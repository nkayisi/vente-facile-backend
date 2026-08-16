"""
Expiration des points de fidélité arrivés à échéance.

`LoyaltyProgram.points_expiry_days` était configurable dans l'interface mais
totalement inerte : aucune tâche, aucune commande ne le lisait. Une organisation
qui réglait « 90 jours » ne voyait jamais un seul point expirer.

Modèle appliqué : **expiration par lot, FIFO**. Chaque crédit (gain, bonus,
restitution) est un lot daté ; les débits consomment les lots les plus anciens
d'abord. Ce qui reste d'un lot plus vieux que la durée configurée expire.

Propriétés :
  - **Idempotente** : la ligne ``EXPIRE`` écrite devient elle-même un débit du
    registre, un second passage ne retire donc rien de plus.
  - **Sûre pour la production** : ``--dry-run`` pour prévisualiser.
  - N'agit que sur les programmes **actifs** dont ``points_expiry_days > 0`` :
    désactiver le programme gèle les points plutôt que de les faire fondre.

Usage :
    python manage.py expire_loyalty_points --dry-run
    python manage.py expire_loyalty_points
    python manage.py expire_loyalty_points --organization <uuid>
"""
from django.core.management.base import BaseCommand

from apps.organizations.models import Organization
from apps.settings.services import LoyaltyExpiryService


class Command(BaseCommand):
    help = "Retire les points de fidélité arrivés à échéance (FIFO par lot)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Prévisualise sans rien modifier.",
        )
        parser.add_argument(
            '--organization',
            type=str,
            default='',
            help="Limite le traitement à une organisation (UUID).",
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        org_id = options['organization'].strip()

        if dry_run:
            self.stdout.write(self.style.WARNING('Mode simulation : aucune écriture.'))

        if org_id:
            organization = Organization.objects.filter(id=org_id).first()
            if not organization:
                self.stderr.write(self.style.ERROR(f"Organisation {org_id} introuvable."))
                return
            reports = [
                LoyaltyExpiryService.expire_for_organization(organization, dry_run=dry_run)
            ]
        else:
            reports = LoyaltyExpiryService.expire_all(dry_run=dry_run)

        touched = [r for r in reports if r['points']]

        if not touched:
            self.stdout.write(self.style.SUCCESS(
                "\nAucun point à expirer."
            ))
            return

        for report in touched:
            self.stdout.write(
                f"\n{report['organization'].name} : "
                f"{report['points']} point(s) sur {report['accounts']} compte(s)"
            )
            for customer_name, points in report['details']:
                self.stdout.write(f"  {customer_name} : {points} pts")

        total = sum(r['points'] for r in touched)
        accounts = sum(r['accounts'] for r in touched)
        verb = "à expirer" if dry_run else "expirés"
        self.stdout.write(self.style.SUCCESS(
            f"\nTotal : {total} point(s) {verb} sur {accounts} compte(s)."
        ))
