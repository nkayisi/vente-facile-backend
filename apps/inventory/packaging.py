"""
Conversion et déconditionnement pour les produits vendus en gros et au détail.

Point d'entrée **unique** du projet pour tout ce qui touche au conditionnement :
conversion entre unités, partage scellé/vrac, ouverture d'un conditionnement,
et formatage lisible. Aucun contrôleur, aucun serializer et aucun écran ne doit
refaire cette arithmétique de son côté.

Modèle de données
-----------------
``Stock.quantity`` est la **seule** source de vérité de la quantité totale, en
unité de détail. ``Stock.loose_quantity`` n'est pas un second compteur : c'est
la part de ce total qui se trouve hors emballage scellé.

    paquets scellés = (quantity - loose_quantity) // facteur
    unités en vrac  = loose_quantity + reste de la division

Le « reste » (dit *orphelin*) est réintégré au vrac plutôt que traité comme une
anomalie. Cela rend la définition **auto-cicatrisante** dans deux situations qui
arrivent en pratique :

1. l'activation du mode gros sur un produit ayant déjà du stock (37 bouteilles
   avec un facteur de 12 : 3 paquets + 1 bouteille, sans migration de données) ;
2. les chemins d'écriture qui ne connaissent pas encore le conditionnement
   (transferts, réceptions fournisseur, synchronisation mobile) et qui font
   varier ``quantity`` sans toucher à ``loose_quantity``.

Un invariant strict aurait fait échouer ces cas ; ici la représentation reste
cohérente et se corrige d'elle-même au mouvement suivant.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError


ZERO = Decimal('0.000')
TWO_PLACES = Decimal('0.01')


def _plural(word: str, count) -> str:
    """
    Pluriel français approximatif d'un libellé d'unité saisi librement.

    ``Unit.name`` est du texte libre : un pluriel fiable est hors de portée. La
    règle simple couvre les cas réels du domaine (paquet, carton, casier, sac,
    bouteille, pièce, sachet) ; une saisie exotique produira au pire un pluriel
    maladroit, jamais une erreur.

    La casse du mot d'origine est respectée : un marchand qui saisit
    « PLAQUETTE » doit lire « 10 PLAQUETTES », pas « 10 PLAQUETTEs ».
    """
    word = (word or '').strip()
    if not word:
        return ''
    if abs(Decimal(count)) < 2:
        return word
    if word[-1].lower() in ('s', 'x', 'z'):
        return word
    suffixe = 'S' if word == word.upper() and word.lower() != word else 's'
    return f"{word}{suffixe}"


def _fmt_number(value) -> str:
    """Formate une quantité sans zéros décimaux inutiles : 10.000 → « 10 »."""
    quantized = Decimal(value).normalize()
    if quantized == quantized.to_integral_value():
        quantized = quantized.to_integral_value()
    return f"{quantized:f}"


class PackagingService:
    """Conversion, partage scellé/vrac et déconditionnement."""

    # -- Lecture ------------------------------------------------------------

    @staticmethod
    def is_dual(product) -> bool:
        """True si le produit se vend par conditionnement (gros seul ou mixte)."""
        return getattr(product, 'selling_mode', 'retail_only') != 'retail_only'

    @staticmethod
    def factor(product):
        """
        Nombre d'unités de détail par conditionnement, ou ``None``.

        Ne lève **jamais** : cette méthode est appelée depuis des serializers de
        liste, où une exception se traduirait par une erreur 500 sur le
        catalogue entier. Un produit mal configuré est traité comme un produit
        mono-unité.
        """
        if not PackagingService.is_dual(product):
            return None
        value = getattr(product, 'units_per_package', None)
        if not value or int(value) < 2:
            return None
        return int(value)

    @staticmethod
    def retail_unit_label(product) -> str:
        unit = getattr(product, 'unit', None)
        return getattr(unit, 'name', '') or ''

    @staticmethod
    def package_unit_label(product) -> str:
        unit = getattr(product, 'packaging_unit', None)
        return getattr(unit, 'name', '') or ''

    # -- Arithmétique pure --------------------------------------------------

    @staticmethod
    def to_base(product, package_quantity=0, loose_quantity=0) -> Decimal:
        """
        Convertit une saisie « X conditionnements + Y unités » en unité de base.

        Sans conditionnement configuré, seule la part détail est retenue : un
        produit mono-unité ne peut pas se voir attribuer des paquets.
        """
        package_quantity = Decimal(package_quantity or 0)
        loose_quantity = Decimal(loose_quantity or 0)
        factor = PackagingService.factor(product)
        if factor is None:
            return loose_quantity.quantize(ZERO)
        return (package_quantity * factor + loose_quantity).quantize(ZERO)

    # -- Arithmétique des prix ----------------------------------------------
    #
    # Note pour les passages futurs : `apps/purchases/serializers.py` fait la
    # même division mais avec `data.setdefault`, c'est-à-dire qu'un prix
    # unitaire saisi l'emporte sur le prix au contenant. Cet écart est
    # volontaire, ne pas l'uniformiser sans reprendre les tests des achats.

    @staticmethod
    def unit_cost_from_package(package_cost, factor) -> Decimal:
        """Prix d'une unité de détail déduit du prix d'un conditionnement."""
        if not factor or factor < 2:
            return Decimal(package_cost or 0).quantize(TWO_PLACES)
        return (Decimal(package_cost or 0) / factor).quantize(TWO_PLACES)

    @staticmethod
    def package_cost_from_unit(unit_cost, factor) -> Decimal:
        """Prix d'un conditionnement déduit du prix d'une unité de détail."""
        if not factor or factor < 2:
            return Decimal(unit_cost or 0).quantize(TWO_PLACES)
        return (Decimal(unit_cost or 0) * factor).quantize(TWO_PLACES)

    @staticmethod
    def blended_unit_cost(
        *, package_quantity, package_cost, loose_quantity, loose_cost, factor
    ) -> Decimal:
        """
        Coût unitaire d'une entrée achetée en partie au contenant, en partie
        à l'unité : « ce que j'ai payé divisé par ce que j'ai reçu ».

        C'est la seule définition cohérente avec l'aval, où le lot FIFO et le
        coût moyen pondéré valorisent tous deux à ``unit_cost × quantité``.

        La quantisation n'intervient qu'à la fin : arrondir chaque terme ferait
        dériver la valorisation de plusieurs francs sur une entrée mixte.
        """
        package_quantity = Decimal(package_quantity or 0)
        loose_quantity = Decimal(loose_quantity or 0)
        base_quantity = package_quantity * (factor or 0) + loose_quantity
        if base_quantity <= 0:
            return Decimal(loose_cost or 0).quantize(TWO_PLACES)

        total_value = (
            package_quantity * Decimal(package_cost or 0)
            + loose_quantity * Decimal(loose_cost or 0)
        )
        return (total_value / base_quantity).quantize(TWO_PLACES)

    @staticmethod
    def split(base_quantity, loose_quantity, factor):
        """
        Partage une quantité de base en (conditionnements scellés, unités en vrac).

        Fonction **pure**, sans accès base de données.

        Garantit toujours ``scellés × facteur + vrac == base_quantity``, y
        compris sur un stock négatif (entrepôt autorisant le découvert), où le
        déficit est porté par le vrac plutôt que par un nombre de paquets
        négatif qui n'aurait aucun sens à l'écran.
        """
        base_quantity = Decimal(base_quantity or 0)
        loose_quantity = Decimal(loose_quantity or 0)

        if not factor or factor < 2:
            return 0, base_quantity.quantize(ZERO)

        # Le vrac ne peut pas excéder le total ni être négatif.
        loose_quantity = max(ZERO, min(loose_quantity, max(base_quantity, ZERO)))

        sealed_base = base_quantity - loose_quantity
        if sealed_base < 0:
            return 0, base_quantity.quantize(ZERO)

        sealed = int(sealed_base // factor)
        remainder = sealed_base - (sealed * factor)
        return sealed, (loose_quantity + remainder).quantize(ZERO)

    @staticmethod
    def loose_share(product, base_quantity, loose_hint=None) -> Decimal:
        """
        Part d'une quantité qui circule hors emballage scellé.

        Utile partout où la quantité qui bouge peut différer de la saisie
        d'origine : réception partielle, refus qualité, transfert reçu
        incomplet. La part scellée est plafonnée par ce qui reste réellement, et
        l'orphelin retombe dans le vrac - un carton entamé ne se rescelle pas.

        Pour un produit sans conditionnement, tout est vrac : la valeur n'est
        alors lue par personne, ``apply_delta`` ignorant le vrac dans ce cas.
        """
        base_quantity = Decimal(base_quantity or 0)
        factor = PackagingService.factor(product)
        if factor is None:
            return base_quantity.quantize(ZERO)

        hint = max(ZERO, min(Decimal(loose_hint or 0), max(base_quantity, ZERO)))
        _sealed, loose = PackagingService.split(base_quantity, hint, factor)
        return loose

    @staticmethod
    def available_split(stock, factor):
        """
        Partage la quantité **disponible** (hors réservations) en scellé/vrac.

        Les réservations ne distinguent pas le scellé du vrac ; on les impute
        donc d'abord au scellé. L'approximation est volontairement conservatrice
        : elle peut refuser une vente en gros de justesse, jamais en autoriser
        une qui viderait un paquet déjà promis à un devis.
        """
        available_base = Decimal(stock.quantity) - Decimal(stock.reserved_quantity)
        available_loose = min(
            Decimal(stock.loose_quantity), max(available_base, ZERO)
        )
        return PackagingService.split(available_base, available_loose, factor)

    # -- Formatage ----------------------------------------------------------

    @staticmethod
    def format_quantity(product, base_quantity, loose_quantity=None) -> str:
        """
        Rend une quantité lisible : « 1 paquet + 10 bouteilles ».

        Pour un produit mono-unité, retourne la quantité suivie de son unité.
        La partie nulle est omise : « 2 paquets », pas « 2 paquets + 0 bouteille ».
        """
        base_quantity = Decimal(base_quantity or 0)
        factor = PackagingService.factor(product)
        retail_label = PackagingService.retail_unit_label(product)

        if factor is None:
            text = _fmt_number(base_quantity)
            return f"{text} {_plural(retail_label, base_quantity)}".strip()

        if loose_quantity is None:
            loose_quantity = ZERO
        sealed, loose = PackagingService.split(base_quantity, loose_quantity, factor)
        package_label = PackagingService.package_unit_label(product)

        parts = []
        if sealed:
            parts.append(f"{sealed} {_plural(package_label, sealed)}".strip())
        if loose or not parts:
            parts.append(f"{_fmt_number(loose)} {_plural(retail_label, loose)}".strip())
        return ' + '.join(parts)

    @staticmethod
    def format_movement_quantity(movement) -> str:
        """
        Rend la quantité d'un mouvement dans les termes de sa saisie d'origine.

        L'historique doit se relire comme le marchand l'a vécu : « 10 cartons +
        5 bouteilles » plutôt que « 125 ». On repart donc des champs figés sur le
        mouvement (``input_*``, ``packaging_factor``) et non de la configuration
        actuelle du produit, qui a pu changer depuis.

        La valeur retournée est toujours positive : le sens du mouvement se lit
        à son type, pas à un signe collé au libellé.
        """
        product = movement.product
        magnitude = abs(Decimal(movement.quantity or 0))
        factor = int(movement.packaging_factor or 0)
        retail_label = PackagingService.retail_unit_label(product)

        if factor < 2:
            # Sans facteur figé, le mouvement s'est joué à l'unité : le lire au
            # conditionnement d'aujourd'hui réécrirait un passé qui l'ignorait.
            return f"{_fmt_number(magnitude)} {_plural(retail_label, magnitude)}".strip()

        packages = abs(Decimal(movement.input_package_quantity or 0))
        loose = abs(Decimal(movement.input_loose_quantity or 0))
        if packages <= 0 and loose <= 0:
            # Mouvement écrit par un chemin qui ignore le conditionnement : on
            # répartit le total au facteur enregistré.
            packages, loose = PackagingService.split(magnitude, ZERO, factor)

        package_label = PackagingService.package_unit_label(product)

        parts = []
        if packages:
            parts.append(
                f"{_fmt_number(packages)} {_plural(package_label, packages)}".strip()
            )
        if loose or not parts:
            parts.append(f"{_fmt_number(loose)} {_plural(retail_label, loose)}".strip())
        return ' + '.join(parts)

    # -- Mutations (le lock est pris par l'appelant) ------------------------

    @staticmethod
    def assert_sealed_available(stock, product, package_quantity, action_label='vendre'):
        """
        Refuse une sortie en gros quand les conditionnements scellés manquent.

        On ne reconditionne pas : des unités en vrac ne se remettent pas dans un
        emballage neuf. Le message propose l'équivalent en détail, qui est la
        seule issue réelle pour l'opérateur ; ``action_label`` accorde le verbe
        au chemin appelant (vendre, transférer…).

        Sans objet quand l'entrepôt autorise le stock négatif : y imposer ce
        contrôle retirerait une possibilité qui existe aujourd'hui.
        """
        package_quantity = Decimal(package_quantity or 0)
        if package_quantity <= 0:
            return

        factor = PackagingService.factor(product)
        if factor is None:
            return

        warehouse = stock.warehouse
        if getattr(warehouse, 'allow_negative_stock', False):
            return

        sealed, loose = PackagingService.available_split(stock, factor)
        if package_quantity <= sealed:
            return

        package_label = _plural(
            PackagingService.package_unit_label(product), package_quantity
        )
        retail_label = _plural(PackagingService.retail_unit_label(product), 2)
        available_base = max(
            Decimal(stock.quantity) - Decimal(stock.reserved_quantity), ZERO
        )
        raise ValidationError({
            'items': (
                f"{product.name} : seulement {sealed} "
                f"{_plural(PackagingService.package_unit_label(product), sealed)} "
                f"en stock, {_fmt_number(package_quantity)} {package_label} demandés. "
                f"Les {retail_label} déjà sorties d'un emballage ne peuvent pas y être "
                f"remises. Vous pouvez {action_label} jusqu'à {_fmt_number(available_base)} "
                f"{retail_label} au détail."
            )
        })

    @staticmethod
    def ensure_loose_available(
        stock, product, needed_loose, user=None,
        reference_type='', reference_id=None, force=False,
    ):
        """
        Ouvre autant de conditionnements que nécessaire pour servir une vente
        au détail, et trace l'opération.

        **Contrat de verrouillage** : ``stock`` doit déjà être verrouillé par
        l'appelant (``select_for_update``). C'est ce qui empêche deux ventes
        simultanées d'ouvrir chacune un paquet pour le même besoin.

        Retourne ``(nombre de conditionnements ouverts, mouvement)`` -
        ``(0, None)`` si le vrac disponible suffisait.

        Le mouvement tracé porte ``quantity = 0`` : un déconditionnement ne
        change pas la quantité totale, il ne fait que déplacer du scellé vers le
        vrac. La réconciliation « somme des mouvements = stock » reste donc
        vraie.
        """
        from apps.inventory.models import StockMovement

        needed_loose = Decimal(needed_loose or 0)
        if needed_loose <= 0:
            return 0, None

        factor = PackagingService.factor(product)
        if factor is None:
            return 0, None

        assert stock.pk is not None, "ensure_loose_available exige un Stock persisté"

        sealed, loose = PackagingService.split(
            stock.quantity, stock.loose_quantity, factor
        )
        if loose >= needed_loose:
            return 0, None

        retail_label = _plural(PackagingService.retail_unit_label(product), 2)
        package_label = PackagingService.package_unit_label(product)

        # `force` est réservé à l'ouverture décidée par le vendeur : il anticipe
        # ou débloque une vente que le refus automatique vient d'arrêter.
        if not product.allow_auto_unpacking and not force:
            raise ValidationError({
                'items': (
                    f"{product.name} : aucune {_plural(PackagingService.retail_unit_label(product), 1)} "
                    f"à l'unité disponible. Ouvrez un {package_label} pour continuer."
                )
            })

        missing = needed_loose - loose
        # Arrondi supérieur explicite : sur des `Decimal`, `//` tronque vers zéro
        # au lieu de faire un plancher, ce qui ferait ouvrir un paquet de moins
        # que nécessaire.
        packages_to_open = int(
            (missing / factor).to_integral_value(rounding=ROUND_CEILING)
        )
        if packages_to_open <= 0:
            return 0, None

        allow_negative = getattr(stock.warehouse, 'allow_negative_stock', False)
        if packages_to_open > sealed and not allow_negative:
            raise ValidationError({
                'items': (
                    f"{product.name} : stock insuffisant pour servir "
                    f"{_fmt_number(needed_loose)} {retail_label}."
                )
            })

        quantity_snapshot = Decimal(stock.quantity)
        stock.loose_quantity = (
            Decimal(stock.loose_quantity) + packages_to_open * factor
        ).quantize(ZERO)
        stock.save(update_fields=['loose_quantity'])

        movement = StockMovement.objects.create(
            organization=stock.organization,
            product=product,
            variant=stock.variant,
            warehouse=stock.warehouse,
            movement_type='unpack',
            quantity=ZERO,
            quantity_before=quantity_snapshot,
            quantity_after=quantity_snapshot,
            input_package_quantity=Decimal(packages_to_open),
            packaging_factor=factor,
            reference_type=reference_type or '',
            reference_id=reference_id,
            notes=(
                f"Déconditionnement automatique : {packages_to_open} "
                f"{_plural(package_label, packages_to_open)} "
                f"({_fmt_number(Decimal(packages_to_open) * factor)} {retail_label})"
            ),
            created_by=user,
        )
        return packages_to_open, movement

    @staticmethod
    def apply_delta(stock, product, *, delta_base, delta_loose=ZERO):
        """
        Applique une variation de stock en maintenant le partage scellé/vrac.

        Méthode mutante unique : entrées, sorties, retours et annulations
        passent tous par ici, ce qui évite que la mise à jour du vrac ne
        diverge d'un chemin d'écriture à l'autre.

        - vente de 2 paquets + 3 pièces (facteur 12) :
          ``delta_base=-27, delta_loose=-3``
        - approvisionnement de 10 paquets : ``delta_base=+120, delta_loose=0``
        - approvisionnement de 5 pièces :   ``delta_base=+5,   delta_loose=+5``
        - retour ou annulation de 2 pièces : ``delta_base=+2,  delta_loose=+2``

        Un retour ne reconstitue jamais un conditionnement scellé : les unités
        rendues reviennent toujours en vrac.

        Ne sauvegarde pas - l'appelant maîtrise le moment de l'écriture.
        """
        stock.quantity = (Decimal(stock.quantity) + Decimal(delta_base)).quantize(ZERO)

        if not PackagingService.is_dual(product):
            return stock

        new_loose = Decimal(stock.loose_quantity) + Decimal(delta_loose or 0)
        stock.loose_quantity = max(
            ZERO, min(new_loose, max(Decimal(stock.quantity), ZERO))
        ).quantize(ZERO)
        return stock

    @staticmethod
    def touch(stock):
        """Horodate le dernier mouvement, comme le font les autres services."""
        stock.last_movement_at = timezone.now()
        return stock
