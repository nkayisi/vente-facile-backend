# Planifie la purge des journaux de l'administration (rétention 12 mois) et la
# clôture des prises en main échues. Horaire exprimé dans CELERY_TIMEZONE
# (Africa/Kinshasa) : 03:30, hors des heures d'ouverture des boutiques.

from django.db import migrations

TASK_NAME = 'purger_journaux_administration'
TASK_PATH = 'apps.platform_admin.tasks.purger_journaux'


def forwards(apps, schema_editor):
    CrontabSchedule = apps.get_model('django_celery_beat', 'CrontabSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    cron, _ = CrontabSchedule.objects.get_or_create(
        minute='30', hour='3', day_of_week='*', day_of_month='*', month_of_year='*',
    )
    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            'crontab': cron,
            'interval': None,
            'task': TASK_PATH,
            'enabled': True,
            'description': (
                "Clôt les prises en main échues, puis purge le journal d'audit, les "
                "prises en main et l'activité des comptes au-delà de douze mois."
            ),
        },
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('platform_admin', '0002_staff_existants_super_admin'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
