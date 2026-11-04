from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import Notification, Order, OrderStatusHistory, VendorOrder


VENDOR_STATUS_TRANSITIONS = {
	VendorOrder.Status.PENDING: {
		VendorOrder.Status.CONFIRMED,
		VendorOrder.Status.REJECTED,
	},
	VendorOrder.Status.CONFIRMED: {VendorOrder.Status.PREPARING},
	VendorOrder.Status.PREPARING: {VendorOrder.Status.READY_FOR_DELIVERY},
	VendorOrder.Status.READY_FOR_DELIVERY: {VendorOrder.Status.OUT_FOR_DELIVERY},
	VendorOrder.Status.OUT_FOR_DELIVERY: {VendorOrder.Status.DELIVERED},
}


def _create_notification(recipient, kind, title, message, dedupe_key, order=None, vendor_order=None):
	if recipient is None or not recipient.is_active:
		return None
	notification, _ = Notification.objects.get_or_create(
		recipient=recipient,
		dedupe_key=dedupe_key,
		defaults={
			'kind': kind,
			'title': title,
			'message': message,
			'order': order,
			'vendor_order': vendor_order,
		},
	)
	return notification


def _notify_customer(order, kind, title, message, event_key, vendor_order=None):
	return _create_notification(
		order.customer.user,
		kind,
		title,
		message,
		f'order:{order.id}:{event_key}:customer',
		order=order,
		vendor_order=vendor_order,
	)


def _notify_admins(order, kind, title, message, event_key):
	user_model = get_user_model()
	for user in user_model.objects.filter(is_active=True, is_staff=True).iterator():
		if user.has_perm('market.view_order'):
			_create_notification(
				user,
				kind,
				title,
				message,
				f'order:{order.id}:{event_key}:admin:{user.id}',
				order=order,
			)


def _record_order_status(order, previous_status, new_status, actor=None):
	if previous_status == new_status:
		return False
	if not OrderStatusHistory.objects.filter(
		order=order,
		vendor_order__isnull=True,
		previous_status=previous_status,
		new_status=new_status,
	).exists():
		OrderStatusHistory.objects.create(
			order=order,
			previous_status=previous_status,
			new_status=new_status,
			actor=actor,
		)
	return True


def _notify_customer_order_status(order, status):
	labels = dict(Order.Status.choices)
	label = labels.get(status, status.replace('_', ' ').title())
	messages = {
		Order.Status.CONFIRMED: f'Your order #{order.id} has been confirmed.',
		Order.Status.PREPARING: f'Your order #{order.id} is being prepared.',
		Order.Status.OUT_FOR_DELIVERY: f'Your order #{order.id} is out for delivery.',
		Order.Status.DELIVERED: f'Your order #{order.id} has been delivered.',
		Order.Status.CANCELLED: f'Your order #{order.id} has been cancelled.',
		Order.Status.REJECTED: f'Your order #{order.id} was rejected.',
		Order.Status.PARTIALLY_FULFILLED: (
			f'Order #{order.id} was partially fulfilled. Review the vendor updates for details.'
		),
	}
	message = messages.get(status)
	if message:
		_notify_customer(
			order,
			Notification.Kind.ORDER_STATUS,
			f'Order {label}',
			message,
			f'status:{status}',
		)


def _derive_order_status(order):
	if order.status == Order.Status.CANCELLED:
		return order.status
	statuses = list(
		order.vendor_orders.order_by().values_list('status', flat=True)
	)
	if not statuses:
		return order.status
	rejected_count = statuses.count(VendorOrder.Status.REJECTED)
	active_statuses = [
		status for status in statuses
		if status != VendorOrder.Status.REJECTED
	]
	if not active_statuses:
		return Order.Status.REJECTED
	if all(status == VendorOrder.Status.DELIVERED for status in active_statuses):
		return (
			Order.Status.PARTIALLY_FULFILLED
			if rejected_count
			else Order.Status.DELIVERED
		)
	progress = {
		VendorOrder.Status.PENDING: 0,
		VendorOrder.Status.CONFIRMED: 1,
		VendorOrder.Status.PREPARING: 2,
		VendorOrder.Status.READY_FOR_DELIVERY: 2,
		VendorOrder.Status.OUT_FOR_DELIVERY: 3,
		VendorOrder.Status.DELIVERED: 4,
	}
	minimum_progress = min(progress[status] for status in active_statuses)
	return {
		0: Order.Status.PENDING,
		1: Order.Status.CONFIRMED,
		2: Order.Status.PREPARING,
		3: Order.Status.OUT_FOR_DELIVERY,
		4: Order.Status.DELIVERED,
	}[minimum_progress]


def _sync_order_status(order, actor=None):
	previous_status = order.status
	new_status = _derive_order_status(order)
	if not _record_order_status(order, previous_status, new_status, actor):
		return False
	order.status = new_status
	order.save(update_fields=['status'])
	_notify_customer_order_status(order, new_status)
	return True


def record_order_placed(order, actor=None):
	with transaction.atomic():
		locked_order = Order.objects.select_for_update().select_related(
			'customer__user',
		).get(id=order.id)
		_record_order_status(
			locked_order,
			'',
			Order.Status.PENDING,
			actor,
		)
		if locked_order.payment_method == Order.PaymentMethod.ONLINE:
			message = (
				f'Your order #{locked_order.id} has been placed. '
				'Complete online payment to confirm it.'
			)
			title = 'Order awaiting payment'
		else:
			message = f'Your order #{locked_order.id} has been placed successfully.'
			title = 'Order placed'
		_notify_customer(
			locked_order,
			Notification.Kind.ORDER_PLACED,
			title,
			message,
			'placed',
		)
		return locked_order


def notify_vendors_new_order(order):
	vendor_orders = order.vendor_orders.select_related('vendor__user')
	for vendor_order in vendor_orders:
		if vendor_order.vendor_id is None:
			continue
		vendor_user = vendor_order.vendor.user
		if vendor_user is None:
			continue
		_create_notification(
			vendor_user,
			Notification.Kind.VENDOR_ORDER,
			'New order received',
			f'New order #{order.id} received.',
			f'vendor-order:{vendor_order.id}:received',
			order=order,
			vendor_order=vendor_order,
		)
	_notify_admins(
		order,
		Notification.Kind.ADMIN_ORDER,
		'New marketplace order',
		f'New order #{order.id} was placed.',
		'new-order',
	)


def record_payment_failure(order, payment_id=''):
	attempt_key = payment_id or 'unidentified-attempt'
	message = f'Payment for order #{order.id} failed. You may retry online payment.'
	_notify_customer(
		order,
		Notification.Kind.PAYMENT_FAILED,
		'Payment failed',
		message,
		f'payment-failed:{attempt_key}',
	)
	_notify_admins(
		order,
		Notification.Kind.PAYMENT_FAILED,
		'Payment failed',
		f'Online payment failed for order #{order.id}.',
		f'payment-failed:{attempt_key}',
	)


def set_order_status(order_id, new_status, actor=None):
	with transaction.atomic():
		order = Order.objects.select_for_update().select_related(
			'customer__user',
		).get(id=order_id)
		if (
			new_status != Order.Status.CANCELLED
			or order.payment_method != Order.PaymentMethod.ONLINE
			or order.status in {
				Order.Status.CANCELLED,
				Order.Status.REJECTED,
				Order.Status.PARTIALLY_FULFILLED,
				Order.Status.DELIVERED,
			}
		):
			return order, False
		previous_status = order.status
		if not _record_order_status(order, previous_status, new_status, actor):
			return order, False
		order.status = new_status
		order.save(update_fields=['status'])
		_notify_customer_order_status(order, new_status)
		return order, True


def transition_vendor_order(vendor_order_id, new_status, actor=None):
	with transaction.atomic():
		vendor_order = (
			VendorOrder.objects.select_for_update()
			.select_related('order__customer__user', 'vendor__user')
			.get(id=vendor_order_id)
		)
		if actor is None or not actor.is_active:
			return vendor_order, False
		if vendor_order.vendor_id is None:
			if not actor.is_staff:
				return vendor_order, False
		elif (
			actor is not None
			and not actor.is_staff
			and actor.id != vendor_order.vendor.user_id
		):
			return vendor_order, False
		previous_status = vendor_order.status
		if new_status not in VENDOR_STATUS_TRANSITIONS.get(previous_status, set()):
			return vendor_order, False
		order = Order.objects.select_for_update().get(id=vendor_order.order_id)
		if order.status in {
			Order.Status.CANCELLED,
			Order.Status.REJECTED,
			Order.Status.PARTIALLY_FULFILLED,
			Order.Status.DELIVERED,
		}:
			return vendor_order, False
		vendor_order.status = new_status
		update_fields = ['status']
		if new_status == VendorOrder.Status.DELIVERED:
			vendor_order.delivered_at = timezone.now()
			update_fields.append('delivered_at')
		vendor_order.save(update_fields=update_fields)
		OrderStatusHistory.objects.create(
			order=order,
			vendor_order=vendor_order,
			previous_status=previous_status,
			new_status=new_status,
			actor=actor,
		)
		order = Order.objects.select_related('customer__user').get(id=order.id)
		previous_order_status = order.status
		order_status_changed = _sync_order_status(order, actor)
		if (
			new_status == VendorOrder.Status.REJECTED
			and order.status != Order.Status.REJECTED
		):
			vendor_name = (
				vendor_order.vendor.shop_name or vendor_order.vendor.name
				if vendor_order.vendor_id
				else 'Sbji@Home'
			)
			_notify_customer(
				order,
				Notification.Kind.ORDER_REJECTED,
				'Items unavailable',
				f'Some items from {vendor_name} were unavailable for order #{order.id}.',
				f'vendor-order:{vendor_order.id}:rejected',
				vendor_order=vendor_order,
			)
		if actor.is_staff and vendor_order.vendor_id is not None:
			vendor_user = vendor_order.vendor.user
			_create_notification(
				vendor_user,
				Notification.Kind.ORDER_UPDATED,
				'Order updated',
				f'Order #{order.id} is now {vendor_order.get_status_display().lower()}.',
				f'vendor-order:{vendor_order.id}:status:{new_status}',
				order=order,
				vendor_order=vendor_order,
			)
		if order_status_changed:
			_notify_admins(
				order,
				Notification.Kind.ADMIN_ORDER,
				'Order status updated',
				f'Order #{order.id} is now {order.get_status_display().lower()}.',
				f'status:{order.status}',
			)
		return vendor_order, True


def add_progress_to_order(order):
	statuses = [
		(Order.Status.PENDING, 'Order placed'),
		(Order.Status.CONFIRMED, 'Confirmed'),
		(Order.Status.PREPARING, 'Preparing'),
		(Order.Status.OUT_FOR_DELIVERY, 'Out for delivery'),
		(Order.Status.DELIVERED, 'Delivered'),
	]
	terminal = order.status in {
		Order.Status.CANCELLED,
		Order.Status.REJECTED,
		Order.Status.PARTIALLY_FULFILLED,
	}
	current_index = next(
		(index for index, (status, _) in enumerate(statuses) if status == order.status),
		-1,
	)
	order.progress_steps = [
		{
			'label': label,
			'is_complete': not terminal and index < current_index,
			'is_current': not terminal and index == current_index,
		}
		for index, (_, label) in enumerate(statuses)
	]
	order.is_terminal_status = terminal
	order.is_partially_fulfilled = (
		order.status == Order.Status.PARTIALLY_FULFILLED
	)
	return order