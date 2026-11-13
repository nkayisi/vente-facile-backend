"""
Modifier un article et un client depuis le terminal (lot L12), à parité avec la vue.

Les deux actes rejouent le serializer de `partial_update` du back-office :
`ProductUpdateSerializer` et `CustomerUpdateSerializer`. Ce qui ne se rejoue pas
de lui-même, et que ces tests tiennent :

- le PÉRIMÈTRE ENTREPÔT d'un article (`ProductViewSet.get_queryset` borne un
  `partial_update` aux articles suivis dans un dépôt du membre) ;
- l'unicité du SKU qui n'exclurait pas la fiche modifiée d'elle-même ;
- le refus par la PERMISSION (`blocked`), pour un caissier qui n'a ni
  `products.edit` ni `customers.edit`.

**Rôle BORNÉ** : le gérant, affecté au seul entrepôt principal.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock, Warehouse
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _EditionBase(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.nord = Warehouse.objects.create(organization=self.org, name='Dépôt Nord', code='WH-NORD')

        self.ici = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-1',
            selling_price=Decimal('1000.00'), cost_price=Decimal('600.00'), track_inventory=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.ici, warehouse=self.warehouse, quantity=Decimal('10'),
        )
        self.ailleurs = Product.objects.create(
            organization=self.org, name='Huile', slug='huile', sku='HUI-1',
            selling_price=Decimal('5000.00'), cost_price=Decimal('3000.00'), track_inventory=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.ailleurs, warehouse=self.nord, quantity=Decimal('10'),
        )
        self.client_ = Customer.objects.create(
            organization=self.org, name='Mama Lucie', code='CL-1', phone='0990000001',
        )
        self.client.force_authenticate(user=self.manager)

    def _journal(self, kind, payload, op_id='33333333-3333-4333-8333-333333333333'):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-11-13T09:00:00Z',
                'payload': payload,
            }]},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(
            verdict['verdict'], attendu,
            f"verdict={verdict['verdict']} errors={verdict.get('errors')}",
        )
        return verdict


class ModificationArticleTests(_EditionBase):
    def test_le_gerant_retarife_un_article_de_son_depot(self):
        self._verdict(self._journal('product.update', {'id': str(self.ici.id), 'selling_price': '1200'}))
        self.ici.refresh_from_db()
        self.assertEqual(self.ici.selling_price, Decimal('1200.00'))
        # Partiel : ce qui n'est pas envoyé ne bouge pas.
        self.assertEqual(self.ici.cost_price, Decimal('600.00'))

    def test_un_article_d_un_autre_depot_est_introuvable(self):
        # ⚠ Le défaut redouté : `_referentiel_update` ne borne que par
        #   organisation, et ouvrirait ce que `ProductViewSet` refuse.
        verdict = self._verdict(
            self._journal('product.update', {'id': str(self.ailleurs.id), 'selling_price': '1'}),
            attendu='rejected',
        )
        self.assertIn('introuvable', str(verdict.get('errors') or verdict).lower())
        self.ailleurs.refresh_from_db()
        self.assertEqual(self.ailleurs.selling_price, Decimal('5000.00'))

    def test_un_article_non_suivi_reste_modifiable_partout(self):
        # La règle du back-office : un article sans suivi de stock n'appartient
        # à aucun dépôt, il est visible de tous.
        service = Product.objects.create(
            organization=self.org, name='Livraison', slug='livraison', sku='SRV-1',
            selling_price=Decimal('500.00'), cost_price=Decimal('0'), track_inventory=False,
        )
        self._verdict(self._journal('product.update', {'id': str(service.id), 'name': 'Livraison express'}))
        service.refresh_from_db()
        self.assertEqual(service.name, 'Livraison express')

    def test_un_article_garde_son_propre_sku(self):
        self._verdict(self._journal('product.update', {
            'id': str(self.ici.id), 'sku': 'SAV-1', 'name': 'Savon de Marseille',
        }))

    def test_le_sku_d_un_autre_article_est_refuse(self):
        verdict = self._verdict(
            self._journal('product.update', {'id': str(self.ici.id), 'sku': 'HUI-1'}),
            attendu='rejected',
        )
        self.assertIn('sku', str(verdict.get('errors')).lower())

    def test_un_caissier_est_bloque(self):
        self.client.force_authenticate(user=self.cashier_a)
        self._verdict(
            self._journal('product.update', {'id': str(self.ici.id), 'selling_price': '1'}),
            attendu='blocked',
        )
        self.ici.refresh_from_db()
        self.assertEqual(self.ici.selling_price, Decimal('1000.00'))


class ModificationClientTests(_EditionBase):
    def test_le_gerant_modifie_le_client_et_ses_conditions_de_credit(self):
        verdict = self._verdict(self._journal('customer.update', {
            'id': str(self.client_.id), 'phone': '0990000002',
            'allow_credit': False, 'credit_limit': '50000',
        }))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.phone, '0990000002')
        self.assertFalse(self.client_.allow_credit)
        self.assertEqual(self.client_.credit_limit, Decimal('50000'))
        self.assertEqual(verdict['authoritative']['phone'], '0990000002')

    def test_un_particulier_perd_sa_raison_sociale(self):
        # Même règle que le back-office : le serializer la vide.
        self._verdict(self._journal('customer.update', {
            'id': str(self.client_.id), 'customer_type': 'individual', 'company_name': 'SARL Lucie',
        }))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.company_name, '')

    def test_un_client_d_une_autre_organisation_est_introuvable(self):
        from apps.organizations.models import Organization

        autre = Organization.objects.create(name='Autre', slug='autre-org')
        etranger = Customer.objects.create(organization=autre, name='Étranger', code='X-1', phone='0990000009')
        self._verdict(
            self._journal('customer.update', {'id': str(etranger.id), 'name': 'Piraté'}),
            attendu='rejected',
        )
        etranger.refresh_from_db()
        self.assertEqual(etranger.name, 'Étranger')

    def test_un_caissier_est_bloque(self):
        self.client.force_authenticate(user=self.cashier_a)
        self._verdict(
            self._journal('customer.update', {'id': str(self.client_.id), 'name': 'Autre'}),
            attendu='blocked',
        )
