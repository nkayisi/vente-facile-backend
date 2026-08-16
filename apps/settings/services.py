"""
Currency conversion service.
Centralized utility for all currency-related operations.
"""
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)


class CurrencyService:
    """
    Service centralisé pour les opérations de conversion de devises.
    
    Convention des taux de change:
    - La devise principale a toujours un taux de 1.000000
    - Pour les devises secondaires, exchange_rate = combien d'unités de 
      devise PRINCIPALE pour 1 unité de CETTE devise.
    - Ex: Si CDF est principal et USD a rate=2800, alors 1 USD = 2800 CDF
    
    Formules de conversion:
    - Vers la devise principale: amount_primary = amount * source.exchange_rate
    - Depuis la devise principale: amount_target = amount / target.exchange_rate
    - Entre deux devises: amount_target = (amount * source.rate) / target.rate
    """
    
    @staticmethod
    def get_primary_currency(organization):
        """
        Retourne l'OrganizationCurrency principale d'une organisation.
        Utilise un cache court pour éviter les requêtes répétées.
        """
        from .models import OrganizationCurrency
        
        cache_key = f'primary_currency_{organization.id}'
        primary = cache.get(cache_key)
        
        if primary is None:
            primary = OrganizationCurrency.objects.select_related('currency').filter(
                organization=organization,
                is_primary=True
            ).first()
            if primary:
                cache.set(cache_key, primary, timeout=300)  # 5 min cache
        
        return primary
    
    @staticmethod
    def invalidate_cache(organization):
        """Invalide le cache de devise pour une organisation."""
        cache.delete(f'primary_currency_{organization.id}')
        cache.delete(f'org_currencies_{organization.id}')

    @classmethod
    def primary_code(cls, organization):
        """Code de la devise principale d'une org (accepte une instance ou un id)."""
        from apps.organizations.models import Organization

        if isinstance(organization, Organization):
            return organization.currency or 'CDF'
        return (
            Organization.objects.filter(id=organization)
            .values_list('currency', flat=True)
            .first()
        ) or 'CDF'

    @classmethod
    def resolve(cls, organization, currency=None, exchange_rate=None, strict=False):
        """
        Résout ``(currency, exchange_rate)`` d'une ligne monétaire d'un tenant.

        Point d'entrée UNIQUE de la résolution de devise : ventes, caisse,
        dépenses, achats et fournisseurs passent tous par ici, pour qu'aucun
        module ne réinvente une devise par défaut codée en dur.

        ``exchange_rate`` retourné = unités de devise PRINCIPALE de l'org pour
        1 unité de ``currency`` (la principale vaut toujours 1).

        - devise absente ⇒ devise principale de l'organisation, taux 1 ;
        - devise = principale ⇒ taux 1 (forcé) ;
        - devise secondaire avec un taux absent, nul OU égal à 1 ⇒ taux relu
          depuis ``OrganizationCurrency``. Un taux de 1 sur une devise
          secondaire est impossible (seule la principale vaut 1) : c'est la
          signature d'un taux jamais renseigné, qui fausserait les rapports ;
        - devise secondaire avec un taux explicite ⇒ conservé tel quel.

        ``strict=True`` (saisie utilisateur) lève ``ValidationError`` si la
        devise n'est pas activée pour l'organisation. ``strict=False`` (flux
        internes déjà validés ailleurs) retombe sur un taux de 1 plutôt que de
        casser la transaction.
        """
        from apps.organizations.models import Organization

        primary = cls.primary_code(organization)
        currency = (currency or '').strip() or primary

        if currency == primary:
            return currency, Decimal('1.000000')

        if not isinstance(organization, Organization):
            organization = Organization.objects.filter(id=organization).first()
            if organization is None:
                return currency, Decimal('1.000000')

        org_currency = cls.get_org_currencies(organization).get(currency)
        if org_currency is None:
            if strict:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({
                    'currency': f"La devise '{currency}' n'est pas activée pour cette organisation."
                })
            return currency, cls._trusted_rate(exchange_rate)

        if cls._trusted_rate(exchange_rate) != Decimal('1.000000'):
            return currency, Decimal(exchange_rate)

        return currency, org_currency.exchange_rate

    @staticmethod
    def _trusted_rate(exchange_rate):
        """Taux exploitable, ou 1 s'il est absent, nul, négatif ou égal à 1."""
        if exchange_rate is None:
            return Decimal('1.000000')
        rate = Decimal(exchange_rate)
        return rate if rate > 0 and rate != Decimal('1') else Decimal('1.000000')
    
    @staticmethod
    def get_org_currencies(organization):
        """
        Retourne toutes les devises actives d'une organisation.
        Retourne un dict {code: OrganizationCurrency}.
        """
        from .models import OrganizationCurrency
        
        cache_key = f'org_currencies_{organization.id}'
        currencies = cache.get(cache_key)
        
        if currencies is None:
            qs = OrganizationCurrency.objects.select_related('currency').filter(
                organization=organization,
                is_active=True
            )
            currencies = {oc.currency.code: oc for oc in qs}
            cache.set(cache_key, currencies, timeout=300)
        
        return currencies
    
    @classmethod
    def convert_to_primary(cls, amount, from_currency_code, organization):
        """
        Convertit un montant vers la devise principale de l'organisation.
        
        Args:
            amount: Montant à convertir (Decimal ou float/int)
            from_currency_code: Code de la devise source (ex: 'USD')
            organization: Instance Organization
            
        Returns:
            Decimal: Montant converti dans la devise principale
        """
        amount = Decimal(str(amount))
        
        currencies = cls.get_org_currencies(organization)
        source = currencies.get(from_currency_code)
        
        if not source:
            return amount  # Devise non configurée, retourne tel quel
        
        if source.is_primary:
            return amount  # Déjà en devise principale
        
        # amount_in_primary = amount * source.exchange_rate
        return (amount * source.exchange_rate).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
    
    @classmethod
    def convert_from_primary(cls, amount, to_currency_code, organization):
        """
        Convertit un montant depuis la devise principale vers une autre devise.
        
        Args:
            amount: Montant en devise principale
            to_currency_code: Code de la devise cible
            organization: Instance Organization
            
        Returns:
            Decimal: Montant converti dans la devise cible
        """
        amount = Decimal(str(amount))
        
        currencies = cls.get_org_currencies(organization)
        target = currencies.get(to_currency_code)
        
        if not target:
            return amount
        
        if target.is_primary:
            return amount
        
        if target.exchange_rate == 0:
            return Decimal('0')
        
        # amount_in_target = amount / target.exchange_rate
        return (amount / target.exchange_rate).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
    
    @classmethod
    def convert(cls, amount, from_currency_code, to_currency_code, organization):
        """
        Convertit un montant d'une devise à une autre.
        
        Args:
            amount: Montant à convertir
            from_currency_code: Code devise source
            to_currency_code: Code devise cible
            organization: Instance Organization
            
        Returns:
            dict: {
                'converted_amount': Decimal,
                'exchange_rate': Decimal,
                'from_currency': str,
                'to_currency': str,
            }
        """
        amount = Decimal(str(amount))
        
        if from_currency_code == to_currency_code:
            return {
                'converted_amount': amount,
                'exchange_rate': Decimal('1'),
                'from_currency': from_currency_code,
                'to_currency': to_currency_code,
            }
        
        currencies = cls.get_org_currencies(organization)
        source = currencies.get(from_currency_code)
        target = currencies.get(to_currency_code)
        
        if not source or not target:
            return {
                'converted_amount': amount,
                'exchange_rate': Decimal('1'),
                'from_currency': from_currency_code,
                'to_currency': to_currency_code,
            }
        
        # Convertir via la devise principale
        # 1. Source → Primary: amount * source.rate
        # 2. Primary → Target: result / target.rate
        if target.exchange_rate == 0:
            converted = Decimal('0')
            rate = Decimal('0')
        else:
            amount_in_primary = amount * source.exchange_rate
            converted = (amount_in_primary / target.exchange_rate).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            rate = (source.exchange_rate / target.exchange_rate).quantize(
                Decimal('0.000001'), rounding=ROUND_HALF_UP
            )
        
        return {
            'converted_amount': converted,
            'exchange_rate': rate,
            'from_currency': from_currency_code,
            'to_currency': to_currency_code,
        }
    
    @classmethod
    def get_display_info(cls, organization):
        """
        Retourne les infos de la devise principale pour l'affichage.
        
        Returns:
            dict: {'code': 'CDF', 'symbol': 'FC', 'name': '...', 'decimal_places': 0}
        """
        primary = cls.get_primary_currency(organization)
        if primary:
            return {
                'code': primary.currency.code,
                'symbol': primary.currency.symbol,
                'name': primary.currency.name,
                'decimal_places': primary.currency.decimal_places,
            }
        
        # Fallback
        code = organization.currency or 'CDF'
        defaults = {
            'CDF': {'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0},
            'USD': {'name': 'Dollar Américain', 'symbol': '$', 'decimal_places': 2},
            'EUR': {'name': 'Euro', 'symbol': '€', 'decimal_places': 2},
        }
        info = defaults.get(code, {'name': code, 'symbol': code, 'decimal_places': 2})
        return {'code': code, **info}
    
    @classmethod
    def format_amount(cls, amount, organization=None, currency_code=None):
        """
        Formate un montant avec le symbole de devise.
        
        Args:
            amount: Montant à formater
            organization: Organisation (pour obtenir la devise par défaut)
            currency_code: Code devise explicite (prioritaire sur org)
            
        Returns:
            str: Ex: "20 000 FC" ou "10.00 $"
        """
        from .models import Currency
        
        amount = Decimal(str(amount))
        
        if currency_code:
            try:
                currency = Currency.objects.get(code=currency_code)
                symbol = currency.symbol
                decimals = currency.decimal_places
            except Currency.DoesNotExist:
                symbol = currency_code
                decimals = 2
        elif organization:
            info = cls.get_display_info(organization)
            symbol = info['symbol']
            decimals = info['decimal_places']
        else:
            symbol = 'FC'
            decimals = 0
        
        # Formater le nombre
        if decimals == 0:
            formatted = f"{int(amount):,}".replace(',', ' ')
        else:
            formatted = f"{amount:,.{decimals}f}".replace(',', ' ')
        
        return f"{formatted} {symbol}"


class LoyaltyService:
    """
    Service de fidélité - point d'entrée unique de l'attribution et de la
    consommation des points.

    Deux implémentations divergentes d'``_award_loyalty_points`` coexistaient
    (une dans ``sales/serializers.py``, une dans ``sales/views.py``), dont une
    sans garde d'idempotence. Tout est centralisé ici, sur la variante durcie.
    """

    @staticmethod
    def points_for_sale(sale, program):
        """
        Points gagnés pour une vente, **convertis en devise principale**.

        ``LoyaltyProgram.amount_per_unit`` est documenté « en devise principale ».
        Comparer directement ``sale.total``, qui est exprimé dans la devise de la
        FACTURE, faisait qu'une facture de 50 USD (soit ~140 000 CDF) donnait 0
        point avec un barème de 1 point par 1 000 CDF.

        ``Sale.exchange_rate`` = unités de devise principale pour 1 unité de la
        devise de facture ⇒ on multiplie pour revenir à la principale.
        """
        total_primary = (sale.total * (sale.exchange_rate or Decimal('1')))
        return program.calculate_points(total_primary)

    @staticmethod
    def active_program(organization):
        from apps.settings.models import LoyaltyProgram
        return LoyaltyProgram.objects.filter(
            organization=organization, is_active=True,
        ).first()

    @staticmethod
    def award_points_for_sale(sale, user=None):
        """
        Attribue les points d'une vente complétée.

        Idempotent : la contrainte unique ``(sale, transaction_type='earn')``
        empêche un double crédit en cas de retry ou de race.
        """
        from django.db import IntegrityError
        from apps.settings.models import CustomerLoyalty, LoyaltyTransaction

        if not sale.customer:
            return None

        program = LoyaltyService.active_program(sale.organization)
        if program is None:
            return None

        if program.only_registered_customers and not sale.customer:
            return None

        points = LoyaltyService.points_for_sale(sale, program)
        if points <= 0:
            return None

        if LoyaltyTransaction.objects.filter(
            sale=sale, transaction_type=LoyaltyTransaction.TransactionType.EARN,
        ).exists():
            return None

        loyalty, _ = CustomerLoyalty.objects.select_for_update().get_or_create(
            organization=sale.organization,
            customer=sale.customer,
        )
        loyalty.add_points(points)

        try:
            return LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.EARN,
                points=points,
                balance_after=loyalty.current_points,
                sale=sale,
                description=f"Points gagnés sur vente {sale.reference}",
                created_by=user,
            )
        except IntegrityError:
            # Race extrême : une autre transaction a créé la ligne EARN juste
            # avant. On retire les points qu'on venait d'ajouter.
            loyalty.add_points(-points)
            return None

    @staticmethod
    def reverse_sale_transactions(sale, user=None, label='annulation'):
        """
        Inverse les mouvements de points d'une vente annulée OU retournée :
        - ligne ``EARN`` → on retire les points, on crée ``EARN_REVERSAL`` ;
        - lignes ``REDEEM`` → on restitue les points, on crée ``REDEEM_REVERSAL``.

        Idempotent : si un reversal existe déjà pour cette vente, ne fait rien.
        Chaque compteur à vie est corrigé par ``reverse_points``, qui sait
        lequel toucher selon le sens du mouvement d'origine.
        """
        from apps.settings.models import CustomerLoyalty, LoyaltyTransaction

        TxType = LoyaltyTransaction.TransactionType

        for original_type, reversal_type, kind, reversal_label in (
            (TxType.EARN, TxType.EARN_REVERSAL, 'earn', "Annulation des points gagnés"),
            (TxType.REDEEM, TxType.REDEEM_REVERSAL, 'redeem', "Restitution des points utilisés"),
        ):
            if LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=reversal_type,
            ).exists():
                continue  # déjà reversé

            originals = list(LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=original_type,
            ))
            if not originals:
                continue

            # Une vente peut avoir été réglée en points plusieurs fois : on
            # cumule toutes les lignes REDEEM avant d'écrire un seul reversal.
            total_points = sum(abs(o.points) for o in originals)
            loyalty = CustomerLoyalty.objects.select_for_update().filter(
                pk=originals[0].customer_loyalty_id,
            ).first()
            if not loyalty:
                continue

            loyalty.reverse_points(total_points, kind)

            LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=loyalty,
                transaction_type=reversal_type,
                points=(-total_points if kind == 'earn' else total_points),
                balance_after=loyalty.current_points,
                sale=sale,
                description=f"{reversal_label} - {label} vente {sale.reference}",
                created_by=user,
            )

    @staticmethod
    def redemption_value(program, points):
        """Valeur monétaire de N points, en devise principale."""
        return (Decimal(points) * program.point_value).quantize(Decimal('0.01'))

    @staticmethod
    def consume_points(organization, resolution, *, user=None, sale=None,
                       payment=None, description=''):
        """
        Consomme réellement les points et écrit la ligne de registre.

        Trois cas d'usage, distingués par ``sale`` / ``payment`` :
        - remise à la création d'une vente : ``sale`` seul ;
        - règlement d'une facture déjà émise : ``sale`` + ``payment`` ;
        - épongement de la dette globale d'un client : ni l'un ni l'autre.
        """
        from apps.settings.models import LoyaltyTransaction

        loyalty = resolution['loyalty']
        points = resolution['points']
        loyalty.redeem_points(points)

        return LoyaltyTransaction.objects.create(
            organization=organization,
            customer_loyalty=loyalty,
            transaction_type=LoyaltyTransaction.TransactionType.REDEEM,
            points=-points,
            balance_after=loyalty.current_points,
            sale=sale,
            payment=payment,
            description=description or 'Points utilisés',
            created_by=user,
        )

    @staticmethod
    def persist_redemption(sale, resolution, *, user=None, payment=None,
                           description=''):
        """Consommation de points rattachée à une vente."""
        if not description:
            description = (
                f"Points utilisés sur vente {sale.reference} "
                f"(réduction {resolution['amount']} {sale.currency})"
            )
        return LoyaltyService.consume_points(
            sale.organization, resolution, user=user, sale=sale,
            payment=payment, description=description,
        )

    @staticmethod
    def resolve_redemption(customer, organization, points_used, max_amount,
                           target_currency=None):
        """
        Calcule combien de points peuvent réellement être consommés et la valeur
        monétaire correspondante, plafonnée par : le solde du client, le minimum
        configuré, et ``max_amount`` (exprimé dans ``target_currency``).

        Ne persiste rien. Renvoie ``{'points', 'amount', 'loyalty', 'program'}``
        ou ``None``.
        """
        from apps.settings.models import CustomerLoyalty

        if points_used <= 0 or not customer or max_amount <= 0:
            return None

        program = LoyaltyService.active_program(organization)
        if program is None:
            return None
        if points_used < program.min_points_to_redeem:
            return None

        loyalty = CustomerLoyalty.objects.filter(
            organization=organization, customer=customer,
        ).first()
        if loyalty is None:
            return None

        point_value = program.point_value or Decimal('0')
        if point_value <= 0:
            return None

        # `point_value` est en devise principale : on ramène le plafond dans la
        # même unité avant de le convertir en nombre de points.
        primary = CurrencyService.primary_code(organization)
        target_currency = (target_currency or primary).strip() or primary
        if target_currency == primary:
            max_amount_primary = max_amount
        else:
            max_amount_primary = CurrencyService.convert_to_primary(
                max_amount, target_currency, organization,
            )

        max_by_balance = int(loyalty.current_points)
        max_by_total = int(max_amount_primary / point_value)
        points_to_use = min(int(points_used), max_by_balance, max_by_total)
        if points_to_use < program.min_points_to_redeem:
            return None

        amount_primary = LoyaltyService.redemption_value(program, points_to_use)
        if target_currency == primary:
            amount = amount_primary
        else:
            amount = CurrencyService.convert_from_primary(
                amount_primary, target_currency, organization,
            )
        if amount > max_amount:
            amount = max_amount

        return {
            'points': points_to_use,
            'amount': amount,
            'loyalty': loyalty,
            'program': program,
        }


# =============================================================================
# EXPIRATION DES POINTS
# =============================================================================
#
# `LoyaltyProgram.points_expiry_days` était configurable dans l'interface mais
# totalement inerte : aucune tâche ne le lisait, une organisation qui réglait
# « 90 jours » ne voyait jamais rien expirer.
#
# Modèle retenu : **expiration par lot, FIFO**. Chaque crédit (gain, bonus,
# restitution) constitue un lot daté ; les débits (utilisation, ajustement
# négatif, annulation de gain, expiration) consomment les lots les plus anciens
# d'abord. Ce qui reste d'un lot plus vieux que `points_expiry_days` expire.
#
# C'est la lecture littérale du champ (« nombre de jours avant expiration des
# points ») et le comportement usuel des programmes de fidélité : un point
# gagné a une durée de vie propre, indépendante de l'activité du client.

class LoyaltyExpiryService:
    """Calcul et application de l'expiration des points."""

    @staticmethod
    def _remaining_lots(loyalty):
        """
        Reconstitue les lots de points encore vivants, du plus ancien au plus
        récent : ``[(date_du_gain, points_restants), ...]``.

        Les débits consomment les lots dans l'ordre d'ancienneté. Une
        approximation assumée : un `earn_reversal` retire les points les plus
        anciens plutôt que ceux du gain qu'il annule. L'écart ne porte que sur
        la DATE d'expiration, jamais sur le solde.
        """
        from apps.settings.models import LoyaltyTransaction

        rows = list(
            LoyaltyTransaction.objects
            .filter(customer_loyalty=loyalty)
            .order_by('created_at')
            .values_list('created_at', 'points')
        )

        lots = [[created_at, points] for created_at, points in rows if points > 0]
        debits = sum(-points for _, points in rows if points < 0)

        index = 0
        while debits > 0 and index < len(lots):
            take = min(debits, lots[index][1])
            lots[index][1] -= take
            debits -= take
            if lots[index][1] == 0:
                index += 1

        return [(created_at, remaining) for created_at, remaining in lots if remaining > 0]

    @staticmethod
    def expiry_snapshot(loyalty, program, now=None):
        """
        Photographie de l'expiration pour un compte : ce qui est déjà périmé, et
        le prochain lot à périmer.

        Renvoie ``{'expired_now', 'next_expiry_at', 'next_expiry_points'}``.
        Sans expiration configurée, tout est nul / vide.
        """
        from django.utils import timezone as tz

        empty = {'expired_now': 0, 'next_expiry_at': None, 'next_expiry_points': 0}
        if not program or not program.is_active or program.points_expiry_days <= 0:
            return empty

        now = now or tz.now()
        lifetime = timedelta(days=program.points_expiry_days)
        cutoff = now - lifetime

        expired_now = 0
        next_expiry_at = None
        next_expiry_points = 0

        for earned_at, remaining in LoyaltyExpiryService._remaining_lots(loyalty):
            if earned_at <= cutoff:
                expired_now += remaining
            else:
                expires_at = earned_at + lifetime
                if next_expiry_at is None:
                    next_expiry_at = expires_at
                    next_expiry_points = remaining
                elif expires_at == next_expiry_at:
                    next_expiry_points += remaining

        # Filet : le registre peut être incomplet (comptes antérieurs au
        # registre). On n'expire jamais plus que le solde réel.
        expired_now = min(expired_now, max(0, loyalty.current_points))

        return {
            'expired_now': expired_now,
            'next_expiry_at': next_expiry_at,
            'next_expiry_points': next_expiry_points,
        }

    @staticmethod
    def expire_for_organization(organization, now=None, dry_run=False):
        """
        Applique l'expiration à tous les comptes de fidélité d'une organisation.

        Idempotente : la ligne ``EXPIRE`` écrite devient elle-même un débit du
        registre, donc un second passage le même jour ne retire rien de plus.
        """
        from apps.settings.models import CustomerLoyalty, LoyaltyTransaction

        program = LoyaltyService.active_program(organization)
        if not program or program.points_expiry_days <= 0:
            return {'organization': organization, 'accounts': 0, 'points': 0, 'details': []}

        report = {'organization': organization, 'accounts': 0, 'points': 0, 'details': []}

        accounts = CustomerLoyalty.objects.filter(
            organization=organization, current_points__gt=0,
        ).select_related('customer')

        for loyalty in accounts:
            snapshot = LoyaltyExpiryService.expiry_snapshot(loyalty, program, now=now)
            to_expire = snapshot['expired_now']
            if to_expire <= 0:
                continue

            if dry_run:
                report['accounts'] += 1
                report['points'] += to_expire
                report['details'].append((loyalty.customer.name, to_expire))
                continue

            with transaction.atomic():
                removed = loyalty.expire_points(to_expire)
                if removed <= 0:
                    continue
                LoyaltyTransaction.objects.create(
                    organization=organization,
                    customer_loyalty=loyalty,
                    transaction_type=LoyaltyTransaction.TransactionType.EXPIRE,
                    points=-removed,
                    balance_after=loyalty.current_points,
                    description=(
                        f"{removed} points expirés "
                        f"(validité {program.points_expiry_days} jours)"
                    ),
                )

            report['accounts'] += 1
            report['points'] += removed
            report['details'].append((loyalty.customer.name, removed))

        if report['points']:
            logger.info(
                'Expiration fidélité: %s - %d point(s) retiré(s) sur %d compte(s)',
                organization.name, report['points'], report['accounts'],
            )

        return report

    @staticmethod
    def expire_all(now=None, dry_run=False):
        """Balaie toutes les organisations dont le programme fixe une validité."""
        from apps.organizations.models import Organization
        from apps.settings.models import LoyaltyProgram

        org_ids = LoyaltyProgram.objects.filter(
            is_active=True, points_expiry_days__gt=0,
        ).values_list('organization_id', flat=True)

        reports = []
        for organization in Organization.objects.filter(id__in=list(org_ids)):
            reports.append(
                LoyaltyExpiryService.expire_for_organization(
                    organization, now=now, dry_run=dry_run,
                )
            )
        return reports
