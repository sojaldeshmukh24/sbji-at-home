from decimal import Decimal
from functools import wraps

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.dateparse import parse_date

from .models import (
	Category,
	Customer,
	Order,
	OrderItem,
	OrderStatusHistory,
	Product,
	Vendor,
	VendorOrder,
)


LOW_STOCK_THRESHOLD = 5
PAGE_SIZE = 25
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ITEM_AMOUNT = ExpressionWrapper(
	F('orderitem__unit_price') * F('orderitem__quantity'),
	output_field=MONEY_FIELD,
)


def marketplace_permissions_required(*permissions):
	def decorate(view):
		@wraps(view)
		@staff_member_required
		def wrapped(request, *args, **kwargs):
			if not request.user.has_perms(permissions):
				raise PermissionDenied
			return view(request, *args, **kwargs)
		return wrapped
	return decorate


def _page_links(request, page_obj):
	def page_url(page_number):
		query = request.GET.copy()
		query['page'] = page_number
		return f'?{query.urlencode()}'

	return {
		'previous_page_url': page_url(page_obj.previous_page_number())
		if page_obj.has_previous() else None,
		'next_page_url': page_url(page_obj.next_page_number())
		if page_obj.has_next() else None,
	}


def _list_page(request, queryset, title, description, headers, row_builder, filters=()):
	page_obj = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get('page'))
	rows = [row_builder(item) for item in page_obj.object_list]
	for row in rows:
		row['columns'] = [
			{'label': label, 'value': value}
			for label, value in zip(headers, row['cells'])
		]
	query = request.GET.get('q', '').strip()
	return render(request, 'market/admin/list.html', {
		'page_title': title,
		'page_description': description,
		'headers': headers,
		'rows': rows,
		'page_obj': page_obj,
		'filters': filters,
		'query': query,
		'has_active_filters': bool(query or any(item.get('value') for item in filters)),
		**_page_links(request, page_obj),
	})


def _money(value):
	return f'₹{value:.2f}'


def _order_total_annotation(queryset):
	return queryset.annotate(
		admin_order_total=Coalesce(
			Sum(ITEM_AMOUNT),
			Value(Decimal('0.00')),
			output_field=MONEY_FIELD,
		),
		admin_item_count=Coalesce(Sum('orderitem__quantity'), Value(0)),
	)


@marketplace_permissions_required(
	'market.view_customer',
	'auth.view_user',
	'market.view_vendor',
	'market.view_product',
	'market.view_category',
	'market.view_order',
	'market.view_orderitem',
	'market.view_vendororder',
)
def dashboard(request):
	order_status_counts = list(
		Order.objects.order_by()
		.values('status')
		.annotate(count=Count('id'))
		.order_by('status')
	)
	payment_status_counts = list(
		Order.objects.order_by()
		.values('payment_status')
		.annotate(count=Count('id'))
		.order_by('payment_status')
	)
	recent_orders = _order_total_annotation(
		Order.objects.select_related('customer__user')
	).order_by('-order_date', '-id')[:8]
	marketplace_total = OrderItem.objects.aggregate(
		total=Coalesce(
			Sum(ExpressionWrapper(
				F('unit_price') * F('quantity'),
				output_field=MONEY_FIELD,
			)),
			Value(Decimal('0.00')),
			output_field=MONEY_FIELD,
		),
	)['total']
	return render(request, 'market/admin/dashboard.html', {
		'customer_count': Customer.objects.count(),
		'active_customer_count': Customer.objects.filter(user__is_active=True).count(),
		'vendor_count': Vendor.objects.count(),
		'active_vendor_count': Vendor.objects.filter(user__is_active=True).count(),
		'vendors_with_products': Vendor.objects.annotate(
			product_count=Count('products'),
		).filter(product_count__gt=0).count(),
		'product_count': Product.objects.count(),
		'available_product_count': Product.objects.filter(stock__gt=0).count(),
		'out_of_stock_count': Product.objects.filter(stock__lte=0).count(),
		'low_stock_count': Product.objects.filter(
			stock__gt=0,
			stock__lte=LOW_STOCK_THRESHOLD,
		).count(),
		'order_count': Order.objects.count(),
		'marketplace_order_value': marketplace_total,
		'order_status_counts': order_status_counts,
		'payment_status_counts': payment_status_counts,
		'recent_orders': recent_orders,
	})


@marketplace_permissions_required('market.view_customer', 'auth.view_user')
def customer_list(request):
	customers = Customer.objects.select_related('user').annotate(
		order_count=Count('order', distinct=True),
	).order_by('user__username', 'id')
	query = request.GET.get('q', '').strip()
	if query:
		customers = customers.filter(
			Q(user__username__icontains=query)
			| Q(user__email__icontains=query)
			| Q(user__first_name__icontains=query)
			| Q(user__last_name__icontains=query)
			| Q(phone__icontains=query)
		)
	return _list_page(
		request,
		customers,
		'Customers',
		'Customer accounts and marketplace activity.',
		('Customer', 'Email', 'Phone', 'Orders', 'Account'),
		lambda customer: {
			'cells': [
				customer.user.get_full_name().strip() or customer.user.username,
				customer.user.email or 'Not provided',
				customer.phone,
				str(customer.order_count),
				'Active' if customer.user.is_active else 'Inactive',
			],
			'detail_url': reverse('market_admin_customer_detail', args=[customer.id]),
		},
	)


@marketplace_permissions_required('market.view_customer', 'auth.view_user')
def customer_detail(request, customer_id):
	customer = get_object_or_404(
		Customer.objects.select_related('user'),
		id=customer_id,
	)
	orders = _order_total_annotation(
		Order.objects.filter(customer=customer),
	).order_by('-order_date', '-id')[:20]
	return render(request, 'market/admin/customer_detail.html', {
		'customer': customer,
		'orders': orders,
		'order_count': Order.objects.filter(customer=customer).count(),
	})


@marketplace_permissions_required(
	'market.view_vendor',
	'auth.view_user',
	'market.view_product',
	'market.view_vendororder',
	'market.view_order',
	'market.view_orderitem',
)
def vendor_list(request):
	vendors = Vendor.objects.select_related('user').annotate(
		product_count=Count('products', distinct=True),
		order_count=Count('vendor_orders', distinct=True),
	).order_by('name', 'id')
	query = request.GET.get('q', '').strip()
	if query:
		vendors = vendors.filter(
			Q(name__icontains=query)
			| Q(shop_name__icontains=query)
			| Q(email__icontains=query)
			| Q(phone__icontains=query)
			| Q(user__username__icontains=query)
		)
	return _list_page(
		request,
		vendors,
		'Vendors',
		'Shop profiles, account status, products, and order activity.',
		('Vendor', 'Shop', 'Email', 'Products', 'Orders', 'Account'),
		lambda vendor: {
			'cells': [
				vendor.name,
				vendor.shop_name or 'Not provided',
				vendor.email or 'Not provided',
				str(vendor.product_count),
				str(vendor.order_count),
				'Active' if vendor.user and vendor.user.is_active else 'Inactive',
			],
			'detail_url': reverse('market_admin_vendor_detail', args=[vendor.id]),
		},
	)


@marketplace_permissions_required(
	'market.view_vendor',
	'auth.view_user',
	'market.view_product',
	'market.view_vendororder',
	'market.view_order',
	'market.view_orderitem',
)
def vendor_detail(request, vendor_id):
	vendor = get_object_or_404(Vendor.objects.select_related('user'), id=vendor_id)
	products = vendor.products.select_related('category').order_by('-id')[:10]
	vendor_orders = (
		vendor.vendor_orders.select_related('order__customer__user')
		.annotate(
			admin_subtotal=Coalesce(
				Sum(ExpressionWrapper(
					F('items__unit_price') * F('items__quantity'),
					output_field=MONEY_FIELD,
				)),
				Value(Decimal('0.00')),
				output_field=MONEY_FIELD,
			),
		)
		.order_by('-order__order_date', '-order_id')[:10]
	)
	return render(request, 'market/admin/vendor_detail.html', {
		'vendor': vendor,
		'products': products,
		'vendor_orders': vendor_orders,
		'product_count': vendor.products.count(),
		'order_count': vendor.vendor_orders.count(),
	})


@marketplace_permissions_required('market.view_product')
def product_list(request):
	products = Product.objects.select_related('vendor', 'category').order_by('name', 'id')
	query = request.GET.get('q', '').strip()
	if query:
		products = products.filter(
			Q(name__icontains=query)
			| Q(vendor__name__icontains=query)
			| Q(vendor__shop_name__icontains=query)
			| Q(category__name__icontains=query)
		)
	vendor_id = request.GET.get('vendor', '')
	if vendor_id.isdecimal():
		products = products.filter(vendor_id=int(vendor_id))
	category_id = request.GET.get('category', '')
	if category_id.isdecimal():
		products = products.filter(category_id=int(category_id))
	availability = request.GET.get('availability', '')
	if availability == 'available':
		products = products.filter(stock__gt=0)
	elif availability == 'out_of_stock':
		products = products.filter(stock__lte=0)
	elif availability == 'low_stock':
		products = products.filter(stock__gt=0, stock__lte=LOW_STOCK_THRESHOLD)

	return _list_page(
		request,
		products,
		'Products',
		'All marketplace listings, with stock and ownership at a glance.',
		('Product', 'Vendor', 'Category', 'Price / unit', 'Stock', 'Availability'),
		lambda product: {
			'cells': [
				product.name,
				product.vendor.shop_name or product.vendor.name
				if product.vendor else 'Unassigned',
				product.category.name,
				f'{_money(product.price)} / {product.units}',
				f'{product.stock} {product.units}',
				'Out of stock' if product.stock <= 0
				else 'Low stock' if product.stock <= LOW_STOCK_THRESHOLD
				else 'Available',
			],
			'detail_url': reverse('admin:market_product_change', args=[product.id])
			if request.user.has_perm('market.change_product') else None,
			'action_url': reverse('admin:market_product_change', args=[product.id])
			if request.user.has_perm('market.change_product') else None,
			'action_label': 'Manage' if request.user.has_perm('market.change_product') else '',
			'image_url': product.image.url if product.image else '',
		},
		filters=(
			{
				'name': 'vendor', 'label': 'Vendor', 'value': vendor_id,
				'options': [('', 'All vendors')] + [
					(str(vendor.id), vendor.shop_name or vendor.name)
					for vendor in Vendor.objects.order_by('name')
				],
			},
			{
				'name': 'category', 'label': 'Category', 'value': category_id,
				'options': [('', 'All categories')] + [
					(str(category.id), category.name)
					for category in Category.objects.order_by('name')
				],
			},
			{
				'name': 'availability', 'label': 'Availability', 'value': availability,
				'options': [
					('', 'Any availability'),
					('available', 'Available'),
					('low_stock', 'Low stock'),
					('out_of_stock', 'Out of stock'),
				],
			},
		),
	)


@marketplace_permissions_required('market.view_category')
def category_list(request):
	categories = Category.objects.annotate(
		product_count=Count('product', distinct=True),
	).order_by('name', 'id')
	query = request.GET.get('q', '').strip()
	if query:
		categories = categories.filter(name__icontains=query)
	return _list_page(
		request,
		categories,
		'Categories',
		'Database-backed product categories. Deletion is disabled to protect linked products.',
		('Category', 'Products'),
		lambda category: {
			'cells': [category.name, str(category.product_count)],
			'detail_url': reverse('admin:market_category_change', args=[category.id])
			if request.user.has_perm('market.change_category') else None,
			'action_url': reverse('admin:market_category_change', args=[category.id])
			if request.user.has_perm('market.change_category') else None,
			'action_label': 'Edit' if request.user.has_perm('market.change_category') else '',
		},
	)


@marketplace_permissions_required(
	'market.view_order',
	'market.view_orderitem',
	'market.view_customer',
	'auth.view_user',
)
def order_list(request):
	orders = _order_total_annotation(
		Order.objects.select_related('customer__user'),
	).order_by('-order_date', '-id')
	query = request.GET.get('q', '').strip()
	if query:
		search = Q(customer__user__username__icontains=query)
		search |= Q(customer__user__email__icontains=query)
		if query.isdecimal():
			search |= Q(id=int(query))
		orders = orders.filter(search)
	status = request.GET.get('status', '')
	if status:
		orders = orders.filter(status=status)
	payment_status = request.GET.get('payment_status', '')
	if payment_status in dict(Order.PaymentStatus.choices):
		orders = orders.filter(payment_status=payment_status)
	payment_method = request.GET.get('payment_method', '')
	if payment_method in dict(Order.PaymentMethod.choices):
		orders = orders.filter(payment_method=payment_method)
	date_from_raw = request.GET.get('date_from', '')
	date_to_raw = request.GET.get('date_to', '')
	date_from = parse_date(date_from_raw) if date_from_raw else None
	date_to = parse_date(date_to_raw) if date_to_raw else None
	if date_from:
		orders = orders.filter(order_date__date__gte=date_from)
	if date_to:
		orders = orders.filter(order_date__date__lte=date_to)
	filters = (
		{
			'name': 'status', 'label': 'Order status', 'value': status,
			'options': [('', 'All order statuses')] + [
				(value, value.replace('_', ' ').title())
				for value in Order.objects.order_by().values_list('status', flat=True).distinct()
			],
		},
		{
			'name': 'payment_status', 'label': 'Payment status', 'value': payment_status,
			'options': [('', 'All payment states')] + list(Order.PaymentStatus.choices),
		},
		{
			'name': 'payment_method', 'label': 'Payment method', 'value': payment_method,
			'options': [('', 'All payment methods')] + list(Order.PaymentMethod.choices),
		},
	)
	page_obj = Paginator(orders, PAGE_SIZE).get_page(request.GET.get('page'))
	rows = []
	for order in page_obj.object_list:
		rows.append({
			'cells': [
				f'#{order.id}',
				order.customer.user.get_full_name().strip()
				or order.customer.user.username,
				order.order_date.strftime('%d %b %Y, %H:%M'),
				str(order.admin_item_count),
				_money(order.admin_order_total),
				order.get_payment_status_display(),
				order.status.replace('_', ' ').title(),
			],
			'detail_url': reverse('market_admin_order_detail', args=[order.id]),
		})
	for row in rows:
		row['columns'] = [
			{'label': label, 'value': value}
			for label, value in zip(
				('Order', 'Customer', 'Date', 'Items', 'Total', 'Payment', 'Status'),
				row['cells'],
			)
		]
	return render(request, 'market/admin/list.html', {
		'page_title': 'Orders',
		'page_description': 'Marketplace orders and payment states. Totals use historical order-item prices.',
		'headers': ('Order', 'Customer', 'Date', 'Items', 'Total', 'Payment', 'Status'),
		'rows': rows,
		'page_obj': page_obj,
		'filters': filters,
		'query': query,
		'has_active_filters': bool(
			query or status or payment_status or payment_method or date_from or date_to
		),
		'date_from': date_from_raw,
		'date_to': date_to_raw,
		**_page_links(request, page_obj),
	})


@marketplace_permissions_required(
	'market.view_order',
	'market.view_orderitem',
	'market.view_customer',
	'auth.view_user',
	'market.view_product',
	'market.view_vendor',
	'market.view_vendororder',
)
def order_detail(request, order_id):
	order = get_object_or_404(
		Order.objects.select_related('customer__user', 'delivery_window'),
		id=order_id,
	)
	items = list(
		OrderItem.objects.filter(order=order)
		.select_related('product__vendor', 'product__category', 'vendor_order__vendor')
		.order_by('id')
	)
	order_total = Decimal('0.00')
	for item in items:
		item.admin_product_name = (
			item.product_name_snapshot
			or (item.product.name if item.product else 'Product information unavailable')
		)
		item.admin_product_units = (
			item.product_units_snapshot
			or (item.product.units if item.product else '')
		)
		item.admin_vendor_name = (
			(
				f'{item.vendor_order.vendor.shop_name} '
				f'({item.vendor_order.vendor.name})'
				if (
					item.vendor_order.vendor.shop_name
					and item.vendor_order.vendor.shop_name
					!= item.vendor_order.vendor.name
				)
				else item.vendor_order.vendor.name
			)
			if item.vendor_order_id and item.vendor_order.vendor_id
			else 'Sbji@Home platform'
			if item.vendor_order_id
			else item.product.vendor.name
			if item.product_id and item.product.vendor_id
			else 'Unassigned'
		)
		item.admin_line_total = item.unit_price * item.quantity
		order_total += item.admin_line_total
	return render(request, 'market/admin/order_detail.html', {
		'order': order,
		'items': items,
		'order_total': order_total,
		'status_history': OrderStatusHistory.objects.filter(
			order=order,
		).select_related('actor', 'vendor_order__vendor'),
	})