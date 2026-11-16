"""
Un devis converti SORT son stock, dès la conversion.

Un devis converti est une facture émise : le client repart avec la
marchandise. La conversion se contentait de la RÉSERVER, et elle ne sortait du
rayon qu'au paiement complet - donc jamais si la facture restait due. C'est le
défaut corrigé le 2026-10-20 sur la vente à crédit, resté ouvert sur ce chemin.

Les tests sont en GÉRANT, rôle borné à son entrepôt : un propriétaire passerait
à côté de tout le code de périmètre.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import InventorySession, Stock, StockMovement
from apps.products.models import Product
from apps.sales.models import Quotation, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _Base(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.methode = make_cash_payment_method(self.org)
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('1000.00'), cost_price=Decimal('700.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('50.000'), avg_cost=Decimal('700.00'),
        )
        self.acheteur = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='09',
            credit_limit=Decimal('0'),
        )
        self.client.force_authenticate(user=self.manager)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _devis(self, quantite='2'):
        r = self.client.post(
            '/api/v1/quotations/',
            {
                'customer': str(self.acheteur.id),
                'valid_until': '2026-12-31',
                'items': [{
                    'product': str(self.produit.id),
                    'quantity': quantite,
                    'unit_price': '1000.00',
                }],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        return Quotation.objects.get(customer=self.acheteur)

    def _convertir(self, devis):
        return self.client.post(
            f'/api/v1/quotations/{devis.id}/convert/',
            {'warehouse': str(self.warehouse.id)},
            format='json', **self._headers(),
        )

    def _stock(self):
        return Stock.objects.get(product=self.produit, warehouse=self.warehouse)

    def _sorties(self, vente):
        return StockMovement.objects.filter(
            reference_type='sale', movement_type='sale', reference_id=vente.id,
        )


class LaConversionSortLeStockTests(_Base):
    def test_la_conversion_sort_le_stock_et_ecrit_son_mouvement(self):
        r = self._convertir(self._devis('2'))
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        vente = Sale.objects.get(id=r.data['sale_id'])

        stock = self._stock()
        self.assertEqual(stock.quantity, Decimal('48.000'))
        # Rien ne reste « réservé » : la marchandise est partie, elle ne bloque
        # plus un disponible qu'elle ne fait plus partie.
        self.assertEqual(stock.reserved_quantity, Decimal('0.000'))
        self.assertEqual(self._sorties(vente).count(), 1)
        # La vente reste DUE : sortir le stock n'est pas encaisser.
        self.assertEqual(vente.status, 'pending')
        self.assertEqual(vente.amount_due, Decimal('2000.00'))

    def test_le_reglement_ensuite_ne_sort_pas_une_seconde_fois(self):
        r = self._convertir(self._devis('2'))
        vente = Sale.objects.get(id=r.data['sale_id'])

        p = self.client.post(
            f'/api/v1/sales/{vente.id}/add-payment/',
            {'payment_method': str(self.methode.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(p.status_code, status.HTTP_200_OK, p.data)

        vente.refresh_from_db()
        self.assertEqual(vente.status, 'completed')
        self.assertEqual(self._stock().quantity, Decimal('48.000'))
        self.assertEqual(self._sorties(vente).count(), 1)

    def test_annuler_la_vente_convertie_remet_le_stock(self):
        r = self._convertir(self._devis('2'))
        vente = Sale.objects.get(id=r.data['sale_id'])

        c = self.client.post(
            f'/api/v1/sales/{vente.id}/cancel/',
            {'reason': 'Client parti'},
            format='json', **self._headers(),
        )
        self.assertEqual(c.status_code, status.HTTP_200_OK, c.data)
        self.assertEqual(self._stock().quantity, Decimal('50.000'))


class LesRefusNEcriventRienTests(_Base):
    def test_un_stock_insuffisant_refuse_et_n_ecrit_rien(self):
        Stock.objects.filter(warehouse=self.warehouse).update(quantity=Decimal('1.000'))
        devis = self._devis('2')

        r = self._convertir(devis)

        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertEqual(r.data.get('code'), 'stock_insuffisant', r.data)
        devis.refresh_from_db()
        self.assertEqual(devis.status, 'draft')
        self.assertFalse(Sale.objects.filter(customer=self.acheteur).exists())
        self.assertEqual(self._stock().quantity, Decimal('1.000'))

    def test_un_inventaire_en_cours_refuse_la_conversion(self):
        devis = self._devis('2')
        session = InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse, reference='INV-DEV-0001',
            name='Inventaire', scope_type=InventorySession.ScopeType.PRODUCT,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        session.products.add(self.produit)

        r = self._convertir(devis)

        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertEqual(r.data.get('code'), 'inventaire_en_cours', r.data)
        devis.refresh_from_db()
        self.assertEqual(devis.status, 'draft')
        self.assertEqual(self._stock().quantity, Decimal('50.000'))


class LeJournalSortAussiLeStockTests(_Base):
    def test_une_conversion_venue_du_terminal_sort_le_stock(self):
        devis = self._devis('3')
        r = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': 'ffff5555-0000-4000-8000-000000000001',
                'kind': 'quotation.convert', 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-11-16T09:00:00Z',
                'payload': {'quotation': str(devis.id), 'warehouse': str(self.warehouse.id)},
            }]},
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        verdict = r.data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))

        vente = Sale.objects.get(id=verdict['server_ids']['sale'])
        self.assertEqual(self._stock().quantity, Decimal('47.000'))
        self.assertEqual(self._sorties(vente).count(), 1)
