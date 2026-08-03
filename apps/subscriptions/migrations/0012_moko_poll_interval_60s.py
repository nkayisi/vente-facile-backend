# Optimisation prod : porte l'intervalle de polling MOKO de 15s à 60s.
# La tâche court déjà à vide quand la file Redis est vide, et le webhook
# moko_callback reste la voie de confirmation principale ; 60s suffit en filet.

from django.db import migrations


def forwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=60,
        period='seconds',
    )
    PeriodicTask.objects.filter(
        name='poll_moko_pending_payments'
    ).update(interval=schedule)


def backwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=15,
        period='seconds',
    )
    PeriodicTask.objects.filter(
        name='poll_moko_pending_payments'
    ).update(interval=schedule)


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0011_plan_max_warehouses'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
