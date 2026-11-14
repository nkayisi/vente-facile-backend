"""
Réinitialiser le mot de passe d'un compte depuis l'administration.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CES TESTS PROTÈGENT, PAR ORDRE DE GRAVITÉ.                            │
│                                                                              │
│ 1. Que le support ne prenne jamais la main d'un ADMINISTRATEUR par ce biais :│
│    poser son mot de passe, c'est entrer avec ses pouvoirs.                   │
│ 2. Que l'ancien accès soit coupé TOUT DE SUITE : jetons de rafraîchissement, │
│    jetons d'accès encore en vie, terminaux enrôlés.                          │
│ 3. Que le mot de passe dicté au téléphone se change avant toute autre chose, │
│    et que le serveur l'impose quoi que fasse le client.                      │
│ 4. Que le mot de passe ne s'écrive nulle part.                               │
│                                                                              │
│ ⚠ `force_authenticate` CONTOURNE L'AUTHENTIFICATION, donc la règle qu'on     │
│ éprouve ici. Tout ce qui vise un jeton passe par un VRAI jeton, posé dans    │
│ l'en-tête `Authorization`.                                                   │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import json

from django.core.cache import cache
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.platform_admin.models import AdminActionLog
from apps.users.devices import enroll_device
from apps.users.models import Device, UserActivity

from ._helpers import BaseAdminTest

USERS = '/api/v1/platform-admin/users/'
TOKEN = '/api/v1/auth/token/'
REFRESH = '/api/v1/auth/token/refresh/'
LOGIN_TERMINAL = '/api/v1/auth/login/'
CHANGER = '/api/v1/users/me/change-password/'
ME = '/api/v1/users/me/'
ORGS = '/api/v1/organizations/'
SESSION_TERMINAL = '/api/v1/auth/devices/session/'

TEMPORAIRE = 'kivu-4821-mara'
NOUVEAU = 'goma-7395-tana'
MOTIF = 'Le titulaire a oublie son mot de passe, rappel au 0812345678.'


class _Base(BaseAdminTest):
    def reinitialiser(self, cible, *, par=None, password=TEMPORAIRE, reason=MOTIF):
        return self.en(par or self.support).post(
            f'{USERS}{cible.pk}/reset_password/',
            {'password': password, 'reason': reason}, format='json',
        )

    def porteur(self, access):
        """Un client qui s'authentifie par un VRAI jeton, pas par `force_authenticate`."""
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        return self.client

    def anonyme(self):
        self.client.force_authenticate(user=None)
        self.client.credentials()
        return self.client


class LeGesteTests(_Base):
    def test_le_support_pose_un_mot_de_passe_temporaire(self):
        reponse = self.reinitialiser(self.cashier_a)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        self.cashier_a.refresh_from_db()
        self.assertTrue(self.cashier_a.check_password(TEMPORAIRE))
        self.assertFalse(self.cashier_a.check_password('pw12345!'))
        self.assertTrue(self.cashier_a.must_change_password)
        self.assertIsNotNone(self.cashier_a.sessions_revoked_at)

    def test_le_mot_de_passe_ne_s_ecrit_nulle_part(self):
        reponse = self.reinitialiser(self.cashier_a)
        self.assertNotIn(TEMPORAIRE, json.dumps(reponse.data))

        ligne = AdminActionLog.objects.get(action=AdminActionLog.Action.RESET_PASSWORD)
        self.assertEqual(ligne.target_id, str(self.cashier_a.pk))
        self.assertEqual(ligne.actor, self.support)
        self.assertEqual(ligne.reason, MOTIF)
        trace = json.dumps([ligne.before, ligne.after, ligne.reason, ligne.target_repr])
        self.assertNotIn(TEMPORAIRE, trace)

        activite = UserActivity.objects.get(user=self.cashier_a, resource_type='password')
        self.assertEqual(activite.details, {'method': 'admin_reset'})

    def test_le_motif_est_obligatoire(self):
        reponse = self.reinitialiser(self.cashier_a, reason='  ')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('reason', reponse.data)
        self.cashier_a.refresh_from_db()
        self.assertFalse(self.cashier_a.must_change_password)

    def test_un_mot_de_passe_faible_est_refuse_sur_son_champ(self):
        reponse = self.reinitialiser(self.cashier_a, password='12345678')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password', reponse.data)
        self.assertFalse(AdminActionLog.objects.filter(action=AdminActionLog.Action.RESET_PASSWORD).exists())


class QuiPeutTests(_Base):
    def test_pas_son_propre_compte(self):
        reponse = self.reinitialiser(self.support, par=self.support)
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('profil', reponse.data['detail'])

    def test_pas_un_compte_desactive(self):
        self.cashier_a.is_active = False
        self.cashier_a.save(update_fields=['is_active', 'updated_at'])
        reponse = self.reinitialiser(self.cashier_a)
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('réactivez', reponse.data['detail'])

    def test_le_support_ne_touche_pas_a_un_administrateur(self):
        """Poser le mot de passe d'un super-admin, c'est entrer avec ses pouvoirs."""
        for cible in (self.super_admin, self.racine):
            with self.subTest(cible=cible.email):
                reponse = self.reinitialiser(cible)
                self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
                cible.refresh_from_db()
                self.assertFalse(cible.check_password(TEMPORAIRE))

    def test_le_super_admin_le_peut(self):
        reponse = self.reinitialiser(self.support, par=self.super_admin)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.support.refresh_from_db()
        self.assertTrue(self.support.must_change_password)

    def test_un_marchand_n_y_a_pas_acces(self):
        reponse = self.en(self.owner).post(
            f'{USERS}{self.cashier_a.pk}/reset_password/',
            {'password': TEMPORAIRE, 'reason': MOTIF}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)


class ToutEstCoupeTests(_Base):
    def test_le_jeton_de_rafraichissement_est_sur_liste_noire(self):
        ancien = RefreshToken.for_user(self.cashier_a)
        self.reinitialiser(self.cashier_a)
        reponse = self.anonyme().post(REFRESH, {'refresh': str(ancien)}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_un_jeton_d_acces_emis_avant_est_mort(self):
        acces = RefreshToken.for_user(self.cashier_a).access_token
        self.assertEqual(self.porteur(str(acces)).get(ME).status_code, status.HTTP_200_OK)

        self.reinitialiser(self.cashier_a)
        reponse = self.porteur(str(acces)).get(ME)
        self.assertEqual(reponse.status_code, status.HTTP_401_UNAUTHORIZED)
        # ⚠ Le `code` est dans le CORPS : c'est ce que le back-office lit pour
        # déconnecter la page, et un `AuthenticationFailed` ordinaire ne l'y
        # mettait pas. L'en-tête reste celui d'un vrai 401.
        self.assertEqual(reponse.data['code'], 'session_revoked')
        self.assertIn('Bearer', reponse.get('WWW-Authenticate', ''))

    def test_un_jeton_de_la_meme_seconde_meurt_aussi(self):
        """
        La première règle comparait l'`iat`, un entier, à la seconde : un jeton
        émis dans la seconde de la réinitialisation, mais AVANT elle, passait.
        On le date exactement de cette seconde pour que l'ancienne règle le laisse
        passer à coup sûr ; la marque, elle, ne lit aucune horloge.
        """
        acces = RefreshToken.for_user(self.cashier_a).access_token
        self.reinitialiser(self.cashier_a)
        self.cashier_a.refresh_from_db()
        acces.set_iat(at_time=self.cashier_a.sessions_revoked_at.replace(microsecond=0))
        reponse = self.porteur(str(acces)).get(ME)
        self.assertEqual(reponse.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_un_rafraichissement_qui_echappe_a_la_liste_noire_est_refuse(self):
        """
        La réinitialisation met sur liste noire les jetons qu'elle LIT. Un
        rafraîchissement parti au même instant en fait naître un qu'elle ne lit
        pas. On le simule en retirant le jeton de la liste noire : le refus doit
        venir de la marque, que la rotation recopie.
        """
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

        ancien = RefreshToken.for_user(self.cashier_a)
        self.reinitialiser(self.cashier_a)
        BlacklistedToken.objects.filter(token__jti=ancien['jti']).delete()

        reponse = self.anonyme().post(REFRESH, {'refresh': str(ancien)}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_401_UNAUTHORIZED, reponse.data)
        self.assertEqual(reponse.data['code'], 'session_revoked')
        # Et le refus arrive AVANT la rotation : rien de neuf n'est inscrit.
        self.assertFalse(BlacklistedToken.objects.filter(token__jti=ancien['jti']).exists())

    def test_une_seconde_reinitialisation_coupe_la_session_de_la_premiere(self):
        self.reinitialiser(self.cashier_a)
        cache.clear()
        connexion = self.anonyme().post(
            TOKEN, {'email': self.cashier_a.email, 'password': TEMPORAIRE}, format='json',
        )
        acces, rafraichi = connexion.data['access'], connexion.data['refresh']
        self.assertEqual(self.porteur(acces).get(ME).status_code, status.HTTP_200_OK)

        self.reinitialiser(self.cashier_a, password='nyira-5126-buta')
        self.assertEqual(self.porteur(acces).get(ME).status_code, status.HTTP_401_UNAUTHORIZED)
        refus = self.anonyme().post(REFRESH, {'refresh': rafraichi}, format='json')
        self.assertEqual(refus.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_les_terminaux_sont_revoques_et_le_disent(self):
        _, jeton = enroll_device(self.cashier_a, self.org, name='Caisse', platform='android')
        reponse = self.reinitialiser(self.cashier_a)
        self.assertEqual(reponse.data['devices_revoked'], 1)
        self.assertFalse(Device.objects.filter(user=self.cashier_a, revoked_at__isnull=True).exists())

        reveil = self.anonyme().post(SESSION_TERMINAL, {'device_token': jeton}, format='json')
        self.assertEqual(reveil.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_les_comptes_rendus_comptent_juste(self):
        RefreshToken.for_user(self.cashier_a)
        RefreshToken.for_user(self.cashier_a)
        # Le jeton d'un AUTRE compte n'est pas compté, ni noirci.
        autre = RefreshToken.for_user(self.cashier_b)
        reponse = self.reinitialiser(self.cashier_a)
        self.assertEqual(reponse.data['sessions_closed'], 2)
        survie = self.anonyme().post(REFRESH, {'refresh': str(autre)}, format='json')
        self.assertEqual(survie.status_code, status.HTTP_200_OK)

    def test_les_prises_en_main_de_l_administrateur_sont_closes(self):
        """
        Un jeton de prise en main échappe à `sessions_revoked_at` : sans la
        fermeture, l'administrateur dont on vient de poser le mot de passe
        garderait les minutes qui restent à la sienne.
        """
        from apps.platform_admin.impersonation import demarrer
        from apps.platform_admin.models import ImpersonationSession

        session, _ = demarrer(
            impersonator=self.support, target_user=self.cashier_a,
            organization=self.org, reason='Diagnostic de caisse',
        )
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.reinitialiser(self.support, par=self.super_admin)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        session.refresh_from_db()
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.ended_reason, ImpersonationSession.EndReason.REVOKED)


class LeChangementObligatoireTests(_Base):
    def setUp(self):
        super().setUp()
        self.reinitialiser(self.cashier_a)
        cache.clear()  # le throttle de la connexion anonyme

    def connexion(self, mot_de_passe=TEMPORAIRE):
        return self.anonyme().post(
            TOKEN, {'email': self.cashier_a.email, 'password': mot_de_passe}, format='json',
        )

    def test_la_connexion_annonce_le_changement(self):
        reponse = self.connexion()
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertTrue(reponse.data['user']['must_change_password'])

    def test_la_porte_du_terminal_l_annonce_aussi(self):
        reponse = self.anonyme().post(
            LOGIN_TERMINAL, {'email': self.cashier_a.email, 'password': TEMPORAIRE}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertTrue(reponse.data['user']['must_change_password'])

    def test_le_jeton_du_meme_instant_passe(self):
        """Né dans la seconde de la réinitialisation : il ne doit pas naître refusé."""
        acces = self.connexion().data['access']
        self.assertEqual(self.porteur(acces).get(ME).status_code, status.HTTP_200_OK)

    def test_le_rafraichissement_d_apres_garde_la_session(self):
        """La rotation recopie la marque : la session d'après se rafraîchit."""
        rafraichi = self.connexion().data['refresh']
        reponse = self.anonyme().post(REFRESH, {'refresh': rafraichi}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(self.porteur(reponse.data['access']).get(ME).status_code, status.HTTP_200_OK)

    def test_tout_le_reste_est_ferme_jusqu_au_changement(self):
        acces = self.connexion().data['access']
        client = self.porteur(acces)
        refus = client.get(ORGS)
        self.assertEqual(refus.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(refus.data['code'], 'password_change_required')
        # L'enrôlement d'un terminal aussi : le contourner par l'application ne sert à rien.
        enrolement = client.post(
            '/api/v1/auth/devices/enroll/', {'name': 'Caisse', 'platform': 'android'},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.pk),
        )
        self.assertEqual(enrolement.status_code, status.HTTP_403_FORBIDDEN)
        # Savoir qui l'on est reste permis.
        self.assertEqual(client.get(ME).status_code, status.HTTP_200_OK)

    def test_garder_le_mot_de_passe_dicte_est_refuse(self):
        acces = self.connexion().data['access']
        reponse = self.porteur(acces).post(CHANGER, {
            'current_password': TEMPORAIRE,
            'new_password': TEMPORAIRE,
            'new_password_confirm': TEMPORAIRE,
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('new_password', reponse.data)

    def test_un_jeton_qui_ignore_le_temporaire_ne_peut_rien_changer(self):
        acces = self.connexion().data['access']
        reponse = self.porteur(acces).post(CHANGER, {
            'current_password': 'pw12345!',
            'new_password': NOUVEAU,
            'new_password_confirm': NOUVEAU,
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('current_password', reponse.data)

    def test_le_changement_rouvre_tout(self):
        acces = self.connexion().data['access']
        client = self.porteur(acces)
        reponse = client.post(CHANGER, {
            'current_password': TEMPORAIRE,
            'new_password': NOUVEAU,
            'new_password_confirm': NOUVEAU,
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        self.cashier_a.refresh_from_db()
        self.assertFalse(self.cashier_a.must_change_password)
        self.assertTrue(self.cashier_a.check_password(NOUVEAU))
        # Le MÊME jeton : la session n'est pas à refaire.
        self.assertEqual(client.get(ORGS).status_code, status.HTTP_200_OK)

    def test_le_lien_par_e_mail_remet_aussi_le_drapeau(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        # Le jeton du lien porte l'empreinte du mot de passe : sur l'instance
        # d'avant la réinitialisation, il serait déjà invalide.
        self.cashier_a.refresh_from_db()
        reponse = self.anonyme().post('/api/v1/auth/password-reset/confirm/', {
            'uid': urlsafe_base64_encode(force_bytes(self.cashier_a.pk)),
            'token': default_token_generator.make_token(self.cashier_a),
            'new_password': NOUVEAU,
            'new_password_confirm': NOUVEAU,
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.cashier_a.refresh_from_db()
        self.assertFalse(self.cashier_a.must_change_password)

    def test_la_prise_en_main_n_y_est_pas_soumise(self):
        """Le support regarde un compte qui n'a pas encore changé son mot de passe."""
        reponse = self.en(self.support).post(
            f'{USERS}{self.cashier_a.pk}/impersonate/',
            {'organization': str(self.org.pk), 'reason': 'Diagnostic de son écran de caisse'},
            format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        # Une route que le jeton ORDINAIRE du titulaire se voit refuser.
        lecture = self.porteur(reponse.data['access']).get(ORGS)
        self.assertEqual(lecture.status_code, status.HTTP_200_OK)
