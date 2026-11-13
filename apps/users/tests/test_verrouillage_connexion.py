"""
Cinq échecs, quinze minutes, par couple (compte, adresse), sur les DEUX portes.

⚠ `cache.clear()` en tête de chaque test : en test le cache est Redis, et il
survit d'un `pytest` à l'autre. Un verrou posé par un test fermerait la porte
du suivant, qui échouerait pour une raison qui n'est pas la sienne.
"""
from unittest import mock

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.tests._helpers import make_user
from apps.users import connexion

JETON = '/api/v1/auth/token/'
TERMINAL = '/api/v1/auth/login/'
BON = 'pw12345!'


class _Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.marchand = make_user('marchand@vf.test', 'Mar', 'Chand')

    def essayer(self, mot_de_passe, *, email='marchand@vf.test', porte=JETON, ip='10.0.0.1'):
        return self.client.post(porte, {'email': email, 'password': mot_de_passe}, format='json',
                                REMOTE_ADDR=ip)

    def echouer(self, fois, **kw):
        for _ in range(fois):
            reponse = self.essayer('mauvais', **kw)
            self.assertNotEqual(reponse.status_code, status.HTTP_200_OK)
        return reponse


class VerrouTests(_Base):
    def test_le_cinquieme_echec_verrouille_et_le_dit(self):
        quatrieme = self.echouer(4)
        self.assertEqual(quatrieme.status_code, status.HTTP_400_BAD_REQUEST)
        cinquieme = self.essayer('mauvais')
        self.assertEqual(cinquieme.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(cinquieme.data['code'], 'login_locked')
        self.assertEqual(cinquieme.data['retry_after'], 15 * 60)
        self.assertEqual(cinquieme['Retry-After'], str(15 * 60))
        self.assertIn('15 minutes', cinquieme.data['detail'])

    def test_un_mot_de_passe_juste_n_ouvre_pas_pendant_le_verrou(self):
        self.echouer(5)
        self.assertEqual(self.essayer(BON).status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_le_verrou_tombe_a_son_echeance(self):
        self.echouer(5)
        plus_tard = connexion.time.time() + connexion.DUREE_SECONDES + 1
        with mock.patch.object(connexion.time, 'time', return_value=plus_tard):
            self.assertEqual(self.essayer(BON).status_code, status.HTTP_200_OK)

    def test_une_reussite_remet_le_compteur_a_zero(self):
        self.echouer(4)
        self.assertEqual(self.essayer(BON).status_code, status.HTTP_200_OK)
        self.assertEqual(self.echouer(4).status_code, status.HTTP_400_BAD_REQUEST)


class PerimetreTests(_Base):
    def test_une_autre_adresse_n_est_pas_verrouillee(self):
        """Sans quoi cinq faux mots de passe tapés de chez soi fermeraient la caisse d'un marchand."""
        self.echouer(5, ip='10.0.0.1')
        self.assertEqual(self.essayer(BON, ip='10.0.0.2').status_code, status.HTTP_200_OK)

    def test_un_autre_compte_n_est_pas_verrouille(self):
        make_user('voisin@vf.test')
        self.echouer(5)
        self.assertEqual(self.essayer(BON, email='voisin@vf.test').status_code, status.HTTP_200_OK)

    def test_la_casse_de_l_adresse_ne_remet_pas_le_compteur_a_zero(self):
        self.echouer(4, email='Marchand@VF.test')
        self.assertEqual(self.essayer('mauvais').status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_les_deux_portes_partagent_le_verrou(self):
        """Le back-office par `/auth/token/`, le terminal par `/auth/login/` : un seul compteur."""
        self.echouer(3, porte=JETON)
        self.echouer(1, porte=TERMINAL)
        self.assertEqual(self.essayer('mauvais', porte=TERMINAL).status_code,
                         status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(self.essayer(BON, porte=JETON).status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_un_compte_inexistant_se_verrouille_comme_un_autre(self):
        """Sans quoi le verrou dirait quelles adresses existent."""
        self.echouer(4, email='personne@vf.test')
        reponse = self.essayer('mauvais', email='personne@vf.test')
        self.assertEqual(reponse.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class CompteDesactiveTests(_Base):
    def test_le_bon_mot_de_passe_d_un_compte_desactive_n_est_pas_un_echec(self):
        self.marchand.is_active = False
        self.marchand.save(update_fields=['is_active', 'updated_at'])
        for _ in range(6):
            reponse = self.essayer(BON)
            self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn('désactivé', str(reponse.data))
        terminal = self.essayer(BON, porte=TERMINAL)
        self.assertEqual(terminal.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn('désactivé', terminal.data['error'])
