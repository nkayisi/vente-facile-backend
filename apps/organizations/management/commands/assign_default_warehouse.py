"""
Normalisation des données : assigne l'entrepôt principal par défaut.

Pour chaque organisation :
  - Les membres actifs (``OrganizationMembership``) SANS aucun entrepôt assigné
    reçoivent l'entrepôt principal dans ``assigned_warehouses``. Sans cela, un
    membre non-owner sans entrepôt n'a accès à aucune donnée scoped (il ne peut
    notamment pas ouvrir de session de caisse).
  - Les caisses (``Register``) sans entrepôt valide (``NULL`` ou rattachées à un
    entrepôt soft-deleted) sont rattachées à l'entrepôt principal.

« Entrepôt principal » = ``Warehouse.is_default=True`` ; à défaut, le premier
entrepôt actif non supprimé de l'organisation.

Propriétés :
  - **Idempotente** : un second passage ne modifie rien.
  - **Sûre pour la production** : ``--dry-run`` pour prévisualiser, écritures
    transactionnelles par organisation.

Note : assigner un entrepôt à un owner est sans effet fonctionnel (un owner voit
tous les entrepôts, son ``assigned_warehouses`` est ignoré par le scope). Utiliser
``--skip-owners`` pour ne pas les toucher du tout.

Usage :
    python manage.py assign_default_warehouse --dry-run
    python manage.py assign_default_warehouse
    python manage.py assign_default_warehouse --organization <uuid>
    python manage.py assign_default_warehouse --skip-owners
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q

from apps.organizations.models import Organization, OrganizationMembership
from apps.inventory.models import Warehouse
from apps.sales.models import Register


class Command(BaseCommand):
    help = (
        "Assigne l'entrepôt principal par défaut aux membres et aux caisses qui "
        "n'ont pas d'entrepôt associé (normalisation des données)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Affiche les changements sans rien écrire en base.",
        )
        parser.add_argument(
            '--organization',
            dest='organization',
            default=None,
            help="Limiter à une organisation (UUID). Par défaut : toutes.",
        )
        parser.add_argument(
            '--skip-owners',
            action='store_true',
            help="Ne pas assigner d'entrepôt aux propriétaires (ils voient tout).",
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        org_id = options['organization']
        skip_owners = options['skip_owners']
        prefix = "[DRY-RUN] " if dry else ""

        orgs = Organization.objects.all()
        if org_id:
            orgs = orgs.filter(id=org_id)

        if not orgs.exists():
            self.stderr.write(self.style.WARNING("Aucune organisation trouvée."))
            return

        total_members = 0
        total_registers = 0
        skipped_orgs = 0

        for org in orgs.iterator():
            principal = self._principal_warehouse(org)
            if principal is None:
                skipped_orgs += 1
                self.stderr.write(self.style.WARNING(
                    f"⚠ Org « {org.name} » ({org.id}) : aucun entrepôt disponible, ignorée."
                ))
                continue

            members_qs = (
                OrganizationMembership.objects
                .filter(organization=org, is_active=True)
                .annotate(wh_count=Count(
                    'assigned_warehouses',
                    filter=Q(assigned_warehouses__is_deleted=False),
                ))
                .filter(wh_count=0)
                .select_related('user')
            )
            if skip_owners:
                members_qs = members_qs.exclude(role=OrganizationMembership.Role.OWNER)
            members_to_fix = list(members_qs)

            registers_to_fix = list(
                Register.objects.filter(organization=org).filter(
                    Q(warehouse__isnull=True) | Q(warehouse__is_deleted=True)
                )
            )

            if not members_to_fix and not registers_to_fix:
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nOrg « {org.name} » ({org.id}) — entrepôt principal : "
                f"{principal.name} ({principal.id})"
            ))

            with transaction.atomic():
                for m in members_to_fix:
                    self.stdout.write(
                        f"  {prefix}membre {m.user.email} (role={m.role}) "
                        f"→ + « {principal.name} »"
                    )
                    if not dry:
                        m.assigned_warehouses.add(principal)
                    total_members += 1

                for reg in registers_to_fix:
                    reason = "sans entrepôt" if reg.warehouse_id is None else "entrepôt supprimé"
                    self.stdout.write(
                        f"  {prefix}caisse « {reg.name} » ({reason}) → « {principal.name} »"
                    )
                    if not dry:
                        reg.warehouse = principal
                        reg.save(update_fields=['warehouse'])
                    total_registers += 1

        self.stdout.write("")
        summary = (
            f"{prefix}Terminé : {total_members} membre(s) et "
            f"{total_registers} caisse(s) normalisé(s)."
        )
        if skipped_orgs:
            summary += f" {skipped_orgs} organisation(s) ignorée(s) (aucun entrepôt)."
        self.stdout.write(self.style.SUCCESS(summary))

    @staticmethod
    def _principal_warehouse(org):
        """Entrepôt principal de l'org : ``is_default`` en priorité, sinon le
        premier entrepôt actif non supprimé, sinon n'importe lequel non supprimé."""
        qs = Warehouse.objects.filter(organization=org, is_deleted=False)
        return (
            qs.filter(is_default=True).order_by('name').first()
            or qs.filter(is_active=True).order_by('name').first()
            or qs.order_by('name').first()
        )
