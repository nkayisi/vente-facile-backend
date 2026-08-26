# Programme les alertes de créances clients.
#
# `Alert.AlertType` déclarait `payment_due`, `payment_overdue` et `credit_limit`
# depuis l'origine, mais aucune tâche ne les produisait : `Sale.due_date` était
# stocké et exposé sans que rien ne le lise.
# Horaire exprimé dans CELERY_TIMEZONE (Africa/Kinshasa).

from django.db import migrations

TASK_NAME = 'check_customer_payment_due'
TASK_PATH = 'apps.notifications.tasks.check_customer_payment_due'


def forwards(apps, schema_editor):
    CrontabSchedule = apps.get_model('django_celery_beat', 'CrontabSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    # Chaque jour à 06:30, entre la péremption produits (06:00) et l'abonnement
    # (07:00) : le marchand ouvre sa journée avec ses relances déjà prêtes.
    cron, _ = CrontabSchedule.objects.get_or_create(
        minute='30', hour='6', day_of_week='*', day_of_month='*', month_of_year='*',
    )

    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            'crontab': cron,
            'interval': None,
            'task': TASK_PATH,
            'enabled': True,
            'description': (
                "Alerte sur les factures échues ou proches de l'échéance, et "
                "sur les clients proches de leur limite de crédit."
            ),
        },
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0003_schedule_alert_tasks'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
