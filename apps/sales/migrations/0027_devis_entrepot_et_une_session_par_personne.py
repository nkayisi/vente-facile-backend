"""
Le devis reçoit son entrepôt, et une personne n'a plus qu'une session ouverte.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DEUX OPÉRATIONS DE DONNÉES, ET L'ORDRE COMPTE.                              │
│                                                                              │
│ 1. La reprise des devis : l'entrepôt de la vente convertie, sinon l'unique  │
│    entrepôt du créateur, sinon le principal, sinon rien (toléré, comme les  │
│    ventes anciennes). `updated_at` est posé À LA MAIN : `update()` ne passe │
│    pas par `save()`, et une ligne dont l'horodatage ne bouge pas est        │
│    invisible au tirage.                                                      │
│ 2. Le contrôle préalable de la contrainte « une session ouverte par         │
│    personne » : s'il en existe déjà deux pour un même membre, la migration  │
│    ÉCHOUE en les nommant. Clôturer une caisse est un acte comptable (un Z), │
│    jamais une correction qu'une migration a le droit de faire en silence.   │
│    `scripts/repetition_migrations.sh` le montre sur une copie, avant.       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def reprendre_les_devis(apps, schema_editor):
    Quotation = apps.get_model('sales', 'Quotation')
    Warehouse = apps.get_model('inventory', 'Warehouse')
    MembershipWarehouse = apps.get_model('organizations', 'MembershipWarehouse')
    OrganizationMembership = apps.get_model('organizations', 'OrganizationMembership')

    principaux = dict(
        Warehouse.objects.filter(is_default=True, is_active=True, is_deleted=False)
        .values_list('organization_id', 'id')
    )
    maintenant = timezone.now()

    for devis in Quotation.objects.filter(warehouse__isnull=True).iterator():
        entrepot = None
        if devis.converted_sale_id:
            entrepot = (
                apps.get_model('sales', 'Sale').objects
                .filter(id=devis.converted_sale_id)
                .values_list('warehouse_id', flat=True)
                .first()
            )
        if entrepot is None and devis.created_by_id:
            membre = OrganizationMembership.objects.filter(
                organization_id=devis.organization_id, user_id=devis.created_by_id,
            ).exclude(role='owner').first()
            if membre is not None:
                affectes = list(
                    MembershipWarehouse.objects.filter(
                        membership_id=membre.id, warehouse__is_deleted=False,
                    ).values_list('warehouse_id', flat=True)[:2]
                )
                if len(affectes) == 1:
                    entrepot = affectes[0]
        if entrepot is None:
            entrepot = principaux.get(devis.organization_id)
        if entrepot is not None:
            Quotation.objects.filter(id=devis.id).update(
                warehouse_id=entrepot, updated_at=maintenant,
            )


def exiger_une_session_par_personne(apps, schema_editor):
    RegisterSession = apps.get_model('sales', 'RegisterSession')
    doublons = list(
        RegisterSession.objects.filter(status='open', opened_by__isnull=False)
        .values('organization_id', 'opened_by_id')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    if not doublons:
        return
    lignes = []
    for d in doublons:
        sessions = RegisterSession.objects.filter(
            organization_id=d['organization_id'],
            opened_by_id=d['opened_by_id'],
            status='open',
        ).values_list('id', 'register__name')
        detail = ', '.join(f'{nom} ({sid})' for sid, nom in sessions)
        lignes.append(
            f"  organisation {d['organization_id']}, utilisateur {d['opened_by_id']} : {detail}"
        )
    raise RuntimeError(
        "Des membres ont plusieurs sessions de caisse ouvertes. Clôturez-en une "
        "par membre (ticket Z) avant de déployer :\n" + '\n'.join(lignes)
    )


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0019_horloge_metier'),
        ('organizations', '0010_consultation_tous_entrepots'),
        ('sales', '0026_cagnotte_moyen_de_paiement'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='quotation',
            name='warehouse',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='quotations', to='inventory.warehouse'),
        ),
        migrations.RunPython(reprendre_les_devis, migrations.RunPython.noop),
        migrations.RunPython(exiger_une_session_par_personne, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='registersession',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'open')), fields=('organization', 'opened_by'), name='unique_open_session_per_user'),
        ),
    ]
