"""
Currency conversion service.
Centralized utility for all currency-related operations.
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from django.core.cache import cache


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
    def infos_par_code(cls, code):
        """Nom, symbole et décimales d'une devise, lus dans la TABLE.

        ⚠ CETTE FONCTION EXISTE PARCE QUE SA LOGIQUE ÉTAIT ÉCRITE DEUX FOIS.
        `get_display_info` ci-dessous et `_get_default_currency_info` de
        `apps/organizations/serializers.py` portaient le même repli
        `{CDF, USD, EUR}`, mot pour mot : pour toute autre devise, ils rendaient
        `symbol=code` et `decimal_places=2`, donc un XAF s'affichait à deux
        décimales sur une monnaie qui n'en a pas. Deux copies d'une même règle
        finissent par diverger ; celles-ci avaient déjà le même défaut.

        Le repli ultime garde `decimal_places=2` : il n'est atteint que par un code
        absent de la table, c'est-à-dire par aucune devise que les serializers
        laissent désormais entrer. C'est la même borne que `currency_decimals` de
        `apps.core.exports`, et pour la même raison.
        """
        from apps.settings.models import Currency

        code = (code or 'CDF').upper()
        row = (
            Currency.objects.filter(code=code)
            .values('name', 'symbol', 'decimal_places')
            .first()
        )
        if row:
            return {'code': code, **row}
        return {'code': code, 'name': code, 'symbol': code, 'decimal_places': 2}

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
        
        # Pas de ligne principale : on interroge la table avant d'abandonner.
        return cls.infos_par_code(organization.currency)
    
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
    La cagnotte de fidélité - point d'entrée unique de son crédit et de son
    débit.

    Deux implémentations divergentes d'``_award_loyalty_points`` coexistaient
    (une dans ``sales/serializers.py``, une dans ``sales/views.py``), dont une
    sans garde d'idempotence. Tout est centralisé ici, sur la variante durcie.
    """

    @staticmethod
    def credit_for_sale(sale, program):
        """
        Cagnotte gagnée sur une vente, **convertie en devise principale**.

        L'assiette est le total de la FACTURE, jamais ce qui a été remis. Le
        montant perçu peut être encaissé dans une autre devise, fractionné entre
        plusieurs devises, et la monnaie rendue dans une troisième : rien de tout
        cela ne change ce que le client a acheté. Les deux points d'attribution
        (création de la vente et règlement différé) passent d'ailleurs par
        ``award_for_sale(sale)``, qui ne connaît que la vente.

        La cagnotte est libellée en devise PRINCIPALE. ``Sale.exchange_rate`` =
        unités de devise principale pour 1 unité de la devise de facture ⇒ on
        multiplie pour y revenir. On retient le taux **figé sur la vente**, pas
        celui du jour : une facture de la semaine dernière ne se revalorise pas
        parce que le dollar a bougé.
        """
        rate = sale.exchange_rate
        if not rate or rate <= 0:
            # Un taux nul ou négatif ne peut pas sortir de `Sale.save()`, qui
            # passe par `CurrencyService.resolve`. S'il en arrive un malgré tout
            # (écriture SQL directe, reprise de données), le multiplier donnerait
            # une cagnotte nulle sans le moindre signal. On relit alors le taux
            # de l'org.
            _currency, rate = CurrencyService.resolve(
                sale.organization, sale.currency,
            )

        return program.credit_for(sale.total * rate)

    @staticmethod
    def active_program(organization):
        from apps.settings.models import LoyaltyProgram
        return LoyaltyProgram.objects.filter(
            organization=organization, is_active=True,
        ).first()

    @staticmethod
    def award_for_sale(sale, user=None):
        """
        Crédite la cagnotte d'une vente complétée.

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

        montant = LoyaltyService.credit_for_sale(sale, program)
        if montant <= 0:
            return None

        if LoyaltyTransaction.objects.filter(
            sale=sale, transaction_type=LoyaltyTransaction.TransactionType.EARN,
        ).exists():
            return None

        loyalty, _ = CustomerLoyalty.objects.select_for_update().get_or_create(
            organization=sale.organization,
            customer=sale.customer,
        )
        loyalty.credit(montant)

        try:
            return LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.EARN,
                amount=montant,
                balance_after=loyalty.balance,
                sale=sale,
                description=f"Cagnotte gagnée sur vente {sale.reference}",
                created_by=user,
            )
        except IntegrityError:
            # Race extrême : une autre transaction a créé la ligne EARN juste
            # avant. On retire ce qu'on venait de créditer.
            loyalty.reverse(montant, 'earn')
            return None

    @staticmethod
    def reverse_sale_transactions(sale, user=None, label='annulation'):
        """
        Inverse les mouvements de cagnotte d'une vente annulée OU retournée :
        - ligne ``EARN`` → on retire le gain, on crée ``EARN_REVERSAL`` ;
        - lignes ``REDEEM`` → on restitue, on crée ``REDEEM_REVERSAL``.

        Idempotent : si un reversal existe déjà pour cette vente, ne fait rien.
        Chaque compteur à vie est corrigé par ``CustomerLoyalty.reverse``, qui
        sait lequel toucher selon le sens du mouvement d'origine.
        """
        from apps.settings.models import CustomerLoyalty, LoyaltyTransaction

        TxType = LoyaltyTransaction.TransactionType

        for original_type, reversal_type, kind, reversal_label in (
            (TxType.EARN, TxType.EARN_REVERSAL, 'earn', "Annulation de la cagnotte gagnée"),
            (TxType.REDEEM, TxType.REDEEM_REVERSAL, 'redeem', "Restitution de la cagnotte utilisée"),
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

            # Une vente peut avoir été réglée en cagnotte plusieurs fois : on
            # cumule toutes les lignes REDEEM avant d'écrire un seul reversal.
            total = sum(abs(o.amount) for o in originals)
            loyalty = CustomerLoyalty.objects.select_for_update().filter(
                pk=originals[0].customer_loyalty_id,
            ).first()
            if not loyalty:
                continue

            loyalty.reverse(total, kind)

            LoyaltyTransaction.objects.create(
                organization=sale.organization,
                customer_loyalty=loyalty,
                transaction_type=reversal_type,
                amount=(-total if kind == 'earn' else total),
                balance_after=loyalty.balance,
                sale=sale,
                description=f"{reversal_label} - {label} vente {sale.reference}",
                created_by=user,
            )

    @staticmethod
    def consume(organization, resolution, *, user=None, sale=None,
                payment=None, description=''):
        """
        Débite réellement la cagnotte et écrit la ligne de registre.

        Trois cas d'usage, distingués par ``sale`` / ``payment`` :
        - remise à la création d'une vente : ``sale`` seul ;
        - règlement d'une facture déjà émise : ``sale`` + ``payment`` ;
        - épongement de la dette globale d'un client : ni l'un ni l'autre.
        """
        from apps.settings.models import LoyaltyTransaction

        loyalty = resolution['loyalty']
        montant = resolution['amount_primary']
        loyalty.debit(montant)

        return LoyaltyTransaction.objects.create(
            organization=organization,
            customer_loyalty=loyalty,
            transaction_type=LoyaltyTransaction.TransactionType.REDEEM,
            amount=-montant,
            balance_after=loyalty.balance,
            sale=sale,
            payment=payment,
            description=description or 'Cagnotte utilisée',
            created_by=user,
        )

    @staticmethod
    def persist_redemption(sale, resolution, *, user=None, payment=None,
                           description=''):
        """Débit de cagnotte rattaché à une vente."""
        if not description:
            description = (
                f"Cagnotte utilisée sur vente {sale.reference} "
                f"(réduction {resolution['amount']} {sale.currency})"
            )
        return LoyaltyService.consume(
            sale.organization, resolution, user=user, sale=sale,
            payment=payment, description=description,
        )

    @staticmethod
    def resolve_redemption(customer, organization, amount_requested, max_amount,
                           target_currency=None):
        """
        Calcule ce qui peut réellement être pris sur la cagnotte, plafonné par
        le solde du client et par ``max_amount`` (exprimé dans
        ``target_currency``).

        ┌──────────────────────────────────────────────────────────────────────┐
        │ LA CAGNOTTE EST EN DEVISE PRINCIPALE, LA FACTURE PEUT ÊTRE AILLEURS. │
        │                                                                      │
        │ C'est la seule complexité qui reste, et elle n'est pas optionnelle : │
        │ un solde de 50 $ opposé à une facture en francs se compare après     │
        │ conversion, dans un sens pour le plafond et dans l'autre pour la     │
        │ remise. Les points avaient une troisième unité à traverser ; elle a  │
        │ disparu, pas celle-ci.                                               │
        └──────────────────────────────────────────────────────────────────────┘

        Ne persiste rien. Renvoie
        ``{'amount', 'amount_primary', 'loyalty', 'program'}`` ou ``None`` :
        ``amount`` est dans la devise de la facture, ``amount_primary`` celui
        qu'on débite réellement de la cagnotte.
        """
        from apps.settings.models import MONEY_PRECISION, CustomerLoyalty, _as_money

        amount_requested = _as_money(amount_requested)
        if amount_requested <= 0 or not customer or max_amount <= 0:
            return None

        program = LoyaltyService.active_program(organization)
        if program is None:
            return None

        loyalty = CustomerLoyalty.objects.filter(
            organization=organization, customer=customer,
        ).first()
        if loyalty is None:
            return None

        # Le plafond de la facture, ramené en devise principale : c'est la seule
        # unité dans laquelle la cagnotte se compare à quoi que ce soit.
        primary = CurrencyService.primary_code(organization)
        target_currency = (target_currency or primary).strip() or primary
        if target_currency == primary:
            max_amount_primary = Decimal(max_amount)
        else:
            max_amount_primary = CurrencyService.convert_to_primary(
                max_amount, target_currency, organization,
            )

        amount_primary = min(
            amount_requested,
            _as_money(loyalty.balance),
            Decimal(max_amount_primary).quantize(
                MONEY_PRECISION, rounding=ROUND_FLOOR,
            ),
        )
        if amount_primary <= 0:
            return None

        if target_currency == primary:
            amount = amount_primary
        else:
            amount = CurrencyService.convert_from_primary(
                amount_primary, target_currency, organization,
            )
        if amount > max_amount:
            amount = Decimal(max_amount)

        return {
            'amount': amount,
            'amount_primary': amount_primary,
            'loyalty': loyalty,
            'program': program,
        }
