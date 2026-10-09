"""
Un caissier lit SA journée, et rien d'autre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE CLOISONNEMENT LUI AVAIT TOUT RETIRÉ, Y COMPRIS SA PROPRE LECTURE.        │
│                                                                              │
│ Les huit onglets de Rapports sont gardés par `reports.view`, accordé au seul │
│ propriétaire et au gérant : un caissier qui ouvrait la rubrique recevait     │
│ 403, donc il n'avait AUCUN moyen de savoir ce qu'il avait vendu dans la      │
│ journée. C'est la contrepartie de T4 du retour de test : on ne lui retire    │
│ pas la lecture de son périmètre en lui retirant celle des autres.            │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ `my-activity` N'ACCEPTE AUCUNE CIBLE. Le périmètre n'est donc pas une règle à
tenir, c'est la FORME de l'endpoint : il n'y a aucun paramètre par lequel se
tromper de personne. Les tests l'éprouvent dans les deux sens.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import Expense, ExpenseCategory
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users


class MaJourneeTests(APITestCase):
    URL = '/api/v1/reports/statistics/my-activity/'
    URL_AUTRUI = '/api/v1/reports/statistics/user_activity/'

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh = self.d['warehouse']

        # Deux caissiers, deux journées : c'est l'écart entre les deux que
        # l'endpoint doit rendre, pas leur somme.
        self.vente_a = self._vente(self.d['cashier_a'], 'VT-A-1', '500')
        self.vente_b = self._vente(self.d['cashier_b'], 'VT-B-1', '900')

        categorie = ExpenseCategory.objects.create(
            organization=self.org, name='Carburant',
        )
        Expense.objects.create(
            organization=self.org, category=categorie, warehouse=self.wh,
            created_by=self.d['cashier_a'], reference='DEP-A-1',
            description='Essence', amount=Decimal('40'),
            currency='CDF', exchange_rate=Decimal('1'),
            expense_date=self.vente_a.sale_date.date(),
        )

    def _vente(self, vendeur, reference, total):
        return Sale.objects.create(
            organization=self.org, warehouse=self.wh, sold_by=vendeur,
            reference=reference, status=Sale.Status.COMPLETED,
            subtotal=Decimal(total), total=Decimal(total),
            amount_paid=Decimal(total), currency='CDF', exchange_rate=Decimal('1'),
        )

    def _appel(self, qui, url=None, **params):
        self.client.force_authenticate(user=qui)
        return self.client.get(
            url or self.URL,
            {'period': 'last_30_days', **params},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    # --- Le droit ------------------------------------------------------------

    def test_un_caissier_lit_sa_journee(self):
        """Le défaut lui-même : avant, aucune lecture ne lui était ouverte."""
        r = self._appel(self.d['cashier_a'])
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

    def test_les_quatre_roles_y_ont_acces(self):
        for cle in ('owner', 'manager', 'cashier_a', 'cashier_b'):
            with self.subTest(role=cle):
                r = self._appel(self.d[cle])
                self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_le_contrat_de_user_activity_NE_BOUGE_PAS(self):
        """
        L'action d'origine garde son droit : c'est elle qui sert à regarder
        l'activité d'un COLLÈGUE, et un caissier n'a pas à la lire. La relâcher
        aurait mêlé deux droits dans une seule action.
        """
        r = self._appel(
            self.d['cashier_a'], url=self.URL_AUTRUI,
            user=str(self.d['cashier_b'].id),
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    # --- Le périmètre --------------------------------------------------------

    def test_elle_rend_SES_ventes_et_pas_celles_du_collegue(self):
        r = self._appel(self.d['cashier_a'])
        self.assertEqual(r.data['sales']['count'], 1)
        self.assertEqual(Decimal(r.data['sales']['total']), Decimal('500.00'))

        autre = self._appel(self.d['cashier_b'])
        self.assertEqual(Decimal(autre.data['sales']['total']), Decimal('900.00'))

    def test_un_parametre_user_est_SANS_EFFET(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ LE CONTRÔLE QUI COMPTE : aucune cible ne se glisse par la requête.   │
        │                                                                      │
        │ Sans lui, rien ne distinguerait « l'endpoint ignore le paramètre »   │
        │ de « l'endpoint l'honore » sur un jeu d'essai où les deux donnent le │
        │ même nombre.                                                         │
        └──────────────────────────────────────────────────────────────────────┘
        """
        r = self._appel(self.d['cashier_a'], user=str(self.d['cashier_b'].id))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data['user']['id'], str(self.d['cashier_a'].id))
        self.assertEqual(Decimal(r.data['sales']['total']), Decimal('500.00'))

    def test_elle_porte_SON_identite(self):
        r = self._appel(self.d['cashier_a'])
        self.assertEqual(r.data['user']['id'], str(self.d['cashier_a'].id))
        self.assertEqual(r.data['user']['role'], 'cashier')

    # --- Le contenu ----------------------------------------------------------

    def test_elle_porte_ce_qu_il_a_vendu_encaisse_et_depense(self):
        """Les trois que le retour de test nomme, et rien de plus."""
        r = self._appel(self.d['cashier_a'])
        self.assertIn('sales', r.data)
        self.assertIn('expenses', r.data)
        self.assertIn('cash', r.data)

    def test_ses_depenses_sont_les_siennes(self):
        r = self._appel(self.d['cashier_a'])
        self.assertEqual(r.data['expenses']['count'], 1)
        self.assertEqual(Decimal(r.data['expenses']['total']), Decimal('40.00'))

        # Le collègue n'en a aucune : un compteur qui rendrait 1 ici signerait
        # une somme faite sur toute l'organisation.
        autre = self._appel(self.d['cashier_b'])
        self.assertEqual(autre.data['expenses']['count'], 0)

    def test_les_montants_sont_quantifies(self):
        """
        Un montant converti est le produit d'une somme par un taux à douze
        décimales. `user_activity` l'a déjà payé : la dépense d'un vendeur y
        sortait « 1.5217391315 ».
        """
        r = self._appel(self.d['cashier_a'])
        for valeur in (r.data['sales']['total'], r.data['expenses']['total']):
            _, _, decimales = str(valeur).partition('.')
            self.assertLessEqual(len(decimales), 2, valeur)
