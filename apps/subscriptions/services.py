"""
Service de gestion des abonnements.
Centralise toute la logique métier liée aux subscriptions.
"""
import logging
from datetime import timedelta
from decimal import Decimal
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)

# Clés de cache par organisation (Redis en prod). TTL court : les données restent
# fraîches à ~60s, et l'invalidation explicite (signals sur Subscription /
# SubscriptionPayment) garantit une mise à jour immédiate lors d'un renouvellement
# ou d'un blocage. Voir apps/subscriptions/signals.py.
SUB_BLOCK_CACHE_KEY = "vf:sub:block:{org_id}"
SUB_QUOTA_CACHE_KEY = "vf:sub:quota:{org_id}"
SUB_CACHE_TTL = 60

from apps.organizations.models import Organization, OrganizationInvitation
from apps.settings.models import Currency

from .models import Plan, Subscription, SubscriptionPayment, Invoice, InvoiceItem, GlobalConfig


def _default_plan_currency():
    return Currency.objects.filter(code="USD", is_active=True).first()


class MokoActivationDeferred(Exception):
    """
    L'activation d'un paiement MOKO encaissé ne peut pas aboutir maintenant, mais
    l'argent a bien été prélevé : le paiement doit rester ``PENDING`` pour être
    repris par la réconciliation ou traité à la main.

    Ne JAMAIS transformer cette situation en ``FAILED`` : un paiement que MOKO a
    confirmé réussi ne doit pas être marqué comme échoué côté plateforme.
    """


class SubscriptionService:
    """Service centralisé pour la gestion des abonnements."""
    CHECKOUT_MODE_NEW = 'new'
    CHECKOUT_MODE_EXTEND = 'extend'

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
    def get_cached_block_state(organization):
        """
        État de blocage léger et sérialisable, destiné au CHEMIN CHAUD
        (SubscriptionMiddleware, exécuté à chaque requête métier).

        Ne renvoie que les primitifs utilisés par le middleware - jamais
        l'instance modèle. Caché ~60s (Redis en prod) : retire ~2-3 requêtes
        DB de chaque appel API. La transition d'état (EXPIRED/PAST_DUE) reste
        assurée par get_subscription_status() lors des appels non cachés
        (endpoint /subscriptions/status/, tâche périodique d'expiration).
        """
        key = SUB_BLOCK_CACHE_KEY.format(org_id=organization.id)
        cached = cache.get(key)
        if cached is not None:
            return cached

        full = SubscriptionService.get_subscription_status(organization)
        state = {
            'is_blocked': full['is_blocked'],
            'status': full['status'],
            'message': full.get('message'),
            'days_remaining': full.get('days_remaining', 0),
        }
        cache.set(key, state, timeout=SUB_CACHE_TTL)
        return state

    @staticmethod
    def invalidate_org_cache(org_id):
        """Purge les caches d'abonnement/quotas d'une organisation (appelé par les signals)."""
        cache.delete_many([
            SUB_BLOCK_CACHE_KEY.format(org_id=org_id),
            SUB_QUOTA_CACHE_KEY.format(org_id=org_id),
        ])

    @staticmethod
    def is_blocked(organization):
        """Vérifie rapidement si l'organisation est bloquée."""
        return SubscriptionService.get_cached_block_state(organization)['is_blocked']

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
        """
        Comptages + plafonds + drapeaux UI (renouvellement / upgrade).

        Caché ~60s par organisation : évite ~6 COUNT séquentiels à chaque
        chargement dashboard/status. N'est utilisé QUE pour l'affichage - les
        garde-fous réels (assert_can_add_user, etc.) recomptent en direct, donc
        une légère péremption est sans risque fonctionnel.
        """
        key = SUB_QUOTA_CACHE_KEY.format(org_id=organization.id)
        cached = cache.get(key)
        if cached is not None:
            return cached
        snapshot = SubscriptionService._compute_quota_snapshot(organization)
        cache.set(key, snapshot, timeout=SUB_CACHE_TTL)
        return snapshot

    @staticmethod
    def _compute_quota_snapshot(organization):
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
    def evaluate_checkout(organization, target_plan: Plan, mode: str = CHECKOUT_MODE_NEW) -> dict:
        """
        Règles : anti-downgrade (palier min), prolongation possible du même plan,
        upgrade (tier strictement supérieur) autorisé pendant la période payée.
        """
        now = timezone.now()
        mode = (mode or SubscriptionService.CHECKOUT_MODE_NEW).strip().lower()
        if mode not in (SubscriptionService.CHECKOUT_MODE_NEW, SubscriptionService.CHECKOUT_MODE_EXTEND):
            return {
                'allowed': False,
                'reason_code': 'SUBSCRIPTION_INVALID_CHECKOUT_MODE',
                'message': "Mode de paiement invalide.",
                'checkout_mode': mode,
            }

        if target_plan.tier < organization.subscription_floor_tier:
            return {
                'allowed': False,
                'reason_code': 'SUBSCRIPTION_DOWNGRADE_FORBIDDEN',
                'message': (
                    f"Vous avez déjà bénéficié d'un plan de palier "
                    f"{organization.subscription_floor_tier} ou supérieur. "
                    "Un retour à une offre inférieure n'est pas autorisé."
                ),
                'checkout_mode': mode,
            }

        sub = SubscriptionService._subscription_for_quotas(organization)
        if not sub:
            if mode == SubscriptionService.CHECKOUT_MODE_EXTEND:
                return {
                    'allowed': False,
                    'reason_code': 'SUBSCRIPTION_EXTEND_REQUIRES_ACTIVE',
                    'message': "Aucun abonnement actif à prolonger.",
                    'checkout_mode': mode,
                }
            return {'allowed': True, 'reason_code': None, 'message': None, 'checkout_mode': mode}

        period_end = sub.current_period_end
        if period_end and now < period_end:
            if target_plan.id == sub.plan.id:
                if mode == SubscriptionService.CHECKOUT_MODE_EXTEND:
                    return {'allowed': True, 'reason_code': None, 'message': None, 'checkout_mode': mode}
                return {
                    'allowed': False,
                    'reason_code': 'SUBSCRIPTION_EARLY_RENEW_FORBIDDEN',
                    'message': (
                        "Renouvellement impossible avant la fin de la période en cours. "
                        "Vous pourrez prolonger ou renouveler une fois la période expirée."
                    ),
                    'checkout_mode': mode,
                }
            if target_plan.tier <= sub.plan.tier:
                return {
                    'allowed': False,
                    'reason_code': 'SUBSCRIPTION_UPGRADE_REQUIRED',
                    'message': (
                        "Pendant la période en cours, seul un plan à palier "
                        "strictement supérieur est disponible (upgrade)."
                    ),
                    'checkout_mode': mode,
                }
            if mode == SubscriptionService.CHECKOUT_MODE_EXTEND:
                return {
                    'allowed': False,
                    'reason_code': 'SUBSCRIPTION_EXTEND_PLAN_MISMATCH',
                    'message': "La prolongation doit se faire sur le plan actuel.",
                    'checkout_mode': mode,
                }

        if mode == SubscriptionService.CHECKOUT_MODE_EXTEND:
            return {
                'allowed': False,
                'reason_code': 'SUBSCRIPTION_EXTEND_PERIOD_ENDED',
                'message': "La prolongation est réservée aux abonnements encore en cours.",
                'checkout_mode': mode,
            }

        return {'allowed': True, 'reason_code': None, 'message': None, 'checkout_mode': mode}

    @staticmethod
    def require_checkout_allowed(organization, plan: Plan, mode: str = CHECKOUT_MODE_NEW):
        r = SubscriptionService.evaluate_checkout(organization, plan, mode=mode)
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
            # Devise du PLAN, pas celle d'exploitation du marchand : le prix
            # vient du plan. `activate_subscription` faisait déjà ainsi, d'où
            # une devise qui basculait à la conversion de l'essai.
            currency=trial_plan.currency.code,
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
    def _duration_months_from_cycle(billing_cycle) -> int:
        if billing_cycle == Plan.BillingCycle.YEARLY:
            return 12
        if billing_cycle == Plan.BillingCycle.QUARTERLY:
            return 3
        return 1

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
            duration_months = SubscriptionService._duration_months_from_cycle(billing_cycle)

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

    @staticmethod
    @transaction.atomic
    def extend_subscription(organization, plan, billing_cycle, notes=''):
        """
        Prolonge la période de l'abonnement actif courant (même plan) sans créer une nouvelle ligne.
        """
        now = timezone.now()
        subscription = (
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
        if not subscription:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_EXTEND_REQUIRES_ACTIVE',
                    'message': "Aucun abonnement actif à prolonger.",
                }
            )
        if subscription.plan_id != plan.id:
            raise ValidationError(
                {
                    'code': 'SUBSCRIPTION_EXTEND_PLAN_MISMATCH',
                    'message': "La prolongation doit se faire sur le plan actuel.",
                }
            )

        duration_months = SubscriptionService._duration_months_from_cycle(billing_cycle)
        duration_days = duration_months * 30
        start = subscription.current_period_end or now

        subscription.current_period_end = start + timedelta(days=duration_days)
        subscription.billing_cycle = billing_cycle
        subscription.price = plan.get_price(billing_cycle) * duration_months
        if billing_cycle == Plan.BillingCycle.YEARLY:
            subscription.price = plan.price_yearly
        if subscription.status in [Subscription.Status.PAST_DUE, Subscription.Status.EXPIRED]:
            subscription.status = Subscription.Status.ACTIVE
        if notes:
            metadata = subscription.metadata or {}
            metadata['last_extension_note'] = notes
            subscription.metadata = metadata

        subscription.save(
            update_fields=[
                'current_period_end',
                'billing_cycle',
                'price',
                'status',
                'metadata',
                'updated_at',
            ]
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
    def _resolve_activation_mode(organization, plan):
        """
        Détermine, à partir de l'état COURANT de l'organisation, s'il faut
        prolonger l'abonnement existant ou en activer un nouveau.

        Appelé au moment de la confirmation d'un paiement : la métadonnée
        enregistrée à l'initiation peut être périmée (période expirée depuis,
        ou abonnement déjà activé par un autre paiement).
        """
        current = SubscriptionService._subscription_for_quotas(organization)
        if (
            current
            and current.plan_id == plan.id
            and current.current_period_end
            and timezone.now() < current.current_period_end
        ):
            return SubscriptionService.CHECKOUT_MODE_EXTEND
        return SubscriptionService.CHECKOUT_MODE_NEW

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
        Finalise un SubscriptionPayment après confirmation MOKO.

        Accepte un paiement ``PENDING`` (cas nominal) mais aussi ``FAILED`` : la
        réconciliation répare ainsi les paiements que l'ancienne logique avait
        marqués à tort comme échoués alors que MOKO les avait encaissés.
        Idempotent sur un paiement déjà ``COMPLETED`` rattaché à un abonnement.
        """
        repairable = (
            SubscriptionPayment.Status.PENDING,
            SubscriptionPayment.Status.FAILED,
        )
        if pending_payment.status not in repairable:
            return {
                'already_done': True,
                'payment': pending_payment,
                'subscription': pending_payment.subscription,
                'invoice': getattr(pending_payment, 'invoice', None),
            }
        if pending_payment.status == SubscriptionPayment.Status.FAILED:
            logger.warning(
                'MOKO activation: réparation d\'un paiement marqué FAILED alors '
                'que MOKO le donne encaissé (ref=%s org=%s)',
                pending_payment.reference, pending_payment.organization_id,
            )

        metadata = pending_payment.metadata or {}
        plan_id = metadata.get('plan_id')
        billing_cycle = metadata.get('billing_cycle')
        if not plan_id or not billing_cycle:
            raise MokoActivationDeferred(
                f'Métadonnées de paiement MOKO incomplètes (ref={pending_payment.reference}).'
            )

        # Le plan était actif au moment de l'achat : on ne re-filtre PAS sur
        # `is_active`. Un plan désactivé entre l'initiation et la confirmation ne
        # doit pas faire perdre son abonnement à un marchand qui a déjà payé.
        try:
            plan = Plan.objects.get(id=plan_id)
        except Plan.DoesNotExist:
            raise MokoActivationDeferred(
                f'Plan {plan_id} introuvable pour le paiement {pending_payment.reference}.'
            )

        # Le mode est déduit de l'ÉTAT COURANT, pas de la métadonnée figée à
        # l'initiation : entre-temps la période a pu expirer (une prolongation
        # devient une souscription) ou un autre paiement a pu activer
        # l'abonnement (une souscription devient une prolongation).
        checkout_mode = SubscriptionService._resolve_activation_mode(
            pending_payment.organization, plan,
        )
        if metadata.get('checkout_mode') != checkout_mode:
            metadata['checkout_mode'] = checkout_mode
            pending_payment.metadata = metadata
            pending_payment.save(update_fields=['metadata', 'updated_at'])

        notes = (metadata.get('notes') or '') + (extra_notes or '')

        # Règle de checkout CONSULTATIVE : elle est bloquante à l'initiation
        # (`moko_initiate`), jamais après encaissement. L'argent a été prélevé,
        # la contrepartie est due. On journalise l'écart pour l'admin.
        verdict = SubscriptionService.evaluate_checkout(
            pending_payment.organization, plan, mode=checkout_mode,
        )
        if not verdict['allowed']:
            logger.warning(
                'MOKO activation: règle de checkout outrepassée ref=%s org=%s '
                'plan=%s mode=%s code=%s (%s)',
                pending_payment.reference,
                pending_payment.organization_id,
                plan.code,
                checkout_mode,
                verdict['reason_code'],
                verdict['message'],
            )
            metadata['checkout_override'] = {
                'reason_code': verdict['reason_code'],
                'message': verdict['message'],
                'mode': checkout_mode,
                'at': timezone.now().isoformat(),
            }
            pending_payment.metadata = metadata
            extra_notes = (extra_notes or '') + (
                f" | Règle outrepassée ({verdict['reason_code']}) : paiement encaissé"
            )

        subscription = None
        if checkout_mode == SubscriptionService.CHECKOUT_MODE_EXTEND:
            try:
                subscription = SubscriptionService.extend_subscription(
                    organization=pending_payment.organization,
                    plan=plan,
                    billing_cycle=billing_cycle,
                    notes=notes or 'Prolongation MOKO',
                )
            except ValidationError as exc:
                # Repli : l'abonnement visé a changé entre la résolution du mode
                # et l'écriture. On active plutôt que de perdre le paiement.
                logger.warning(
                    'MOKO activation: prolongation impossible ref=%s (%s), '
                    'bascule sur activation.',
                    pending_payment.reference, exc.detail,
                )
        if subscription is None:
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
        # Source de vérité unique sur le vocabulaire de statuts : les helpers du
        # client MOKO. Les dupliquer ici faisait retomber 'processing' et
        # 'rejected' dans 'unknown', et la référence restait en file sans fin.
        from .moko_client import (
            is_payment_failed_v2,
            is_payment_pending_v2,
            is_payment_successful_v2,
        )

        s = (moko_status or '').strip().lower()
        if is_payment_pending_v2(s):
            return 'pending'
        if is_payment_successful_v2(s):
            ext = (payment.external_transaction_id or payment.external_id or '').strip()
            SubscriptionService.complete_pending_moko_payment(
                payment,
                paid_by=paid_by,
                external_id=ext,
                extra_notes=detail_note or ' | Confirmé (MOKO status)',
            )
            return 'completed'
        if is_payment_failed_v2(s):
            SubscriptionService.fail_pending_moko_payment(
                payment,
                reason=detail_note or 'MOKO: transaction failed',
            )
            return 'failed'
        logger.warning(
            'MOKO: statut inconnu "%s" pour ref=%s - paiement laissé en attente.',
            moko_status, payment.reference,
        )
        return 'unknown'

    @staticmethod
    def reconcile_moko_payments(max_age_days: int = 30, dry_run: bool = False,
                                reference: str = ''):
        """
        Filet de sécurité : rattrape les paiements MOKO encaissés dont
        l'abonnement n'a jamais été activé.

        Interroge la BASE (et non la file Redis), donc survit à un flush Redis, à
        un worker arrêté, ou à l'expiration du TTL des métadonnées. Reprend aussi
        les paiements marqués ``FAILED`` à tort par l'ancienne logique de poll.

        Renvoie un rapport : reconciled / still_pending / really_failed /
        unresolved.
        """
        from .moko_client import (
            extract_payment_status_v2,
            get_payment_status_v2,
            is_payment_failed_v2,
            is_payment_pending_v2,
            is_payment_successful_v2,
        )
        from .moko_pending import remove_pending_payment

        qs = SubscriptionPayment.objects.filter(
            payment_method=SubscriptionPayment.PaymentMethod.MOBILE_MONEY,
            status__in=[
                SubscriptionPayment.Status.PENDING,
                SubscriptionPayment.Status.FAILED,
            ],
        )
        if reference:
            qs = qs.filter(reference=reference)
        else:
            cutoff = timezone.now() - timedelta(days=max_age_days)
            qs = qs.filter(created_at__gte=cutoff)

        report = {
            'checked': 0,
            'reconciled': [],
            'still_pending': [],
            'really_failed': [],
            'unresolved': [],
        }

        for payment in qs.select_related('organization').order_by('created_at'):
            report['checked'] += 1
            ref = payment.reference

            try:
                code, data = get_payment_status_v2(ref)
            except Exception as exc:
                report['unresolved'].append((ref, f'appel MOKO impossible: {exc}'))
                continue

            if code >= 400:
                report['unresolved'].append((ref, f'MOKO HTTP {code}'))
                continue

            moko_status = extract_payment_status_v2(data)
            if not moko_status:
                report['unresolved'].append((ref, 'statut absent de la réponse'))
                continue

            if is_payment_pending_v2(moko_status):
                report['still_pending'].append((ref, moko_status))
                continue

            if is_payment_successful_v2(moko_status):
                if dry_run:
                    report['reconciled'].append((ref, f'{moko_status} (simulation)'))
                    continue
                try:
                    result = SubscriptionService.complete_pending_moko_payment(
                        payment,
                        external_id=(payment.external_transaction_id or payment.external_id or ''),
                        extra_notes=' | Réconciliation MOKO',
                    )
                except MokoActivationDeferred as exc:
                    report['unresolved'].append((ref, f'activation différée: {exc}'))
                    continue
                except Exception as exc:
                    logger.exception('Réconciliation MOKO: échec sur %s', ref)
                    report['unresolved'].append((ref, f'erreur: {exc}'))
                    continue
                remove_pending_payment(ref)
                report['reconciled'].append(
                    (ref, f"abonnement {result['subscription'].id}")
                )
                continue

            if is_payment_failed_v2(moko_status):
                if not dry_run and payment.status == SubscriptionPayment.Status.PENDING:
                    SubscriptionService.fail_pending_moko_payment(
                        payment, reason=f'Réconciliation MOKO: {moko_status}',
                    )
                    remove_pending_payment(ref)
                report['really_failed'].append((ref, moko_status))
                continue

            report['unresolved'].append((ref, f'statut inconnu: {moko_status}'))

        return report

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
