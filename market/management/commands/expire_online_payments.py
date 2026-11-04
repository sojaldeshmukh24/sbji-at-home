from django.core.management.base import BaseCommand, CommandError

from market.payments import (
	PaymentConfigurationError,
	PaymentGatewayError,
	expire_pending_razorpay_orders,
)


class Command(BaseCommand):
	help = 'Expire pending Razorpay test payments and release their stock reservations.'

	def handle(self, *args, **options):
		try:
			expired = expire_pending_razorpay_orders()
		except (PaymentConfigurationError, PaymentGatewayError) as error:
			raise CommandError(str(error)) from error
		self.stdout.write(
			self.style.SUCCESS(
				f'Expired {expired} pending online payment(s).',
			),
		)
