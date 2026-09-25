"""
Un document filtré DIT sur quoi il porte.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN PAPIER VOYAGE SANS SA BARRE DE FILTRES.                                  │
│                                                                              │
│ L'export des ventes et les huit onglets de rapports appliquaient `warehouse` │
│ et `user` sans jamais les écrire en tête : un fichier ne portant qu'un dépôt │
│ sur trois était indiscernable d'un fichier complet, et son lecteur n'avait   │
│ aucun moyen de s'en apercevoir.                                              │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Le demandeur est un GÉRANT : `reports.view` n'est accordé qu'au propriétaire
et au gérant, et un propriétaire sort en amont de tout le code de périmètre.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users


class PerimetreDansLEnTeteTests(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt Bravo', code='WH-B',
        )
        m = OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        )
        m.assigned_warehouses.add(self.wh_b)

        Sale.objects.create(
            organization=self.org, warehouse=self.wh_b, sold_by=self.d['cashier_b'],
            reference='VT-B-1', status=Sale.Status.COMPLETED,
            subtotal=Decimal('500'), total=Decimal('500'),
            amount_paid=Decimal('500'), currency='CDF', exchange_rate=Decimal('1'),
        )

        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def _csv(self, url, **params):
        reponse = self.client.get(url, {'export_format': 'csv', **params})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.content[:300])
        return b''.join(reponse.streaming_content).decode('utf-8-sig') \
            if reponse.streaming else reponse.content.decode('utf-8-sig')

    # -- l'export des ventes -------------------------------------------------

    def test_l_export_des_ventes_NOMME_l_entrepot_filtre(self):
        texte = self._csv('/api/v1/sales/export/', warehouse=str(self.wh_b.id))
        self.assertIn('Entrepôt', texte)
        self.assertIn('Dépôt Bravo', texte, "le document ne dit pas sur quoi il porte")

    def test_l_export_des_ventes_NOMME_l_utilisateur_filtre(self):
        texte = self._csv('/api/v1/sales/export/', user=str(self.d['cashier_b'].id))
        self.assertIn('Utilisateur', texte)
        self.assertIn('Cash B', texte)

    def test_sans_filtre_les_deux_lignes_disent_TOUS(self):
        """
        Le défaut s'écrit même en l'absence de filtre : un lecteur doit pouvoir
        distinguer « pas de filtre » de « filtre oublié dans l'en-tête ».
        """
        texte = self._csv('/api/v1/sales/export/')
        self.assertIn('Entrepôt', texte)
        self.assertIn('Utilisateur', texte)
        self.assertNotIn('Dépôt Bravo', texte)

    # -- les onglets de rapports ---------------------------------------------

    def test_un_onglet_de_rapport_NOMME_son_entrepot(self):
        texte = self._csv(
            '/api/v1/reports/statistics/export/',
            tab='overview', period='last_30_days', warehouse=str(self.wh_b.id),
        )
        self.assertIn('Entrepôt', texte)
        self.assertIn('Dépôt Bravo', texte)

    # -- les bords -----------------------------------------------------------

    def test_un_identifiant_illisible_n_interrompt_pas_le_document(self):
        """
        Le queryset l'a déjà écarté ; faire échouer un export pour un libellé
        manquant serait disproportionné. On écrit « inconnu », et on continue.
        """
        from apps.core.report_params import perimeter_filters

        lignes = perimeter_filters({'warehouse': 'pas-un-uuid'}, self.org)
        self.assertEqual(dict(lignes)['Entrepôt'], 'inconnu')
