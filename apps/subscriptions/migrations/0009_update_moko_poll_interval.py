# Met à jour l'intervalle de polling MOKO de 10s à 15s.

from django.db import migrations


def forwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    # Créer ou récupérer l'intervalle de 15 secondes
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=15,
        period='seconds',
    )
    
    # Mettre à jour la tâche existante
    PeriodicTask.objects.filter(
        name='poll_moko_pending_payments'
    ).update(interval=schedule)


def backwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    # Revenir à l'intervalle de 10 secondes
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=10,
        period='seconds',
    )
    PeriodicTask.objects.filter(
        name='poll_moko_pending_payments'
    ).update(interval=schedule)


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0008_moko_poll_periodic_task'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
