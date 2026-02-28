"""
Service de gestion des abonnements.
Centralise toute la logique métier liée aux subscriptions.
"""
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from .models import Plan, Subscription, SubscriptionPayment, Invoice, InvoiceItem, GlobalConfig


class SubscriptionService:
    """Service centralisé pour la gestion des abonnements."""

    # ------------------------------------------------------------------ #
    # Vérification du statut
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_subscription_status(organization):
        """
        Retourne le statut détaillé de l'abonnement d'une organisation.
        Utilisé par le middleware et l'API /subscription/status.
        """
        subscription = organization.subscriptions.filter(
            status__in=[
                Subscription.Status.TRIAL,
                Subscription.Status.ACTIVE,
                Subscription.Status.PAST_DUE,
            ]
        ).order_by('-created_at').first()

        now = timezone.now()
        config = GlobalConfig.get()

        if not subscription:
            return {
                'has_subscription': False,
                'is_active': False,
                'is_blocked': True,
                'status': 'none',
                'message': "Aucun abonnement trouvé. Veuillez souscrire à un plan.",
                'subscription': None,
            }

        # Vérifier l'expiration
        if subscription.current_period_end and subscription.current_period_end < now:
            grace_end = subscription.current_period_end + timedelta(days=config.grace_period_days)

            if now > grace_end:
                # Période de grâce dépassée -> bloquer
                if subscription.status != Subscription.Status.EXPIRED:
                    subscription.status = Subscription.Status.EXPIRED
                    subscription.save(update_fields=['status', 'updated_at'])

                return {
                    'has_subscription': True,
                    'is_active': False,
                    'is_blocked': True,
                    'status': 'expired',
                    'message': "Votre abonnement a expiré. Veuillez le renouveler pour continuer.",
                    'subscription': subscription,
                    'expired_at': subscription.current_period_end,
                }
            else:
                # En période de grâce
                if subscription.status not in [Subscription.Status.EXPIRED, Subscription.Status.PAST_DUE]:
                    subscription.status = Subscription.Status.PAST_DUE
                    subscription.save(update_fields=['status', 'updated_at'])

                days_left = (grace_end - now).days
                return {
                    'has_subscription': True,
                    'is_active': True,
                    'is_blocked': False,
                    'status': 'past_due',
                    'message': f"Votre abonnement a expiré. Vous avez {days_left} jour(s) de grâce restant(s).",
                    'subscription': subscription,
                    'grace_end': grace_end,
                    'days_remaining_grace': days_left,
                }

        # Abonnement en cours (trial ou actif)
        days_remaining = (subscription.current_period_end - now).days if subscription.current_period_end else 0

        return {
            'has_subscription': True,
            'is_active': True,
            'is_blocked': False,
            'status': subscription.status,
            'message': None,
            'subscription': subscription,
            'days_remaining': max(0, days_remaining),
        }

    @staticmethod
    def is_blocked(organization):
        """Vérifie rapidement si l'organisation est bloquée."""
        status = SubscriptionService.get_subscription_status(organization)
        return status['is_blocked']

    # ------------------------------------------------------------------ #
    # Création d'abonnement trial
    # ------------------------------------------------------------------ #

    @staticmethod
    @transaction.atomic
    def create_trial(organization):
        """
        Crée un abonnement d'essai pour une organisation.
        Appelé lors de la création d'une organisation.
        """
        config = GlobalConfig.get()
        trial_days = config.trial_days

        trial_plan = Plan.objects.filter(code='trial', is_active=True).first()
        if not trial_plan:
            trial_plan = Plan.objects.create(
                name='Essai Gratuit',
                code='trial',
                description=f"Plan d'essai gratuit de {trial_days} jours",
                price_monthly=0,
                price_yearly=0,
                max_users=3,
                max_branches=1,
                max_products=100,
                max_monthly_transactions=500,
                storage_limit_mb=100,
                trial_days=trial_days,
                is_active=True
            )

        now = timezone.now()
        end = now + timedelta(days=trial_days)

        subscription = Subscription.objects.create(
            organization=organization,
            plan=trial_plan,
            status=Subscription.Status.TRIAL,
            billing_cycle=Plan.BillingCycle.MONTHLY,
            price=Decimal('0.00'),
            currency=organization.currency or 'USD',
            trial_start=now,
            trial_end=end,
            current_period_start=now,
            current_period_end=end,
        )
        return subscription

    # ------------------------------------------------------------------ #
    # Activation / Renouvellement par admin
    # ------------------------------------------------------------------ #

    @staticmethod
    @transaction.atomic
    def activate_subscription(organization, plan, billing_cycle, duration_months=None, activated_by=None, notes=''):
        """
        Active ou renouvelle un abonnement pour une organisation.
        Peut être appelé par un admin ou suite à un paiement en ligne.
        
        Args:
            organization: Organisation cible
            plan: Plan sélectionné
            billing_cycle: 'monthly', 'quarterly' ou 'yearly'
            duration_months: Nombre de mois (optionnel, calculé depuis billing_cycle si absent)
            activated_by: Utilisateur qui active (admin ou None pour paiement en ligne)
            notes: Notes optionnelles
        """
        now = timezone.now()

        # Calculer la durée
        if duration_months is None:
            if billing_cycle == Plan.BillingCycle.YEARLY:
                duration_months = 12
            elif billing_cycle == Plan.BillingCycle.QUARTERLY:
                duration_months = 3
            else:
                duration_months = 1

        end = now + timedelta(days=duration_months * 30)

        # Expirer les anciens abonnements actifs
        organization.subscriptions.filter(
            status__in=[
                Subscription.Status.TRIAL,
                Subscription.Status.ACTIVE,
                Subscription.Status.PAST_DUE,
            ]
        ).update(status=Subscription.Status.EXPIRED, updated_at=now)

        # Déterminer le prix
        price = plan.get_price(billing_cycle) * duration_months
        if billing_cycle == Plan.BillingCycle.YEARLY:
            price = plan.price_yearly

        # Créer le nouvel abonnement
        subscription = Subscription.objects.create(
            organization=organization,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            billing_cycle=billing_cycle,
            price=price,
            currency=plan.currency or 'USD',
            current_period_start=now,
            current_period_end=end,
            metadata={
                'activated_by': str(activated_by.id) if activated_by else None,
                'activation_type': 'admin' if activated_by else 'payment',
                'notes': notes,
            }
        )

        return subscription

    # ------------------------------------------------------------------ #
    # Activation suite à un paiement
    # ------------------------------------------------------------------ #

    @staticmethod
    @transaction.atomic
    def process_payment(organization, plan, billing_cycle, amount, payment_method,
                        reference='', paid_by=None, notes=''):
        """
        Traite un paiement et active l'abonnement.
        Utilisé pour les paiements en ligne ou manuels.
        """
        # Activer l'abonnement
        subscription = SubscriptionService.activate_subscription(
            organization=organization,
            plan=plan,
            billing_cycle=billing_cycle,
            activated_by=paid_by,
            notes=notes,
        )

        # Créer le paiement
        payment = SubscriptionPayment.objects.create(
            organization=organization,
            subscription=subscription,
            amount=amount,
            currency=plan.currency or 'USD',
            payment_method=payment_method,
            status=SubscriptionPayment.Status.COMPLETED,
            reference=reference,
            paid_at=timezone.now(),
            notes=notes,
            created_by=paid_by,
        )

        # Créer la facture
        invoice = SubscriptionService._create_invoice(
            organization=organization,
            subscription=subscription,
            payment=payment,
            plan=plan,
        )

        return {
            'subscription': subscription,
            'payment': payment,
            'invoice': invoice,
        }

    # ------------------------------------------------------------------ #
    # Helpers internes
    # ------------------------------------------------------------------ #

    @staticmethod
    def _create_invoice(organization, subscription, payment, plan):
        """Crée une facture liée à un paiement d'abonnement."""
        now = timezone.now()

        # Générer un numéro de facture unique
        year = now.year
        count = Invoice.objects.filter(
            invoice_number__startswith=f"INV-{year}"
        ).count() + 1
        invoice_number = f"INV-{year}-{count:05d}"

        invoice = Invoice.objects.create(
            organization=organization,
            subscription=subscription,
            invoice_number=invoice_number,
            status=Invoice.Status.PAID,
            subtotal=payment.amount,
            tax_amount=Decimal('0.00'),
            discount_amount=Decimal('0.00'),
            total=payment.amount,
            currency=payment.currency,
            issue_date=now.date(),
            due_date=now.date(),
            paid_date=now.date(),
            period_start=subscription.current_period_start.date(),
            period_end=subscription.current_period_end.date(),
        )

        InvoiceItem.objects.create(
            invoice=invoice,
            description=f"Abonnement {plan.name} - {subscription.get_billing_cycle_display()}",
            quantity=Decimal('1.00'),
            unit_price=payment.amount,
            total=payment.amount,
        )

        return invoice

    # ------------------------------------------------------------------ #
    # Expiration batch (appelable via management command / cron)
    # ------------------------------------------------------------------ #

    @staticmethod
    def expire_overdue_subscriptions():
        """
        Expire les abonnements dont la période est dépassée + période de grâce.
        À appeler périodiquement (cron/celery).
        """
        config = GlobalConfig.get()
        now = timezone.now()
        grace_cutoff = now - timedelta(days=config.grace_period_days)

        expired = Subscription.objects.filter(
            status__in=[
                Subscription.Status.TRIAL,
                Subscription.Status.ACTIVE,
                Subscription.Status.PAST_DUE,
            ],
            current_period_end__lt=grace_cutoff,
        ).update(status=Subscription.Status.EXPIRED, updated_at=now)

        return expired
