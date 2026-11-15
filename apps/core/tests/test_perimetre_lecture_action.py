"""
Lire et agir ne relèvent pas du même périmètre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LA MATRICE DES DÉCISIONS DU 2026-11-14, ÉPROUVÉE PAR L'API.                 │
│                                                                              │
│ - Isolation par défaut : un membre non propriétaire lit et agit dans ses    │
│   entrepôts affectés, nulle part ailleurs.                                   │
│ - « Consulter tous les entrepôts » (case du propriétaire) : LIRE partout,   │
│   AGIR chez soi. Une pièce seulement lisible répond `consultation_seule`.   │
│ - « Voir tout » (`sales.view_all`) : le travail des collègues, DANS SES     │
│   entrepôts, jamais au-delà.                                                 │
│ - Une session de caisse se clôture par tout membre de l'entrepôt, mais on   │
│   ne VEND que dans la sienne, et une personne n'en a qu'une d'ouverte.      │
│ - Un transfert part de SES entrepôts vers n'importe quel entrepôt actif ;   │
│   la source approuve et expédie, la destination reçoit.                     │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ AUCUN TEST N'EST ÉCRIT DEPUIS LE PROPRIÉTAIRE, sauf pour dire ce qu'il voit :
il sort en amont de toute la logique, et un test écrit depuis lui serait vert
sans rien exécuter de ce qu'il prétend couvrir.
"""
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import Expense, ExpenseCategory
from apps.inventory.models import StockTransfer, StockTransferItem, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.models import Quotation, Register, RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users, make_user

R = OrganizationMembership.Role
OPERATIONS = '/api/v1/sync/operations/'


class _Perimetre(APITestCase):
    """
    Deux entrepôts : A (principal) et B. Dans A, deux caissiers, un gérant, un
    magasinier, un caissier « Voir tout » et un gérant « Consulter tous ».
    Dans B, un caissier et un magasinier.
    """

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
        self.caisse_a2 = Register.objects.create(
            organization=self.org, branch=self.branch, warehouse=self.depot_a,
            name='Caisse A2', code='CA2',
        )
        self.payment_method = make_cash_payment_method(self.org)

        def membre(email, role, depots, **options):
            utilisateur = make_user(email, email.split('@')[0], 'T')
            m = OrganizationMembership.objects.create(
                user=utilisateur, organization=self.org, role=role, is_active=True, **options,
            )
            # ⚠ `set()` : un signal donne le principal à tout membre borné né
            #   sans affectation ; `add()` laisserait un membre de B dans A aussi.
            m.assigned_warehouses.set(depots)
            return utilisateur

        self.caissier_b = membre('cb@p.test', R.CASHIER, [self.depot_b])
        self.magasinier_a = membre('ma@p.test', R.STOCK_KEEPER, [self.depot_a])
        self.magasinier_b = membre('mb@p.test', R.STOCK_KEEPER, [self.depot_b])
        self.voit_tout = membre(
            'vt@p.test', R.CASHIER, [self.depot_a], extra_permissions=['sales.view_all'],
        )
        self.consulte_tout = membre(
            'ct@p.test', R.MANAGER, [self.depot_a], consult_all_warehouses=True,
        )

        def vente(reference, vendeur, depot):
            return Sale.objects.create(
                organization=self.org, reference=reference, warehouse=depot,
                sold_by=vendeur, total=Decimal('1000'), amount_paid=Decimal('1000'),
                status='completed', is_pos=True,
            )

        self.vente_a1 = vente('VT-A1', self.cashier_a, self.depot_a)
        self.vente_a2 = vente('VT-A2', self.cashier_b, self.depot_a)
        self.vente_b1 = vente('VT-B1', self.caissier_b, self.depot_b)

    def _h(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _references(self, utilisateur, chemin):
        self.client.force_authenticate(user=utilisateur)
        reponse = self.client.get(chemin, {'page_size': 100}, **self._h())
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        return {ligne['reference'] for ligne in reponse.data['results']}


class MatriceDesVentesTests(_Perimetre):

    def test_chaque_membre_lit_ce_que_la_matrice_lui_donne(self):
        attendus = {
            'proprietaire': (self.owner, {'VT-A1', 'VT-A2', 'VT-B1'}),
            'gerant de A': (self.manager, {'VT-A1', 'VT-A2'}),
            'caissier de A': (self.cashier_a, {'VT-A1'}),
            'caissier « Voir tout »': (self.voit_tout, {'VT-A1', 'VT-A2'}),
            'gerant « Consulter tous »': (self.consulte_tout, {'VT-A1', 'VT-A2', 'VT-B1'}),
            'caissier de B': (self.caissier_b, {'VT-B1'}),
        }
        for nom, (utilisateur, attendu) in attendus.items():
            with self.subTest(nom):
                self.assertEqual(self._references(utilisateur, '/api/v1/sales/'), attendu)

    def test_voir_tout_ne_franchit_pas_ses_entrepots(self):
        """La case ouvre les COLLÈGUES, jamais la boutique d'en face."""
        self.assertNotIn('VT-B1', self._references(self.voit_tout, '/api/v1/sales/'))


class ConsultationSeuleTests(_Perimetre):

    def test_il_lit_la_vente_de_l_autre_boutique(self):
        self.client.force_authenticate(user=self.consulte_tout)
        reponse = self.client.get(f'/api/v1/sales/{self.vente_b1.id}/', **self._h())
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)

    def test_il_ne_la_touche_pas_et_le_refus_le_dit(self):
        """⚠ 403 `consultation_seule`, jamais « introuvable » : il l'a sous les yeux."""
        self.client.force_authenticate(user=self.consulte_tout)
        reponse = self.client.patch(
            f'/api/v1/sales/{self.vente_b1.id}/', {'notes': 'x'}, format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, reponse.data)
        self.assertEqual(reponse.data.get('code'), 'consultation_seule')

    def test_il_touche_ce_qui_est_chez_lui(self):
        self.client.force_authenticate(user=self.consulte_tout)
        reponse = self.client.patch(
            f'/api/v1/sales/{self.vente_a1.id}/', {'notes': 'ok'}, format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_il_n_ouvre_pas_de_caisse_dans_l_autre_boutique(self):
        self.client.force_authenticate(user=self.consulte_tout)
        reponse = self.client.post(
            '/api/v1/register-sessions/open/', {'register': str(self.caisse_b.id)},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND, reponse.data)


class DepensesParAuteurTests(_Perimetre):

    def setUp(self):
        super().setUp()
        categorie = ExpenseCategory.objects.create(organization=self.org, name='Divers')

        def depense(reference, auteur, depot):
            Expense.objects.create(
                organization=self.org, reference=reference, category=categorie,
                warehouse=depot, description=reference, amount=Decimal('10'),
                expense_date=date(2026, 11, 14), created_by=auteur,
            )

        depense('DEP-A1', self.cashier_a, self.depot_a)
        depense('DEP-A2', self.cashier_b, self.depot_a)
        depense('DEP-B1', self.caissier_b, self.depot_b)

    def test_le_caissier_ne_voit_que_les_siennes(self):
        self.assertEqual(self._references(self.cashier_a, '/api/v1/expenses/'), {'DEP-A1'})

    def test_voir_tout_ouvre_ses_collegues_de_sa_boutique(self):
        """La règle d'auteur suit la PERMISSION : elle bornait la caisse par le RÔLE."""
        self.assertEqual(
            self._references(self.voit_tout, '/api/v1/expenses/'), {'DEP-A1', 'DEP-A2'},
        )


class DevisParEntrepotTests(_Perimetre):

    def test_un_gerant_ne_lit_que_les_devis_de_ses_entrepots(self):
        """Sans entrepôt, un gérant lisait les devis de toute l'organisation."""
        for reference, depot in (('DEV-A', self.depot_a), ('DEV-B', self.depot_b), ('DEV-0', None)):
            Quotation.objects.create(
                organization=self.org, reference=reference, warehouse=depot,
                created_by=self.owner, valid_until=date(2026, 12, 31),
            )
        # Les devis anciens, sans entrepôt, restent visibles (`include_null`).
        self.assertEqual(
            self._references(self.manager, '/api/v1/quotations/'), {'DEV-A', 'DEV-0'},
        )


class SessionDeCaisseTests(_Perimetre):

    def _vendre(self, caisse):
        produit = Product.objects.create(
            organization=self.org, name='Savon', slug='savon', sku='SAV',
            cost_price=Decimal('100'), selling_price=Decimal('200'),
            tax_rate=Decimal('0'), is_taxable=False, track_inventory=False,
        )
        return self.client.post(
            '/api/v1/sales/',
            {
                'register': str(caisse.id), 'is_pos': True,
                'items': [{'product': str(produit.id), 'quantity': '1', 'unit_price': '200'}],
                'payments': [{
                    'payment_method': str(self.payment_method.id), 'amount': '200',
                }],
            },
            format='json', **self._h(),
        )

    def test_un_gerant_ne_vend_pas_dans_la_session_d_un_caissier(self):
        """
        ⚠ Le gérant le pouvait : ses ventes entraient dans un tiroir qu'il ne
        comptait pas, et le Z du caissier annonçait un excédent.
        """
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.manager)
        reponse = self._vendre(self.register)
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        # ⚠ Le code au PREMIER niveau : DRF jette le code d'une erreur de champ
        #   au rendu JSON, et le client n'aurait rien sur quoi réagir.
        self.assertEqual(reponse.json()['code'], 'session_d_un_autre')

    def test_l_ouvreur_vend_dans_sa_session(self):
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self._vendre(self.register)
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)

    def test_un_autre_membre_cloture_la_session(self):
        """Elle ne s'isole pas : un caissier parti ne bloque pas la fermeture."""
        session = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_b)
        reponse = self.client.post(
            f'/api/v1/register-sessions/{session.id}/close/', {}, format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_une_personne_n_a_qu_une_session_ouverte(self):
        self.client.force_authenticate(user=self.cashier_a)
        premiere = self.client.post(
            '/api/v1/register-sessions/open/', {'register': str(self.register.id)},
            format='json', **self._h(),
        )
        self.assertEqual(premiere.status_code, status.HTTP_201_CREATED, premiere.data)
        seconde = self.client.post(
            '/api/v1/register-sessions/open/', {'register': str(self.caisse_a2.id)},
            format='json', **self._h(),
        )
        self.assertEqual(seconde.status_code, status.HTTP_400_BAD_REQUEST, seconde.data)
        self.assertEqual(seconde.data['code'], 'session_personnelle_ouverte')
        self.assertIn('Caisse 1', seconde.data['error'])

    def test_la_base_l_interdit_aussi(self):
        """Le filet d'une course : deux ouvertures simultanées sur deux caisses."""
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            RegisterSession.objects.create(
                organization=self.org, register=self.caisse_a2, opened_by=self.cashier_a,
                opening_balance=Decimal('0'), status='open',
            )

    def test_le_journal_refuse_une_seconde_session(self):
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': '4f0c8e1e-6a7d-4a35-9d1b-9b7a1f0e0a01',
                'kind': 'register_session.open', 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-11-14T09:00:00Z',
                'payload': {'register': str(self.caisse_a2.id)},
            }]},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'session_personnelle_ouverte')


class TransfertsTests(_Perimetre):

    def setUp(self):
        super().setUp()
        self.produit = Product.objects.create(
            organization=self.org, name='Riz', slug='riz', sku='RIZ',
            cost_price=Decimal('100'), selling_price=Decimal('200'),
        )

    def _transfert(self, statut):
        transfert = StockTransfer.objects.create(
            organization=self.org, reference=f'TR-{statut}', status=statut,
            source_warehouse=self.depot_a, destination_warehouse=self.depot_b,
            requested_by=self.magasinier_a,
        )
        StockTransferItem.objects.create(
            organization=self.org, transfer=transfert, product=self.produit,
            quantity_requested=Decimal('1'),
        )
        return transfert

    def test_on_expedie_vers_une_boutique_qui_ne_nous_est_pas_affectee(self):
        """Les deux côtés devaient être affectés : le dépôt central ne livrait personne."""
        self.client.force_authenticate(user=self.magasinier_a)
        reponse = self.client.post(
            '/api/v1/stock-transfers/',
            {
                'source_warehouse': str(self.depot_a.id),
                'destination_warehouse': str(self.depot_b.id),
                'items': [{'product': str(self.produit.id), 'quantity_requested': '1'}],
            },
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)

    def test_on_n_expedie_pas_depuis_la_boutique_d_un_autre(self):
        self.client.force_authenticate(user=self.magasinier_a)
        reponse = self.client.post(
            '/api/v1/stock-transfers/',
            {
                'source_warehouse': str(self.depot_b.id),
                'destination_warehouse': str(self.depot_a.id),
                'items': [{'product': str(self.produit.id), 'quantity_requested': '1'}],
            },
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)

    def test_la_destination_n_approuve_pas(self):
        transfert = self._transfert('draft')
        self.client.force_authenticate(user=self.magasinier_b)
        reponse = self.client.post(
            f'/api/v1/stock-transfers/{transfert.id}/approve/', {}, format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertIn('source_warehouse', reponse.data)

    def test_la_source_approuve(self):
        transfert = self._transfert('draft')
        self.client.force_authenticate(user=self.magasinier_a)
        reponse = self.client.post(
            f'/api/v1/stock-transfers/{transfert.id}/approve/', {}, format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_la_source_ne_recoit_pas_par_le_journal(self):
        """⚠ Le journal ne bornait que par organisation : un terminal recevait tout."""
        transfert = self._transfert('in_transit')
        self.client.force_authenticate(user=self.magasinier_a)
        reponse = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': '4f0c8e1e-6a7d-4a35-9d1b-9b7a1f0e0a02',
                'kind': 'stock_transfer.receive', 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-11-14T09:00:00Z',
                'payload': {'transfer': str(transfert.id)},
            }]},
            format='json', **self._h(),
        )
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        transfert.refresh_from_db()
        self.assertEqual(transfert.status, 'in_transit')

    def test_une_autre_boutique_ne_touche_pas_au_transfert_par_le_journal(self):
        """Un caissier de B n'a aucun côté d'un transfert entre A et C."""
        depot_c = Warehouse.objects.create(
            organization=self.org, branch=self.branch, name='C', code='WH-C',
        )
        transfert = StockTransfer.objects.create(
            organization=self.org, reference='TR-AC', status='draft',
            source_warehouse=self.depot_a, destination_warehouse=depot_c,
            requested_by=self.magasinier_a,
        )
        self.client.force_authenticate(user=self.magasinier_b)
        reponse = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': '4f0c8e1e-6a7d-4a35-9d1b-9b7a1f0e0a03',
                'kind': 'stock_transfer.approve', 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-11-14T09:00:00Z',
                'payload': {'transfer': str(transfert.id)},
            }]},
            format='json', **self._h(),
        )
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'not_found')


class CaseEtPermissionsTests(_Perimetre):

    def _membre(self, utilisateur):
        return OrganizationMembership.objects.get(user=utilisateur, organization=self.org)

    def test_le_gerant_ne_coche_pas_la_case(self):
        cible = self._membre(self.cashier_a)
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.patch(
            f'/api/v1/memberships/{cible.id}/', {'consult_all_warehouses': True},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, reponse.data)
        cible.refresh_from_db()
        self.assertFalse(cible.consult_all_warehouses)

    def test_le_proprietaire_la_coche(self):
        cible = self._membre(self.cashier_a)
        self.client.force_authenticate(user=self.owner)
        reponse = self.client.patch(
            f'/api/v1/memberships/{cible.id}/', {'consult_all_warehouses': True},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertTrue(reponse.data['consult_all_warehouses'])

    def test_on_n_accorde_pas_ce_qu_on_n_a_pas(self):
        cible = self._membre(self.cashier_a)
        self.client.force_authenticate(user=self.manager)
        for permission in ('warehouses.create', 'settings.manage'):
            with self.subTest(permission):
                reponse = self.client.patch(
                    f'/api/v1/memberships/{cible.id}/permissions/',
                    {'extra_permissions': [permission]}, format='json', **self._h(),
                )
                self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        cible.refresh_from_db()
        self.assertEqual(cible.extra_permissions, [])

    def test_le_gerant_accorde_ce_qu_il_a(self):
        cible = self._membre(self.cashier_a)
        self.client.force_authenticate(user=self.manager)
        reponse = self.client.patch(
            f'/api/v1/memberships/{cible.id}/permissions/',
            {'extra_permissions': ['sales.view_all']}, format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_les_droits_exposent_la_case_et_l_empreinte(self):
        self.client.force_authenticate(user=self.consulte_tout)
        reponse = self.client.get('/api/v1/users/me/permissions/', **self._h())
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertTrue(reponse.data['consult_all_warehouses'])
        self.assertTrue(reponse.data['perimetre_version'])
        self.assertEqual(
            reponse.data['assigned_warehouses'],
            [{'id': str(self.depot_a.id), 'name': self.depot_a.name, 'is_active': True}],
        )

    def test_l_empreinte_change_avec_la_case(self):
        self.client.force_authenticate(user=self.cashier_a)
        avant = self.client.get('/api/v1/users/me/permissions/', **self._h()).data
        membre = self._membre(self.cashier_a)
        membre.consult_all_warehouses = True
        membre.save(update_fields=['consult_all_warehouses'])
        apres = self.client.get('/api/v1/users/me/permissions/', **self._h()).data
        self.assertNotEqual(avant['perimetre_version'], apres['perimetre_version'])


class RevisionsDuTirageTests(_Perimetre):
    """
    ⚠ LES TABLES RÉVISÉES SONT CELLES DONT LA RÈGLE A CHANGÉ, ET ELLES SEULES.

    Une révision fait effacer puis re-tirer la table à tout le parc. En poser
    une de trop coûte un re-tirage pour rien ; en oublier une laisse sur les
    terminaux des lignes que la nouvelle règle ne leur donne plus - les
    dépenses des collègues d'un caissier, par exemple.
    """

    def test_exactement_les_tables_dont_la_regle_a_change(self):
        from apps.sync.pull import PULL_TABLES

        revisees = {t.name for t in PULL_TABLES if t.revision}
        self.assertEqual(revisees, {
            'register_sessions',  # le fonds par devise (lot L13)
            'expenses', 'cash_movements', 'sale_returns', 'quotations',
        })

    def test_la_case_change_le_jeton(self):
        """Cocher « consulter tous » élargit la lecture : le terminal retire."""
        from apps.sync.pull import PULL_TABLES_BY_NAME, scope_token

        membre = OrganizationMembership.objects.get(user=self.manager, organization=self.org)
        table = PULL_TABLES_BY_NAME['sales']
        avant = scope_token(table, membre)
        membre.consult_all_warehouses = True
        membre.save(update_fields=['consult_all_warehouses'])
        membre = OrganizationMembership.objects.get(pk=membre.pk)
        self.assertNotEqual(scope_token(table, membre), avant)

    def test_le_caissier_ne_tire_plus_les_depenses_de_ses_collegues(self):
        categorie = ExpenseCategory.objects.create(organization=self.org, name='Divers')
        for reference, auteur in (('DEP-1', self.cashier_a), ('DEP-2', self.cashier_b)):
            Expense.objects.create(
                organization=self.org, reference=reference, category=categorie,
                warehouse=self.depot_a, description=reference, amount=Decimal('10'),
                expense_date=date(2026, 11, 14), created_by=auteur,
            )
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.get(
            '/api/v1/sync/pull/', {'table': 'expenses', 'limit': 500}, **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual([r['reference'] for r in reponse.data['rows']], ['DEP-1'])


class CodeRemarquableTests(_Perimetre):

    def test_le_journal_garde_le_code_de_consultation_seule(self):
        """Une vente poussée dans un entrepôt seulement lisible : le code voyage."""
        self.client.force_authenticate(user=self.consulte_tout)
        produit = Product.objects.create(
            organization=self.org, name='Sel', slug='sel', sku='SEL',
            cost_price=Decimal('1'), selling_price=Decimal('2'), track_inventory=False,
        )
        reponse = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': '4f0c8e1e-6a7d-4a35-9d1b-9b7a1f0e0a04',
                'kind': 'sale.create', 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-11-14T09:00:00Z',
                'payload': {
                    'warehouse': str(self.depot_b.id), 'sale_type': 'retail', 'is_pos': False,
                    'items': [{'product': str(produit.id), 'quantity': '1', 'unit_price': '2'}],
                    'payments': [],
                },
            }]},
            format='json', **self._h(),
        )
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'consultation_seule')


class ReglementParLeJournalTests(_Perimetre):
    """
    `sale.add_payment` par le journal, borné comme `SaleViewSet.add_payment`.

    ⚠ AUCUN TEST NE L'APPLIQUAIT : le seul qui l'empruntait vérifiait un refus
    de dépendance. Une mutation qui cassait entièrement ce gestionnaire passait
    au vert sur toute la suite de synchronisation.
    """

    def setUp(self):
        from apps.contacts.models import Customer

        super().setUp()
        self.acheteur = Customer.objects.create(
            organization=self.org, code='C-REG', name='Client',
            credit_limit=Decimal('1000000'), current_balance=Decimal('0'),
        )
        produit = Product.objects.create(
            organization=self.org, name='Huile', slug='huile', sku='HUI',
            cost_price=Decimal('100'), selling_price=Decimal('500'),
            tax_rate=Decimal('0'), is_taxable=False, track_inventory=False,
        )
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id), 'is_pos': True,
                'sale_type': 'credit', 'customer': str(self.acheteur.id),
                'items': [{'product': str(produit.id), 'quantity': '2', 'unit_price': '500'}],
                'payments': [],
            },
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.vente = Sale.objects.get(pk=reponse.data['id'])

    def _regler(self, utilisateur, op):
        self.client.force_authenticate(user=utilisateur)
        reponse = self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op, 'kind': 'sale.add_payment', 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-11-14T10:00:00Z',
                'payload': {
                    'sale': str(self.vente.id),
                    'payment_method': str(self.payment_method.id),
                    'tendered_amount': '400',
                },
            }]},
            format='json', **self._h(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        return reponse.data['results'][0]

    def test_le_vendeur_encaisse_sa_dette(self):
        verdict = self._regler(self.cashier_a, '5a5a5a5a-0000-4000-8000-000000000001')
        self.assertEqual(verdict['verdict'], 'applied', verdict)
        self.vente.refresh_from_db()
        self.assertEqual(self.vente.amount_paid, Decimal('400.00'))

    def test_l_autre_boutique_ne_voit_pas_la_vente(self):
        verdict = self._regler(self.caissier_b, '5a5a5a5a-0000-4000-8000-000000000002')
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'not_found')

    def test_consulter_tous_ne_touche_pas_l_argent_d_une_autre_boutique(self):
        """Il LIT la vente de A depuis B ; il n'y encaisse rien."""
        self.vente.warehouse = self.depot_b
        self.vente.save(update_fields=['warehouse'])
        verdict = self._regler(self.consulte_tout, '5a5a5a5a-0000-4000-8000-000000000003')
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'consultation_seule')

    def test_une_dette_s_encaisse_meme_si_l_entrepot_est_desactive(self):
        """La SEULE exception de la règle « entrepôt inactif = fermé »."""
        Warehouse.objects.filter(id=self.depot_a.id).update(is_active=False)
        verdict = self._regler(self.cashier_a, '5a5a5a5a-0000-4000-8000-000000000004')
        self.assertEqual(verdict['verdict'], 'applied', verdict)
