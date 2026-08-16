"""
Serializers DRF pour l'app Inventory.
"""
from rest_framework import serializers
from decimal import Decimal
from django.utils import timezone
from django.db.models import F
from apps.products.models import Category
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem,
    InventorySession, InventoryCount, STOCK_IN_MOVEMENT_TYPES
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
    unit_symbol = serializers.CharField(source='product.unit.symbol', read_only=True)
    product_image = serializers.ImageField(source='product.image', read_only=True)
    stock_display = serializers.SerializerMethodField()
    stock_packages = serializers.SerializerMethodField()
    stock_loose = serializers.SerializerMethodField()

    class Meta:
        model = Stock
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'product_image',
            'variant', 'variant_name',
            'warehouse', 'warehouse_name',
            'quantity', 'reserved_quantity', 'available_quantity',
            'loose_quantity', 'stock_display', 'stock_packages', 'stock_loose',
            'unit_symbol',
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

    def get_stock_display(self, obj):
        """Quantité prête à afficher : « 1 paquet + 10 bouteilles »."""
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(
            obj.product, obj.quantity,
            min(obj.loose_quantity, max(obj.quantity, Decimal('0.000'))),
        )

    def _split(self, obj):
        from apps.inventory.packaging import PackagingService

        factor = PackagingService.factor(obj.product)
        if factor is None:
            return None
        return PackagingService.split(obj.quantity, obj.loose_quantity, factor)

    def get_stock_packages(self, obj):
        split = self._split(obj)
        return split[0] if split else None

    def get_stock_loose(self, obj):
        split = self._split(obj)
        return str(split[1]) if split else None


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
    unit_symbol = serializers.CharField(source='product.unit.symbol', read_only=True)
    product_image = serializers.ImageField(source='product.image', read_only=True)
    stock_display = serializers.SerializerMethodField()
    stock_packages = serializers.SerializerMethodField()
    stock_loose = serializers.SerializerMethodField()

    class Meta:
        model = Stock
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'product_image',
            'variant', 'variant_name',
            'warehouse', 'warehouse_name',
            'location', 'location_name',
            'quantity', 'reserved_quantity', 'available_quantity',
            'loose_quantity', 'stock_display', 'stock_packages', 'stock_loose',
            'unit_symbol',
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

    # Partage scellé/vrac - même logique que la liste, déléguée au service.
    get_stock_display = StockListSerializer.get_stock_display
    _split = StockListSerializer._split
    get_stock_packages = StockListSerializer.get_stock_packages
    get_stock_loose = StockListSerializer.get_stock_loose

    def get_recent_movements(self, obj):
        """Retourne les 10 derniers mouvements."""
        movements = StockMovement.objects.filter(
            product=obj.product,
            warehouse=obj.warehouse
        ).select_related(
            'product__unit', 'product__packaging_unit', 'warehouse', 'created_by'
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
    location_name = serializers.CharField(source='location.name', read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    days_until_expiry = serializers.SerializerMethodField()
    
    class Meta:
        model = StockBatch
        fields = [
            'id', 'product', 'product_name', 'variant',
            'warehouse', 'warehouse_name', 'location', 'location_name',
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
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    movement_type_display = serializers.CharField(
        source='get_movement_type_display', read_only=True
    )
    # Quantité telle qu'elle a été saisie : « 10 cartons + 5 bouteilles ».
    # `quantity` reste la valeur en unité de base et fait foi pour tout calcul.
    quantity_display = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant',
            'warehouse', 'warehouse_name',
            'movement_type', 'movement_type_display',
            'quantity', 'quantity_display', 'unit_cost',
            'quantity_before', 'quantity_after',
            'packaging_factor',
            'reference_type', 'reference_id',
            'notes',
            'created_by', 'created_by_name',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']

    def get_quantity_display(self, obj):
        from .packaging import PackagingService

        return PackagingService.format_movement_quantity(obj)


class StockMovementDetailSerializer(StockMovementListSerializer):
    """Serializer complet pour le détail d'un mouvement."""

    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)

    class Meta(StockMovementListSerializer.Meta):
        fields = StockMovementListSerializer.Meta.fields + ['batch', 'batch_number']


class StockMovementCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création manuelle de mouvement.

    Pour les produits vendus en gros, l'approvisionnement peut se saisir en
    conditionnements, en unités, ou dans les deux à la fois. La conversion vers
    l'unité de base est faite **ici**, dans ``validate()`` : tout l'aval
    (``perform_create``, lots FIFO, coût moyen pondéré, quantité du mouvement)
    continue de raisonner en unité de base sans modification.

    Les prix suivent la même logique : le marchand saisit ce qu'il a payé dans
    les termes où il l'a payé (au carton, à la bouteille, ou les deux), et le
    serializer en déduit le coût unitaire unique que porte le mouvement.
    Optionnellement, ces prix remontent sur la fiche produit.
    """

    # Champs additionnels pour la création de lots lors des approvisionnements
    location = serializers.PrimaryKeyRelatedField(
        queryset=StockLocation.objects.all(),
        required=False,
        allow_null=True,
        write_only=True
    )
    expiry_date = serializers.DateField(required=False, allow_null=True, write_only=True)

    # Saisie en conditionnement. `quantity` devient optionnelle dès que l'une
    # de ces valeurs est fournie.
    package_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=3, required=False, write_only=True
    )
    loose_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=3, required=False, write_only=True
    )
    package_unit_cost = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True, write_only=True
    )

    # Report des prix sur la fiche produit. Les noms sont ceux de `Product` :
    # toute autre convention imposerait un mapping mental à chaque relecture.
    update_product_prices = serializers.BooleanField(
        required=False, default=False, write_only=True
    )
    selling_price = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True, write_only=True
    )
    wholesale_price = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True, write_only=True
    )

    class Meta:
        model = StockMovement
        fields = [
            'product', 'variant', 'warehouse', 'batch',
            'movement_type', 'quantity', 'unit_cost', 'notes',
            'location', 'expiry_date',
            'package_quantity', 'loose_quantity', 'package_unit_cost',
            'update_product_prices', 'selling_price', 'wholesale_price',
        ]
        extra_kwargs = {'quantity': {'required': False}}

    def validate(self, data):
        """Valide le mouvement de stock et convertit la saisie en unité de base."""
        from apps.inventory.packaging import PackagingService

        movement_type = data.get('movement_type')

        # Un déconditionnement n'est pas un mouvement de quantité : il déplace du
        # scellé vers le vrac. `perform_create` ne saurait pas maintenir ce
        # partage, seul `PackagingService` en est capable.
        if movement_type == StockMovement.MovementType.UNPACK:
            raise serializers.ValidationError({
                'movement_type': (
                    "Le déconditionnement ne se saisit pas comme un mouvement. "
                    "Utilisez l'action « Ouvrir un conditionnement »."
                )
            })

        product = data.get('product')
        package_quantity = data.pop('package_quantity', None)
        loose_quantity = data.pop('loose_quantity', None)
        package_unit_cost = data.pop('package_unit_cost', None)
        update_product_prices = data.pop('update_product_prices', False)
        selling_price = data.pop('selling_price', None)
        wholesale_price = data.pop('wholesale_price', None)

        factor = PackagingService.factor(product) if product else None

        if package_quantity is not None or loose_quantity is not None:
            if factor is None:
                raise serializers.ValidationError({
                    'package_quantity': (
                        "Ce produit n'est pas vendu par conditionnement : "
                        "saisissez une quantité simple."
                    )
                })
            package_quantity = package_quantity or Decimal('0.000')
            loose_quantity = loose_quantity or Decimal('0.000')
            data['quantity'] = PackagingService.to_base(
                product, package_quantity, loose_quantity
            )
            data['input_package_quantity'] = package_quantity
            data['input_loose_quantity'] = loose_quantity
            data['packaging_factor'] = factor

        if data.get('quantity') is None:
            raise serializers.ValidationError({
                'quantity': "Indiquez une quantité."
            })

        if package_unit_cost is not None and not factor:
            raise serializers.ValidationError({
                'package_unit_cost': (
                    "Ce produit n'est pas vendu par conditionnement."
                )
            })

        # Le coût du mouvement se calcule avant l'inversion de signe des
        # sorties, sur les quantités saisies, qui sont positives.
        loose_unit_cost = data.get('unit_cost')
        self._apply_costs(
            data,
            factor=factor,
            package_quantity=package_quantity,
            loose_quantity=loose_quantity,
            package_unit_cost=package_unit_cost,
            loose_unit_cost=loose_unit_cost,
        )

        if update_product_prices:
            data['_product_prices'] = self._validate_product_prices(
                product=product,
                movement_type=movement_type,
                cost_price=data.get('input_loose_unit_cost'),
                package_cost_price=data.get('input_package_unit_cost'),
                selling_price=selling_price,
                wholesale_price=wholesale_price,
            )

        quantity = data['quantity']

        # Les mouvements sortants doivent avoir une quantité négative
        outgoing_types = ['sale', 'return_out', 'transfer_out', 'adjustment_out', 'damage', 'expired']
        if movement_type in outgoing_types and quantity > 0:
            data['quantity'] = -abs(quantity)

        return data

    def _apply_costs(
        self, data, *, factor, package_quantity, loose_quantity,
        package_unit_cost, loose_unit_cost,
    ):
        """
        Déduit le coût unitaire du mouvement des prix saisis, et fige ces prix.

        Le marchand peut avoir payé au conditionnement, à l'unité, ou les deux
        dans la même livraison : « 2 cartons à 6 000 et 3 bouteilles à 550 ».
        Le mouvement ne porte qu'un coût unitaire, qui vaut alors « ce que j'ai
        payé divisé par ce que j'ai reçu ». Quand un seul prix est saisi, on
        complète l'autre par conversion et la pondération se réduit exactement
        à la division d'avant.
        """
        from apps.inventory.packaging import PackagingService

        # Un prix nul vaut « non saisi » : le formulaire envoie 0 par défaut, et
        # figer ce zéro ferait croire à un achat gratuit tout en écrasant la
        # pondération. C'est déjà la convention de `perform_create`, qui retombe
        # sur le prix du produit quand le coût saisi est nul.
        package_unit_cost = package_unit_cost or None
        loose_unit_cost = loose_unit_cost or None

        if package_unit_cost is None and loose_unit_cost is None:
            return

        # Prix réellement saisis, avant toute conversion : c'est eux qui rendent
        # l'historique relisible.
        data['input_package_unit_cost'] = package_unit_cost
        data['input_loose_unit_cost'] = loose_unit_cost

        if not factor:
            return

        if package_unit_cost is None:
            package_unit_cost = PackagingService.package_cost_from_unit(
                loose_unit_cost, factor
            )
        if loose_unit_cost is None:
            loose_unit_cost = PackagingService.unit_cost_from_package(
                package_unit_cost, factor
            )

        data['unit_cost'] = PackagingService.blended_unit_cost(
            package_quantity=package_quantity,
            package_cost=package_unit_cost,
            loose_quantity=loose_quantity,
            loose_cost=loose_unit_cost,
            factor=factor,
        )

    def _validate_product_prices(
        self, *, product, movement_type, cost_price, package_cost_price,
        selling_price, wholesale_price,
    ):
        """
        Contrôle la demande de report des prix sur la fiche produit.

        Le refus est une erreur de champ et non un 403 : créer le mouvement
        reste autorisé, seul cet effet de bord optionnel ne l'est pas. Un 403
        afficherait « vous n'avez pas la permission de créer un mouvement », ce
        qui est faux et bloquerait un opérateur qui n'a qu'à décocher la case.
        """
        from apps.core.services import PermissionService
        from apps.products.pricing import ProductPricingService

        if movement_type not in STOCK_IN_MOVEMENT_TYPES:
            raise serializers.ValidationError({
                'update_product_prices': (
                    "Les prix de la fiche produit ne se mettent à jour que sur "
                    "une entrée de stock."
                )
            })

        request = self.context.get('request')
        view = self.context.get('view')
        organization = (
            view.get_organization()
            if view is not None and hasattr(view, 'get_organization')
            else None
        )
        if request and organization and not PermissionService.has_permission(
            request.user, organization, 'products.edit'
        ):
            raise serializers.ValidationError({
                'update_product_prices': (
                    "Vous n'avez pas la permission de modifier les prix de la "
                    "fiche produit."
                )
            })

        selling_mode = getattr(product, 'selling_mode', 'retail_only')
        prices = ProductPricingService.resolve_and_validate(
            selling_mode=selling_mode,
            units_per_package=getattr(product, 'units_per_package', None),
            cost_price=cost_price,
            package_cost_price=package_cost_price,
            selling_price=selling_price,
            wholesale_price=wholesale_price,
            # Le prix de vente au conditionnement est déjà enregistré sur la
            # fiche : ne pas l'exiger à nouveau à chaque approvisionnement.
            require_wholesale=False,
        )

        # `wholesale_price` garde son sens historique de prix de gros à la pièce
        # en vente au détail seule : l'écraser depuis un approvisionnement y
        # changerait la sémantique en silence.
        if selling_mode == 'retail_only':
            prices.pop('wholesale_price', None)
            prices.pop('package_cost_price', None)
        elif selling_mode == 'wholesale_only':
            prices.pop('selling_price', None)

        if not prices:
            raise serializers.ValidationError({
                'update_product_prices': (
                    "Indiquez au moins un prix à reporter sur la fiche produit."
                )
            })
        return prices

    def create(self, validated_data):
        """Crée le mouvement en extrayant les champs write_only."""
        # Extraire les champs qui ne font pas partie du modèle StockMovement
        validated_data.pop('location', None)
        validated_data.pop('expiry_date', None)
        validated_data.pop('_product_prices', None)

        # Créer le mouvement normalement
        return super().create(validated_data)


# =============================================================================
# STOCK TRANSFER SERIALIZERS
# =============================================================================

class StockTransferItemSerializer(serializers.ModelSerializer):
    """
    Serializer pour les articles de transfert.

    Un transfert se prépare comme il se charge : « 4 cartons + 3 bouteilles ».
    ``quantity_requested`` reste la quantité en unité de détail et continue de
    piloter l'expédition et la réception.
    """

    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    requested_display = serializers.SerializerMethodField()

    class Meta:
        model = StockTransferItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant', 'batch',
            'quantity_requested', 'quantity_shipped', 'quantity_received',
            'package_quantity', 'loose_quantity', 'packaging_factor',
            'requested_display', 'notes'
        ]
        read_only_fields = ['id', 'packaging_factor']
        extra_kwargs = {'quantity_requested': {'required': False}}

    def get_requested_display(self, obj):
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(
            obj.product, obj.quantity_requested, obj.loose_quantity
        )

    def validate(self, data):
        """Recompose la quantité demandée à partir de la saisie en contenants."""
        from apps.inventory.packaging import PackagingService

        product = data.get('product') or getattr(self.instance, 'product', None)
        factor = PackagingService.factor(product) if product else None

        packages = data.get('package_quantity')
        loose = data.get('loose_quantity')

        if packages or loose:
            if factor is None:
                raise serializers.ValidationError({
                    'package_quantity': (
                        "Ce produit ne se vend pas par contenant : "
                        "indiquez une quantité simple."
                    )
                })
            packages = packages or Decimal('0.000')
            loose = loose or Decimal('0.000')
            if product.selling_mode == 'wholesale_only' and loose > 0:
                raise serializers.ValidationError({
                    'loose_quantity': (
                        f"{product.name} ne se transfère que par contenant entier."
                    )
                })
            data['package_quantity'] = packages
            data['loose_quantity'] = loose
            data['packaging_factor'] = factor
            data['quantity_requested'] = PackagingService.to_base(
                product, packages, loose
            )

        if not data.get('quantity_requested'):
            raise serializers.ValidationError({
                'quantity_requested': "Indiquez une quantité à transférer."
            })

        return data


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
    """
    Serializer pour les articles d'ajustement.

    Un produit vendu par contenant se compte comme il se range : « 3 cartons +
    2 bouteilles ». La recomposition vers l'unité de base se fait ici, si bien
    que l'approbation, les écarts et les valorisations continuent de ne
    connaître que ``quantity_counted``.
    """

    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    counted_display = serializers.SerializerMethodField()
    expected_display = serializers.SerializerMethodField()
    package_unit_cost = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, write_only=True
    )

    class Meta:
        model = StockAdjustmentItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant', 'batch',
            'quantity_counted', 'quantity_expected', 'quantity_difference',
            'counted_package_quantity', 'counted_loose_quantity',
            'packaging_factor', 'counted_display', 'expected_display',
            'unit_cost', 'package_unit_cost', 'notes'
        ]
        read_only_fields = ['id', 'quantity_difference', 'packaging_factor']
        extra_kwargs = {'quantity_counted': {'required': False}}

    def get_counted_display(self, obj):
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(
            obj.product, obj.quantity_counted, obj.counted_loose_quantity
        )

    def get_expected_display(self, obj):
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(obj.product, obj.quantity_expected)

    def validate(self, data):
        """Recompose la quantité comptée et le coût unitaire de base."""
        from apps.inventory.packaging import PackagingService

        product = data.get('product') or getattr(self.instance, 'product', None)
        factor = PackagingService.factor(product) if product else None

        packages = data.get('counted_package_quantity')
        loose = data.get('counted_loose_quantity')
        package_unit_cost = data.pop('package_unit_cost', None)

        if packages is not None or loose is not None:
            if factor is None:
                raise serializers.ValidationError({
                    'counted_package_quantity': (
                        "Ce produit ne se vend pas par contenant : "
                        "indiquez une quantité simple."
                    )
                })
            packages = packages or Decimal('0.000')
            loose = loose or Decimal('0.000')
            data['counted_package_quantity'] = packages
            data['counted_loose_quantity'] = loose
            data['packaging_factor'] = factor
            data['quantity_counted'] = PackagingService.to_base(
                product, packages, loose
            )

        if data.get('quantity_counted') is None:
            raise serializers.ValidationError({
                'quantity_counted': "Indiquez la quantité comptée."
            })

        if package_unit_cost is not None:
            if factor is None:
                raise serializers.ValidationError({
                    'package_unit_cost': "Ce produit ne se vend pas par contenant."
                })
            data['unit_cost'] = (
                Decimal(package_unit_cost) / factor
            ).quantize(Decimal('0.01'))

        return data


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


# =============================================================================
# INVENTORY SESSION SERIALIZERS
# =============================================================================

class InventoryCountSerializer(serializers.ModelSerializer):
    """Serializer pour les lignes de comptage d'inventaire."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    product_category_name = serializers.SerializerMethodField()
    variant_name = serializers.CharField(source='variant.name', read_only=True, default=None)
    counted_by_name = serializers.SerializerMethodField()
    unit_name = serializers.SerializerMethodField()
    package_unit_name = serializers.CharField(
        source='product.packaging_unit.name', read_only=True, default=None
    )
    expected_display = serializers.SerializerMethodField()
    counted_display = serializers.SerializerMethodField()

    class Meta:
        model = InventoryCount
        fields = [
            'id', 'session', 'product', 'product_name', 'product_sku',
            'product_category_name', 'variant', 'variant_name',
            'quantity_expected', 'quantity_counted', 'quantity_difference',
            'expected_loose_quantity', 'counted_package_quantity',
            'counted_loose_quantity', 'packaging_factor',
            'expected_display', 'counted_display',
            'package_unit_name',
            'unit_cost', 'difference_value',
            'is_counted', 'counted_by', 'counted_by_name', 'counted_at',
            'unit_name', 'notes',
        ]
        read_only_fields = [
            'id', 'session', 'product', 'variant',
            'quantity_expected', 'quantity_difference', 'difference_value',
            'unit_cost',
        ]

    def get_product_category_name(self, obj):
        if obj.product.category:
            return obj.product.category.name
        return None

    def get_counted_by_name(self, obj):
        if obj.counted_by:
            return obj.counted_by.full_name or obj.counted_by.email
        return None

    def get_unit_name(self, obj):
        if obj.product.unit:
            return obj.product.unit.symbol
        return None

    def get_expected_display(self, obj):
        """Attendu en clair : « 3 paquets + 1 bouteille »."""
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(
            obj.product, obj.quantity_expected, obj.expected_loose_quantity
        )

    def get_counted_display(self, obj):
        from apps.inventory.packaging import PackagingService

        if not obj.is_counted:
            return None
        return PackagingService.format_quantity(
            obj.product, obj.quantity_counted, obj.counted_loose_quantity
        )


class InventorySessionListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de sessions d'inventaire."""
    
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    scope_type_display = serializers.CharField(source='get_scope_type_display', read_only=True)
    progress_percentage = serializers.FloatField(read_only=True)
    items_total = serializers.IntegerField(read_only=True)
    items_counted = serializers.IntegerField(read_only=True)
    items_with_difference = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = InventorySession
        fields = [
            'id', 'reference', 'name', 'warehouse', 'warehouse_name',
            'scope_type', 'scope_type_display',
            'status', 'status_display', 'is_stock_locked',
            'progress_percentage', 'items_total', 'items_counted', 'items_with_difference',
            'total_expected_quantity', 'total_counted_quantity',
            'total_difference_quantity', 'total_difference_value',
            'created_by', 'created_by_name',
            'started_at', 'completed_at', 'validated_at',
            'created_at',
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.full_name or obj.created_by.email
        return None


class InventorySessionDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une session d'inventaire."""
    
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    validated_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    scope_type_display = serializers.CharField(source='get_scope_type_display', read_only=True)
    progress_percentage = serializers.FloatField(read_only=True)
    items_total = serializers.IntegerField(read_only=True)
    items_counted = serializers.IntegerField(read_only=True)
    items_with_difference = serializers.IntegerField(read_only=True)
    counts = InventoryCountSerializer(many=True, read_only=True)
    category_names = serializers.SerializerMethodField()
    product_names = serializers.SerializerMethodField()
    
    class Meta:
        model = InventorySession
        fields = [
            'id', 'reference', 'name', 'warehouse', 'warehouse_name',
            'scope_type', 'scope_type_display',
            'status', 'status_display', 'is_stock_locked',
            'notes',
            'progress_percentage', 'items_total', 'items_counted', 'items_with_difference',
            'total_expected_quantity', 'total_counted_quantity',
            'total_difference_quantity', 'total_difference_value',
            'counts', 'category_names', 'product_names',
            'created_by', 'created_by_name',
            'validated_by', 'validated_by_name',
            'started_at', 'completed_at', 'validated_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.full_name or obj.created_by.email
        return None

    def get_validated_by_name(self, obj):
        if obj.validated_by:
            return obj.validated_by.full_name or obj.validated_by.email
        return None

    def get_category_names(self, obj):
        return list(obj.categories.values_list('name', flat=True))

    def get_product_names(self, obj):
        return list(obj.products.values_list('name', flat=True))


class InventorySessionCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création d'une session d'inventaire."""
    
    category_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list
    )
    product_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list
    )
    name = serializers.CharField(required=False, allow_blank=True)
    
    class Meta:
        model = InventorySession
        fields = ['name', 'warehouse', 'scope_type', 'notes', 'category_ids', 'product_ids']

    def validate(self, data):
        scope_type = data.get('scope_type', 'full')
        category_ids = data.get('category_ids', [])
        product_ids = data.get('product_ids', [])
        
        if scope_type == 'category' and not category_ids:
            raise serializers.ValidationError({
                'category_ids': "Au moins une catégorie est requise pour un inventaire par catégorie."
            })
        
        if scope_type == 'product' and not product_ids:
            raise serializers.ValidationError({
                'product_ids': "Au moins un produit est requis pour un inventaire par produit."
            })
        
        # Check warehouse has stock
        warehouse = data.get('warehouse')
        organization = self.context['request'].headers.get('X-Organization-ID')

        if scope_type == 'product' and product_ids:
            available_product_ids = set(
                Stock.objects.filter(
                    organization_id=organization,
                    warehouse=warehouse,
                    product_id__in=product_ids,
                    variant__isnull=True,
                    quantity__gt=F('reserved_quantity'),
                ).values_list('product_id', flat=True).distinct()
            )
            if any(pid not in available_product_ids for pid in product_ids):
                raise serializers.ValidationError({
                    'product_ids': (
                        "Certains produits sélectionnés n'ont pas de stock disponible "
                        "dans l'entrepôt choisi."
                    )
                })

        if scope_type == 'category' and category_ids:
            valid_category_ids = set(
                Category.objects.filter(
                    organization_id=organization,
                    id__in=category_ids,
                    is_deleted=False,
                ).values_list('id', flat=True)
            )
            invalid_selection = [
                cid for cid in category_ids if cid not in valid_category_ids
            ]
            if invalid_selection:
                raise serializers.ValidationError({
                    'category_ids': (
                        "Certaines catégories sélectionnées sont invalides pour cette organisation."
                    )
                })

            available_category_ids = set(
                Stock.objects.filter(
                    organization_id=organization,
                    warehouse=warehouse,
                    product__category_id__in=category_ids,
                    quantity__gt=F('reserved_quantity'),
                    product__is_deleted=False,
                ).values_list('product__category_id', flat=True).distinct()
            )
            if any(cid not in available_category_ids for cid in category_ids):
                raise serializers.ValidationError({
                    'category_ids': (
                        "Certaines catégories sélectionnées n'ont aucun produit en stock "
                        "disponible dans l'entrepôt choisi."
                    )
                })
        
        has_stock = Stock.objects.filter(
            organization_id=organization,
            warehouse=warehouse,
            quantity__gt=F('reserved_quantity'),
        ).exists()
        if not has_stock:
            raise serializers.ValidationError({
                'warehouse': "Cet entrepôt ne contient aucun produit en stock."
            })
        
        # Check no active inventory session for same warehouse
        active_sessions = InventorySession.objects.filter(
            organization_id=organization,
            warehouse=warehouse,
            status__in=['in_progress', 'review'],
            is_deleted=False,
        )
        if active_sessions.exists():
            raise serializers.ValidationError({
                'warehouse': "Un inventaire est déjà en cours pour cet entrepôt."
            })
        
        return data

    def create(self, validated_data):
        category_ids = validated_data.pop('category_ids', [])
        product_ids = validated_data.pop('product_ids', [])
        
        from apps.core.utils import ReferenceGenerator
        organization_id = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization_id)
        
        validated_data['reference'] = ReferenceGenerator.generate_inventory_reference(org)
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        
        # Auto-generate name from date if not provided
        if not validated_data.get('name'):
            now = timezone.now()
            validated_data['name'] = f"Inventaire du {now.strftime('%d/%m/%Y')}"
        
        session = InventorySession.objects.create(**validated_data)
        
        if category_ids:
            session.categories.set(category_ids)
        if product_ids:
            session.products.set(product_ids)
        
        return session
