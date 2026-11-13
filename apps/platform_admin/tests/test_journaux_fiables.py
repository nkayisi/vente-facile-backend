"""
Des journaux qu'on peut croire : la bonne adresse, un motif là où il le faut,
et rien de gardé au-delà d'un an.
"""
from datetime import timedelta
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django_celery_beat.models import PeriodicTask
from rest_framework import status

from apps.core.adresse import adresse_client
from apps.core.throttles import AnonParAdresse
from apps.platform_admin.models import AdminActionLog, ImpersonationSession
from apps.platform_admin.tasks import clore_sessions_expirees, purger_au_dela_de_la_retention
from apps.settings.models import Currency
from apps.subscriptions.models import Plan, Subscription
from apps.users.models import UserActivity

from ._helpers import BaseAdminTest

BASE = '/api/v1/platform-admin'
CLE = 'secret-de-bordure-de-test'


def requete(**meta):
    req = RequestFactory().get('/')
    req.META.update(meta)
    return req


class AdresseTests(SimpleTestCase):
    """⚠ L'entrée de GAUCHE de X-Forwarded-For est écrite par le client lui-même."""

    @override_settings(TRUSTED_PROXY_COUNT=0, VF_EDGE_KEY='')
    def test_sans_proxy_l_en_tete_forge_est_ignore(self):
        req = requete(REMOTE_ADDR='10.0.0.9', HTTP_X_FORWARDED_FOR='6.6.6.6')
        self.assertEqual(adresse_client(req), '10.0.0.9')

    @override_settings(TRUSTED_PROXY_COUNT=1, VF_EDGE_KEY='')
    def test_derriere_un_proxy_on_lit_la_droite(self):
        # Le client a forgé « 6.6.6.6 » ; le proxy a ajouté l'adresse réelle.
        req = requete(REMOTE_ADDR='172.18.0.2', HTTP_X_FORWARDED_FOR='6.6.6.6, 41.243.10.7')
        self.assertEqual(adresse_client(req), '41.243.10.7')

    @override_settings(TRUSTED_PROXY_COUNT=1, VF_EDGE_KEY=CLE)
    def test_le_back_office_atteste_l_adresse_du_navigateur(self):
        req = requete(REMOTE_ADDR='172.18.0.5', HTTP_X_VF_EDGE_KEY=CLE, HTTP_X_VF_CLIENT_IP='41.243.10.7')
        self.assertEqual(adresse_client(req), '41.243.10.7')

    @override_settings(TRUSTED_PROXY_COUNT=0, VF_EDGE_KEY=CLE)
    def test_sans_le_bon_secret_l_attestation_ne_vaut_rien(self):
        for cle in ('mauvaise-cle', ''):
            with self.subTest(cle=cle):
                req = requete(REMOTE_ADDR='172.18.0.5', HTTP_X_VF_EDGE_KEY=cle, HTTP_X_VF_CLIENT_IP='6.6.6.6')
                self.assertEqual(adresse_client(req), '172.18.0.5')

    @override_settings(TRUSTED_PROXY_COUNT=0, VF_EDGE_KEY='')
    def test_un_secret_vide_cote_serveur_n_ouvre_rien(self):
        """Une clé vide des DEUX côtés ne doit pas valoir accord."""
        req = requete(REMOTE_ADDR='172.18.0.5', HTTP_X_VF_EDGE_KEY='', HTTP_X_VF_CLIENT_IP='6.6.6.6')
        self.assertEqual(adresse_client(req), '172.18.0.5')

    @override_settings(TRUSTED_PROXY_COUNT=1, VF_EDGE_KEY=CLE)
    def test_une_valeur_qui_n_est_pas_une_adresse_est_ecartee(self):
        req = requete(REMOTE_ADDR='172.18.0.5', HTTP_X_VF_EDGE_KEY=CLE,
                      HTTP_X_VF_CLIENT_IP="' OR 1=1 --", HTTP_X_FORWARDED_FOR='pas-une-ip')
        self.assertEqual(adresse_client(req), '172.18.0.5')

    @override_settings(TRUSTED_PROXY_COUNT=0, VF_EDGE_KEY=CLE)
    def test_les_limites_de_debit_comptent_la_vraie_adresse(self):
        """Sans cela, toutes les connexions web partagent l'adresse de Next."""
        req = requete(REMOTE_ADDR='172.18.0.5', HTTP_X_VF_EDGE_KEY=CLE, HTTP_X_VF_CLIENT_IP='41.243.10.7')
        self.assertEqual(AnonParAdresse().get_ident(req), '41.243.10.7')


class MotifRequisTests(BaseAdminTest):
    """Un geste qui ARRÊTE quelqu'un exige un motif, au serveur aussi."""

    def poster(self, url, corps):
        return self.en(self.super_admin).post(url, corps, format='json')

    def test_desactiver_un_etablissement_sans_motif_est_refuse(self):
        for corps in ({}, {'reason': '   '}, {'reason': 'ok'}):
            with self.subTest(corps=corps):
                reponse = self.poster(f'{BASE}/organizations/{self.org.pk}/toggle_active/', corps)
                self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('reason', reponse.data)
        self.org.refresh_from_db()
        self.assertTrue(self.org.is_active)

    def test_reactiver_n_exige_pas_de_motif(self):
        self.org.is_active = False
        self.org.save(update_fields=['is_active', 'updated_at'])
        reponse = self.poster(f'{BASE}/organizations/{self.org.pk}/toggle_active/', {})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)

    def test_desactiver_un_compte_sans_motif_est_refuse(self):
        reponse = self.poster(f'{BASE}/users/{self.cashier_a.pk}/toggle_active/', {})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.cashier_a.refresh_from_db()
        self.assertTrue(self.cashier_a.is_active)

    def test_retirer_un_role_sans_motif_est_refuse(self):
        reponse = self.poster(f'{BASE}/users/{self.support.pk}/revoke_role/', {})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)

    def test_annuler_un_abonnement_sans_motif_est_refuse(self):
        usd, _ = Currency.objects.get_or_create(code='USD', defaults={'name': 'Dollar', 'symbol': '$'})
        plan = Plan.objects.create(name='P', code='p-motif', price_monthly=10, price_yearly=100, currency=usd)
        sub = Subscription.objects.create(
            organization=self.org, plan=plan, status='active', billing_cycle='monthly',
            price=10, currency='USD', current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=20),
        )
        reponse = self.poster(f'{BASE}/subscriptions/{sub.pk}/cancel/', {})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'active')

    def test_le_motif_est_garde_sans_ses_blancs(self):
        self.poster(f'{BASE}/organizations/{self.org.pk}/toggle_active/', {'reason': '  Fraude signalée  '})
        ligne = AdminActionLog.objects.filter(target_id=str(self.org.pk)).latest('created_at')
        self.assertEqual(ligne.reason, 'Fraude signalée')

    @override_settings(TRUSTED_PROXY_COUNT=0, VF_EDGE_KEY=CLE)
    def test_le_journal_garde_l_adresse_attestee(self):
        self.en(self.super_admin).post(
            f'{BASE}/organizations/{self.org.pk}/toggle_active/', {'reason': 'Fraude signalée'},
            format='json', HTTP_X_VF_EDGE_KEY=CLE, HTTP_X_VF_CLIENT_IP='41.243.10.7',
        )
        ligne = AdminActionLog.objects.filter(target_id=str(self.org.pk)).latest('created_at')
        self.assertEqual(ligne.ip_address, '41.243.10.7')


class RetentionTests(BaseAdminTest):
    """Douze mois, puis purge ; une prise en main ouverte n'est jamais purgée."""

    def vieillir(self, modele, pk, jours, champ='created_at'):
        modele.objects.filter(pk=pk).update(**{champ: timezone.now() - timedelta(days=jours)})

    def session(self, *, ouverte, echeance):
        return ImpersonationSession.objects.create(
            impersonator=self.support, target_user=self.cashier_a, organization=self.org,
            reason='Contrôle', jti=f'jti-{timezone.now().timestamp()}-{ouverte}-{echeance}',
            expires_at=echeance, ended_at=None if ouverte else echeance,
            ended_reason='' if ouverte else ImpersonationSession.EndReason.MANUAL,
        )

    def test_la_purge_retire_ce_qui_a_plus_d_un_an_et_garde_le_reste(self):
        vieux = AdminActionLog.objects.create(action='update', target_type='organization', target_id='x')
        recent = AdminActionLog.objects.create(action='update', target_type='organization', target_id='y')
        self.vieillir(AdminActionLog, vieux.pk, 400)
        activite = UserActivity.objects.create(user=self.cashier_a, action='login', resource_type='session')
        self.vieillir(UserActivity, activite.pk, 400)

        purges = purger_au_dela_de_la_retention()

        self.assertFalse(AdminActionLog.objects.filter(pk=vieux.pk).exists())
        self.assertTrue(AdminActionLog.objects.filter(pk=recent.pk).exists())
        self.assertFalse(UserActivity.objects.filter(pk=activite.pk).exists())
        self.assertGreaterEqual(purges['admin_action_logs'], 1)

    def test_une_prise_en_main_ouverte_n_est_jamais_purgee(self):
        ancienne = timezone.now() - timedelta(days=500)
        ouverte = self.session(ouverte=True, echeance=ancienne)
        close = self.session(ouverte=False, echeance=ancienne)
        purger_au_dela_de_la_retention()
        self.assertTrue(ImpersonationSession.objects.filter(pk=ouverte.pk).exists())
        self.assertFalse(ImpersonationSession.objects.filter(pk=close.pk).exists())

    def test_le_balayage_clot_les_sessions_echues_a_leur_echeance(self):
        echeance = timezone.now() - timedelta(minutes=5)
        echue = self.session(ouverte=True, echeance=echeance)
        vivante = self.session(ouverte=True, echeance=timezone.now() + timedelta(minutes=10))
        avant = ImpersonationSession.objects.get(pk=echue.pk).updated_at

        self.assertEqual(clore_sessions_expirees(), 1)

        echue.refresh_from_db()
        vivante.refresh_from_db()
        self.assertEqual(echue.ended_reason, ImpersonationSession.EndReason.EXPIRED)
        self.assertEqual(echue.ended_at, echeance)
        self.assertGreater(echue.updated_at, avant)
        self.assertIsNone(vivante.ended_at)


class PlanificationTests(TestCase):
    def test_la_purge_est_planifiee_chaque_nuit(self):
        tache = PeriodicTask.objects.get(name='purger_journaux_administration')
        self.assertEqual(tache.task, 'apps.platform_admin.tasks.purger_journaux')
        self.assertTrue(tache.enabled)

    def test_la_tache_enchaine_cloture_puis_purge(self):
        from apps.platform_admin import tasks

        with mock.patch.object(tasks, 'clore_sessions_expirees', return_value=0) as clore, \
                mock.patch.object(tasks, 'purger_au_dela_de_la_retention', return_value={}) as purger:
            tasks.purger_journaux()
        clore.assert_called_once()
        purger.assert_called_once()
