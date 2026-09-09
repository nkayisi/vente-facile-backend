"""
Ce qui reste à rendre sur une ligne de facture, exposé par l'API.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LA RÈGLE VIT SUR LE SERVEUR, DONC ELLE DOIT S'Y LIRE.                       │
│                                                                              │
│ `SaleReturnCreateSerializer` refuse depuis peu de rendre deux fois la même   │
│ marchandise : le reste à rendre est la quantité vendue moins ce qui a déjà   │
│ été rendu. Mais la fiche de vente ne DISAIT pas ce reste, si bien qu'un      │
│ écran ne pouvait proposer sa borne qu'en recalculant la règle de son côté -  │
│ c'est-à-dire en la faisant vivre à deux endroits, et diverger.               │
│                                                                              │
│ Le terminal la recalcule bien, lui, et il le doit : il travaille hors ligne  │
│ et compte aussi ses retours encore en file. Le back-office, lui, est TOUJOURS│
│ en ligne, et n'a aucune raison de tenir une seconde copie.                    │
└──────────────────────────────────────────────────────────────────────────────┘

Un retour REJETÉ ne consomme rien - la marchandise est encore là. Un BROUILLON,
si : il est approuvable, et deux brouillons sur les mêmes unités seraient tous
deux approuvables.
"""
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.products.models import Product, Unit
from apps.sales.models import Sale, SaleItem, SaleReturn, SaleReturnItem
from apps.sales.tests._helpers import make_org_with_users


class _VenteRendue(APITestCase):
    """Une vente de 10 pièces, sur laquelle on rendra par petits bouts."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)

        self.piece = Unit.objects.create(
            organization=self.org, name='PIECE', symbol='pc'
        )
        self.product = Product.objects.create(
            organization=self.org, name='Savon', slug='savon-r', sku='SAV-R',
            unit=self.piece,
            cost_price=Decimal('400.00'), selling_price=Decimal('1000.00'),
            is_taxable=False, track_inventory=True, is_active=True,
        )
        self.sale = Sale.objects.create(
            organization=self.org, reference='VTE-RET-001',
            warehouse=self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=Decimal('10000'), total=Decimal('10000'),
            amount_paid=Decimal('10000'),
            sold_by=self.manager, sale_date=timezone.now(),
        )
        self.item = SaleItem.objects.create(
            organization=self.org, sale=self.sale, product=self.product,
            quantity=Decimal('10'), unit_price=Decimal('1000.00'),
            cost_price=Decimal('400.00'),
            subtotal=Decimal('10000'), total=Decimal('10000'),
        )

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _retour(self, quantite, statut):
        retour = SaleReturn.objects.create(
            organization=self.org, original_sale=self.sale,
            reference=f'RET-{statut}-{quantite}',
            return_type='partial', status=statut,
            total_amount=Decimal('0'), refund_amount=Decimal('0'),
            reason='controle', created_by=self.manager,
        )
        SaleReturnItem.objects.create(
            organization=self.org, sale_return=retour,
            original_item=self.item, quantity=Decimal(quantite),
            unit_price=Decimal('1000.00'), total=Decimal('0'),
        )
        return retour

    def _ligne(self):
        reponse = self.client.get(
            f'/api/v1/sales/{self.sale.id}/', **self._headers
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        return reponse.data['items'][0]


class QuantiteRendueExposeeTests(_VenteRendue):

    def test_une_ligne_intacte_n_a_rien_de_rendu(self):
        ligne = self._ligne()
        self.assertEqual(Decimal(ligne['returned_quantity']), Decimal('0'))
        self.assertEqual(Decimal(ligne['returnable_quantity']), Decimal('10'))

    def test_un_retour_approuve_consomme(self):
        self._retour('3', 'approved')
        ligne = self._ligne()
        self.assertEqual(Decimal(ligne['returned_quantity']), Decimal('3'))
        self.assertEqual(Decimal(ligne['returnable_quantity']), Decimal('7'))

    def test_un_BROUILLON_consomme_lui_aussi(self):
        """Il est approuvable : deux brouillons seraient tous deux appliqués."""
        self._retour('4', 'draft')
        ligne = self._ligne()
        self.assertEqual(Decimal(ligne['returned_quantity']), Decimal('4'))

    def test_un_retour_REJETE_ne_consomme_rien(self):
        """La marchandise est encore là : le client peut la rendre pour de bon."""
        self._retour('4', 'rejected')
        ligne = self._ligne()
        self.assertEqual(Decimal(ligne['returned_quantity']), Decimal('0'))
        self.assertEqual(Decimal(ligne['returnable_quantity']), Decimal('10'))

    def test_le_reste_ne_descend_JAMAIS_sous_zero(self):
        """
        Une donnée ancienne peut porter plus de rendu que de vendu. Un reste
        négatif s'afficherait tel quel sur un formulaire, et le plafond qu'il
        pose n'aurait aucun sens.
        """
        self._retour('12', 'approved')
        ligne = self._ligne()
        self.assertEqual(Decimal(ligne['returnable_quantity']), Decimal('0'))

    @override_settings(DEBUG=False)
    def test_la_fiche_ne_paie_PAS_une_requete_par_LIGNE(self):
        """
        Le reste à rendre s'annote, il ne se compte pas ligne à ligne : une
        facture de trente articles ferait trente requêtes de plus à chaque
        ouverture. C'est le budget déjà défendu par
        `test_serialization_query_budget`.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(6):
            produit = Product.objects.create(
                organization=self.org, name=f'Article {i}', slug=f'art-r-{i}',
                sku=f'AR-{i}', unit=self.piece,
                cost_price=Decimal('100'), selling_price=Decimal('200'),
                is_taxable=False, track_inventory=True, is_active=True,
            )
            SaleItem.objects.create(
                organization=self.org, sale=self.sale, product=produit,
                quantity=Decimal('1'), unit_price=Decimal('200'),
                cost_price=Decimal('100'),
                subtotal=Decimal('200'), total=Decimal('200'),
            )

        self._ligne()  # amorce des caches d'identité
        with CaptureQueriesContext(connection) as capture:
            self._ligne()
        peu = len(capture)

        for i in range(6, 18):
            produit = Product.objects.create(
                organization=self.org, name=f'Article {i}', slug=f'art-r-{i}',
                sku=f'AR-{i}', unit=self.piece,
                cost_price=Decimal('100'), selling_price=Decimal('200'),
                is_taxable=False, track_inventory=True, is_active=True,
            )
            SaleItem.objects.create(
                organization=self.org, sale=self.sale, product=produit,
                quantity=Decimal('1'), unit_price=Decimal('200'),
                cost_price=Decimal('100'),
                subtotal=Decimal('200'), total=Decimal('200'),
            )
        with CaptureQueriesContext(connection) as capture:
            self._ligne()

        self.assertEqual(peu, len(capture))


class RetoursFiltrablesParVenteTests(_VenteRendue):

    def test_on_liste_les_retours_D_UNE_vente(self):
        """
        Sans ce filtre, une fiche de vente ne peut pas montrer ses retours : il
        faudrait tirer toute la table et trier côté client.
        """
        self._retour('2', 'approved')
        autre = Sale.objects.create(
            organization=self.org, reference='VTE-RET-002',
            warehouse=self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=Decimal('0'), total=Decimal('0'), amount_paid=Decimal('0'),
            sold_by=self.manager, sale_date=timezone.now(),
        )
        SaleReturn.objects.create(
            organization=self.org, original_sale=autre, reference='RET-AUTRE',
            return_type='partial', status='draft',
            total_amount=Decimal('0'), refund_amount=Decimal('0'),
            reason='x', created_by=self.manager,
        )

        reponse = self.client.get(
            f'/api/v1/sale-returns/?original_sale={self.sale.id}',
            **self._headers,
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        refs = [r['reference'] for r in reponse.data['results']]
        self.assertEqual(refs, ['RET-approved-2'])
