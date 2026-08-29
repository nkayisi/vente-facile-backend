"""
Sonde préalable : quelles tables ont du neuf.

Ce que ce fichier protège. Le tirage complet parcourait les trente et une
tables du manifeste, une requête HTTP séquentielle chacune, **même quand rien
n'avait changé** : vingt et une réponses consécutives de 250 à 300 octets
disant « rien de neuf », relevées dans le journal du backend. Sur un réseau
mobile à 300 ms de latence, c'est une dizaine de secondes d'attente pour zéro
donnée.

La sonde ramène ce cas à un aller-retour. Elle n'a le droit d'exister que si
elle ne ment JAMAIS par omission : annoncer « rien de neuf » pour une table qui
a bougé ferait disparaître silencieusement une donnée du terminal, ce qui est
bien pire que le coût qu'on cherche à éviter. C'est ce que vérifient les tests
ci-dessous, écriture ET suppression, avec leurs deux curseurs distincts.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users
from apps.sync.pull import PULL_TABLES

CHANGED = '/api/v1/sync/pull/changed/'
PULL = '/api/v1/sync/pull/'


class _ChangedBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _drain(self, table):
        """Tire une table jusqu'au bout, comme le ferait le client."""
        cursor = deleted_cursor = None
        while True:
            params = {'table': table, 'limit': 500}
            if cursor:
                params['cursor'] = cursor
            if deleted_cursor:
                params['deleted_cursor'] = deleted_cursor
            resp = self.client.get(PULL, params, **self._headers())
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
            cursor = resp.data['next_cursor']
            deleted_cursor = resp.data['next_deleted_cursor']
            if not resp.data['has_more']:
                return cursor, deleted_cursor

    def _changed(self, cursors, deleted_cursors=None):
        resp = self.client.post(
            CHANGED,
            {'cursors': cursors, 'deleted_cursors': deleted_cursors or {}},
            format='json',
            **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return resp.data['changed']

    def _make_product(self, sku):
        return Product.objects.create(
            organization=self.org, name=f'Article {sku}', slug=f'article-{sku}',
            sku=sku, selling_price=Decimal('100'), cost_price=Decimal('50'),
        )


class ChangedTablesTests(_ChangedBaseTest):

    def test_client_without_cursors_is_told_everything_changed(self):
        """Première synchronisation : rien n'est connu, tout est à tirer."""
        changed = self._changed({})

        self.assertEqual(len(changed), len(PULL_TABLES))

    def test_unreadable_cursor_falls_back_to_pulling(self):
        """
        Un curseur corrompu doit provoquer un tirage, pas un silence. C'est le
        même parti pris que `decode_cursor` : redonner des lignes déjà connues
        ne coûte que du temps, en manquer perd une donnée.

        Le produit créé ici n'est pas décoratif : c'est lui qui rend le test
        concluant. Sur une table vide, un curseur illisible produit « rien de
        neuf », et c'est correct puisqu'il n'y a effectivement rien à tirer ;
        le test passerait alors sans rien démontrer.
        """
        self._make_product('A1')

        changed = self._changed({'products': 'ceci-nest-pas-un-curseur'})

        self.assertIn('products', changed)

    def test_nothing_changed_after_a_full_pull(self):
        """Le cas nominal, et toute la raison d'être de l'endpoint."""
        self._make_product('A1')
        cursors, deleted = {}, {}
        for table in PULL_TABLES:
            c, d = self._drain(table.name)
            cursors[table.name] = c
            deleted[table.name] = d

        changed = self._changed(cursors, deleted)

        self.assertEqual(
            changed, [],
            f'{len(changed)} table(s) annoncées modifiées alors que rien '
            f'n\'a bougé depuis le tirage : {changed}',
        )

    def test_a_written_row_is_reported(self):
        self._make_product('A1')
        cursor, deleted_cursor = self._drain('products')

        self._make_product('A2')

        changed = self._changed(
            {'products': cursor}, {'products': deleted_cursor},
        )
        self.assertIn('products', changed)

    def test_an_updated_row_is_reported(self):
        produit = self._make_product('A1')
        cursor, deleted_cursor = self._drain('products')

        produit.selling_price = Decimal('120')
        produit.save()

        changed = self._changed(
            {'products': cursor}, {'products': deleted_cursor},
        )
        self.assertIn('products', changed)

    def test_a_deleted_row_is_reported(self):
        produit = self._make_product('A1')
        cursor, deleted_cursor = self._drain('products')

        produit.soft_delete()

        changed = self._changed(
            {'products': cursor}, {'products': deleted_cursor},
        )
        self.assertIn(
            'products', changed,
            'une suppression non signalée laisse la ligne visible sur le '
            'terminal, indéfiniment',
        )

    def test_a_deletion_is_seen_even_when_writes_ran_far_ahead(self):
        """
        Le piège que la première version de cette sonde contenait.

        Les écritures avancent sur ``updated_at``, les suppressions sur
        ``deleted_at``, chacune à son rythme. Sonder les pierres tombales avec
        le curseur des ÉCRITURES manque toutes celles situées entre les deux,
        dans le cas très ordinaire d'une table beaucoup écrite et peu
        supprimée. Ici : on supprime, puis on écrit beaucoup, puis on tire les
        seules écritures ; le curseur d'écriture dépasse alors largement la
        date de suppression.
        """
        condamne = self._make_product('MORT')
        cursor, deleted_cursor = self._drain('products')

        condamne.soft_delete()

        # Le curseur d'écriture part loin devant, sans que le client ne
        # récupère la pierre tombale.
        for i in range(5):
            self._make_product(f'APRES-{i}')
        cursor_ecritures, _ = self._drain('products')

        changed = self._changed(
            {'products': cursor_ecritures},
            {'products': deleted_cursor},
        )
        self.assertIn(
            'products', changed,
            'la pierre tombale a été enjambée par le curseur des écritures',
        )

    def test_another_organization_writes_do_not_wake_this_client(self):
        """L'isolation tenant vaut aussi pour la sonde."""
        from apps.organizations.models import Organization

        self._make_product('A1')
        cursors, deleted = {}, {}
        for table in PULL_TABLES:
            c, d = self._drain(table.name)
            cursors[table.name] = c
            deleted[table.name] = d

        autre = Organization.objects.create(name='Autre', slug='autre-org')
        Product.objects.create(
            organization=autre, name='Ailleurs', slug='ailleurs', sku='X1',
            selling_price=Decimal('10'), cost_price=Decimal('5'),
        )

        self.assertNotIn('products', self._changed(cursors, deleted))

    def test_malformed_body_is_refused(self):
        resp = self.client.post(
            CHANGED, {'cursors': 'pas-un-objet'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
