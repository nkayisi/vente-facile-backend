"""
Serializers DRF pour l'app Sales (POS).
"""
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError
from decimal import Decimal
from django.db import transaction
from .models import (
    Register, RegisterSession, RegisterSessionCurrencyBalance,
    Sale, SaleItem, PaymentMethod, Payment,
    SaleReturn, SaleReturnItem, Quotation, QuotationItem
)
from .sale_validation import max_sale_discount_percent
from apps.core.warehouse_scope import accessible_warehouse_ids, get_membership_for_request


# =============================================================================
# REGISTER SERIALIZERS
# =============================================================================

class RegisterSerializer(serializers.ModelSerializer):
    """Serializer pour les caisses."""
    
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    current_session = serializers.SerializerMethodField()
    
    class Meta:
        model = Register
        fields = [
            'id', 'name', 'code', 'branch', 'branch_name',
            'warehouse', 'warehouse_name', 'is_active',
            'receipt_header', 'receipt_footer', 'current_session',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']
        # La succursale n'est plus saisie dans l'UI : elle suit l'entrepôt et est
        # dérivée côté serveur si absente (cf. validate).
        extra_kwargs = {
            'branch': {'required': False, 'allow_null': True},
        }

    @staticmethod
    def _default_branch_for(organization_id):
        """Succursale par défaut d'une organisation : la principale (``is_main``),
        sinon la première par ordre alphabétique."""
        from apps.organizations.models import Branch
        qs = Branch.objects.filter(organization_id=organization_id, is_deleted=False)
        return qs.filter(is_main=True).first() or qs.order_by('name').first()

    def validate(self, attrs):
        instance = getattr(self, 'instance', None)

        if 'warehouse' in attrs:
            warehouse = attrs['warehouse']
        else:
            warehouse = instance.warehouse if instance is not None else None

        if warehouse is None:
            raise serializers.ValidationError(
                {'warehouse': 'Entrepôt requis.'}
            )

        # La succursale suit l'entrepôt : succursale fournie > succursale de
        # l'entrepôt > succursale principale de l'org > succursale existante.
        branch = (
            attrs.get('branch')
            or warehouse.branch
            or self._default_branch_for(warehouse.organization_id)
        )
        if branch is None and instance is not None:
            branch = instance.branch

        if branch is None:
            raise serializers.ValidationError(
                {'branch': 'Aucune succursale disponible pour cette organisation.'}
            )

        if warehouse.organization_id != branch.organization_id:
            raise serializers.ValidationError({
                'warehouse':
                    'L\'entrepôt doit appartenir à la même organisation que la succursale.'
            })

        wh_branch_id = getattr(warehouse, 'branch_id', None)
        if wh_branch_id is not None and wh_branch_id != branch.id:
            raise serializers.ValidationError({
                'warehouse': 'Cet entrepôt est rattaché à une autre succursale.'
            })

        # Injecter la succursale dérivée pour qu'elle soit persistée même quand
        # le client ne l'envoie pas.
        attrs['branch'] = branch
        return attrs

    def get_current_session(self, obj):
        """Retourne la session active si elle existe."""
        session = obj.sessions.filter(status='open').first()
        if session:
            return {
                'id': str(session.id),
                'opened_by': session.opened_by.full_name,
                'opening_balance': str(session.opening_balance),
                'opened_at': session.opened_at
            }
        return None


# =============================================================================
# REGISTER SESSION SERIALIZERS
# =============================================================================

class RegisterSessionCurrencyBalanceSerializer(serializers.ModelSerializer):
    """Solde de caisse d'une session, ventilé par devise."""

    class Meta:
        model = RegisterSessionCurrencyBalance
        fields = [
            'currency', 'opening_balance', 'expected_balance',
            'counted_balance', 'difference',
        ]


class RegisterSessionListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de sessions."""

    currency_balances = RegisterSessionCurrencyBalanceSerializer(many=True, read_only=True)
    register_name = serializers.CharField(source='register.name', read_only=True)
    warehouse = serializers.CharField(source='register.warehouse.id', read_only=True)
    warehouse_name = serializers.CharField(source='register.warehouse.name', read_only=True)
    opened_by_name = serializers.CharField(source='opened_by.full_name', read_only=True)
    closed_by_name = serializers.CharField(source='closed_by.full_name', read_only=True)
    sales_count = serializers.SerializerMethodField()
    sales_total = serializers.SerializerMethodField()
    
    class Meta:
        model = RegisterSession
        fields = [
            'id', 'register', 'register_name',
            'warehouse', 'warehouse_name',
            'opened_by', 'opened_by_name',
            'closed_by', 'closed_by_name',
            'status', 'opening_balance', 'closing_balance',
            'expected_balance', 'difference',
            'currency_balances',
            'opened_at', 'closed_at',
            'sales_count', 'sales_total'
        ]
        read_only_fields = ['id', 'opened_at']

    def get_sales_count(self, obj):
        return obj.sales.filter(status='completed').count()

    def get_sales_total(self, obj):
        total = obj.sales.filter(status='completed').aggregate(
            total=serializers.models.Sum('total')
        )['total']
        return str(total or Decimal('0.00'))


class RegisterSessionDetailSerializer(RegisterSessionListSerializer):
    """Serializer complet pour le détail d'une session."""
    
    payments_summary = serializers.SerializerMethodField()
    
    class Meta(RegisterSessionListSerializer.Meta):
        fields = RegisterSessionListSerializer.Meta.fields + ['notes', 'payments_summary']

    def get_payments_summary(self, obj):
        """Résumé des paiements par méthode."""
        from django.db.models import Sum
        
        payments = Payment.objects.filter(
            sale__session=obj,
            status='completed'
        ).values('payment_method__name').annotate(
            total=Sum('amount')
        )
        
        return [
            {'method': p['payment_method__name'], 'total': str(p['total'])}
            for p in payments
        ]


class CurrencyAmountSerializer(serializers.Serializer):
    """Couple (devise, montant) pour les saisies multi-devise de caisse."""
    currency = serializers.CharField(max_length=3)
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)


class RegisterSessionOpenSerializer(serializers.Serializer):
    """
    Ouverture de session.

    Le solde d'ouverture est hérité automatiquement, PAR DEVISE, de la dernière
    session fermée sur cette caisse (ou 0). `opening_balances` permet de surcharger
    les fonds d'ouverture par devise (ex. [{"currency": "USD", "amount": 20}]).
    """

    register = serializers.UUIDField()
    opening_balances = CurrencyAmountSerializer(many=True, required=False)


class RegisterSessionCloseSerializer(serializers.Serializer):
    """
    Fermeture de session avec comptage manuel optionnel, PAR DEVISE.

    - `counted_balances` : comptage réel par devise (ex. [{"currency":"USD","amount":20}]).
    - `counted_balance` : compat mono-devise (appliqué à la devise principale).
    - `notes` : obligatoire si un écart est non nul dans une devise.
    """

    counted_balance = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    counted_balances = CurrencyAmountSerializer(many=True, required=False)
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=2000,
        default='',
    )


# =============================================================================
# PAYMENT METHOD SERIALIZERS
# =============================================================================

class PaymentMethodSerializer(serializers.ModelSerializer):
    """Serializer pour les méthodes de paiement."""
    
    method_type_display = serializers.CharField(
        source='get_method_type_display', read_only=True
    )
    
    class Meta:
        model = PaymentMethod
        fields = [
            'id', 'name', 'code', 'method_type', 'method_type_display',
            'is_active', 'is_default', 'requires_reference', 'icon'
        ]
        read_only_fields = ['id']


# =============================================================================
# SALE ITEM SERIALIZERS
# =============================================================================

class SaleItemSerializer(serializers.ModelSerializer):
    """Serializer pour les lignes de vente."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    variant_name = serializers.CharField(source='variant.name', read_only=True)
    loose_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=3, read_only=True
    )
    quantity_display = serializers.SerializerMethodField()
    package_unit_name = serializers.CharField(
        source='product.packaging_unit.name', read_only=True
    )
    unit_name = serializers.CharField(source='product.unit.name', read_only=True)

    class Meta:
        model = SaleItem
        fields = [
            'id', 'product', 'product_name', 'product_sku',
            'variant', 'variant_name', 'batch',
            'description', 'quantity', 'unit_price', 'cost_price',
            'package_quantity', 'package_unit_price', 'packaging_factor',
            'loose_quantity', 'quantity_display',
            'package_unit_name', 'unit_name',
            'discount_amount', 'discount_percentage',
            'tax_rate', 'tax_amount',
            'subtotal', 'total', 'notes'
        ]
        read_only_fields = ['id', 'subtotal', 'total', 'tax_amount']

    def get_quantity_display(self, obj):
        """
        Ligne lisible pour le client : « 2 paquets + 3 bouteilles ».

        Calculée ici pour que le ticket, la fiche de vente et l'historique
        disent la même chose sans réimplémenter la mise en forme.
        """
        from apps.inventory.packaging import PackagingService

        if not obj.package_quantity or not obj.packaging_factor:
            return PackagingService.format_quantity(obj.product, obj.quantity)
        return PackagingService.format_quantity(
            obj.product, obj.quantity, obj.loose_quantity
        )


class SaleItemCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création de ligne de vente.

    Pour un produit vendu en gros, le caissier envoie ``package_quantity`` et
    ``loose_quantity`` ; le serveur en déduit ``quantity`` (unité de base) et
    fige le conditionnement du moment. Une ligne peut mêler les deux formes -
    « 2 paquets + 3 bouteilles ».
    """

    loose_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=3, required=False, write_only=True
    )

    class Meta:
        model = SaleItem
        fields = [
            'product', 'variant', 'batch',
            'quantity', 'unit_price',
            'package_quantity', 'package_unit_price', 'loose_quantity',
            'discount_percentage', 'tax_rate', 'notes'
        ]
        extra_kwargs = {'quantity': {'required': False}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope FK par organisation : le serializer parent (SaleCreate)
        # transmet son contexte via ``many=True``, donc le helper trouve la
        # request et filtre Product/ProductVariant/StockBatch.
        from apps.core.serializer_helpers import scope_fk_to_org
        scope_fk_to_org(self, 'product', 'variant', 'batch')

    def validate_tax_rate(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Le taux de taxe ne peut pas être négatif.")
        if value is not None and value > 100:
            raise serializers.ValidationError("Le taux de taxe ne peut pas dépasser 100%.")
        return value

    def validate_product(self, value):
        # Bloque les produits désactivés ou non-vendables. La vérification
        # cross-tenant est portée par ``scope_fk_to_org`` (queryset filtré).
        if value is None:
            return value
        if not value.is_active:
            raise serializers.ValidationError(
                f"Le produit « {value.name} » est désactivé et ne peut pas être vendu."
            )
        if not value.is_sellable:
            raise serializers.ValidationError(
                f"Le produit « {value.name} » n'est pas marqué comme vendable."
            )
        return value

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("La quantité doit être positive.")
        return value

    def validate_unit_price(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Le prix unitaire ne peut pas être négatif.")
        return value

    def validate(self, data):
        """Convertit la saisie en conditionnements vers l'unité de base."""
        from apps.inventory.packaging import PackagingService

        product = data.get('product')
        package_quantity = data.get('package_quantity') or Decimal('0.000')
        loose_quantity = data.pop('loose_quantity', None)
        factor = PackagingService.factor(product) if product else None

        if package_quantity > 0 and factor is None:
            raise serializers.ValidationError({
                'package_quantity': (
                    f"« {product.name} » n'est pas vendu par conditionnement."
                )
            })

        if factor is not None and (package_quantity > 0 or loose_quantity is not None):
            loose_quantity = loose_quantity or Decimal('0.000')

            if product.selling_mode == 'wholesale_only' and loose_quantity > 0:
                raise serializers.ValidationError({
                    'loose_quantity': (
                        f"« {product.name} » se vend uniquement par "
                        f"conditionnement entier."
                    )
                })

            # Le facteur est un entier : vendre une fraction d'unité de détail
            # rendrait le partage scellé/vrac incalculable.
            if package_quantity % 1 or loose_quantity % 1:
                raise serializers.ValidationError({
                    'quantity': (
                        f"« {product.name} » se vend par unités entières."
                    )
                })

            data['quantity'] = PackagingService.to_base(
                product, package_quantity, loose_quantity
            )
            # Snapshot posé par le serveur : jamais accepté du client, sinon un
            # appel forgé pourrait réécrire le contenu d'un conditionnement.
            data['packaging_factor'] = factor
            data['package_quantity'] = package_quantity

        if not data.get('quantity'):
            raise serializers.ValidationError({
                'quantity': "Indiquez une quantité."
            })

        self._validate_package_price(data, factor)
        return data

    def _validate_package_price(self, data, factor):
        """
        Garde-fou sur le prix du conditionnement.

        Le prix ne peut pas être relu depuis le produit : le POS convertit les
        prix dans la devise de facture avant de les envoyer, et un prix relu en
        devise principale serait faux sur une facture en devise étrangère. On
        vérifie donc une règle métier vraie dans **toute** devise : un
        conditionnement ne coûte jamais plus cher que la somme de son contenu.
        """
        package_quantity = data.get('package_quantity') or Decimal('0.000')
        if package_quantity <= 0 or not factor:
            return

        package_unit_price = data.get('package_unit_price')
        if package_unit_price is None:
            raise serializers.ValidationError({
                'package_unit_price': "Indiquez le prix du conditionnement."
            })
        if package_unit_price < 0:
            raise serializers.ValidationError({
                'package_unit_price': "Le prix ne peut pas être négatif."
            })

        unit_price = data.get('unit_price') or Decimal('0.00')
        if unit_price > 0 and package_unit_price > unit_price * factor:
            raise serializers.ValidationError({
                'package_unit_price': (
                    "Le prix du conditionnement dépasse le prix de son contenu "
                    "vendu à l'unité."
                )
            })

    def validate_discount_percentage(self, value):
        if value < 0:
            raise serializers.ValidationError("La remise ne peut pas être négative.")
        request = self.context.get("request")
        max_p = Decimal("50")
        if request:
            org_id = request.headers.get("X-Organization-ID")
            if org_id:
                from apps.organizations.models import Organization
                org = Organization.objects.filter(id=org_id).first()
                if org:
                    max_p = max_sale_discount_percent(org)
        if value > max_p:
            raise serializers.ValidationError(f"La remise par article ne peut pas dépasser {max_p}%.")
        return value


# =============================================================================
# PAYMENT SERIALIZERS
# =============================================================================

class PaymentSerializer(serializers.ModelSerializer):
    """Serializer pour les paiements."""
    
    payment_method_name = serializers.CharField(
        source='payment_method.name', read_only=True
    )
    received_by_name = serializers.CharField(
        source='received_by.full_name', read_only=True
    )
    
    class Meta:
        model = Payment
        fields = [
            'id', 'payment_method', 'payment_method_name',
            'amount', 'tendered_amount', 'currency', 'exchange_rate',
            'reference', 'status',
            'received_by', 'received_by_name',
            'paid_at', 'notes'
        ]
        read_only_fields = ['id', 'paid_at']


class PaymentCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création de paiement.

    ``tendered_amount`` = montant réellement remis, dans ``currency``. Pour la
    rétro-compatibilité, ``amount`` seul est accepté et traité comme le montant
    remis. La conversion vers la devise de la vente est faite côté service.
    """

    amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True
    )
    tendered_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True
    )

    class Meta:
        model = Payment
        fields = ['payment_method', 'amount', 'tendered_amount', 'currency', 'exchange_rate', 'reference', 'notes']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.core.serializer_helpers import scope_fk_to_org
        scope_fk_to_org(self, 'payment_method')

    def validate(self, data):
        tendered = data.get('tendered_amount')
        amount = data.get('amount')
        effective = tendered if tendered is not None else amount
        if effective is None:
            raise serializers.ValidationError(
                "Le montant du règlement (tendered_amount ou amount) est requis."
            )
        if effective <= 0:
            raise serializers.ValidationError("Le montant doit être strictement positif.")
        return data

    def validate_exchange_rate(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError("Le taux de change doit être strictement positif.")
        return value


# =============================================================================
# SALE SERIALIZERS
# =============================================================================

class SaleListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de ventes."""
    
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    sold_by_name = serializers.CharField(source='sold_by.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    sale_type_display = serializers.CharField(source='get_sale_type_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Sale
        fields = [
            'id', 'reference', 'customer', 'customer_name',
            'sale_type', 'sale_type_display',
            'status', 'status_display',
            'subtotal', 'tax_amount', 'discount_percentage', 'discount_amount', 'total',
            'amount_paid', 'amount_due',
            'sold_by', 'sold_by_name',
            'sale_date', 'items_count', 'is_pos'
        ]
        read_only_fields = ['id', 'reference', 'sale_date']

    def get_items_count(self, obj):
        # Utilise l'annotation `_items_count` posée par SaleViewSet.get_queryset
        # pour la liste (évite un COUNT par ligne). Repli sur un count direct si
        # le serializer est utilisé hors de ce contexte.
        count = getattr(obj, '_items_count', None)
        if count is not None:
            return count
        return obj.items.count()


def _points_out(value):
    """
    Points rendus au client HTTP en **nombre**, jamais en chaîne.

    Un ``Decimal`` traverse le rendu JSON de DRF sous forme de chaîne selon le
    contexte ; ces quatre champs sont lus directement par le reçu et le POS, qui
    les comparent et les affichent. On fixe la forme ici une fois pour toutes.
    """
    quantized = Decimal(value or 0).quantize(Decimal('0.01'))
    # Un solde entier reste un entier à l'écran : « 3 pts », pas « 3.0 pts ».
    return int(quantized) if quantized == quantized.to_integral_value() else float(quantized)


class SaleDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une vente."""
    
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    sold_by_name = serializers.CharField(source='sold_by.full_name', read_only=True)
    register_name = serializers.CharField(source='register.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    
    items = SaleItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    unpacking_notices = serializers.SerializerMethodField()

    loyalty_points_earned = serializers.SerializerMethodField()
    loyalty_points_used = serializers.SerializerMethodField()
    loyalty_points_balance = serializers.SerializerMethodField()
    loyalty_program_active = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = [
            'id', 'reference',
            'session', 'register', 'register_name',
            'warehouse', 'warehouse_name',
            'customer', 'customer_name', 'customer_phone',
            'sale_type', 'status', 'price_list',
            'subtotal', 'tax_amount', 'discount_amount', 'discount_percentage',
            'loyalty_redemption_amount', 'total',
            'amount_paid', 'amount_due', 'change_amount', 'change_currency',
            'currency', 'exchange_rate',
            'notes', 'internal_notes',
            'sold_by', 'sold_by_name',
            'sale_date', 'due_date', 'is_pos', 'receipt_printed',
            'items', 'payments', 'unpacking_notices',
            'loyalty_points_earned', 'loyalty_points_used',
            'loyalty_points_balance', 'loyalty_program_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'sale_date', 'created_at', 'updated_at']

    def get_unpacking_notices(self, obj):
        """
        Emballages ouverts automatiquement pour servir cette vente.

        Le caissier est informé sans être interrompu : l'opération a déjà eu
        lieu. Le message est reconstitué depuis les mouvements enregistrés, il
        reste donc consultable après coup sur la fiche de vente.
        """
        from apps.inventory.models import Stock, StockMovement
        from apps.inventory.packaging import PackagingService, _plural

        movements = StockMovement.objects.filter(
            reference_type='sale',
            reference_id=obj.id,
            movement_type='unpack',
        ).select_related('product', 'product__unit', 'product__packaging_unit')

        notices = []
        for movement in movements:
            product = movement.product
            opened = int(movement.input_package_quantity or 0)
            factor = movement.packaging_factor or 0
            package_word = _plural(
                PackagingService.package_unit_label(product) or 'conditionnement',
                opened,
            )

            remaining = None
            stock = Stock.objects.filter(
                organization=obj.organization,
                product=product,
                variant=movement.variant,
                warehouse=movement.warehouse,
            ).first()
            if stock:
                remaining = PackagingService.format_quantity(
                    product, stock.quantity, stock.loose_quantity
                )

            message = (
                f"Stock détail insuffisant : {opened} {package_word} "
                f"{'ont' if opened > 1 else 'a'} été "
                f"{'ouverts' if opened > 1 else 'ouvert'} automatiquement "
                f"({opened * factor} "
                f"{PackagingService.retail_unit_label(product) or 'unités'})."
            )
            if remaining:
                message += f" Il reste {remaining}."

            notices.append({
                'product': str(product.id),
                'product_name': product.name,
                'packages_opened': opened,
                'message': message,
            })
        return notices

    # -- Fidélité -----------------------------------------------------------
    #
    # Ces quatre champs existent pour que le reçu n'ait plus à rejouer le barème
    # du programme. Le POS le faisait, avec la seule formule « pourcentage » en
    # dur : sur un programme `fixed_per_amount` (le défaut), une vente de 5 000
    # CDF imprimait 50 points quand la base en enregistrait 5. On ne recalcule
    # donc rien ici, on lit le registre `LoyaltyTransaction`, qui est la seule
    # trace de ce qui a réellement été crédité ou consommé.

    def _loyalty_rows(self, obj):
        """Lignes de fidélité de cette vente, chargées une seule fois."""
        cached = getattr(obj, '_loyalty_rows_cache', None)
        if cached is None:
            from apps.settings.models import LoyaltyTransaction

            cached = list(
                LoyaltyTransaction.objects
                .filter(sale=obj)
                .values_list('transaction_type', 'points')
            )
            obj._loyalty_rows_cache = cached
        return cached

    def get_loyalty_points_earned(self, obj):
        """
        Points nets gagnés sur cette vente : les gains, moins leur annulation
        éventuelle. Un reçu réimprimé après annulation ne doit pas annoncer des
        points que le client n'a plus.
        """
        TxType = self._tx_types()
        return _points_out(sum(
            (points for kind, points in self._loyalty_rows(obj)
             if kind in (TxType.EARN, TxType.EARN_REVERSAL)),
            Decimal('0.00'),
        ))

    def get_loyalty_points_used(self, obj):
        """
        Points nets consommés, remise à la création et règlements ultérieurs
        confondus. Retourné positif : c'est une quantité utilisée, le signe est
        porté par le libellé du reçu.
        """
        TxType = self._tx_types()
        total = sum(
            (points for kind, points in self._loyalty_rows(obj)
             if kind in (TxType.REDEEM, TxType.REDEEM_REVERSAL)),
            Decimal('0.00'),
        )
        return _points_out(-total)

    def get_loyalty_points_balance(self, obj):
        """Solde du client après cette vente, 0 s'il n'a pas encore de compte."""
        from apps.settings.models import CustomerLoyalty

        if not obj.customer_id:
            return _points_out(Decimal('0.00'))
        balance = CustomerLoyalty.objects.filter(
            organization_id=obj.organization_id, customer_id=obj.customer_id,
        ).values_list('current_points', flat=True).first()
        return _points_out(balance or Decimal('0.00'))

    def get_loyalty_program_active(self, obj):
        """
        Permet au reçu de décider d'imprimer le bloc fidélité sans second appel
        réseau au moment de l'impression.
        """
        from apps.settings.services import LoyaltyService

        return LoyaltyService.active_program(obj.organization) is not None

    @staticmethod
    def _tx_types():
        from apps.settings.models import LoyaltyTransaction
        return LoyaltyTransaction.TransactionType


class SaleCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création de vente (POS).
    Gère la création des items et paiements en une seule requête.
    """
    
    items = SaleItemCreateSerializer(many=True)
    payments = PaymentCreateSerializer(many=True, required=False)
    # Décimal : le solde d'un client peut être fractionnaire (1 % d'un panier
    # de 58 USD vaut 0,58 point), il doit pouvoir être dépensé tel quel.
    points_used = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False,
        min_value=Decimal('0'), default=Decimal('0'), coerce_to_string=False,
    )
    global_discount_amount = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        required=False,
        min_value=Decimal('0'),
        default=Decimal('0'),
    )

    class Meta:
        model = Sale
        fields = [
            'register', 'warehouse', 'customer', 'sale_type', 'price_list',
            'discount_percentage', 'global_discount_amount', 'currency', 'exchange_rate',
            'change_currency',
            'notes', 'internal_notes', 'due_date', 'is_pos',
            'items', 'payments', 'points_used'
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.core.serializer_helpers import scope_fk_to_org
        scope_fk_to_org(
            self, 'customer', 'register', 'warehouse', 'price_list'
        )

    def validate_discount_percentage(self, value):
        v = value if value is not None else Decimal("0")
        if v < 0:
            raise serializers.ValidationError("La remise globale ne peut pas être négative.")
        # Hard-cap absolu à 100% indépendant de la config org : une remise
        # de 100% revient à offrir la vente, au-delà n'a aucun sens métier.
        if v > Decimal("100"):
            raise serializers.ValidationError("La remise globale ne peut pas dépasser 100%.")
        request = self.context.get("request")
        max_p = Decimal("50")
        if request:
            oid = request.headers.get("X-Organization-ID")
            if oid:
                from apps.organizations.models import Organization
                org = Organization.objects.filter(id=oid).first()
                if org:
                    max_p = max_sale_discount_percent(org)
        if v > max_p:
            raise serializers.ValidationError(
                f"La remise globale ne peut pas dépasser {max_p}%."
            )
        return value

    def validate_exchange_rate(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError(
                "Le taux de change de la vente doit être strictement positif."
            )
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Au moins un article est requis.")
        return value

    def validate(self, data):
        """Validations globales de la vente."""
        items = data.get('items', [])
        warehouse = data.get('warehouse')
        is_pos = data.get('is_pos', True)
        register = data.get('register')

        # Vente POS : la caisse est obligatoire
        if is_pos and not register:
            raise serializers.ValidationError({
                'register': "Une caisse est obligatoire pour une vente POS."
            })

        # Récupérer l'organisation
        organization_id = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization_id)
        
        # Vérifier si des produits sont bloqués par un inventaire en cours
        from apps.inventory.models import InventorySession
        product_ids = [item['product'].id for item in items]
        request = self.context.get('request')
        wh_filter = None
        if warehouse:
            wh_filter = [warehouse.pk]
        elif request:
            m = get_membership_for_request(request)
            if m:
                scoped = accessible_warehouse_ids(m)
                if scoped is not None:
                    wh_filter = scoped

        if wh_filter is not None and len(wh_filter) == 0:
            locked_product_ids = set()
        else:
            locked_product_ids = InventorySession.get_all_locked_product_ids(
                org, warehouse_ids=wh_filter
            )
        
        blocked_products = []
        for item in items:
            if item['product'].id in locked_product_ids:
                blocked_products.append(item['product'].name)
        
        if blocked_products:
            # Récupérer les sessions qui bloquent ces produits
            locking_sessions = InventorySession.get_locking_sessions_for_products(
                org, product_ids, warehouse_ids=wh_filter
            )
            session_refs = [s.reference for s in locking_sessions]
            raise serializers.ValidationError({
                'items': f"Les produits suivants sont bloqués par un inventaire en cours ({', '.join(session_refs)}): {', '.join(blocked_products)}. "
                         f"Veuillez attendre la fin de l'inventaire pour effectuer cette vente."
            })
        
        # Vérifier le stock si nécessaire.
        # Contrôle d'ergonomie : il refuse tôt et avec un message clair, mais ne
        # garantit rien - la vérité est établie sous verrou dans
        # `SaleStockService.apply_decrement`.
        if warehouse:
            from apps.inventory.models import Stock
            from apps.inventory.packaging import PackagingService

            # Le découvert se décide au niveau de l'ENTREPÔT, comme partout en
            # aval (`assert_sealed_available`, `ensure_loose_available`,
            # `Stock.save`). Lire ici `product.allow_negative_stock` faisait
            # sauter ce pré-contrôle courtois sur une configuration divergente,
            # et la vente tombait alors sur la `ValidationError` Django de
            # `Stock.save()`, qui n'est pas une erreur DRF : 500 au lieu de 400.
            allow_negative = getattr(warehouse, 'allow_negative_stock', False)
            for item in items:
                product = item['product']
                if product.track_inventory and not allow_negative:
                    stock = Stock.objects.filter(
                        product=product,
                        variant=item.get('variant'),
                        warehouse=warehouse
                    ).first()

                    available = stock.available_quantity if stock else 0
                    package_quantity = item.get('package_quantity') or 0

                    if item['quantity'] > available:
                        readable = PackagingService.format_quantity(
                            product, available,
                            stock.loose_quantity if stock else 0,
                        )
                        message = (
                            f"Stock insuffisant pour {product.name}. "
                            f"Disponible : {readable}."
                        )
                        # Refuser une vente en gros sans dire ce qu'il reste
                        # laisserait le vendeur sans solution devant son client.
                        if package_quantity > 0 and available > 0:
                            message += (
                                f" Vous pouvez vendre ce qu'il reste au détail."
                            )
                        raise serializers.ValidationError({'items': message})

                    # Vente en gros : on ne reconditionne pas des unités déjà
                    # sorties de leur emballage.
                    if package_quantity > 0 and stock is not None:
                        PackagingService.assert_sealed_available(
                            stock, product, package_quantity
                        )

        payments = data.get("payments") or []
        for p in payments:
            # Montant effectivement remis : tendered_amount prioritaire, sinon amount.
            amt = p.get("tendered_amount")
            if amt is None:
                amt = p.get("amount")
            if amt is not None and amt < 0:
                raise serializers.ValidationError({
                    "payments": "Les montants de paiement ne peuvent pas être négatifs."
                })
            # Vérification cross-tenant : DRF n'injecte pas le context dans
            # les serializers enfants instanciés via `many=True`, donc le
            # scoping de queryset ne peut pas être fait au niveau de
            # PaymentCreateSerializer. On rattrape ici.
            pm = p.get("payment_method")
            if pm is not None and getattr(pm, 'organization_id', None) and pm.organization_id != org.id:
                raise serializers.ValidationError({
                    'payments': "Mode de paiement invalide pour cette organisation."
                })

        # Idem : vérifier que chaque product appartient bien à l'org.
        for item in items:
            product = item.get('product')
            if product is not None and product.organization_id != org.id:
                raise serializers.ValidationError({
                    'items': f"Produit « {product.name} » invalide pour cette organisation."
                })

        # Règles métier sur le type de vente.
        sale_type = data.get('sale_type', 'retail')
        customer = data.get('customer')

        # Une vente à crédit exige un client identifié et actif.
        if sale_type == 'credit':
            if not customer:
                raise serializers.ValidationError({
                    'customer': "Un client est obligatoire pour une vente à crédit."
                })
            if not getattr(customer, 'is_active', True):
                raise serializers.ValidationError({
                    'customer': (
                        f"Le client « {customer.name} » est suspendu ou désactivé "
                        "et ne peut pas effectuer d'achat à crédit."
                    )
                })

        # Une vente comptant (retail/wholesale) doit avoir au moins un paiement.
        # Pour différer le règlement, le caissier doit basculer en sale_type='credit'.
        if sale_type in ('retail', 'wholesale') and is_pos:
            total_paid = sum(
                ((p.get('tendered_amount') if p.get('tendered_amount') is not None
                  else p.get('amount')) or Decimal('0') for p in payments),
                Decimal('0'),
            )
            if total_paid <= 0:
                raise serializers.ValidationError({
                    'payments': (
                        "Une vente comptant doit avoir au moins un paiement. "
                        "Utilisez le type « crédit » pour différer le règlement."
                    )
                })

        return data

    def validate_global_discount_amount(self, value):
        v = value if value is not None else Decimal('0')
        if v < 0:
            raise serializers.ValidationError("La remise ne peut pas être négative.")
        return v

    @transaction.atomic
    def create(self, validated_data):
        items_data = validated_data.pop('items')
        payments_data = validated_data.pop('payments', [])
        points_used = validated_data.pop('points_used', 0)
        global_discount_amount_input = validated_data.pop('global_discount_amount', Decimal('0')) or Decimal('0')

        # Générer la référence (le lock sur la dernière vente du jour évite la
        # race condition entre deux requêtes simultanées qui tireraient le même
        # MAX et produiraient une référence en doublon).
        from apps.core.utils import ReferenceGenerator
        from apps.core.api_permissions import is_manager_or_above
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)

        request = self.context['request']

        # Lock pour la génération de référence : on verrouille la dernière vente
        # du jour pour l'org. C'est suffisant car ReferenceGenerator lit le MAX.
        from django.utils import timezone as _tz
        today_prefix = f"VT-{_tz.now().strftime('%Y%m%d')}"
        _ = list(
            Sale.objects.select_for_update().filter(
                organization=org, reference__startswith=today_prefix
            ).order_by('-reference').values_list('id', flat=True)[:1]
        )
        validated_data['reference'] = ReferenceGenerator.generate_sale_reference(org)
        validated_data['organization'] = org
        validated_data['sold_by'] = request.user

        # Récupérer la session active si POS
        if validated_data.get('is_pos') and validated_data.get('register'):
            session = RegisterSession.objects.filter(
                register=validated_data['register'],
                status='open'
            ).first()

            # Vente POS sans session ouverte → refus
            if session is None:
                raise serializers.ValidationError({
                    'register': "Aucune session ouverte sur cette caisse. "
                                "Veuillez ouvrir une session avant d'encaisser."
                })

            # Auditabilité : un cashier ne peut pas utiliser la session d'un autre
            if session.opened_by_id != request.user.id and not is_manager_or_above(request):
                raise serializers.ValidationError({
                    'register': "Cette session a été ouverte par un autre utilisateur."
                })

            validated_data['session'] = session

            # Si pas de warehouse spécifié, utiliser celui de la caisse
            if not validated_data.get('warehouse') and validated_data['register'].warehouse:
                validated_data['warehouse'] = validated_data['register'].warehouse
        
        # Fallback: utiliser l'entrepôt par défaut si aucun n'est spécifié
        if not validated_data.get('warehouse'):
            from apps.inventory.models import Warehouse
            default_warehouse = Warehouse.objects.filter(
                organization=org,
                is_default=True,
                is_active=True,
                is_deleted=False
            ).first()
            if not default_warehouse:
                default_warehouse = Warehouse.objects.filter(
                    organization=org,
                    is_active=True,
                    is_deleted=False
                ).first()
            if default_warehouse:
                validated_data['warehouse'] = default_warehouse
        
        # Créer la vente
        sale = Sale.objects.create(**validated_data)
        warehouse = sale.warehouse
        
        # Import pour la gestion du stock (FIFO uniquement ici ; la décrémentation
        # est centralisée dans `SaleStockService.apply_decrement` appelée plus bas).
        from apps.inventory.services import FIFOService
        TWO_PLACES = Decimal('0.01')
        
        # Créer les items - SaleItem.save() calcule et arrondit tous les montants
        for item_data in items_data:
            product = item_data['product']
            variant = item_data.get('variant')
            quantity = item_data['quantity']
            # Priorité : prix fourni par le frontend > prix du variant > prix du produit.
            variant_price = getattr(variant, 'selling_price', None) if variant else None
            unit_price = (
                item_data.get('unit_price')
                or (variant_price if variant_price and variant_price > 0 else None)
                or product.selling_price
            )
            discount_pct = item_data.get('discount_percentage', Decimal('0.00'))
            tax_rate = item_data.get('tax_rate') or (product.tax_rate if product.is_taxable else Decimal('0.00'))
            
            # Déterminer le cost_price via FIFO si le produit a un suivi de stock
            cost_price = product.cost_price
            batch = item_data.get('batch')
            
            if product.track_inventory and warehouse:
                # Utiliser FIFO pour déterminer le coût réel des lots
                allocations, _ = FIFOService.allocate_quantity(
                    organization=org,
                    product=product,
                    warehouse=warehouse,
                    quantity_needed=quantity,
                    variant=item_data.get('variant'),
                    exclude_expired=True,
                    use_fefo=product.has_expiry_date if hasattr(product, 'has_expiry_date') else False
                )
                
                if allocations:
                    # Utiliser le coût moyen pondéré des lots alloués
                    cost_price = FIFOService.calculate_weighted_cost(allocations)
                    # Si un seul lot, l'associer à l'item
                    if len(allocations) == 1:
                        batch = allocations[0].batch
            
            SaleItem.objects.create(
                sale=sale,
                organization=org,
                product=product,
                variant=item_data.get('variant'),
                batch=batch,
                quantity=quantity,
                unit_price=unit_price,
                cost_price=cost_price,
                # Conditionnement : `packaging_factor` est le snapshot posé par
                # le serializer, jamais une valeur du client.
                package_quantity=item_data.get('package_quantity') or Decimal('0.000'),
                package_unit_price=item_data.get('package_unit_price'),
                packaging_factor=item_data.get('packaging_factor'),
                discount_percentage=discount_pct,
                tax_rate=tax_rate,
                notes=item_data.get('notes', '')
            )
        
        # Relire les valeurs réelles sauvegardées par SaleItem.save()
        saved_items = sale.items.all()
        items_subtotal = sum((i.subtotal for i in saved_items), Decimal('0.00'))
        items_discount_total = sum((i.discount_amount for i in saved_items), Decimal('0.00'))
        tax_total = sum((i.tax_amount for i in saved_items), Decimal('0.00'))
        
        # Calculer les totaux de la vente
        sale.subtotal = items_subtotal.quantize(TWO_PLACES)
        sale.tax_amount = tax_total.quantize(TWO_PLACES)
        
        # Remise globale (montant fixe prioritaire, sinon pourcentage legacy)
        net_after_item_discounts = items_subtotal - items_discount_total
        global_discount = Decimal('0.00')
        if global_discount_amount_input > 0:
            global_discount = global_discount_amount_input.quantize(TWO_PLACES)
            if global_discount > net_after_item_discounts:
                raise DRFValidationError(
                    "La remise ne peut pas dépasser le montant après remises articles."
                )
            sale.discount_percentage = Decimal('0.00')
        elif sale.discount_percentage > 0:
            global_discount = (net_after_item_discounts * sale.discount_percentage / 100).quantize(TWO_PLACES)
        
        # discount_amount = total de toutes les remises (articles + globale)
        sale.discount_amount = (items_discount_total + global_discount).quantize(TWO_PLACES)

        # Points de fidélité : appliquer comme réduction effective AVANT le total.
        # Cappés à la fois par le solde points du client et par le total tentatif
        # (pas de total négatif).
        loyalty_resolution = None
        sale.loyalty_redemption_amount = Decimal('0.00')
        if points_used > 0 and sale.customer:
            from apps.settings.services import LoyaltyService
            tentative_total = (items_subtotal - sale.discount_amount + tax_total).quantize(TWO_PLACES)
            loyalty_resolution = LoyaltyService.resolve_redemption(
                sale.customer, org, points_used, tentative_total,
                target_currency=sale.currency,
            )
            if loyalty_resolution:
                sale.discount_amount = (sale.discount_amount + loyalty_resolution['amount']).quantize(TWO_PLACES)
                sale.loyalty_redemption_amount = loyalty_resolution['amount']

        # total = subtotal - toutes remises + taxes
        sale.total = (items_subtotal - sale.discount_amount + tax_total).quantize(TWO_PLACES)
        if sale.total < 0:
            raise DRFValidationError("Le total de la vente ne peut pas être négatif.")
        sale.amount_due = sale.total

        # Créer les paiements - conversion multi-devise centralisée : chaque
        # règlement peut être dans une devise différente de la facture (le client
        # peut même en cumuler deux). `create_payment` convertit `tendered_amount`
        # (devise du règlement) vers la devise de la vente et remplit `amount`.
        from .services import create_payment, resolve_change
        user = self.context['request'].user
        created_payments = []
        total_paid = Decimal('0.00')
        for payment_data in payments_data:
            payment = create_payment(
                sale, user,
                payment_method=payment_data.get('payment_method'),
                tendered_amount=payment_data.get('tendered_amount'),
                amount=payment_data.get('amount'),
                currency=payment_data.get('currency'),
                exchange_rate=payment_data.get('exchange_rate'),
                reference=payment_data.get('reference', '') or '',
                notes=payment_data.get('notes', '') or '',
            )
            created_payments.append(payment)
            total_paid += payment.amount

        sale.amount_paid = total_paid.quantize(TWO_PLACES)
        sale.amount_due = (sale.total - sale.amount_paid).quantize(TWO_PLACES)

        change_amount = Decimal('0.00')
        change_currency = sale.change_currency or sale.currency
        if sale.amount_paid >= sale.total:
            # Monnaie rendue dans la devise choisie par le caissier (défaut = vente).
            change_amount, change_currency, _ = resolve_change(sale, change_currency)
            sale.change_amount = change_amount
            sale.change_currency = change_currency
            sale.amount_due = Decimal('0.00')
            sale.status = 'completed'
        elif sale.amount_paid > 0:
            sale.status = 'partially_paid'
        else:
            sale.status = 'pending'

        sale.save()

        # Mouvements de caisse : une ENTRÉE par règlement (dans la devise
        # réellement remise) + la monnaie rendue en SORTIE. Le tiroir reflète
        # ainsi chaque devise physiquement présente.
        if created_payments:
            from apps.cashbook.services import record_sale_payment_income, record_change
            for payment in created_payments:
                record_sale_payment_income(org, sale, payment, user)
            if change_amount > 0:
                record_change(org, sale, change_amount, change_currency, user)
        
        # Mettre à jour le stock si la vente est complétée et qu'un entrepôt est défini.
        # Décrémentation centralisée : FIFO sur les lots + re-check `allow_negative_stock`
        # sous lock + StockMovement, le tout idempotent.
        if sale.status == 'completed' and warehouse:
            from .services import SaleStockService
            SaleStockService.apply_decrement(sale, self.context['request'].user)
        
        # Inscrire la dette dès qu'il reste quelque chose à payer, quel que soit
        # le `sale_type` : une vente au détail laissée partiellement payée est
        # une dette réelle. Avant, seul `sale_type='credit'` alimentait le solde,
        # et le reste à payer d'une vente au détail n'apparaissait nulle part
        # dans la gestion de la dette.
        #
        # `amount_due` est déjà net de l'acompte encaissé plus haut.
        if sale.customer and sale.amount_due > 0:
            from apps.contacts import services as contacts_services
            contacts_services.apply_debt(
                sale.customer, sale.amount_due,
                currency=sale.currency,
                sale=sale,
                reference=sale.reference,
                notes=(
                    f"Vente à crédit {sale.reference}"
                    if sale.sale_type == 'credit'
                    else f"Reste à payer sur vente {sale.reference}"
                ),
                user=self.context['request'].user,
            )

        # Persister la consommation de points et la transaction loyalty maintenant
        # que la vente est sauvegardée (FK loyalty_transaction.sale → sale.id).
        from apps.settings.services import LoyaltyService
        if loyalty_resolution:
            LoyaltyService.persist_redemption(
                sale, loyalty_resolution, user=self.context['request'].user,
            )

        # Attribuer les points de fidélité si applicable
        if sale.status == 'completed' and sale.customer:
            LoyaltyService.award_points_for_sale(
                sale, user=self.context['request'].user,
            )

        return sale


class SalePaymentSerializer(serializers.Serializer):
    """Serializer pour ajouter un paiement à une vente existante.

    ``amount`` = montant remis dans ``currency`` (compat historique). Un split
    multi-devise se fait via plusieurs appels ``add-payment`` successifs.
    ``change_currency`` : devise de la monnaie rendue si le règlement solde la vente.
    ``points_used`` : points de fidélité à convertir en règlement sur cette
    facture. Contrairement au POS (où la remise précède l'émission de la
    facture), un règlement en points sur une facture DÉJÀ émise ne modifie pas
    son total : il s'impute comme n'importe quel autre moyen de paiement.
    """

    payment_method = serializers.UUIDField(required=False, allow_null=True)
    amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, default=Decimal('0.00'),
    )
    # Décimal : le solde d'un client peut être fractionnaire (1 % d'un panier
    # de 58 USD vaut 0,58 point), il doit pouvoir être dépensé tel quel.
    points_used = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False,
        min_value=Decimal('0'), default=Decimal('0'), coerce_to_string=False,
    )
    currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    exchange_rate = serializers.DecimalField(
        max_digits=15, decimal_places=6, required=False
    )
    change_currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    reference = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        amount = attrs.get('amount') or Decimal('0.00')
        points = attrs.get('points_used') or 0
        if amount <= 0 and points <= 0:
            raise serializers.ValidationError(
                "Indiquez un montant ou un nombre de points à utiliser."
            )
        if amount > 0 and not attrs.get('payment_method'):
            raise serializers.ValidationError(
                {'payment_method': "Moyen de paiement requis pour un règlement en argent."}
            )
        return attrs


# =============================================================================
# SALE RETURN SERIALIZERS
# =============================================================================

class SaleReturnItemSerializer(serializers.ModelSerializer):
    """Serializer pour les articles de retour."""
    
    product_name = serializers.CharField(
        source='original_item.product.name', read_only=True
    )
    
    class Meta:
        model = SaleReturnItem
        fields = [
            'id', 'original_item', 'product_name',
            'quantity', 'unit_price', 'total',
            'reason', 'restock'
        ]
        read_only_fields = ['id']


class SaleReturnListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de retours."""
    
    original_sale_reference = serializers.CharField(
        source='original_sale.reference', read_only=True
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = SaleReturn
        fields = [
            'id', 'reference', 'original_sale', 'original_sale_reference',
            'return_type', 'status', 'status_display',
            'total_amount', 'refund_amount',
            'return_date'
        ]
        read_only_fields = ['id', 'reference', 'return_date']


class SaleReturnDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un retour."""
    
    original_sale_reference = serializers.CharField(
        source='original_sale.reference', read_only=True
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True
    )
    approved_by_name = serializers.CharField(
        source='approved_by.full_name', read_only=True
    )
    items = SaleReturnItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = SaleReturn
        fields = [
            'id', 'reference', 'original_sale', 'original_sale_reference',
            'return_type', 'status',
            'total_amount', 'refund_amount', 'reason',
            'created_by', 'created_by_name',
            'approved_by', 'approved_by_name',
            'return_date', 'approved_at',
            'items', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'return_date', 'created_at']


class SaleReturnCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de retour."""
    
    items = SaleReturnItemSerializer(many=True)
    
    class Meta:
        model = SaleReturn
        fields = ['original_sale', 'return_type', 'reason', 'items']

    def validate(self, data):
        items = data.get('items', [])
        if not items:
            raise serializers.ValidationError({
                'items': "Au moins un article est requis."
            })
        
        # Vérifier que les quantités ne dépassent pas les quantités vendues
        for item in items:
            original_item = item['original_item']
            if item['quantity'] > original_item.quantity:
                raise serializers.ValidationError({
                    'items': f"Quantité de retour supérieure à la quantité vendue pour {original_item.product.name}"
                })

        # Vérifier le périmètre entrepôt sur la vente d'origine
        original_sale = data.get('original_sale')
        request = self.context.get('request')
        if request and original_sale is not None:
            m = get_membership_for_request(request)
            if m:
                allowed_ids = accessible_warehouse_ids(m)
                if allowed_ids is not None:
                    sale_wh_id = original_sale.warehouse_id
                    if sale_wh_id is not None and sale_wh_id not in allowed_ids:
                        raise serializers.ValidationError({
                            'original_sale': "Cette vente n'est pas dans votre périmètre d'entrepôts."
                        })

        return data

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        # Générer la référence
        from django.utils import timezone
        today = timezone.now()
        prefix = f"RET{today.strftime('%Y%m%d')}"
        
        last = SaleReturn.objects.filter(
            organization=org,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except:
                num = 1
        else:
            num = 1
        
        validated_data['reference'] = f"{prefix}-{num:04d}"
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        
        sale_return = SaleReturn.objects.create(**validated_data)
        
        total_amount = Decimal('0.00')
        for item_data in items_data:
            original_item = item_data['original_item']
            quantity = item_data['quantity']
            unit_price = original_item.unit_price
            total = quantity * unit_price
            
            SaleReturnItem.objects.create(
                sale_return=sale_return,
                organization=org,
                original_item=original_item,
                quantity=quantity,
                unit_price=unit_price,
                total=total,
                reason=item_data.get('reason', ''),
                restock=item_data.get('restock', True)
            )
            
            total_amount += total
        
        sale_return.total_amount = total_amount
        sale_return.refund_amount = total_amount
        sale_return.save()
        
        return sale_return


# =============================================================================
# QUOTATION SERIALIZERS
# =============================================================================

class QuotationItemSerializer(serializers.ModelSerializer):
    """Serializer pour les lignes de devis."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    
    class Meta:
        model = QuotationItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant',
            'description', 'quantity', 'unit_price',
            'discount_percentage', 'tax_rate', 'total'
        ]
        read_only_fields = ['id', 'total']


class QuotationListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de devis."""
    
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Quotation
        fields = [
            'id', 'reference', 'customer', 'customer_name',
            'status', 'status_display',
            'subtotal', 'total', 'valid_until',
            'items_count', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_items_count(self, obj):
        return obj.items.count()


class QuotationDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un devis."""
    
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    items = QuotationItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = Quotation
        fields = [
            'id', 'reference', 'customer', 'customer_name',
            'status', 'subtotal', 'tax_amount', 'discount_amount', 'total',
            'valid_until', 'notes', 'terms',
            'created_by', 'created_by_name',
            'converted_sale', 'items',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']


class QuotationCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de devis."""
    
    items = QuotationItemSerializer(many=True)
    
    class Meta:
        model = Quotation
        fields = [
            'customer', 'valid_until', 'notes', 'terms', 'items'
        ]

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Au moins un article est requis.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        # Générer la référence
        from django.utils import timezone
        today = timezone.now()
        prefix = f"DEV{today.strftime('%Y%m%d')}"
        
        last = Quotation.objects.filter(
            organization=org,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except:
                num = 1
        else:
            num = 1
        
        validated_data['reference'] = f"{prefix}-{num:04d}"
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        
        quotation = Quotation.objects.create(**validated_data)
        
        subtotal = Decimal('0.00')
        tax_total = Decimal('0.00')
        
        for item_data in items_data:
            product = item_data['product']
            quantity = item_data['quantity']
            unit_price = item_data.get('unit_price') or product.selling_price
            discount_pct = item_data.get('discount_percentage', Decimal('0.00'))
            tax_rate = item_data.get('tax_rate', Decimal('0.00'))
            
            item_subtotal = quantity * unit_price
            discount = item_subtotal * (discount_pct / 100)
            taxable = item_subtotal - discount
            tax = taxable * (tax_rate / 100)
            item_total = taxable + tax
            
            QuotationItem.objects.create(
                quotation=quotation,
                organization=org,
                product=product,
                variant=item_data.get('variant'),
                description=item_data.get('description', ''),
                quantity=quantity,
                unit_price=unit_price,
                discount_percentage=discount_pct,
                tax_rate=tax_rate,
                total=item_total
            )
            
            subtotal += item_subtotal
            tax_total += tax
        
        quotation.subtotal = subtotal
        quotation.tax_amount = tax_total
        quotation.total = subtotal + tax_total
        quotation.save()
        
        return quotation
