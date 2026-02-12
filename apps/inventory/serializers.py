"""
Serializers DRF pour l'app Inventory.
"""
from rest_framework import serializers
from decimal import Decimal
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem
)


# =============================================================================
# WAREHOUSE SERIALIZERS
# =============================================================================

class WarehouseListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes d'entrepôts."""
    
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    stock_value = serializers.SerializerMethodField()
    
    class Meta:
        model = Warehouse
        fields = [
            'id', 'name', 'code', 'branch', 'branch_name',
            'address', 'manager', 'manager_name', 'is_default', 'is_active',
            'allow_negative_stock', 'stock_value'
        ]
        read_only_fields = ['id']

    def get_stock_value(self, obj):
        """Calcule la valeur totale du stock."""
        total = sum(
            stock.quantity * (stock.avg_cost if stock.avg_cost > 0 else (stock.product.cost_price or 0))
            for stock in obj.stocks.select_related('product').all()
        )
        return str(total)


class WarehouseDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un entrepôt."""
    
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    locations = serializers.SerializerMethodField()
    stock_value = serializers.SerializerMethodField()
    
    class Meta:
        model = Warehouse
        fields = [
            'id', 'name', 'code', 'branch', 'branch_name',
            'address', 'manager', 'manager_name',
            'is_default', 'is_active', 'allow_negative_stock',
            'stock_value', 'locations', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_stock_value(self, obj):
        """Calcule la valeur totale du stock."""
        total = sum(
            stock.quantity * (stock.avg_cost if stock.avg_cost > 0 else (stock.product.cost_price or 0))
            for stock in obj.stocks.select_related('product').all()
        )
        return str(total)

    def get_locations(self, obj):
        return StockLocationSerializer(
            obj.locations.filter(is_active=True),
            many=True
        ).data


class WarehouseCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création d'entrepôt."""
    
    class Meta:
        model = Warehouse
        fields = [
            'name', 'code', 'branch', 'address', 'manager',
            'is_default', 'is_active', 'allow_negative_stock'
        ]

    def validate_code(self, value):
        """Vérifie l'unicité du code dans l'organisation."""
        organization = self.context['request'].headers.get('X-Organization-ID')
        queryset = Warehouse.objects.filter(
            organization_id=organization,
            code=value,
            is_deleted=False
        )
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        if queryset.exists():
            raise serializers.ValidationError("Ce code existe déjà.")
        return value


# =============================================================================
# STOCK LOCATION SERIALIZERS
# =============================================================================

class StockLocationSerializer(serializers.ModelSerializer):
    """Serializer pour les emplacements de stock."""
    
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    parent_name = serializers.CharField(source='parent.name', read_only=True)
    
    class Meta:
        model = StockLocation
        fields = [
            'id', 'name', 'code', 'warehouse', 'warehouse_name',
            'parent', 'parent_name', 'is_active'
        ]
        read_only_fields = ['id']


# =============================================================================
# STOCK SERIALIZERS
# =============================================================================

class StockListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de stock."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    variant_name = serializers.CharField(source='variant.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    available_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=3, read_only=True
    )
    avg_cost = serializers.SerializerMethodField()
    stock_value = serializers.SerializerMethodField()
    
    class Meta:
        model = Stock
        fields = [
            'id', 'product', 'product_name', 'product_sku',
            'variant', 'variant_name',
            'warehouse', 'warehouse_name',
            'quantity', 'reserved_quantity', 'available_quantity',
            'avg_cost', 'stock_value',
            'last_movement_at'
        ]
        read_only_fields = ['id', 'last_movement_at']

    def _get_effective_cost(self, obj):
        return obj.avg_cost if obj.avg_cost > 0 else (obj.product.cost_price or Decimal('0.00'))

    def get_avg_cost(self, obj):
        return str(self._get_effective_cost(obj))

    def get_stock_value(self, obj):
        return str(obj.quantity * self._get_effective_cost(obj))


class StockDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail du stock."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    variant_name = serializers.CharField(source='variant.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    available_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=3, read_only=True
    )
    avg_cost = serializers.SerializerMethodField()
    stock_value = serializers.SerializerMethodField()
    recent_movements = serializers.SerializerMethodField()
    batches = serializers.SerializerMethodField()
    
    class Meta:
        model = Stock
        fields = [
            'id', 'product', 'product_name', 'product_sku',
            'variant', 'variant_name',
            'warehouse', 'warehouse_name',
            'location', 'location_name',
            'quantity', 'reserved_quantity', 'available_quantity',
            'avg_cost', 'stock_value', 'last_counted_at', 'last_movement_at',
            'recent_movements', 'batches',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def _get_effective_cost(self, obj):
        return obj.avg_cost if obj.avg_cost > 0 else (obj.product.cost_price or Decimal('0.00'))

    def get_avg_cost(self, obj):
        return str(self._get_effective_cost(obj))

    def get_stock_value(self, obj):
        return str(obj.quantity * self._get_effective_cost(obj))

    def get_recent_movements(self, obj):
        """Retourne les 10 derniers mouvements."""
        movements = StockMovement.objects.filter(
            product=obj.product,
            warehouse=obj.warehouse
        ).order_by('-created_at')[:10]
        return StockMovementListSerializer(movements, many=True).data

    def get_batches(self, obj):
        """Retourne les lots actifs."""
        batches = StockBatch.objects.filter(
            product=obj.product,
            warehouse=obj.warehouse,
            quantity__gt=0
        )
        return StockBatchSerializer(batches, many=True).data


# =============================================================================
# STOCK BATCH SERIALIZERS
# =============================================================================

class StockBatchSerializer(serializers.ModelSerializer):
    """Serializer pour les lots de stock."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    days_until_expiry = serializers.SerializerMethodField()
    
    class Meta:
        model = StockBatch
        fields = [
            'id', 'product', 'product_name', 'variant',
            'warehouse', 'warehouse_name',
            'batch_number', 'quantity', 'cost_price',
            'manufacturing_date', 'expiry_date',
            'is_expired', 'days_until_expiry',
            'received_at', 'notes'
        ]
        read_only_fields = ['id', 'received_at']

    def get_days_until_expiry(self, obj):
        if obj.expiry_date:
            from django.utils import timezone
            delta = obj.expiry_date - timezone.now().date()
            return delta.days
        return None


# =============================================================================
# STOCK MOVEMENT SERIALIZERS
# =============================================================================

class StockMovementListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de mouvements."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    movement_type_display = serializers.CharField(
        source='get_movement_type_display', read_only=True
    )
    
    class Meta:
        model = StockMovement
        fields = [
            'id', 'product', 'product_name', 'variant',
            'warehouse', 'warehouse_name',
            'movement_type', 'movement_type_display',
            'quantity', 'unit_cost',
            'quantity_before', 'quantity_after',
            'reference_type', 'reference_id',
            'created_by', 'created_by_name',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class StockMovementDetailSerializer(StockMovementListSerializer):
    """Serializer complet pour le détail d'un mouvement."""
    
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    
    class Meta(StockMovementListSerializer.Meta):
        fields = StockMovementListSerializer.Meta.fields + ['batch', 'batch_number', 'notes']


class StockMovementCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création manuelle de mouvement."""
    
    class Meta:
        model = StockMovement
        fields = [
            'product', 'variant', 'warehouse', 'batch',
            'movement_type', 'quantity', 'unit_cost', 'notes'
        ]

    def validate(self, data):
        """Valide le mouvement de stock."""
        movement_type = data.get('movement_type')
        quantity = data.get('quantity')
        
        # Les mouvements sortants doivent avoir une quantité négative
        outgoing_types = ['sale', 'return_out', 'transfer_out', 'adjustment_out', 'damage', 'expired']
        if movement_type in outgoing_types and quantity > 0:
            data['quantity'] = -abs(quantity)
        
        return data


# =============================================================================
# STOCK TRANSFER SERIALIZERS
# =============================================================================

class StockTransferItemSerializer(serializers.ModelSerializer):
    """Serializer pour les articles de transfert."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    
    class Meta:
        model = StockTransferItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant', 'batch',
            'quantity_requested', 'quantity_shipped', 'quantity_received',
            'notes'
        ]
        read_only_fields = ['id']


class StockTransferListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de transferts."""
    
    source_warehouse_name = serializers.CharField(
        source='source_warehouse.name', read_only=True
    )
    destination_warehouse_name = serializers.CharField(
        source='destination_warehouse.name', read_only=True
    )
    requested_by_name = serializers.CharField(
        source='requested_by.full_name', read_only=True
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = StockTransfer
        fields = [
            'id', 'reference', 'source_warehouse', 'source_warehouse_name',
            'destination_warehouse', 'destination_warehouse_name',
            'status', 'status_display', 'items_count',
            'requested_by', 'requested_by_name',
            'requested_at', 'shipped_at', 'received_at'
        ]
        read_only_fields = ['id', 'reference', 'requested_at']

    def get_items_count(self, obj):
        return obj.items.count()


class StockTransferDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un transfert."""
    
    source_warehouse_name = serializers.CharField(
        source='source_warehouse.name', read_only=True
    )
    destination_warehouse_name = serializers.CharField(
        source='destination_warehouse.name', read_only=True
    )
    requested_by_name = serializers.CharField(
        source='requested_by.full_name', read_only=True
    )
    approved_by_name = serializers.CharField(
        source='approved_by.full_name', read_only=True
    )
    items = StockTransferItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = StockTransfer
        fields = [
            'id', 'reference',
            'source_warehouse', 'source_warehouse_name',
            'destination_warehouse', 'destination_warehouse_name',
            'status', 'notes',
            'requested_by', 'requested_by_name',
            'approved_by', 'approved_by_name',
            'requested_at', 'shipped_at', 'received_at',
            'items', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']


class StockTransferCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de transfert."""
    
    items = StockTransferItemSerializer(many=True)
    
    class Meta:
        model = StockTransfer
        fields = [
            'source_warehouse', 'destination_warehouse', 'notes', 'items'
        ]

    def validate(self, data):
        """Valide le transfert."""
        source = data.get('source_warehouse')
        destination = data.get('destination_warehouse')
        
        if source == destination:
            raise serializers.ValidationError({
                'destination_warehouse': "L'entrepôt de destination doit être différent de la source."
            })
        
        items = data.get('items', [])
        if not items:
            raise serializers.ValidationError({
                'items': "Au moins un article est requis."
            })
        
        return data

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        # Générer la référence
        from apps.core.utils import ReferenceGenerator
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        validated_data['reference'] = ReferenceGenerator.generate_transfer_reference(org)
        validated_data['organization'] = org
        validated_data['requested_by'] = self.context['request'].user
        
        transfer = StockTransfer.objects.create(**validated_data)
        
        for item_data in items_data:
            StockTransferItem.objects.create(
                transfer=transfer,
                organization=org,
                **item_data
            )
        
        return transfer


# =============================================================================
# STOCK ADJUSTMENT SERIALIZERS
# =============================================================================

class StockAdjustmentItemSerializer(serializers.ModelSerializer):
    """Serializer pour les articles d'ajustement."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    
    class Meta:
        model = StockAdjustmentItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant', 'batch',
            'quantity_counted', 'quantity_expected', 'quantity_difference',
            'unit_cost', 'notes'
        ]
        read_only_fields = ['id', 'quantity_difference']


class StockAdjustmentListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes d'ajustements."""
    
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    adjustment_type_display = serializers.CharField(
        source='get_adjustment_type_display', read_only=True
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = StockAdjustment
        fields = [
            'id', 'reference', 'warehouse', 'warehouse_name',
            'adjustment_type', 'adjustment_type_display',
            'status', 'status_display', 'items_count',
            'created_by', 'created_by_name',
            'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_items_count(self, obj):
        return obj.items.count()


class StockAdjustmentDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un ajustement."""
    
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.full_name', read_only=True)
    items = StockAdjustmentItemSerializer(many=True, read_only=True)
    total_difference = serializers.SerializerMethodField()
    
    class Meta:
        model = StockAdjustment
        fields = [
            'id', 'reference', 'warehouse', 'warehouse_name',
            'adjustment_type', 'status', 'reason',
            'created_by', 'created_by_name',
            'approved_by', 'approved_by_name', 'approved_at',
            'items', 'total_difference',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']

    def get_total_difference(self, obj):
        """Calcule la différence totale en valeur."""
        total = sum(
            item.quantity_difference * item.unit_cost
            for item in obj.items.all()
        )
        return str(total)


class StockAdjustmentCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création d'ajustement."""
    
    items = StockAdjustmentItemSerializer(many=True)
    
    class Meta:
        model = StockAdjustment
        fields = ['warehouse', 'adjustment_type', 'reason', 'items']

    def validate(self, data):
        items = data.get('items', [])
        if not items:
            raise serializers.ValidationError({
                'items': "Au moins un article est requis."
            })
        return data

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        from apps.core.utils import ReferenceGenerator
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        validated_data['reference'] = ReferenceGenerator.generate_adjustment_reference(org)
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        
        adjustment = StockAdjustment.objects.create(**validated_data)
        
        for item_data in items_data:
            # Calculer la différence
            item_data['quantity_difference'] = (
                item_data['quantity_counted'] - item_data['quantity_expected']
            )
            StockAdjustmentItem.objects.create(
                adjustment=adjustment,
                organization=org,
                **item_data
            )
        
        return adjustment
