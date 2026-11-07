from datetime import time

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from .models import DeliveryWindow


class DeliveryWindowAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('boss', 'b@x.com', 'pw12345!x')
        self.client.force_login(self.admin)
        self.add_url = reverse('market_admin_delivery_window_add')
        self.list_url = reverse('market_admin_delivery_window_list')

    def payload(self, **overrides):
        data = {'name': 'Morning', 'start_time': '08:00', 'end_time': '10:00',
                'capacity': '20', 'is_active': 'on'}
        data.update(overrides)
        return data

    def make_window(self, **overrides):
        values = {'name': 'Evening', 'start_time': time(17), 'end_time': time(19),
                  'capacity': 10, 'is_active': True}
        values.update(overrides)
        return DeliveryWindow.objects.create(**values)

    def test_empty_list_warns_that_checkout_is_blocked(self):
        response = self.client.get(self.list_url)
        self.assertContains(response, 'No active delivery windows')

    def test_add_window(self):
        response = self.client.post(self.add_url, self.payload())
        self.assertRedirects(response, self.list_url)
        window = DeliveryWindow.objects.get()
        self.assertEqual((window.name, window.start_time, window.end_time,
                          window.capacity, window.is_active),
                         ('Morning', time(8), time(10), 20, True))
        self.assertContains(self.client.get(self.list_url), 'Morning')

    def test_end_must_be_after_start(self):
        for end in ('08:00', '07:00'):
            response = self.client.post(self.add_url, self.payload(end_time=end))
            self.assertContains(response, 'End time must be after the start time.')
        self.assertEqual(DeliveryWindow.objects.count(), 0)

    def test_capacity_must_be_positive(self):
        for capacity in ('0', '-3', 'abc'):
            response = self.client.post(self.add_url, self.payload(capacity=capacity))
            self.assertEqual(response.status_code, 200)
        self.assertEqual(DeliveryWindow.objects.count(), 0)

    def test_duplicate_name_is_rejected(self):
        self.make_window(name='Morning')
        response = self.client.post(self.add_url, self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DeliveryWindow.objects.count(), 1)

    def test_edit_window(self):
        window = self.make_window()
        url = reverse('market_admin_delivery_window_edit', args=[window.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, self.payload(name='Late', capacity='5'))
        self.assertRedirects(response, self.list_url)
        window.refresh_from_db()
        self.assertEqual((window.name, window.capacity, window.is_active), ('Late', 5, True))

    def test_unchecked_active_turns_window_off(self):
        window = self.make_window()
        data = self.payload(name='Evening')
        del data['is_active']
        self.client.post(reverse('market_admin_delivery_window_edit', args=[window.id]), data)
        window.refresh_from_db()
        self.assertFalse(window.is_active)

    def test_toggle_flips_status_and_needs_post(self):
        window = self.make_window()
        url = reverse('market_admin_delivery_window_toggle', args=[window.id])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        window.refresh_from_db()
        self.assertFalse(window.is_active)
        self.client.post(url)
        window.refresh_from_db()
        self.assertTrue(window.is_active)

    def test_anonymous_and_non_staff_cannot_open(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        shopper = User.objects.create_user('shopper', password='pw12345!x')
        self.client.force_login(shopper)
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertEqual(self.client.post(self.add_url, self.payload()).status_code, 302)
        self.assertEqual(DeliveryWindow.objects.count(), 0)

    def test_staff_with_view_only_cannot_add_edit_or_toggle(self):
        window = self.make_window()
        staff = User.objects.create_user('viewer', password='pw12345!x', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='view_deliverywindow'))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        self.assertEqual(self.client.get(self.add_url).status_code, 403)
        self.assertEqual(self.client.post(self.add_url, self.payload()).status_code, 403)
        edit = reverse('market_admin_delivery_window_edit', args=[window.id])
        self.assertEqual(self.client.get(edit).status_code, 403)
        toggle = reverse('market_admin_delivery_window_toggle', args=[window.id])
        self.assertEqual(self.client.post(toggle).status_code, 403)
        window.refresh_from_db()
        self.assertTrue(window.is_active)

    def test_nav_shows_delivery_windows_link(self):
        self.assertContains(self.client.get(self.list_url), 'Delivery windows')
