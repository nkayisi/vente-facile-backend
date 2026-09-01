"""
Services pour la gestion des stocks et des lots (FIFO).
"""
from decimal import Decimal
from typing import List, Tuple, Optional
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import InventoryCount, Stock, StockBatch, StockMovement
from apps.core.clock import maintenant


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
        CA HT par ligne après remises (y compris remise globale répartie), aligné sur les rapports.

        Args:
            sale: Instance de Sale

        Returns:
            Dict avec total_revenue, total_cost, total_profit, margin_percentage, items_detail
        """
        from apps.sales.profit_allocation import allocated_line_ht_revenues_for_sale, effective_unit_cost

        total_revenue = Decimal('0.00')
        total_cost = Decimal('0.00')
        items_detail = []

        alloc_by_item_id = {
            i.id: rev for i, rev in allocated_line_ht_revenues_for_sale(sale)
        }

        for item in sale.items.all():
            revenue = alloc_by_item_id.get(item.id, Decimal('0.00')).quantize(Decimal('0.01'))
            cu = effective_unit_cost(item)
            cost = (cu * item.quantity).quantize(Decimal('0.01'))
            profit = (revenue - cost).quantize(Decimal('0.01'))
            margin_percentage = Decimal('0.00')
            if revenue > 0:
                margin_percentage = ((profit / revenue) * 100).quantize(Decimal('0.01'))

            total_revenue += revenue
            total_cost += cost

            items_detail.append({
                'product_id': str(item.product_id),
                'product_name': item.product.name,
                'quantity': item.quantity,
                'unit_price': item.unit_price,
                'cost_price': item.cost_price,
                'revenue': revenue,
                'cost': cost,
                'profit': profit,
                'margin_percentage': margin_percentage,
            })

        total_profit = (total_revenue - total_cost).quantize(Decimal('0.01'))
        margin_sale = Decimal('0.00')
        if total_revenue > 0:
            margin_sale = ((total_profit / total_revenue) * 100).quantize(Decimal('0.01'))

        return {
            'total_revenue': total_revenue,
            'total_cost': total_cost,
            'total_profit': total_profit,
            'margin_percentage': margin_sale,
            'items_detail': items_detail
        }


# ---------------------------------------------------------------------------
# Transitions d'un TRANSFERT de stock.
#
# Elles vivent ICI et non dans la vue parce que DEUX surfaces les déclenchent :
# le back-office par `StockTransferViewSet`, et le terminal mobile par le
# journal d'opérations. Tant que le corps était écrit dans la vue, le
# gestionnaire de synchronisation devait le réécrire - et c'est exactement
# ainsi que la dette client avait déjà divergé (lot 6).
#
# Chacune refuse une transition impossible en levant `TransitionRefusee`. Le
# refus est DÉTERMINISTE : côté journal, il vaut verdict `rejected`, jamais
# `retry`. Réessayer un transfert déjà expédié le réexpédierait.
# ---------------------------------------------------------------------------


class TransitionRefusee(Exception):
    """Refus métier déterministe : à ne JAMAIS réessayer."""


def approve_transfer(transfer, user):
    """Approuve un transfert en attente."""
    
    if transfer.status != 'draft':
        raise TransitionRefusee("Seuls les transferts en brouillon peuvent être approuvés")
    
    transfer.status = 'pending'
    transfer.approved_by = user
    transfer.save()
    
    return {'status': 'approved'}


def ship_transfer(transfer, user):
    """Expédie un transfert : le stock quitte l'entrepôt source."""
    
    if transfer.status not in ['draft', 'pending']:
        raise TransitionRefusee("Ce transfert ne peut pas être expédié")
    
    from .packaging import PackagingService

    with transaction.atomic():
        # Déduire le stock de l'entrepôt source
        for item in transfer.items.select_related('product').all():
            product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
            stock, created = Stock.objects.select_for_update().get_or_create(
                organization=transfer.organization,
                product=item.product,
                variant=item.variant,
                warehouse=transfer.source_warehouse,
                defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
            )

            if not created and stock.avg_cost == 0 and product_cost > 0:
                stock.avg_cost = product_cost

            quantity_before = stock.quantity

            # Conditionnement : on ne charge pas un contenant scellé qui
            # n'existe pas, et servir la part au détail peut exiger d'en
            # ouvrir un. Le stock est déjà verrouillé, contrat exigé par
            # `ensure_loose_available`.
            loose_shipped = PackagingService.loose_share(
                item.product, item.quantity_requested, item.loose_quantity
            )
            PackagingService.assert_sealed_available(
                stock, item.product, item.package_quantity,
                action_label='transférer',
            )
            if loose_shipped > 0:
                PackagingService.ensure_loose_available(
                    stock, item.product, loose_shipped,
                    user=user,
                    reference_type='stock_transfer',
                    reference_id=transfer.id,
                )

            PackagingService.apply_base_delta(
                stock, item.product,
                -item.quantity_requested,
                loose_hint=loose_shipped,
            )
            PackagingService.touch(stock)
            stock.save()

            # Créer le mouvement sortant
            StockMovement.objects.create(
                organization=transfer.organization,
                product=item.product,
                variant=item.variant,
                warehouse=transfer.source_warehouse,
                movement_type='transfer_out',
                quantity=-item.quantity_requested,
                unit_cost=stock.avg_cost,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                input_package_quantity=item.package_quantity,
                input_loose_quantity=item.loose_quantity,
                packaging_factor=item.packaging_factor,
                reference_type='stock_transfer',
                reference_id=transfer.id,
                notes=f"Transfert {transfer.reference}",
                created_by=user
            )

            item.quantity_shipped = item.quantity_requested
            item.save()
        
        transfer.status = 'in_transit'
        transfer.shipped_at = maintenant()
        transfer.save()
    
    return {'status': 'shipped'}


def receive_transfer(transfer, user, received_items=None):
    """Réceptionne un transfert : le stock entre à destination.

    ``received_items`` porte ce que le magasinier a réellement compté, en
    contenants (« 3 cartons + 2 bouteilles ») ou en total. Absent, on retient
    ce qui a été expédié."""
    
    if transfer.status != 'in_transit':
        raise TransitionRefusee("Seuls les transferts en transit peuvent être reçus")
    
    received_items = received_items or []

    from .packaging import PackagingService

    with transaction.atomic():
        for item in transfer.items.select_related('product').all():
            # Chercher la quantité reçue dans les données. Elle peut arriver
            # en contenants (« 3 cartons + 2 bouteilles ») : c'est la forme
            # sous laquelle le magasinier compte ce qu'il décharge.
            received_qty = None
            received_loose = None
            for ri in received_items:
                if str(ri.get('id')) != str(item.id):
                    continue
                packages = ri.get('package_quantity')
                loose = ri.get('loose_quantity')
                if packages is not None or loose is not None:
                    received_loose = Decimal(str(loose or 0))
                    received_qty = PackagingService.to_base(
                        item.product, Decimal(str(packages or 0)), received_loose
                    )
                elif ri.get('quantity_received') is not None:
                    received_qty = Decimal(str(ri.get('quantity_received')))
                break

            if received_qty is None:
                received_qty = item.quantity_shipped

            item.quantity_received = received_qty
            item.save()

            # Une réception partielle ne conserve pas forcément le partage
            # d'origine : `loose_share` replafonne la part scellée sur ce qui
            # arrive vraiment.
            loose_received = PackagingService.loose_share(
                item.product,
                received_qty,
                received_loose if received_loose is not None else item.loose_quantity,
            )

            # Récupérer le coût moyen de la source
            product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
            source_stock = Stock.objects.filter(
                organization=transfer.organization,
                product=item.product,
                variant=item.variant,
                warehouse=transfer.source_warehouse
            ).first()
            source_avg_cost = source_stock.avg_cost if source_stock and source_stock.avg_cost > 0 else product_cost
            
            # Ajouter au stock destination avec verrouillage
            stock, created = Stock.objects.select_for_update().get_or_create(
                organization=transfer.organization,
                product=item.product,
                variant=item.variant,
                warehouse=transfer.destination_warehouse,
                defaults={'quantity': Decimal('0.000'), 'avg_cost': source_avg_cost}
            )
            
            if not created and stock.avg_cost == 0 and source_avg_cost > 0:
                stock.avg_cost = source_avg_cost
            
            quantity_before = stock.quantity
            
            # Mettre à jour le coût moyen pondéré
            if received_qty > 0 and source_avg_cost > 0:
                if stock.quantity > 0:
                    total_existing = stock.quantity * stock.avg_cost
                    total_incoming = received_qty * source_avg_cost
                    stock.avg_cost = (
                        (total_existing + total_incoming) /
                        (stock.quantity + received_qty)
                    ).quantize(Decimal('0.01'))
                else:
                    stock.avg_cost = source_avg_cost
            
            PackagingService.apply_base_delta(
                stock, item.product,
                received_qty,
                loose_hint=loose_received,
            )
            PackagingService.touch(stock)
            stock.save()

            # Créer le mouvement entrant
            StockMovement.objects.create(
                organization=transfer.organization,
                product=item.product,
                variant=item.variant,
                warehouse=transfer.destination_warehouse,
                movement_type='transfer_in',
                quantity=received_qty,
                unit_cost=source_avg_cost,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                input_package_quantity=(
                    (received_qty - loose_received) / item.packaging_factor
                    if item.packaging_factor else Decimal('0.000')
                ),
                input_loose_quantity=loose_received if item.packaging_factor else Decimal('0.000'),
                packaging_factor=item.packaging_factor,
                reference_type='stock_transfer',
                reference_id=transfer.id,
                notes=f"Transfert {transfer.reference}",
                created_by=user
            )
        
        transfer.status = 'completed'
        transfer.received_at = maintenant()
        transfer.save()
    
    return {'status': 'received'}


def cancel_transfer(transfer, user):
    """Annule un transfert, et REMET le stock s'il était déjà expédié."""
    
    if transfer.status == 'completed':
        raise TransitionRefusee("Un transfert terminé ne peut pas être annulé")
    
    from .packaging import PackagingService

    with transaction.atomic():
        # Si déjà expédié, remettre le stock
        if transfer.status == 'in_transit':
            for item in transfer.items.select_related('product').all():
                product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
                stock, created = Stock.objects.select_for_update().get_or_create(
                    organization=transfer.organization,
                    product=item.product,
                    variant=item.variant,
                    warehouse=transfer.source_warehouse,
                    defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
                )
                
                if not created and stock.avg_cost == 0 and product_cost > 0:
                    stock.avg_cost = product_cost
                
                quantity_before = stock.quantity
                quantity_to_restore = item.quantity_shipped or Decimal('0.000')

                # Annuler une expédition, c'est décharger le camion : les
                # contenants qui n'ont jamais été ouverts reviennent scellés.
                # Le partage restitué est donc l'exact symétrique de celui
                # retiré à l'expédition.
                loose_to_restore = PackagingService.loose_share(
                    item.product, quantity_to_restore, item.loose_quantity
                )
                PackagingService.apply_base_delta(
                    stock, item.product,
                    quantity_to_restore,
                    loose_hint=loose_to_restore,
                )
                PackagingService.touch(stock)
                stock.save()

                # Créer le mouvement de retour
                if quantity_to_restore > 0:
                    StockMovement.objects.create(
                        organization=transfer.organization,
                        product=item.product,
                        variant=item.variant,
                        warehouse=transfer.source_warehouse,
                        movement_type='transfer_in',
                        quantity=quantity_to_restore,
                        quantity_before=quantity_before,
                        quantity_after=stock.quantity,
                        input_package_quantity=item.package_quantity,
                        input_loose_quantity=item.loose_quantity,
                        packaging_factor=item.packaging_factor,
                        reference_type='stock_transfer_cancel',
                        reference_id=transfer.id,
                        notes=f"Annulation transfert {transfer.reference}",
                        created_by=user
                    )
        
        transfer.status = 'cancelled'
        transfer.save()
    
    return {'status': 'cancelled'}


# ---------------------------------------------------------------------------
# Transitions d'un AJUSTEMENT de stock. Même partage que les transferts.
# ---------------------------------------------------------------------------


def approve_adjustment(adjustment, user):
    """Approuve et APPLIQUE l'ajustement : le stock bouge ici."""
    
    if adjustment.status != 'draft':
        raise TransitionRefusee("Seuls les ajustements en brouillon peuvent être approuvés")
    
    from .packaging import PackagingService

    with transaction.atomic():
        # Appliquer les ajustements
        for item in adjustment.items.select_related('product').all():
            product_cost = item.product.cost_price if item.product.cost_price else Decimal('0.00')
            stock, created = Stock.objects.select_for_update().get_or_create(
                organization=adjustment.organization,
                product=item.product,
                variant=item.variant,
                warehouse=adjustment.warehouse,
                defaults={'quantity': Decimal('0.000'), 'avg_cost': product_cost}
            )
            
            if not created and stock.avg_cost == 0 and product_cost > 0:
                stock.avg_cost = product_cost
            
            quantity_before = stock.quantity
            
            # Mettre à jour le coût moyen si un coût unitaire est fourni
            if item.unit_cost and item.unit_cost > 0 and item.quantity_difference > 0:
                if stock.quantity > 0:
                    total_existing = stock.quantity * stock.avg_cost
                    total_incoming = item.quantity_difference * item.unit_cost
                    stock.avg_cost = (
                        (total_existing + total_incoming) /
                        (stock.quantity + item.quantity_difference)
                    ).quantize(Decimal('0.01'))
                else:
                    stock.avg_cost = item.unit_cost
            
            # Le comptage physique fait foi sur les DEUX canaux : « j'ai
            # compté 3 casiers et 2 bouteilles » se pose tel quel, sans
            # repasser par une division. `reconcile` réaligne `quantity`.
            stock.quantity = item.quantity_counted
            if item.counted_loose_quantity is not None:
                stock.loose_quantity = item.counted_loose_quantity
            if item.counted_package_quantity is not None:
                stock.package_quantity = item.counted_package_quantity
            stock.last_counted_at = timezone.now()
            stock.last_movement_at = timezone.now()
            stock.save()

            # Déterminer le type de mouvement
            if item.quantity_difference > 0:
                movement_type = 'adjustment_in'
            else:
                movement_type = 'adjustment_out'

            # L'écart se relit en contenants dans l'historique : « il
            # manquait 2 cartons + 1 bouteille » parle au marchand, « -25 »
            # non. Le signe reste porté par `quantity`.
            factor = item.packaging_factor or PackagingService.factor(item.product)
            gap = abs(item.quantity_difference)
            loose_gap = (
                PackagingService.loose_share(item.product, gap)
                if factor else Decimal('0.000')
            )
            package_gap = (gap - loose_gap) / factor if factor else Decimal('0.000')

            # Créer le mouvement
            StockMovement.objects.create(
                organization=adjustment.organization,
                product=item.product,
                variant=item.variant,
                warehouse=adjustment.warehouse,
                movement_type=movement_type,
                quantity=item.quantity_difference,
                unit_cost=item.unit_cost,
                quantity_before=quantity_before,
                quantity_after=stock.quantity,
                input_package_quantity=package_gap,
                input_loose_quantity=loose_gap,
                packaging_factor=factor,
                reference_type='stock_adjustment',
                reference_id=adjustment.id,
                notes=f"Ajustement {adjustment.reference}: {adjustment.get_adjustment_type_display()}",
                created_by=user
            )
        
        adjustment.status = 'approved'
        adjustment.approved_by = user
        adjustment.approved_at = maintenant()
        adjustment.save()
    
    return {'status': 'approved'}


def reject_adjustment(adjustment, user):
    """Rejette un ajustement en brouillon. Le stock ne bouge pas."""
    
    if adjustment.status != 'draft':
        raise TransitionRefusee("Seuls les ajustements en brouillon peuvent être rejetés")
    
    adjustment.status = 'rejected'
    adjustment.save()
    
    return {'status': 'rejected'}


# ---------------------------------------------------------------------------
# Déconditionnement manuel.
# ---------------------------------------------------------------------------


def unpack_stock(stock, user, packages=1):
    """
    Ouvre un ou plusieurs conditionnements sans attendre une vente.

    Sert au vendeur qui anticipe, et débloque le cas où le déconditionnement
    automatique est désactivé sur le produit.

    Corps partagé entre `StockViewSet.unpack` et le journal du terminal : c'est
    un geste de comptoir, il doit fonctionner hors ligne.
    """
    from .packaging import PackagingService

    try:
        packages = int(packages)
    except (TypeError, ValueError):
        packages = 0
    if packages < 1:
        raise TransitionRefusee("Indiquez combien de conditionnements ouvrir.")

    product = stock.product
    factor = PackagingService.factor(product)
    if factor is None:
        raise TransitionRefusee("Ce produit n'est pas vendu par conditionnement.")

    with transaction.atomic():
        locked = Stock.objects.select_for_update().get(pk=stock.pk)
        _, loose = PackagingService.stored_split(locked, factor)
        opened, _movement = PackagingService.ensure_loose_available(
            locked, product,
            needed_loose=loose + packages * factor,
            user=user,
            reference_type='manual_unpack',
            force=True,
        )
        locked.last_movement_at = timezone.now()
        locked.save()

    locked.refresh_from_db()
    return {
        'packages_opened': opened,
        'stock_display': PackagingService.format_quantity(
            product, locked.quantity, locked.loose_quantity
        ),
    }


# ---------------------------------------------------------------------------
# Transitions d'une SESSION D'INVENTAIRE.
#
# Même partage que les transferts : le back-office et le journal du terminal
# appellent ces fonctions, il n'y a plus qu'un corps. C'est ici que se joue le
# meilleur usage mobile du produit - on compte debout dans le rayon, souvent
# sans réseau - donc chaque transition doit être rejouable par le journal.
# ---------------------------------------------------------------------------


def target_products(session):
    """
    Produits visés par la session, selon son périmètre.

    Extrait de la vue avec le reste : le journal démarre des sessions, et il
    doit cibler exactement les mêmes produits que le back-office.

    `Category.subtree_ids` est une méthode de CLASSE, et prend la liste des
    racines. L'appeler sur une instance sans argument lève `TypeError` : toute
    session portant sur une catégorie échouait donc au démarrage, en 500, des
    deux côtés (vue et journal). Aucun test ne l'avait vu, tous démarrant des
    sessions de périmètre `full`. Un seul parcours pour toutes les racines, ce
    qui est aussi ce qu'il fallait faire : une descente par catégorie visée
    rejouerait la même hiérarchie autant de fois qu'il y a de racines.
    """
    from django.db.models import F
    from apps.products.models import Category, Product

    base_qs = Product.objects.filter(
        organization=session.organization, is_active=True, track_inventory=True,
    )
    if session.scope_type == 'category':
        ids = Category.subtree_ids(list(session.categories.values_list('id', flat=True)))
        base_qs = base_qs.filter(category_id__in=ids)
    elif session.scope_type == 'product':
        base_qs = base_qs.filter(id__in=session.products.values_list('id', flat=True))

    # Seuls les produits RÉELLEMENT présents dans l'entrepôt, disponibles.
    return base_qs.filter(
        stocks__warehouse=session.warehouse,
        stocks__variant__isnull=True,
        stocks__quantity__gt=F('stocks__reserved_quantity'),
    ).distinct()


def start_inventory_session(session, user):
    """
    Démarre une session : verrouille le stock ciblé, en prend un INSTANTANÉ et
    engendre les lignes de comptage.

    L'instantané compte : la ligne retient le stock théorique ET sa part vrac au
    moment du départ. Sans elle, l'écart se comparerait à un attendu redécoupé
    au conditionnement du jour, c'est-à-dire à un attendu qui n'a jamais existé.
    """
    from .packaging import PackagingService

    if session.status != 'draft':
        raise TransitionRefusee(
            "Seules les sessions en brouillon peuvent être démarrées"
        )

    with transaction.atomic():
        lignes = []
        for product in target_products(session):
            stock = Stock.objects.filter(
                organization=session.organization,
                product=product,
                warehouse=session.warehouse,
                variant=None,
            ).first()

            quantite = stock.quantity if stock else Decimal('0.000')
            cout = Decimal('0.00')
            if stock and stock.avg_cost > 0:
                cout = stock.avg_cost
            elif product.cost_price:
                cout = product.cost_price

            lignes.append(InventoryCount(
                organization=session.organization,
                session=session,
                product=product,
                variant=None,
                quantity_expected=quantite,
                expected_loose_quantity=(
                    stock.loose_quantity if stock else Decimal('0.000')
                ),
                packaging_factor=PackagingService.factor(product),
                unit_cost=cout,
            ))

        InventoryCount.objects.bulk_create(lignes)

        session.status = 'in_progress'
        session.is_stock_locked = True
        session.started_at = maintenant()
        session.save()

    return session


def record_inventory_counts(session, user, lignes=None):
    """
    Enregistre le comptage d'une ou plusieurs lignes.

    Le comptage arrive en « X conditionnements + Y unités » dès que la ligne
    porte un facteur : c'est la forme sous laquelle on compte un rayon. La
    saisie simple reste acceptée pour les produits vendus à l'unité.

    Une ligne inconnue est IGNORÉE, pas refusée : un lot de comptages envoyé
    depuis un terminal ne doit pas être condamné en entier par une ligne
    supprimée entre-temps.
    """
    if session.status != 'in_progress':
        raise TransitionRefusee(
            "L'inventaire doit être en cours pour enregistrer des comptages"
        )

    lignes = lignes or []
    if not lignes:
        raise TransitionRefusee("Aucun comptage fourni")

    modifiees = []
    with transaction.atomic():
        for item in lignes:
            identifiant = item.get('id')
            compte = item.get('quantity_counted')
            notes = item.get('notes', '')

            if identifiant is None or compte is None:
                continue

            try:
                ligne = InventoryCount.objects.select_for_update().get(
                    id=identifiant, session=session,
                )
            except InventoryCount.DoesNotExist:
                continue

            paquets = item.get('counted_package_quantity')
            vrac = item.get('counted_loose_quantity')
            if ligne.packaging_factor and (paquets is not None or vrac is not None):
                ligne.counted_package_quantity = Decimal(str(paquets or 0))
                ligne.counted_loose_quantity = Decimal(str(vrac or 0))
            else:
                ligne.quantity_counted = Decimal(str(compte))
                ligne.counted_loose_quantity = Decimal(str(compte))
            ligne.is_counted = True
            ligne.counted_by = user
            ligne.counted_at = maintenant()
            if notes:
                ligne.notes = notes
            ligne.save()
            modifiees.append(str(ligne.id))

    return {
        'status': 'counted',
        'updated_count': len(modifiees),
        'updated_ids': modifiees,
    }


def submit_inventory_session(session, user):
    """
    Soumet la session pour révision, une fois TOUT compté.

    Le refus sur les lignes non comptées est volontaire : une session soumise à
    moitié appliquerait des écarts sur les seuls produits regardés, et
    laisserait croire que les autres sont justes.
    """
    from django.db.models import Sum

    if session.status != 'in_progress':
        raise TransitionRefusee("Seules les sessions en cours peuvent être soumises")

    manquants = session.counts.filter(is_counted=False).count()
    if manquants > 0:
        raise TransitionRefusee(
            f"{manquants} produit(s) n'ont pas encore été comptés"
        )

    totaux = session.counts.aggregate(
        total_expected=Sum('quantity_expected'),
        total_counted=Sum('quantity_counted'),
        total_diff=Sum('quantity_difference'),
        total_diff_value=Sum('difference_value'),
    )
    session.total_expected_quantity = totaux['total_expected'] or Decimal('0.000')
    session.total_counted_quantity = totaux['total_counted'] or Decimal('0.000')
    session.total_difference_quantity = totaux['total_diff'] or Decimal('0.000')
    session.total_difference_value = totaux['total_diff_value'] or Decimal('0.00')
    session.status = 'review'
    session.completed_at = maintenant()
    session.save()

    return session


def validate_inventory_session(session, user):
    """
    Valide la session : APPLIQUE les écarts au stock, écrit les mouvements et
    déverrouille.

    **Le comptage physique écrase les DEUX canaux.** Un rayon compté constate ce
    qui s'y trouve ; conserver l'ancien partage continuerait d'annoncer des
    contenants scellés qui n'existent plus.
    """
    if session.status != 'review':
        raise TransitionRefusee("Seules les sessions en révision peuvent être validées")

    with transaction.atomic():
        for ligne in session.counts.filter(is_counted=True).select_related('product'):
            if ligne.quantity_difference == 0:
                continue

            cout = ligne.unit_cost or (ligne.product.cost_price or Decimal('0.00'))
            stock, cree = Stock.objects.select_for_update().get_or_create(
                organization=session.organization,
                product=ligne.product,
                variant=ligne.variant,
                warehouse=session.warehouse,
                defaults={'quantity': Decimal('0.000'), 'avg_cost': cout},
            )
            if not cree and stock.avg_cost == 0 and cout > 0:
                stock.avg_cost = cout

            avant = stock.quantity
            stock.quantity = ligne.quantity_counted
            if ligne.packaging_factor:
                stock.loose_quantity = ligne.counted_loose_quantity
                stock.package_quantity = (
                    ligne.counted_package_quantity or Decimal('0.000')
                )
            stock.last_counted_at = timezone.now()
            stock.last_movement_at = timezone.now()
            stock.save()

            StockMovement.objects.create(
                organization=session.organization,
                product=ligne.product,
                variant=ligne.variant,
                warehouse=session.warehouse,
                movement_type=(
                    'adjustment_in' if ligne.quantity_difference > 0
                    else 'adjustment_out'
                ),
                quantity=ligne.quantity_difference,
                unit_cost=ligne.unit_cost,
                quantity_before=avant,
                quantity_after=stock.quantity,
                reference_type='inventory_session',
                reference_id=session.id,
                notes=f"Inventaire {session.reference}: {session.name}",
                created_by=user,
            )

        session.status = 'validated'
        session.is_stock_locked = False
        session.validated_at = maintenant()
        session.validated_by = user
        session.save()

    return session


def cancel_inventory_session(session, user):
    """Annule la session. Le stock ne bouge pas, et se déverrouille."""
    if session.status in ['validated', 'cancelled']:
        raise TransitionRefusee("Cette session ne peut pas être annulée")

    session.status = 'cancelled'
    session.is_stock_locked = False
    session.save()
    return session
