"""
Remet à zéro les rayons que l'ancien découvert a laissés négatifs.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN RAYON NÉGATIF NE VEND PLUS RIEN, ET IL NE SE RÉPARE PAS TOUT SEUL.        │
│                                                                              │
│ Depuis que le découvert n'existe plus, toute sortie d'un rayon à -5 est      │
│ refusée : la vente suivante de cet article échoue au comptoir. Une réception │
│ le ferait remonter, mais seulement de ce qu'elle apporte - un rayon à -40    │
│ qui reçoit 30 bouteilles reste invendable.                                   │
│                                                                              │
│ La reprise écrit un mouvement `adjustment_in` du manquant, motif « Reprise : │
│ fin du stock négatif ». C'est une écriture comptable qui se contrepasse et   │
│ ne se rature pas, d'où la simulation par défaut.                             │
│                                                                              │
│ ⚠ UN ARTICLE SOUS INVENTAIRE N'EST PAS TOUCHÉ : le comptage relève le rayon  │
│ tel qu'il est, et le remettre à zéro pendant qu'on le compte fausserait      │
│ l'écart. Il est nommé ; la validation de l'inventaire le corrigera.          │
└──────────────────────────────────────────────────────────────────────────────┘

SIMULATION PAR DÉFAUT. `--appliquer` pour écrire ; `--dry-run` est accepté et
ne fait que confirmer le défaut. Idempotente : relancée, elle ne trouve plus rien.

Usage :
    python manage.py reparer_stocks_negatifs
    python manage.py reparer_stocks_negatifs --appliquer
    python manage.py reparer_stocks_negatifs --organization <uuid> --appliquer
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import InventorySession, Stock, StockMovement
from apps.inventory.packaging import PackagingService

MOTIF = 'Reprise : fin du stock négatif'
ZERO = Decimal('0.000')


def rayons_negatifs(organization=None):
    qs = (
        Stock.objects.filter(quantity__lt=0)
        .select_related('organization', 'product', 'warehouse')
        .order_by('organization__name', 'warehouse__name', 'product__name')
    )
    if organization:
        qs = qs.filter(organization_id=organization)
    return qs


def _verrouilles(rayons):
    """`{(organisation, entrepôt): {articles}}` sous inventaire, une requête par couple."""
    couples = {(r.organization_id, r.warehouse_id): r.organization for r in rayons}
    return {
        (org_id, wh_id): set(
            InventorySession.get_all_locked_product_ids(org, warehouse_ids=[wh_id])
        )
        for (org_id, wh_id), org in couples.items()
    }


@transaction.atomic
def remettre_a_zero(stock_id):
    """Remonte un rayon à zéro sous verrou ; rend le mouvement, ou `None` s'il ne l'est plus."""
    stock = Stock.objects.select_for_update().select_related('product').get(pk=stock_id)
    if stock.quantity >= 0:
        return None
    avant = stock.quantity
    manquant = -avant
    # Une entrée va au vrac : on ne rescelle jamais (`apply_base_delta`).
    PackagingService.apply_base_delta(stock, stock.product, manquant)
    stock.last_movement_at = timezone.now()
    stock.save()
    return StockMovement.objects.create(
        organization=stock.organization,
        product=stock.product,
        variant=stock.variant,
        warehouse=stock.warehouse,
        movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
        quantity=manquant,
        quantity_before=avant,
        quantity_after=stock.quantity,
        reference_type='reprise_stock_negatif',
        notes=MOTIF,
    )


class Command(BaseCommand):
    help = (
        "Remet à zéro les rayons négatifs laissés par l'ancien découvert. "
        "Simulation par défaut ; --appliquer pour écrire."
    )

    def add_arguments(self, parser):
        parser.add_argument('--appliquer', action='store_true', help='Écrire en base.')
        parser.add_argument('--dry-run', action='store_true', help='Simulation (le défaut).')
        parser.add_argument('--organization', default=None, help='Limiter à un établissement.')

    def handle(self, *args, **options):
        if options['appliquer'] and options['dry_run']:
            raise CommandError("`--appliquer` et `--dry-run` s'excluent.")
        appliquer = options['appliquer']

        rayons = list(rayons_negatifs(options['organization']))
        if not rayons:
            self.stdout.write(self.style.SUCCESS("Aucun rayon négatif."))
            return

        verrous = _verrouilles(rayons)
        a_faire, sous_inventaire = [], []
        for r in rayons:
            if r.product_id in verrous[(r.organization_id, r.warehouse_id)]:
                sous_inventaire.append(r)
            else:
                a_faire.append(r)

        for r in a_faire:
            self.stdout.write(
                f"{r.organization.name} · {r.warehouse.name} · {r.product.name} : "
                f"{PackagingService.format_base_total(r.product, r.quantity)} -> 0"
            )
        for r in sous_inventaire:
            self.stdout.write(self.style.WARNING(
                f"{r.organization.name} · {r.warehouse.name} · {r.product.name} : "
                "sous inventaire, laissé tel quel (la validation du comptage le corrigera)."
            ))

        if appliquer:
            faits = sum(1 for r in a_faire if remettre_a_zero(r.id) is not None)
        else:
            faits = len(a_faire)

        rayons_mot = 'rayon' if faits < 2 else 'rayons'
        if appliquer:
            verbe, fin = 'remis', ''
        else:
            verbe = 'serait remis' if faits < 2 else 'seraient remis'
            fin = " (simulation : rien n'a été écrit, --appliquer pour écrire)"
        self.stdout.write(self.style.SUCCESS(
            f"{faits} {rayons_mot} {verbe} à zéro{fin} ; sous inventaire : {len(sous_inventaire)}."
        ))
