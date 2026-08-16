# Generated manually - durcit RegisterSession (UniqueConstraint open + counted_balance)
# et ajoute un index sur Sale.sold_by pour le filtre par caissier.

from decimal import Decimal
from django.db import migrations, models
from django.utils import timezone


def close_duplicate_open_sessions(apps, schema_editor):
    """
    Avant d'appliquer la UniqueConstraint, fermer toute session 'open' en double
    sur une même caisse en gardant la plus récente. Sans ce nettoyage, la
    contrainte refuserait de s'appliquer sur les données existantes.
    """
    RegisterSession = apps.get_model('sales', 'RegisterSession')
    from django.db.models import Count

    duplicates = (
        RegisterSession.objects.filter(status='open')
        .values('register_id')
        .annotate(c=Count('id'))
        .filter(c__gt=1)
    )
    for row in duplicates:
        sessions = list(
            RegisterSession.objects.filter(
                register_id=row['register_id'], status='open'
            ).order_by('-opened_at')
        )
        # Garder la plus récente, fermer les autres
        for stale in sessions[1:]:
            stale.status = 'closed'
            stale.closed_at = timezone.now()
            stale.expected_balance = stale.expected_balance or Decimal('0.00')
            stale.closing_balance = stale.closing_balance or stale.expected_balance
            stale.difference = stale.difference or Decimal('0.00')
            stale.notes = (
                (stale.notes or '') +
                ' | Auto-fermée par migration 0006 (session en double)'
            ).strip(' |')
            stale.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0005_register_warehouse_required'),
    ]

    operations = [
        # Données : nettoyer les sessions en double AVANT la contrainte
        migrations.RunPython(close_duplicate_open_sessions, noop_reverse),

        # Nouveau champ : comptage manuel optionnel à la fermeture
        migrations.AddField(
            model_name='registersession',
            name='counted_balance',
            field=models.DecimalField(
                max_digits=15,
                decimal_places=2,
                null=True,
                blank=True,
                help_text='Montant réel compté en caisse à la fermeture (saisie manuelle optionnelle).',
            ),
        ),

        # Contrainte : une seule session 'open' par caisse à la fois
        migrations.AddConstraint(
            model_name='registersession',
            constraint=models.UniqueConstraint(
                fields=['register'],
                condition=models.Q(status='open'),
                name='unique_open_session_per_register',
            ),
        ),

        # Index pour le filtre cashier (Sale.sold_by + sale_date desc)
        migrations.AddIndex(
            model_name='sale',
            index=models.Index(
                fields=['organization', 'sold_by', '-sale_date'],
                name='sales_org_sold_by_date_idx',
            ),
        ),
    ]
