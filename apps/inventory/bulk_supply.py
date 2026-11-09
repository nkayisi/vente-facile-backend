"""
L'approvisionnement de MASSE : la feuille, puis son envoi.

┌──────────────────────────────────────────────────────────────────────────────┐
│ L'APPROVISIONNEMENT NE SE FAISAIT QU'ARTICLE PAR ARTICLE.                    │
│                                                                              │
│ Le formulaire de `stock/movements` saisit UN produit à la fois, avec son      │
│ conditionnement et ses quatre prix. Un marchand qui revient du marché avec    │
│ quarante références devait ouvrir quarante fois le même dialogue, et          │
│ retrouver chaque article dans un catalogue de plusieurs centaines.            │
│                                                                              │
│ On désigne donc un PÉRIMÈTRE - des catégories, une liste d'articles, ce qui   │
│ est sous le seuil - exactement comme une session d'inventaire désigne ce       │
│ qu'elle compte, et le serveur pré-remplit la feuille.                         │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ CE N'EST PAS UNE COMMANDE D'ACHAT, et c'est la frontière à tenir.
  `GoodsReceipt` fait déjà de l'entrée multi-ligne avec brouillon QUAND il y a
  un fournisseur et une commande - `PurchaseOrder.supplier` est obligatoire. Le
  trou que ce module comble est l'entrée de masse SANS commande, celle du
  marchand qui revient du marché. On ne construit pas un troisième document
  parallèle : chaque ligne devient un `StockMovement` ordinaire.

⚠ AUCUNE ARITHMÉTIQUE DE STOCK N'EST RÉÉCRITE ICI. Le verrou sur la ligne de
  stock, le lot FIFO, le coût moyen pondéré, le partage scellé/vrac et le report
  des prix vivent dans `StockMovementCreateSerializer` et
  `stock_movements.create_stock_movement`. Ce module ne fait que BOUCLER dessus.
  C'est la règle de §5.5 : la parité entre la vue et le journal n'est pas
  surveillée, elle est structurelle.
"""
from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from apps.core.warehouse_scope import assert_warehouse_allowed_for_request

from .models import STOCK_IN_MOVEMENT_TYPES, Stock
from .packaging import PackagingService

#: Borne dure de la feuille.
#:
#: ⚠ IL N'Y A PAS DE PAGINATION, ET C'EST VOULU : un formulaire se remplit d'un
#:   bloc. Mais `scope=all` sur un catalogue de cinq mille références rendrait
#:   un écran inutilisable, et tronquer EN SILENCE ferait croire la livraison
#:   saisie en entier. La feuille porte donc un drapeau `truncated` que l'écran
#:   AFFICHE.
LIGNES_MAX = 300


def _profil_article(produit, stock=None) -> dict:
    """Ce qu'une ligne de feuille doit porter pour être saisissable hors ligne."""
    facteur = PackagingService.factor(produit)
    if stock is not None:
        paquets, vrac = PackagingService.stored_split(stock, facteur)
        total = stock.quantity or Decimal('0')
    else:
        # ⚠ UN ARTICLE SANS LIGNE DE STOCK SORT À ZÉRO, IL NE DISPARAÎT PAS :
        #   c'est le PREMIER approvisionnement d'une référence neuve, et il doit
        #   être possible. Voir `_articles_du_perimetre`.
        paquets, vrac, total = Decimal('0'), Decimal('0'), Decimal('0')

    return {
        'product': str(produit.id),
        'name': produit.name,
        'sku': produit.sku,
        'selling_mode': produit.selling_mode,
        'packaging_factor': facteur,
        'retail_unit_label': PackagingService.retail_unit_label(produit),
        'package_unit_label': PackagingService.package_unit_label(produit),
        # Le partage ACTUEL du rayon : c'est lui qui permet de juger ce qu'il
        # faut commander. « 15 casiers + 10 bouteilles » se lit, « 370 » non.
        'stock': str(total),
        'stock_packages': str(paquets),
        'stock_loose': str(vrac),
        'stock_display': PackagingService.format_split(produit, paquets, vrac),
        'reorder_point': str(produit.reorder_point or Decimal('0')),
        # ⚠ SANS LUI, LA FEUILLE NE PEUT PAS RENDRE LA DATE OBLIGATOIRE là où
        #   le formulaire d'un seul mouvement le fait. Un lot périssable entré
        #   sans péremption n'alerte jamais, et la marchandise se vend après sa
        #   date : c'est le serializer qui refuse, mais le marchand l'apprend
        #   alors APRÈS avoir rempli quarante lignes.
        'has_expiry_date': produit.has_expiry_date,
        # Les quatre prix de la fiche, pour pré-remplir les blocs de prix. Le
        # marchand saisit ce qu'il a payé ; ceux-ci sont ce qu'il payait avant.
        'cost_price': str(produit.cost_price or Decimal('0')),
        'package_cost_price': str(produit.package_cost_price or Decimal('0')),
        'selling_price': str(produit.selling_price or Decimal('0')),
        'wholesale_price': str(produit.wholesale_price or Decimal('0')),
    }


def _articles_du_perimetre(organization, warehouse, *, scope, categories, products, search):
    """
    Les articles que la feuille propose, et leur stock dans CET entrepôt.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ ON APPROVISIONNE CE QU'ON N'A PAS : LE JOINT EST EXTERNE.                │
    │                                                                          │
    │ `inventory.services.target_products` exige                               │
    │ `stocks__quantity__gt=reserved_quantity` - on ne COMPTE que ce qu'on a.  │
    │ Recopier ce filtre ici ferait disparaître de la feuille exactement les   │
    │ articles qu'on vient réapprovisionner, et le premier approvisionnement   │
    │ d'une référence neuve serait impossible.                                 │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ `track_inventory=False` EST EXCLU : un service n'a pas de stock. Même
      critère que `target_products` et que `lignes_proposees`.
    """
    from apps.products.models import Category, Product

    if scope == 'low':
        # ⚠ UNE SEULE SOURCE DU « SOUS LE SEUIL ». Un article qui déclenche une
        #   alerte et qui n'apparaîtrait pas ici ferait douter des deux écrans.
        from apps.purchases.services import lignes_proposees

        lignes = lignes_proposees(organization, warehouse)
        produits = [ligne['product'] for ligne in lignes]
    else:
        qs = Product.objects.filter(
            organization=organization,
            is_active=True,
            track_inventory=True,
        ).select_related('unit', 'packaging_unit').order_by('name')

        if scope == 'category':
            if not categories:
                raise serializers.ValidationError({
                    'categories': "Choisissez au moins une catégorie."
                })
            # Filtrer une catégorie, c'est filtrer son SOUS-ARBRE : « Boissons »
            # doit ramener « Boissons > Sodas ».
            qs = qs.filter(category_id__in=Category.subtree_ids(categories))
        elif scope == 'product':
            if not products:
                raise serializers.ValidationError({
                    'products': "Choisissez au moins un article."
                })
            qs = qs.filter(id__in=products)
        elif scope != 'all':
            raise serializers.ValidationError({
                'scope': f"Périmètre inconnu : « {scope} »."
            })

        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(sku__icontains=search)
                | Q(barcode__icontains=search)
            )
        produits = list(qs[: LIGNES_MAX + 1])

    tronquee = len(produits) > LIGNES_MAX
    produits = produits[:LIGNES_MAX]

    # ⚠ UNE SEULE REQUÊTE POUR TOUS LES STOCKS, et c'est ce qui rend la feuille
    #   tenable : une lecture par article coûterait trois cents allers-retours.
    stocks = {
        s.product_id: s
        for s in Stock.objects.filter(
            organization=organization,
            warehouse=warehouse,
            product__in=produits,
        )
    }
    return produits, stocks, tronquee


def _selection_nommee(organization, *, scope, categories, products) -> dict:
    """
    Les catégories et articles CHOISIS, avec leur nom.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE PÉRIMÈTRE VOYAGE EN IDENTIFIANTS, DONC C'EST LE SERVEUR QUI LE NOMME. │
    │                                                                          │
    │ L'écran passe le périmètre en paramètres d'URL pour que la feuille soit  │
    │ rechargeable. Sur un rechargement, il ne détient plus que des UUID : sans │
    │ cet écho, ses puces afficheraient `3f2a9c1e-…` et le marchand ne pourrait │
    │ pas savoir laquelle retirer. Les recopier dans l'URL donnerait deux       │
    │ sources pour le même mot, et un nom porte virgules et accents.           │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ Une seule requête, et SEULEMENT pour la portée qui en a besoin : les deux
      autres portées n'ont rien à nommer.
    """
    from apps.products.models import Category, Product

    def nommer(modele, ids):
        if not ids:
            return []
        return [
            {'id': str(o.id), 'name': o.name}
            for o in modele.objects.filter(organization=organization, id__in=ids)
        ]

    return {
        'categories': nommer(Category, categories) if scope == 'category' else [],
        'products': nommer(Product, products) if scope == 'product' else [],
    }


def build_supply_sheet(organization, warehouse, *, scope='all', categories=None,
                       products=None, search=None) -> dict:
    """La feuille pré-remplie d'un périmètre. Lecture seule."""
    produits, stocks, tronquee = _articles_du_perimetre(
        organization, warehouse,
        scope=scope, categories=categories, products=products, search=search,
    )
    return {
        'warehouse': str(warehouse.id),
        'warehouse_name': warehouse.name,
        'scope': scope,
        'count': len(produits),
        'truncated': tronquee,
        'limit': LIGNES_MAX,
        'selection': _selection_nommee(
            organization, scope=scope, categories=categories, products=products,
        ),
        'lines': [_profil_article(p, stocks.get(p.id)) for p in produits],
    }


def create_bulk_supply(*, organization, user, warehouse, movement_type, lines,
                       notes='', request=None) -> list:
    """
    Écrit N mouvements d'entrée, d'un seul bloc. Appelée par la vue ET le journal.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UN SEUL `atomic` AUTOUR DE TOUT, ET C'EST L'EXCEPTION ASSUMÉE À LA       │
    │ DOCTRINE « VERDICT PAR OPÉRATION ».                                      │
    │                                                                          │
    │ Une livraison est un TOUT : la moitié d'un bon de livraison enregistrée  │
    │ est pire que rien - le magasinier ne sait plus ce qui est entré, et       │
    │ reprendre la saisie fait entrer deux fois ce qui est déjà passé.          │
    │                                                                          │
    │ C'est précisément la raison pour laquelle le message d'erreur, lui, est   │
    │ PAR LIGNE : si tout tombe, il faut savoir laquelle corriger.              │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from .serializers import StockMovementCreateSerializer
    from .stock_movements import create_stock_movement

    if movement_type not in STOCK_IN_MOVEMENT_TYPES:
        # ⚠ `movement_type` EST UNIQUE POUR TOUT L'ENVOI, et borné aux ENTRÉES :
        #   ce n'est pas un panier d'écritures hétérogènes, c'est une livraison.
        raise serializers.ValidationError({
            'movement_type': (
                "Un approvisionnement de masse n'enregistre que des entrées."
            )
        })

    if request is not None:
        assert_warehouse_allowed_for_request(request, warehouse.id)

    # ⚠ UNE LIGNE À QUANTITÉ NULLE EST IGNORÉE, PAS REFUSÉE. Une feuille
    #   pré-remplie de quarante articles dont on n'en reçoit que douze est le cas
    #   ORDINAIRE : refuser obligerait à effacer les vingt-huit autres à la main.
    retenues = [(i, ligne) for i, ligne in enumerate(lines or []) if _a_une_quantite(ligne)]

    if not retenues:
        # Mais si TOUTES sont nulles, l'envoi réussirait sans rien faire, et le
        # magasinier croirait sa livraison enregistrée.
        raise serializers.ValidationError({
            'lines': "Aucune quantité saisie : il n'y a rien à enregistrer."
        })

    _refuser_les_doublons(retenues)

    erreurs: dict = {}
    serializers_valides = []
    for index, ligne in retenues:
        corps = dict(ligne)
        corps['warehouse'] = str(warehouse.id)
        corps['movement_type'] = movement_type
        if notes and not corps.get('notes'):
            corps['notes'] = notes

        # ⚠ L'ORGANISATION VOYAGE DANS LE CONTEXTE, ET CE N'EST PAS DÉCORATIF.
        #   `_validate_product_prices` l'exige pour opposer `products.edit` au
        #   report des prix, et il REFUSE quand il ne la trouve pas - « un
        #   contrôle de droit qui ignore sur quoi il porte refuse ». Ce chemin
        #   construit le serializer lui-même, donc sans `view` : sans cette clé,
        #   TOUTE ligne cochée « reporter les prix » était refusée, pour tous les
        #   rôles, propriétaire compris - et sur le terminal le refus condamne la
        #   livraison entière. C'est la même clé que `stock_movement.create` pose
        #   dans `apps/sync/handlers.py`, pour la même raison.
        s = StockMovementCreateSerializer(
            data=corps,
            context={'request': request, 'organization': organization},
        )
        if s.is_valid():
            serializers_valides.append(s)
        else:
            erreurs[index] = s.errors

    if erreurs:
        # ⚠ TOUTES LES ERREURS SONT COLLECTÉES, ET INDEXÉES PAR LIGNE. Lever à
        #   la première ferait corriger une erreur à la fois, soit quarante
        #   allers-retours sur une feuille de quarante lignes ; lever sans index
        #   ferait lire « Indiquez une quantité » sans savoir laquelle.
        raise serializers.ValidationError({'lines': erreurs})

    mouvements = []
    with transaction.atomic():
        for s in serializers_valides:
            mouvements.append(create_stock_movement(
                s, organization=organization, user=user, request=request,
            ))
    return mouvements


def _a_une_quantite(ligne) -> bool:
    """
    La ligne porte-t-elle une quantité, sur l'un des trois champs ?

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ SEUL UN ZÉRO EXACT - OU UN CHAMP VIDE - VEUT DIRE « RIEN REÇU ».         │
    │                                                                          │
    │ Tout le reste est RETENU, et c'est la pièce qui compte : une première    │
    │ écriture ne gardait que les valeurs `> 0`, si bien qu'une quantité       │
    │ NÉGATIVE était silencieusement écartée. Le magasinier qui tape « -5 »    │
    │ voyait sa ligne disparaître de la livraison sans un mot, et croyait       │
    │ l'article enregistré. ⚠ Mesuré : le test des erreurs indexées ne         │
    │ nommait que la ligne 1, et la 2 avait été avalée.                        │
    │                                                                          │
    │ Un négatif et une valeur illisible sont des FAUTES DE SAISIE : on les    │
    │ retient pour que le serializer les refuse en NOMMANT leur ligne.         │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    for cle in ('quantity', 'package_quantity', 'loose_quantity'):
        valeur = ligne.get(cle)
        if valeur in (None, ''):
            continue
        try:
            if Decimal(str(valeur)) != 0:
                return True
        except Exception:
            return True
    return False


def _refuser_les_doublons(retenues) -> None:
    """
    Deux lignes sur le même article sont REFUSÉES, en le nommant.

    ⚠ Elles donneraient deux mouvements dont le coût moyen pondéré dépend de
      l'ordre d'exécution : le rayon finirait avec un `avg_cost` que personne ne
      peut reconstituer. Et le magasinier qui saisit deux fois la même référence
      a presque toujours voulu corriger la première, pas en ajouter une seconde.
    """
    vus: dict = {}
    doublons = {}
    for index, ligne in retenues:
        produit = str(ligne.get('product') or '')
        if not produit:
            continue
        if produit in vus:
            doublons[index] = [
                f"Cet article est déjà saisi à la ligne {vus[produit] + 1}."
            ]
        else:
            vus[produit] = index
    if doublons:
        raise serializers.ValidationError({'lines': doublons})
