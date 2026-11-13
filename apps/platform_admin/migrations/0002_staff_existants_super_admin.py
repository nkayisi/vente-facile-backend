"""
Chaque compte `is_staff` existant reçoit un profil de SUPER-ADMIN.

┌──────────────────────────────────────────────────────────────────────────────┐
│ SANS CETTE MIGRATION, LE DÉPLOIEMENT ENFERME DEHORS TOUTE L'ÉQUIPE.         │
│                                                                              │
│ `IsPlatformAdmin` ne lit plus `is_staff` mais le profil. Un compte qui       │
│ administrait la plateforme la veille recevrait 403 sur chaque écran le       │
│ lendemain.                                                                   │
│                                                                              │
│ ⚠ TOUT LE MONDE DEVIENT SUPER-ADMIN, ET C'EST UN SUR-PROVISIONNEMENT         │
│ VOLONTAIRE. C'est le pouvoir que chacun avait déjà ; rien ne permet de       │
│ deviner ici qui ne devrait être que support. Le rétrograder est un geste     │
│ humain, à faire ensuite depuis l'écran « Administrateurs », et il laissera   │
│ sa trace au journal.                                                         │
└──────────────────────────────────────────────────────────────────────────────┘

Idempotente : un compte qui a déjà un profil n'est pas touché.
"""
from django.db import migrations
from django.utils import timezone

MARQUE = "Migration automatique : is_staff préexistant."


def accorder(apps, schema_editor):
    User = apps.get_model('users', 'User')
    Profil = apps.get_model('platform_admin', 'PlatformAdminProfile')
    Journal = apps.get_model('platform_admin', 'AdminActionLog')

    deja = set(Profil.objects.values_list('user_id', flat=True))
    comptes = list(
        User.objects.filter(is_staff=True).exclude(pk__in=deja).only('pk', 'email')
    )
    if not comptes:
        return

    maintenant = timezone.now()
    Profil.objects.bulk_create([
        Profil(
            user_id=u.pk, role='super_admin', is_active=True,
            extra_permissions=[], granted_at=maintenant, notes=MARQUE,
        )
        for u in comptes
    ])
    Journal.objects.bulk_create([
        Journal(
            actor=None, actor_email='', action='grant_role',
            target_type='platform_admin', target_id=str(u.pk), target_repr=u.email,
            before={}, after={'role': 'super_admin', 'is_active': True, 'extra_permissions': []},
            reason=MARQUE,
        )
        for u in comptes
    ])


def retirer(apps, schema_editor):
    Profil = apps.get_model('platform_admin', 'PlatformAdminProfile')
    Journal = apps.get_model('platform_admin', 'AdminActionLog')
    Profil.objects.filter(notes=MARQUE).delete()
    Journal.objects.filter(reason=MARQUE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('platform_admin', '0001_profils_et_journal'),
    ]

    operations = [
        migrations.RunPython(accorder, retirer),
    ]
