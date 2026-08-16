# Planifie la réconciliation des paiements MOKO encaissés sans abonnement actif.
#
# Le polling (`poll_moko_pending_payments`) ne voit que ce que la file Redis lui
# présente. Cette tâche repart de la base toutes les 30 minutes et rattrape les
# paiements orphelins : file vidée, worker arrêté, TTL des métadonnées expiré, ou
# paiement marqué `failed` à tort par l'ancienne logique.

from django.db import migrations

TASK_NAME = 'reconcile_moko_payments'
TASK_PATH = 'apps.subscriptions.tasks.reconcile_moko_payments'


def forwards(apps, schema_editor):
    IntervalSchedule = apps.get_model('django_celery_beat', 'IntervalSchedule')
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=30,
        period='minutes',
    )

    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            'task': TASK_PATH,
            'interval': schedule,
            'enabled': True,
            'description': (
                "Rattrape les paiements MOKO encaissés dont l'abonnement n'a pas "
                "été activé. Indépendant de la file Redis."
            ),
        },
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0014_backfill_plan_currency'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
