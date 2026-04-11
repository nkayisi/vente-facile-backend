# Generated manually for MOKO paydrc_reference storage

from django.db import migrations, models


def copy_external_id_to_transaction_id(apps, schema_editor):
    SubscriptionPayment = apps.get_model('subscriptions', 'SubscriptionPayment')
    for p in SubscriptionPayment.objects.exclude(external_id='').filter(external_transaction_id=''):
        p.external_transaction_id = p.external_id[:255]
        p.save(update_fields=['external_transaction_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0006_alter_plan_trial_days_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscriptionpayment',
            name='external_transaction_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Identifiant transaction PayDRC / MOKO (paydrc_reference)',
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name='subscriptionpayment',
            name='reference',
            field=models.CharField(blank=True, db_index=True, max_length=255),
        ),
        migrations.RunPython(copy_external_id_to_transaction_id, migrations.RunPython.noop),
    ]
