"""
Le nombre de requêtes d'un détail de vente ne dépend pas du nombre d'articles.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CE TEST DÉFEND : DEUX REQUÊTES PAR PRODUIT, EN SILENCE.              │
│                                                                              │
│ `SaleItemSerializer` lit `product.unit.name` et `product.packaging_unit.name`│
│ pour écrire « 3 casiers + 7 bouteilles ». Le préchargement s'arrêtait à      │
│ `items__product` : chaque produit distinct d'une vente coûtait donc deux     │
│ requêtes de plus, à chaque ouverture de fiche.                                │
│                                                                              │
│ Rien ne le signalait : la réponse était juste, seulement lente, et une vente │
│ de démonstration à deux lignes ne le montre pas. C'est sur une facture de    │
│ trente articles que la fiche met une seconde à s'ouvrir.                      │
└──────────────────────────────────────────────────────────────────────────────┘

On mesure l'INVARIANT (le compte ne bouge pas avec le nombre d'articles) plutôt
qu'un nombre absolu : figer « 22 requêtes » ferait échouer le test au premier
champ ajouté, et on le relâcherait sans réfléchir. C'est le motif de
`apps/core/tests/test_identity_query_budget.py`.
"""
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.products.models import Product, Unit
from apps.sales.models import Sale, SaleItem
from apps.sales.tests._helpers import make_org_with_users


class BudgetDeSerialisationTests(APITestCase):
    """
    Rôle GÉRANT et non propriétaire : `_scope_sales` sort en amont pour un
    propriétaire, et la moitié du code de périmètre ne serait pas exécutée.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)

        self.piece = Unit.objects.create(
            organization=self.org, name='piece', symbol='pc'
        )
        self.carton = Unit.objects.create(
            organization=self.org, name='carton', symbol='ct'
        )

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _vente_a(self, nb_articles):
        """Une vente portant `nb_articles` produits DISTINCTS."""
        vente = Sale.objects.create(
            organization=self.org, reference=f'VTE-{nb_articles:03d}',
            warehouse=self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=Decimal('0'), total=Decimal('0'), amount_paid=Decimal('0'),
            sold_by=self.manager, sale_date=timezone.now(),
        )
        for i in range(nb_articles):
            produit = Product.objects.create(
                organization=self.org, name=f'Article {i:02d}',
                slug=f'art-{nb_articles}-{i}', sku=f'A{nb_articles}-{i}',
                unit=self.piece, packaging_unit=self.carton,
                selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
                units_per_package=12,
                cost_price=Decimal('600.00'), selling_price=Decimal('1000.00'),
                is_taxable=False, track_inventory=True, is_active=True,
            )
            SaleItem.objects.create(
                organization=self.org, sale=vente, product=produit,
                quantity=Decimal('1'), unit_price=Decimal('1000.00'),
                cost_price=Decimal('600.00'),
                subtotal=Decimal('1000.00'), total=Decimal('1000.00'),
            )
        return vente

    @override_settings(DEBUG=False)
    def test_le_detail_d_une_vente_ne_paie_PAS_par_article(self):
        petite = self._vente_a(1)
        grande = self._vente_a(12)

        # Le premier appel amorce les caches d'identité et d'appartenance : sans
        # lui, on comparerait une requête froide à une chaude et l'écart
        # mesurerait le cache, pas le préchargement.
        self.client.get(f'/api/v1/sales/{petite.id}/', **self._headers)

        peu = self._compte(petite)
        beaucoup = self._compte(grande)

        self.assertEqual(
            peu,
            beaucoup,
            "Le détail d'une vente coûte des requêtes PAR ARTICLE : il manque "
            "un `prefetch_related`. Vérifier `items__product__unit` et "
            "`items__product__packaging_unit` dans `SaleViewSet.get_queryset`.",
        )

    def _compte(self, vente) -> int:
        """Nombre de requêtes pour rendre le détail d'une vente."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as capture:
            reponse = self.client.get(
                f'/api/v1/sales/{vente.id}/', **self._headers
            )
            self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        return len(capture)

    @override_settings(DEBUG=False)
    def test_la_lecture_en_contenants_est_bien_RENDUE(self):
        """
        Sans cette vérification, le préchargement pourrait être retiré sans que
        rien ne le signale : le test ci-dessus passerait aussi si le serializer
        cessait de lire les unités.
        """
        vente = self._vente_a(1)
        reponse = self.client.get(f'/api/v1/sales/{vente.id}/', **self._headers)
        ligne = reponse.data['items'][0]
        self.assertIn('quantity_display', ligne)
        self.assertTrue(str(ligne['quantity_display']).strip())



class BudgetDExportTests(BudgetDeSerialisationTests):
    """
    L'export des ventes lit EXACTEMENT ce que la liste lit.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ IL TOMBAIT DANS LA BRANCHE DÉTAIL.                                      │
    │                                                                          │
    │ `get_queryset` ne connaissait que `list` et « tout le reste » : l'action │
    │ `export` héritait donc du préchargement complet du DÉTAIL - produit,     │
    │ unité, unité de conditionnement, variante, règlements - alors que        │
    │ `build_sales_report` ne lit que le client, le vendeur et un nombre       │
    │ d'articles. Sur un périmètre volontairement NON PAGINÉ, c'est tout le    │
    │ catalogue vendu qui descendait pour rien, et `iterator(chunk_size=500)`  │
    │ le refaisait à chaque tranche.                                           │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ Comparer l'export à la LISTE ne mesure rien : l'export paie en plus la
    devise et l'identité de l'émetteur qu'il imprime en tête, la liste paie en
    plus le `COUNT` de sa pagination. Deux écarts légitimes qui se croisent, et
    c'est la première version de ce test qui s'y est prise.

    Ce qui se mesure vraiment, c'est que le coût de l'export ne DÉPEND PAS de
    ce qu'il ne lit pas : Django saute les préchargements descendants quand
    l'intermédiaire est vide, si bien qu'une vente PORTANT des articles coûtait
    quatre requêtes de plus qu'une vente nue - sur un document où le détail des
    lignes n'apparaît jamais.
    """

    def _vente_nue(self, reference):
        """Une vente sans aucun article : rien à précharger."""
        return Sale.objects.create(
            organization=self.org, reference=reference,
            warehouse=self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=Decimal('0'), total=Decimal('0'), amount_paid=Decimal('0'),
            sold_by=self.manager, sale_date=timezone.now(),
        )

    @override_settings(DEBUG=False)
    def test_l_export_ne_precharge_PAS_ce_qu_il_ne_lit_pas(self):
        self._vente_nue('VTE-NUE-1')
        # Amorce des caches d'identité : sans elle on comparerait une requête
        # froide à une chaude, et l'écart mesurerait le cache.
        self._exporte()

        sans_article = self._exporte()
        self._vente_a(3)
        avec_articles = self._exporte()

        self.assertEqual(
            sans_article,
            avec_articles,
            f"L'export coûte {avec_articles} requêtes dès qu'une vente porte "
            f"des articles, contre {sans_article} sans : il est retombé dans "
            "la branche DÉTAIL de `SaleViewSet.get_queryset` et précharge "
            "items/produits/unités/variantes, que le document ne lit jamais.",
        )

    @override_settings(DEBUG=False)
    def test_l_export_ne_paie_PAS_par_VENTE(self):
        """
        Le nombre d'articles est ANNOTÉ, il ne se compte pas ligne à ligne.

        Retirer le préchargement sans poser l'annotation ferait retomber
        `build_sales_report` sur `sale.items.count()`, soit une requête par
        vente - un N+1 sur un périmètre non paginé, donc sur tout l'historique.
        """
        self._vente_a(1)
        self._exporte()
        une = self._exporte()

        for i in range(2, 7):
            self._vente_a(i)
        six = self._exporte()

        self.assertEqual(
            une,
            six,
            "L'export coûte des requêtes PAR VENTE : l'annotation "
            "`_items_count` manque, et `build_sales_report` retombe sur "
            "`sale.items.count()`.",
        )

    def _exporte(self) -> int:
        """Nombre de requêtes pour produire le document, en CSV."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as capture:
            reponse = self.client.get(
                '/api/v1/sales/export/?export_format=csv', **self._headers
            )
            self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        return len(capture)
