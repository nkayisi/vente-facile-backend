"""
Les membres non propriétaires sans entrepôt reçoivent l'entrepôt principal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ « AUCUN ENTREPÔT » NE VOIT RIEN, ET C'EST VOULU : CETTE COMMANDE RÉPARE.    │
│                                                                              │
│ Un membre borné sans entrepôt actif ne lit aucune donnée et ne peut ouvrir  │
│ aucune caisse. La règle est juste - deviner un entrepôt l'ouvrirait au       │
│ mauvais dépôt - mais les comptes créés avant elle sont à reprendre.          │
│                                                                              │
│ - Sans aucune affectation vivante : il reçoit le principal, par              │
│   `assurer_entrepot_du_membre`, le même corps que le signal de création.     │
│ - Affecté seulement à des entrepôts DÉSACTIVÉS : il est NOMMÉ, jamais        │
│   réaffecté. Le remettre ailleurs est une décision du propriétaire.          │
│ - Le propriétaire n'est jamais touché : son accès vient de son rôle.         │
└──────────────────────────────────────────────────────────────────────────────┘

SIMULATION PAR DÉFAUT. `--appliquer` pour écrire ; `--dry-run` est accepté et
ne fait que confirmer le défaut.

Usage :
    python manage.py reparer_membres_sans_entrepot
    python manage.py reparer_membres_sans_entrepot --appliquer
    python manage.py reparer_membres_sans_entrepot --organization <uuid> --appliquer
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from apps.organizations.models import OrganizationMembership
from apps.organizations.services import assurer_entrepot_du_membre, entrepot_principal


class Command(BaseCommand):
    help = "Donne l'entrepôt principal aux membres non propriétaires qui n'en ont aucun."

    def add_arguments(self, parser):
        parser.add_argument('--appliquer', action='store_true', help='Écrire en base.')
        parser.add_argument('--dry-run', action='store_true', help='Simulation (le défaut).')
        parser.add_argument('--organization', default=None, help="Limiter à un établissement.")

    def handle(self, *args, **options):
        if options['appliquer'] and options['dry_run']:
            raise CommandError("`--appliquer` et `--dry-run` s'excluent.")
        appliquer = options['appliquer']

        membres = (
            OrganizationMembership.objects.filter(is_active=True)
            .exclude(role=OrganizationMembership.Role.OWNER)
            .select_related('user', 'organization')
            .annotate(
                _vivants=Count(
                    'assigned_warehouses',
                    filter=Q(assigned_warehouses__is_deleted=False),
                    distinct=True,
                ),
                _actifs=Count(
                    'assigned_warehouses',
                    filter=Q(
                        assigned_warehouses__is_deleted=False,
                        assigned_warehouses__is_active=True,
                    ),
                    distinct=True,
                ),
            )
            .order_by('organization__name', 'user__email')
        )
        if options['organization']:
            membres = membres.filter(organization_id=options['organization'])

        sans, inactifs, sans_principal = [], [], []
        for m in membres:
            if m._vivants == 0:
                sans.append(m)
            elif m._actifs == 0:
                inactifs.append(m)

        for m in sans:
            principal = entrepot_principal(m.organization)
            if principal is None:
                sans_principal.append(m)
                continue
            self.stdout.write(
                f"{m.organization.name} : {m.user.email} ({m.get_role_display()}) "
                f"-> {principal.name}"
            )
            if appliquer:
                with transaction.atomic():
                    assurer_entrepot_du_membre(m)

        for m in sans_principal:
            self.stdout.write(self.style.WARNING(
                f"{m.organization.name} : {m.user.email} n'a aucun entrepôt, et "
                "l'établissement n'a aucun entrepôt actif à lui donner."
            ))
        for m in inactifs:
            self.stdout.write(self.style.WARNING(
                f"{m.organization.name} : {m.user.email} n'est affecté qu'à des "
                "entrepôts désactivés. Au propriétaire de l'affecter ailleurs."
            ))

        repares = len(sans) - len(sans_principal)
        membres_mot = 'membre' if repares < 2 else 'membres'
        if appliquer:
            verbe = 'a reçu' if repares < 2 else 'ont reçu'
        else:
            verbe = 'recevrait' if repares < 2 else 'recevraient'
        self.stdout.write(self.style.SUCCESS(
            f"{repares} {membres_mot} {verbe} l'entrepôt principal"
            + ('' if appliquer else ' (simulation : rien n\'a été écrit, --appliquer pour écrire)')
            + f" ; à affecter par le propriétaire : {len(inactifs)}."
        ))
