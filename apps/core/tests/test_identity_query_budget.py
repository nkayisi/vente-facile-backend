"""
Budget de requêtes SQL pour la résolution de l'identité tenant.

Ce que ce fichier protège : le membership de l'utilisateur courant doit être
lu **une seule fois par requête HTTP**, quel que soit le nombre de chemins qui
en ont besoin.

Il était auparavant résolu par trois routes qui ne partageaient aucun cache :
`api_permissions._get_membership` (mémoïsé), `warehouse_scope.
get_membership_for_request` (37 sites d'appel, aucune mémoïsation) et
`TenantViewSetMixin.get_organization` (rappelé à chaque action). Une simple
liste payait ainsi cinq résolutions de la même identité, et
``reports/summary`` une trentaine.

Les seuils ci-dessous sont volontairement exprimés en maximum : ils n'ont pas
vocation à figer un total exact, qui bouge avec le contenu des serializers,
mais à faire échouer bruyamment le retour d'une résolution non partagée.
"""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from apps.organizations.models import OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users


def _membership_queries(captured):
    """Les requêtes qui lisent la table d'appartenance."""
    return [
        q['sql'] for q in captured.captured_queries
        if 'organization_memberships' in q['sql'] and q['sql'].lstrip().upper().startswith('SELECT')
    ]


class MembershipResolvedOnceTests(APITestCase):
    """Une requête HTTP, une lecture d'appartenance."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.headers = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _get(self, url, user):
        self.client.force_authenticate(user=user)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, **self.headers)
        return response, captured

    def test_sales_list_reads_membership_once(self):
        """
        `GET /sales/` traverse IsTenantMember, HasActiveSubscription,
        HasPermission, TenantViewSetMixin.get_queryset et
        WarehouseScopedQuerysetMixin. Tous veulent la même appartenance.
        """
        response, captured = self._get('/api/v1/sales/', self.manager)

        self.assertEqual(response.status_code, 200, response.content[:400])
        lectures = _membership_queries(captured)
        self.assertLessEqual(
            len(lectures), 1,
            f'{len(lectures)} lectures de l\'appartenance au lieu d\'une seule :\n'
            + '\n'.join(lectures),
        )

    def test_products_list_reads_membership_once(self):
        response, captured = self._get('/api/v1/products/', self.manager)

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertLessEqual(len(_membership_queries(captured)), 1)

    def test_reports_summary_reads_membership_once(self):
        """
        Le plus gourmand : `_scope_sales` appelait `_is_cashier` ET
        `_accessible_warehouse_ids`, chacun résolvant l'appartenance à neuf, et
        six blocs de statistiques scopaient à leur tour.
        """
        response, captured = self._get('/api/v1/reports/statistics/summary/', self.manager)

        self.assertEqual(response.status_code, 200, response.content[:400])
        lectures = _membership_queries(captured)
        self.assertLessEqual(
            len(lectures), 1,
            f'{len(lectures)} lectures de l\'appartenance sur reports/summary.',
        )

    def test_warehouse_scope_is_read_once_per_request(self):
        """
        La liste d'entrepôts accessibles est lue par chaque bloc scopé. Elle est
        mémoïsée sur l'instance de membership, elle-même mémoïsée sur la
        requête : une lecture suffit. Un caissier, donc bien scopé.

        La table pivot s'appelle ``membership_warehouses`` et non
        ``assigned_warehouses`` (voir le ``db_table`` de `MembershipWarehouse`) :
        chercher le nom du champ dans le SQL ne trouverait jamais rien, et le
        test passerait sans rien mesurer.
        """
        response, captured = self._get(
            '/api/v1/reports/statistics/summary/', self.cashier_a,
        )

        self.assertEqual(response.status_code, 200, response.content[:400])
        lectures = [
            q['sql'] for q in captured.captured_queries
            if 'membership_warehouses' in q['sql']
        ]
        self.assertLessEqual(
            len(lectures), 1,
            f'{len(lectures)} lectures du périmètre entrepôt au lieu d\'une seule.',
        )

    def test_summary_query_budget_stays_bounded(self):
        """
        Garde-fou global sur l'endpoint le plus lourd du backend.

        Mesuré sur une base vide : 55 requêtes pour un gérant avant correction
        (dont 14 lectures d'appartenance et 21 du périmètre entrepôt), 22 après.
        Le seuil laisse de la marge au contenu du rapport ; il se déclenche si
        une résolution d'identité non partagée revient.
        """
        response, captured = self._get(
            '/api/v1/reports/statistics/summary/', self.manager,
        )

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertLess(
            len(captured.captured_queries), 35,
            f'{len(captured.captured_queries)} requêtes sur un rapport de '
            'synthèse à base vide : une résolution redondante est revenue.',
        )


class TenantIsolationStillEnforcedTests(APITestCase):
    """
    Le partage du cache ne doit pas relâcher l'isolation.

    `get_organization` rendait un 404 quand l'en-tête désignait une
    organisation dont l'utilisateur n'est pas membre. Rendre ``None`` à la
    place ferait sauter le filtre ``organization`` de `get_queryset`, donc
    ouvrirait la lecture aux données des autres organisations : c'est la
    régression que ce test interdit.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

    def test_header_of_a_foreign_organization_is_refused(self):
        from apps.organizations.models import Organization

        etrangere = Organization.objects.create(name='Autre', slug='autre')
        self.client.force_authenticate(user=self.manager)

        response = self.client.get(
            '/api/v1/sales/',
            HTTP_X_ORGANIZATION_ID=str(etrangere.id),
        )

        self.assertIn(response.status_code, (403, 404), response.content[:300])

    def test_inactive_membership_is_refused(self):
        OrganizationMembership.objects.filter(
            user=self.manager, organization=self.org,
        ).update(is_active=False)
        self.client.force_authenticate(user=self.manager)

        response = self.client.get(
            '/api/v1/sales/',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

        self.assertIn(response.status_code, (403, 404), response.content[:300])
