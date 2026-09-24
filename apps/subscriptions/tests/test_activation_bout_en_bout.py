"""
Après le paiement, l'abonnement existe VRAIMENT - et la porte se rouvre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUI EST ÉPROUVÉ ICI N'EST PAS L'ACTIVATION, C'EST LA CHAÎNE ENTIÈRE.     │
│                                                                              │
│ `test_moko_activation.py` couvre finement `complete_pending_moko_payment` -  │
│ ses replis, son idempotence, sa réparation d'un `FAILED`. Mais personne ne   │
│ vérifiait le PARCOURS : lancer un paiement par l'API, le faire confirmer,    │
│ et constater qu'un abonnement est né, qu'il est actif, qu'une facture existe │
│ et que le marchand peut de nouveau écrire.                                   │
│                                                                              │
│ C'est pourtant la seule question que se pose un marchand qui vient de payer, │
│ et le défaut de production qu'on redoute - « l'argent est prélevé, rien ne   │
│ s'active » - vit dans les jointures, pas dans les fonctions.                 │
└──────────────────────────────────────────────────────────────────────────────┘

Les DEUX surfaces empruntent ce chemin : le back-office et le terminal appellent
les mêmes `moko/initiate/` et `moko/status/`. Ce qui est vérifié ici vaut donc
pour l'un comme pour l'autre.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization, OrganizationMembership
from apps.settings.models import Currency
from apps.subscriptions.models import Invoice, Plan, Subscription, SubscriptionPayment
from apps.subscriptions.services import SubscriptionService

User = get_user_model()


def _reponse_moko_ok(transaction_id='pd-test-1'):
    """Un accusé d'initiation accepté par `initiate_payment_v2`."""
    return 200, {'Status': 'success', 'Transaction_id': transaction_id}


def _reponse_statut(statut):
    return 200, {'payment': {'status': statut, 'reference': 'ignored'}}


@override_settings(DEBUG=False, MOKO_CALLBACK_SECRET='')
class PaiementCreeUnAbonnementTests(APITestCase):
    """
    Le parcours nominal, de bout en bout, par l'API.
    """

    def setUp(self):
        suf = uuid.uuid4().hex[:8]
        self.devise = Currency.objects.filter(code='USD').first() or (
            Currency.objects.create(code='USD', name='US Dollar', symbol='$')
        )
        self.org = Organization.objects.create(name=f'Org {suf}', slug=f'org-{suf}')
        self.proprietaire = User.objects.create_user(
            email=f'own{suf}@vf.test', password='pw12345!', first_name='Own', last_name='Er',
        )
        self.caissier = User.objects.create_user(
            email=f'cash{suf}@vf.test', password='pw12345!', first_name='Cash', last_name='Ier',
        )
        for u, role in (
            (self.proprietaire, OrganizationMembership.Role.OWNER),
            (self.caissier, OrganizationMembership.Role.CASHIER),
        ):
            OrganizationMembership.objects.create(
                organization=self.org, user=u, role=role, is_active=True,
            )
        self.plan = Plan.objects.create(
            name='Standard', code=f'std-{suf}', description='',
            price_monthly=Decimal('10'), price_yearly=Decimal('100'),
            currency=self.devise, tier=2, is_active=True,
        )
        cache.clear()
        self.client.force_authenticate(user=self.proprietaire)

    def _entetes(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _lancer_paiement(self, cycle='monthly', mode='new'):
        with patch(
            'apps.subscriptions.views.initiate_payment_v2',
            return_value=_reponse_moko_ok(),
        ):
            return self.client.post(
                '/api/v1/subscriptions/moko/initiate/',
                {
                    'plan_id': str(self.plan.id),
                    'billing_cycle': cycle,
                    'mode': mode,
                    'method': 'airtel',
                    'customer_number': '0997057917',
                },
                format='json',
                **self._entetes(),
            )

    def _confirmer(self, reference):
        """Confirme par la voie du SONDAGE, celle des deux surfaces."""
        with patch(
            'apps.subscriptions.views.get_payment_status_v2',
            return_value=_reponse_statut('Successful'),
        ):
            return self.client.get(
                f'/api/v1/subscriptions/moko/status/?reference={reference}',
                **self._entetes(),
            )

    # ------------------------------------------------------- le parcours

    def test_le_paiement_cree_un_abonnement_ACTIF(self):
        """La question que se pose le marchand : « suis-je abonné ? »"""
        self.assertFalse(Subscription.objects.filter(organization=self.org).exists())

        lancement = self._lancer_paiement()
        self.assertEqual(lancement.status_code, status.HTTP_200_OK, lancement.data)
        reference = lancement.data['reference']
        # Rien n'est encore acquis : Moko n'a pas confirmé.
        self.assertFalse(Subscription.objects.filter(organization=self.org).exists())

        confirmation = self._confirmer(reference)
        self.assertEqual(confirmation.data['status'], 'completed', confirmation.data)
        self.assertTrue(confirmation.data['subscription_activated'])

        sub = Subscription.objects.get(organization=self.org)
        self.assertEqual(sub.plan_id, self.plan.id)
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)
        self.assertTrue(sub.is_active)
        self.assertGreater(sub.current_period_end, timezone.now())

    def test_le_reglement_est_SOLDE_et_rattache_a_son_abonnement(self):
        """
        Un paiement `completed` mais `subscription=None` est le symptôme exact
        du défaut de production : l'argent est pris, rien ne le relie à rien.
        """
        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)

        paiement = SubscriptionPayment.objects.get(reference=reference)
        self.assertEqual(paiement.status, SubscriptionPayment.Status.COMPLETED)
        self.assertIsNotNone(paiement.paid_at)
        self.assertIsNotNone(paiement.subscription_id)
        self.assertEqual(
            paiement.subscription_id,
            Subscription.objects.get(organization=self.org).id,
        )

    def test_une_facture_est_emise(self):
        """Le marchand doit pouvoir justifier la dépense."""
        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)

        facture = Invoice.objects.get(organization=self.org)
        self.assertTrue(facture.invoice_number)
        self.assertEqual(facture.total, Decimal('10.00'))

    def test_le_plancher_anti_retrogradation_monte(self):
        """
        Sans lui, le marchand pourrait redescendre sous ce qu'il utilise déjà,
        et le serveur retirerait des entrepôts ou des produits DÉJÀ créés.
        """
        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)

        self.org.refresh_from_db()
        self.assertGreaterEqual(self.org.subscription_floor_tier, self.plan.tier)

    def test_le_cycle_ANNUEL_donne_bien_une_annee(self):
        """
        Le montant et la durée viennent du cycle : une confusion ferait payer
        un an pour trente jours.
        """
        reference = self._lancer_paiement(cycle='yearly').data['reference']
        paiement = SubscriptionPayment.objects.get(reference=reference)
        self.assertEqual(paiement.amount, Decimal('100.00'))

        self._confirmer(reference)
        sub = Subscription.objects.get(organization=self.org)
        self.assertEqual(sub.billing_cycle, Plan.BillingCycle.YEARLY)
        self.assertGreater(sub.current_period_end, timezone.now() + timedelta(days=300))

    # --------------------------------------------------------- la porte

    def test_LA_PORTE_SE_ROUVRE_sans_attendre_le_cache(self):
        """
        ⚠ Le contrôle qui relie le paiement à l'application.

        Le verdict de blocage est caché ~60 s. Si l'activation ne purgeait pas
        ce cache, le marchand paierait et resterait dehors jusqu'à une minute -
        devant ses clients, sans rien comprendre, à réappuyer sur « Actualiser ».
        Les signaux de `Subscription` s'en chargent ; ce test l'exige.
        """
        # On amorce le cache sur l'état bloqué, comme une requête réelle le ferait.
        self.assertTrue(SubscriptionService.get_cached_block_state(self.org)['is_blocked'])

        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)

        # Aucun `cache.clear()` ici : c'est tout l'objet du test.
        self.assertFalse(SubscriptionService.get_cached_block_state(self.org)['is_blocked'])

    def test_le_marchand_peut_de_nouveau_ECRIRE(self):
        """La preuve par l'usage : une écriture refusée en 402, puis acceptée."""
        refuse = self.client.post(
            '/api/v1/customers/',
            {'name': 'Client', 'phone': '0997000000'},
            format='json',
            **self._entetes(),
        )
        self.assertEqual(refuse.status_code, status.HTTP_402_PAYMENT_REQUIRED)

        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)

        accepte = self.client.post(
            '/api/v1/customers/',
            {'name': 'Client', 'phone': '0997000000'},
            format='json',
            **self._entetes(),
        )
        self.assertEqual(accepte.status_code, status.HTTP_201_CREATED, accepte.data)

    def test_le_TERMINAL_voit_le_nouvel_etat_des_le_reveil_suivant(self):
        """
        Le verdict descend par la session d'appareil, pour TOUS les rôles - la
        seule voie qu'un caissier reçoive. Sans cela, un terminal paierait et
        garderait son voile jusqu'à une reconnexion.
        """
        from apps.users.devices import build_session_payload

        avant = build_session_payload(self.caissier, self.org)['subscription']
        self.assertTrue(avant['is_blocked'])

        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)

        apres = build_session_payload(self.caissier, self.org)['subscription']
        self.assertFalse(apres['is_blocked'])
        self.assertEqual(apres['status'], 'active')
        self.assertIsNotNone(apres['access_until'])

    # ------------------------------------------------------ prolongation

    def test_une_prolongation_REPOUSSE_l_echeance_au_lieu_de_la_remplacer(self):
        """
        Prolonger doit ajouter du temps à ce qui reste. Repartir de zéro ferait
        perdre au marchand les jours qu'il avait déjà payés.
        """
        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)
        premiere_fin = Subscription.objects.get(organization=self.org).current_period_end

        ref2 = self._lancer_paiement(mode='extend').data['reference']
        self._confirmer(ref2)

        sub = Subscription.objects.get(organization=self.org)
        self.assertGreater(sub.current_period_end, premiere_fin)
        self.assertEqual(SubscriptionPayment.objects.filter(
            organization=self.org, status=SubscriptionPayment.Status.COMPLETED,
        ).count(), 2)

    def test_une_seconde_confirmation_ne_double_RIEN(self):
        """
        Les trois voies de confirmation (sondage, webhook, tâche) peuvent
        arriver ensemble. Deux abonnements, ou deux factures, pour un seul
        paiement seraient pires qu'un retard.
        """
        reference = self._lancer_paiement().data['reference']
        self._confirmer(reference)
        fin = Subscription.objects.get(organization=self.org).current_period_end

        self._confirmer(reference)

        self.assertEqual(Subscription.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(Invoice.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(
            Subscription.objects.get(organization=self.org).current_period_end, fin,
        )


@override_settings(DEBUG=False, MOKO_CALLBACK_SECRET='')
class ConversionDEssaiTests(APITestCase):
    """
    Un essai se convertit en abonnement payant AVANT son terme.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ PERSONNE NE POUVAIT PAYER PENDANT SON ESSAI, ET LE PRODUIT LE PROMETTAIT.│
    │                                                                          │
    │ La règle « pendant la période en cours, seul un palier strictement       │
    │ supérieur » vise les changements d'offre au milieu d'un mois déjà réglé. │
    │ Appliquée à un essai, elle fermait tout : le plan d'essai et le premier  │
    │ plan payant partagent le palier 1, donc aucun plan n'était supérieur.    │
    │                                                                          │
    │ Le marchand devait attendre l'expiration, se faire bloquer, et payer     │
    │ seulement là - pendant que le bandeau du back-office lui proposait       │
    │ « Passer au payant » depuis le premier jour.                             │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        suf = uuid.uuid4().hex[:8]
        self.devise = Currency.objects.filter(code='USD').first() or (
            Currency.objects.create(code='USD', name='US Dollar', symbol='$')
        )
        self.org = Organization.objects.create(name=f'Org {suf}', slug=f'org-{suf}')
        self.proprietaire = User.objects.create_user(
            email=f'own{suf}@vf.test', password='pw12345!', first_name='O', last_name='W',
        )
        OrganizationMembership.objects.create(
            organization=self.org, user=self.proprietaire,
            role=OrganizationMembership.Role.OWNER, is_active=True,
        )
        # Les deux plans partagent le palier 1, comme en production.
        self.essai = Plan.objects.create(
            name='Essai Gratuit', code=f'trial-{suf}', description='',
            price_monthly=Decimal('0'), price_yearly=Decimal('0'),
            currency=self.devise, tier=1, is_active=True,
        )
        self.payant = Plan.objects.create(
            name='Standard', code=f'std-{suf}', description='',
            price_monthly=Decimal('3'), price_yearly=Decimal('30'),
            currency=self.devise, tier=1, is_active=True,
        )
        Subscription.objects.create(
            organization=self.org, plan=self.essai,
            status=Subscription.Status.TRIAL,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=10),
        )
        cache.clear()
        self.client.force_authenticate(user=self.proprietaire)

    def _verdict(self, plan, mode='new'):
        return SubscriptionService.evaluate_checkout(self.org, plan, mode=mode)

    def test_le_plan_payant_est_ACCESSIBLE_pendant_l_essai(self):
        verdict = self._verdict(self.payant)
        self.assertTrue(verdict['allowed'], verdict)

    def test_on_ne_relance_pas_un_essai_gratuit_par_dessus_un_essai(self):
        """Sans ce contrôle, l'essai se renouvellerait indéfiniment."""
        verdict = self._verdict(self.essai)
        self.assertFalse(verdict['allowed'])
        self.assertEqual(verdict['reason_code'], 'SUBSCRIPTION_TRIAL_ALREADY_RUNNING')

    def test_un_essai_ne_se_PROLONGE_pas(self):
        """Le prolonger donnerait du temps gratuit, pas un abonnement."""
        verdict = self._verdict(self.payant, mode='extend')
        self.assertFalse(verdict['allowed'])
        self.assertEqual(verdict['reason_code'], 'SUBSCRIPTION_TRIAL_NOT_EXTENDABLE')

    def test_le_paiement_ABOUTIT_et_remplace_l_essai(self):
        """La preuve de bout en bout : c'est le parcours que le marchand suit."""
        with patch(
            'apps.subscriptions.views.initiate_payment_v2',
            return_value=_reponse_moko_ok(),
        ):
            lancement = self.client.post(
                '/api/v1/subscriptions/moko/initiate/',
                {
                    'plan_id': str(self.payant.id), 'billing_cycle': 'monthly',
                    'mode': 'new', 'method': 'airtel', 'customer_number': '0997057917',
                },
                format='json',
                HTTP_X_ORGANIZATION_ID=str(self.org.id),
            )
        self.assertEqual(lancement.status_code, status.HTTP_200_OK, lancement.data)

        with patch(
            'apps.subscriptions.views.get_payment_status_v2',
            return_value=_reponse_statut('Successful'),
        ):
            self.client.get(
                f'/api/v1/subscriptions/moko/status/?reference={lancement.data["reference"]}',
                HTTP_X_ORGANIZATION_ID=str(self.org.id),
            )

        courant = Subscription.objects.filter(
            organization=self.org, status=Subscription.Status.ACTIVE,
        ).get()
        self.assertEqual(courant.plan_id, self.payant.id)
        self.assertFalse(courant.is_trial)

    def test_une_PÉRIODE_PAYÉE_garde_sa_règle_d_upgrade(self):
        """
        ⚠ La frontière. Relâcher la règle pour un abonnement PAYANT en cours
        rouvrirait les changements d'offre au milieu d'un mois déjà réglé.
        """
        Subscription.objects.filter(organization=self.org).update(
            status=Subscription.Status.ACTIVE, plan=self.payant,
        )
        SubscriptionService.invalidate_org_cache(self.org.id)

        autre = Plan.objects.create(
            name='Autre', code=f'oth-{uuid.uuid4().hex[:6]}', description='',
            price_monthly=Decimal('5'), price_yearly=Decimal('50'),
            currency=self.devise, tier=1, is_active=True,
        )
        verdict = self._verdict(autre)
        self.assertFalse(verdict['allowed'])
        self.assertEqual(verdict['reason_code'], 'SUBSCRIPTION_UPGRADE_REQUIRED')
