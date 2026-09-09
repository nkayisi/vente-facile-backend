"""
Les CHEMINS des statistiques, figés.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CE TEST DÉFEND : SEPT RUBRIQUES DU TERMINAL RÉPONDAIENT 404.          │
│                                                                              │
│ `@action` sans `url_path` laisse le nom de la MÉTHODE tel quel et ne         │
│ remplace les soulignés que dans `url_name`. Le chemin est donc               │
│ `sales_by_category` et le nom de route `statistics-sales-by-category` : deux │
│ orthographes pour la même action, dont une seule marche.                     │
│                                                                              │
│ Le terminal les avait écrits avec des tirets. Relevé sur le vrai serveur le  │
│ 2 septembre 2026 : sept des huit rubriques de « Rapports & Statistiques »    │
│ rendaient 404, et la huitième des zéros. Rien ne le signalait, ni côté       │
│ serveur (une route absente n'est pas une erreur) ni côté client (un 404 se   │
│ lit comme une rubrique vide).                                                │
│                                                                              │
│ Ce test fige les chemins DANS CE SENS ; `src/data/rapports-chemins.test.ts`  │
│ les fige dans l'autre. Renommer une action casse donc les deux tests, et non │
│ l'application d'un marchand.                                                 │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from django.test import SimpleTestCase
from django.urls import resolve

from apps.reports.views import StatisticsViewSet


#: Les chemins publiés. Ajouter une action est libre ; en RENOMMER une casse le
#: terminal, qui les écrit en dur - d'où cette liste, à mettre à jour des DEUX
#: côtés en même temps.
CHEMINS_PUBLIES = {
    'cash_flow',
    'cashbook',
    'customers',
    'daily_cash_report',
    'export',
    'product_profits',
    'product_supplies',
    'profit_margins',
    'receivables',
    'sales',
    'sales-by-packaging',
    'sales_by_category',
    'sales_by_payment_method',
    'sales_by_period',
    'stock',
    'stock_details',
    'stock_movements_summary',
    'summary',
    'top_customers',
    'top_products',
    'user_activity',
}

#: Ce que le terminal appelle, à l'identique de `CHEMINS_STATISTIQUES`.
CHEMINS_DU_TERMINAL = {
    'summary',
    'daily_cash_report',
    'sales_by_category',
    'top_products',
    'top_customers',
    'stock_details',
    'product_profits',
    'user_activity',
    'receivables',
}


class CheminsDesStatistiquesTests(SimpleTestCase):
    def test_le_balayage_trouve_bien_des_actions(self):
        """Un balayage qui ne balaie rien passe et ne prouve rien."""
        self.assertGreater(len(StatisticsViewSet.get_extra_actions()), 15)

    def test_aucune_action_n_a_ete_renommee(self):
        actuels = {a.url_path for a in StatisticsViewSet.get_extra_actions()}
        disparus = CHEMINS_PUBLIES - actuels
        self.assertEqual(
            disparus,
            set(),
            "Chemin(s) renommé(s) ou supprimé(s). Le terminal les écrit en dur : "
            "mettre à jour CHEMINS_STATISTIQUES et rapports-chemins.test.ts "
            "dans mobile/vf-marchand AVANT de livrer.",
        )

    def test_tout_ce_que_le_terminal_appelle_se_resout(self):
        """
        On RÉSOUT réellement l'URL, on ne se contente pas de comparer des noms.

        Comparer deux listes de chaînes ne prouve pas qu'un chemin est routé :
        c'est le routeur qui décide, et c'est lui qui a rendu les 404.
        """
        for chemin in sorted(CHEMINS_DU_TERMINAL):
            with self.subTest(chemin=chemin):
                match = resolve(f'/api/v1/reports/statistics/{chemin}/')
                self.assertIs(match.func.cls, StatisticsViewSet)

    def test_la_forme_a_TIRETS_ne_se_resout_PAS(self):
        """
        Le contrôle en sens inverse, sans lequel le test ci-dessus passerait
        même si le routeur acceptait les deux orthographes.
        """
        from django.urls.exceptions import Resolver404

        for faux in ('sales-by-category', 'top-products', 'daily-cash-report',
                     'stock-details', 'product-profits', 'user-activity',
                     'top-customers'):
            with self.subTest(chemin=faux):
                with self.assertRaises(Resolver404):
                    resolve(f'/api/v1/reports/statistics/{faux}/')
