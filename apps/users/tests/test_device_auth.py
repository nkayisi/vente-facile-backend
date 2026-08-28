"""
Sessions longues sur terminal enrôlé.

Ce que ces tests protègent : un caissier doit pouvoir ouvrir l'application après
trois semaines sans réseau et vendre, sans mot de passe et sans connexion. Et un
gérant doit pouvoir couper l'accès d'un terminal perdu, immédiatement, sans
effacer les ventes qu'il porte encore.

Les deux exigences se contredisent si on les traite à la légère : c'est pourquoi
le jeton d'appareil n'authentifie AUCUN endpoint métier, ne sert qu'à réobtenir
une paire JWT, glisse à l'usage et se révoque d'un clic.
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.tests._helpers import make_org_with_users
from apps.users.devices import enroll_device
from apps.users.models import Device


class _DeviceBaseTest(APITestCase):
    def setUp(self):
        # Le réveil est limité à 5 appels par minute, et DRF compte dans le
        # cache partagé : sans purge, le cinquième test paie pour les quatre
        # précédents et échoue en 429 sans rapport avec ce qu'il vérifie.
        cache.clear()
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _enroll(self, user=None):
        """Enrôle par l'API et retourne la réponse."""
        self.client.force_authenticate(user=user or self.cashier_a)
        return self.client.post(
            '/api/v1/auth/devices/enroll/',
            {
                'name': 'Caisse 1',
                'platform': 'android',
                'model': 'NYX NB55',
                'os_version': '11',
                'app_version': '1.0.0',
            },
            format='json',
            **self._headers(),
        )


class EnrollmentTests(_DeviceBaseTest):
    def test_enroll_returns_token_once_and_stores_only_its_hash(self):
        resp = self._enroll()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        raw = resp.data['device_token']
        self.assertTrue(raw)

        device = Device.objects.get(id=resp.data['device']['id'])
        # Le jeton en clair ne doit exister nulle part en base.
        self.assertNotEqual(device.token_hash, raw)
        self.assertEqual(device.token_hash, Device.hash_token(raw))
        self.assertEqual(len(device.token_hash), 64)

        # Et il ne se relit pas : la liste ne le porte pas.
        self.client.force_authenticate(user=self.owner)
        listing = self.client.get('/api/v1/auth/devices/', **self._headers())
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        body = str(listing.data)
        self.assertNotIn(raw, body)
        self.assertNotIn('token_hash', body)

    def test_enroll_allocates_a_short_readable_device_code(self):
        resp = self._enroll()
        code = resp.data['device']['device_code']

        self.assertEqual(len(code), Device.CODE_LENGTH)
        # Ni I ni O : sur un ticket thermique ils se confondent avec 1 et 0, et
        # le code sert justement à rattacher un papier à sa caisse.
        self.assertNotIn('I', code)
        self.assertNotIn('O', code)
        self.assertTrue(all(c in Device.CODE_ALPHABET for c in code))

    def test_device_codes_are_unique_within_an_organization(self):
        codes = {
            enroll_device(
                self.cashier_a, self.org, name=f'Caisse {i}', platform='android'
            )[0].device_code
            for i in range(12)
        }
        self.assertEqual(len(codes), 12)

    def test_enroll_requires_an_authenticated_member(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(
            '/api/v1/auth/devices/enroll/',
            {'name': 'Caisse', 'platform': 'android'},
            format='json',
            **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class SessionPayloadTests(_DeviceBaseTest):
    """
    La réponse doit suffire à travailler hors ligne.

    Ce n'est pas une commodité : la connexion qui vient de revenir peut repartir
    avant le deuxième appel. Ce qui manque ici manque pour la journée.
    """

    def test_session_returns_everything_needed_to_work_offline(self):
        token = self._enroll().data['device_token']
        self.client.force_authenticate(user=None)

        resp = self.client.post(
            '/api/v1/auth/devices/session/',
            {'device_token': token},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for key in (
            'access', 'refresh', 'user', 'organization', 'membership',
            'settings', 'currencies', 'loyalty_program', 'device', 'server_time',
        ):
            self.assertIn(key, resp.data, f'« {key} » manque à la charge de réveil')

        # Le rôle et les permissions effectives : sans eux, l'interface ne peut
        # pas se garder hors ligne.
        self.assertEqual(resp.data['membership']['role'], 'cashier')
        self.assertIn('sales.create', resp.data['membership']['permissions'])
        self.assertNotIn('stock.view', resp.data['membership']['permissions'])

        # L'identité de l'organisation, qui s'imprime en tête de chaque ticket.
        self.assertEqual(resp.data['organization']['name'], self.org.name)

    def test_session_token_actually_authenticates_the_api(self):
        token = self._enroll().data['device_token']
        self.client.force_authenticate(user=None)

        access = self.client.post(
            '/api/v1/auth/devices/session/',
            {'device_token': token},
            format='json',
        ).data['access']

        resp = self.client.get(
            '/api/v1/users/me/',
            HTTP_AUTHORIZATION=f'Bearer {access}',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['email'], self.cashier_a.email)


class SessionRefusalTests(_DeviceBaseTest):
    def test_unknown_token_is_refused(self):
        resp = self.client.post(
            '/api/v1/auth/devices/session/',
            {'device_token': 'jeton-invente'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(resp.data['code'], 'device_not_authorized')

    def test_revoked_device_is_refused_and_indistinguishable_from_unknown(self):
        token = self._enroll().data['device_token']
        device = Device.objects.get(token_hash=Device.hash_token(token))
        device.revoke(by=self.owner)

        self.client.force_authenticate(user=None)
        resp = self.client.post(
            '/api/v1/auth/devices/session/',
            {'device_token': token},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        # Même code qu'un jeton inconnu : rien ne doit permettre de distinguer
        # « révoqué » de « n'a jamais existé » depuis l'extérieur.
        self.assertEqual(resp.data['code'], 'device_not_authorized')

    def test_expired_device_is_refused(self):
        token = self._enroll().data['device_token']
        device = Device.objects.get(token_hash=Device.hash_token(token))
        device.expires_at = timezone.now() - timedelta(seconds=1)
        device.save(update_fields=['expires_at'])

        self.client.force_authenticate(user=None)
        resp = self.client.post(
            '/api/v1/auth/devices/session/', {'device_token': token}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_losing_membership_closes_the_device(self):
        """Un caissier retiré de l'établissement ne rouvre plus de session."""
        token = self._enroll().data['device_token']
        self.cashier_a.memberships.filter(organization=self.org).update(is_active=False)

        self.client.force_authenticate(user=None)
        resp = self.client.post(
            '/api/v1/auth/devices/session/', {'device_token': token}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(resp.data['code'], 'membership_revoked')

    def test_deactivated_user_closes_the_device(self):
        token = self._enroll().data['device_token']
        self.cashier_a.is_active = False
        self.cashier_a.save(update_fields=['is_active'])

        self.client.force_authenticate(user=None)
        resp = self.client.post(
            '/api/v1/auth/devices/session/', {'device_token': token}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(resp.data['code'], 'user_inactive')


class SlidingExpiryTests(_DeviceBaseTest):
    """
    L'échéance glisse à l'usage.

    Une échéance fixe ferait mourir tout le parc le même jour, un mois après le
    déploiement. Une échéance glissante ne tue que les terminaux réellement
    silencieux.
    """

    def test_expiry_is_pushed_back_on_each_session(self):
        token = self._enroll().data['device_token']
        device = Device.objects.get(token_hash=Device.hash_token(token))

        # Vingt jours passent sans réseau.
        device.expires_at = timezone.now() + timedelta(days=10)
        device.save(update_fields=['expires_at'])

        self.client.force_authenticate(user=None)
        self.client.post(
            '/api/v1/auth/devices/session/', {'device_token': token}, format='json'
        )

        device.refresh_from_db()
        remaining = (device.expires_at - timezone.now()).days
        self.assertGreaterEqual(remaining, Device.TTL_DAYS - 1)

    def test_a_device_offline_for_more_than_thirty_days_expires(self):
        device, token = enroll_device(
            self.cashier_a, self.org, name='Caisse', platform='android'
        )
        device.expires_at = timezone.now() - timedelta(days=1)
        device.save(update_fields=['expires_at'])

        self.assertFalse(device.is_usable)

    def test_sliding_never_exceeds_the_absolute_ceiling(self):
        """Un parc ne devient pas immortel à force de se synchroniser."""
        device, _ = enroll_device(
            self.cashier_a, self.org, name='Caisse', platform='android'
        )
        # L'appareil a été enrôlé il y a presque six mois.
        device.created_at = timezone.now() - timedelta(days=Device.ABSOLUTE_TTL_DAYS - 5)
        device.save(update_fields=['created_at'])

        device.touch()

        device.refresh_from_db()
        self.assertLessEqual(device.expires_at, device.absolute_deadline)
        self.assertLess((device.expires_at - timezone.now()).days, Device.TTL_DAYS)


class RevocationTests(_DeviceBaseTest):
    def test_manager_can_revoke_a_device(self):
        device_id = self._enroll().data['device']['id']

        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f'/api/v1/auth/devices/{device_id}/revoke/', **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        device = Device.objects.get(id=device_id)
        self.assertTrue(device.is_revoked)
        self.assertEqual(device.revoked_by, self.owner)

    def test_cashier_cannot_revoke_a_device(self):
        device_id = self._enroll().data['device']['id']

        self.client.force_authenticate(user=self.cashier_b)
        resp = self.client.post(
            f'/api/v1/auth/devices/{device_id}/revoke/', **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_revoking_is_idempotent_and_keeps_the_first_author(self):
        device, _ = enroll_device(
            self.cashier_a, self.org, name='Caisse', platform='android'
        )
        device.revoke(by=self.owner)
        first = device.revoked_at

        device.revoke(by=self.manager)

        device.refresh_from_db()
        self.assertEqual(device.revoked_at, first)
        self.assertEqual(device.revoked_by, self.owner)

    def test_devices_of_another_organization_are_invisible(self):
        from apps.organizations.models import Organization, OrganizationMembership
        from apps.users.models import User

        voisin = Organization.objects.create(name='Voisin', slug='voisin')
        autre_user = User.objects.create_user(
            email='voisin@vf.test', password='x', first_name='Voi', last_name='Sin'
        )
        OrganizationMembership.objects.create(
            user=autre_user, organization=voisin,
            role=OrganizationMembership.Role.CASHIER, is_active=True,
        )
        enroll_device(autre_user, voisin, name='Ailleurs', platform='android')
        self._enroll()

        self.client.force_authenticate(user=self.owner)
        resp = self.client.get('/api/v1/auth/devices/', **self._headers())

        names = [d['name'] for d in resp.data.get('results', resp.data)]
        self.assertIn('Caisse 1', names)
        self.assertNotIn('Ailleurs', names)
