"""
Services pour la gestion des stocks et des lots (FIFO).
"""
from decimal import Decimal
from typing import List, Tuple, Optional
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import Stock, StockBatch, StockMovement


class BatchAllocation:
    """Représente une allocation de quantité depuis un lot."""
    def __init__(self, batch: StockBatch, quantity: Decimal, cost_price: Decimal):
        self.batch = batch
        self.quantity = quantity
        self.cost_price = cost_price
    
    @property
    def total_cost(self) -> Decimal:
        return (self.quantity * self.cost_price).quantize(Decimal('0.01'))


class FIFOService:
    """
    Service pour la gestion FIFO (First In First Out) des lots de produits.
    
    Lors d'une vente, les lots les plus anciens (received_at) sont utilisés en premier.
    Pour les produits périssables, les lots avec la date d'expiration la plus proche
    sont utilisés en priorité (FEFO - First Expired First Out).
    """
    
    @staticmethod
    def get_available_batches(
        organization,
        product,
        warehouse,
        variant=None,
        exclude_expired: bool = True,
        use_fefo: bool = False
    ) -> List[StockBatch]:
        """
        Récupère les lots disponibles pour un produit, triés par FIFO ou FEFO.
        
        Args:
            organization: Organisation
            product: Produit
            warehouse: Entrepôt
            variant: Variante du produit (optionnel)
            exclude_expired: Exclure les lots expirés
            use_fefo: Utiliser FEFO (First Expired First Out) au lieu de FIFO
        
        Returns:
            Liste des lots triés par ordre de priorité
        """
        queryset = StockBatch.objects.filter(
            organization=organization,
            product=product,
            warehouse=warehouse,
            quantity__gt=0
        )
        
        if variant:
            queryset = queryset.filter(variant=variant)
        else:
            queryset = queryset.filter(variant__isnull=True)
        
        if exclude_expired:
            today = timezone.now().date()
            queryset = queryset.filter(
                Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
            )
        
        if use_fefo:
            # FEFO: Les lots qui expirent bientôt en premier, puis par date de réception
            # Les lots sans date d'expiration sont traités en dernier
            queryset = queryset.order_by(
                F('expiry_date').asc(nulls_last=True),
                'received_at'
            )
        else:
            # FIFO: Les lots reçus en premier sont utilisés en premier
            queryset = queryset.order_by('received_at')
        
        return list(queryset)
    
    @staticmethod
    def allocate_quantity(
        organization,
        product,
        warehouse,
        quantity_needed: Decimal,
        variant=None,
        exclude_expired: bool = True,
        use_fefo: bool = False
    ) -> Tuple[List[BatchAllocation], Decimal]:
        """
        Alloue une quantité depuis les lots disponibles en utilisant FIFO/FEFO.
        
        Args:
            organization: Organisation
            product: Produit
            warehouse: Entrepôt
            quantity_needed: Quantité à allouer
            variant: Variante du produit (optionnel)
            exclude_expired: Exclure les lots expirés
            use_fefo: Utiliser FEFO au lieu de FIFO
        
        Returns:
            Tuple (liste des allocations, quantité non allouée)
        """
        batches = FIFOService.get_available_batches(
            organization=organization,
            product=product,
            warehouse=warehouse,
            variant=variant,
            exclude_expired=exclude_expired,
            use_fefo=use_fefo
        )
        
        allocations = []
        remaining = quantity_needed
        
        for batch in batches:
            if remaining <= 0:
                break
            
            # Quantité à prendre de ce lot
            take_quantity = min(batch.quantity, remaining)
            
            allocations.append(BatchAllocation(
                batch=batch,
                quantity=take_quantity,
                cost_price=batch.cost_price
            ))
            
            remaining -= take_quantity
        
        return allocations, remaining
    
    @staticmethod
    def calculate_weighted_cost(allocations: List[BatchAllocation]) -> Decimal:
        """
        Calcule le coût moyen pondéré des allocations.
        
        Args:
            allocations: Liste des allocations de lots
        
        Returns:
            Coût moyen pondéré
        """
        if not allocations:
            return Decimal('0.00')
        
        total_quantity = sum(a.quantity for a in allocations)
        if total_quantity == 0:
            return Decimal('0.00')
        
        total_cost = sum(a.total_cost for a in allocations)
        return (total_cost / total_quantity).quantize(Decimal('0.01'))
    
    @staticmethod
    @transaction.atomic
    def consume_from_batches(
        organization,
        product,
        warehouse,
        quantity: Decimal,
        variant=None,
        reference_type: str = 'sale',
        reference_id: str = None,
        user=None,
        notes: str = '',
        exclude_expired: bool = True,
        use_fefo: bool = False
    ) -> Tuple[List[BatchAllocation], Decimal]:
        """
        Consomme une quantité depuis les lots en utilisant FIFO/FEFO.
        Met à jour les quantités des lots et crée les mouvements de stock.
        
        Args:
            organization: Organisation
            product: Produit
            warehouse: Entrepôt
            quantity: Quantité à consommer
            variant: Variante du produit (optionnel)
            reference_type: Type de référence (sale, transfer, etc.)
            reference_id: ID de la référence
            user: Utilisateur effectuant l'opération
            notes: Notes additionnelles
            exclude_expired: Exclure les lots expirés
            use_fefo: Utiliser FEFO au lieu de FIFO
        
        Returns:
            Tuple (liste des allocations consommées, quantité non consommée)
        """
        # Verrouiller les lots pour éviter les conflits
        batches = StockBatch.objects.select_for_update().filter(
            organization=organization,
            product=product,
            warehouse=warehouse,
            quantity__gt=0
        )
        
        if variant:
            batches = batches.filter(variant=variant)
        else:
            batches = batches.filter(variant__isnull=True)
        
        if exclude_expired:
            today = timezone.now().date()
            batches = batches.filter(
                Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
            )
        
        if use_fefo:
            batches = batches.order_by(F('expiry_date').asc(nulls_last=True), 'received_at')
        else:
            batches = batches.order_by('received_at')
        
        allocations = []
        remaining = quantity
        
        for batch in batches:
            if remaining <= 0:
                break
            
            take_quantity = min(batch.quantity, remaining)
            quantity_before = batch.quantity
            
            # Mettre à jour la quantité du lot
            batch.quantity -= take_quantity
            batch.save(update_fields=['quantity'])
            
            allocations.append(BatchAllocation(
                batch=batch,
                quantity=take_quantity,
                cost_price=batch.cost_price
            ))
            
            remaining -= take_quantity
        
        return allocations, remaining
    
    @staticmethod
    @transaction.atomic
    def add_to_batch(
        organization,
        product,
        warehouse,
        quantity: Decimal,
        cost_price: Decimal,
        batch_number: str = None,
        variant=None,
        location=None,
        expiry_date=None,
        manufacturing_date=None,
        notes: str = '',
        user=None
    ) -> StockBatch:
        """
        Ajoute du stock à un lot existant ou crée un nouveau lot.
        
        Args:
            organization: Organisation
            product: Produit
            warehouse: Entrepôt
            quantity: Quantité à ajouter
            cost_price: Prix de revient unitaire
            batch_number: Numéro de lot (auto-généré si non fourni)
            variant: Variante du produit (optionnel)
            location: Emplacement dans l'entrepôt (optionnel)
            expiry_date: Date d'expiration (optionnel)
            manufacturing_date: Date de fabrication (optionnel)
            notes: Notes additionnelles
            user: Utilisateur effectuant l'opération
        
        Returns:
            Le lot créé ou mis à jour
        """
        if not batch_number:
            # Générer un numéro de lot automatique unique pour l'organisation
            from apps.core.utils import ReferenceGenerator
            batch_number = ReferenceGenerator.generate_batch_number(organization)
        
        # Chercher un lot existant avec le même numéro
        batch, created = StockBatch.objects.select_for_update().get_or_create(
            organization=organization,
            product=product,
            warehouse=warehouse,
            variant=variant,
            batch_number=batch_number,
            defaults={
                'quantity': Decimal('0.000'),
                'cost_price': cost_price,
                'location': location,
                'expiry_date': expiry_date,
                'manufacturing_date': manufacturing_date,
                'notes': notes
            }
        )
        
        if not created:
            # Mettre à jour le coût moyen pondéré si le lot existe déjà
            if batch.quantity > 0:
                total_existing = batch.quantity * batch.cost_price
                total_incoming = quantity * cost_price
                batch.cost_price = (
                    (total_existing + total_incoming) / (batch.quantity + quantity)
                ).quantize(Decimal('0.01'))
            else:
                batch.cost_price = cost_price
            
            # Mettre à jour l'emplacement si fourni
            if location:
                batch.location = location
            
            # Mettre à jour la date d'expiration si fournie
            if expiry_date:
                batch.expiry_date = expiry_date
            if manufacturing_date:
                batch.manufacturing_date = manufacturing_date
        
        batch.quantity += quantity
        batch.save()
        
        return batch
    
    @staticmethod
    def get_expiring_batches(
        organization,
        days_ahead: int = 30,
        warehouse=None
    ) -> List[StockBatch]:
        """
        Récupère les lots qui vont expirer dans les prochains jours.
        
        Args:
            organization: Organisation
            days_ahead: Nombre de jours à vérifier
            warehouse: Entrepôt spécifique (optionnel)
        
        Returns:
            Liste des lots qui vont expirer
        """
        today = timezone.now().date()
        expiry_limit = today + timezone.timedelta(days=days_ahead)
        
        queryset = StockBatch.objects.filter(
            organization=organization,
            quantity__gt=0,
            expiry_date__isnull=False,
            expiry_date__lte=expiry_limit,
            expiry_date__gte=today  # Pas encore expirés
        ).select_related('product', 'warehouse')
        
        if warehouse:
            queryset = queryset.filter(warehouse=warehouse)
        
        return list(queryset.order_by('expiry_date'))
    
    @staticmethod
    def get_expired_batches(organization, warehouse=None) -> List[StockBatch]:
        """
        Récupère les lots expirés avec du stock restant.
        
        Args:
            organization: Organisation
            warehouse: Entrepôt spécifique (optionnel)
        
        Returns:
            Liste des lots expirés
        """
        today = timezone.now().date()
        
        queryset = StockBatch.objects.filter(
            organization=organization,
            quantity__gt=0,
            expiry_date__isnull=False,
            expiry_date__lt=today
        ).select_related('product', 'warehouse')
        
        if warehouse:
            queryset = queryset.filter(warehouse=warehouse)
        
        return list(queryset.order_by('expiry_date'))


class ProfitCalculationService:
    """
    Service pour le calcul du profit réel basé sur le coût des lots FIFO.
    """
    
    @staticmethod
    def calculate_item_profit(
        selling_price: Decimal,
        quantity: Decimal,
        cost_price: Decimal,
        discount_amount: Decimal = Decimal('0.00')
    ) -> dict:
        """
        Calcule le profit pour un article de vente.
        
        Args:
            selling_price: Prix de vente unitaire
            quantity: Quantité vendue
            cost_price: Coût de revient unitaire (du lot FIFO)
            discount_amount: Montant de la remise
        
        Returns:
            Dict avec revenue, cost, profit, margin_percentage
        """
        revenue = (selling_price * quantity - discount_amount).quantize(Decimal('0.01'))
        cost = (cost_price * quantity).quantize(Decimal('0.01'))
        profit = (revenue - cost).quantize(Decimal('0.01'))
        
        margin_percentage = Decimal('0.00')
        if revenue > 0:
            margin_percentage = ((profit / revenue) * 100).quantize(Decimal('0.01'))
        
        return {
            'revenue': revenue,
            'cost': cost,
            'profit': profit,
            'margin_percentage': margin_percentage
        }
    
    @staticmethod
    def calculate_sale_profit(sale) -> dict:
        """
        Calcule le profit total d'une vente basé sur les coûts FIFO des lots.
        
        Args:
            sale: Instance de Sale
        
        Returns:
            Dict avec total_revenue, total_cost, total_profit, margin_percentage, items_detail
        """
        total_revenue = Decimal('0.00')
        total_cost = Decimal('0.00')
        items_detail = []
        
        for item in sale.items.all():
            item_profit = ProfitCalculationService.calculate_item_profit(
                selling_price=item.unit_price,
                quantity=item.quantity,
                cost_price=item.cost_price,
                discount_amount=item.discount_amount
            )
            
            total_revenue += item_profit['revenue']
            total_cost += item_profit['cost']
            
            items_detail.append({
                'product_id': str(item.product_id),
                'product_name': item.product.name,
                'quantity': item.quantity,
                'unit_price': item.unit_price,
                'cost_price': item.cost_price,
                **item_profit
            })
        
        total_profit = total_revenue - total_cost
        margin_percentage = Decimal('0.00')
        if total_revenue > 0:
            margin_percentage = ((total_profit / total_revenue) * 100).quantize(Decimal('0.01'))
        
        return {
            'total_revenue': total_revenue,
            'total_cost': total_cost,
            'total_profit': total_profit,
            'margin_percentage': margin_percentage,
            'items_detail': items_detail
        }
