"""
Sortir le stock des ventes émises avant que l'émission ne le décrémente.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE CORRECTIF NE RÉPARE PAS LE PASSÉ.                                         │
│                                                                              │
│ Le décrément était gardé par `status == 'completed'`, et le statut ne vient   │
│ que du montant payé : toute vente à crédit émise avant le correctif a laissé │
│ son stock intact. La marchandise est partie, le rayon la montre toujours.    │
│                                                                              │
│ Ces lignes ne se rattrapent pas toutes seules : `apply_payment_to_sale` ne   │
│ décrémente qu'au BASCULEMENT vers `completed`, donc une vente qui ne sera    │
│ jamais soldée ne sortira jamais de stock.                                    │
└──────────────────────────────────────────────────────────────────────────────┘

Idempotente par construction : `SaleStockService.apply_decrement` sort sur
`is_committed()`, qui cherche un `StockMovement` de vente sur cette vente. La
relancer ne peut donc pas décrémenter deux fois.

⚠ `--dry-run` D'ABORD. Le volume de production est inconnu, et chaque ligne
touchée écrit un mouvement de stock, donc une écriture comptable qui se
contrepasse mais ne se rature pas.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.inventory.models import StockMovement
from apps.sales.models import Sale
from apps.sales.services import SaleStockService


class Command(BaseCommand):
    help = "Décrémente le stock des ventes émises qui ne l'ont jamais fait."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Montre ce qui serait décrémenté, sans rien écrire.",
        )
        parser.add_argument(
            '--organization', default=None,
            help="Limite à une organisation (son identifiant).",
        )

    def handle(self, *args, **options):
        sec = options['dry_run']

        # Les ventes qui ENGAGENT la marchandise. `draft` n'engage rien, et une
        # vente annulée a déjà été contrepassée - la décrémenter la sortirait
        # une seconde fois, sans contrepartie.
        qs = Sale.objects.exclude(status__in=('draft', 'cancelled')).filter(
            warehouse__isnull=False,
        ).select_related('organization', 'warehouse').order_by('sale_date')

        if options['organization']:
            qs = qs.filter(organization_id=options['organization'])

        # Un seul aller-retour pour savoir lesquelles ont déjà leur mouvement :
        # `is_committed` par vente ferait une requête par ligne.
        deja = set(
            StockMovement.objects.filter(
                reference_type='sale', movement_type='sale',
            ).values_list('reference_id', flat=True)
        )

        a_faire = [v for v in qs if str(v.id) not in deja and v.id not in deja]

        if not a_faire:
            self.stdout.write(self.style.SUCCESS(
                "Aucune vente à rattraper : tout le stock émis est déjà sorti."
            ))
            return

        self.stdout.write(f"{len(a_faire)} vente(s) à rattraper :")
        for v in a_faire:
            lignes = v.items.count()
            self.stdout.write(
                f"  {v.reference}  {v.status:16s} {v.organization.name}"
                f"  ({lignes} ligne(s), dû {v.amount_due})"
            )

        if sec:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run : rien n'a été écrit. Relancez sans l'option pour appliquer."
            ))
            return

        faits, refus = 0, []
        for v in a_faire:
            # Chaque vente dans SA transaction : une ligne qui refuse - stock
            # négatif interdit sur son entrepôt - ne doit pas condamner les
            # autres. C'est la doctrine du journal de synchronisation.
            try:
                with transaction.atomic():
                    SaleStockService.apply_decrement(v, v.sold_by)
                faits += 1
            except Exception as exc:  # noqa: BLE001 - on NOMME et on continue
                refus.append((v.reference, str(exc)[:120]))

        self.stdout.write(self.style.SUCCESS(f"\n{faits} vente(s) rattrapée(s)."))
        for ref, motif in refus:
            self.stdout.write(self.style.ERROR(f"  REFUSÉ {ref} : {motif}"))
