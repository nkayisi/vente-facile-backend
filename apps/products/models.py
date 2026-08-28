from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal
from apps.core.models import TenantSoftDeleteModel, TenantModel, TenantSyncableModel
from apps.core.managers import TenantSoftDeleteManager


class Category(TenantSoftDeleteModel):
    """Product categories with hierarchical support."""
    
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True)
    
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children'
    )
    
    image = models.ImageField(upload_to='categories/', null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'categories'
        verbose_name_plural = 'categories'
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
            models.Index(fields=['parent']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'parent', 'slug'],
                condition=models.Q(is_deleted=False),
                name='unique_category_slug_per_parent_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'parent', 'name'],
                condition=models.Q(is_deleted=False),
                name='unique_category_name_per_parent_org'
            ),
        ]

    def __str__(self):
        return self.name

    @classmethod
    def subtree_ids(cls, root_ids):
        """
        Renvoie les identifiants d'un ou plusieurs sous-arbres, racines incluses.

        Filtrer un rapport sur « Boissons » doit ramener les lignes classées dans
        « Boissons > Sodas » : sans la descendance, un tirage par catégorie sous
        estime silencieusement, ce qui est pire qu'une erreur visible.

        Parcours en largeur, borné par le jeu déjà visité : une hiérarchie
        accidentellement cyclique ralentit la requête mais ne la fait pas boucler.
        """
        collected = {rid for rid in (root_ids or []) if rid}
        if not collected:
            return set()

        pending = list(collected)
        while pending:
            children = list(
                cls.objects.filter(parent_id__in=pending)
                .values_list('id', flat=True)
            )
            fresh = [cid for cid in children if cid not in collected]
            collected.update(fresh)
            pending = fresh

        return collected

    def descendant_ids(self):
        """Identifiants de cette catégorie et de toute sa descendance."""
        return type(self).subtree_ids([self.id])

    def get_ancestors(self):
        """Get all parent categories. Safe even si un cycle existait
        accidentellement (limite via set d'IDs visités)."""
        ancestors = []
        seen = set()
        current = self.parent
        while current and current.id not in seen:
            ancestors.append(current)
            seen.add(current.id)
            current = current.parent
        return ancestors[::-1]

    def clean(self):
        """
        Empêche les cycles parent → enfant → ... → soi-même.

        Sans ce contrôle, ``get_ancestors`` (et toute fonction d'arbre)
        peut boucler indéfiniment. La contrainte ``parent != self`` est
        triviale ; les cycles indirects (A → B → A) sont détectés en
        remontant la chaîne et en s'arrêtant si l'on retombe sur ``self.id``.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        super().clean()

        if self.parent_id is None:
            return

        if self.pk and self.parent_id == self.pk:
            raise DjangoValidationError({
                'parent': "Une catégorie ne peut pas être son propre parent."
            })

        if self.pk:
            visited = {self.pk}
            cursor = self.parent
            while cursor is not None:
                if cursor.pk in visited:
                    raise DjangoValidationError({
                        'parent': (
                            "Cycle détecté dans la hiérarchie des catégories. "
                            "Le parent choisi est déjà un descendant."
                        )
                    })
                visited.add(cursor.pk)
                cursor = cursor.parent

    def save(self, *args, **kwargs):
        # Exécuter la validation anti-cycle même si le caller n'appelle pas
        # full_clean explicitement (ORM direct, scripts, admin).
        self.clean()
        super().save(*args, **kwargs)


class Brand(TenantSoftDeleteModel):
    """Product brands/manufacturers."""
    
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    logo = models.ImageField(upload_to='brands/', null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'brands'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'slug'],
                condition=models.Q(is_deleted=False),
                name='unique_brand_slug_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'name'],
                condition=models.Q(is_deleted=False),
                name='unique_brand_name_per_org'
            ),
        ]

    def __str__(self):
        return self.name


class Unit(TenantModel):
    """Units of measurement for products."""
    
    name = models.CharField(max_length=50)
    symbol = models.CharField(max_length=10)
    
    base_unit = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_units'
    )
    conversion_factor = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('1.0000')
    )

    class Meta:
        db_table = 'units'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'symbol'],
                name='unique_unit_symbol_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'name'],
                name='unique_unit_name_per_org'
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class Product(TenantSyncableModel):
    """
    Main product model.
    Supports variants, multiple prices, and inventory tracking.

    Conditionnement (vente en gros et en détail)
    -------------------------------------------
    Un produit peut être vendu à la pièce, par conditionnement (paquet, carton,
    casier…), ou dans les deux formes. Quel que soit le mode :

    - ``unit`` est l'unité de **détail**, qui est aussi l'unité de base du stock ;
    - ``packaging_unit`` est l'unité de **gros** ;
    - ``units_per_package`` est le nombre d'unités de détail dans un conditionnement ;
    - ``selling_price`` est le prix à l'unité de détail ;
    - ``wholesale_price`` est le prix d'un conditionnement entier **dès lors que
      ``selling_mode`` n'est pas ``retail_only``**. Pour les produits en vente au
      détail seule, ce champ conserve son usage historique de prix de gros
      indicatif à la pièce.

    Le stock reste **toujours** exprimé en unité de détail, y compris en mode
    ``wholesale_only`` : c'est ce qui garantit que les lots FIFO, le coût moyen
    pondéré et les rapports restent cohérents entre les modes.
    """

    class SellingMode(models.TextChoices):
        RETAIL_ONLY = 'retail_only', 'Vente au détail uniquement'
        WHOLESALE_ONLY = 'wholesale_only', 'Vente en gros uniquement'
        WHOLESALE_AND_RETAIL = 'wholesale_and_retail', 'Vente en gros et au détail'

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    sku = models.CharField(max_length=100, db_index=True)
    barcode = models.CharField(max_length=100, blank=True, db_index=True)
    
    short_description = models.CharField(max_length=500, blank=True)
    
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )

    selling_mode = models.CharField(
        max_length=20,
        choices=SellingMode.choices,
        default=SellingMode.RETAIL_ONLY,
        help_text="Forme sous laquelle ce produit est vendu."
    )
    packaging_unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='packaged_products',
        help_text="Unité de gros (paquet, carton, casier…)."
    )
    units_per_package = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Nombre d'unités de détail contenues dans un conditionnement."
    )
    allow_auto_unpacking = models.BooleanField(
        default=True,
        help_text=(
            "Autorise l'ouverture automatique d'un conditionnement lorsque le "
            "stock à l'unité est insuffisant pour servir une vente au détail."
        )
    )

    cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    selling_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    wholesale_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    package_cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text=(
            "Prix d'achat d'un conditionnement entier. Confort de saisie pour "
            "le marchand qui achète au carton ; `cost_price` (à l'unité) reste "
            "la valeur utilisée pour le coût moyen pondéré et les marges."
        )
    )
    
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )
    is_taxable = models.BooleanField(default=True)
    
    track_inventory = models.BooleanField(default=True)
    allow_negative_stock = models.BooleanField(default=False)
    has_expiry_date = models.BooleanField(
        default=False,
        help_text="Indique si ce produit est périssable et nécessite un suivi des dates d'expiration"
    )
    min_stock_level = models.PositiveIntegerField(default=0)
    max_stock_level = models.PositiveIntegerField(null=True, blank=True)
    reorder_point = models.PositiveIntegerField(default=0)
    reorder_quantity = models.PositiveIntegerField(default=0)
    
    weight = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True
    )
    dimensions = models.JSONField(default=dict, blank=True)
    
    image = models.ImageField(upload_to='products/', null=True, blank=True)
    
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    is_sellable = models.BooleanField(default=True)
    is_purchasable = models.BooleanField(default=True)
    
    expiry_tracking = models.BooleanField(default=False)
    batch_tracking = models.BooleanField(default=False)
    serial_tracking = models.BooleanField(default=False)
    
    attributes = models.JSONField(default=dict, blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_products'
    )

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'products'
        ordering = ['name']
        indexes = [
            # Index du tirage de synchronisation. L'ordre `(organisation,
            # updated_at, id)` est celui de la pagination par curseur : sans lui,
            # chaque page fait un balayage complet suivi d'un tri.
            models.Index(fields=['organization', 'updated_at', 'id']),
            models.Index(fields=['organization', 'is_active']),
            models.Index(fields=['organization', 'category']),
            models.Index(fields=['sku']),
            models.Index(fields=['barcode']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'sku'],
                condition=models.Q(is_deleted=False),
                name='unique_product_sku_per_org'
            ),
            models.UniqueConstraint(
                fields=['organization', 'slug'],
                condition=models.Q(is_deleted=False),
                name='unique_product_slug_per_org'
            ),
            # Garde-fous de conditionnement au niveau base : l'import Excel
            # (`ProductExcelService`) écrit des produits hors DRF, la validation
            # des serializers ne suffit donc pas.
            models.CheckConstraint(
                condition=(
                    models.Q(units_per_package__isnull=True)
                    | models.Q(units_per_package__gte=2)
                ),
                name='product_units_per_package_gte_2',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(selling_mode='retail_only')
                    | models.Q(units_per_package__isnull=False)
                ),
                name='product_packaging_requires_units_per_package',
            ),
        ]
        permissions = [
            ('view_cost_price', 'Can view product cost price'),
            ('change_price', 'Can change product prices'),
            ('export_products', 'Can export products'),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def profit_margin(self):
        """Calculate profit margin percentage."""
        if self.cost_price and self.cost_price > 0:
            return ((self.selling_price - self.cost_price) / self.cost_price) * 100
        return Decimal('0.00')

    def get_price_with_tax(self):
        """Get selling price including tax."""
        if self.is_taxable:
            return self.selling_price * (1 + self.tax_rate / 100)
        return self.selling_price


class ProductImage(TenantModel):
    """Additional product images."""
    
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='images'
    )
    image = models.ImageField(upload_to='products/gallery/')
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = 'product_images'
        ordering = ['sort_order']

    def __str__(self):
        return f"Image for {self.product.name}"


class ProductVariant(TenantSyncableModel):
    """
    Product variants (e.g., size, color combinations).
    Each variant can have its own SKU, barcode, and price.
    """
    
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='variants'
    )
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100)
    barcode = models.CharField(max_length=100, blank=True)
    
    cost_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    selling_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    
    attributes = models.JSONField(default=dict)
    image = models.ImageField(upload_to='products/variants/', null=True, blank=True)
    
    is_active = models.BooleanField(default=True)

    objects = TenantSoftDeleteManager()

    class Meta:
        db_table = 'product_variants'
        indexes = [
            # Index du tirage de synchronisation. L'ordre `(organisation,
            # updated_at, id)` est celui de la pagination par curseur : sans lui,
            # chaque page fait un balayage complet suivi d'un tri.
            models.Index(fields=['organization', 'updated_at', 'id']),
            models.Index(fields=['product']),
            models.Index(fields=['sku']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'sku'],
                condition=models.Q(is_deleted=False),
                name='unique_variant_sku_per_org'
            ),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.name}"

    def get_price(self):
        """Get variant price or fallback to product price."""
        return self.selling_price or self.product.selling_price


class PriceList(TenantModel):
    """
    Multiple price lists for different customer types.
    E.g., retail, wholesale, VIP customers.
    """
    
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    description = models.TextField(blank=True)
    
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00')
    )

    class Meta:
        db_table = 'price_lists'
        unique_together = ['organization', 'code']

    def __str__(self):
        return self.name


class ProductPrice(TenantModel):
    """Product-specific prices for different price lists."""
    
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='prices'
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='prices'
    )
    price_list = models.ForeignKey(
        PriceList,
        on_delete=models.CASCADE,
        related_name='product_prices'
    )
    
    price = models.DecimalField(max_digits=15, decimal_places=2)
    min_quantity = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = 'product_prices'
        unique_together = ['product', 'variant', 'price_list', 'min_quantity']

    def __str__(self):
        return f"{self.product.name} - {self.price_list.name}: {self.price}"
