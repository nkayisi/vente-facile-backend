"""
Parité du LIVRE DE CAISSE entre le back-office et le terminal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CES DEUX ACTES N'ONT JAMAIS ÉTÉ COUVERTS, ET C'EST POUR CELA QU'ILS SONT     │
│ CASSÉS.                                                                      │
│                                                                              │
│ `expense.create` et `cash_movement.create` appellent `serializer.save()` en  │
│ direct et ne rejouent JAMAIS le `perform_create` de leur vue. Or c'est lui   │
│ qui pose `organization` (une FK NON NULLE sans défaut), la référence, la     │
│ devise résolue, le solde du tiroir PAR DEVISE, la session ouverte et         │
│ `created_by`. Aucun signal ne le remplace.                                   │
│                                                                              │
│ Ce fichier est le premier geste du chantier de parité : il établit le mode   │
│ d'échec EXACT avant tout correctif, parce qu'une dépense saisie au comptoir  │
│ depuis le lot 9 n'est peut-être jamais arrivée nulle part.                   │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`.** Règle posée par `test_pull_scope_resolves`
et confirmée à ses dépens par le lot 11 : `accessible_warehouse_ids` sort en
amont pour un propriétaire, si bien que la moitié du code de périmètre n'est
jamais exécutée. Le caissier est ici le bon rôle : c'est lui qui tient le
tiroir, et c'est sa session que `get_open_session_for_user` doit retrouver.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement, Expense, ExpenseCategory
from apps.sales.models import RegisterSession
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency

OPERATIONS = '/api/v1/sync/operations/'


class _CaisseBaseTest(APITestCase):
    """Une organisation à DEUX devises : c'est la seule façon de voir un taux."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        cdf = Currency.objects.get(code='CDF')
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=cdf, is_primary=True,
            exchange_rate=Decimal('1.000000'), is_active=True,
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=False,
            exchange_rate=Decimal('2800.000000'), is_active=True,
        )

        self.categorie = ExpenseCategory.objects.create(
            organization=self.org, name='Transport', code='TRA',
        )
        # La session du CAISSIER : `get_open_session_for_user` la cherche par
        # `opened_by`, et c'est elle qui doit se retrouver sur le mouvement.
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open', opened_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, kind, payload, op_id):
        """Le chemin du TERMINAL."""
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-08-31T09:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(
            verdict['verdict'], attendu,
            f"verdict={verdict['verdict']} errors={verdict.get('errors')} "
            f"detail={verdict.get('detail')}",
        )
        return verdict


class DepenseParityTests(_CaisseBaseTest):
    """Une dépense saisie au comptoir doit valoir celle saisie au back-office."""

    def _payload_terminal(self):
        """Le payload de `features/caisse/actes.ts::creerDepense`, CORRIGÉ.

        `expense_date` s'y ajoute : le modèle l'exige, sans défaut, et le
        back-office l'envoie (`cashbook/expenses/page.tsx:146`). Le terminal ne
        l'envoyait pas, et toutes ses dépenses étaient refusées.
        """
        return {
            'id': '11111111-1111-4111-8111-111111111111',
            'category': str(self.categorie.id),
            'description': 'Taxi pour la banque',
            'amount': '12.50',
            'currency': 'USD',
            'beneficiary': 'Chauffeur',
            'expense_date': '2026-08-31',
            'notes': '',
        }

    def test_le_terminal_peut_enregistrer_une_depense(self):
        """
        Le test qui porte tout le lot 1.

        S'il échoue, c'est que toute dépense saisie sur un terminal depuis le
        lot 9 part en quarantaine, et que le marchand ne l'a jamais su.
        """
        reponse = self._journal(
            'expense.create', self._payload_terminal(),
            '11111111-1111-4111-8111-111111111111',
        )
        self._verdict(reponse, 'applied')
        self.assertEqual(Expense.objects.count(), 1)

    def test_la_depense_du_journal_vaut_celle_de_la_vue(self):
        """
        Champ par champ. Ce que `perform_create` pose et que le handler oublie :
        `organization`, la référence, la devise RÉSOLUE, le taux, `created_by`.
        """
        vue = self.client.post(
            '/api/v1/expenses/',
            {
                'category': str(self.categorie.id),
                'description': 'Taxi pour la banque',
                'amount': '12.50',
                'currency': 'USD',
                'beneficiary': 'Chauffeur',
                'expense_date': '2026-08-31',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_201_CREATED, vue.data)
        # `ExpenseCreateSerializer` ne rend pas d'`id` : la dépense de la vue
        # est simplement la seule en base à ce stade.
        par_la_vue = Expense.objects.get()

        self._verdict(
            self._journal(
                'expense.create', self._payload_terminal(),
                '11111111-1111-4111-8111-111111111111',
            ),
            'applied',
        )
        par_le_journal = Expense.objects.exclude(id=par_la_vue.id).get()

        self.assertEqual(par_le_journal.organization_id, self.org.id)
        self.assertTrue(par_le_journal.reference, "Référence vide : `ReferenceGenerator` n'a pas tourné.")
        self.assertEqual(par_le_journal.currency, par_la_vue.currency)
        # Le taux est le point subtil : `exchange_rate` a un défaut modèle de 1,
        # donc un handler qui ne résout rien enregistre une dépense en dollars
        # au taux 1, et le rapport comptable la compte pour 12,50 francs.
        self.assertEqual(par_le_journal.exchange_rate, par_la_vue.exchange_rate)
        self.assertEqual(par_le_journal.exchange_rate, Decimal('2800.000000'))
        self.assertEqual(par_le_journal.created_by_id, self.cashier_a.id)


class MouvementCaisseParityTests(_CaisseBaseTest):
    """Un mouvement de tiroir saisi au comptoir doit valoir celui du back-office."""

    def _payload_terminal(self, op_id):
        """Le payload de `creerMouvementCaisse`, CORRIGÉ sur deux points.

        `movement_type` valait « income » / « expense », qui ne sont PAS des
        choix du modèle : les valeurs sont `fund_in`, `fund_out`, `other_in`,
        `other_out`… Le back-office envoie `other_in` par défaut
        (`cashbook/page.tsx:175`). Et `movement_date` manquait, alors que le
        modèle l'exige et que le web l'envoie (`:178`).
        """
        return {
            'id': op_id,
            'direction': 'in',
            'movement_type': 'other_in',
            'amount': '5000',
            'currency': 'CDF',
            'description': 'Apport du gérant',
            'movement_date': '2026-08-31T09:00:00Z',
            'session': str(self.session.id),
        }

    def test_le_terminal_peut_enregistrer_un_mouvement(self):
        op = '22222222-2222-4222-8222-222222222222'
        self._verdict(
            self._journal('cash_movement.create', self._payload_terminal(op), op),
            'applied',
        )
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_le_mouvement_du_journal_vaut_celui_de_la_vue(self):
        vue = self.client.post(
            '/api/v1/cash-movements/',
            {
                'direction': 'in', 'movement_type': 'other_in',
                'amount': '5000', 'currency': 'CDF',
                'description': 'Apport du gérant',
                'movement_date': '2026-08-31T09:00:00Z',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_201_CREATED, vue.data)
        par_la_vue = CashMovement.objects.get()

        op = '22222222-2222-4222-8222-222222222222'
        self._verdict(
            self._journal('cash_movement.create', self._payload_terminal(op), op),
            'applied',
        )
        par_le_journal = CashMovement.objects.exclude(id=par_la_vue.id).get()

        self.assertEqual(par_le_journal.organization_id, self.org.id)
        self.assertTrue(par_le_journal.reference, "Référence vide.")
        self.assertEqual(par_le_journal.currency, par_la_vue.currency)
        # `balance_after` est le solde du TIROIR, par devise. Un handler qui ne
        # le calcule pas laisse le livre de caisse à zéro pour toujours.
        self.assertIsNotNone(par_le_journal.balance_after)
        self.assertEqual(par_le_journal.balance_after, Decimal('10000.00'))
        # La session rattache le mouvement à la caisse, donc au périmètre
        # entrepôt. Sans elle, il est invisible aux magasiniers.
        self.assertEqual(par_le_journal.session_id, self.session.id)
        self.assertEqual(par_le_journal.created_by_id, self.cashier_a.id)


class CategorieDepuisLeComptoirTests(_CaisseBaseTest):
    """
    Les deux actes de catégorie, et l'opération qui BOUCLAIT.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ CE TEST EST LE SEUL QUI PROUVE QUE LE DÉFAUT EST REFERMÉ.               │
    │                                                                          │
    │ Les handlers importaient `IncomeCategorySerializer` et                   │
    │ `ExpenseCategorySerializer` : deux noms qui n'existent pas. À            │
    │ l'exécution, `ImportError` - que `_classify` rangeait en `retry`.        │
    │ L'opération repartait à CHAQUE synchronisation, indéfiniment, sur le     │
    │ terminal d'un marchand. Aucun test n'entrait dans ces corps, et la       │
    │ suite était verte.                                                       │
    │                                                                          │
    │ Le contrôle qui compte n'est donc pas « la catégorie existe », c'est     │
    │ « le verdict est `applied` et surtout PAS `retry` ».                     │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ Le caissier n'a PAS `cashbook.manage_categories` : c'est le gérant qui
    crée une catégorie, sur les deux surfaces.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.manager)

    def test_une_categorie_de_depense_est_APPLIQUEE_et_jamais_reessayee(self):
        from apps.cashbook.models import ExpenseCategory

        op = '33333333-3333-4333-8333-333333333333'
        reponse = self._journal('expense_category.create', {
            'id': op, 'name': 'Carburant', 'description': 'Gasoil et essence',
            'color': '#10B981', 'is_active': True,
        }, op)
        verdict = self._verdict(reponse)

        categorie = ExpenseCategory.objects.get(id=op)
        self.assertEqual(categorie.name, 'Carburant')
        self.assertEqual(categorie.description, 'Gasoil et essence')
        self.assertEqual(categorie.color, '#10B981')
        self.assertEqual(categorie.organization, self.org)
        # La réponse passe par le serializer de DÉTAIL : sans `id`, le terminal
        # ne saurait pas rattacher la catégorie qu'il vient de créer.
        self.assertIn('id', verdict['authoritative'])

    def test_un_type_d_entree_est_APPLIQUE_et_jamais_reessaye(self):
        from apps.cashbook.models import IncomeCategory

        op = '44444444-4444-4444-8444-444444444444'
        reponse = self._journal('income_category.create', {
            'id': op, 'name': 'Subvention', 'is_active': True,
        }, op)
        self._verdict(reponse)
        self.assertEqual(IncomeCategory.objects.get(id=op).name, 'Subvention')

    def test_un_nom_deja_pris_est_REFUSE_avec_un_message_de_champ(self):
        """Et non un `IntegrityError` brut, illisible en quarantaine."""
        op = '55555555-5555-4555-8555-555555555555'
        reponse = self._journal('expense_category.create', {
            'id': op, 'name': 'transport',  # `Transport` existe déjà
        }, op)
        verdict = self._verdict(reponse, attendu='rejected')
        self.assertIn('name', str(verdict.get('errors')))

    def test_un_caissier_est_BLOQUE_et_son_opération_est_conservee(self):
        """
        `manage_categories` n'est pas dans ses droits. Bloqué n'est pas refusé :
        l'opération repartira seule le jour où le gérant accorde le droit.
        """
        self.client.force_authenticate(user=self.cashier_a)
        op = '66666666-6666-4666-8666-666666666666'
        reponse = self._journal('expense_category.create', {
            'id': op, 'name': 'Fournitures',
        }, op)
        self._verdict(reponse, attendu='blocked')


class NumeroDAppareilTests(_CaisseBaseTest):
    """
    Le numéro imprimé au comptoir est celui que le serveur enregistre.

    Une dépense se règle souvent avant que le réseau ne revienne, et le
    bénéficiaire repart avec sa pièce justificative - qui porte une ligne de
    signature. Un numéro remplacé ensuite par celui du serveur rendrait ce
    papier muet. C'est le défaut corrigé sur `sale.add_payment` au lot 6.
    """

    def test_la_reference_du_terminal_est_reprise_VERBATIM(self):
        op = '77777777-7777-4777-8777-777777777777'
        numero = 'DEP-20260910-K7QM-0042'
        reponse = self._journal('expense.create', {
            'id': op, 'reference': numero,
            'category': str(self.categorie.id),
            'description': 'Carburant', 'amount': '30.00', 'currency': 'USD',
            'expense_date': '2026-09-10',
        }, op)
        self._verdict(reponse)
        self.assertEqual(Expense.objects.get(id=op).reference, numero)

    def test_sans_reference_le_serveur_alloue_la_sienne(self):
        """C'est le chemin d'une dépense saisie au back-office."""
        op = '88888888-8888-4888-8888-888888888888'
        reponse = self._journal('expense.create', {
            'id': op,
            'category': str(self.categorie.id),
            'description': 'Loyer', 'amount': '100.00', 'currency': 'USD',
            'expense_date': '2026-09-10',
        }, op)
        self._verdict(reponse)
        self.assertRegex(Expense.objects.get(id=op).reference, r'^DEP-\d{8}-\d{4}$')


class DefautDeCodeTests(_CaisseBaseTest):
    """
    Un symbole qui n'existe pas ne se répare pas en réessayant.

    Sans ce classement, un `ImportError` tombait dans le repli `unexpected`,
    donc en `retry` : l'opération repartait à chaque synchronisation, en
    batterie et en données, pour un acte qui ne passera jamais.
    """

    def test_un_ImportError_de_handler_rend_REJECTED_jamais_RETRY(self):
        from apps.sync.operations import _classify
        from apps.sync.models import SyncOperation

        verdict, corps = _classify(ImportError("cannot import name 'Fantome'"))
        self.assertEqual(verdict, SyncOperation.Verdict.REJECTED)
        self.assertEqual(corps['code'], 'handler_defect')

    def test_une_panne_de_base_reste_RETRY(self):
        """La frontière ne bouge pas : un aléa technique se réessaie."""
        from django.db import OperationalError
        from apps.sync.operations import _classify
        from apps.sync.models import SyncOperation

        verdict, _ = _classify(OperationalError('deadlock detected'))
        self.assertEqual(verdict, SyncOperation.Verdict.RETRY)
