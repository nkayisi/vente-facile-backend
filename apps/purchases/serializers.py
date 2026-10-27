"""
Serializers DRF pour l'app Purchases.
"""
import logging

from rest_framework import serializers
from decimal import Decimal
from .models import (
    PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem,
    SupplierPayment, SupplierPaymentAllocation, PurchaseReturn, PurchaseReturnItem,
    Requisition, RequisitionItem,
)

logger = logging.getLogger(__name__)


# =============================================================================
# PURCHASE ORDER ITEM SERIALIZERS
# =============================================================================

def _organisation(serializer):
    """
    L'organisation du contexte, résolue par la VUE et non par l'en-tête HTTP.

    ⚠ Les trois serializers de création lisaient
    `request.headers.get('X-Organization-ID')` puis faisaient un `get()` dessus.
    Deux conséquences : un en-tête absent ou malformé rendait 500 au lieu de 400,
    et aucun chemin qui ne soit pas une requête HTTP ne pouvait les emprunter -
    ce qui ferme d'avance la porte au journal de synchronisation. C'est le
    défaut qu'`_validate_product_prices` a déjà payé côté inventaire.

    `TenantViewSetMixin.get_organization` mémoïse la résolution sur la requête
    et rend 404 sur un en-tête sans appartenance : ne pas le court-circuiter,
    sans quoi le filtre d'organisation de `get_queryset` sauterait.
    """
    vue = serializer.context.get('view')
    if vue is not None and hasattr(vue, 'get_organization'):
        return vue.get_organization()
    raise serializers.ValidationError(
        "Organisation introuvable dans le contexte de la requête."
    )


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    """Serializer pour les lignes de commande d'achat."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    quantity_pending = serializers.DecimalField(
        max_digits=15, decimal_places=3, read_only=True
    )
    
    ordered_display = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrderItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'variant',
            'description', 'quantity_ordered', 'quantity_received',
            'quantity_pending', 'unit_price',
            'package_quantity', 'loose_quantity', 'package_unit_price',
            'packaging_factor', 'ordered_display',
            'tax_rate', 'tax_amount', 'discount_percentage',
            'subtotal', 'total'
        ]
        read_only_fields = ['id', 'quantity_received', 'subtotal', 'total', 'tax_amount']

    def get_ordered_display(self, obj):
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(
            obj.product, obj.quantity_ordered, obj.loose_quantity
        )


class PurchaseOrderItemCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création de ligne de commande.

    Le marchand commande à son fournisseur dans l'unité où celui-ci vend :
    « 10 cartons à 12 500 ». La conversion vers l'unité de détail est faite ici,
    et le prix du contenant est conservé tel quel : c'est lui qui sera facturé.
    """

    class Meta:
        model = PurchaseOrderItem
        fields = [
            'product', 'variant', 'description',
            'quantity_ordered', 'unit_price',
            'package_quantity', 'loose_quantity', 'package_unit_price',
            'tax_rate', 'discount_percentage'
        ]
        extra_kwargs = {
            'quantity_ordered': {'required': False},
            'unit_price': {'required': False},
        }

    def validate_quantity_ordered(self, value):
        if value <= 0:
            raise serializers.ValidationError("La quantité doit être positive.")
        return value

    def validate(self, data):
        from apps.inventory.packaging import PackagingService

        product = data.get('product')
        factor = PackagingService.factor(product) if product else None
        packages = data.get('package_quantity') or Decimal('0.000')
        loose = data.get('loose_quantity') or Decimal('0.000')
        package_unit_price = data.get('package_unit_price')

        if packages or loose or package_unit_price is not None:
            if factor is None:
                raise serializers.ValidationError({
                    'package_quantity': (
                        "Ce produit ne s'achète pas par contenant : "
                        "indiquez une quantité et un prix à l'unité."
                    )
                })
            data['packaging_factor'] = factor

        if packages or loose:
            data['quantity_ordered'] = PackagingService.to_base(
                product, packages, loose
            )

        if package_unit_price is not None:
            if package_unit_price < 0:
                raise serializers.ValidationError({
                    'package_unit_price': "Le prix ne peut pas être négatif."
                })
            data.setdefault(
                'unit_price',
                (Decimal(package_unit_price) / factor).quantize(Decimal('0.01')),
            )

        if not data.get('quantity_ordered'):
            raise serializers.ValidationError({
                'quantity_ordered': "Indiquez une quantité à commander."
            })
        if data.get('unit_price') is None:
            raise serializers.ValidationError({
                'unit_price': "Indiquez un prix d'achat."
            })

        return data


# =============================================================================
# PURCHASE ORDER SERIALIZERS
# =============================================================================

class PurchaseOrderListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de commandes d'achat."""
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'status', 'status_display',
            'subtotal', 'total', 'amount_paid', 'amount_due',
            'currency', 'order_date', 'expected_date',
            'created_by', 'created_by_name',
            'items_count', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_items_count(self, obj):
        return obj.items.count()


class PurchaseOrderDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une commande d'achat."""
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    supplier_email = serializers.CharField(source='supplier.email', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.full_name', read_only=True)
    
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    receipts_count = serializers.SerializerMethodField()
    
    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'reference',
            'supplier', 'supplier_name', 'supplier_email',
            'warehouse', 'warehouse_name',
            'status',
            'subtotal', 'tax_amount', 'discount_amount', 'shipping_cost', 'total',
            'amount_paid', 'amount_due',
            'currency', 'exchange_rate',
            'order_date', 'expected_date',
            'notes', 'terms',
            'created_by', 'created_by_name',
            'approved_by', 'approved_by_name',
            'items', 'receipts_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']

    def get_receipts_count(self, obj):
        return obj.receipts.count()


class PurchaseOrderCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de commande d'achat."""
    
    items = PurchaseOrderItemCreateSerializer(many=True)
    
    class Meta:
        model = PurchaseOrder
        fields = [
            'supplier', 'warehouse', 'order_date', 'expected_date',
            'currency', 'exchange_rate', 'shipping_cost',
            'notes', 'terms', 'items'
        ]

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Au moins un article est requis.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        from apps.core.numbering import (
            PREFIX_PURCHASE_ORDER, allocate_document_number,
        )

        org = _organisation(self)
        validated_data['reference'] = allocate_document_number(
            org, PREFIX_PURCHASE_ORDER,
        )
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        
        po = PurchaseOrder.objects.create(**validated_data)
        
        subtotal = Decimal('0.00')
        tax_total = Decimal('0.00')
        
        for item_data in items_data:
            quantity = item_data['quantity_ordered']
            unit_price = item_data['unit_price']
            tax_rate = item_data.get('tax_rate', Decimal('0.00'))
            discount_pct = item_data.get('discount_percentage', Decimal('0.00'))
            packages = item_data.get('package_quantity') or Decimal('0.000')
            loose = item_data.get('loose_quantity') or Decimal('0.000')
            package_unit_price = item_data.get('package_unit_price')

            # Commande passée au contenant : le montant se calcule sur le prix
            # du contenant, pas sur son quotient arrondi. Sinon une commande de
            # 10 cartons à 12 500 se facturerait 125 000,40 au lieu de 125 000.
            if packages and package_unit_price is not None:
                item_subtotal = packages * package_unit_price + loose * unit_price
            else:
                item_subtotal = quantity * unit_price
            discount = item_subtotal * (discount_pct / 100)
            taxable = item_subtotal - discount
            tax_amount = taxable * (tax_rate / 100)
            item_total = taxable + tax_amount

            PurchaseOrderItem.objects.create(
                purchase_order=po,
                organization=org,
                product=item_data['product'],
                variant=item_data.get('variant'),
                description=item_data.get('description', ''),
                quantity_ordered=quantity,
                unit_price=unit_price,
                package_quantity=packages,
                loose_quantity=loose,
                package_unit_price=package_unit_price,
                packaging_factor=item_data.get('packaging_factor'),
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                discount_percentage=discount_pct,
                subtotal=item_subtotal,
                total=item_total
            )
            
            subtotal += item_subtotal
            tax_total += tax_amount
        
        po.subtotal = subtotal
        po.tax_amount = tax_total
        po.total = subtotal + tax_total + (po.shipping_cost or Decimal('0.00'))
        po.amount_due = po.total
        po.save()
        
        return po


# =============================================================================
# GOODS RECEIPT SERIALIZERS
# =============================================================================

class GoodsReceiptItemSerializer(serializers.ModelSerializer):
    """Serializer pour les lignes de réception."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    
    received_display = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReceiptItem
        fields = [
            'id', 'purchase_order_item', 'product', 'product_name', 'product_sku',
            'variant', 'quantity_received', 'quantity_accepted', 'quantity_rejected',
            'unit_cost', 'package_quantity', 'loose_quantity',
            'package_unit_cost', 'packaging_factor', 'received_display',
            'batch_number', 'expiry_date',
            'notes', 'rejection_reason'
        ]
        read_only_fields = ['id']

    def get_received_display(self, obj):
        from apps.inventory.packaging import PackagingService

        return PackagingService.format_quantity(
            obj.product, obj.quantity_received, obj.loose_quantity
        )


class GoodsReceiptItemCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour la création de ligne de réception.

    Le magasinier compte ce qu'il décharge : « 10 cartons + 3 bouteilles ». La
    quantité en unité de détail en est déduite, et c'est elle qui alimente le
    stock, les lots et le coût moyen pondéré.
    """

    class Meta:
        model = GoodsReceiptItem
        fields = [
            'purchase_order_item', 'product', 'variant',
            'quantity_received', 'quantity_accepted', 'quantity_rejected',
            'unit_cost', 'package_quantity', 'loose_quantity',
            'package_unit_cost',
            'batch_number', 'expiry_date',
            'notes', 'rejection_reason'
        ]
        extra_kwargs = {
            'quantity_received': {'required': False},
            'quantity_accepted': {'required': False},
            'unit_cost': {'required': False},
        }

    def validate(self, data):
        from apps.inventory.packaging import PackagingService

        product = data.get('product')
        factor = PackagingService.factor(product) if product else None
        packages = data.get('package_quantity') or Decimal('0.000')
        loose = data.get('loose_quantity') or Decimal('0.000')
        package_unit_cost = data.get('package_unit_cost')

        if packages or loose or package_unit_cost is not None:
            if factor is None:
                raise serializers.ValidationError({
                    'package_quantity': (
                        "Ce produit ne s'achète pas par contenant : "
                        "indiquez une quantité et un coût à l'unité."
                    )
                })
            data['packaging_factor'] = factor

        if packages or loose:
            data['quantity_received'] = PackagingService.to_base(
                product, packages, loose
            )

        if package_unit_cost is not None:
            if package_unit_cost < 0:
                raise serializers.ValidationError({
                    'package_unit_cost': "Le coût ne peut pas être négatif."
                })
            data.setdefault(
                'unit_cost',
                (Decimal(package_unit_cost) / factor).quantize(Decimal('0.01')),
            )

        received = data.get('quantity_received')
        if not received:
            raise serializers.ValidationError({
                'quantity_received': "Indiquez la quantité reçue."
            })
        if data.get('unit_cost') is None:
            raise serializers.ValidationError({
                'unit_cost': "Indiquez le coût d'achat."
            })

        # Sans contrôle qualité saisi, tout ce qui est reçu est accepté : c'est
        # le cas courant, et l'exiger ferait échouer une réception normale.
        rejected = data.get('quantity_rejected') or Decimal('0.000')
        data.setdefault('quantity_accepted', received - rejected)
        accepted = data['quantity_accepted']

        if accepted + rejected != received:
            raise serializers.ValidationError(
                "La somme des quantités acceptées et rejetées doit égaler la quantité reçue."
            )

        return data


class GoodsReceiptListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de réceptions."""
    
    # ⚠ LA DEVISE D'UNE PIÈCE D'ACHAT EST CELLE DE SA COMMANDE.
    # Ni `GoodsReceipt` ni `PurchaseReturn` ne porte de devise à lui, et
    # `expedier_retour` comme `enregistrer_reglement` résolvent la leur depuis
    # `purchase_order.currency`. Sans ce champ, les écrans rendaient leurs
    # montants sous le symbole de la devise PRINCIPALE : « 40 000 FC » sur un
    # retour de 40 000 USD, dans la phrase même qui annonce un geste
    # irréversible.
    purchase_order_currency = serializers.CharField(
        source='purchase_order.currency', read_only=True, default=''
    )
    
    purchase_order_reference = serializers.CharField(
        source='purchase_order.reference', read_only=True
    )
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    received_by_name = serializers.CharField(source='received_by.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = GoodsReceipt
        fields = [
            'id', 'reference', 'purchase_order', 'purchase_order_reference',
            'purchase_order_currency',
            'warehouse', 'warehouse_name',
            'status', 'status_display',
            'receipt_date', 'supplier_invoice', 'supplier_delivery_note',
            'received_by', 'received_by_name',
            'items_count', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_items_count(self, obj):
        return obj.items.count()


class GoodsReceiptDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'une réception."""
    
    # ⚠ LA DEVISE D'UNE PIÈCE D'ACHAT EST CELLE DE SA COMMANDE.
    # Ni `GoodsReceipt` ni `PurchaseReturn` ne porte de devise à lui, et
    # `expedier_retour` comme `enregistrer_reglement` résolvent la leur depuis
    # `purchase_order.currency`. Sans ce champ, les écrans rendaient leurs
    # montants sous le symbole de la devise PRINCIPALE : « 40 000 FC » sur un
    # retour de 40 000 USD, dans la phrase même qui annonce un geste
    # irréversible.
    purchase_order_currency = serializers.CharField(
        source='purchase_order.currency', read_only=True, default=''
    )
    
    purchase_order_reference = serializers.CharField(
        source='purchase_order.reference', read_only=True
    )
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    received_by_name = serializers.CharField(source='received_by.full_name', read_only=True)
    items = GoodsReceiptItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = GoodsReceipt
        fields = [
            'id', 'reference', 'purchase_order', 'purchase_order_reference',
            'purchase_order_currency',
            'warehouse', 'warehouse_name',
            'status', 'receipt_date',
            'supplier_invoice', 'supplier_delivery_note',
            'notes', 'received_by', 'received_by_name',
            'items', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']


class GoodsReceiptCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de réception."""
    
    items = GoodsReceiptItemCreateSerializer(many=True)
    
    class Meta:
        model = GoodsReceipt
        fields = [
            'purchase_order', 'warehouse', 'receipt_date',
            'supplier_invoice', 'supplier_delivery_note',
            'notes', 'items'
        ]

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Au moins un article est requis.")
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        from apps.core.numbering import (
            PREFIX_GOODS_RECEIPT, allocate_document_number,
        )

        org = _organisation(self)
        validated_data['reference'] = allocate_document_number(
            org, PREFIX_GOODS_RECEIPT,
        )
        validated_data['organization'] = org
        validated_data['received_by'] = self.context['request'].user

        grn = GoodsReceipt.objects.create(**validated_data)

        for item_data in items_data:
            GoodsReceiptItem.objects.create(
                goods_receipt=grn,
                organization=org,
                **item_data
            )

        # ⚠ `quantity_received` NE BOUGE PAS ICI, et c'est le correctif.
        # L'incrémenter dès le brouillon faisait qu'une réception saisie puis
        # oubliée gonflait `quantity_pending` pour toujours : la commande
        # s'annonçait reçue alors que rien n'était entré en rayon. Le stock, le
        # statut de la commande et la dette bougent tous à la VALIDATION
        # (`services.completer_reception`) ; la quantité reçue n'avait aucune
        # raison d'être seule à bouger avant.
        return grn


# =============================================================================
# SUPPLIER PAYMENT SERIALIZERS
# =============================================================================

class SupplierPaymentAllocationSerializer(serializers.ModelSerializer):
    """Serializer pour les allocations de paiement."""
    
    purchase_order_reference = serializers.CharField(
        source='purchase_order.reference', read_only=True
    )
    
    class Meta:
        model = SupplierPaymentAllocation
        fields = ['id', 'purchase_order', 'purchase_order_reference', 'amount']
        read_only_fields = ['id']


class SupplierPaymentListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de paiements fournisseur."""
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = SupplierPayment
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'amount', 'currency', 'payment_method',
            'status', 'status_display', 'payment_date',
            'created_by', 'created_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']


class SupplierPaymentDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un paiement fournisseur."""
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    payment_method_name = serializers.CharField(source='payment_method.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    allocations = SupplierPaymentAllocationSerializer(many=True, read_only=True)
    
    class Meta:
        model = SupplierPayment
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'amount', 'currency', 'exchange_rate',
            'payment_method', 'payment_method_name',
            'payment_reference', 'status', 'payment_date',
            'notes', 'created_by', 'created_by_name',
            'allocations', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']


class SupplierPaymentCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de paiement fournisseur."""
    
    allocations = SupplierPaymentAllocationSerializer(many=True, required=False)
    
    class Meta:
        model = SupplierPayment
        fields = [
            'supplier', 'amount', 'currency', 'exchange_rate',
            'payment_method', 'payment_reference', 'payment_date',
            'notes', 'allocations'
        ]

    def validate(self, data):
        """
        Borne l'imputation : même établissement, même fournisseur, et pas plus
        que ce qui est versé.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ `purchase_order` EST UN CHAMP DE RELATION NON BORNÉ.                │
        │                                                                      │
        │ `SupplierPaymentAllocationSerializer` l'expose par un               │
        │ `ModelSerializer`, donc DRF le construit sur `PurchaseOrder.objects` │
        │ ENTIER - aucun filtre d'organisation, aucun filtre de fournisseur.   │
        │ `enregistrer_reglement` écrit ensuite `amount_paid` et `amount_due`  │
        │ sur la commande désignée : une commande d'une AUTRE organisation     │
        │ était donc modifiable depuis l'extérieur. C'est le défaut exact que  │
        │ `create_requisition` vient de fermer sur son `product` nu.          │
        └──────────────────────────────────────────────────────────────────────┘

        ⚠ ET LA SOMME DES IMPUTATIONS NE DOIT PAS DÉPASSER LE VERSEMENT.
        `settle_debt` ne réduit la dette que du montant réellement payé, tandis
        que chaque `amount_paid` était crédité de ce que le client demandait :
        la somme des commandes dues cessait de correspondre au solde qu'elle
        résume, et c'est très exactement la divergence que l'imputation existe
        pour empêcher.
        """
        allocations = data.get('allocations') or []
        if not allocations:
            return data

        org = _organisation(self)
        fournisseur = data.get('supplier')

        for alloc in allocations:
            po = alloc['purchase_order']
            if po.organization_id != org.id or po.supplier_id != getattr(
                fournisseur, 'id', None
            ):
                # ⚠ LE MESSAGE NE RÉPÈTE PAS LA RÉFÉRENCE : la renvoyer
                # confirmerait au client qu'une commande d'autrui existe.
                raise serializers.ValidationError({
                    'allocations': (
                        "Une commande imputée n'appartient pas à ce fournisseur."
                    )
                })

        total = sum(
            (Decimal(alloc['amount']) for alloc in allocations), Decimal('0.00')
        )
        if total > Decimal(data['amount']):
            raise serializers.ValidationError({
                'allocations': (
                    f"L'imputation totale ({total}) dépasse le montant versé "
                    f"({data['amount']})."
                )
            })

        return data

    def create(self, validated_data):
        """Délègue à `services.enregistrer_reglement`.

        Ce corps faisait tout lui-même, et portait quatre défauts : organisation
        lue dans l'en-tête HTTP, référence triée alphabétiquement et datée en
        UTC, écriture inline de `supplier.current_balance` (sans devise, sans
        grand livre), et aucune imputation quand aucune allocation n'était
        fournie - le solde baissait pendant que chaque `amount_due` restait
        intact.
        """
        from .services import enregistrer_reglement

        allocations_data = validated_data.pop('allocations', [])
        return enregistrer_reglement(
            _organisation(self),
            validated_data['supplier'],
            validated_data['amount'],
            payment_date=validated_data['payment_date'],
            currency=validated_data.get('currency'),
            exchange_rate=validated_data.get('exchange_rate'),
            payment_method=validated_data.get('payment_method'),
            payment_reference=validated_data.get('payment_reference', ''),
            notes=validated_data.get('notes', ''),
            allocations=allocations_data,
            user=self.context['request'].user,
        )


# =============================================================================
# PURCHASE RETURN SERIALIZERS
# =============================================================================

class PurchaseReturnItemSerializer(serializers.ModelSerializer):
    """Serializer pour les articles de retour fournisseur."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    
    class Meta:
        model = PurchaseReturnItem
        fields = [
            'id', 'product', 'product_name', 'product_sku',
            'variant', 'batch', 'quantity', 'unit_cost', 'total', 'reason'
        ]
        read_only_fields = ['id', 'total']


class PurchaseReturnListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes de retours fournisseur."""
    
    # ⚠ LA DEVISE D'UNE PIÈCE D'ACHAT EST CELLE DE SA COMMANDE.
    # Ni `GoodsReceipt` ni `PurchaseReturn` ne porte de devise à lui, et
    # `expedier_retour` comme `enregistrer_reglement` résolvent la leur depuis
    # `purchase_order.currency`. Sans ce champ, les écrans rendaient leurs
    # montants sous le symbole de la devise PRINCIPALE : « 40 000 FC » sur un
    # retour de 40 000 USD, dans la phrase même qui annonce un geste
    # irréversible.
    purchase_order_currency = serializers.CharField(
        source='purchase_order.currency', read_only=True, default=''
    )
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = PurchaseReturn
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'purchase_order', 'purchase_order_currency',
            'status', 'status_display',
            'total_amount', 'return_date',
            'items_count', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_items_count(self, obj):
        return obj.items.count()


class PurchaseReturnDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un retour fournisseur."""
    
    # ⚠ LA DEVISE D'UNE PIÈCE D'ACHAT EST CELLE DE SA COMMANDE.
    # Ni `GoodsReceipt` ni `PurchaseReturn` ne porte de devise à lui, et
    # `expedier_retour` comme `enregistrer_reglement` résolvent la leur depuis
    # `purchase_order.currency`. Sans ce champ, les écrans rendaient leurs
    # montants sous le symbole de la devise PRINCIPALE : « 40 000 FC » sur un
    # retour de 40 000 USD, dans la phrase même qui annonce un geste
    # irréversible.
    purchase_order_currency = serializers.CharField(
        source='purchase_order.currency', read_only=True, default=''
    )
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    items = PurchaseReturnItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = PurchaseReturn
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'purchase_order', 'purchase_order_currency',
            'warehouse', 'warehouse_name',
            'status', 'total_amount', 'reason', 'return_date',
            'created_by', 'created_by_name',
            'items', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at', 'updated_at']


class PurchaseReturnCreateSerializer(serializers.ModelSerializer):
    """Serializer pour la création de retour fournisseur."""
    
    items = PurchaseReturnItemSerializer(many=True)
    
    class Meta:
        model = PurchaseReturn
        fields = [
            'supplier', 'purchase_order', 'warehouse',
            'reason', 'return_date', 'items'
        ]

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Au moins un article est requis.")
        return value

    def validate(self, data):
        """
        Borne chaque ligne par ce qui RESTE à rendre sur la commande.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ ON POUVAIT RENDRE DEUX FOIS LA MÊME MARCHANDISE.                    │
        │                                                                      │
        │ Rien ne comparait la quantité rendue à ce qui avait été reçu, ni à   │
        │ ce qui avait déjà été rendu. Deux retours identiques sortaient donc  │
        │ deux fois la marchandise du stock et éteignaient deux fois la dette. │
        │ C'est le défaut exact corrigé côté VENTES le 2 octobre, resté ouvert │
        │ ici.                                                                 │
        └──────────────────────────────────────────────────────────────────────┘

        ⚠ Un retour SANS commande d'origine n'est pas borné, et c'est une
        limite assumée : on ne sait alors ni de quelle livraison la
        marchandise vient, ni ce qui a déjà été rendu dessus. Le stock, lui,
        reste opposé à l'approbation. Le back-office crée toujours un retour
        depuis sa commande.

        ⚠ Le message porte les TROIS nombres. Un refus qui dirait seulement
        « quantité trop élevée » laisserait le magasinier devant son carton
        sans savoir quoi saisir.
        """
        from django.db.models import Sum

        po = data.get('purchase_order')
        if not po:
            return data

        recues = {
            ligne['product_id']: ligne['total'] or Decimal('0.000')
            for ligne in GoodsReceiptItem.objects.filter(
                goods_receipt__purchase_order=po,
                goods_receipt__status=GoodsReceipt.Status.COMPLETED,
                goods_receipt__is_deleted=False,
            ).values('product_id').annotate(total=Sum('quantity_accepted'))
        }
        # ⚠ Un retour REJETÉ ou ANNULÉ ne consomme rien : la marchandise n'est
        # jamais repartie. Un BROUILLON, si - il est approuvable, et deux
        # brouillons sur les mêmes unités seraient tous deux approuvables.
        # Même raisonnement que le retour de vente.
        rendues = {
            ligne['product_id']: ligne['total'] or Decimal('0.000')
            for ligne in PurchaseReturnItem.objects.filter(
                purchase_return__purchase_order=po,
                purchase_return__is_deleted=False,
            ).exclude(
                purchase_return__status=PurchaseReturn.Status.CANCELLED
            ).values('product_id').annotate(total=Sum('quantity'))
        }

        demandees = {}
        for item in data.get('items', []):
            produit = item['product']
            demandees[produit.id] = (
                demandees.get(produit.id, Decimal('0.000')) + item['quantity']
            )

        erreurs = []
        for produit_id, demandee in demandees.items():
            recue = recues.get(produit_id, Decimal('0.000'))
            rendue = rendues.get(produit_id, Decimal('0.000'))
            reste = recue - rendue
            if demandee > reste:
                nom = next(
                    i['product'].name for i in data['items']
                    if i['product'].id == produit_id
                )
                erreurs.append(
                    f"{nom} : {rendue} déjà rendu sur {recue} reçu, "
                    f"il en reste {reste} à rendre (demandé : {demandee})."
                )
        if erreurs:
            raise serializers.ValidationError({'items': erreurs})

        return data

    def create(self, validated_data):
        items_data = validated_data.pop('items')

        from apps.core.numbering import (
            PREFIX_PURCHASE_RETURN, allocate_document_number,
        )

        org = _organisation(self)
        validated_data['reference'] = allocate_document_number(
            org, PREFIX_PURCHASE_RETURN,
        )
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        
        purchase_return = PurchaseReturn.objects.create(**validated_data)
        
        total_amount = Decimal('0.00')
        for item_data in items_data:
            quantity = item_data['quantity']
            unit_cost = item_data['unit_cost']
            total = quantity * unit_cost
            
            PurchaseReturnItem.objects.create(
                purchase_return=purchase_return,
                organization=org,
                product=item_data['product'],
                variant=item_data.get('variant'),
                batch=item_data.get('batch'),
                quantity=quantity,
                unit_cost=unit_cost,
                total=total,
                reason=item_data.get('reason', '')
            )
            
            total_amount += total
        
        purchase_return.total_amount = total_amount
        purchase_return.save()
        
        return purchase_return


# ==============================================================================
# RÉQUISITIONS DE PRODUITS (T12)
# ==============================================================================

class RequisitionItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    unit_name = serializers.CharField(source='product.unit.name', read_only=True)

    class Meta:
        model = RequisitionItem
        fields = [
            'id', 'product', 'product_name', 'product_sku', 'unit_name',
            'quantity_requested', 'stock_at_request', 'reorder_point_at_request',
            'notes',
        ]
        # ⚠ L'instantané est RELEVÉ par le service, jamais saisi : le laisser
        # écrire permettrait d'inscrire une pénurie qui n'existe pas, sur un
        # document qui sert à engager de l'argent.
        read_only_fields = ['stock_at_request', 'reorder_point_at_request']


class RequisitionSerializer(serializers.ModelSerializer):
    items = RequisitionItemSerializer(many=True, read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Requisition
        fields = [
            'id', 'reference', 'warehouse', 'warehouse_name', 'status',
            'status_display', 'notes', 'requested_by', 'requested_by_name',
            'decided_by', 'decided_by_name', 'decided_at', 'decision_notes',
            'requested_at', 'items',
        ]
        read_only_fields = [
            'reference', 'status', 'requested_by', 'decided_by', 'decided_at',
            'decision_notes', 'requested_at',
        ]

    @staticmethod
    def _nom(user):
        if not user:
            return None
        return f"{user.first_name} {user.last_name}".strip() or user.email

    def get_requested_by_name(self, obj):
        return self._nom(obj.requested_by)

    def get_decided_by_name(self, obj):
        return self._nom(obj.decided_by)


class RequisitionLineInputSerializer(serializers.Serializer):
    """Une ligne telle que le client l'envoie : l'article et la quantité."""

    product = serializers.UUIDField()
    quantity_requested = serializers.DecimalField(max_digits=12, decimal_places=3)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=255)


class RequisitionCreateSerializer(serializers.Serializer):
    """
    Le corps d'une création, et c'est EXACTEMENT celui que le journal envoie.

    La parité n'est pas surveillée, elle est structurelle : le gestionnaire de
    synchronisation rejoue cet acte par ce serializer et par le service que la
    vue appelle, sans dialecte intermédiaire.
    """

    warehouse = serializers.UUIDField()
    notes = serializers.CharField(required=False, allow_blank=True)
    items = RequisitionLineInputSerializer(many=True)
    #: Le numéro du TERMINAL, quand il en a déjà imprimé un. Le serveur le
    #: reprend : un papier qui porte un numéro remplacé ensuite ne désigne plus
    #: rien.
    reference = serializers.CharField(required=False, allow_blank=True, max_length=50)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError(
                "Une réquisition sans article ne demande rien."
            )
        vus = set()
        for ligne in value:
            if ligne['product'] in vus:
                raise serializers.ValidationError(
                    "Un même article ne peut figurer qu'une fois."
                )
            vus.add(ligne['product'])
        return value
