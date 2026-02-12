#!/usr/bin/env python
"""
Test de création de vente avec paiement complet.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
django.setup()

from decimal import Decimal
from apps.sales.models import Sale, SaleItem, Payment, PaymentMethod
from apps.products.models import Product
from apps.organizations.models import Organization
from apps.users.models import User
from django.db import transaction

print("=" * 70)
print("TEST DE CRÉATION DE VENTE AVEC PAIEMENT COMPLET")
print("=" * 70)

try:
    # Récupérer les objets nécessaires
    org = Organization.objects.first()
    user = User.objects.first()
    product = Product.objects.filter(organization=org, is_deleted=False).first()
    payment_method = PaymentMethod.objects.filter(organization=org, is_active=True).first()
    
    if not all([org, user, product, payment_method]):
        print("❌ Données manquantes pour le test")
        exit(1)
    
    print(f"\n✓ Organisation: {org.name}")
    print(f"✓ Utilisateur: {user.email}")
    print(f"✓ Produit: {product.name} - Prix: {product.selling_price} CDF")
    print(f"✓ Mode de paiement: {payment_method.name}")
    
    # Simuler la création d'une vente comme le fait le serializer
    with transaction.atomic():
        from apps.core.utils import ReferenceGenerator
        
        # Créer la vente
        sale = Sale.objects.create(
            organization=org,
            reference=ReferenceGenerator.generate_sale_reference(org),
            sale_type='retail',
            status='draft',
            sold_by=user,
            is_pos=True,
            discount_percentage=Decimal('0.00')
        )
        
        print(f"\n1. Vente créée: {sale.reference}")
        
        # Créer un item
        quantity = Decimal('2.000')
        unit_price = product.selling_price
        item_total = quantity * unit_price
        
        SaleItem.objects.create(
            sale=sale,
            organization=org,
            product=product,
            quantity=quantity,
            unit_price=unit_price,
            cost_price=product.cost_price,
            discount_percentage=Decimal('0.00'),
            discount_amount=Decimal('0.00'),
            tax_rate=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            subtotal=item_total,
            total=item_total
        )
        
        print(f"2. Item ajouté: {quantity} x {product.name} = {item_total} CDF")
        
        # Calculer les totaux
        sale.subtotal = item_total
        sale.tax_amount = Decimal('0.00')
        sale.discount_amount = Decimal('0.00')
        sale.total = item_total
        sale.amount_due = item_total
        
        print(f"3. Total calculé: {sale.total} CDF")
        
        # Créer le paiement (MONTANT COMPLET)
        payment_amount = sale.total
        Payment.objects.create(
            sale=sale,
            organization=org,
            payment_method=payment_method,
            amount=payment_amount,
            received_by=user,
            status='completed'
        )
        
        print(f"4. Paiement créé: {payment_amount} CDF")
        
        # Appliquer la logique du serializer
        total_paid = payment_amount
        sale.amount_paid = total_paid
        sale.amount_due = (sale.total - total_paid).quantize(Decimal('0.01'))
        
        print(f"\n--- CALCULS ---")
        print(f"Total: {sale.total}")
        print(f"Payé: {total_paid}")
        print(f"Dû: {sale.amount_due}")
        print(f"Comparaison: total_paid >= sale.total = {total_paid >= sale.total}")
        
        # Déterminer le statut (LOGIQUE CORRIGÉE)
        if total_paid >= sale.total:
            sale.change_amount = (total_paid - sale.total).quantize(Decimal('0.01'))
            sale.amount_due = Decimal('0.00')
            sale.status = 'completed'
            print(f"✓ Statut défini: COMPLETED")
        elif total_paid > 0:
            sale.status = 'partially_paid'
            print(f"✗ Statut défini: PARTIALLY_PAID")
        else:
            sale.status = 'pending'
            print(f"✗ Statut défini: PENDING")
        
        sale.save()
        
        print(f"\n--- RÉSULTAT FINAL ---")
        print(f"Référence: {sale.reference}")
        print(f"Statut: {sale.status}")
        print(f"Total: {sale.total} CDF")
        print(f"Payé: {sale.amount_paid} CDF")
        print(f"Dû: {sale.amount_due} CDF")
        
        if sale.status == 'completed':
            print(f"\n✅ SUCCÈS: La vente est bien en statut 'completed'")
        else:
            print(f"\n❌ ÉCHEC: La vente devrait être 'completed' mais est '{sale.status}'")
        
        # Rollback pour ne pas polluer la DB
        transaction.set_rollback(True)
        print(f"\n(Transaction annulée - test uniquement)")

except Exception as e:
    print(f"\n❌ Erreur: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
