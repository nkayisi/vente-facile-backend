"""
Le verrou d'inventaire, opposé à TOUT ce qui fait bouger du stock.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE VERROU NE BLOQUAIT QUE LA VENTE, ET SA PROPRE DOCSTRING DISAIT L'INVERSE. │
│                                                                              │
│ `InventorySession.is_stock_locked` annonce « stock movements for targeted    │
│ products are blocked », et seul `SaleCreateSerializer` le lisait. Pendant    │
│ qu'un magasinier comptait son rayon, une réception fournisseur, un          │
│ transfert, un ajustement ou une saisie manuelle pouvaient y faire entrer ou  │
│ sortir de la marchandise. L'écart constaté à la validation ne mesurait plus │
│ un manquant mais ces mouvements, et c'est sur ce chiffre que le stock était  │
│ ajusté - donc faussé, deux fois.                                             │
└──────────────────────────────────────────────────────────────────────────────┘

Un point d'entrée, `exiger_hors_inventaire`, appelé par chaque écriture de stock
AVANT toute écriture, sous le même `atomic`. Le refus est un `RefusMetier` :
400 au back-office, verdict `rejected` au journal - il se reproduirait à
l'identique tant que la session n'est pas validée.

⚠ DEUX ÉCRITURES SONT EXEMPTÉES, ET CE SONT CELLES DE L'INVENTAIRE LUI-MÊME :
  sa validation (qui écrit les écarts) et son annulation (qui ne bouge rien mais
  lève le verrou). Les bloquer rendrait la session impossible à clore.

⚠ LA VENTE EXISTANTE N'EST PAS BLOQUÉE AU RÈGLEMENT. Le décrément d'une vente à
  crédit ancienne, opéré au paiement de son solde (`apply_decrement`), passerait
  sinon par ce verrou : refuser l'argent d'un client parce qu'un rayon est en
  comptage serait pire que l'écart qu'on évite. La vente est contrôlée à sa
  CRÉATION, comme avant.
"""
from apps.core.exceptions import RefusMetier

#: Combien de noms d'articles le message cite avant de résumer.
NOMS_CITES = 5


class InventaireEnCours(RefusMetier):
    """Des articles de ce dépôt sont sous un inventaire en cours."""

    code = 'inventaire_en_cours'

    def __init__(self, references, noms, restants=0):
        self.references = references
        self.noms = noms
        liste = ', '.join(noms)
        if restants:
            liste += f" et {restants} autre{'s' if restants > 1 else ''}"
        super().__init__(
            f"Un inventaire est en cours dans ce dépôt ({', '.join(references)}) : "
            f"{liste} ne {'peuvent' if len(noms) + restants > 1 else 'peut'} pas "
            "bouger avant sa validation."
        )


def sessions_qui_verrouillent(organization, warehouse_id, product_ids):
    """
    `{product_id: session}` pour les articles de `product_ids` qu'une session EN
    COURS verrouille dans CE dépôt.

    Lit `InventorySession.get_locked_product_ids`, la même lecture que le comptoir
    et que `locked_products` : compter, verrouiller et refuser lisent le même jeu.
    """
    from .models import InventorySession

    demandes = {p for p in product_ids if p}
    if not demandes or not warehouse_id:
        return {}

    sessions = InventorySession.objects.filter(
        organization=organization,
        warehouse_id=warehouse_id,
        is_stock_locked=True,
        status__in=[InventorySession.Status.IN_PROGRESS, InventorySession.Status.REVIEW],
        is_deleted=False,
    ).order_by('created_at')

    verrous = {}
    for session in sessions:
        for produit in session.get_locked_product_ids() & demandes:
            verrous.setdefault(produit, session)
    return verrous


def refus(organization, verrous):
    """Le refus nommé pour un `{product_id: session}` non vide."""
    from apps.products.models import Product

    references = sorted({s.reference for s in verrous.values()})
    noms = list(
        Product.objects.filter(organization=organization, id__in=list(verrous))
        .order_by('name')
        .values_list('name', flat=True)
    )
    return InventaireEnCours(references, noms[:NOMS_CITES], max(0, len(noms) - NOMS_CITES))


def exiger_hors_inventaire(organization, warehouse_id, product_ids):
    """Lève `InventaireEnCours` si l'un des articles est sous un inventaire en cours."""
    verrous = sessions_qui_verrouillent(organization, warehouse_id, product_ids)
    if verrous:
        raise refus(organization, verrous)
