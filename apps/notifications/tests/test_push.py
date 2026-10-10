"""
L'envoi poussé : il fait sonner, il ne porte pas l'information.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUI SE PASSE QUAND LE SERVICE EST INJOIGNABLE EST LA MOITIÉ DU SUJET.    │
│                                                                              │
│ Une approbation d'ajustement ne se défait pas parce qu'un téléphone n'a pas  │
│ sonné. Toutes les erreurs sont donc avalées, et ces tests le VÉRIFIENT :    │
│ c'est le genre de garantie qu'on croit tenir et qui se perd au premier       │
│ refactor.                                                                    │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.notifications.models import Alert, Notification
from apps.notifications.push import envoyer, pousser_notifications
from apps.notifications.services import alerter
from apps.organizations.models import OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users
from apps.users.models import Device

JETON = 'ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]'


class FormeDuJetonTests(TestCase):
    """Un jeton malformé ne part pas : il ferait répondre 400 pour TOUT le lot."""

    def test_un_jeton_malforme_est_ECARTE_avant_l_appel(self):
        with patch('apps.notifications.push._envoyer_un_lot') as envoi:
            self.assertEqual(envoyer([{'to': 'pas-un-jeton', 'title': 'x'}]), 0)
            envoi.assert_not_called()

    def test_un_jeton_valide_part(self):
        with patch('apps.notifications.push._envoyer_un_lot', return_value=True):
            self.assertEqual(envoyer([{'to': JETON, 'title': 'x'}]), 1)

    def test_UNE_adresse_abimee_ne_fait_pas_taire_les_autres(self):
        """
        Le motif du contrôle : Expo refuse le lot entier sur un seul jeton
        malformé, donc une adresse abîmée ferait taire les autres.
        """
        with patch('apps.notifications.push._envoyer_un_lot', return_value=True) as envoi:
            envoyer([
                {'to': JETON, 'title': 'a'},
                {'to': '', 'title': 'b'},
                {'to': JETON, 'title': 'c'},
            ])
            lot = envoi.call_args[0][0]
            self.assertEqual(len(lot), 2)

    def test_au_dela_de_cent_messages_on_DECOUPE(self):
        """Expo accepte cent messages par requête."""
        with patch('apps.notifications.push._envoyer_un_lot', return_value=True) as envoi:
            envoyer([{'to': JETON, 'title': str(i)} for i in range(250)])
            self.assertEqual(envoi.call_count, 3)


class AdressageTests(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

        self.appareil = Device.objects.create(
            user=self.manager, organization=self.org, name='POS 1',
            platform='android', device_code='AAAA', token_hash='x' * 64,
            expires_at=timezone.now() + timezone.timedelta(days=30),
            push_token=JETON,
        )

    def _alerte(self):
        return alerter(
            self.org,
            alert_type=Alert.AlertType.LOW_STOCK, severity=Alert.Severity.HIGH,
            title='Stock bas', message='Rayon sous son seuil',
            data={'warehouse_id': str(self.warehouse.id)},
        )

    def test_la_livraison_en_base_DECLENCHE_le_push(self):
        with patch('apps.notifications.push.envoyer', return_value=1) as envoi:
            self._alerte()
            self.assertTrue(envoi.called)
            messages = envoi.call_args[0][0]
            self.assertEqual(messages[0]['to'], JETON)
            self.assertEqual(messages[0]['title'], 'Stock bas')

    def test_le_corps_est_TRONQUE(self):
        """
        Une notification système se tronque de toute façon, et le message
        complet est dans l'application. Le push est un RAPPEL.
        """
        long = 'x' * 500
        alerte = Alert.objects.create(
            organization=self.org, alert_type=Alert.AlertType.LOW_STOCK,
            severity=Alert.Severity.HIGH, title='T', message=long,
            data={'warehouse_id': str(self.warehouse.id)},
        )
        from apps.notifications.services import notifier

        with patch('apps.notifications.push.envoyer', return_value=1) as envoi:
            notifier(alerte)
            self.assertLessEqual(len(envoi.call_args[0][0][0]['body']), 140)

    def test_un_appareil_REVOQUE_ne_sonne_plus(self):
        self.appareil.revoked_at = timezone.now()
        self.appareil.save(update_fields=['revoked_at'])
        with patch('apps.notifications.push.envoyer', return_value=0) as envoi:
            self._alerte()
            self.assertEqual(envoi.call_args[0][0], [])

    def test_un_appareil_SANS_jeton_ne_sonne_pas(self):
        self.appareil.push_token = ''
        self.appareil.save(update_fields=['push_token'])
        with patch('apps.notifications.push.envoyer', return_value=0) as envoi:
            self._alerte()
            self.assertEqual(envoi.call_args[0][0], [])

    def test_une_alerte_d_une_AUTRE_organisation_ne_sonne_pas_ici(self):
        """
        La même personne peut tenir un terminal chez deux marchands : une
        rupture de la boutique A n'a rien à faire sur celui de B.
        """
        # ⚠ `make_org_with_users` ne prend AUCUN argument et emploie des
        # adresses FIXES : un second appel heurte `users_email_key`. La seconde
        # organisation se monte donc à la main, avec le MÊME propriétaire -
        # c'est tout le cas de figure.
        from apps.organizations.models import Organization

        ailleurs = Organization.objects.create(name='Chez B', slug='chez-b')
        OrganizationMembership.objects.create(
            user=self.manager, organization=ailleurs,
            role=OrganizationMembership.Role.OWNER, is_active=True,
        )

        with patch('apps.notifications.push.envoyer', return_value=0) as envoi:
            alerter(
                ailleurs,
                alert_type=Alert.AlertType.SYSTEM, severity=Alert.Severity.LOW,
                title='Ailleurs', message='Autre établissement',
            )
            self.assertEqual([m['to'] for m in envoi.call_args[0][0]], [])

    def test_l_envoi_qui_LEVE_ne_defait_PAS_l_acte(self):
        """
        ⚠ LA GARANTIE CENTRALE. Un service injoignable ne doit pas annuler une
        approbation d'ajustement : la notification est déjà en base quand on
        pousse.
        """
        with patch(
            'apps.notifications.push.pousser_notifications',
            side_effect=RuntimeError('service injoignable'),
        ):
            alerte = self._alerte()

        self.assertTrue(Alert.objects.filter(id=alerte.id).exists())
        self.assertTrue(
            Notification.objects.filter(data__alert_id=str(alerte.id)).exists()
        )


class VerdictDExpoTests(TestCase):
    """Expo rend un verdict PAR MESSAGE : c'est la seule façon de savoir."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.appareil = Device.objects.create(
            user=self.manager, organization=self.org, name='POS 1',
            platform='android', device_code='AAAA', token_hash='x' * 64,
            expires_at=timezone.now() + timezone.timedelta(days=30),
            push_token=JETON,
        )

    def _reponse(self, charge):
        class Fausse:
            def read(self):
                return json.dumps(charge).encode('utf-8')

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Fausse()

    def test_un_jeton_MORT_est_detache(self):
        """
        Sans ce détachement, on pousse dans le vide à chaque alerte,
        indéfiniment.
        """
        charge = {'data': [{
            'status': 'error', 'message': 'not registered',
            'details': {'error': 'DeviceNotRegistered'},
        }]}
        with patch('urllib.request.urlopen', return_value=self._reponse(charge)):
            envoyer([{'to': JETON, 'title': 'x'}])

        self.appareil.refresh_from_db()
        self.assertEqual(self.appareil.push_token, '')

    def test_l_appareil_N_EST_PAS_revoque_pour_autant(self):
        """
        ⚠ Une application désinstallée rend `DeviceNotRegistered`, mais le
        terminal peut être réinstallé demain avec ses ventes en file : révoquer
        sa session l'enfermerait dehors.
        """
        charge = {'data': [{
            'status': 'error', 'details': {'error': 'DeviceNotRegistered'},
        }]}
        with patch('urllib.request.urlopen', return_value=self._reponse(charge)):
            envoyer([{'to': JETON, 'title': 'x'}])

        self.appareil.refresh_from_db()
        self.assertIsNone(self.appareil.revoked_at)

    def test_un_service_injoignable_ne_LEVE_pas(self):
        import urllib.error

        with patch(
            'urllib.request.urlopen',
            side_effect=urllib.error.URLError('injoignable'),
        ):
            self.assertEqual(envoyer([{'to': JETON, 'title': 'x'}]), 0)

    def test_une_reponse_illisible_ne_LEVE_pas(self):
        class Cassee:
            def read(self):
                return b'<html>502 Bad Gateway</html>'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch('urllib.request.urlopen', return_value=Cassee()):
            self.assertEqual(envoyer([{'to': JETON, 'title': 'x'}]), 0)


class JetonNeDescendPasTests(TestCase):
    """
    ⚠ L'ADRESSE DE NOTIFICATION NE DESCEND SUR AUCUN TERMINAL.

    `devices` est tirée BORNÉE ET AMPUTÉE depuis le lot 10 : un terminal se
    perd, sa base n'étant pas chiffrée. Le jeton de push n'authentifie rien -
    il ne donne accès à aucune donnée - mais quiconque le détient peut faire
    sonner le téléphone d'un collègue, et cela n'a aucune raison de voyager.
    """

    def test_push_token_n_est_pas_au_manifeste_de_tirage(self):
        from apps.sync.pull import PULL_TABLES

        table = next(t for t in PULL_TABLES if t.name == 'devices')
        self.assertIsNotNone(
            table.fields,
            "`devices` doit rester AMPUTÉE : sans liste de champs, toute "
            "colonne ajoutée au modèle descend sur chaque terminal.",
        )
        self.assertNotIn('push_token', table.fields)
        self.assertNotIn('push_refused_at', table.fields)
        # Et le contrôle positif : la liste décrit bien cette table.
        self.assertIn('device_code', table.fields)


class EnregistrementDuJetonTests(TestCase):
    """Le jeton voyage avec le RÉVEIL, et une adresse ne sert qu'un appareil."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.appareil = Device.objects.create(
            user=self.manager, organization=self.org, name='POS 1',
            platform='android', device_code='AAAA', token_hash='x' * 64,
            expires_at=timezone.now() + timezone.timedelta(days=30),
        )

    def _poser(self, **donnees):
        from apps.users.devices import _enregistrer_jeton_push

        _enregistrer_jeton_push(self.appareil, donnees)
        self.appareil.refresh_from_db()

    def test_un_jeton_neuf_est_range(self):
        self._poser(push_token=JETON)
        self.assertEqual(self.appareil.push_token, JETON)

    def test_un_REFUS_se_note_et_ne_se_confond_pas_avec_un_vide(self):
        """
        Vide veut dire « on n'a pas encore demandé » ; un refus noté veut dire
        « on a demandé, et c'est non ». Les confondre ferait redemander à chaque
        démarrage sur un appareil qui a déjà dit non.
        """
        self._poser(push_refused=True)
        self.assertEqual(self.appareil.push_token, '')
        self.assertIsNotNone(self.appareil.push_refused_at)

    def test_UNE_ADRESSE_NE_SERT_QU_UN_APPAREIL(self):
        """
        Expo réattribue une adresse quand une application est réinstallée.
        Sans ce détachement, l'ancien propriétaire recevrait les alertes du
        nouveau - les ruptures et les créances d'un établissement qui n'est
        plus le sien.
        """
        ancien = Device.objects.create(
            user=self.cashier_a, organization=self.org, name='POS 2',
            platform='android', device_code='BBBB', token_hash='y' * 64,
            expires_at=timezone.now() + timezone.timedelta(days=30),
            push_token=JETON,
        )
        self._poser(push_token=JETON)

        ancien.refresh_from_db()
        self.assertEqual(ancien.push_token, '')
        self.assertEqual(self.appareil.push_token, JETON)

    def test_un_reveil_SANS_changement_n_ecrit_rien(self):
        """
        `touch()` vient de poser `last_seen_at` ; repousser le jeton à chaque
        réveil ferait un `UPDATE` par battement sur une table que le tirage
        pagine.
        """
        self._poser(push_token=JETON)
        avant = self.appareil.updated_at
        self._poser(push_token=JETON)
        self.assertEqual(self.appareil.updated_at, avant)
