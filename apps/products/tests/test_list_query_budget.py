"""
Le coût de la LISTE des articles ne dépend pas du nombre d'articles (lot L15).

`ProductListSerializer` lit la catégorie, la marque, l'unité, l'unité de
conditionnement et le stock : sans préchargement, chacun coûte une requête PAR
ligne. On mesure l'INVARIANT - deux articles, puis douze, chacun avec SA
catégorie, SA marque, SES unités et SON stock.

Rôle GÉRANT : le périmètre entrepôt des articles n'est appliqué qu'à un rôle
borné.
"""
from decimal import Decimal

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock
from apps.products.models import Brand, Category, Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class BudgetDeLaListeDesArticlesTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)
        self.rang = 0

    def _articles(self, n):
        for _ in range(n):
            self.rang += 1
            r = self.rang
            categorie = Category.objects.create(organization=self.org, name=f'Cat {r}', slug=f'cat-{r}')
            marque = Brand.objects.create(organization=self.org, name=f'Marque {r}', slug=f'marque-{r}')
            unite = Unit.objects.create(organization=self.org, name=f'piece{r}', symbol=f'p{r}')
            contenant = Unit.objects.create(organization=self.org, name=f'carton{r}', symbol=f'c{r}')
            produit = Product.objects.create(
                organization=self.org, name=f'Article {r:02d}', slug=f'art-{r}', sku=f'A-{r}',
                category=categorie, brand=marque, unit=unite, packaging_unit=contenant,
                selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL, units_per_package=12,
                cost_price=Decimal('600.00'), selling_price=Decimal('1000.00'),
                track_inventory=True, is_active=True,
            )
            Stock.objects.create(
                organization=self.org, product=produit, warehouse=self.warehouse,
                quantity=Decimal('30'),
            )

    def _compte(self) -> int:
        with CaptureQueriesContext(connection) as capture:
            r = self.client.get(
                '/api/v1/products/', {'page_size': 50}, HTTP_X_ORGANIZATION_ID=str(self.org.id),
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        return len(capture)

    @override_settings(DEBUG=False)
    def test_la_liste_ne_paie_PAS_par_article(self):
        self._articles(2)
        self._compte()
        peu = self._compte()
        self._articles(10)
        beaucoup = self._compte()
        self.assertEqual(
            peu, beaucoup,
            f"La liste des articles coûte des requêtes PAR ARTICLE ({peu} pour 2, "
            f"{beaucoup} pour 12) : il manque un `select_related` ou un "
            "`prefetch_related` dans `ProductViewSet.get_queryset`.",
        )

    @override_settings(DEBUG=False)
    def test_les_champs_lus_sont_bien_RENDUS(self):
        # Sans ce contrôle, retirer le préchargement ET les champs ferait passer
        # le test ci-dessus pour une raison qui n'est pas la sienne.
        self._articles(1)
        r = self.client.get('/api/v1/products/', HTTP_X_ORGANIZATION_ID=str(self.org.id))
        ligne = r.data['results'][0]
        self.assertEqual(ligne['category_name'], 'Cat 1')
        self.assertEqual(ligne['brand_name'], 'Marque 1')
        self.assertEqual(ligne['packaging_unit_name'], 'carton1')
