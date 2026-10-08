"""
La porte de mise à jour du terminal : ce que le serveur descend, et ce qu'il refuse.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CES TESTS PROTÈGENT, PAR ORDRE DE GRAVITÉ.                            │
│                                                                              │
│ 1. Qu'une migration ou un défaut mal posé ne bloque pas TOUT LE PARC à       │
│    l'instant du déploiement. C'est le seul défaut de ce lot qui ne se répare │
│    pas depuis un terminal.                                                   │
│ 2. Que la SORTIE D'URGENCE fonctionne : vider un seuil posé trop haut doit   │
│    agir au réveil suivant, pas au bon vouloir d'un cache.                    │
│ 3. Qu'un formulaire refuse un seuil au-dessus de ce que le magasin sert,     │
│    plutôt que de laisser un humain briquer le parc d'un clic.                │
└──────────────────────────────────────────────────────────────────────────────┘

Le rôle est BORNÉ, jamais `owner` : un terminal est tenu par un caissier, et
c'est pour lui que la porte doit se fermer comme pour les autres.
"""
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.versions import comparer_versions, urls_magasin
from apps.sales.tests._helpers import make_org_with_users
from apps.subscriptions.models import GlobalConfig
from apps.users.devices import app_update_payload, build_session_payload, enroll_device

FICHE_PLAY = 'https://play.google.com/store/apps/details?id=com.cdir.ventefacile'
FICHE_IOS = 'https://apps.apple.com/app/id123456789'


class _BaseTest(TestCase):
    def setUp(self):
        # Le réveil est limité à 5 appels par minute, et le cache porte DEUX
        # choses ici : le compteur de DRF et les réglages de plateforme. Sans
        # purge, un test lit les réglages qu'un autre vient d'écrire.
        cache.clear()
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

    def _appareil(self, user=None, plateforme='android'):
        return enroll_device(
            user=user or self.cashier_a,
            organization=self.org,
            name='Caisse 1',
            platform=plateforme,
            model='NYX',
            os_version='11',
            app_version='1.0.0',
            ip='127.0.0.1',
        )[0]

    def _poser_seuil(self, **champs):
        config = GlobalConfig.get()
        for cle, valeur in champs.items():
            setattr(config, cle, valeur)
        config.save()
        return config


class ParDefautAucuneExigenceTests(_BaseTest):
    """
    Le test qui protège contre le déploiement lui-même. S'il tombe, un seuil est
    posé par défaut quelque part, et le parc se bloque à la migration.
    """

    def test_une_installation_neuve_n_exige_rien(self):
        for plateforme in ('android', 'ios'):
            with self.subTest(plateforme=plateforme):
                bloc = app_update_payload(self._appareil(plateforme=plateforme))
                self.assertIsNone(bloc['minimum_version'])
                self.assertIsNone(bloc['current_version'])
                self.assertIsNone(bloc['message'])
                self.assertEqual(bloc['store_urls'], [])

    def test_sans_appareil_on_n_exige_rien(self):
        # On ne sait pas quelle plateforme viser : une exigence devinée bloque
        # au hasard.
        self.assertIsNone(app_update_payload(None)['minimum_version'])


class PlateformeTests(_BaseTest):
    def setUp(self):
        super().setUp()
        self._poser_seuil(
            android_minimum_version='1.3.0', android_current_version='1.3.0',
            android_store_url=FICHE_PLAY,
            ios_minimum_version='2.0.0', ios_current_version='2.0.0',
            ios_store_url=FICHE_IOS,
        )

    def test_chaque_appareil_recoit_le_seuil_de_SA_plateforme(self):
        android = app_update_payload(self._appareil(plateforme='android'))
        self.assertEqual(android['minimum_version'], '1.3.0')
        self.assertEqual(android['store_urls'][-1], FICHE_PLAY)

        ios = app_update_payload(self._appareil(user=self.cashier_b, plateforme='ios'))
        self.assertEqual(ios['minimum_version'], '2.0.0')
        self.assertEqual(ios['store_urls'], [FICHE_IOS])

    def test_un_appareil_ne_recoit_JAMAIS_les_deux(self):
        # Un binaire iOS qui lirait le seuil Android se bloquerait sur une
        # version qui n'existe pas chez lui, et rien ne le signalerait.
        bloc = app_update_payload(self._appareil(plateforme='android'))
        self.assertNotIn('ios', bloc)
        self.assertNotIn('android', bloc)
        self.assertEqual(
            set(bloc), {'minimum_version', 'current_version', 'store_urls', 'message'}
        )


class ChargeDeReveilTests(_BaseTest):
    def test_le_caissier_recoit_l_exigence(self):
        # Tout l'objet de l'exercice : c'est lui qui tient le terminal.
        self._poser_seuil(
            android_minimum_version='1.3.0', android_store_url=FICHE_PLAY,
        )
        payload = build_session_payload(
            self.cashier_a, self.org, device=self._appareil()
        )
        self.assertIn('app_update', payload)
        self.assertEqual(payload['app_update']['minimum_version'], '1.3.0')

    def test_le_cout_du_reveil_n_augmente_pas(self):
        """
        Mesuré à 15 requêtes AVANT ce lot, 13 après : l'accesseur caché en retire
        deux (`GlobalConfig.get()` était appelé une fois par
        `get_subscription_status` et une fois par `subscription_payload`).

        ⚠ Ce chemin est parcouru à chaque réveil de chaque terminal. Le plafond
        est là pour qu'une lecture de plus s'y voie ; si ce test tombe en montant,
        c'est une requête ajoutée, pas un seuil à relever.
        """
        appareil = self._appareil()
        build_session_payload(self.cashier_a, self.org, device=appareil)  # chauffe
        with CaptureQueriesContext(connection) as capture:
            build_session_payload(self.cashier_a, self.org, device=appareil)
        self.assertLessEqual(
            len(capture.captured_queries), 13,
            f"le réveil coûte {len(capture.captured_queries)} requêtes",
        )


class SortieDUrgenceTests(_BaseTest):
    def test_vider_le_seuil_rouvre_la_porte(self):
        appareil = self._appareil()
        self._poser_seuil(
            android_minimum_version='9.9.9', android_store_url=FICHE_PLAY,
        )
        self.assertEqual(app_update_payload(appareil)['minimum_version'], '9.9.9')

        self._poser_seuil(android_minimum_version='')
        self.assertIsNone(app_update_payload(appareil)['minimum_version'])

    def test_save_purge_le_cache_des_reglages(self):
        # Sans cette purge, une urgence attendrait la fin du TTL : le marchand
        # reste enfermé alors que la correction est faite.
        GlobalConfig.reglages()  # remplit le cache
        self._poser_seuil(
            android_minimum_version='1.3.0', android_store_url=FICHE_PLAY,
        )
        self.assertEqual(
            GlobalConfig.reglages()['android']['minimum_version'], '1.3.0'
        )


class ValidationTests(TestCase):
    def setUp(self):
        cache.clear()

    def _config(self, **champs):
        config = GlobalConfig.get()
        for cle, valeur in champs.items():
            setattr(config, cle, valeur)
        return config

    def test_un_seuil_se_saisit_en_trois_segments(self):
        for mauvais in ('v1.2.3', '1.2', '1', '1.2.3.4', 'derniere'):
            with self.subTest(valeur=mauvais):
                with self.assertRaises(ValidationError) as leve:
                    self._config(android_minimum_version=mauvais).clean()
                self.assertIn('android_minimum_version', leve.exception.message_dict)

    def test_un_seuil_au_dessus_du_magasin_est_REFUSE(self):
        # La règle qui empêche de briquer le parc.
        with self.assertRaises(ValidationError) as leve:
            self._config(
                android_minimum_version='2.0.0',
                android_current_version='1.9.0',
                android_store_url=FICHE_PLAY,
            ).clean()
        self.assertIn('android_minimum_version', leve.exception.message_dict)

    def test_un_seuil_sans_version_de_magasin_est_REFUSE(self):
        """
        Le cas qui brique le parc, et rien ne le signalait.

        La garde anti-brique était gardée par `if courante`, or le formulaire
        présente ce champ comme facultatif : l'ordre naturel - poser le seuil
        d'abord, nommer la version du magasin ensuite - la sautait ENTIÈREMENT.
        Une faute de frappe (`1.30.0` pour `1.3.0`) passait alors, et plus aucun
        terminal ne pouvait s'y conformer.
        """
        with self.assertRaises(ValidationError) as leve:
            self._config(
                android_minimum_version='99.0.0',
                android_current_version='',
                android_store_url=FICHE_PLAY,
            ).clean()
        self.assertIn('android_current_version', leve.exception.message_dict)

    def test_un_numero_hors_bornes_est_REFUSE(self):
        """
        La porte paraissait armée et était INERTE.

        `SEMVER` accepte vingt chiffres (trois segments numériques), mais
        `segments_version` rend `None` au-delà de `MAX_SEGMENT` : la comparaison
        ne s'effectuait pas, la garde ne disait rien, et le seuil descendait sur
        des terminaux où `porte.ts` le rejette pour la même raison. Aucun
        terminal n'était bloqué, et aucune mise à jour réellement obligatoire
        n'était imposée - sans que rien nulle part ne le dise.
        """
        for hors_bornes in ('99999999999999999999.0.0', '1.0.9007199254740992'):
            with self.subTest(valeur=hors_bornes):
                with self.assertRaises(ValidationError) as leve:
                    self._config(
                        android_minimum_version=hors_bornes,
                        android_current_version='1.0.0',
                        android_store_url=FICHE_PLAY,
                    ).clean()
                self.assertIn(
                    'android_minimum_version', leve.exception.message_dict
                )

    def test_un_seuil_EGAL_a_la_version_du_magasin_est_accepte(self):
        # LE CONTRÔLE. Sans lui, on ne saurait pas si les deux règles ci-dessus
        # refusent trop : c'est le cas ORDINAIRE - on publie, puis on exige ce
        # qu'on vient de publier.
        self._config(
            android_minimum_version='1.3.0',
            android_current_version='1.3.0',
            android_store_url=FICHE_PLAY,
        ).clean()

    def test_un_seuil_doit_mener_quelque_part(self):
        # Un mur sans rien dessus est le seul défaut irréparable DEPUIS le terminal.
        #
        # ⚠ La version du magasin est FOURNIE : sans elle, la règle du champ
        # obligatoire lèverait aussi, et ce test passerait pour une raison qui
        # n'est pas la sienne.
        with self.assertRaises(ValidationError) as leve:
            self._config(
                android_minimum_version='1.3.0',
                android_current_version='1.3.0',
            ).clean()
        self.assertIn('android_store_url', leve.exception.message_dict)

    def test_un_message_suffit_quand_il_n_y_a_pas_de_magasin(self):
        # Le cas des terminaux de comptoir sans Play Store : l'adresse n'existe
        # pas, mais le numéro du revendeur, si.
        self._config(
            android_minimum_version='1.3.0',
            android_current_version='1.3.0',
            android_update_message="Appelez le 0990000000 pour recevoir la mise à jour.",
        ).clean()

    def test_aucune_exigence_ne_declenche_aucune_regle(self):
        self._config().clean()


class UrlsMagasinTests(TestCase):
    def test_une_fiche_play_donne_le_raccourci_PUIS_le_repli(self):
        self.assertEqual(
            urls_magasin('android', FICHE_PLAY),
            ['market://details?id=com.cdir.ventefacile', FICHE_PLAY],
        )

    def test_une_adresse_de_revendeur_est_servie_telle_quelle(self):
        # Ni interprétée, ni complétée : c'est ce qui permet de servir un APK
        # direct à un terminal sans magasin, sans publier de version.
        for url in ('https://vente-facile.net/apk/vf-1.3.0.apk',
                    'https://appgallery.huawei.com/app/C123',
                    'pas une url'):
            with self.subTest(url=url):
                self.assertEqual(urls_magasin('android', url), [url])

    def test_ios_n_a_qu_une_adresse(self):
        # `https://apps.apple.com/...` ouvre l'App Store par lien universel :
        # un schéma `itms-apps://` n'ajouterait rien et exigerait une
        # déclaration native que ce projet n'a pas.
        self.assertEqual(urls_magasin('ios', FICHE_IOS), [FICHE_IOS])

    def test_aucune_adresse_donne_une_liste_vide(self):
        # Liste vide = aucun bouton à l'écran. Un bouton qui ne mène nulle part
        # est pire que pas de bouton.
        self.assertEqual(urls_magasin('android', ''), [])
        self.assertEqual(urls_magasin('android', None), [])


class ComparerVersionsTests(TestCase):
    """Le miroir exact de `mobile/vf-marchand/src/features/mise-a-jour/porte.ts`."""

    def test_la_comparaison_est_ENTIERE_et_non_lexicographique(self):
        self.assertEqual(comparer_versions('1.10.0', '1.9.0'), 1)

    def test_les_segments_manquants_valent_ZERO(self):
        self.assertEqual(comparer_versions('1.2', '1.2.0'), 0)
        self.assertEqual(comparer_versions('1.2.0.1', '1.2.0'), 1)

    def test_un_suffixe_est_ignore(self):
        self.assertEqual(comparer_versions('1.2.3-rc.1', '1.2.3'), 0)
        self.assertEqual(comparer_versions('1.2.3 (42)', '1.2.3'), 0)

    def test_l_illisible_rend_None_et_ne_ferme_donc_jamais(self):
        for valeur in ('v1.2.3', '', None, 'abc', '.1.2', '9007199254740993.0.0'):
            with self.subTest(valeur=valeur):
                self.assertIsNone(comparer_versions(valeur, '1.0.0'))
                self.assertIsNone(comparer_versions('1.0.0', valeur))


class PorteWebTests(APITestCase):
    """L'endpoint d'administration plateforme."""

    URL = '/api/v1/platform-admin/app-release/'

    def setUp(self):
        cache.clear()
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.staff = self.owner
        self.staff.is_staff = True
        self.staff.save(update_fields=['is_staff'])

    def test_un_non_staff_est_refuse(self):
        self.client.force_authenticate(user=self.manager)
        self.assertEqual(self.client.get(self.URL).status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_le_staff_lit_et_corrige(self):
        self.client.force_authenticate(user=self.staff)
        self.assertEqual(self.client.get(self.URL).status_code, status.HTTP_200_OK)

        reponse = self.client.patch(self.URL, {
            'android_minimum_version': '1.3.0',
            'android_current_version': '1.3.0',
            'android_store_url': FICHE_PLAY,
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertEqual(GlobalConfig.get().android_minimum_version, '1.3.0')

    def test_la_regle_du_modele_est_opposee_par_l_API_AUSSI(self):
        # Les trois règles ne doivent exister qu'une fois : si ce test tombe,
        # le sérialiseur a cessé d'appeler `clean()`.
        self.client.force_authenticate(user=self.staff)
        reponse = self.client.patch(
            self.URL, {'android_minimum_version': 'v1.2'}, format='json'
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('android_minimum_version', reponse.data)

    def test_un_PATCH_partiel_est_confronte_a_ce_qui_est_DEJA_enregistre(self):
        """
        Le défaut qu'une validation champ par champ laisserait passer : poser un
        seuil au-dessus d'une version de magasin qu'on ne renvoie pas.

        ⚠ ON EXIGE LE CHAMP FAUTIF, PAS UN 400. Mesuré : validé contre une
        instance VIERGE, le même envoi rend aussi 400 - mais sur
        `android_store_url`, parce qu'un seuil sans adresse ne mène nulle part.
        Le test passait alors pour une raison qui n'était pas la sienne.
        """
        config = GlobalConfig.get()
        config.android_current_version = '1.3.0'
        config.android_store_url = FICHE_PLAY
        config.save()

        self.client.force_authenticate(user=self.staff)
        reponse = self.client.patch(
            self.URL, {'android_minimum_version': '9.9.9'}, format='json'
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('android_minimum_version', reponse.data)
