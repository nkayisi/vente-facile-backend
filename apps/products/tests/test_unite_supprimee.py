"""
Une unité supprimée au back-office quitte AUSSI les terminaux.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ELLE Y SURVIVAIT POUR TOUJOURS.                                              │
│                                                                              │
│ La suppression était SQL : aucune pierre tombale ne pouvait partir, et       │
│ l'unité restait listée et proposée à la création d'article au comptoir.     │
│ Elle se supprime désormais en douceur, et `units` descend avec ses pierres  │
│ tombales - à condition que plus aucun article ne l'emploie, sans quoi le     │
│ comptoir afficherait des prix sans unité.                                    │
└──────────────────────────────────────────────────────────────────────────────┘

Rôle BORNÉ : un gérant, qui porte `products.delete` sans être propriétaire.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class UniteSupprimeeTests(APITestCase):
    def setUp(self):
        self.__dict__.update(make_org_with_users())
        self.client.force_authenticate(user=self.manager)
        self.kg = Unit.objects.create(organization=self.org, name='kilogramme', symbol='kg')

    def _h(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _supprimer(self, unite):
        return self.client.delete(f'/api/v1/units/{unite.id}/', **self._h())

    def test_une_unite_libre_se_supprime_en_douceur(self):
        self.assertEqual(self._supprimer(self.kg).status_code, 204)
        self.assertFalse(Unit.objects.filter(pk=self.kg.pk).exists())
        reste = Unit.objects.with_deleted().get(pk=self.kg.pk)
        self.assertTrue(reste.is_deleted)
        self.assertIsNotNone(reste.deleted_at)

    def test_une_unite_employee_est_refusee_en_disant_combien(self):
        Product.objects.create(
            organization=self.org, name='Riz', slug='riz', sku='RIZ', unit=self.kg,
            cost_price=Decimal('1'), selling_price=Decimal('2'),
        )
        reponse = self._supprimer(self.kg)
        self.assertEqual(reponse.status_code, 400)
        self.assertIn('1 article', str(reponse.data))
        self.assertTrue(Unit.objects.filter(pk=self.kg.pk).exists())

    def test_le_contenant_d_un_article_compte_aussi(self):
        sac = Unit.objects.create(organization=self.org, name='sac', symbol='sac')
        Product.objects.create(
            organization=self.org, name='Ciment', slug='ciment', sku='CIM',
            unit=self.kg, packaging_unit=sac, units_per_package=50,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            cost_price=Decimal('1'), selling_price=Decimal('2'),
        )
        self.assertEqual(self._supprimer(sac).status_code, 400)

    def test_le_tirage_emet_la_pierre_tombale(self):
        self._supprimer(self.kg)
        page = self.client.get('/api/v1/sync/pull/', {'table': 'units'}, **self._h())
        self.assertEqual(page.status_code, 200)
        self.assertIn(str(self.kg.pk), page.data['deleted_ids'])
        self.assertNotIn(str(self.kg.pk), {r['id'] for r in page.data['rows']})

    def test_le_nom_d_une_unite_supprimee_se_reprend(self):
        """Sans contrainte conditionnelle, « kg » serait pris pour toujours."""
        self._supprimer(self.kg)
        reponse = self.client.post(
            '/api/v1/units/', {'name': 'kilogramme', 'symbol': 'kg'},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
