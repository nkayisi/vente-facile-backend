"""
Réquisitions de produits : la liste de ce qu'il faut acheter.

┌──────────────────────────────────────────────────────────────────────────────┐
│ T12 DU RETOUR DE TEST. Rien n'existait sous ce nom.                         │
│                                                                              │
│ Et ce n'est PAS une commande d'achat : `PurchaseOrder.supplier` est          │
│ obligatoire, alors qu'une réquisition dit « il nous manque ceci » sans qu'on │
│ sache encore chez qui l'acheter - c'est même souvent la question qu'on se    │
│ pose en la lisant.                                                           │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LE MAGASINIER DEMANDE, LE GÉRANT DÉCIDE, et c'est ce que la moitié de ces
tests éprouve : c'est le magasinier qui voit le rayon vide, et c'est le gérant
qui engage l'argent.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.purchases.models import Requisition
from apps.purchases.services import RequisitionRefusee, create_requisition
from apps.sales.tests._helpers import make_org_with_users


class RequisitionsTests(APITestCase):
    URL = '/api/v1/requisitions/'

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh = self.d['warehouse']

        # ⚠ `create`, pas `get_or_create` : le manager de `Unit` emploie un
        # combinateur, et `get_or_create` lève alors « Calling QuerySet.get(...)
        # with filters after ... is not supported ». La fixture est neuve à
        # chaque test, donc il n'y a rien à retrouver.
        from apps.products.models import Unit
        # ⚠ `Unit` n'a PAS de slug, contrairement à `Product`, `Category` et
        # `Brand` : son nom est court et déjà unique par organisation.
        self.unite = Unit.objects.create(
            organization=self.org, name='PIECE', symbol='pc',
        )

        self.magasinier = self.d['cashier_b']
        m = OrganizationMembership.objects.get(user=self.magasinier, organization=self.org)
        m.role = OrganizationMembership.Role.STOCK_KEEPER
        m.save(update_fields=['role'])

        # Trois articles : un SOUS son seuil, un au-dessus, un sans seuil.
        self.manquant = self._article('Savon', 'SAV-1', seuil=20, stock=5)
        self.suffisant = self._article('Sucre', 'SUC-1', seuil=10, stock=80)
        self.sans_seuil = self._article('Sachet', 'SAC-1', seuil=0, stock=0)
        # Et un service, qui n'a pas de stock du tout.
        self.service = Product.objects.create(
            organization=self.org, name='Livraison', sku='SRV-1', slug='srv-1',
            cost_price=Decimal('0'), selling_price=Decimal('5'),
            track_inventory=False, reorder_point=Decimal('99'),
        )

    def _article(self, nom, sku, *, seuil, stock):
        p = Product.objects.create(
            organization=self.org, name=nom, sku=sku, slug=sku.lower(),
            cost_price=Decimal('10'), selling_price=Decimal('15'),
            track_inventory=True, reorder_point=Decimal(seuil),
            # ⚠ L'unité est NOMMÉE sur le document (« 5 PIECES ») : sans elle,
            # `format_base_total` rend un nombre nu, et « 5 » ne dit pas 5 quoi.
            unit=self.unite,
        )
        Stock.objects.create(
            organization=self.org, product=p, warehouse=self.wh,
            quantity=Decimal(stock), avg_cost=Decimal('10'),
        )
        return p

    def _get(self, qui, chemin, **params):
        self.client.force_authenticate(user=qui)
        return self.client.get(chemin, params, HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def _post(self, qui, chemin, corps=None):
        self.client.force_authenticate(user=qui)
        return self.client.post(
            chemin, corps or {}, format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    # --- La proposition ------------------------------------------------------

    def test_elle_propose_D_EMBLEE_ce_qui_est_sous_le_seuil(self):
        """C'est la demande de T12, mot pour mot."""
        r = self._get(self.d['owner'], f'{self.URL}proposer/', warehouse=str(self.wh.id))
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        noms = {i['product_name'] for i in r.data['items']}
        self.assertEqual(noms, {'Savon'})

    def test_elle_suggere_de_REMONTER_au_seuil(self):
        """
        Une proposition, pas une décision : le magasinier corrige. Lui imposer
        la quantité ferait acheter un carton là où il en faut dix.
        """
        r = self._get(self.d['owner'], f'{self.URL}proposer/', warehouse=str(self.wh.id))
        ligne = r.data['items'][0]
        self.assertEqual(Decimal(str(ligne['suggested'])), Decimal('15'))

    def test_un_article_SANS_seuil_n_est_jamais_propose(self):
        """
        Un `reorder_point` à zéro ne déclenche rien : sinon tout article épuisé
        serait réquisitionné en permanence, y compris ceux qu'on ne
        réapprovisionne pas.
        """
        r = self._get(self.d['owner'], f'{self.URL}proposer/', warehouse=str(self.wh.id))
        self.assertNotIn('Sachet', {i['product_name'] for i in r.data['items']})

    def test_un_SERVICE_n_est_jamais_propose(self):
        r = self._get(self.d['owner'], f'{self.URL}proposer/', warehouse=str(self.wh.id))
        self.assertNotIn('Livraison', {i['product_name'] for i in r.data['items']})

    def test_l_entrepot_est_REQUIS(self):
        r = self._get(self.d['owner'], f'{self.URL}proposer/')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_entrepot_hors_perimetre_LEVE(self):
        """Un identifiant hors périmètre lève, il ne rend jamais un ensemble vide."""
        autre = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'], name='Nord', code='WH-N',
        )
        r = self._get(
            self.magasinier, f'{self.URL}proposer/', warehouse=str(autre.id)
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Le droit ------------------------------------------------------------

    def test_le_magasinier_DEMANDE(self):
        r = self._get(self.magasinier, f'{self.URL}proposer/', warehouse=str(self.wh.id))
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_le_caissier_n_y_a_pas_acces(self):
        r = self._get(self.d['cashier_a'], f'{self.URL}proposer/', warehouse=str(self.wh.id))
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_le_magasinier_NE_DECIDE_PAS(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ LA RÈGLE QUI PORTE TOUT LE CIRCUIT.                                 │
        │                                                                      │
        │ C'est lui qui voit le rayon vide, et c'est le gérant qui engage      │
        │ l'argent. `requisitions.create` va donc au magasinier,               │
        │ `requisitions.approve` non.                                          │
        └──────────────────────────────────────────────────────────────────────┘
        """
        req = self._creer(self.magasinier)
        self._post(self.magasinier, f'{self.URL}{req["id"]}/submit/')
        r = self._post(self.magasinier, f'{self.URL}{req["id"]}/approve/')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    # --- La création ---------------------------------------------------------

    def _creer(self, qui, quantite='15'):
        r = self._post(qui, self.URL, {
            'warehouse': str(self.wh.id),
            'notes': 'Réassort',
            'items': [{'product': str(self.manquant.id), 'quantity_requested': quantite}],
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        return r.data

    def test_elle_nait_en_BROUILLON(self):
        self.assertEqual(self._creer(self.magasinier)['status'], 'draft')

    def test_le_stock_et_le_seuil_sont_FIGES_a_la_saisie(self):
        """
        ⚠ LE DÉFAUT MESURÉ CONTRE LE VRAI SERVEUR : la première écriture lisait
        le seuil dans la ligne ENVOYÉE, que l'API ne porte pas. L'instantané
        sortait à `0.000` sous une proposition qui annonçait 12 - le document
        aurait dit que l'article n'avait aucun seuil, alors que c'est son seuil
        qui a déclenché la demande.
        """
        ligne = self._creer(self.magasinier)['items'][0]
        self.assertEqual(Decimal(ligne['stock_at_request']), Decimal('5.000'))
        self.assertEqual(Decimal(ligne['reorder_point_at_request']), Decimal('20.000'))

    def test_l_instantane_ne_bouge_PAS_quand_le_stock_bouge(self):
        """Les relire plus tard donnerait le stock d'aujourd'hui, et le document
        ne dirait plus pourquoi la demande a été faite."""
        req = self._creer(self.magasinier)
        Stock.objects.filter(product=self.manquant, warehouse=self.wh).update(
            quantity=Decimal('99')
        )
        r = self._get(self.magasinier, f"{self.URL}{req['id']}/")
        self.assertEqual(
            Decimal(r.data['items'][0]['stock_at_request']), Decimal('5.000')
        )

    def test_une_requisition_SANS_article_est_refusee(self):
        r = self._post(self.magasinier, self.URL, {
            'warehouse': str(self.wh.id), 'items': [],
        })
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_meme_article_ne_figure_qu_une_fois(self):
        r = self._post(self.magasinier, self.URL, {
            'warehouse': str(self.wh.id),
            'items': [
                {'product': str(self.manquant.id), 'quantity_requested': '5'},
                {'product': str(self.manquant.id), 'quantity_requested': '7'},
            ],
        })
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_une_quantite_nulle_est_refusee(self):
        r = self._post(self.magasinier, self.URL, {
            'warehouse': str(self.wh.id),
            'items': [{'product': str(self.manquant.id), 'quantity_requested': '0'}],
        })
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_le_NUMERO_du_terminal_est_repris(self):
        """
        Une réquisition imprimée au fond d'un dépôt porte déjà son numéro : le
        remplacer ferait que le papier du magasinier ne désigne plus rien.
        """
        r = self._post(self.magasinier, self.URL, {
            'warehouse': str(self.wh.id),
            'reference': 'REQ-20261009-K7QM-0007',
            'items': [{'product': str(self.manquant.id), 'quantity_requested': '5'}],
        })
        self.assertEqual(r.data['reference'], 'REQ-20261009-K7QM-0007')

    # --- Les transitions -----------------------------------------------------

    def test_le_cycle_complet(self):
        req = self._creer(self.magasinier)
        self.assertEqual(
            self._post(self.magasinier, f"{self.URL}{req['id']}/submit/").data['status'],
            'submitted',
        )
        r = self._post(self.d['manager'], f"{self.URL}{req['id']}/approve/", {'notes': 'OK'})
        self.assertEqual(r.data['status'], 'approved')
        self.assertEqual(r.data['decision_notes'], 'OK')
        self.assertIsNotNone(r.data['decided_at'])

    def test_une_transition_impossible_est_un_refus_DETERMINISTE(self):
        """
        Jamais un `retry` : réessayer une approbation déjà faite n'aurait aucun
        sens, et le journal du terminal la renverrait indéfiniment.
        """
        req = self._creer(self.magasinier)
        r = self._post(self.d['manager'], f"{self.URL}{req['id']}/approve/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_une_transition_DEJA_FAITE_est_idempotente(self):
        """Le terminal a pu pousser deux fois : un échec ferait réessayer."""
        req = self._creer(self.magasinier)
        self._post(self.magasinier, f"{self.URL}{req['id']}/submit/")
        self._post(self.d['manager'], f"{self.URL}{req['id']}/approve/")
        r = self._post(self.d['manager'], f"{self.URL}{req['id']}/approve/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data['status'], 'approved')

    def test_annuler_part_de_n_importe_ou(self):
        """On annule une demande qu'on n'aurait pas dû faire, sans avoir à la
        faire approuver d'abord."""
        req = self._creer(self.magasinier)
        r = self._post(self.magasinier, f"{self.URL}{req['id']}/cancel/")
        self.assertEqual(r.data['status'], 'cancelled')

    def test_annuler_une_decision_prise_est_refuse(self):
        req = self._creer(self.magasinier)
        self._post(self.magasinier, f"{self.URL}{req['id']}/submit/")
        self._post(self.d['manager'], f"{self.URL}{req['id']}/approve/")
        r = self._post(self.magasinier, f"{self.URL}{req['id']}/cancel/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Ce qui N'EXISTE PAS -------------------------------------------------

    def test_une_requisition_ne_se_MODIFIE_ni_ne_se_SUPPRIME(self):
        """
        Elle s'imprime et s'emporte au marché : la réécrire sous le même numéro
        ferait que le papier du magasinier ne désigne plus la même demande. On
        la corrige en l'annulant et en en créant une autre.
        """
        req = self._creer(self.magasinier)
        self.client.force_authenticate(user=self.d['owner'])
        entetes = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        for methode in (self.client.patch, self.client.put, self.client.delete):
            with self.subTest(methode=methode.__name__):
                r = methode(f"{self.URL}{req['id']}/", {}, format='json', **entetes)
                self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_le_service_leve_un_refus_DETERMINISTE(self):
        with self.assertRaises(RequisitionRefusee):
            create_requisition(self.org, warehouse=self.wh, lignes=[])


class DocumentDeRequisitionTests(RequisitionsTests):
    """
    Le document qu'on emporte au marché.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ IL LIT L'INSTANTANÉ, JAMAIS LE STOCK DU JOUR.                           │
    │                                                                          │
    │ C'est toute la raison d'être du figeage : le papier doit dire pourquoi   │
    │ la demande a été faite, et le stock a bougé depuis.                      │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def _document(self, qui, rid, fmt='csv'):
        return self._get(self.__class__.URL + f'{rid}/export/', **{}) if False else (
            self._get(qui, f'{self.URL}{rid}/export/', export_format=fmt)
        )

    def test_les_trois_formats_sortent(self):
        req = self._creer(self.magasinier)
        for fmt in ('pdf', 'xlsx', 'csv'):
            with self.subTest(format=fmt):
                r = self._document(self.magasinier, req['id'], fmt)
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                self.assertGreater(len(r.content), 100)

    def test_il_porte_le_NUMERO_et_l_entrepot(self):
        req = self._creer(self.magasinier)
        corps = self._document(self.magasinier, req['id']).content.decode('utf-8-sig')
        self.assertIn(req['reference'], corps)
        self.assertIn(self.wh.name, corps)

    def test_il_lit_l_INSTANTANE_meme_si_le_stock_a_bouge(self):
        req = self._creer(self.magasinier)
        Stock.objects.filter(product=self.manquant, warehouse=self.wh).update(
            quantity=Decimal('999')
        )
        corps = self._document(self.magasinier, req['id']).content.decode('utf-8-sig')
        # Le stock figé était 5, pas 999.
        self.assertIn('5', corps)
        self.assertNotIn('999', corps)

    def test_les_quantites_portent_leur_UNITE(self):
        """« 5 » ne dit pas 5 quoi, et c'est au marché que la question se pose."""
        req = self._creer(self.magasinier)
        corps = self._document(self.magasinier, req['id']).content.decode('utf-8-sig')
        self.assertIn('PIECE', corps.upper())

    def test_la_colonne_du_PRIX_CONSTATE_est_vide(self):
        """
        Délibérément : le magasinier y écrit au stylo le prix qu'on lui annonce.
        Même intention que la feuille d'inventaire.
        """
        req = self._creer(self.magasinier)
        corps = self._document(self.magasinier, req['id']).content.decode('utf-8-sig')
        self.assertIn('Prix constaté', corps)

    def test_un_caissier_ne_peut_pas_l_imprimer(self):
        req = self._creer(self.magasinier)
        r = self._document(self.d['cashier_a'], req['id'])
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
