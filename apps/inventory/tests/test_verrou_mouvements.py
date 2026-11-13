"""
Un inventaire en cours bloque TOUT ce qui fait bouger son stock, pas seulement la vente.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE VERROU NE BLOQUAIT QUE LA VENTE, ET SA PROPRE DOCSTRING DISAIT L'INVERSE. │
│                                                                              │
│ Une saisie manuelle, un transfert, un ajustement, un déconditionnement       │
│ faisaient bouger un rayon pendant qu'on le comptait : l'écart constaté à la  │
│ validation mesurait alors ces mouvements, et c'est sur ce chiffre que le     │
│ stock était ajusté.                                                          │
└──────────────────────────────────────────────────────────────────────────────┘

Le rôle est BORNÉ : un MAGASINIER, le plus faible qui porte `stock_movements.create`.
Chaque refus est suivi d'un CONTRÔLE (un autre dépôt, un autre article, la
session close) : un verrou qui refuserait tout passerait les premiers tests.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import (
    InventorySession, Stock, StockAdjustment, StockAdjustmentItem, StockMovement,
    StockTransfer, StockTransferItem, Warehouse,
)
from apps.inventory.services import (
    approve_adjustment, cancel_inventory_session, cancel_transfer, receive_transfer,
    ship_transfer, unpack_stock,
)
from apps.inventory.verrous import InventaireEnCours
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users, make_user

MOUVEMENTS = '/api/v1/stock-movements/'
OPERATIONS = '/api/v1/sync/operations/'


class _Base(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.magasinier = make_user('stock@vf.test', 'Sto', 'Ck')
        adhesion = OrganizationMembership.objects.create(
            user=self.magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        self.reserve = Warehouse.objects.create(
            organization=self.org, name='Réserve', code='RES',
        )
        adhesion.assigned_warehouses.add(self.warehouse, self.reserve)

        self.compte = self._produit('Coca 50cl', 'C50')
        self.libre = self._produit('Fanta 50cl', 'F50')

        # La session compte le Coca, et lui seul, dans le dépôt principal.
        self.session = InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse, reference='INV-TEST-0001',
            name='Inventaire Coca', scope_type=InventorySession.ScopeType.PRODUCT,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        self.session.products.add(self.compte)

        self.client.force_authenticate(user=self.magasinier)

    def _produit(self, nom, sku, facteur=None):
        produit = Product.objects.create(
            organization=self.org, name=nom, slug=sku.lower(), sku=sku,
            selling_price=Decimal('2000.00'), cost_price=Decimal('1200.00'),
            track_inventory=True,
            **({'units_per_package': facteur, 'selling_mode': 'wholesale_and_retail'} if facteur else {}),
        )
        for entrepot in (self.warehouse, self.reserve):
            Stock.objects.create(
                organization=self.org, product=produit, warehouse=entrepot,
                quantity=Decimal('48.000'), avg_cost=Decimal('1200.00'),
                **({'package_quantity': Decimal('4')} if facteur else {}),
            )
        return produit

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _stock(self, produit, entrepot=None):
        return Stock.objects.get(
            product=produit, warehouse=entrepot or self.warehouse,
        ).quantity

    def _saisie(self, produit, entrepot=None, quantite='5'):
        return self.client.post(
            MOUVEMENTS,
            {
                'product': str(produit.id),
                'warehouse': str((entrepot or self.warehouse).id),
                'movement_type': 'purchase', 'quantity': quantite, 'unit_cost': '1200',
            },
            format='json', **self._headers(),
        )


class SaisieManuelleTests(_Base):
    def test_une_entree_sur_un_article_en_comptage_est_refusee(self):
        r = self._saisie(self.compte)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertEqual(r.data['code'], 'inventaire_en_cours')
        self.assertIn('INV-TEST-0001', r.data['detail'])
        self.assertIn('Coca 50cl', r.data['detail'])
        self.assertEqual(self._stock(self.compte), Decimal('48.000'))
        self.assertFalse(StockMovement.objects.filter(product=self.compte).exists())

    def test_un_article_hors_du_comptage_bouge(self):
        r = self._saisie(self.libre)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(self._stock(self.libre), Decimal('53.000'))

    def test_le_meme_article_dans_un_autre_depot_bouge(self):
        # Le verrou porte sur un RAYON, pas sur l'article partout.
        r = self._saisie(self.compte, entrepot=self.reserve)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(self._stock(self.compte, self.reserve), Decimal('53.000'))

    def test_l_annulation_de_l_inventaire_leve_le_verrou(self):
        cancel_inventory_session(self.session, self.owner)
        r = self._saisie(self.compte)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)


class LivraisonDeMasseTests(_Base):
    def _livraison(self):
        return self.client.post(
            f'{MOUVEMENTS}bulk/',
            {
                'warehouse': str(self.warehouse.id), 'movement_type': 'purchase',
                'lines': [
                    {'product': str(self.libre.id), 'quantity': '5', 'unit_cost': '1200'},
                    {'product': str(self.compte.id), 'quantity': '5', 'unit_cost': '1200'},
                ],
            },
            format='json', **self._headers(),
        )

    def test_le_refus_est_indexe_sur_sa_ligne(self):
        r = self._livraison()
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        lignes = r.data['lines']
        self.assertEqual(set(lignes), {'1'} if '1' in lignes else {1}, lignes)
        message = str(lignes.get(1, lignes.get('1')))
        self.assertIn('INV-TEST-0001', message)

    def test_la_livraison_ne_passe_pas_a_moitie(self):
        self._livraison()
        # Une livraison est un tout : la ligne libre n'est pas entrée non plus.
        self.assertEqual(self._stock(self.libre), Decimal('48.000'))
        self.assertEqual(StockMovement.objects.count(), 0)


class JournalTests(_Base):
    def test_le_terminal_recoit_un_refus_nomme(self):
        op = '44444444-4444-4444-8444-444444444444'
        r = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op, 'kind': 'stock_movement.create', 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-11-13T09:00:00Z',
                'payload': {
                    'id': op, 'product': str(self.compte.id),
                    'warehouse': str(self.warehouse.id),
                    'movement_type': 'purchase', 'quantity': '5', 'unit_cost': '1200',
                },
            }]},
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        verdict = r.data['results'][0]
        # REJETÉ, jamais `retry` : il se reproduirait tant que la session vit.
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'inventaire_en_cours', verdict)
        self.assertEqual(self._stock(self.compte), Decimal('48.000'))


class TransfertsTests(_Base):
    def _transfert(self, source, destination, statut='draft'):
        t = StockTransfer.objects.create(
            organization=self.org, reference=f'TR-{source.code}-{destination.code}',
            source_warehouse=source, destination_warehouse=destination,
            status=statut, requested_by=self.owner,
        )
        StockTransferItem.objects.create(
            organization=self.org, transfer=t, product=self.compte,
            quantity_requested=Decimal('6'), quantity_shipped=Decimal('6'),
        )
        return t

    def test_on_n_expedie_pas_depuis_un_rayon_en_comptage(self):
        t = self._transfert(self.warehouse, self.reserve)
        with self.assertRaises(InventaireEnCours):
            ship_transfer(t, self.owner)
        self.assertEqual(self._stock(self.compte), Decimal('48.000'))

    def test_on_ne_receptionne_pas_dans_un_rayon_en_comptage(self):
        t = self._transfert(self.reserve, self.warehouse, statut='in_transit')
        with self.assertRaises(InventaireEnCours):
            receive_transfer(t, self.owner)
        self.assertEqual(self._stock(self.compte), Decimal('48.000'))

    def test_l_annulation_d_un_expedie_ne_remet_rien_sous_un_comptage(self):
        t = self._transfert(self.warehouse, self.reserve, statut='in_transit')
        with self.assertRaises(InventaireEnCours):
            cancel_transfer(t, self.owner)
        t.refresh_from_db()
        self.assertEqual(t.status, 'in_transit')

    def test_un_brouillon_s_annule_toujours(self):
        # Contrôle : un brouillon n'a rien sorti, son annulation ne bouge rien.
        t = self._transfert(self.warehouse, self.reserve)
        cancel_transfer(t, self.owner)
        t.refresh_from_db()
        self.assertEqual(t.status, 'cancelled')

    def test_un_transfert_entre_rayons_libres_passe(self):
        # Contrôle : la réserve n'est pas comptée.
        t = self._transfert(self.reserve, self.reserve)
        ship_transfer(t, self.owner)
        t.refresh_from_db()
        self.assertEqual(t.status, 'in_transit')


class AjustementEtDeconditionnementTests(_Base):
    def test_un_ajustement_ne_s_applique_pas_sous_un_comptage(self):
        a = StockAdjustment.objects.create(
            organization=self.org, reference='ADJ-TEST-0001', warehouse=self.warehouse,
            adjustment_type='damage', reason='Casse', status='draft', created_by=self.owner,
        )
        StockAdjustmentItem.objects.create(
            organization=self.org, adjustment=a, product=self.compte,
            quantity_counted=Decimal('40'), quantity_expected=Decimal('48'),
            quantity_difference=Decimal('-8'),
        )
        with self.assertRaises(InventaireEnCours):
            approve_adjustment(a, self.owner)
        a.refresh_from_db()
        self.assertEqual(a.status, 'draft')
        self.assertEqual(self._stock(self.compte), Decimal('48.000'))

    def test_on_n_ouvre_pas_un_carton_sous_un_comptage(self):
        casier = self._produit('Bière casier', 'BIE', facteur=12)
        self.session.products.add(casier)
        stock = Stock.objects.get(product=casier, warehouse=self.warehouse)
        with self.assertRaises(InventaireEnCours):
            unpack_stock(stock, self.owner, packages=1)
        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('4'))
