"""
Ce qui garantit qu'un membre borné a toujours au moins un entrepôt.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ON FERME LE CAS, ON NE LE RATTRAPE PLUS À LA MAIN.                          │
│                                                                              │
│ Un rôle borné sans affectation ne voit RIEN : c'est désormais la réponse des │
│ trois surfaces, tirage compris. C'est la bonne réponse - un périmètre par    │
│ défaut doit se fermer, pas s'ouvrir - mais elle laisse un membre mal         │
│ configuré devant un écran vide.                                              │
│                                                                              │
│ `assign_default_warehouse` existait pour cela et n'avait AUCUN appelant :    │
│ un membre créé demain retombait dans le trou. Le corps vit ici, la commande  │
│ et le signal l'appellent tous deux.                                          │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.db import transaction

from apps.inventory.models import Warehouse


def entrepot_principal(organization):
    """
    L'entrepôt à donner par défaut : ``is_default``, sinon le premier actif.

    Rend ``None`` quand l'organisation n'en a aucun - une jeune organisation,
    ou une configuration à reprendre. On ne fabrique rien dans ce cas.
    """
    qs = Warehouse.objects.filter(organization=organization, is_deleted=False)
    return (
        qs.filter(is_default=True).order_by('name').first()
        or qs.filter(is_active=True).order_by('name').first()
        or qs.order_by('name').first()
    )


def assurer_entrepot_du_membre(membership) -> bool:
    """
    Donne l'entrepôt principal à un membre borné qui n'en a aucun.

    Rend ``True`` si une affectation a été posée. Idempotente : un second appel
    ne trouve plus de membre sans entrepôt.

    ⚠ LE PROPRIÉTAIRE EST ÉPARGNÉ, et ce n'est pas un détail : son accès vient
    de son RÔLE, `accessible_warehouse_ids` rendant `None` pour lui. Lui poser
    une affectation le ferait passer de « tous les dépôts » à « celui-là », et
    on rétrécirait son périmètre en croyant l'élargir.

    ⚠ ON NE COMPTE QUE LES ENTREPÔTS VIVANTS. Un membre dont le seul dépôt est
    supprimé a bien une affectation, et aucun accès : c'est le cas que
    `accessible_warehouse_ids` écarte par son `is_deleted=False`, et il doit
    être rattrapé ici comme une absence.
    """
    from apps.organizations.models import OrganizationMembership

    if membership.role == OrganizationMembership.Role.OWNER:
        return False
    if membership.assigned_warehouses.filter(is_deleted=False).exists():
        return False

    principal = entrepot_principal(membership.organization)
    if principal is None:
        return False

    membership.assigned_warehouses.add(principal)
    # Le périmètre du membre vient de changer : son jeton de périmètre bascule,
    # et son terminal retirera les tables concernées à la prochaine
    # synchronisation. Sans ce mécanisme, les lignes du dépôt nouvellement
    # accessible seraient derrière son curseur, donc perdues pour toujours.
    return True


# ==============================================================================
# REMISE À ZÉRO D'UN ESSAI
# ==============================================================================

#: Ce qu'une remise à zéro EFFACE, dans l'ordre des dépendances.
#:
#: ┌────────────────────────────────────────────────────────────────────────────┐
#: │ UNE LISTE EXPLICITE, JAMAIS UNE CASCADE.                                  │
#: │                                                                            │
#: │ S'en remettre aux `on_delete` demanderait de les avoir tous relus, et une  │
#: │ seule relation en `PROTECT` ferait échouer l'opération à mi-chemin - sur   │
#: │ un geste destructeur, à l'intérieur d'une transaction qu'on vient          │
#: │ d'ouvrir. Une liste se relit, se teste, et son ordre est une preuve.       │
#: │                                                                            │
#: │ L'ordre va de l'ENFANT vers le PARENT : supprimer une vente avant ses     │
#: │ lignes marcherait par cascade, mais l'écrire ainsi rend l'intention        │
#: │ illisible et masquerait une relation `PROTECT` ajoutée plus tard.          │
#: └────────────────────────────────────────────────────────────────────────────┘
EFFACES_PAR_LA_REMISE_A_ZERO = [
    # --- La trace de synchronisation, en premier : elle référence tout le reste.
    ('sync', 'SyncOperation'),
    ('users', 'UserActivity'),
    ('notifications', 'Notification'),
    ('notifications', 'Alert'),
    ('notifications', 'EmailLog'),

    # --- Les ventes et leur suite.
    ('sales', 'SaleReturnItem'),
    ('sales', 'SaleReturn'),
    ('sales', 'Payment'),
    ('sales', 'SaleItem'),
    ('sales', 'Sale'),
    ('sales', 'QuotationItem'),
    ('sales', 'Quotation'),
    ('sales', 'RegisterSessionCurrencyBalance'),
    ('sales', 'RegisterSession'),

    # --- Les achats. La RÉQUISITION en premier : elle ne référence aucune
    # commande - c'est tout ce qui la distingue d'un bon d'achat - mais elle
    # est bien une donnée d'exploitation de l'essai, et la garder laisserait
    # au marchand une liste de courses qui ne correspond plus à son stock.
    ('purchases', 'RequisitionItem'),
    ('purchases', 'Requisition'),
    ('purchases', 'SupplierPaymentAllocation'),
    ('purchases', 'SupplierPayment'),
    ('purchases', 'PurchaseReturnItem'),
    ('purchases', 'PurchaseReturn'),
    ('purchases', 'GoodsReceiptItem'),
    ('purchases', 'GoodsReceipt'),
    ('purchases', 'PurchaseOrderItem'),
    ('purchases', 'PurchaseOrder'),

    # --- Le stock : les MOUVEMENTS et les comptages, puis les soldes.
    ('inventory', 'InventoryCount'),
    ('inventory', 'InventorySession'),
    ('inventory', 'StockAdjustmentItem'),
    ('inventory', 'StockAdjustment'),
    ('inventory', 'StockTransferItem'),
    ('inventory', 'StockTransfer'),
    ('inventory', 'StockMovement'),
    ('inventory', 'StockBatch'),
    ('inventory', 'Stock'),

    # --- La caisse.
    ('cashbook', 'CashMovement'),
    ('cashbook', 'Expense'),

    # --- La dette et la fidélité des clients. Les CLIENTS restent.
    ('contacts', 'CustomerTransaction'),
    ('contacts', 'CustomerBalance'),
    ('settings', 'LoyaltyTransaction'),
    ('settings', 'CustomerLoyalty'),

    # --- Les compteurs de documents : sans eux, la numérotation reprendrait où
    # l'essai l'avait laissée, sur un établissement qui n'a plus aucune pièce.
    ('core', 'DocumentSequence'),
]

#: Ce qu'elle GARDE, et c'est la moitié du contrat.
#:
#: Articles, catégories, marques, unités, tarifs, entrepôts, caisses, moyens de
#: paiement, devises, programme de fidélité, réglages, membres, clients et
#: fournisseurs. Un marchand qui remet son essai à zéro veut refaire ses ventes,
#: pas resaisir son catalogue.
GARDES_PAR_LA_REMISE_A_ZERO = [
    'products.Product', 'products.Category', 'products.Brand', 'products.Unit',
    'products.PriceList', 'products.ProductPrice', 'products.ProductVariant',
    'inventory.Warehouse', 'inventory.StockLocation',
    'sales.Register', 'sales.PaymentMethod',
    'contacts.Customer', 'contacts.Supplier', 'contacts.SupplierProduct',
    'cashbook.IncomeCategory', 'cashbook.ExpenseCategory',
    'settings.LoyaltyProgram', 'settings.LoyaltyReward',
    'settings.OrganizationCurrency', 'settings.OrganizationSettings',
    'organizations.Branch', 'organizations.OrganizationMembership',
]


class RemiseAZeroRefusee(Exception):
    """Refus DÉTERMINISTE : l'établissement n'est pas un essai, ou pas confirmé."""


@transaction.atomic
def reset_trial_data(organization, *, confirmation: str, dry_run: bool = False) -> dict:
    """
    Efface les OPÉRATIONS d'un établissement d'essai, et garde sa configuration.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ T8 DU RETOUR DE TEST, DEMANDÉ DEUX FOIS.                                │
    │                                                                          │
    │ « Supprimer les ventes et les opérations effectuées » après un essai.    │
    │ L'annulation comptable d'une vente existe et contrepasse ; ce qui        │
    │ n'existait nulle part, c'est la remise à zéro.                           │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ TROIS VERROUS, ET IL EN FAUT TROIS.

    1. L'abonnement doit être en statut `trial`. Un établissement en production
       porte des pièces comptables et le papier déjà remis à ses clients : rien
       ne doit pouvoir les effacer, pas même son propriétaire.
    2. La `confirmation` doit être le NOM de l'établissement, saisi. Un bouton
       derrière un simple « êtes-vous sûr » se presse par réflexe.
    3. L'appelant vérifie le rôle (`IsTenantOwner`) : ce n'est pas un geste de
       comptoir.

    ⚠ ON N'OFFRE PAS LA SUPPRESSION D'UNE VENTE RÉGLÉE, et c'est pourquoi cette
    opération est globale : effacer une pièce isolée trouerait la série de
    numéros et contredirait le papier déjà remis au client. Ici il ne reste
    AUCUNE pièce, et les compteurs repartent de zéro avec elles.

    `dry_run` rend les mêmes comptes sans rien écrire : c'est ce que l'écran
    montre avant de demander la confirmation.
    """
    from django.apps import apps

    attendu = (organization.name or '').strip()
    if (confirmation or '').strip() != attendu:
        raise RemiseAZeroRefusee(
            f"Saisissez le nom de l'établissement pour confirmer : « {attendu} »."
        )

    abonnement = (
        organization.subscriptions.order_by('-created_at').first()
        if hasattr(organization, 'subscriptions') else None
    )
    if abonnement is None or not abonnement.is_trial:
        raise RemiseAZeroRefusee(
            "La remise à zéro est réservée aux établissements en période d'essai. "
            "Une vente réglée ne s'efface pas : elle s'annule, et la contrepassation "
            "laisse sa trace."
        )

    comptes = {}
    for app_label, nom in EFFACES_PAR_LA_REMISE_A_ZERO:
        modele = apps.get_model(app_label, nom)
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ `with_deleted()`, ET SURTOUT PAS `all_objects`.                  │
        # │                                                                  │
        # │ MESURÉ : `all_objects` n'existe NULLE PART dans ce dépôt - le    │
        # │ balayage ne trouvait que la ligne qui le cherchait. `getattr`     │
        # │ rendait donc toujours `None`, on retombait sur `objects`, et      │
        # │ `TenantSoftDeleteManager` filtre `is_deleted=False`.              │
        # │                                                                  │
        # │ Conséquence sur les sept modèles à suppression douce (`Sale`,     │
        # │ `SaleReturn`, `Quotation`, `Requisition`, `StockTransfer`,        │
        # │ `StockAdjustment`, `InventorySession`) : une vente annulée        │
        # │                                                                   │
        # │ ⚠ `Requisition` N'EN ÉTAIT PAS, ET C'ÉTAIT UN AUTRE DÉFAUT.      │
        # │   Son manager était le `models.Manager` automatique, qui ne filtre │
        # │   rien : elle tombait sur `base.all()` et voyait tout. La phrase   │
        # │   ci-dessus ne vaut donc que depuis que le manager lui a été posé  │
        # │   (session du 2026-10-12) - avant, elle était fausse pour elle, et │
        # │   juste pour les six autres.                                       │
        # │ SURVIVAIT pendant que ses `SaleItem` et ses `Payment` - de        │
        # │ simples `TenantModel` - étaient effacés. Il restait une vente     │
        # │ SANS LIGNES, sous un `DocumentSequence` remis à zéro, et les      │
        # │ comptes rendus au marchand sous-estimaient ce qui reste.          │
        # │                                                                  │
        # │ L'échappatoire réelle est `with_deleted()`, portée par            │
        # │ `SoftDeleteManager` et `TenantSoftDeleteManager`.                 │
        # └──────────────────────────────────────────────────────────────────┘
        base = modele.objects
        complet = (
            base.with_deleted() if hasattr(base, 'with_deleted') else base.all()
        )
        qs = complet.filter(organization=organization)
        n = qs.count()
        if n:
            comptes[f'{app_label}.{nom}'] = n
            if not dry_run:
                qs.delete()

    if dry_run:
        # ⚠ On annule ce que la transaction aurait pu écrire : `count()` n'écrit
        # rien, mais un `dry_run` qui laisserait la transaction ouverte sur un
        # chemin futur serait un piège.
        transaction.set_rollback(True)

    return comptes
