"""
Aucune sortie ne fait passer un rayon sous zéro, par aucun chemin.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE STOCK NÉGATIF N'EXISTE PLUS, ET UN REFUS DE STOCK EST UN 400.            │
│                                                                              │
│ Deux défauts se refermaient l'un sur l'autre :                               │
│ - un entrepôt « négatif autorisé » laissait tout passer, et le rayon        │
│   annonçait ensuite une marchandise qui n'existait plus ;                    │
│ - partout ailleurs, la sortie qui manquait son pré-contrôle tombait sur la  │
│   `ValidationError` Django de `Stock.save()`, qui n'est pas une erreur DRF : │
│   500 au back-office, « erreur interne » au lieu de « stock insuffisant ».   │
│                                                                              │
│ Le refus porte un code, `stock_insuffisant`, que les deux clients lisent.    │
└──────────────────────────────────────────────────────────────────────────────┘

Le rôle est BORNÉ : un MAGASINIER pour le stock, un GÉRANT pour la vente. Chaque
refus a son CONTRÔLE (la même sortie dans la limite du stock passe) : un refus
qui mordrait sur tout passerait les premiers tests.

⚠ Les entrepôts et les articles sont posés `allow_negative_stock=True` partout
où le refus est éprouvé : c'est le drapeau qui ne doit plus rien ouvrir.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Supplier
from apps.core.exceptions import StockInsuffisant
from apps.inventory.models import (
    Stock, StockMovement, StockTransfer, StockTransferItem, Warehouse,
)
from apps.inventory.services import ship_transfer
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.purchases.models import PurchaseReturn, PurchaseReturnItem
from apps.purchases.services import approuver_retour
from apps.sales.models import RegisterSession
from apps.sales.tests._helpers import (
    make_cash_payment_method, make_org_with_users, make_user,
)

MOUVEMENTS = '/api/v1/stock-movements/'
TRANSFERTS = '/api/v1/stock-transfers/'
VENTES = '/api/v1/sales/'
OPERATIONS = '/api/v1/sync/operations/'


class _Base(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        # Le drapeau qui ouvrait le découvert : il ne doit plus rien ouvrir.
        Warehouse.objects.filter(id=self.warehouse.id).update(allow_negative_stock=True)
        self.warehouse.refresh_from_db()

        self.magasinier = make_user('stock@vf.test', 'Sto', 'Ck')
        adhesion = OrganizationMembership.objects.create(
            user=self.magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        self.reserve = Warehouse.objects.create(
            organization=self.org, name='Réserve', code='RES',
        )
        adhesion.assigned_warehouses.add(self.warehouse)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org,
        ).assigned_warehouses.add(self.reserve)

        self.produit = Product.objects.create(
            organization=self.org, name='Coca 50cl', slug='c50', sku='C50',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1200.00'),
            track_inventory=True, is_sellable=True, is_active=True,
        )
        Product.objects.filter(id=self.produit.id).update(allow_negative_stock=True)
        self.stock = Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('5.000'), avg_cost=Decimal('1200.00'),
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _quantite(self):
        self.stock.refresh_from_db()
        return self.stock.quantity

    def _refus_nomme(self, reponse):
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.json().get('code'), 'stock_insuffisant', reponse.json())


class SortieManuelleTests(_Base):
    def _sortie(self, quantite):
        self.client.force_authenticate(user=self.magasinier)
        return self.client.post(
            MOUVEMENTS,
            {
                'product': str(self.produit.id), 'warehouse': str(self.warehouse.id),
                'movement_type': 'damage', 'quantity': quantite,
            },
            format='json', **self._headers(),
        )

    def test_une_casse_au_dela_du_stock_est_un_400_nomme(self):
        r = self._sortie('8')
        self._refus_nomme(r)
        self.assertIn('Coca 50cl', r.json()['detail'])
        # Le contrôle explicite dit ce qui reste ; le filet de `Stock.save`, non.
        self.assertIn('Disponible', r.json()['detail'])
        self.assertEqual(self._quantite(), Decimal('5.000'))
        self.assertFalse(StockMovement.objects.filter(product=self.produit).exists())

    def test_une_casse_dans_la_limite_du_stock_passe(self):
        r = self._sortie('5')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(self._quantite(), Decimal('0.000'))


class VenteTests(_Base):
    def setUp(self):
        super().setUp()
        self.moyen = make_cash_payment_method(self.org)
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.manager,
            opening_balance=Decimal('0'), status='open',
        )

    def _vendre(self, *quantites):
        self.client.force_authenticate(user=self.manager)
        montant = str(sum(Decimal(q) for q in quantites) * Decimal('2000.00'))
        return self.client.post(
            VENTES,
            {
                'register': str(self.register.id), 'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [
                    {'product': str(self.produit.id), 'quantity': q, 'unit_price': '2000.00'}
                    for q in quantites
                ],
                'payments': [{'payment_method': str(self.moyen.id), 'amount': montant}],
            },
            format='json', **self._headers(),
        )

    def test_deux_lignes_du_meme_article_s_additionnent(self):
        # 3 + 3 sur un rayon de 5 : chacune passait seule le pré-contrôle, et la
        # seconde tombait au décrément, sous un autre message.
        r = self._vendre('3', '3')
        self._refus_nomme(r)
        self.assertIn('items', r.json())
        self.assertEqual(self._quantite(), Decimal('5.000'))

    def test_une_vente_au_dela_du_stock_est_refusee_meme_sur_un_entrepot_tolerant(self):
        r = self._vendre('8')
        self._refus_nomme(r)
        self.assertEqual(self._quantite(), Decimal('5.000'))

    def test_une_vente_dans_la_limite_du_stock_passe(self):
        r = self._vendre('5')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(self._quantite(), Decimal('0.000'))


class TransfertTests(_Base):
    def _transfert(self, quantite):
        t = StockTransfer.objects.create(
            organization=self.org, reference=f'TR-NEG-{quantite}',
            source_warehouse=self.warehouse, destination_warehouse=self.reserve,
            status='draft', requested_by=self.manager,
        )
        StockTransferItem.objects.create(
            organization=self.org, transfer=t, product=self.produit,
            quantity_requested=Decimal(quantite), quantity_shipped=Decimal(quantite),
        )
        return t

    def test_expedier_au_dela_du_stock_par_l_api_est_un_400_nomme(self):
        t = self._transfert('8')
        self.client.force_authenticate(user=self.manager)
        r = self.client.post(f'{TRANSFERTS}{t.id}/ship/', {}, format='json', **self._headers())
        self._refus_nomme(r)
        self.assertIn('Disponible', r.json()['detail'])
        t.refresh_from_db()
        self.assertEqual(t.status, 'draft')
        self.assertEqual(self._quantite(), Decimal('5.000'))

    def test_expedier_dans_la_limite_du_stock_passe(self):
        t = self._transfert('5')
        ship_transfer(t, self.manager)
        t.refresh_from_db()
        self.assertEqual(t.status, 'in_transit')
        self.assertEqual(self._quantite(), Decimal('0.000'))


class RetourFournisseurTests(_Base):
    def _retour(self, quantite):
        fournisseur = Supplier.objects.create(
            organization=self.org, code=f'F-{quantite}', name='Grossiste',
        )
        retour = PurchaseReturn.objects.create(
            organization=self.org, reference=f'RTF-NEG-{quantite}', supplier=fournisseur,
            warehouse=self.warehouse, reason='Avarie', return_date=date(2026, 11, 15),
            total_amount=Decimal('0'), created_by=self.manager,
        )
        PurchaseReturnItem.objects.create(
            organization=self.org, purchase_return=retour, product=self.produit,
            quantity=Decimal(quantite), unit_cost=Decimal('1200'), total=Decimal('0'),
        )
        return retour

    def test_rendre_au_fournisseur_au_dela_du_stock_est_refuse(self):
        with self.assertRaisesMessage(StockInsuffisant, 'Disponible'):
            approuver_retour(self._retour('8'), self.manager)
        self.assertEqual(self._quantite(), Decimal('5.000'))

    def test_rendre_dans_la_limite_du_stock_passe(self):
        approuver_retour(self._retour('5'), self.manager)
        self.assertEqual(self._quantite(), Decimal('0.000'))


class JournalTests(_Base):
    def test_le_terminal_recoit_un_refus_nomme_jamais_un_retry(self):
        op = '55555555-5555-4555-8555-555555555555'
        self.client.force_authenticate(user=self.magasinier)
        r = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op, 'kind': 'stock_movement.create', 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-11-15T09:00:00Z',
                'payload': {
                    'id': op, 'product': str(self.produit.id),
                    'warehouse': str(self.warehouse.id),
                    'movement_type': 'damage', 'quantity': '8',
                },
            }]},
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        verdict = r.data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'stock_insuffisant', verdict)
        self.assertEqual(self._quantite(), Decimal('5.000'))


class FiletDuStockTests(TestCase):
    """`Stock.save` refuse une BAISSE sous zéro, jamais une remontée."""

    def setUp(self):
        ctx = make_org_with_users()
        self.org, self.warehouse = ctx['org'], ctx['warehouse']
        self.produit = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV',
            selling_price=Decimal('500.00'), track_inventory=True,
        )

    def _rayon_negatif(self, quantite='-5.000'):
        stock = Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('0.000'),
        )
        # Un rayon déjà négatif, tel que l'ancien découvert l'a laissé : posé
        # sans passer par `save()`, qui le refuserait désormais.
        Stock.objects.filter(id=stock.id).update(quantity=Decimal(quantite))
        stock.refresh_from_db()
        return stock

    def test_un_rayon_neuf_ne_nait_pas_negatif(self):
        with self.assertRaises(StockInsuffisant):
            Stock.objects.create(
                organization=self.org, product=self.produit, warehouse=self.warehouse,
                quantity=Decimal('-1.000'),
            )

    def test_une_baisse_sous_zero_est_refusee(self):
        stock = Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('2.000'),
        )
        stock.quantity = Decimal('-1.000')
        with self.assertRaises(StockInsuffisant):
            stock.save()

    def test_une_entree_sur_un_rayon_negatif_passe(self):
        # Sans cela, la reprise serait impossible : on ne pourrait plus rien
        # faire entrer dans un rayon que l'ancien découvert a laissé négatif.
        stock = self._rayon_negatif()
        stock.quantity = Decimal('-2.000')
        stock.save()
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, Decimal('-2.000'))

    def test_une_ecriture_qui_ne_bouge_pas_la_quantite_passe(self):
        stock = self._rayon_negatif()
        stock.avg_cost = Decimal('10.00')
        stock.save()

    def test_creuser_un_rayon_deja_negatif_est_refuse(self):
        stock = self._rayon_negatif()
        stock.quantity = Decimal('-6.000')
        with self.assertRaises(StockInsuffisant):
            stock.save()
