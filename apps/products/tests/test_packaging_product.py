"""
Tests de configuration du conditionnement sur la fiche produit.

Couvre les validations de l'API produit et la règle de modification du nombre
d'unités par conditionnement.
"""
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _ProductPackagingSetup(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.bottle = Unit.objects.create(
            organization=self.org, name='bouteille', symbol='btl'
        )
        self.pack = Unit.objects.create(
            organization=self.org, name='paquet', symbol='pqt'
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _payload(self, **overrides):
        payload = {
            'name': 'Eau 50cl',
            'sku': 'EAU-50',
            'unit': str(self.bottle.id),
            'packaging_unit': str(self.pack.id),
            'selling_mode': 'wholesale_and_retail',
            'units_per_package': 12,
            'cost_price': '400.00',
            'selling_price': '600.00',
            'wholesale_price': '6000.00',
        }
        payload.update(overrides)
        return payload

    def _post(self, **overrides):
        return self.client.post(
            '/api/v1/products/', self._payload(**overrides),
            format='json', **self._headers,
        )


class ProductPackagingCreationTests(_ProductPackagingSetup):

    def test_creation_en_gros_et_detail(self):
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        product = Product.objects.get(sku='EAU-50')
        self.assertEqual(product.selling_mode, 'wholesale_and_retail')
        self.assertEqual(product.units_per_package, 12)
        self.assertEqual(product.packaging_unit, self.pack)
        self.assertEqual(product.unit, self.bottle)
        self.assertTrue(product.allow_auto_unpacking)

    def test_la_reponse_de_creation_contient_l_identifiant(self):
        """
        Sans `id` dans la réponse, l'écran redirige vers une fiche inexistante
        et le marchand voit « Produit non trouvé » juste après avoir créé son
        produit.
        """
        response = self._post()

        self.assertIn('id', response.data)
        self.assertEqual(
            str(Product.objects.get(sku='EAU-50').id), str(response.data['id'])
        )

    def test_photo_envoyee_apres_creation(self):
        """
        La photo part dans un second appel, en multipart : la création elle-même
        transmet du JSON, qui ne sait pas porter de fichier.
        """
        product_id = self._post().data['id']

        # Le plus petit GIF valide possible, suffisant pour Pillow.
        gif = (
            b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!'
            b'\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00'
            b'\x00\x02\x02D\x01\x00;'
        )
        upload = SimpleUploadedFile('photo.gif', gif, content_type='image/gif')

        response = self.client.patch(
            f'/api/v1/products/{product_id}/',
            {'image': upload},
            format='multipart',
            **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        product = Product.objects.get(id=product_id)
        self.assertTrue(product.image)

    def test_les_deux_prix_restent_independants(self):
        """Le prix du paquet n'est pas déduit du prix unitaire."""
        self._post(wholesale_price='6000.00', selling_price='600.00')

        product = Product.objects.get(sku='EAU-50')
        self.assertEqual(product.wholesale_price, Decimal('6000.00'))
        self.assertEqual(product.selling_price, Decimal('600.00'))
        self.assertNotEqual(
            product.wholesale_price, product.selling_price * 12
        )

    def test_unites_par_conditionnement_requis(self):
        response = self._post(units_per_package=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('units_per_package', response.data)

    def test_unites_par_conditionnement_doit_depasser_un(self):
        response = self._post(units_per_package=1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('au moins 2', str(response.data['units_per_package']))

    def test_unites_par_conditionnement_refuse_zero(self):
        response = self._post(units_per_package=0)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unite_de_gros_requise(self):
        response = self._post(packaging_unit=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('packaging_unit', response.data)

    def test_unite_de_detail_requise(self):
        response = self._post(unit=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('unit', response.data)

    def test_prix_du_conditionnement_requis(self):
        response = self._post(wholesale_price=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('wholesale_price', response.data)

    def test_messages_sans_jargon_technique(self):
        """Le vendeur ne doit jamais lire « facteur de conversion »."""
        response = self._post(units_per_package=1)
        self.assertNotIn('facteur', str(response.data).lower())

    def test_mode_gros_seul_accepte(self):
        response = self._post(selling_mode='wholesale_only')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_prix_achat_au_contenant_enregistre(self):
        self._post(package_cost_price='4800.00')

        product = Product.objects.get(sku='EAU-50')
        self.assertEqual(product.package_cost_price, Decimal('4800.00'))

    def test_prix_achat_unitaire_deduit_du_contenant(self):
        """
        Le marchand achète au carton : s'il ne saisit que ce prix, le coût
        unitaire s'en déduit : c'est lui qui sert au coût moyen pondéré.
        """
        response = self._post(package_cost_price='4800.00', cost_price='0')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        product = Product.objects.get(sku='EAU-50')
        self.assertEqual(product.cost_price, Decimal('400.00'))

    def test_prix_achat_unitaire_saisi_prime(self):
        self._post(package_cost_price='4800.00', cost_price='450.00')

        product = Product.objects.get(sku='EAU-50')
        self.assertEqual(product.cost_price, Decimal('450.00'))


class ProductRetailOnlyRegressionTests(_ProductPackagingSetup):
    """Non-régression : les produits mono-unité restent inchangés."""

    def test_creation_au_detail_seul_sans_conditionnement(self):
        response = self.client.post(
            '/api/v1/products/',
            {
                'name': 'Savon',
                'sku': 'SAV-01',
                'unit': str(self.bottle.id),
                'cost_price': '400.00',
                'selling_price': '800.00',
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        product = Product.objects.get(sku='SAV-01')
        self.assertEqual(product.selling_mode, 'retail_only')
        self.assertIsNone(product.units_per_package)
        self.assertIsNone(product.packaging_unit)

    def test_wholesale_price_reste_libre_en_detail_seul(self):
        """
        En mode détail seul, `wholesale_price` conserve son usage historique de
        prix de gros indicatif : aucune validation nouvelle ne s'y applique.
        """
        response = self.client.post(
            '/api/v1/products/',
            {
                'name': 'Savon',
                'sku': 'SAV-02',
                'cost_price': '400.00',
                'selling_price': '800.00',
                'wholesale_price': '700.00',
            },
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            Product.objects.get(sku='SAV-02').wholesale_price, Decimal('700.00')
        )


class ProductPackagingChangeTests(_ProductPackagingSetup):
    """Modification du nombre d'unités par conditionnement."""

    def setUp(self):
        super().setUp()
        self._post()
        self.product = Product.objects.get(sku='EAU-50')

    def _patch(self, **payload):
        return self.client.patch(
            f'/api/v1/products/{self.product.id}/',
            payload, format='json', **self._headers,
        )

    def test_changement_autorise_sans_vrac_ni_historique(self):
        """
        Le cas courant : le marchand corrige la taille du conditionnement peu
        après l'avoir saisie, y compris s'il a déjà du stock.
        """
        # 3 paquets scellés, aucun emballage entamé.
        Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse, quantity=Decimal('36.000'),
            package_quantity=Decimal('3.000'),
        )

        response = self._patch(units_per_package=24)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.units_per_package, 24)

    def test_changement_refuse_si_un_emballage_est_ouvert(self):
        Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('24.000'), loose_quantity=Decimal('12.000'),
        )

        response = self._patch(units_per_package=24)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('emballage', str(response.data['units_per_package']))

    def test_changement_refuse_si_ventes_en_gros_enregistrees(self):
        from django.utils import timezone
        from apps.sales.models import Sale, SaleItem

        sale = Sale.objects.create(
            organization=self.org, reference='VTE-PKG-0001',
            warehouse=self.warehouse, status='completed',
            currency='CDF', total=Decimal('6000.00'),
            sale_date=timezone.now(),
        )
        SaleItem.objects.create(
            organization=self.org, sale=sale, product=self.product,
            quantity=Decimal('12.000'), unit_price=Decimal('600.00'),
            package_quantity=Decimal('1.000'),
            package_unit_price=Decimal('6000.00'),
            packaging_factor=12,
        )

        response = self._patch(units_per_package=24)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('ventes en', str(response.data['units_per_package']))

    def test_meme_valeur_toujours_acceptee(self):
        Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('24.000'), loose_quantity=Decimal('12.000'),
        )

        response = self._patch(units_per_package=12, selling_price='650.00')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ProductPackagingReadTests(_ProductPackagingSetup):
    """Ce que l'API expose au frontend."""

    def setUp(self):
        super().setUp()
        self._post()
        self.product = Product.objects.get(sku='EAU-50')
        # 1 paquet scellé + 10 bouteilles.
        Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('22.000'),
            package_quantity=Decimal('1.000'),
            loose_quantity=Decimal('10.000'),
        )

    def test_detail_expose_la_phrase_de_confirmation(self):
        response = self.client.get(
            f'/api/v1/products/{self.product.id}/', **self._headers
        )
        self.assertEqual(
            response.data['packaging_summary'],
            "Ce produit sera vendu par paquet de 12 bouteilles, ou à l'unité (bouteille).",
        )

    def test_liste_expose_le_stock_lisible(self):
        response = self.client.get(
            '/api/v1/products/',
            {'warehouse': str(self.warehouse.id)},
            **self._headers,
        )
        row = next(
            r for r in response.data['results'] if r['sku'] == 'EAU-50'
        )
        self.assertEqual(row['stock_display'], '1 paquet + 10 bouteilles')
        self.assertEqual(row['stock_packages'], 1)
        self.assertEqual(row['stock_loose'], '10.000')

    def test_produit_simple_affiche_sa_quantite_sans_paquet(self):
        simple = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV-01',
            unit=self.bottle, selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=simple,
            warehouse=self.warehouse, quantity=Decimal('22.000'),
        )

        response = self.client.get(
            '/api/v1/products/',
            {'warehouse': str(self.warehouse.id)},
            **self._headers,
        )
        row = next(r for r in response.data['results'] if r['sku'] == 'SAV-01')
        self.assertEqual(row['stock_display'], '22 bouteilles')
        self.assertIsNone(row['stock_packages'])
