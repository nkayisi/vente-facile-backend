"""
Tests de l'expiration des points de fidélité.

`points_expiry_days` était configurable dans l'interface mais totalement inerte :
aucune tâche ne le lisait. Une organisation qui réglait « 90 jours » ne voyait
jamais un seul point expirer.

Modèle vérifié ici : expiration **par lot, FIFO**. Chaque crédit est un lot daté,
les débits consomment les lots les plus anciens d'abord, et ce qui reste d'un lot
plus vieux que la durée configurée expire.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.contacts.models import Customer
from apps.organizations.models import Organization
from apps.settings.models import (
    CustomerLoyalty, LoyaltyProgram, LoyaltyTransaction,
)
from apps.settings.services import LoyaltyExpiryService


class _ExpiryBaseTest(TestCase):
    def setUp(self):
        suf = uuid.uuid4().hex[:8]
        self.org = Organization.objects.create(name=f'Org {suf}', slug=f'org-{suf}')
        self.customer = Customer.objects.create(
            organization=self.org, name='Client', code=f'C{suf}', phone='0900000000',
        )
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, is_active=True,
            points_expiry_days=90,
            point_value=Decimal('10.00'),
            min_points_to_redeem=10,
        )
        self.loyalty = CustomerLoyalty.objects.create(
            organization=self.org, customer=self.customer, current_points=0,
        )

    def _credit(self, points, days_ago, kind=LoyaltyTransaction.TransactionType.EARN):
        """Écrit un crédit daté et met le solde à jour."""
        txn = LoyaltyTransaction.objects.create(
            organization=self.org, customer_loyalty=self.loyalty,
            transaction_type=kind, points=points, balance_after=0,
        )
        self._backdate(txn, days_ago)
        self.loyalty.current_points += points
        self.loyalty.total_points_earned += points
        self.loyalty.save(update_fields=['current_points', 'total_points_earned'])
        return txn

    def _debit(self, points, days_ago, kind=LoyaltyTransaction.TransactionType.REDEEM):
        txn = LoyaltyTransaction.objects.create(
            organization=self.org, customer_loyalty=self.loyalty,
            transaction_type=kind, points=-abs(points), balance_after=0,
        )
        self._backdate(txn, days_ago)
        self.loyalty.current_points -= abs(points)
        self.loyalty.total_points_redeemed += abs(points)
        self.loyalty.save(update_fields=['current_points', 'total_points_redeemed'])
        return txn

    def _backdate(self, txn, days_ago):
        """`created_at` est auto_now_add : on le force en base."""
        moment = timezone.now() - timedelta(days=days_ago)
        LoyaltyTransaction.objects.filter(pk=txn.pk).update(created_at=moment)

    def _expire(self, **kwargs):
        return LoyaltyExpiryService.expire_for_organization(self.org, **kwargs)


class ExpiryComputationTests(_ExpiryBaseTest):

    def test_points_older_than_lifetime_expire(self):
        self._credit(100, days_ago=120)

        report = self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 0)
        self.assertEqual(report['points'], 100)

    def test_recent_points_are_untouched(self):
        self._credit(100, days_ago=30)

        self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 100)

    def test_only_the_expired_lot_is_removed(self):
        self._credit(100, days_ago=120)   # périmé
        self._credit(40, days_ago=10)     # encore valide

        self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 40)

    def test_redemptions_consume_the_oldest_lot_first(self):
        """
        FIFO : 100 pts périmés puis 40 récents, dont 60 déjà dépensés.
        Les 60 sortent du lot ancien ⇒ il n'en reste que 40 à expirer.
        """
        self._credit(100, days_ago=120)
        self._credit(40, days_ago=10)
        self._debit(60, days_ago=5)

        report = self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(report['points'], 40)
        self.assertEqual(self.loyalty.current_points, 40)

    def test_redemption_can_empty_the_expired_lot_entirely(self):
        self._credit(100, days_ago=120)
        self._credit(40, days_ago=10)
        self._debit(100, days_ago=5)

        report = self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(report['points'], 0, "Le lot ancien a déjà été dépensé.")
        self.assertEqual(self.loyalty.current_points, 40)

    def test_bonus_counts_as_a_dated_lot(self):
        self._credit(50, days_ago=120, kind=LoyaltyTransaction.TransactionType.BONUS)

        report = self._expire()

        self.assertEqual(report['points'], 50)


class ExpiryWritesLedgerTests(_ExpiryBaseTest):

    def test_an_expire_transaction_is_written(self):
        self._credit(100, days_ago=120)

        self._expire()

        txn = LoyaltyTransaction.objects.get(
            customer_loyalty=self.loyalty,
            transaction_type=LoyaltyTransaction.TransactionType.EXPIRE,
        )
        self.assertEqual(txn.points, -100)
        self.assertEqual(txn.balance_after, 0)

    def test_lifetime_counters_are_not_distorted(self):
        """
        Des points périmés ont bien été gagnés (on ne réduit pas
        `total_points_earned`) et n'ont pas été utilisés (on ne gonfle pas
        `total_points_redeemed`).
        """
        self._credit(100, days_ago=120)
        earned_before = self.loyalty.total_points_earned
        redeemed_before = self.loyalty.total_points_redeemed

        self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.total_points_earned, earned_before)
        self.assertEqual(self.loyalty.total_points_redeemed, redeemed_before)
        self.assertEqual(self.loyalty.current_points, 0)

    def test_running_twice_expires_nothing_more(self):
        """Idempotence : la ligne EXPIRE devient elle-même un débit du registre."""
        self._credit(100, days_ago=120)

        first = self._expire()
        second = self._expire()

        self.assertEqual(first['points'], 100)
        self.assertEqual(second['points'], 0)
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.current_points, 0)
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                customer_loyalty=self.loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.EXPIRE,
            ).count(), 1,
        )

    def test_balance_is_never_pushed_below_zero(self):
        """Registre incomplet (compte antérieur au registre) : on borne au solde."""
        self._credit(100, days_ago=120)
        # Le solde réel est plus faible que ce que dit le registre.
        self.loyalty.current_points = 30
        self.loyalty.save(update_fields=['current_points'])

        report = self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(report['points'], 30)
        self.assertEqual(self.loyalty.current_points, 0)


class ExpiryConfigurationTests(_ExpiryBaseTest):

    def test_zero_days_means_never(self):
        self.program.points_expiry_days = 0
        self.program.save(update_fields=['points_expiry_days'])
        self._credit(100, days_ago=3650)

        report = self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(report['points'], 0)
        self.assertEqual(self.loyalty.current_points, 100)

    def test_inactive_program_freezes_points(self):
        """Désactiver le programme gèle les points plutôt que de les faire fondre."""
        self.program.is_active = False
        self.program.save(update_fields=['is_active'])
        self._credit(100, days_ago=120)

        report = self._expire()

        self.loyalty.refresh_from_db()
        self.assertEqual(report['points'], 0)
        self.assertEqual(self.loyalty.current_points, 100)

    def test_dry_run_writes_nothing(self):
        self._credit(100, days_ago=120)

        report = self._expire(dry_run=True)

        self.loyalty.refresh_from_db()
        self.assertEqual(report['points'], 100)
        self.assertEqual(self.loyalty.current_points, 100)
        self.assertFalse(
            LoyaltyTransaction.objects.filter(
                customer_loyalty=self.loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.EXPIRE,
            ).exists()
        )


class ExpirySnapshotTests(_ExpiryBaseTest):
    """L'échéance à venir doit être annonçable au client avant qu'elle ne tombe."""

    def test_next_expiry_is_the_oldest_living_lot(self):
        self._credit(40, days_ago=80)   # expire dans 10 jours
        self._credit(60, days_ago=10)   # expire dans 80 jours

        snapshot = LoyaltyExpiryService.expiry_snapshot(self.loyalty, self.program)

        self.assertEqual(snapshot['expired_now'], 0)
        self.assertEqual(snapshot['next_expiry_points'], 40)
        remaining = (snapshot['next_expiry_at'] - timezone.now()).days
        self.assertIn(remaining, (9, 10))

    def test_no_expiry_configured_gives_an_empty_snapshot(self):
        self.program.points_expiry_days = 0
        self.program.save(update_fields=['points_expiry_days'])
        self._credit(100, days_ago=10)

        snapshot = LoyaltyExpiryService.expiry_snapshot(self.loyalty, self.program)

        self.assertIsNone(snapshot['next_expiry_at'])
        self.assertEqual(snapshot['next_expiry_points'], 0)


class ExpireAllTests(_ExpiryBaseTest):

    def test_only_organizations_with_expiry_are_swept(self):
        other_org = Organization.objects.create(name='Sans expiration', slug='sans-exp')
        other_customer = Customer.objects.create(
            organization=other_org, name='Autre', code='CX', phone='0911111111',
        )
        LoyaltyProgram.objects.create(
            organization=other_org, is_active=True, points_expiry_days=0,
        )
        other_loyalty = CustomerLoyalty.objects.create(
            organization=other_org, customer=other_customer, current_points=500,
        )

        self._credit(100, days_ago=120)
        reports = LoyaltyExpiryService.expire_all()

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['organization'].id, self.org.id)
        other_loyalty.refresh_from_db()
        self.assertEqual(other_loyalty.current_points, 500)
