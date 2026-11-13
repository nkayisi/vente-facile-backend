"""
Une annulation de vente et un retour client ne remettent rien en rayon sous un comptage.

Décision du 2026-11-13 : le verrou d'inventaire bloque AUSSI les remboursements
qui remettent en stock. L'annulation entière est refusée, plutôt qu'une
annulation sans remise en stock qui laisserait le rayon court de ce que le
client a rendu. Voir `apps/inventory/verrous.py`.

Le rôle est BORNÉ : un GÉRANT, le plus faible qui porte `sales.cancel` et
`sale_returns.approve`.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import InventorySession, Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale, SaleReturn
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users


class _Base(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        make_cash_payment_method(self.org)
        self.produit = Product.objects.create(
            organization=self.org, name='Article compté', slug='article', sku='A1',
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
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.manager,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.manager)
        self.vente = self._vente()

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _vente(self):
        r = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id), 'warehouse': str(self.warehouse.id),
                'sale_type': 'credit', 'is_pos': True, 'customer': str(self.acheteur.id),
                'items': [{'product': str(self.produit.id), 'quantity': '5', 'unit_price': '1000.00'}],
                'payments': [],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        return Sale.objects.get(pk=r.data['id'])

    def _compter(self):
        session = InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse, reference='INV-VTE-0001',
            name='Inventaire', scope_type=InventorySession.ScopeType.PRODUCT,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        session.products.add(self.produit)

    def _stock(self):
        return Stock.objects.get(product=self.produit, warehouse=self.warehouse).quantity


class AnnulationTests(_Base):
    def _annuler(self):
        return self.client.post(
            f'/api/v1/sales/{self.vente.id}/cancel/', {'reason': 'Erreur de saisie'},
            format='json', **self._headers(),
        )

    def test_une_vente_ne_s_annule_pas_sous_un_comptage(self):
        self.assertEqual(self._stock(), Decimal('45.000'))
        self._compter()
        r = self._annuler()
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertEqual(r.data['code'], 'inventaire_en_cours')
        self.vente.refresh_from_db()
        self.assertNotEqual(self.vente.status, 'cancelled')
        self.assertEqual(self._stock(), Decimal('45.000'))

    def test_sans_comptage_l_annulation_remet_en_stock(self):
        r = self._annuler()
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(self._stock(), Decimal('50.000'))


class RetourClientTests(_Base):
    def setUp(self):
        super().setUp()
        r = self.client.post(
            '/api/v1/sale-returns/',
            {
                'original_sale': str(self.vente.id), 'reason': 'Défectueux',
                'items': [{
                    'original_item': str(self.vente.items.first().id),
                    'quantity': '2', 'restock': True,
                }],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.retour = SaleReturn.objects.get(pk=r.data['id'])

    def test_la_creation_rend_l_identifiant_et_la_reference(self):
        # Le dialogue du back-office ouvre la fiche par `reponse.data.id`.
        self.assertTrue(self.retour.reference)

    def _approuver(self):
        return self.client.post(
            f'/api/v1/sale-returns/{self.retour.id}/approve/', {}, format='json', **self._headers(),
        )

    def test_un_retour_ne_remet_rien_en_rayon_sous_un_comptage(self):
        self._compter()
        r = self._approuver()
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertEqual(r.data['code'], 'inventaire_en_cours')
        self.retour.refresh_from_db()
        self.assertEqual(self.retour.status, 'draft')
        self.assertEqual(self._stock(), Decimal('45.000'))

    def test_sans_comptage_le_retour_remet_en_rayon(self):
        r = self._approuver()
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(self._stock(), Decimal('47.000'))
