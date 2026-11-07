from decimal import Decimal
from datetime import date, timedelta
import logging
import uuid

from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import (
	Avg,
	Case,
	Count,
	DecimalField,
	Exists,
	ExpressionWrapper,
	F,
	IntegerField,
	OuterRef,
	Prefetch,
	Q,
	Sum,
	Value,
	When,
)
from django.db.models.functions import Coalesce
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from .tracking import build_tracking
from .geo import (
	DELIVERY_RADIUS_KM,
	distance_to_vendor,
	format_distance,
	is_far_away,
	parse_coordinates,
)
from .forms import (
	CustomerProfileForm,
	CustomerUserProfileForm,
	DeliveryAddressForm,
	ProductForm,
	ProductSearchForm,
	ReviewForm,
	StockUpdateForm,
	VendorAuthenticationForm,
	VendorRegistrationForm,
	VendorSearchForm,
)
from .models import (
	CartItem,
	Category,
	Customer,
	DeliveryAddress,
	DeliveryWindow,
	Order,
	OrderItem,
	OrderStatusHistory,
	Notification,
	Product,
	RecentlyViewedItem,
	Review,
	Vendor,
	VendorOrder,
	WishlistItem,
)
from .payments import (
	PaymentConfigurationError,
	PaymentGatewayError,
	PaymentStockError,
	WebhookPayloadError,
	WebhookSignatureError,
	amount_in_paise,
	cancel_razorpay_order,
	ensure_razorpay_order,
	expire_pending_razorpay_orders,
	order_total,
	process_razorpay_webhook,
	record_razorpay_failure,
	razorpay_test_credentials,
	verify_razorpay_payment,
)
from .order_lifecycle import (
	add_progress_to_order,
	notify_vendors_new_order,
	record_order_placed,
	transition_vendor_order,
)


logger = logging.getLogger(__name__)


class CheckoutValidationError(Exception):
	pass


# Create your views here.
RECENTLY_VIEWED_SESSION_KEY = 'recently_viewed_product_ids'
RECENTLY_VIEWED_LIMIT = 12


def home(request):
	categories = (
		Category.objects.annotate(
			listing_count=Count(
				'product',
				filter=Q(product__vendor__isnull=False),
			),
		)
		.filter(listing_count__gt=0)
		.order_by('name')
	)
	featured_products = (
		Product.objects.filter(vendor__isnull=False)
		.select_related('category', 'vendor')
		.order_by('-stock', '-id')[:8]
	)
	available_products = Product.objects.filter(
		vendor__isnull=False,
		stock__gt=0,
	)
	return render(request, 'market/home.html', {
		'categories': categories,
		'featured_products': featured_products,
		'recently_viewed_products': _recently_viewed_products(request),
		'wishlist_product_ids': _wishlist_product_ids(request),
		'product_count': available_products.count(),
		'seller_count': available_products.values('vendor_id').distinct().count(),
	})


def _recently_viewed_customer(request):
	if not request.user.is_authenticated:
		return None
	return Customer.objects.filter(user_id=request.user.id).first()


def _guest_recently_viewed_ids(request):
	raw_ids = request.session.get(RECENTLY_VIEWED_SESSION_KEY, [])
	if not isinstance(raw_ids, list):
		return []
	product_ids = []
	seen = set()
	for raw_id in raw_ids:
		if not isinstance(raw_id, (str, int)) or not str(raw_id).isdecimal():
			continue
		product_id = int(raw_id)
		if product_id > 0 and product_id not in seen:
			product_ids.append(product_id)
			seen.add(product_id)
		if len(product_ids) == RECENTLY_VIEWED_LIMIT:
			break
	return product_ids


def _record_recently_viewed(request, product):
	customer = _recently_viewed_customer(request)
	if customer is not None:
		RecentlyViewedItem.objects.update_or_create(
			customer=customer,
			product=product,
			defaults={'viewed_at': timezone.now()},
		)
		overflow_ids = list(
			RecentlyViewedItem.objects.filter(customer=customer)
			.order_by('-viewed_at', '-id')
			.values_list('id', flat=True)[RECENTLY_VIEWED_LIMIT:]
		)
		if overflow_ids:
			RecentlyViewedItem.objects.filter(id__in=overflow_ids).delete()
		return
	if request.user.is_authenticated:
		return

	product_ids = _guest_recently_viewed_ids(request)
	product_ids = [product_id for product_id in product_ids if product_id != product.id]
	product_ids.insert(0, product.id)
	request.session[RECENTLY_VIEWED_SESSION_KEY] = product_ids[:RECENTLY_VIEWED_LIMIT]


def _recently_viewed_products(request):
	customer = _recently_viewed_customer(request)
	if customer is not None:
		items = (
			RecentlyViewedItem.objects.filter(
				customer=customer,
				product__vendor__isnull=False,
			)
			.select_related('product__category', 'product__vendor')
			.order_by('-viewed_at', '-id')[:RECENTLY_VIEWED_LIMIT]
		)
		return [item.product for item in items]
	if request.user.is_authenticated:
		return []

	product_ids = _guest_recently_viewed_ids(request)
	if not product_ids:
		return []
	products_by_id = Product.objects.filter(
		id__in=product_ids,
		vendor__isnull=False,
	).select_related('category', 'vendor').in_bulk()
	valid_ids = [product_id for product_id in product_ids if product_id in products_by_id]
	if valid_ids != product_ids:
		request.session[RECENTLY_VIEWED_SESSION_KEY] = valid_ids
	return [products_by_id[product_id] for product_id in valid_ids]


def _wishlist_product_ids(request):
	if not request.user.is_authenticated:
		return set()
	return set(
		WishlistItem.objects.filter(
			customer__user_id=request.user.id,
		).values_list('product_id', flat=True)
	)


def product_list(request):
	form = ProductSearchForm(request.GET or None)
	form.fields['name'].widget.attrs.update({
		'placeholder': 'Search for vegetables, fruits and more...',
		'autocomplete': 'off',
		'list': 'marketplace-search-suggestions',
	})
	products = Product.objects.filter(
		vendor__isnull=False,
	).select_related('category', 'vendor').order_by('name', 'vendor__name')
	categories = Category.objects.order_by('name')
	raw_category_id = request.GET.get('category', '')
	selected_category = None
	if raw_category_id.isdecimal():
		selected_category = categories.filter(id=int(raw_category_id)).first()
	if selected_category is not None:
		products = products.filter(category=selected_category)

	searched = False
	if form.is_valid() and form.cleaned_data['name']:
		search_term = form.cleaned_data['name']
		products = products.filter(
			Q(name__icontains=search_term)
			| Q(description__icontains=search_term)
			| Q(category__name__icontains=search_term)
			| Q(vendor__name__icontains=search_term)
			| Q(vendor__shop_name__icontains=search_term)
		)
		searched = True
	availability = request.GET.get('availability', '')
	if availability == 'in_stock':
		products = products.filter(stock__gt=0)

	sort_options = {
		'relevance': ('name', 'vendor__name', 'id'),
		'price_asc': ('price', 'name', 'vendor__name', 'id'),
		'price_desc': ('-price', 'name', 'vendor__name', 'id'),
		'name_asc': ('name', 'vendor__name', 'id'),
		'name_desc': ('-name', 'vendor__name', 'id'),
		'availability': ('availability_rank', '-stock', 'name', 'id'),
		'newest': ('-id',),
	}
	sort = request.GET.get('sort', 'relevance')
	if sort not in sort_options:
		sort = 'relevance'
	if sort == 'availability':
		products = products.annotate(
			availability_rank=Case(
				When(stock__gt=0, then=0),
				default=1,
				output_field=IntegerField(),
			),
		)
	products = products.order_by(*sort_options[sort])

	active_filters = []
	if selected_category is not None:
		active_filters.append(f'Category: {selected_category.name}')
	if searched:
		active_filters.append(f'Search: {form.cleaned_data["name"]}')
	if availability == 'in_stock':
		active_filters.append('In stock only')
	if sort != 'relevance':
		active_filters.append(dict(
			price_asc='Price: Low to High',
			price_desc='Price: High to Low',
			name_asc='Name: A to Z',
			name_desc='Name: Z to A',
			availability='Availability',
			newest='Newest',
		)[sort])
	wishlist_product_ids = _wishlist_product_ids(request)
	suggestion_products = Product.objects.filter(vendor__isnull=False)
	if selected_category is not None:
		suggestion_products = suggestion_products.filter(category=selected_category)
	if availability == 'in_stock':
		suggestion_products = suggestion_products.filter(stock__gt=0)
	search_suggestions = list(
		suggestion_products.order_by('name')
		.values_list('name', flat=True)
		.distinct()[:8]
	)
	return render(request, 'market/product_list.html', {
		'form': form,
		'products': products,
		'categories': categories,
		'selected_category': selected_category,
		'searched': searched,
		'selected_availability': availability if availability == 'in_stock' else '',
		'selected_sort': sort,
		'active_filters': active_filters,
		'recently_viewed_products': _recently_viewed_products(request),
		'wishlist_product_ids': wishlist_product_ids,
		'search_suggestions': search_suggestions,
	})

def product_detail(request, product_id):
	product = get_object_or_404(
		Product.objects.filter(vendor__isnull=False)
		.select_related('category', 'vendor'),
		id=product_id,
	)
	_record_recently_viewed(request, product)
	recommendations = (
		Product.objects.filter(
			category_id=product.category_id,
			vendor__isnull=False,
		)
		.exclude(id=product.id)
		.select_related('category', 'vendor')
		.order_by('-stock', 'name', 'vendor__name', 'id')[:8]
	)
	return render(request, 'market/product_detail.html', {
		'product': product,
		'recommendations': recommendations,
	})

@login_required(login_url='login')
def wishlist(request):
	customer = get_object_or_404(Customer, user=request.user)
	wishlist_items = (
		WishlistItem.objects.filter(customer=customer)
		.select_related('product__category', 'product__vendor')
	)
	return render(request, 'market/wishlist.html', {
		'wishlist_items': wishlist_items,
	})

@login_required(login_url='login')
@require_POST
def add_to_wishlist(request, product_id):
	customer = get_object_or_404(Customer, user=request.user)
	product = get_object_or_404(Product, id=product_id)
	wishlist_item, created = WishlistItem.objects.get_or_create(
		customer=customer,
		product=product,
	)
	if created:
		messages.success(request, f'{product.name} was added to your wishlist.')
	else:
		messages.info(request, f'{product.name} is already in your wishlist.')
	next_url = request.POST.get('next', '')
	if next_url and url_has_allowed_host_and_scheme(
		next_url,
		allowed_hosts={request.get_host()},
		require_https=request.is_secure(),
	):
		return redirect(next_url)
	return redirect('wishlist')

@login_required(login_url='login')
@require_POST
def remove_from_wishlist(request, product_id):
	customer = get_object_or_404(Customer, user=request.user)
	wishlist_item = get_object_or_404(
		WishlistItem,
		customer=customer,
		product_id=product_id,
	)
	product_name = wishlist_item.product.name
	wishlist_item.delete()
	messages.success(request, f'{product_name} was removed from your wishlist.')
	next_url = request.POST.get('next', '')
	if next_url and url_has_allowed_host_and_scheme(
		next_url,
		allowed_hosts={request.get_host()},
		require_https=request.is_secure(),
	):
		return redirect(next_url)
	return redirect('wishlist')

def vendor_search(request):
	form = VendorSearchForm(request.GET or None)
	vendors = Vendor.objects.none()
	if form.is_valid() and form.cleaned_data['name']:
		vendors = (
			Vendor.objects.filter(name__icontains=form.cleaned_data['name'])
			.prefetch_related('products__category')
			.order_by('name')
		)
	return render(request, 'market/vendor_search.html', {
		'form': form,
		'vendors': vendors,
		'searched': bool(form.cleaned_data.get('name')) if form.is_valid() else False,
	})

def vendor_register(request):
	if request.user.is_authenticated:
		if Vendor.objects.filter(user=request.user).exists():
			return redirect('vendor_dashboard')
		messages.info(request, 'Log out of your customer account before creating a vendor account.')
		return redirect('product_list')

	form = VendorRegistrationForm(request.POST or None)
	if request.method == 'POST' and form.is_valid():
		with transaction.atomic():
			user = User.objects.create_user(
				username=form.cleaned_data['username'],
				email=form.cleaned_data['email'],
				password=form.cleaned_data['password'],
			)
			Vendor.objects.create(
				user=user,
				name=form.cleaned_data['vendor_name'],
				shop_name=form.cleaned_data['shop_name'],
				shop_address=form.cleaned_data['shop_address'],
				business_registration=form.cleaned_data['business_registration'],
				description=form.cleaned_data['description'],
				email=form.cleaned_data['email'],
				phone=form.cleaned_data['phone'],
				latitude=form.cleaned_data.get('latitude'),
				longitude=form.cleaned_data.get('longitude'),
			)
		login(request, user)
		return redirect('vendor_dashboard')
	return render(request, 'market/vendor_register.html', {'form': form})

class VendorLoginView(LoginView):
	authentication_form = VendorAuthenticationForm
	template_name = 'market/vendor_login.html'
	next_page = 'vendor_dashboard'

LOW_STOCK_THRESHOLD = 5


@login_required(login_url='vendor_login')
def vendor_dashboard(request):
	vendor = get_object_or_404(Vendor, user=request.user)
	products = (
		vendor.products.select_related('category')
		.annotate(has_orders=Exists(OrderItem.objects.filter(product_id=OuterRef('pk'))))
		.order_by('-id')
	)
	product_stats = vendor.products.aggregate(
		total=Count('id'),
		out_of_stock=Count('id', filter=Q(stock__lte=0)),
		low_stock=Count(
			'id',
			filter=Q(stock__gt=0, stock__lte=LOW_STOCK_THRESHOLD),
		),
	)
	vendor_orders = VendorOrder.objects.filter(vendor=vendor)
	delivered_items_value = ExpressionWrapper(
		F('items__unit_price') * F('items__quantity'),
		output_field=DecimalField(max_digits=12, decimal_places=2),
	)
	delivered_stats = vendor_orders.filter(
		status=VendorOrder.Status.DELIVERED,
	).aggregate(
		sales=Coalesce(
			Sum(delivered_items_value),
			Value(Decimal('0.00')),
			output_field=DecimalField(max_digits=12, decimal_places=2),
		),
		products_sold=Coalesce(Sum('items__quantity'), Value(0)),
	)
	order_stats = vendor_orders.aggregate(
		total=Count('id'),
		pending=Count('id', filter=Q(status=VendorOrder.Status.PENDING)),
		in_progress=Count(
			'id',
			filter=Q(status__in=[
				VendorOrder.Status.CONFIRMED,
				VendorOrder.Status.PREPARING,
				VendorOrder.Status.READY_FOR_DELIVERY,
				VendorOrder.Status.OUT_FOR_DELIVERY,
			]),
		),
		delivered=Count('id', filter=Q(status=VendorOrder.Status.DELIVERED)),
		rejected=Count('id', filter=Q(status=VendorOrder.Status.REJECTED)),
	)
	recent_orders = (
		vendor_orders.select_related('order')
		.annotate(
			vendor_item_count=Coalesce(Sum('items__quantity'), Value(0)),
			vendor_subtotal=Coalesce(
				Sum(delivered_items_value),
				Value(Decimal('0.00')),
				output_field=DecimalField(max_digits=12, decimal_places=2),
			),
		)
		.order_by('-order__order_date', '-order_id')[:5]
	)
	return render(request, 'market/vendor_dashboard.html', {
		'vendor': vendor,
		'products': products,
		'product_stats': product_stats,
		'order_stats': order_stats,
		'sales_stats': delivered_stats,
		'recent_orders': recent_orders,
		'low_stock_threshold': LOW_STOCK_THRESHOLD,
	})

@login_required(login_url='vendor_login')
def add_vegetable(request):
	vendor = get_object_or_404(Vendor, user=request.user)
	form = ProductForm(request.POST or None, request.FILES or None)
	if request.method == 'POST' and form.is_valid():
		product = form.save(commit=False)
		product.vendor = vendor
		product.save()
		messages.success(request, f'{product.name} was added to your vegetables.')
		return redirect('vendor_dashboard')
	return render(request, 'market/add_vegetable.html', {
		'form': form,
		'vendor': vendor,
	})

@login_required(login_url='vendor_login')
def edit_vegetable(request, product_id):
	vendor = get_object_or_404(Vendor, user=request.user)
	product = get_object_or_404(vendor.products, id=product_id)
	form = ProductForm(
		request.POST or None,
		request.FILES or None,
		instance=product,
	)
	if request.method == 'POST' and form.is_valid():
		form.save()
		messages.success(request, f'{product.name} was updated.')
		return redirect('vendor_dashboard')
	return render(request, 'market/edit_vegetable.html', {
		'form': form,
		'product': product,
		'vendor': vendor,
	})

@login_required(login_url='vendor_login')
@require_POST
def update_vegetable_stock(request, product_id):
	vendor = get_object_or_404(Vendor, user=request.user)
	form = StockUpdateForm(request.POST)
	if not form.is_valid():
		messages.error(request, 'Enter a valid stock quantity of zero or more.')
		return redirect('vendor_dashboard')

	with transaction.atomic():
		product = get_object_or_404(
			vendor.products.select_for_update(),
			id=product_id,
		)
		product.stock = form.cleaned_data['stock']
		product.save(update_fields=['stock'])

	if product.stock > 0:
		messages.success(request, f'{product.name} is now available with stock {product.stock}.')
	else:
		messages.success(request, f'{product.name} is now out of stock.')
	return redirect('vendor_dashboard')

@login_required(login_url='vendor_login')
def delete_vegetable(request, product_id):
	vendor = get_object_or_404(Vendor, user=request.user)
	product = get_object_or_404(vendor.products, id=product_id)
	if OrderItem.objects.filter(product=product).exists():
		messages.error(
			request,
			'This vegetable cannot be deleted because it has appeared in an order.',
		)
		return redirect('vendor_dashboard')

	if request.method == 'POST':
		with transaction.atomic():
			product = get_object_or_404(
				vendor.products.select_for_update(),
				id=product_id,
			)
			if OrderItem.objects.filter(product=product).exists():
				messages.error(
					request,
					'This vegetable cannot be deleted because it has appeared in an order.',
				)
				return redirect('vendor_dashboard')
			product.delete()
		messages.success(request, 'Vegetable deleted.')
		return redirect('vendor_dashboard')

	return render(request, 'market/delete_vegetable.html', {
		'product': product,
		'vendor': vendor,
	})

@login_required(login_url='vendor_login')
def vendor_order_list(request):
	vendor = get_object_or_404(Vendor, user=request.user)
	vendor_items = (
		OrderItem.objects.filter(vendor_order__vendor=vendor)
		.select_related('product', 'product__category')
	)
	vendor_item_amount = ExpressionWrapper(
		F('items__unit_price') * F('items__quantity'),
		output_field=DecimalField(max_digits=12, decimal_places=2),
	)
	vendor_orders = (
		VendorOrder.objects.filter(vendor=vendor)
		.select_related('order', 'order__customer__user')
		.prefetch_related(Prefetch('items', queryset=vendor_items))
		.annotate(
			vendor_item_count=Coalesce(Sum('items__quantity'), Value(0)),
			vendor_subtotal=Coalesce(
				Sum(vendor_item_amount),
				Value(Decimal('0.00')),
				output_field=DecimalField(max_digits=12, decimal_places=2),
			),
		)
		.order_by('-order__order_date', '-order_id')
	)
	return render(request, 'market/vendor_order_list.html', {
		'vendor': vendor,
		'vendor_orders': vendor_orders,
	})

@login_required(login_url='vendor_login')
def vendor_order_detail(request, order_id):
	vendor = get_object_or_404(Vendor, user=request.user)
	vendor_items = (
		OrderItem.objects.filter(vendor_order__vendor=vendor)
		.select_related('product', 'product__category')
	)
	vendor_order = get_object_or_404(
		VendorOrder.objects.filter(vendor=vendor)
		.select_related('order', 'order__customer__user')
		.prefetch_related(Prefetch('items', queryset=vendor_items)),
		order_id=order_id,
	)
	items = list(vendor_order.items.all())
	vendor_subtotal = sum(
		(item.unit_price * item.quantity for item in items),
		Decimal('0.00'),
	)
	return render(request, 'market/vendor_order_detail.html', {
		'vendor': vendor,
		'vendor_order': vendor_order,
		'order': vendor_order.order,
		'items': items,
		'vendor_subtotal': vendor_subtotal,
		'status_history': vendor_order.status_history.select_related('actor'),
	})

@login_required(login_url='vendor_login')
@require_POST
def accept_vendor_order(request, vendor_order_id):
	return _transition_vendor_order(
		request,
		vendor_order_id,
		{VendorOrder.Status.PENDING: VendorOrder.Status.CONFIRMED},
	)

@login_required(login_url='vendor_login')
@require_POST
def advance_vendor_order(request, vendor_order_id):
	return _transition_vendor_order(
		request,
		vendor_order_id,
		{
			VendorOrder.Status.PENDING: VendorOrder.Status.CONFIRMED,
			VendorOrder.Status.CONFIRMED: VendorOrder.Status.PREPARING,
			VendorOrder.Status.PREPARING: VendorOrder.Status.READY_FOR_DELIVERY,
		},
	)

def _transition_vendor_order(request, vendor_order_id, transitions):
	vendor = get_object_or_404(Vendor, user=request.user)
	vendor_order = get_object_or_404(
		VendorOrder.objects.select_related('order'),
		id=vendor_order_id,
		vendor=vendor,
	)
	next_status = transitions.get(vendor_order.status)
	if next_status is None:
		messages.error(
			request,
			'This vendor order cannot be advanced from its current status.',
		)
		return redirect('vendor_order_detail', order_id=vendor_order.order_id)
	vendor_order, changed = transition_vendor_order(
		vendor_order.id,
		next_status,
		actor=request.user,
	)
	if not changed:
		messages.error(
			request,
			'This vendor order changed before your update could be applied.',
		)
		return redirect('vendor_order_detail', order_id=vendor_order.order_id)
	messages.success(
		request,
		f'Your items in order #{vendor_order.order_id} are now {vendor_order.get_status_display().lower()}.',
	)
	return redirect('vendor_order_detail', order_id=vendor_order.order_id)

@login_required(login_url='vendor_login')
@require_http_methods(['GET', 'POST'])
def reject_vendor_order(request, vendor_order_id):
	vendor = get_object_or_404(Vendor, user=request.user)
	vendor_order = get_object_or_404(
		VendorOrder.objects.select_related('order').prefetch_related(
			Prefetch(
				'items',
				queryset=OrderItem.objects.filter(vendor_order__vendor=vendor)
				.select_related('product'),
			)
		),
		id=vendor_order_id,
		vendor=vendor,
	)
	if vendor_order.status != VendorOrder.Status.PENDING:
		messages.error(request, 'Only pending vendor orders can be rejected.')
		return redirect('vendor_order_detail', order_id=vendor_order.order_id)

	if request.method == 'POST':
		with transaction.atomic():
			vendor_order = get_object_or_404(
				VendorOrder.objects.select_for_update(),
				id=vendor_order_id,
				vendor=vendor,
			)
			if vendor_order.status != VendorOrder.Status.PENDING:
				messages.error(request, 'Only pending vendor orders can be rejected.')
				return redirect('vendor_order_detail', order_id=vendor_order.order_id)

			items = list(vendor_order.items.values_list('product_id', 'quantity'))
			products = {
				product.id: product
				for product in Product.objects.select_for_update().filter(
					id__in=[product_id for product_id, _ in items]
				)
			}
			if len(products) != len({product_id for product_id, _ in items}):
				messages.error(request, 'A product in this vendor order is no longer available.')
				return redirect('vendor_order_detail', order_id=vendor_order.order_id)
			vendor_order, changed = transition_vendor_order(
				vendor_order.id,
				VendorOrder.Status.REJECTED,
				actor=request.user,
			)
			if not changed:
				messages.error(request, 'This vendor order can no longer be rejected.')
				return redirect('vendor_order_detail', order_id=vendor_order.order_id)
			for product_id, quantity in items:
				product = products[product_id]
				product.stock = F('stock') + quantity
				product.save(update_fields=['stock'])

		messages.success(
			request,
			f'Your items in order #{vendor_order.order_id} were rejected and returned to stock.',
		)
		return redirect('vendor_order_detail', order_id=vendor_order.order_id)

	return render(request, 'market/reject_vendor_order.html', {
		'vendor_order': vendor_order,
		'order': vendor_order.order,
		'vendor': vendor,
		'items': vendor_order.items.all(),
	})

def _normalize_cart(cart):
	if not isinstance(cart, dict):
		return {}

	valid_cart = {}
	for product_id, quantity in cart.items():
		try:
			product_id = str(int(product_id))
			quantity = int(quantity)
		except (TypeError, ValueError):
			continue
		if int(product_id) > 0 and quantity > 0:
			valid_cart[product_id] = quantity
	return valid_cart

def _get_cart(request):
	if request.user.is_authenticated:
		cart = dict(
			CartItem.objects.filter(user=request.user)
			.values_list('product_id', 'quantity')
		)
		cart = {str(product_id): quantity for product_id, quantity in cart.items()}
	else:
		cart = request.session.get('cart', {})
	return _normalize_cart(cart)

def _save_cart(request, cart):
	valid_cart = _normalize_cart(cart)

	if not request.user.is_authenticated:
		request.session['cart'] = valid_cart
		return

	_save_cart_for_user(request.user, valid_cart)

def _merge_guest_cart(request, user, guest_cart=None):
	if guest_cart is None:
		guest_cart = request.session.get('cart', {})
	if not isinstance(guest_cart, dict):
		guest_cart = {}
		cart_cleaned = True
	else:
		normalized_guest_cart = _normalize_cart(guest_cart)
		cart_cleaned = len(normalized_guest_cart) != len(guest_cart)
		guest_cart = normalized_guest_cart

	cart_limited = False
	with transaction.atomic():
		saved_items = list(
			CartItem.objects.select_for_update()
			.filter(user=user)
			.order_by('product_id')
		)
		saved_cart = {
			str(item.product_id): item.quantity
			for item in saved_items
		}
		product_ids = {
			int(product_id)
			for product_id in (*saved_cart.keys(), *guest_cart.keys())
		}
		products = {
			str(product.id): product
			for product in Product.objects.select_for_update()
			.filter(id__in=product_ids)
			.order_by('id')
		}
		cart = {}
		for product_id, quantity in saved_cart.items():
			product = products.get(product_id)
			if product is None or product.stock < 1:
				cart_cleaned = True
				continue
			if quantity > product.stock:
				quantity = product.stock
				cart_limited = True
			cart[product_id] = quantity

		for product_id, quantity in guest_cart.items():
			product = products.get(product_id)
			if product is None or product.stock < 1:
				cart_cleaned = True
				continue
			combined_quantity = cart.get(product_id, 0) + quantity
			if combined_quantity > product.stock:
				combined_quantity = product.stock
				cart_limited = True
			cart[product_id] = combined_quantity

		_save_cart_for_user(user, cart)
	request.session.pop('cart', None)
	if cart_cleaned:
		messages.warning(
			request,
			'Unavailable or invalid items were removed from your cart.',
		)
	if cart_limited:
		messages.warning(
			request,
			'Cart quantities were adjusted to current available stock.',
		)

def _save_cart_for_user(user, cart):
	valid_cart = _normalize_cart(cart)
	with transaction.atomic():
		cart_items = CartItem.objects.filter(user=user)
		product_ids = [int(product_id) for product_id in valid_cart]
		cart_items.exclude(product_id__in=product_ids).delete()
		for product_id, quantity in valid_cart.items():
			cart_items.update_or_create(
				user=user,
				product_id=int(product_id),
				defaults={'quantity': quantity},
			)

def _cart_context(request):
	if request.user.is_authenticated:
		cart = {
			str(product_id): quantity
			for product_id, quantity in CartItem.objects.filter(
				user=request.user,
			).values_list('product_id', 'quantity')
		}
	else:
		cart = request.session.get('cart', {})
	if not isinstance(cart, dict):
		cart = {'invalid-cart-data': None}

	parsed_entries = []
	product_ids = set()
	for raw_product_id, raw_quantity in cart.items():
		product_id = str(raw_product_id)
		try:
			if isinstance(raw_product_id, bool) or not isinstance(
				raw_product_id,
				(int, str),
			):
				raise ValueError
			parsed_product_id = int(raw_product_id)
			if parsed_product_id < 1:
				raise ValueError
		except (TypeError, ValueError):
			parsed_product_id = None
		try:
			if isinstance(raw_quantity, bool) or not isinstance(
				raw_quantity,
				(int, str),
			):
				raise ValueError
			if isinstance(raw_quantity, str) and not raw_quantity.strip().isdecimal():
				raise ValueError
			quantity = int(raw_quantity)
			if quantity < 1:
				raise ValueError
		except (TypeError, ValueError):
			quantity = None
		if parsed_product_id is not None:
			product_ids.add(parsed_product_id)
		parsed_entries.append((
			product_id,
			parsed_product_id,
			quantity,
			raw_quantity,
		))

	products = Product.objects.select_related(
		'category',
		'vendor',
	).in_bulk(product_ids)
	items = []
	total = Decimal('0.00')
	all_items_available = True
	for product_id, parsed_product_id, quantity, raw_quantity in parsed_entries:
		product = products.get(parsed_product_id)
		if parsed_product_id is None:
			all_items_available = False
			items.append({
				'product': None,
				'product_id': product_id,
				'quantity': raw_quantity,
				'quantity_is_valid': False,
				'line_total': Decimal('0.00'),
				'is_available': False,
				'availability_message': 'This cart item has an invalid product listing.',
			})
			continue
		if product is None:
			all_items_available = False
			items.append({
				'product': None,
				'product_id': product_id,
				'quantity': raw_quantity,
				'quantity_is_valid': False,
				'line_total': Decimal('0.00'),
				'is_available': False,
				'availability_message': 'This product listing is no longer available.',
			})
			continue
		if quantity is None:
			all_items_available = False
			items.append({
				'product': product,
				'product_id': product.id,
				'quantity': raw_quantity,
				'quantity_is_valid': False,
				'line_total': Decimal('0.00'),
				'is_available': False,
				'availability_message': 'This cart item has an invalid quantity. Enter a positive whole number or remove it.',
			})
			continue
		line_total = product.price * quantity
		total += line_total
		is_available = product.stock > 0 and quantity <= product.stock
		if not is_available:
			all_items_available = False
		if product.stock <= 0:
			availability_message = 'Out of stock.'
		elif quantity > product.stock:
			availability_message = (
				f'Only {product.stock} {product.units} currently available. '
				'Update the quantity or remove this item before checkout.'
			)
		else:
			availability_message = ''
		items.append({
			'product': product,
			'product_id': product.id,
			'quantity': quantity,
			'quantity_is_valid': True,
			'line_total': line_total,
			'is_available': is_available,
			'availability_message': availability_message,
		})

	vendor_groups_by_key = {}
	for item in items:
		product = item['product']
		if product is None:
			group_key = 'unavailable'
			vendor = None
			vendor_name = 'Unavailable listings'
			shop_name = ''
		elif product.vendor is None:
			group_key = 'unassigned'
			vendor = None
			vendor_name = 'Marketplace listings'
			shop_name = ''
		else:
			group_key = f'vendor-{product.vendor_id}'
			vendor = product.vendor
			vendor_name = product.vendor.name
			shop_name = product.vendor.shop_name

		group = vendor_groups_by_key.setdefault(group_key, {
			'key': group_key,
			'vendor': vendor,
			'vendor_name': vendor_name,
			'shop_name': shop_name,
			'items': [],
			'subtotal': Decimal('0.00'),
		})
		group['items'].append(item)
		group['subtotal'] += item['line_total']

	return {
		'items': items,
		'vendor_groups': list(vendor_groups_by_key.values()),
		'total': total,
		'all_items_available': all_items_available,
	}

def _checkout_address_session_keys():
	return (
		'checkout_delivery_address_customer_id',
		'checkout_delivery_address_id',
	)

def _checkout_delivery_slot_session_keys():
	return (
		'checkout_delivery_slot_customer_id',
		'checkout_delivery_slot_date',
		'checkout_delivery_window_id',
	)

def _checkout_token_session_keys():
	return 'checkout_order_token_customer_id', 'checkout_order_token'

def _ensure_checkout_token(request, customer):
	customer_key, token_key = _checkout_token_session_keys()
	token = request.session.get(token_key)
	if request.session.get(customer_key) != customer.id:
		token = None
	else:
		try:
			token = uuid.UUID(str(token))
		except (TypeError, ValueError, AttributeError):
			token = None
	if token is None:
		token = uuid.uuid4()
	request.session[customer_key] = customer.id
	request.session[token_key] = str(token)
	return str(token)

def _checkout_page_context(request, context, address_form=None):
	customer = Customer.objects.filter(user=request.user).first()
	addresses = (
		DeliveryAddress.objects.filter(customer=customer).order_by('-is_default', 'id')
		if customer is not None
		else DeliveryAddress.objects.none()
	)
	selected_address = None
	if customer is not None:
		customer_session_key, address_session_key = _checkout_address_session_keys()
		session_customer_id = request.session.get(customer_session_key)
		session_address_id = request.session.get(address_session_key)
		if session_customer_id == customer.id and session_address_id is not None:
			selected_address = addresses.filter(id=session_address_id).first()
		if selected_address is None:
			request.session.pop(customer_session_key, None)
			request.session.pop(address_session_key, None)
			selected_address = addresses.filter(is_default=True).first()
			if selected_address is not None:
				request.session[customer_session_key] = customer.id
				request.session[address_session_key] = selected_address.id

	if address_form is None:
		initial = {'label': 'Home'}
		if customer is not None:
			initial.update({
				'recipient_name': (
					customer.user.get_full_name().strip()
					or customer.user.get_username()
				),
				'phone': customer.phone,
			})
		address_form = DeliveryAddressForm(initial=initial)

	context.update({
		'delivery_addresses': addresses,
		'selected_delivery_address': selected_address,
		'new_address_form': address_form,
	})
	return _checkout_slot_page_context(request, context)

def _orders_consuming_delivery_capacity(queryset):
	return queryset.exclude(
		Q(status__in=[
			Order.Status.CANCELLED,
			Order.Status.REJECTED,
		])
		| Q(
			payment_method=Order.PaymentMethod.ONLINE,
			payment_status__in=[
				Order.PaymentStatus.FAILED,
				Order.PaymentStatus.CANCELLED,
				Order.PaymentStatus.EXPIRED,
			],
		)
	)

def _checkout_slot_page_context(request, context):
	today = timezone.localdate()
	current_time = timezone.localtime().time()
	last_delivery_date = today + timedelta(days=6)
	windows = list(DeliveryWindow.objects.filter(is_active=True))
	booked_counts = {
		(row['delivery_slot_date'], row['delivery_window_id']): row['order_count']
		for row in _orders_consuming_delivery_capacity(
			Order.objects.filter(
				delivery_slot_date__range=(today, last_delivery_date),
				delivery_window__in=windows,
			)
		)
		.values('delivery_slot_date', 'delivery_window_id')
		.annotate(order_count=Count('id'))
	}

	delivery_slots = []
	for day_offset in range(7):
		delivery_date = today + timedelta(days=day_offset)
		day_slots = []
		for window in windows:
			booked = booked_counts.get((delivery_date, window.id), 0)
			is_in_future = (
				delivery_date > today
				or window.start_time > current_time
			)
			day_slots.append({
				'date': delivery_date,
				'window': window,
				'booked': booked,
				'remaining': max(window.capacity - booked, 0),
				'is_available': booked < window.capacity and is_in_future,
			})
		delivery_slots.append({
			'date': delivery_date,
			'slots': day_slots,
		})

	selected_slot = None
	customer = Customer.objects.filter(user=request.user).first()
	if customer is not None:
		customer_key, date_key, window_key = _checkout_delivery_slot_session_keys()
		if request.session.get(customer_key) == customer.id:
			try:
				selected_date = date.fromisoformat(request.session.get(date_key, ''))
				selected_window_id = int(request.session.get(window_key, ''))
			except (TypeError, ValueError):
				selected_date = None
				selected_window_id = None
			if (
				selected_date is not None
				and today <= selected_date <= last_delivery_date
				and selected_window_id is not None
			):
				for day in delivery_slots:
					for slot in day['slots']:
						if (
							slot['date'] == selected_date
							and slot['window'].id == selected_window_id
							and slot['is_available']
						):
							selected_slot = slot
							break
					if selected_slot is not None:
						break
		if selected_slot is None:
			request.session.pop(customer_key, None)
			request.session.pop(date_key, None)
			request.session.pop(window_key, None)

	context.update({
		'delivery_windows_configured': bool(windows),
		'delivery_slots': delivery_slots,
		'selected_delivery_slot': selected_slot,
	})
	return context

def _resolve_checkout_delivery_address(request, customer):
	customer_session_key, address_session_key = _checkout_address_session_keys()
	session_customer_id = request.session.get(customer_session_key)
	session_address_id = request.session.get(address_session_key)
	if session_customer_id is not None or session_address_id is not None:
		if session_customer_id != customer.id or session_address_id is None:
			return None, 'Your selected delivery address is no longer valid. Choose it again.'
		selected_address = DeliveryAddress.objects.select_for_update().filter(
			id=session_address_id,
			customer=customer,
		).first()
		if selected_address is None:
			return None, 'Your selected delivery address is no longer available. Choose another address.'
		return selected_address, None

	return (
		DeliveryAddress.objects.select_for_update()
		.filter(customer=customer, is_default=True)
		.first(),
		None,
	)

def _checkout_delivery_snapshot(customer, selected_address):
	if selected_address is not None:
		return {
			'delivery_address_label': selected_address.label,
			'delivery_name': selected_address.recipient_name,
			'delivery_phone': selected_address.phone,
			'delivery_address_line_1': selected_address.address_line_1,
			'delivery_address_line_2': selected_address.address_line_2,
			'delivery_city': selected_address.city,
			'delivery_state': selected_address.state,
			'delivery_postal_code': selected_address.postal_code,
		}

	if not customer.address.strip() or not customer.phone.strip():
		raise ValueError('Complete your customer profile delivery address and phone before checkout.')
	full_name = customer.user.get_full_name().strip()
	return {
		'delivery_address_label': 'Profile address',
		'delivery_name': full_name or customer.user.username,
		'delivery_phone': customer.phone,
		'delivery_address_line_1': customer.address,
		'delivery_address_line_2': '',
		'delivery_city': '',
		'delivery_state': '',
		'delivery_postal_code': '',
	}

def _resolve_checkout_delivery_slot(request, customer):
	customer_key, date_key, window_key = _checkout_delivery_slot_session_keys()
	if request.session.get(customer_key) != customer.id:
		return None, None, 'Choose an available delivery slot.'
	try:
		delivery_date = date.fromisoformat(request.session.get(date_key, ''))
		window_id = int(request.session.get(window_key, ''))
	except (TypeError, ValueError):
		return None, None, 'Choose an available delivery slot.'

	today = timezone.localdate()
	if not today <= delivery_date <= today + timedelta(days=6):
		return None, None, 'That delivery date is no longer available. Choose another slot.'

	delivery_window = (
		DeliveryWindow.objects.select_for_update()
		.filter(id=window_id, is_active=True)
		.first()
	)
	if delivery_window is None:
		return None, None, 'That delivery window is no longer available. Choose another slot.'
	if (
		delivery_date == today
		and delivery_window.start_time <= timezone.localtime().time()
	):
		return None, None, 'That delivery time has passed. Choose another slot.'
	booked_count = _orders_consuming_delivery_capacity(
		Order.objects.filter(
			delivery_slot_date=delivery_date,
			delivery_window=delivery_window,
		),
	).count()
	if booked_count >= delivery_window.capacity:
		return None, None, 'That delivery slot has just filled. Choose another available slot.'
	return delivery_date, delivery_window, None

@require_POST
def add_to_cart(request, product_id):
	product = get_object_or_404(Product, id=product_id)
	try:
		quantity = int(request.POST.get('quantity', '1'))
	except (TypeError, ValueError):
		quantity = 0

	if quantity < 1:
		messages.error(request, 'Enter a quantity greater than zero.')
	elif product.stock < quantity:
		messages.error(request, f'Only {product.stock} {product.units} of {product.name} are available.')
	else:
		cart = _get_cart(request)
		new_quantity = cart.get(str(product.id), 0) + quantity
		if new_quantity > product.stock:
			messages.error(request, f'Only {product.stock} {product.units} of {product.name} are available.')
		else:
			cart[str(product.id)] = new_quantity
			_save_cart(request, cart)
			messages.success(request, f'Added {product.name} to your cart.')
	return redirect('cart')

def cart_view(request):
	context = _cart_context(request)
	location = _current_location(request)
	for group in context['vendor_groups']:
		distance = distance_to_vendor(location, group['vendor'])
		group['distance_km'] = distance
		group['distance_label'] = format_distance(distance)
		group['far_away'] = is_far_away(distance)
	context['delivery_radius_km'] = DELIVERY_RADIUS_KM
	context['has_location'] = location is not None
	return render(request, 'market/cart.html', context)

@require_POST
def update_cart(request):
	product_id = request.POST.get('product_id', '')
	if request.user.is_authenticated:
		try:
			parsed_product_id = int(product_id)
		except (TypeError, ValueError):
			parsed_product_id = None
		cart_item = (
			CartItem.objects.filter(
				user=request.user,
				product_id=parsed_product_id,
			).first()
			if parsed_product_id is not None
			else None
		)
		cart_contains_item = cart_item is not None
		product = cart_item.product if cart_item is not None else None
		session_cart = None
	else:
		session_cart = request.session.get('cart', {})
		if not isinstance(session_cart, dict):
			session_cart = {}
		matched_key = next(
			(key for key in session_cart if str(key) == product_id),
			None,
		)
		cart_contains_item = matched_key is not None
		parsed_product_id = None
		product = None
		if matched_key is not None:
			try:
				parsed_product_id = int(matched_key)
			except (TypeError, ValueError):
				parsed_product_id = None
			if parsed_product_id is not None and parsed_product_id > 0:
				product = Product.objects.filter(id=parsed_product_id).first()

	if not cart_contains_item:
		messages.error(request, 'That product is not in your cart.')
		return redirect('cart')

	if request.POST.get('remove_item') == '1':
		quantity = 0
	elif request.POST.get('adjustment') in {'increase', 'decrease'}:
		raw_quantity = (
			cart_item.quantity
			if request.user.is_authenticated
			else session_cart[matched_key]
		)
		try:
			current_quantity = int(raw_quantity)
			if (
				isinstance(raw_quantity, bool)
				or current_quantity < 1
				or (
					isinstance(raw_quantity, str)
					and not raw_quantity.strip().isdecimal()
				)
			):
				raise ValueError
		except (TypeError, ValueError):
			messages.error(
				request,
				'This cart quantity is invalid. Remove the item and add it again.',
			)
			return redirect('cart')
		if request.POST['adjustment'] == 'increase':
			quantity = current_quantity + 1
		else:
			quantity = max(1, current_quantity - 1)
	else:
		try:
			quantity = int(request.POST.get('quantity', ''))
		except (TypeError, ValueError):
			messages.error(request, 'Enter a valid quantity.')
			return redirect('cart')

	if quantity < 0:
		messages.error(request, 'Quantity cannot be negative.')
		return redirect('cart')
	if quantity == 0:
		if request.user.is_authenticated:
			cart_item.delete()
		else:
			del session_cart[matched_key]
			request.session['cart'] = session_cart
		messages.success(request, 'Item removed from your cart.')
	else:
		if product is None:
			messages.error(
				request,
				'This product listing is no longer available. Remove it from your cart.',
			)
			return redirect('cart')
		if quantity > product.stock:
			messages.error(request, f'Only {product.stock} {product.units} of {product.name} are available.')
			return redirect('cart')
		if request.user.is_authenticated:
			cart_item.quantity = quantity
			cart_item.save(update_fields=['quantity'])
		else:
			session_cart[matched_key] = quantity
			request.session['cart'] = session_cart
		messages.success(request, 'Cart quantity updated.')
	return redirect('cart')

@login_required(login_url='login')
def checkout(request):
	if request.method == 'POST':
		customer = Customer.objects.filter(user=request.user).first()
		if customer is None:
			messages.error(request, 'A customer profile is required before placing an order.')
			return redirect('cart')
		payment_method = request.POST.get('payment_method')
		if payment_method not in (
			Order.PaymentMethod.CASH_ON_DELIVERY,
			Order.PaymentMethod.ONLINE,
		):
			messages.error(request, 'Select a supported payment method.')
			return redirect('checkout')
		if payment_method == Order.PaymentMethod.ONLINE:
			try:
				razorpay_test_credentials()
			except PaymentConfigurationError as error:
				messages.error(request, str(error))
				return redirect('checkout')

		raw_checkout_token = request.POST.get('checkout_token')
		if raw_checkout_token:
			try:
				checkout_token = uuid.UUID(raw_checkout_token)
			except (TypeError, ValueError, AttributeError):
				messages.error(request, 'Your checkout session expired. Review checkout and try again.')
				return redirect('checkout')
		else:
			checkout_token = uuid.UUID(
				_ensure_checkout_token(request, customer),
			)

		existing_order = Order.objects.filter(
			customer=customer,
			checkout_token=checkout_token,
		).first()
		if existing_order is not None:
			if (
				existing_order.payment_method == Order.PaymentMethod.ONLINE
				and existing_order.payment_status == Order.PaymentStatus.PAID
			):
				return redirect('order_confirmation', order_id=existing_order.id)
			if (
				existing_order.payment_method == Order.PaymentMethod.ONLINE
				and existing_order.payment_status in (
					Order.PaymentStatus.PENDING,
					Order.PaymentStatus.FAILED,
				)
			):
				return redirect('online_payment', order_id=existing_order.id)
			messages.info(request, f'Order #{existing_order.id} has already been placed.')
			return redirect('order_confirmation', order_id=existing_order.id)

		token_customer_key, token_key = _checkout_token_session_keys()
		if (
			request.session.get(token_customer_key) != customer.id
			or request.session.get(token_key) != str(checkout_token)
		):
			messages.error(request, 'Your checkout session expired. Review checkout and try again.')
			return redirect('checkout')

		order = None
		try:
			with transaction.atomic():
				customer = get_object_or_404(
					Customer.objects.select_for_update().select_related('user'),
					id=customer.id,
				)
				cart_items = list(
					CartItem.objects.select_for_update()
					.filter(user=request.user)
					.order_by('product_id')
				)
				locked_cart = {
					str(item.product_id): item.quantity
					for item in cart_items
				}
				if not locked_cart:
					messages.error(request, 'Your cart is empty.')
					return redirect('cart')

				product_ids = [int(product_id) for product_id in locked_cart]
				products = list(
					Product.objects.select_for_update()
					.filter(id__in=product_ids)
					.order_by('id')
				)
				products_by_id = {str(product.id): product for product in products}
				if set(products_by_id) != set(locked_cart):
					messages.error(
						request,
						'A product in your cart is no longer available. Please review your cart.',
					)
					return redirect('cart')
				for product_id, quantity in locked_cart.items():
					product = products_by_id.get(product_id)
					if not isinstance(quantity, int) or quantity < 1:
						messages.error(
							request,
							'Your cart contains an invalid quantity. Please review your cart.',
						)
						return redirect('cart')
					if quantity > product.stock:
						messages.error(
							request,
							f'Only {product.stock} {product.units} of {product.name} remain. Please update your cart.',
						)
						return redirect('cart')

				selected_address, address_error = (
					_resolve_checkout_delivery_address(request, customer)
				)
				if address_error:
					messages.error(request, address_error)
					return redirect('checkout')

				delivery_date, delivery_window, slot_error = (
					_resolve_checkout_delivery_slot(request, customer)
				)
				if slot_error:
					messages.error(request, slot_error)
					return redirect('checkout')

				try:
					delivery_snapshot = _checkout_delivery_snapshot(
						customer,
						selected_address,
					)
				except ValueError as error:
					messages.error(request, str(error))
					return redirect('checkout')

				order = Order.objects.create(
					customer=customer,
					checkout_token=checkout_token,
					payment_method=payment_method,
					payment_status=Order.PaymentStatus.PENDING,
					stock_reserved=(
						payment_method == Order.PaymentMethod.ONLINE
					),
					payment_expires_at=(
						timezone.now() + timedelta(minutes=15)
						if payment_method == Order.PaymentMethod.ONLINE
						else None
					),
					**delivery_snapshot,
					delivery_slot_date=delivery_date,
					delivery_slot_name=delivery_window.name,
					delivery_window=delivery_window,
					delivery_slot_start_time=delivery_window.start_time,
					delivery_slot_end_time=delivery_window.end_time,
				)
				vendor_orders = {}
				if payment_method == Order.PaymentMethod.CASH_ON_DELIVERY:
					for product in products:
						if product.vendor_id not in vendor_orders:
							vendor_orders[product.vendor_id] = VendorOrder.objects.create(
								order=order,
								vendor_id=product.vendor_id,
							)
				for product_id, quantity in locked_cart.items():
					product = products_by_id[product_id]
					updated = Product.objects.filter(
						id=product.id,
						stock__gte=quantity,
					).update(stock=F('stock') - quantity)
					if updated != 1:
						raise CheckoutValidationError(
							f'{product.name} no longer has enough stock. Please review your cart.',
						)
					OrderItem.objects.create(
						order=order,
						vendor_order=vendor_orders.get(product.vendor_id),
						product=product,
						quantity=quantity,
						unit_price=product.price,
					)
				CartItem.objects.filter(user=request.user).delete()
				record_order_placed(order, request.user)
				if payment_method == Order.PaymentMethod.CASH_ON_DELIVERY:
					notify_vendors_new_order(order)
		except CheckoutValidationError as error:
			messages.error(request, str(error))
			return redirect('cart')
		except IntegrityError:
			existing_order = Order.objects.filter(
				customer=customer,
				checkout_token=checkout_token,
			).first()
			if existing_order is None:
				raise
			messages.info(request, f'Order #{existing_order.id} has already been placed.')
			return redirect('order_confirmation', order_id=existing_order.id)

		for session_key in (
			*_checkout_delivery_slot_session_keys(),
			*_checkout_address_session_keys(),
			*_checkout_token_session_keys(),
		):
			request.session.pop(session_key, None)
		if order.payment_method == Order.PaymentMethod.ONLINE:
			try:
				ensure_razorpay_order(order.id, customer.id)
			except (
				PaymentGatewayError,
				PaymentConfigurationError,
				PaymentStockError,
			) as error:
				messages.error(
					request,
					f'Your order is reserved. Online payment could not be initialized: {error}',
				)
			return redirect('online_payment', order_id=order.id)
		messages.success(request, f'Order #{order.id} placed successfully.')
		return redirect('order_confirmation', order_id=order.id)

	cart = _get_cart(request)
	if not cart:
		messages.error(request, 'Your cart is empty.')
		return redirect('cart')

	context = _cart_context(request)
	if len(context['items']) != len(cart):
		messages.error(request, 'Your cart contains an unavailable product. Please review it before checkout.')
		return redirect('cart')

	customer = Customer.objects.filter(user=request.user).first()
	if customer is None:
		messages.error(request, 'A customer profile is required before placing an order.')
		return redirect('cart')
	context['checkout_token'] = _ensure_checkout_token(request, customer)
	context = _checkout_page_context(request, context)
	try:
		razorpay_test_credentials()
		context['razorpay_configured'] = True
	except PaymentConfigurationError:
		context['razorpay_configured'] = False
	return render(request, 'market/checkout.html', context)


@login_required(login_url='login')
def online_payment(request, order_id):
	try:
		expire_pending_razorpay_orders()
	except PaymentGatewayError:
		messages.error(
			request,
			'Payment status could not be checked. Please try again shortly.',
		)
	order = get_object_or_404(
		Order,
		id=order_id,
		customer__user=request.user,
		payment_method=Order.PaymentMethod.ONLINE,
	)
	try:
		key_id, _ = razorpay_test_credentials()
		payment_configured = True
	except PaymentConfigurationError:
		key_id = ''
		payment_configured = False
	total = order_total(order)
	return render(request, 'market/online_payment.html', {
		'order': order,
		'amount': total,
		'amount_paise': amount_in_paise(total),
		'key_id': key_id,
		'payment_configured': payment_configured,
	})


@login_required(login_url='login')
@require_POST
def start_online_payment(request, order_id):
	try:
		expire_pending_razorpay_orders()
		order = get_object_or_404(
			Order,
			id=order_id,
			customer__user=request.user,
			payment_method=Order.PaymentMethod.ONLINE,
		)
		ensure_razorpay_order(order.id, order.customer_id)
	except (PaymentConfigurationError, PaymentGatewayError, PaymentStockError) as error:
		messages.error(request, str(error))
		return redirect('online_payment', order_id=order_id)
	return redirect('online_payment', order_id=order_id)


@login_required(login_url='login')
@require_POST
def verify_online_payment(request, order_id):
	order = get_object_or_404(
		Order,
		id=order_id,
		customer__user=request.user,
		payment_method=Order.PaymentMethod.ONLINE,
	)
	try:
		status = verify_razorpay_payment(
			order.id,
			order.customer_id,
			request.POST.get('razorpay_order_id', ''),
			request.POST.get('razorpay_payment_id', ''),
			request.POST.get('razorpay_signature', ''),
		)
	except (PaymentConfigurationError, PaymentGatewayError, PaymentStockError) as error:
		messages.error(request, str(error))
		return redirect('online_payment', order_id=order.id)
	if status == Order.PaymentStatus.PAID:
		messages.success(request, f'Payment received for order #{order.id}.')
		return redirect('order_confirmation', order_id=order.id)
	if status == Order.PaymentStatus.FAILED:
		messages.error(request, 'Payment failed. You can retry this order.')
	else:
		messages.info(request, 'Payment is not yet confirmed by Razorpay.')
	return redirect('online_payment', order_id=order.id)


@login_required(login_url='login')
@require_POST
def fail_online_payment(request, order_id):
	order = get_object_or_404(
		Order,
		id=order_id,
		customer__user=request.user,
		payment_method=Order.PaymentMethod.ONLINE,
	)
	try:
		status = record_razorpay_failure(
			order.id,
			order.customer_id,
			request.POST.get('razorpay_order_id', ''),
			request.POST.get('razorpay_payment_id', ''),
		)
	except (PaymentConfigurationError, PaymentGatewayError) as error:
		messages.error(request, str(error))
		return redirect('online_payment', order_id=order.id)
	if status == Order.PaymentStatus.FAILED:
		messages.error(request, 'Payment failed. Reserved stock was released; you may retry.')
	elif status == Order.PaymentStatus.PAID:
		messages.success(request, 'Payment was confirmed by Razorpay.')
	else:
		messages.info(request, 'Payment remains pending.')
	return redirect('online_payment', order_id=order.id)


@login_required(login_url='login')
@require_POST
def cancel_online_payment(request, order_id):
	order = get_object_or_404(
		Order,
		id=order_id,
		customer__user=request.user,
		payment_method=Order.PaymentMethod.ONLINE,
	)
	try:
		status = cancel_razorpay_order(order.id, order.customer_id, actor=request.user)
	except (PaymentConfigurationError, PaymentGatewayError, PaymentStockError) as error:
		messages.error(request, str(error))
		return redirect('online_payment', order_id=order.id)
	if status == Order.PaymentStatus.PAID:
		messages.success(request, 'Payment was already confirmed by Razorpay.')
		return redirect('order_confirmation', order_id=order.id)
	if status == Order.PaymentStatus.CANCELLED:
		messages.info(request, 'Online payment was cancelled and reserved stock released.')
		return redirect('order_confirmation', order_id=order.id)
	return redirect('online_payment', order_id=order.id)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
	try:
		result = process_razorpay_webhook(
			request.body,
			request.headers.get('X-Razorpay-Signature', ''),
		)
	except PaymentConfigurationError:
		return JsonResponse(
			{'status': 'webhook_not_configured'},
			status=503,
		)
	except WebhookSignatureError:
		return JsonResponse({'status': 'invalid_signature'}, status=400)
	except WebhookPayloadError:
		return JsonResponse({'status': 'invalid_payload'}, status=400)
	except Exception:
		logger.exception('Razorpay webhook processing failed.')
		return JsonResponse({'status': 'processing_error'}, status=500)
	return JsonResponse({'status': result}, status=200)


@login_required(login_url='login')
@require_POST
def select_checkout_address(request):
	customer = get_object_or_404(Customer, user=request.user)
	raw_address_id = request.POST.get('address_id', '')
	if not raw_address_id.isdecimal():
		messages.error(request, 'Select a valid saved delivery address.')
		return redirect('checkout')

	address = get_object_or_404(
		DeliveryAddress,
		id=int(raw_address_id),
		customer=customer,
	)
	customer_session_key, address_session_key = _checkout_address_session_keys()
	request.session[customer_session_key] = customer.id
	request.session[address_session_key] = address.id
	messages.success(request, f'{address.label} selected for this checkout.')
	return redirect('checkout')

@login_required(login_url='login')
@require_POST
def select_checkout_delivery_slot(request):
	customer = get_object_or_404(Customer, user=request.user)
	raw_slot = request.POST.get('slot_key', '')
	try:
		raw_date, raw_window_id = raw_slot.split('|', 1)
		delivery_date = date.fromisoformat(raw_date)
		window_id = int(raw_window_id)
	except (TypeError, ValueError):
		messages.error(request, 'Choose a valid delivery slot.')
		return redirect('checkout')

	today = timezone.localdate()
	if not today <= delivery_date <= today + timedelta(days=6):
		messages.error(request, 'Choose a delivery slot within the next seven days.')
		return redirect('checkout')

	with transaction.atomic():
		window = (
			DeliveryWindow.objects.select_for_update()
			.filter(id=window_id, is_active=True)
			.first()
		)
		if window is None:
			messages.error(request, 'That delivery window is unavailable.')
			return redirect('checkout')
		if (
			delivery_date == today
			and window.start_time <= timezone.localtime().time()
		):
			messages.error(request, 'That delivery time has passed. Choose another slot.')
			return redirect('checkout')
		booked_count = _orders_consuming_delivery_capacity(
			Order.objects.filter(
				delivery_slot_date=delivery_date,
				delivery_window=window,
			),
		).count()
		if booked_count >= window.capacity:
			messages.error(request, 'That delivery slot is full. Choose another slot.')
			return redirect('checkout')

	slot_customer_key, slot_date_key, window_key = (
		_checkout_delivery_slot_session_keys()
	)
	request.session[slot_customer_key] = customer.id
	request.session[slot_date_key] = delivery_date.isoformat()
	request.session[window_key] = window.id
	messages.success(
		request,
		f'{window} on {delivery_date:%B} {delivery_date.day} selected.',
	)
	return redirect('checkout')

@login_required(login_url='login')
@require_POST
def add_checkout_address(request):
	cart = _get_cart(request)
	if not cart:
		messages.error(request, 'Your cart is empty.')
		return redirect('cart')

	customer = get_object_or_404(Customer, user=request.user)
	form = DeliveryAddressForm(request.POST)
	if form.is_valid():
		with transaction.atomic():
			customer = get_object_or_404(
				Customer.objects.select_for_update(),
				id=customer.id,
			)
			address = form.save(commit=False)
			address.customer = customer
			address.is_default = not customer.delivery_addresses.exists()
			address.save()
		customer_session_key, address_session_key = _checkout_address_session_keys()
		request.session[customer_session_key] = customer.id
		request.session[address_session_key] = address.id
		messages.success(
			request,
			f'{address.label} was added and selected for this checkout.',
		)
		return redirect('checkout')

	context = _cart_context(request)
	if len(context['items']) != len(cart):
		messages.error(
			request,
			'Your cart contains an unavailable product. Please review it before checkout.',
		)
		return redirect('cart')
	context = _checkout_page_context(request, context, form)
	return render(request, 'market/checkout.html', context)

@login_required(login_url='login')
def order_confirmation(request, order_id):
	vendor_order_items = OrderItem.objects.select_related('product')
	lifecycle_history = OrderStatusHistory.objects.filter(
		vendor_order__isnull=True,
	).select_related('actor')
	order = get_object_or_404(
		Order.objects.prefetch_related(
			Prefetch(
				'status_history',
				queryset=lifecycle_history,
				to_attr='lifecycle_history',
			),
			Prefetch(
				'vendor_orders',
				queryset=VendorOrder.objects.select_related('vendor').prefetch_related(
					Prefetch('items', queryset=vendor_order_items),
				),
			),
		),
		id=order_id,
		customer__user=request.user,
	)
	items = list(order.orderitem_set.select_related('product'))
	add_progress_to_order(order)
	total = sum(
		(item.unit_price * item.quantity for item in items),
		Decimal('0.00'),
	)
	return render(request, 'market/order_confirmation.html', {
		'order': order,
		'items': items,
		'total': total,
		'item_count': sum(item.quantity for item in items),
		'status_history': order.lifecycle_history,
	})

@login_required(login_url='login')
def order_history(request):
	vendor_order_items = OrderItem.objects.select_related('product')
	lifecycle_history = OrderStatusHistory.objects.filter(
		vendor_order__isnull=True,
	).select_related('actor')
	order_total_expression = ExpressionWrapper(
		F('orderitem__unit_price') * F('orderitem__quantity'),
		output_field=DecimalField(max_digits=12, decimal_places=2),
	)
	orders = (
		Order.objects.filter(customer__user=request.user)
		.annotate(
			order_total=Coalesce(
				Sum(order_total_expression),
				Value(Decimal('0.00')),
				output_field=DecimalField(max_digits=12, decimal_places=2),
			),
			item_count=Coalesce(Sum('orderitem__quantity'), Value(0)),
		)
		.prefetch_related(
			'orderitem_set__product',
			Prefetch(
				'status_history',
				queryset=lifecycle_history,
				to_attr='lifecycle_history',
			),
			Prefetch(
				'vendor_orders',
				queryset=VendorOrder.objects.select_related('vendor').prefetch_related(
					Prefetch('items', queryset=vendor_order_items),
				),
			),
		)
		.order_by('-order_date', '-id')
	)
	page_obj = Paginator(orders, 20).get_page(request.GET.get('page'))
	for order in page_obj.object_list:
		add_progress_to_order(order)
	return render(request, 'market/order_history.html', {
		'orders': page_obj.object_list,
		'page_obj': page_obj,
	})


@login_required(login_url='login')
def notification_center(request):
	if not (
		request.user.is_staff
		or Customer.objects.filter(user=request.user).exists()
		or Vendor.objects.filter(user=request.user).exists()
	):
		raise Http404
	notifications = Notification.objects.filter(
		recipient=request.user,
	).select_related(
		'order__customer__user',
		'vendor_order__vendor__user',
	).order_by('-created_at', '-id')
	page_obj = Paginator(notifications, 20).get_page(request.GET.get('page'))
	for notification in page_obj.object_list:
		notification.target_url = None
		if (
			notification.vendor_order_id
			and notification.vendor_order.vendor.user_id == request.user.id
		):
			notification.target_url = reverse(
				'vendor_order_detail',
				args=[notification.vendor_order.order_id],
			)
		elif (
			notification.order_id
			and notification.order.customer.user_id == request.user.id
		):
			notification.target_url = reverse(
				'order_confirmation',
				args=[notification.order_id],
			)
		elif (
			notification.order_id
			and request.user.is_staff
			and request.user.has_perm('market.view_order')
		):
			notification.target_url = reverse(
				'market_admin_order_detail',
				args=[notification.order_id],
			)
	return render(request, 'market/notifications.html', {
		'notifications': page_obj.object_list,
		'page_obj': page_obj,
		'unread_notification_count': notifications.filter(read_at__isnull=True).count(),
	})


@login_required(login_url='login')
@require_POST
def mark_notification_read(request, notification_id):
	updated = Notification.objects.filter(
		id=notification_id,
		recipient=request.user,
		read_at__isnull=True,
	).update(read_at=timezone.now())
	if not updated and not Notification.objects.filter(
		id=notification_id,
		recipient=request.user,
	).exists():
		raise Http404
	return redirect('notification_center')


@login_required(login_url='login')
@require_POST
def mark_all_notifications_read(request):
	Notification.objects.filter(
		recipient=request.user,
		read_at__isnull=True,
	).update(read_at=timezone.now())
	return redirect('notification_center')

@login_required(login_url='login')
@require_http_methods(['GET', 'POST'])
def customer_profile(request):
	customer = get_object_or_404(
		Customer.objects.select_related('user').prefetch_related(
			Prefetch(
				'delivery_addresses',
				queryset=DeliveryAddress.objects.order_by('-is_default', 'id'),
				to_attr='account_addresses',
			),
		),
		user=request.user,
	)
	user_form = CustomerUserProfileForm(
		request.POST or None,
		instance=request.user,
	)
	customer_form = CustomerProfileForm(
		request.POST or None,
		instance=customer,
	)
	if request.method == 'POST':
		user_form_valid = user_form.is_valid()
		customer_form_valid = customer_form.is_valid()
		if user_form_valid and customer_form_valid:
			with transaction.atomic():
				user_form.save()
				customer_form.save()
			messages.success(request, 'Your profile has been updated.')
			return redirect('customer_profile')

	return render(request, 'market/customer_profile.html', {
		'user_form': user_form,
		'customer_form': customer_form,
		'customer': customer,
		'addresses': customer.account_addresses,
		'order_count': Order.objects.filter(customer=customer).count(),
	})

@login_required(login_url='login')
@require_http_methods(['GET', 'POST'])
def add_delivery_address(request):
	customer = get_object_or_404(Customer, user=request.user)
	form = DeliveryAddressForm(request.POST or None)
	if request.method == 'POST' and form.is_valid():
		with transaction.atomic():
			customer = get_object_or_404(
				Customer.objects.select_for_update(),
				id=customer.id,
			)
			address = form.save(commit=False)
			address.customer = customer
			address.is_default = not customer.delivery_addresses.exists()
			address.save()
		messages.success(request, 'Delivery address added.')
		return redirect('customer_profile')

	return render(request, 'market/add_delivery_address.html', {'form': form})

@login_required(login_url='login')
@require_http_methods(['GET', 'POST'])
def edit_delivery_address(request, address_id):
	address = get_object_or_404(
		DeliveryAddress,
		id=address_id,
		customer__user=request.user,
	)
	form = DeliveryAddressForm(request.POST or None, instance=address)
	if request.method == 'POST' and form.is_valid():
		form.save()
		messages.success(request, 'Delivery address updated.')
		return redirect('customer_profile')

	return render(request, 'market/edit_delivery_address.html', {
		'form': form,
		'address': address,
	})

@login_required(login_url='login')
@require_http_methods(['GET', 'POST'])
def delete_delivery_address(request, address_id):
	address = get_object_or_404(
		DeliveryAddress,
		id=address_id,
		customer__user=request.user,
	)
	if request.method == 'POST':
		with transaction.atomic():
			address = get_object_or_404(
				DeliveryAddress.objects.select_for_update(),
				id=address_id,
				customer__user=request.user,
			)
			address.delete()
		messages.success(request, 'Delivery address deleted.')
		return redirect('customer_profile')

	return render(request, 'market/delete_delivery_address.html', {
		'address': address,
	})

@login_required(login_url='login')
@require_POST
def set_default_delivery_address(request, address_id):
	with transaction.atomic():
		customer = get_object_or_404(
			Customer.objects.select_for_update(),
			user=request.user,
		)
		address = get_object_or_404(
			DeliveryAddress.objects.select_for_update(),
			id=address_id,
			customer=customer,
		)
		if not address.is_default:
			customer.delivery_addresses.filter(is_default=True).update(
				is_default=False,
			)
			address.is_default = True
			address.save(update_fields=['is_default'])

	messages.success(request, f'{address.label} is now your default address.')
	return redirect('customer_profile')

def register(request):
	if request.method == 'POST':
		username = request.POST.get('username', '').strip()
		email = request.POST.get('email', '').strip()
		password = request.POST.get('password', '')
		password_confirm = request.POST.get('password_confirm', '')
		address = request.POST.get('address', '').strip()
		phone = request.POST.get('phone', '').strip()

		if not username or not email or not password or not password_confirm or not address or not phone:
			return render(request, 'market/register.html', {'error': 'Complete all fields.'})

		if password != password_confirm:
			return render(request, 'market/register.html', {'error': 'Passwords do not match.'})

		if User.objects.filter(username=username).exists():
			return render(request, 'market/register.html', {'error': 'That username is already taken.'})

		try:
			validate_password(password, user=User(username=username, email=email))
		except ValidationError as error:
			return render(request, 'market/register.html', {'error': error.messages})

		coordinates = parse_coordinates(
			request.POST.get('latitude'),
			request.POST.get('longitude'),
		)
		user = User.objects.create_user(username=username, email=email, password=password)
		Customer.objects.create(
			user=user,
			address=address,
			phone=phone,
			latitude=coordinates[0] if coordinates else None,
			longitude=coordinates[1] if coordinates else None,
		)
		login(request, user)
		_merge_guest_cart(request, user)
		return redirect('product_list')

	return render(request, 'market/register.html')

class CartLoginView(LoginView):
	template_name = 'market/login.html'
	next_page = 'product_list'

	def form_valid(self, form):
		user = form.get_user()
		if Vendor.objects.filter(user=user).exists():
			self.request.session.pop('cart', None)
		elif self.request.user.is_authenticated:
			self.request.session.pop('cart', None)
		else:
			_merge_guest_cart(self.request, user)
		return super().form_valid(form)

	def get_success_url(self):
		if self.get_redirect_url():
			return self.get_redirect_url()
		if Vendor.objects.filter(user=self.request.user).exists():
			return reverse('vendor_dashboard')
		return super().get_success_url()

def user_logout(request):
	logout(request)
	return redirect('product_list')


# ---------------------------------------------------------------------------
# Location: nearby shops, shop pages and saving a browser location
# ---------------------------------------------------------------------------
LOCATION_SESSION_KEY = 'location'


def _current_location(request):
	"""Best known (lat, lng) for this visitor, or None.

	Order: location saved in this browser session, then the customer's default
	delivery address, then the customer's profile location.
	"""
	saved = request.session.get(LOCATION_SESSION_KEY)
	if isinstance(saved, (list, tuple)) and len(saved) == 2:
		coordinates = parse_coordinates(saved[0], saved[1])
		if coordinates:
			return coordinates
	if request.user.is_authenticated:
		address = DeliveryAddress.objects.filter(
			customer__user_id=request.user.id,
			is_default=True,
			latitude__isnull=False,
			longitude__isnull=False,
		).first()
		if address:
			return address.latitude, address.longitude
		customer = Customer.objects.filter(
			user_id=request.user.id,
			latitude__isnull=False,
			longitude__isnull=False,
		).first()
		if customer:
			return customer.latitude, customer.longitude
	return None


@require_POST
def save_location(request):
	"""Store the browser location for the visitor.

	Vendors save it as their shop location, customers as their profile
	location. Guests keep it only in the session.
	"""
	coordinates = parse_coordinates(
		request.POST.get('latitude'),
		request.POST.get('longitude'),
	)
	if coordinates is None:
		return JsonResponse(
			{'ok': False, 'error': 'Enter a valid latitude and longitude.'},
			status=400,
		)
	latitude, longitude = coordinates
	if request.user.is_authenticated:
		vendor = Vendor.objects.filter(user=request.user).first()
		if vendor is not None:
			vendor.latitude = latitude
			vendor.longitude = longitude
			vendor.save(update_fields=['latitude', 'longitude'])
			return JsonResponse({'ok': True, 'role': 'vendor'})
		Customer.objects.filter(user=request.user).update(
			latitude=latitude,
			longitude=longitude,
		)
	request.session[LOCATION_SESSION_KEY] = [latitude, longitude]
	return JsonResponse({'ok': True, 'role': 'customer'})


def nearby_shops(request):
	"""All shops, closest first. Far shops are flagged, never hidden."""
	location = _current_location(request)
	vendors = list(
		Vendor.objects.annotate(
			listing_count=Count('products'),
			available_count=Count('products', filter=Q(products__stock__gt=0)),
		)
		.filter(listing_count__gt=0)
		.order_by('name')
	)
	categories_by_vendor = {}
	for vendor_id, category_name in (
		Product.objects.filter(vendor__in=vendors)
		.order_by('category__name')
		.values_list('vendor_id', 'category__name')
		.distinct()
	):
		categories_by_vendor.setdefault(vendor_id, []).append(category_name)

	ratings = _vendor_rating_summary([vendor.id for vendor in vendors])
	shops = []
	for vendor in vendors:
		distance = distance_to_vendor(location, vendor)
		shops.append({
			'vendor': vendor,
			'rating': ratings.get(vendor.id),
			'distance_km': distance,
			'distance_label': format_distance(distance),
			'far_away': is_far_away(distance),
			'categories': categories_by_vendor.get(vendor.id, [])[:4],
		})
	# Closest first. Shops without a saved location go last, A-Z.
	shops.sort(key=lambda shop: (
		shop['distance_km'] is None,
		shop['distance_km'] if shop['distance_km'] is not None else 0,
		(shop['vendor'].shop_name or shop['vendor'].name).lower(),
	))
	return render(request, 'market/nearby_shops.html', {
		'shops': shops,
		'has_location': location is not None,
		'delivery_radius_km': DELIVERY_RADIUS_KM,
		'nearby_count': sum(
			1 for shop in shops
			if shop['distance_km'] is not None and not shop['far_away']
		),
	})


def shop_detail(request, vendor_id):
	"""One shop and only its own vegetables and fruits."""
	vendor = get_object_or_404(Vendor, id=vendor_id)
	products = (
		Product.objects.filter(vendor=vendor)
		.select_related('category', 'vendor')
		.order_by('category__name', 'name', 'id')
	)
	location = _current_location(request)
	distance = distance_to_vendor(location, vendor)
	return render(request, 'market/shop_detail.html', {
		'vendor': vendor,
		'products': products,
		'distance_label': format_distance(distance),
		'far_away': is_far_away(distance),
		'has_location': location is not None,
		'delivery_radius_km': DELIVERY_RADIUS_KM,
		'rating': _vendor_rating_summary([vendor.id]).get(vendor.id),
		'reviews': list(vendor.reviews.select_related('customer__user')[:5]),
		'wishlist_product_ids': _wishlist_product_ids(request),
	})


# ---------------------------------------------------------------------------
# Delivery tracking and shop reviews
# ---------------------------------------------------------------------------
def _vendor_rating_summary(vendor_ids):
	"""{vendor_id: {'average': 4.5, 'count': 12}} for vendors with reviews."""
	rows = (
		Review.objects.filter(vendor_id__in=vendor_ids)
		.values('vendor_id')
		.annotate(average=Avg('rating'), total=Count('id'))
		.order_by()
	)
	return {
		row['vendor_id']: {
			'average': round(row['average'], 1),
			'count': row['total'],
		}
		for row in rows
	}


@login_required(login_url='login')
def track_order(request, order_id):
	"""Step-by-step delivery tracking, one timeline per shop."""
	order = get_object_or_404(
		Order.objects.prefetch_related(
			Prefetch(
				'vendor_orders',
				queryset=VendorOrder.objects.select_related('vendor').prefetch_related(
					'items',
					'status_history',
				),
			),
		),
		id=order_id,
		customer__user=request.user,
	)
	tracks = []
	for vendor_order in order.vendor_orders.all():
		reviewed = Review.objects.filter(vendor_order=vendor_order).exists()
		delivered = vendor_order.status == VendorOrder.Status.DELIVERED
		tracks.append({
			'vendor_order': vendor_order,
			'name': (
				vendor_order.vendor.shop_name or vendor_order.vendor.name
				if vendor_order.vendor_id
				else 'Sbji@Home platform'
			),
			'item_count': sum(item.quantity for item in vendor_order.items.all()),
			'steps': build_tracking(vendor_order, order.order_date),
			'rejected': vendor_order.status == VendorOrder.Status.REJECTED,
			'delivered': delivered,
			'reviewed': reviewed,
			'can_review': (
				delivered and vendor_order.vendor_id is not None and not reviewed
			),
		})
	is_open = order.status != Order.Status.CANCELLED and any(
		not track['delivered'] and not track['rejected'] for track in tracks
	)
	return render(request, 'market/track_order.html', {
		'order': order,
		'tracks': tracks,
		'auto_refresh': is_open,
	})


@login_required(login_url='login')
@require_http_methods(['GET', 'POST'])
def review_vendor_order(request, vendor_order_id):
	"""Let a customer rate a shop once, after that shop delivered."""
	vendor_order = get_object_or_404(
		VendorOrder.objects.select_related('vendor', 'order__customer'),
		id=vendor_order_id,
		order__customer__user=request.user,
	)
	back = redirect('order_confirmation', order_id=vendor_order.order_id)
	if (
		vendor_order.vendor_id is None
		or vendor_order.status != VendorOrder.Status.DELIVERED
	):
		messages.error(
			request,
			'You can rate a shop only after it has delivered your items.',
		)
		return back
	if Review.objects.filter(vendor_order=vendor_order).exists():
		messages.info(request, 'You have already rated this shop for this order.')
		return back

	form = ReviewForm(request.POST or None)
	if request.method == 'POST' and form.is_valid():
		review = form.save(commit=False)
		review.vendor_order = vendor_order
		review.vendor = vendor_order.vendor
		review.customer = vendor_order.order.customer
		try:
			with transaction.atomic():
				review.save()
		except IntegrityError:
			messages.info(request, 'You have already rated this shop for this order.')
		else:
			messages.success(request, 'Thanks! Your review was saved.')
		return back
	return render(request, 'market/review_form.html', {
		'form': form,
		'vendor_order': vendor_order,
		'shop_name': vendor_order.vendor.shop_name or vendor_order.vendor.name,
	})
