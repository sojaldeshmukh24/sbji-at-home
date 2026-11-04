import base64
from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Order, OrderItem, Product, VendorOrder
from .order_lifecycle import (
    notify_vendors_new_order,
    record_payment_failure,
    set_order_status,
)


PAYMENT_RESERVATION_MINUTES = 15
PAYMENT_ID_PATTERN = re.compile(r'^pay_[A-Za-z0-9]+$')


class PaymentConfigurationError(Exception):
    pass


class PaymentGatewayError(Exception):
    pass


class PaymentStockError(Exception):
    pass


class WebhookSignatureError(Exception):
    pass


class WebhookPayloadError(Exception):
    pass


def razorpay_test_credentials():
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '')
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '')
    if not key_id or not key_secret or not key_id.startswith('rzp_test_'):
        raise PaymentConfigurationError(
            'Online payments require Razorpay test-mode credentials.',
        )
    return key_id, key_secret


def verify_razorpay_webhook_signature(raw_body, signature):
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '')
    webhook_secret = getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', '')
    if not key_id or not key_id.startswith('rzp_test_') or not webhook_secret:
        raise PaymentConfigurationError(
            'Razorpay test-mode webhook configuration is incomplete.',
        )
    if not signature:
        raise WebhookSignatureError('Razorpay webhook signature is missing.')
    expected = hmac.new(
        webhook_secret.encode('utf-8'),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookSignatureError('Razorpay webhook signature is invalid.')


def process_razorpay_webhook(raw_body, signature):
    verify_razorpay_webhook_signature(raw_body, signature)
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WebhookPayloadError('Razorpay webhook payload is malformed.') from error
    if not isinstance(payload, dict):
        raise WebhookPayloadError('Razorpay webhook payload is malformed.')

    event = payload.get('event')
    if not isinstance(event, str):
        raise WebhookPayloadError('Razorpay webhook event is malformed.')
    event_status = {
        'payment.captured': 'captured',
        'payment.failed': 'failed',
    }.get(event)
    if event_status is None:
        return 'ignored'

    try:
        payment = payload['payload']['payment']['entity']
    except (KeyError, TypeError):
        raise WebhookPayloadError('Razorpay payment event is malformed.')
    if not isinstance(payment, dict):
        raise WebhookPayloadError('Razorpay payment event is malformed.')

    payment_id = payment.get('id')
    provider_order_id = payment.get('order_id')
    if (
        not isinstance(payment_id, str)
        or not PAYMENT_ID_PATTERN.fullmatch(payment_id)
        or not isinstance(provider_order_id, str)
        or not provider_order_id.startswith('order_')
        or payment.get('status') != event_status
    ):
        return 'ignored'

    with transaction.atomic():
        matching_orders = list(
            Order.objects.select_for_update()
            .filter(
                payment_method=Order.PaymentMethod.ONLINE,
                razorpay_order_id=provider_order_id,
            )
            .order_by('id')[:2]
        )
        if len(matching_orders) != 1:
            return 'ignored'
        order = matching_orders[0]
        if (
            type(payment.get('amount')) is not int
            or payment.get('amount') != amount_in_paise(order_total(order))
            or payment.get('currency') != 'INR'
        ):
            return 'ignored'

        if order.payment_status == Order.PaymentStatus.PAID:
            return (
                'processed'
                if order.razorpay_payment_id == payment_id
                else 'ignored'
            )
        if order.payment_status != Order.PaymentStatus.PENDING:
            return 'ignored'

        if event_status == 'captured':
            _mark_paid(order, payment_id)
            return 'processed'

        if order.razorpay_payment_id not in ('', payment_id):
            return 'ignored'
        _record_failed(order, payment_id)
        return 'processed'


def razorpay_api_request(method, path, payload=None):
    key_id, key_secret = razorpay_test_credentials()
    token = base64.b64encode(
        f'{key_id}:{key_secret}'.encode('utf-8'),
    ).decode('ascii')
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    request = Request(
        f'https://api.razorpay.com/v1/{path.lstrip("/")}',
        data=body,
        headers={
            'Authorization': f'Basic {token}',
            'Content-Type': 'application/json',
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        raise PaymentGatewayError(
            'Razorpay test mode could not complete the request.',
        ) from error
    if not isinstance(result, dict):
        raise PaymentGatewayError('Razorpay returned an invalid response.')
    return result


def order_total(order):
    return sum(
        (
            item.unit_price * item.quantity
            for item in OrderItem.objects.filter(order=order)
        ),
        Decimal('0.00'),
    )


def amount_in_paise(amount):
    paise = amount * 100
    if paise != paise.to_integral_value() or paise <= 0:
        raise PaymentGatewayError('The order amount cannot be paid online.')
    return int(paise)


def _release_stock(order):
    if not order.stock_reserved:
        return
    for item in OrderItem.objects.filter(order=order).order_by('product_id'):
        Product.objects.filter(id=item.product_id).update(
            stock=F('stock') + item.quantity,
        )
    order.stock_reserved = False


def _reserve_stock(order):
    if order.stock_reserved:
        return
    items = list(
        OrderItem.objects.filter(order=order)
        .select_related('product')
        .order_by('product_id')
    )
    products = {
        product.id: product
        for product in Product.objects.select_for_update()
        .filter(id__in=[item.product_id for item in items])
        .order_by('id')
    }
    for item in items:
        product = products.get(item.product_id)
        if product is None or product.stock < item.quantity:
            raise PaymentStockError(
                'Some items are no longer available in the required quantity.',
            )
    for item in items:
        changed = Product.objects.filter(
            id=item.product_id,
            stock__gte=item.quantity,
        ).update(stock=F('stock') - item.quantity)
        if changed != 1:
            raise PaymentStockError(
                'Some items are no longer available in the required quantity.',
            )
    order.stock_reserved = True


def _create_vendor_fulfillments(order):
    items = list(
        OrderItem.objects.filter(order=order)
        .select_related('product')
        .order_by('product__vendor_id', 'id')
    )
    vendor_orders = {}
    for item in items:
        if item.product is None:
            continue
        vendor_id = item.product.vendor_id
        if vendor_id not in vendor_orders:
            vendor_order, _ = VendorOrder.objects.get_or_create(
                order=order,
                vendor_id=vendor_id,
            )
            vendor_orders[vendor_id] = vendor_order
        if item.vendor_order_id != vendor_orders[vendor_id].id:
            item.vendor_order = vendor_orders[vendor_id]
            item.save(update_fields=['vendor_order'])


def _mark_paid(order, payment_id):
    if order.payment_status == Order.PaymentStatus.PAID:
        if order.razorpay_payment_id != payment_id:
            raise PaymentGatewayError('This order was paid with a different payment.')
        return
    if order.payment_status in (
        Order.PaymentStatus.CANCELLED,
        Order.PaymentStatus.EXPIRED,
    ):
        raise PaymentGatewayError('This payment order is no longer active.')
    _reserve_stock(order)
    order.payment_status = Order.PaymentStatus.PAID
    order.razorpay_payment_id = payment_id
    order.payment_expires_at = None
    order.save(update_fields=[
        'payment_status',
        'razorpay_payment_id',
        'payment_expires_at',
        'stock_reserved',
    ])
    _create_vendor_fulfillments(order)
    notify_vendors_new_order(order)


def _record_failed(order, payment_id=''):
    if order.payment_status != Order.PaymentStatus.PENDING:
        return
    if payment_id:
        order.razorpay_payment_id = payment_id
    _release_stock(order)
    order.payment_status = Order.PaymentStatus.FAILED
    order.save(update_fields=[
        'payment_status',
        'razorpay_payment_id',
        'stock_reserved',
    ])
    record_payment_failure(order, payment_id)


def ensure_razorpay_order(order_id, customer_id):
    key_id, _ = razorpay_test_credentials()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(
            id=order_id,
            customer_id=customer_id,
            payment_method=Order.PaymentMethod.ONLINE,
        )
        if order.payment_status in (
            Order.PaymentStatus.PAID,
            Order.PaymentStatus.CANCELLED,
            Order.PaymentStatus.EXPIRED,
        ):
            raise PaymentGatewayError('This payment order is no longer active.')
        if (
            order.payment_status == Order.PaymentStatus.PENDING
            and order.payment_expires_at is not None
            and order.payment_expires_at <= timezone.now()
        ):
            raise PaymentGatewayError('This payment session has expired.')
        if order.payment_status == Order.PaymentStatus.FAILED:
            _reserve_stock(order)
            order.payment_status = Order.PaymentStatus.PENDING
            order.payment_expires_at = (
                timezone.now() + timedelta(minutes=PAYMENT_RESERVATION_MINUTES)
            )
            order.save(update_fields=[
                'payment_status',
                'payment_expires_at',
                'stock_reserved',
            ])
        if order.razorpay_order_id:
            return {
                'key_id': key_id,
                'order_id': order.razorpay_order_id,
                'amount': amount_in_paise(order_total(order)),
            }
        amount = amount_in_paise(order_total(order))
        result = razorpay_api_request(
            'POST',
            'orders',
            {
                'amount': amount,
                'currency': 'INR',
                'receipt': f'market-order-{order.id}',
                'notes': {'marketplace_order_id': str(order.id)},
            },
        )
        gateway_order_id = result.get('id')
        if (
            not isinstance(gateway_order_id, str)
            or not gateway_order_id.startswith('order_')
            or result.get('amount') != amount
            or result.get('currency') != 'INR'
        ):
            raise PaymentGatewayError('Razorpay returned a mismatched order.')
        order.razorpay_order_id = gateway_order_id
        if order.payment_expires_at is None:
            order.payment_expires_at = (
                timezone.now() + timedelta(minutes=PAYMENT_RESERVATION_MINUTES)
            )
        order.save(update_fields=['razorpay_order_id', 'payment_expires_at'])
        return {
            'key_id': key_id,
            'order_id': gateway_order_id,
            'amount': amount,
        }


def verify_razorpay_signature(order_id, payment_id, signature):
    _, key_secret = razorpay_test_credentials()
    if not PAYMENT_ID_PATTERN.fullmatch(payment_id or '') or not signature:
        raise PaymentGatewayError('Razorpay returned invalid payment details.')
    message = f'{order_id}|{payment_id}'.encode('utf-8')
    expected = hmac.new(
        key_secret.encode('utf-8'),
        message,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PaymentGatewayError('Razorpay payment signature is invalid.')


def verify_razorpay_payment(order_id, customer_id, provider_order_id, payment_id, signature):
    if not PAYMENT_ID_PATTERN.fullmatch(payment_id or ''):
        raise PaymentGatewayError('Razorpay returned an invalid payment ID.')
    with transaction.atomic():
        order = Order.objects.select_for_update().get(
            id=order_id,
            customer_id=customer_id,
            payment_method=Order.PaymentMethod.ONLINE,
        )
        if order.razorpay_order_id != provider_order_id:
            raise PaymentGatewayError('Razorpay order does not match this order.')
        if order.payment_status in (
            Order.PaymentStatus.CANCELLED,
            Order.PaymentStatus.EXPIRED,
        ):
            raise PaymentGatewayError('This payment order is no longer active.')
        if order.payment_status == Order.PaymentStatus.PAID:
            if order.razorpay_payment_id != payment_id:
                raise PaymentGatewayError('This order was paid with a different payment.')
            return Order.PaymentStatus.PAID
        verify_razorpay_signature(provider_order_id, payment_id, signature)
        result = razorpay_api_request(
            'GET',
            f'payments/{quote(payment_id, safe="")}',
        )
        if (
            result.get('id') != payment_id
            or result.get('order_id') != provider_order_id
            or result.get('amount') != amount_in_paise(order_total(order))
            or result.get('currency') != 'INR'
        ):
            raise PaymentGatewayError('Razorpay payment does not match this order.')
        status = result.get('status')
        if status == 'captured':
            _mark_paid(order, payment_id)
            return Order.PaymentStatus.PAID
        if status == 'failed':
            _record_failed(order, payment_id)
            return Order.PaymentStatus.FAILED
        return Order.PaymentStatus.PENDING


def record_razorpay_failure(order_id, customer_id, provider_order_id, payment_id):
    if not PAYMENT_ID_PATTERN.fullmatch(payment_id or ''):
        raise PaymentGatewayError('Razorpay returned an invalid payment ID.')
    with transaction.atomic():
        order = Order.objects.select_for_update().get(
            id=order_id,
            customer_id=customer_id,
            payment_method=Order.PaymentMethod.ONLINE,
        )
        if order.razorpay_order_id != provider_order_id:
            raise PaymentGatewayError('Razorpay order does not match this order.')
        result = razorpay_api_request(
            'GET',
            f'payments/{quote(payment_id, safe="")}',
        )
        if (
            result.get('id') != payment_id
            or result.get('order_id') != provider_order_id
            or result.get('amount') != amount_in_paise(order_total(order))
            or result.get('currency') != 'INR'
            or result.get('status') != 'failed'
        ):
            raise PaymentGatewayError('Razorpay has not confirmed a failed payment.')
        _record_failed(order, payment_id)
        return order.payment_status


def cancel_razorpay_order(order_id, customer_id, actor=None):
    with transaction.atomic():
        order = Order.objects.select_for_update().get(
            id=order_id,
            customer_id=customer_id,
            payment_method=Order.PaymentMethod.ONLINE,
        )
        if order.payment_status in (
            Order.PaymentStatus.PAID,
            Order.PaymentStatus.CANCELLED,
            Order.PaymentStatus.EXPIRED,
        ):
            return order.payment_status
        if order.razorpay_order_id:
            payments = razorpay_api_request(
                'GET',
                f'orders/{quote(order.razorpay_order_id, safe="")}/payments',
            ).get('items', [])
            for payment in payments:
                if (
                    payment.get('order_id') == order.razorpay_order_id
                    and payment.get('status') == 'captured'
                    and payment.get('amount') == amount_in_paise(order_total(order))
                    and payment.get('currency') == 'INR'
                ):
                    _mark_paid(order, payment['id'])
                    return order.payment_status
        _release_stock(order)
        order.payment_status = Order.PaymentStatus.CANCELLED
        order.payment_expires_at = None
        order.save(update_fields=[
            'payment_status',
            'payment_expires_at',
            'stock_reserved',
        ])
        set_order_status(order.id, Order.Status.CANCELLED, actor=actor)
        return order.payment_status


def expire_pending_razorpay_orders():
    now = timezone.now()
    expired_ids = list(
        Order.objects.filter(
            payment_method=Order.PaymentMethod.ONLINE,
            payment_status=Order.PaymentStatus.PENDING,
            payment_expires_at__lte=now,
        ).values_list('id', flat=True)
    )
    expired_count = 0
    for order_id in expired_ids:
        with transaction.atomic():
            order = (
                Order.objects.select_for_update()
                .filter(
                    id=order_id,
                    payment_method=Order.PaymentMethod.ONLINE,
                    payment_status=Order.PaymentStatus.PENDING,
                    payment_expires_at__lte=now,
                )
                .first()
            )
            if order is None:
                continue
            if order.razorpay_order_id:
                payments = razorpay_api_request(
                    'GET',
                    f'orders/{quote(order.razorpay_order_id, safe="")}/payments',
                ).get('items', [])
                captured = next(
                    (
                        payment for payment in payments
                        if (
                            payment.get('order_id') == order.razorpay_order_id
                            and payment.get('status') == 'captured'
                            and payment.get('amount') == amount_in_paise(order_total(order))
                            and payment.get('currency') == 'INR'
                        )
                    ),
                    None,
                )
                if captured is not None:
                    _mark_paid(order, captured['id'])
                    continue
            _release_stock(order)
            order.payment_status = Order.PaymentStatus.EXPIRED
            order.save(update_fields=[
                'payment_status',
                'stock_reserved',
            ])
            set_order_status(order.id, Order.Status.CANCELLED)
            expired_count += 1
    return expired_count
