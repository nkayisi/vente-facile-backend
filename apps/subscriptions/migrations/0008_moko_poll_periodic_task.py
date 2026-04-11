# Enregistre la tâche Beat de polling MOKO (toutes les 10 s).

from django.db import migrations


def forwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=10,
        period='seconds',
    )
    PeriodicTask.objects.update_or_create(
        name='poll_moko_pending_payments',
        defaults={
            'interval': schedule,
            'task': 'apps.subscriptions.tasks.poll_moko_pending_payments',
            'enabled': True,
        },
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name='poll_moko_pending_payments').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0007_subscriptionpayment_external_transaction_id'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
