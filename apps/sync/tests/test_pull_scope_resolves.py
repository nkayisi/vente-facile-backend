"""
Chaque table du registre doit se tirer POUR UN MEMBRE BORNÉ, pas seulement pour
un propriétaire.

`_scope_to_warehouses` sort avant le filtre quand le rôle est `owner` : un
`warehouse_path` faux n'est alors JAMAIS évalué. Toutes les suites de tirage
existantes n'ouvrent qu'une session de propriétaire, si bien que
`sale_returns` a porté `warehouse_path='warehouse_id'` - un champ que
`SaleReturn` n'a pas - sans qu'aucun test ne bronche. En production, la même
ligne lève un `FieldError`, donc un 500, sur trois chemins à la fois :
`pull/?table=sale_returns`, la sonde `pull/changed/` et
`pull/manifest/?counts=1`. Les deux derniers sont la PREMIÈRE synchronisation
d'un terminal, qui n'aboutit donc jamais.

Le rôle employé ici est `manager` : `make_org_with_users` lui assigne l'entrepôt
principal, ce qui est exactement la condition qui déclenche le filtre.
"""
from django.core.exceptions import EmptyResultSet, FieldError
from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.tests._helpers import make_org_with_users
from apps.sync.pull import PULL_TABLES

PULL = '/api/v1/sync/pull/'
CHANGED = '/api/v1/sync/pull/changed/'
MANIFEST = '/api/v1/sync/pull/manifest/'


class PerimetreEntrepotResolvableTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)

    def _entetes(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def test_every_warehouse_path_resolves_on_its_model(self):
        """
        Le contrôle statique : un chemin d'entrepôt qui ne se résout pas est une
        faute de frappe, et elle ne se voit qu'à l'exécution.
        """
        fautes = []
        for table in PULL_TABLES:
            if table.warehouse_path is None:
                continue
            modele = table.get_model()
            lookup = 'id' if table.warehouse_path == '' else table.warehouse_path
            try:
                str(modele.objects.filter(**{f'{lookup}__in': []}).query)
            except EmptyResultSet:
                pass  # Filtre vide : le chemin est valide, la requête est nulle.
            except FieldError as erreur:
                fautes.append(f'{table.name}: {erreur}')
        self.assertEqual(fautes, [], '\n'.join(fautes))

    def test_a_scoped_member_can_pull_every_table(self):
        """
        Le contrôle de bout en bout : le tirage complet, avec le rôle qui borne.
        """
        for table in PULL_TABLES:
            with self.subTest(table=table.name):
                reponse = self.client.get(
                    PULL, {'table': table.name}, **self._entetes()
                )
                self.assertEqual(
                    reponse.status_code, status.HTTP_200_OK,
                    f'{table.name}: {reponse.status_code}',
                )

    def test_a_scoped_member_can_probe_and_read_the_manifest(self):
        """
        Ces deux appels sont la PREMIÈRE synchronisation. Une seule table fautive
        les fait échouer en entier, et le terminal n'a alors aucune donnée.
        """
        sonde = self.client.post(
            CHANGED, {'cursors': {}}, format='json', **self._entetes()
        )
        self.assertEqual(sonde.status_code, status.HTTP_200_OK, sonde.data)

        manifeste = self.client.get(MANIFEST, {'counts': '1'}, **self._entetes())
        self.assertEqual(manifeste.status_code, status.HTTP_200_OK, manifeste.data)
        noms = {t['name'] for t in manifeste.data['tables']}
        self.assertIn('sale_returns', noms)
