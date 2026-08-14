"""
Currency conversion service.
Centralized utility for all currency-related operations.
"""
from decimal import Decimal, ROUND_HALF_UP
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
