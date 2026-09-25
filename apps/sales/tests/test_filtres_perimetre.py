"""
Les filtres volontaires des listes de vente, et le périmètre qu'ils ne percent pas.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Quotation, Sale
from apps.sales.tests._helpers import make_org_with_users


def _vente(org, warehouse, vendeur, montant, reference):
    return Sale.objects.create(
        organization=org, warehouse=warehouse, sold_by=vendeur,
        reference=reference, status=Sale.Status.COMPLETED,
        subtotal=Decimal(montant), total=Decimal(montant),
        amount_paid=Decimal(montant), currency='CDF', exchange_rate=Decimal('1'),
    )


class FiltresDesVentesTests(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_a = self.d['warehouse']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt B', code='WH-B',
        )
        OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        ).assigned_warehouses.add(self.wh_b)

        _vente(self.org, self.wh_a, self.d['cashier_a'], '100', 'VT-A-1')
        _vente(self.org, self.wh_b, self.d['cashier_b'], '500', 'VT-B-1')
        self.url = '/api/v1/sales/'

    def _refs(self, qui, **params):
        self.client.force_authenticate(user=qui)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        reponse = self.client.get(self.url, params)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        lignes = reponse.data.get('results', reponse.data)
        return {ligne['reference'] for ligne in lignes}

    def test_le_gerant_filtre_par_entrepot(self):
        self.assertEqual(self._refs(self.d['manager']), {'VT-A-1', 'VT-B-1'})
        self.assertEqual(
            self._refs(self.d['manager'], warehouse=str(self.wh_b.id)),
            {'VT-B-1'},
        )

    def test_le_gerant_filtre_par_vendeur(self):
        self.assertEqual(
            self._refs(self.d['manager'], user=str(self.d['cashier_a'].id)),
            {'VT-A-1'},
        )

    def test_viser_un_collegue_est_REFUSE_et_non_rendu_vide(self):
        """
        ┌──────────────────────────────────────────────────────────────────┐
        │ CE TEST FIGEAIT L'INVERSE, ET IL A ÉTÉ RETOURNÉ.                 │
        │                                                                  │
        │ Il exigeait une liste VIDE. C'était correct - `get_queryset`      │
        │ borne déjà un caissier à ses propres ventes, donc l'intersection  │
        │ est bien vide - mais ce n'est pas ce qu'il faut MONTRER : la      │
        │ doctrine de `warehouse_scope` dit qu'un identifiant hors          │
        │ périmètre LÈVE, parce que zéro ligne se lit « ce caissier n'a     │
        │ rien vendu » là où la vérité est « vous n'avez pas le droit de le │
        │ regarder ». Les rapports et le tableau de bord la suivaient ; les │
        │ onze listes, non.                                                 │
        └──────────────────────────────────────────────────────────────────┘
        """
        self.client.force_authenticate(user=self.d['cashier_a'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        reponse = self.client.get(self.url, {'user': str(self.d['cashier_b'].id)})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('user', reponse.data)

    def test_et_son_propre_perimetre_reste_intact(self):
        """Le contrôle : sans lui, tout refuser passerait le test précédent."""
        self.assertEqual(self._refs(self.d['cashier_a']), {'VT-A-1'})
        self.assertEqual(
            self._refs(self.d['cashier_a'], user=str(self.d['cashier_a'].id)),
            {'VT-A-1'},
        )


class PerimetreDesDevisTests(APITestCase):
    """
    `QuotationViewSet` n'avait AUCUN périmètre de rôle : un caissier y lisait
    les devis de toute l'organisation, quand le même caissier ne voit que ses
    propres ventes. Deux écrans voisins disaient deux choses du même périmètre.
    """

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        for user, ref in [
            (self.d['cashier_a'], 'DV-A-1'),
            (self.d['cashier_b'], 'DV-B-1'),
        ]:
            Quotation.objects.create(
                organization=self.org, reference=ref, created_by=user,
                status=Quotation.Status.DRAFT,
                subtotal=Decimal('10'), total=Decimal('10'),
                valid_until='2030-01-01',
            )
        self.url = '/api/v1/quotations/'

    def _refs(self, qui, **params):
        self.client.force_authenticate(user=qui)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        reponse = self.client.get(self.url, params)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        lignes = reponse.data.get('results', reponse.data)
        return {ligne['reference'] for ligne in lignes}

    def test_un_caissier_ne_lit_que_ses_propres_devis(self):
        self.assertEqual(self._refs(self.d['cashier_a']), {'DV-A-1'})

    def test_un_gerant_les_lit_tous_et_peut_filtrer(self):
        self.assertEqual(self._refs(self.d['manager']), {'DV-A-1', 'DV-B-1'})
        self.assertEqual(
            self._refs(self.d['manager'], user=str(self.d['cashier_b'].id)),
            {'DV-B-1'},
        )
