"""
Les utilisateurs et les appareils ne franchissent JAMAIS la frontière d'un
établissement.

Un compte peut servir plusieurs marchands ; tirer la table entière donnerait à
n'importe quel caissier la liste des employés d'un concurrent. Et un terminal
est un objet qu'on perd : sa base n'est pas chiffrée.

Ces deux tests coûtent quelques secondes et couvrent une fuite qui ne se verrait
jamais autrement - un tirage réussi ne se plaint de rien.
"""
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization, OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users
from apps.users.models import Device, User

PULL = '/api/v1/sync/pull/'


class PerimetreDesPersonnesTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        # Un SECOND établissement, avec son propre employé.
        self.autre_org = Organization.objects.create(
            name='Concurrent', slug='concurrent', email='c@c.cd',
        )
        self.espion = User.objects.create_user(
            email='espion@concurrent.cd', password='x', first_name='Espion',
        )
        OrganizationMembership.objects.create(
            organization=self.autre_org, user=self.espion, role='owner',
        )
        self.client.force_authenticate(user=self.owner)

    def _tirer(self, table):
        reponse = self.client.get(
            PULL, {'table': table},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        return reponse.data['rows']

    def test_a_user_of_another_organization_is_never_pulled(self):
        emails = {r['email'] for r in self._tirer('users')}
        self.assertNotIn('espion@concurrent.cd', emails)
        self.assertIn(self.owner.email, emails)

    def test_no_secret_column_ever_leaves_the_server(self):
        """
        Un terminal se perd, et sa base n'est pas chiffrée. Ni mot de passe,
        ni drapeau de super-utilisateur ne doivent s'y trouver.
        """
        lignes = self._tirer('users')
        self.assertTrue(lignes)
        interdits = {'password', 'is_superuser', 'is_staff', 'preferences'}
        for ligne in lignes:
            self.assertEqual(interdits & set(ligne), set(), ligne.keys())

    def test_a_device_of_another_organization_is_never_pulled(self):
        Device.objects.create(
            user=self.espion, organization=self.autre_org, name='Terminal voisin',
            platform='android', device_code='ZZZZ', token_hash='x' * 64,
            expires_at='2027-01-01T00:00:00Z',
        )
        mien = Device.objects.create(
            user=self.owner, organization=self.org, name='Mon terminal',
            platform='android', device_code='AAAA', token_hash='y' * 64,
            expires_at='2027-01-01T00:00:00Z',
        )
        codes = {r['device_code'] for r in self._tirer('devices')}
        self.assertEqual(codes, {'AAAA'})
        self.assertIn(str(mien.id), {r['id'] for r in self._tirer('devices')})

    def test_the_device_token_never_leaves_the_server(self):
        Device.objects.create(
            user=self.owner, organization=self.org, name='Mon terminal',
            platform='android', device_code='BBBB', token_hash='z' * 64,
            expires_at='2027-01-01T00:00:00Z',
        )
        for ligne in self._tirer('devices'):
            self.assertNotIn('token_hash', ligne)
