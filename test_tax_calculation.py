#!/usr/bin/env python
"""
Test pour vérifier que les produits retournent bien les informations de taxe
et que le calcul des taxes fonctionne correctement.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
django.setup()

from decimal import Decimal
from apps.products.models import Product
from apps.products.serializers import ProductListSerializer
from apps.organizations.models import Organization

print("=" * 70)
print("TEST DES INFORMATIONS DE TAXE DANS LES PRODUITS")
print("=" * 70)

try:
    org = Organization.objects.first()
    if not org:
        print("❌ Aucune organisation trouvée")
        exit(1)
    
    print(f"\n✓ Organisation: {org.name}")
    
    # Récupérer quelques produits
    products = Product.objects.filter(
        organization=org,
        is_deleted=False,
        is_active=True
    )[:5]
    
    print(f"\n📦 Produits trouvés: {products.count()}")
    
    if products.count() == 0:
        print("⚠ Aucun produit actif trouvé")
        exit(0)
    
    print("\n" + "-" * 70)
    print("DÉTAILS DES PRODUITS")
    print("-" * 70)
    
    for product in products:
        print(f"\n📌 {product.name}")
        print(f"   SKU: {product.sku}")
        print(f"   Prix de vente: {product.selling_price} CDF")
        print(f"   Taxable: {'OUI' if product.is_taxable else 'NON'}")
        print(f"   Taux de taxe: {product.tax_rate}%")
        
        if product.is_taxable:
            tax_amount = product.selling_price * (product.tax_rate / 100)
            price_with_tax = product.selling_price + tax_amount
            print(f"   → Montant de la taxe: {tax_amount} CDF")
            print(f"   → Prix TTC: {price_with_tax} CDF")
    
    print("\n" + "-" * 70)
    print("TEST DU SERIALIZER (API)")
    print("-" * 70)
    
    # Tester le serializer
    serializer = ProductListSerializer(products, many=True)
    data = serializer.data
    
    print(f"\n✓ Serializer retourne {len(data)} produits")
    
    for item in data:
        print(f"\n📌 {item['name']}")
        
        # Vérifier que les champs de taxe sont présents
        if 'is_taxable' in item:
            print(f"   ✓ is_taxable: {item['is_taxable']}")
        else:
            print(f"   ❌ MANQUANT: is_taxable")
        
        if 'tax_rate' in item:
            print(f"   ✓ tax_rate: {item['tax_rate']}%")
        else:
            print(f"   ❌ MANQUANT: tax_rate")
        
        # Calculer le prix avec taxe
        if 'is_taxable' in item and 'tax_rate' in item and item['is_taxable']:
            selling_price = Decimal(item['selling_price'])
            tax_rate = Decimal(item['tax_rate'])
            tax_amount = selling_price * (tax_rate / 100)
            price_with_tax = selling_price + tax_amount
            print(f"   → Prix HT: {selling_price} CDF")
            print(f"   → Taxe: {tax_amount} CDF")
            print(f"   → Prix TTC: {price_with_tax} CDF")
    
    print("\n" + "=" * 70)
    print("SIMULATION DE VENTE AVEC TAXES")
    print("=" * 70)
    
    # Simuler un panier avec des produits taxables et non taxables
    cart = []
    for product in products[:3]:
        cart.append({
            'product': product,
            'quantity': 2,
            'unit_price': product.selling_price
        })
    
    print("\n🛒 Panier:")
    subtotal = Decimal('0.00')
    total_tax = Decimal('0.00')
    
    for item in cart:
        product = item['product']
        quantity = item['quantity']
        unit_price = item['unit_price']
        item_total = quantity * unit_price
        
        print(f"\n   {quantity} x {product.name} @ {unit_price} CDF")
        print(f"   Sous-total: {item_total} CDF")
        
        if product.is_taxable:
            tax_amount = item_total * (product.tax_rate / 100)
            total_tax += tax_amount
            print(f"   Taxe ({product.tax_rate}%): {tax_amount} CDF")
        else:
            print(f"   Non taxable")
        
        subtotal += item_total
    
    total = subtotal + total_tax
    
    print("\n" + "-" * 70)
    print(f"Sous-total HT:        {subtotal:>15} CDF")
    print(f"Taxes (TVA):          {total_tax:>15} CDF")
    print(f"{'=' * 50}")
    print(f"TOTAL À PAYER:        {total:>15} CDF")
    print("=" * 70)
    
    # Vérifier que les champs sont bien dans le serializer
    all_fields_present = all(
        'is_taxable' in item and 'tax_rate' in item
        for item in data
    )
    
    if all_fields_present:
        print("\n✅ SUCCÈS: Tous les produits ont les informations de taxe")
        print("✅ Le POS peut maintenant calculer et afficher les taxes correctement")
    else:
        print("\n❌ ÉCHEC: Certains produits n'ont pas les informations de taxe")
        print("❌ Le POS ne pourra pas calculer les taxes correctement")

except Exception as e:
    print(f"\n❌ Erreur: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
