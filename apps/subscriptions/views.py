"""
ViewSets pour le module Abonnements.
"""
import logging
import uuid
from decimal import Decimal

from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.core.api_permissions import IsTenantMember, IsTenantOwner, HasPermission

from .models import Plan, Subscription, SubscriptionPayment, Invoice
from .serializers import (
    PlanSerializer,
    PublicPlanSerializer,
    SubscriptionSerializer,
    ActivateSubscriptionSerializer,
    MokoInitiateSerializer,
    SubscriptionPaymentSerializer,
    InvoiceSerializer,
)
from .services import SubscriptionService
from .moko_client import (
    initiate_payment_v2,
    extract_transaction_id_v2,
    extract_payment_status_v2,
    is_payment_successful_v2,
    is_payment_failed_v2,
    is_payment_pending_v2,
    MokoConfigurationError,
)
from .moko_pending import enqueue_pending_payment, remove_pending_payment


class MokoCallbackThrottle(AnonRateThrottle):
    """Throttle dédié au webhook Moko (scope ``moko_callback``, 60/min/IP).

    Actif même en DEBUG car attaché directement à l'action via
    ``throttle_classes`` (n'est pas filtré par ``DEFAULT_THROTTLE_CLASSES``).
    """
    scope = 'moko_callback'


logger = logging.getLogger(__name__)


def _subscription_checkout_amount(plan, billing_cycle: str) -> Decimal:
    if billing_cycle == 'yearly':
        return plan.price_yearly
    if billing_cycle == 'quarterly':
        return plan.price_monthly * Decimal('3')
    return plan.price_monthly


def _normalize_customer_number(raw: str) -> str:
    return ''.join(c for c in (raw or '').strip() if c.isdigit())


def _moko_body_is_error(data) -> bool:
    if not isinstance(data, dict):
        return True
    return str(data.get('Status', '')).strip().lower() == 'error'


def _moko_body_ack_success(data) -> bool:
    if not isinstance(data, dict):
        return False
    return str(data.get('Status', '')).strip().lower() == 'success'


def _moko_transaction_id(data) -> str:
    if not isinstance(data, dict):
        return ''
    return str(
        data.get('Transaction_id')
        or data.get('transaction_id')
        or data.get('PayDRC_Reference')
        or ''
    )


class PublicPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Plans visibles publiquement sur la landing page.
    Aucune authentification requise.
    """

    def get_queryset(self):
        return (
            Plan.objects.filter(is_active=True)
            .select_related("currency")
            .order_by("sort_order", "price_monthly")
        )
    serializer_class = PublicPlanSerializer
    permission_classes = [AllowAny]
    pagination_class = None


class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Liste des plans d'abonnement disponibles.
    Accessible à tous les utilisateurs authentifiés.
    """

    def get_queryset(self):
        return Plan.objects.filter(is_active=True).select_related("currency")
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None


class SubscriptionViewSet(viewsets.GenericViewSet):
    """
    Gestion de l'abonnement de l'organisation courante.
    """
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    action_permissions = {
        'status': 'subscription.view',
        'current': 'subscription.view',
        'history': 'subscription.view',
        'payments': 'subscription.view',
        'invoices': 'subscription.view',
        'moko_payment_status': 'subscription.view',
        'activate': 'subscription.manage',
        'moko_initiate': 'subscription.manage',
        'moko_callback': '*',
    }

    def _get_organization(self, request):
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return None
        from apps.organizations.models import Organization
        try:
            return Organization.objects.get(id=org_id, is_deleted=False)
        except Organization.DoesNotExist:
            return None

    @action(detail=False, methods=['get'])
    def status(self, request):
        """
        Retourne le statut complet de l'abonnement de l'organisation.
        Endpoint: GET /api/v1/subscriptions/status/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        sub_status = SubscriptionService.get_subscription_status(organization)

        # Sérialiser la réponse
        data = {
            'has_subscription': sub_status['has_subscription'],
            'is_active': sub_status['is_active'],
            'is_blocked': sub_status['is_blocked'],
            'status': sub_status['status'],
            'message': sub_status['message'],
        }

        subscription = sub_status.get('subscription')
        if subscription:
            data['subscription'] = SubscriptionSerializer(subscription).data
            data['plan'] = PlanSerializer(subscription.plan).data
            data['days_remaining'] = sub_status.get('days_remaining', 0)
            data['days_remaining_grace'] = sub_status.get('days_remaining_grace')
        else:
            data['subscription'] = None
            data['plan'] = None

        data['quotas'] = SubscriptionService.get_quota_snapshot(organization)

        return Response(data)

    @action(detail=False, methods=['get'])
    def current(self, request):
        """
        Retourne l'abonnement actuel.
        Endpoint: GET /api/v1/subscriptions/current/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        subscription = organization.subscriptions.filter(
            status__in=[
                Subscription.Status.TRIAL,
                Subscription.Status.ACTIVE,
                Subscription.Status.PAST_DUE,
            ]
        ).order_by('-created_at').first()

        if not subscription:
            return Response(
                {'detail': 'Aucun abonnement actif.'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(SubscriptionSerializer(subscription).data)

    @action(detail=False, methods=['get'])
    def history(self, request):
        """
        Historique de tous les abonnements de l'organisation.
        Endpoint: GET /api/v1/subscriptions/history/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        subscriptions = organization.subscriptions.all().order_by('-created_at')
        page = self.paginate_queryset(subscriptions)
        if page is not None:
            serializer = SubscriptionSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = SubscriptionSerializer(subscriptions, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def activate(self, request):
        """
        Active un abonnement suite à un paiement.
        Endpoint: POST /api/v1/subscriptions/activate/
        
        TEMPORAIREMENT BLOQUÉ pour les utilisateurs normaux tant que
        le système de paiement n'est pas entièrement configuré.
        Seuls les administrateurs plateforme (is_staff) peuvent activer
        un abonnement via l'admin panel.
        """
        if not request.user.is_staff:
            return Response(
                {
                    'detail': (
                        'Le paiement en ligne n\'est pas encore disponible. '
                        'Veuillez contacter l\'administrateur pour activer '
                        'votre abonnement.'
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Vérifier que l'utilisateur est owner ou admin
        org_id = request.headers.get('X-Organization-ID')
        if not request.user.is_staff:
            membership = request.user.memberships.filter(
                organization_id=org_id, is_active=True
            ).first()
            if not membership or membership.role != 'owner':
                return Response(
                    {'detail': 'Seul le propriétaire peut gérer l\'abonnement.'},
                    status=status.HTTP_403_FORBIDDEN
                )

        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ActivateSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan = Plan.objects.get(id=serializer.validated_data['plan_id'])

        result = SubscriptionService.process_payment(
            organization=organization,
            plan=plan,
            billing_cycle=serializer.validated_data['billing_cycle'],
            amount=serializer.validated_data['amount'],
            payment_method=serializer.validated_data['payment_method'],
            reference=serializer.validated_data.get('reference', ''),
            paid_by=request.user,
            notes=serializer.validated_data.get('notes', ''),
        )

        return Response({
            'message': 'Abonnement activé avec succès.',
            'subscription': SubscriptionSerializer(result['subscription']).data,
            'invoice': InvoiceSerializer(result['invoice']).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def payments(self, request):
        """
        Historique des paiements d'abonnement.
        Endpoint: GET /api/v1/subscriptions/payments/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        payments = SubscriptionPayment.objects.filter(
            organization=organization
        ).order_by('-created_at')

        page = self.paginate_queryset(payments)
        if page is not None:
            serializer = SubscriptionPaymentSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = SubscriptionPaymentSerializer(payments, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def invoices(self, request):
        """
        Factures d'abonnement.
        Endpoint: GET /api/v1/subscriptions/invoices/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        invoices = Invoice.objects.filter(
            organization=organization
        ).order_by('-issue_date')

        page = self.paginate_queryset(invoices)
        if page is not None:
            serializer = InvoiceSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = InvoiceSerializer(invoices, many=True)
        return Response(serializer.data)

    @action(
        detail=False,
        methods=['post'],
        url_path='moko/initiate',
        permission_classes=[IsAuthenticated, IsTenantOwner],
    )
    def moko_initiate(self, request):
        """
        Démarre un paiement Mobile Money (MOKO API v2) pour un abonnement.
        POST /api/v1/subscriptions/moko/initiate/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        ser = MokoInitiateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        plan = Plan.objects.get(id=data['plan_id'], is_active=True)
        try:
            SubscriptionService.require_checkout_allowed(
                organization,
                plan,
                mode=data.get('mode', SubscriptionService.CHECKOUT_MODE_NEW),
            )
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        amount_dec = _subscription_checkout_amount(plan, data['billing_cycle'])
        if amount_dec <= 0:
            return Response(
                {'detail': 'Ce plan ne nécessite pas de paiement en ligne.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        currency = plan.currency.code
        reference = f"vf_sub_{uuid.uuid4().hex[:20]}"
        customer_number = _normalize_customer_number(data['customer_number'])
        if len(customer_number) < 9:
            return Response(
                {'detail': 'Numéro de téléphone invalide.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        public_base = settings.PUBLIC_BACKEND_URL.rstrip('/')
        callback_url = f"{public_base}/api/v1/subscriptions/moko/callback/"
        callback_secret = getattr(settings, 'MOKO_CALLBACK_SECRET', '') or ''
        if callback_secret:
            from urllib.parse import urlencode as _urlencode
            callback_url = f"{callback_url}?{_urlencode({'token': callback_secret})}"

        metadata = {
            'plan_id': str(plan.id),
            'billing_cycle': data['billing_cycle'],
            'checkout_mode': data.get('mode', SubscriptionService.CHECKOUT_MODE_NEW),
            'moko_method': data['method'],
            'user_id': str(request.user.id),
        }
        notes = f"MOKO {data['method']} - en attente"

        pending = SubscriptionPayment.objects.create(
            organization=organization,
            subscription=None,
            amount=amount_dec,
            currency=currency,
            payment_method=SubscriptionPayment.PaymentMethod.MOBILE_MONEY,
            status=SubscriptionPayment.Status.PENDING,
            reference=reference,
            notes=notes,
            created_by=request.user,
            metadata=metadata,
        )

        amount_str = format(amount_dec.quantize(Decimal('0.01')), 'f')

        try:
            http_code, moko_data = initiate_payment_v2(
                amount=amount_str,
                currency=currency,
                phone=customer_number,
                method=data['method'],
                reference=reference,
                callback_url=callback_url,
            )
        except MokoConfigurationError as e:
            pending.delete()
            return Response({'detail': str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as e:
            logger.exception('MOKO v2 initiate failure')
            pending.status = SubscriptionPayment.Status.FAILED
            pending.notes = (pending.notes or '') + f' | Erreur: {e!s}'
            pending.save(update_fields=['status', 'notes', 'updated_at'])
            return Response(
                {'detail': 'Impossible de joindre le service de paiement.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Vérifier si erreur dans la réponse
        if http_code >= 400 or _moko_body_is_error(moko_data):
            msg = None
            if isinstance(moko_data, dict):
                msg = (
                    moko_data.get('Comment')
                    or moko_data.get('detail')
                    or moko_data.get('message')
                )
            pending.status = SubscriptionPayment.Status.FAILED
            pending.notes = (pending.notes or '') + f' | Refus: {msg or moko_data}'
            pending.save(update_fields=['status', 'notes', 'updated_at'])
            return Response(
                {'detail': msg or 'Le paiement a été refusé.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Extraire l'ID de transaction depuis la réponse v2
        tx_id = extract_transaction_id_v2(moko_data)
        tx_id = (tx_id or '')[:255]

        update_fields = ['updated_at']
        if tx_id:
            pending.external_id = tx_id
            pending.external_transaction_id = tx_id
            update_fields.extend(['external_id', 'external_transaction_id'])
        pending.save(update_fields=list(dict.fromkeys(update_fields)))

        # Enqueue pour polling Celery
        enqueue_pending_payment(
            reference=reference,
            payment_id=str(pending.id),
            paydrc_reference=tx_id,
            thirdparty_reference=reference,
        )

        # Extraire le message de la réponse
        comment = ''
        if isinstance(moko_data, dict):
            gw = moko_data.get('gateway_response') or {}
            comment = str(gw.get('Comment') or moko_data.get('message') or '')

        return Response(
            {
                'reference': reference,
                'transaction_id': tx_id or None,
                'subscription_activated': False,
                'moko_ack_success': _moko_body_ack_success(moko_data),
                'message': comment or 'Demande de paiement envoyée. Confirmez sur votre téléphone.',
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=False,
        methods=['get'],
        url_path='moko/status',
        permission_classes=[IsAuthenticated, IsTenantMember],
    )
    def moko_payment_status(self, request):
        """
        Vérifie le statut d'un paiement MOKO en cours.
        GET /api/v1/subscriptions/moko/status/?reference=xxx
        
        Utilisé par le frontend pour le polling.
        
        Retourne:
        - status: pending | completed | failed
        - message: Message descriptif
        - subscription_activated: True si l'abonnement a été activé
        """
        reference = request.query_params.get('reference', '').strip()
        if not reference:
            return Response(
                {'detail': 'Paramètre reference requis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        payment = SubscriptionPayment.objects.filter(
            organization=organization,
            reference=reference,
        ).first()

        if not payment:
            return Response(
                {'detail': 'Paiement non trouvé.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Mapper le statut interne vers un statut simplifié pour le frontend
        if payment.status == SubscriptionPayment.Status.PENDING:
            return Response({
                'status': 'pending',
                'message': 'Paiement en cours de traitement. Veuillez confirmer sur votre téléphone.',
                'subscription_activated': False,
            })
        elif payment.status == SubscriptionPayment.Status.COMPLETED:
            return Response({
                'status': 'completed',
                'message': 'Paiement réussi ! Votre abonnement est maintenant actif.',
                'subscription_activated': True,
            })
        elif payment.status == SubscriptionPayment.Status.FAILED:
            return Response({
                'status': 'failed',
                'message': 'Le paiement a échoué. Veuillez réessayer.',
                'subscription_activated': False,
            })
        else:
            return Response({
                'status': 'unknown',
                'message': 'Statut inconnu.',
                'subscription_activated': False,
            })

    @action(
        detail=False,
        methods=['post'],
        url_path='moko/callback',
        permission_classes=[AllowAny],
        authentication_classes=[],
        throttle_classes=[MokoCallbackThrottle],
    )
    def moko_callback(self, request):
        """
        Callback MOKO v2 (notification asynchrone).

        Auth :
        - Header ``Authorization: Bearer <secret>`` recommandé.
        - Fallback header ``X-Webhook-Token: <secret>``.
        - Fallback rétro-compat query string ``?token=<secret>`` (deprecated,
          warning loggué - supprimer dès que Moko aura migré côté config).

        Throttle :
        - Scope ``moko_callback`` (60/min par IP) - actif même en DEBUG.

        Durcissement :
        - Si MOKO_CALLBACK_SECRET est configuré, le secret est obligatoire.
        - La référence doit suivre notre convention (``vf_sub_*``).
        - Si la payload contient un ``amount``, il doit correspondre à
          ``payment.amount`` (tolérance 0.01 pour les arrondis Moko).
        - Idempotence renforcée : si ``payment.status != PENDING`` mais que la
          subscription n'a pas été activée, on retraite le callback.
        """
        # 1) Auth via secret partagé (header prioritaire, query en fallback)
        expected_secret = getattr(settings, 'MOKO_CALLBACK_SECRET', '') or ''
        if expected_secret:
            import hmac

            auth_header = request.headers.get('Authorization', '') or ''
            provided = ''
            if auth_header.lower().startswith('bearer '):
                provided = auth_header[7:].strip()
            if not provided:
                provided = request.headers.get('X-Webhook-Token', '') or ''
            if not provided:
                # Rétro-compat : query string (signaler comme deprecated)
                provided = request.query_params.get('token', '') or ''
                if provided:
                    logger.warning(
                        'MOKO callback: secret reçu en query string (deprecated). '
                        'Migrer côté Moko vers Authorization: Bearer.'
                    )

            if not provided or not hmac.compare_digest(provided, expected_secret):
                logger.warning('MOKO callback: invalid or missing secret')
                return Response({'detail': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)

        payload = request.data
        logger.info('MOKO callback received: %s', payload)

        if not isinstance(payload, dict):
            return Response({'detail': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)

        # Extraire la référence (format v2 ou legacy)
        ref = None
        moko_status = None
        description = ''
        callback_amount = None

        # Format API v2: { "payment": { "reference": "...", "status": "...", "amount": ... } }
        payment_data = payload.get('payment')
        if isinstance(payment_data, dict):
            ref = payment_data.get('reference')
            moko_status = payment_data.get('status')
            description = payment_data.get('description') or ''
            if 'amount' in payment_data:
                callback_amount = payment_data.get('amount')

        # Format legacy: { "Reference": "...", "Trans_Status": "..." }
        if not ref:
            ref = payload.get('Reference') or payload.get('reference')
        if not moko_status:
            moko_status = (
                payload.get('Trans_Status')
                or payload.get('trans_status')
                or payload.get('status')
                or ''
            )
        if not description:
            description = (
                payload.get('Trans_Status_Description')
                or payload.get('Status_Description')
                or payload.get('description')
                or ''
            )
        if callback_amount is None:
            callback_amount = payload.get('amount') or payload.get('Amount')

        if not ref:
            logger.warning('MOKO callback: no reference in payload')
            return Response({'status': 'ignored', 'reason': 'no reference'}, status=status.HTTP_200_OK)

        # 2) Format de référence attendu
        if not str(ref).startswith('vf_sub_'):
            logger.warning('MOKO callback: reference outside expected scope: %s', ref)
            return Response({'status': 'ignored', 'reason': 'unexpected reference'}, status=status.HTTP_200_OK)

        payment = SubscriptionPayment.objects.filter(reference=str(ref)).first()
        if not payment:
            logger.warning('MOKO callback: unknown reference %s', ref)
            return Response({'status': 'ok'}, status=status.HTTP_200_OK)

        # 3) Vérification du montant (si présent dans la payload)
        if callback_amount is not None:
            try:
                cb_amt = Decimal(str(callback_amount))
                if abs(cb_amt - payment.amount) > Decimal('0.01'):
                    logger.warning(
                        'MOKO callback: amount mismatch ref=%s expected=%s got=%s',
                        ref, payment.amount, cb_amt,
                    )
                    return Response({'detail': 'amount mismatch'}, status=status.HTTP_400_BAD_REQUEST)
            except (TypeError, ValueError):
                logger.warning('MOKO callback: invalid amount in payload: %r', callback_amount)
                return Response({'detail': 'invalid amount'}, status=status.HTTP_400_BAD_REQUEST)

        if payment.status != SubscriptionPayment.Status.PENDING:
            # Idempotence renforcée : si le payment est COMPLETED mais que la
            # subscription n'a pas réellement été activée (cas d'un crash
            # entre payment.save() et subscription.activate()), on retraite.
            needs_reapply = (
                payment.status == SubscriptionPayment.Status.COMPLETED
                and payment.subscription_id is not None
                and not getattr(payment.subscription, 'is_active', True)
            )
            if not needs_reapply:
                remove_pending_payment(str(ref))
                return Response(
                    {'status': 'Callback received successfully'},
                    status=status.HTTP_200_OK,
                )
            logger.warning(
                'MOKO callback: re-applying terminal status for ref=%s '
                '(payment completed but subscription not active)',
                ref,
            )

        if not moko_status:
            logger.info('MOKO callback: no status in payload for ref=%s', ref)
            return Response({'status': 'Callback received successfully'}, status=status.HTTP_200_OK)

        # Normaliser le statut
        status_normalized = str(moko_status).strip().lower()
        
        try:
            outcome = SubscriptionService.apply_moko_terminal_or_intermediate_status(
                payment,
                status_normalized,
                paid_by=None,
                detail_note=f' | Callback MOKO v2: {description}' if description else ' | Callback MOKO v2',
            )
            logger.info('MOKO callback: ref=%s status=%s outcome=%s', ref, status_normalized, outcome)
        except Exception:
            logger.exception('MOKO callback apply status failed for %s', ref)
            return Response({'detail': 'internal error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if outcome in ('completed', 'failed'):
            remove_pending_payment(str(ref))

        return Response({'status': 'Callback received successfully'}, status=status.HTTP_200_OK)
