"""
Transitions d'un RETOUR et d'un DEVIS.

Elles vivent ici et non dans les vues parce que deux surfaces les déclenchent :
le back-office et le journal d'opérations du terminal. C'est le partage établi
aux lots 6 à 9, et la même raison : deux corps auraient fini par diverger sur
l'arithmétique de la dette et du stock.

**Ni le retour ni le devis n'ont d'écran nulle part** avant ce lot - ni sur le
web, ni sur le mobile. Le terminal crée donc la référence, et ces fonctions
sont le seul chemin d'écriture.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone


class TransitionRefusee(Exception):
    """Refus métier déterministe : à ne JAMAIS réessayer."""


def approve_return(sale_return, user):
    """
    Approuve un retour : remet le stock, éteint la dette, rembourse le reste.

    **L'ORDRE COMPTE, et il a coûté cher.** Un retour sur une facture encore due
    éteint D'ABORD la dette : le client a rendu la marchandise, il n'a plus à la
    payer. Seul le reliquat sort physiquement de la caisse. Sans cela, le client
    rendait le produit ET continuait de devoir la totalité, pendant que le
    marchand lui remboursait en espèces de l'argent jamais encaissé.
    """
    from apps.cashbook.services import record_sale_return_refund
    from apps.contacts import services as contacts_services
    from apps.settings.services import LoyaltyService

    from .services import SaleStockService

    if sale_return.status != 'draft':
        raise TransitionRefusee(
            "Seuls les retours en brouillon peuvent être approuvés"
        )

    with transaction.atomic():
        # Remise en stock déléguée au service, qui centralise le recalcul du
        # coût moyen et la mise à jour du partage scellé/vrac.
        SaleStockService.apply_return(sale_return, user)

        sale_return.status = 'completed'
        sale_return.approved_by = user
        sale_return.approved_at = timezone.now()
        sale_return.save()

        sale = sale_return.original_sale
        refund_amount = sale_return.refund_amount or Decimal('0.00')

        debt_offset = Decimal('0.00')
        if sale and sale.customer and sale.amount_due > 0 and refund_amount > 0:
            debt_offset = min(refund_amount, sale.amount_due)
            contacts_services.settle_debt(
                sale.customer, debt_offset,
                currency=sale.currency,
                transaction_type=(
                    contacts_services.CustomerTransaction.TransactionType.REFUND
                ),
                sale=sale,
                reference=sale_return.reference,
                notes=f"Retour {sale_return.reference} sur vente {sale.reference}",
                user=user,
            )
            sale.amount_due = (sale.amount_due - debt_offset).quantize(Decimal('0.01'))
            sale.save(update_fields=['amount_due'])

        # Mouvement de caisse pour la seule part réellement remboursée.
        cash_refund = refund_amount - debt_offset
        if cash_refund > 0:
            record_sale_return_refund(
                organization=sale_return.organization,
                sale_return=sale_return,
                amount=cash_refund,
                user=user,
            )

        # Reverser les points : seule l'annulation le faisait, un retour
        # laissait le client garder les points d'une vente rendue. Un retour
        # TOTAL seulement : sur un retour partiel, les points gagnés sur la part
        # conservée restent acquis.
        if sale and sale_return.total_amount >= sale.total:
            LoyaltyService.reverse_sale_transactions(sale, user, label='retour')

    return sale_return


def reject_return(sale_return, user):
    """Rejette un retour en brouillon. Ni stock ni argent ne bougent."""
    if sale_return.status != 'draft':
        raise TransitionRefusee(
            "Seuls les retours en brouillon peuvent être rejetés"
        )
    sale_return.status = 'rejected'
    sale_return.save()
    return sale_return


def resolve_conversion_warehouse(quotation, explicite=None, autorises=None):
    """
    Entrepôt cible d'une conversion.

    Extrait pour que le contrôle de périmètre reste chez l'appelant (qui a la
    requête) tandis que la RÈGLE de choix - explicite, puis principal, puis le
    premier accessible - reste unique.
    """
    from apps.inventory.models import Warehouse

    base = Warehouse.objects.filter(
        organization=quotation.organization, is_active=True, is_deleted=False,
    )
    if explicite:
        trouve = base.filter(id=explicite).first()
        if trouve:
            return trouve

    if autorises is not None:
        base = base.filter(id__in=autorises)
    return base.filter(is_default=True).first() or base.first()


def convert_quotation(quotation, user, warehouse, perimetre_borne=False):
    """
    Convertit un devis en vente, et INSCRIT LA DETTE.

    ``perimetre_borne`` dit que l'appelant travaillait sous un périmètre
    d'entrepôt : sans entrepôt trouvé, c'est un refus, pas un repli silencieux.

    **La dette n'est pas optionnelle.** Un devis converti est une facture émise
    et non payée, au même titre qu'une vente à crédit. Sans `register_sale_debt`,
    la facture était retenue par `open_credit_sales` alors qu'aucune dette
    n'avait été inscrite : son règlement décrémentait un solde jamais
    incrémenté, et rendait le client artificiellement créditeur.
    """
    from apps.core.utils import ReferenceGenerator
    from apps.inventory.models import Stock

    from .models import Sale, SaleItem
    from .services import SaleStockService, register_sale_debt

    if quotation.status == 'converted':
        raise TransitionRefusee("Ce devis a déjà été converti")
    if quotation.status == 'expired':
        raise TransitionRefusee("Ce devis est expiré")

    if warehouse is None and perimetre_borne:
        raise TransitionRefusee(
            "Aucun entrepôt accessible pour convertir ce devis."
        )

    # Contrôle de stock AVANT d'écrire quoi que ce soit : convertir puis
    # échouer laisserait un devis converti sur une vente impossible à servir.
    if warehouse:
        for item in quotation.items.all():
            if item.product.track_inventory and not item.product.allow_negative_stock:
                stock = Stock.objects.filter(
                    product=item.product, variant=item.variant, warehouse=warehouse,
                ).first()
                available = stock.available_quantity if stock else 0
                if item.quantity > available:
                    raise TransitionRefusee(
                        f"Stock insuffisant pour {item.product.name}. "
                        f"Disponible: {available}"
                    )

    with transaction.atomic():
        sale = Sale.objects.create(
            organization=quotation.organization,
            reference=ReferenceGenerator.generate_sale_reference(quotation.organization),
            customer=quotation.customer,
            warehouse=warehouse,
            sale_type='retail',
            status='pending',
            subtotal=quotation.subtotal,
            tax_amount=quotation.tax_amount,
            discount_amount=quotation.discount_amount,
            total=quotation.total,
            amount_due=quotation.total,
            notes=quotation.notes,
            sold_by=user,
            is_pos=False,
        )

        for item in quotation.items.all():
            SaleItem.objects.create(
                sale=sale,
                organization=quotation.organization,
                product=item.product,
                variant=item.variant,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                cost_price=item.product.cost_price,
                discount_percentage=item.discount_percentage,
                tax_rate=item.tax_rate,
                subtotal=item.quantity * item.unit_price,
                total=item.total,
            )

        quotation.status = 'converted'
        quotation.converted_sale = sale
        quotation.save()

        # Réserver immédiatement : sans quoi un autre caissier vendrait les
        # mêmes unités pendant la fenêtre conversion → encaissement.
        if sale.warehouse:
            SaleStockService.reserve_stock(sale, user)

        # APRÈS la réservation : si une avance solde entièrement la facture,
        # `register_sale_debt` enchaîne sur le décrément, dans le même ordre
        # réservation → décrément que le chemin `add_payment`.
        if sale.customer and sale.amount_due > 0:
            register_sale_debt(
                sale, user,
                notes=(
                    f"Devis {quotation.reference} converti en vente {sale.reference}"
                ),
            )

    return sale
