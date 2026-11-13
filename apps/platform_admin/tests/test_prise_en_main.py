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
import re
from datetime import timedelta

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import URLResolver, get_resolver
from django.utils import timezone
from rest_framework import status
from rest_framework_simplejwt.tokens import AccessToken

from apps.core.authentication import INTERDITS
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


class PerimetreDesEcransTests(_Base):
    """Lecture seule ne suffit pas : la synchro, les exports et les traces du marchand restent fermés."""

    def sans_en_tete(self, access):
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        return self.client

    def test_ce_qui_sert_a_emporter_ou_recouper_est_ferme(self):
        client = self.sous(self.jeton())
        for url in (
            '/api/v1/sync/pull/manifest/',
            '/api/v1/sync/pull/?table=products',
            '/api/v1/sales/export/?export_format=csv',
            '/api/v1/stock-movements/supplies-export/',
            '/api/v1/cash-movements/export-report/',
            f'/api/v1/customers/{self.org.pk}/report/export/',
            '/api/v1/users/activities/',
            '/api/v1/user-activities/',
            '/api/v1/auth/devices/',
        ):
            with self.subTest(url=url):
                reponse = client.get(url)
                self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)
                self.assertEqual(reponse.data['detail'].code, 'impersonation_forbidden')

    def test_sans_en_tete_seules_les_routes_de_niveau_compte_repondent(self):
        client = self.sans_en_tete(self.jeton())
        self.assertEqual(client.get('/api/v1/users/me/').status_code, status.HTTP_200_OK)
        self.assertEqual(client.get('/api/v1/organizations/').status_code, status.HTTP_200_OK)
        # Les catalogues de la plateforme ne lisent aucune organisation.
        self.assertEqual(client.get('/api/v1/settings/currencies/').status_code, status.HTTP_200_OK)
        self.assertEqual(client.get('/api/v1/plans/').status_code, status.HTTP_200_OK)
        # ⚠ Les devises ACTIVÉES par l'établissement, elles, le lisent.
        self.assertEqual(client.get('/api/v1/settings/organization-currencies/').status_code,
                         status.HTTP_403_FORBIDDEN)
        refus = client.get('/api/v1/sales/')
        self.assertEqual(refus.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(refus.data['detail'].code, 'impersonation_org_required')

    def test_le_profil_ne_montre_que_l_organisation_de_la_prise_en_main(self):
        autre = Organization.objects.create(name='Autre boutique', slug='autre-boutique-profil')
        OrganizationMembership.objects.create(user=self.cashier_a, organization=autre, role='owner', is_active=True)
        self.cashier_a.active_organization = autre
        self.cashier_a.save(update_fields=['active_organization', 'updated_at'])

        profil = self.sans_en_tete(self.jeton()).get('/api/v1/users/me/').data

        self.assertEqual([o['id'] for o in profil['organizations']], [str(self.org.pk)])
        self.assertEqual(str(profil['active_organization']), str(self.org.pk))
        self.assertNotIn('Autre boutique', str(profil))

    def test_un_jeton_ordinaire_lit_toujours_ses_exports(self):
        """Le contrôle : la règle ne touche que les jetons de prise en main."""
        client = self.sous(str(AccessToken.for_user(self.owner)))
        self.assertNotEqual(client.get('/api/v1/sales/export/?export_format=csv').status_code,
                            status.HTTP_403_FORBIDDEN)


class ContratDesExportsTests(SimpleTestCase):
    """
    ⚠ Le point faible d'une liste d'interdits : un export ajouté demain.

    On parcourt le routeur réel et on exige que toute route dont le chemin
    porte « export » tombe sous la règle. Un balayage qui ne trouve rien
    passerait au vert sans rien prouver : on compte d'abord.
    """

    def routes(self):
        def parcourir(motifs, prefixe=''):
            for motif in motifs:
                brut = prefixe + str(motif.pattern)
                if isinstance(motif, URLResolver):
                    yield from parcourir(motif.url_patterns, brut)
                else:
                    yield brut

        return list(parcourir(get_resolver().url_patterns))

    def test_toute_route_d_export_est_fermee(self):
        exports = [r for r in self.routes() if 'export' in r]
        self.assertGreaterEqual(len(exports), 8, "Le balayage ne trouve plus les exports.")
        for brut in exports:
            # Un chemin d'exemple : chaque groupe nommé remplacé par une valeur.
            chemin = '/' + re.sub(r'\(\?P<[^>]+>[^)]+\)|<[^>]+>', 'x', brut)
            chemin = chemin.replace('^', '').replace('$', '').replace('\\', '')
            chemin = re.sub(r'\.\(\?P<format>[^)]*\)/?', '', chemin)
            with self.subTest(route=brut):
                self.assertTrue(
                    any(m.search(chemin) for m in INTERDITS),
                    f"{chemin} n'est pas fermé sous une prise en main.",
                )


class FinTests(_Base):
    def test_la_fin_coupe_l_acces_tout_de_suite(self):
        acces = self.jeton()
        client = self.sous(acces)
        # ⚠ Le cache n'est purgé qu'au COMMIT (une requête concurrente le
        # remettrait sinon « vivante » pendant la transaction) : sans capture,
        # le test lirait le cache et ne prouverait rien.
        with self.captureOnCommitCallbacks(execute=True):
            fin = client.post(f'{BASE}/impersonation/end/', {}, format='json')
        self.assertEqual(fin.status_code, status.HTTP_200_OK)
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
