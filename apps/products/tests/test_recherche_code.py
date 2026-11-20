"""
`GET /products/search-barcode/` : l'article d'un code scanné, vu comme une carte.

Le comptoir web scanne un code (douchette ou caméra) puis ouvre la saisie de
quantité de l'article trouvé. Il lui faut donc la forme de la LISTE, stock du
dépôt compris : la fiche ne porte aucun stock, et un article bien présent se
serait lu « 0 en stock ».

Rôle GÉRANT : le périmètre entrepôt n'est appliqué qu'à un rôle borné.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, Warehouse
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users

URL = '/api/v1/products/search-barcode/'


class RechercheParCodeTests(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)
        self.article = Product.objects.create(
            organization=self.org, name='Savon Omo', slug='savon-omo', sku='OMO-500',
            barcode='6001087011234', selling_price=Decimal('1500'),
            cost_price=Decimal('1000'), track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.article, warehouse=self.warehouse,
            quantity=Decimal('12'),
        )

    def _chercher(self, code, **params):
        return self.client.get(
            URL, {'barcode': code, 'warehouse': str(self.warehouse.id), **params},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def test_la_reponse_porte_le_STOCK_DU_DEPOT(self):
        reponse = self._chercher('6001087011234')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(reponse.data['id'], str(self.article.id))
        self.assertEqual(Decimal(str(reponse.data['stock_quantity'])), Decimal('12'))

    def test_le_SKU_exact_est_un_repli(self):
        reponse = self._chercher('OMO-500')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(reponse.data['id'], str(self.article.id))

    def test_un_article_inactif_n_est_jamais_rendu(self):
        self.article.is_active = False
        self.article.save()
        reponse = self._chercher('6001087011234')
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND, reponse.data)
        self.assertEqual(reponse.data['code'], 'produit_introuvable')

    def test_sellable_ecarte_ce_qui_ne_se_vend_pas(self):
        self.article.is_sellable = False
        self.article.save()
        self.assertEqual(self._chercher('6001087011234').status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._chercher('6001087011234', sellable='true').status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_full_catalog_trouve_un_article_sans_rayon_dans_le_depot(self):
        autre = Warehouse.objects.create(
            organization=self.org, branch=self.branch, name='Réserve', code='RES',
        )
        nouveau = Product.objects.create(
            organization=self.org, name='Lait Nido', slug='lait-nido', sku='NIDO-1',
            barcode='7613035000001', selling_price=Decimal('9000'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=nouveau, warehouse=autre, quantity=Decimal('5'),
        )
        # Sans `full_catalog`, le gérant ne voit que les articles de SES dépôts.
        self.assertEqual(self._chercher('7613035000001').status_code, status.HTTP_404_NOT_FOUND)
        reponse = self._chercher('7613035000001', full_catalog='true')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        # Le stock lu est celui de la CAISSE : aucun rayon ici.
        self.assertEqual(Decimal(str(reponse.data['stock_quantity'])), Decimal('0'))

    def test_un_code_inconnu_rend_404_avec_son_code(self):
        reponse = self._chercher('0000000000000')
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND, reponse.data)
        self.assertEqual(reponse.data['code'], 'produit_introuvable')
