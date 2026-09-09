"""
Remplit toute devise laissée VIDE par la devise principale de l'établissement.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CES LIGNES N'ONT PAS PERDU LEUR DEVISE : ELLES N'EN AVAIENT QU'UNE.          │
│                                                                              │
│ Elles datent d'avant la gestion multi-devise, où un établissement n'en        │
│ portait qu'une seule - la sienne. Les remplir avec la principale n'invente    │
│ donc rien, cela rétablit ce que la colonne aurait dû contenir dès l'origine.  │
│                                                                              │
│ Une devise vide n'est pas un défaut d'affichage : `money(x, "")` rend le      │
│ nombre SANS SYMBOLE, dans une application où le même chiffre vaut soit trois  │
│ dollars, soit trois francs. Et la rature de télémétrie du terminal ancre les  │
│ montants sur leur devise : un montant sans symbole n'est pas raturé et part   │
│ en clair vers un service tiers.                                              │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ `updated_at` EST POSÉ À LA MAIN, ET CE N'EST PAS FACULTATIF.                 │
│                                                                              │
│ Le tirage pagine sur un curseur `(updated_at, id)`. Une ligne remplie dont    │
│ l'horodatage ne bouge pas devient INVISIBLE AU TIRAGE, définitivement et sur  │
│ tous les terminaux à la fois : ils serviraient éternellement l'ancienne       │
│ valeur, sans qu'aucune synchronisation n'y puisse rien. C'est le défaut de    │
│ `CustomerBalance` du lot 6, et celui des neuf écritures en masse du lot 5.    │
│                                                                              │
│ `queryset.update()` court-circuite `save()`, donc `auto_now` avec lui, et le  │
│ garde-fou `apps/core/tests/test_bulk_write_visibility.py` EXCLUT les          │
│ migrations : rien ici ne rattraperait l'oubli.                               │
└──────────────────────────────────────────────────────────────────────────────┘

Idempotente : elle ne touche que les lignes réellement vides.
"""
from django.db import migrations
from django.utils import timezone


#: `app.Modele` → nom du champ de devise. Les champs dont la devise est portée
#: par une FK déjà résolue (Payment ← Sale, Invoice ← Subscription) sont
#: volontairement absents : les remplir depuis l'organisation contredirait leur
#: parent, qui fait autorité.
CHAMPS = [
    ('cashbook', 'Expense', 'currency'),
    ('cashbook', 'CashMovement', 'currency'),
    ('contacts', 'CustomerTransaction', 'currency'),
    ('contacts', 'CustomerBalance', 'currency'),
    ('purchases', 'PurchaseOrder', 'currency'),
    ('purchases', 'SupplierPayment', 'currency'),
    ('sales', 'Sale', 'currency'),
]


def remplir(apps, schema_editor):
    Organization = apps.get_model('organizations', 'Organization')
    maintenant = timezone.now()

    for org in Organization.objects.all().iterator():
        # `Organization.currency` porte le CODE de la principale, et une
        # migration antérieure (settings/0009) garantit qu'il concorde avec la
        # ligne `OrganizationCurrency` marquée `is_primary`.
        principale = (org.currency or '').strip() or 'CDF'

        for app_label, nom, champ in CHAMPS:
            modele = apps.get_model(app_label, nom)
            vides = modele.objects.filter(organization_id=org.id, **{champ: ''})

            valeurs = {champ: principale, 'updated_at': maintenant}
            # Les modèles synchronisables portent un SECOND horodatage, distinct :
            # `updated_at` porte le curseur du tirage, `sync_updated_at` la
            # résolution de conflit. Les deux doivent suivre.
            if any(f.name == 'sync_updated_at' for f in modele._meta.get_fields()):
                valeurs['sync_updated_at'] = maintenant

            vides.update(**valeurs)


def rien(apps, schema_editor):
    """Irréversible : on ne sait plus quelles lignes étaient vides, et les
    revider recréerait exactement le défaut que cette migration referme."""


class Migration(migrations.Migration):

    dependencies = [
        ('cashbook', '0011_horloge_metier'),
        ('contacts', '0016_horloge_metier'),
        ('purchases', '0007_horloge_metier'),
        ('sales', '0024_horloge_metier'),
        ('organizations', '0001_initial'),
        ('settings', '0009_ensure_primary_organization_currency'),
    ]

    operations = [migrations.RunPython(remplir, rien)]
