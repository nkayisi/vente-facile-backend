from decimal import Decimal
from rest_framework import status
from rest_framework.test import APITestCase
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users


class ProductDetailEndpointTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.owner)

    @property
    def _h(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def test_detail_produit_simple(self):
        p = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            cost_price=Decimal('400.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        r = self.client.get(f'/api/v1/products/{p.id}/', **self._h)
        print("STATUS SIMPLE:", r.status_code)
        if r.status_code != 200:
            print("BODY:", r.data)
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_detail_apres_creation_api(self):
        r = self.client.post('/api/v1/products/', {
            'name': 'Riz 25kg', 'sku': 'RIZ-25',
            'cost_price': '400.00', 'selling_price': '800.00',
        }, format='json', **self._h)
        print("STATUS CREATE:", r.status_code)
        pid = r.data.get('id')
        print("ID:", pid)
        r2 = self.client.get(f'/api/v1/products/{pid}/', **self._h)
        print("STATUS DETAIL:", r2.status_code)
        if r2.status_code != 200:
            print("BODY:", r2.data)
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
