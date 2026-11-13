"""
La prise en main : ce que le jeton ouvre, et surtout ce qu'il n'ouvre pas.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CES TESTS EMPLOIENT DE VRAIS JETONS, JAMAIS `force_authenticate`.            │
│                                                                              │
│ `force_authenticate` CONTOURNE la classe d'authentification - c'est-à-dire   │
│ précisément ce qu'il faut éprouver ici. Un test écrit avec lui passerait     │
│ même si aucune règle n'était appliquée.                                      │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework_simplejwt.tokens import AccessToken

from apps.organizations.models import Organization, OrganizationMembership
from apps.platform_admin.models import AdminActionLog, ImpersonationSession
from apps.users.models import UserActivity

from ._helpers import BaseAdminTest

BASE = '/api/v1/platform-admin'


class _Base(BaseAdminTest):
    def prendre_la_main(self, cible=None, org=None, par=None, motif='Le marchand signale un écart de caisse'):
        reponse = self.en(par or self.support).post(
            f'{BASE}/users/{(cible or self.cashier_a).pk}/impersonate/',
            {'organization': str((org or self.org).pk), 'reason': motif}, format='json',
        )
        self.client.force_authenticate(user=None)
        return reponse

    def jeton(self, **kw):
        reponse = self.prendre_la_main(**kw)
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        return reponse.data['access']

    def sous(self, access, org=None):
        """Un client qui présente ce jeton, comme un navigateur."""
        self.client.force_authenticate(user=None)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {access}',
            HTTP_X_ORGANIZATION_ID=str((org or self.org).pk),
        )
        return self.client


class OuvertureTests(_Base):
    def test_le_jeton_est_court_et_ne_se_rafraichit_pas(self):
        reponse = self.prendre_la_main()
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.assertNotIn('refresh', reponse.data)
        jeton = AccessToken(reponse.data['access'])
        self.assertTrue(jeton['imp'])
        restant = jeton['exp'] - timezone.now().timestamp()
        self.assertLessEqual(restant, 20 * 60 + 5)
        # Ce n'est pas un jeton de rafraîchissement : l'endpoint le refuse.
        refus = self.client.post('/api/v1/auth/token/refresh/', {'refresh': reponse.data['access']}, format='json')
        self.assertEqual(refus.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_elle_est_tracee_des_deux_cotes(self):
        self.jeton()
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.impersonator, self.support)
        self.assertEqual(session.target_user, self.cashier_a)
        self.assertTrue(AdminActionLog.objects.filter(action='impersonate_start', actor=self.support).exists())
        # Le journal de l'ORGANISATION aussi : le marchand doit pouvoir le savoir.
        trace = UserActivity.objects.get(organization=self.org, action='impersonated')
        self.assertEqual(trace.user, self.support)
        self.assertEqual(trace.details['reason'], 'Le marchand signale un écart de caisse')

    def test_un_motif_est_exige(self):
        self.assertEqual(self.prendre_la_main(motif='').status_code, status.HTTP_400_BAD_REQUEST)

    def test_on_ne_prend_pas_la_main_sur_un_administrateur(self):
        for cible in (self.super_admin, self.racine):
            with self.subTest(cible=cible.email):
                OrganizationMembership.objects.get_or_create(
                    user=cible, organization=self.org, defaults={'role': 'manager', 'is_active': True},
                )
                self.assertEqual(self.prendre_la_main(cible=cible).status_code, status.HTTP_400_BAD_REQUEST)

    def test_la_cible_doit_etre_membre_de_l_organisation(self):
        ailleurs = Organization.objects.create(name='Ailleurs', slug='ailleurs')
        self.assertEqual(self.prendre_la_main(org=ailleurs).status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_marchand_ne_prend_pas_la_main(self):
        self.assertEqual(self.prendre_la_main(par=self.owner).status_code, status.HTTP_403_FORBIDDEN)

    def test_une_nouvelle_prise_en_main_clot_la_precedente(self):
        self.jeton()
        self.jeton(cible=self.cashier_b)
        self.assertEqual(
            sorted(ImpersonationSession.objects.values_list('ended_reason', flat=True)),
            ['', 'replaced'],
        )


class LectureSeuleTests(_Base):
    def test_il_lit_ce_que_lit_le_marchand(self):
        client = self.sous(self.jeton())
        self.assertEqual(client.get('/api/v1/sales/').status_code, status.HTTP_200_OK)
        self.assertEqual(client.get('/api/v1/users/me/').status_code, status.HTTP_200_OK)

    def test_aucune_ecriture_ne_passe(self):
        """
        Les trois écritures qu'un inventaire à la main risquait d'oublier : le mot
        de passe, le profil, et l'enrôlement d'un terminal - qui délivrerait un
        jeton d'appareil de trente jours au nom du marchand.
        """
        client = self.sous(self.jeton())
        for methode, url, corps in (
            ('post', '/api/v1/users/me/change-password/', {'old_password': 'pw12345!', 'new_password': 'X-2026-nouveau!'}),
            ('patch', '/api/v1/users/me/', {'first_name': 'Pirate'}),
            ('post', '/api/v1/auth/devices/enroll/', {'name': 'Caisse pirate', 'platform': 'android'}),
            ('post', '/api/v1/sales/', {}),
        ):
            with self.subTest(url=url):
                reponse = getattr(client, methode)(url, corps, format='json')
                self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)
                self.assertEqual(reponse.data.get('detail').code, 'impersonation_read_only')
        self.cashier_a.refresh_from_db()
        self.assertEqual(self.cashier_a.first_name, 'Cash')
        self.assertTrue(self.cashier_a.check_password('pw12345!'))

    def test_une_seule_organisation(self):
        autre = Organization.objects.create(name='Autre boutique', slug='autre-boutique')
        OrganizationMembership.objects.create(user=self.cashier_a, organization=autre, role='owner', is_active=True)
        acces = self.jeton()
        self.assertEqual(self.sous(acces, org=autre).get('/api/v1/sales/').status_code,
                         status.HTTP_403_FORBIDDEN)
        # La liste des organisations du marchand ne montre que celle-ci.
        liste = self.sous(acces).get('/api/v1/organizations/')
        self.assertEqual(liste.status_code, status.HTTP_200_OK)
        donnees = liste.data['results'] if isinstance(liste.data, dict) else liste.data
        self.assertEqual([o['id'] for o in donnees], [str(self.org.pk)])

    def test_le_jeton_n_ouvre_pas_l_administration(self):
        self.assertEqual(self.sous(self.jeton()).get(f'{BASE}/organizations/').status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_un_jeton_ordinaire_ecrit_toujours(self):
        """Le contrôle : la classe d'authentification ne change rien à un jeton normal."""
        access = str(AccessToken.for_user(self.cashier_a))
        client = self.sous(access)
        reponse = client.patch('/api/v1/users/me/', {'first_name': 'Josué'}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)


class FinTests(_Base):
    def test_la_fin_coupe_l_acces_tout_de_suite(self):
        acces = self.jeton()
        client = self.sous(acces)
        self.assertEqual(client.post(f'{BASE}/impersonation/end/', {}, format='json').status_code,
                         status.HTTP_200_OK)
        self.assertEqual(client.get('/api/v1/sales/').status_code, status.HTTP_401_UNAUTHORIZED)
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.ended_reason, 'manual')
        self.assertTrue(AdminActionLog.objects.filter(action='impersonate_end').exists())

    def test_une_session_echue_ne_lit_plus_rien(self):
        acces = self.jeton()
        ImpersonationSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        cache.clear()
        self.assertEqual(self.sous(acces).get('/api/v1/sales/').status_code, status.HTTP_401_UNAUTHORIZED)

    def test_l_historique_est_lisible_par_l_administration(self):
        self.jeton()
        reponse = self.en(self.super_admin).get(f'{BASE}/impersonation-sessions/')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(reponse.data['results'][0]['target_email'], 'a@vf.test')
        self.assertTrue(reponse.data['results'][0]['is_live'])
