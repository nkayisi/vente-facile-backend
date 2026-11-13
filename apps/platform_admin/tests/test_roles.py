"""
Qui entre dans l'administration, et ce que chacun y fait.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CES TESTS PROTÈGENT, PAR ORDRE DE GRAVITÉ.                            │
│                                                                              │
│ 1. Qu'on ne puisse pas enfermer la plateforme dehors : ni en retirant le     │
│    dernier super-admin, ni en se retirant soi-même.                          │
│ 2. Que le support ne fasse que ce qui se défait.                             │
│ 3. Que `is_staff` seul n'ouvre plus rien : il se pose depuis l'admin Django  │
│    sans passer par le journal, et le laisser ouvrir la porte rendrait le     │
│    rôle décoratif.                                                           │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from rest_framework import status

from apps.platform_admin.models import AdminActionLog, PlatformAdminProfile, PlatformRole
from apps.platform_admin.permissions_catalog import PLATFORM_PERMISSION_CATALOG
from apps.platform_admin.services import PlatformAdminService, PlatformPermissionService, RefusAdministration
from apps.sales.tests._helpers import make_user

from ._helpers import BaseAdminTest

ME = '/api/v1/platform-admin/me/'
ORGS = '/api/v1/platform-admin/organizations/'
USERS = '/api/v1/platform-admin/users/'
RELEASE = '/api/v1/platform-admin/app-release/'


class QuiEntreTests(BaseAdminTest):
    def test_is_staff_seul_n_ouvre_plus_rien(self):
        self.assertEqual(self.en(self.staff_nu).get(ME).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.en(self.staff_nu).get(ORGS).status_code, status.HTTP_403_FORBIDDEN)

    def test_un_marchand_n_entre_pas(self):
        self.assertEqual(self.en(self.owner).get(ME).status_code, status.HTTP_403_FORBIDDEN)

    def test_le_superutilisateur_sans_profil_a_tout(self):
        """Le compte de `createsuperuser` : sans lui, une erreur de profil enfermerait dehors."""
        reponse = self.en(self.racine).get(ME)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(reponse.data['role'], PlatformRole.SUPER_ADMIN)
        self.assertEqual(set(reponse.data['permissions']), set(PLATFORM_PERMISSION_CATALOG))

    def test_me_rend_le_role_et_les_permissions(self):
        reponse = self.en(self.support).get(ME)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertTrue(reponse.data['is_support'])
        self.assertFalse(reponse.data['is_super_admin'])
        self.assertIn('subscriptions.extend', reponse.data['permissions'])
        self.assertNotIn('subscriptions.cancel', reponse.data['permissions'])

    def test_un_profil_revoque_ferme_la_porte(self):
        PlatformAdminService.revoke(self.support, revoked_by=self.super_admin)
        self.support.refresh_from_db()
        self.assertEqual(self.en(self.support).get(ME).status_code, status.HTTP_403_FORBIDDEN)

    def test_un_compte_desactive_n_entre_plus(self):
        self.super_admin.is_active = False
        self.super_admin.save(update_fields=['is_active', 'updated_at'])
        self.assertFalse(PlatformPermissionService.get_effective_permissions(self.super_admin))


class CeQueFaitLeSupportTests(BaseAdminTest):
    """Il consulte tout, agit sur ce qui se défait, et rien d'autre."""

    def test_il_lit_partout(self):
        client = self.en(self.support)
        for url in (ORGS, USERS, RELEASE, '/api/v1/platform-admin/plans/',
                    '/api/v1/platform-admin/subscriptions/', '/api/v1/platform-admin/audit-log/',
                    '/api/v1/platform-admin/metrics/', '/api/v1/platform-admin/dashboard/'):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, status.HTTP_200_OK)

    def test_il_ne_touche_ni_aux_tarifs_ni_a_la_porte_ni_aux_roles(self):
        client = self.en(self.support)
        refus = [
            client.post('/api/v1/platform-admin/plans/', {'name': 'X', 'code': 'x', 'price_monthly': '1', 'price_yearly': '10'}, format='json'),
            client.patch(RELEASE, {'android_update_message': 'x'}, format='json'),
            client.post(f'{USERS}{self.cashier_a.pk}/grant_role/', {'role': 'support'}, format='json'),
            client.patch(f'{ORGS}{self.org.pk}/', {'name': 'Autre'}, format='json'),
        ]
        for reponse in refus:
            with self.subTest(url=reponse.wsgi_request.path):
                self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN)

    def test_il_reactive_une_organisation(self):
        reponse = self.en(self.support).post(f'{ORGS}{self.org.pk}/toggle_active/', {}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)

    def test_il_ne_desactive_pas_un_administrateur(self):
        reponse = self.en(self.support).post(f'{USERS}{self.super_admin.pk}/toggle_active/', {}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.super_admin.refresh_from_db()
        self.assertTrue(self.super_admin.is_active)

    def test_un_extra_ouvre_une_seule_action(self):
        PlatformAdminService.grant(
            self.support, PlatformRole.SUPPORT, granted_by=self.super_admin,
            extra_permissions=['subscriptions.cancel'],
        )
        self.support.refresh_from_db()
        effectives = PlatformPermissionService.get_effective_permissions(self.support)
        self.assertIn('subscriptions.cancel', effectives)
        self.assertNotIn('plans.create', effectives)

    def test_un_extra_inconnu_est_ignore(self):
        profil = self.support.platform_admin_profile
        profil.extra_permissions = ['n_existe.pas']
        profil.save()
        frais = type(self.support).objects.get(pk=self.support.pk)
        self.assertNotIn('n_existe.pas', PlatformPermissionService.get_effective_permissions(frais))


class OctroiEtRetraitTests(BaseAdminTest):
    def test_l_octroi_pose_is_staff_et_journalise(self):
        reponse = self.en(self.super_admin).post(
            f'{USERS}{self.cashier_a.pk}/grant_role/',
            {'role': 'support', 'notes': 'Équipe support Kinshasa'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.cashier_a.refresh_from_db()
        self.assertTrue(self.cashier_a.is_staff)
        self.assertEqual(reponse.data['platform_role'], 'support')
        ligne = AdminActionLog.objects.get(action=AdminActionLog.Action.GRANT_ROLE, target_id=str(self.cashier_a.pk))
        self.assertEqual(ligne.actor, self.super_admin)
        self.assertEqual(ligne.after['role'], 'support')

    def test_changer_de_role_est_un_changement_pas_un_octroi(self):
        self.en(self.super_admin).post(f'{USERS}{self.support.pk}/grant_role/', {'role': 'super_admin'}, format='json')
        self.assertTrue(AdminActionLog.objects.filter(
            action=AdminActionLog.Action.CHANGE_ROLE, target_id=str(self.support.pk),
        ).exists())

    def test_le_retrait_garde_la_trace(self):
        reponse = self.en(self.super_admin).post(
            f'{USERS}{self.support.pk}/revoke_role/', {'reason': 'Départ'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        profil = PlatformAdminProfile.objects.get(user=self.support)
        self.assertFalse(profil.is_active)
        self.assertEqual(profil.revoked_by, self.super_admin)
        self.support.refresh_from_db()
        self.assertFalse(self.support.is_staff)

    def test_personne_ne_modifie_son_propre_role(self):
        for url, corps in ((f'{USERS}{self.super_admin.pk}/grant_role/', {'role': 'support'}),
                           (f'{USERS}{self.super_admin.pk}/revoke_role/', {})):
            with self.subTest(url=url):
                self.assertEqual(
                    self.en(self.super_admin).post(url, corps, format='json').status_code,
                    status.HTTP_400_BAD_REQUEST,
                )

    def test_le_dernier_super_admin_ne_se_retire_pas(self):
        """
        Sans superutilisateur ni autre super-admin, retirer celui-ci laisserait la
        plateforme sans personne pour nommer quiconque.
        """
        self.racine.is_superuser = False
        self.racine.save(update_fields=['is_superuser', 'updated_at'])
        autre = make_user('autre@pa.test')
        PlatformAdminService.grant(autre, PlatformRole.SUPER_ADMIN, granted_by=self.super_admin)

        PlatformAdminService.revoke(autre, revoked_by=self.super_admin)  # il en reste un
        with self.assertRaises(RefusAdministration):
            PlatformAdminService.revoke(self.super_admin, revoked_by=self.support)
        with self.assertRaises(RefusAdministration):
            PlatformAdminService.grant(self.super_admin, PlatformRole.SUPPORT, granted_by=self.support)

    def test_un_compte_desactive_ne_recoit_pas_de_role(self):
        self.cashier_a.is_active = False
        self.cashier_a.save(update_fields=['is_active', 'updated_at'])
        reponse = self.en(self.super_admin).post(
            f'{USERS}{self.cashier_a.pk}/grant_role/', {'role': 'support'}, format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)

    def test_la_liste_des_administrateurs_garde_les_revoques(self):
        PlatformAdminService.revoke(self.support, revoked_by=self.super_admin)
        reponse = self.en(self.super_admin).get('/api/v1/platform-admin/platform-admins/')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        emails = {(p['email'], p['is_active']) for p in reponse.data['results']}
        self.assertIn(('support@pa.test', False), emails)
        self.assertIn(('super@pa.test', True), emails)


class JetonTests(BaseAdminTest):
    """`is_staff` du jeton dit « peut entrer dans l'administration »."""

    URL = '/api/v1/auth/token/'

    def _connexion(self, email):
        self.client.force_authenticate(user=None)
        return self.client.post(self.URL, {'email': email, 'password': 'pw12345!'}, format='json')

    def test_le_staff_sans_role_n_est_pas_route_vers_l_admin(self):
        reponse = self._connexion('staffnu@pa.test')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertFalse(reponse.data['user']['is_staff'])

    def test_le_support_l_est(self):
        reponse = self._connexion('support@pa.test')
        self.assertTrue(reponse.data['user']['is_staff'])
