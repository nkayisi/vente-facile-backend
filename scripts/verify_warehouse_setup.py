#!/usr/bin/env python
"""
Script pour vérifier que les caisses ont bien un entrepôt configuré
et que le stock sera réduit lors des ventes.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
django.setup()

from apps.sales.models import Register, RegisterSession
from apps.organizations.models import Organization

print("=" * 70)
print("VÉRIFICATION DE LA CONFIGURATION DES ENTREPÔTS")
print("=" * 70)

try:
    org = Organization.objects.first()
    if not org:
        print("❌ Aucune organisation trouvée")
        exit(1)
    
    print(f"\n✓ Organisation: {org.name}")
    
    # Vérifier les caisses
    registers = Register.objects.filter(
        organization=org,
        is_deleted=False
    )
    
    print(f"\n📦 Caisses trouvées: {registers.count()}")
    
    if registers.count() == 0:
        print("\n⚠ ATTENTION: Aucune caisse trouvée!")
        print("   Vous devez créer une caisse pour utiliser le POS.")
        exit(0)
    
    print("\n" + "-" * 70)
    print("DÉTAILS DES CAISSES")
    print("-" * 70)
    
    caisses_sans_entrepot = []
    
    for register in registers:
        print(f"\n📌 {register.name} ({register.code})")
        print(f"   Branche: {register.branch.name}")
        
        if register.warehouse:
            print(f"   ✓ Entrepôt: {register.warehouse.name}")
            print(f"   → Le stock sera réduit lors des ventes")
        else:
            print(f"   ❌ AUCUN ENTREPÔT CONFIGURÉ")
            print(f"   → Le stock NE SERA PAS réduit lors des ventes!")
            caisses_sans_entrepot.append(register)
        
        # Vérifier les sessions actives
        active_session = register.sessions.filter(status='open').first()
        if active_session:
            print(f"   Session active: Ouverte par {active_session.opened_by.full_name}")
        else:
            print(f"   Aucune session active")
    
    print("\n" + "=" * 70)
    print("RÉSUMÉ")
    print("=" * 70)
    
    if caisses_sans_entrepot:
        print(f"\n❌ PROBLÈME DÉTECTÉ:")
        print(f"   {len(caisses_sans_entrepot)} caisse(s) n'ont pas d'entrepôt configuré:")
        for register in caisses_sans_entrepot:
            print(f"   - {register.name}")
        
        print(f"\n💡 SOLUTION:")
        print(f"   1. Allez dans 'Ventes' → 'Caisses'")
        print(f"   2. Modifiez chaque caisse")
        print(f"   3. Sélectionnez un entrepôt dans le champ 'Entrepôt'")
        print(f"   4. Sauvegardez")
        print(f"\n   Après cela, le stock sera automatiquement réduit lors des ventes.")
    else:
        print(f"\n✅ TOUT EST CORRECT:")
        print(f"   Toutes les caisses ont un entrepôt configuré.")
        print(f"   Le stock sera automatiquement réduit lors des ventes.")
    
    # Vérifier qu'il y a au moins un entrepôt
    from apps.inventory.models import Warehouse
    warehouses = Warehouse.objects.filter(
        organization=org,
        is_deleted=False,
        is_active=True
    )
    
    print(f"\n📦 Entrepôts disponibles: {warehouses.count()}")
    for warehouse in warehouses:
        print(f"   - {warehouse.name} ({warehouse.code})")
    
    if warehouses.count() == 0:
        print(f"\n⚠ ATTENTION: Aucun entrepôt actif trouvé!")
        print(f"   Vous devez créer au moins un entrepôt.")
        print(f"   Allez dans 'Inventaire' → 'Entrepôts' pour en créer un.")

except Exception as e:
    print(f"\n❌ Erreur: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
