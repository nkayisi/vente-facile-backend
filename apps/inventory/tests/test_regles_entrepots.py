"""
La vie d'un entrepôt : le créer, le modifier, le fermer, le supprimer.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LES DÉCISIONS DU 2026-11-14, ÉPROUVÉES PAR L'API.                           │
│                                                                              │
│ - Créer, activer, désactiver, désigner le principal, supprimer : le          │
│   PROPRIÉTAIRE seul. Le gérant renomme et réadresse SES entrepôts.           │
│ - Un seul principal, qui ne se désactive ni ne se supprime.                  │
│ - Fermer est refusé, en NOMMANT ce qui bloque, tant qu'une caisse est        │
│   ouverte, qu'un transfert est en route, qu'un inventaire n'est pas fini,   │
│   ou que c'est le seul entrepôt actif d'un membre. Supprimer l'est aussi    │
│   tant qu'il porte du stock.                                                 │
│ - Un entrepôt désactivé est fermé à toute opération et ne s'affecte plus.   │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Les refus se lisent par leur `code`, au premier niveau : c'est lui que le
back-office et le terminal lisent pour dire quoi faire.
"""
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.tests.migrations_harness import MigrationDataTestCase
from apps.inventory.models import (
    InventorySession, Stock, StockAdjustment, StockTransfer, Warehouse,
)
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.models import Register, RegisterSession
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users, make_user

R = OrganizationMembership.Role


class _Entrepots(APITestCase):
    """L'entrepôt principal A (celui de tous) et B, où travaille un caissier."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.depot_a = self.warehouse
        self.depot_b = Warehouse.objects.create(
            organization=self.org, branch=self.branch, name='Boutique B', code='WH-B',
        )
        self.caisse_b = Register.objects.create(
            organization=self.org, branch=self.branch, warehouse=self.depot_b,
            name='Caisse B', code='CB',
        )
        self.caissier_b = make_user('cb@e.test', 'Caissier', 'B')
        m = OrganizationMembership.objects.create(
            user=self.caissier_b, organization=self.org, role=R.CASHIER, is_active=True,
        )
        m.assigned_warehouses.set([self.depot_b])
        self.produit = Product.objects.create(
            organization=self.org, name='Riz', slug='riz', sku='RIZ',
            cost_price=Decimal('100'), selling_price=Decimal('200'),
            tax_rate=Decimal('0'), is_taxable=False, track_inventory=True,
        )

    def _h(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _patch(self, qui, entrepot, corps):
        self.client.force_authenticate(user=qui)
        return self.client.patch(
            f'/api/v1/warehouses/{entrepot.id}/', corps, format='json', **self._h(),
        )

    def _code(self, reponse):
        return reponse.json().get('code')


class CreerTests(_Entrepots):

    def setUp(self):
        from apps.subscriptions.models import Plan

        super().setUp()
        # Le plan de test plafonne à un entrepôt : on éprouve la RÈGLE, pas le plan.
        Plan.objects.filter(code='TEST').update(max_warehouses=10)

    def test_le_gerant_ne_cree_pas_d_entrepot(self):
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.post(
            '/api/v1/warehouses/', {'name': 'Dépôt C', 'code': 'WH-C'},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, reponse.data)

    def test_une_permission_reservee_accordee_avant_la_regle_n_ouvre_rien(self):
        # Posée en base, comme un gérant l'aurait reçue avant que l'écran la
        # refuse : elle ne doit plus rien ouvrir.
        OrganizationMembership.objects.filter(user=self.manager, organization=self.org).update(
            extra_permissions=['warehouses.create', 'warehouses.delete'],
        )
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.post(
            '/api/v1/warehouses/', {'name': 'Dépôt C', 'code': 'WH-C'},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, reponse.data)
        self.assertFalse(Warehouse.objects.filter(code='WH-C').exists())

    def test_la_creation_rend_l_identifiant(self):
        """⚠ La réponse ne portait pas `id` : la carte menait à « introuvable »."""
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.post(
            '/api/v1/warehouses/', {'name': 'Dépôt C', 'code': 'WH-C'},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.assertTrue(Warehouse.objects.filter(id=reponse.data['id'], name='Dépôt C').exists())

    def test_creer_un_principal_retire_le_titre_a_l_ancien(self):
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.post(
            '/api/v1/warehouses/', {'name': 'Dépôt C', 'code': 'WH-C', 'is_default': True},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        principaux = list(
            Warehouse.objects.filter(organization=self.org, is_default=True).values_list('name', flat=True)
        )
        self.assertEqual(principaux, ['Dépôt C'])


class ModifierTests(_Entrepots):

    def test_le_gerant_renomme_son_entrepot(self):
        reponse = self._patch(self.manager, self.depot_a, {'name': 'Dépôt central'})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.depot_a.refresh_from_db()
        self.assertEqual(self.depot_a.name, 'Dépôt central')

    def test_le_gerant_ne_desactive_pas(self):
        reponse = self._patch(self.manager, self.depot_a, {'is_active': False})
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, reponse.data)
        self.assertEqual(self._code(reponse), 'reserve_au_proprietaire')
        self.assertEqual(reponse.json()['fields'], ['is_active'])

    def test_le_gerant_ne_touche_pas_l_entrepot_d_un_autre(self):
        reponse = self._patch(self.manager, self.depot_b, {'name': 'X'})
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND, reponse.data)

    def test_designer_le_principal_retire_le_titre_a_l_ancien(self):
        reponse = self._patch(self.owner, self.depot_b, {'is_default': True})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.depot_a.refresh_from_db()
        self.depot_b.refresh_from_db()
        self.assertFalse(self.depot_a.is_default)
        self.assertTrue(self.depot_b.is_default)

    def test_le_principal_ne_perd_pas_son_titre_sans_successeur(self):
        reponse = self._patch(self.owner, self.depot_a, {'is_default': False})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'entrepot_principal')


class FermerTests(_Entrepots):
    """Chaque refus NOMME ce qui bloque : un « impossible » renvoie au support."""

    def test_le_principal_ne_se_desactive_pas(self):
        reponse = self._patch(self.owner, self.depot_a, {'is_active': False})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'entrepot_principal')

    def test_une_caisse_ouverte_bloque_et_se_nomme(self):
        RegisterSession.objects.create(
            organization=self.org, register=self.caisse_b, opened_by=self.caissier_b,
            opening_balance=Decimal('0'), status='open',
        )
        reponse = self._patch(self.owner, self.depot_b, {'is_active': False})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'session_ouverte')
        self.assertIn('Caisse B', reponse.json()['detail'])

    def test_un_transfert_en_route_bloque(self):
        StockTransfer.objects.create(
            organization=self.org, reference='TR-ROUTE', status='in_transit',
            source_warehouse=self.depot_a, destination_warehouse=self.depot_b,
            requested_by=self.owner,
        )
        reponse = self._patch(self.owner, self.depot_b, {'is_active': False})
        self.assertEqual(self._code(reponse), 'transfert_en_route', reponse.data)
        self.assertIn('TR-ROUTE', reponse.json()['detail'])

    def test_un_inventaire_en_cours_bloque(self):
        InventorySession.objects.create(
            organization=self.org, warehouse=self.depot_b, reference='INV-B',
            name='Inventaire B', status='in_progress', created_by=self.owner,
        )
        reponse = self._patch(self.owner, self.depot_b, {'is_active': False})
        self.assertEqual(self._code(reponse), 'inventaire_en_cours', reponse.data)

    def test_le_seul_entrepot_actif_d_un_membre_bloque_et_le_nomme(self):
        reponse = self._patch(self.owner, self.depot_b, {'is_active': False})
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'seul_entrepot_actif')
        self.assertIn('Caissier B', reponse.json()['detail'])

    def test_desactiver_ferme_les_caisses_et_garde_l_historique(self):
        OrganizationMembership.objects.get(user=self.caissier_b).assigned_warehouses.set(
            [self.depot_a]
        )
        reponse = self._patch(self.owner, self.depot_b, {'is_active': False})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.caisse_b.refresh_from_db()
        self.assertFalse(self.caisse_b.is_active)
        # Il reste lisible : on relit son passé, on filtre ses ventes.
        self.client.force_authenticate(user=self.owner)
        liste = self.client.get('/api/v1/warehouses/', {'page_size': 100}, **self._h())
        self.assertIn(
            (str(self.depot_b.id), False),
            [(w['id'], w['is_active']) for w in liste.data['results']],
        )

    def test_on_ne_supprime_pas_un_entrepot_qui_porte_du_stock(self):
        OrganizationMembership.objects.get(user=self.caissier_b).assigned_warehouses.set(
            [self.depot_a]
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.depot_b,
            quantity=Decimal('4.000'),
        )
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.delete(f'/api/v1/warehouses/{self.depot_b.id}/', **self._h())
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'stock_restant')
        self.assertTrue(Warehouse.objects.filter(id=self.depot_b.id).exists())


class EntrepotFermeTests(_Entrepots):
    """Un entrepôt désactivé est fermé à toute opération, et ne s'affecte plus."""

    def setUp(self):
        super().setUp()
        Warehouse.objects.filter(id=self.depot_b.id).update(is_active=False)

    def test_on_n_y_saisit_pas_de_mouvement(self):
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.produit.id), 'warehouse': str(self.depot_b.id),
                'movement_type': 'purchase', 'quantity': '1', 'unit_cost': '100',
            },
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'entrepot_inactif')

    def test_on_ne_l_affecte_pas(self):
        cible = OrganizationMembership.objects.get(user=self.cashier_a)
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.patch(
            f'/api/v1/memberships/{cible.id}/', {'warehouse_ids': [str(self.depot_b.id)]},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(self._code(reponse), 'entrepot_inactif')

    def test_une_affectation_en_place_ne_bloque_pas_la_fiche(self):
        """Le caissier de B garde son entrepôt fermé ; on enregistre son rôle."""
        cible = OrganizationMembership.objects.get(user=self.caissier_b)
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.patch(
            f'/api/v1/memberships/{cible.id}/',
            {'warehouse_ids': [str(self.depot_b.id)], 'is_active': True},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_un_ajustement_ne_s_y_applique_pas_mais_se_rejette(self):
        ajustement = StockAdjustment.objects.create(
            organization=self.org, warehouse=self.depot_b, reference='ADJ-B',
            reason='count', created_by=self.owner,
        )
        self.client.force_authenticate(user=self.owner)
        refus = self.client.post(
            f'/api/v1/stock-adjustments/{ajustement.id}/approve/', {}, format='json', **self._h(),
        )
        self.assertEqual(refus.status_code, status.HTTP_400_BAD_REQUEST, refus.data)
        self.assertEqual(self._code(refus), 'entrepot_inactif')
        rejet = self.client.post(
            f'/api/v1/stock-adjustments/{ajustement.id}/reject/', {}, format='json', **self._h(),
        )
        self.assertEqual(rejet.status_code, status.HTTP_200_OK, rejet.data)


class AffectationsDuGerantTests(_Entrepots):

    def test_le_gerant_n_affecte_pas_dans_l_entrepot_d_un_autre(self):
        cible = OrganizationMembership.objects.get(user=self.cashier_a)
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.patch(
            f'/api/v1/memberships/{cible.id}/', {'warehouse_ids': [str(self.depot_b.id)]},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        cible.refresh_from_db()
        self.assertEqual(list(cible.assigned_warehouses.values_list('id', flat=True)), [self.depot_a.id])

    def test_le_gerant_ne_voit_pas_l_equipe_des_autres_boutiques(self):
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.get('/api/v1/memberships/', {'page_size': 100}, **self._h())
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        emails = {m['user_email'] for m in reponse.data['results']}
        self.assertNotIn('cb@e.test', emails)
        self.assertIn('owner@vf.test', emails)

    def test_le_gerant_organise_les_emplacements_de_son_entrepot(self):
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.post(
            '/api/v1/stock-locations/',
            {'warehouse': str(self.depot_a.id), 'name': 'Allée 1', 'code': 'A1'},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)


class AnnuaireTests(_Entrepots):

    def test_l_annuaire_nomme_tous_les_entrepots_sans_rien_d_autre(self):
        magasinier = make_user('mag@e.test', 'Mag', 'A')
        m = OrganizationMembership.objects.create(
            user=magasinier, organization=self.org, role=R.STOCK_KEEPER, is_active=True,
        )
        m.assigned_warehouses.set([self.depot_a])
        self.client.force_authenticate(user=magasinier)
        reponse = self.client.get('/api/v1/warehouses/directory/', **self._h())
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(
            {e['name'] for e in reponse.data}, {self.depot_a.name, 'Boutique B'},
        )
        for entree in reponse.data:
            self.assertEqual(set(entree), {'id', 'name', 'is_active'})

    def test_le_reveil_du_terminal_porte_l_annuaire(self):
        from apps.users.devices import build_session_payload

        charge = build_session_payload(self.cashier_a, self.org)
        self.assertEqual(
            {e['name'] for e in charge['warehouse_directory']}, {self.depot_a.name, 'Boutique B'},
        )


class CommandeDeRepriseTests(_Entrepots):

    def _orphelin(self):
        orphelin = make_user('orph@e.test', 'Orphelin', 'X')
        m = OrganizationMembership.objects.create(
            user=orphelin, organization=self.org, role=R.MANAGER, is_active=True,
        )
        m.assigned_warehouses.set([])
        return m

    def test_la_simulation_n_ecrit_rien(self):
        m = self._orphelin()
        call_command('reparer_membres_sans_entrepot', stdout=open('/dev/null', 'w'))
        self.assertFalse(m.assigned_warehouses.exists())

    def test_appliquer_donne_le_principal(self):
        m = self._orphelin()
        call_command('reparer_membres_sans_entrepot', '--appliquer', stdout=open('/dev/null', 'w'))
        self.assertEqual(list(m.assigned_warehouses.values_list('id', flat=True)), [self.depot_a.id])

    def test_un_membre_aux_seuls_entrepots_fermes_n_est_pas_reaffecte(self):
        Warehouse.objects.filter(id=self.depot_b.id).update(is_active=False)
        call_command('reparer_membres_sans_entrepot', '--appliquer', stdout=open('/dev/null', 'w'))
        caissier = OrganizationMembership.objects.get(user=self.caissier_b)
        self.assertEqual(
            list(caissier.assigned_warehouses.values_list('id', flat=True)), [self.depot_b.id],
        )


class UnSeulPrincipalMigrationTests(MigrationDataTestCase):
    migrate_from = ('inventory', '0019_horloge_metier')
    migrate_to = ('inventory', '0020_un_seul_principal')

    def preparer(self, apps):
        Organization = apps.get_model('organizations', 'Organization')
        Warehouse = apps.get_model('inventory', 'Warehouse')
        deux = Organization.objects.create(name='Deux', slug='deux')
        aucun = Organization.objects.create(name='Aucun', slug='aucun')
        Warehouse.objects.create(organization=deux, name='Ancien', code='A', is_default=True)
        Warehouse.objects.create(organization=deux, name='Second', code='B', is_default=True)
        Warehouse.objects.create(
            organization=aucun, name='Fermé', code='F', is_default=True, is_active=False,
        )
        Warehouse.objects.create(organization=aucun, name='Ouvert', code='O')

    def test_il_n_en_reste_qu_un_et_il_est_actif(self):
        Warehouse = self.apps_apres.get_model('inventory', 'Warehouse')
        self.assertEqual(
            list(Warehouse.objects.filter(organization__slug='deux', is_default=True)
                 .values_list('name', flat=True)),
            ['Ancien'],
        )
        self.assertEqual(
            list(Warehouse.objects.filter(organization__slug='aucun', is_default=True)
                 .values_list('name', flat=True)),
            ['Ouvert'],
        )
