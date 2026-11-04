from decimal import Decimal

from django import forms
from django.contrib import admin
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone

# Register your models here.
from .models import (
    Category,
    Customer,
    DeliveryWindow,
    Notification,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    Vendor,
    VendorOrder,
    WishlistItem,
)
from .order_lifecycle import transition_vendor_order

admin.site.register(WishlistItem)


class MarketplaceProductAdminForm(forms.ModelForm):
    price = forms.DecimalField(
        min_value=Decimal('0.01'),
        max_digits=10,
        decimal_places=2,
    )
    stock = forms.IntegerField(min_value=0)

    class Meta:
        model = Product
        fields = '__all__'


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = MarketplaceProductAdminForm
    list_display = ('name', 'vendor', 'category', 'price', 'units', 'stock')
    list_filter = ('category', 'vendor')
    search_fields = ('name', 'description', 'vendor__name', 'vendor__shop_name')
    list_select_related = ('vendor', 'category')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'product_count')
    search_fields = ('name',)
    ordering = ('name',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(admin_product_count=Count('product'))

    @admin.display(description='Products')
    def product_count(self, category):
        return category.admin_product_count

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('username', 'customer_name', 'email', 'phone', 'account_active', 'order_count')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name', 'phone')
    list_filter = ('user__is_active',)
    list_select_related = ('user',)
    readonly_fields = ('user', 'address', 'phone')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user').annotate(
            admin_order_count=Count('order', distinct=True),
        )

    @admin.display(ordering='user__username', description='Username')
    def username(self, customer):
        return customer.user.username

    @admin.display(description='Name')
    def customer_name(self, customer):
        return customer.user.get_full_name().strip() or 'Not provided'

    @admin.display(ordering='user__email', description='Email')
    def email(self, customer):
        return customer.user.email

    @admin.display(boolean=True, ordering='user__is_active', description='Active')
    def account_active(self, customer):
        return customer.user.is_active

    @admin.display(description='Orders')
    def order_count(self, customer):
        return customer.admin_order_count

@admin.register(DeliveryWindow)
class DeliveryWindowAdmin(admin.ModelAdmin):
    list_display = ('name', 'start_time', 'end_time', 'capacity', 'is_active')
    list_filter = ('is_active',)
    ordering = ('start_time', 'name')

@admin.register(VendorOrder)
class VendorOrderAdmin(admin.ModelAdmin):
    list_display = ('order', 'fulfillment_owner', 'status', 'delivered_at')
    list_filter = ('status',)
    search_fields = ('order__id', 'vendor__name', 'vendor__shop_name')
    readonly_fields = ('status', 'delivered_at')
    actions = (
        'confirm_platform_fulfillments',
        'prepare_platform_fulfillments',
        'ready_platform_fulfillments',
        'reject_platform_fulfillments',
        'mark_out_for_delivery',
        'mark_delivered',
    )

    @admin.display(description='Fulfillment owner')
    def fulfillment_owner(self, vendor_order):
        if vendor_order.vendor_id is None:
            return 'Sbji@Home platform'
        return vendor_order.vendor.shop_name or vendor_order.vendor.name

    def _transition_platform_fulfillments(
        self,
        request,
        queryset,
        new_status,
        action_description,
        restock=False,
    ):
        updated = 0
        skipped = 0
        with transaction.atomic():
            for vendor_order in queryset.select_for_update().filter(vendor__isnull=True):
                items = list(
                    vendor_order.items.values_list('product_id', 'quantity'),
                )
                products = {}
                if restock:
                    product_ids = {product_id for product_id, _ in items}
                    if None in product_ids:
                        skipped += 1
                        continue
                    products = {
                        product.id: product
                        for product in Product.objects.select_for_update()
                        .filter(id__in=product_ids)
                        .order_by('id')
                    }
                    if len(products) != len(product_ids):
                        skipped += 1
                        continue
                _, changed = transition_vendor_order(
                    vendor_order.id,
                    new_status,
                    actor=request.user,
                )
                if not changed:
                    continue
                if restock:
                    for product_id, quantity in items:
                        Product.objects.filter(id=product_id).update(
                            stock=F('stock') + quantity,
                        )
                updated += 1
        self.message_user(
            request,
            f'{updated} platform fulfillment(s) {action_description}. '
            f'{skipped} skipped because product records are unavailable.',
        )

    @admin.action(description='Confirm pending platform fulfillments')
    def confirm_platform_fulfillments(self, request, queryset):
        self._transition_platform_fulfillments(
            request,
            queryset,
            VendorOrder.Status.CONFIRMED,
            'confirmed',
        )

    @admin.action(description='Start preparing platform fulfillments')
    def prepare_platform_fulfillments(self, request, queryset):
        self._transition_platform_fulfillments(
            request,
            queryset,
            VendorOrder.Status.PREPARING,
            'marked preparing',
        )

    @admin.action(description='Mark preparing platform fulfillments ready')
    def ready_platform_fulfillments(self, request, queryset):
        self._transition_platform_fulfillments(
            request,
            queryset,
            VendorOrder.Status.READY_FOR_DELIVERY,
            'marked ready for delivery',
        )

    @admin.action(description='Reject pending platform fulfillments and return stock')
    def reject_platform_fulfillments(self, request, queryset):
        self._transition_platform_fulfillments(
            request,
            queryset,
            VendorOrder.Status.REJECTED,
            'rejected and returned to stock',
            restock=True,
        )

    @admin.action(description='Move ready orders to out for delivery')
    def mark_out_for_delivery(self, request, queryset):
        updated = 0
        with transaction.atomic():
            for vendor_order in queryset.select_for_update():
                _, changed = transition_vendor_order(
                    vendor_order.id,
                    VendorOrder.Status.OUT_FOR_DELIVERY,
                    actor=request.user,
                )
                updated += int(changed)
        self.message_user(
            request,
            f'{updated} vendor order(s) moved to out for delivery.',
        )

    @admin.action(description='Mark out-for-delivery orders as delivered')
    def mark_delivered(self, request, queryset):
        updated = 0
        with transaction.atomic():
            for vendor_order in queryset.select_for_update():
                _, changed = transition_vendor_order(
                    vendor_order.id,
                    VendorOrder.Status.DELIVERED,
                    actor=request.user,
                )
                updated += int(changed)
        self.message_user(request, f'{updated} vendor order(s) marked as delivered.')

@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop_name', 'email', 'phone', 'product_count', 'order_count', 'account_active')
    search_fields = ('name', 'shop_name', 'email', 'phone', 'user__username')
    list_filter = ('user__is_active',)
    list_select_related = ('user',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            admin_product_count=Count('products', distinct=True),
            admin_order_count=Count('vendor_orders', distinct=True),
    )

    @admin.display(description='Products')
    def product_count(self, vendor):
        return vendor.admin_product_count

    @admin.display(description='Orders')
    def order_count(self, vendor):
        return vendor.admin_order_count

    @admin.display(boolean=True, description='Active')
    def account_active(self, vendor):
        return bool(vendor.user and vendor.user.is_active)

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = (
        'product_name_snapshot',
        'product_units_snapshot',
        'vendor_order',
        'quantity',
        'unit_price',
        'line_total',
    )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Line total')
    def line_total(self, item):
        return item.line_total

    fields = readonly_fields


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    inlines = [OrderItemInline]
    list_display = ('id', 'customer', 'order_date', 'status', 'payment_method', 'payment_status')
    list_filter = ('status', 'payment_method', 'payment_status')
    search_fields = ('id', 'customer__user__username', 'customer__user__email')
    list_select_related = ('customer__user',)
    readonly_fields = tuple(field.name for field in Order._meta.fields)


@admin.register(OrderStatusHistory)
class OrderStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ('order', 'previous_status_label', 'new_status_label', 'vendor_order', 'actor', 'created_at')
    list_filter = ('new_status', 'created_at')
    search_fields = ('order__id', 'actor__username', 'vendor_order__vendor__name')
    list_select_related = ('order', 'vendor_order__vendor', 'actor')
    readonly_fields = tuple(field.name for field in OrderStatusHistory._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'kind', 'title', 'order', 'created_at', 'read_at')
    list_filter = ('kind', 'read_at', 'created_at')
    search_fields = ('recipient__username', 'title', 'message', 'order__id')
    list_select_related = ('recipient', 'order')
    readonly_fields = tuple(field.name for field in Notification._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
