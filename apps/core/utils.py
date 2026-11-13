"""
Utility functions for the application.
"""
import uuid
import secrets
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone


def generate_token(length=32):
    """Generate a secure random token."""
    return secrets.token_urlsafe(length)


def round_decimal(value, places=2):
    """Round a decimal to specified places."""
    if value is None:
        return Decimal('0.00')
    return Decimal(str(value)).quantize(
        Decimal(10) ** -places,
        rounding=ROUND_HALF_UP
    )


class ReferenceGenerator:
    """Generate sequential references for documents."""
    
    @staticmethod
    def generate_sale_reference(organization):
        """Generate sale reference: VT-YYYYMMDD-XXXX"""
        from apps.sales.models import Sale
        
        # Le jour du MARCHAND : en UTC, la vente de 23 h 30 portait la date du
        # lendemain, à Kinshasa.
        today = timezone.localdate()
        prefix = f"VT-{today.strftime('%Y%m%d')}"
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ ON NE COMPTE QUE LES RÉFÉRENCES DE LA SÉRIE DU SERVEUR.          │
        # │                                                                  │
        # │ Un terminal alloue son propre numéro avant d'imprimer, et il     │
        # │ porte un code d'appareil : `VT-20260910-K7QM-0042`. Or le tri   │
        # │ est ALPHABÉTIQUE et « K » passe au-dessus de « 0 », si bien que  │
        # │ cette référence-là devenait le dernier rang connu, et            │
        # │ `split('-')[-1]` en tirait 42. La série du serveur sautait donc  │
        # │ à 0043 alors qu'elle en était à 0004.                            │
        # │                                                                  │
        # │ Ce n'est pas une collision - les deux séries ne se croisent pas  │
        # │ - c'est un TROU, et une série trouée porte le RCCM et le NIF.    │
        # │ Le motif ne retient que les rangs à quatre chiffres collés au    │
        # │ préfixe du jour, c'est-à-dire la seule forme que produit cette   │
        # │ fonction.                                                        │
        # └──────────────────────────────────────────────────────────────────┘
        last = Sale.objects.filter(
            organization=organization,
            reference__startswith=prefix,
        ).filter(
            reference__regex=r'^' + prefix + r'-[0-9]{4}$'
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_purchase_reference(organization):
        """Référence de commande d'achat, par le compteur verrouillé.

        ⚠ Le corps d'origine triait les références ALPHABÉTIQUEMENT puis
        incrémentait le dernier rang lu, et datait son préfixe en UTC. Les deux
        défauts sont ceux corrigés sur `generate_expense_reference` en
        septembre : « K » passe au-dessus de « 0 », donc une référence
        d'appareil trouait la série, et entre 23 h et minuit UTC le préfixe
        portait la veille à Kinshasa.

        `allocate_document_number` n'a ni l'un ni l'autre : son rang vient d'un
        `DocumentSequence` relu sous verrou, par organisation et par année.
        Conserver cette méthode plutôt que la supprimer laisse un point d'entrée
        nommé, au cas où un appelant reviendrait.
        """
        from apps.core.numbering import (
            PREFIX_PURCHASE_ORDER, allocate_document_number,
        )

        return allocate_document_number(organization, PREFIX_PURCHASE_ORDER)

    @staticmethod
    def generate_transfer_reference(organization):
        """Référence de transfert : TR-AAAAMMJJ-NNNN.

        Rang tenu par une séquence verrouillée, au jour du MARCHAND : voir
        `apps.core.numbering.allocate_daily_reference`.
        """
        from apps.core.numbering import PREFIX_TRANSFER, allocate_daily_reference
        from apps.inventory.models import StockTransfer

        return allocate_daily_reference(
            organization, PREFIX_TRANSFER, StockTransfer, field='reference',
        )

    @staticmethod
    def generate_adjustment_reference(organization):
        """Référence d'ajustement : ADJ-AAAAMMJJ-NNNN.

        Rang tenu par une séquence verrouillée, au jour du MARCHAND : voir
        `apps.core.numbering.allocate_daily_reference`.
        """
        from apps.core.numbering import PREFIX_STOCK_ADJUSTMENT, allocate_daily_reference
        from apps.inventory.models import StockAdjustment

        return allocate_daily_reference(
            organization, PREFIX_STOCK_ADJUSTMENT, StockAdjustment, field='reference',
        )

    @staticmethod
    def generate_batch_number(organization):
        """Numéro de lot : LOT-AAAAMMJJ-NNNN.

        Rang tenu par une séquence verrouillée, au jour du MARCHAND : voir
        `apps.core.numbering.allocate_daily_reference`.
        """
        from apps.core.numbering import PREFIX_BATCH, allocate_daily_reference
        from apps.inventory.models import StockBatch

        return allocate_daily_reference(
            organization, PREFIX_BATCH, StockBatch, field='batch_number',
        )

    @staticmethod
    def generate_inventory_reference(organization):
        """Référence d'inventaire : INV-AAAAMMJJ-NNNN.

        Rang tenu par une séquence verrouillée, au jour du MARCHAND : voir
        `apps.core.numbering.allocate_daily_reference`.
        """
        from apps.core.numbering import PREFIX_INVENTORY, allocate_daily_reference
        from apps.inventory.models import InventorySession

        return allocate_daily_reference(
            organization, PREFIX_INVENTORY, InventorySession, field='reference',
        )

    @staticmethod
    def generate_customer_code(organization):
        """Generate customer code: CL-XXXXX"""
        from apps.contacts.models import Customer
        
        last = Customer.objects.filter(
            organization=organization
        ).order_by('-code').first()
        
        if last:
            try:
                num = int(last.code.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"CL-{num:05d}"

    @staticmethod
    def generate_supplier_code(organization):
        """Generate supplier code: FR-XXXXX"""
        from apps.contacts.models import Supplier
        
        last = Supplier.objects.filter(
            organization=organization
        ).order_by('-code').first()
        
        if last:
            try:
                num = int(last.code.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"FR-{num:05d}"

    @staticmethod
    def generate_expense_reference(organization):
        """Generate expense reference: DEP-YYYYMMDD-XXXX"""
        from apps.cashbook.models import Expense
        
        # Le jour du MARCHAND : en UTC, la vente de 23 h 30 portait la date du
        # lendemain, à Kinshasa.
        today = timezone.localdate()
        prefix = f"DEP-{today.strftime('%Y%m%d')}"
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ ON NE COMPTE QUE LES RÉFÉRENCES DE LA SÉRIE DU SERVEUR.          │
        # │                                                                  │
        # │ Un terminal alloue son propre numéro avant d'imprimer, et il     │
        # │ porte un code d'appareil : `DEP-20260910-K7QM-0042`. Or le tri   │
        # │ est ALPHABÉTIQUE et « K » passe au-dessus de « 0 », si bien que  │
        # │ cette référence-là devenait le dernier rang connu, et            │
        # │ `split('-')[-1]` en tirait 42. La série du serveur sautait donc  │
        # │ à 0043 alors qu'elle en était à 0004.                            │
        # │                                                                  │
        # │ Ce n'est pas une collision - les deux séries ne se croisent pas  │
        # │ - c'est un TROU, et une série trouée porte le RCCM et le NIF.    │
        # │ Le motif ne retient que les rangs à quatre chiffres collés au    │
        # │ préfixe du jour, c'est-à-dire la seule forme que produit cette   │
        # │ fonction.                                                        │
        # └──────────────────────────────────────────────────────────────────┘
        last = Expense.objects.filter(
            organization=organization,
            reference__startswith=prefix,
        ).filter(
            reference__regex=r'^' + prefix + r'-[0-9]{4}$'
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError):
                num = 1
        else:
            num = 1
        
        return f"{prefix}-{num:04d}"

    @staticmethod
    def generate_cash_movement_reference(organization):
        """Référence de mouvement de caisse : MC-AAAAMMJJ-NNNN.

        Rang tenu par une séquence verrouillée, au jour du MARCHAND : voir
        `apps.core.numbering.allocate_daily_reference`.
        """
        from apps.core.numbering import PREFIX_CASH_MOVEMENT, allocate_daily_reference
        from apps.cashbook.models import CashMovement

        return allocate_daily_reference(
            organization, PREFIX_CASH_MOVEMENT, CashMovement, field='reference',
        )
