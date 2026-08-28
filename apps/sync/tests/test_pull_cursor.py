"""
Tirage par curseurs.

Le test central de ce fichier est `test_no_row_is_ever_lost` : il crée plus de
lignes qu'une page n'en contient et vérifie qu'un client qui reboucle les
récupère TOUTES. C'est exactement ce que l'ancien tirage ratait, et personne ne
s'en apercevait : `[:1000]` sans `ORDER BY` rendait une tranche arbitraire, le
point de reprise avançait quand même, et les lignes hors tranche disparaissaient
définitivement. Une organisation de 1 200 produits en perdait 200, en silence.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, Warehouse
from apps.organizations.models import Branch, OrganizationMembership
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users
from apps.sync.pull import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    PULL_TABLES_BY_NAME,
    decode_cursor,
    encode_cursor,
)

PULL = '/api/v1/sync/pull/'


class _PullBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _page(self, table, cursor=None, limit=None, user=None):
        if user:
            self.client.force_authenticate(user=user)
        params = {'table': table}
        if cursor:
            params['cursor'] = cursor
        if limit:
            params['limit'] = limit
        return self.client.get(PULL, params, **self._headers())

    def _walk(self, table, limit=7, user=None):
        """Rejoue ce que fait le client : reboucler tant que `has_more`."""
        seen, cursor, pages = [], None, 0
        while True:
            resp = self._page(table, cursor, limit, user)
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
            seen += [row['id'] for row in resp.data['rows']]
            cursor = resp.data['next_cursor']
            pages += 1
            if not resp.data['has_more']:
                return seen, pages
            self.assertLess(pages, 500, "le tirage ne se termine pas")


class CursorEncodingTests(APITestCase):
    def test_cursor_round_trips(self):
        moment = timezone.now()
        cursor = encode_cursor(moment, 'abc-123')
        decoded = decode_cursor(cursor)
        self.assertEqual(decoded[0], moment)
        self.assertEqual(decoded[1], 'abc-123')

    def test_cursor_is_opaque(self):
        """Sa forme doit pouvoir changer sans casser un client déjà déployé."""
        cursor = encode_cursor(timezone.now(), 'abc-123')
        self.assertNotIn('abc-123', cursor)

    def test_a_corrupt_cursor_restarts_instead_of_failing(self):
        # Le pire cas est de retirer des lignes déjà connues, que le client
        # écrase à l'identique. Échouer bloquerait la synchronisation.
        self.assertIsNone(decode_cursor('nimportequoi'))
        self.assertIsNone(decode_cursor(''))
        self.assertIsNone(decode_cursor(None))


class PaginationTests(_PullBaseTest):
    def _make_products(self, count):
        # `bulk_create` court-circuite `save()` : le slug, qui y est fabriqué,
        # reste vide et viole sa contrainte d'unicité par organisation. On le
        # pose donc à la main. `updated_at` survit, lui : `auto_now` passe par
        # `pre_save`, que `bulk_create` appelle.
        Product.objects.bulk_create([
            Product(
                organization=self.org, name=f'Article {i:04d}', slug=f'article-{i:04d}',
                sku=f'SKU{i:04d}',
                selling_price=Decimal('1000.00'), cost_price=Decimal('800.00'),
            )
            for i in range(count)
        ])
        # Sans horodatage, l'ordre de pagination n'existe pas : on le vérifie
        # une fois ici plutôt que de le supposer dans chaque test.
        self.assertFalse(
            Product.objects.filter(organization=self.org, updated_at__isnull=True).exists(),
            "bulk_create n'a pas renseigné updated_at : la pagination n'a plus d'ordre",
        )

    def test_no_row_is_ever_lost(self):
        """
        Le test qui aurait attrapé le défaut d'origine.

        Plus de lignes qu'une page n'en porte : un client qui reboucle doit
        toutes les voir, une fois chacune.
        """
        self._make_products(2500)
        attendu = set(str(pk) for pk in Product.objects.filter(
            organization=self.org).values_list('id', flat=True))

        vus, pages = self._walk('products', limit=100)

        self.assertGreater(pages, 20, "la pagination ne s'est pas déclenchée")
        self.assertEqual(len(vus), len(set(vus)), "des lignes sont revenues deux fois")
        self.assertEqual(set(vus), attendu, "des lignes ont été perdues")

    def test_rows_sharing_a_timestamp_are_neither_skipped_nor_looped(self):
        """
        Le cœur du curseur composite.

        Un horodatage seul ferait soit sauter les lignes suivantes de la même
        milliseconde, soit tourner en rond dessus. Le couple `(updated_at, id)`
        les départage.
        """
        self._make_products(50)
        instant = timezone.now()
        # `update()` ne déclenche pas `auto_now` : c'est justement ce qui permet
        # d'imposer un horodatage identique à toutes les lignes.
        Product.objects.filter(organization=self.org).update(updated_at=instant)

        attendu = set(str(pk) for pk in Product.objects.filter(
            organization=self.org).values_list('id', flat=True))
        vus, _ = self._walk('products', limit=7)

        self.assertEqual(set(vus), attendu)
        self.assertEqual(len(vus), len(set(vus)))

    def test_an_interrupted_pull_resumes_where_it_stopped(self):
        self._make_products(60)

        first = self._page('products', limit=25)
        self.assertTrue(first.data['has_more'])
        deja_vus = {row['id'] for row in first.data['rows']}

        # Coupure réseau : le client garde son curseur et repart de là.
        suite = self._page('products', cursor=first.data['next_cursor'], limit=25)
        nouveaux = {row['id'] for row in suite.data['rows']}

        self.assertEqual(deja_vus & nouveaux, set(), "la reprise a rejoué des lignes")

    def test_page_size_is_bounded(self):
        self._make_products(10)
        resp = self._page('products', limit=99999)
        self.assertLessEqual(len(resp.data['rows']), MAX_PAGE_SIZE)

        resp = self._page('products', limit=0)
        self.assertGreaterEqual(len(resp.data['rows']), 1)

    def test_cursor_is_kept_when_a_page_comes_back_empty(self):
        """Sinon le tirage suivant repartirait du début de la table."""
        self._make_products(3)
        _, _ = self._walk('products', limit=100)

        resp = self._page('products', limit=100)
        final = resp.data['next_cursor']
        encore = self._page('products', cursor=final, limit=100)

        self.assertEqual(encore.data['rows'], [])
        self.assertEqual(encore.data['next_cursor'], final)


class TombstoneTests(_PullBaseTest):
    def test_deletions_travel_on_their_own_cursor(self):
        """
        Suppressions et écritures ont chacune leur pagination.

        Les mêler dans un seul curseur ferait sauter les unes ou les autres :
        une ligne modifiée puis supprimée avancerait le curseur au-delà de sa
        propre pierre tombale.
        """
        vivants = [
            Product.objects.create(
                organization=self.org, name=f'Vivant {i}', slug=f'vivant-{i}', sku=f'V{i}',
                selling_price=Decimal('100'), cost_price=Decimal('50'),
            )
            for i in range(3)
        ]
        morts = [
            Product.objects.create(
                organization=self.org, name=f'Mort {i}', slug=f'mort-{i}', sku=f'M{i}',
                selling_price=Decimal('100'), cost_price=Decimal('50'),
            )
            for i in range(2)
        ]
        for p in morts:
            p.soft_delete()

        resp = self._page('products', limit=100)

        rendus = {row['id'] for row in resp.data['rows']}
        supprimes = set(resp.data['deleted_ids'])

        self.assertEqual(rendus, {str(p.id) for p in vivants})
        self.assertEqual(supprimes, {str(p.id) for p in morts})
        # Une ligne supprimée ne doit pas figurer dans les deux listes : le
        # client la créerait puis la retirerait à chaque tirage.
        self.assertEqual(rendus & supprimes, set())

    def test_a_table_without_soft_delete_never_sends_tombstones(self):
        resp = self._page('stock_movements', limit=10)
        self.assertEqual(resp.data['deleted_ids'], [])


class ScopeTests(_PullBaseTest):
    """Un caissier ne tire pas le stock de toute l'entreprise."""

    def setUp(self):
        super().setUp()
        autre_branche = Branch.objects.create(
            organization=self.org, name='Annexe', code='ANX'
        )
        self.autre_entrepot = Warehouse.objects.create(
            organization=self.org, branch=autre_branche, name='Annexe WH', code='ANX-WH'
        )
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('100'), cost_price=Decimal('50'),
        )
        Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('10.000'),
        )
        Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.autre_entrepot, quantity=Decimal('20.000'),
        )

    def test_a_cashier_only_pulls_the_stock_of_assigned_warehouses(self):
        resp = self._page('stocks', limit=100, user=self.cashier_a)
        entrepots = {row['warehouse_id'] for row in resp.data['rows']}
        self.assertEqual(entrepots, {str(self.warehouse.id)})

    def test_an_owner_pulls_everything(self):
        """Comme sur le web : un propriétaire n'a pas de périmètre."""
        resp = self._page('stocks', limit=100, user=self.owner)
        entrepots = {row['warehouse_id'] for row in resp.data['rows']}
        self.assertEqual(
            entrepots, {str(self.warehouse.id), str(self.autre_entrepot.id)}
        )

    def test_another_organization_is_invisible(self):
        from apps.organizations.models import Organization
        autre_org = Organization.objects.create(name='Voisin', slug='voisin-pull')
        Product.objects.create(
            organization=autre_org, name='Ailleurs', slug='ailleurs', sku='X1',
            selling_price=Decimal('100'), cost_price=Decimal('50'),
        )

        resp = self._page('products', limit=100)
        noms = {row['name'] for row in resp.data['rows']}
        self.assertNotIn('Ailleurs', noms)


class PayloadTests(_PullBaseTest):
    def test_decimals_travel_as_strings(self):
        """
        Un panier en francs congolais à sept chiffres perd ses unités en
        virgule flottante. La discipline vaut des deux côtés du réseau.
        """
        Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('1250036.40'), cost_price=Decimal('999999.99'),
        )
        resp = self._page('products', limit=10)
        row = resp.data['rows'][0]

        self.assertIsInstance(row['selling_price'], str)
        self.assertEqual(row['selling_price'], '1250036.40')

    def test_foreign_keys_travel_as_identifiers(self):
        Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('100'), cost_price=Decimal('50'),
        )
        row = self._page('products', limit=10).data['rows'][0]

        self.assertIn('category_id', row)
        self.assertNotIn('category', row)

    def test_the_organization_column_never_travels(self):
        """Le client n'en connaît qu'une : la répéter sur chaque ligne est du bruit."""
        Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('100'), cost_price=Decimal('50'),
        )
        row = self._page('products', limit=10).data['rows'][0]
        self.assertNotIn('organization_id', row)

    def test_packaging_columns_are_exposed(self):
        """
        L'ancien tirage ne les exposait pas, et le serveur refusait donc tout
        produit vendu au conditionnement. C'est la fonctionnalité phare.
        """
        Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('100'), cost_price=Decimal('50'),
        )
        row_keys = set(self._page('products', limit=10).data['rows'][0])

        for champ in ('selling_mode', 'units_per_package', 'package_cost_price',
                      'wholesale_price', 'allow_auto_unpacking'):
            self.assertIn(champ, row_keys, f'{champ} manque au tirage des produits')


class ContractTests(_PullBaseTest):
    def test_an_unknown_table_is_refused_with_the_list_of_valid_ones(self):
        resp = self._page('table_inventee')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'unknown_table')
        self.assertIn('products', resp.data['available'])

    def test_the_manifest_drives_the_client(self):
        """Le client ne code pas la liste des tables en dur."""
        resp = self.client.get('/api/v1/sync/pull/manifest/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        noms = [t['name'] for t in resp.data['tables']]
        self.assertEqual(set(noms), set(PULL_TABLES_BY_NAME))
        # L'ordre porte du sens : les référentiels avant ce qui s'y rattache.
        self.assertLess(noms.index('warehouses'), noms.index('stocks'))
        self.assertLess(noms.index('products'), noms.index('sales'))

    def test_sales_carry_their_lines_and_payments(self):
        resp = self._page('sales', limit=10)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        manifest = self.client.get('/api/v1/sync/pull/manifest/', **self._headers())
        vente = next(t for t in manifest.data['tables'] if t['name'] == 'sales')

        # Chaque enfant porte sa cle d'imbrication ET son nom de table locale :
        # les confondre engendrerait une table nommee « items », sans rapport
        # avec ce qu'elle contient.
        self.assertEqual([c['name'] for c in vente['children']], ['items', 'payments'])
        self.assertEqual(
            [c['table'] for c in vente['children']], ['sale_items', 'payments']
        )
        self.assertTrue(all(c['columns'] for c in vente['children']))

    def test_membership_is_required(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(PULL, {'table': 'products'}, **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
