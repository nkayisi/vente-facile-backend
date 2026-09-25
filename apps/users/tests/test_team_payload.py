"""
L'équipe descendue au terminal, et servie au back-office par la même main.

┌──────────────────────────────────────────────────────────────────────────────┐
│ POURQUOI CE PAYLOAD EXISTE.                                                 │
│                                                                              │
│ Un filtre « Utilisateur » doit savoir QUI proposer quand un entrepôt est     │
│ choisi. La table locale `memberships` ne porte pas les affectations, et le   │
│ tirage ne peut pas descendre un M2M (`describe_columns` n'itère que les      │
│ champs concrets). La session est donc le seul chemin vers une réponse        │
│ disponible HORS LIGNE.                                                       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users, make_user
from apps.users.devices import build_team_payload


class EquipeDeLaSessionTests(TestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']

    def _membership(self, user):
        return OrganizationMembership.objects.get(user=user, organization=self.org)

    def test_un_caissier_ne_recoit_pas_l_equipe(self):
        """
        Ses deux filtres sont verrouillés sur lui-même : la liste nominative de
        ses collègues et leurs affectations ne lui serviraient à rien, et son
        terminal est le plus exposé du parc.
        """
        payload = build_team_payload(self.org, self._membership(self.d['cashier_a']))
        self.assertIs(payload['visible'], False)
        self.assertEqual(payload['members'], [])

    def test_un_magasinier_la_recoit(self):
        """
        La borne est le RÔLE, pas `users.view` : cette permission n'est accordée
        qu'au propriétaire et au gérant, or la règle donne le filtre utilisateur
        au magasinier aussi.
        """
        magasinier = make_user('stock@vf.test', 'Stock', 'Keeper')
        m = OrganizationMembership.objects.create(
            user=magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        m.assigned_warehouses.add(self.d['warehouse'])
        payload = build_team_payload(self.org, m)
        self.assertIs(payload['visible'], True)
        self.assertTrue(payload['members'])

    def test_chaque_membre_porte_ses_entrepots(self):
        payload = build_team_payload(self.org, self._membership(self.d['owner']))
        par_id = {m['user_id']: m for m in payload['members']}
        caissier = par_id[str(self.d['cashier_a'].id)]
        self.assertEqual(caissier['warehouses'], [str(self.d['warehouse'].id)])
        self.assertEqual(caissier['role'], OrganizationMembership.Role.CASHIER)
        # Un propriétaire n'a AUCUNE affectation : c'est ce qui le rend
        # « partout », et le client doit pouvoir le distinguer.
        self.assertEqual(par_id[str(self.d['owner'].id)]['warehouses'], [])

    def test_le_payload_ne_porte_ni_email_ni_permission(self):
        """Il part à CHAQUE réveil : on n'y met que ce qu'un sélecteur emploie."""
        payload = build_team_payload(self.org, self._membership(self.d['owner']))
        for membre in payload['members']:
            self.assertEqual(
                set(membre), {'user_id', 'name', 'role', 'warehouses'}
            )

    def test_un_nom_vide_retombe_sur_l_email(self):
        """Une ligne muette dans un sélecteur ne désigne personne."""
        anonyme = make_user('anonyme@vf.test', '', '')
        OrganizationMembership.objects.create(
            user=anonyme, organization=self.org,
            role=OrganizationMembership.Role.CASHIER, is_active=True,
        )
        payload = build_team_payload(self.org, self._membership(self.d['owner']))
        par_id = {m['user_id']: m for m in payload['members']}
        self.assertEqual(par_id[str(anonyme.id)]['name'], 'anonyme@vf.test')

    def test_le_cout_ne_croit_pas_avec_l_effectif(self):
        """
        Un `assigned_warehouses` par membre coûterait 1+N requêtes, à chaque
        réveil de chaque terminal.

        ⚠ La table pivot s'appelle `membership_warehouses`, pas
        `assigned_warehouses` : chercher le second dans le SQL ne trouve jamais
        rien et laisse le test passer sans rien mesurer.
        """
        membership = self._membership(self.d['owner'])
        with self.assertNumQueries(2):
            build_team_payload(self.org, membership)

        for i in range(10):
            u = make_user(f'extra{i}@vf.test', f'Extra{i}', 'X')
            m = OrganizationMembership.objects.create(
                user=u, organization=self.org,
                role=OrganizationMembership.Role.CASHIER, is_active=True,
            )
            m.assigned_warehouses.add(self.d['warehouse'])

        membership = self._membership(self.d['owner'])
        with self.assertNumQueries(2):
            payload = build_team_payload(self.org, membership)
        self.assertEqual(len(payload['members']), 14)

    def test_les_membres_d_une_autre_organisation_n_y_sont_pas(self):
        # ⚠ Pas un second `make_org_with_users()` : ses emails sont figés, et
        # l'unicité globale de `User.email` ferait échouer le test sur une
        # contrainte de base plutôt que sur la règle qu'il éprouve.
        from apps.organizations.models import Organization

        ailleurs = Organization.objects.create(name='Ailleurs', slug='ailleurs')
        intrus = make_user('intrus@vf.test', 'In', 'Trus')
        OrganizationMembership.objects.create(
            user=intrus, organization=ailleurs,
            role=OrganizationMembership.Role.OWNER, is_active=True,
        )
        payload = build_team_payload(self.org, self._membership(self.d['owner']))
        ids = {m['user_id'] for m in payload['members']}
        self.assertNotIn(str(intrus.id), ids)


class PorteWebDeLEquipeTests(APITestCase):
    """`GET /memberships/team/`, pour le back-office."""

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.url = '/api/v1/memberships/team/'

    def _lire(self, user):
        self.client.force_authenticate(user=user)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        return self.client.get(self.url)

    def test_un_magasinier_y_a_droit_alors_qu_il_n_a_pas_users_view(self):
        """
        C'est toute la raison d'être de cette porte : `/memberships/` exige
        `users.view`, que le magasinier n'a pas, et il y recevrait donc 403 au
        chargement de chaque page.
        """
        magasinier = make_user('stock2@vf.test', 'Stock', 'Two')
        m = OrganizationMembership.objects.create(
            user=magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        m.assigned_warehouses.add(self.d['warehouse'])

        reponse = self._lire(magasinier)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertIs(reponse.data['visible'], True)

    def test_un_caissier_recoit_une_equipe_fermee(self):
        reponse = self._lire(self.d['cashier_a'])
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertIs(reponse.data['visible'], False)
        self.assertEqual(reponse.data['members'], [])

    def test_la_porte_web_rend_le_meme_payload_que_la_session(self):
        """Deux surfaces, un seul corps : elles ne peuvent pas diverger."""
        reponse = self._lire(self.d['owner'])
        attendu = build_team_payload(
            self.org,
            OrganizationMembership.objects.get(
                user=self.d['owner'], organization=self.org
            ),
        )
        self.assertEqual(reponse.data, attendu)
