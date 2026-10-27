"""
Tout champ de quantité est décimal, à trois décimales, sans exception.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN SEUL CHAMP ENTIER SUFFIT À RAMENER LE DÉFAUT.                             │
│                                                                              │
│ Les quantités se vendent au POIDS depuis octobre : on pèse 0,5 kg de farine  │
│ et on sert 1,5 L au détail. Tout ce qui compte de la marchandise doit donc    │
│ porter trois décimales, comme `SaleItem.quantity` qui est la référence du     │
│ dépôt : `DecimalField(max_digits=15, decimal_places=3)`.                     │
│                                                                              │
│ Le défaut est revenu trois fois par trois chemins différents : un            │
│ `IntegerField` dans un serializer de rapport, un `PositiveIntegerField` sur  │
│ cinq colonnes de seuil, et un `int(float(...))` dans l'import Excel. Aucun    │
│ des trois ne levait : le premier affichait « 0 », le second répondait 400,    │
│ le troisième écrasait la valeur EN SILENCE.                                   │
│                                                                              │
│ Ce balayage est EXHAUSTIF PAR CONSTRUCTION : il part du registre             │
│ d'applications de Django, pas d'une liste tenue à la main. Une liste          │
│ recopiée est exactement ce qui a laissé passer trois codes fantômes dans le   │
│ test de permissions du terminal, qui validait ce qu'il aurait dû refuser.     │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from django.apps import apps as django_apps
from django.db import models
from django.test import SimpleTestCase


class QuantitesDecimalesPartoutTests(SimpleTestCase):
    """
    Les exemptions sont NOMMÉES, une par une, avec leur raison.

    Une exemption sans motif est un oubli qui a trouvé une cachette.
    """

    #: Les comptes qui ne sont pas de la marchandise.
    EXEMPTIONS = {
        # Un contenant contient un nombre ENTIER d'unités : un carton de
        # 12,5 bouteilles n'existe pas. Le plancher à 2 est tenu en base par
        # `product_units_per_package_gte_2`, dans DRF, et dans l'import Excel.
        'products.Product.units_per_package': "le contenu d'un contenant",

        # Instantanés du facteur, FIGÉS au moment de l'acte. Entiers pour la
        # même raison, et figés parce que les relire au conditionnement
        # d'aujourd'hui réécrirait le passé.
        'sales.SaleItem.packaging_factor': "un facteur figé",
        'inventory.Stock.packaging_factor': "un facteur figé",
        'inventory.StockMovement.packaging_factor': "un facteur figé",
        'inventory.StockTransferItem.packaging_factor': "un facteur figé",
        'inventory.StockAdjustmentItem.packaging_factor': "un facteur figé",
        'inventory.InventoryCount.packaging_factor': "un facteur figé",
        'purchases.PurchaseOrderItem.packaging_factor': "un facteur figé",
        'purchases.GoodsReceiptItem.packaging_factor': "un facteur figé",

        # ⚠ TROUVÉ PAR CE BALAYAGE, et par lui seul : aucune exploration ne
        # l'avait vu. Ce n'est pas de la marchandise du marchand mais une ligne
        # de la facture que l'ÉDITEUR lui adresse (« 1 abonnement Standard,
        # 12 mois »). Rien ne s'y pèse, et l'y aligner imposerait une migration
        # sur `subscriptions` pour un champ que le comptoir ne touche jamais.
        'subscriptions.InvoiceItem.quantity': "une ligne de facture d'abonnement",
    }

    #: Ce qui fait d'un nom de champ une QUANTITÉ de marchandise.
    #:
    #: ⚠ `packaging_factor` et `units_per_package` contiennent aussi un mot de
    #: cette liste : ils sont exemptés nommément ci-dessus, et c'est voulu - une
    #: exemption se lit, un motif resserré se devine.
    MOTS = ('quantity', 'quantities')

    def _champs_de_quantite(self):
        for modele in django_apps.get_models():
            if not modele._meta.app_label.startswith('apps') and '.' in modele.__module__:
                pass
            for champ in modele._meta.get_fields():
                if not isinstance(champ, models.Field):
                    continue
                if not any(mot in champ.name for mot in self.MOTS):
                    continue
                yield (
                    f'{modele._meta.app_label}.{modele.__name__}.{champ.name}',
                    champ,
                )

    def test_le_balayage_MORD(self):
        """Un balayage qui ne trouve rien passe au vert et ne prouve rien."""
        vus = [cle for cle, _ in self._champs_de_quantite()]
        self.assertGreaterEqual(
            len(vus), 30,
            f"Le balayage ne voit que {len(vus)} champs de quantité ; il en "
            "existe une trentaine, dont vingt-neuf dans `inventory` seul. Il ne "
            "mesure donc rien.",
        )

    def test_aucune_quantite_n_est_entiere(self):
        entiers = []
        for cle, champ in self._champs_de_quantite():
            if cle in self.EXEMPTIONS:
                continue
            if not isinstance(champ, models.DecimalField):
                entiers.append(f'{cle} : {type(champ).__name__}')

        self.assertEqual(
            entiers, [],
            "Ces champs comptent de la marchandise et ne sont pas décimaux. Un "
            "commerce qui pèse sa farine ne peut alors ni la vendre, ni la "
            "compter, ni s'en alerter au demi-kilo - et le refus arrive en 400 "
            "ou, pire, en troncature silencieuse. Les passer en "
            "`DecimalField(max_digits=15, decimal_places=3)`, comme "
            "`SaleItem.quantity` qui est la référence.",
        )

    def test_toute_quantite_porte_trois_decimales(self):
        mal_dimensionnes = []
        for cle, champ in self._champs_de_quantite():
            if cle in self.EXEMPTIONS or not isinstance(champ, models.DecimalField):
                continue
            if champ.decimal_places != 3:
                mal_dimensionnes.append(f'{cle} : {champ.decimal_places} décimale(s)')

        self.assertEqual(
            mal_dimensionnes, [],
            "Trois décimales, parce que c'est le pas que `PackagingService` "
            "quantise (`ZERO = Decimal('0.000')`) et celui que le comptoir "
            "oppose au caissier (`MIN_QUANTITY = 0.001`). Moins, et une saisie "
            "licite se ferait tronquer sans un mot.",
        )

    def test_les_seuils_de_stock_sont_decimaux_eux_aussi(self):
        """
        Un seuil n'est pas de la marchandise, mais il se COMPARE à elle.

        Ces quatre colonnes sont confrontées en SQL à `Stock.quantity`, qui
        porte trois décimales, à une dizaine d'endroits. Entières, elles
        rendaient « alerter sous 2,5 kg » impossible - et l'incohérence était
        déjà en base : l'instantané du seuil d'une réquisition
        (`reorder_point_at_request`) est décimal depuis son écriture.
        """
        produit = django_apps.get_model('products', 'Product')
        for nom in ('min_stock_level', 'max_stock_level', 'reorder_point', 'reorder_quantity'):
            with self.subTest(champ=nom):
                champ = produit._meta.get_field(nom)
                self.assertIsInstance(champ, models.DecimalField, nom)
                self.assertEqual(champ.decimal_places, 3, nom)

    def test_le_palier_tarifaire_descend_au_plus_petit_pas(self):
        """
        ⚠ DÉCISION MÉTIER, et elle est délibérée.

        Le défaut de `ProductPrice.min_quantity` passe de 1 à 0,001 : un tarif
        s'applique désormais dès la plus petite quantité vendable, là où le
        défaut à 1 obligeait à décider. Aucune ligne existante n'en change de
        sens - la table était vide au moment de la migration - donc le choix
        n'affecte que les paliers créés après.
        """
        champ = django_apps.get_model('products', 'ProductPrice')._meta.get_field('min_quantity')
        self.assertIsInstance(champ, models.DecimalField)
        self.assertEqual(champ.decimal_places, 3)
        self.assertEqual(champ.default, Decimal('0.001'))
