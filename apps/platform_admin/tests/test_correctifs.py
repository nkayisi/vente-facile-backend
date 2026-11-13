"""
Les défauts relevés dans l'administration, chacun avec le test qui l'aurait vu.

Chaque classe nomme son défaut ; chaque test a été lancé sur le code d'origine.
"""
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status

from apps.core.exception_handler import api_exception_handler
from apps.organizations.models import Organization
from apps.platform_admin.models import AdminActionLog
from apps.subscriptions.models import Plan, Subscription
from apps.users.models import User

from ._helpers import BaseAdminTest

BASE = '/api/v1/platform-admin'


class RienNeSeSupprimeTests(BaseAdminTest):
    """`DELETE` exécutait un vrai DELETE SQL, en cascade sur toutes les ventes."""

    def test_aucune_suppression_n_aboutit(self):
        client = self.en(self.super_admin)
        plan = Plan.objects.get(code='TEST')
        for url in (f'{BASE}/organizations/{self.org.pk}/', f'{BASE}/users/{self.cashier_a.pk}/',
                    f'{BASE}/plans/{plan.pk}/'):
            with self.subTest(url=url):
                self.assertEqual(client.delete(url).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
                self.assertEqual(client.put(url, {}, format='json').status_code,
                                 status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.cashier_a.pk).exists())

    def test_une_organisation_ne_se_cree_pas_d_ici(self):
        """Elle naîtrait orpheline, sans propriétaire pour s'y connecter."""
        reponse = self.en(self.super_admin).post(
            f'{BASE}/organizations/', {'name': 'Orpheline'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)

    def test_un_plan_se_retire_de_la_vente(self):
        plan = Plan.objects.get(code='TEST')
        reponse = self.en(self.super_admin).post(f'{BASE}/plans/{plan.pk}/toggle_active/', {}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertFalse(plan.is_active)
        # Les abonnés en cours le gardent.
        self.assertTrue(Subscription.objects.filter(plan=plan, status='active').exists())

    def test_une_ProtectedError_rend_409_et_non_500(self):
        reponse = api_exception_handler(ProtectedError('cité ailleurs', set()), {})
        self.assertEqual(reponse.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(reponse.data['code'], 'protected_reference')


class CreationDeCompteTests(BaseAdminTest):
    """Le compte naissait sans mot de passe haché, donc inutilisable."""

    URL = f'{BASE}/users/'

    def test_le_compte_cree_peut_se_connecter(self):
        reponse = self.en(self.super_admin).post(self.URL, {
            'email': 'Nouveau@PA.test', 'first_name': 'Nou', 'last_name': 'Veau',
            'password': 'Kinshasa-2026!', 'password_confirm': 'Kinshasa-2026!',
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.assertEqual(reponse.data['email'], 'nouveau@pa.test')
        self.assertIsNotNone(authenticate(email='nouveau@pa.test', password='Kinshasa-2026!'))
        self.assertTrue(AdminActionLog.objects.filter(
            action='create', target_type='user', target_repr='nouveau@pa.test',
        ).exists())

    def test_sans_mot_de_passe_c_est_refuse(self):
        reponse = self.en(self.super_admin).post(self.URL, {'email': 'x@pa.test'}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password', reponse.data)

    def test_deux_mots_de_passe_differents_sont_refuses(self):
        reponse = self.en(self.super_admin).post(self.URL, {
            'email': 'y@pa.test', 'password': 'Kinshasa-2026!', 'password_confirm': 'autre-chose-1',
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password_confirm', reponse.data)

    def test_une_adresse_deja_prise_quelle_que_soit_la_casse(self):
        reponse = self.en(self.super_admin).post(self.URL, {
            'email': 'OWNER@vf.test', 'password': 'Kinshasa-2026!', 'password_confirm': 'Kinshasa-2026!',
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', reponse.data)


class EditionTests(BaseAdminTest):
    def test_l_organisation_se_corrige_et_l_ecart_est_journalise(self):
        reponse = self.en(self.super_admin).patch(
            f'{BASE}/organizations/{self.org.pk}/',
            {'name': 'Boutique Kin Centre', 'city': 'Kinshasa', 'is_active': False, 'currency': 'USD'},
            format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, 'Boutique Kin Centre')
        # Ni l'état ni la devise ne passent par ici.
        self.assertTrue(self.org.is_active)
        self.assertEqual(self.org.currency, 'CDF')
        ligne = AdminActionLog.objects.get(action='update', target_type='organization')
        self.assertEqual(ligne.before['name'], 'Test Org')
        self.assertEqual(ligne.after['name'], 'Boutique Kin Centre')
        self.assertNotIn('currency', ligne.after)

    def test_le_compte_se_corrige_sans_toucher_a_l_adresse(self):
        reponse = self.en(self.super_admin).patch(
            f'{BASE}/users/{self.cashier_a.pk}/',
            {'first_name': 'Josué', 'email': 'pirate@pa.test'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.cashier_a.refresh_from_db()
        self.assertEqual(self.cashier_a.first_name, 'Josué')
        self.assertEqual(self.cashier_a.email, 'a@vf.test')


class DesactivationVisibleAuTirageTests(BaseAdminTest):
    """
    `User` n'hérite pas de `TimeStampedModel` : `update_fields=['is_active']`
    laissait `updated_at` en place, et la table `users` descend au tirage avec
    `is_active`. Un compte désactivé restait actif sur tous les terminaux.
    """

    def test_updated_at_avance(self):
        avant = User.objects.get(pk=self.cashier_a.pk).updated_at
        reponse = self.en(self.super_admin).post(
            f'{BASE}/users/{self.cashier_a.pk}/toggle_active/', {'reason': 'Départ'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        apres = User.objects.get(pk=self.cashier_a.pk)
        self.assertFalse(apres.is_active)
        self.assertGreater(apres.updated_at, avant)

    def test_on_ne_se_desactive_pas_soi_meme(self):
        reponse = self.en(self.super_admin).post(
            f'{BASE}/users/{self.super_admin.pk}/toggle_active/', {}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)


class AbonnementsTests(BaseAdminTest):
    URL = f'{BASE}/subscriptions/'

    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.get(code='TEST')

    def test_la_date_de_debut_est_honoree(self):
        """Elle était validée puis ignorée : l'abonnement partait toujours d'aujourd'hui."""
        debut = timezone.localdate() - timedelta(days=5)
        reponse = self.en(self.super_admin).post(self.URL, {
            'organization': str(self.org.pk), 'plan': str(self.plan.pk),
            'billing_cycle': 'monthly', 'start_date': debut.isoformat(),
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        sub = Subscription.objects.get(pk=reponse.data['id'])
        self.assertEqual(timezone.localtime(sub.current_period_start).date(), debut)
        self.assertEqual((sub.current_period_end - sub.current_period_start).days, 30)

    def test_une_date_future_est_refusee(self):
        reponse = self.en(self.super_admin).post(self.URL, {
            'organization': str(self.org.pk), 'plan': str(self.plan.pk),
            'billing_cycle': 'monthly',
            'start_date': (timezone.localdate() + timedelta(days=3)).isoformat(),
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('start_date', reponse.data)

    def test_une_periode_deja_echue_est_refusee_et_rien_ne_change(self):
        actif = Subscription.objects.get(organization=self.org, status='active')
        reponse = self.en(self.super_admin).post(self.URL, {
            'organization': str(self.org.pk), 'plan': str(self.plan.pk),
            'billing_cycle': 'monthly',
            'start_date': (timezone.localdate() - timedelta(days=60)).isoformat(),
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        # La transaction a tout défait, y compris l'expiration de l'abonnement en cours.
        actif.refresh_from_db()
        self.assertEqual(actif.status, 'active')

    def test_une_prolongation_mal_saisie_rend_400_et_non_500(self):
        sub = Subscription.objects.get(organization=self.org, status='active')
        client = self.en(self.super_admin)
        for jours in ('trente', '', -10, 0):
            with self.subTest(jours=jours):
                reponse = client.post(f'{self.URL}{sub.pk}/extend/', {'days': jours}, format='json')
                self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        sub.refresh_from_db()
        self.assertGreater(sub.current_period_end, timezone.now() + timedelta(days=25))

    def test_le_support_prolonge_et_c_est_journalise(self):
        sub = Subscription.objects.get(organization=self.org, status='active')
        fin = sub.current_period_end
        reponse = self.en(self.support).post(
            f'{self.URL}{sub.pk}/extend/', {'days': 10, 'reason': 'Geste commercial'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        sub.refresh_from_db()
        self.assertEqual(sub.current_period_end, fin + timedelta(days=10))
        ligne = AdminActionLog.objects.get(action='extend_subscription')
        self.assertEqual(ligne.actor, self.support)
        self.assertEqual(ligne.reason, 'Geste commercial')

    def test_le_support_n_annule_pas(self):
        sub = Subscription.objects.get(organization=self.org, status='active')
        reponse = self.en(self.support).post(f'{self.URL}{sub.pk}/cancel/', {}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)

    def test_une_annulation_ne_se_rejoue_pas_et_ne_se_prolonge_pas(self):
        sub = Subscription.objects.get(organization=self.org, status='active')
        client = self.en(self.super_admin)
        self.assertEqual(client.post(f'{self.URL}{sub.pk}/cancel/', {}, format='json').status_code,
                         status.HTTP_200_OK)
        self.assertEqual(client.post(f'{self.URL}{sub.pk}/cancel/', {}, format='json').status_code,
                         status.HTTP_400_BAD_REQUEST)
        self.assertEqual(client.post(f'{self.URL}{sub.pk}/extend/', {'days': 5}, format='json').status_code,
                         status.HTTP_400_BAD_REQUEST)


class ListesSansN1Tests(BaseAdminTest):
    """
    Le sérialiseur relisait l'abonnement trois fois par ligne. On épingle
    l'INVARIANT - le coût ne croît pas avec le nombre de lignes - pas un nombre.
    """

    def _cout(self, url):
        client = self.en(self.super_admin)
        # Échauffement : la première requête charge le profil de l'administrateur,
        # mémoïsé ensuite sur l'instance. Le compter ferait croire à un N+1.
        client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            reponse = client.get(url)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries)

    def test_organisations(self):
        un = self._cout(f'{BASE}/organizations/')
        for i in range(5):
            Organization.objects.create(name=f'Org {i}', slug=f'org-{i}')
        self.assertEqual(self._cout(f'{BASE}/organizations/'), un)

    def test_comptes(self):
        un = self._cout(f'{BASE}/users/')
        from apps.sales.tests._helpers import make_user
        for i in range(5):
            make_user(f'n{i}@pa.test')
        self.assertEqual(self._cout(f'{BASE}/users/'), un)
