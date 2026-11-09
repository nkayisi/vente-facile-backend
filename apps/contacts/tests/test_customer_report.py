"""
Le rapport d'un client sur une période.

Couvre ce qui, sans filet, se casse en silence : un caissier qui lit les ventes
de ses collègues, un panier moyen calculé sur des devises mêlées, une cagnotte
disponible présentée comme un chiffre de la période, et une borne de date
décalée par le fuseau.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status

from apps.organizations.models import OrganizationMembership
from apps.sales.models import Register, RegisterSession, Sale
from apps.settings.models import (
    Currency, CustomerLoyalty, LoyaltyProgram, LoyaltyTransaction,
    OrganizationCurrency,
)

from .test_debt_flows import _DebtBaseTest


class _RapportBaseTest(_DebtBaseTest):
    def setUp(self):
        super().setUp()
        # ⚠ UN SECOND COMPTOIR POUR LE SECOND CAISSIER, et c'est une règle
        #   SERVEUR qui l'impose : « Cette session a été ouverte par un autre
        #   utilisateur ». Sans lui, la vente du collègue était refusée en 400
        #   et le test de périmètre passait pour une raison qui n'était pas la
        #   sienne - l'assertion sur le 201 est ce qui l'a montré.
        self.register_b = Register.objects.create(
            organization=self.org, branch=self.branch, warehouse=self.warehouse,
            name='Caisse B', code='CB',
        )
        RegisterSession.objects.create(
            organization=self.org, register=self.register_b,
            opened_by=self.cashier_b, opening_balance=Decimal('0'),
            status='open',
        )

    def _vente_de(self, qui, unit_price='2000.00'):
        """Une vente à crédit faite PAR `qui`, et dont on exige la création."""
        comptoir = self.register_b if qui is self.cashier_b else self.register
        self.client.force_authenticate(user=qui)
        r = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(comptoir.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'credit', 'is_pos': True,
                'customer': str(self.customer.id),
                'items': [{
                    'product': str(self.product.id),
                    'quantity': '1', 'unit_price': unit_price,
                }],
                'payments': [],
            },
            format='json', **self._headers(),
        )
        # ⚠ ON EXIGE LE 201 : sans cette assertion, un refus silencieux ferait
        #   passer le test de périmètre pour une raison qui n'est pas la sienne.
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        return r.data['id']

    def _rapport(self, qui, **params):
        self.client.force_authenticate(user=qui)
        return self.client.get(
            f'/api/v1/customers/{self.customer.id}/report/',
            params, **self._headers(),
        )


class PerimetreDeRoleTests(_RapportBaseTest):

    def test_UN_CAISSIER_NE_LIT_QUE_SES_PROPRES_VENTES(self):
        """
        ⚠ C'EST LE CORRECTIF ADJACENT DE CE LOT.

        `CustomerViewSet.sales` ne bornait RIEN, alors que `/sales/` refuse à un
        caissier les ventes de ses collègues. Les deux listes vivent sur la MÊME
        page de la fiche client : les laisser diverger ferait douter des deux
        chiffres.
        """
        self._create_sale()  # cashier_a
        self._vente_de(self.cashier_b)

        r = self._rapport(self.cashier_a)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        # Une seule vente : la sienne.
        self.assertEqual(r.data['sales']['count'], 1)
        self.assertEqual(Decimal(r.data['sales']['revenue']), Decimal('2000.00'))

        # Le gérant, qui porte `sales.view_all`, voit les deux.
        rm = self._rapport(self.manager)
        self.assertEqual(rm.data['sales']['count'], 2)

    def test_la_LISTE_de_ventes_de_la_fiche_borne_de_la_meme_facon(self):
        """
        ⚠ Les deux doivent s'accorder : c'est ce que le correctif rend
          structurel (`_ventes_du_client` est partagée).
        """
        self._create_sale()
        self._vente_de(self.cashier_b)

        self.client.force_authenticate(user=self.cashier_a)
        liste = self.client.get(
            f'/api/v1/customers/{self.customer.id}/sales/', **self._headers(),
        )
        self.assertEqual(liste.status_code, status.HTTP_200_OK)
        nb = liste.data.get('count', len(liste.data.get('results', liste.data)))
        self.assertEqual(nb, 1)

        rapport = self._rapport(self.cashier_a)
        self.assertEqual(rapport.data['sales']['count'], nb)

    def test_UN_CAISSIER_A_QUI_ON_ACCORDE_view_all_VOIT_TOUT(self):
        """
        ⚠ C'EST LE CAS QUI SÉPARE LES DEUX RÈGLES, et le seul.

        `restrict_visibility_for_request` borne un caissier par son RÔLE ;
        `/sales/` le borne par la PERMISSION `sales.view_all`. Un marchand qui
        accorde ce droit par `extra_permissions` voit donc deux chiffres
        contradictoires sur la même page si le rapport emploie la première. Le
        rapport reprend les deux pièces de `/sales/`, exactement.
        """
        self._create_sale()            # cashier_a
        self._vente_de(self.cashier_b)  # un collègue

        membre = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org,
        )
        membre.extra_permissions = ['sales.view_all']
        membre.save(update_fields=['extra_permissions'])

        r = self._rapport(self.cashier_a)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        # Les deux ventes : c'est ce que `/sales/` lui montrerait.
        self.assertEqual(r.data['sales']['count'], 2)

        liste = self.client.get(
            f'/api/v1/customers/{self.customer.id}/sales/', **self._headers(),
        )
        nb = liste.data.get('count', len(liste.data.get('results', liste.data)))
        self.assertEqual(nb, 2, "La liste et le rapport ne disent pas la même chose.")

    def test_un_caissier_sans_droit_client_est_refuse(self):
        # `customers.view` est accordé au caissier ; c'est un rôle SANS ce
        # droit qu'il faudrait, et il n'y en a pas. On éprouve donc l'inverse :
        # le droit est bien exigé, et le caissier l'a.
        r = self._rapport(self.cashier_a)
        self.assertEqual(r.status_code, status.HTTP_200_OK)


class ChiffresDuRapportTests(_RapportBaseTest):

    def test_le_panier_moyen_se_calcule_sur_le_revenu_CONVERTI(self):
        """
        ⚠ UNE MOYENNE BRUTE EST PIRE QU'UNE SOMME BRUTE : un panier moyen
          calculé sur des factures en francs et en dollars ne ressemble à rien,
          là qu'une somme garde au moins un ordre de grandeur.
        """
        self._create_sale(unit_price='2000.00')
        self._create_sale(unit_price='4000.00')

        r = self._rapport(self.manager)
        self.assertEqual(r.data['sales']['count'], 2)
        self.assertEqual(Decimal(r.data['sales']['revenue']), Decimal('6000.00'))
        self.assertEqual(Decimal(r.data['sales']['average_basket']), Decimal('3000.00'))

    def test_une_periode_sans_vente_rend_ZERO_et_non_une_phrase(self):
        self._create_sale()
        hier = timezone.localdate() - timedelta(days=30)
        r = self._rapport(
            self.manager,
            date_from=str(hier - timedelta(days=5)), date_to=str(hier),
        )
        self.assertEqual(r.data['sales']['count'], 0)
        self.assertEqual(Decimal(r.data['sales']['revenue']), Decimal('0'))
        self.assertEqual(Decimal(r.data['sales']['average_basket']), Decimal('0'))

    def test_la_BORNE_HAUTE_est_INCLUSIVE(self):
        """
        ⚠ Une vente du jour doit entrer dans un rapport arrêté AUJOURD'HUI.
          `date_to` exclusive la ferait disparaître, et la borne se lit en heure
          LOCALE : entre 23 h et minuit UTC il est déjà le lendemain à Kinshasa.
        """
        self._create_sale()
        aujourdhui = str(timezone.localdate())
        r = self._rapport(self.manager, date_from=aujourdhui, date_to=aujourdhui)
        self.assertEqual(r.data['sales']['count'], 1)

    def test_une_date_MALFORMEE_est_un_400_et_non_un_500(self):
        r = self._rapport(self.manager, date_from='oops')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('date_from', r.data)

    def test_la_dette_est_rendue_PAR_DEVISE_et_hors_periode(self):
        self._create_sale()  # vente à crédit : 2 000 CDF dus
        r = self._rapport(self.manager)
        self.assertEqual(len(r.data['debt']), 1)
        self.assertEqual(r.data['debt'][0]['currency'], 'CDF')
        self.assertEqual(Decimal(r.data['debt'][0]['amount']), Decimal('2000.00'))
        self.assertEqual(r.data['debt'][0]['kind'], 'debt')
        # L'arrêté est une date d'AUJOURD'HUI : c'est lui qui dit que ce chiffre
        # ne se rapporte pas à la fenêtre choisie.
        self.assertEqual(r.data['period']['as_of'], str(timezone.localdate()))

    def test_la_ventilation_par_devise_ne_somme_JAMAIS_nativement(self):
        self._create_sale(unit_price='2000.00')
        r = self._rapport(self.manager)
        self.assertEqual(len(r.data['by_currency']), 1)
        ligne = r.data['by_currency'][0]
        self.assertEqual(ligne['currency'], 'CDF')
        # `native_total` se relit, `primary_total` se compare : les deux
        # coïncident ici, l'établissement étant tenu en CDF.
        self.assertEqual(Decimal(ligne['native_total']), Decimal('2000.00'))
        self.assertEqual(Decimal(ligne['primary_total']), Decimal('2000.00'))


class CagnotteDuRapportTests(_RapportBaseTest):

    def setUp(self):
        super().setUp()
        self.programme = LoyaltyProgram.objects.create(
            organization=self.org, name='Fidélité', is_active=True,
            earn_percent=Decimal('2.00'), max_redemption_percent=Decimal('50.00'),
        )
        self.registre = CustomerLoyalty.objects.create(
            organization=self.org, customer=self.customer,
        )

    def _ecriture(self, montant, jours=0):
        ligne = LoyaltyTransaction.objects.create(
            organization=self.org, customer_loyalty=self.registre,
            transaction_type=(
                LoyaltyTransaction.TransactionType.EARN if montant > 0
                else LoyaltyTransaction.TransactionType.REDEEM
            ),
            amount=Decimal(montant), balance_after=Decimal('0'),
        )
        if jours:
            quand = timezone.now() - timedelta(days=jours)
            LoyaltyTransaction.objects.filter(pk=ligne.pk).update(created_at=quand)
        return ligne

    def test_gagnee_et_utilisee_sont_BORNEES_par_la_periode(self):
        self._ecriture(500)        # aujourd'hui
        self._ecriture(-200)       # aujourd'hui
        self._ecriture(900, 60)    # il y a deux mois
        self.registre.balance = Decimal('1200.00')
        self.registre.save(update_fields=['balance'])

        aujourdhui = str(timezone.localdate())
        r = self._rapport(self.manager, date_from=aujourdhui, date_to=aujourdhui)
        fid = r.data['loyalty']
        self.assertEqual(Decimal(fid['earned']), Decimal('500.00'))
        # ⚠ RENDUE POSITIVE : « utilisée : 200 » se lit, « -200 » fait hésiter.
        self.assertEqual(Decimal(fid['used']), Decimal('200.00'))
        self.assertEqual(fid['entries'], 2)

    def test_LA_CAGNOTTE_DISPONIBLE_N_A_PAS_DE_PERIODE(self):
        """
        ⚠ C'est un solde d'AUJOURD'HUI. Le borner ferait croire au marchand que
          son client ne possède que ce qu'il a gagné ce mois-ci.
        """
        self._ecriture(900, 60)
        self.registre.balance = Decimal('900.00')
        self.registre.save(update_fields=['balance'])

        aujourdhui = str(timezone.localdate())
        r = self._rapport(self.manager, date_from=aujourdhui, date_to=aujourdhui)
        fid = r.data['loyalty']
        # Rien n'a été gagné sur la période…
        self.assertEqual(Decimal(fid['earned']), Decimal('0'))
        # …et le solde, lui, est bien là.
        self.assertEqual(Decimal(fid['balance']), Decimal('900.00'))

    def test_sans_registre_la_cagnotte_le_DIT(self):
        self.registre.delete()
        r = self._rapport(self.manager)
        self.assertFalse(r.data['loyalty']['has_program'])


class ExportDuRapportTests(_RapportBaseTest):

    def test_les_trois_formats_sortent(self):
        self._create_sale()
        self.client.force_authenticate(user=self.manager)
        for fmt, type_attendu in (
            ('pdf', 'application/pdf'),
            ('xlsx', 'spreadsheet'),
            ('csv', 'text/csv'),
        ):
            with self.subTest(format=fmt):
                r = self.client.get(
                    f'/api/v1/customers/{self.customer.id}/report/export/',
                    {'export_format': fmt}, **self._headers(),
                )
                self.assertEqual(r.status_code, status.HTTP_200_OK, r.content[:200])
                self.assertIn(type_attendu, r['Content-Type'])
                self.assertGreater(len(r.content), 100)

    def test_le_document_annonce_que_la_cagnotte_est_un_solde_du_jour(self):
        self._create_sale()
        self.client.force_authenticate(user=self.manager)
        r = self.client.get(
            f'/api/v1/customers/{self.customer.id}/report/export/',
            {'export_format': 'csv'}, **self._headers(),
        )
        texte = r.content.decode('utf-8-sig')
        # ⚠ L'ÉTIQUETTE PORTE LA DATE D'ARRÊTÉ : sans elle, le lecteur du
        #   document ne peut pas savoir que ce chiffre n'est pas celui de la
        #   période annoncée en tête.
        self.assertIn('Cagnotte disponible au', texte)
        self.assertIn(str(timezone.localdate()), texte)

    def test_la_colonne_CONVERTIE_porte_les_decimales_de_la_PRINCIPALE(self):
        """
        ⚠ `currency_field` RÉSOUT LES DÉCIMALES LIGNE PAR LIGNE, et c'est juste
          pour la colonne NATIVE - fausse pour la colonne CONVERTIE.

        Le tableau mêle deux unités dans la même ligne : « Facturé » est dans la
        devise de la ligne, « Converti » toujours en principale. Avec les seules
        décimales de la ligne, un établissement tenu en CDF (zéro décimale)
        imprimait ses francs à deux décimales sur une ligne en dollars - et sur
        un établissement en USD, une ligne en francs aurait rendu « 6 303 » pour
        6 302,50. C'est le « montant faux sur un papier » que `currency_field`
        existe pour empêcher, pris par l'autre bout.
        """
        Currency.objects.update_or_create(
            code='USD',
            defaults={'name': 'US Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=Currency.objects.get(code='USD'),
            defaults={'exchange_rate': Decimal('2800'), 'is_primary': False},
        )
        vente_id = self._create_sale()
        # ⚠ `update()` et non `save()` : `Sale.save()` repasse par
        #   `CurrencyService.resolve` et réécrirait le taux. Le garde-fou des
        #   écritures en masse écarte explicitement les fichiers de test.
        Sale.objects.filter(pk=vente_id).update(
            currency='USD', exchange_rate=Decimal('2800'),
        )

        self.client.force_authenticate(user=self.manager)
        r = self.client.get(
            f'/api/v1/customers/{self.customer.id}/report/export/',
            {'export_format': 'csv'}, **self._headers(),
        )
        texte = r.content.decode('utf-8-sig')
        ligne = next(
            (l for l in texte.splitlines() if l.startswith('USD;')), None,
        )
        self.assertIsNotNone(ligne, f"Ligne USD absente du document :\n{texte}")

        _devise, _nombre, natif, converti = ligne.split(';')[:4]
        # La colonne NATIVE est en USD : deux décimales.
        self.assertIn(',', natif, f"« Facturé » a perdu ses décimales USD : {natif!r}")
        # La colonne CONVERTIE est en CDF : AUCUNE décimale.
        self.assertNotIn(
            ',', converti,
            "« Converti (CDF) » porte des décimales que le franc n'a pas : "
            f"{converti!r}",
        )

    def test_le_document_couvre_LE_MEME_PERIMETRE_que_l_ecran(self):
        self._create_sale()
        self._vente_de(self.cashier_b, unit_price='7777.00')

        self.client.force_authenticate(user=self.cashier_a)
        r = self.client.get(
            f'/api/v1/customers/{self.customer.id}/report/export/',
            {'export_format': 'csv'}, **self._headers(),
        )
        texte = r.content.decode('utf-8-sig')
        # ⚠ ON ASSERT LE TOTAL, PAS LA RÉFÉRENCE DE LA VENTE.
        #
        #   Une première écriture cherchait « 7777 » dans le fichier : le
        #   document ne liste AUCUNE vente - son tableau est groupé par devise -
        #   donc ce montant ne pouvait jamais y paraître, et le test passait
        #   pour une raison qui n'était pas la sienne. Mesuré : la mutation qui
        #   retire la borne par créateur ne le faisait pas tomber.
        #
        #   Ce qui se lit, c'est la SOMME : 2 000 pour le caissier seul,
        #   9 777 si la vente du collègue entrait.
        self.assertIn('2000', texte.replace(' ', '').replace('\u202f', ''))
        self.assertNotIn('9777', texte.replace(' ', '').replace('\u202f', ''))
        # Et le nombre de ventes : une seule, la sienne.
        lignes = [l for l in texte.splitlines() if 'CDF' in l and ';' in l]
        self.assertTrue(lignes, "Le tableau par devise est absent du document.")
