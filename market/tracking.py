"""Delivery tracking helpers: turn a VendorOrder into a step-by-step timeline."""
from .models import VendorOrder


TRACKING_STEPS = [
    (VendorOrder.Status.PENDING, 'Order placed'),
    (VendorOrder.Status.CONFIRMED, 'Shop confirmed'),
    (VendorOrder.Status.PREPARING, 'Packing your items'),
    (VendorOrder.Status.READY_FOR_DELIVERY, 'Ready for delivery'),
    (VendorOrder.Status.OUT_FOR_DELIVERY, 'Out for delivery'),
    (VendorOrder.Status.DELIVERED, 'Delivered'),
]


def build_tracking(vendor_order, order_date):
    """Return the timeline steps for one shop's part of an order.

    Each step is a dict: label, time (datetime or None), is_done, is_current.
    Uses the prefetched status_history, so it adds no queries per step.
    """
    reached = {VendorOrder.Status.PENDING: order_date}
    for change in vendor_order.status_history.all():
        reached[change.new_status] = change.created_at

    keys = [status for status, _ in TRACKING_STEPS]
    if vendor_order.status not in keys:  # rejected: no delivery timeline
        return []
    current = keys.index(vendor_order.status)
    delivered = vendor_order.status == VendorOrder.Status.DELIVERED
    return [
        {
            'label': label,
            'time': reached.get(status),
            'is_done': index < current or (delivered and index == current),
            'is_current': index == current and not delivered,
        }
        for index, (status, label) in enumerate(TRACKING_STEPS)
    ]
