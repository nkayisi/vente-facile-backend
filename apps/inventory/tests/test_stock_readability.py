"""
Tests de LISIBILITÉ du stock vendu en gros et au détail.

Un rayon qui porte 3 casiers scellés et 7 bouteilles isolées ne doit jamais se
présenter comme « 43 » : partout où une quantité s'affiche - niveau de stock,
mouvements, comptages, rapports, exports - les deux compteurs restent distincts.

La contrainte inverse compte tout autant : là où le partage réel n'est PAS
enregistré (le stock avant/après d'un mouvement, un lot qui périme, une
réservation), il ne doit surtout pas être inventé. Un « 4 casiers + 3
bouteilles » calculé au facteur pour un rayon qui portait « 3 casiers +
27 bouteilles » serait une contrevérité, et d'autant plus trompeuse qu'elle a
l'air exacte.
"""
from decimal import Decimal

from django.test import TestCase

from apps.inventory.models import Stock, StockMovement
from apps.inventory.packaging import PackagingProfile, PackagingService
from apps.inventory.serializers import (
    StockBatchSerializer,
    StockListSerializer,
    StockMovementListSerializer,
)
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _Rayon(TestCase):
    """Eau 50cl : casier de 12 bouteilles. Savon : vendu à la pièce."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.crate = Unit.objects.create(
            organization=self.org, name='casier', symbol='cs'
        )
        self.product = Product.objects.create(
            organization=self.org,
            name='Eau 50cl', slug='eau-50cl', sku='EAU-50',
            unit=self.bottle, packaging_unit=self.crate,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            cost_price=Decimal('400.00'), selling_price=Decimal('600.00'),
            track_inventory=True, is_active=True,
        )
        self.simple = Product.objects.create(
            organization=self.org,
            name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle,
            cost_price=Decimal('500.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )

    def _stock(self, packages, loose, reserved='0.000', product=None):
        """Ligne de stock décrite par ses DEUX compteurs, comme en base."""
        product = product or self.product
        factor = PackagingService.factor(product) or 0
        return Stock.objects.create(
            organization=self.org,
            product=product,
            warehouse=self.warehouse,
            quantity=Decimal(packages) * factor + Decimal(loose)
            if factor else Decimal(loose),
            package_quantity=Decimal(packages) if factor else Decimal('0.000'),
            loose_quantity=Decimal(loose) if factor else Decimal('0.000'),
            reserved_quantity=Decimal(reserved),
            avg_cost=Decimal('400.00'),
        )


class FormatSplitTests(_Rayon):
    """``format_split`` rend un partage connu SANS le recalculer."""

    def test_les_deux_canaux_restent_distincts(self):
        self.assertEqual(
            PackagingService.format_split(self.product, 3, Decimal('7')),
            '3 casiers + 7 bouteilles',
        )

    def test_un_partage_deja_connu_n_est_jamais_redivise(self):
        """
        3 casiers + 27 bouteilles reste 3 casiers + 27 bouteilles.

        C'est LE cas que ``format_quantity`` ne sait pas traiter à partir du
        seul total (63) : il en tirerait « 5 casiers + 3 bouteilles », un rayon
        qui n'existe pas.
        """
        self.assertEqual(
            PackagingService.format_split(self.product, 3, Decimal('27')),
            '3 casiers + 27 bouteilles',
        )

    def test_partie_nulle_omise(self):
        self.assertEqual(
            PackagingService.format_split(self.product, 3, Decimal('0')),
            '3 casiers',
        )

    def test_partage_vide_se_lit_en_unite_de_detail(self):
        self.assertEqual(
            PackagingService.format_split(self.product, 0, Decimal('0')),
            '0 bouteille',
        )

    def test_singulier_respecte(self):
        self.assertEqual(
            PackagingService.format_split(self.product, 1, Decimal('1')),
            '1 casier + 1 bouteille',
        )


class StockLisibleTests(_Rayon):
    """``format_stock`` et ``format_available`` lisent les compteurs stockés."""

    def test_stock_lit_les_deux_compteurs(self):
        stock = self._stock(3, '7')
        self.assertEqual(
            PackagingService.format_stock(stock), '3 casiers + 7 bouteilles'
        )

    def test_stock_ne_recolle_pas_le_vrac_en_casiers(self):
        """27 bouteilles isolées le restent, même si elles feraient 2 casiers."""
        stock = self._stock(3, '27')
        self.assertEqual(
            PackagingService.format_stock(stock), '3 casiers + 27 bouteilles'
        )

    def test_produit_simple_reste_en_unites(self):
        stock = self._stock(0, '9', product=self.simple)
        self.assertEqual(PackagingService.format_stock(stock), '9 bouteilles')

    def test_disponible_impute_la_reservation_au_scelle(self):
        """
        3 casiers + 7 bouteilles dont 12 réservées → 2 casiers + 7 bouteilles.

        L'imputation conservatrice est celle du contrôle de vente : ce qui
        s'affiche comme disponible est exactement ce que le POS acceptera.
        """
        stock = self._stock(3, '7', reserved='12.000')
        self.assertEqual(
            PackagingService.format_available(stock), '2 casiers + 7 bouteilles'
        )

    def test_disponible_sans_reservation_egale_le_rayon(self):
        stock = self._stock(3, '7')
        self.assertEqual(
            PackagingService.format_available(stock),
            PackagingService.format_stock(stock),
        )


class TotalNomméTests(_Rayon):
    """``format_base_total`` nomme l'unité sans inventer de contenants."""

    def test_nomme_l_unite_de_detail(self):
        self.assertEqual(
            PackagingService.format_base_total(self.product, Decimal('63')),
            '63 bouteilles',
        )

    def test_n_invente_jamais_de_casiers(self):
        rendu = PackagingService.format_base_total(self.product, Decimal('63'))
        self.assertNotIn('casier', rendu)

    def test_valeur_negative_garde_son_signe(self):
        self.assertEqual(
            PackagingService.format_base_total(self.product, Decimal('-6')),
            '-6 bouteilles',
        )


class EcartVentileTests(_Rayon):
    """Un écart de comptage se lit canal par canal, avec son signe."""

    def test_manquant_de_casiers_et_surplus_de_bouteilles(self):
        """
        Les deux se compensent presque dans le total (-19) et s'y effacent.
        Ventilés, chacun désigne sa cause.
        """
        self.assertEqual(
            PackagingService.format_signed_split(
                self.product, Decimal('-2'), Decimal('5')
            ),
            '-2 casiers, +5 bouteilles',
        )

    def test_ecart_sur_un_seul_canal(self):
        self.assertEqual(
            PackagingService.format_signed_split(
                self.product, Decimal('0'), Decimal('-7')
            ),
            '-7 bouteilles',
        )

    def test_format_difference_sans_ventilation_reste_au_total(self):
        """
        Sans partage connu, l'écart ne se répartit pas.

        Un manquant de 43 bouteilles n'est pas « -3 casiers, -7 bouteilles »
        tant que personne n'a constaté qu'un casier scellé manquait.
        """
        self.assertEqual(
            PackagingService.format_difference(self.product, Decimal('-43')),
            '-43 bouteilles',
        )

    def test_format_difference_ventile_quand_les_deux_parts_sont_connues(self):
        self.assertEqual(
            PackagingService.format_difference(
                self.product, Decimal('-19'),
                package_delta=Decimal('-2'), loose_delta=Decimal('5'),
            ),
            '-2 casiers, +5 bouteilles',
        )


class ProfilAgregeTests(_Rayon):
    """``PackagingProfile`` remplace un ``Product`` dans les rapports agrégés."""

    def test_profil_reconstitue_depuis_une_ligne_values(self):
        row = {
            'product__selling_mode': Product.SellingMode.WHOLESALE_AND_RETAIL,
            'product__units_per_package': 12,
            'product__unit__name': 'bouteille',
            'product__packaging_unit__name': 'casier',
            'product__name': 'Eau 50cl',
        }
        profile = PackagingProfile.from_values(row)
        self.assertEqual(PackagingService.factor(profile), 12)
        self.assertEqual(
            PackagingService.format_split(profile, 3, Decimal('7')),
            '3 casiers + 7 bouteilles',
        )

    def test_profil_d_un_produit_simple_n_a_pas_de_facteur(self):
        profile = PackagingProfile.from_values({
            'product__selling_mode': 'retail_only',
            'product__unit__name': 'pièce',
        })
        self.assertIsNone(PackagingService.factor(profile))
        self.assertEqual(
            PackagingService.format_quantity(profile, Decimal('9')), '9 pièces'
        )


class SerializerTests(_Rayon):
    """Les champs exposés par l'API portent la même lecture que les écrans."""

    def test_stock_expose_rayon_dispo_et_reserve(self):
        stock = self._stock(3, '7', reserved='12.000')
        data = StockListSerializer(stock).data

        self.assertEqual(data['stock_display'], '3 casiers + 7 bouteilles')
        self.assertEqual(data['available_display'], '2 casiers + 7 bouteilles')
        self.assertEqual(data['stock_packages'], 3)
        self.assertEqual(data['available_packages'], 2)
        self.assertEqual(data['packaging_factor'], 12)

    def test_reserve_ne_se_traduit_pas_en_casiers(self):
        """
        Une réservation ne porte pas sur des contenants précis : annoncer
        « 1 casier réservé » affirmerait qu'un scellé est bloqué, ce que rien
        ne garantit.
        """
        stock = self._stock(3, '7', reserved='12.000')
        data = StockListSerializer(stock).data
        self.assertEqual(data['reserved_display'], '12 bouteilles')

    def test_produit_simple_n_expose_aucun_partage(self):
        stock = self._stock(0, '9', product=self.simple)
        data = StockListSerializer(stock).data

        self.assertEqual(data['stock_display'], '9 bouteilles')
        self.assertIsNone(data['stock_packages'])
        self.assertIsNone(data['available_packages'])
        self.assertIsNone(data['packaging_factor'])

    def test_mouvement_relit_sa_saisie_pas_le_facteur_du_jour(self):
        """
        L'historique se relit comme il a été vécu.

        Le mouvement fige son facteur : repasser le produit de 12 à 6 unités par
        casier ne doit pas réécrire une entrée déjà enregistrée.
        """
        movement = StockMovement.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.PURCHASE,
            quantity=Decimal('125.000'),
            quantity_before=Decimal('0.000'),
            quantity_after=Decimal('125.000'),
            input_package_quantity=Decimal('10.000'),
            input_loose_quantity=Decimal('5.000'),
            packaging_factor=12,
        )
        self.product.units_per_package = 6
        self.product.save(update_fields=['units_per_package'])
        movement.refresh_from_db()

        data = StockMovementListSerializer(movement).data
        self.assertEqual(data['quantity_display'], '10 casiers + 5 bouteilles')

    def test_avant_apres_n_invente_pas_de_partage(self):
        """
        Seul le TOTAL est enregistré à ces deux bornes.

        Le rayon pouvait porter « 3 casiers + 27 bouteilles » ; le redécouper
        au facteur annoncerait « 5 casiers + 3 bouteilles ».
        """
        movement = StockMovement.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            movement_type=StockMovement.MovementType.SALE,
            quantity=Decimal('12.000'),
            quantity_before=Decimal('63.000'),
            quantity_after=Decimal('51.000'),
            input_package_quantity=Decimal('1.000'),
            input_loose_quantity=Decimal('0.000'),
            packaging_factor=12,
        )
        data = StockMovementListSerializer(movement).data

        self.assertEqual(data['quantity_before_display'], '63 bouteilles')
        self.assertEqual(data['quantity_after_display'], '51 bouteilles')
        self.assertNotIn('casier', data['quantity_before_display'])

    def test_lot_expose_son_restant_sans_annoncer_de_contenants(self):
        """Un lot suit une date de péremption, pas un emballage."""
        from apps.inventory.models import StockBatch

        batch = StockBatch.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            batch_number='LOT-1',
            quantity=Decimal('240.000'),
            cost_price=Decimal('400.00'),
        )
        data = StockBatchSerializer(batch).data
        self.assertEqual(data['quantity_display'], '240 bouteilles')
