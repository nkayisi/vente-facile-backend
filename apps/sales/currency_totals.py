"""
Ventiler les totaux d'une vente par devise, et les ramener en devise de facture.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE DÉPÔT EST DÉJÀ MULTI-DEVISE PARTOUT, SAUF SUR UNE VENTE.                  │
│                                                                              │
│ La dette client (`CustomerBalance`), le tiroir                               │
│ (`RegisterSessionCurrencyBalance`), le livre de caisse, la balance âgée et   │
│ le Z de caisse ventilent tous. Une vente, elle, portait une devise unique et │
│ huit montants scalaires - c'était le dernier grain mono-devise du produit.   │
│                                                                              │
│ Ce module ne change pas ce grain : il l'AUGMENTE. Les scalaires de `Sale`    │
│ restent la somme convertie en devise de facture, et c'est ce qui permet aux  │
│ dix-sept agrégats `primary_sum` de continuer à lire une COLONNE, sans        │
│ jointure - donc sans le fan-out qui, mesuré ici, faisait rendre 217 227,70 à │
│ une somme qui valait 107 356,40.                                             │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from apps.settings.services import CurrencyService

TWO = Decimal('0.01')
UN = Decimal('1.000000000000')


def resoudre_devise_de_ligne(item, sale, organisation=None):
    """
    Pose `price_currency` et `price_exchange_rate` sur une ligne.

    Sans devise explicite, la ligne suit celle de la VENTE - c'est le
    comportement d'avant ce lot, et c'est celui de l'écrasante majorité des
    ventes. Le taux est alors celui de la vente, déjà résolu par `Sale.save()`.

    ⚠ UN TAUX DE 1 SUR UNE DEVISE SECONDAIRE EST IMPOSSIBLE : c'est la
    signature d'un taux jamais renseigné. `CurrencyService.resolve` le relit
    alors depuis l'organisation, exactement comme il le fait pour la vente.
    """
    devise = (item.price_currency or '').strip() or (sale.currency or '').strip()
    taux = item.price_exchange_rate

    if devise == (sale.currency or '').strip():
        # Même devise que la facture : le taux de la vente fait foi, il a déjà
        # été résolu et figé. En relire un second ouvrirait deux vérités.
        item.price_currency = sale.currency
        item.price_exchange_rate = sale.exchange_rate or UN
        return item

    org = organisation or sale.organization
    item.price_currency, item.price_exchange_rate = CurrencyService.resolve(
        org, devise, taux,
    )
    return item


def vers_devise_de_facture(montant, taux_ligne, taux_vente):
    """
    Un montant de ligne, exprimé dans la devise de la FACTURE.

    Les deux taux mènent à la devise principale (c'est la convention du
    projet) : on y passe donc, puis on redescend. `convert` du noyau et
    `CurrencyService.convert` font exactement cela.
    """
    taux_vente = taux_vente or UN
    if not taux_vente:
        return Decimal(montant)
    return (Decimal(montant) * Decimal(taux_ligne or UN)) / Decimal(taux_vente)


def ventiler(sale, items):
    """
    Écrit un `SaleCurrencyTotal` par devise, et rend les totaux de la facture.

    Rend `(subtotal, remise_lignes, taxe)` déjà convertis en devise de vente,
    pour que l'appelant pose ses scalaires sans refaire la conversion - une
    seconde arithmétique serait la divergence que ce module existe pour éviter.
    """
    from .models import SaleCurrencyTotal

    taux_vente = sale.exchange_rate or UN
    par_devise = {}

    for item in items:
        resoudre_devise_de_ligne(item, sale)
        d = par_devise.setdefault(item.price_currency, {
            'subtotal': Decimal('0.00'), 'tax': Decimal('0.00'),
            'discount': Decimal('0.00'), 'rate': item.price_exchange_rate,
        })
        d['subtotal'] += item.subtotal
        d['tax'] += item.tax_amount
        d['discount'] += item.discount_amount

    # ⚠ LA RÉSOLUTION SE PERSISTE, sinon elle ne vit que le temps de l'appel :
    # les lignes repartiraient avec une `price_currency` VIDE, et la prochaine
    # lecture les croirait toutes dans la devise de la facture.
    #
    # `bulk_update` plutôt qu'un `save()` par ligne, et c'est sûr ICI : les
    # lignes viennent d'être créées dans la même transaction, donc leur
    # `updated_at` est déjà de cet instant. Le tirage, qui pagine sur
    # `(updated_at, id)`, les verra. Ne pas transposer ce raisonnement à une
    # mise à jour de lignes anciennes - là, `apps.core.bulk.bulk_update_rows`.
    if items:
        type(items[0]).objects.bulk_update(
            items, ['price_currency', 'price_exchange_rate'],
        )

    # Les lignes par devise, écrites telles qu'elles ont été ANNONCÉES au
    # client : aucune conversion ici, c'est le montant du ticket.
    SaleCurrencyTotal.objects.filter(sale=sale).delete()
    SaleCurrencyTotal.objects.bulk_create([
        SaleCurrencyTotal(
            organization=sale.organization, sale=sale, currency=code,
            subtotal=d['subtotal'].quantize(TWO),
            tax_amount=d['tax'].quantize(TWO),
            discount_amount=d['discount'].quantize(TWO),
            total=(d['subtotal'] - d['discount'] + d['tax']).quantize(TWO),
            exchange_rate=d['rate'],
        )
        for code, d in sorted(par_devise.items())
    ])

    # Et les scalaires de la facture, convertis au taux FIGÉ de chaque devise.
    sous_total = sum(
        (vers_devise_de_facture(d['subtotal'], d['rate'], taux_vente)
         for d in par_devise.values()),
        Decimal('0.00'),
    )
    remise = sum(
        (vers_devise_de_facture(d['discount'], d['rate'], taux_vente)
         for d in par_devise.values()),
        Decimal('0.00'),
    )
    taxe = sum(
        (vers_devise_de_facture(d['tax'], d['rate'], taux_vente)
         for d in par_devise.values()),
        Decimal('0.00'),
    )
    return sous_total.quantize(TWO), remise.quantize(TWO), taxe.quantize(TWO)
