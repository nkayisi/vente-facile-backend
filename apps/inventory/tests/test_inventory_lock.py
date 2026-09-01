"""
Le verrou d'inventaire : accessible, et du bon périmètre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE VERROU ÉTAIT INERTE SUR LE WEB AUSSI.                                     │
│                                                                              │
│ `locked_products` n'était dans AUCUN `action_permissions`, et la règle du    │
│ dépôt est « action non listée = accès refusé » : l'endpoint répondait 403 à  │
│ tous les rôles, propriétaire compris. Le POS web appelait donc une route     │
│ qui refusait, échouait en silence, et n'a jamais posé le moindre verrou.     │
│                                                                              │
│ C'est ce qui a causé le refus observé au comptoir : une vente encaissée ET   │
│ IMPRIMÉE, puis refusée par le serveur parce que ses produits étaient bloqués │
│ par un inventaire en cours. Le mobile n'avait aucun retard à rattraper sur   │
│ le web : les deux surfaces étaient aveugles.                                 │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN PRODUIT ÉTAIT COMPTÉ SANS ÊTRE VERROUILLÉ.                                │
│                                                                              │
│ `target_products` (ce que la session COMPTE) filtre le `subtree_ids()` des   │
│ catégories visées ; `get_locked_product_ids` (ce que la session BLOQUE)      │
│ filtrait les catégories exactes. Un produit rangé dans « Boissons > Sodas »  │
│ sous une session portant sur « Boissons » était donc compté au démarrage et  │
│ vendable pendant le comptage : l'écart mesuré à la validation n'est plus un  │
│ manquant, c'est le volume des ventes de la journée, et le magasinier         │
│ ajusterait son stock sur ce chiffre.                                         │
│                                                                              │
│ Compter et verrouiller doivent lire le MÊME jeu de produits. Il n'y a pas de │
│ lecture intermédiaire défendable.                                            │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`** (règle du chantier, posée par
`test_pull_scope_resolves`) : `accessible_warehouse_ids` sort en amont pour le
propriétaire, et la moitié du code de périmètre ne serait pas exécutée.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import InventorySession, Stock
from apps.inventory.services import target_products
from apps.organizations.models import OrganizationMembership
from apps.products.models import Category, Product
from apps.sales.tests._helpers import make_org_with_users, make_user

LOCKED = '/api/v1/inventory-sessions/locked-products/'


class _Base(APITestCase):
    """Une catégorie mère, sa fille, un produit dans chacune, du stock partout."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        # Un magasinier : c'est le rôle qui porte `inventory.view` sans porter
        # `sales.view`, donc celui qui distingue les deux codes de permission.
        self.stock_keeper = make_user('sk@vf.test', 'Stock', 'Keeper')
        m = OrganizationMembership.objects.create(
            user=self.stock_keeper, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        m.assigned_warehouses.add(self.warehouse)

        self.mere = Category.objects.create(
            organization=self.org, name='Boissons', slug='boissons',
        )
        self.fille = Category.objects.create(
            organization=self.org, name='Sodas', slug='sodas', parent=self.mere,
        )

        self.produit_mere = self._produit('Eau plate', 'EAU-01', self.mere)
        self.produit_fille = self._produit('Cola 33cl', 'COL-01', self.fille)

    def _produit(self, nom, sku, categorie):
        produit = Product.objects.create(
            organization=self.org, name=nom, sku=sku, slug=sku.lower(),
            category=categorie,
            cost_price=Decimal('500.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal('10.000'), avg_cost=Decimal('500.00'),
        )
        return produit

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _session_categorie(self):
        """Session démarrée, portant sur la catégorie MÈRE seule."""
        session = InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse,
            name='Inventaire boissons', scope_type=InventorySession.ScopeType.CATEGORY,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        session.categories.add(self.mere)
        return session


class VerrouAccessibleTests(_Base):
    """L'endpoint répond, à chaque rôle qui vend ou qui compte."""

    def test_le_caissier_peut_lire_le_verrou(self):
        """
        Le caissier est le PREMIER concerné : c'est lui qui encaisse.

        Il n'a pas `inventory.view` (voir `apps/core/services.py`) ; c'est
        `sales.view` qui lui ouvre la route, d'où les deux codes déclarés.
        """
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.get(LOCKED, **self._headers)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)

    def test_le_magasinier_peut_lire_le_verrou(self):
        self.client.force_authenticate(user=self.stock_keeper)
        reponse = self.client.get(LOCKED, **self._headers)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)

    def test_le_gerant_peut_lire_le_verrou(self):
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.get(LOCKED, **self._headers)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)

    def test_la_session_qui_verrouille_est_NOMMEE(self):
        """
        Un refus sans référence n'a pas d'issue : le caissier ne sait ni quoi
        attendre, ni à qui parler. La réponse porte les sessions actives.
        """
        session = self._session_categorie()
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.get(LOCKED, **self._headers)

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertTrue(reponse.data['has_active_inventory'])
        refs = [s['reference'] for s in reponse.data['active_sessions']]
        self.assertIn(session.reference, refs)


class PerimetreDuVerrouTests(_Base):
    """Ce qui est compté est ce qui est bloqué. Sans exception."""

    def test_le_produit_d_une_SOUS_categorie_est_verrouille(self):
        session = self._session_categorie()
        verrouilles = session.get_locked_product_ids()

        self.assertIn(self.produit_mere.id, verrouilles)
        self.assertIn(
            self.produit_fille.id, verrouilles,
            "Un produit de sous-catégorie est COMPTÉ par la session : le laisser "
            "vendable fait mesurer les ventes du jour comme un manquant.",
        )

    def test_compter_et_verrouiller_lisent_LE_MEME_JEU(self):
        """
        Le garde-fou de fond : les deux lectures sont croisées ici, pour qu'un
        futur filtre ajouté d'un seul côté fasse échouer le test.
        """
        session = self._session_categorie()
        comptes = set(target_products(session).values_list('id', flat=True))
        verrouilles = session.get_locked_product_ids()

        self.assertTrue(comptes)
        self.assertTrue(
            comptes <= verrouilles,
            f"Comptés mais non verrouillés : {sorted(comptes - verrouilles)}",
        )

    def test_le_produit_d_une_autre_categorie_reste_vendable(self):
        """La réciproque : élargir au sous-arbre n'élargit pas à tout."""
        autre = Category.objects.create(
            organization=self.org, name='Épicerie', slug='epicerie',
        )
        hors_champ = self._produit('Riz 5kg', 'RIZ-01', autre)

        session = self._session_categorie()
        self.assertNotIn(hors_champ.id, session.get_locked_product_ids())

    def test_l_endpoint_rend_le_produit_de_sous_categorie(self):
        """De bout en bout : c'est cette liste que le comptoir consomme."""
        self._session_categorie()
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.get(LOCKED, **self._headers)

        ids = {str(i) for i in reponse.data['locked_product_ids']}
        self.assertIn(str(self.produit_fille.id), ids)


class DemarrageParCategorieTests(_Base):
    """
    Démarrer une session PAR CATÉGORIE, de bout en bout.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ AUCUN TEST NE DÉMARRAIT DE SESSION PAR CATÉGORIE, ET ELLE PLANTAIT.     │
    │                                                                          │
    │ `target_products` appelait `categorie.subtree_ids()` sur une INSTANCE,   │
    │ alors que c'est une méthode de CLASSE qui prend la liste des racines :   │
    │ `TypeError`, donc 500, sur la vue comme sur le journal. Le défaut est né │
    │ de l'extraction du corps dans le service (lot 8) et a traversé la suite  │
    │ entière, toutes les sessions des tests étant de périmètre `full`.        │
    │                                                                          │
    │ Or un inventaire partiel est le cas COURANT : on ne ferme pas un magasin │
    │ pour compter les boissons.                                               │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def _demarrer(self):
        """Session par catégorie, créée puis démarrée en GÉRANT (rôle borné)."""
        self.client.force_authenticate(user=self.manager)
        creation = self.client.post(
            '/api/v1/inventory-sessions/',
            {
                'warehouse': str(self.warehouse.id),
                'scope_type': 'category',
                'category_ids': [str(self.mere.id)],
                'name': 'Inventaire boissons',
            },
            format='json', **self._headers,
        )
        self.assertEqual(creation.status_code, status.HTTP_201_CREATED, creation.data)
        session = InventorySession.objects.get(organization=self.org)
        reponse = self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/start/',
            {}, format='json', **self._headers,
        )
        return session, reponse

    def test_demarrer_une_session_par_categorie_ABOUTIT(self):
        session, reponse = self._demarrer()
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.Status.IN_PROGRESS)

    def test_la_feuille_porte_le_produit_de_SOUS_categorie(self):
        session, _ = self._demarrer()
        comptes = set(session.counts.values_list('product_id', flat=True))

        self.assertIn(self.produit_mere.id, comptes)
        self.assertIn(self.produit_fille.id, comptes)

    def test_le_produit_hors_categorie_reste_hors_de_la_feuille(self):
        autre = Category.objects.create(
            organization=self.org, name='Épicerie', slug='epicerie',
        )
        hors_champ = self._produit('Riz 5kg', 'RIZ-01', autre)

        session, _ = self._demarrer()
        comptes = set(session.counts.values_list('product_id', flat=True))
        self.assertNotIn(hors_champ.id, comptes)
