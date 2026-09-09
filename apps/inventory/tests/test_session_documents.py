"""
Les deux documents d'une session d'inventaire, et leur seule règle de lecture.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UNE LIGNE NE PEUT PAS SE CONTREDIRE ELLE-MÊME.                              │
│                                                                              │
│ « Stock système », « Compté » et « Écart » décrivent le MÊME rayon. Ils      │
│ doivent donc reconstituer le partage scellé/vrac par un seul chemin. La      │
│ fiche le reconstituait par une division `Decimal` brute et imprimait         │
│ « 7.9166666666666666666666666667 CASIERS » sur un document qu'on remplit au  │
│ stylo, pendant que la colonne « Écart » de la même ligne, elle, passait par  │
│ `PackagingService.split`.                                                    │
└──────────────────────────────────────────────────────────────────────────────┘

Le contrôle porte donc sur DEUX choses, et il faut les deux : qu'aucune mesure
ne sorte fractionnaire, et que les trois colonnes s'accordent entre elles.
"""
from decimal import Decimal

from django.test import TestCase

from apps.inventory.models import InventoryCount, InventorySession, Stock
from apps.inventory.session_documents import (
    A_REMPLIR,
    build_inventory_report,
    build_inventory_sheet,
)
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _FeuilleSetup(TestCase):
    """Eau 50cl : casier de 12 bouteilles. Savon : vendu à la pièce."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bouteille = Unit.objects.create(
            organization=self.org, name='BOUTEILLE', symbol='btl'
        )
        self.casier = Unit.objects.create(
            organization=self.org, name='CASIER', symbol='cs'
        )
        self.product = Product.objects.create(
            organization=self.org,
            name='Eau 50cl', slug='eau-50cl', sku='EAU-50',
            unit=self.bouteille, packaging_unit=self.casier,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            cost_price=Decimal('400.00'), selling_price=Decimal('600.00'),
            track_inventory=True, is_active=True,
        )
        self.simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bouteille,
            cost_price=Decimal('500.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        self.session = InventorySession.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            reference='INV-TEST-0001',
            scope_type='full',
            status=InventorySession.Status.IN_PROGRESS,
        )

    def _stock(self, packages, loose, product=None):
        produit = product or self.product
        return Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal(packages) * 12 + Decimal(loose),
            package_quantity=Decimal(packages),
            loose_quantity=Decimal(loose),
            avg_cost=Decimal('400.00'),
        )

    def _comptage(self, *, attendu, attendu_vrac, paquets=None, vrac=None,
                  product=None, facteur=12):
        """Une ligne de comptage posée telle que le serveur l'engendre."""
        produit = product or self.product
        compte = InventoryCount(
            organization=self.org,
            session=self.session,
            product=produit,
            quantity_expected=Decimal(attendu),
            expected_loose_quantity=Decimal(attendu_vrac),
            packaging_factor=facteur,
            unit_cost=Decimal('400.00'),
        )
        if paquets is not None or vrac is not None:
            compte.counted_package_quantity = Decimal(paquets or 0)
            compte.counted_loose_quantity = Decimal(vrac or 0)
            compte.is_counted = True
        compte.save()
        return compte

    def _ligne(self, constructeur=build_inventory_sheet):
        counts = self.session.counts.select_related(
            'product', 'product__category', 'product__unit',
            'product__packaging_unit',
        ).order_by('product__name')
        spec = constructeur(
            self.session, counts, self.org, currency='CDF',
        )
        return spec


class FicheDeComptageTests(_FeuilleSetup):

    def test_le_stock_systeme_ne_porte_jamais_de_contenant_fractionnaire(self):
        """
        95 bouteilles au facteur 12, c'est 7 casiers et 11 bouteilles.

        La division brute rendait 7,9166666666666666666666666667 casiers - un
        nombre qu'aucun magasinier ne peut rapprocher de ce qu'il a sous les
        yeux, sur le document même qu'il emporte pour compter.
        """
        self._stock(7, 11)
        self._comptage(attendu='95.000', attendu_vrac='0.000')

        ligne = self._ligne().rows[0]

        self.assertEqual(ligne['expected'], '7 CASIERS + 11 BOUTEILLES')
        self.assertNotIn(
            '.', ligne['expected'],
            "La mesure porte une fraction : le partage a été divisé au lieu "
            f"d'être reconstitué ({ligne['expected']!r}).",
        )

    def test_les_trois_colonnes_s_accordent_sur_le_meme_partage(self):
        """
        Attendu et compté identiques : l'écart doit être NUL.

        C'est le contrôle qui compte le plus. Si « Stock système » et « Écart »
        reconstituent l'attendu par deux chemins, un rayon parfaitement juste
        s'annonce avec un manquant, et le gérant part chercher une marchandise
        qui est là.
        """
        self._stock(7, 11)
        self._comptage(
            attendu='95.000', attendu_vrac='0.000', paquets='7', vrac='11',
        )

        ligne = self._ligne().rows[0]

        self.assertEqual(ligne['expected'], '7 CASIERS + 11 BOUTEILLES')
        self.assertEqual(ligne['counted'], '7 CASIERS + 11 BOUTEILLES')
        self.assertEqual(ligne['difference'], '0 BOUTEILLE')

    def test_l_ecart_reste_ventile_par_canal(self):
        """
        « -2 casiers, +5 bouteilles » : un manquant de scellés et un surplus
        d'unités isolées se compensent dans le total et y disparaissent.
        """
        self._stock(7, 11)
        self._comptage(
            attendu='95.000', attendu_vrac='11.000', paquets='5', vrac='16',
        )

        ligne = self._ligne().rows[0]

        self.assertEqual(ligne['expected'], '7 CASIERS + 11 BOUTEILLES')
        self.assertEqual(ligne['difference'], '-2 CASIERS, +5 BOUTEILLES')

    def test_un_produit_sans_conditionnement_lit_son_total_nomme(self):
        """Pas de partage à inventer là où il n'y a pas de contenant."""
        self._stock(0, 0, product=self.simple)
        self._comptage(
            attendu='40.000', attendu_vrac='0.000',
            product=self.simple, facteur=None,
        )

        ligne = self._ligne().rows[0]

        self.assertEqual(ligne['expected'], '40 BOUTEILLES')

    def test_une_ligne_non_comptee_porte_des_tirets_et_jamais_un_zero(self):
        """
        « 0 compté » affirmerait qu'on a regardé et trouvé vide, quand la ligne
        n'a simplement pas encore été visitée.
        """
        self._stock(7, 11)
        self._comptage(attendu='95.000', attendu_vrac='0.000')

        ligne = self._ligne().rows[0]

        self.assertEqual(ligne['counted'], A_REMPLIR)
        self.assertEqual(ligne['difference'], A_REMPLIR)


class RapportDEcartsTests(_FeuilleSetup):

    def test_le_rapport_lit_l_attendu_par_le_meme_chemin_que_la_fiche(self):
        """
        Les deux documents décrivent le même rayon : ils ne peuvent pas en
        donner deux lectures. Le rapport ne garde que les écarts, c'est sa
        seule différence.
        """
        self._stock(7, 11)
        self._comptage(
            attendu='95.000', attendu_vrac='0.000', paquets='6', vrac='11',
        )

        fiche = self._ligne().rows[0]
        rapport = self._ligne(build_inventory_report).rows[0]

        self.assertEqual(fiche['expected'], rapport['expected'])
        self.assertEqual(fiche['difference'], rapport['difference'])
        self.assertNotIn('.', rapport['expected'])


class FacteurFigeTests(_FeuilleSetup):
    """
    Le partage attendu se lit au facteur SOUS LEQUEL ON A COMPTÉ.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ SOUSTRAIRE DEUX MESURES PRISES AVEC DEUX RÈGLES DIFFÉRENTES.            │
    │                                                                          │
    │ `counted_package_quantity` est LU : c'est le nombre de contenants que le │
    │ magasinier a écrit, sous le conditionnement en vigueur ce jour-là -      │
    │ `packaging_factor` est d'ailleurs REPOSÉ à chaque comptage               │
    │ (`InventoryCountUpdateSerializer.validate`). L'attendu, lui, se          │
    │ reconstitue. Le reconstituer au facteur d'AUJOURD'HUI puis soustraire    │
    │ l'autre revient à retrancher des casiers de douze à des casiers de       │
    │ vingt-quatre : le nombre qui en sort ne désigne rien.                    │
    │                                                                          │
    │ C'est la règle déjà tenue par `format_movement_quantity` : « le lire au  │
    │ conditionnement d'aujourd'hui réécrirait un passé qui l'ignorait ».      │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def test_le_conditionnement_peut_changer_APRES_le_comptage(self):
        self._stock(7, 11)
        # Compté sous un facteur de 12 : 7 casiers + 11 bouteilles = 95.
        self._comptage(
            attendu='95.000', attendu_vrac='0.000',
            paquets='7', vrac='11', facteur=12,
        )
        # Le marchand repasse ensuite ses casiers à 24 bouteilles.
        self.product.units_per_package = 24
        self.product.save(update_fields=['units_per_package'])

        ligne = self._ligne().rows[0]

        # L'attendu se relit sous la règle du comptage, donc l'écart est NUL.
        # Au facteur d'aujourd'hui, l'attendu sortirait « 3 CASIERS +
        # 23 BOUTEILLES » et l'écart annoncerait « +4 CASIERS » de trop.
        self.assertEqual(ligne['expected'], '7 CASIERS + 11 BOUTEILLES')
        self.assertEqual(ligne['counted'], '7 CASIERS + 11 BOUTEILLES')
        self.assertEqual(ligne['difference'], '0 BOUTEILLE')

    def test_l_ecart_de_l_API_suit_la_MEME_regle_que_le_document(self):
        """
        Le document et la fiche que lisent le terminal et le web décrivent le
        même rayon : ils ne peuvent pas en donner deux lectures.
        """
        from apps.inventory.serializers import InventoryCountSerializer

        self._stock(7, 11)
        compte = self._comptage(
            attendu='95.000', attendu_vrac='0.000',
            paquets='7', vrac='11', facteur=12,
        )
        self.product.units_per_package = 24
        self.product.save(update_fields=['units_per_package'])
        compte.refresh_from_db()

        ligne = self._ligne().rows[0]
        api = InventoryCountSerializer(compte).data

        self.assertEqual(api['difference_display'], ligne['difference'])
        self.assertEqual(api['difference_display'], '0 BOUTEILLE')
