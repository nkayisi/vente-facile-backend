# Programme l'expiration des points de fidélité.
#
# `LoyaltyProgram.points_expiry_days` était configurable depuis l'interface mais
# aucune tâche ne le lisait : une organisation qui réglait « 90 jours » ne voyait
# jamais un point expirer. Horaire exprimé dans CELERY_TIMEZONE (Africa/Kinshasa).

from django.db import migrations

TASK_NAME = 'expire_loyalty_points'
TASK_PATH = 'apps.settings.tasks.expire_loyalty_points'


def forwards(apps, schema_editor):
    CrontabSchedule = apps.get_model('django_celery_beat', 'CrontabSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    # Chaque jour à 02:00, avant les autres tâches d'alertes : le solde de points
    # doit être à jour quand les rapports du matin le lisent.
    cron, _ = CrontabSchedule.objects.get_or_create(
        minute='0', hour='2', day_of_week='*', day_of_month='*', month_of_year='*',
    )

    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            'crontab': cron,
            'interval': None,
            'task': TASK_PATH,
            'enabled': True,
            'description': (
                "Retire les points de fidélité arrivés à échéance "
                "(expiration par lot, FIFO)."
            ),
        },
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0010_remove_loyaltytransaction_loyalty_tx_unique_per_sale_type_and_more'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
