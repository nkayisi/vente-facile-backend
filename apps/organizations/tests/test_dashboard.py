"""
Le tableau de bord : des périodes EMBOÎTÉES, et un seul chiffre par relevé.

┌──────────────────────────────────────────────────────────────────────────────┐
│ « MOIS » POUVAIT ÊTRE PLUS PETIT QUE « SEMAINE ».                            │
│                                                                              │
│ Les quatre boutons ne parlaient pas la même langue : `week` était GLISSANT   │
│ (`today - 6`), `month` et `year` CALENDAIRES (le 1er du mois, le 1er         │
│ janvier). Le 1er septembre, « Mois » couvrait donc une seule journée pendant │
│ que « Semaine » remontait au 26 août : une vente du 28 août apparaissait     │
│ dans « Semaine » et dans « Année », et disparaissait de « Mois ».            │
│                                                                              │
│ Ce n'était pas une erreur de calcul mais un mélange de sémantiques, et il    │
│ se reproduisait les six premiers jours de CHAQUE mois. Passer tout en        │
│ calendaire n'aurait rien réglé : le 1er septembre, la semaine calendaire     │
│ commence le 31 août et déborde encore du mois.                               │
│                                                                              │
│ Tout est donc GLISSANT, seule sémantique où chaque période contient          │
│ strictement la précédente. C'est l'invariant que tient ce fichier.           │
└──────────────────────────────────────────────────────────────────────────────┘

Le second volet est la DEVISE. Le tableau de bord sommait `Sum('total')` sans
regarder `currency` : sur un établissement qui facture en francs et en dollars,
il additionnait 107 000 et 50 pour afficher 107 050 sous le symbole de la
principale. Le chiffre n'existait pas. Tout est désormais converti au taux FIGÉ
sur chaque vente (`Sale.exchange_rate`), jamais au taux du jour : un tableau de
bord dont les chiffres d'hier bougent avec le cours n'est pas relisable.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.views import _periode_glissante
from apps.sales.models import Payment, PaymentMethod, Sale, SaleItem
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency

#: Dates de contrôle. Le 1er d'un mois et le 1er janvier sont les deux jours où
#: l'ancienne règle cassait ; le milieu de mois est le témoin.
JOURS_TEMOINS = [
    date(2026, 9, 1),    # 1er du mois : « Mois » ne valait qu'un jour
    date(2026, 1, 1),    # 1er janvier : « Année » ne valait qu'un jour non plus
    date(2026, 3, 2),    # le lendemain d'un 1er
    date(2026, 7, 17),   # milieu de mois, le cas ordinaire
    date(2028, 2, 29),   # année bissextile, pour que le recul de 12 mois tienne
]


class PeriodesEmboiteesTests(APITestCase):
    """
    L'invariant : `jour ⊆ semaine ⊆ mois ⊆ année`, quel que soit le jour.

    Un test par bornes plutôt que par endpoint : c'est une fonction pure, et la
    faire tourner sur cinq dates coûte moins qu'un aller-retour HTTP. La
    reproduction chiffrée, elle, passe bien par l'endpoint (classe suivante).
    """

    def test_chaque_periode_contient_la_precedente(self):
        for jour in JOURS_TEMOINS:
            debuts = {}
            for periode in ('day', 'week', 'month', 'year'):
                debut, _, _ = _periode_glissante(periode, jour)
                debuts[periode] = debut
                self.assertLessEqual(
                    debut, jour,
                    f"{periode} au {jour} : le début est dans le futur",
                )

            with self.subTest(jour=jour):
                # Un début PLUS ANCIEN veut dire une période plus large : c'est
                # l'emboîtement, la borne haute étant toujours aujourd'hui.
                self.assertLessEqual(
                    debuts['week'], debuts['day'],
                    f"au {jour}, « semaine » ne contient pas « jour »",
                )
                self.assertLessEqual(
                    debuts['month'], debuts['week'],
                    f"au {jour}, « mois » ne contient pas « semaine » "
                    f"(mois part du {debuts['month']}, semaine du {debuts['week']})",
                )
                self.assertLessEqual(
                    debuts['year'], debuts['month'],
                    f"au {jour}, « année » ne contient pas « mois »",
                )

    def test_les_decalages_sont_ceux_que_le_terminal_recopie(self):
        """
        Les valeurs exactes, parce que `mobile/.../data/tableau-de-bord.ts`
        les recopie. Les changer d'un côté sans l'autre ferait donner deux
        chiffres au même établissement, sans que rien ne le signale.
        """
        jour = date(2026, 9, 1)
        self.assertEqual(_periode_glissante('day', jour)[0], jour)
        self.assertEqual(_periode_glissante('week', jour)[0], date(2026, 8, 26))
        self.assertEqual(_periode_glissante('month', jour)[0], date(2026, 8, 3))
        # L'année part du 1er d'un mois, pas de `today - 364` : le graphique
        # groupe par mois, et une fenêtre à cheval rendrait treize seaux dont
        # deux partiels, avec deux étiquettes « sept. » sur le même axe.
        self.assertEqual(_periode_glissante('year', jour)[0], date(2025, 10, 1))

    def test_la_periode_precedente_est_de_meme_longueur(self):
        """
        Sans quoi la variation compare deux fenêtres inégales et invente une
        hausse. La précédente s'arrête la veille du début de la courante.
        """
        for jour in JOURS_TEMOINS:
            for periode in ('day', 'week', 'month', 'year'):
                debut, debut_prec, fin_prec = _periode_glissante(periode, jour)
                with self.subTest(jour=jour, periode=periode):
                    self.assertEqual(fin_prec, debut - timedelta(days=1))
                    self.assertLessEqual(debut_prec, fin_prec)
                    if periode != 'year':
                        # L'année recule de douze mois, dont la longueur varie :
                        # seule l'égalité stricte des périodes en jours est
                        # exigée des trois autres.
                        self.assertEqual(
                            (fin_prec - debut_prec).days,
                            (jour - debut).days,
                        )

    def test_une_periode_inconnue_retombe_sur_le_mois(self):
        """`period` vient d'une chaîne de requête : elle peut être n'importe quoi."""
        self.assertEqual(
            _periode_glissante('n_importe_quoi', date(2026, 9, 1)),
            _periode_glissante('month', date(2026, 9, 1)),
        )


class _BaseDashboard(APITestCase):
    """Socle commun : une organisation, et un appel au tableau de bord."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.owner)

    def _dashboard(self, period, aujourdhui=None):
        cible = 'apps.organizations.views.timezone.now'
        entetes = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        url = f'/api/v1/organizations/{self.org.id}/dashboard/'

        if aujourdhui is None:
            reponse = self.client.get(url, {'period': period}, **entetes)
        else:
            fige = timezone.make_aware(
                timezone.datetime.combine(aujourdhui, timezone.datetime.min.time())
            ) + timedelta(hours=12)
            with patch(cible, return_value=fige):
                reponse = self.client.get(url, {'period': period}, **entetes)

        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        return reponse.data

    def _vente(self, total, quand, currency='', cout=Decimal('0')):
        vente = Sale.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            register=self.register,
            reference=f'VT-{Sale.objects.count() + 1:04d}',
            status='completed',
            subtotal=total,
            total=total,
            amount_paid=total,
            currency=currency,
            sold_by=self.owner,
        )
        # `sale_date` porte un défaut d'horloge métier : on l'écrit après coup,
        # le test devant placer la vente à une date choisie.
        Sale.objects.filter(pk=vente.pk).update(sale_date=quand)
        vente.refresh_from_db()
        if cout:
            SaleItem.objects.create(
                organization=self.org, sale=vente, product=self._produit(),
                quantity=Decimal('1'), unit_price=total,
                cost_price=cout, subtotal=total, total=total,
            )
        return vente

    def _produit(self):
        from apps.products.models import Product

        if not hasattr(self, '_p'):
            self._p = Product.objects.create(
                organization=self.org, name='Article', sku='ART-1',
                selling_price=Decimal('10'), cost_price=Decimal('6'),
            )
        return self._p


class ReproductionDuDefautTests(_BaseDashboard):
    """
    Le défaut tel qu'il a été rapporté : « sur semaine j'ai des données, sur
    mois il n'y en a plus, et sur année elles reviennent ».
    """

    def test_une_vente_de_fin_de_mois_est_dans_les_trois_periodes(self):
        aujourdhui = date(2026, 9, 1)
        quand = timezone.make_aware(
            timezone.datetime(2026, 8, 28, 10, 0)
        )
        self._vente(Decimal('500.00'), quand)

        for periode in ('week', 'month', 'year'):
            with self.subTest(periode=periode):
                donnees = self._dashboard(periode, aujourdhui)
                self.assertEqual(
                    Decimal(donnees['cards']['total_sales']['value']),
                    Decimal('500.00'),
                    f"la vente du 28 août est absente de « {periode} » "
                    f"vue depuis le 1er septembre",
                )

    def test_le_mois_couvre_au_moins_la_semaine(self):
        """
        Le contrôle que le marchand fait à l'œil : « 30 jours » ne peut pas
        annoncer moins que « 7 jours ».
        """
        aujourdhui = date(2026, 9, 1)
        self._vente(
            Decimal('100.00'),
            timezone.make_aware(timezone.datetime(2026, 8, 27, 9, 0)),
        )
        self._vente(
            Decimal('40.00'),
            timezone.make_aware(timezone.datetime(2026, 8, 10, 9, 0)),
        )

        semaine = Decimal(self._dashboard('week', aujourdhui)['cards']['total_sales']['value'])
        mois = Decimal(self._dashboard('month', aujourdhui)['cards']['total_sales']['value'])
        annee = Decimal(self._dashboard('year', aujourdhui)['cards']['total_sales']['value'])

        self.assertGreaterEqual(mois, semaine)
        self.assertGreaterEqual(annee, mois)
        self.assertEqual(semaine, Decimal('100.00'))
        self.assertEqual(mois, Decimal('140.00'))


class DeviseUniqueTests(_BaseDashboard):
    """
    Tout le tableau de bord est en devise principale, converti au taux figé sur
    la vente. Le livre de caisse, lui, reste multi-devise : il rend la réalité
    physique du tiroir.
    """

    def setUp(self):
        super().setUp()
        self.org.currency = 'USD'
        self.org.save(update_fields=['currency'])

        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        cdf = Currency.objects.get(code='CDF')
        OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=True,
        )
        # 1 CDF = 0,000357142857 USD, soit 1 USD = 2 800 CDF.
        OrganizationCurrency.objects.create(
            organization=self.org, currency=cdf, is_primary=False,
            exchange_rate=Decimal('0.000357142857'),
        )
        from apps.settings.services import CurrencyService
        CurrencyService.invalidate_cache(self.org)

    def test_le_chiffre_d_affaires_est_converti_et_non_additionne(self):
        quand = timezone.now() - timedelta(days=2)
        self._vente(Decimal('100.00'), quand, currency='USD')
        self._vente(Decimal('2800.00'), quand, currency='CDF')

        donnees = self._dashboard('month')
        total = Decimal(donnees['cards']['total_sales']['value'])

        self.assertEqual(
            total, Decimal('101.00'),
            "les deux devises ont été additionnées brutes au lieu d'être converties",
        )
        self.assertEqual(donnees['currency'], 'USD')

    def test_l_evolution_est_convertie_elle_aussi(self):
        quand = timezone.now() - timedelta(days=2)
        self._vente(Decimal('100.00'), quand, currency='USD')
        self._vente(Decimal('2800.00'), quand, currency='CDF')

        points = self._dashboard('month')['charts']['sales_evolution']
        self.assertEqual(
            sum(Decimal(p['total']) for p in points), Decimal('101.00'),
        )

    def test_le_cout_n_est_PAS_converti(self):
        """
        `SaleItem.cost_price` vient du catalogue, qui n'a pas de devise : il est
        DÉJÀ en principale. Lui appliquer le taux de la vente diviserait le coût
        d'une vente en francs par deux mille huit cents, et la marge affichée
        passerait de 40 % à 100 %.
        """
        quand = timezone.now() - timedelta(days=2)
        # Facturé 2 800 CDF (= 1 USD), coûtant 0,60 USD : marge de 0,40 USD.
        self._vente(Decimal('2800.00'), quand, currency='CDF', cout=Decimal('0.60'))

        cartes = self._dashboard('month')['cards']
        self.assertEqual(Decimal(cartes['gross_profit']['value']), Decimal('0.40'))
        self.assertEqual(cartes['gross_profit']['margin'], 40.0)

    def test_les_reglements_sont_ventiles_par_devise_ET_convertis(self):
        quand = timezone.now() - timedelta(days=2)
        methode = PaymentMethod.objects.create(
            organization=self.org, name='Espèces', code='CASH',
            method_type='cash', is_active=True, is_default=True,
        )
        vente = self._vente(Decimal('2800.00'), quand, currency='CDF')
        # Le client règle en francs : `amount` est en devise de VENTE (CDF),
        # `tendered_amount` est le billet reçu, dans `currency`.
        Payment.objects.create(
            organization=self.org, sale=vente, payment_method=methode,
            amount=Decimal('2800.00'), tendered_amount=Decimal('2800.00'),
            currency='CDF', exchange_rate=Decimal('1.000000'), status='completed',
        )

        graphiques = self._dashboard('month')['charts']
        par_devise = {ligne['code']: ligne for ligne in graphiques['by_currency']}

        self.assertIn('CDF', par_devise)
        self.assertEqual(Decimal(par_devise['CDF']['native_total']), Decimal('2800.00'))
        self.assertEqual(Decimal(par_devise['CDF']['primary_total']), Decimal('1.00'))
        self.assertEqual(par_devise['CDF']['count'], 1)

        par_moyen = graphiques['by_payment_method']
        self.assertEqual(Decimal(par_moyen[0]['value']), Decimal('1.00'))
