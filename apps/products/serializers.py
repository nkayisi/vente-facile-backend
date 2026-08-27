"""
Serializers DRF pour l'app Products.
"""
from rest_framework import serializers
from .models import Category, Brand, Unit, Product, ProductImage, ProductVariant, PriceList, ProductPrice
from apps.core.warehouse_scope import accessible_warehouse_ids, get_membership_for_request


# =============================================================================
# CATEGORY SERIALIZERS
# =============================================================================

class CategoryListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de catégories."""
    
    children_count = serializers.SerializerMethodField()
    products_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'slug', 'parent', 'image',
            'is_active', 'sort_order', 'children_count', 'products_count'
        ]
        read_only_fields = ['id']

    def get_children_count(self, obj):
        return obj.children.filter(is_deleted=False).count()

    def get_products_count(self, obj):
        return obj.products.filter(is_deleted=False).count()


class CategoryDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une catégorie."""
    
    parent_name = serializers.CharField(source='parent.name', read_only=True)
    ancestors = serializers.SerializerMethodField()
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'slug', 'description', 'parent', 'parent_name',
            'image', 'sort_order', 'is_active', 'ancestors',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_ancestors(self, obj):
        """Retourne la hiérarchie des catégories parentes."""
        return [
            {'id': str(cat.id), 'name': cat.name}
            for cat in obj.get_ancestors()
        ]

    def validate(self, data):
        """Vérifie l'unicité du nom et du slug dans l'organisation."""
        request = self.context.get('request')
        if request:
            organization_id = request.headers.get('X-Organization-ID')
            if organization_id:
                # Vérifier l'unicité du nom
                name = data.get('name')
                if name:
                    queryset = Category.objects.filter(
                        organization_id=organization_id,
                        name__iexact=name,
                        is_deleted=False
                    )
                    if self.instance:
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if queryset.exists():
                        raise serializers.ValidationError({
                            'name': "Il existe déjà une catégorie avec ce même nom."
                        })
                
                # Vérifier l'unicité du slug
                slug = data.get('slug')
                if slug:
                    queryset = Category.objects.filter(
                        organization_id=organization_id,
                        slug=slug,
                        is_deleted=False
                    )
                    if self.instance:
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if queryset.exists():
                        raise serializers.ValidationError({
                            'slug': "Il existe déjà une catégorie avec ce même slug."
                        })
        
        # Validation du parent
        parent = data.get('parent')
        if self.instance and parent:
            if parent.id == self.instance.id:
                raise serializers.ValidationError({
                    'parent': "Une catégorie ne peut pas être son propre parent."
                })
            # Vérifie les références circulaires
            current = parent
            while current.parent:
                if current.parent.id == self.instance.id:
                    raise serializers.ValidationError({
                        'parent': "Référence circulaire détectée dans la hiérarchie."
                    })
                current = current.parent
        
        return data


class CategoryCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de catégorie."""
    
    class Meta:
        model = Category
        fields = ['name', 'slug', 'description', 'parent', 'image', 'sort_order', 'is_active']

    def validate(self, data):
        """Vérifie l'unicité du nom et du slug dans l'organisation."""
        request = self.context.get('request')
        if not request:
            return data
            
        organization_id = request.headers.get('X-Organization-ID')
        if not organization_id:
            return data
        
        # Vérifier l'unicité du nom
        name = data.get('name')
        if name:
            queryset = Category.objects.filter(
                organization_id=organization_id,
                name__iexact=name,
                is_deleted=False
            )
            if queryset.exists():
                raise serializers.ValidationError({
                    'name': "Il existe déjà une catégorie avec ce même nom."
                })
        
        # Vérifier l'unicité du slug
        slug = data.get('slug')
        if slug:
            queryset = Category.objects.filter(
                organization_id=organization_id,
                slug=slug,
                is_deleted=False
            )
            if queryset.exists():
                raise serializers.ValidationError({
                    'slug': "Il existe déjà une catégorie avec ce même slug."
                })
        
        return data


# =============================================================================
# BRAND SERIALIZERS
# =============================================================================

class BrandSerializer(serializers.ModelSerializer):
    """Serializer pour les marques."""
    
    products_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Brand
        fields = ['id', 'name', 'slug', 'logo', 'is_active', 'products_count', 'created_at']
        read_only_fields = ['id', 'created_at']

    def get_products_count(self, obj):
        return obj.products.filter(is_deleted=False).count()

    def validate(self, data):
        """Vérifie l'unicité du nom et du slug dans l'organisation."""
        request = self.context.get('request')
        if request:
            organization_id = request.headers.get('X-Organization-ID')
            if organization_id:
                # Vérifier l'unicité du nom
                name = data.get('name')
                if name:
                    queryset = Brand.objects.filter(
                        organization_id=organization_id,
                        name__iexact=name,
                        is_deleted=False
                    )
                    if self.instance:
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if queryset.exists():
                        raise serializers.ValidationError({
                            'name': "Il existe déjà une marque avec ce même nom."
                        })
                
                # Vérifier l'unicité du slug
                slug = data.get('slug')
                if slug:
                    queryset = Brand.objects.filter(
                        organization_id=organization_id,
                        slug=slug,
                        is_deleted=False
                    )
                    if self.instance:
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if queryset.exists():
                        raise serializers.ValidationError({
                            'slug': "Il existe déjà une marque avec ce même slug."
                        })
        
        return data


# =============================================================================
# UNIT SERIALIZERS
# =============================================================================

class UnitSerializer(serializers.ModelSerializer):
    """Serializer pour les unités de mesure."""
    
    base_unit_name = serializers.CharField(source='base_unit.name', read_only=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'symbol', 'base_unit', 'base_unit_name',
            'conversion_factor', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

    def validate(self, data):
        """Vérifie l'unicité du nom et du symbole dans l'organisation."""
        request = self.context.get('request')
        if request:
            organization_id = request.headers.get('X-Organization-ID')
            if organization_id:
                # Vérifier l'unicité du nom
                name = data.get('name')
                if name:
                    queryset = Unit.objects.filter(
                        organization_id=organization_id,
                        name__iexact=name
                    )
                    if self.instance:
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if queryset.exists():
                        raise serializers.ValidationError({
                            'name': "Il existe déjà une unité avec ce même nom."
                        })
                
                # Vérifier l'unicité du symbole
                symbol = data.get('symbol')
                if symbol:
                    queryset = Unit.objects.filter(
                        organization_id=organization_id,
                        symbol__iexact=symbol
                    )
                    if self.instance:
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if queryset.exists():
                        raise serializers.ValidationError({
                            'symbol': "Il existe déjà une unité avec ce même symbole."
                        })
        
        return data


# =============================================================================
# PRODUCT IMAGE SERIALIZERS
# =============================================================================

class ProductImageSerializer(serializers.ModelSerializer):
    """Serializer pour les images produit."""
    
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'alt_text', 'sort_order', 'is_primary']
        read_only_fields = ['id']


# =============================================================================
# PRODUCT VARIANT SERIALIZERS
# =============================================================================

class ProductVariantSerializer(serializers.ModelSerializer):
    """Serializer pour les variantes produit."""
    
    effective_price = serializers.SerializerMethodField()
    
    class Meta:
        model = ProductVariant
        fields = [
            'id', 'name', 'sku', 'barcode', 'cost_price', 'selling_price',
            'attributes', 'image', 'is_active', 'effective_price'
        ]
        read_only_fields = ['id']

    def get_effective_price(self, obj):
        """Retourne le prix effectif (variant ou produit parent)."""
        return str(obj.get_price())


# =============================================================================
# PRICE LIST SERIALIZERS
# =============================================================================

class PriceListSerializer(serializers.ModelSerializer):
    """Serializer pour les listes de prix."""
    
    class Meta:
        model = PriceList
        fields = [
            'id', 'name', 'code', 'description', 'is_default', 'is_active',
            'valid_from', 'valid_until', 'discount_percentage', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ProductPriceSerializer(serializers.ModelSerializer):
    """Serializer pour les prix produit par liste."""
    
    price_list_name = serializers.CharField(source='price_list.name', read_only=True)
    
    class Meta:
        model = ProductPrice
        fields = ['id', 'price_list', 'price_list_name', 'price', 'min_quantity']
        read_only_fields = ['id']


# =============================================================================
# PRODUCT SERIALIZERS
# =============================================================================

class ProductListSerializer(serializers.ModelSerializer):
    """
    Serializer léger pour les listes de produits.
    Optimisé pour les performances (pas de nested serializers lourds).
    """
    
    category_name = serializers.CharField(source='category.name', read_only=True)
    brand_name = serializers.CharField(source='brand.name', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    packaging_unit_name = serializers.CharField(
        source='packaging_unit.name', read_only=True
    )
    packaging_unit_symbol = serializers.CharField(
        source='packaging_unit.symbol', read_only=True
    )
    stock_quantity = serializers.SerializerMethodField()
    stock_location = serializers.SerializerMethodField()
    warehouse_name = serializers.SerializerMethodField()
    stock_packages = serializers.SerializerMethodField()
    stock_loose = serializers.SerializerMethodField()
    stock_display = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'sku', 'barcode', 'image',
            'category', 'category_name', 'brand', 'brand_name',
            'unit', 'unit_symbol', 'unit_name',
            'selling_mode', 'packaging_unit',
            'packaging_unit_name', 'packaging_unit_symbol',
            'units_per_package', 'allow_auto_unpacking', 'wholesale_price', 'package_cost_price',
            'selling_price', 'cost_price',
            'is_taxable', 'tax_rate',
            'is_active', 'is_featured', 'track_inventory',
            'allow_negative_stock', 'reorder_point',
            'stock_quantity', 'stock_location', 'warehouse_name',
            'stock_packages', 'stock_loose', 'stock_display',
        ]
        read_only_fields = ['id']

    def _warehouse_id_from_request(self):
        request = self.context.get('request')
        if not request:
            return None
        wh = request.query_params.get('warehouse')
        return str(wh).strip() if wh else None

    def _stock_for_warehouse(self, obj):
        wh_id = self._warehouse_id_from_request()
        stocks = list(obj.stocks.all())
        if wh_id:
            for s in stocks:
                if str(s.warehouse_id) == wh_id:
                    return s
            return None
        return stocks[0] if len(stocks) == 1 else None

    def get_stock_quantity(self, obj):
        """Retourne le stock disponible = quantité − réservé (entrepôt filtré si ?warehouse=).

        En liste, ``total_stock`` est annoté par le ViewSet déjà net du réservé ;
        sinon on retombe sur ``available_quantity`` (idem net du réservé).
        """
        if hasattr(obj, 'total_stock') and obj.total_stock is not None:
            return obj.total_stock
        stock = self._stock_for_warehouse(obj)
        if stock:
            return stock.available_quantity
        return sum((s.available_quantity for s in obj.stocks.all()), 0)

    def get_stock_location(self, obj):
        """Emplacement dans l'entrepôt (rayon, allée, etc.)."""
        stock = self._stock_for_warehouse(obj)
        if stock and stock.location_id:
            return stock.location.name
        return None

    def get_warehouse_name(self, obj):
        """Nom de l'entrepôt associé au stock affiché."""
        stock = self._stock_for_warehouse(obj)
        if stock:
            return stock.warehouse.name
        return None

    def _packaging_split(self, obj):
        """
        Partage scellé/vrac du stock, ou ``None``.

        Volontairement limité au chemin « un entrepôt identifié » : en liste,
        ``stock_quantity`` peut provenir de l'annotation ``total_stock``, qui
        somme plusieurs entrepôts. Un nombre de paquets n'a alors aucun sens et
        surtout ne peut pas être reconstitué. On renvoie ``None`` plutôt que
        ``0`` : à l'écran, ``0`` ferait croire au caissier qu'aucun paquet n'est
        disponible. Le POS interroge toujours avec ``?warehouse=``, il obtient
        donc bien la valeur.
        """
        from apps.inventory.packaging import PackagingService

        factor = PackagingService.factor(obj)
        if factor is None:
            return None
        stock = self._stock_for_warehouse(obj)
        if stock is None:
            return None
        return PackagingService.available_split(stock, factor)

    def get_stock_packages(self, obj):
        split = self._packaging_split(obj)
        return split[0] if split else None

    def get_stock_loose(self, obj):
        split = self._packaging_split(obj)
        return str(split[1]) if split else None

    def get_stock_display(self, obj):
        """Quantité prête à afficher : « 1 paquet + 10 bouteilles »."""
        from apps.inventory.packaging import PackagingService

        stock = self._stock_for_warehouse(obj)
        if stock is None:
            quantity = self.get_stock_quantity(obj)
            return PackagingService.format_quantity(obj, quantity or 0)
        # Même partage que `stock_packages` / `stock_loose`, RENDU tel quel :
        # le repasser par `format_quantity` le ferait redécouper depuis le total
        # et les deux lectures pourraient se mettre à diverger.
        split = self._packaging_split(obj)
        if split is None:
            return PackagingService.format_quantity(obj, stock.available_quantity)
        sealed, loose = split
        return PackagingService.format_split(obj, sealed, loose)


class ProductDetailSerializer(serializers.ModelSerializer):
    """
    Serializer complet pour le détail d'un produit.
    Inclut les relations nested.
    """
    
    category_name = serializers.CharField(source='category.name', read_only=True)
    brand_name = serializers.CharField(source='brand.name', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    packaging_unit_name = serializers.CharField(
        source='packaging_unit.name', read_only=True
    )
    packaging_unit_symbol = serializers.CharField(
        source='packaging_unit.symbol', read_only=True
    )
    packaging_summary = serializers.SerializerMethodField()

    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    prices = ProductPriceSerializer(many=True, read_only=True)
    
    profit_margin = serializers.DecimalField(
        max_digits=5, decimal_places=2, read_only=True
    )
    price_with_tax = serializers.SerializerMethodField()
    stock_by_warehouse = serializers.SerializerMethodField()
    
    class Meta:
        model = Product
        fields = [
            'id', 'name', 'slug', 'sku', 'barcode',
            'short_description',
            'category', 'category_name',
            'brand', 'brand_name',
            'unit', 'unit_name', 'unit_symbol',
            'selling_mode', 'packaging_unit',
            'packaging_unit_name', 'packaging_unit_symbol',
            'units_per_package', 'allow_auto_unpacking', 'packaging_summary',
            'cost_price', 'selling_price', 'wholesale_price', 'package_cost_price',
            'tax_rate', 'is_taxable', 'price_with_tax',
            'profit_margin',
            'track_inventory', 'allow_negative_stock', 'has_expiry_date',
            'min_stock_level', 'max_stock_level',
            'reorder_point', 'reorder_quantity',
            'weight', 'dimensions',
            'image', 'images',
            'is_active', 'is_featured', 'is_sellable', 'is_purchasable',
            'expiry_tracking', 'batch_tracking', 'serial_tracking',
            'attributes',
            'variants', 'prices', 'stock_by_warehouse',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'profit_margin']

    def get_price_with_tax(self, obj):
        return str(obj.get_price_with_tax())

    def get_packaging_summary(self, obj):
        """
        Phrase de confirmation affichée sous le bloc conditionnement.

        Produite côté serveur pour que le formulaire, le POS et les écrans de
        stock disent tous exactement la même chose.
        """
        from apps.inventory.packaging import PackagingService, _plural

        factor = PackagingService.factor(obj)
        if factor is None:
            return None

        package = PackagingService.package_unit_label(obj) or 'conditionnement'
        retail = PackagingService.retail_unit_label(obj) or 'unité'
        # Pluriel délégué au service : lui seul respecte la casse du libellé
        # saisi, sans quoi « PIECE » devenait « PIECEs ».
        plural_retail = _plural(retail, 2)

        if obj.selling_mode == Product.SellingMode.WHOLESALE_ONLY:
            return (
                f"Ce produit sera vendu uniquement par {package} "
                f"de {factor} {plural_retail}."
            )
        return (
            f"Ce produit sera vendu par {package} de {factor} "
            f"{plural_retail}, ou à l'unité ({retail})."
        )

    def get_stock_by_warehouse(self, obj):
        """Retourne le stock par entrepôt, filtré par le scope warehouse du membership."""
        from apps.inventory.packaging import PackagingService

        stocks = obj.stocks.select_related('warehouse').all()

        request = self.context.get('request')
        if request:
            m = get_membership_for_request(request)
            if m:
                wh_ids = accessible_warehouse_ids(m)
                if wh_ids is not None:
                    stocks = stocks.filter(warehouse_id__in=wh_ids)

        factor = PackagingService.factor(obj)
        result = []
        for stock in stocks:
            entry = {
                'warehouse_id': str(stock.warehouse_id),
                'warehouse_name': stock.warehouse.name,
                'quantity': str(stock.quantity),
                'available': str(stock.available_quantity),
                'reserved': str(stock.reserved_quantity),
                # `display` porte le DISPONIBLE depuis l'origine : c'est ce que
                # le POS y lit. Le total en rayon et le réservé s'y ajoutent
                # sous leurs propres noms plutôt que de changer ce sens.
                'display': PackagingService.format_available(stock),
                'quantity_display': PackagingService.format_stock(stock),
                'reserved_display': PackagingService.format_base_total(
                    obj, stock.reserved_quantity
                ),
            }
            if factor is not None:
                sealed, loose = PackagingService.available_split(stock, factor)
                entry['packages'] = sealed
                entry['loose'] = str(loose)
                stored_packages, stored_loose = PackagingService.stored_split(stock, factor)
                entry['quantity_packages'] = stored_packages
                entry['quantity_loose'] = str(stored_loose)
            result.append(entry)
        return result


class ProductCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création de produit.
    Gère la validation et la génération automatique du SKU.
    """
    
    slug = serializers.SlugField(required=False, allow_blank=True)
    
    class Meta:
        model = Product
        fields = [
            # `id` est indispensable dans la réponse : l'appelant redirige vers
            # la fiche du produit qu'il vient de créer.
            'id',
            'name', 'slug', 'sku', 'barcode',
            'short_description',
            'category', 'brand', 'unit',
            'selling_mode', 'packaging_unit', 'units_per_package',
            'allow_auto_unpacking',
            'cost_price', 'selling_price', 'wholesale_price', 'package_cost_price',
            'tax_rate', 'is_taxable',
            'track_inventory', 'allow_negative_stock', 'has_expiry_date',
            'min_stock_level', 'max_stock_level',
            'reorder_point', 'reorder_quantity',
            'weight', 'dimensions',
            'image',
            'is_active', 'is_featured', 'is_sellable', 'is_purchasable',
            'expiry_tracking', 'batch_tracking', 'serial_tracking',
            'attributes'
        ]
        read_only_fields = ['id']

    def validate_sku(self, value):
        """Vérifie l'unicité du SKU dans l'organisation."""
        if not value:
            return value
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        queryset = Product.objects.filter(
            organization_id=organization,
            sku=value,
            is_deleted=False
        )
        
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        
        if queryset.exists():
            raise serializers.ValidationError("Il existe déjà un produit avec ce même SKU.")
        return value

    def validate_barcode(self, value):
        """Vérifie l'unicité du code-barres dans l'organisation."""
        if not value:
            return value
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        queryset = Product.objects.filter(
            organization_id=organization,
            barcode=value,
            is_deleted=False
        )
        
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        
        if queryset.exists():
            raise serializers.ValidationError("Il existe déjà un produit avec ce même code-barres.")
        return value

    def validate(self, data):
        """Validations globales."""
        self._validate_packaging(data)
        return data

    def _resolve(self, data, field):
        """Valeur soumise, ou valeur existante en mise à jour partielle."""
        if field in data:
            return data[field]
        return getattr(self.instance, field, None) if self.instance else None

    def _validate_packaging(self, data):
        """
        Cohérence du conditionnement et des prix.

        Les contrôles de structure (unités, nombre par conditionnement) restent
        ici : ils ne concernent que la fiche produit. Les règles de prix sont
        déléguées à `ProductPricingService`, partagé avec l'approvisionnement et
        l'import Excel, pour qu'il n'existe qu'une seule vérité.

        Les libellés sont volontairement dépourvus de jargon : le vendeur ne
        doit jamais lire « facteur de conversion ».
        """
        from apps.products.pricing import ProductPricingService

        selling_mode = self._resolve(data, 'selling_mode') or Product.SellingMode.RETAIL_ONLY
        units_per_package = self._resolve(data, 'units_per_package')
        is_packaged = selling_mode != Product.SellingMode.RETAIL_ONLY

        errors = {}
        if is_packaged:
            if not self._resolve(data, 'unit'):
                errors['unit'] = (
                    "Précisez l'unité de détail (bouteille, pièce, sachet…) pour "
                    "un produit vendu en gros."
                )
            if not self._resolve(data, 'packaging_unit'):
                errors['packaging_unit'] = (
                    "Précisez l'unité de gros (paquet, carton, casier…)."
                )
            if not units_per_package or int(units_per_package) < 2:
                errors['units_per_package'] = (
                    "Indiquez combien d'unités contient un conditionnement (au moins 2)."
                )

        # Le marchand achète au carton : s'il n'a saisi que ce prix-là, on en
        # déduit le coût unitaire, seule grandeur avec laquelle le coût moyen
        # pondéré et les marges savent travailler.
        resolved = ProductPricingService.resolve(
            selling_mode=selling_mode,
            units_per_package=units_per_package,
            cost_price=data.get('cost_price'),
            package_cost_price=data.get('package_cost_price'),
        )
        if 'cost_price' in resolved:
            data['cost_price'] = resolved['cost_price']

        errors.update(ProductPricingService.collect_errors(
            selling_mode=selling_mode,
            cost_price=data.get('cost_price'),
            selling_price=self._resolve(data, 'selling_price'),
            wholesale_price=self._resolve(data, 'wholesale_price'),
        ))

        if errors:
            raise serializers.ValidationError(errors)

        if is_packaged:
            self._validate_packaging_change(units_per_package)

    def _validate_packaging_change(self, units_per_package):
        """
        Interdit de changer le nombre d'unités par conditionnement quand cela
        réinterpréterait des données déjà enregistrées.

        On ne bloque pas dès qu'il existe du stock : activer le mode gros sur un
        produit déjà approvisionné est le cas d'usage le plus courant, et il est
        sans danger - le stock est en unité de détail et le partage scellé/vrac
        se déduit. On bloque seulement quand un emballage a déjà été ouvert ou
        quand des ventes en gros ont été enregistrées : leur relecture
        deviendrait fausse. L'historique n'est jamais recalculé.
        """
        if not self.instance:
            return
        previous = self.instance.units_per_package
        if previous is None or int(previous) == int(units_per_package):
            return

        from apps.inventory.models import Stock
        from apps.sales.models import SaleItem

        if Stock.objects.filter(
            product=self.instance, loose_quantity__gt=0
        ).exists():
            raise serializers.ValidationError({
                'units_per_package': (
                    "Impossible de modifier le conditionnement : des unités "
                    "sont déjà sorties d'un emballage. Écoulez ou régularisez "
                    "ce stock par un inventaire avant de le changer."
                )
            })

        if SaleItem.objects.filter(
            product=self.instance, package_quantity__gt=0
        ).exists():
            raise serializers.ValidationError({
                'units_per_package': (
                    "Impossible de modifier le conditionnement : des ventes en "
                    "gros ont déjà été enregistrées avec l'ancien "
                    "conditionnement. Créez un nouveau produit pour le nouveau "
                    "format."
                )
            })

    def create(self, validated_data):
        """Génère automatiquement un slug unique si non fourni."""
        from django.utils.text import slugify
        
        slug = validated_data.get('slug')
        if not slug:
            slug = slugify(validated_data['name'])
        
        organization = validated_data.get('organization')
        base_slug = slug
        counter = 1
        while Product.objects.filter(
            organization=organization, slug=slug, is_deleted=False
        ).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        validated_data['slug'] = slug
        return super().create(validated_data)


class ProductUpdateSerializer(ProductCreateSerializer):
    """Serializer pour la mise à jour de produit."""
    
    class Meta(ProductCreateSerializer.Meta):
        pass


class ProductBulkUpdateSerializer(serializers.Serializer):
    """Serializer pour la mise à jour en masse de produits."""
    
    ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1
    )
    is_active = serializers.BooleanField(required=False)
    is_featured = serializers.BooleanField(required=False)
    category = serializers.UUIDField(required=False)
    brand = serializers.UUIDField(required=False)
