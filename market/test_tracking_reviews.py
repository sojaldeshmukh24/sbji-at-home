from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .models import (
    Category, Customer, Order, OrderItem, OrderStatusHistory, Product, Review,
    Vendor, VendorOrder,
)
from .tracking import build_tracking


class TrackingReviewBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'asha',
            password='pw12345!x',
            first_name='Asha',
        )
        self.customer = Customer.objects.create(
            user=self.user,
            address='A',
            phone='1',
        )
        self.other = User.objects.create_user('ravi', password='pw12345!x')
        Customer.objects.create(user=self.other, address='B', phone='2')
        self.vendor = Vendor.objects.create(name='Ram', shop_name='Ram Sabzi')
        cat = Category.objects.create(name='Vegetables')
        self.product = Product.objects.create(
            name='Tomato',
            price=10,
            stock=5,
            units='kg',
            category=cat,
            vendor=self.vendor,
        )
        self.order = Order.objects.create(customer=self.customer)
        self.vo = VendorOrder.objects.create(
            order=self.order,
            vendor=self.vendor,
        )
        OrderItem.objects.create(
            order=self.order,
            vendor_order=self.vo,
            product=self.product,
            quantity=2,
            unit_price=10,
        )

    def move(self, status):
        previous = self.vo.status
        self.vo.status = status
        self.vo.save()
        OrderStatusHistory.objects.create(
            order=self.order,
            vendor_order=self.vo,
            previous_status=previous,
            new_status=status,
        )

    def login(self, user=None):
        self.client.force_login(user or self.user)


class TrackingTests(TrackingReviewBase):
    def test_build_tracking_marks_done_and_current(self):
        self.move(VendorOrder.Status.CONFIRMED)
        self.move(VendorOrder.Status.PREPARING)
        vo = VendorOrder.objects.prefetch_related('status_history').get(
            id=self.vo.id,
        )
        steps = build_tracking(vo, self.order.order_date)
        self.assertEqual(
            [step['is_done'] for step in steps],
            [True, True, False, False, False, False],
        )
        self.assertEqual(
            [step['is_current'] for step in steps],
            [False, False, True, False, False, False],
        )
        self.assertIsNotNone(steps[0]['time'])
        self.assertIsNone(steps[5]['time'])

    def test_delivered_marks_every_step_done(self):
        self.move(VendorOrder.Status.DELIVERED)
        vo = VendorOrder.objects.prefetch_related('status_history').get(
            id=self.vo.id,
        )
        steps = build_tracking(vo, self.order.order_date)
        self.assertTrue(all(step['is_done'] for step in steps))
        self.assertFalse(any(step['is_current'] for step in steps))

    def test_rejected_has_no_timeline(self):
        self.move(VendorOrder.Status.REJECTED)
        vo = VendorOrder.objects.prefetch_related('status_history').get(
            id=self.vo.id,
        )
        self.assertEqual(build_tracking(vo, self.order.order_date), [])

    def test_track_page_shows_shop_and_steps(self):
        self.move(VendorOrder.Status.OUT_FOR_DELIVERY)
        self.login()
        response = self.client.get(
            reverse('track_order', args=[self.order.id]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ram Sabzi')
        self.assertContains(response, 'Out for delivery')
        self.assertTrue(response.context['auto_refresh'])

    def test_no_auto_refresh_when_everything_delivered(self):
        self.move(VendorOrder.Status.DELIVERED)
        self.login()
        response = self.client.get(
            reverse('track_order', args=[self.order.id]),
        )
        self.assertFalse(response.context['auto_refresh'])

    def test_other_customers_cannot_track_order(self):
        self.login(self.other)
        response = self.client.get(
            reverse('track_order', args=[self.order.id]),
        )
        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(
            reverse('track_order', args=[self.order.id]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class ReviewTests(TrackingReviewBase):
    def url(self):
        return reverse('review_vendor_order', args=[self.vo.id])

    def test_cannot_review_before_delivery(self):
        self.login()
        for method in (
            self.client.get,
            lambda url: self.client.post(url, {'rating': 5}),
        ):
            response = method(self.url())
            self.assertEqual(response.status_code, 302)
        self.assertEqual(Review.objects.count(), 0)

    def test_review_after_delivery_is_saved(self):
        self.move(VendorOrder.Status.DELIVERED)
        self.login()
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        response = self.client.post(
            self.url(),
            {'rating': 4, 'comment': 'Fresh!'},
        )
        self.assertRedirects(
            response,
            reverse('order_confirmation', args=[self.order.id]),
        )
        review = Review.objects.get()
        self.assertEqual(
            (review.rating, review.vendor, review.customer),
            (4, self.vendor, self.customer),
        )

    def test_second_review_is_blocked(self):
        self.move(VendorOrder.Status.DELIVERED)
        self.login()
        self.client.post(self.url(), {'rating': 5})
        self.client.post(self.url(), {'rating': 1})
        self.assertEqual(Review.objects.get().rating, 5)

    def test_invalid_rating_is_rejected(self):
        self.move(VendorOrder.Status.DELIVERED)
        self.login()
        for bad in ('0', '6', 'abc', ''):
            response = self.client.post(self.url(), {'rating': bad})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(Review.objects.count(), 0)

    def test_other_customer_cannot_review(self):
        self.move(VendorOrder.Status.DELIVERED)
        self.login(self.other)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(
            self.client.post(self.url(), {'rating': 5}).status_code,
            404,
        )

    def test_database_blocks_out_of_range_rating(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Review.objects.create(
                vendor_order=self.vo,
                vendor=self.vendor,
                customer=self.customer,
                rating=9,
            )

    def test_shop_pages_show_average_and_comment(self):
        self.move(VendorOrder.Status.DELIVERED)
        Review.objects.create(
            vendor_order=self.vo,
            vendor=self.vendor,
            customer=self.customer,
            rating=4,
            comment='Very fresh',
        )
        shop = self.client.get(
            reverse('shop_detail', args=[self.vendor.id]),
        )
        self.assertContains(shop, '★ 4.0 (1 review)')
        self.assertContains(shop, 'Very fresh')
        self.assertContains(shop, 'Asha')
        listing = self.client.get(reverse('nearby_shops'))
        self.assertContains(listing, '★ 4.0 (1 review)')

    def test_order_page_links_to_review_and_tracking(self):
        self.move(VendorOrder.Status.DELIVERED)
        self.login()
        page = self.client.get(
            reverse('order_confirmation', args=[self.order.id]),
        )
        self.assertContains(page, self.url())
        self.assertContains(
            page,
            reverse('track_order', args=[self.order.id]),
        )
