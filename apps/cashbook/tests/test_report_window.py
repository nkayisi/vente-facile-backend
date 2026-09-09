"""
La fenêtre d'un rapport de caisse : ce qu'elle accepte, et ce qu'elle refuse.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UNE SAISIE FAUTIVE SE REFUSE, ELLE NE RESSEMBLE PAS À UNE PANNE.             │
│                                                                              │
│ `_report_window` lisait `year`, `month` et `date` sans les garder : `int()`   │
│ sur une chaîne, `datetime.date()` sur un mois hors bornes, et la chaîne brute │
│ passée au lookup `__date=`. Ni `ValueError` ni la `ValidationError` de Django │
│ n'étant une `APIException`, DRF les laissait passer et l'appelant recevait un │
│ **500** pour sa propre faute de frappe.                                       │
│                                                                              │
│ L'action gardait déjà `scope` correctement ; c'est la FENÊTRE qui avait été   │
│ oubliée. `format_day` ne rattrape rien : il rend la valeur inchangée quand    │
│ elle est illisible, par choix, ce qui déplace la panne à la frontière de      │
│ l'ORM au lieu de l'empêcher.                                                  │
└──────────────────────────────────────────────────────────────────────────────┘

Les tests s'authentifient en GÉRANT : un propriétaire sort en amont du périmètre
entrepôt, et la moitié du code ne serait pas exécutée.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency


class _SetupCaisse(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        Currency.objects.get_or_create(
            code='CDF',
            defaults={'name': 'Franc congolais', 'symbol': 'FC',
                      'decimal_places': 0},
        )
        self.client.force_authenticate(user=self.manager)
        # De quoi que la caisse ne soit pas vide. Ces tests portent sur la
        # FENÊTRE, pas sur le périmètre entrepôt, qui a ses propres suites.
        CashMovement.objects.create(
            organization=self.org,
            movement_type=CashMovement.MovementType.OTHER_IN,
            amount=Decimal('1500.00'), currency=self.org.currency,
            description='Apport', movement_date=timezone.now(),
            balance_after=Decimal('1500.00'),
        )

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _get(self, requete):
        return self.client.get(
            f'/api/v1/cash-movements/export-report/?{requete}', **self._headers,
        )


class FenetreRefuseeTests(_SetupCaisse):
    """Chaque refus NOMME son champ : « requête invalide » ne dit pas lequel."""

    def test_un_mois_hors_bornes_repond_400_et_non_500(self):
        resp = self._get('scope=monthly&month=13&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('month', resp.data)

    def test_une_annee_illisible_repond_400_et_non_500(self):
        resp = self._get('scope=monthly&year=oops&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('year', resp.data)

    def test_une_annee_hors_de_ce_que_date_sait_construire(self):
        """`datetime.date(99999, 1, 1)` lève : la borne se pose AVANT elle."""
        resp = self._get('scope=annual&year=99999&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('year', resp.data)

    def test_une_date_illisible_repond_400_et_non_500(self):
        resp = self._get('scope=daily&date=oops&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('date', resp.data)

    def test_les_deux_bornes_d_une_periode_libre_sont_gardees(self):
        for champ in ('date_from', 'date_to'):
            with self.subTest(champ=champ):
                params = {'date_from': '2026-01-01', 'date_to': '2026-09-04'}
                params[champ] = 'oops'
                resp = self._get(
                    f"scope=custom&date_from={params['date_from']}"
                    f"&date_to={params['date_to']}&export_format=csv"
                )
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(champ, resp.data)

    def test_une_portee_inconnue_reste_refusee(self):
        resp = self._get('scope=hebdomadaire&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('scope', resp.data)


class FenetreAccepteeTests(_SetupCaisse):
    """Le garde-fou ne doit rien fermer de ce qui marchait."""

    def test_les_quatre_portees_rendent_leur_document(self):
        aujourdhui = timezone.localdate()
        fenetres = {
            'daily': f"scope=daily&date={aujourdhui.isoformat()}",
            'monthly': f"scope=monthly&year={aujourdhui.year}&month={aujourdhui.month}",
            'annual': f"scope=annual&year={aujourdhui.year}",
            'custom': f"scope=custom&date_from={aujourdhui.isoformat()}"
                      f"&date_to={aujourdhui.isoformat()}",
        }
        libelles = {
            'daily': aujourdhui.strftime('%d/%m/%Y'),
            'monthly': str(aujourdhui.year),
            'annual': f'Année {aujourdhui.year}',
            'custom': aujourdhui.strftime('%d/%m/%Y'),
        }
        for portee, requete in fenetres.items():
            with self.subTest(portee=portee):
                resp = self._get(f'{requete}&export_format=csv')
                self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
                # Le document ANNONCE la fenêtre qu'on lui a demandée : c'est ce
                # que le garde-fou doit laisser passer intact.
                self.assertIn(libelles[portee], resp.content.decode('utf-8'))

    def test_sans_parametre_la_fenetre_est_celle_du_jour(self):
        resp = self._get('scope=daily&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

    def test_un_mois_sur_deux_chiffres_est_accepte(self):
        """« 09 » vient d'un `<input type=month>`, pas d'une faute de saisie."""
        resp = self._get('scope=monthly&year=2026&month=09&export_format=csv')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
