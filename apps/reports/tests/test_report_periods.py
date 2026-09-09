"""
Les périodes des rapports.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CES TESTS DÉFENDENT : LA PAGE S'OUVRAIT SUR UNE FENÊTRE VIDE.        │
│                                                                              │
│ Le défaut de `_parse_date_range` était `month`, qui est CALENDAIRE. Un appel │
│ sans paramètre couvrait donc du 1er du mois à aujourd'hui : le 2 septembre   │
│ 2026, DEUX JOURS. Mesuré sur les vraies données de développement, la même    │
│ base rendait 0 vente sur cette fenêtre et 18 sur trente jours glissants. Le  │
│ back-office et le terminal annonçaient « Aucune donnée », et un marchand y   │
│ lit une perte de données, pas un début de mois.                              │
│                                                                              │
│ Le terminal, qui n'envoyait aucune date, y tombait TOUJOURS.                 │
└──────────────────────────────────────────────────────────────────────────────┘

Les périodes CALENDAIRES restent, et gardent leur sens : le back-office les
nomme « Ce mois », « Cette année », et une fenêtre nommée par un calendrier
doit suivre le calendrier. Ce qui était faux, c'était de l'imposer par défaut.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from apps.reports.views import StatisticsViewSet


class PeriodesDesRapportsTests(SimpleTestCase):
    """
    On appelle `_parse_date_range` directement : ce sont les BORNES qui sont en
    cause, et les faire passer par une requête complète mêlerait au test le
    périmètre entrepôt et les conversions, qui ont leurs propres suites.
    """

    def bornes(self, requete_query='', aujourdhui=date(2026, 9, 2)):
        vue = StatisticsViewSet()
        requete = RequestFactory().get(f'/?{requete_query}')
        # DRF expose `query_params` ; le `RequestFactory` de Django rend un
        # `HttpRequest`, dont `GET` porte la même chose.
        requete.query_params = requete.GET
        # On simule `localdate` et non `now` : les bornes se lisent en heure
        # LOCALE, sans quoi la fenêtre désigne la veille pendant l'heure qui
        # sépare 23h UTC de minuit à Kinshasa.
        with patch('apps.reports.views.timezone.localdate', return_value=aujourdhui):
            debut, fin, _, _ = vue._parse_date_range(requete)
        return debut, fin

    def test_le_defaut_est_glissant_sur_trente_jours(self):
        """Le cœur du défaut : un appel sans paramètre couvrait deux jours."""
        debut, fin = self.bornes()
        self.assertEqual(debut, date(2026, 8, 4))
        self.assertEqual(fin, date(2026, 9, 2))

    def test_le_defaut_ne_depend_pas_du_quantieme(self):
        """
        Trente jours restent trente jours le 1er du mois comme le 28.

        C'est ce que `month` ne faisait pas, et c'est pourquoi le défaut ne se
        voyait que les premiers jours de chaque mois.
        """
        for jour in (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 28),
                     date(2026, 1, 1), date(2026, 3, 1)):
            with self.subTest(aujourdhui=jour):
                debut, fin = self.bornes(aujourdhui=jour)
                self.assertEqual((fin - debut).days + 1, 30)

    def test_les_fenetres_glissantes_s_emboitent(self):
        """
        `7 jours ⊆ 30 jours ⊆ 12 mois`, quel que soit le quantième.

        Seul le glissant le garantit : le 1er septembre, la semaine calendaire
        commence le 31 août et déborde déjà du mois calendaire. Un marchand qui
        voit « 30 jours » rendre MOINS que « 7 jours » conclut à une panne.
        """
        for jour in (date(2026, 9, 1), date(2026, 9, 2), date(2026, 1, 1),
                     date(2026, 2, 28), date(2028, 2, 29)):
            with self.subTest(aujourdhui=jour):
                d7, f7 = self.bornes('period=last_7_days', jour)
                d30, f30 = self.bornes('period=last_30_days', jour)
                d12, f12 = self.bornes('period=last_12_months', jour)
                self.assertGreaterEqual(d7, d30)
                self.assertGreaterEqual(d30, d12)
                self.assertEqual(f7, jour)
                self.assertEqual(f30, jour)
                self.assertEqual(f12, jour)

    def test_sept_jours_en_compte_sept_aujourd_hui_inclus(self):
        """
        Huit jours fausseraient la comparaison à la période précédente, qui a
        la même longueur : deux fenêtres inégales inventent une variation.
        """
        debut, fin = self.bornes('period=last_7_days')
        self.assertEqual((fin - debut).days + 1, 7)

    def test_douze_mois_part_du_premier_d_un_mois(self):
        """
        Le graphique groupe par mois : une fenêtre à cheval rendrait treize
        seaux dont deux partiels, avec deux étiquettes « sept. » sur le même axe.
        """
        debut, _ = self.bornes('period=last_12_months')
        self.assertEqual(debut, date(2025, 10, 1))
        self.assertEqual(debut.day, 1)

    def test_les_periodes_calendaires_restent_calendaires(self):
        """
        Elles ne sont pas un défaut : leur libellé les annonce. Les basculer en
        glissant ferait mentir « Ce mois » au back-office.
        """
        debut, _ = self.bornes('period=month')
        self.assertEqual(debut, date(2026, 9, 1))
        debut, _ = self.bornes('period=year')
        self.assertEqual(debut, date(2026, 1, 1))
        debut, _ = self.bornes('period=today')
        self.assertEqual(debut, date(2026, 9, 2))

    def test_une_periode_inconnue_retombe_sur_le_defaut(self):
        """Et non sur une fenêtre vide : un client à jour n'est pas puni."""
        self.assertEqual(self.bornes('period=n_importe_quoi'), self.bornes())

    def test_les_dates_explicites_priment_sur_tout(self):
        """C'est ce que le terminal envoie : sa fenêtre ne dépend d'aucun défaut."""
        debut, fin = self.bornes('period=today&date_from=2026-08-01&date_to=2026-08-31')
        self.assertEqual(debut, date(2026, 8, 1))
        self.assertEqual(fin, date(2026, 8, 31))

    def test_la_periode_precedente_a_la_MEME_longueur(self):
        """Sinon la variation compare deux fenêtres inégales."""
        vue = StatisticsViewSet()
        requete = RequestFactory().get('/?period=last_30_days')
        requete.query_params = requete.GET
        with patch('apps.reports.views.timezone.localdate', return_value=date(2026, 9, 2)):
            debut, fin, prec_debut, prec_fin = vue._parse_date_range(requete)
        self.assertEqual((fin - debut).days, (prec_fin - prec_debut).days)
        self.assertEqual(prec_fin, debut - timedelta(days=1))
