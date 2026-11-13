"""
La porte d'abonnement : un seul verdict, un refus lisible, la grâce qui sert.

┌──────────────────────────────────────────────────────────────────────────────┐
│ TROIS DÉFAUTS SE REFERMENT ICI, ET AUCUN NE SE VOYAIT.                      │
│                                                                              │
│ 1. `SubscriptionMiddleware` devait rendre 402 ; il est INERTE pour toute     │
│    l'API (DRF authentifie le JWT APRÈS les middlewares, donc son            │
│    `request.user.is_authenticated` est toujours faux). Le refus venait donc  │
│    de `HasActiveSubscription`, en 403, que le terminal range en « problème   │
│    d'identité » au lieu de « abonnement à régler ».                          │
│                                                                              │
│ 2. La période de grâce ne valait RIEN : `/subscriptions/status/` annonçait   │
│    « non bloqué, il vous reste N jours » pendant que la permission, adossée  │
│    à `get_active_subscription()` qui exclut `PAST_DUE`, refusait la moindre  │
│    écriture. Le bandeau du back-office promettait ce que le serveur niait.   │
│                                                                              │
│ 3. Le corps d'un refus à détail dictionnaire sort de DRF avec ses booléens   │
│    CONVERTIS EN CHAÎNES. Un client qui compare `is_blocked === true` lisait  │
│    un corps qui a l'air juste et ne l'est pas.                               │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.tests._helpers import make_org_with_users
from apps.subscriptions.models import Subscription
from apps.subscriptions.services import SubscriptionService


@override_settings(DEBUG=False)
class PorteAbonnementTests(APITestCase):
    """
    Le rôle est BORNÉ, jamais `owner` : c'est un gérant qui écrit ici, et la
    porte doit se fermer pour lui comme pour les autres.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        # ⚠ Le verdict est caché ~60 s par organisation. Sans cette purge, un
        # test qui expire un abonnement lirait l'état d'avant.
        cache.clear()
        self.client.force_authenticate(user=self.manager)

    def _entetes(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _echoir(self, il_y_a_jours):
        """Fait passer l'échéance dans le passé, par `save()` donc par signal."""
        sub = Subscription.objects.get(organization=self.org)
        sub.current_period_end = timezone.now() - timedelta(days=il_y_a_jours)
        sub.save(update_fields=['current_period_end', 'updated_at'])
        SubscriptionService.invalidate_org_cache(self.org.id)
        return sub

    def _creer_un_client(self):
        return self.client.post(
            '/api/v1/customers/',
            {'name': 'Client Test', 'phone': '0997000000'},
            format='json',
            **self._entetes(),
        )

    # ------------------------------------------------------------- le refus

    def test_une_ecriture_est_refusee_en_402(self):
        self._echoir(30)
        resp = self._creer_un_client()
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_le_corps_porte_un_code_lisible_par_machine(self):
        """Sans lui, le client n'a que le message à analyser."""
        self._echoir(30)
        resp = self._creer_un_client()
        self.assertEqual(resp.data['code'], 'subscription_required')
        self.assertIn('abonnement', resp.data['detail'].lower())

    def test_is_blocked_est_un_BOOLEEN_et_non_la_chaine_True(self):
        """
        ⚠ Le test qui tient le piège de DRF.

        `APIException` fait traverser tout détail dictionnaire par
        `_get_error_details`, qui rend `ErrorDetail(force_str(v))` : `True`
        deviendrait `"True"` et `0` deviendrait `"0"`. Le corps aurait l'air
        juste, et `is_blocked === true` serait faux côté terminal.
        """
        self._echoir(30)
        resp = self._creer_un_client()
        self.assertIs(resp.data['is_blocked'], True)
        self.assertIsInstance(resp.data['days_remaining'], int)
        self.assertEqual(resp.data['subscription_status'], 'expired')

    def test_un_droit_manquant_repond_toujours_403(self):
        """
        Les deux refus n'appellent pas le même geste : un droit se demande à
        son gérant, un abonnement se paie. Les confondre enverrait chercher la
        solution au mauvais endroit - et, côté journal, ferait ranger en
        quarantaine ce qui n'attend qu'un règlement.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/products/',
            {'name': 'Article', 'selling_price': '100'},
            format='json',
            **self._entetes(),
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------ la lecture

    def test_la_lecture_reste_ouverte(self):
        """Sortir son propre historique ne se monnaie pas."""
        self._echoir(30)
        resp = self.client.get('/api/v1/customers/', **self._entetes())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # -------------------------------------------------------------- la grâce

    def test_la_grace_laisse_ECRIRE(self):
        """
        Le défaut que ce lot referme. `get_active_subscription()` filtre
        `[TRIAL, ACTIVE]` et exclut `PAST_DUE` : pendant la grâce, l'écran
        promettait « il vous reste N jours » et le serveur refusait tout.
        """
        self._echoir(1)  # échu d'hier, donc dans les 3 jours de grâce
        etat = SubscriptionService.get_subscription_status(self.org)
        self.assertEqual(etat['status'], 'past_due')
        self.assertFalse(etat['is_blocked'])

        SubscriptionService.invalidate_org_cache(self.org.id)
        resp = self._creer_un_client()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    # ------------------------------------------------- une source de vérité

    def test_la_permission_et_le_statut_disent_LA_MEME_CHOSE(self):
        """
        Deux surfaces ne doivent pas pouvoir dire deux choses du même
        abonnement. C'est la garantie structurelle, et elle vaut mieux qu'une
        surveillance : la permission LIT le verdict du statut.
        """
        for jours, attendu in [(None, False), (1, False), (30, True)]:
            with self.subTest(echu_depuis=jours):
                if jours is not None:
                    self._echoir(jours)
                cache.clear()
                complet = SubscriptionService.get_subscription_status(self.org)
                cache.clear()
                rapide = SubscriptionService.get_cached_block_state(self.org)
                self.assertEqual(complet['is_blocked'], rapide['is_blocked'])
                self.assertEqual(rapide['is_blocked'], attendu)


@override_settings(DEBUG=False)
class SessionPorteLeVerdictTests(APITestCase):
    """
    Le verdict descend par la session d'APPAREIL, et c'est la seule voie.

    Le terminal ne lit pas `/subscriptions/status/` : il a besoin du verdict
    HORS LIGNE, donc de `access_until`, que seul son réveil lui donne. Et ce
    réveil est la seule réponse mise en cache pour un démarrage à froid.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        cache.clear()

    def _payload(self, user):
        from apps.users.devices import build_session_payload
        return build_session_payload(user, self.org)

    def test_le_caissier_recoit_le_verdict(self):
        """Tout l'objet de l'exercice : c'est lui qui tient le terminal."""
        bloc = self._payload(self.cashier_a)['subscription']
        self.assertIn('is_blocked', bloc)
        self.assertIs(bloc['is_blocked'], False)
        self.assertEqual(bloc['status'], 'active')

    def test_can_manage_suit_le_role(self):
        """
        `moko_initiate` est `IsTenantOwner`. Sans ce drapeau, l'écran offrirait
        à un caissier de payer, et le serveur refuserait après la saisie du
        numéro de téléphone.
        """
        self.assertTrue(self._payload(self.owner)['subscription']['can_manage'])
        for u in (self.manager, self.cashier_a):
            with self.subTest(user=u.email):
                self.assertFalse(self._payload(u)['subscription']['can_manage'])

    def test_access_until_porte_la_grace(self):
        """
        C'est la date que le terminal JUGE hors ligne. Sans la grâce, il
        fermerait la porte avant le serveur.
        """
        from apps.subscriptions.models import GlobalConfig
        sub = Subscription.objects.get(organization=self.org)
        grace = GlobalConfig.get().grace_period_days

        bloc = self._payload(self.owner)['subscription']
        attendu = sub.current_period_end + timedelta(days=grace)
        self.assertEqual(bloc['access_until'], attendu)

    def test_un_abonnement_echu_descend_bloque(self):
        sub = Subscription.objects.get(organization=self.org)
        sub.current_period_end = timezone.now() - timedelta(days=30)
        sub.save(update_fields=['current_period_end', 'updated_at'])

        bloc = self._payload(self.cashier_a)['subscription']
        self.assertIs(bloc['is_blocked'], True)
        self.assertEqual(bloc['status'], 'expired')
        self.assertTrue(bloc['message'])

    def test_sans_abonnement_access_until_est_nulle(self):
        """`null` ne se lit jamais comme une date : on ne fabrique pas d'échéance."""
        Subscription.objects.filter(organization=self.org).delete()
        bloc = self._payload(self.owner)['subscription']
        self.assertIsNone(bloc['access_until'])
        self.assertIs(bloc['is_blocked'], True)
        self.assertEqual(bloc['status'], 'none')


@override_settings(DEBUG=False)
class StatutLisibleParTousTests(APITestCase):
    """
    Le VERDICT d'abonnement se lit par tout membre ; son DÉTAIL par le seul
    propriétaire.

    Réservé à `subscription.view`, `/subscriptions/status/` répondait 403 à un
    caissier : le back-office ne lui montrait jamais « bloqué », il continuait
    de saisir pour se faire refuser en 402, et chaque page journalisait une
    erreur client. Le rôle éprouvé est donc BORNÉ.
    """

    URL = '/api/v1/subscriptions/status/'

    def setUp(self):
        self.__dict__.update(make_org_with_users())
        cache.clear()

    def _lire(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get(self.URL, HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def test_un_caissier_lit_le_verdict_sans_le_detail(self):
        reponse = self._lire(self.cashier_a)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertFalse(reponse.data['is_blocked'])
        self.assertFalse(reponse.data['can_manage'])
        # Le nom du plan et l'essai, rien de plus : c'est ce que le terminal
        # reçoit déjà de son réveil, pour tous les rôles.
        self.assertEqual(set(reponse.data['subscription']), {'plan_name', 'is_trial'})
        self.assertNotIn('quotas', reponse.data)
        self.assertNotIn('plan', reponse.data)

    def test_un_caissier_SE_SAIT_bloque(self):
        """La raison d'être du lot : sans elle, le tableau de bord ne se fermait jamais pour lui."""
        sub = Subscription.objects.get(organization=self.org)
        sub.current_period_end = timezone.now() - timedelta(days=30)
        sub.save(update_fields=['current_period_end', 'updated_at'])
        SubscriptionService.invalidate_org_cache(self.org.id)
        reponse = self._lire(self.cashier_a)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertTrue(reponse.data['is_blocked'])

    def test_le_proprietaire_garde_tout_le_detail(self):
        reponse = self._lire(self.owner)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertTrue(reponse.data['can_manage'])
        self.assertIn('quotas', reponse.data)
        self.assertIn('price', reponse.data['subscription'])

    def test_le_detail_suit_la_PERMISSION_pas_le_role(self):
        """
        Un marchand peut accorder `subscription.view` à son gérant : il lit
        alors le détail. Il ne peut pas pour autant régler, `moko_initiate`
        restant réservé au propriétaire.
        """
        adhesion = self.manager.memberships.get(organization=self.org)
        adhesion.extra_permissions = ['subscription.view']
        adhesion.save(update_fields=['extra_permissions'])
        reponse = self._lire(self.manager)
        self.assertIn('quotas', reponse.data)
        self.assertFalse(reponse.data['can_manage'])
