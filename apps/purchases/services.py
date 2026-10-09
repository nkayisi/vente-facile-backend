"""
Services métier pour l'app Purchases.

Centralise la logique d'incrémentation / restitution du stock à la réception
de marchandises (GoodsReceipt) pour éviter la duplication entre la view
``GoodsReceiptViewSet.complete`` et tout autre point d'appel futur.

Pattern miroir de ``apps.sales.services.SaleStockService`` :
- idempotence basée sur l'existence d'un ``StockMovement`` lié au GRN ;
- atomicité via ``@transaction.atomic`` ;
- verrouillage ``select_for_update`` sur ``Stock``.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.clock import maintenant


TWO_PLACES = Decimal('0.01')


class GoodsReceiptStockService:
    """Incrémente et restitue le stock pour une réception de marchandises."""

    @staticmethod
    def is_committed(grn) -> bool:
        """True si le stock a déjà été incrémenté pour ce GRN."""
        from apps.inventory.models import StockMovement
        return StockMovement.objects.filter(
            reference_type='goods_receipt',
            reference_id=grn.id,
            movement_type='purchase',
        ).exists()

    @staticmethod
    @transaction.atomic
    def apply_increment(grn, user):
        """
        Incrémente le stock pour chaque item du GRN. Crée les lots si
        ``batch_number`` ou ``expiry_date`` est renseigné. Met à jour le coût
        moyen pondéré sur ``Stock``.

        Idempotent : ne fait rien si ``is_committed(grn)`` est vrai.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService
        from apps.inventory.services import FIFOService

        if GoodsReceiptStockService.is_committed(grn):
            return

        warehouse = grn.warehouse
        org = grn.organization

        for item in grn.items.select_related('product', 'variant').all():
            if not item.quantity_accepted or item.quantity_accepted <= 0:
                continue

            stock, _ = Stock.objects.select_for_update().get_or_create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                defaults={
                    'quantity': Decimal('0.000'),
                    'avg_cost': item.unit_cost or Decimal('0.00'),
                },
            )

            quantity_before = stock.quantity
            new_quantity = quantity_before + item.quantity_accepted

            unit_cost = item.unit_cost or Decimal('0.00')
            if new_quantity > 0 and unit_cost > 0:
                total_value = (quantity_before * stock.avg_cost) + (
                    item.quantity_accepted * unit_cost
                )
                stock.avg_cost = (total_value / new_quantity).quantize(TWO_PLACES)
            elif stock.avg_cost == 0 and unit_cost > 0:
                stock.avg_cost = unit_cost

            # Ce qui arrive scellé reste scellé : seule la part reçue hors
            # contenant vient grossir le vrac. Une réception partiellement
            # refusée voit sa part scellée replafonnée par `loose_share`.
            loose_accepted = PackagingService.loose_share(
                item.product, item.quantity_accepted, item.loose_quantity
            )
            # Recevoir 3 casiers + 12 bouteilles donne 3 casiers ET 12
            # bouteilles, pas 4 casiers : chaque canal alimente son compteur.
            PackagingService.apply_base_delta(
                stock, item.product,
                item.quantity_accepted,
                loose_hint=loose_accepted,
            )
            PackagingService.touch(stock)
            stock.save()

            paquets, vrac = PackagingService.split(
                item.quantity_accepted, loose_accepted, item.packaging_factor
            )

            batch = None
            if item.batch_number or item.expiry_date:
                batch = FIFOService.add_to_batch(
                    organization=org,
                    product=item.product,
                    warehouse=warehouse,
                    quantity=item.quantity_accepted,
                    cost_price=unit_cost,
                    batch_number=item.batch_number or None,
                    variant=item.variant,
                    expiry_date=item.expiry_date,
                    user=user,
                )

            StockMovement.objects.create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                batch=batch,
                movement_type='purchase',
                quantity=item.quantity_accepted,
                unit_cost=unit_cost,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                # ⚠ Le partage se RECONSTITUE, il ne se divise pas.
                # `loose_share` découpe au facteur du PRODUIT ; diviser par le
                # facteur FIGÉ sur la ligne donnait un nombre fractionnaire de
                # contenants dès que le marchand changeait son conditionnement
                # entre la commande et la réception - et ce nombre partait en
                # base, dans une colonne que le journal relit toujours.
                # Voir `inventory.services.receive_transfer`, même remède.
                input_package_quantity=(
                    Decimal(paquets) if item.packaging_factor else Decimal('0.000')
                ),
                input_loose_quantity=(
                    vrac if item.packaging_factor else Decimal('0.000')
                ),
                packaging_factor=item.packaging_factor,
                reference_type='goods_receipt',
                reference_id=grn.id,
                notes=f"Réception {grn.reference}",
                created_by=user,
            )

    @staticmethod
    @transaction.atomic
    def revert(grn, user):
        """
        Annule l'incrément de stock effectué par ``apply_increment`` : crée
        des mouvements ``return_out`` et décrémente ``Stock.quantity``.

        Lève ``ValidationError`` si le stock résultant deviendrait négatif et
        que ``warehouse.allow_negative_stock`` est ``False``.

        Ne fait rien si le stock n'avait pas été incrémenté.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService

        if not GoodsReceiptStockService.is_committed(grn):
            return

        warehouse = grn.warehouse
        org = grn.organization
        allow_negative = getattr(warehouse, 'allow_negative_stock', False)

        for item in grn.items.select_related('product', 'variant').all():
            if not item.quantity_accepted or item.quantity_accepted <= 0:
                continue

            stock = Stock.objects.select_for_update().filter(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
            ).first()

            if not stock:
                continue

            quantity_before = stock.quantity
            new_quantity = quantity_before - item.quantity_accepted

            if new_quantity < 0 and not allow_negative:
                raise ValidationError({
                    'items': (
                        f"Impossible d'annuler la réception : stock insuffisant pour "
                        f"{item.product.name}. Disponible: {quantity_before}, "
                        f"à retirer: {item.quantity_accepted}."
                    )
                })

            # Symétrique exact de l'entrée : on retire du vrac ce qui y était
            # entré, le reste des contenants repart scellé.
            loose_accepted = PackagingService.loose_share(
                item.product, item.quantity_accepted, item.loose_quantity
            )
            PackagingService.apply_base_delta(
                stock, item.product,
                -item.quantity_accepted,
                loose_hint=loose_accepted,
            )
            PackagingService.touch(stock)
            stock.save()

            paquets, vrac = PackagingService.split(
                item.quantity_accepted, loose_accepted, item.packaging_factor
            )

            StockMovement.objects.create(
                organization=org,
                product=item.product,
                variant=item.variant,
                warehouse=warehouse,
                movement_type='return_out',
                quantity=-item.quantity_accepted,
                unit_cost=item.unit_cost or Decimal('0.00'),
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                # ⚠ Le partage se RECONSTITUE, il ne se divise pas.
                # `loose_share` découpe au facteur du PRODUIT ; diviser par le
                # facteur FIGÉ sur la ligne donnait un nombre fractionnaire de
                # contenants dès que le marchand changeait son conditionnement
                # entre la commande et la réception - et ce nombre partait en
                # base, dans une colonne que le journal relit toujours.
                # Voir `inventory.services.receive_transfer`, même remède.
                input_package_quantity=(
                    Decimal(paquets) if item.packaging_factor else Decimal('0.000')
                ),
                input_loose_quantity=(
                    vrac if item.packaging_factor else Decimal('0.000')
                ),
                packaging_factor=item.packaging_factor,
                reference_type='goods_receipt_cancel',
                reference_id=grn.id,
                notes=f"Annulation réception {grn.reference}",
                created_by=user,
            )


# ==============================================================================
# RÉQUISITIONS DE PRODUITS (T12)
# ==============================================================================

class RequisitionRefusee(Exception):
    """Refus métier DÉTERMINISTE : le journal le traduit en `rejected`."""


def lignes_proposees(organization, warehouse, *, inclure_rupture_seule=False):
    """
    Les articles dont le stock est DÉJÀ sous leur seuil, dans cet entrepôt.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ L'ÉCRAN PROPOSE D'EMBLÉE, IL NE FAIT PAS CHERCHER.                      │
    │                                                                          │
    │ C'est la demande de T12, mot pour mot : « un écran qui propose d'emblée  │
    │ les articles dont le stock est déjà sous le seuil de réassort ». Faire   │
    │ chercher un par un dans un catalogue de plusieurs centaines d'articles   │
    │ reviendrait à demander au magasinier de refaire le travail que le seuil  │
    │ existe pour faire.                                                       │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ LE CRITÈRE EST CELUI DE `check_low_stock_alerts` ET DE `status=low`, et il
    doit l'être : un article qui déclenche une alerte et qui n'apparaîtrait pas
    ici ferait douter des deux écrans. `track_inventory` est exigé - un service
    n'a pas de seuil - et le seuil doit être RÉEL : un `reorder_point` à zéro ne
    déclenche rien, sinon tout article épuisé serait réquisitionné en
    permanence, y compris ceux qu'on ne réapprovisionne pas.

    `inclure_rupture_seule` restreint aux ruptures : c'est ce qu'on emporte au
    marché quand on n'a pas le temps de tout racheter.
    """
    from apps.inventory.models import Stock

    qs = (
        Stock.objects.filter(
            organization=organization,
            warehouse=warehouse,
            product__track_inventory=True,
            product__is_active=True,
            product__reorder_point__gt=0,
        )
        .select_related('product', 'product__unit')
        .order_by('product__name')
    )
    qs = qs.filter(quantity__lte=F('product__reorder_point'))
    if inclure_rupture_seule:
        qs = qs.filter(quantity__lte=0)

    lignes = []
    for s in qs:
        seuil = s.product.reorder_point or Decimal('0')
        dispo = s.quantity or Decimal('0')
        lignes.append({
            'product': s.product,
            'stock': dispo,
            'reorder_point': seuil,
            # ⚠ LE MANQUE EST UNE PROPOSITION, PAS UNE DÉCISION : on propose de
            # remonter au seuil, et le magasinier corrige. Lui imposer la
            # quantité ferait acheter un carton là où il sait qu'il en faut dix.
            'suggested': max(seuil - dispo, Decimal('0')),
        })
    return lignes


@transaction.atomic
def create_requisition(
    organization, *, warehouse, lignes, requested_by=None, notes='', reference=None,
):
    """
    Crée une réquisition en BROUILLON, avec son instantané de stock.

    ⚠ LE STOCK ET LE SEUIL SONT FIGÉS ICI. Les relire à l'impression donnerait
    le stock d'aujourd'hui, qui a bougé : le document ne dirait plus pourquoi la
    demande a été faite, et son lecteur ne pourrait pas juger si elle tient
    encore. Même règle que l'instantané d'une session d'inventaire.

    ⚠ L'ATTENDU EST RELEVÉ, JAMAIS SAISI. Le client envoie l'article et la
    quantité demandée ; le stock et le seuil viennent de la base. Les laisser
    saisir permettrait d'écrire une pénurie qui n'existe pas, sur un document
    qui sert à engager de l'argent.
    """
    from apps.core.numbering import PREFIX_REQUISITION, allocate_document_number
    from apps.inventory.models import Stock
    from apps.products.models import Product
    from apps.purchases.models import Requisition, RequisitionItem

    if not lignes:
        raise RequisitionRefusee("Une réquisition sans article ne demande rien.")

    requisition = Requisition.objects.create(
        organization=organization,
        warehouse=warehouse,
        # Le numéro du client est REPRIS quand il en fournit un : une
        # réquisition imprimée au fond d'un dépôt porte déjà son numéro, et le
        # remplacer ferait que le papier ne désigne plus rien.
        reference=reference or allocate_document_number(organization, PREFIX_REQUISITION),
        status=Requisition.Status.DRAFT,
        requested_by=requested_by,
        notes=notes or '',
    )

    # Un seul aller-retour pour tous les stocks ET tous les seuils concernés.
    ids = [l['product'].id if hasattr(l['product'], 'id') else l['product'] for l in lignes]
    stocks = {
        s.product_id: s
        for s in Stock.objects.filter(
            organization=organization, warehouse=warehouse, product_id__in=ids
        )
    }
    # ⚠ LE SEUIL EST RELEVÉ SUR L'ARTICLE, JAMAIS PRIS DANS LE CORPS. Mesuré
    # contre le vrai serveur : la première écriture le lisait dans la ligne
    # envoyée, que l'API ne porte pas, et l'instantané sortait à `0.000` sous
    # une proposition qui annonçait 12 - le document aurait dit que l'article
    # n'avait aucun seuil, alors que c'est son seuil qui a déclenché la demande.
    seuils = dict(
        Product.objects.filter(organization=organization, id__in=ids)
        .values_list('id', 'reorder_point')
    )

    for ligne in lignes:
        produit = ligne['product']
        pid = produit.id if hasattr(produit, 'id') else produit
        quantite = Decimal(str(ligne['quantity_requested']))
        if quantite <= 0:
            raise RequisitionRefusee(
                "Une quantité demandée doit être supérieure à zéro."
            )
        s = stocks.get(pid)
        RequisitionItem.objects.create(
            organization=organization,
            requisition=requisition,
            product_id=pid,
            quantity_requested=quantite,
            # `null` ne se lit jamais comme zéro ailleurs, mais ici l'absence de
            # ligne de stock SIGNIFIE zéro : l'article n'a jamais été reçu dans
            # ce dépôt, et c'est exactement ce qu'une réquisition constate.
            stock_at_request=(s.quantity if s else Decimal('0')),
            reorder_point_at_request=seuils.get(pid) or Decimal('0'),
            notes=ligne.get('notes') or '',
        )

    return requisition


#: Les transitions permises, et depuis quel état.
#:
#: ⚠ UNE TABLE, PAS UNE CHAÎNE DE `if`. C'est la forme retenue pour les
#: transferts, les ajustements et les inventaires : un refus devient
#: DÉTERMINISTE et se lit, au lieu de dépendre de l'ordre des conditions.
TRANSITIONS_REQUISITION = {
    'submit': ('draft', 'submitted'),
    'approve': ('submitted', 'approved'),
    'reject': ('submitted', 'rejected'),
    'cancel': (None, 'cancelled'),
}


@transaction.atomic
def transition_requisition(requisition, action, *, user=None, notes=''):
    """
    Fait avancer une réquisition, ou refuse DÉTERMINISTEMENT.

    ⚠ UN REFUS N'EST JAMAIS UN `retry`. Réessayer une approbation déjà faite
    n'aurait aucun sens, et le journal du terminal la renverrait indéfiniment -
    le défaut de l'ancienne file pris par l'autre bout.

    ⚠ `cancel` PART DE N'IMPORTE OÙ sauf d'un état terminal : on annule une
    demande qu'on n'aurait pas dû faire, et le marchand ne doit pas avoir à
    la faire approuver d'abord pour pouvoir la retirer.
    """
    from apps.purchases.models import Requisition

    if action not in TRANSITIONS_REQUISITION:
        raise RequisitionRefusee(f"Transition inconnue : {action}.")

    depart, arrivee = TRANSITIONS_REQUISITION[action]
    terminaux = {
        Requisition.Status.APPROVED,
        Requisition.Status.REJECTED,
        Requisition.Status.CANCELLED,
    }

    if requisition.status == arrivee:
        # Idempotence : le terminal a pu pousser deux fois. On rend la pièce
        # telle quelle plutôt que de refuser - un verdict d'échec ferait
        # réessayer une transition déjà appliquée.
        return requisition

    if depart is None:
        if requisition.status in terminaux:
            raise RequisitionRefusee(
                f"Cette réquisition est déjà {requisition.get_status_display().lower()}."
            )
    elif requisition.status != depart:
        raise RequisitionRefusee(
            f"Une réquisition {requisition.get_status_display().lower()} ne peut pas "
            f"passer à « {dict(Requisition.Status.choices)[arrivee].lower()} »."
        )

    requisition.status = arrivee
    champs = ['status']

    if action in ('approve', 'reject'):
        requisition.decided_by = user
        requisition.decided_at = maintenant()
        requisition.decision_notes = notes or ''
        champs += ['decided_by', 'decided_at', 'decision_notes']

    requisition.save(update_fields=champs)
    return requisition
