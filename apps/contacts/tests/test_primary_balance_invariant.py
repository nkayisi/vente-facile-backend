"""
`Customer.current_balance` est TOUJOURS la somme des soldes par devise.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE N'EST PAS UNE DIVERGENCE, ET C'EST PRÉCISÉMENT CE QU'IL FAUT ÉPINGLER.   │
│                                                                              │
│ L'audit de parité soupçonnait `current_balance` de dériver : la dette vit    │
│ dans `CustomerBalance`, une ligne PAR DEVISE, et le scalaire n'en est qu'un  │
│ résumé converti. Vérification faite, `recompute_primary_balance(save=True)`  │
│ est appelée après chaque écriture, et le scalaire suit. Aucun code ne change.│
│                                                                              │
│ Mais ce scalaire est le SEUL chiffre que le terminal oppose au caissier :    │
│ `evaluateCredit` compare `current_balance` à `credit_limit`, tous deux en    │
│ devise principale, parce qu'une facture peut être libellée ailleurs. Une     │
│ dérive ici ferait accorder du crédit au-delà du plafond, hors ligne, sans    │
│ que rien ne le signale avant la poussée. L'invariant vaut donc d'être tenu   │
│ par un test plutôt que par une lecture de code.                              │
│                                                                              │
│ Le multi-devise est le cas qui mord : c'est la conversion qui peut diverger, │
│ pas l'addition.                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from apps.contacts import services
from apps.contacts.models import Customer, CustomerBalance
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency


class SoldePrincipalInvariantTests(APITestCase):
    """CDF principale, USD à 2 800 : un dollar vaut 2 800 francs."""

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
        self.org.currency = 'CDF'
        self.org.save(update_fields=['currency'])

        self.client_ = Customer.objects.create(
            organization=self.org, name='Nelly', code='C1',
        )

    def _somme_attendue(self):
        """La somme des lignes de devise, convertie, relue depuis la base."""
        from apps.settings.services import CurrencyService

        total = Decimal('0.00')
        for row in CustomerBalance.objects.filter(customer=self.client_):
            total += CurrencyService.convert_to_primary(
                row.amount, row.currency, self.org,
            )
        return total.quantize(Decimal('0.01'))

    def _assert_invariant(self, etape):
        self.client_.refresh_from_db()
        self.assertEqual(
            self.client_.current_balance, self._somme_attendue(),
            f"{etape} : le scalaire opposé au caissier ne résume plus ses "
            f"lignes de devise.",
        )

    def test_une_dette_en_devise_principale(self):
        services.apply_debt(self.client_, Decimal('5000.00'), currency='CDF')
        self._assert_invariant("après une dette en CDF")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('5000.00'))

    def test_une_dette_en_devise_ETRANGERE_est_convertie(self):
        services.apply_debt(self.client_, Decimal('10.00'), currency='USD')

        self._assert_invariant("après une dette en USD")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('28000.00'))

    def test_DEUX_devises_se_cumulent_dans_le_scalaire(self):
        services.apply_debt(self.client_, Decimal('5000.00'), currency='CDF')
        services.apply_debt(self.client_, Decimal('10.00'), currency='USD')

        self._assert_invariant("après deux dettes de devises différentes")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('33000.00'))

    def test_un_reglement_partiel_suit(self):
        services.apply_debt(self.client_, Decimal('10.00'), currency='USD')
        services.settle_debt(self.client_, Decimal('4.00'), currency='USD')

        self._assert_invariant("après un règlement partiel")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('16800.00'))

    def test_une_AVANCE_rend_le_scalaire_negatif(self):
        # Payer plus que sa dette rend le client créditeur. Le scalaire doit
        # passer sous zéro, sinon le comptoir croirait le client encore
        # endetté et lui refuserait un crédit qu'il a déjà payé d'avance.
        services.settle_debt(self.client_, Decimal('3.00'), currency='USD')

        self._assert_invariant("après une avance")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('-8400.00'))

    def test_un_AJUSTEMENT_manuel_suit(self):
        services.apply_debt(self.client_, Decimal('5000.00'), currency='CDF')
        services.adjust_balance(self.client_, Decimal('-2000.00'), currency='CDF')

        self._assert_invariant("après un ajustement")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('3000.00'))

    def test_l_invariant_TIENT_sur_une_suite_d_ecritures(self):
        """
        Le contrôle qui compte : c'est la dérive CUMULÉE qu'on redoute, pas une
        écriture isolée. Sept mouvements, deux devises, dans les deux sens.
        """
        etapes = [
            (services.apply_debt, Decimal('12000.00'), 'CDF'),
            (services.apply_debt, Decimal('25.00'), 'USD'),
            (services.settle_debt, Decimal('5000.00'), 'CDF'),
            (services.apply_debt, Decimal('7.50'), 'USD'),
            (services.settle_debt, Decimal('30.00'), 'USD'),
            (services.apply_debt, Decimal('3333.33'), 'CDF'),
            (services.settle_debt, Decimal('1111.11'), 'CDF'),
        ]
        for numero, (acte, montant, devise) in enumerate(etapes, 1):
            with self.subTest(etape=numero, devise=devise):
                acte(self.client_, montant, currency=devise)
                self._assert_invariant(f"après le mouvement {numero}")

    def test_une_ligne_de_devise_a_ZERO_ne_fausse_rien(self):
        """
        Une dette soldée laisse une ligne à zéro. `balances_by_currency` les
        écarte ; le scalaire doit les traiter pareil, sinon un client à jour
        traînerait un solde résiduel.
        """
        services.apply_debt(self.client_, Decimal('10.00'), currency='USD')
        services.settle_debt(self.client_, Decimal('10.00'), currency='USD')

        self._assert_invariant("après extinction complète")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.current_balance, Decimal('0.00'))
