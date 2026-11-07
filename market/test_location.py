from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .geo import DELIVERY_RADIUS_KM, haversine_km, parse_coordinates
from .models import Category, Customer, Product, Vendor


class GeoHelperTests(SimpleTestCase):
    def test_distance_is_roughly_correct(self):
        # ~1.1 km north-south for 0.01 degrees of latitude
        d = haversine_km(22.70, 75.85, 22.71, 75.85)
        self.assertAlmostEqual(d, 1.11, delta=0.05)

    def test_parse_coordinates_validation(self):
        self.assertEqual(parse_coordinates('22.7', '75.8'), (22.7, 75.8))
        self.assertIsNone(parse_coordinates('91', '0'))
        self.assertIsNone(parse_coordinates('abc', '0'))
        self.assertIsNone(parse_coordinates('nan', '0'))
        self.assertIsNone(parse_coordinates(None, None))

    def test_radius_setting(self):
        self.assertEqual(DELIVERY_RADIUS_KM, 2)


class LocationFeatureTests(TestCase):
    def setUp(self):
        cat = Category.objects.create(name='Vegetables')
        self.near = Vendor.objects.create(
            name='Near', shop_name='Near Shop', latitude=22.7100, longitude=75.8500)
        self.far = Vendor.objects.create(
            name='Far', shop_name='Far Shop', latitude=22.8000, longitude=75.8500)
        self.nowhere = Vendor.objects.create(name='Nowhere', shop_name='No Loc Shop')
        for v in (self.near, self.far, self.nowhere):
            Product.objects.create(name=f'Tomato {v.name}', price=10, stock=5,
                                   units='kg', category=cat, vendor=v)

    def _set_location(self, client, lat=22.7000, lng=75.8500):
        return client.post(reverse('save_location'),
                           {'latitude': lat, 'longitude': lng})

    def test_shops_sorted_closest_first_and_far_flagged_not_hidden(self):
        self._set_location(self.client)
        response = self.client.get(reverse('nearby_shops'))
        names = [s['vendor'].name for s in response.context['shops']]
        self.assertEqual(names, ['Near', 'Far', 'Nowhere'])
        shops = {s['vendor'].name: s for s in response.context['shops']}
        self.assertFalse(shops['Near']['far_away'])
        self.assertTrue(shops['Far']['far_away'])
        self.assertContains(response, 'Far away')

    def test_page_works_without_location(self):
        response = self.client.get(reverse('nearby_shops'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['shops']), 3)

    def test_shop_page_shows_only_that_shops_products(self):
        response = self.client.get(reverse('shop_detail', args=[self.near.id]))
        self.assertContains(response, 'Tomato Near')
        self.assertNotContains(response, 'Tomato Far')

    def test_save_location_rejects_bad_values(self):
        response = self._set_location(self.client, lat=999)
        self.assertEqual(response.status_code, 400)

    def test_customer_location_saved_to_profile(self):
        user = User.objects.create_user('cust', password='pw-Str0ng-pass')
        customer = Customer.objects.create(user=user, address='a', phone='1')
        self.client.force_login(user)
        self._set_location(self.client)
        customer.refresh_from_db()
        self.assertEqual(customer.latitude, 22.7)

    def test_vendor_location_saved_as_shop_location(self):
        user = User.objects.create_user('vend', password='pw-Str0ng-pass')
        self.nowhere.user = user
        self.nowhere.save()
        self.client.force_login(user)
        self._set_location(self.client, 22.5, 75.5)
        self.nowhere.refresh_from_db()
        self.assertEqual((self.nowhere.latitude, self.nowhere.longitude), (22.5, 75.5))

    def test_vendor_registration_stores_coordinates(self):
        self.client.post(reverse('vendor_register'), {
            'username': 'newv', 'email': 'v@x.com', 'vendor_name': 'NewV',
            'shop_name': 'NV', 'shop_address': 'addr', 'phone': '123',
            'latitude': '22.71', 'longitude': '75.86',
            'password': 'Str0ng-pass-123', 'password_confirm': 'Str0ng-pass-123',
        })
        vendor = Vendor.objects.get(name='NewV')
        self.assertEqual(vendor.latitude, 22.71)

    def test_customer_registration_stores_coordinates(self):
        self.client.post(reverse('register'), {
            'username': 'newc', 'email': 'c@x.com', 'address': 'addr',
            'phone': '123', 'latitude': '22.71', 'longitude': '75.86',
            'password': 'Str0ng-pass-123', 'password_confirm': 'Str0ng-pass-123',
        })
        self.assertEqual(Customer.objects.get(user__username='newc').longitude, 75.86)

    def test_cart_shows_distance_per_shop(self):
        self._set_location(self.client)
        product = Product.objects.get(vendor=self.near)
        self.client.post(reverse('add_to_cart', args=[product.id]), {'quantity': 1})
        response = self.client.get(reverse('cart'))
        self.assertContains(response, 'away')


class MobileAndPasswordAssetTests(TestCase):
    def test_login_pages_load_password_toggle_and_mobile_css(self):
        for name in ('login', 'vendor_login', 'register', 'vendor_register'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('market/password-toggle.js', html, name)
            self.assertIn('market/mobile.css', html, name)
            self.assertIn('viewport-fit=cover', html, name)

    def test_admin_pages_inherit_shared_assets(self):
        admin = User.objects.create_superuser('boss', 'b@x.com', 'Str0ng-pass-123')
        self.client.force_login(admin)
        response = self.client.get(reverse('market_admin_dashboard'))
        self.assertContains(response, 'market/responsive.js')
