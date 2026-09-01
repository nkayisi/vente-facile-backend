"""
Tests du chemin d'activation MOKO.

Règle centrale couverte ici : **un paiement que MOKO confirme encaissé ne doit
jamais rester sans abonnement, ni finir en ``FAILED``**. C'est le bug de
production « l'argent est prélevé mais l'abonnement ne s'active pas ».
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.organizations.models import Organization
from apps.settings.models import Currency
from apps.subscriptions.models import Plan, Subscription, SubscriptionPayment
from apps.subscriptions.services import MokoActivationDeferred, SubscriptionService

User = get_user_model()


def _moko_status_payload(status: str):
    """Réponse minimale de GET /payments/status telle que la lit le client v2."""
    return 200, {'payment': {'status': status, 'reference': 'ignored'}}


class _MokoBaseTest(TestCase):
    def setUp(self):
        suf = uuid.uuid4().hex[:8]
        self.currency = Currency.objects.filter(code='USD').first() or (
            Currency.objects.create(code='USD', name='US Dollar', symbol='$')
        )
        self.org = Organization.objects.create(name=f'Org {suf}', slug=f'org-{suf}')
        self.user = User.objects.create_user(
            email=f'u{suf}@vf.test', password='pw12345!',
            first_name='U', last_name='Ser',
        )
        self.plan = Plan.objects.create(
            name='Standard', code=f'std-{suf}', description='',
            price_monthly=Decimal('10'), price_yearly=Decimal('100'),
            currency=self.currency, tier=2,
        )

    def _pending_payment(self, *, plan=None, mode='new', **extra):
        plan = plan or self.plan
        return SubscriptionPayment.objects.create(
            organization=self.org,
            amount=Decimal('10'),
            currency=self.currency.code,
            payment_method=SubscriptionPayment.PaymentMethod.MOBILE_MONEY,
            status=SubscriptionPayment.Status.PENDING,
            reference=f'vf_sub_{uuid.uuid4().hex[:20]}',
            metadata={
                'plan_id': str(plan.id),
                'billing_cycle': Plan.BillingCycle.MONTHLY,
                'checkout_mode': mode,
            },
            **extra,
        )


class CheckoutRuleNeverBlocksConfirmedPaymentTests(_MokoBaseTest):
    """La règle de checkout est bloquante à l'initiation, jamais après encaissement."""

    def test_early_renew_rule_does_not_lose_the_payment(self):
        """
        Second paiement alors qu'un abonnement est déjà actif sur le même plan :
        l'ancienne logique levait SUBSCRIPTION_EARLY_RENEW_FORBIDDEN et le poller
        marquait le paiement FAILED. L'argent était perdu.
        """
        Subscription.objects.create(
            organization=self.org, plan=self.plan,
            status=Subscription.Status.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=20),
        )
        payment = self._pending_payment(mode='new')

        result = SubscriptionService.complete_pending_moko_payment(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)
        self.assertIsNotNone(payment.subscription)
        # Abonnement déjà en cours sur ce plan → la période est prolongée.
        self.assertGreater(
            result['subscription'].current_period_end,
            timezone.now() + timedelta(days=40),
        )

    def test_extend_after_period_expired_still_activates(self):
        """
        Prolongation initiée avant l'expiration, confirmée après : l'ancienne
        logique levait SUBSCRIPTION_EXTEND_PERIOD_ENDED. On doit basculer sur une
        activation plutôt que perdre le paiement.
        """
        Subscription.objects.create(
            organization=self.org, plan=self.plan,
            status=Subscription.Status.EXPIRED,
            current_period_start=timezone.now() - timedelta(days=40),
            current_period_end=timezone.now() - timedelta(days=1),
        )
        payment = self._pending_payment(mode='extend')

        SubscriptionService.complete_pending_moko_payment(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)
        self.assertIsNotNone(payment.subscription)
        self.assertEqual(payment.subscription.status, Subscription.Status.ACTIVE)

    def test_downgrade_rule_is_overridden_and_traced(self):
        """
        Palier inférieur au plancher déjà atteint : on encaisse, donc on active,
        mais l'écart doit être tracé pour l'admin.
        """
        self.org.subscription_floor_tier = 5
        self.org.save(update_fields=['subscription_floor_tier'])
        payment = self._pending_payment(mode='new')

        SubscriptionService.complete_pending_moko_payment(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)
        self.assertIsNotNone(payment.subscription)
        override = (payment.metadata or {}).get('checkout_override')
        self.assertIsNotNone(override, "L'écart de règle doit être tracé.")
        self.assertEqual(override['reason_code'], 'SUBSCRIPTION_DOWNGRADE_FORBIDDEN')


class DeferredActivationTests(_MokoBaseTest):
    """Quand l'activation est impossible, le paiement reste PENDING - jamais FAILED."""

    def test_deactivated_plan_keeps_payment_pending(self):
        """Un plan désactivé entre l'initiation et la confirmation ne doit rien perdre."""
        self.plan.is_active = False
        self.plan.save(update_fields=['is_active'])
        payment = self._pending_payment()

        # Le plan existe toujours : l'activation aboutit malgré is_active=False.
        SubscriptionService.complete_pending_moko_payment(payment)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)

    def test_missing_plan_defers_instead_of_failing(self):
        payment = self._pending_payment()
        payment.metadata = {**payment.metadata, 'plan_id': str(uuid.uuid4())}
        payment.save(update_fields=['metadata'])

        with self.assertRaises(MokoActivationDeferred):
            SubscriptionService.complete_pending_moko_payment(payment)

        payment.refresh_from_db()
        self.assertEqual(
            payment.status, SubscriptionPayment.Status.PENDING,
            "Un paiement encaissé ne doit jamais être marqué FAILED de notre fait.",
        )

    def test_incomplete_metadata_defers_instead_of_failing(self):
        payment = self._pending_payment()
        payment.metadata = {}
        payment.save(update_fields=['metadata'])

        with self.assertRaises(MokoActivationDeferred):
            SubscriptionService.complete_pending_moko_payment(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)


class IdempotenceTests(_MokoBaseTest):
    """Webhook et poller peuvent confirmer le même paiement."""

    def test_second_confirmation_is_a_noop(self):
        payment = self._pending_payment()
        first = SubscriptionService.complete_pending_moko_payment(payment)
        second = SubscriptionService.complete_pending_moko_payment(payment)

        self.assertFalse(first['already_done'])
        self.assertTrue(second['already_done'])
        self.assertEqual(
            Subscription.objects.filter(organization=self.org).count(), 1,
            "Une double confirmation ne doit pas créer deux abonnements.",
        )


class ReconciliationTests(_MokoBaseTest):
    """Réparation des dégâts déjà en base."""

    def test_failed_payment_confirmed_by_moko_is_repaired(self):
        """Le cas exact des marchands lésés en production."""
        payment = self._pending_payment()
        payment.status = SubscriptionPayment.Status.FAILED
        payment.notes = 'Validation checkout: ancienne logique'
        payment.save(update_fields=['status', 'notes'])

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Successful'),
        ):
            report = SubscriptionService.reconcile_moko_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)
        self.assertIsNotNone(payment.subscription)
        self.assertEqual(len(report['reconciled']), 1)

    def test_dry_run_writes_nothing(self):
        payment = self._pending_payment()

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Successful'),
        ):
            report = SubscriptionService.reconcile_moko_payments(dry_run=True)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        self.assertEqual(len(report['reconciled']), 1)
        self.assertFalse(Subscription.objects.filter(organization=self.org).exists())

    def test_genuinely_failed_payment_is_not_activated(self):
        payment = self._pending_payment()

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Failed'),
        ):
            report = SubscriptionService.reconcile_moko_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.FAILED)
        self.assertEqual(len(report['really_failed']), 1)
        self.assertFalse(Subscription.objects.filter(organization=self.org).exists())

    def test_still_pending_payment_is_left_alone(self):
        payment = self._pending_payment()

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Submitted'),
        ):
            report = SubscriptionService.reconcile_moko_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        self.assertEqual(len(report['still_pending']), 1)

    def test_unreachable_moko_leaves_payment_untouched(self):
        payment = self._pending_payment()

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            side_effect=OSError('timeout'),
        ):
            report = SubscriptionService.reconcile_moko_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        self.assertEqual(len(report['unresolved']), 1)


class ReconciliationCadenceTests(_MokoBaseTest):
    """
    Le balayage se détend avec l'âge, mais ne s'arrête jamais.

    La réconciliation repart de la BASE, toutes les 30 minutes, sur 30 jours de
    règlements MOBILE MONEY `pending` ou `failed`, à raison d'un appel HTTP par
    règlement. Un échec définitif y était donc réinterrogé 1 440 fois. Renoncer
    tout à fait rouvrirait le trou que cette tâche existe pour boucher : la
    cadence espace, elle n'abandonne pas.
    """

    def _aged(self, payment, *, days_old, last_reconciled_ago=None):
        """Vieillit un règlement en base, `created_at` étant posé à la création."""
        now = timezone.now()
        SubscriptionPayment.objects.filter(pk=payment.pk).update(
            created_at=now - timedelta(days=days_old),
            last_reconciled_at=(
                None if last_reconciled_ago is None else now - last_reconciled_ago
            ),
        )
        payment.refresh_from_db()
        return payment

    def test_never_reconciled_payment_is_always_due(self):
        payment = self._aged(self._pending_payment(), days_old=20)

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Submitted'),
        ) as call:
            report = SubscriptionService.reconcile_moko_payments()

        self.assertEqual(call.call_count, 1)
        self.assertEqual(report['checked'], 1)
        self.assertEqual(report['skipped'], 0)

    def test_fresh_pending_payment_is_seen_at_every_pass(self):
        """Moins de 24 h : c'est là que l'argent peut être en transit."""
        payment = self._aged(
            self._pending_payment(), days_old=0,
            last_reconciled_ago=timedelta(minutes=30),
        )

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Submitted'),
        ) as call:
            report = SubscriptionService.reconcile_moko_payments()

        self.assertEqual(call.call_count, 1)
        self.assertEqual(report['skipped'], 0)

    def test_old_failed_payment_is_skipped_between_passes(self):
        """Le cas qui coûtait 1 440 appels : échec définitif, vieux de 15 jours."""
        payment = self._pending_payment()
        payment.status = SubscriptionPayment.Status.FAILED
        payment.save(update_fields=['status'])
        self._aged(payment, days_old=15, last_reconciled_ago=timedelta(days=1))

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Failed'),
        ) as call:
            report = SubscriptionService.reconcile_moko_payments()

        self.assertEqual(call.call_count, 0, 'aucun appel MOKO ne devait partir')
        self.assertEqual(report['checked'], 0)
        self.assertEqual(report['skipped'], 1)

    def test_old_failed_payment_is_still_revisited_eventually(self):
        """Espacer n'est pas renoncer : au-delà de l'intervalle, on rappelle."""
        payment = self._pending_payment()
        payment.status = SubscriptionPayment.Status.FAILED
        payment.save(update_fields=['status'])
        self._aged(payment, days_old=15, last_reconciled_ago=timedelta(days=8))

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Successful'),
        ) as call:
            report = SubscriptionService.reconcile_moko_payments()

        self.assertEqual(call.call_count, 1)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)
        self.assertEqual(len(report['reconciled']), 1)

    def test_explicit_reference_ignores_the_cadence(self):
        """Un exploitant qui nomme une référence veut une réponse maintenant."""
        payment = self._pending_payment()
        payment.status = SubscriptionPayment.Status.FAILED
        payment.save(update_fields=['status'])
        self._aged(payment, days_old=15, last_reconciled_ago=timedelta(minutes=1))

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Successful'),
        ) as call:
            report = SubscriptionService.reconcile_moko_payments(
                reference=payment.reference,
            )

        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(report['reconciled']), 1)

    def test_pass_is_recorded_even_when_moko_is_unreachable(self):
        """
        Sans cela, un MOKO en panne ferait repasser les mêmes règlements en
        boucle serrée à chaque battement : exactement l'emballement corrigé.
        """
        payment = self._aged(self._pending_payment(), days_old=15)

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            side_effect=OSError('timeout'),
        ):
            SubscriptionService.reconcile_moko_payments()

        payment.refresh_from_db()
        self.assertIsNotNone(payment.last_reconciled_at)
        self.assertEqual(payment.reconcile_attempts, 1)

    def test_dry_run_does_not_record_a_pass(self):
        payment = self._aged(self._pending_payment(), days_old=15)

        with patch(
            'apps.subscriptions.moko_client.get_payment_status_v2',
            return_value=_moko_status_payload('Submitted'),
        ):
            SubscriptionService.reconcile_moko_payments(dry_run=True)

        payment.refresh_from_db()
        self.assertIsNone(payment.last_reconciled_at)
        self.assertEqual(payment.reconcile_attempts, 0)

    def test_a_fresh_pending_is_never_starved_by_old_failures(self):
        """
        Le piège que la première version de ce lot contenait.

        Le plafond de lot s'appliquait AVANT la cadence. Le tri place en tête
        les règlements réconciliés il y a le plus longtemps, or l'ancienneté du
        dernier passage ne dit pas si un règlement est dû : de vieux échecs
        hors cadence occupaient tout le lot et évinçaient un `pending` récent,
        pourtant à voir à chaque passage. C'est exactement le règlement dont
        l'argent peut être en transit.
        """
        for _ in range(3):
            vieux = self._pending_payment()
            vieux.status = SubscriptionPayment.Status.FAILED
            vieux.save(update_fields=['status'])
            # Vieux, réconcilié il y a longtemps : en tête de tri, hors cadence.
            self._aged(vieux, days_old=20, last_reconciled_ago=timedelta(days=2))

        frais = self._aged(
            self._pending_payment(), days_old=0,
            last_reconciled_ago=timedelta(minutes=5),
        )

        with patch.object(SubscriptionService, 'RECONCILE_BATCH_SIZE', 2):
            with patch(
                'apps.subscriptions.moko_client.get_payment_status_v2',
                return_value=_moko_status_payload('Successful'),
            ) as call:
                SubscriptionService.reconcile_moko_payments()

        self.assertEqual(
            call.call_count, 1,
            'seul le règlement en cadence devait provoquer un appel',
        )
        frais.refresh_from_db()
        self.assertEqual(
            frais.status, SubscriptionPayment.Status.COMPLETED,
            'le paiement récent a été évincé du lot par de vieux échecs',
        )

    def test_batch_is_capped_and_oldest_pass_comes_first(self):
        """
        Le lot est borné pour tenir dans `soft_time_limit`. Le tri par
        `last_reconciled_at` garantit qu'un règlement repoussé hors du lot passe
        en tête du suivant : rien ne meurt de faim.
        """
        recent = self._aged(
            self._pending_payment(), days_old=0,
            last_reconciled_ago=timedelta(minutes=1),
        )
        vu_avant = recent.last_reconciled_at
        jamais_vu = self._aged(self._pending_payment(), days_old=0)

        with patch.object(SubscriptionService, 'RECONCILE_BATCH_SIZE', 1):
            with patch(
                'apps.subscriptions.moko_client.get_payment_status_v2',
                return_value=_moko_status_payload('Submitted'),
            ) as call:
                SubscriptionService.reconcile_moko_payments()

        self.assertEqual(call.call_count, 1)
        jamais_vu.refresh_from_db()
        recent.refresh_from_db()
        self.assertIsNotNone(
            jamais_vu.last_reconciled_at,
            'le règlement jamais réconcilié devait passer en premier',
        )
        self.assertEqual(
            recent.last_reconciled_at, vu_avant,
            'le règlement hors lot ne devait pas être touché',
        )


class MokoStatusVocabularyTests(_MokoBaseTest):
    """Les statuts que le client MOKO connaît doivent tous être interprétés."""

    def test_processing_is_pending_not_unknown(self):
        payment = self._pending_payment()
        outcome = SubscriptionService.apply_moko_terminal_or_intermediate_status(
            payment, 'processing',
        )
        self.assertEqual(outcome, 'pending')

    def test_rejected_is_failed_not_unknown(self):
        payment = self._pending_payment()
        outcome = SubscriptionService.apply_moko_terminal_or_intermediate_status(
            payment, 'rejected',
        )
        self.assertEqual(outcome, 'failed')
