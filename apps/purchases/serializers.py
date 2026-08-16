"""
Serializers DRF pour l'app Purchases.
"""
import logging

from rest_framework import serializers
from decimal import Decimal
from .models import (
    PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem,
    SupplierPayment, SupplierPaymentAllocation, PurchaseReturn, PurchaseReturnItem
)

logger = logging.getLogger(__name__)


# =============================================================================
# PURCHASE ORDER ITEM SERIALIZERS
# =============================================================================

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
        
        from apps.core.utils import ReferenceGenerator
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        validated_data['reference'] = ReferenceGenerator.generate_purchase_reference(org)
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
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        # Générer la référence
        from django.utils import timezone
        today = timezone.now()
        prefix = f"GRN{today.strftime('%Y%m%d')}"
        
        last = GoodsReceipt.objects.filter(
            organization=org,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError, AttributeError) as exc:
                # Format de référence inattendu (ancienne convention, données
                # migrées, ...). On loggue pour diagnostic et on repart à 1.
                logger.warning(
                    "Format de référence inattendu pour %s : %r (%s)",
                    last.__class__.__name__, last.reference, exc,
                )
                num = 1
        else:
            num = 1
        
        validated_data['reference'] = f"{prefix}-{num:04d}"
        validated_data['organization'] = org
        validated_data['received_by'] = self.context['request'].user
        
        grn = GoodsReceipt.objects.create(**validated_data)
        
        for item_data in items_data:
            GoodsReceiptItem.objects.create(
                goods_receipt=grn,
                organization=org,
                **item_data
            )
            
            # Mettre à jour la quantité reçue sur la commande
            po_item = item_data.get('purchase_order_item')
            if po_item:
                po_item.quantity_received += item_data['quantity_accepted']
                po_item.save()
        
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

    def create(self, validated_data):
        allocations_data = validated_data.pop('allocations', [])
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        # Générer la référence
        from django.utils import timezone
        today = timezone.now()
        prefix = f"PAY{today.strftime('%Y%m%d')}"
        
        last = SupplierPayment.objects.filter(
            organization=org,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError, AttributeError) as exc:
                # Format de référence inattendu (ancienne convention, données
                # migrées, ...). On loggue pour diagnostic et on repart à 1.
                logger.warning(
                    "Format de référence inattendu pour %s : %r (%s)",
                    last.__class__.__name__, last.reference, exc,
                )
                num = 1
        else:
            num = 1
        
        validated_data['reference'] = f"{prefix}-{num:04d}"
        validated_data['organization'] = org
        validated_data['created_by'] = self.context['request'].user
        validated_data['status'] = 'completed'
        
        payment = SupplierPayment.objects.create(**validated_data)
        
        for alloc_data in allocations_data:
            SupplierPaymentAllocation.objects.create(
                payment=payment,
                organization=org,
                **alloc_data
            )
            
            # Mettre à jour le montant payé sur la commande
            po = alloc_data['purchase_order']
            po.amount_paid += alloc_data['amount']
            po.amount_due = po.total - po.amount_paid
            po.save()
        
        # Mettre à jour le solde fournisseur
        supplier = payment.supplier
        supplier.current_balance -= payment.amount
        supplier.save()
        
        # Enregistrer le mouvement de caisse
        from apps.cashbook.services import record_purchase_payment
        # Lier au premier PO alloué s'il existe
        first_po = allocations_data[0]['purchase_order'] if allocations_data else None
        record_purchase_payment(
            organization=org,
            purchase_order=first_po,
            amount=payment.amount,
            supplier=supplier,
            # La sortie de caisse est dans la devise réellement décaissée.
            # `SupplierPayment.save()` l'a résolue vers une devise activée de
            # l'organisation, elle est donc fiable (auparavant le champ portait
            # un défaut 'USD' sans rapport, qu'on ne pouvait pas suivre).
            currency=payment.currency,
            exchange_rate=payment.exchange_rate,
            user=self.context['request'].user,
        )
        
        return payment


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
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = PurchaseReturn
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'purchase_order', 'status', 'status_display',
            'total_amount', 'return_date',
            'items_count', 'created_at'
        ]
        read_only_fields = ['id', 'reference', 'created_at']

    def get_items_count(self, obj):
        return obj.items.count()


class PurchaseReturnDetailSerializer(serializers.ModelSerializer):
    """Serializer complet pour le détail d'un retour fournisseur."""
    
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    items = PurchaseReturnItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = PurchaseReturn
        fields = [
            'id', 'reference', 'supplier', 'supplier_name',
            'purchase_order', 'warehouse', 'warehouse_name',
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

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        
        organization = self.context['request'].headers.get('X-Organization-ID')
        from apps.organizations.models import Organization
        org = Organization.objects.get(id=organization)
        
        # Générer la référence
        from django.utils import timezone
        today = timezone.now()
        prefix = f"PRET{today.strftime('%Y%m%d')}"
        
        last = PurchaseReturn.objects.filter(
            organization=org,
            reference__startswith=prefix
        ).order_by('-reference').first()
        
        if last:
            try:
                num = int(last.reference.split('-')[-1]) + 1
            except (ValueError, IndexError, AttributeError) as exc:
                # Format de référence inattendu (ancienne convention, données
                # migrées, ...). On loggue pour diagnostic et on repart à 1.
                logger.warning(
                    "Format de référence inattendu pour %s : %r (%s)",
                    last.__class__.__name__, last.reference, exc,
                )
                num = 1
        else:
            num = 1
        
        validated_data['reference'] = f"{prefix}-{num:04d}"
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
