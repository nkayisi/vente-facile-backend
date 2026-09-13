"""
Les colonnes de fichier descendent en URL utilisable, jamais en clé de stockage.

`queryset.values()` rend la colonne BRUTE d'un `FileField` - « products/x.jpg » -
sans `MEDIA_URL` ni hôte. Le terminal la traitait comme une URL finie : les
photos d'articles n'ont jamais paru sur mobile, et rien ne le signalait, le
composant d'image retombant en silence sur son icône de repli.

Ces cas tiennent la résolution dans les DEUX configurations de stockage, parce
que c'est la bascule vers S3 qui casserait un préfixe deviné côté client.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users

PULL = '/api/v1/sync/pull/'


class UrlsDeFichierAuTirageTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        # Rôle BORNÉ, jamais propriétaire : le périmètre entrepôt sort en amont
        # pour un propriétaire, et la moitié du code ne serait pas exécutée.
        self.client.force_authenticate(user=self.manager)
        self.produit = Product.objects.create(
            organization=self.org,
            name='Boisson',
            slug='boisson',
            sku='B1',
            selling_price=Decimal('2000.00'),
            cost_price=Decimal('1500.00'),
            track_inventory=True,
            image='products/photo-test.jpg',
        )

    def _ligne(self):
        resp = self.client.get(
            PULL,
            {'table': 'products', 'limit': 500},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        lignes = [r for r in resp.data['rows'] if r['id'] == str(self.produit.id)]
        self.assertEqual(len(lignes), 1, "l'article doit descendre une fois")
        return lignes[0]

    @override_settings(PUBLIC_BACKEND_URL='https://api.exemple.cd')
    def test_stockage_local_rend_une_url_absolue(self):
        image = self._ligne()['image']
        self.assertTrue(
            image.startswith('https://api.exemple.cd/'),
            f"l'URL doit porter l'hôte public, reçu : {image!r}",
        )
        self.assertIn('photo-test.jpg', image)
        # Le défaut qu'on referme : la clé de stockage nue.
        self.assertNotEqual(image, 'products/photo-test.jpg')

    def test_stockage_distant_est_laisse_intact(self):
        # Quand `django-storages` est actif, `default_storage.url` rend déjà une
        # URL absolue : la préfixer une seconde fois la casserait.
        distante = 'https://seau.exemple.net/media/products/photo-test.jpg'
        with patch('apps.sync.pull.default_storage.url', return_value=distante):
            self.assertEqual(self._ligne()['image'], distante)

    def test_une_colonne_vide_reste_vide(self):
        # `null` ne se lit jamais comme autre chose : un article sans photo ne
        # doit pas descendre avec l'URL du dossier de stockage.
        self.produit.image = ''
        self.produit.save(update_fields=['image'])
        self.assertIn(self._ligne()['image'], ('', None))

    def test_un_nom_abime_n_arrete_pas_le_tirage(self):
        # Un seul nom de fichier illisible ne doit pas faire répondre 500 :
        # cela bloquerait la table entière, sur tous les terminaux à la fois.
        with patch(
            'apps.sync.pull.default_storage.url', side_effect=ValueError('nom invalide')
        ):
            self.assertEqual(self._ligne()['image'], 'products/photo-test.jpg')
