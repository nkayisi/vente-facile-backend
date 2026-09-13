"""
Ce que le serveur REND et ce qu'il ACCEPTE en écriture, sur les deux chemins.

┌──────────────────────────────────────────────────────────────────────────────┐
│ QUATRE DÉFAUTS, TOUS SILENCIEUX, TOUS SUR LE PAPIER DU MARCHAND.            │
│                                                                              │
│ 1. `POST /expenses/` répondait par le serializer d'ÉCRITURE, sans `id` ni    │
│    `reference` : le reçu thermique du back-office sortait sans numéro et le  │
│    fichier s'appelait `depense-undefined.pdf`.                               │
│ 2. `…CategoryCreateSerializer` n'exposait pas `id` : la création en ligne    │
│    d'un type d'entrée ne resélectionnait rien, et l'entrée partait sans      │
│    catégorie.                                                                │
│ 3. Un nom de catégorie déjà pris levait un `IntegrityError` non rattrapé,    │
│    donc HTTP 500.                                                            │
│ 4. `generate_expense_reference` triait ALPHABÉTIQUEMENT : une référence      │
│    d'appareil `DEP-…-K7QM-0042` passait au-dessus de `DEP-…-0003` et la      │
│    série du serveur sautait à 0043. Une série trouée porte le RCCM.          │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Le rôle est BORNÉ, jamais `owner` : `restrict_visibility_for_request` sort en
amont pour un propriétaire, et la moitié du code de périmètre ne serait pas
exécutée. Pour l'entrepôt, le rôle qui prouve quelque chose est le GÉRANT.
"""
from datetime import date
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import Expense, ExpenseCategory, IncomeCategory
from apps.core.utils import ReferenceGenerator
from apps.sales.tests._helpers import make_org_with_users


class _Base(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.categorie = ExpenseCategory.objects.create(
            organization=self.org, name='Transport', color='#6B7280',
        )

    def _en_gerant(self):
        self.client.force_authenticate(user=self.manager)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))


class CreationRendLaFicheTests(_Base):
    def test_POST_expenses_rend_id_et_reference(self):
        """
        Sans quoi le back-office imprime un reçu sans numéro : il lit
        `created.reference`, `created.category_name` et
        `created.payment_method_name` pour bâtir son ticket.
        """
        self._en_gerant()
        reponse = self.client.post('/api/v1/expenses/', {
            'category': str(self.categorie.id),
            'description': 'Carburant',
            'amount': '30.00',
            'expense_date': date.today().isoformat(),
            'warehouse': str(self.warehouse.id),
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        for champ in ('id', 'reference', 'category_name', 'status'):
            self.assertIn(champ, reponse.data, f"`{champ}` manque à la réponse")
        self.assertTrue(reponse.data['reference'].startswith('DEP-'))
        self.assertEqual(reponse.data['category_name'], 'Transport')

    def test_POST_income_categories_rend_id(self):
        """`result.data.id` est ce que le back-office resélectionne."""
        self._en_gerant()
        reponse = self.client.post('/api/v1/income-categories/', {
            'name': 'Subvention', 'is_active': True,
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.assertIn('id', reponse.data)
        self.assertTrue(IncomeCategory.objects.filter(id=reponse.data['id']).exists())


class UniciteDuNomTests(_Base):
    def test_un_nom_deja_pris_rend_400_et_NOMME_le_champ(self):
        """Et non un 500 : `IntegrityError` non rattrapé n'est pas un message."""
        self._en_gerant()
        reponse = self.client.post('/api/v1/expense-categories/', {
            'name': 'Transport',
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertIn('name', reponse.data)

    def test_la_casse_ne_sauve_pas_un_doublon(self):
        """
        `__iexact`, comme les catégories de PRODUITS et comme le terminal qui
        oppose `toLowerCase` avant l'envoi. Trois surfaces, une seule règle.
        """
        self._en_gerant()
        reponse = self.client.post('/api/v1/expense-categories/', {
            'name': 'tRaNsPoRt',
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)

    def test_un_nom_libre_passe(self):
        """Le contrôle refuse un doublon, pas la création."""
        self._en_gerant()
        reponse = self.client.post('/api/v1/expense-categories/', {
            'name': 'Carburant', 'description': 'Gasoil', 'color': '#10B981',
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.assertEqual(reponse.data['color'], '#10B981')

    def test_le_meme_nom_dans_une_AUTRE_organisation_passe(self):
        """La contrainte est par organisation : la borner ailleurs serait un bug."""
        from apps.organizations.models import Organization
        autre = Organization.objects.create(name='Autre', slug='autre')
        ExpenseCategory.objects.create(organization=autre, name='Transport')
        self.assertEqual(
            ExpenseCategory.objects.filter(name='Transport').count(), 2,
        )


class SerieDuServeurTests(_Base):
    def test_une_reference_d_appareil_ne_TROUE_pas_la_serie_du_serveur(self):
        """
        Le tri est alphabétique et « K » passe au-dessus de « 0 » : sans borne,
        `DEP-…-K7QM-0042` devenait le dernier rang connu et la série sautait
        à 0043.
        """
        prefixe = ReferenceGenerator.generate_expense_reference(self.org)[:12]
        commun = dict(
            organization=self.org, category=self.categorie, description='x',
            amount=Decimal('1.00'), expense_date=date.today(),
        )
        Expense.objects.create(reference=f'{prefixe}-0001', **commun)
        Expense.objects.create(reference=f'{prefixe}-0002', **commun)
        Expense.objects.create(reference=f'{prefixe}-K7QM-0042', **commun)

        self.assertEqual(
            ReferenceGenerator.generate_expense_reference(self.org),
            f'{prefixe}-0003',
        )

    def test_la_serie_des_ventes_porte_la_MEME_borne(self):
        """
        `sale.create` accepte une référence cliente depuis le lot 3 : le trou
        y existait déjà, et il se referme de la même main.
        """
        from apps.sales.models import Sale
        prefixe = ReferenceGenerator.generate_sale_reference(self.org)[:11]
        commun = dict(organization=self.org, warehouse=self.warehouse)
        Sale.objects.create(reference=f'{prefixe}-0001', **commun)
        Sale.objects.create(reference=f'{prefixe}-JJK6-0099', **commun)

        self.assertEqual(
            ReferenceGenerator.generate_sale_reference(self.org),
            f'{prefixe}-0002',
        )
