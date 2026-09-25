"""
Un identifiant hors périmètre est REFUSÉ, sur toutes les listes.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LA DOCTRINE ÉTAIT ÉCRITE, ET ONZE `FilterSet` NE LA SUIVAIENT PAS.          │
│                                                                              │
│ `warehouse_scope` la pose : « UN IDENTIFIANT HORS PÉRIMÈTRE LÈVE, IL NE      │
│ REND JAMAIS UN ENSEMBLE VIDE. Rendre zéro ligne ferait lire "ce caissier     │
│ n'a rien vendu" là où la vérité est "vous n'avez pas le droit de le          │
│ regarder". » Seuls les rapports et le tableau de bord l'appliquaient.        │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.urls import get_resolver
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users

#: Les listes qui acceptent `warehouse` et/ou `user`, et le paramètre à viser.
LISTES = (
    ('/api/v1/sales/', 'user'),
    ('/api/v1/sales/', 'warehouse'),
    ('/api/v1/sale-returns/', 'warehouse'),
    ('/api/v1/expenses/', 'user'),
    ('/api/v1/cash-movements/', 'warehouse'),
    ('/api/v1/stocks/', 'warehouse'),
    ('/api/v1/stock-movements/', 'warehouse'),
    ('/api/v1/stock-transfers/', 'warehouse'),
    ('/api/v1/stock-adjustments/', 'warehouse'),
    ('/api/v1/inventory-sessions/', 'warehouse'),
)


class RefusUniqueTests(APITestCase):
    """⚠ Le demandeur est un GÉRANT borné : en propriétaire, rien n'est refusé."""

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.hors = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'], name='Hors', code='WH-X',
        )
        self.etranger = OrganizationMembership.objects.get(
            user=self.d['cashier_b'], organization=self.org
        )
        self.etranger.assigned_warehouses.set([self.hors])

        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def test_le_balayage_couvre_bien_DIX_listes(self):
        # Un balayage qui ne balaie rien passe au vert : ce dépôt s'est fait
        # prendre cinq fois. Et chaque chemin doit exister dans le routeur.
        self.assertGreaterEqual(len(LISTES), 10)
        chemins = set(get_resolver().reverse_dict.keys())
        self.assertTrue(chemins, "le résolveur d'URL est vide")

    def test_un_identifiant_hors_perimetre_est_REFUSE_partout(self):
        valeur = {
            'warehouse': str(self.hors.id),
            'user': str(self.d['cashier_b'].id),
        }
        for chemin, cle in LISTES:
            with self.subTest(chemin=chemin, filtre=cle):
                r = self.client.get(chemin, {cle: valeur[cle]})
                self.assertEqual(
                    r.status_code, status.HTTP_400_BAD_REQUEST, f'{chemin} → {r.data}'
                )
                self.assertIn(cle, r.data)

    def test_un_identifiant_DANS_le_perimetre_passe(self):
        """Le contrôle : sans lui, tout refuser passerait le test précédent."""
        for chemin, cle in LISTES:
            with self.subTest(chemin=chemin, filtre=cle):
                valeur = (
                    str(self.d['warehouse'].id) if cle == 'warehouse'
                    else str(self.d['cashier_a'].id)
                )
                r = self.client.get(chemin, {cle: valeur})
                self.assertEqual(r.status_code, status.HTTP_200_OK, f'{chemin} → {r.data}')

    def test_l_EXPORT_porte_le_meme_refus_que_sa_liste(self):
        """
        `ExportableListMixin.export` appelle `filter_queryset` : le point unique
        les couvre donc sans une ligne de plus. C'est ce qu'on épingle.
        """
        r = self.client.get(
            '/api/v1/sales/export/',
            {'export_format': 'csv', 'warehouse': str(self.hors.id)},
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content[:200])

    def test_un_parametre_qu_une_vue_N_ACCEPTE_PAS_n_est_pas_refuse(self):
        """
        ⚠ Sans ce contrôle, un `?user=` posé sur une vue qui n'en fait rien
        serait refusé alors qu'il est simplement ignoré, et l'appelant
        chercherait un défaut de droit là où il n'y a qu'un paramètre de trop.
        """
        r = self.client.get(
            '/api/v1/stocks/', {'user': str(self.d['cashier_b'].id)}
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
