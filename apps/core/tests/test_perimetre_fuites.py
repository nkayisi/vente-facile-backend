"""
Les deux endroits où une donnée sortait de son périmètre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LA COMPOSITION PROTÈGE LES CHIFFRES, PAS TOUT LE RESTE.                     │
│                                                                              │
│ Le chantier « filtres Entrepôt / Utilisateur » s'appuie sur un principe :    │
│ le filtre volontaire est TOUJOURS superposé au périmètre du rôle, donc il    │
│ ne peut rien montrer de neuf. C'est vrai des agrégats. Ce fichier épingle    │
│ les deux endroits où ce n'était PAS vrai : une action qui réimplémentait     │
│ son propre périmètre, et un bloc d'identité rendu hors de toute borne.       │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Le demandeur est un rôle BORNÉ, jamais `owner` : un propriétaire sort en amont
de toute la logique, et ce fichier serait vert sans rien exécuter.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import Expense, ExpenseCategory
from apps.organizations.models import OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users


def _depense(org, cat, auteur, entrepot, ref, montant='10.00'):
    return Expense.objects.create(
        organization=org, reference=ref, category=cat,
        description=f"Dépense {ref}", amount=Decimal(montant),
        expense_date=timezone.now().date(), warehouse=entrepot,
        created_by=auteur, status='approved',
    )


class StatsDesDepensesTests(APITestCase):
    """`GET /expenses/stats/` agrégeait ce que `GET /expenses/` refuse."""

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        cat = ExpenseCategory.objects.create(
            organization=self.org, name='Divers', code='DIV'
        )
        self.mienne = _depense(
            self.org, cat, self.d['cashier_a'], self.d['warehouse'], 'E-MOI', '10.00'
        )
        self.du_collegue = _depense(
            self.org, cat, self.d['cashier_b'], self.d['warehouse'], 'E-LUI', '500.00'
        )
        # Une dépense d'établissement : la liste la réserve au propriétaire.
        self.etablissement = _depense(
            self.org, cat, self.d['manager'], None, 'E-ORG', '9000.00'
        )

    def _stats(self, qui):
        self.client.force_authenticate(user=qui)
        r = self.client.get(
            '/api/v1/expenses/stats/', HTTP_X_ORGANIZATION_ID=str(self.org.id)
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content[:300])
        return r.data

    def _liste(self, qui):
        self.client.force_authenticate(user=qui)
        r = self.client.get(
            '/api/v1/expenses/', HTTP_X_ORGANIZATION_ID=str(self.org.id)
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content[:300])
        return {e['reference'] for e in r.data['results']}

    def test_un_caissier_n_agrege_QUE_ses_propres_depenses(self):
        """
        Il a `cashbook.view`, donc l'action lui est ouverte. Sa liste ne lui
        rend qu'une dépense ; son total en rendait trois.
        """
        self.assertEqual(self._liste(self.d['cashier_a']), {'E-MOI'})
        self.assertEqual(self._stats(self.d['cashier_a'])['count'], 1)

    def test_un_gerant_n_agrege_pas_les_depenses_d_ETABLISSEMENT(self):
        """
        `get_queryset` les réserve au propriétaire (`include_null_warehouse`
        n'est pas passé, donc faux). `stats` les tolérait.
        """
        self.assertNotIn('E-ORG', self._liste(self.d['manager']))
        self.assertEqual(self._stats(self.d['manager'])['count'], 2)

    def test_le_total_agrege_EXACTEMENT_ce_que_la_liste_montre(self):
        """L'invariant qui porte tout : deux chemins, un seul périmètre."""
        for role in ('cashier_a', 'manager', 'owner'):
            with self.subTest(role=role):
                self.assertEqual(
                    self._stats(self.d[role])['count'], len(self._liste(self.d[role]))
                )


class ActiviteDUnMembreDesactiveTests(APITestCase):
    """`user_activity` rendait le nom, l'e-mail et le rôle d'un désactivé."""

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.parti = OrganizationMembership.objects.get(
            user=self.d['cashier_b'], organization=self.org
        )
        self.parti.is_active = False
        self.parti.save(update_fields=['is_active'])

        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def _activite(self, uid):
        return self.client.get(
            '/api/v1/reports/statistics/user_activity/',
            {'period': 'last_30_days', 'user': str(uid)},
        )

    def test_un_membre_DESACTIVE_est_introuvable(self):
        r = self._activite(self.d['cashier_b'].id)
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND, r.data)

    def test_son_e_mail_ne_part_PAS(self):
        """
        C'est ce qui fuyait : les chiffres étaient bornés par la composition,
        le bloc `user` ne l'était par rien.
        """
        corps = str(self._activite(self.d['cashier_b'].id).data)
        self.assertNotIn(self.d['cashier_b'].email, corps)

    def test_un_membre_ACTIF_du_meme_depot_reste_lisible(self):
        """Le contrôle : sans lui, tout refuser passerait le test précédent."""
        r = self._activite(self.d['cashier_a'].id)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

    def test_le_ROSTER_et_la_validation_disent_la_meme_chose(self):
        """
        Le sens que le croisement à deux sens ne couvrait pas : le roster
        excluait déjà les désactivés, la validation les acceptait.
        """
        from apps.users.devices import build_team_payload

        demandeur = OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        )
        proposes = {
            m['user_id'] for m in build_team_payload(self.org, demandeur)['members']
        }
        self.assertNotIn(str(self.d['cashier_b'].id), proposes)
        self.assertEqual(
            self._activite(self.d['cashier_b'].id).status_code,
            status.HTTP_404_NOT_FOUND,
        )


class IdentifiantIllisibleTests(APITestCase):
    """Une saisie fautive se refuse ; elle ne ressemble pas à une panne."""

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']

    def _get(self, qui, chemin, **params):
        self.client.force_authenticate(user=qui)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        return self.client.get(chemin, {'period': 'last_30_days', **params})

    def test_un_user_illisible_ne_rend_pas_500(self):
        r = self._get(
            self.d['manager'],
            '/api/v1/reports/statistics/user_activity/',
            user='pas-un-uuid',
        )
        self.assertLess(r.status_code, 500, r.content[:200])

    def test_un_entrepot_illisible_ne_rend_pas_500_MEME_POUR_UN_PROPRIETAIRE(self):
        """
        ⚠ La garde était APRÈS la sortie du propriétaire : le même paramètre
        rendait 400 à un gérant et 500 à un propriétaire. Il faut donc les deux
        rôles ici, et le propriétaire est celui qui manquait.
        """
        for role in ('owner', 'manager'):
            with self.subTest(role=role):
                r = self._get(
                    self.d[role],
                    '/api/v1/reports/statistics/sales/',
                    warehouse='pas-un-uuid',
                )
                self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content[:200])
                self.assertIn('warehouse', r.data)
