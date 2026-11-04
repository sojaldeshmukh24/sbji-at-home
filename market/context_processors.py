from django.db.models import Sum

from .models import CartItem, Customer, DeliveryAddress, Notification


def marketplace_header(request):
    cart_count = 0
    unread_notification_count = 0
    delivery_area = 'Choose your delivery area'
    delivery_area_url = 'login'

    if request.user.is_authenticated:
        unread_notification_count = Notification.objects.filter(
            recipient=request.user,
            read_at__isnull=True,
        ).count()
        cart_count = (
            CartItem.objects.filter(user=request.user)
            .aggregate(total=Sum('quantity'))['total']
            or 0
        )
        delivery_address = (
            DeliveryAddress.objects.filter(
                customer__user_id=request.user.id,
                is_default=True,
            )
            .values('city', 'state', 'address_line_1')
            .first()
        )
        if delivery_address:
            delivery_area = (
                f'{delivery_address["city"]}, {delivery_address["state"]}'
                if delivery_address['city'] or delivery_address['state']
                else delivery_address['address_line_1']
            )
            delivery_area_url = 'customer_profile'
        else:
            customer_address = Customer.objects.filter(
                user_id=request.user.id,
            ).values_list('address', flat=True).first()
            if customer_address:
                delivery_area = customer_address
                delivery_area_url = 'customer_profile'
            else:
                delivery_area = 'Add your delivery address'
                delivery_area_url = 'checkout'
    else:
        cart = request.session.get('cart', {})
        if isinstance(cart, dict):
            for quantity in cart.values():
                try:
                    parsed_quantity = int(quantity)
                except (TypeError, ValueError):
                    continue
                if parsed_quantity > 0:
                    cart_count += parsed_quantity

    return {
        'header_cart_count': cart_count,
        'header_unread_notification_count': unread_notification_count,
        'header_delivery_area': delivery_area,
        'header_delivery_area_url': delivery_area_url,
    }
