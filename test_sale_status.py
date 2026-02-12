#!/usr/bin/env python
"""
Script de test pour vérifier la logique de statut des ventes.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
django.setup()

from decimal import Decimal
from apps.sales.models import Sale
from apps.organizations.models import Organization

print("=" * 70)
print("TEST DE LA LOGIQUE DE STATUT DES VENTES")
print("=" * 70)

# Test 1: Vérifier la logique de comparaison
print("\n1. Test de la logique de comparaison Decimal:")
total = Decimal('10000.00')
paid = Decimal('10000.00')
amount_due = (total - paid).quantize(Decimal('0.01'))

print(f"   Total: {total}")
print(f"   Payé: {paid}")
print(f"   Dû: {amount_due}")
print(f"   paid >= total: {paid >= total}")
print(f"   amount_due <= 0: {amount_due <= 0}")

if paid >= total:
    print("   ✓ Devrait être COMPLETED")
else:
    print("   ✗ Serait PARTIALLY_PAID")

# Test 2: Vérifier les ventes existantes
print("\n2. Vérification des ventes existantes:")
try:
    org = Organization.objects.first()
    if org:
        sales = Sale.objects.filter(
            organization=org,
            status='partially_paid',
            is_deleted=False
        )[:5]
        
        print(f"   Ventes 'partially_paid' trouvées: {sales.count()}")
        
        for sale in sales:
            amount_due = (sale.total - sale.amount_paid).quantize(Decimal('0.01'))
            should_be_completed = sale.amount_paid >= sale.total
            
            print(f"\n   Vente: {sale.reference}")
            print(f"   - Total: {sale.total}")
            print(f"   - Payé: {sale.amount_paid}")
            print(f"   - Dû (DB): {sale.amount_due}")
            print(f"   - Dû (calculé): {amount_due}")
            print(f"   - Statut actuel: {sale.status}")
            print(f"   - Devrait être: {'COMPLETED' if should_be_completed else 'PARTIALLY_PAID'}")
            
            if should_be_completed:
                print(f"   ⚠ PROBLÈME: Cette vente devrait être 'completed'!")
    else:
        print("   Aucune organisation trouvée")
except Exception as e:
    print(f"   Erreur: {e}")

# Test 3: Vérifier la dernière vente créée
print("\n3. Dernière vente créée:")
try:
    last_sale = Sale.objects.filter(is_deleted=False).order_by('-created_at').first()
    if last_sale:
        print(f"   Référence: {last_sale.reference}")
        print(f"   Total: {last_sale.total}")
        print(f"   Payé: {last_sale.amount_paid}")
        print(f"   Dû: {last_sale.amount_due}")
        print(f"   Statut: {last_sale.status}")
        print(f"   Date: {last_sale.created_at}")
        
        # Vérifier les paiements
        payments = last_sale.payments.all()
        print(f"   Paiements: {payments.count()}")
        for payment in payments:
            print(f"     - {payment.payment_method.name}: {payment.amount} CDF")
    else:
        print("   Aucune vente trouvée")
except Exception as e:
    print(f"   Erreur: {e}")

print("\n" + "=" * 70)
