from decimal import Decimal
from datetime import time, timedelta
import hashlib
import hmac
import json
from io import BytesIO
import tempfile
import uuid
from unittest.mock import patch

from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase
from django.test import RequestFactory
from django.test.utils import override_settings

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from SbjiAtHome.settings import (
    _allowed_hosts,
    _environment_flag,
    _email_mailer,
    _default_from_email,
    _non_negative_integer,
    _secret_key,
    _trusted_proxy_ssl_header,
)
from .admin import MarketplaceProductAdminForm
from .payments import amount_in_paise, expire_pending_razorpay_orders

from .models import (
    CartItem,
    Category,
    Customer,
    DeliveryAddress,
    DeliveryWindow,
    Notification,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    RecentlyViewedItem,
    Vendor,
    VendorOrder,
    WishlistItem,
)


class DjangoSecuritySettingsTests(SimpleTestCase):
    def test_debug_flag_parses_explicit_values_and_rejects_invalid_values(self):
        with patch.dict('os.environ', {'DJANGO_DEBUG': 'false'}):
            self.assertFalse(_environment_flag('DJANGO_DEBUG'))
        with patch.dict('os.environ', {'DJANGO_DEBUG': 'yes'}):
            self.assertTrue(_environment_flag('DJANGO_DEBUG'))
        with patch.dict('os.environ', {'DJANGO_DEBUG': 'sometimes'}):
            with self.assertRaisesMessage(
                ImproperlyConfigured,
                'DJANGO_DEBUG must be set to a boolean value.',
            ):
                _environment_flag('DJANGO_DEBUG')

    def test_production_requires_an_explicit_secret_key(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Set DJANGO_SECRET_KEY when DEBUG is disabled.',
        ):
            _secret_key(False, '')

    def test_development_secret_is_ephemeral_when_unconfigured(self):
        first_key = _secret_key(True, '')
        second_key = _secret_key(True, '')

        self.assertNotEqual(first_key, second_key)
        self.assertGreaterEqual(len(first_key), 50)

    def test_production_requires_explicit_allowed_hosts(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Set DJANGO_ALLOWED_HOSTS when DEBUG is disabled.',
        ):
            _allowed_hosts(False, '')

    def test_allowed_hosts_are_parsed_and_local_defaults_are_development_only(self):
        self.assertEqual(
            _allowed_hosts(False, 'shop.example.com, admin.example.com'),
            ['shop.example.com', 'admin.example.com'],
        )
        self.assertEqual(
            _allowed_hosts(True, ''),
            ['localhost', '127.0.0.1', '[::1]'],
        )

    def test_wildcard_allowed_host_is_rejected(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'DJANGO_ALLOWED_HOSTS must not contain the wildcard host "*".',
        ):
            _allowed_hosts(False, 'market.example.com, *')

    def test_development_email_uses_console_backend(self):
        self.assertEqual(
            _email_mailer(True, {}),
            {
                'BACKEND': 'django.core.mail.backends.console.EmailBackend',
                'OPTIONS': {},
            },
        )
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Set DJANGO_EMAIL_HOST to configure SMTP email.',
        ):
            _email_mailer(True, {
                'DJANGO_EMAIL_BACKEND':
                    'django.core.mail.backends.smtp.EmailBackend',
            })

    def test_production_email_requires_real_backend_configuration(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Set DJANGO_EMAIL_HOST to configure SMTP email.',
        ):
            _email_mailer(False, {})
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Configure a production email backend with DJANGO_EMAIL_BACKEND.',
        ):
            _email_mailer(False, {
                'DJANGO_EMAIL_BACKEND':
                    'django.core.mail.backends.console.EmailBackend',
                'DJANGO_EMAIL_HOST': 'smtp.example.test',
            })
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Set DJANGO_DEFAULT_FROM_EMAIL when DEBUG is disabled.',
        ):
            _default_from_email(False, {})
        self.assertEqual(
            _default_from_email(True, {}),
            'webmaster@localhost',
        )

    def test_production_smtp_settings_use_tls_and_configured_host(self):
        mailer = _email_mailer(False, {
            'DJANGO_EMAIL_HOST': 'smtp.example.test',
            'DJANGO_EMAIL_PORT': '465',
            'DJANGO_EMAIL_HOST_USER': 'service-user',
            'DJANGO_EMAIL_HOST_PASSWORD': 'test-only-placeholder',
            'DJANGO_EMAIL_USE_TLS': 'false',
            'DJANGO_EMAIL_USE_SSL': 'true',
        })

        self.assertEqual(
            mailer['BACKEND'],
            'django.core.mail.backends.smtp.EmailBackend',
        )
        self.assertEqual(mailer['OPTIONS']['host'], 'smtp.example.test')
        self.assertEqual(mailer['OPTIONS']['port'], 465)
        self.assertEqual(mailer['OPTIONS']['username'], 'service-user')
        self.assertEqual(mailer['OPTIONS']['password'], 'test-only-placeholder')
        self.assertTrue(mailer['OPTIONS']['use_ssl'])
        self.assertFalse(mailer['OPTIONS']['use_tls'])
        self.assertEqual(
            _default_from_email(False, {
                'DJANGO_DEFAULT_FROM_EMAIL': 'orders@example.test',
            }),
            'orders@example.test',
        )

    def test_tls_proxy_header_requires_explicit_opt_in(self):
        self.assertIsNone(_trusted_proxy_ssl_header({}))
        self.assertEqual(
            _trusted_proxy_ssl_header({
                'DJANGO_TRUST_X_FORWARDED_PROTO': 'true',
            }),
            ('HTTP_X_FORWARDED_PROTO', 'https'),
        )

    def test_non_negative_integer_environment_setting_is_validated(self):
        self.assertEqual(_non_negative_integer('TEST_SETTING', '3600', 0), 3600)
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'TEST_SETTING must be a non-negative integer.',
        ):
            _non_negative_integer('TEST_SETTING', '-1', 0)

    def test_smtp_rejects_conflicting_tls_modes(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'DJANGO_EMAIL_USE_TLS and DJANGO_EMAIL_USE_SSL cannot both be enabled.',
        ):
            _email_mailer(False, {
                'DJANGO_EMAIL_HOST': 'smtp.example.test',
                'DJANGO_EMAIL_USE_TLS': 'true',
                'DJANGO_EMAIL_USE_SSL': 'true',
            })


class DeliveryAddressModelTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            user=User.objects.create_user(
                username='address-owner',
                password='safe-password',
            ),
            address='Legacy address',
            phone='555-0100',
        )

    def create_address(self, customer, **overrides):
        fields = {
            'customer': customer,
            'label': 'Home',
            'recipient_name': 'Address Owner',
            'address_line_1': '1 Market Street',
            'city': 'Springfield',
            'state': 'State',
            'postal_code': '12345',
            'phone': '555-0100',
        }
        fields.update(overrides)
        return DeliveryAddress.objects.create(**fields)

    def test_address_is_related_to_its_owning_customer(self):
        address = self.create_address(self.customer)

        self.assertEqual(address.customer, self.customer)
        self.assertEqual(
            list(self.customer.delivery_addresses.all()),
            [address],
        )

    def test_each_customer_can_have_one_default_address(self):
        default_address = self.create_address(self.customer, is_default=True)
        another_customer = Customer.objects.create(
            user=User.objects.create_user(
                username='another-address-owner',
                password='safe-password',
            ),
            address='Another legacy address',
            phone='555-0101',
        )
        another_default = self.create_address(
            another_customer,
            is_default=True,
        )

        self.assertTrue(default_address.is_default)
        self.assertTrue(another_default.is_default)

    def test_database_rejects_two_default_addresses_for_one_customer(self):
        self.create_address(self.customer, is_default=True)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_address(
                    self.customer,
                    label='Office',
                    is_default=True,
                )

    def test_customer_can_have_multiple_non_default_addresses(self):
        first = self.create_address(self.customer)
        second = self.create_address(
            self.customer,
            label='Office',
            address_line_1='2 Market Street',
        )

        self.assertEqual(
            set(self.customer.delivery_addresses.all()),
            {first, second},
        )


class CartAndCheckoutTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Produce')
        self.product = Product.objects.create(
            name='Apples',
            price='2.50',
            stock=5,
            units='kg',
            category=self.category,
        )
        self.user = User.objects.create_user(username='shopper', password='safe-password')
        self.customer = Customer.objects.create(
            user=self.user,
            address='1 Market Street',
            phone='555-0100',
        )
        self.delivery_window = DeliveryWindow.objects.create(
            name='Morning delivery',
            start_time=time(9, 0),
            end_time=time(11, 0),
            capacity=10,
        )

    def set_cart(self, cart):
        session = self.client.session
        session['cart'] = cart
        session.save()

    def select_delivery_slot(self, delivery_window=None, delivery_date=None):
        session = self.client.session
        session['checkout_delivery_slot_customer_id'] = self.customer.id
        session['checkout_delivery_slot_date'] = (
            delivery_date or timezone.localdate() + timedelta(days=1)
        ).isoformat()
        session['checkout_delivery_window_id'] = (
            delivery_window or self.delivery_window
        ).id
        session['checkout_order_token_customer_id'] = self.customer.id
        session['checkout_order_token'] = str(uuid.uuid4())
        session.save()

    def post_checkout(self, data=None, follow=False):
        payload = {'payment_method': Order.PaymentMethod.CASH_ON_DELIVERY}
        if data:
            payload.update(data)
        return self.client.post(
            reverse('checkout'),
            payload,
            follow=follow,
        )

    def test_add_to_cart_stores_quantity_in_session(self):
        response = self.client.post(
            reverse('add_to_cart', args=[self.product.id]),
            {'quantity': '2'},
        )

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(self.client.session['cart'], {str(self.product.id): 2})

    def test_add_to_cart_rejects_quantity_above_stock(self):
        self.client.post(
            reverse('add_to_cart', args=[self.product.id]),
            {'quantity': '6'},
        )

        self.assertNotIn('cart', self.client.session)

    def test_guest_cart_persists_across_product_and_cart_navigation(self):
        vendor = Vendor.objects.create(name='Test Vendor')
        Product.objects.filter(id=self.product.id).update(vendor=vendor)

        self.client.post(
            reverse('add_to_cart', args=[self.product.id]),
            {'quantity': '2'},
        )

        product_page = self.client.get(reverse('product_list'))
        detail_page = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )
        cart_page = self.client.get(reverse('cart'))

        self.assertEqual(product_page.status_code, 200)
        self.assertEqual(detail_page.status_code, 200)
        self.assertEqual(cart_page.status_code, 200)
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product.id): 2},
        )
        self.assertContains(cart_page, 'Total: 5.00')

    def test_same_named_vendor_listings_keep_distinct_cart_quantities(self):
        first_vendor = Vendor.objects.create(name='First Apple Vendor')
        second_vendor = Vendor.objects.create(name='Second Apple Vendor')
        first_listing = Product.objects.create(
            name=self.product.name,
            price='2.50',
            stock=5,
            units='kg',
            category=self.category,
            vendor=first_vendor,
        )
        second_listing = Product.objects.create(
            name=self.product.name,
            price='3.25',
            stock=2,
            units='kg',
            category=self.category,
            vendor=second_vendor,
        )

        self.client.post(
            reverse('add_to_cart', args=[first_listing.id]),
            {'quantity': '2'},
        )
        self.client.post(
            reverse('add_to_cart', args=[second_listing.id]),
            {'quantity': '1'},
        )

        self.assertEqual(
            self.client.session['cart'],
            {
                str(first_listing.id): 2,
                str(second_listing.id): 1,
            },
        )
        cart_response = self.client.get(reverse('cart'))
        self.assertContains(cart_response, first_vendor.name)
        self.assertContains(cart_response, second_vendor.name)
        self.assertContains(cart_response, 'Unit price: 2.50 per kg')
        self.assertContains(cart_response, 'Unit price: 3.25 per kg')
        self.assertContains(cart_response, 'Line total: 5.00')
        self.assertContains(cart_response, 'Line total: 3.25')
        self.assertContains(cart_response, 'Total: 8.25')

    def test_cart_groups_items_by_vendor_and_calculates_vendor_subtotals(self):
        first_vendor = Vendor.objects.create(
            name='Group One Produce',
            shop_name='Green Basket',
        )
        second_vendor = Vendor.objects.create(
            name='Group Two Produce',
            shop_name='Market Garden',
        )
        first_listing = Product.objects.create(
            name='Carrots',
            price='2.00',
            stock=10,
            units='kg',
            category=self.category,
            vendor=first_vendor,
        )
        second_listing = Product.objects.create(
            name='Potatoes',
            price='3.00',
            stock=10,
            units='kg',
            category=self.category,
            vendor=first_vendor,
        )
        third_listing = Product.objects.create(
            name='Onions',
            price='4.00',
            stock=10,
            units='kg',
            category=self.category,
            vendor=second_vendor,
        )
        self.set_cart({
            str(first_listing.id): 2,
            str(second_listing.id): 1,
            str(third_listing.id): 3,
        })

        response = self.client.get(reverse('cart'))
        groups = response.context['vendor_groups']

        self.assertEqual(len(groups), 2)
        groups_by_vendor = {group['vendor']: group for group in groups}
        self.assertEqual(len(groups_by_vendor[first_vendor]['items']), 2)
        self.assertEqual(groups_by_vendor[first_vendor]['subtotal'], Decimal('7.00'))
        self.assertEqual(len(groups_by_vendor[second_vendor]['items']), 1)
        self.assertEqual(groups_by_vendor[second_vendor]['subtotal'], Decimal('12.00'))
        self.assertEqual(response.context['total'], Decimal('19.00'))
        self.assertContains(response, 'Green Basket')
        self.assertContains(response, 'Market Garden')
        self.assertContains(response, 'Total: 19.00')

    def test_vendor_group_keeps_out_of_stock_listing_visible_and_disables_checkout(self):
        vendor = Vendor.objects.create(name='Seasonal Farm')
        unavailable_listing = Product.objects.create(
            name='Fresh Peas',
            price='2.50',
            stock=0,
            units='kg',
            category=self.category,
            vendor=vendor,
        )
        self.set_cart({str(unavailable_listing.id): 2})

        response = self.client.get(reverse('cart'))

        self.assertEqual(len(response.context['vendor_groups']), 1)
        group = response.context['vendor_groups'][0]
        self.assertEqual(group['vendor'], vendor)
        self.assertEqual(group['subtotal'], Decimal('5.00'))
        self.assertContains(response, 'Seasonal Farm')
        self.assertContains(response, 'Out of stock.')
        self.assertContains(response, 'Checkout unavailable')
        self.assertNotContains(response, reverse('checkout'))

    def test_cart_quantity_controls_increase_and_decrease_selected_listing(self):
        self.set_cart({str(self.product.id): 2})

        increased = self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(self.product.id),
                'quantity': '2',
                'adjustment': 'increase',
            },
        )
        self.assertRedirects(increased, reverse('cart'))
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product.id): 3},
        )

        decreased = self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(self.product.id),
                'quantity': '3',
                'adjustment': 'decrease',
            },
        )
        self.assertRedirects(decreased, reverse('cart'))
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product.id): 2},
        )

    def test_cart_quantity_can_be_edited_directly_and_saved(self):
        self.set_cart({str(self.product.id): 2})

        cart_page = self.client.get(reverse('cart'))
        response = self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(self.product.id),
                'quantity': '4',
                'update_quantity': '1',
            },
            follow=True,
        )

        self.assertContains(cart_page, 'data-cart-quantity')
        self.assertNotContains(cart_page, 'name="update_quantity" value="1">Update')
        self.assertContains(response, 'Cart quantity updated.')
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product.id): 4},
        )

    def test_empty_cart_has_shopping_guidance(self):
        response = self.client.get(reverse('cart'))

        self.assertContains(response, 'Your cart is empty')
        self.assertContains(response, 'Explore local shops')
        self.assertContains(response, 'Continue shopping')

    def test_authenticated_cart_keeps_vendor_listing_identity(self):
        first_vendor = Vendor.objects.create(name='Persistent First Vendor')
        second_vendor = Vendor.objects.create(name='Persistent Second Vendor')
        first_listing = Product.objects.create(
            name='Tomatoes',
            price='2.00',
            stock=5,
            units='kg',
            category=self.category,
            vendor=first_vendor,
        )
        second_listing = Product.objects.create(
            name='Tomatoes',
            price='2.75',
            stock=8,
            units='kg',
            category=self.category,
            vendor=second_vendor,
        )
        self.client.force_login(self.user)

        self.client.post(
            reverse('add_to_cart', args=[first_listing.id]),
            {'quantity': '2'},
        )
        self.client.post(
            reverse('add_to_cart', args=[second_listing.id]),
            {'quantity': '3'},
        )

        self.assertEqual(
            {
                item.product_id: item.quantity
                for item in CartItem.objects.filter(user=self.user)
            },
            {
                first_listing.id: 2,
                second_listing.id: 3,
            },
        )

    def test_out_of_stock_vendor_listing_does_not_block_another_listing(self):
        available_vendor = Vendor.objects.create(name='Available Listing Vendor')
        unavailable_vendor = Vendor.objects.create(name='Unavailable Listing Vendor')
        available_listing = Product.objects.create(
            name='Lettuce',
            price='1.50',
            stock=3,
            units='head',
            category=self.category,
            vendor=available_vendor,
        )
        unavailable_listing = Product.objects.create(
            name='Lettuce',
            price='1.25',
            stock=0,
            units='head',
            category=self.category,
            vendor=unavailable_vendor,
        )

        response = self.client.get(reverse('product_list'), {'name': 'lettuce'})

        self.assertContains(response, available_vendor.name)
        self.assertContains(response, unavailable_vendor.name)
        self.assertContains(response, 'Out of stock')
        self.assertContains(response, f'max="{available_listing.stock}"')
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[unavailable_listing.id])}"',
        )

        self.client.post(
            reverse('add_to_cart', args=[available_listing.id]),
            {'quantity': '1'},
        )
        self.client.post(
            reverse('add_to_cart', args=[unavailable_listing.id]),
            {'quantity': '1'},
        )
        self.assertEqual(
            self.client.session['cart'],
            {str(available_listing.id): 1},
        )

    def test_cart_and_checkout_pages_render(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)

        cart_response = self.client.get(reverse('cart'))
        checkout_response = self.client.get(reverse('checkout'))

        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(checkout_response.status_code, 200)
        self.assertContains(checkout_response, 'Cash on Delivery')
        self.assertContains(checkout_response, 'No payment is collected now.')
        self.assertContains(checkout_response, 'Place order')
        self.assertContains(checkout_response, 'Confirm where and when to deliver')
        self.assertContains(checkout_response, 'Order summary')
        self.assertContains(checkout_response, 'Select an available slot.')
        self.assertContains(checkout_response, '₹5.00')
        self.assertContains(checkout_response, 'name="payment_method" value="cod" required')
        self.assertContains(checkout_response, 'checkout-address-grid')
        self.assertContains(checkout_response, 'name="address_line_1"')
        self.assertNotContains(checkout_response, 'data-location-picker')
        self.assertNotRegex(
            checkout_response.content.decode(),
            r'id="payment-cod"[^>]*\bchecked\b',
        )

    def test_checkout_prefills_recipient_and_phone_from_customer_profile(self):
        self.user.first_name = 'Pat'
        self.user.last_name = 'Customer'
        self.user.save(update_fields=['first_name', 'last_name'])
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.get(reverse('checkout'))
        address_form = response.context['new_address_form']

        self.assertEqual(address_form.initial['label'], 'Home')
        self.assertEqual(address_form.initial['recipient_name'], 'Pat Customer')
        self.assertEqual(address_form.initial['phone'], self.customer.phone)

    def test_checkout_shows_saved_addresses_and_selects_default(self):
        default_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0100',
            is_default=True,
        )
        other_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Office',
            recipient_name='Shopper',
            address_line_1='14 Office Road',
            city='Springfield',
            state='State',
            postal_code='54321',
            phone='555-0101',
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.get(reverse('checkout'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Home')
        self.assertContains(response, '(Default)')
        self.assertContains(response, 'Office')
        self.assertEqual(
            response.context['selected_delivery_address'],
            default_address,
        )
        self.assertEqual(
            self.client.session['checkout_delivery_address_id'],
            default_address.id,
        )
        self.assertIn('Use this address', response.content.decode())
        self.assertNotEqual(default_address.id, other_address.id)

    def test_checkout_lists_seven_days_of_delivery_windows_and_capacity(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.get(reverse('checkout'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['delivery_slots']), 7)
        self.assertTrue(response.context['delivery_windows_configured'])
        tomorrow_slot = response.context['delivery_slots'][1]['slots'][0]
        self.assertEqual(tomorrow_slot['window'], self.delivery_window)
        self.assertEqual(tomorrow_slot['remaining'], 10)
        self.assertTrue(tomorrow_slot['is_available'])
        self.assertContains(response, 'Morning delivery')
        self.assertContains(response, '10 spot(s) left')

    def test_checkout_delivery_slot_select_is_constrained_to_its_container(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.get(reverse('checkout'))

        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.content.decode(),
            r'\.checkout-section input:not\(\[type="hidden"\]\), \.checkout-section select, \.checkout-section textarea\s*\{[^}]*max-width:\s*100%',
        )
        self.assertRegex(
            response.content.decode(),
            r'\.checkout-section input:not\(\[type="hidden"\]\), \.checkout-section select, \.checkout-section textarea\s*\{[^}]*min-height:\s*44px',
        )

    def test_full_delivery_slot_is_displayed_as_unavailable(self):
        delivery_date = timezone.localdate() + timedelta(days=1)
        self.delivery_window.capacity = 1
        self.delivery_window.save(update_fields=['capacity'])
        Order.objects.create(
            customer=self.customer,
            delivery_slot_date=delivery_date,
            delivery_window=self.delivery_window,
            delivery_slot_name=self.delivery_window.name,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.get(reverse('checkout'))

        slot = next(
            slot
            for slot in response.context['delivery_slots'][1]['slots']
            if slot['window'].id == self.delivery_window.id
        )
        self.assertFalse(slot['is_available'])
        self.assertEqual(slot['remaining'], 0)
        self.assertContains(response, 'Full')

    def test_released_and_terminal_orders_do_not_consume_delivery_capacity(self):
        delivery_date = timezone.localdate() + timedelta(days=1)
        self.delivery_window.capacity = 3
        self.delivery_window.save(update_fields=['capacity'])
        non_booking_orders = (
            {
                'payment_method': Order.PaymentMethod.ONLINE,
                'payment_status': Order.PaymentStatus.FAILED,
            },
            {
                'payment_method': Order.PaymentMethod.ONLINE,
                'payment_status': Order.PaymentStatus.CANCELLED,
                'status': Order.Status.CANCELLED,
            },
            {
                'payment_method': Order.PaymentMethod.ONLINE,
                'payment_status': Order.PaymentStatus.EXPIRED,
            },
            {
                'status': Order.Status.CANCELLED,
            },
            {
                'status': Order.Status.REJECTED,
            },
        )
        for order_fields in non_booking_orders:
            Order.objects.create(
                customer=self.customer,
                delivery_slot_date=delivery_date,
                delivery_window=self.delivery_window,
                **order_fields,
            )
        Order.objects.create(
            customer=self.customer,
            delivery_slot_date=delivery_date,
            delivery_window=self.delivery_window,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.get(reverse('checkout'))

        slot = next(
            slot
            for slot in response.context['delivery_slots'][1]['slots']
            if slot['window'].id == self.delivery_window.id
        )
        self.assertEqual(slot['remaining'], 2)
        self.assertTrue(slot['is_available'])

    def test_customer_can_select_available_dated_delivery_window(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        delivery_date = timezone.localdate() + timedelta(days=2)

        response = self.client.post(
            reverse('select_checkout_delivery_slot'),
            {
                'slot_key': (
                    f'{delivery_date.isoformat()}|{self.delivery_window.id}'
                ),
            },
        )

        self.assertRedirects(response, reverse('checkout'))
        session = self.client.session
        self.assertEqual(
            session['checkout_delivery_slot_customer_id'],
            self.customer.id,
        )
        self.assertEqual(
            session['checkout_delivery_slot_date'],
            delivery_date.isoformat(),
        )
        self.assertEqual(
            session['checkout_delivery_window_id'],
            self.delivery_window.id,
        )
        checkout_response = self.client.get(reverse('checkout'))
        self.assertEqual(
            checkout_response.context['selected_delivery_slot']['date'],
            delivery_date,
        )

    def test_checkout_rejects_delivery_slot_that_filled_after_selection(self):
        delivery_date = timezone.localdate() + timedelta(days=1)
        self.delivery_window.capacity = 1
        self.delivery_window.save(update_fields=['capacity'])
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        slot_selection = self.client.post(
            reverse('select_checkout_delivery_slot'),
            {
                'slot_key': (
                    f'{delivery_date.isoformat()}|{self.delivery_window.id}'
                ),
            },
        )
        self.assertRedirects(slot_selection, reverse('checkout'))

        Order.objects.create(
            customer=self.customer,
            delivery_slot_date=delivery_date,
            delivery_window=self.delivery_window,
            delivery_slot_name=self.delivery_window.name,
        )
        response = self.post_checkout()

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_failed_online_order_does_not_block_reusing_its_delivery_capacity(self):
        delivery_date = timezone.localdate() + timedelta(days=1)
        self.delivery_window.capacity = 1
        self.delivery_window.save(update_fields=['capacity'])
        Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.ONLINE,
            payment_status=Order.PaymentStatus.FAILED,
            delivery_slot_date=delivery_date,
            delivery_window=self.delivery_window,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.client.get(reverse('checkout'))
        slot_selection = self.client.post(
            reverse('select_checkout_delivery_slot'),
            {
                'slot_key': (
                    f'{delivery_date.isoformat()}|{self.delivery_window.id}'
                ),
            },
        )
        self.assertRedirects(slot_selection, reverse('checkout'))

        response = self.post_checkout(follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 2)
        placed_order = Order.objects.exclude(
            payment_status=Order.PaymentStatus.FAILED,
        ).get()
        self.assertEqual(
            placed_order.payment_method,
            Order.PaymentMethod.CASH_ON_DELIVERY,
        )
        self.assertEqual(placed_order.delivery_slot_date, delivery_date)
        self.assertEqual(placed_order.delivery_window, self.delivery_window)

    def test_full_delivery_slot_cannot_be_selected(self):
        delivery_date = timezone.localdate() + timedelta(days=1)
        self.delivery_window.capacity = 1
        self.delivery_window.save(update_fields=['capacity'])
        Order.objects.create(
            customer=self.customer,
            delivery_slot_date=delivery_date,
            delivery_window=self.delivery_window,
            delivery_slot_name=self.delivery_window.name,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('select_checkout_delivery_slot'),
            {
                'slot_key': (
                    f'{delivery_date.isoformat()}|{self.delivery_window.id}'
                ),
            },
        )

        self.assertRedirects(response, reverse('checkout'))
        self.assertNotIn('checkout_delivery_slot_date', self.client.session)
        self.assertEqual(Order.objects.count(), 1)

    def test_delivery_slot_outside_seven_day_range_cannot_be_selected(self):
        delivery_date = timezone.localdate() + timedelta(days=7)
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('select_checkout_delivery_slot'),
            {
                'slot_key': (
                    f'{delivery_date.isoformat()}|{self.delivery_window.id}'
                ),
            },
        )

        self.assertRedirects(response, reverse('checkout'))
        self.assertNotIn('checkout_delivery_slot_date', self.client.session)
        self.assertEqual(Order.objects.count(), 0)

    def test_order_snapshots_delivery_date_window_name_and_times(self):
        delivery_date = timezone.localdate() + timedelta(days=2)
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.client.post(
            reverse('select_checkout_delivery_slot'),
            {
                'slot_key': (
                    f'{delivery_date.isoformat()}|{self.delivery_window.id}'
                ),
            },
        )

        response = self.post_checkout(follow=True)

        order = Order.objects.get(customer=self.customer)
        self.assertEqual(order.delivery_slot_date, delivery_date)
        self.assertEqual(order.delivery_slot_name, 'Morning delivery')
        self.assertEqual(order.delivery_slot_start_time, time(9, 0))
        self.assertEqual(order.delivery_slot_end_time, time(11, 0))
        self.assertEqual(order.delivery_window, self.delivery_window)
        self.assertContains(response, 'Morning delivery')
        self.assertContains(response, '9:00 AM–11:00 AM')
        self.assertNotIn('checkout_delivery_slot_date', self.client.session)

        self.delivery_window.name = 'Changed window'
        self.delivery_window.start_time = time(12, 0)
        self.delivery_window.end_time = time(14, 0)
        self.delivery_window.save(update_fields=['name', 'start_time', 'end_time'])
        order.refresh_from_db()
        self.assertEqual(order.delivery_slot_name, 'Morning delivery')
        self.assertEqual(order.delivery_slot_start_time, time(9, 0))
        confirmation = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        self.assertContains(confirmation, 'Morning delivery')
        self.assertContains(confirmation, '9:00 AM–11:00 AM')
        self.assertNotContains(confirmation, 'Changed window')

    def test_checkout_requires_delivery_slot_before_order_and_stock_changes(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.post_checkout()

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_customer_can_select_owned_address_for_checkout_session_only(self):
        DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0100',
            is_default=True,
        )
        selected_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Office',
            recipient_name='Shopper',
            address_line_1='14 Office Road',
            city='Springfield',
            state='State',
            postal_code='54321',
            phone='555-0101',
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('select_checkout_address'),
            {'address_id': str(selected_address.id)},
        )

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(
            self.client.session['checkout_delivery_address_customer_id'],
            self.customer.id,
        )
        self.assertEqual(
            self.client.session['checkout_delivery_address_id'],
            selected_address.id,
        )
        checkout_response = self.client.get(reverse('checkout'))
        self.assertEqual(
            checkout_response.context['selected_delivery_address'],
            selected_address,
        )
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_order_stores_selected_delivery_address_snapshot(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Office',
            recipient_name='Sam Shopper',
            address_line_1='14 Office Road',
            address_line_2='Suite 3',
            city='Springfield',
            state='State',
            postal_code='54321',
            phone='555-0101',
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.client.post(
            reverse('select_checkout_address'),
            {'address_id': str(address.id)},
        )
        self.select_delivery_slot()

        response = self.post_checkout(follow=True)

        order = Order.objects.get(customer=self.customer)
        self.assertEqual(order.delivery_address_label, 'Office')
        self.assertEqual(order.delivery_name, 'Sam Shopper')
        self.assertEqual(order.delivery_phone, '555-0101')
        self.assertEqual(order.delivery_address_line_1, '14 Office Road')
        self.assertEqual(order.delivery_address_line_2, 'Suite 3')
        self.assertEqual(order.delivery_city, 'Springfield')
        self.assertEqual(order.delivery_state, 'State')
        self.assertEqual(order.delivery_postal_code, '54321')
        self.assertContains(response, 'Sam Shopper (555-0101)')
        self.assertContains(response, '14 Office Road')
        self.assertContains(response, 'Suite 3')

    def test_order_delivery_snapshot_does_not_change_after_address_or_profile_edits(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            address_line_2='Apartment 4',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0100',
            is_default=True,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()
        response = self.post_checkout(follow=True)
        order = Order.objects.get(customer=self.customer)

        address.recipient_name = 'Updated Name'
        address.address_line_1 = '99 Changed Road'
        address.phone = '555-0999'
        address.save(update_fields=[
            'recipient_name',
            'address_line_1',
            'phone',
        ])
        self.customer.address = 'Changed profile address'
        self.customer.phone = '555-0888'
        self.customer.save(update_fields=['address', 'phone'])
        order.refresh_from_db()
        confirmation = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        history = self.client.get(reverse('order_history'))

        self.assertEqual(order.delivery_name, 'Sam Shopper')
        self.assertEqual(order.delivery_phone, '555-0100')
        self.assertEqual(order.delivery_address_line_1, '12 Garden Road')
        self.assertContains(response, '12 Garden Road')
        self.assertContains(confirmation, 'Sam Shopper (555-0100)')
        self.assertContains(confirmation, '12 Garden Road')
        self.assertContains(history, '12 Garden Road')
        self.assertNotContains(confirmation, '99 Changed Road')
        self.assertNotContains(history, 'Changed profile address')

    def test_order_without_saved_address_snapshots_customer_profile_address(self):
        self.user.first_name = 'Taylor'
        self.user.last_name = 'Customer'
        self.user.save(update_fields=['first_name', 'last_name'])
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()

        self.post_checkout()

        order = Order.objects.get(customer=self.customer)
        self.assertEqual(order.delivery_address_label, 'Profile address')
        self.assertEqual(order.delivery_name, 'Taylor Customer')
        self.assertEqual(order.delivery_phone, '555-0100')
        self.assertEqual(order.delivery_address_line_1, '1 Market Street')
        self.assertEqual(order.delivery_address_line_2, '')
        self.assertEqual(order.delivery_city, '')
        self.assertEqual(order.delivery_state, '')
        self.assertEqual(order.delivery_postal_code, '')

    def test_customer_cannot_select_another_customers_checkout_address(self):
        other_user = User.objects.create_user(
            username='checkout-address-owner',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='Other street',
            phone='555-0101',
        )
        other_address = DeliveryAddress.objects.create(
            customer=other_customer,
            label='Other home',
            recipient_name='Other Customer',
            address_line_1='99 Private Road',
            city='Springfield',
            state='State',
            postal_code='99999',
            phone='555-0101',
            is_default=True,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('select_checkout_address'),
            {'address_id': str(other_address.id)},
        )

        self.assertEqual(response.status_code, 404)
        self.assertNotIn('checkout_delivery_address_id', self.client.session)
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_checkout_address_selection_does_not_change_session(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        response = self.client.post(
            reverse('select_checkout_address'),
            {'address_id': 'not-an-id'},
        )

        self.assertRedirects(response, reverse('checkout'))
        self.assertNotIn('checkout_delivery_address_id', self.client.session)
        self.assertEqual(Order.objects.count(), 0)

    def test_customer_can_add_checkout_address_and_select_it_for_this_session(self):
        previous_default = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0100',
            is_default=True,
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('add_checkout_address'),
            {
                'label': 'Office',
                'recipient_name': 'Shopper',
                'address_line_1': '14 Office Road',
                'address_line_2': 'Suite 3',
                'city': 'Springfield',
                'state': 'State',
                'postal_code': '54321',
                'phone': '555-0101',
                'customer': '',
                'is_default': 'on',
            },
        )

        self.assertRedirects(response, reverse('checkout'))
        added_address = self.customer.delivery_addresses.get(label='Office')
        self.assertEqual(added_address.customer, self.customer)
        self.assertFalse(added_address.is_default)
        self.assertEqual(
            self.client.session['checkout_delivery_address_customer_id'],
            self.customer.id,
        )
        self.assertEqual(
            self.client.session['checkout_delivery_address_id'],
            added_address.id,
        )
        previous_default.refresh_from_db()
        self.assertTrue(previous_default.is_default)
        checkout_response = self.client.get(reverse('checkout'))
        self.assertEqual(
            checkout_response.context['selected_delivery_address'],
            added_address,
        )
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_first_checkout_address_is_default_and_selected(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('add_checkout_address'),
            {
                'label': 'Home',
                'recipient_name': 'Shopper',
                'address_line_1': '12 Garden Road',
                'city': 'Springfield',
                'state': 'State',
                'postal_code': '12345',
                'phone': '555-0100',
            },
        )

        self.assertRedirects(response, reverse('checkout'))
        address = self.customer.delivery_addresses.get()
        self.assertTrue(address.is_default)
        self.assertEqual(
            self.client.session['checkout_delivery_address_id'],
            address.id,
        )
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_checkout_address_is_not_saved_or_selected(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)

        response = self.client.post(
            reverse('add_checkout_address'),
            {
                'label': 'Home',
                'recipient_name': 'Shopper',
                'address_line_1': '12 Garden Road',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'market/checkout.html')
        self.assertContains(response, 'This field is required.')
        self.assertFalse(self.customer.delivery_addresses.exists())
        self.assertNotIn('checkout_delivery_address_id', self.client.session)
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_checkout_address_addition_requires_login(self):
        response = self.client.post(
            reverse('add_checkout_address'),
            {
                'label': 'Home',
                'recipient_name': 'Shopper',
                'address_line_1': '12 Garden Road',
                'city': 'Springfield',
                'state': 'State',
                'postal_code': '12345',
                'phone': '555-0100',
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('add_checkout_address')}",
        )
        self.assertFalse(self.customer.delivery_addresses.exists())

    def test_update_cart_removes_item_when_quantity_is_zero(self):
        self.set_cart({str(self.product.id): 2})

        response = self.client.post(
            reverse('update_cart'),
            {'product_id': str(self.product.id), 'quantity': '0'},
        )

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(self.client.session['cart'], {})

    def test_cart_remove_item_action_removes_only_selected_vendor_listing(self):
        first_vendor = Vendor.objects.create(name='Cart Remove Vendor One')
        second_vendor = Vendor.objects.create(name='Cart Remove Vendor Two')
        first_listing = Product.objects.create(
            name='Peppers',
            price='1.25',
            stock=5,
            units='each',
            category=self.category,
            vendor=first_vendor,
        )
        second_listing = Product.objects.create(
            name='Peppers',
            price='1.50',
            stock=5,
            units='each',
            category=self.category,
            vendor=second_vendor,
        )
        self.set_cart({
            str(first_listing.id): 1,
            str(second_listing.id): 2,
        })

        response = self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(first_listing.id),
                'quantity': '1',
                'remove_item': '1',
            },
        )

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(
            self.client.session['cart'],
            {str(second_listing.id): 2},
        )

    def test_cart_rejects_quantity_above_current_stock(self):
        self.set_cart({str(self.product.id): 2})

        response = self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(self.product.id),
                'quantity': '6',
            },
            follow=True,
        )

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product.id): 2},
        )
        self.assertContains(response, 'Only 5 kg of Apples are available.')

    def test_cart_shows_total_and_blocks_checkout_when_stock_is_insufficient(self):
        second_vendor = Vendor.objects.create(name='Stock Conflict Vendor')
        second_listing = Product.objects.create(
            name='Apples',
            price='3.00',
            stock=1,
            units='kg',
            category=self.category,
            vendor=second_vendor,
        )
        unavailable_vendor = Vendor.objects.create(name='Out of Stock Vendor')
        unavailable_listing = Product.objects.create(
            name='Pears',
            price='4.00',
            stock=0,
            units='kg',
            category=self.category,
            vendor=unavailable_vendor,
        )
        self.set_cart({
            str(self.product.id): 2,
            str(second_listing.id): 2,
            str(unavailable_listing.id): 1,
        })

        response = self.client.get(reverse('cart'))

        self.assertContains(response, 'Line total: 5.00')
        self.assertContains(response, 'Line total: 6.00')
        self.assertContains(response, 'Line total: 4.00')
        self.assertContains(response, 'Total: 15.00')
        self.assertContains(response, 'Only 1 kg currently available.')
        self.assertContains(response, 'Out of stock.')
        self.assertContains(response, 'Update or remove unavailable items')
        self.assertNotContains(response, reverse('checkout'))
        self.assertEqual(
            self.client.session['cart'],
            {
                str(self.product.id): 2,
                str(second_listing.id): 2,
                str(unavailable_listing.id): 1,
            },
        )

    def test_cart_allows_checkout_when_every_listing_has_enough_stock(self):
        self.set_cart({str(self.product.id): 2})

        response = self.client.get(reverse('cart'))

        self.assertContains(response, reverse('checkout'))
        self.assertContains(response, 'Total: 5.00')

    def test_cart_keeps_unavailable_session_listing_visible_and_blocks_checkout(self):
        unavailable_product_id = self.product.id + 1000
        self.set_cart({str(unavailable_product_id): 1})

        response = self.client.get(reverse('cart'))

        self.assertContains(
            response,
            f'Unavailable product listing #{unavailable_product_id}',
        )
        self.assertContains(response, 'This product listing is no longer available.')
        self.assertNotContains(response, reverse('checkout'))
        self.assertEqual(
            self.client.session['cart'],
            {str(unavailable_product_id): 1},
        )

    def test_cart_flags_invalid_quantity_and_allows_removing_that_item(self):
        self.set_cart({str(self.product.id): 'not-a-number'})

        cart_response = self.client.get(reverse('cart'))

        self.assertContains(cart_response, 'This cart item has an invalid quantity.')
        self.assertContains(
            cart_response,
            'Update or remove unavailable items before continuing to checkout.',
        )
        self.assertNotContains(cart_response, reverse('checkout'))

        remove_response = self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(self.product.id),
                'quantity': '1',
                'remove_item': '1',
            },
        )

        self.assertRedirects(remove_response, reverse('cart'))
        self.assertEqual(self.client.session['cart'], {})

    def test_cart_flags_invalid_product_key_and_allows_removing_it(self):
        self.set_cart({'invalid-product-key': 2})

        cart_response = self.client.get(reverse('cart'))

        self.assertContains(
            cart_response,
            'This cart item has an invalid product listing.',
        )
        self.assertNotContains(cart_response, reverse('checkout'))

        remove_response = self.client.post(
            reverse('update_cart'),
            {
                'product_id': 'invalid-product-key',
                'quantity': '2',
                'remove_item': '1',
            },
        )

        self.assertRedirects(remove_response, reverse('cart'))
        self.assertEqual(self.client.session['cart'], {})

    def test_stale_listing_blocks_handoff_until_item_is_removed(self):
        stale_product_id = self.product.id + 1000
        self.set_cart({str(stale_product_id): 1})

        blocked_cart = self.client.get(reverse('cart'))
        self.assertContains(
            blocked_cart,
            'This product listing is no longer available.',
        )
        self.assertNotContains(blocked_cart, reverse('checkout'))

        self.client.post(
            reverse('update_cart'),
            {
                'product_id': str(stale_product_id),
                'quantity': '1',
                'remove_item': '1',
            },
        )
        self.assertEqual(self.client.session['cart'], {})

    def test_valid_cart_hands_off_to_checkout_without_creating_order_or_deducting_stock(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)

        cart_response = self.client.get(reverse('cart'))

        self.assertContains(cart_response, reverse('checkout'))
        self.assertEqual(cart_response.context['total'], Decimal('5.00'))
        checkout_response = self.client.get(reverse('checkout'))

        self.assertEqual(checkout_response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            2,
        )

    def test_checkout_creates_order_and_decrements_stock(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        self.select_delivery_slot()

        response = self.post_checkout(follow=True)

        order = Order.objects.get(customer=self.customer)
        self.assertEqual(
            response.redirect_chain,
            [(reverse('order_confirmation', args=[order.id]), 302)],
        )
        self.assertTemplateUsed(response, 'market/order_confirmation.html')
        self.assertContains(response, f'Order #{order.id} was placed')
        self.assertEqual(
            order.payment_method,
            Order.PaymentMethod.CASH_ON_DELIVERY,
        )
        platform_fulfillment = order.vendor_orders.get()
        self.assertIsNone(platform_fulfillment.vendor_id)
        self.assertEqual(platform_fulfillment.status, VendorOrder.Status.PENDING)
        order_item = order.orderitem_set.get()
        self.assertEqual(order_item.vendor_order, platform_fulfillment)
        self.assertEqual(order_item.quantity, 2)
        self.assertEqual(order_item.unit_price, Decimal('2.50'))
        self.assertContains(response, 'line total 5.00')
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.user,
                kind=Notification.Kind.ORDER_PLACED,
                order=order,
            ).count(),
            1,
        )
        self.assertTrue(OrderStatusHistory.objects.filter(
            order=order,
            previous_status='',
            new_status=Order.Status.PENDING,
            vendor_order__isnull=True,
        ).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())

    def test_checkout_rejects_missing_payment_method(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()

        response = self.client.post(reverse('checkout'), {})

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_checkout_rejects_unsupported_payment_method(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()

        response = self.client.post(
            reverse('checkout'),
            {'payment_method': 'upi'},
        )

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_existing_order_without_payment_method_remains_compatible(self):
        order = Order.objects.create(customer=self.customer)

        self.assertEqual(order.payment_method, '')
        order.refresh_from_db()
        self.assertEqual(order.payment_method, '')

    def test_payment_method_is_saved_only_on_the_authenticated_customers_order(self):
        other_user = User.objects.create_user(
            username='other-payment-customer',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='2 Market Street',
            phone='555-0101',
        )
        other_order = Order.objects.create(customer=other_customer)
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()

        self.post_checkout()

        own_order = Order.objects.get(customer=self.customer)
        other_order.refresh_from_db()
        self.assertEqual(
            own_order.payment_method,
            Order.PaymentMethod.CASH_ON_DELIVERY,
        )
        self.assertEqual(other_order.payment_method, '')

    def test_repeated_checkout_submission_with_same_token_creates_one_order(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        self.select_delivery_slot()
        checkout_page = self.client.get(reverse('checkout'))
        token = checkout_page.context['checkout_token']
        payload = {
            'checkout_token': token,
            'payment_method': Order.PaymentMethod.CASH_ON_DELIVERY,
        }

        first_response = self.post_checkout(payload)
        first_order = Order.objects.get(customer=self.customer)
        second_response = self.post_checkout(payload)

        self.assertEqual(first_response.status_code, 302)
        self.assertRedirects(
            second_response,
            reverse('order_confirmation', args=[first_order.id]),
        )
        self.assertEqual(Order.objects.filter(customer=self.customer).count(), 1)
        self.assertEqual(
            OrderItem.objects.get(order=first_order).quantity,
            2,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())

    def test_failure_during_order_item_creation_rolls_back_all_placement_writes(self):
        self.product.vendor = Vendor.objects.create(name='Rollback vendor')
        self.product.save(update_fields=['vendor'])
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        self.select_delivery_slot()
        checkout_page = self.client.get(reverse('checkout'))
        payload = {
            'checkout_token': checkout_page.context['checkout_token'],
            'payment_method': Order.PaymentMethod.CASH_ON_DELIVERY,
        }

        with patch(
            'market.views.OrderItem.objects.create',
            side_effect=RuntimeError('simulated order item failure'),
        ):
            with self.assertRaises(RuntimeError):
                self.post_checkout(payload)

        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(VendorOrder.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(
            Order.objects.filter(
                delivery_slot_date=timezone.localdate() + timedelta(days=1),
                delivery_window=self.delivery_window,
            ).count(),
            0,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            2,
        )
        self.assertIn('checkout_order_token', self.client.session)

    def test_order_price_and_total_remain_unchanged_after_product_price_edit(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        self.select_delivery_slot()
        self.post_checkout()
        order = Order.objects.get(customer=self.customer)

        self.product.price = '9.99'
        self.product.save(update_fields=['price'])

        confirmation = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        history = self.client.get(reverse('order_history'))

        self.assertContains(confirmation, '2 × Apples')
        self.assertContains(confirmation, '2.50 each')
        self.assertContains(confirmation, 'line total 5.00')
        self.assertContains(confirmation, 'Total: 5.00')
        self.assertContains(history, '2.50 each')
        self.assertContains(history, 'line total 5.00')
        self.assertNotContains(confirmation, '9.99')
        self.assertNotContains(history, '9.99')

    def test_checkout_rejects_cart_that_exceeds_current_stock(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=6)
        self.select_delivery_slot()

        response = self.post_checkout()

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            6,
        )

    def test_checkout_with_valid_slot_rejects_out_of_stock_cart_before_order_creation(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=6)
        self.select_delivery_slot()

        response = self.post_checkout()

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            6,
        )

    def test_checkout_rejects_selected_address_deleted_before_placement(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0100',
        )
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.client.post(
            reverse('select_checkout_address'),
            {'address_id': str(address.id)},
        )
        self.select_delivery_slot()
        address.delete()

        response = self.post_checkout()

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_checkout_rejects_address_session_owned_by_another_customer(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        session = self.client.session
        session['checkout_delivery_address_customer_id'] = self.customer.id + 100
        session['checkout_delivery_address_id'] = self.customer.id
        session.save()
        self.select_delivery_slot()

        response = self.post_checkout()

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_checkout_rejects_inactive_delivery_window_before_order_creation(self):
        self.client.force_login(self.user)
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()
        self.delivery_window.is_active = False
        self.delivery_window.save(update_fields=['is_active'])

        response = self.post_checkout()

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_authenticated_cart_persists_in_database_after_logout(self):
        self.client.force_login(self.user)

        self.client.post(
            reverse('add_to_cart', args=[self.product.id]),
            {'quantity': '2'},
        )
        self.client.post(reverse('logout'))
        self.client.force_login(self.user)

        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            2,
        )
        self.assertEqual(self.client.get(reverse('cart')).status_code, 200)

    def test_login_merges_guest_cart_into_persistent_cart(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.set_cart({str(self.product.id): 2})

        response = self.client.post(
            reverse('login'),
            {'username': 'shopper', 'password': 'safe-password'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            3,
        )
        self.assertNotIn('cart', self.client.session)

    def test_login_merge_does_not_exceed_current_stock(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=4)
        self.set_cart({str(self.product.id): 3})

        self.client.post(
            reverse('login'),
            {'username': 'shopper', 'password': 'safe-password'},
        )

        self.assertEqual(
            CartItem.objects.get(user=self.user, product=self.product).quantity,
            self.product.stock,
        )

    def test_login_revalidates_saved_cart_stock_when_no_guest_cart_exists(self):
        out_of_stock_product = Product.objects.create(
            name='Unavailable Apples',
            price='2.00',
            stock=4,
            units='kg',
            category=self.category,
        )
        CartItem.objects.create(user=self.user, product=self.product, quantity=5)
        CartItem.objects.create(
            user=self.user,
            product=out_of_stock_product,
            quantity=2,
        )
        self.product.stock = 3
        self.product.save(update_fields=['stock'])
        out_of_stock_product.stock = 0
        out_of_stock_product.save(update_fields=['stock'])

        response = self.client.post(
            reverse('login'),
            {'username': 'shopper', 'password': 'safe-password'},
            follow=True,
        )

        self.assertRedirects(response, reverse('product_list'))
        self.assertEqual(
            list(
                CartItem.objects.filter(user=self.user)
                .values_list('product_id', 'quantity')
            ),
            [(self.product.id, 3)],
        )
        self.assertContains(
            response,
            'Unavailable or invalid items were removed from your cart.',
        )
        self.assertContains(
            response,
            'Cart quantities were adjusted to current available stock.',
        )

    def test_switching_authenticated_accounts_does_not_merge_session_cart(self):
        second_user = User.objects.create_user(
            username='second-shopper',
            password='safe-password',
        )
        second_product = Product.objects.create(
            name='Second Shopper Pears',
            price='4.00',
            stock=5,
            units='kg',
            category=self.category,
        )
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        CartItem.objects.create(user=second_user, product=second_product, quantity=2)
        self.client.force_login(self.user)
        self.set_cart({str(self.product.id): 3})

        response = self.client.post(
            reverse('login'),
            {'username': 'second-shopper', 'password': 'safe-password'},
            follow=True,
        )

        self.assertRedirects(response, reverse('product_list'))
        self.assertEqual(
            list(
                CartItem.objects.filter(user=self.user)
                .values_list('product_id', 'quantity')
            ),
            [(self.product.id, 1)],
        )
        self.assertEqual(
            list(
                CartItem.objects.filter(user=second_user)
                .values_list('product_id', 'quantity')
            ),
            [(second_product.id, 2)],
        )
        self.assertNotIn('cart', self.client.session)
        cart_response = self.client.get(reverse('cart'))
        self.assertContains(cart_response, second_product.name)
        self.assertNotContains(cart_response, self.product.name)

    def test_login_cleans_invalid_and_deleted_guest_cart_entries(self):
        stale_product_id = self.product.id + 1000
        self.set_cart({
            'invalid-product-key': 2,
            str(stale_product_id): 1,
            str(self.product.id): 2,
        })

        response = self.client.post(
            reverse('login'),
            {'username': 'shopper', 'password': 'safe-password'},
            follow=True,
        )

        self.assertRedirects(response, reverse('product_list'))
        self.assertEqual(
            list(
                CartItem.objects.filter(user=self.user)
                .values_list('product_id', 'quantity')
            ),
            [(self.product.id, 2)],
        )
        self.assertNotIn('cart', self.client.session)
        self.assertContains(
            response,
            'Unavailable or invalid items were removed from your cart.',
        )

    def test_anonymous_checkout_redirects_to_login_with_return_url(self):
        self.set_cart({str(self.product.id): 1})

        response = self.client.get(reverse('checkout'))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('checkout')}",
        )

    def test_order_history_lists_only_the_signed_in_users_orders(self):
        self.client.force_login(self.user)
        own_order = Order.objects.create(customer=self.customer)

        other_user = User.objects.create_user(username='another-shopper', password='safe-password')
        other_customer = Customer.objects.create(
            user=other_user,
            address='2 Market Street',
            phone='555-0101',
        )
        other_order = Order.objects.create(customer=other_customer)

        response = self.client.get(reverse('order_history'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'market/order_history.html')
        self.assertContains(response, f'Order #{own_order.id}')
        self.assertNotContains(response, f'Order #{other_order.id}')

    def test_order_history_shows_database_totals_status_and_payment_state(self):
        order = Order.objects.create(
            customer=self.customer,
            status='confirmed',
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.PENDING,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('2.50'),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('order_history'))

        self.assertContains(response, f'Order #{order.id}')
        self.assertContains(response, 'Confirmed')
        self.assertContains(response, '2 items')
        self.assertContains(response, '₹5.00')
        self.assertContains(response, 'Cash on Delivery')
        self.assertContains(response, 'Pending')
        self.assertContains(response, 'line total 5.00')

    def test_empty_order_history_offers_shopping_action(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('order_history'))

        self.assertContains(response, 'No orders yet')
        self.assertContains(response, 'Your orders will appear here')
        self.assertContains(response, 'Start shopping')

    def test_order_history_paginates_orders_and_prefetches_one_page(self):
        orders = [
            Order.objects.create(customer=self.customer)
            for _ in range(21)
        ]
        self.client.force_login(self.user)

        first_page = self.client.get(reverse('order_history'))
        second_page = self.client.get(
            reverse('order_history'),
            {'page': '2'},
        )

        self.assertEqual(first_page.context['page_obj'].paginator.num_pages, 2)
        self.assertEqual(len(first_page.context['orders']), 20)
        self.assertContains(first_page, 'Page 1 of 2')
        self.assertContains(first_page, 'Next')
        self.assertEqual(len(second_page.context['orders']), 1)
        self.assertContains(second_page, 'Page 2 of 2')
        self.assertContains(
            second_page,
            f'Order #{orders[0].id}',
        )
        self.assertNotContains(
            first_page,
            f'href="{reverse("order_confirmation", args=[orders[0].id])}"',
        )

    def test_order_details_use_saved_prices_and_show_current_stock_note(self):
        order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('2.50'),
        )
        self.product.price = Decimal('9.99')
        self.product.stock = 0
        self.product.save(update_fields=['price', 'stock'])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )

        self.assertContains(response, 'Order details')
        self.assertContains(response, 'Cash on Delivery')
        self.assertContains(response, 'Pending')
        self.assertContains(response, '2.50 each')
        self.assertContains(response, 'line total 5.00')
        self.assertContains(response, 'Total: 5.00')
        self.assertContains(response, 'Currently out of stock')
        self.assertNotContains(response, '9.99')
        self.assertNotContains(response, 'Retry Payment')

    def test_deleted_product_keeps_historical_order_line_readable(self):
        order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
        )
        item = OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('2.50'),
        )
        self.product.delete()
        self.client.force_login(self.user)

        detail_response = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        history_response = self.client.get(reverse('order_history'))

        item.refresh_from_db()
        self.assertIsNone(item.product)
        self.assertEqual(item.product_name_snapshot, 'Apples')
        self.assertEqual(item.product_units_snapshot, 'kg')
        for response in (detail_response, history_response):
            self.assertContains(response, 'Apples')
            self.assertContains(response, 'kg')
            self.assertContains(response, '2.50')
        self.assertContains(detail_response, 'This product listing is no longer available.')

    def test_failed_online_order_exposes_existing_payment_retry_route(self):
        order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.ONLINE,
            payment_status=Order.PaymentStatus.FAILED,
        )
        self.client.force_login(self.user)

        detail_response = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        history_response = self.client.get(reverse('order_history'))

        retry_url = reverse('online_payment', args=[order.id])
        self.assertContains(detail_response, 'Retry Payment')
        self.assertContains(detail_response, retry_url)
        self.assertContains(history_response, 'Retry Payment')
        self.assertContains(history_response, retry_url)

    def test_customer_cannot_access_another_customers_order_detail(self):
        other_user = User.objects.create_user(
            username='account-isolation-shopper',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='2 Market Street',
            phone='555-0101',
        )
        own_order = Order.objects.create(customer=self.customer)
        other_order = Order.objects.create(customer=other_customer)
        self.client.force_login(self.user)

        self.assertEqual(
            self.client.get(
                reverse('order_confirmation', args=[own_order.id]),
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse('order_confirmation', args=[other_order.id]),
            ).status_code,
            404,
        )

        self.client.force_login(other_user)
        self.assertEqual(
            self.client.get(
                reverse('order_confirmation', args=[other_order.id]),
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse('order_confirmation', args=[own_order.id]),
            ).status_code,
            404,
        )

    def test_anonymous_order_history_redirects_to_login(self):
        response = self.client.get(reverse('order_history'))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('order_history')}",
        )

    def test_customer_profile_displays_saved_customer_details(self):
        self.user.first_name = 'Sam'
        self.user.last_name = 'Shopper'
        self.user.email = 'sam@example.com'
        self.user.save()
        self.client.force_login(self.user)

        response = self.client.get(reverse('customer_profile'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'market/customer_profile.html')
        self.assertContains(response, 'value="Sam"')
        self.assertContains(response, 'value="Shopper"')
        self.assertContains(response, 'value="sam@example.com"')
        self.assertContains(response, 'value="555-0100"')

    def test_customer_can_update_profile_without_changing_delivery_address(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('customer_profile'),
            {
                'first_name': 'Sam',
                'last_name': 'Shopper',
                'email': 'sam@example.com',
                'phone': '555-0199',
            },
        )

        self.assertRedirects(response, reverse('customer_profile'))
        self.user.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Sam')
        self.assertEqual(self.user.last_name, 'Shopper')
        self.assertEqual(self.user.email, 'sam@example.com')
        self.assertEqual(self.customer.phone, '555-0199')
        self.assertEqual(self.customer.address, '1 Market Street')

    def test_customer_profile_rejects_invalid_email_without_partial_update(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('customer_profile'),
            {
                'first_name': 'Changed',
                'last_name': 'Name',
                'email': 'not-an-email',
                'phone': '555-0199',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.user.first_name, '')
        self.assertEqual(self.user.last_name, '')
        self.assertEqual(self.user.email, '')
        self.assertEqual(self.customer.phone, '555-0100')

    def test_customer_profile_requires_login_and_customer_account(self):
        anonymous_response = self.client.get(reverse('customer_profile'))
        self.assertRedirects(
            anonymous_response,
            f"{reverse('login')}?next={reverse('customer_profile')}",
        )

        vendor_user = User.objects.create_user(
            username='profile-vendor',
            password='safe-password',
        )
        self.client.force_login(vendor_user)
        self.assertEqual(
            self.client.get(reverse('customer_profile')).status_code,
            404,
        )

    def test_customer_can_add_first_delivery_address_as_default(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('add_delivery_address'),
            {
                'label': 'Home',
                'recipient_name': 'Sam Shopper',
                'address_line_1': '12 Garden Road',
                'address_line_2': 'Apartment 4',
                'city': 'Springfield',
                'state': 'State',
                'postal_code': '12345',
                'phone': '555-0199',
            },
        )

        self.assertRedirects(response, reverse('customer_profile'))
        address = self.customer.delivery_addresses.get()
        self.assertEqual(address.label, 'Home')
        self.assertEqual(address.address_line_1, '12 Garden Road')
        self.assertEqual(address.address_line_2, 'Apartment 4')
        self.assertEqual(address.customer, self.customer)
        self.assertTrue(address.is_default)

    def test_customer_profile_lists_addresses_with_edit_links(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
            is_default=True,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('customer_profile'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '12 Garden Road')
        self.assertContains(
            response,
            reverse('edit_delivery_address', args=[address.id]),
        )

    def test_customer_can_edit_own_delivery_address(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            address_line_2='Old apartment',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
            is_default=True,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('edit_delivery_address', args=[address.id]),
            {
                'label': 'New home',
                'recipient_name': 'Sam Shopper',
                'address_line_1': '15 Garden Road',
                'address_line_2': 'New apartment',
                'city': 'Shelbyville',
                'state': 'New State',
                'postal_code': '54321',
                'phone': '555-0177',
                'customer': '',
                'is_default': '',
            },
        )

        self.assertRedirects(response, reverse('customer_profile'))
        address.refresh_from_db()
        self.assertEqual(address.label, 'New home')
        self.assertEqual(address.address_line_1, '15 Garden Road')
        self.assertEqual(address.address_line_2, 'New apartment')
        self.assertEqual(address.city, 'Shelbyville')
        self.assertEqual(address.state, 'New State')
        self.assertEqual(address.postal_code, '54321')
        self.assertEqual(address.phone, '555-0177')
        self.assertEqual(address.customer, self.customer)
        self.assertTrue(address.is_default)

    def test_customer_cannot_view_or_edit_another_customers_address(self):
        other_user = User.objects.create_user(
            username='address-private-customer',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='Other street',
            phone='555-0101',
        )
        address = DeliveryAddress.objects.create(
            customer=other_customer,
            label='Other home',
            recipient_name='Other Customer',
            address_line_1='99 Private Road',
            city='Springfield',
            state='State',
            postal_code='99999',
            phone='555-0101',
        )
        self.client.force_login(self.user)

        get_response = self.client.get(
            reverse('edit_delivery_address', args=[address.id]),
        )
        post_response = self.client.post(
            reverse('edit_delivery_address', args=[address.id]),
            {
                'label': 'Hijacked',
                'recipient_name': 'Changed',
                'address_line_1': 'Changed Road',
                'city': 'Changed City',
                'state': 'Changed State',
                'postal_code': '00000',
                'phone': '555-0000',
            },
        )

        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(post_response.status_code, 404)
        address.refresh_from_db()
        self.assertEqual(address.label, 'Other home')
        self.assertEqual(address.address_line_1, '99 Private Road')
        self.assertEqual(address.customer, other_customer)

    def test_anonymous_customer_cannot_edit_delivery_address(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
        )

        response = self.client.get(
            reverse('edit_delivery_address', args=[address.id]),
        )

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('edit_delivery_address', args=[address.id])}",
        )

    def test_customer_must_confirm_before_deleting_own_address(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('delete_delivery_address', args=[address.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            'market/delete_delivery_address.html',
        )
        self.assertContains(response, 'Are you sure you want to delete')
        self.assertTrue(DeliveryAddress.objects.filter(id=address.id).exists())

    def test_customer_can_delete_own_default_address_without_stale_default(self):
        default_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
            is_default=True,
        )
        other_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Office',
            recipient_name='Sam Shopper',
            address_line_1='14 Office Road',
            city='Springfield',
            state='State',
            postal_code='54321',
            phone='555-0100',
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('delete_delivery_address', args=[default_address.id]),
        )

        self.assertRedirects(response, reverse('customer_profile'))
        self.assertFalse(
            DeliveryAddress.objects.filter(id=default_address.id).exists(),
        )
        other_address.refresh_from_db()
        self.assertFalse(other_address.is_default)
        self.assertFalse(
            self.customer.delivery_addresses.filter(is_default=True).exists(),
        )

    def test_customer_can_change_default_address_and_unset_previous_default(self):
        previous_default = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
            is_default=True,
        )
        new_default = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Office',
            recipient_name='Sam Shopper',
            address_line_1='14 Office Road',
            city='Springfield',
            state='State',
            postal_code='54321',
            phone='555-0100',
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('set_default_delivery_address', args=[new_default.id]),
        )

        self.assertRedirects(response, reverse('customer_profile'))
        previous_default.refresh_from_db()
        new_default.refresh_from_db()
        self.assertFalse(previous_default.is_default)
        self.assertTrue(new_default.is_default)
        self.assertEqual(
            self.customer.delivery_addresses.filter(is_default=True).count(),
            1,
        )

    def test_customer_profile_offers_set_default_only_for_non_default_addresses(self):
        default_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
            is_default=True,
        )
        other_address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Office',
            recipient_name='Sam Shopper',
            address_line_1='14 Office Road',
            city='Springfield',
            state='State',
            postal_code='54321',
            phone='555-0100',
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('customer_profile'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse('set_default_delivery_address', args=[other_address.id]),
        )
        self.assertNotContains(
            response,
            reverse('set_default_delivery_address', args=[default_address.id]),
        )

    def test_customer_cannot_set_another_customers_address_as_default(self):
        other_user = User.objects.create_user(
            username='default-private-customer',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='Other street',
            phone='555-0101',
        )
        other_address = DeliveryAddress.objects.create(
            customer=other_customer,
            label='Other home',
            recipient_name='Other Customer',
            address_line_1='99 Private Road',
            city='Springfield',
            state='State',
            postal_code='99999',
            phone='555-0101',
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('set_default_delivery_address', args=[other_address.id]),
        )

        self.assertEqual(response.status_code, 404)
        other_address.refresh_from_db()
        self.assertFalse(other_address.is_default)

    def test_setting_default_address_requires_post(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('set_default_delivery_address', args=[address.id]),
        )

        self.assertEqual(response.status_code, 405)
        address.refresh_from_db()
        self.assertFalse(address.is_default)

    def test_customer_cannot_delete_another_customers_address(self):
        other_user = User.objects.create_user(
            username='delete-private-customer',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='Other street',
            phone='555-0101',
        )
        address = DeliveryAddress.objects.create(
            customer=other_customer,
            label='Other home',
            recipient_name='Other Customer',
            address_line_1='99 Private Road',
            city='Springfield',
            state='State',
            postal_code='99999',
            phone='555-0101',
            is_default=True,
        )
        self.client.force_login(self.user)

        get_response = self.client.get(
            reverse('delete_delivery_address', args=[address.id]),
        )
        post_response = self.client.post(
            reverse('delete_delivery_address', args=[address.id]),
        )

        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(post_response.status_code, 404)
        address.refresh_from_db()
        self.assertEqual(address.customer, other_customer)
        self.assertTrue(address.is_default)

    def test_anonymous_customer_cannot_delete_delivery_address(self):
        address = DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='12 Garden Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0199',
        )

        response = self.client.post(
            reverse('delete_delivery_address', args=[address.id]),
        )

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('delete_delivery_address', args=[address.id])}",
        )
        self.assertTrue(DeliveryAddress.objects.filter(id=address.id).exists())

    def test_additional_delivery_addresses_are_not_default_automatically(self):
        DeliveryAddress.objects.create(
            customer=self.customer,
            label='Home',
            recipient_name='Sam Shopper',
            address_line_1='1 Home Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0100',
            is_default=True,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('add_delivery_address'),
            {
                'label': 'Office',
                'recipient_name': 'Sam Shopper',
                'address_line_1': '2 Office Road',
                'city': 'Springfield',
                'state': 'State',
                'postal_code': '54321',
                'phone': '555-0199',
            },
        )

        self.assertRedirects(response, reverse('customer_profile'))
        addresses = list(self.customer.delivery_addresses.order_by('label'))
        self.assertEqual(len(addresses), 2)
        self.assertTrue(addresses[0].is_default)
        self.assertFalse(addresses[1].is_default)

    def test_add_delivery_address_requires_all_mandatory_fields(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('add_delivery_address'),
            {
                'label': 'Home',
                'recipient_name': 'Sam Shopper',
                'address_line_1': '12 Garden Road',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.customer.delivery_addresses.exists())
        self.assertContains(response, 'This field is required.')

    def test_add_delivery_address_requires_customer_login_and_uses_own_customer(self):
        anonymous_response = self.client.get(reverse('add_delivery_address'))
        self.assertRedirects(
            anonymous_response,
            f"{reverse('login')}?next={reverse('add_delivery_address')}",
        )

        other_user = User.objects.create_user(
            username='other-address-customer',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='Other Street',
            phone='555-0101',
        )
        self.client.force_login(other_user)

        response = self.client.post(
            reverse('add_delivery_address'),
            {
                'label': 'Home',
                'recipient_name': 'Other Customer',
                'address_line_1': '2 Other Street',
                'city': 'Springfield',
                'state': 'State',
                'postal_code': '22222',
                'phone': '555-0101',
                'customer': self.customer.id,
                'is_default': 'on',
            },
        )

        self.assertRedirects(response, reverse('customer_profile'))
        self.assertFalse(self.customer.delivery_addresses.exists())
        self.assertEqual(other_customer.delivery_addresses.count(), 1)
        created_address = other_customer.delivery_addresses.get()
        self.assertTrue(created_address.is_default)


class OnlinePaymentTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Payment produce')
        self.vendor = Vendor.objects.create(name='Payment Vendor')
        self.product = Product.objects.create(
            name='Tomatoes',
            price='2.50',
            stock=5,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )
        self.user = User.objects.create_user(
            username='payment-shopper',
            password='safe-password',
        )
        self.customer = Customer.objects.create(
            user=self.user,
            address='1 Payment Street',
            phone='555-0110',
        )
        self.delivery_window = DeliveryWindow.objects.create(
            name='Payment delivery',
            start_time=time(9, 0),
            end_time=time(11, 0),
            capacity=10,
        )
        self.client.force_login(self.user)

    def select_delivery_slot(self):
        session = self.client.session
        session['checkout_delivery_slot_customer_id'] = self.customer.id
        session['checkout_delivery_slot_date'] = (
            timezone.localdate() + timedelta(days=1)
        ).isoformat()
        session['checkout_delivery_window_id'] = self.delivery_window.id
        session['checkout_order_token_customer_id'] = self.customer.id
        session['checkout_order_token'] = str(uuid.uuid4())
        session.save()

    def create_pending_order(self):
        self.product.stock = 3
        self.product.save(update_fields=['stock'])
        order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.ONLINE,
            payment_status=Order.PaymentStatus.PENDING,
            razorpay_order_id='order_test_123',
            payment_expires_at=timezone.now() + timedelta(minutes=15),
            stock_reserved=True,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('2.50'),
        )
        return order

    def signature_for(self, order_id='order_test_123', payment_id='pay_test123'):
        return hmac.new(
            b'test_secret',
            f'{order_id}|{payment_id}'.encode(),
            hashlib.sha256,
        ).hexdigest()

    def verify_payment(self, order, **overrides):
        payload = {
            'razorpay_order_id': order.razorpay_order_id,
            'razorpay_payment_id': 'pay_test123',
            'razorpay_signature': self.signature_for(order.razorpay_order_id),
        }
        payload.update(overrides)
        return self.client.post(
            reverse('verify_online_payment', args=[order.id]),
            payload,
        )

    def post_webhook(self, event, payment=None, signature=None, raw_body=None):
        if raw_body is None:
            payment = payment or {
                'id': 'pay_test123',
                'order_id': 'order_test_123',
                'amount': 500,
                'currency': 'INR',
                'status': {
                    'payment.captured': 'captured',
                    'payment.failed': 'failed',
                }.get(event, 'captured'),
            }
            raw_body = json.dumps({
                'event': event,
                'payload': {'payment': {'entity': payment}},
            }).encode('utf-8')
        if signature is None:
            signature = hmac.new(
                b'test_webhook_secret',
                raw_body,
                hashlib.sha256,
            ).hexdigest()
        with self.settings(
            RAZORPAY_KEY_ID='rzp_test_example',
            RAZORPAY_KEY_SECRET='test_secret',
            RAZORPAY_WEBHOOK_SECRET='test_webhook_secret',
        ):
            return self.client.generic(
                'POST',
                reverse('razorpay_webhook'),
                raw_body,
                content_type='application/json',
                HTTP_X_RAZORPAY_SIGNATURE=signature,
            )

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_online_checkout_creates_test_order_for_server_calculated_amount(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        self.select_delivery_slot()
        api_response = {
            'id': 'order_test_123',
            'amount': 500,
            'currency': 'INR',
        }

        with patch(
            'market.payments.razorpay_api_request',
            return_value=api_response,
        ) as api_request:
            response = self.client.post(
                reverse('checkout'),
                {'payment_method': Order.PaymentMethod.ONLINE},
            )

        order = Order.objects.get(customer=self.customer)
        self.assertRedirects(
            response,
            reverse('online_payment', args=[order.id]),
        )
        self.assertEqual(order.payment_method, Order.PaymentMethod.ONLINE)
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(order.razorpay_order_id, 'order_test_123')
        self.assertTrue(order.stock_reserved)
        online_placement_notice = Notification.objects.get(
            recipient=self.user,
            order=order,
            kind=Notification.Kind.ORDER_PLACED,
        )
        self.assertIn('Complete online payment', online_placement_notice.message)
        api_request.assert_called_once_with(
            'POST',
            'orders',
            {
                'amount': 500,
                'currency': 'INR',
                'receipt': f'market-order-{order.id}',
                'notes': {'marketplace_order_id': str(order.id)},
            },
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())
        self.assertEqual(order.vendor_orders.count(), 0)

    def test_inr_to_paise_conversion_is_exact(self):
        self.assertEqual(amount_in_paise(Decimal('2.50')), 250)
        self.assertEqual(amount_in_paise(Decimal('1234.56')), 123456)

    def test_checkout_displays_online_choice_only_when_test_mode_is_configured(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        with self.settings(RAZORPAY_KEY_ID='', RAZORPAY_KEY_SECRET=''):
            response = self.client.get(reverse('checkout'))

        self.assertContains(response, 'Cash on Delivery')
        self.assertContains(response, 'Online Payment')
        self.assertContains(response, 'test mode only')
        self.assertContains(response, 'Online test payments are not configured.')

    def test_online_checkout_fails_closed_without_test_configuration(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()
        with self.settings(RAZORPAY_KEY_ID='', RAZORPAY_KEY_SECRET=''):
            response = self.client.post(
                reverse('checkout'),
                {'payment_method': Order.PaymentMethod.ONLINE},
                follow=True,
            )

        self.assertEqual(Order.objects.count(), 0)
        self.assertContains(response, 'Online payments require Razorpay test-mode credentials.')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_failed_order_creation_rolls_back_marketplace_order_stock_and_cart(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=2)
        self.select_delivery_slot()
        with patch(
            'market.payments.razorpay_api_request',
            side_effect=RuntimeError('simulated placement failure'),
        ), patch(
            'market.views.OrderItem.objects.create',
            side_effect=RuntimeError('simulated order item failure'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('checkout'),
                    {'payment_method': Order.PaymentMethod.ONLINE},
                )

        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(CartItem.objects.filter(user=self.user).exists())

    def test_live_key_is_rejected(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=1)
        self.select_delivery_slot()
        with self.settings(
            RAZORPAY_KEY_ID='rzp_live_not-allowed',
            RAZORPAY_KEY_SECRET='not-used',
        ):
            response = self.client.post(
                reverse('checkout'),
                {'payment_method': Order.PaymentMethod.ONLINE},
                follow=True,
            )

        self.assertEqual(Order.objects.count(), 0)
        self.assertContains(response, 'Razorpay test-mode credentials')

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_successful_verification_updates_only_owned_order_and_keeps_stock_deduction(self):
        self.vendor.user = User.objects.create_user(
            username='captured-payment-vendor',
            password='Safe-vendor-password-927!',
        )
        self.vendor.save(update_fields=['user'])
        order = self.create_pending_order()
        with patch(
            'market.payments.razorpay_api_request',
            return_value={
                'id': 'pay_test123',
                'order_id': order.razorpay_order_id,
                'amount': 500,
                'currency': 'INR',
                'status': 'captured',
            },
        ):
            response = self.verify_payment(order)

        self.assertRedirects(response, reverse('order_confirmation', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.razorpay_payment_id, 'pay_test123')
        self.assertEqual(order.orderitem_set.get().vendor_order.vendor, self.vendor)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.vendor.user,
                kind=Notification.Kind.VENDOR_ORDER,
                order=order,
            ).count(),
            1,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_duplicate_verification_is_idempotent_and_never_deducts_stock_twice(self):
        order = self.create_pending_order()
        captured = {
            'id': 'pay_test123',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'INR',
            'status': 'captured',
        }
        with patch('market.payments.razorpay_api_request', return_value=captured):
            self.verify_payment(order)
            self.verify_payment(order)

        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.vendor_orders.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_invalid_provider_order_id_payment_id_and_signature_are_rejected(self):
        order = self.create_pending_order()
        with patch('market.payments.razorpay_api_request') as api_request:
            mismatched_order = self.verify_payment(
                order,
                razorpay_order_id='order_tampered',
            )
            invalid_payment = self.verify_payment(
                order,
                razorpay_payment_id='not-a-payment-id',
            )
            invalid_signature = self.verify_payment(
                order,
                razorpay_signature='invalid',
            )

        self.assertRedirects(
            mismatched_order,
            reverse('online_payment', args=[order.id]),
        )
        self.assertRedirects(
            invalid_payment,
            reverse('online_payment', args=[order.id]),
        )
        self.assertRedirects(
            invalid_signature,
            reverse('online_payment', args=[order.id]),
        )
        api_request.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_payment_details_from_razorpay_must_match_order_amount_and_currency(self):
        order = self.create_pending_order()
        with patch(
            'market.payments.razorpay_api_request',
            return_value={
                'id': 'pay_test123',
                'order_id': order.razorpay_order_id,
                'amount': 499,
                'currency': 'INR',
                'status': 'captured',
            },
        ):
            response = self.verify_payment(order)

        self.assertRedirects(response, reverse('online_payment', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_failed_payment_releases_reserved_stock_once(self):
        order = self.create_pending_order()
        failed = {
            'id': 'pay_test123',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'INR',
            'status': 'failed',
        }
        with patch('market.payments.razorpay_api_request', return_value=failed):
            first = self.verify_payment(order)
            second = self.verify_payment(order)

        self.assertRedirects(first, reverse('online_payment', args=[order.id]))
        self.assertRedirects(second, reverse('online_payment', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertFalse(order.stock_reserved)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.user,
                kind=Notification.Kind.PAYMENT_FAILED,
                order=order,
            ).count(),
            1,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_retry_reuses_same_gateway_and_marketplace_order_without_double_reservation(self):
        order = self.create_pending_order()
        failed = {
            'id': 'pay_test123',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'INR',
            'status': 'failed',
        }
        with patch('market.payments.razorpay_api_request', return_value=failed):
            self.verify_payment(order)
        with patch('market.payments.razorpay_api_request') as api_request:
            response = self.client.post(
                reverse('start_online_payment', args=[order.id]),
            )

        self.assertRedirects(response, reverse('online_payment', args=[order.id]))
        api_request.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.razorpay_order_id, 'order_test_123')
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertTrue(order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_failed_payment_retry_is_blocked_without_stock_and_keeps_failure_state(self):
        order = self.create_pending_order()
        with patch(
            'market.payments.razorpay_api_request',
            return_value={
                'id': 'pay_test123',
                'order_id': order.razorpay_order_id,
                'amount': 500,
                'currency': 'INR',
                'status': 'failed',
            },
        ):
            self.verify_payment(order)
        self.product.stock = 0
        self.product.save(update_fields=['stock'])

        with patch('market.payments.razorpay_api_request') as api_request:
            response = self.client.post(
                reverse('start_online_payment', args=[order.id]),
                follow=True,
            )

        self.assertContains(response, 'Some items are no longer available')
        api_request.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertFalse(order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_failed_order_can_be_cancelled_without_releasing_stock_twice(self):
        order = self.create_pending_order()
        with patch(
            'market.payments.razorpay_api_request',
            return_value={
                'id': 'pay_test123',
                'order_id': order.razorpay_order_id,
                'amount': 500,
                'currency': 'INR',
                'status': 'failed',
            },
        ):
            self.verify_payment(order)
        with patch(
            'market.payments.razorpay_api_request',
            return_value={'items': []},
        ):
            first = self.client.post(
                reverse('cancel_online_payment', args=[order.id]),
            )
            second = self.client.post(
                reverse('cancel_online_payment', args=[order.id]),
            )

        self.assertRedirects(
            first,
            reverse('order_confirmation', args=[order.id]),
        )
        self.assertRedirects(
            second,
            reverse('order_confirmation', args=[order.id]),
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.CANCELLED)
        self.assertFalse(order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_payment_page_keeps_pending_and_failed_attempts_retryable(self):
        pending_order = self.create_pending_order()
        pending_response = self.client.get(
            reverse('online_payment', args=[pending_order.id]),
        )
        self.assertContains(pending_response, 'Cancel online payment')
        self.assertContains(
            pending_response,
            'Payment window closed. Your order is still pending; you can retry.',
        )
        self.assertContains(pending_response, 'Pay ₹')

        with patch(
            'market.payments.razorpay_api_request',
            return_value={
                'id': 'pay_test123',
                'order_id': pending_order.razorpay_order_id,
                'amount': 500,
                'currency': 'INR',
                'status': 'failed',
            },
        ):
            self.verify_payment(pending_order)
        failed_response = self.client.get(
            reverse('online_payment', args=[pending_order.id]),
        )
        self.assertContains(failed_response, 'Retry online payment')
        self.assertContains(failed_response, 'Cancel order')
        self.assertNotContains(failed_response, 'payment-dismissed-message')

    def test_cod_order_cannot_be_cancelled_through_online_payment_endpoint(self):
        cod_order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.PENDING,
        )
        response = self.client.post(
            reverse('cancel_online_payment', args=[cod_order.id]),
        )

        self.assertEqual(response.status_code, 404)
        cod_order.refresh_from_db()
        self.assertEqual(cod_order.payment_method, Order.PaymentMethod.CASH_ON_DELIVERY)
        self.assertEqual(cod_order.payment_status, Order.PaymentStatus.PENDING)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_cancelled_payment_releases_stock_and_cannot_become_paid(self):
        order = self.create_pending_order()
        with patch(
            'market.payments.razorpay_api_request',
            return_value={'items': []},
        ):
            response = self.client.post(
                reverse('cancel_online_payment', args=[order.id]),
            )

        self.assertRedirects(
            response,
            reverse('order_confirmation', args=[order.id]),
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.CANCELLED)
        self.assertEqual(order.status, 'cancelled')
        self.assertFalse(order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        with patch('market.payments.razorpay_api_request') as api_request:
            rejected = self.verify_payment(
                order,
                razorpay_signature=self.signature_for(),
            )
        self.assertRedirects(
            rejected,
            reverse('online_payment', args=[order.id]),
        )
        api_request.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.CANCELLED)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_cancel_reconciles_a_captured_payment_as_paid(self):
        order = self.create_pending_order()
        with patch(
            'market.payments.razorpay_api_request',
            return_value={
                'items': [{
                    'id': 'pay_test123',
                    'order_id': order.razorpay_order_id,
                    'amount': 500,
                    'currency': 'INR',
                    'status': 'captured',
                }],
            },
        ):
            response = self.client.post(
                reverse('cancel_online_payment', args=[order.id]),
            )

        self.assertRedirects(
            response,
            reverse('order_confirmation', args=[order.id]),
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.razorpay_payment_id, 'pay_test123')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_expired_pending_payment_releases_stock_and_delivery_capacity(self):
        order = self.create_pending_order()
        order.payment_expires_at = timezone.now() - timedelta(seconds=1)
        order.save(update_fields=['payment_expires_at'])
        with patch(
            'market.payments.razorpay_api_request',
            return_value={'items': []},
        ):
            expired = expire_pending_razorpay_orders()

        self.assertEqual(expired, 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.EXPIRED)
        self.assertFalse(order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_customer_cannot_access_another_customers_payment(self):
        other_user = User.objects.create_user(
            username='other-payment-user',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='2 Payment Street',
            phone='555-0111',
        )
        other_order = Order.objects.create(
            customer=other_customer,
            payment_method=Order.PaymentMethod.ONLINE,
        )
        response = self.client.get(
            reverse('online_payment', args=[other_order.id]),
        )
        verify = self.client.post(
            reverse('verify_online_payment', args=[other_order.id]),
            {
                'razorpay_order_id': 'order_test_123',
                'razorpay_payment_id': 'pay_test123',
                'razorpay_signature': 'invalid',
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(verify.status_code, 404)
        other_order.refresh_from_db()
        self.assertEqual(other_order.payment_status, Order.PaymentStatus.PENDING)

    @override_settings(
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    def test_checkout_stock_validation_still_blocks_online_order_creation(self):
        CartItem.objects.create(user=self.user, product=self.product, quantity=6)
        self.select_delivery_slot()
        with patch('market.payments.razorpay_api_request') as api_request:
            response = self.client.post(
                reverse('checkout'),
                {'payment_method': Order.PaymentMethod.ONLINE},
            )

        self.assertRedirects(response, reverse('cart'))
        self.assertEqual(Order.objects.count(), 0)
        api_request.assert_not_called()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_valid_captured_webhook_reconciles_payment_and_stock_once(self):
        order = self.create_pending_order()
        self.client.logout()
        response = self.post_webhook('payment.captured')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'processed'})
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.razorpay_payment_id, 'pay_test123')
        self.assertTrue(order.stock_reserved)
        self.assertEqual(order.vendor_orders.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    def test_duplicate_captured_webhooks_do_not_repeat_business_actions(self):
        order = self.create_pending_order()
        first = self.post_webhook('payment.captured')
        second = self.post_webhook('payment.captured')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.vendor_orders.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    def test_valid_failed_webhook_releases_reserved_stock_only_once(self):
        order = self.create_pending_order()
        first = self.post_webhook('payment.failed')
        second = self.post_webhook('payment.failed')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertFalse(order.stock_reserved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_invalid_or_missing_webhook_signature_is_rejected(self):
        order = self.create_pending_order()
        invalid = self.post_webhook(
            'payment.captured',
            signature='invalid',
        )
        missing = self.post_webhook(
            'payment.captured',
            signature='',
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(missing.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertTrue(order.stock_reserved)

    def test_malformed_signed_webhook_payload_is_rejected(self):
        response = self.post_webhook(
            'payment.captured',
            raw_body=b'{"event":',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'status': 'invalid_payload'})

    def test_unsupported_event_is_signature_checked_then_acknowledged(self):
        order = self.create_pending_order()
        invalid_signature = self.post_webhook(
            'order.paid',
            signature='invalid',
        )
        ignored = self.post_webhook('order.paid')

        self.assertEqual(invalid_signature.status_code, 400)
        self.assertEqual(ignored.status_code, 200)
        self.assertEqual(ignored.json(), {'status': 'ignored'})
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)

    def test_unknown_or_mismatched_razorpay_order_is_ignored(self):
        order = self.create_pending_order()
        unknown = self.post_webhook('payment.captured', {
            'id': 'pay_test123',
            'order_id': 'order_unknown',
            'amount': 500,
            'currency': 'INR',
            'status': 'captured',
        })
        invalid_payment_id = self.post_webhook('payment.captured', {
            'id': 'not-a-payment-id',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'INR',
            'status': 'captured',
        })

        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(invalid_payment_id.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertTrue(order.stock_reserved)

    def test_payment_amount_currency_and_event_state_must_match(self):
        order = self.create_pending_order()
        amount_mismatch = self.post_webhook('payment.captured', {
            'id': 'pay_test123',
            'order_id': order.razorpay_order_id,
            'amount': 499,
            'currency': 'INR',
            'status': 'captured',
        })
        currency_mismatch = self.post_webhook('payment.captured', {
            'id': 'pay_test123',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'USD',
            'status': 'captured',
        })
        event_state_mismatch = self.post_webhook('payment.captured', {
            'id': 'pay_test123',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'INR',
            'status': 'failed',
        })

        self.assertEqual(amount_mismatch.status_code, 200)
        self.assertEqual(currency_mismatch.status_code, 200)
        self.assertEqual(event_state_mismatch.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertTrue(order.stock_reserved)

    def test_finalized_payment_cannot_be_overwritten_by_contradictory_event(self):
        order = self.create_pending_order()
        self.post_webhook('payment.captured')
        contradictory = self.post_webhook('payment.failed')
        conflicting_capture = self.post_webhook('payment.captured', {
            'id': 'pay_other123',
            'order_id': order.razorpay_order_id,
            'amount': 500,
            'currency': 'INR',
            'status': 'captured',
        })
        order.refresh_from_db()

        self.assertEqual(contradictory.status_code, 200)
        self.assertEqual(conflicting_capture.status_code, 200)
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.razorpay_payment_id, 'pay_test123')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    def test_webhook_cannot_modify_cod_or_another_customers_order(self):
        order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            razorpay_order_id='order_test_123',
        )
        response = self.post_webhook('payment.captured')

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(order.payment_method, Order.PaymentMethod.CASH_ON_DELIVERY)

    def test_webhook_updates_only_the_marketplace_order_with_matching_gateway_id(self):
        matching_order = self.create_pending_order()
        other_user = User.objects.create_user(
            username='unrelated-payment-user',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='3 Payment Street',
            phone='555-0112',
        )
        unrelated_order = Order.objects.create(
            customer=other_customer,
            payment_method=Order.PaymentMethod.ONLINE,
            payment_status=Order.PaymentStatus.PENDING,
            razorpay_order_id='order_test_other',
        )
        response = self.post_webhook('payment.captured')

        self.assertEqual(response.status_code, 200)
        matching_order.refresh_from_db()
        unrelated_order.refresh_from_db()
        self.assertEqual(matching_order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(unrelated_order.payment_status, Order.PaymentStatus.PENDING)

    def test_missing_webhook_configuration_returns_retryable_response(self):
        order = self.create_pending_order()
        raw_body = json.dumps({'event': 'payment.captured'}).encode('utf-8')
        signature = hmac.new(
            b'test_webhook_secret',
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        with self.settings(
            RAZORPAY_KEY_ID='rzp_test_example',
            RAZORPAY_WEBHOOK_SECRET='',
        ):
            response = self.client.generic(
                'POST',
                reverse('razorpay_webhook'),
                raw_body,
                content_type='application/json',
                HTTP_X_RAZORPAY_SIGNATURE=signature,
            )

        self.assertEqual(response.status_code, 503)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)

    def test_temporary_processing_failure_returns_retryable_response(self):
        order = self.create_pending_order()
        with patch(
            'market.payments._mark_paid',
            side_effect=RuntimeError('temporary database failure'),
        ):
            failed_response = self.post_webhook('payment.captured')

        self.assertEqual(failed_response.status_code, 500)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertTrue(order.stock_reserved)
        retry_response = self.post_webhook('payment.captured')

        self.assertEqual(retry_response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)


class VendorOrderManagementTests(TestCase):
    def setUp(self):
        self.vendor_user = User.objects.create_user(
            username='orders-vendor',
            password='Safe-vendor-password-927!',
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            name='Orders Vendor',
            shop_name='Orders Shop',
        )
        self.other_vendor = Vendor.objects.create(name='Other Orders Vendor')
        self.category = Category.objects.create(name='Fresh Vegetables')
        self.customer_user = User.objects.create_user(
            username='order-customer',
            password='Safe-customer-password-927!',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            address='10 Customer Lane',
            phone='555-0300',
        )
        self.delivery_window = DeliveryWindow.objects.create(
            name='Vendor morning delivery',
            start_time=time(9, 0),
            end_time=time(11, 0),
            capacity=10,
        )
        self.own_product = Product.objects.create(
            name='Vendor Spinach',
            price='3.00',
            stock=5,
            units='bunch',
            category=self.category,
            vendor=self.vendor,
        )
        self.other_product = Product.objects.create(
            name='Other Vendor Carrots',
            price='2.00',
            stock=5,
            units='kg',
            category=self.category,
            vendor=self.other_vendor,
        )

    def create_vendor_item(self, order, product, quantity):
        vendor_order = VendorOrder.objects.get_or_create(
            order=order,
            vendor=product.vendor,
        )[0]
        return OrderItem.objects.create(
            order=order,
            vendor_order=vendor_order,
            product=product,
            quantity=quantity,
            unit_price=product.price,
        )

    def select_checkout_delivery_slot(self):
        session = self.client.session
        session['checkout_delivery_slot_customer_id'] = self.customer.id
        session['checkout_delivery_slot_date'] = (
            timezone.localdate() + timedelta(days=1)
        ).isoformat()
        session['checkout_delivery_window_id'] = self.delivery_window.id
        session['checkout_order_token_customer_id'] = self.customer.id
        session['checkout_order_token'] = str(uuid.uuid4())
        session.save()

    def test_customer_sees_each_vendor_fulfillment_status_on_order_pages(self):
        order = Order.objects.create(customer=self.customer)
        own_vendor_order = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
            status=VendorOrder.Status.PREPARING,
        )
        other_vendor_order = VendorOrder.objects.create(
            order=order,
            vendor=self.other_vendor,
            status=VendorOrder.Status.OUT_FOR_DELIVERY,
        )
        self.create_vendor_item(order, self.own_product, 2)
        self.create_vendor_item(order, self.other_product, 1)
        self.client.force_login(self.customer_user)

        history_response = self.client.get(reverse('order_history'))
        confirmation_response = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )

        for response in (history_response, confirmation_response):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Fulfillment by vendor')
            self.assertContains(response, self.vendor.shop_name)
            self.assertContains(response, self.other_vendor.name)
            self.assertContains(response, own_vendor_order.get_status_display())
            self.assertContains(response, other_vendor_order.get_status_display())
            self.assertContains(response, self.own_product.name)
            self.assertContains(response, self.other_product.name)

    def test_customer_cannot_track_another_customers_order(self):
        other_user = User.objects.create_user(
            username='private-customer',
            password='Safe-customer-password-927!',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='Private Customer Lane',
            phone='555-0400',
        )
        other_order = Order.objects.create(customer=other_customer)
        VendorOrder.objects.create(order=other_order, vendor=self.vendor)
        self.client.force_login(self.customer_user)

        response = self.client.get(
            reverse('order_confirmation', args=[other_order.id]),
        )

        self.assertEqual(response.status_code, 404)

    def test_vendor_order_list_includes_only_orders_with_its_products(self):
        own_order = Order.objects.create(customer=self.customer)
        self.create_vendor_item(own_order, self.own_product, 2)
        unrelated_order = Order.objects.create(customer=self.customer)
        self.create_vendor_item(unrelated_order, self.other_product, 1)
        self.client.force_login(self.vendor_user)

        response = self.client.get(reverse('vendor_order_list'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'market/vendor_order_list.html')
        self.assertContains(response, f'#{own_order.id}')
        self.assertContains(response, 'Vendor Spinach')
        self.assertContains(response, '₹6.00')
        self.assertContains(response, '3.00 each')
        self.assertNotContains(response, f'#{unrelated_order.id}')
        self.assertNotContains(response, 'Other Vendor Carrots')
        self.assertNotContains(response, '2.00 each')

    def test_mixed_vendor_order_detail_shows_only_this_vendors_lines(self):
        order = Order.objects.create(customer=self.customer)
        self.create_vendor_item(order, self.own_product, 2)
        self.create_vendor_item(order, self.other_product, 3)
        self.client.force_login(self.vendor_user)

        response = self.client.get(reverse('vendor_order_detail', args=[order.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'market/vendor_order_detail.html')
        self.assertContains(response, 'This page shows only the items supplied')
        self.assertContains(response, 'Vendor Spinach')
        self.assertContains(response, '10 Customer Lane')
        self.assertContains(response, '555-0300')
        self.assertNotContains(response, 'Other Vendor Carrots')
        self.assertNotContains(response, '2.00')
        self.assertContains(response, '3.00 each')
        self.assertContains(response, 'Your item subtotal')

    def test_vendor_cannot_view_order_containing_only_other_vendor_products(self):
        order = Order.objects.create(customer=self.customer)
        self.create_vendor_item(order, self.other_product, 1)
        self.client.force_login(self.vendor_user)

        response = self.client.get(reverse('vendor_order_detail', args=[order.id]))

        self.assertEqual(response.status_code, 404)

    def test_anonymous_vendor_order_list_redirects_to_vendor_login(self):
        response = self.client.get(reverse('vendor_order_list'))

        self.assertRedirects(
            response,
            f"{reverse('vendor_login')}?next={reverse('vendor_order_list')}",
        )

    def test_vendor_order_list_shows_empty_state(self):
        self.client.force_login(self.vendor_user)

        response = self.client.get(reverse('vendor_order_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No orders contain your products yet.')

    def test_vendor_acceptance_updates_only_its_fulfillment_status(self):
        order = Order.objects.create(customer=self.customer)
        own_vendor_order = VendorOrder.objects.create(order=order, vendor=self.vendor)
        other_vendor_order = VendorOrder.objects.create(order=order, vendor=self.other_vendor)
        self.create_vendor_item(order, self.own_product, 2)
        self.create_vendor_item(order, self.other_product, 3)
        self.client.force_login(self.vendor_user)

        response = self.client.post(
            reverse('accept_vendor_order', args=[own_vendor_order.id]),
        )

        self.assertRedirects(
            response,
            reverse('vendor_order_detail', args=[order.id]),
        )
        own_vendor_order.refresh_from_db()
        other_vendor_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(own_vendor_order.status, VendorOrder.Status.CONFIRMED)
        self.assertEqual(other_vendor_order.status, VendorOrder.Status.PENDING)
        self.assertEqual(order.status, 'pending')

    def test_vendor_can_progress_its_fulfillment_to_ready_for_delivery(self):
        order = Order.objects.create(customer=self.customer)
        own_vendor_order = VendorOrder.objects.create(order=order, vendor=self.vendor)
        other_vendor_order = VendorOrder.objects.create(order=order, vendor=self.other_vendor)
        self.client.force_login(self.vendor_user)

        expected_statuses = (
            VendorOrder.Status.CONFIRMED,
            VendorOrder.Status.PREPARING,
            VendorOrder.Status.READY_FOR_DELIVERY,
        )
        for expected_status in expected_statuses:
            response = self.client.post(
                reverse('advance_vendor_order', args=[own_vendor_order.id]),
            )
            self.assertRedirects(
                response,
                reverse('vendor_order_detail', args=[order.id]),
            )
            own_vendor_order.refresh_from_db()
            other_vendor_order.refresh_from_db()
            self.assertEqual(own_vendor_order.status, expected_status)
            self.assertEqual(other_vendor_order.status, VendorOrder.Status.PENDING)

    def test_vendor_cannot_advance_delivery_status_or_skip_steps(self):
        order = Order.objects.create(customer=self.customer)
        vendor_order = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
            status=VendorOrder.Status.READY_FOR_DELIVERY,
        )
        self.client.force_login(self.vendor_user)

        response = self.client.post(
            reverse('advance_vendor_order', args=[vendor_order.id]),
        )

        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.id]))
        vendor_order.refresh_from_db()
        self.assertEqual(vendor_order.status, VendorOrder.Status.READY_FOR_DELIVERY)

    def test_staff_admin_actions_enforce_delivery_sequence_and_timestamp_delivery(self):
        order = Order.objects.create(customer=self.customer)
        ready_order = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
            status=VendorOrder.Status.READY_FOR_DELIVERY,
        )
        pending_order = VendorOrder.objects.create(
            order=Order.objects.create(customer=self.customer),
            vendor=self.vendor,
            status=VendorOrder.Status.PENDING,
        )
        request = RequestFactory().post('/admin/market/vendororder/')
        request.user = User.objects.create_superuser(
            username='delivery-admin',
            email='admin@example.com',
            password='safe-password',
        )
        request.session = {}
        request._messages = FallbackStorage(request)
        vendor_order_admin = admin.site._registry[VendorOrder]

        vendor_order_admin.mark_out_for_delivery(
            request,
            VendorOrder.objects.filter(id__in=[ready_order.id, pending_order.id]),
        )
        ready_order.refresh_from_db()
        pending_order.refresh_from_db()
        self.assertEqual(ready_order.status, VendorOrder.Status.OUT_FOR_DELIVERY)
        self.assertEqual(pending_order.status, VendorOrder.Status.PENDING)

        vendor_order_admin.mark_delivered(
            request,
            VendorOrder.objects.filter(id=ready_order.id),
        )
        ready_order.refresh_from_db()
        self.assertEqual(ready_order.status, VendorOrder.Status.DELIVERED)
        self.assertIsNotNone(ready_order.delivered_at)

    def test_vendor_rejection_confirms_and_restocks_only_its_items(self):
        order = Order.objects.create(customer=self.customer)
        own_vendor_order = VendorOrder.objects.create(order=order, vendor=self.vendor)
        other_vendor_order = VendorOrder.objects.create(order=order, vendor=self.other_vendor)
        self.create_vendor_item(order, self.own_product, 2)
        self.create_vendor_item(order, self.other_product, 3)
        self.own_product.stock = 3
        self.own_product.save(update_fields=['stock'])
        self.other_product.stock = 2
        self.other_product.save(update_fields=['stock'])
        self.client.force_login(self.vendor_user)

        confirmation = self.client.get(
            reverse('reject_vendor_order', args=[own_vendor_order.id]),
        )
        self.assertEqual(confirmation.status_code, 200)
        self.assertTemplateUsed(confirmation, 'market/reject_vendor_order.html')
        self.assertContains(confirmation, 'Other vendors’ items')
        self.assertEqual(own_vendor_order.status, VendorOrder.Status.PENDING)

        response = self.client.post(
            reverse('reject_vendor_order', args=[own_vendor_order.id]),
            follow=True,
        )

        own_vendor_order.refresh_from_db()
        other_vendor_order.refresh_from_db()
        self.own_product.refresh_from_db()
        self.other_product.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(own_vendor_order.status, VendorOrder.Status.REJECTED)
        self.assertEqual(other_vendor_order.status, VendorOrder.Status.PENDING)
        self.assertEqual(self.own_product.stock, 5)
        self.assertEqual(self.other_product.stock, 2)
        self.assertEqual(order.status, 'pending')
        self.assertContains(response, 'returned to stock')

        duplicate_response = self.client.post(
            reverse('reject_vendor_order', args=[own_vendor_order.id]),
        )
        self.assertRedirects(
            duplicate_response,
            reverse('vendor_order_detail', args=[order.id]),
        )
        self.own_product.refresh_from_db()
        self.assertEqual(self.own_product.stock, 5)

    def test_vendor_cannot_act_on_another_vendors_fulfillment(self):
        order = Order.objects.create(customer=self.customer)
        other_vendor_order = VendorOrder.objects.create(
            order=order,
            vendor=self.other_vendor,
        )
        self.create_vendor_item(order, self.other_product, 1)
        self.client.force_login(self.vendor_user)

        response = self.client.post(
            reverse('accept_vendor_order', args=[other_vendor_order.id]),
        )

        self.assertEqual(response.status_code, 404)
        other_vendor_order.refresh_from_db()
        self.assertEqual(other_vendor_order.status, VendorOrder.Status.PENDING)

    def test_vendor_cannot_reject_an_already_confirmed_fulfillment(self):
        order = Order.objects.create(customer=self.customer)
        vendor_order = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
            status=VendorOrder.Status.CONFIRMED,
        )
        self.create_vendor_item(order, self.own_product, 1)
        self.client.force_login(self.vendor_user)

        response = self.client.post(
            reverse('reject_vendor_order', args=[vendor_order.id]),
        )

        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.id]))
        vendor_order.refresh_from_db()
        self.own_product.refresh_from_db()
        self.assertEqual(vendor_order.status, VendorOrder.Status.CONFIRMED)
        self.assertEqual(self.own_product.stock, 5)

    def test_checkout_creates_vendor_and_platform_fulfillments_for_mixed_cart(self):
        other_vendor_user = User.objects.create_user(
            username='second-checkout-vendor',
            password='Safe-vendor-password-927!',
        )
        self.other_vendor.user = other_vendor_user
        self.other_vendor.save(update_fields=['user'])
        platform_product = Product.objects.create(
            name='Platform-Owned Greens',
            price='1.50',
            stock=5,
            units='bunch',
            category=self.category,
        )
        CartItem.objects.create(
            user=self.customer_user,
            product=self.own_product,
            quantity=2,
        )
        CartItem.objects.create(
            user=self.customer_user,
            product=self.other_product,
            quantity=1,
        )
        CartItem.objects.create(
            user=self.customer_user,
            product=platform_product,
            quantity=1,
        )
        self.client.force_login(self.customer_user)
        self.select_checkout_delivery_slot()

        response = self.client.post(
            reverse('checkout'),
            {'payment_method': Order.PaymentMethod.CASH_ON_DELIVERY},
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(customer=self.customer)
        vendor_orders = list(order.vendor_orders.order_by('vendor_id'))
        self.assertEqual(len(vendor_orders), 3)
        self.assertEqual(
            {vendor_order.vendor_id for vendor_order in vendor_orders},
            {self.vendor.id, self.other_vendor.id, None},
        )
        platform_fulfillment = next(
            vendor_order for vendor_order in vendor_orders
            if vendor_order.vendor_id is None
        )
        self.assertEqual(
            list(platform_fulfillment.items.values_list('product_id', flat=True)),
            [platform_product.id],
        )
        for vendor_order in vendor_orders:
            self.assertEqual(vendor_order.status, VendorOrder.Status.PENDING)
            self.assertEqual(vendor_order.items.count(), 1)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.vendor_user,
                kind=Notification.Kind.VENDOR_ORDER,
                order=order,
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient=other_vendor_user,
                kind=Notification.Kind.VENDOR_ORDER,
                order=order,
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.customer_user,
                kind=Notification.Kind.ORDER_PLACED,
                order=order,
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                kind=Notification.Kind.VENDOR_ORDER,
                vendor_order=platform_fulfillment,
            ).count(),
            0,
        )


class ProductCategoryTests(TestCase):
    def setUp(self):
        self.produce = Category.objects.create(name='Produce')
        self.dairy = Category.objects.create(name='Dairy')
        self.vendor = Vendor.objects.create(
            user=User.objects.create_user(
                username='catalog-vendor',
                password='safe-password',
            ),
            name='Catalog Vendor',
        )
        self.produce_product = Product.objects.create(
            name='Apples',
            price='2.50',
            stock=5,
            units='kg',
            category=self.produce,
            vendor=self.vendor,
        )
        self.dairy_product = Product.objects.create(
            name='Milk',
            price='3.00',
            stock=0,
            units='litre',
            category=self.dairy,
            vendor=self.vendor,
        )

    def test_selected_category_shows_only_its_products(self):
        response = self.client.get(
            reverse('product_list'),
            {'category': str(self.produce.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.produce_product.name)
        self.assertNotContains(response, self.dairy_product.name)
        self.assertEqual(
            list(response.context['products']),
            [self.produce_product],
        )

    def test_invalid_category_is_ignored_without_breaking_product_discovery(self):
        response = self.client.get(
            reverse('product_list'),
            {'category': '999999'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['selected_category'])
        self.assertEqual(
            set(response.context['products']),
            {self.produce_product, self.dairy_product},
        )

    def test_all_products_option_shows_products_from_every_category(self):
        response = self.client.get(reverse('product_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'All products')
        self.assertContains(response, self.produce_product.name)
        self.assertContains(response, self.dairy_product.name)
        self.assertIsNone(response.context['selected_category'])

    def test_category_selection_preserves_out_of_stock_indicator(self):
        response = self.client.get(
            reverse('product_list'),
            {'category': str(self.dairy.id)},
        )

        self.assertContains(response, self.dairy_product.name)
        self.assertContains(response, 'Out of stock')
        self.assertNotContains(response, 'Add to cart')

    def test_category_filter_composes_with_existing_product_name_search(self):
        response = self.client.get(
            reverse('product_list'),
            {'category': str(self.produce.id), 'name': 'apple'},
        )

        self.assertContains(response, self.produce_product.name)
        self.assertNotContains(response, self.dairy_product.name)
        self.assertEqual(
            list(response.context['products']),
            [self.produce_product],
        )

    def test_in_stock_filter_excludes_unavailable_items(self):
        response = self.client.get(
            reverse('product_list'),
            {'availability': 'in_stock'},
        )

        self.assertEqual(
            list(response.context['products']),
            [self.produce_product],
        )
        self.assertContains(response, 'In stock only')
        self.assertNotContains(response, self.dairy_product.name)

    def test_default_availability_keeps_out_of_stock_products_visible(self):
        response = self.client.get(reverse('product_list'))

        self.assertContains(response, self.dairy_product.name)
        self.assertContains(response, 'Out of stock')

    def test_price_sort_modes_order_vendor_listings_by_listing_price(self):
        vendor_a = Vendor.objects.create(name='Apple Vendor A')
        vendor_b = Vendor.objects.create(name='Apple Vendor B')
        cheaper_listing = Product.objects.create(
            name='Apples',
            price='1.25',
            stock=4,
            units='kg',
            category=self.produce,
            vendor=vendor_a,
        )
        dearer_listing = Product.objects.create(
            name='Apples',
            price='4.75',
            stock=4,
            units='kg',
            category=self.produce,
            vendor=vendor_b,
        )

        low_response = self.client.get(
            reverse('product_list'),
            {'sort': 'price_asc'},
        )
        high_response = self.client.get(
            reverse('product_list'),
            {'sort': 'price_desc'},
        )

        low_products = list(low_response.context['products'])
        high_products = list(high_response.context['products'])
        self.assertLess(
            low_products.index(cheaper_listing),
            low_products.index(dearer_listing),
        )
        self.assertLess(
            high_products.index(dearer_listing),
            high_products.index(cheaper_listing),
        )

    def test_name_and_availability_sort_modes_are_server_side_and_deterministic(self):
        name_z = Product.objects.create(
            name='Zucchini',
            price='2.00',
            stock=0,
            units='kg',
            category=self.produce,
            vendor=self.vendor,
        )
        name_a = Product.objects.create(
            name='Apricots',
            price='2.00',
            stock=2,
            units='kg',
            category=self.produce,
            vendor=self.vendor,
        )

        ascending = self.client.get(
            reverse('product_list'),
            {'sort': 'name_asc'},
        )
        descending = self.client.get(
            reverse('product_list'),
            {'sort': 'name_desc'},
        )
        availability = self.client.get(
            reverse('product_list'),
            {'sort': 'availability'},
        )

        ascending_products = list(ascending.context['products'])
        descending_products = list(descending.context['products'])
        availability_products = list(availability.context['products'])
        self.assertLess(ascending_products.index(name_a), ascending_products.index(name_z))
        self.assertLess(descending_products.index(name_z), descending_products.index(name_a))
        self.assertEqual(availability_products[0], self.produce_product)
        self.assertContains(ascending, 'Name: A to Z')
        self.assertContains(descending, 'Name: Z to A')
        self.assertContains(availability, 'Availability')

    def test_newest_sort_uses_product_creation_order(self):
        newer_product = Product.objects.create(
            name='Newest item',
            price='5.00',
            stock=2,
            units='each',
            category=self.produce,
            vendor=self.vendor,
        )

        response = self.client.get(
            reverse('product_list'),
            {'sort': 'newest'},
        )

        self.assertEqual(
            list(response.context['products'])[0],
            newer_product,
        )
        self.assertContains(response, 'Newest')

    def test_relevance_sort_keeps_existing_name_vendor_ordering(self):
        response = self.client.get(
            reverse('product_list'),
            {'sort': 'relevance'},
        )

        self.assertEqual(
            list(response.context['products']),
            list(
                Product.objects.select_related('category', 'vendor')
                .order_by('name', 'vendor__name', 'id')
            ),
        )

    def test_search_category_availability_and_sort_combine(self):
        vendor = Vendor.objects.create(name='Green Apple Vendor')
        in_stock_listing = Product.objects.create(
            name='Green Apples',
            description='Crisp green fruit',
            price='3.00',
            stock=5,
            units='kg',
            category=self.produce,
            vendor=vendor,
        )
        Product.objects.create(
            name='Green Apples',
            price='1.00',
            stock=0,
            units='kg',
            category=self.produce,
            vendor=Vendor.objects.create(name='Unavailable Apple Vendor'),
        )

        response = self.client.get(
            reverse('product_list'),
            {
                'name': 'green',
                'category': str(self.produce.id),
                'availability': 'in_stock',
                'sort': 'price_desc',
            },
        )

        self.assertEqual(list(response.context['products']), [in_stock_listing])
        self.assertContains(response, 'name="name"')
        self.assertContains(response, f'value="{self.produce.id}" selected')
        self.assertContains(response, 'value="in_stock" selected')
        self.assertContains(response, 'value="price_desc" selected')
        self.assertContains(response, 'Search: green')
        self.assertContains(response, 'Category: Produce')
        self.assertContains(response, 'In stock only')
        self.assertContains(response, 'Price: High to Low')

    def test_clear_filters_link_clears_search_category_availability_and_sort(self):
        response = self.client.get(
            reverse('product_list'),
            {
                'name': 'milk',
                'category': str(self.dairy.id),
                'availability': 'in_stock',
                'sort': 'price_asc',
            },
        )

        self.assertContains(
            response,
            f'href="{reverse("product_list")}"',
        )
        self.assertContains(response, 'Clear filters')

    def test_header_search_suggestions_are_real_products_and_keep_filter_state(self):
        response = self.client.get(
            reverse('product_list'),
            {'category': str(self.produce.id)},
        )

        self.assertContains(response, 'Search for vegetables, fruits and more...')
        self.assertContains(response, '<option value="Apples">', html=False)
        self.assertNotContains(response, '<option value="Milk">', html=False)
        self.assertContains(response, 'aria-label="Browse by category"')

    def test_shared_header_cart_does_not_show_guest_quantity(self):
        self.client.post(
            reverse('add_to_cart', args=[self.produce_product.id]),
            {'quantity': '2'},
        )

        response = self.client.get(reverse('product_list'))

        self.assertContains(response, 'My Cart')
        self.assertContains(response, 'aria-label="My Cart"')
        self.assertNotContains(response, 'aria-label="My Cart, 2 items"')

    def test_shared_header_cart_does_not_show_authenticated_quantity(self):
        user = User.objects.create_user(
            username='header-cart-customer',
            password='safe-password',
        )
        CartItem.objects.create(
            user=user,
            product=self.produce_product,
            quantity=3,
        )
        self.client.force_login(user)

        response = self.client.get(reverse('product_list'))

        self.assertContains(response, 'aria-label="My Cart"')
        self.assertNotContains(response, 'aria-label="My Cart, 3 items"')

    def test_header_search_keeps_active_marketplace_filters(self):
        response = self.client.get(
            reverse('product_list'),
            {
                'name': 'apple',
                'category': str(self.produce.id),
                'availability': 'in_stock',
                'sort': 'price_asc',
            },
        )

        self.assertContains(
            response,
            f'name="category" value="{self.produce.id}"',
        )
        self.assertContains(response, 'name="availability" value="in_stock"')
        self.assertContains(response, 'name="sort" value="price_asc"')

    def test_product_catalog_hides_products_without_a_vendor(self):
        self.dairy_product.vendor = None
        self.dairy_product.save(update_fields=['vendor'])
        self.produce_product.stock = 2
        self.produce_product.save(update_fields=['stock'])
        response = self.client.get(reverse('product_list'))
        self.assertContains(response, 'Low stock')
        self.assertContains(response, 'Low stock')
        self.assertNotContains(response, 'Local vendor')
        self.assertNotContains(response, self.dairy_product.name)
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.produce_product.id])}"',
        )
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.dairy_product.id])}"',
        )

    def test_vendor_specific_products_remain_distinct_under_price_sort(self):
        vendor_a = Vendor.objects.create(name='Produce Shop A')
        vendor_b = Vendor.objects.create(name='Produce Shop B')
        listing_a = Product.objects.create(
            name='Carrots',
            price='2.00',
            stock=5,
            units='kg',
            category=self.produce,
            vendor=vendor_a,
        )
        listing_b = Product.objects.create(
            name='Carrots',
            price='3.00',
            stock=2,
            units='kg',
            category=self.produce,
            vendor=vendor_b,
        )

        response = self.client.get(
            reverse('product_list'),
            {'sort': 'price_asc'},
        )

        filtered_listings = list(response.context['products'])
        self.assertIn(listing_a, filtered_listings)
        self.assertIn(listing_b, filtered_listings)
        self.assertLess(
            filtered_listings.index(listing_a),
            filtered_listings.index(listing_b),
        )
        self.assertContains(response, vendor_a.name)
        self.assertContains(response, vendor_b.name)

    def test_empty_combined_filter_state_has_clear_empty_message(self):
        response = self.client.get(
            reverse('product_list'),
            {
                'name': 'no such product',
                'category': str(self.produce.id),
                'availability': 'in_stock',
            },
        )

        self.assertEqual(list(response.context['products']), [])
        self.assertContains(response, 'No products match the current search and filters.')
        self.assertContains(response, 'Clear filters')


class VendorSearchTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Produce')
        self.vendor = Vendor.objects.create(
            name='Green Valley Farms',
            email='hello@example.com',
            phone='555-0123',
        )
        self.product = Product.objects.create(
            name='Apples',
            price='2.50',
            stock=5,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )

    def test_search_matches_vendor_name_and_displays_products(self):
        response = self.client.get(reverse('vendor_search'), {'name': 'valley'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.vendor.name)
        self.assertContains(response, self.vendor.email)
        self.assertContains(response, self.product.name)

    def test_product_detail_shows_its_own_vendor_price_and_stock(self):
        other_vendor = Vendor.objects.create(name='Other Apple Seller')
        other_listing = Product.objects.create(
            name='Apples',
            price='3.75',
            stock=0,
            units='kg',
            description='Sweet orchard apples.',
            category=self.category,
            vendor=other_vendor,
        )

        response = self.client.get(
            reverse('product_detail', args=[other_listing.id]),
        )

        self.assertContains(response, other_vendor.name)
        self.assertContains(response, '3.75')
        self.assertContains(response, 'Sweet orchard apples.')
        self.assertContains(response, 'Stock:</strong> 0 kg')
        self.assertContains(response, 'Out of stock')
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[other_listing.id])}"',
        )
        self.assertEqual(response.context['product'], other_listing)

    def test_product_detail_targets_the_requested_listing_id(self):
        other_vendor = Vendor.objects.create(name='Other Apple Seller')
        other_listing = Product.objects.create(
            name=self.product.name,
            price='3.75',
            stock=4,
            units='kg',
            description='A different vendor listing.',
            category=self.category,
            vendor=other_vendor,
        )

        response = self.client.get(
            reverse('product_detail', args=[other_listing.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['product'].id, other_listing.id)
        self.assertContains(response, other_vendor.name)
        self.assertContains(response, 'A different vendor listing.')
        self.assertContains(response, '3.75')
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[other_listing.id])}"',
        )
        self.assertEqual(
            response.content.decode().count(
                f'action="{reverse("add_to_cart", args=[other_listing.id])}"',
            ),
            1,
        )

    def test_product_detail_shows_its_image_and_alt_text(self):
        image_bytes = BytesIO()
        Image.new('RGB', (1, 1), color='green').save(image_bytes, format='PNG')
        image = SimpleUploadedFile(
            'apples.png',
            image_bytes.getvalue(),
            content_type='image/png',
        )

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.product.image.save('apples.png', image, save=True)
                response = self.client.get(
                    reverse('product_detail', args=[self.product.id]),
                )

                self.assertContains(
                    response,
                    f'<img src="{self.product.image.url}" alt="{self.product.name}">',
                    html=True,
                )

    def test_product_detail_has_responsive_media_and_submit_feedback(self):
        response = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )
        content = response.content.decode()

        self.assertContains(response, 'class="product-detail-card"')
        self.assertContains(response, 'data-loading-text="Adding to cart…"', html=False)
        self.assertRegex(
            content,
            r'\.product-detail-media img, \.product-detail-image\s*\{[^}]*aspect-ratio:\s*4 / 3',
        )
        self.assertContains(response, "document.addEventListener('submit'", html=False)

    def test_in_stock_product_detail_allows_add_to_cart_for_its_listing(self):
        response = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )

        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.product.id])}"',
        )
        self.assertContains(response, f'max="{self.product.stock}"')
        self.assertNotContains(response, 'Out of stock')

    def test_product_detail_returns_not_found_for_unknown_listing_id(self):
        response = self.client.get(
            reverse('product_detail', args=[self.product.id + 1000]),
        )

        self.assertEqual(response.status_code, 404)

    def test_product_detail_recommends_other_listings_from_same_category(self):
        other_category = Category.objects.create(name='Dairy')
        recommended = Product.objects.create(
            name='Pears',
            price='3.00',
            stock=4,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )
        unrelated = Product.objects.create(
            name='Milk',
            price='4.00',
            stock=8,
            units='litre',
            category=other_category,
        )

        response = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )

        self.assertEqual(
            list(response.context['recommendations']),
            [recommended],
        )
        self.assertContains(response, 'You may also like')
        self.assertContains(response, recommended.name)
        self.assertNotContains(response, unrelated.name)
        self.assertNotContains(
            response,
            f'href="{reverse("product_detail", args=[self.product.id])}"',
        )

    def test_recommendations_prefer_in_stock_vendor_specific_listings(self):
        out_of_stock_vendor = Vendor.objects.create(name='Out of Stock Seller')
        in_stock_vendor = Vendor.objects.create(name='Available Seller')
        out_of_stock_listing = Product.objects.create(
            name='Carrots',
            price='1.50',
            stock=0,
            units='kg',
            category=self.category,
            vendor=out_of_stock_vendor,
        )
        available_listing = Product.objects.create(
            name='Carrots',
            price='2.00',
            stock=3,
            units='kg',
            category=self.category,
            vendor=in_stock_vendor,
        )

        response = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )
        recommendations = list(response.context['recommendations'])

        self.assertEqual(
            recommendations,
            [available_listing, out_of_stock_listing],
        )
        self.assertContains(response, available_listing.vendor.name)
        self.assertContains(response, out_of_stock_listing.vendor.name)
        self.assertContains(response, 'Out of stock')
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[available_listing.id])}"',
        )
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[out_of_stock_listing.id])}"',
        )

    def test_recommendation_add_to_cart_uses_exact_product_listing(self):
        other_vendor = Vendor.objects.create(name='Recommendation Vendor')
        recommended_listing = Product.objects.create(
            name='Pears',
            price='3.00',
            stock=4,
            units='kg',
            category=self.category,
            vendor=other_vendor,
        )

        response = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[recommended_listing.id])}"',
        )
        cart_response = self.client.post(
            reverse('add_to_cart', args=[recommended_listing.id]),
            {'quantity': '1'},
            follow=True,
        )

        self.assertEqual(
            self.client.session['cart'],
            {str(recommended_listing.id): 1},
        )
        self.assertContains(cart_response, other_vendor.name)

    def test_product_detail_has_no_recommendation_section_without_other_category_listings(self):
        response = self.client.get(
            reverse('product_detail', args=[self.product.id]),
        )

        self.assertEqual(list(response.context['recommendations']), [])
        self.assertNotContains(response, 'You may also like')

    def test_product_detail_recommendation_query_count_is_bounded(self):
        for index in range(10):
            Product.objects.create(
                name=f'Recommended produce {index}',
                price='1.00',
                stock=index,
                units='kg',
                category=self.category,
                vendor=self.vendor,
            )

        with patch('market.views._record_recently_viewed'):
            with self.assertNumQueries(2):
                response = self.client.get(
                    reverse('product_detail', args=[self.product.id]),
                )
                list(response.context['recommendations'])

        recommendations = list(response.context['recommendations'])
        self.assertLessEqual(len(recommendations), 8)

    def test_search_with_no_match_displays_empty_state(self):
        response = self.client.get(reverse('vendor_search'), {'name': 'unknown'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No vendors match that name.')
        self.assertNotContains(response, self.vendor.name)

    def test_unsubmitted_search_does_not_list_all_vendors(self):
        response = self.client.get(reverse('vendor_search'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.vendor.name)

    def test_product_search_shows_each_vendor_listing_for_matching_name(self):
        second_vendor = Vendor.objects.create(
            name='Orchard Co-op',
            email='sales@orchard.example',
            phone='555-0456',
        )
        second_product = Product.objects.create(
            name='Apples',
            price='2.75',
            stock=8,
            units='kg',
            category=self.category,
            vendor=second_vendor,
        )

        response = self.client.get(reverse('product_list'), {'name': 'apple'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.vendor.name)
        self.assertContains(response, second_vendor.name)
        self.assertContains(response, self.vendor.email)
        self.assertContains(response, second_vendor.email)
        self.assertContains(response, f'Available: {self.product.stock} {self.product.units}')
        self.assertContains(response, f'Available: {second_product.stock} {second_product.units}')

    def test_product_search_with_no_match_displays_empty_state(self):
        response = self.client.get(reverse('product_list'), {'name': 'unknown'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No products match the current search and filters.')
        self.assertNotContains(response, self.vendor.name)

    def test_product_search_matches_description_case_insensitively(self):
        self.product.description = 'Crisp and locally grown fruit'
        self.product.save(update_fields=['description'])

        response = self.client.get(reverse('product_list'), {'name': 'LOCALLY'})

        self.assertContains(response, self.product.name)
        self.assertContains(response, f'Category: {self.category.name}')

    def test_product_search_matches_category_name(self):
        response = self.client.get(
            reverse('product_list'),
            {'name': 'duce'},
        )

        self.assertContains(response, self.product.name)
        self.assertContains(response, f'Category: {self.category.name}')

    def test_product_search_matches_vendor_and_shop_names(self):
        self.vendor.shop_name = 'Valley Orchard'
        self.vendor.save(update_fields=['shop_name'])

        vendor_response = self.client.get(
            reverse('product_list'),
            {'name': 'green val'},
        )
        shop_response = self.client.get(
            reverse('product_list'),
            {'name': 'ORCH'},
        )

        self.assertContains(vendor_response, self.product.name)
        self.assertContains(vendor_response, self.vendor.name)
        self.assertContains(
            shop_response,
            self.product.name,
        )
        self.assertContains(
            shop_response,
            f'Vendor: {self.vendor.name} — {self.vendor.shop_name}',
        )

    def test_product_search_includes_out_of_stock_products_without_cart_action(self):
        self.product.stock = 0
        self.product.save(update_fields=['stock'])

        response = self.client.get(
            reverse('product_list'),
            {'name': 'apple'},
        )

        self.assertContains(response, self.product.name)
        self.assertContains(response, 'Out of stock')
        self.assertNotContains(response, 'Add to cart')


class RecentlyViewedTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Produce')
        self.vendor = Vendor.objects.create(
            name='Green Valley Farms',
            shop_name='Green Valley Shop',
        )
        self.product = Product.objects.create(
            name='Carrots',
            price='2.50',
            stock=5,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )

    def create_product(self, name, vendor=None, stock=4):
        return Product.objects.create(
            name=name,
            price='3.25',
            stock=stock,
            units='kg',
            category=self.category,
            vendor=vendor or self.vendor,
        )

    def test_guest_detail_view_records_exact_vendor_listing_in_session(self):
        other_vendor = Vendor.objects.create(name='Other Carrot Seller')
        other_listing = self.create_product('Carrots', vendor=other_vendor)

        self.client.get(reverse('product_detail', args=[self.product.id]))
        self.client.get(reverse('product_detail', args=[other_listing.id]))

        self.assertEqual(
            self.client.session['recently_viewed_product_ids'],
            [other_listing.id, self.product.id],
        )

    def test_authenticated_detail_view_records_history_for_its_customer(self):
        user = User.objects.create_user(
            username='recent-owner',
            password='safe-password',
        )
        customer = Customer.objects.create(
            user=user,
            address='1 Market Street',
            phone='555-0100',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('product_detail', args=[self.product.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            RecentlyViewedItem.objects.filter(
                customer=customer,
                product=self.product,
            ).exists(),
        )

    def test_history_is_most_recent_first_and_duplicate_view_moves_listing(self):
        second_product = self.create_product('Potatoes')

        for product in (self.product, second_product, self.product):
            self.client.get(reverse('product_detail', args=[product.id]))
        response = self.client.get(reverse('product_list'))

        self.assertEqual(
            [product.id for product in response.context['recently_viewed_products']],
            [self.product.id, second_product.id],
        )
        self.assertEqual(
            self.client.session['recently_viewed_product_ids'],
            [self.product.id, second_product.id],
        )

    def test_guest_history_is_limited_to_twelve_listings(self):
        products = [self.create_product(f'Produce {index}') for index in range(14)]

        for product in products:
            self.client.get(reverse('product_detail', args=[product.id]))

        self.assertEqual(
            self.client.session['recently_viewed_product_ids'],
            [product.id for product in reversed(products[-12:])],
        )

    def test_authenticated_history_is_limited_to_twelve_listings(self):
        user = User.objects.create_user(
            username='recent-limit-owner',
            password='safe-password',
        )
        customer = Customer.objects.create(
            user=user,
            address='1 Market Street',
            phone='555-0100',
        )
        products = [self.create_product(f'Fresh item {index}') for index in range(14)]
        self.client.force_login(user)

        for product in products:
            self.client.get(reverse('product_detail', args=[product.id]))

        self.assertEqual(
            list(
                RecentlyViewedItem.objects.filter(customer=customer)
                .order_by('-viewed_at', '-id')
                .values_list('product_id', flat=True)
            ),
            [product.id for product in reversed(products[-12:])],
        )

    def test_guest_sessions_do_not_share_history(self):
        other_client = self.client_class()
        self.client.get(reverse('product_detail', args=[self.product.id]))
        response = other_client.get(reverse('product_list'))

        self.assertEqual(response.context['recently_viewed_products'], [])
        self.assertContains(response, 'Products you view will appear here.')

    def test_authenticated_customers_see_only_their_own_history(self):
        first_user = User.objects.create_user(
            username='first-recent-owner',
            password='safe-password',
        )
        second_user = User.objects.create_user(
            username='second-recent-owner',
            password='safe-password',
        )
        first_customer = Customer.objects.create(
            user=first_user,
            address='1 Market Street',
            phone='555-0100',
        )
        second_customer = Customer.objects.create(
            user=second_user,
            address='2 Market Street',
            phone='555-0101',
        )
        RecentlyViewedItem.objects.create(
            customer=first_customer,
            product=self.product,
        )
        second_client = self.client_class()
        second_client.force_login(second_user)

        response = second_client.get(reverse('product_list'))

        self.assertEqual(response.context['recently_viewed_products'], [])
        self.assertContains(response, 'Products you view will appear here.')
        self.assertFalse(
            RecentlyViewedItem.objects.filter(customer=second_customer).exists(),
        )

    def test_guest_history_is_not_merged_into_customer_history_on_login(self):
        user = User.objects.create_user(
            username='guest-to-customer',
            password='safe-password',
        )
        Customer.objects.create(user=user, address='1 Market Street', phone='555-0100')
        self.client.get(reverse('product_detail', args=[self.product.id]))

        self.client.force_login(user)
        response = self.client.get(reverse('product_list'))

        self.assertEqual(response.context['recently_viewed_products'], [])
        self.assertFalse(RecentlyViewedItem.objects.exists())

    def test_deleted_guest_listing_is_pruned_without_breaking_catalog(self):
        self.client.get(reverse('product_detail', args=[self.product.id]))
        self.product.delete()

        response = self.client.get(reverse('product_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['recently_viewed_products'], [])
        self.assertEqual(self.client.session['recently_viewed_product_ids'], [])

    def test_deleted_authenticated_listing_is_removed_by_relationship(self):
        user = User.objects.create_user(
            username='deleted-recent-owner',
            password='safe-password',
        )
        customer = Customer.objects.create(
            user=user,
            address='1 Market Street',
            phone='555-0100',
        )
        RecentlyViewedItem.objects.create(customer=customer, product=self.product)

        self.product.delete()

        self.assertFalse(RecentlyViewedItem.objects.filter(customer=customer).exists())

    def test_out_of_stock_recent_listing_is_visible_but_not_purchasable(self):
        self.product.stock = 0
        self.product.save(update_fields=['stock'])
        self.client.get(reverse('product_detail', args=[self.product.id]))

        response = self.client.get(reverse('product_list'))

        self.assertContains(response, 'Recently Viewed')
        self.assertContains(response, 'Out of stock')
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.product.id])}"',
        )

    def test_available_recent_listing_uses_existing_add_to_cart_flow(self):
        self.client.get(reverse('product_detail', args=[self.product.id]))
        response = self.client.get(reverse('product_list'))

        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.product.id])}"',
        )
        cart_response = self.client.post(
            reverse('add_to_cart', args=[self.product.id]),
            {'quantity': '1'},
        )

        self.assertRedirects(cart_response, reverse('cart'))
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product.id): 1},
        )

    def test_empty_history_has_a_quiet_empty_state(self):
        response = self.client.get(reverse('product_list'))

        self.assertContains(response, 'Recently Viewed')
        self.assertContains(response, 'Products you view will appear here.')


class HomePageTests(TestCase):
    def setUp(self):
        self.greens = Category.objects.create(name='Garden greens')
        self.roots = Category.objects.create(name='Root vegetables')
        self.empty_category = Category.objects.create(name='Coming soon')
        self.vendor = Vendor.objects.create(
            name='Market Garden',
            shop_name='Green Basket',
        )
        self.spinach = Product.objects.create(
            name='Baby spinach',
            price='3.20',
            stock=8,
            units='bunch',
            description='Tender local spinach.',
            category=self.greens,
            vendor=self.vendor,
        )
        self.carrots = Product.objects.create(
            name='Sweet carrots',
            price='1.75',
            stock=0,
            units='kg',
            category=self.roots,
            vendor=self.vendor,
        )

    def test_homepage_renders_original_hero_and_dynamic_categories(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'market/home.html')
        self.assertContains(response, 'Fresh picks from your neighborhood.')
        self.assertContains(response, 'Shop by category')
        self.assertContains(response, 'Garden greens')
        self.assertContains(response, 'Root vegetables')
        self.assertNotContains(response, 'Coming soon')
        self.assertContains(
            response,
            f'href="{reverse("product_list")}?category={self.greens.id}"',
        )

    def test_customer_registration_page_renders_account_creation_form(self):
        response = self.client.get(reverse('register'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'customer-register-layout')
        self.assertContains(response, 'Create your customer account')
        self.assertContains(response, 'name="password_confirm"')
        self.assertContains(response, 'Vendor signup')
        self.assertContains(
            response,
            f'href="{reverse("register")}" aria-current="page">Customer signup</a>',
        )
        self.assertContains(
            response,
            f'href="{reverse("vendor_register")}">Vendor signup</a>',
        )

    def test_homepage_uses_shared_login_and_join_us_entry_points(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("login")}">Login</a>')
        self.assertContains(response, f'href="{reverse("register")}">Join us</a>')
        self.assertNotContains(response, 'Vendor login')
        self.assertNotContains(response, 'Become a vendor')

    def test_shared_login_signs_in_vendor_and_redirects_to_dashboard(self):
        vendor_user = User.objects.create_user(
            username='shared-login-vendor',
            password='safe-password',
        )
        Vendor.objects.create(user=vendor_user, name='Shared Login Vendor')
        session = self.client.session
        session['cart'] = {str(self.spinach.id): 1}
        session.save()

        response = self.client.post(
            reverse('login'),
            {'username': vendor_user.username, 'password': 'safe-password'},
        )

        self.assertRedirects(response, reverse('vendor_dashboard'))
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.assertNotIn('cart', self.client.session)
        self.assertFalse(CartItem.objects.filter(user=vendor_user).exists())

    def test_logged_in_vendor_sees_dashboard_instead_of_greeting_or_vendor_login(self):
        vendor_user = User.objects.create_user(
            username='homepage-vendor',
            password='safe-password',
        )
        Vendor.objects.create(user=vendor_user, name='Homepage Vendor')
        self.client.force_login(vendor_user)

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("vendor_dashboard")}">Vendor dashboard</a>',
        )
        self.assertNotContains(response, 'Hi, homepage-vendor')
        self.assertNotContains(response, 'Vendor login')
        self.assertNotContains(response, 'Become a vendor')

    def test_logged_in_vendor_does_not_see_customer_shopping_navigation(self):
        vendor_user = User.objects.create_user(
            username='marketplace-vendor',
            password='safe-password',
        )
        Vendor.objects.create(user=vendor_user, name='Marketplace Vendor')
        self.client.force_login(vendor_user)

        for page in ('home', 'product_list', 'vendor_search'):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))

                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, f'href="{reverse("product_list")}">Shop</a>')
                self.assertNotContains(response, f'href="{reverse("vendor_search")}">Local shops</a>')
                self.assertNotContains(response, f'href="{reverse("cart")}"')
                self.assertNotContains(response, 'Search products')
                self.assertNotContains(response, 'DELIVERING TO')
                self.assertContains(response, 'Vendor dashboard')

    def test_homepage_loads_featured_vendor_listings_and_preserves_cart_actions(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(
            {product.id for product in response.context['featured_products']},
            {self.spinach.id, self.carrots.id},
        )
        self.assertContains(response, 'Baby spinach')
        self.assertContains(response, 'Green Basket')
        self.assertContains(response, 'Sweet carrots')
        self.assertContains(response, 'Out of stock')
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.spinach.id])}"',
        )
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.carrots.id])}"',
        )

    def test_homepage_hides_listings_without_a_vendor_account(self):
        self.spinach.vendor = None
        self.spinach.save(update_fields=['vendor'])

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Baby spinach')
        self.assertNotContains(response, 'Local vendor')

    def test_product_catalog_and_detail_hide_listings_without_a_vendor_account(self):
        self.spinach.vendor = None
        self.spinach.save(update_fields=['vendor'])

        listing_response = self.client.get(reverse('product_list'))
        detail_response = self.client.get(
            reverse('product_detail', args=[self.spinach.id]),
        )

        self.assertEqual(listing_response.status_code, 200)
        self.assertNotContains(listing_response, 'Baby spinach')
        self.assertEqual(detail_response.status_code, 404)

    def test_homepage_uses_existing_guest_recently_viewed_history(self):
        self.client.get(reverse('product_detail', args=[self.spinach.id]))

        response = self.client.get(reverse('home'))

        self.assertEqual(
            [product.id for product in response.context['recently_viewed_products']],
            [self.spinach.id],
        )
        self.assertContains(response, 'Recently viewed')
        self.assertContains(response, 'Baby spinach')
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.spinach.id])}"',
        )

    def test_homepage_reads_customer_default_delivery_address(self):
        user = User.objects.create_user(
            username='home-delivery-customer',
            password='safe-password',
        )
        customer = Customer.objects.create(
            user=user,
            address='Legacy customer area',
            phone='555-0160',
        )
        DeliveryAddress.objects.create(
            customer=customer,
            label='Home',
            recipient_name='Home Customer',
            address_line_1='17 Orchard Road',
            city='Springfield',
            state='State',
            postal_code='12345',
            phone='555-0160',
            is_default=True,
        )
        self.client.force_login(user)

        response = self.client.get(reverse('home'))

        self.assertContains(response, 'Springfield, State')
        self.assertContains(
            response,
            f'href="{reverse("customer_profile")}"',
        )
        self.assertNotContains(response, 'Legacy customer area')

    def test_homepage_retains_existing_cart_and_wishlist_navigation(self):
        user = User.objects.create_user(
            username='home-navigation-customer',
            password='safe-password',
        )
        Customer.objects.create(
            user=user,
            address='Market Street',
            phone='555-0161',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('home'))

        self.assertContains(response, f'href="{reverse("cart")}"')
        self.assertContains(response, f'href="{reverse("wishlist")}"')
        self.assertContains(response, 'Local shops')

    def test_local_shop_directory_keeps_customer_marketplace_navigation(self):
        response = self.client.get(reverse('vendor_search'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Local shops')
        self.assertContains(response, 'Shop products')
        self.assertContains(response, 'My Cart')


class WishlistTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Leafy Greens')
        self.vendor = Vendor.objects.create(
            name='Valley Growers',
            shop_name='Valley Farm Shop',
        )
        self.product = Product.objects.create(
            name='Spinach',
            price='2.50',
            stock=5,
            units='bunch',
            description='Fresh spinach.',
            category=self.category,
            vendor=self.vendor,
        )
        self.user = User.objects.create_user(
            username='wishlist-owner',
            password='safe-password',
        )
        self.customer = Customer.objects.create(
            user=self.user,
            address='1 Market Street',
            phone='555-0100',
        )
        self.client.force_login(self.user)

    def test_customer_can_add_listing_to_wishlist(self):
        response = self.client.post(
            reverse('add_to_wishlist', args=[self.product.id]),
        )

        self.assertRedirects(response, reverse('wishlist'))
        self.assertTrue(
            WishlistItem.objects.filter(
                customer=self.customer,
                product=self.product,
            ).exists(),
        )

    def test_duplicate_wishlist_submission_keeps_one_exact_listing(self):
        other_vendor = Vendor.objects.create(name='Other Spinach Vendor')
        other_listing = Product.objects.create(
            name='Spinach',
            price='3.00',
            stock=2,
            units='bunch',
            category=self.category,
            vendor=other_vendor,
        )

        self.client.post(reverse('add_to_wishlist', args=[self.product.id]))
        self.client.post(reverse('add_to_wishlist', args=[self.product.id]))
        self.client.post(reverse('add_to_wishlist', args=[other_listing.id]))

        self.assertEqual(
            WishlistItem.objects.filter(customer=self.customer).count(),
            2,
        )
        self.assertEqual(
            set(
                WishlistItem.objects.filter(customer=self.customer)
                .values_list('product_id', flat=True)
            ),
            {self.product.id, other_listing.id},
        )

    def test_wishlist_displays_listing_details_and_current_availability(self):
        other_vendor = Vendor.objects.create(name='Other Spinach Vendor')
        other_listing = Product.objects.create(
            name='Spinach',
            price='3.00',
            stock=0,
            units='bunch',
            category=self.category,
            vendor=other_vendor,
        )
        WishlistItem.objects.create(customer=self.customer, product=self.product)
        WishlistItem.objects.create(customer=self.customer, product=other_listing)

        response = self.client.get(reverse('wishlist'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertContains(response, self.category.name)
        self.assertContains(response, self.vendor.name)
        self.assertContains(response, self.vendor.shop_name)
        self.assertContains(response, '2.50 / bunch')
        self.assertContains(response, '5 available')
        self.assertContains(response, '3.00 / bunch')
        self.assertContains(response, 'Other Spinach Vendor')
        self.assertContains(response, 'Out of stock')
        self.assertContains(
            response,
            f'action="{reverse("add_to_cart", args=[self.product.id])}"',
        )
        self.assertNotContains(
            response,
            f'action="{reverse("add_to_cart", args=[other_listing.id])}"',
        )

    def test_customer_can_remove_own_wishlist_item(self):
        WishlistItem.objects.create(customer=self.customer, product=self.product)

        response = self.client.post(
            reverse('remove_from_wishlist', args=[self.product.id]),
        )

        self.assertRedirects(response, reverse('wishlist'))
        self.assertFalse(WishlistItem.objects.filter(customer=self.customer).exists())

    def test_product_card_shows_active_wishlist_and_removes_in_place(self):
        WishlistItem.objects.create(customer=self.customer, product=self.product)
        response = self.client.get(reverse('product_list'))

        self.assertContains(response, 'Remove Spinach from wishlist')
        self.assertContains(
            response,
            f'action="{reverse("remove_from_wishlist", args=[self.product.id])}"',
        )
        remove_response = self.client.post(
            reverse('remove_from_wishlist', args=[self.product.id]),
            {'next': reverse('product_list')},
        )

        self.assertRedirects(remove_response, reverse('product_list'))
        self.assertFalse(WishlistItem.objects.filter(customer=self.customer).exists())

    def test_product_card_adds_wishlist_item_and_returns_to_current_listing(self):
        response = self.client.post(
            reverse('add_to_wishlist', args=[self.product.id]),
            {'next': f'{reverse("product_list")}?name=spinach'},
        )

        self.assertRedirects(
            response,
            f'{reverse("product_list")}?name=spinach',
        )
        self.assertTrue(
            WishlistItem.objects.filter(
                customer=self.customer,
                product=self.product,
            ).exists(),
        )

    def test_customers_cannot_view_or_remove_another_customers_wishlist_item(self):
        other_user = User.objects.create_user(
            username='other-wishlist-owner',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='2 Market Street',
            phone='555-0101',
        )
        other_item = WishlistItem.objects.create(
            customer=other_customer,
            product=self.product,
        )

        response = self.client.get(reverse('wishlist'))
        remove_response = self.client.post(
            reverse('remove_from_wishlist', args=[self.product.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.product.name)
        self.assertEqual(remove_response.status_code, 404)
        self.assertTrue(WishlistItem.objects.filter(pk=other_item.pk).exists())

    def test_each_customer_can_save_the_same_listing_to_their_own_wishlist(self):
        other_user = User.objects.create_user(
            username='second-wishlist-owner',
            password='safe-password',
        )
        other_customer = Customer.objects.create(
            user=other_user,
            address='2 Market Street',
            phone='555-0101',
        )
        WishlistItem.objects.create(customer=self.customer, product=self.product)
        self.client.force_login(other_user)

        response = self.client.post(
            reverse('add_to_wishlist', args=[self.product.id]),
        )

        self.assertRedirects(response, reverse('wishlist'))
        self.assertEqual(
            WishlistItem.objects.filter(product=self.product).count(),
            2,
        )
        self.assertTrue(
            WishlistItem.objects.filter(
                customer=other_customer,
                product=self.product,
            ).exists(),
        )

    def test_available_wishlist_listing_can_be_added_to_vendor_specific_cart(self):
        other_vendor = Vendor.objects.create(name='Another Spinach Shop')
        other_listing = Product.objects.create(
            name='Spinach',
            price='3.00',
            stock=2,
            units='bunch',
            category=self.category,
            vendor=other_vendor,
        )
        WishlistItem.objects.create(customer=self.customer, product=other_listing)

        wishlist_response = self.client.get(reverse('wishlist'))
        self.assertContains(
            wishlist_response,
            f'action="{reverse("add_to_cart", args=[other_listing.id])}"',
        )
        cart_response = self.client.post(
            reverse('add_to_cart', args=[other_listing.id]),
            {'quantity': '1'},
            follow=True,
        )

        self.assertEqual(
            list(
                CartItem.objects.filter(user=self.user)
                .values_list('product_id', 'quantity')
            ),
            [(other_listing.id, 1)],
        )
        self.assertContains(cart_response, other_vendor.name)

    def test_wishlist_requires_an_authenticated_customer(self):
        self.client.logout()
        anonymous_response = self.client.get(reverse('wishlist'))
        self.assertRedirects(
            anonymous_response,
            f"{reverse('login')}?next={reverse('wishlist')}",
        )

        vendor_user = User.objects.create_user(
            username='wishlist-vendor',
            password='safe-password',
        )
        Vendor.objects.create(user=vendor_user, name='Wishlist Vendor')
        self.client.force_login(vendor_user)
        vendor_response = self.client.get(reverse('wishlist'))
        self.assertEqual(vendor_response.status_code, 404)


class VendorAccountTests(TestCase):
    def vendor_registration_data(self):
        return {
            'username': 'newvendor',
            'email': 'owner@shop.example',
            'vendor_name': 'Sam Green',
            'shop_name': 'Green Grocer',
            'shop_address': '12 Market Road',
            'business_registration': 'LIC-2026-123',
            'description': 'Local produce and pantry goods.',
            'phone': '555-0145',
            'password': 'Safe-vendor-password-927!',
            'password_confirm': 'Safe-vendor-password-927!',
        }

    def test_vendor_can_register_and_is_signed_in(self):
        response = self.client.post(
            reverse('vendor_register'),
            self.vendor_registration_data(),
        )

        vendor = Vendor.objects.get(name='Sam Green')
        self.assertRedirects(response, reverse('vendor_dashboard'))
        self.assertEqual(vendor.user.username, 'newvendor')
        self.assertEqual(vendor.shop_name, 'Green Grocer')
        self.assertEqual(vendor.shop_address, '12 Market Road')
        self.assertEqual(vendor.business_registration, 'LIC-2026-123')
        self.assertEqual(vendor.description, 'Local produce and pantry goods.')
        self.assertEqual(int(self.client.session['_auth_user_id']), vendor.user_id)

    def test_vendor_registration_page_renders_styled_shop_form(self):
        response = self.client.get(reverse('vendor_register'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'vendor-auth-layout')
        self.assertContains(response, 'Create your vendor account')
        self.assertContains(response, 'name="shop_name"')
        self.assertContains(response, 'Log in')
        self.assertContains(
            response,
            f'href="{reverse("register")}">Customer signup</a>',
        )
        self.assertContains(
            response,
            f'href="{reverse("vendor_register")}" aria-current="page">Vendor signup</a>',
        )

    def test_vendor_login_rejects_customer_account(self):
        customer_user = User.objects.create_user(
            username='customer-only',
            password='Safe-vendor-password-927!',
        )

        response = self.client.post(
            reverse('vendor_login'),
            {
                'username': customer_user.username,
                'password': 'Safe-vendor-password-927!',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['user'].is_authenticated)
        self.assertNotContains(response, 'Local shops')
        self.assertNotContains(response, 'Shop products')
        self.assertNotContains(response, 'My Cart')

    def test_vendor_can_log_in_and_view_shop_details(self):
        response = self.client.post(
            reverse('vendor_register'),
            self.vendor_registration_data(),
        )
        self.assertRedirects(response, reverse('vendor_dashboard'))
        self.client.post(reverse('logout'))

        response = self.client.post(
            reverse('vendor_login'),
            {
                'username': 'newvendor',
                'password': 'Safe-vendor-password-927!',
            },
        )

        self.assertRedirects(response, reverse('vendor_dashboard'))
        dashboard = self.client.get(reverse('vendor_dashboard'))
        self.assertContains(dashboard, 'Green Grocer')
        self.assertContains(dashboard, '12 Market Road')
        self.assertContains(dashboard, 'LIC-2026-123')
        self.assertContains(dashboard, 'Local produce and pantry goods.')
        self.assertNotContains(dashboard, 'Local shops')
        self.assertNotContains(dashboard, 'Shop products')
        self.assertNotContains(dashboard, 'My Cart')
        self.assertNotContains(dashboard, 'Marketplace')

    def test_vendor_dashboard_lists_vegetables_and_availability(self):
        category = Category.objects.create(name='Leafy Greens')
        vendor = Vendor.objects.create(
            user=User.objects.create_user(
                username='garden-vendor',
                password='Safe-vendor-password-927!',
            ),
            name='Garden Vendor',
            shop_name='Garden Shop',
        )
        Product.objects.create(
            name='Spinach',
            price='3.25',
            stock=7,
            units='bunch',
            category=category,
            vendor=vendor,
        )
        Product.objects.create(
            name='Kale',
            price='4.00',
            stock=0,
            units='bunch',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor.user)

        response = self.client.get(reverse('vendor_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My Vegetables')
        self.assertContains(response, 'Garden Vendor')
        self.assertContains(response, 'Leafy Greens')
        self.assertContains(response, '3.25')
        self.assertContains(response, '4.00')
        self.assertContains(response, 'bunch')
        self.assertContains(response, 'Available')
        self.assertContains(response, 'Out of stock')
        self.assertContains(response, 'Edit')
        self.assertContains(response, 'Delete')

    def test_vendor_dashboard_shows_empty_state_without_products(self):
        vendor = Vendor.objects.create(
            user=User.objects.create_user(
                username='empty-vendor',
                password='Safe-vendor-password-927!',
            ),
            name='Empty Vendor',
            shop_name='Empty Shop',
        )
        self.client.force_login(vendor.user)

        response = self.client.get(reverse('vendor_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You have not listed any vegetables yet.')

    def test_vendor_navigation_links_to_dashboard_products_and_profile(self):
        vendor = Vendor.objects.create(
            user=User.objects.create_user(
                username='navigation-vendor',
                password='Safe-vendor-password-927!',
            ),
            name='Navigation Vendor',
        )
        self.client.force_login(vendor.user)

        dashboard = self.client.get(reverse('vendor_dashboard'))
        orders = self.client.get(reverse('vendor_order_list'))

        self.assertContains(
            dashboard,
            f'href="{reverse("vendor_dashboard")}#products">Products</a>',
        )
        self.assertContains(
            dashboard,
            f'href="{reverse("vendor_dashboard")}#shop-profile">Shop profile</a>',
        )
        self.assertContains(dashboard, 'id="products"')
        self.assertContains(dashboard, 'id="shop-profile"')
        self.assertContains(
            orders,
            f'href="{reverse("vendor_dashboard")}#products">Products</a>',
        )
        self.assertContains(
            orders,
            f'href="{reverse("vendor_dashboard")}#shop-profile">Shop profile</a>',
        )

    def test_vendor_dashboard_metrics_use_only_own_products_and_delivered_orders(self):
        category = Category.objects.create(name='Dashboard produce')
        vendor_user = User.objects.create_user(
            username='metrics-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            name='Metrics Vendor',
            shop_name='Metrics Shop',
        )
        other_vendor = Vendor.objects.create(name='Metrics Other Vendor')
        available = Product.objects.create(
            name='Available item',
            price='2.00',
            stock=8,
            units='kg',
            category=category,
            vendor=vendor,
        )
        low_stock = Product.objects.create(
            name='Low stock item',
            price='3.00',
            stock=4,
            units='bunch',
            category=category,
            vendor=vendor,
        )
        at_threshold = Product.objects.create(
            name='Threshold item',
            price='1.00',
            stock=5,
            units='each',
            category=category,
            vendor=vendor,
        )
        Product.objects.create(
            name='Out item',
            price='4.00',
            stock=0,
            units='kg',
            category=category,
            vendor=vendor,
        )
        Product.objects.create(
            name='Other shop item',
            price='99.00',
            stock=9,
            units='kg',
            category=category,
            vendor=other_vendor,
        )
        delivered_order = Order.objects.create(
            customer=Customer.objects.create(
                user=User.objects.create_user(username='metrics-customer'),
                address='Delivery Lane',
                phone='555-0190',
            ),
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.PENDING,
        )
        delivered_vendor_order = VendorOrder.objects.create(
            order=delivered_order,
            vendor=vendor,
            status=VendorOrder.Status.DELIVERED,
        )
        OrderItem.objects.create(
            order=delivered_order,
            vendor_order=delivered_vendor_order,
            product=available,
            quantity=2,
            unit_price=Decimal('2.00'),
        )
        OrderItem.objects.create(
            order=delivered_order,
            vendor_order=delivered_vendor_order,
            product=low_stock,
            quantity=1,
            unit_price=Decimal('3.00'),
        )
        other_order = Order.objects.create(customer=delivered_order.customer)
        VendorOrder.objects.create(
            order=other_order,
            vendor=vendor,
            status=VendorOrder.Status.PENDING,
        )
        foreign_vendor_order = VendorOrder.objects.create(
            order=delivered_order,
            vendor=other_vendor,
        )
        OrderItem.objects.create(
            order=delivered_order,
            vendor_order=foreign_vendor_order,
            product=Product.objects.get(vendor=other_vendor),
            quantity=1,
            unit_price=Decimal('99.00'),
        )
        self.client.force_login(vendor_user)

        response = self.client.get(reverse('vendor_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['product_stats'], {
            'total': 4,
            'out_of_stock': 1,
            'low_stock': 2,
        })
        self.assertEqual(response.context['order_stats']['total'], 2)
        self.assertEqual(response.context['order_stats']['pending'], 1)
        self.assertEqual(response.context['order_stats']['delivered'], 1)
        self.assertEqual(response.context['sales_stats']['sales'], Decimal('7.00'))
        self.assertEqual(response.context['sales_stats']['products_sold'], 3)
        self.assertContains(response, 'Delivered sales')
        self.assertContains(response, '₹7.00')
        self.assertContains(response, f'Order #{delivered_order.id}')
        self.assertNotContains(response, 'Other shop item')
        self.assertNotContains(response, '99.00')
        self.assertNotEqual(at_threshold.stock, low_stock.stock)

    def test_vendor_dashboard_requires_vendor_login_and_profile(self):
        anonymous = self.client.get(reverse('vendor_dashboard'))
        self.assertRedirects(
            anonymous,
            f"{reverse('vendor_login')}?next={reverse('vendor_dashboard')}",
        )

        customer_user = User.objects.create_user(
            username='dashboard-customer',
            password='Safe-vendor-password-927!',
        )
        self.client.force_login(customer_user)
        self.assertEqual(
            self.client.get(reverse('vendor_dashboard')).status_code,
            404,
        )

    def test_vendor_can_edit_own_vegetable_and_return_to_dashboard(self):
        vendor_user = User.objects.create_user(
            username='edit-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            name='Edit Vendor',
            shop_name='Edit Shop',
        )
        other_vendor = Vendor.objects.create(name='Other Edit Vendor')
        category = Category.objects.create(name='Vegetables')
        other_category = Category.objects.create(name='Organic Vegetables')
        product = Product.objects.create(
            name='Old Name',
            price='1.50',
            stock=3,
            units='kg',
            description='Old description',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('edit_vegetable', args=[product.id]),
            {
                'name': 'Updated Name',
                'category': other_category.id,
                'price': '2.75',
                'units': 'bunch',
                'stock': '0',
                'description': 'Updated description',
                'vendor': other_vendor.id,
            },
            follow=True,
        )

        product.refresh_from_db()
        self.assertRedirects(response, reverse('vendor_dashboard'))
        self.assertEqual(product.name, 'Updated Name')
        self.assertEqual(product.category, other_category)
        self.assertEqual(product.price, Decimal('2.75'))
        self.assertEqual(product.units, 'bunch')
        self.assertEqual(product.stock, 0)
        self.assertEqual(product.description, 'Updated description')
        self.assertEqual(product.vendor, vendor)
        self.assertContains(response, 'Updated Name')
        self.assertContains(response, 'Out of stock')

    def test_vendor_cannot_access_another_vendors_edit_page(self):
        vendor_a_user = User.objects.create_user(
            username='vendor-a',
            password='Safe-vendor-password-927!',
        )
        vendor_b = Vendor.objects.create(name='Vendor B')
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Vendor B Carrots',
            price='2.00',
            stock=4,
            units='kg',
            category=category,
            vendor=vendor_b,
        )
        self.client.force_login(vendor_a_user)

        response = self.client.get(reverse('edit_vegetable', args=[product.id]))

        self.assertEqual(response.status_code, 404)
        product.refresh_from_db()
        self.assertEqual(product.name, 'Vendor B Carrots')
        self.assertEqual(product.vendor, vendor_b)

    def test_vendor_cannot_edit_own_product_to_a_non_positive_price(self):
        vendor_user = User.objects.create_user(
            username='price-edit-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            name='Price Edit Vendor',
        )
        category = Category.objects.create(name='Price Edit Produce')
        product = Product.objects.create(
            name='Price-tested item',
            price='2.00',
            stock=3,
            units='kg',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('edit_vegetable', args=[product.id]),
            {
                'name': product.name,
                'category': category.id,
                'price': '-1.00',
                'units': product.units,
                'stock': '3',
                'description': '',
            },
        )

        product.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(product.price, Decimal('2.00'))

    def test_vendor_can_update_own_stock_and_availability(self):
        vendor_user = User.objects.create_user(
            username='stock-update-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            name='Stock Update Vendor',
        )
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Courgettes',
            price='2.00',
            stock=5,
            units='kg',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('update_vegetable_stock', args=[product.id]),
            {'stock': '8'},
        )

        self.assertRedirects(response, reverse('vendor_dashboard'))
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertContains(self.client.get(reverse('vendor_dashboard')), 'Available')

        response = self.client.post(
            reverse('update_vegetable_stock', args=[product.id]),
            {'stock': '0'},
            follow=True,
        )

        product.refresh_from_db()
        self.assertEqual(response.redirect_chain, [(reverse('vendor_dashboard'), 302)])
        self.assertEqual(product.stock, 0)
        self.assertContains(response, 'Out of stock')

    def test_vendor_cannot_update_stock_for_another_vendors_product(self):
        vendor_user = User.objects.create_user(
            username='stock-update-other-vendor',
            password='Safe-vendor-password-927!',
        )
        other_vendor = Vendor.objects.create(name='Stock Owner')
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Other Vendor Lettuce',
            price='1.50',
            stock=5,
            units='head',
            category=category,
            vendor=other_vendor,
        )
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('update_vegetable_stock', args=[product.id]),
            {'stock': '0'},
        )

        self.assertEqual(response.status_code, 404)
        product.refresh_from_db()
        self.assertEqual(product.stock, 5)

    def test_stock_update_rejects_negative_values(self):
        vendor_user = User.objects.create_user(
            username='negative-stock-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            name='Negative Stock Vendor',
        )
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Peppers',
            price='2.00',
            stock=5,
            units='kg',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('update_vegetable_stock', args=[product.id]),
            {'stock': '-3'},
        )

        self.assertRedirects(response, reverse('vendor_dashboard'))
        product.refresh_from_db()
        self.assertEqual(product.stock, 5)

    def test_stock_update_requires_post(self):
        vendor_user = User.objects.create_user(
            username='stock-method-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(user=vendor_user, name='Method Vendor')
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Onions',
            price='1.00',
            stock=5,
            units='kg',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor_user)

        response = self.client.get(
            reverse('update_vegetable_stock', args=[product.id]),
        )

        self.assertEqual(response.status_code, 405)
        product.refresh_from_db()
        self.assertEqual(product.stock, 5)

    def test_edit_vegetable_requires_vendor_login(self):
        category = Category.objects.create(name='Vegetables')
        vendor = Vendor.objects.create(name='Login Vendor')
        product = Product.objects.create(
            name='Beets',
            price='1.25',
            stock=2,
            units='bunch',
            category=category,
            vendor=vendor,
        )

        response = self.client.get(reverse('edit_vegetable', args=[product.id]))

        self.assertRedirects(
            response,
            f"{reverse('vendor_login')}?next={reverse('edit_vegetable', args=[product.id])}",
        )

    def test_vendor_can_confirm_deletion_of_own_never_ordered_vegetable(self):
        vendor_user = User.objects.create_user(
            username='delete-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(user=vendor_user, name='Delete Vendor')
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Turnips',
            price='2.00',
            stock=4,
            units='kg',
            category=category,
            vendor=vendor,
        )
        self.client.force_login(vendor_user)

        confirmation = self.client.get(reverse('delete_vegetable', args=[product.id]))
        self.assertEqual(confirmation.status_code, 200)
        self.assertTemplateUsed(confirmation, 'market/delete_vegetable.html')
        self.assertContains(confirmation, 'Are you sure')
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

        response = self.client.post(
            reverse('delete_vegetable', args=[product.id]),
            follow=True,
        )

        self.assertRedirects(response, reverse('vendor_dashboard'))
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_vendor_cannot_delete_vegetable_owned_by_another_vendor(self):
        vendor_a_user = User.objects.create_user(
            username='delete-vendor-a',
            password='Safe-vendor-password-927!',
        )
        vendor_b = Vendor.objects.create(name='Delete Vendor B')
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Other Vendor Turnips',
            price='2.00',
            stock=4,
            units='kg',
            category=category,
            vendor=vendor_b,
        )
        self.client.force_login(vendor_a_user)

        response = self.client.get(reverse('delete_vegetable', args=[product.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_vendor_cannot_delete_vegetable_that_appears_in_an_order(self):
        vendor_user = User.objects.create_user(
            username='ordered-delete-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(user=vendor_user, name='Ordered Vendor')
        category = Category.objects.create(name='Vegetables')
        product = Product.objects.create(
            name='Ordered Carrots',
            price='2.00',
            stock=4,
            units='kg',
            category=category,
            vendor=vendor,
        )
        customer_user = User.objects.create_user(
            username='delete-test-customer',
            password='safe-password',
        )
        customer = Customer.objects.create(
            user=customer_user,
            address='3 Market Street',
            phone='555-0200',
        )
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=1,
            unit_price=product.price,
        )
        self.client.force_login(vendor_user)

        confirmation = self.client.get(reverse('delete_vegetable', args=[product.id]))
        response = self.client.post(reverse('delete_vegetable', args=[product.id]))

        self.assertRedirects(confirmation, reverse('vendor_dashboard'))
        self.assertRedirects(response, reverse('vendor_dashboard'))
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertTrue(OrderItem.objects.filter(order=order, product=product).exists())
        dashboard = self.client.get(reverse('vendor_dashboard'))
        self.assertContains(dashboard, 'Cannot delete: in order history')

    def test_vendor_adds_vegetable_to_own_shop(self):
        vendor_user = User.objects.create_user(
            username='product-vendor',
            password='Safe-vendor-password-927!',
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            name='Product Vendor',
            shop_name='Fresh Shop',
        )
        other_vendor = Vendor.objects.create(name='Other Vendor')
        category = Category.objects.create(name='Root Vegetables')
        image_bytes = BytesIO()
        Image.new('RGB', (1, 1), color='green').save(image_bytes, format='PNG')
        image = SimpleUploadedFile(
            'carrots.png',
            image_bytes.getvalue(),
            content_type='image/png',
        )
        self.client.force_login(vendor_user)

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = self.client.post(
                    reverse('add_vegetable'),
                    {
                        'name': 'Carrots',
                        'category': category.id,
                        'price': '2.25',
                        'units': 'kg',
                        'stock': '10',
                        'description': 'Fresh local carrots.',
                        'image': image,
                    },
                )

                product = Product.objects.get(name='Carrots')
                self.assertRedirects(response, reverse('vendor_dashboard'))
                self.assertEqual(product.vendor, vendor)
                self.assertNotEqual(product.vendor, other_vendor)
                self.assertEqual(product.category, category)
                self.assertEqual(product.stock, 10)
                self.assertEqual(product.description, 'Fresh local carrots.')
                self.assertTrue(product.image.name.endswith('carrots.png'))
                dashboard = self.client.get(reverse('vendor_dashboard'))
                self.assertContains(dashboard, 'Carrots')
                self.assertContains(dashboard, 'Available')

    def test_non_vendor_cannot_add_vegetable(self):
        customer_user = User.objects.create_user(
            username='regular-customer',
            password='Safe-customer-password-927!',
        )
        self.client.force_login(customer_user)

        response = self.client.get(reverse('add_vegetable'))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Product.objects.exists())

    def test_add_vegetable_rejects_negative_stock(self):
        vendor_user = User.objects.create_user(
            username='stock-vendor',
            password='Safe-vendor-password-927!',
        )
        Vendor.objects.create(user=vendor_user, name='Stock Vendor')
        category = Category.objects.create(name='Vegetables')
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('add_vegetable'),
            {
                'name': 'Potatoes',
                'category': category.id,
                'price': '1.50',
                'units': 'kg',
                'stock': '-1',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Potatoes').exists())

    def test_add_vegetable_rejects_non_positive_price(self):
        vendor_user = User.objects.create_user(
            username='invalid-price-vendor',
            password='Safe-vendor-password-927!',
        )
        Vendor.objects.create(user=vendor_user, name='Invalid Price Vendor')
        category = Category.objects.create(name='Price validation')
        self.client.force_login(vendor_user)

        response = self.client.post(
            reverse('add_vegetable'),
            {
                'name': 'Invalid price item',
                'category': category.id,
                'price': '0.00',
                'units': 'kg',
                'stock': '5',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Invalid price item').exists())


class MarketplaceAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='market-admin',
            email='admin@example.test',
            password='Safe-admin-password-927!',
        )
        self.customer_user = User.objects.create_user(
            username='admin-customer',
            email='customer@example.test',
            password='Safe-customer-password-927!',
            first_name='Market',
            last_name='Customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            address='8 Admin Avenue',
            phone='555-0110',
        )
        self.vendor_user = User.objects.create_user(
            username='admin-vendor',
            email='vendor@example.test',
            password='Safe-vendor-password-927!',
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            name='Admin Test Vendor',
            shop_name='Admin Test Shop',
            email='vendor@example.test',
            phone='555-0111',
        )
        self.category = Category.objects.create(name='Admin Produce')
        self.product = Product.objects.create(
            name='Admin Apples',
            price='2.50',
            stock=4,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            status='pending',
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.PENDING,
        )
        self.vendor_order = VendorOrder.objects.create(
            order=self.order,
            vendor=self.vendor,
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            vendor_order=self.vendor_order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('2.50'),
        )

    def test_admin_portal_requires_an_active_staff_account(self):
        dashboard_url = reverse('market_admin_dashboard')
        anonymous_response = self.client.get(dashboard_url)
        self.assertRedirects(
            anonymous_response,
            f"{reverse('admin:login')}?next={dashboard_url}",
        )

        for user in (self.customer_user, self.vendor_user):
            self.client.force_login(user)
            response = self.client.get(dashboard_url)
            self.assertRedirects(
                response,
                f"{reverse('admin:login')}?next={dashboard_url}",
            )

    def test_staff_without_required_marketplace_permissions_is_forbidden(self):
        staff_user = User.objects.create_user(
            username='limited-staff',
            password='Safe-staff-password-927!',
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(reverse('market_admin_dashboard'))

        self.assertEqual(response.status_code, 403)

    def test_superuser_dashboard_uses_real_marketplace_metrics(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('market_admin_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['customer_count'], 1)
        self.assertEqual(response.context['active_customer_count'], 1)
        self.assertEqual(response.context['vendor_count'], 1)
        self.assertEqual(response.context['active_vendor_count'], 1)
        self.assertEqual(response.context['vendors_with_products'], 1)
        self.assertEqual(response.context['product_count'], 1)
        self.assertEqual(response.context['available_product_count'], 1)
        self.assertEqual(response.context['low_stock_count'], 1)
        self.assertEqual(response.context['order_count'], 1)
        self.assertEqual(response.context['marketplace_order_value'], Decimal('5.00'))
        self.assertContains(response, 'Pending')
        self.assertContains(response, f'#{self.order.id}')

    def test_customer_search_and_pagination_are_server_side(self):
        for index in range(26):
            user = User.objects.create_user(
                username=f'page-customer-{index:02d}',
                password=None,
            )
            Customer.objects.create(
                user=user,
                address='Paged Address',
                phone=f'555-{index:04d}',
            )
        self.client.force_login(self.admin_user)

        first_page = self.client.get(reverse('market_admin_customer_list'))
        matching = self.client.get(
            reverse('market_admin_customer_list'),
            {'q': 'admin-customer'},
        )

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context['page_obj'].paginator.count, 27)
        self.assertTrue(first_page.context['next_page_url'])
        self.assertContains(first_page, 'Next')
        self.assertEqual(matching.context['page_obj'].paginator.count, 1)
        self.assertContains(matching, 'Market Customer')

    def test_vendor_list_and_detail_show_related_marketplace_activity(self):
        self.client.force_login(self.admin_user)

        listing = self.client.get(reverse('market_admin_vendor_list'))
        detail = self.client.get(
            reverse('market_admin_vendor_detail', args=[self.vendor.id]),
        )

        self.assertContains(listing, 'Admin Test Shop')
        self.assertContains(listing, '1')
        self.assertContains(detail, 'Admin Apples')
        self.assertContains(detail, 'Admin Test Shop')
        self.assertContains(detail, 'Recent vendor orders')
        self.assertContains(detail, f'#{self.order.id}')

    def test_product_filters_and_category_counts_use_database_records(self):
        unavailable = Product.objects.create(
            name='Unavailable Admin Produce',
            price='4.00',
            stock=0,
            units='each',
            category=self.category,
            vendor=self.vendor,
        )
        self.client.force_login(self.admin_user)

        out_of_stock = self.client.get(
            reverse('market_admin_product_list'),
            {'availability': 'out_of_stock'},
        )
        categories = self.client.get(reverse('market_admin_category_list'))

        self.assertEqual(out_of_stock.context['page_obj'].paginator.count, 1)
        self.assertContains(out_of_stock, unavailable.name)
        self.assertNotContains(out_of_stock, self.product.name)
        self.assertContains(categories, 'Admin Produce')
        self.assertContains(categories, '>2<')
        self.assertContains(categories, 'Add category')

    def test_order_list_filters_and_detail_use_historical_values(self):
        self.product.price = Decimal('99.00')
        self.product.save(update_fields=['price'])
        self.client.force_login(self.admin_user)

        order_list = self.client.get(
            reverse('market_admin_order_list'),
            {
                'status': 'pending',
                'payment_status': Order.PaymentStatus.PENDING,
                'payment_method': Order.PaymentMethod.CASH_ON_DELIVERY,
            },
        )
        detail = self.client.get(
            reverse('market_admin_order_detail', args=[self.order.id]),
        )

        self.assertEqual(order_list.context['page_obj'].paginator.count, 1)
        self.assertContains(order_list, 'Cash on Delivery')
        self.assertContains(detail, 'Admin Apples')
        self.assertContains(detail, 'Admin Test Vendor')
        self.assertContains(detail, '₹2.50')
        self.assertContains(detail, '₹5.00')
        self.assertNotContains(detail, '₹99.00')
        self.assertContains(detail, 'COD')

    def test_order_detail_remains_readable_after_product_deletion(self):
        self.product.delete()
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse('market_admin_order_detail', args=[self.order.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Admin Apples')
        self.assertContains(response, 'listing deleted')
        self.assertContains(response, 'kg')
        self.assertContains(response, '₹5.00')

    def test_product_admin_form_rejects_invalid_price_and_stock(self):
        form = MarketplaceProductAdminForm(data={
            'name': 'Invalid product',
            'description': '',
            'price': '0.00',
            'stock': '-1',
            'units': 'kg',
            'category': self.category.id,
            'vendor': self.vendor.id,
            'image': '',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('price', form.errors)
        self.assertIn('stock', form.errors)

    def test_category_delete_is_disabled_in_django_admin(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse('admin:market_category_delete', args=[self.category.id]),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Category.objects.filter(id=self.category.id).exists())

    def test_product_admin_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin_user)

        response = csrf_client.post(
            reverse('admin:market_product_change', args=[self.product.id]),
            {'name': 'Changed without CSRF'},
        )

        self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Admin Apples')


class OrderLifecycleNotificationTests(TestCase):
    def setUp(self):
        self.customer_user = User.objects.create_user(
            username='lifecycle-customer',
            email='lifecycle-customer@example.test',
            password='Safe-customer-password-927!',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            address='Lifecycle Street',
            phone='555-0191',
        )
        self.vendor_user = User.objects.create_user(
            username='lifecycle-vendor',
            email='lifecycle-vendor@example.test',
            password='Safe-vendor-password-927!',
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            name='Lifecycle Vendor',
            shop_name='Lifecycle Shop',
        )
        self.other_vendor_user = User.objects.create_user(
            username='other-lifecycle-vendor',
            password='Safe-vendor-password-927!',
        )
        self.other_vendor = Vendor.objects.create(
            user=self.other_vendor_user,
            name='Other Lifecycle Vendor',
        )
        self.admin_user = User.objects.create_superuser(
            username='lifecycle-admin',
            email='lifecycle-admin@example.test',
            password='Safe-admin-password-927!',
        )
        self.category = Category.objects.create(name='Lifecycle Produce')
        self.product = Product.objects.create(
            name='Lifecycle Pears',
            price='3.00',
            stock=8,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )
        self.other_product = Product.objects.create(
            name='Lifecycle Plums',
            price='4.00',
            stock=8,
            units='kg',
            category=self.category,
            vendor=self.other_vendor,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.PENDING,
        )
        self.vendor_order = VendorOrder.objects.create(
            order=self.order,
            vendor=self.vendor,
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            vendor_order=self.vendor_order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('3.00'),
        )

    def add_other_vendor_fulfillment(self):
        vendor_order = VendorOrder.objects.create(
            order=self.order,
            vendor=self.other_vendor,
        )
        OrderItem.objects.create(
            order=self.order,
            vendor_order=vendor_order,
            product=self.other_product,
            quantity=1,
            unit_price=Decimal('4.00'),
        )
        return vendor_order

    def test_order_placement_and_vendor_notifications_are_idempotent(self):
        from market.order_lifecycle import notify_vendors_new_order, record_order_placed

        record_order_placed(self.order, actor=self.customer_user)
        record_order_placed(self.order, actor=self.customer_user)
        notify_vendors_new_order(self.order)
        notify_vendors_new_order(self.order)

        self.assertEqual(
            Notification.objects.filter(
                recipient=self.customer_user,
                kind=Notification.Kind.ORDER_PLACED,
                order=self.order,
            ).count(),
            1,
        )
        vendor_notice = Notification.objects.get(
            recipient=self.vendor_user,
            kind=Notification.Kind.VENDOR_ORDER,
            order=self.order,
        )
        self.assertEqual(vendor_notice.message, f'New order #{self.order.id} received.')
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.admin_user,
                kind=Notification.Kind.ADMIN_ORDER,
                order=self.order,
            ).count(),
            1,
        )
        self.client.force_login(self.admin_user)
        admin_notifications = self.client.get(reverse('notification_center'))
        self.assertContains(admin_notifications, f'New order #{self.order.id} was placed.')
        self.assertNotContains(admin_notifications, 'Your order has been placed successfully.')
        self.assertEqual(
            OrderStatusHistory.objects.filter(
                order=self.order,
                vendor_order__isnull=True,
            ).count(),
            1,
        )

    def test_customer_status_advances_only_when_all_vendors_reach_stage(self):
        from market.order_lifecycle import record_order_placed, transition_vendor_order

        other_vendor_order = self.add_other_vendor_fulfillment()
        record_order_placed(self.order, actor=self.customer_user)

        _, first_changed = transition_vendor_order(
            self.vendor_order.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.vendor_user,
        )
        self.order.refresh_from_db()
        self.assertTrue(first_changed)
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertFalse(Notification.objects.filter(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_STATUS,
            message__contains='confirmed',
        ).exists())

        _, second_changed = transition_vendor_order(
            other_vendor_order.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.other_vendor_user,
        )
        self.order.refresh_from_db()
        self.assertTrue(second_changed)
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.customer_user,
                kind=Notification.Kind.ORDER_STATUS,
                message=f'Your order #{self.order.id} has been confirmed.',
            ).count(),
            1,
        )

        _, repeated_change = transition_vendor_order(
            other_vendor_order.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.other_vendor_user,
        )
        self.assertFalse(repeated_change)
        self.assertEqual(
            OrderStatusHistory.objects.filter(
                order=self.order,
                vendor_order=other_vendor_order,
                new_status=VendorOrder.Status.CONFIRMED,
            ).count(),
            1,
        )

    def test_accept_endpoint_cannot_be_replayed_to_skip_a_stage(self):
        self.client.force_login(self.vendor_user)

        accepted = self.client.post(
            reverse('accept_vendor_order', args=[self.vendor_order.id]),
        )
        replayed = self.client.post(
            reverse('accept_vendor_order', args=[self.vendor_order.id]),
        )

        self.assertRedirects(accepted, reverse('vendor_order_detail', args=[self.order.id]))
        self.assertRedirects(replayed, reverse('vendor_order_detail', args=[self.order.id]))
        self.vendor_order.refresh_from_db()
        self.assertEqual(self.vendor_order.status, VendorOrder.Status.CONFIRMED)
        self.assertTrue(Notification.objects.filter(
            recipient=self.customer_user,
            message=f'Your order #{self.order.id} has been confirmed.',
        ).exists())

    def test_rejection_notifies_customer_for_only_the_rejected_vendor(self):
        from market.order_lifecycle import transition_vendor_order

        other_vendor_order = self.add_other_vendor_fulfillment()
        _, changed = transition_vendor_order(
            self.vendor_order.id,
            VendorOrder.Status.REJECTED,
            actor=self.vendor_user,
        )

        self.assertTrue(changed)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        rejection = Notification.objects.get(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_REJECTED,
            vendor_order=self.vendor_order,
        )
        self.assertIn('Some items from Lifecycle Shop were unavailable', rejection.message)
        self.assertFalse(Notification.objects.filter(
            recipient=self.other_vendor_user,
            order=self.order,
            kind=Notification.Kind.ORDER_REJECTED,
        ).exists())
        self.assertEqual(other_vendor_order.status, VendorOrder.Status.PENDING)

    def test_failed_payment_notification_is_deduplicated(self):
        from market.order_lifecycle import record_payment_failure

        self.order.payment_method = Order.PaymentMethod.ONLINE
        self.order.save(update_fields=['payment_method'])
        record_payment_failure(self.order, 'pay_test123')
        record_payment_failure(self.order, 'pay_test123')

        customer_notice = Notification.objects.get(
            recipient=self.customer_user,
            kind=Notification.Kind.PAYMENT_FAILED,
            order=self.order,
        )
        self.assertIn('Payment for order #', customer_notice.message)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.admin_user,
                kind=Notification.Kind.PAYMENT_FAILED,
                order=self.order,
            ).count(),
            1,
        )
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PENDING)

    def test_customer_sees_only_own_notifications_and_cannot_read_another_users(self):
        other_user = User.objects.create_user(
            username='private-notification-user',
            password='Safe-customer-password-927!',
        )
        own_notice = Notification.objects.create(
            recipient=self.customer_user,
            order=self.order,
            kind=Notification.Kind.ORDER_PLACED,
            title='Your order',
            message='Private customer notice',
            dedupe_key='own-customer-notice',
        )
        other_notice = Notification.objects.create(
            recipient=other_user,
            order=self.order,
            kind=Notification.Kind.ORDER_PLACED,
            title='Other order',
            message='Other customer private notice',
            dedupe_key='other-customer-notice',
        )
        self.client.force_login(self.customer_user)

        response = self.client.get(reverse('notification_center'))
        forbidden_read = self.client.post(
            reverse('mark_notification_read', args=[other_notice.id]),
        )
        read_own = self.client.post(
            reverse('mark_notification_read', args=[own_notice.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Private customer notice')
        self.assertNotContains(response, 'Other customer private notice')
        self.assertContains(response, 'Open order')
        self.assertEqual(forbidden_read.status_code, 404)
        self.assertRedirects(read_own, reverse('notification_center'))
        own_notice.refresh_from_db()
        other_notice.refresh_from_db()
        self.assertIsNotNone(own_notice.read_at)
        self.assertIsNone(other_notice.read_at)

    def test_vendor_notification_center_is_recipient_scoped(self):
        own_notice = Notification.objects.create(
            recipient=self.vendor_user,
            vendor_order=self.vendor_order,
            order=self.order,
            kind=Notification.Kind.VENDOR_ORDER,
            title='Own vendor order',
            message='Only this vendor can read this.',
            dedupe_key='vendor-own-order',
        )
        other_notice = Notification.objects.create(
            recipient=self.other_vendor_user,
            order=self.order,
            kind=Notification.Kind.VENDOR_ORDER,
            title='Other vendor order',
            message='Other vendor private notice.',
            dedupe_key='vendor-other-order',
        )
        self.client.force_login(self.vendor_user)

        response = self.client.get(reverse('notification_center'))
        forbidden_read = self.client.post(
            reverse('mark_notification_read', args=[other_notice.id]),
        )

        self.assertContains(response, 'Only this vendor can read this.')
        self.assertContains(response, 'Open order')
        self.assertNotContains(response, 'Other vendor private notice.')
        self.assertEqual(forbidden_read.status_code, 404)
        own_notice.refresh_from_db()
        other_notice.refresh_from_db()
        self.assertIsNone(own_notice.read_at)
        self.assertIsNone(other_notice.read_at)

    def test_customer_cannot_view_vendor_or_admin_notification_records(self):
        vendor_notice = Notification.objects.create(
            recipient=self.vendor_user,
            order=self.order,
            kind=Notification.Kind.VENDOR_ORDER,
            title='Vendor only',
            message='Vendor private update.',
            dedupe_key='vendor-only-record',
        )
        admin_notice = Notification.objects.create(
            recipient=self.admin_user,
            order=self.order,
            kind=Notification.Kind.ADMIN_ORDER,
            title='Admin only',
            message='Admin private update.',
            dedupe_key='admin-only-record',
        )
        self.client.force_login(self.customer_user)

        response = self.client.get(reverse('notification_center'))
        vendor_read = self.client.post(
            reverse('mark_notification_read', args=[vendor_notice.id]),
        )
        admin_read = self.client.post(
            reverse('mark_notification_read', args=[admin_notice.id]),
        )

        self.assertNotContains(response, 'Vendor private update.')
        self.assertNotContains(response, 'Admin private update.')
        self.assertEqual(vendor_read.status_code, 404)
        self.assertEqual(admin_read.status_code, 404)

    def test_mark_all_read_only_changes_the_signed_in_recipients(self):
        own_notice = Notification.objects.create(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_STATUS,
            title='Own status',
            message='Own message',
            dedupe_key='own-unread',
        )
        other_notice = Notification.objects.create(
            recipient=self.vendor_user,
            kind=Notification.Kind.ORDER_STATUS,
            title='Vendor status',
            message='Vendor message',
            dedupe_key='vendor-unread',
        )
        self.client.force_login(self.customer_user)

        response = self.client.post(reverse('mark_all_notifications_read'))

        self.assertRedirects(response, reverse('notification_center'))
        own_notice.refresh_from_db()
        other_notice.refresh_from_db()
        self.assertIsNotNone(own_notice.read_at)
        self.assertIsNone(other_notice.read_at)

    def test_notification_center_requires_a_marketplace_identity(self):
        anonymous = self.client.get(reverse('notification_center'))
        self.assertRedirects(
            anonymous,
            f"{reverse('login')}?next={reverse('notification_center')}",
        )
        unlinked_user = User.objects.create_user(
            username='unlinked-account',
            password='Safe-account-password-927!',
        )
        self.client.force_login(unlinked_user)
        response = self.client.get(reverse('notification_center'))
        self.assertEqual(response.status_code, 404)

    def test_notification_center_paginates_at_twenty_records(self):
        for index in range(21):
            Notification.objects.create(
                recipient=self.customer_user,
                kind=Notification.Kind.ORDER_STATUS,
                title=f'Update {index}',
                message=f'Lifecycle update {index}.',
                dedupe_key=f'pagination-{index}',
            )
        self.client.force_login(self.customer_user)

        first_page = self.client.get(reverse('notification_center'))
        second_page = self.client.get(reverse('notification_center'), {'page': '2'})

        self.assertEqual(first_page.context['page_obj'].paginator.count, 21)
        self.assertEqual(len(first_page.context['notifications']), 20)
        self.assertTrue(first_page.context['page_obj'].has_next())
        self.assertEqual(len(second_page.context['notifications']), 1)

    def test_order_detail_exposes_only_customer_facing_history(self):
        from market.order_lifecycle import record_order_placed, transition_vendor_order

        record_order_placed(self.order, actor=self.customer_user)
        transition_vendor_order(
            self.vendor_order.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.vendor_user,
        )
        self.client.force_login(self.customer_user)

        response = self.client.get(
            reverse('order_confirmation', args=[self.order.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['order'].status, Order.Status.CONFIRMED)
        self.assertEqual(len(response.context['status_history']), 2)
        self.assertTrue(all(
            history.vendor_order_id is None
            for history in response.context['status_history']
        ))
        self.assertContains(response, 'Order progress')
        self.assertContains(response, 'Current')

    def test_vendor_detail_exposes_only_its_fulfillment_history(self):
        from market.order_lifecycle import transition_vendor_order

        own_vendor_order, changed = transition_vendor_order(
            self.vendor_order.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.vendor_user,
        )
        self.assertTrue(changed)
        self.client.force_login(self.vendor_user)

        response = self.client.get(
            reverse('vendor_order_detail', args=[self.order.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['status_history']), 1)
        self.assertEqual(
            response.context['status_history'][0].vendor_order_id,
            own_vendor_order.id,
        )
        self.assertContains(response, 'Your fulfillment history')

    def test_admin_order_detail_includes_customer_vendor_history(self):
        from market.order_lifecycle import record_order_placed, transition_vendor_order

        record_order_placed(self.order, actor=self.customer_user)
        transition_vendor_order(
            self.vendor_order.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.vendor_user,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse('market_admin_order_detail', args=[self.order.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['status_history']), 3)
        self.assertContains(response, 'Status history')
        self.assertContains(response, 'Lifecycle Shop fulfillment')

    def test_cancelled_order_does_not_render_normal_delivery_progress(self):
        self.order.status = Order.Status.CANCELLED
        self.order.save(update_fields=['status'])
        self.client.force_login(self.customer_user)

        response = self.client.get(
            reverse('order_confirmation', args=[self.order.id]),
        )

        self.assertContains(response, 'Order ended: Cancelled')
        self.assertNotContains(response, '<ol class="order-progress"')

    def test_notification_mark_actions_require_post_and_csrf(self):
        notice = Notification.objects.create(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_STATUS,
            title='Unread',
            message='Still unread',
            dedupe_key='csrf-notification',
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.customer_user)

        read_response = csrf_client.post(
            reverse('mark_notification_read', args=[notice.id]),
        )
        all_response = csrf_client.post(reverse('mark_all_notifications_read'))

        self.assertEqual(read_response.status_code, 403)
        self.assertEqual(all_response.status_code, 403)
        notice.refresh_from_db()
        self.assertIsNone(notice.read_at)
        get_response = csrf_client.get(
            reverse('mark_notification_read', args=[notice.id]),
        )
        self.assertEqual(get_response.status_code, 405)


class PlatformFulfillmentTests(TestCase):
    def setUp(self):
        self.customer_user = User.objects.create_user(
            username='platform-customer',
            password='Safe-customer-password-927!',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            address='Platform Street',
            phone='555-0180',
        )
        self.vendor_user = User.objects.create_user(
            username='platform-test-vendor',
            password='Safe-vendor-password-927!',
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            name='Platform Test Vendor',
        )
        self.other_vendor_user = User.objects.create_user(
            username='other-platform-vendor',
            password='Safe-vendor-password-927!',
        )
        self.other_vendor = Vendor.objects.create(
            user=self.other_vendor_user,
            name='Other Platform Vendor',
        )
        self.admin_user = User.objects.create_superuser(
            username='platform-staff',
            email='platform-staff@example.test',
            password='Safe-admin-password-927!',
        )
        self.category = Category.objects.create(name='Platform Products')
        self.platform_product = Product.objects.create(
            name='Platform Carrots',
            price='2.00',
            stock=5,
            units='kg',
            category=self.category,
            vendor=None,
        )
        self.vendor_product = Product.objects.create(
            name='Vendor Potatoes',
            price='3.00',
            stock=5,
            units='kg',
            category=self.category,
            vendor=self.vendor,
        )

    def create_platform_order(self, quantity=2):
        order = Order.objects.create(
            customer=self.customer,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.PENDING,
        )
        platform_fulfillment = VendorOrder.objects.create(
            order=order,
            vendor=None,
        )
        OrderItem.objects.create(
            order=order,
            vendor_order=platform_fulfillment,
            product=self.platform_product,
            quantity=quantity,
            unit_price=self.platform_product.price,
        )
        return order, platform_fulfillment

    def admin_request(self):
        request = RequestFactory().post('/admin/market/vendororder/')
        request.user = self.admin_user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def run_admin_action(self, action_name, platform_fulfillment):
        vendor_order_admin = admin.site._registry[VendorOrder]
        getattr(vendor_order_admin, action_name)(
            self.admin_request(),
            VendorOrder.objects.filter(id=platform_fulfillment.id),
        )

    def test_staff_advances_platform_fulfillment_through_the_existing_lifecycle(self):
        from market.order_lifecycle import record_order_placed, transition_vendor_order

        order, platform_fulfillment = self.create_platform_order()
        record_order_placed(order, actor=self.customer_user)

        unauthorized, changed = transition_vendor_order(
            platform_fulfillment.id,
            VendorOrder.Status.CONFIRMED,
            actor=self.vendor_user,
        )
        self.assertFalse(changed)
        unauthorized.refresh_from_db()
        self.assertEqual(unauthorized.status, VendorOrder.Status.PENDING)

        self.run_admin_action('confirm_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('prepare_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('ready_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('mark_out_for_delivery', platform_fulfillment)
        self.run_admin_action('mark_delivered', platform_fulfillment)

        order.refresh_from_db()
        platform_fulfillment.refresh_from_db()
        self.assertEqual(platform_fulfillment.status, VendorOrder.Status.DELIVERED)
        self.assertIsNotNone(platform_fulfillment.delivered_at)
        self.assertEqual(order.status, Order.Status.DELIVERED)
        self.assertEqual(
            OrderStatusHistory.objects.filter(
                order=order,
                vendor_order=platform_fulfillment,
            ).count(),
            5,
        )
        self.assertEqual(
            OrderStatusHistory.objects.filter(
                order=order,
                vendor_order__isnull=True,
            ).count(),
            5,
        )
        self.assertTrue(Notification.objects.filter(
            recipient=self.customer_user,
            message=f'Your order #{order.id} has been delivered.',
        ).exists())

    def test_rejecting_platform_fulfillment_returns_stock_once_and_finishes_order(self):
        from market.order_lifecycle import record_order_placed

        order, platform_fulfillment = self.create_platform_order()
        self.platform_product.stock = 3
        self.platform_product.save(update_fields=['stock'])
        record_order_placed(order, actor=self.customer_user)

        self.run_admin_action('reject_platform_fulfillments', platform_fulfillment)

        order.refresh_from_db()
        platform_fulfillment.refresh_from_db()
        self.platform_product.refresh_from_db()
        self.assertEqual(platform_fulfillment.status, VendorOrder.Status.REJECTED)
        self.assertEqual(order.status, Order.Status.REJECTED)
        self.assertEqual(self.platform_product.stock, 5)
        self.assertTrue(Notification.objects.filter(
            recipient=self.customer_user,
            message=f'Your order #{order.id} was rejected.',
        ).exists())
        self.assertFalse(Notification.objects.filter(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_REJECTED,
            order=order,
        ).exists())
        history_count = OrderStatusHistory.objects.filter(order=order).count()
        notification_count = Notification.objects.filter(
            recipient=self.customer_user,
            order=order,
        ).count()

        self.run_admin_action('reject_platform_fulfillments', platform_fulfillment)
        self.platform_product.refresh_from_db()
        self.assertEqual(self.platform_product.stock, 5)
        self.assertEqual(
            OrderStatusHistory.objects.filter(order=order).count(),
            history_count,
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.customer_user,
                order=order,
            ).count(),
            notification_count,
        )

    def test_mixed_order_is_rejected_only_after_every_fulfillment_rejects(self):
        order, platform_fulfillment = self.create_platform_order()
        vendor_fulfillment = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
        )
        OrderItem.objects.create(
            order=order,
            vendor_order=vendor_fulfillment,
            product=self.vendor_product,
            quantity=1,
            unit_price=self.vendor_product.price,
        )
        self.platform_product.stock = 3
        self.platform_product.save(update_fields=['stock'])
        self.vendor_product.stock = 4
        self.vendor_product.save(update_fields=['stock'])
        self.client.force_login(self.vendor_user)

        vendor_rejection = self.client.post(
            reverse('reject_vendor_order', args=[vendor_fulfillment.id]),
        )

        order.refresh_from_db()
        vendor_fulfillment.refresh_from_db()
        self.assertRedirects(
            vendor_rejection,
            reverse('vendor_order_detail', args=[order.id]),
        )
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(vendor_fulfillment.status, VendorOrder.Status.REJECTED)

        self.run_admin_action('reject_platform_fulfillments', platform_fulfillment)

        order.refresh_from_db()
        platform_fulfillment.refresh_from_db()
        self.platform_product.refresh_from_db()
        self.vendor_product.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REJECTED)
        self.assertEqual(platform_fulfillment.status, VendorOrder.Status.REJECTED)
        self.assertEqual(self.platform_product.stock, 5)
        self.assertEqual(self.vendor_product.stock, 5)
        self.assertTrue(Notification.objects.filter(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_STATUS,
            message=f'Your order #{order.id} was rejected.',
        ).exists())

    def test_mixed_order_does_not_become_delivered_while_a_vendor_is_preparing(self):
        from market.order_lifecycle import record_order_placed, transition_vendor_order

        order, platform_fulfillment = self.create_platform_order()
        vendor_fulfillment = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
        )
        OrderItem.objects.create(
            order=order,
            vendor_order=vendor_fulfillment,
            product=self.vendor_product,
            quantity=1,
            unit_price=self.vendor_product.price,
        )
        record_order_placed(order, actor=self.customer_user)

        self.run_admin_action('confirm_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('prepare_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('ready_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('mark_out_for_delivery', platform_fulfillment)
        self.run_admin_action('mark_delivered', platform_fulfillment)
        for status in (
            VendorOrder.Status.CONFIRMED,
            VendorOrder.Status.PREPARING,
        ):
            vendor_fulfillment, changed = transition_vendor_order(
                vendor_fulfillment.id,
                status,
                actor=self.vendor_user,
            )
            self.assertTrue(changed)

        order.refresh_from_db()
        self.assertEqual(vendor_fulfillment.status, VendorOrder.Status.PREPARING)
        self.assertEqual(order.status, Order.Status.PREPARING)
        self.assertNotEqual(order.status, Order.Status.DELIVERED)

    def test_partial_rejection_remains_visible_until_other_fulfillment_completes(self):
        from market.order_lifecycle import record_order_placed, transition_vendor_order

        order, platform_fulfillment = self.create_platform_order()
        vendor_fulfillment = VendorOrder.objects.create(
            order=order,
            vendor=self.vendor,
        )
        OrderItem.objects.create(
            order=order,
            vendor_order=vendor_fulfillment,
            product=self.vendor_product,
            quantity=1,
            unit_price=self.vendor_product.price,
        )
        record_order_placed(order, actor=self.customer_user)
        vendor_fulfillment, rejected = transition_vendor_order(
            vendor_fulfillment.id,
            VendorOrder.Status.REJECTED,
            actor=self.vendor_user,
        )
        self.assertTrue(rejected)
        self.assertEqual(vendor_fulfillment.status, VendorOrder.Status.REJECTED)
        self.order_status_is(order, Order.Status.PENDING)

        self.run_admin_action('confirm_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('prepare_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('ready_platform_fulfillments', platform_fulfillment)
        self.run_admin_action('mark_out_for_delivery', platform_fulfillment)
        self.run_admin_action('mark_delivered', platform_fulfillment)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PARTIALLY_FULFILLED)
        self.assertTrue(Notification.objects.filter(
            recipient=self.customer_user,
            kind=Notification.Kind.ORDER_REJECTED,
            vendor_order=vendor_fulfillment,
        ).exists())
        self.client.force_login(self.customer_user)
        customer_detail = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        self.assertContains(
            customer_detail,
            'Some items were unavailable; the remaining items in this order were delivered.',
        )

    def test_second_platform_fulfillment_for_one_order_is_rejected_by_database(self):
        order, _ = self.create_platform_order()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VendorOrder.objects.create(order=order, vendor=None)

    def test_customer_and_admin_can_read_platform_item_history(self):
        from market.order_lifecycle import record_order_placed

        order, platform_fulfillment = self.create_platform_order()
        record_order_placed(order, actor=self.customer_user)
        self.run_admin_action('confirm_platform_fulfillments', platform_fulfillment)

        self.client.force_login(self.customer_user)
        customer_detail = self.client.get(
            reverse('order_confirmation', args=[order.id]),
        )
        self.assertContains(customer_detail, 'Sbji@Home platform fulfillment')
        self.assertContains(customer_detail, 'Platform Carrots')

        self.client.force_login(self.admin_user)
        admin_detail = self.client.get(
            reverse('market_admin_order_detail', args=[order.id]),
        )
        self.assertContains(admin_detail, 'Sbji@Home platform')
        self.assertContains(admin_detail, 'Status history')

    def order_status_is(self, order, expected_status):
        order.refresh_from_db()
        self.assertEqual(order.status, expected_status)
