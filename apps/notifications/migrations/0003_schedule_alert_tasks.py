# Programme les tâches d'alertes (jusqu'ici définies mais jamais schedulées).
# Horaires exprimés dans CELERY_TIMEZONE (Africa/Kinshasa).

from django.db import migrations


TASKS = {
    'check_low_stock_alerts': 'apps.notifications.tasks.check_low_stock_alerts',
    'check_expiring_products': 'apps.notifications.tasks.check_expiring_products',
    'check_subscription_expiry': 'apps.notifications.tasks.check_subscription_expiry',
    'cleanup_old_notifications': 'apps.notifications.tasks.cleanup_old_notifications',
}


def forwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    CrontabSchedule = apps.get_model('django_celery_beat', 'CrontabSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    # Stock bas : toutes les heures (léger désormais grâce à l'agrégation DB).
    hourly, _ = IntervalSchedule.objects.get_or_create(every=1, period='hours')

    # Péremption produits : chaque jour à 06:00.
    expiry_cron, _ = CrontabSchedule.objects.get_or_create(
        minute='0', hour='6', day_of_week='*', day_of_month='*', month_of_year='*',
    )
    # Expiration abonnement : chaque jour à 07:00.
    sub_cron, _ = CrontabSchedule.objects.get_or_create(
        minute='0', hour='7', day_of_week='*', day_of_month='*', month_of_year='*',
    )
    # Nettoyage notifications : chaque lundi à 03:00.
    cleanup_cron, _ = CrontabSchedule.objects.get_or_create(
        minute='0', hour='3', day_of_week='1', day_of_month='*', month_of_year='*',
    )

    PeriodicTask.objects.update_or_create(
        name='check_low_stock_alerts',
        defaults={'interval': hourly, 'crontab': None,
                  'task': TASKS['check_low_stock_alerts'], 'enabled': True},
    )
    PeriodicTask.objects.update_or_create(
        name='check_expiring_products',
        defaults={'crontab': expiry_cron, 'interval': None,
                  'task': TASKS['check_expiring_products'], 'enabled': True},
    )
    PeriodicTask.objects.update_or_create(
        name='check_subscription_expiry',
        defaults={'crontab': sub_cron, 'interval': None,
                  'task': TASKS['check_subscription_expiry'], 'enabled': True},
    )
    PeriodicTask.objects.update_or_create(
        name='cleanup_old_notifications',
        defaults={'crontab': cleanup_cron, 'interval': None,
                  'task': TASKS['cleanup_old_notifications'], 'enabled': True},
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name__in=list(TASKS.keys())).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0002_initial'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
