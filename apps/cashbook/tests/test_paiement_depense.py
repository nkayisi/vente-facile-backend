"""
Payer une dépense : qui le peut, et de quel tiroir sort l'argent.

┌──────────────────────────────────────────────────────────────────────────┐
│ L'ARGENT SORT AU PAIEMENT, DU TIROIR DE CELUI QUI PAIE.                  │
│                                                                          │
│ Il sortait à l'APPROBATION, rattaché à l'approbateur - le plus souvent   │
│ un propriétaire sans tiroir ouvert. Quand le caissier payait ensuite,    │
│ rien ne sortait de SA caisse, et son Z annonçait un excédent égal à la   │
│ dépense. Ces tests épinglent le circuit retenu :                         │
│                                                                          │
│ - soumettre : l'auteur (caissier, gérant) ;                              │
│ - approuver : propriétaire ou gérant, JAMAIS sur sa propre dépense ;     │
│ - payer : l'auteur, ou qui peut approuver ; en espèces, il faut une      │
│   caisse ouverte, et la sortie entre dans le Z du payeur.                │
└──────────────────────────────────────────────────────────────────────────┘

Rôles BORNÉS : caissier et gérant, jamais seulement le propriétaire.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement, Expense, ExpenseCategory
from apps.cashbook.services import create_cash_movement_for_expense
from apps.notifications.models import Notification
from apps.organizations.models import OrganizationMembership
from apps.sales.models import PaymentMethod, RegisterSession
from apps.sales.register_sessions import close_register_session
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency
from apps.settings.services import CurrencyService

OPERATIONS = '/api/v1/sync/operations/'


class _Base(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        cdf = Currency.objects.get(code='CDF')
        OrganizationCurrency.objects.get_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1'), 'is_active': True},
        )
        CurrencyService.invalidate_cache(self.org)
        self.categorie = ExpenseCategory.objects.create(
            organization=self.org, name='Transport', code='TRA',
        )
        self.especes = make_cash_payment_method(self.org)
        self.mobile = PaymentMethod.objects.create(
            organization=self.org, name='M-Pesa', code='MPESA',
            method_type='mobile_money', is_active=True,
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _ouvrir_caisse(self, user, fonds='50000'):
        return RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=user,
            opening_balance=Decimal(fonds), status='open', opened_at=timezone.now(),
        )

    def _depense(self, auteur, montant='2000', statut='draft'):
        """Une dépense saisie par `auteur`, au back-office, puis amenée à `statut`."""
        self.client.force_authenticate(user=auteur)
        reponse = self.client.post('/api/v1/expenses/', {
            'category': str(self.categorie.id),
            'description': 'Carburant groupe',
            'amount': montant,
            'expense_date': '2026-11-20',
            'warehouse': str(self.warehouse.id),
        }, format='json', **self._headers())
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        depense = Expense.objects.get(id=reponse.data['id'])
        if statut in ('pending', 'approved', 'rejected'):
            depense.status = 'pending'
            depense.save(update_fields=['status', 'updated_at'])
        if statut in ('approved', 'rejected'):
            depense.status = statut
            depense.approved_by = self.owner if statut == 'approved' else None
            depense.save(update_fields=['status', 'approved_by', 'updated_at'])
        return depense

    def _agir(self, user, depense, geste, **corps):
        self.client.force_authenticate(user=user)
        return self.client.post(
            f'/api/v1/expenses/{depense.id}/{geste}/', corps,
            format='json', **self._headers(),
        )

    def _journal(self, user, kind, payload, op_id, quand='2026-11-20T09:00:00Z'):
        self.client.force_authenticate(user=user)
        reponse = self.client.post(OPERATIONS, {'operations': [{
            'operation_id': op_id, 'kind': kind, 'seq': 1, 'depends_on': [],
            'occurred_at': quand, 'payload': payload,
        }]}, format='json', **self._headers())
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        return reponse.data['results'][0]


class CircuitDuCaissierTests(_Base):
    """Le caissier soumet, le propriétaire approuve, le caissier paie."""

    def test_l_approbation_ne_sort_PAS_l_argent(self):
        depense = self._depense(self.cashier_a, statut='pending')
        reponse = self._agir(self.owner, depense, 'approve')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        depense.refresh_from_db()
        self.assertEqual(depense.status, 'approved')
        self.assertFalse(
            CashMovement.objects.filter(expense=depense).exists(),
            "L'approbation a fait sortir l'argent : c'est le paiement qui doit le faire.",
        )

    def test_l_auteur_est_invite_a_payer(self):
        depense = self._depense(self.cashier_a, statut='pending')
        self._agir(self.owner, depense, 'approve')
        avis = Notification.objects.filter(user=self.cashier_a).order_by('-created_at').first()
        self.assertIsNotNone(avis)
        self.assertIn('payer', avis.message)

    def test_le_caissier_paie_en_especes_DEPUIS_SA_CAISSE(self):
        session = self._ouvrir_caisse(self.cashier_a, fonds='50000')
        depense = self._depense(self.cashier_a, montant='2000', statut='approved')

        reponse = self._agir(self.cashier_a, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        depense.refresh_from_db()
        self.assertEqual(depense.status, 'paid')
        mouvement = CashMovement.objects.get(expense=depense)
        self.assertEqual(mouvement.session_id, session.id)
        self.assertEqual(mouvement.created_by_id, self.cashier_a.id)
        self.assertEqual(mouvement.direction, 'out')

        # Le Z du caissier tombe juste : 50 000 de fonds, 2 000 sortis.
        close_register_session(session, self.cashier_a, {'counted_balance': '48000'})
        session.refresh_from_db()
        self.assertEqual(session.expected_balance, Decimal('48000.00'))
        self.assertEqual(session.difference, Decimal('0.00'))

    def test_en_especes_SANS_CAISSE_OUVERTE_le_paiement_est_refuse(self):
        depense = self._depense(self.cashier_a, statut='approved')
        reponse = self._agir(self.cashier_a, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'caisse_fermee')
        depense.refresh_from_db()
        self.assertEqual(depense.status, 'approved')
        self.assertFalse(CashMovement.objects.filter(expense=depense).exists())

    def test_le_moyen_especes_explicite_exige_aussi_la_caisse(self):
        depense = self._depense(self.cashier_a, statut='approved')
        reponse = self._agir(
            self.cashier_a, depense, 'pay', payment_method=str(self.especes.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'caisse_fermee')

    def test_en_mobile_money_aucune_caisse_n_est_exigee(self):
        depense = self._depense(self.cashier_a, statut='approved')
        reponse = self._agir(
            self.cashier_a, depense, 'pay', payment_method=str(self.mobile.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        mouvement = CashMovement.objects.get(expense=depense)
        self.assertIsNone(mouvement.session_id)
        self.assertEqual(mouvement.payment_method_id, self.mobile.id)

    def test_le_caissier_ne_paie_pas_sans_approbation(self):
        self._ouvrir_caisse(self.cashier_a)
        depense = self._depense(self.cashier_a, statut='draft')
        reponse = self._agir(self.cashier_a, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'approbation_requise')

    def test_le_caissier_n_approuve_pas(self):
        depense = self._depense(self.cashier_a, statut='pending')
        reponse = self._agir(self.cashier_a, depense, 'approve')
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, reponse.data)

    def test_une_depense_rejetee_ne_se_paie_pas(self):
        self._ouvrir_caisse(self.owner)
        depense = self._depense(self.cashier_a, statut='rejected')
        reponse = self._agir(self.owner, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'depense_rejetee')


class DepenseDUnCollegueTests(_Base):
    """Un caissier ne paie que la sienne, même quand il voit celles des autres."""

    def test_un_caissier_qui_voit_tout_ne_paie_pas_la_depense_d_un_collegue(self):
        # « Voir tout dans son entrepôt » : il VOIT la dépense de son collègue.
        membre = OrganizationMembership.objects.get(user=self.cashier_b, organization=self.org)
        membre.extra_permissions = ['sales.view_all']
        membre.save()
        self._ouvrir_caisse(self.cashier_b)
        depense = self._depense(self.cashier_a, statut='approved')

        reponse = self._agir(self.cashier_b, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'depense_d_un_autre')

    def test_le_gerant_paie_la_depense_approuvee_d_un_caissier(self):
        session = self._ouvrir_caisse(self.manager)
        depense = self._depense(self.cashier_a, statut='approved')
        reponse = self._agir(self.manager, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(CashMovement.objects.get(expense=depense).session_id, session.id)


class JamaisSaPropreDepenseTests(_Base):

    def test_le_gerant_n_approuve_pas_sa_propre_depense(self):
        depense = self._depense(self.manager, statut='pending')
        reponse = self._agir(self.manager, depense, 'approve')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'propre_depense')

    def test_le_gerant_ne_rejette_pas_sa_propre_depense(self):
        depense = self._depense(self.manager, statut='pending')
        reponse = self._agir(self.manager, depense, 'reject', reason='Erreur')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'propre_depense')

    def test_le_gerant_ne_paie_pas_directement_sa_propre_depense(self):
        self._ouvrir_caisse(self.manager)
        depense = self._depense(self.manager, statut='draft')
        reponse = self._agir(self.manager, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'propre_depense')

    def test_le_gerant_approuve_la_depense_d_un_caissier(self):
        depense = self._depense(self.cashier_a, statut='pending')
        reponse = self._agir(self.manager, depense, 'approve')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_le_proprietaire_approuve_la_depense_du_gerant(self):
        depense = self._depense(self.manager, statut='pending')
        reponse = self._agir(self.owner, depense, 'approve')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    def test_le_gerant_PAIE_sa_propre_depense_une_fois_approuvee(self):
        session = self._ouvrir_caisse(self.manager)
        depense = self._depense(self.manager, statut='approved')
        reponse = self._agir(self.manager, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(CashMovement.objects.get(expense=depense).session_id, session.id)


class DepensesApprouveesAvantLeLotTests(_Base):
    """Une dépense approuvée AVANT que la sortie ne passe au paiement."""

    def test_sa_sortie_deja_faite_ne_sort_pas_une_seconde_fois(self):
        depense = self._depense(self.cashier_a, statut='approved')
        create_cash_movement_for_expense(depense, user=self.owner, session=None)

        reponse = self._agir(self.cashier_a, depense, 'pay')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(
            CashMovement.objects.filter(expense=depense, is_cancelled=False).count(), 1,
        )


class LeJournalAppliqueLesMemesReglesTests(_Base):
    """Le terminal passe par le journal : mêmes droits, même tiroir, mêmes codes."""

    def test_le_caissier_paie_depuis_le_terminal_avec_sa_caisse(self):
        session = self._ouvrir_caisse(self.cashier_a)
        depense = self._depense(self.cashier_a, statut='approved')
        verdict = self._journal(
            self.cashier_a, 'expense.pay', {'expense': str(depense.id)},
            '6a1e0000-0000-4000-8000-000000000001',
        )
        self.assertEqual(verdict['verdict'], 'applied', verdict)
        self.assertEqual(CashMovement.objects.get(expense=depense).session_id, session.id)

    def test_sans_caisse_le_journal_refuse_avec_le_code(self):
        depense = self._depense(self.cashier_a, statut='approved')
        verdict = self._journal(
            self.cashier_a, 'expense.pay', {'expense': str(depense.id)},
            '6a1e0000-0000-4000-8000-000000000002',
        )
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'caisse_fermee', verdict)

    def test_le_gerant_n_approuve_pas_la_sienne_depuis_le_terminal(self):
        depense = self._depense(self.manager, statut='pending')
        verdict = self._journal(
            self.manager, 'expense.approve', {'expense': str(depense.id)},
            '6a1e0000-0000-4000-8000-000000000003',
        )
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(verdict['errors']['code'], 'propre_depense', verdict)



class LeRecuDeDepenseSeReimprimeTests(_Base):
    """
    Le reçu sort au paiement : il nomme le PAYEUR, l'instant du paiement et la
    caisse dont l'argent est sorti. La fiche de la dépense porte de quoi le
    réimprimer à l'identique.
    """

    def test_la_fiche_dit_qui_a_paye_et_de_quelle_caisse(self):
        self._ouvrir_caisse(self.cashier_a)
        depense = self._depense(self.cashier_a, statut='approved')
        reponse = self._agir(
            self.cashier_a, depense, 'pay', payment_method=str(self.especes.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        [sortie] = reponse.data['cash_movements']
        self.assertEqual(sortie['register_name'], self.register.name)
        self.assertEqual(sortie['created_by_name'], 'Cash A')

    def test_un_paiement_sans_tiroir_ne_nomme_aucune_caisse(self):
        depense = self._depense(self.cashier_a, statut='approved')
        reponse = self._agir(
            self.cashier_a, depense, 'pay', payment_method=str(self.mobile.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        [sortie] = reponse.data['cash_movements']
        self.assertIsNone(sortie['register_name'])

    def test_paye_hors_ligne_la_depense_garde_le_jour_du_paiement(self):
        """Poussée deux jours plus tard, elle portait la date de la synchronisation."""
        self._ouvrir_caisse(self.cashier_a)
        depense = self._depense(self.cashier_a, statut='approved')
        avant_hier = timezone.localtime() - timezone.timedelta(days=2)
        verdict = self._journal(
            self.cashier_a, 'expense.pay', {'expense': str(depense.id)},
            '6a1e0000-0000-4000-8000-000000000009', quand=avant_hier.isoformat(),
        )
        self.assertEqual(verdict['verdict'], 'applied', verdict)
        depense.refresh_from_db()
        self.assertEqual(depense.paid_date, avant_hier.date())
