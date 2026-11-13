"""
La fin d'une prise en main qui n'a pas été décidée par celui qui l'a ouverte.

┌──────────────────────────────────────────────────────────────────────────────┐
│ TROIS FAÇONS DE PERDRE UN POUVOIR, ET CHACUNE DOIT COUPER L'ACCÈS.          │
│                                                                              │
│ Un super-administrateur arrête la session ; on retire son rôle au support ; │
│ on désactive un compte (l'auteur comme la cible) ou on suspend              │
│ l'établissement. Sans ces fermetures, le support gardait les minutes qui    │
│ restaient à sa prise en main, et la ligne restait ouverte pour toujours.    │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ `captureOnCommitCallbacks(execute=True)` autour de chaque fermeture : le
cache de `session_vivante` n'est purgé qu'au COMMIT, et `APITestCase` enveloppe
chaque test dans une transaction qui n'en voit jamais. Sans lui, la requête
suivante lirait « vivante » en cache et le test échouerait pour une raison qui
n'est pas la sienne - ou, pire, passerait sans avoir prouvé la purge.
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status

from apps.platform_admin.models import AdminActionLog, ImpersonationSession, PlatformAdminProfile
from apps.platform_admin.services import oublier_profil

from .test_prise_en_main import BASE, _Base


class _Fin(_Base):
    def ouvrir(self, **kw):
        """Prend la main et LIT une fois, pour que « vivante » soit en cache."""
        acces = self.jeton(**kw)
        self.assertEqual(self.sous(acces).get('/api/v1/sales/').status_code, status.HTTP_200_OK)
        return acces, ImpersonationSession.objects.get(jti=self._jti(acces))

    def assert_coupe(self, acces, raison=ImpersonationSession.EndReason.REVOKED):
        self.assertEqual(self.sous(acces).get('/api/v1/sales/').status_code, status.HTTP_401_UNAUTHORIZED)
        session = ImpersonationSession.objects.get(jti=self._jti(acces))
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.ended_reason, raison)
        return session

    @staticmethod
    def _jti(acces):
        from rest_framework_simplejwt.tokens import AccessToken

        return str(AccessToken(acces)['jti'])


class ArretForceTests(_Fin):
    def arreter(self, session, par=None, motif='Session laissée ouverte par erreur'):
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.en(par or self.super_admin).post(
                f'{BASE}/impersonation-sessions/{session.pk}/end/', {'reason': motif}, format='json',
            )
        self.client.force_authenticate(user=None)
        return reponse

    def test_un_super_administrateur_coupe_l_acces_tout_de_suite(self):
        acces, session = self.ouvrir()
        reponse = self.arreter(session)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(reponse.data['ended_reason'], 'revoked')
        self.assertFalse(reponse.data['is_live'])
        self.assert_coupe(acces)

    def test_le_journal_l_attribue_a_qui_l_a_decide(self):
        """Le support n'a pas terminé lui-même une session qu'on lui a retirée."""
        _, session = self.ouvrir()
        self.arreter(session, motif='Accès non justifié')
        trace = AdminActionLog.objects.get(action='impersonate_end')
        self.assertEqual(trace.actor, self.super_admin)
        self.assertEqual(trace.reason, 'Accès non justifié')
        self.assertEqual(trace.after['ended_reason'], 'revoked')

    def test_le_support_ne_coupe_pas_la_session_d_un_autre(self):
        _, session = self.ouvrir()
        self.assertEqual(self.arreter(session, par=self.support).status_code, status.HTTP_403_FORBIDDEN)
        session.refresh_from_db()
        self.assertIsNone(session.ended_at)

    def test_le_motif_est_exige(self):
        acces, session = self.ouvrir()
        reponse = self.arreter(session, motif='')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('reason', reponse.data)
        self.assertEqual(self.sous(acces).get('/api/v1/sales/').status_code, status.HTTP_200_OK)

    def test_une_session_terminee_ne_se_coupe_pas_deux_fois(self):
        _, session = self.ouvrir()
        self.arreter(session)
        self.assertEqual(self.arreter(session).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AdminActionLog.objects.filter(action='impersonate_end').count(), 1)

    def test_une_session_echue_ne_se_revoque_pas(self):
        """⚠ L'accès a cessé de lui-même : le journal ne doit pas l'attribuer au super-admin."""
        _, session = self.ouvrir()
        ImpersonationSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.arreter(session).status_code, status.HTTP_400_BAD_REQUEST)
        session.refresh_from_db()
        self.assertNotEqual(session.ended_reason, 'revoked')
        self.assertFalse(AdminActionLog.objects.filter(action='impersonate_end').exists())


class FermetureAutomatiqueTests(_Fin):
    def test_retirer_le_role_ferme_les_sessions_du_support(self):
        acces, _ = self.ouvrir()
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.en(self.super_admin).post(
                f'{BASE}/users/{self.support.pk}/revoke_role/', {'reason': 'Départ du support'}, format='json',
            )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assert_coupe(acces)

    def test_desactiver_le_support_ferme_ses_sessions(self):
        acces, _ = self.ouvrir()
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.en(self.super_admin).post(
                f'{BASE}/users/{self.support.pk}/toggle_active/', {'reason': 'Compte compromis'}, format='json',
            )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assert_coupe(acces)

    def test_desactiver_la_cible_ferme_les_sessions_qui_la_visent(self):
        acces, _ = self.ouvrir()
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.en(self.super_admin).post(
                f'{BASE}/users/{self.cashier_a.pk}/toggle_active/', {'reason': 'Employé parti'}, format='json',
            )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assert_coupe(acces)

    def test_suspendre_l_etablissement_ferme_ses_sessions(self):
        acces, _ = self.ouvrir()
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.en(self.super_admin).post(
                f'{BASE}/organizations/{self.org.pk}/toggle_active/', {'reason': 'Impayé de longue date'},
                format='json',
            )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assert_coupe(acces)

    def test_reactiver_ne_ferme_rien(self):
        """Le contrôle : seule la perte d'un pouvoir ferme, pas son retour."""
        autre = self.cashier_b
        autre.is_active = False
        autre.save(update_fields=['is_active', 'updated_at'])
        acces, session = self.ouvrir()
        with self.captureOnCommitCallbacks(execute=True):
            self.en(self.super_admin).post(f'{BASE}/users/{autre.pk}/toggle_active/', {}, format='json')
        session.refresh_from_db()
        self.assertIsNone(session.ended_at)
        self.assertEqual(self.sous(acces).get('/api/v1/sales/').status_code, status.HTTP_200_OK)

    def test_un_pouvoir_perdu_hors_de_ces_chemins_ferme_aussi(self):
        """
        Le filet : le profil désactivé SANS passer par `revoke` (admin Django,
        commande de gestion). La session se revérifie à la lecture suivante et se
        clôt, au nom du système.
        """
        acces, _ = self.ouvrir()
        PlatformAdminProfile.objects.filter(user=self.support).update(is_active=False)
        oublier_profil(self.support)
        cache.clear()
        session = self.assert_coupe(acces)
        trace = AdminActionLog.objects.get(action='impersonate_end')
        self.assertIsNone(trace.actor)
        self.assertEqual(trace.after['ended_by'], 'système')
        self.assertEqual(trace.after['session'], str(session.pk))


class OuvertureSuccessiveTests(_Fin):
    def test_une_session_echue_n_a_pas_ete_remplacee(self):
        _, premiere = self.ouvrir()
        echeance = timezone.now() - timedelta(minutes=1)
        ImpersonationSession.objects.filter(pk=premiere.pk).update(expires_at=echeance)
        self.jeton(cible=self.cashier_b)
        premiere.refresh_from_db()
        self.assertEqual(premiere.ended_reason, 'expired')
        # La fin est l'ÉCHÉANCE : c'est là que l'accès a réellement cessé.
        self.assertEqual(premiere.ended_at, echeance)

    def test_une_session_vivante_est_remplacee(self):
        _, premiere = self.ouvrir()
        self.jeton(cible=self.cashier_b)
        premiere.refresh_from_db()
        self.assertEqual(premiere.ended_reason, 'replaced')


class FiltreEnCoursTests(_Fin):
    def test_en_cours_ne_rend_que_les_sessions_qui_ouvrent_encore_quelque_chose(self):
        # Terminée par le support lui-même.
        acces, _ = self.ouvrir()
        self.sous(acces).post(f'{BASE}/impersonation/end/', {}, format='json')
        # ⚠ Échue SANS fin écrite : ouverte par un AUTRE administrateur, sinon la
        # prise en main suivante du support la clôturerait au passage.
        _, echue = self.ouvrir(par=self.super_admin, cible=self.cashier_b)
        ImpersonationSession.objects.filter(pk=echue.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        _, vivante = self.ouvrir(cible=self.manager)

        reponse = self.en(self.super_admin).get(f'{BASE}/impersonation-sessions/?en_cours=1')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual([s['id'] for s in reponse.data['results']], [str(vivante.pk)])
        # Sans le filtre, l'historique entier.
        tout = self.en(self.super_admin).get(f'{BASE}/impersonation-sessions/')
        self.assertEqual(tout.data['count'], 3)
