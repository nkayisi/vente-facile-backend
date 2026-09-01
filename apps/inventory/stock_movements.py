"""
L'écriture d'un mouvement de stock, partagée par la vue et par le journal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE CORPS VIVAIT DANS UN `perform_create`, DONC HORS D'ATTEINTE DU TERMINAL.  │
│                                                                              │
│ `stock_movement.create` appelait `serializer.save()` en direct. Résultat     │
│ mesuré : `quantity_before` reste nul, la contrainte NOT NULL rejette         │
│ l'insertion, l'opération part en quarantaine - et le stock n'a jamais bougé. │
│ Depuis le lot 7, aucune entrée de stock saisie sur un terminal n'est arrivée.│
│                                                                              │
│ Ce n'est pas le mouvement qui compte, c'est son EFFET : verrou sur la ligne  │
│ de stock, lot FIFO à l'entrée, consommation FIFO à la sortie, coût moyen     │
│ pondéré, partage scellé/vrac. Un mouvement écrit sans que `Stock` bouge est  │
│ pire qu'un mouvement refusé : l'historique dit qu'on a reçu dix bouteilles,  │
│ et le rayon n'en sait rien.                                                  │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.warehouse_scope import assert_warehouse_allowed_for_request

from .models import Stock

#: Sorties dont les lots se consomment en FIFO.
SORTIES_FIFO = [
    'sale', 'damage', 'expired', 'transfer_out', 'adjustment_out', 'production_out',
]


@transaction.atomic
def create_stock_movement(serializer, *, organization, user, request=None, **extra):
    """
    Écrit le mouvement ET son effet sur le stock. Appelé par la vue ET le journal.

    ``request`` sert au seul contrôle de périmètre entrepôt ; il est exigé dès
    qu'il est fourni, comme la vue l'exige (l'entrepôt n'est jamais facultatif
    sur un mouvement).
    """
    from .models import STOCK_IN_MOVEMENT_TYPES
    from .packaging import PackagingService
    from .services import FIFOService

    data = serializer.validated_data
    if request is not None:
        assert_warehouse_allowed_for_request(request, data['warehouse'].id)

    produit = data['product']
    cout_catalogue = produit.cost_price if produit.cost_price else Decimal('0.00')
    stock, cree = Stock.objects.select_for_update().get_or_create(
        organization=organization,
        product=produit,
        variant=data.get('variant'),
        warehouse=data['warehouse'],
        defaults={'quantity': Decimal('0.000'), 'avg_cost': cout_catalogue},
    )

    quantite_avant = stock.quantity
    cout_unitaire = data.get('unit_cost') or Decimal('0.00')
    type_mouvement = data.get('movement_type', '')

    # Un stock existant sans coût moyen démarre sur celui du catalogue.
    if not cree and stock.avg_cost == 0 and cout_catalogue > 0:
        stock.avg_cost = cout_catalogue

    lot = data.get('batch')
    if data['quantity'] > 0 and type_mouvement in STOCK_IN_MOVEMENT_TYPES:
        lot = FIFOService.add_to_batch(
            organization=organization,
            product=produit,
            warehouse=data['warehouse'],
            quantity=data['quantity'],
            cost_price=cout_unitaire if cout_unitaire > 0 else cout_catalogue,
            batch_number=None,  # auto-généré par le service
            variant=data.get('variant'),
            location=data.get('location'),
            expiry_date=data.get('expiry_date'),
            notes=data.get('notes', ''),
            user=user,
        )
    elif data['quantity'] < 0 and type_mouvement in SORTIES_FIFO:
        allocations, _reste = FIFOService.consume_from_batches(
            organization=organization,
            product=produit,
            warehouse=data['warehouse'],
            quantity=abs(data['quantity']),
            variant=data.get('variant'),
            reference_type=type_mouvement,
            reference_id=data.get('reference_id'),
            user=user,
            notes=data.get('notes', ''),
            exclude_expired=(type_mouvement != 'expired'),
            use_fefo=getattr(produit, 'has_expiry_date', False),
        )
        if allocations:
            lot = allocations[0].batch

    # Coût moyen pondéré, sur les seules entrées valorisées.
    if data['quantity'] > 0 and cout_unitaire > 0:
        if stock.quantity > 0:
            valeur_existante = stock.quantity * stock.avg_cost
            valeur_entrante = data['quantity'] * cout_unitaire
            stock.avg_cost = (
                (valeur_existante + valeur_entrante) / (stock.quantity + data['quantity'])
            ).quantize(Decimal('0.01'))
        else:
            stock.avg_cost = cout_unitaire

    # Part de la saisie exprimée à l'unité : elle alimente (ou prélève) le vrac.
    # Une entrée en conditionnements entiers laisse le vrac inchangé, puisque
    # les emballages arrivent scellés.
    delta_vrac = data.get('input_loose_quantity')
    if delta_vrac is None:
        # Quantité simple : sans indication de conditionnement, elle porte sur
        # des unités hors emballage.
        PackagingService.apply_base_delta(stock, produit, data['quantity'])
    else:
        # « X contenants + Y unités » : chaque part va dans son compteur, sans
        # jamais se convertir dans l'autre.
        signe = -1 if data['quantity'] < 0 else 1
        delta_contenants = data.get('input_package_quantity') or Decimal('0.000')
        PackagingService.apply_delta(
            stock, produit,
            delta_packages=signe * abs(delta_contenants),
            delta_loose=signe * abs(delta_vrac),
        )
    stock.last_movement_at = timezone.now()
    stock.save()

    mouvement = serializer.save(
        organization=organization,
        batch=lot,
        quantity_before=quantite_avant,
        quantity_after=stock.quantity,
        created_by=user,
        **extra,
    )

    # Report des prix sur la fiche produit, EN DERNIER et dans la même
    # transaction. En dernier parce que l'initialisation d'`avg_cost` ci-dessus
    # lit `product.cost_price` : écrire la fiche avant ferait démarrer un stock
    # neuf au nouveau prix au lieu de l'ancien.
    prix = serializer.validated_data.get('_product_prices')
    if prix:
        from apps.products.models import Product
        from apps.products.pricing import ProductPricingService

        # Verrou pris APRÈS celui du stock : ordre d'acquisition constant,
        # sinon deux approvisionnements simultanés peuvent s'interbloquer.
        verrouille = Product.objects.select_for_update().get(pk=produit.pk)
        ProductPricingService.apply(verrouille, prix)

    return mouvement
