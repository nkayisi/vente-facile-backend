"""
Tests de ``PackagingService`` - conversion et déconditionnement.

Produit de référence de toute la fonctionnalité : **Eau 50cl**, paquet de 12
bouteilles, 6 000 CDF le paquet, 600 CDF la bouteille.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.inventory.models import Stock, StockMovement
from apps.inventory.packaging import PackagingService
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class PureSplitTests(TestCase):
    """``split`` est pure : aucune fixture, aucun accès base."""

    def test_paquets_scelles_purs(self):
        sealed, loose = PackagingService.split(Decimal('24'), Decimal('0'), 12)
        self.assertEqual(sealed, 2)
        self.assertEqual(loose, Decimal('0.000'))

    def test_paquet_ouvert_sans_vente(self):
        """
        24 unités dont 12 en vrac = 1 paquet scellé + 12 bouteilles.

        Le cas qui invalide la formule « quantité // facteur » : celle-ci
        afficherait « 2 paquets » alors qu'un emballage est déjà ouvert.
        """
        sealed, loose = PackagingService.split(Decimal('24'), Decimal('12'), 12)
        self.assertEqual(sealed, 1)
        self.assertEqual(loose, Decimal('12.000'))

    def test_scenario_de_recette(self):
        """22 unités dont 10 en vrac → « 1 paquet + 10 bouteilles »."""
        sealed, loose = PackagingService.split(Decimal('22'), Decimal('10'), 12)
        self.assertEqual(sealed, 1)
        self.assertEqual(loose, Decimal('10.000'))

    def test_orphelin_sur_stock_preexistant(self):
        """
        37 unités sans vrac déclaré → 3 paquets + 1 bouteille.

        C'est l'activation du mode gros sur un produit qui a déjà du stock :
        aucune migration de données n'est nécessaire.
        """
        sealed, loose = PackagingService.split(Decimal('37'), Decimal('0'), 12)
        self.assertEqual(sealed, 3)
        self.assertEqual(loose, Decimal('1.000'))

    def test_stock_negatif_ne_produit_pas_de_paquet_negatif(self):
        sealed, loose = PackagingService.split(Decimal('-6'), Decimal('0'), 12)
        self.assertEqual(sealed, 0)
        self.assertEqual(loose, Decimal('-6.000'))

    def test_vrac_superieur_au_total_est_borne(self):
        sealed, loose = PackagingService.split(Decimal('5'), Decimal('99'), 12)
        self.assertEqual(sealed, 0)
        self.assertEqual(loose, Decimal('5.000'))

    def test_sans_facteur_tout_est_en_vrac(self):
        sealed, loose = PackagingService.split(Decimal('22'), Decimal('0'), None)
        self.assertEqual(sealed, 0)
        self.assertEqual(loose, Decimal('22.000'))

    def test_invariant_scelles_fois_facteur_plus_vrac_egale_total(self):
        """L'invariant qui rend la représentation fiable, sur tout l'espace."""
        for base in range(-5, 60):
            for loose in range(0, 30):
                sealed, effective = PackagingService.split(
                    Decimal(base), Decimal(loose), 12
                )
                self.assertEqual(
                    Decimal(sealed) * 12 + effective,
                    Decimal(base),
                    f"invariant rompu pour base={base}, vrac={loose}",
                )


class _PackagingSetup(TestCase):
    """Eau 50cl : paquet de 12, 6 000 le paquet, 600 la bouteille."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.pack = Unit.objects.create(
            organization=self.org, name='paquet', symbol='pqt'
        )
        self.product = Product.objects.create(
            organization=self.org,
            name='Eau 50cl',
            slug='eau-50cl',
            sku='EAU-50',
            unit=self.bottle,
            packaging_unit=self.pack,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=12,
            allow_auto_unpacking=True,
            cost_price=Decimal('400.00'),
            selling_price=Decimal('600.00'),
            wholesale_price=Decimal('6000.00'),
            track_inventory=True,
            is_active=True,
        )
        self.simple_product = Product.objects.create(
            organization=self.org,
            name='Savon',
            slug='savon',
            sku='SAV-01',
            unit=self.bottle,
            selling_price=Decimal('800.00'),
            track_inventory=True,
            is_active=True,
        )

    def _stock(self, quantity='24.000', loose='0.000', product=None):
        """
        Stock décrit par son total et sa part vrac, comme le faisaient les
        écrans avant que les conditionnements scellés ne soient stockés.

        Le nombre de contenants scellés se déduit ici une fois pour toutes, à
        l'identique de la migration `inventory/0016` : « 24 unités dont 0 en
        vrac » signifie bien 2 paquets scellés.
        """
        product = product or self.product
        factor = PackagingService.factor(product)
        packages, effective_loose = PackagingService.split(
            Decimal(quantity), Decimal(loose), factor
        )
        return Stock.objects.create(
            organization=self.org,
            product=product,
            warehouse=self.warehouse,
            quantity=Decimal(quantity),
            package_quantity=Decimal(packages),
            loose_quantity=effective_loose if factor else Decimal('0.000'),
            avg_cost=Decimal('400.00'),
        )


class FactorAndConversionTests(_PackagingSetup):

    def test_is_dual(self):
        self.assertTrue(PackagingService.is_dual(self.product))
        self.assertFalse(PackagingService.is_dual(self.simple_product))

    def test_factor_retourne_none_pour_un_produit_simple(self):
        self.assertIsNone(PackagingService.factor(self.simple_product))
        self.assertEqual(PackagingService.factor(self.product), 12)

    def test_factor_ne_leve_jamais_sur_produit_mal_configure(self):
        """Un produit incohérent dégrade en mono-unité, il ne casse pas l'API."""
        self.product.units_per_package = None
        self.assertIsNone(PackagingService.factor(self.product))

    def test_to_base_mixte(self):
        self.assertEqual(
            PackagingService.to_base(self.product, 2, 3), Decimal('27.000')
        )

    def test_to_base_ignore_les_paquets_sur_produit_simple(self):
        self.assertEqual(
            PackagingService.to_base(self.simple_product, 2, 3), Decimal('3.000')
        )


class FormatQuantityTests(_PackagingSetup):

    def test_format_mixte(self):
        self.assertEqual(
            PackagingService.format_quantity(self.product, Decimal('22'), Decimal('10')),
            '1 paquet + 10 bouteilles',
        )

    def test_format_pluriel_des_paquets(self):
        self.assertEqual(
            PackagingService.format_quantity(self.product, Decimal('24'), Decimal('0')),
            '2 paquets',
        )

    def test_format_omet_la_partie_nulle(self):
        self.assertNotIn(
            '0 bouteille',
            PackagingService.format_quantity(self.product, Decimal('24'), Decimal('0')),
        )

    def test_format_vrac_seul(self):
        self.assertEqual(
            PackagingService.format_quantity(self.product, Decimal('7'), Decimal('7')),
            '7 bouteilles',
        )

    def test_format_singulier(self):
        self.assertEqual(
            PackagingService.format_quantity(self.product, Decimal('13'), Decimal('1')),
            '1 paquet + 1 bouteille',
        )

    def test_format_produit_simple(self):
        self.assertEqual(
            PackagingService.format_quantity(self.simple_product, Decimal('22')),
            '22 bouteilles',
        )

    def test_format_sans_decimales_inutiles(self):
        self.assertEqual(
            PackagingService.format_quantity(self.product, Decimal('12.000'), Decimal('0')),
            '1 paquet',
        )


class FormatMovementQuantityTests(_PackagingSetup):
    """Lisibilité de l'historique des mouvements."""

    def _movement(self, **kwargs):
        defaults = dict(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            movement_type='purchase',
            quantity=Decimal('27.000'),
            quantity_before=Decimal('0.000'),
            quantity_after=Decimal('27.000'),
            input_package_quantity=Decimal('2.000'),
            input_loose_quantity=Decimal('3.000'),
            packaging_factor=12,
        )
        defaults.update(kwargs)
        return StockMovement.objects.create(**defaults)

    def test_reprend_la_saisie_d_origine(self):
        movement = self._movement()
        self.assertEqual(
            PackagingService.format_movement_quantity(movement),
            '2 paquets + 3 bouteilles',
        )

    def test_sortie_affichee_sans_signe(self):
        """Le sens se lit au type du mouvement, pas à un signe collé au libellé."""
        movement = self._movement(
            movement_type='sale',
            quantity=Decimal('-12.000'),
            input_package_quantity=Decimal('1.000'),
            input_loose_quantity=Decimal('0.000'),
            quantity_before=Decimal('27.000'),
            quantity_after=Decimal('15.000'),
        )
        self.assertEqual(
            PackagingService.format_movement_quantity(movement), '1 paquet'
        )

    def test_deconditionnement_montre_le_paquet_ouvert(self):
        """Un `unpack` porte `quantity = 0` : seul le paquet ouvert fait sens."""
        movement = self._movement(
            movement_type='unpack',
            quantity=Decimal('0.000'),
            input_package_quantity=Decimal('1.000'),
            input_loose_quantity=Decimal('0.000'),
            quantity_before=Decimal('24.000'),
            quantity_after=Decimal('24.000'),
        )
        self.assertEqual(
            PackagingService.format_movement_quantity(movement), '1 paquet'
        )

    def test_mouvement_sans_saisie_conditionnee_est_reparti(self):
        """
        Transfert, réception, sync mobile : ces chemins ne renseignent pas la
        saisie d'origine. On répartit alors le total au facteur enregistré.
        """
        movement = self._movement(
            quantity=Decimal('25.000'),
            input_package_quantity=Decimal('0.000'),
            input_loose_quantity=Decimal('0.000'),
            quantity_after=Decimal('25.000'),
        )
        self.assertEqual(
            PackagingService.format_movement_quantity(movement),
            '2 paquets + 1 bouteille',
        )

    def test_produit_mono_unite(self):
        movement = self._movement(
            product=self.simple_product,
            quantity=Decimal('-3.000'),
            input_package_quantity=Decimal('0.000'),
            input_loose_quantity=Decimal('0.000'),
            packaging_factor=None,
            quantity_before=Decimal('10.000'),
            quantity_after=Decimal('7.000'),
        )
        self.assertEqual(
            PackagingService.format_movement_quantity(movement), '3 bouteilles'
        )

    def test_mouvement_anterieur_a_l_activation_du_gros(self):
        """
        Le produit se vendait à l'unité au moment du mouvement : son passage en
        vente au paquet ne doit pas transformer l'écriture d'hier en cartons.
        """
        movement = self._movement(
            quantity=Decimal('24.000'),
            input_package_quantity=Decimal('0.000'),
            input_loose_quantity=Decimal('0.000'),
            packaging_factor=None,
            quantity_after=Decimal('24.000'),
        )
        self.assertEqual(
            PackagingService.format_movement_quantity(movement), '24 bouteilles'
        )

    def test_facteur_fige_prime_sur_la_configuration_actuelle(self):
        """
        Le conditionnement du produit a changé depuis : l'historique doit rester
        lu au facteur du jour du mouvement, sinon les archives se réécrivent.
        """
        movement = self._movement(
            quantity=Decimal('24.000'),
            input_package_quantity=Decimal('0.000'),
            input_loose_quantity=Decimal('0.000'),
            packaging_factor=12,
            quantity_after=Decimal('24.000'),
        )
        self.product.units_per_package = 6
        self.product.save(update_fields=['units_per_package'])

        self.assertEqual(
            PackagingService.format_movement_quantity(movement), '2 paquets'
        )


class AvailableSplitTests(_PackagingSetup):

    def test_les_reservations_sont_imputees_au_scelle(self):
        stock = self._stock(quantity='24.000', loose='0.000')
        stock.reserved_quantity = Decimal('12.000')
        stock.save()

        sealed, loose = PackagingService.available_split(stock, 12)
        self.assertEqual(sealed, 1)
        self.assertEqual(loose, Decimal('0.000'))

    def test_le_vrac_reste_disponible_apres_reservation(self):
        stock = self._stock(quantity='22.000', loose='10.000')
        stock.reserved_quantity = Decimal('2.000')
        stock.save()

        # 20 disponibles, dont 10 en vrac → 10 scellés = 0 paquet + orphelin
        sealed, loose = PackagingService.available_split(stock, 12)
        self.assertEqual(sealed, 0)
        self.assertEqual(loose, Decimal('20.000'))


class EnsureLooseAvailableTests(_PackagingSetup):

    def test_ne_fait_rien_si_le_vrac_suffit(self):
        stock = self._stock(quantity='22.000', loose='10.000')

        opened, movement = PackagingService.ensure_loose_available(
            stock, self.product, Decimal('3'), user=self.owner
        )
        self.assertEqual(opened, 0)
        self.assertIsNone(movement)
        self.assertEqual(stock.loose_quantity, Decimal('10.000'))
        self.assertFalse(StockMovement.objects.filter(movement_type='unpack').exists())

    def test_ouvre_un_paquet(self):
        """Le scénario de référence : 2 paquets pleins, le client veut 2 bouteilles."""
        stock = self._stock(quantity='24.000', loose='0.000')

        opened, movement = PackagingService.ensure_loose_available(
            stock, self.product, Decimal('2'), user=self.owner
        )
        self.assertEqual(opened, 1)
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))
        self.assertEqual(stock.quantity, Decimal('24.000'), "le total ne bouge pas")
        self.assertIsNotNone(movement)

    def test_ouvre_plusieurs_paquets(self):
        stock = self._stock(quantity='36.000', loose='0.000')

        opened, _ = PackagingService.ensure_loose_available(
            stock, self.product, Decimal('25'), user=self.owner
        )
        self.assertEqual(opened, 3)
        self.assertEqual(stock.loose_quantity, Decimal('36.000'))

    def test_le_mouvement_unpack_ne_change_pas_la_quantite(self):
        stock = self._stock(quantity='24.000', loose='0.000')
        PackagingService.ensure_loose_available(
            stock, self.product, Decimal('2'), user=self.owner,
            reference_type='sale',
        )

        movement = StockMovement.objects.get(movement_type='unpack')
        self.assertEqual(movement.quantity, Decimal('0.000'))
        self.assertEqual(movement.quantity_before, Decimal('24.000'))
        self.assertEqual(movement.quantity_after, Decimal('24.000'))
        self.assertEqual(movement.input_package_quantity, Decimal('1.000'))
        self.assertEqual(movement.packaging_factor, 12)
        self.assertEqual(movement.created_by, self.owner)
        self.assertEqual(movement.reference_type, 'sale')

    def test_refuse_si_deconditionnement_automatique_desactive(self):
        self.product.allow_auto_unpacking = False
        self.product.save()
        stock = self._stock(quantity='24.000', loose='0.000')

        with self.assertRaises(ValidationError) as ctx:
            PackagingService.ensure_loose_available(
                stock, self.product, Decimal('2'), user=self.owner
            )
        self.assertIn('Ouvrez un paquet', str(ctx.exception))

    def test_autorise_si_le_vrac_suffit_meme_sans_deconditionnement_auto(self):
        self.product.allow_auto_unpacking = False
        self.product.save()
        stock = self._stock(quantity='22.000', loose='10.000')

        opened, _ = PackagingService.ensure_loose_available(
            stock, self.product, Decimal('3'), user=self.owner
        )
        self.assertEqual(opened, 0)

    def test_refuse_si_pas_assez_de_paquets_a_ouvrir(self):
        stock = self._stock(quantity='5.000', loose='5.000')

        with self.assertRaises(ValidationError):
            PackagingService.ensure_loose_available(
                stock, self.product, Decimal('20'), user=self.owner
            )

    def test_produit_simple_ignore(self):
        stock = self._stock(quantity='10.000', product=self.simple_product)

        opened, movement = PackagingService.ensure_loose_available(
            stock, self.simple_product, Decimal('5'), user=self.owner
        )
        self.assertEqual(opened, 0)
        self.assertIsNone(movement)


class AssertSealedAvailableTests(_PackagingSetup):

    def test_accepte_si_assez_de_paquets(self):
        stock = self._stock(quantity='24.000', loose='0.000')
        PackagingService.assert_sealed_available(stock, self.product, Decimal('2'))

    def test_refuse_le_reconditionnement(self):
        """30 bouteilles en vrac ne font pas 2 paquets."""
        stock = self._stock(quantity='30.000', loose='30.000')

        with self.assertRaises(ValidationError) as ctx:
            PackagingService.assert_sealed_available(stock, self.product, Decimal('2'))

        message = str(ctx.exception)
        self.assertIn('ne peuvent pas y être remises', message)
        self.assertIn('au détail', message, "le message doit proposer une issue")

    def test_ignore_si_entrepot_autorise_le_stock_negatif(self):
        self.warehouse.allow_negative_stock = True
        self.warehouse.save()
        stock = self._stock(quantity='0.000', loose='0.000')

        PackagingService.assert_sealed_available(stock, self.product, Decimal('2'))

    def test_produit_simple_ignore(self):
        stock = self._stock(quantity='0.000', product=self.simple_product)
        PackagingService.assert_sealed_available(
            stock, self.simple_product, Decimal('2')
        )


class ApplyDeltaTests(_PackagingSetup):
    """Les deux compteurs bougent séparément, chacun dans son canal."""

    def test_vente_mixte(self):
        stock = self._stock(quantity='36.000', loose='12.000')  # 2 paquets + 12

        # 2 paquets + 3 bouteilles : chaque part sort de son propre compteur.
        PackagingService.apply_delta(
            stock, self.product, delta_packages=-2, delta_loose=Decimal('-3')
        )
        self.assertEqual(stock.package_quantity, Decimal('0.000'))
        self.assertEqual(stock.loose_quantity, Decimal('9.000'))
        self.assertEqual(stock.quantity, Decimal('9.000'))

    def test_entree_en_paquets_ne_touche_pas_au_vrac(self):
        stock = self._stock(quantity='10.000', loose='10.000')

        PackagingService.apply_delta(
            stock, self.product, delta_packages=10, delta_loose=Decimal('0')
        )
        self.assertEqual(stock.package_quantity, Decimal('10.000'))
        self.assertEqual(stock.loose_quantity, Decimal('10.000'))
        self.assertEqual(stock.quantity, Decimal('130.000'))

    def test_entree_en_pieces_alimente_le_vrac(self):
        stock = self._stock(quantity='120.000', loose='0.000')  # 10 paquets

        PackagingService.apply_delta(
            stock, self.product, delta_packages=0, delta_loose=Decimal('5')
        )
        self.assertEqual(stock.package_quantity, Decimal('10.000'))
        self.assertEqual(stock.loose_quantity, Decimal('5.000'))
        self.assertEqual(stock.quantity, Decimal('125.000'))

    def test_retour_revient_toujours_en_vrac(self):
        stock = self._stock(quantity='22.000', loose='10.000')  # 1 paquet + 10

        PackagingService.apply_delta(
            stock, self.product, delta_packages=0, delta_loose=Decimal('2')
        )
        self.assertEqual(stock.package_quantity, Decimal('1.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))
        self.assertEqual(stock.quantity, Decimal('24.000'))

    def test_produit_simple_ignore_les_paquets(self):
        stock = self._stock(quantity='10.000', product=self.simple_product)

        PackagingService.apply_delta(
            stock, self.simple_product,
            delta_packages=0, delta_loose=Decimal('-3'),
        )
        self.assertEqual(stock.quantity, Decimal('7.000'))
        self.assertEqual(stock.package_quantity, Decimal('0.000'))
        self.assertEqual(stock.loose_quantity, Decimal('0.000'))


class ApplyBaseDeltaTests(_PackagingSetup):
    """
    Chemin de repli des écritures qui ne connaissent qu'un total.

    C'est ici que se joue l'asymétrie du domaine : on ouvre un conditionnement
    pour servir du détail, on n'en rescelle jamais.
    """

    def test_sortie_puise_dabord_dans_le_vrac(self):
        stock = self._stock(quantity='34.000', loose='10.000')  # 2 paquets + 10

        PackagingService.apply_base_delta(stock, self.product, Decimal('-4'))

        self.assertEqual(stock.package_quantity, Decimal('2.000'))
        self.assertEqual(stock.loose_quantity, Decimal('6.000'))

    def test_sortie_casse_un_scelle_quand_le_vrac_ne_suffit_pas(self):
        stock = self._stock(quantity='36.000', loose='0.000')  # 3 paquets

        PackagingService.apply_base_delta(stock, self.product, Decimal('-5'))

        self.assertEqual(stock.package_quantity, Decimal('2.000'))
        self.assertEqual(stock.loose_quantity, Decimal('7.000'))
        self.assertEqual(stock.quantity, Decimal('31.000'))

    def test_entree_va_toujours_au_vrac(self):
        stock = self._stock(quantity='36.000', loose='0.000')  # 3 paquets

        PackagingService.apply_base_delta(stock, self.product, Decimal('5'))

        self.assertEqual(stock.package_quantity, Decimal('3.000'))
        self.assertEqual(stock.loose_quantity, Decimal('5.000'))

    def test_indication_de_partage_respectee(self):
        """Réception de 3 paquets + 12 pièces : 3 paquets ET 12 pièces."""
        stock = self._stock(quantity='0.000', loose='0.000')

        PackagingService.apply_base_delta(
            stock, self.product, Decimal('48'), loose_hint=Decimal('12'),
        )

        self.assertEqual(stock.package_quantity, Decimal('3.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.000'))
        self.assertEqual(stock.quantity, Decimal('48.000'))


class ReconcileTests(_PackagingSetup):
    """``Stock.save()`` réaligne les compteurs sans jamais lever."""

    def test_ecriture_aveugle_en_sortie_casse_un_scelle(self):
        """
        Un chemin qui descend `quantity` sans toucher aux compteurs ne doit pas
        faire fondre des contenants scellés en vrac : il en ouvre un.
        """
        stock = self._stock(quantity='36.000', loose='0.000')  # 3 paquets
        stock.quantity = Decimal('31.000')
        stock.save()

        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('2.000'))
        self.assertEqual(stock.loose_quantity, Decimal('7.000'))

    def test_ecriture_aveugle_en_entree_va_au_vrac(self):
        stock = self._stock(quantity='36.000', loose='0.000')
        stock.quantity = Decimal('41.000')
        stock.save()

        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('3.000'))
        self.assertEqual(stock.loose_quantity, Decimal('5.000'))

    def test_vrac_negatif_ouvre_un_scelle(self):
        stock = self._stock(quantity='24.000', loose='0.000')  # 2 paquets
        stock.loose_quantity = Decimal('-5.000')
        stock.save()

        stock.refresh_from_db()
        # Le total est resté 24 : l'écart repart au vrac, puis se normalise.
        self.assertEqual(stock.quantity, Decimal('24.000'))
        self.assertEqual(
            stock.package_quantity * 12 + stock.loose_quantity, Decimal('24.000')
        )
        self.assertGreaterEqual(stock.loose_quantity, Decimal('0.000'))

    def test_produit_simple_garde_ses_compteurs_a_zero(self):
        stock = self._stock(quantity='10.000', product=self.simple_product)
        stock.package_quantity = Decimal('4.000')
        stock.save()

        stock.refresh_from_db()
        self.assertEqual(stock.package_quantity, Decimal('0.000'))
        self.assertEqual(stock.quantity, Decimal('10.000'))
