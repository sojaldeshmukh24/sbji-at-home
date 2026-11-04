import uuid

from django.db import migrations, models


def populate_checkout_tokens(apps, schema_editor):
    Order = apps.get_model('market', 'Order')
    for order in Order.objects.filter(checkout_token__isnull=True).iterator():
        Order.objects.filter(pk=order.pk).update(checkout_token=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ('market', '0017_order_delivery_slot_date_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='checkout_token',
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.RunPython(
            populate_checkout_tokens,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='order',
            name='checkout_token',
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
            ),
        ),
    ]
