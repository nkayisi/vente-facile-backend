"""
Allocation du CA HT (hors TVA) par ligne de vente, cohérent avec les totaux de Sale.

- Remise globale (non répercutée sur SaleItem.total) est répartie au prorata du net HT
  après remises lignes.
- Coût effectif : coût figé sur la ligne (FIFO) si > 0, sinon coût catalogue produit.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, List, Tuple

TWO_PLACES = Decimal("0.01")


def effective_unit_cost(item: Any) -> Decimal:
    cp = item.cost_price or Decimal("0")
    if cp > 0:
        return cp
    pc = getattr(item.product, "cost_price", None) or Decimal("0")
    return pc


def allocated_line_ht_revenues_for_sale(sale: Any) -> List[Tuple[Any, Decimal]]:
    """
    Retourne (item, ca_ht_net) pour chaque ligne, avec somme des ca_ht_net == sale.subtotal - sale.discount_amount.
    """
    items = list(sale.items.all())
    if not items:
        return []

    bases = [(item.subtotal - item.discount_amount).quantize(TWO_PLACES) for item in items]
    net_line = sum(bases, Decimal("0"))
    line_disc_sum = sum((item.discount_amount for item in items), Decimal("0"))
    gdisc = (sale.discount_amount - line_disc_sum).quantize(TWO_PLACES)
    if gdisc < 0:
        gdisc = Decimal("0")
    target_ht = (sale.subtotal - sale.discount_amount).quantize(TWO_PLACES)

    n = len(items)
    result: List[Tuple[Any, Decimal]] = []

    if net_line > 0:
        running = Decimal("0")
        for i, item in enumerate(items):
            if i < n - 1:
                if gdisc > 0:
                    rev = bases[i] - (gdisc * bases[i] / net_line)
                else:
                    rev = bases[i]
                rev = rev.quantize(TWO_PLACES)
                result.append((item, rev))
                running += rev
            else:
                result.append((item, (target_ht - running).quantize(TWO_PLACES)))
        return result

    wsum = sum((item.subtotal for item in items), Decimal("0"))
    if wsum > 0:
        running = Decimal("0")
        for i, item in enumerate(items):
            if i < n - 1:
                rev = (target_ht * item.subtotal / wsum).quantize(TWO_PLACES)
                result.append((item, rev))
                running += rev
            else:
                result.append((item, (target_ht - running).quantize(TWO_PLACES)))
        return result

    share = (target_ht / Decimal(n)).quantize(TWO_PLACES) if n else Decimal("0")
    running = Decimal("0")
    for i, item in enumerate(items):
        if i < n - 1:
            result.append((item, share))
            running += share
        else:
            result.append((item, (target_ht - running).quantize(TWO_PLACES)))
    return result
