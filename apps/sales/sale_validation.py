"""Règles métier communes pour les ventes (remises, montants)."""
from decimal import Decimal


# Seuil par défaut si non défini dans Organization.settings["max_sale_discount_percent"]
DEFAULT_MAX_SALE_DISCOUNT_PERCENT = Decimal("50")


def max_sale_discount_percent(organization) -> Decimal:
    """Pourcentage max de remise (ligne et globale), lu depuis les paramètres org."""
    if organization is None:
        return DEFAULT_MAX_SALE_DISCOUNT_PERCENT
    settings = organization.settings or {}
    raw = settings.get("max_sale_discount_percent")
    if raw is None:
        return DEFAULT_MAX_SALE_DISCOUNT_PERCENT
    try:
        v = Decimal(str(raw))
        if v < 0:
            return DEFAULT_MAX_SALE_DISCOUNT_PERCENT
        if v > Decimal("100"):
            return Decimal("100")
        return v
    except Exception:
        return DEFAULT_MAX_SALE_DISCOUNT_PERCENT
