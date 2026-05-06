"""
Service de gestion des abonnements.
Centralise toute la logique métier liée aux subscriptions.
"""
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from rest_framework.exceptions import ValidationError

from apps.organizations.models import Organization, OrganizationInvitation
from apps.settings.models import Currency

from .models import Plan, Subscription, SubscriptionPayment, Invoice, InvoiceItem, GlobalConfig


def _default_plan_currency():
    return Currency.objects.filter(code="USD", is_active=True).first()


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
    # Quotas, paliers, checkout
    # ------------------------------------------------------------------ #

    @staticmethod
    def _subscription_for_quotas(organization):
        """
        Abonnement utilisable pour les plafonds (inclut past_due / période de grâce).
        Exclut les statuts expirés / annulés seuls.
        """
        return (
            organization.subscriptions.filter(
                status__in=[
                    Subscription.Status.TRIAL,
                    Subscription.Status.ACTIVE,
                    Subscription.Status.PAST_DUE,
                ]
            )
            .select_related('plan')
            .order_by('-created_at')
            .first()
        )

    @staticmethod
    def get_quota_snapshot(organization):
        """Comptages + plafonds + drapeaux UI (renouvellement / upgrade)."""
        from apps.inventory.models import Warehouse
        from apps.products.models import Product

        now = timezone.now()
        users = organization.memberships.filter(is_active=True).count()
        branches = organization.branches.filter(is_active=True).count()
        products = Product.objects.filter(organization=organization, is_deleted=False).count()
        warehouses = Warehouse.objects.filter(organization=organization, is_deleted=False).count()
        pending_invites = organization.invitations.filter(
            status=OrganizationInvitation.Status.PENDING
        ).count()

        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            return {
                'has_plan': False,
                'users': {'current': users, 'limit': None, 'pending_invitations': pending_invites},
                'branches': {'current': branches, 'limit': None},
                'warehouses': {'current': warehouses, 'limit': None},
                'products': {'current': products, 'limit': None},
                'plan_tier': None,
                'current_plan_id': None,
                'subscription_floor_tier': organization.subscription_floor_tier,
                'period_end': None,
                'period_not_ended': False,
                'can_renew_same_plan': True,
                'can_upgrade': False,
            }

        plan = sub.plan
        period_not_ended = bool(
            sub.current_period_end and now < sub.current_period_end
        )

        return {
            'has_plan': True,
            'users': {
                'current': users,
                'limit': plan.max_users,
                'pending_invitations': pending_invites,
            },
            'branches': {'current': branches, 'limit': plan.max_branches},
            'warehouses': {'current': warehouses, 'limit': plan.max_warehouses},
            'products': {
                'current': products,
                'limit': plan.max_products,
            },
            'plan_tier': plan.tier,
            'current_plan_id': str(plan.id),
            'subscription_floor_tier': organization.subscription_floor_tier,
            'period_end': sub.current_period_end.isoformat() if sub.current_period_end else None,
            'period_not_ended': period_not_ended,
            'can_renew_same_plan': not period_not_ended,
            'can_upgrade': period_not_ended,
        }

    @staticmethod
    def assert_can_add_user(organization, extra_active_members: int = 1):
        """Lève ValidationError si la limite utilisateurs serait dépassée."""
        if extra_active_members < 1:
            return
        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_NONE',
                    'message': "Aucun abonnement actif : impossible d'ajouter des membres.",
                }
            )
        current = organization.memberships.filter(is_active=True).count()
        pending = organization.invitations.filter(
            status=OrganizationInvitation.Status.PENDING
        ).count()
        if current + pending + extra_active_members > sub.plan.max_users:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_USER_LIMIT',
                    'message': f"Limite d'utilisateurs atteinte ({sub.plan.max_users} compte(s) "
                    f"et invitation(s) en attente maximum pour votre formule).",
                    'limit': sub.plan.max_users,
                    'current': current,
                    'pending_invitations': pending,
                }
            )

    @staticmethod
    def assert_can_add_branch(organization):
        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_NONE',
                    'message': "Aucun abonnement actif : impossible d'ajouter une succursale.",
                }
            )
        if not sub.can_add_branch():
            n = organization.branches.filter(is_active=True).count()
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_BRANCH_LIMIT',
                    'message': f"Limite de succursales atteinte ({sub.plan.max_branches}) pour votre abonnement.",
                    'limit': sub.plan.max_branches,
                    'current': n,
                }
            )

    @staticmethod
    def assert_can_add_warehouse(organization):
        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_NONE',
                    'message': "Aucun abonnement actif : impossible d'ajouter un entrepôt.",
                }
            )
        if not sub.can_add_warehouse():
            from apps.inventory.models import Warehouse

            n = Warehouse.objects.filter(organization=organization, is_deleted=False).count()
            cap = sub.plan.max_warehouses
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_WAREHOUSE_LIMIT',
                    'message': (
                        f"Limite d'entrepôts atteinte ({cap}) pour votre abonnement."
                    ),
                    'limit': cap,
                    'current': n,
                }
            )

    @staticmethod
    def assert_can_add_products(organization, count: int = 1):
        if count < 1:
            return
        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_NONE',
                    'message': "Aucun abonnement actif : impossible d'ajouter des produits.",
                }
            )
        if not sub.can_add_products(count):
            from apps.products.models import Product

            n = Product.objects.filter(organization=organization, is_deleted=False).count()
            cap = sub.plan.max_products
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_PRODUCT_LIMIT',
                    'message': f"Limite de produits atteinte ({cap}) pour votre abonnement.",
                    'limit': cap,
                    'current': n,
                    'requested': count,
                }
            )

    @staticmethod
    def evaluate_checkout(organization, target_plan: Plan) -> dict:
        """
        Règles : anti-downgrade (palier min), pas de renouvellement anticipé du même plan,
        upgrade (tier strictement supérieur) autorisé pendant la période payée.
        """
        now = timezone.now()

        if target_plan.tier < organization.subscription_floor_tier:
            return {
                'allowed': False,
                'reason_code': 'SUBSCRIPTION_DOWNGRADE_FORBIDDEN',
                'message': (
                    f"Vous avez déjà bénéficié d'un plan de palier "
                    f"{organization.subscription_floor_tier} ou supérieur. "
                    "Un retour à une offre inférieure n'est pas autorisé."
                ),
            }

        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            return {'allowed': True, 'reason_code': None, 'message': None}

        period_end = sub.current_period_end
        if period_end and now < period_end:
            if target_plan.id == sub.plan.id:
                return {
                    'allowed': False,
                    'reason_code': 'SUBSCRIPTION_EARLY_RENEW_FORBIDDEN',
                    'message': (
                        "Renouvellement impossible avant la fin de la période en cours. "
                        "Vous pourrez prolonger ou renouveler une fois la période expirée."
                    ),
                }
            if target_plan.tier <= sub.plan.tier:
                return {
                    'allowed': False,
                    'reason_code': 'SUBSCRIPTION_UPGRADE_REQUIRED',
                    'message': (
                        "Pendant la période en cours, seul un plan à palier "
                        "strictement supérieur est disponible (upgrade)."
                    ),
                }

        return {'allowed': True, 'reason_code': None, 'message': None}

    @staticmethod
    def require_checkout_allowed(organization, plan: Plan):
        r = SubscriptionService.evaluate_checkout(organization, plan)
        if not r['allowed']:
            raise ValidationError(
                {
                    'code': r['reason_code'],
                    'message': r['message'],
                }
            )

    @staticmethod
    def can_add_user(organization) -> bool:
        """Retour booléen (ex. admin). Inclut invitations en attente. Ne lève pas."""
        try:
            SubscriptionService.assert_can_add_user(organization, 1)
            return True
        except ValidationError:
            return False

    @staticmethod
    def can_add_branch(organization) -> bool:
        try:
            SubscriptionService.assert_can_add_branch(organization)
            return True
        except ValidationError:
            return False

    @staticmethod
    def can_add_warehouse(organization) -> bool:
        try:
            SubscriptionService.assert_can_add_warehouse(organization)
            return True
        except ValidationError:
            return False

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
            default_currency = _default_plan_currency()
            if not default_currency:
                raise RuntimeError(
                    "Devise USD introuvable en base : exécutez les migrations "
                    "de l’app settings (devises) avant de créer un plan d’essai."
                )
            trial_plan = Plan.objects.create(
                name='Essai Gratuit',
                code='trial',
                description=f"Plan d'essai gratuit de {trial_days} jours",
                price_monthly=0,
                price_yearly=0,
                currency=default_currency,
                max_users=3,
                max_branches=1,
                max_warehouses=1,
                max_products=100,
                max_monthly_transactions=500,
                storage_limit_mb=100,
                trial_days=trial_days,
                tier=1,
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
            currency=plan.currency.code,
            current_period_start=now,
            current_period_end=end,
            metadata={
                'activated_by': str(activated_by.id) if activated_by else None,
                'activation_type': 'admin' if activated_by else 'payment',
                'notes': notes,
            }
        )

        organization.refresh_from_db()
        organization.subscription_floor_tier = max(
            organization.subscription_floor_tier, plan.tier
        )
        organization.save(update_fields=['subscription_floor_tier'])

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
        SubscriptionService.require_checkout_allowed(organization, plan)

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
            currency=plan.currency.code,
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

    @staticmethod
    @transaction.atomic
    def complete_pending_moko_payment(
        pending_payment,
        *,
        paid_by=None,
        external_id='',
        extra_notes='',
    ):
        """
        Finalise un SubscriptionPayment en pending après confirmation MOKO.
        Idempotent si le paiement n'est plus pending.
        """
        if pending_payment.status != SubscriptionPayment.Status.PENDING:
            return {
                'already_done': True,
                'payment': pending_payment,
                'subscription': pending_payment.subscription,
                'invoice': getattr(pending_payment, 'invoice', None),
            }

        metadata = pending_payment.metadata or {}
        plan_id = metadata.get('plan_id')
        billing_cycle = metadata.get('billing_cycle')
        if not plan_id or not billing_cycle:
            raise ValueError('Métadonnées de paiement MOKO incomplètes.')

        plan = Plan.objects.get(id=plan_id, is_active=True)
        notes = (metadata.get('notes') or '') + (extra_notes or '')

        SubscriptionService.require_checkout_allowed(pending_payment.organization, plan)

        subscription = SubscriptionService.activate_subscription(
            organization=pending_payment.organization,
            plan=plan,
            billing_cycle=billing_cycle,
            activated_by=paid_by,
            notes=notes or 'Paiement MOKO',
        )

        pending_payment.subscription = subscription
        pending_payment.status = SubscriptionPayment.Status.COMPLETED
        pending_payment.paid_at = timezone.now()
        if external_id:
            pending_payment.external_id = external_id[:255]
            if not pending_payment.external_transaction_id:
                pending_payment.external_transaction_id = external_id[:255]
        if extra_notes:
            pending_payment.notes = (pending_payment.notes or '') + extra_notes
        pending_payment.save()

        invoice = SubscriptionService._create_invoice(
            organization=pending_payment.organization,
            subscription=subscription,
            payment=pending_payment,
            plan=plan,
        )
        pending_payment.invoice = invoice
        pending_payment.save(update_fields=['invoice', 'updated_at'])

        return {
            'already_done': False,
            'subscription': subscription,
            'payment': pending_payment,
            'invoice': invoice,
        }

    @staticmethod
    @transaction.atomic
    def fail_pending_moko_payment(payment, *, reason: str = ''):
        """Marque un paiement MOKO pending comme échoué."""
        if payment.status != SubscriptionPayment.Status.PENDING:
            return {'already_done': True, 'payment': payment}
        payment.status = SubscriptionPayment.Status.FAILED
        if reason:
            payment.notes = (payment.notes or '') + f' | {reason}'
        payment.save(update_fields=['status', 'notes', 'updated_at'])
        return {'already_done': False, 'payment': payment}

    @staticmethod
    def apply_moko_terminal_or_intermediate_status(
        payment,
        moko_status: str,
        *,
        paid_by=None,
        detail_note: str = '',
    ):
        """
        Interprète le statut transaction MOKO (API /payments/transactions/status).
        Retourne: 'pending' | 'completed' | 'failed' | 'unknown'
        """
        s = (moko_status or '').strip().lower()
        if s in ('pending', 'submitted'):
            return 'pending'
        if s in ('successful', 'success', 'completed'):
            ext = (payment.external_transaction_id or payment.external_id or '').strip()
            SubscriptionService.complete_pending_moko_payment(
                payment,
                paid_by=paid_by,
                external_id=ext,
                extra_notes=detail_note or ' | Confirmé (MOKO status)',
            )
            return 'completed'
        if s in ('failed', 'error', 'cancelled', 'canceled'):
            SubscriptionService.fail_pending_moko_payment(
                payment,
                reason=detail_note or 'MOKO: transaction failed',
            )
            return 'failed'
        return 'unknown'

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
