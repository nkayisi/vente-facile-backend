"""
La fin du stock négatif : la reprise des rayons, les drapeaux forcés, les
contrôles de déploiement, et ce que le comptoir demande au serveur.

Chaque garde-fou a son contrôle : la reprise ne touche pas un rayon positif,
la migration ne bascule que les drapeaux levés, le contrôle de déploiement ne
crie pas sur une base saine.
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.management.commands.verifier_deploiement import (
    AVERTISSEMENT, OK, entrepots_principaux, membres_sans_entrepot,
    stocks_negatifs, ventes_sans_sortie_de_stock,
)
from apps.core.tests.migrations_harness import MigrationDataTestCase
from apps.inventory.models import InventorySession, Stock, StockMovement, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem
from apps.sales.services import SaleStockService
from apps.sales.tests._helpers import make_org_with_users

VERROUS = '/api/v1/inventory-sessions/locked-products/'
ARTICLES = '/api/v1/products/'


def _niveaux(constats):
    return {c.niveau for c in constats}


class _Rayons(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

    def _article(self, nom, sku):
        return Product.objects.create(
            organization=self.org, name=nom, slug=sku.lower(), sku=sku,
            selling_price=Decimal('500.00'), track_inventory=True,
        )

    def _rayon(self, produit, quantite):
        stock = Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal('0.000'),
        )
        # Tel que l'ancien découvert l'a laissé : posé sans `save()`, qui le
        # refuserait désormais.
        Stock.objects.filter(id=stock.id).update(quantity=Decimal(quantite))
        return stock


class RepriseDesRayonsTests(_Rayons):
    def setUp(self):
        super().setUp()
        self.savon = self._article('Savon', 'SAV')
        self.sucre = self._article('Sucre', 'SUC')
        self.negatif = self._rayon(self.savon, '-4.000')
        self.sain = self._rayon(self.sucre, '7.000')

    def _quantite(self, stock):
        stock.refresh_from_db()
        return stock.quantity

    def test_la_simulation_n_ecrit_rien(self):
        sortie = StringIO()
        call_command('reparer_stocks_negatifs', stdout=sortie)
        self.assertIn('Savon', sortie.getvalue())
        self.assertEqual(self._quantite(self.negatif), Decimal('-4.000'))
        self.assertFalse(StockMovement.objects.exists())

    def test_appliquer_remet_a_zero_et_trace(self):
        call_command('reparer_stocks_negatifs', '--appliquer', stdout=StringIO())
        self.assertEqual(self._quantite(self.negatif), Decimal('0.000'))
        mouvement = StockMovement.objects.get(product=self.savon)
        self.assertEqual(mouvement.movement_type, 'adjustment_in')
        self.assertEqual(mouvement.quantity, Decimal('4.000'))
        self.assertEqual(mouvement.quantity_before, Decimal('-4.000'))
        self.assertEqual(mouvement.quantity_after, Decimal('0.000'))
        self.assertEqual(mouvement.notes, 'Reprise : fin du stock négatif')

    def test_un_rayon_positif_n_est_pas_touche(self):
        call_command('reparer_stocks_negatifs', '--appliquer', stdout=StringIO())
        self.assertEqual(self._quantite(self.sain), Decimal('7.000'))
        self.assertFalse(StockMovement.objects.filter(product=self.sucre).exists())

    def test_relancee_elle_ne_trouve_plus_rien(self):
        call_command('reparer_stocks_negatifs', '--appliquer', stdout=StringIO())
        call_command('reparer_stocks_negatifs', '--appliquer', stdout=StringIO())
        self.assertEqual(StockMovement.objects.filter(product=self.savon).count(), 1)

    def test_un_article_sous_inventaire_est_nomme_et_laisse(self):
        session = InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse, reference='INV-NEG-0001',
            name='Comptage savon', scope_type=InventorySession.ScopeType.PRODUCT,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        session.products.add(self.savon)
        sortie = StringIO()
        call_command('reparer_stocks_negatifs', '--appliquer', stdout=sortie)
        self.assertIn('sous inventaire', sortie.getvalue())
        self.assertEqual(self._quantite(self.negatif), Decimal('-4.000'))

    def test_les_deux_options_s_excluent(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('reparer_stocks_negatifs', '--appliquer', '--dry-run', stdout=StringIO())


class ControlesDeDeploiementTests(_Rayons):
    def test_une_base_saine_passe_les_quatre(self):
        for controle in (
            stocks_negatifs, entrepots_principaux, membres_sans_entrepot,
            ventes_sans_sortie_de_stock,
        ):
            self.assertEqual(_niveaux(controle()), {OK}, controle.__name__)

    def test_un_rayon_negatif_est_signale(self):
        self._rayon(self._article('Savon', 'SAV'), '-1.000')
        constats = stocks_negatifs()
        self.assertEqual(_niveaux(constats), {AVERTISSEMENT})
        self.assertIn('reparer_stocks_negatifs', constats[0].message)

    def test_un_etablissement_sans_principal_actif_est_signale(self):
        Warehouse.objects.filter(id=self.warehouse.id).update(is_default=False)
        self.assertEqual(_niveaux(entrepots_principaux()), {AVERTISSEMENT})

    def test_un_membre_sans_entrepot_est_signale(self):
        OrganizationMembership.objects.get(user=self.cashier_a).assigned_warehouses.clear()
        constats = membres_sans_entrepot()
        self.assertEqual(_niveaux(constats), {AVERTISSEMENT})
        self.assertIn('reparer_membres_sans_entrepot', constats[0].message)

    def test_une_vente_emise_sans_sortie_de_stock_est_signalee(self):
        produit = self._article('Savon', 'SAV')
        vente = Sale.objects.create(
            organization=self.org, reference='VT-NEG-0001', warehouse=self.warehouse,
            status='pending', sold_by=self.manager,
        )
        SaleItem.objects.create(
            organization=self.org, sale=vente, product=produit,
            quantity=Decimal('1'), unit_price=Decimal('500.00'),
        )
        self.assertEqual(_niveaux(ventes_sans_sortie_de_stock()), {AVERTISSEMENT})


class ReglementSansStockTests(_Rayons):
    """Une ANCIENNE vente à crédit se règle même quand son rayon ne la compte plus."""

    def test_le_decrement_differe_n_empeche_pas_le_reglement(self):
        produit = self._article('Savon', 'SAV')
        Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal('0.000'),
        )
        vente = Sale.objects.create(
            organization=self.org, reference='VT-NEG-0002', warehouse=self.warehouse,
            status='completed', sold_by=self.manager,
        )
        SaleItem.objects.create(
            organization=self.org, sale=vente, product=produit,
            quantity=Decimal('3'), unit_price=Decimal('500.00'),
        )
        # Ne lève pas : le règlement passe, le décrément est abandonné et nommé.
        SaleStockService.decrement_au_reglement(vente, self.manager)
        self.assertFalse(SaleStockService.is_committed(vente))
        self.assertEqual(
            Stock.objects.get(product=produit, warehouse=self.warehouse).quantity,
            Decimal('0.000'),
        )


class DrapeauEntrepotMigrationTests(MigrationDataTestCase):
    migrate_from = ('inventory', '0020_un_seul_principal')
    migrate_to = ('inventory', '0021_fin_du_stock_negatif')

    def preparer(self, apps):
        Organization = apps.get_model('organizations', 'Organization')
        Warehouse = apps.get_model('inventory', 'Warehouse')
        org = Organization.objects.create(name='Org', slug='org-neg')
        self.tolerant = Warehouse.objects.create(
            organization=org, name='Tolérant', code='TOL', allow_negative_stock=True,
        )
        self.strict = Warehouse.objects.create(
            organization=org, name='Strict', code='STR', allow_negative_stock=False,
        )
        self.avant = {w.id: w.updated_at for w in Warehouse.objects.all()}

    def test_le_drapeau_est_force_a_faux_et_l_horodatage_bouge(self):
        Warehouse = self.apps_apres.get_model('inventory', 'Warehouse')
        tolerant = Warehouse.objects.get(id=self.tolerant.id)
        self.assertFalse(tolerant.allow_negative_stock)
        # Sans lui, les terminaux garderaient `True` : `warehouses` descend
        # au tirage par `updated_at`.
        self.assertGreater(tolerant.updated_at, self.avant[self.tolerant.id])

    def test_un_entrepot_deja_strict_n_est_pas_retouche(self):
        Warehouse = self.apps_apres.get_model('inventory', 'Warehouse')
        strict = Warehouse.objects.get(id=self.strict.id)
        self.assertEqual(strict.updated_at, self.avant[self.strict.id])


class DrapeauArticleMigrationTests(MigrationDataTestCase):
    migrate_from = ('products', '0014_unite_suppression_douce')
    migrate_to = ('products', '0015_fin_du_stock_negatif')

    def preparer(self, apps):
        Organization = apps.get_model('organizations', 'Organization')
        Product = apps.get_model('products', 'Product')
        org = Organization.objects.create(name='Org', slug='org-neg-p')
        self.tolerant = Product.objects.create(
            organization=org, name='Tolérant', slug='tol', sku='TOL',
            selling_price=Decimal('1'), allow_negative_stock=True,
        )
        self.avant = Product.objects.get(id=self.tolerant.id).updated_at

    def test_le_drapeau_est_force_a_faux_et_les_horodatages_bougent(self):
        Product = self.apps_apres.get_model('products', 'Product')
        p = Product.objects.get(id=self.tolerant.id)
        self.assertFalse(p.allow_negative_stock)
        self.assertGreater(p.updated_at, self.avant)
        self.assertIsNotNone(p.sync_updated_at)


class ComptoirTests(APITestCase):
    """Ce que le comptoir demande au serveur : verrous par caisse, catalogue vendable."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.boutique = Warehouse.objects.create(
            organization=self.org, name='Boutique', code='BOU',
        )
        OrganizationMembership.objects.get(user=self.manager).assigned_warehouses.add(self.boutique)
        self.savon = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV',
            selling_price=Decimal('500.00'), track_inventory=True, barcode='111',
        )
        self.sachet = Product.objects.create(
            organization=self.org, name='Sachet', slug='sachet', sku='SAC',
            selling_price=Decimal('1.00'), track_inventory=True, is_sellable=False,
        )
        session = InventorySession.objects.create(
            organization=self.org, warehouse=self.boutique, reference='INV-BOU-0001',
            name='Comptage boutique', scope_type=InventorySession.ScopeType.PRODUCT,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        session.products.add(self.savon)
        self.client.force_authenticate(user=self.manager)

    def _h(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def test_les_verrous_de_la_caisse_seulement(self):
        r = self.client.get(VERROUS, {'warehouse': str(self.warehouse.id)}, **self._h())
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data['locked_product_ids'], [])

    def test_le_depot_de_la_caisse_porte_ses_verrous(self):
        r = self.client.get(VERROUS, {'warehouse': str(self.boutique.id)}, **self._h())
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual([str(i) for i in r.data['locked_product_ids']], [str(self.savon.id)])

    def test_sans_caisse_tous_les_verrous_du_perimetre(self):
        r = self.client.get(VERROUS, **self._h())
        self.assertEqual([str(i) for i in r.data['locked_product_ids']], [str(self.savon.id)])

    def test_un_depot_hors_perimetre_leve(self):
        autre = Warehouse.objects.create(organization=self.org, name='Autre', code='AUT')
        r = self.client.get(VERROUS, {'warehouse': str(autre.id)}, **self._h())
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)

    def test_le_catalogue_vendable_ecarte_le_consommable(self):
        r = self.client.get(
            ARTICLES, {'full_catalog': 'true', 'is_sellable': 'true', 'page_size': 50},
            **self._h(),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        noms = {p['name'] for p in r.data['results']}
        self.assertIn('Savon', noms)
        self.assertNotIn('Sachet', noms)

    def test_le_code_barres_trouve_l_article_sans_stock_avec_tout_le_catalogue(self):
        # Le savon n'a de rayon dans AUCUN dépôt du gérant : sans `full_catalog`
        # la règle d'entrepôt l'écarte, avec elle le comptoir le trouve.
        r = self.client.get(f'{ARTICLES}search-barcode/', {'barcode': '111'}, **self._h())
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND, r.data)
        r = self.client.get(
            f'{ARTICLES}search-barcode/', {'barcode': '111', 'full_catalog': 'true'},
            **self._h(),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data['name'], 'Savon')
