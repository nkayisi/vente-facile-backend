#!/usr/bin/env python
"""
Script de test pour l'API Vente Facile
Teste les endpoints principaux avant l'intégration frontend
"""
import requests
import json
from pprint import pprint

BASE_URL = "http://127.0.0.1:8000/api/v1"
ADMIN_EMAIL = "admin@ventefacile.com"
ADMIN_PASSWORD = "admin123"

class APITester:
    def __init__(self):
        self.token = None
        self.refresh_token = None
        self.org_id = None
        
    def print_section(self, title):
        print("\n" + "="*60)
        print(f"  {title}")
        print("="*60)
    
    def print_result(self, test_name, success, data=None):
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"\n{status} - {test_name}")
        if data:
            pprint(data, indent=2, width=80)
    
    def test_authentication(self):
        """Test 1: Authentification JWT"""
        self.print_section("TEST 1: Authentification JWT")
        
        try:
            response = requests.post(
                f"{BASE_URL}/auth/token/",
                json={
                    "email": ADMIN_EMAIL,
                    "password": ADMIN_PASSWORD
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                self.token = data.get('access')
                self.refresh_token = data.get('refresh')
                self.print_result("Obtention du token JWT", True, {
                    "access_token": self.token[:50] + "...",
                    "refresh_token": self.refresh_token[:50] + "..."
                })
                return True
            else:
                self.print_result("Obtention du token JWT", False, response.json())
                return False
        except Exception as e:
            self.print_result("Obtention du token JWT", False, str(e))
            return False
    
    def test_create_organization(self):
        """Test 2: Création d'une organisation"""
        self.print_section("TEST 2: Création d'une organisation")
        
        if not self.token:
            print("❌ Token non disponible, test ignoré")
            return False
        
        try:
            response = requests.post(
                f"{BASE_URL}/organizations/",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "name": "Ma Boutique Test",
                    "slug": "ma-boutique-test",
                    "business_type": "boutique",
                    "email": "contact@maboutique.com",
                    "phone": "+243123456789",
                    "address": "123 Avenue de la Liberté",
                    "city": "Kinshasa",
                    "country": "RDC",
                    "currency": "CDF",
                    "timezone": "Africa/Kinshasa"
                }
            )
            
            if response.status_code == 201:
                data = response.json()
                self.org_id = data.get('id')
                print(f"\n🔍 DEBUG: Organization ID capturé = {self.org_id}")
                self.print_result("Création d'organisation", True, data)
                return True
            else:
                self.print_result("Création d'organisation", False, response.json())
                return False
        except Exception as e:
            self.print_result("Création d'organisation", False, str(e))
            return False
    
    def test_list_organizations(self):
        """Test 3: Liste des organisations"""
        self.print_section("TEST 3: Liste des organisations")
        
        if not self.token:
            print("❌ Token non disponible, test ignoré")
            return False
        
        try:
            response = requests.get(
                f"{BASE_URL}/organizations/",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            
            if response.status_code == 200:
                data = response.json()
                self.print_result("Liste des organisations", True, {
                    "count": data.get('count'),
                    "results": data.get('results', [])[:2]
                })
                return True
            else:
                self.print_result("Liste des organisations", False, response.json())
                return False
        except Exception as e:
            self.print_result("Liste des organisations", False, str(e))
            return False
    
    def test_create_product(self):
        """Test 4: Création d'un produit"""
        self.print_section("TEST 4: Création d'un produit")
        
        print(f"\n🔍 DEBUG: Token = {self.token[:50] if self.token else None}...")
        print(f"🔍 DEBUG: Org ID = {self.org_id}")
        
        if not self.token or not self.org_id:
            print("❌ Token ou Organization ID non disponible, test ignoré")
            return False
        
        try:
            response = requests.post(
                f"{BASE_URL}/products/",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Organization-ID": self.org_id
                },
                json={
                    "name": "Coca-Cola 1.5L",
                    "slug": "coca-cola-15l",
                    "sku": "COCA-15L-001",
                    "barcode": "5449000000996",
                    "description": "Boisson gazeuse Coca-Cola 1.5 litres",
                    "product_type": "physical",
                    "cost_price": "1500.00",
                    "selling_price": "2000.00",
                    "tax_rate": "16.00",
                    "is_taxable": True,
                    "track_inventory": True,
                    "min_stock_level": 10,
                    "reorder_point": 20,
                    "is_active": True
                }
            )
            
            if response.status_code == 201:
                data = response.json()
                self.print_result("Création de produit", True, data)
                return True
            else:
                self.print_result("Création de produit", False, response.json())
                return False
        except Exception as e:
            self.print_result("Création de produit", False, str(e))
            return False
    
    def test_api_documentation(self):
        """Test 5: Accès à la documentation API"""
        self.print_section("TEST 5: Documentation API (Swagger)")
        
        try:
            response = requests.get("http://127.0.0.1:8000/api/docs/")
            
            if response.status_code == 200:
                self.print_result("Accès à Swagger UI", True, {
                    "url": "http://127.0.0.1:8000/api/docs/",
                    "status": "Accessible"
                })
                return True
            else:
                self.print_result("Accès à Swagger UI", False, {
                    "status_code": response.status_code
                })
                return False
        except Exception as e:
            self.print_result("Accès à Swagger UI", False, str(e))
            return False
    
    def run_all_tests(self):
        """Exécute tous les tests"""
        print("\n" + "🚀 " * 30)
        print("  TESTS DE L'API VENTE FACILE")
        print("🚀 " * 30)
        
        results = []
        
        # Test 1: Authentification
        results.append(("Authentification JWT", self.test_authentication()))
        
        # Test 2: Création organisation
        results.append(("Création organisation", self.test_create_organization()))
        
        # Test 3: Liste organisations
        results.append(("Liste organisations", self.test_list_organizations()))
        
        # Test 4: Création produit
        results.append(("Création produit", self.test_create_product()))
        
        # Test 5: Documentation API
        results.append(("Documentation API", self.test_api_documentation()))
        
        # Résumé
        self.print_section("RÉSUMÉ DES TESTS")
        passed = sum(1 for _, success in results if success)
        total = len(results)
        
        print(f"\nRésultats: {passed}/{total} tests réussis\n")
        
        for test_name, success in results:
            status = "✅" if success else "❌"
            print(f"{status} {test_name}")
        
        print("\n" + "="*60)
        
        if passed == total:
            print("🎉 Tous les tests sont passés avec succès!")
            print("✅ Le backend est prêt pour l'intégration frontend")
        else:
            print(f"⚠️  {total - passed} test(s) ont échoué")
            print("Veuillez vérifier les erreurs ci-dessus")
        
        print("="*60 + "\n")
        
        # Informations utiles
        print("📋 INFORMATIONS UTILES:")
        print(f"   - API Base URL: {BASE_URL}")
        print(f"   - Documentation Swagger: http://127.0.0.1:8000/api/docs/")
        print(f"   - Documentation ReDoc: http://127.0.0.1:8000/api/redoc/")
        print(f"   - Admin Django: http://127.0.0.1:8000/admin/")
        print(f"   - Email admin: {ADMIN_EMAIL}")
        print(f"   - Password admin: {ADMIN_PASSWORD}")
        if self.org_id:
            print(f"   - Organization ID: {self.org_id}")
        print()

if __name__ == "__main__":
    tester = APITester()
    tester.run_all_tests()
