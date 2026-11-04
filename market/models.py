from django.conf import settings
from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.utils import timezone
import uuid
# Create your models here.

class Category(models.Model):
    name=models.CharField(max_length=100)

    def __str__(self):
        return self.name

class Vendor(models.Model):
    name = models.CharField(max_length=100, unique=True)
    shop_name = models.CharField(max_length=100, blank=True)
    shop_address = models.CharField(max_length=250, blank=True)
    business_registration = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='vendor_profile',
    )

    def __str__(self):
        return self.name

class Product(models.Model):
    name=models.CharField(max_length=100)
    description=models.TextField(blank=True)
    price=models.DecimalField(max_digits=10, decimal_places=2)
    stock=models.IntegerField()
    units=models.CharField(max_length=20)
    image=models.ImageField(upload_to='products/', blank=True, null=True)
    category=models.ForeignKey(Category, on_delete=models.CASCADE)
    vendor=models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        related_name='products',
    )

    def __str__(self):
        return self.name

class Customer(models.Model):
    user=models.OneToOneField(User, on_delete=models.CASCADE)
    address=models.CharField(max_length=200)
    phone=models.CharField(max_length=20)

    def __str__(self):
        return self.user.username


class DeliveryAddress(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='delivery_addresses',
    )
    label = models.CharField(max_length=50)
    recipient_name = models.CharField(max_length=150)
    address_line_1 = models.CharField(max_length=200)
    address_line_2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20)
    phone = models.CharField(max_length=20)
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['customer'],
                condition=Q(is_default=True),
                name='unique_default_delivery_address_per_customer',
            ),
        ]

    def __str__(self):
        return f"{self.label} - {self.customer.user.username}"


class CartItem(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cart_items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='cart_items')
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'product'], name='unique_user_cart_product'),
            models.CheckConstraint(condition=Q(quantity__gte=1), name='cart_item_quantity_positive'),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.product.name} - Quantity: {self.quantity}"


class WishlistItem(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='wishlist_items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='wishlist_items',
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['customer', 'product'],
                name='unique_customer_wishlist_product',
            ),
        ]
        ordering = ['-added_at', '-id']

    def __str__(self):
        return f'{self.customer.user.username} - {self.product.name}'


class RecentlyViewedItem(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='recently_viewed_items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='recently_viewed_items',
    )
    viewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['customer', 'product'],
                name='unique_customer_recently_viewed_product',
            ),
        ]
        ordering = ['-viewed_at', '-id']

    def __str__(self):
        return f'{self.customer.user.username} recently viewed {self.product.name}'


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CONFIRMED = 'confirmed', 'Confirmed'
        PREPARING = 'preparing', 'Preparing'
        OUT_FOR_DELIVERY = 'out_for_delivery', 'Out for delivery'
        DELIVERED = 'delivered', 'Delivered'
        CANCELLED = 'cancelled', 'Cancelled'
        REJECTED = 'rejected', 'Rejected'
        PARTIALLY_FULFILLED = 'partially_fulfilled', 'Partially fulfilled'

    class PaymentMethod(models.TextChoices):
        CASH_ON_DELIVERY = 'cod', 'Cash on Delivery'
        ONLINE = 'online', 'Online Payment'

    class PaymentStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    order_date=models.DateTimeField(auto_now_add=True)
    customer=models.ForeignKey(Customer, on_delete=models.CASCADE)
    status=models.CharField(max_length=20, default='pending')
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        blank=True,
        default='',
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
    )
    razorpay_order_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
    )
    razorpay_payment_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
    )
    payment_expires_at = models.DateTimeField(null=True, blank=True)
    stock_reserved = models.BooleanField(default=False)
    checkout_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    delivery_address_label = models.CharField(max_length=50, blank=True, default='')
    delivery_name = models.CharField(max_length=150, blank=True, default='')
    delivery_phone = models.CharField(max_length=20, blank=True, default='')
    delivery_address_line_1 = models.CharField(max_length=200, blank=True, default='')
    delivery_address_line_2 = models.CharField(max_length=200, blank=True, default='')
    delivery_city = models.CharField(max_length=100, blank=True, default='')
    delivery_state = models.CharField(max_length=100, blank=True, default='')
    delivery_postal_code = models.CharField(max_length=20, blank=True, default='')
    delivery_slot_date = models.DateField(null=True, blank=True)
    delivery_slot_name = models.CharField(max_length=100, blank=True, default='')
    delivery_window = models.ForeignKey(
        'DeliveryWindow',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='orders',
    )
    delivery_slot_start_time = models.TimeField(null=True, blank=True)
    delivery_slot_end_time = models.TimeField(null=True, blank=True)

    def __str__(self):
        return f"Order {self.id} - {self.customer.user.username}"

    def get_status_display(self):
        return dict(self.Status.choices).get(
            self.status,
            self.status.replace('_', ' ').title(),
        )


class DeliveryWindow(models.Model):
    name = models.CharField(max_length=100, unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    capacity = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['start_time', 'name']
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=models.F('start_time')),
                name='delivery_window_end_after_start',
            ),
            models.CheckConstraint(
                condition=Q(capacity__gt=0),
                name='delivery_window_capacity_positive',
            ),
        ]

    def __str__(self):
        return f'{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})'


class VendorOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CONFIRMED = 'confirmed', 'Confirmed'
        PREPARING = 'preparing', 'Preparing'
        READY_FOR_DELIVERY = 'ready_for_delivery', 'Ready for delivery'
        OUT_FOR_DELIVERY = 'out_for_delivery', 'Out for delivery'
        DELIVERED = 'delivered', 'Delivered'
        REJECTED = 'rejected', 'Rejected'

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='vendor_orders',
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='vendor_orders',
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['order', 'vendor'],
                name='unique_order_vendor_fulfillment',
            ),
            models.UniqueConstraint(
                fields=['order'],
                condition=Q(vendor__isnull=True),
                name='unique_platform_fulfillment_per_order',
            ),
        ]
        ordering = ['-order__order_date', '-order_id']

    def __str__(self):
        vendor_name = (
            self.vendor.shop_name or self.vendor.name
            if self.vendor_id
            else 'Sbji@Home platform'
        )
        return f"Order {self.order_id} - {vendor_name} - {self.status}"


class OrderStatusHistory(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='status_history',
    )
    vendor_order = models.ForeignKey(
        VendorOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='status_history',
    )
    previous_status = models.CharField(max_length=30, blank=True, default='')
    new_status = models.CharField(max_length=30)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='market_order_status_changes',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    @property
    def previous_status_label(self):
        return self._status_label(self.previous_status) if self.previous_status else 'Order placed'

    @property
    def new_status_label(self):
        return self._status_label(self.new_status)

    def _status_label(self, value):
        choices = (
            VendorOrder.Status.choices
            if self.vendor_order_id
            else Order.Status.choices
        )
        return dict(choices).get(value, value.replace('_', ' ').title())

    def __str__(self):
        return f'Order {self.order_id}: {self.previous_status} -> {self.new_status}'


class Notification(models.Model):
    class Kind(models.TextChoices):
        ORDER_PLACED = 'order_placed', 'Order placed'
        ORDER_STATUS = 'order_status', 'Order status'
        ORDER_REJECTED = 'order_rejected', 'Order rejected'
        VENDOR_ORDER = 'vendor_order', 'Vendor order'
        ORDER_UPDATED = 'order_updated', 'Order updated'
        PAYMENT_FAILED = 'payment_failed', 'Payment failed'
        ADMIN_ORDER = 'admin_order', 'Marketplace order'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='market_notifications',
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications',
    )
    vendor_order = models.ForeignKey(
        VendorOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications',
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    title = models.CharField(max_length=120)
    message = models.CharField(max_length=300)
    dedupe_key = models.CharField(max_length=180)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['recipient', 'dedupe_key'],
                name='unique_recipient_notification_event',
            ),
        ]

    @property
    def is_read(self):
        return self.read_at is not None

    def __str__(self):
        return f'{self.title} for {self.recipient.username}'

class OrderItem(models.Model):
    order=models.ForeignKey(Order, on_delete=models.CASCADE)
    vendor_order=models.ForeignKey(
        VendorOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    product_name_snapshot = models.CharField(max_length=100, blank=True, default='')
    product_units_snapshot = models.CharField(max_length=20, blank=True, default='')
    quantity=models.IntegerField()
    unit_price=models.DecimalField(max_digits=10, decimal_places=2)

    @property
    def line_total(self):
        return self.unit_price * self.quantity

    def save(self, *args, **kwargs):
        if self.product_id and (
            not self.product_name_snapshot or not self.product_units_snapshot
        ):
            product = self.product
            if product is not None:
                self.product_name_snapshot = (
                    self.product_name_snapshot or product.name
                )
                self.product_units_snapshot = (
                    self.product_units_snapshot or product.units
                )
        super().save(*args, **kwargs)

    def __str__(self):
        product_name = self.product_name_snapshot
        if self.product_id:
            product_name = self.product.name
        return f"Order {self.order.id} - {product_name} - Quantity: {self.quantity}"
    