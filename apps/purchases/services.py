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
from apps.contacts import supplier_debt
from apps.settings.services import CurrencyService


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

# ==============================================================================
# DETTE FOURNISSEUR
# ==============================================================================

def valeur_recue(grn, *, inclure_port=None):
    """
    Ce que la réception ``grn`` nous fait devoir, dans la devise de la commande.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ ON PART DU COÛT DE LA RÉCEPTION, PAS DU PRIX DE LA COMMANDE.            │
    │                                                                          │
    │ `GoodsReceiptItem.unit_cost` est ce que le fournisseur a RÉELLEMENT      │
    │ facturé - c'est d'ailleurs lui, et pas le prix commandé, qui pilote le   │
    │ coût moyen pondéré du rayon. Un prix qui a bougé entre la commande et la │
    │ livraison est le cas ordinaire ; se fier au prix commandé ferait devoir  │
    │ un montant que la facture posée sur le comptoir ne porte pas.            │
    │                                                                          │
    │ Les conditions COMMERCIALES de la ligne (remise, taxe), elles, viennent  │
    │ de la commande : elles ont été négociées là, et la réception ne les      │
    │ reporte pas.                                                             │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ ``shipping_cost`` est porté par l'ENVELOPPE de la commande et n'est pas
    divisible : il s'inscrit avec la PREMIÈRE réception validée, et une seule
    fois. Le répartir au prorata demanderait une règle d'imputation que
    personne n'a spécifiée, et l'omettre laisserait la dette courte du montant
    du transport, pour toujours.

    ⚠ Ce calcul est une RECONSTRUCTION, et la limite est assumée : le total
    d'une facture fournisseur peut porter des lignes qu'aucune commande ne
    prévoyait. L'écart se corrige depuis la fiche du fournisseur
    (``supplier_debt.adjust_balance``), qui existe pour cela.

    ``inclure_port`` n'est à passer qu'en test : laissé à ``None``, il est
    déduit de l'existence d'une autre réception déjà validée.
    """
    from .models import GoodsReceipt

    po = grn.purchase_order
    total = Decimal('0.00')

    lignes = grn.items.select_related('purchase_order_item').all()
    for item in lignes:
        acceptee = item.quantity_accepted or Decimal('0.000')
        if acceptee <= 0:
            continue

        brut = acceptee * (item.unit_cost or Decimal('0.00'))

        po_item = item.purchase_order_item
        remise = po_item.discount_percentage if po_item else Decimal('0.00')
        taxe = po_item.tax_rate if po_item else Decimal('0.00')

        imposable = brut - brut * (remise / 100)
        total += imposable + imposable * (taxe / 100)

    if inclure_port is None:
        inclure_port = not GoodsReceipt.objects.filter(
            purchase_order=po,
            status=GoodsReceipt.Status.COMPLETED,
        ).exclude(pk=grn.pk).exists()
    if inclure_port:
        total += po.shipping_cost or Decimal('0.00')

    return total.quantize(TWO_PLACES)


class TransitionReceptionRefusee(Exception):
    """Refus métier déterministe sur une réception. Ne jamais réessayer."""


@transaction.atomic
def completer_reception(grn, user):
    """
    Valide une réception : le stock entre, la commande avance, la dette s'inscrit.

    Corps unique, appelé par la vue. Il l'était auparavant écrit DANS la vue, et
    trois règles y avaient déjà dérivé (voir les encadrés ci-dessous). Le
    descendre ici est ce qui rend la parité structurelle le jour où un second
    appelant existera, plutôt que surveillée.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `quantity_received` S'INCRÉMENTE ICI, ET NON À LA CRÉATION.             │
    │                                                                          │
    │ `GoodsReceiptCreateSerializer` le faisait dès le BROUILLON : une         │
    │ réception saisie puis oubliée gonflait `quantity_pending` pour toujours,  │
    │ et la commande s'annonçait reçue alors que rien n'était entré en rayon.  │
    │ Le stock, le statut de la commande et la dette bougent tous à la         │
    │ validation : la quantité reçue n'avait aucune raison d'être seule à      │
    │ bouger avant.                                                            │
    └──────────────────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE STATUT SE DÉCIDE PAR ARTICLE, JAMAIS SUR UNE SOMME.                  │
    │                                                                          │
    │ La vue comparait `Sum(quantity_received)` à `Sum(quantity_ordered)` :    │
    │ un article livré en excédent compensait un article qui attendait         │
    │ encore, la commande passait `received`, et la ligne manquante            │
    │ disparaissait de « commandes à réceptionner ». C'est la règle que        │
    │ l'inventaire a déjà posée - « soumettre exige que TOUT soit compté ».    │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from .models import GoodsReceipt, PurchaseOrder

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ LA RÉCEPTION SE VERROUILLE AVANT D'ÊTRE LUE, ET CE N'EST PAS DU ZÈLE.   │
    # │                                                                          │
    # │ Sans ce verrou, deux appels concurrents - un double appui, un renvoi     │
    # │ réseau, deux onglets - franchissaient TOUS DEUX le contrôle de statut    │
    # │ ci-dessous ET l'idempotence de `apply_increment` (qui cherche un         │
    # │ `StockMovement` qu'aucune des deux transactions n'a encore commité).     │
    # │ Le stock entrait donc DEUX FOIS, le coût moyen pondéré était recalculé   │
    # │ deux fois, et la dette du fournisseur inscrite deux fois - sur le seul   │
    # │ geste de tout ce module que l'écran annonce comme irréversible.          │
    # │                                                                          │
    # │ Le verrou porte sur la LIGNE de la réception : deux réceptions           │
    # │ différentes de la même commande ne se bloquent pas mutuellement.         │
    # └──────────────────────────────────────────────────────────────────────────┘
    verrouillee = GoodsReceipt.objects.select_for_update().filter(pk=grn.pk).first()
    if verrouillee is None:
        raise TransitionReceptionRefusee("Cette réception est introuvable.")
    grn.status = verrouillee.status

    if grn.status != GoodsReceipt.Status.DRAFT:
        raise TransitionReceptionRefusee(
            "Cette réception est déjà validée."
        )

    po = grn.purchase_order

    for item in grn.items.select_related('purchase_order_item').all():
        po_item = item.purchase_order_item
        if po_item and item.quantity_accepted:
            po_item.quantity_received += item.quantity_accepted
            po_item.save(update_fields=['quantity_received', 'updated_at'])

    GoodsReceiptStockService.apply_increment(grn, user)

    # La dette est calculée AVANT de poser le statut : `valeur_recue` déduit
    # l'imputation du port de l'absence d'une AUTRE réception validée, et la
    # réception courante ne doit pas se compter elle-même.
    montant = valeur_recue(grn)

    reste = po.items.filter(
        quantity_received__lt=F('quantity_ordered')
    ).exists()
    po.status = (
        PurchaseOrder.Status.PARTIALLY_RECEIVED if reste
        else PurchaseOrder.Status.RECEIVED
    )
    po.save(update_fields=['status', 'updated_at'])

    grn.status = GoodsReceipt.Status.COMPLETED
    grn.save(update_fields=['status', 'updated_at'])

    if montant > 0:
        supplier_debt.apply_debt(
            po.supplier, montant,
            currency=po.currency,
            exchange_rate=po.exchange_rate,
            purchase_order=po,
            goods_receipt=grn,
            reference=grn.supplier_invoice or grn.reference,
            notes=f"Réception {grn.reference}",
            user=user,
        )

    return grn


@transaction.atomic
def annuler_reception(grn, user=None):
    """
    Annule une réception encore en brouillon.

    ⚠ Elle NE TOUCHE PLUS à `quantity_received`. La vue le décrémentait, ce qui
    était la contrepartie de l'incrément fait à la création : les deux étaient
    faux ensemble, et les retirer ensemble est ce qui garde la commande juste.
    Un brouillon n'a rien avancé, il n'y a rien à défaire.

    Une réception VALIDÉE ne s'annule pas par ici : son stock est entré, son
    coût moyen pondéré a bougé et sa dette est inscrite. `GoodsReceiptStockService.revert`
    porte ce chemin, et il n'a pas encore d'appelant - c'est écrit, pas oublié.
    """
    from .models import GoodsReceipt

    if grn.status != GoodsReceipt.Status.DRAFT:
        raise TransitionReceptionRefusee(
            "Seules les réceptions en brouillon peuvent être annulées."
        )

    grn.status = GoodsReceipt.Status.CANCELLED
    grn.save(update_fields=['status', 'updated_at'])
    return grn


@transaction.atomic
def approuver_retour(purchase_return, user=None):
    """
    Approuve un retour : la marchandise sort du rayon.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `select_for_update` SANS `atomic` EXPLICITE REND 500 SUR POSTGRESQL.    │
    │                                                                          │
    │ Ce corps vivait dans la vue, où il verrouillait `Stock` hors de toute    │
    │ transaction : PostgreSQL lève `TransactionManagementError`, donc 500,    │
    │ et le marchand voyait son approbation échouer sans savoir pourquoi.      │
    │                                                                          │
    │ ⚠ AUCUN TEST NE POUVAIT L'ATTRAPER : `ATOMIC_REQUESTS` est à `False`    │
    │ (et doit y rester, le journal de synchronisation en dépend), tandis que  │
    │ `APITestCase` ouvre sa PROPRE transaction - le verrou y passe donc       │
    │ toujours. Le défaut ne se voit que contre un vrai serveur. C'est le      │
    │ piège exact qui a coûté les trois boutons « Payer » du back-office en    │
    │ août, sur `apply_payment_to_sale`.                                       │
    │                                                                          │
    │ RÈGLE : tout `select_for_update` vit sous un `@transaction.atomic`       │
    │ explicite.                                                               │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from apps.inventory.models import Stock, StockMovement
    from apps.inventory.packaging import PackagingService

    from .models import PurchaseReturn

    if purchase_return.status != PurchaseReturn.Status.DRAFT:
        raise TransitionReceptionRefusee(
            "Seuls les retours en brouillon peuvent être approuvés."
        )

    for item in purchase_return.items.select_related('product').all():
        stock = Stock.objects.select_for_update().filter(
            organization=purchase_return.organization,
            product=item.product,
            variant=item.variant,
            warehouse=purchase_return.warehouse,
        ).first()
        if not stock:
            continue

        quantity_before = stock.quantity
        # ⚠ On ne touche PAS `stock.quantity` directement : cela rongeait
        # silencieusement le nombre de contenants scellés. Retirer des unités
        # puise d'abord dans le vrac, puis ouvre un contenant - on ne rescelle
        # jamais.
        PackagingService.apply_base_delta(stock, item.product, -item.quantity)
        PackagingService.touch(stock)
        stock.save()

        StockMovement.objects.create(
            organization=purchase_return.organization,
            product=item.product,
            variant=item.variant,
            warehouse=purchase_return.warehouse,
            batch=item.batch,
            movement_type='return_out',
            quantity=-item.quantity,
            unit_cost=item.unit_cost,
            quantity_before=quantity_before,
            quantity_after=stock.quantity,
            reference_type='purchase_return',
            reference_id=purchase_return.id,
            notes=f"Retour fournisseur {purchase_return.reference}",
            created_by=user,
        )

    purchase_return.status = PurchaseReturn.Status.APPROVED
    purchase_return.save(update_fields=['status', 'updated_at'])
    return purchase_return


@transaction.atomic
def expedier_retour(purchase_return, user=None):
    """
    Expédie un retour approuvé : la dette s'éteint d'abord, le reliquat revient
    en caisse.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN RETOUR FOURNISSEUR N'EST PAS UNE RENTRÉE D'ARGENT.                   │
    │                                                                          │
    │ La vue enregistrait une ENTRÉE de caisse du montant entier, puis         │
    │ décrémentait le solde du fournisseur : le tiroir recevait donc un argent │
    │ que personne n'avait apporté, et le même retour comptait deux fois. Dans │
    │ le cas ordinaire, le fournisseur ne rend rien : il crédite le compte.    │
    │                                                                          │
    │ C'est la règle déjà posée sur le retour de VENTE, prise par l'autre      │
    │ bout : « un retour sur facture encore due éteint d'abord la dette, seul  │
    │ le reliquat sort en espèces ».                                            │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ Le reliquat est calculé dans la devise du RETOUR, qui est celle de la
    commande d'origine quand il y en a une. Comparer un montant de retour à un
    solde tenu dans une autre devise ferait rendre en espèces une dette qui
    existe pourtant.
    """
    from .models import PurchaseReturn

    if purchase_return.status != PurchaseReturn.Status.APPROVED:
        raise TransitionReceptionRefusee(
            "Seuls les retours approuvés peuvent être expédiés."
        )

    purchase_return.status = PurchaseReturn.Status.SHIPPED
    purchase_return.save(update_fields=['status', 'updated_at'])

    montant = purchase_return.total_amount or Decimal('0.00')
    if montant <= 0:
        return purchase_return

    po = purchase_return.purchase_order
    devise = po.currency if po else None
    taux = po.exchange_rate if po else None

    devise_resolue, taux_resolu = CurrencyService.resolve(
        purchase_return.organization, devise, taux,
    )
    du = supplier_debt.get_balance(purchase_return.supplier, devise_resolue)

    # Ce qui éteint de la dette, et ce qui dépasse.
    eteint = min(montant, du) if du > 0 else Decimal('0.00')
    reliquat = montant - eteint

    if eteint > 0:
        supplier_debt.apply_return_credit(
            purchase_return.supplier, eteint,
            currency=devise_resolue,
            exchange_rate=taux_resolu,
            purchase_order=po,
            reference=purchase_return.reference,
            notes=f"Retour {purchase_return.reference}",
            user=user,
        )

    if reliquat > 0:
        from apps.cashbook.services import record_purchase_return_refund
        record_purchase_return_refund(
            organization=purchase_return.organization,
            purchase_return=purchase_return,
            amount=reliquat,
            supplier=purchase_return.supplier,
            currency=devise_resolue,
            exchange_rate=taux_resolu,
            user=user,
        )

    return purchase_return


def _imputer_commandes(po_queryset, montant):
    """
    Impute un montant sur des commandes ouvertes, de la plus ancienne à la plus
    récente, et renvoie ``[(commande, part), ...]`` plus le reliquat.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN RÈGLEMENT SANS IMPUTATION FAIT DIVERGER DEUX VÉRITÉS.                │
    │                                                                          │
    │ Sans elle, le solde du fournisseur baisse et chaque                      │
    │ `PurchaseOrder.amount_due` reste intact : la somme des commandes dues ne │
    │ correspond plus au solde qu'elle est censée résumer, et le marchand ne   │
    │ peut plus savoir laquelle de ses commandes il a payée. Le côté client a  │
    │ déjà payé exactement cela - `record-advance` a été rendu alias de        │
    │ `record-payment` pour cette raison.                                      │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    reste = Decimal(montant)
    parts = []
    for po in po_queryset:
        if reste <= 0:
            break
        du = po.amount_due or Decimal('0.00')
        if du <= 0:
            continue
        part = min(du, reste)
        parts.append((po, part))
        reste -= part
    return parts, reste


@transaction.atomic
def enregistrer_reglement(
    organization, supplier, amount, *, payment_date, currency=None,
    exchange_rate=None, payment_method=None, payment_reference='',
    notes='', allocations=None, user=None,
):
    """
    Enregistre un règlement fournisseur : la dette baisse, les commandes
    s'imputent, le tiroir sort l'argent.

    Corps unique. Il était écrit dans `SupplierPaymentCreateSerializer.create`,
    où il portait quatre défauts : l'organisation lue dans l'EN-TÊTE HTTP (donc
    500 sans en-tête, et aucun chemin de journal possible), un générateur de
    référence trié ALPHABÉTIQUEMENT et daté en UTC, une écriture inline du
    solde fournisseur, et aucune imputation par défaut.

    ``allocations`` est une liste de ``{'purchase_order': po, 'amount': x}``.
    Vide ou absente, l'imputation se fait de la plus ancienne commande ouverte
    à la plus récente (voir `_imputer_commandes`).
    """
    from apps.cashbook.services import record_purchase_payment
    from apps.core.numbering import PREFIX_SUPPLIER_PAYMENT, allocate_document_number

    from .models import PurchaseOrder, SupplierPayment, SupplierPaymentAllocation

    montant = Decimal(amount).quantize(TWO_PLACES)
    if montant <= 0:
        raise ValidationError({'amount': "Le montant doit être supérieur à zéro."})

    paiement = SupplierPayment.objects.create(
        organization=organization,
        supplier=supplier,
        amount=montant,
        currency=currency or '',
        exchange_rate=exchange_rate or Decimal('1'),
        payment_method=payment_method,
        payment_reference=payment_reference,
        payment_date=payment_date,
        notes=notes,
        status=SupplierPayment.Status.COMPLETED,
        reference=allocate_document_number(organization, PREFIX_SUPPLIER_PAYMENT),
        created_by=user,
    )

    parts = [
        (a['purchase_order'], Decimal(a['amount']).quantize(TWO_PLACES))
        for a in (allocations or [])
    ]
    if not parts:
        # ⚠ Bornée à la devise du règlement : imputer un versement en francs
        # sur une commande libellée en dollars écrirait un `amount_paid` dans
        # une monnaie qui n'est pas la sienne.
        ouvertes = PurchaseOrder.objects.select_for_update().filter(
            organization=organization,
            supplier=supplier,
            currency=paiement.currency,
            amount_due__gt=0,
        ).exclude(
            status__in=[PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.DRAFT]
        ).order_by('order_date', 'reference')
        parts, _ = _imputer_commandes(ouvertes, montant)

    for po, part in parts:
        SupplierPaymentAllocation.objects.create(
            organization=organization,
            payment=paiement,
            purchase_order=po,
            amount=part,
        )
        po.amount_paid = (po.amount_paid or Decimal('0.00')) + part
        po.amount_due = (po.total or Decimal('0.00')) - po.amount_paid
        po.save(update_fields=['amount_paid', 'amount_due', 'updated_at'])

    premiere = parts[0][0] if parts else None

    supplier_debt.settle_debt(
        supplier, montant,
        currency=paiement.currency,
        exchange_rate=paiement.exchange_rate,
        purchase_order=premiere,
        supplier_payment=paiement,
        reference=payment_reference,
        notes=notes,
        user=user,
    )

    record_purchase_payment(
        organization=organization,
        purchase_order=premiere,
        amount=montant,
        supplier=supplier,
        # La sortie de caisse est dans la devise réellement décaissée.
        # `SupplierPayment.save()` l'a résolue vers une devise activée de
        # l'organisation, elle est donc fiable.
        currency=paiement.currency,
        exchange_rate=paiement.exchange_rate,
        user=user,
    )

    return paiement


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
    organization, *, warehouse, lignes, requested_by=None, notes='',
    reference=None, requisition_id=None,
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

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `requisition_id` : L'IDENTIFIANT DU TERMINAL EST HONORÉ.                │
    │                                                                          │
    │ MESURÉ sur le vrai serveur : sans lui, la pièce naissait sous un UUID    │
    │ neuf. Le terminal, qui route sa fiche et adresse ses transitions par     │
    │ l'identifiant qu'il a tiré, se retrouvait alors avec une réquisition     │
    │ qu'il ne pouvait plus désigner : sa propre fiche la déclarait            │
    │ introuvable, et un `requisition.submit` mis en file juste après la        │
    │ création revenait « Réquisition introuvable » - un refus DÉTERMINISTE,   │
    │ donc une quarantaine, pour un geste parfaitement légitime.               │
    │                                                                          │
    │ C'est le motif de `session_id` sur `open_register_session`, et la même   │
    │ forme : `**({'id': …} if … else {})`, pour que la vue - qui n'en fournit  │
    │ aucun - garde l'allocation de la base.                                   │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from apps.core.numbering import PREFIX_REQUISITION, allocate_document_number
    from apps.inventory.models import Stock
    from apps.products.models import Product
    from apps.purchases.models import Requisition, RequisitionItem

    if not lignes:
        raise RequisitionRefusee("Une réquisition sans article ne demande rien.")

    requisition = Requisition.objects.create(
        **({'id': requisition_id} if requisition_id else {}),
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

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ L'ARTICLE DOIT APPARTENIR À L'ÉTABLISSEMENT, ET LE CONTRÔLE EST ICI.    │
    # │                                                                          │
    # │ `RequisitionCreateSerializer.product` est un `UUIDField` NU : ni la vue, │
    # │ ni le gestionnaire de synchronisation ne valident l'appartenance. Un     │
    # │ identifiant d'une autre organisation était donc persisté, puis relu par  │
    # │ `RequisitionItemSerializer` (`product_name`, `product_sku`, `unit_name`) │
    # │ et par le bordereau imprimé. MESURÉ : la ligne sortait avec le nom de    │
    # │ l'article d'autrui et un instantané à `0.000`, `stocks` et `seuils`      │
    # │ étant les seuls à être filtrés par organisation.                         │
    # │                                                                          │
    # │ Le contrôle vit dans le SERVICE, pas dans le serializer : la vue ET le   │
    # │ journal passent par là, donc la parité n'est pas surveillée, elle est    │
    # │ structurelle. C'est la règle de §5.5.                                    │
    # └──────────────────────────────────────────────────────────────────────────┘
    #
    # Les clés de `seuils` sont l'ensemble AUTORITAIRE : la requête qui les
    # relève est déjà bornée à l'organisation, et `Product.objects` est un
    # `TenantSoftDeleteManager`, donc une fiche supprimée en douceur est écartée
    # du même coup.
    #
    # ⚠ LE MESSAGE NE RÉPÈTE PAS L'IDENTIFIANT. Le renvoyer confirmerait au
    #   client qu'un article d'une autre organisation existe. On dit COMBIEN
    #   d'articles sont refusés, jamais lesquels.
    #
    # Le refus arrive APRÈS la création de l'en-tête, et c'est sans conséquence :
    # la fonction est `@transaction.atomic`, donc rien n'est écrit. C'est aussi
    # ce qui rend sûr le refus de quantité nulle, quelques lignes plus bas.
    etrangers = len(set(ids) - set(seuils))
    if etrangers:
        raise RequisitionRefusee(
            f"{etrangers} article(s) demandé(s) n'appartiennent pas à cet "
            "établissement."
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

    # ⚠ PRÉVENIR DEPUIS LE SERVICE, pas depuis la vue : une réquisition se
    # soumet au back-office ET par le journal du terminal, et le brancher sur
    # la vue laisserait muet tout ce qui vient du fond d'un dépôt - c'est-à-dire
    # la quasi-totalité des demandes.
    from apps.notifications.services import decision_prise, demande_approbation

    if action == 'submit':
        demande_approbation(
            requisition.organization,
            quoi='La réquisition', reference=requisition.reference,
            par=requisition.requested_by,
            entrepot=requisition.warehouse_id,
            resource_type='requisition', resource_id=requisition.id,
            detail=f"{requisition.items.count()} article(s)",
            action_url='/dashboard/stock/requisitions',
        )
    elif action in ('approve', 'reject'):
        decision_prise(
            requisition.organization,
            quoi='La réquisition', reference=requisition.reference,
            decidee_par=user, pour=requisition.requested_by,
            approuvee=(action == 'approve'),
            entrepot=requisition.warehouse_id, motif=notes or '',
            action_url='/dashboard/stock/requisitions',
        )

    return requisition
