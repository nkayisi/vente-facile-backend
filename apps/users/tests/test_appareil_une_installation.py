"""
Un téléphone, un appareil actif : la déconnexion révoque, la reconnexion remplace.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DEUX FUITES QUI NE SE VOYAIENT QUE DANS LA LISTE DU GÉRANT.                 │
│                                                                              │
│ La déconnexion du terminal postait un corps VIDE : le jeton de              │
│ rafraîchissement restait valable, l'appareil restait actif. Et chaque       │
│ reconnexion créait un appareil de plus, l'ancien gardant un jeton que plus  │
│ personne ne détenait. La liste s'allongeait de fantômes qu'on n'osait pas   │
│ révoquer, de peur de couper un terminal en service.                         │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.organizations.models import Organization, OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users, make_user
from apps.users.devices import enroll_device
from apps.users.models import Device


class _Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.__dict__.update(make_org_with_users())

    def _enroler(self, user, installation='', org=None):
        return enroll_device(
            user, org or self.org, name='Caisse', platform='android',
            installation_id=installation,
        )


class DeconnexionTests(_Base):
    def _deconnecter(self, user, **corps):
        self.client.force_authenticate(user=user)
        return self.client.post('/api/v1/auth/logout/', corps, format='json')

    def test_la_deconnexion_revoque_l_appareil_du_terminal(self):
        appareil, jeton = self._enroler(self.cashier_a)
        reponse = self._deconnecter(self.cashier_a, device_token=jeton)
        self.assertEqual(reponse.status_code, 200)
        appareil.refresh_from_db()
        self.assertTrue(appareil.is_revoked)

    def test_un_rafraichissement_illisible_n_empeche_pas_la_revocation(self):
        """Le `except` qui rend « Déconnexion réussie » ne doit pas l'avaler."""
        appareil, jeton = self._enroler(self.cashier_a)
        self._deconnecter(self.cashier_a, device_token=jeton, refresh='pas-un-jeton')
        appareil.refresh_from_db()
        self.assertTrue(appareil.is_revoked)

    def test_le_rafraichissement_part_sur_liste_noire(self):
        jeton = RefreshToken.for_user(self.cashier_a)
        self._deconnecter(self.cashier_a, refresh=str(jeton))
        self.client.force_authenticate(user=None)
        relance = self.client.post(
            '/api/v1/auth/token/refresh/', {'refresh': str(jeton)}, format='json',
        )
        self.assertEqual(relance.status_code, 401)

    def test_on_ne_revoque_pas_l_appareil_d_un_collegue(self):
        appareil, jeton = self._enroler(self.cashier_b)
        self._deconnecter(self.cashier_a, device_token=jeton)
        appareil.refresh_from_db()
        self.assertFalse(appareil.is_revoked)

    def test_la_revocation_redescend_au_tirage(self):
        """
        `devices` est tirée : une révocation dont `updated_at` ne bouge pas y
        resterait invisible, la même faille que `CustomerBalance` au lot 6.
        """
        appareil, jeton = self._enroler(self.cashier_a)
        Device.objects.filter(pk=appareil.pk).update(
            updated_at=timezone.now() - timedelta(days=3),
        )
        appareil.refresh_from_db()
        avant = appareil.updated_at
        self._deconnecter(self.cashier_a, device_token=jeton)
        appareil.refresh_from_db()
        self.assertGreater(appareil.updated_at, avant)


class ReconnexionTests(_Base):
    def test_la_meme_installation_remplace_son_appareil(self):
        ancien, _ = self._enroler(self.cashier_a, 'tel-1')
        nouveau, _ = self._enroler(self.cashier_a, 'tel-1')
        ancien.refresh_from_db()
        self.assertTrue(ancien.is_revoked)
        self.assertFalse(nouveau.is_revoked)

    def test_le_code_d_appareil_n_est_jamais_reemploye(self):
        """Il numérote les tickets : le reprendre ferait repartir une série à zéro."""
        ancien, _ = self._enroler(self.cashier_a, 'tel-1')
        nouveau, _ = self._enroler(self.cashier_a, 'tel-1')
        self.assertNotEqual(ancien.device_code, nouveau.device_code)

    def test_un_terminal_partage_change_de_caissier(self):
        """Même boutique, autre compte : le téléphone vient d'effacer l'ancien jeton."""
        ancien, _ = self._enroler(self.cashier_a, 'tel-1')
        self._enroler(self.cashier_b, 'tel-1')
        ancien.refresh_from_db()
        self.assertTrue(ancien.is_revoked)

    def test_une_autre_installation_n_est_pas_touchee(self):
        ancien, _ = self._enroler(self.cashier_a, 'tel-1')
        self._enroler(self.cashier_a, 'tel-2')
        ancien.refresh_from_db()
        self.assertFalse(ancien.is_revoked)

    def test_sans_identifiant_rien_n_est_revoque(self):
        """Un terminal antérieur ne l'envoie pas : il s'enrôle comme avant."""
        ancien, _ = self._enroler(self.cashier_a, '')
        self._enroler(self.cashier_a, '')
        ancien.refresh_from_db()
        self.assertFalse(ancien.is_revoked)

    def test_l_identifiant_ne_permet_pas_de_revoquer_un_autre_etablissement(self):
        """
        L'identifiant vient du CLIENT : ni même compte ni même boutique, on ne
        touche à rien - sinon le connaître suffirait à couper un concurrent.
        """
        autre_org = Organization.objects.create(name='Concurrent', slug='concurrent')
        inconnu = make_user('autre@vf.test')
        OrganizationMembership.objects.create(
            user=inconnu, organization=autre_org, role='cashier', is_active=True,
        )
        etranger, _ = self._enroler(inconnu, 'tel-1', org=autre_org)
        self._enroler(self.cashier_a, 'tel-1')
        etranger.refresh_from_db()
        self.assertFalse(etranger.is_revoked)

    def test_l_enrolement_par_l_api_transmet_l_installation(self):
        ancien, _ = self._enroler(self.cashier_a, 'tel-1')
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.post(
            '/api/v1/auth/devices/enroll/',
            {'name': 'Caisse', 'platform': 'android', 'installation_id': 'tel-1'},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        ancien.refresh_from_db()
        self.assertTrue(ancien.is_revoked)
