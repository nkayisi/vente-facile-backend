"""
Le filtre volontaire des rapports : il se superpose au rôle, il ne le perce pas.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DEUX CHOSES DIFFÉRENTES, ET L'ORDRE COMPTE.                                 │
│                                                                              │
│ Le périmètre du RÔLE est une borne : il n'est pas négociable. Le filtre      │
│ VOLONTAIRE est un choix : « montre-moi le dépôt B ». Appliquer le second     │
│ avant le premier, ou sur un queryset non borné, ferait lire à un caissier    │
│ la journée de son collègue - il suffirait d'en deviner l'identifiant.        │
└──────────────────────────────────────────────────────────────────────────────┘

Les tests s'authentifient en GÉRANT et non en propriétaire : `_scope_sales`
sort en amont pour un propriétaire (`accessible_warehouse_ids` rend `None`), et
la moitié du code de périmètre ne serait donc jamais exécutée.
"""
from decimal import Decimal


from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users, make_user


def _vente(org, warehouse, vendeur, montant, reference):
    return Sale.objects.create(
        organization=org, warehouse=warehouse, sold_by=vendeur,
        reference=reference, status=Sale.Status.COMPLETED,
        subtotal=Decimal(montant), total=Decimal(montant),
        amount_paid=Decimal(montant), currency='CDF', exchange_rate=Decimal('1'),
    )


class PerimetreVolontaireTests(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_a = self.d['warehouse']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt B', code='WH-B',
        )
        # Le gérant reçoit les DEUX dépôts : sans cela, le filtre volontaire
        # n'aurait rien à restreindre et le test passerait pour la mauvaise
        # raison.
        m = OrganizationMembership.objects.get(
            user=self.d['manager'], organization=self.org
        )
        m.assigned_warehouses.add(self.wh_b)

        _vente(self.org, self.wh_a, self.d['cashier_a'], '100', 'VT-A-1')
        _vente(self.org, self.wh_b, self.d['cashier_b'], '500', 'VT-B-1')
        # Une vente ANCIENNE, sans entrepôt : le périmètre du rôle la tolère,
        # le filtre volontaire ne doit PAS en hériter.
        _vente(self.org, None, self.d['cashier_a'], '7', 'VT-LEGACY')

        self.url = '/api/v1/reports/statistics/sales/'

    def _ca(self, qui, **params):
        self.client.force_authenticate(user=qui)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        reponse = self.client.get(self.url, {'period': 'last_30_days', **params})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        return Decimal(str(reponse.data['total_sales']))

    def test_sans_filtre_le_gerant_lit_ses_deux_depots_et_la_vente_ancienne(self):
        self.assertEqual(self._ca(self.d['manager']), Decimal('607.00'))

    def test_le_filtre_entrepot_restreint(self):
        self.assertEqual(
            self._ca(self.d['manager'], warehouse=str(self.wh_b.id)),
            Decimal('500.00'),
        )

    def test_le_filtre_volontaire_n_herite_pas_du_tolerant_null(self):
        """
        Σ(entrepôts) ne doit jamais dépasser le total. Une vente sans entrepôt
        qui compterait dans CHAQUE dépôt ferait exactement cela, et rien à
        l'écran ne l'expliquerait.
        """
        a = self._ca(self.d['manager'], warehouse=str(self.wh_a.id))
        b = self._ca(self.d['manager'], warehouse=str(self.wh_b.id))
        total = self._ca(self.d['manager'])
        self.assertEqual(a, Decimal('100.00'))
        self.assertEqual(b, Decimal('500.00'))
        self.assertLessEqual(a + b, total)

    def test_le_filtre_utilisateur_restreint(self):
        self.assertEqual(
            self._ca(self.d['manager'], user=str(self.d['cashier_b'].id)),
            Decimal('500.00'),
        )

    def test_un_entrepot_hors_perimetre_est_refuse_et_non_ignore(self):
        """
        ⚠ On regarde en GÉRANT, pas en caissier : `reports.view` n'est accordé
        qu'au propriétaire et au gérant, si bien qu'un caissier reçoit 403 bien
        avant d'atteindre le filtre. Écrit avec lui, ce test aurait constaté un
        refus qui n'a rien à voir avec le périmètre.
        """
        autre = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt C', code='WH-C',
        )
        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        reponse = self.client.get(
            self.url, {'period': 'last_30_days', 'warehouse': str(autre.id)}
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('warehouse', reponse.data)

    def test_viser_le_PROPRIETAIRE_ne_perce_pas_le_perimetre(self):
        """
        ┌──────────────────────────────────────────────────────────────────┐
        │ C'EST LA COMPOSITION QUI PROTÈGE, PAS LE REFUS.                  │
        │                                                                  │
        │ Viser le propriétaire est désormais permis - il est « partout »  │
        │ par construction, et les deux clients le proposent. Ce qui       │
        │ garantit qu'aucune donnée ne fuit, c'est que `_vouloir` se pose   │
        │ TOUJOURS par-dessus le périmètre du rôle : le gérant ne lit que   │
        │ l'activité du propriétaire DANS SES dépôts.                      │
        │                                                                  │
        │ Le jour où un endpoint filtrerait par `user` sans composer un     │
        │ `_scope_*`, la garantie disparaîtrait sans que le refus, lui,     │
        │ ait bougé. C'est donc CELA qu'il faut épingler.                   │
        └──────────────────────────────────────────────────────────────────┘
        """
        hors = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt Z', code='WH-Z',
        )
        # Le propriétaire vend dans les deux dépôts du gérant, et dans un
        # troisième qui ne lui appartient pas.
        _vente(self.org, self.wh_a, self.d['owner'], '11', 'VT-OWN-A')
        _vente(self.org, hors, self.d['owner'], '9000', 'VT-OWN-Z')

        lu = self._ca(self.d['manager'], user=str(self.d['owner'].id))
        self.assertEqual(lu, Decimal('11.00'), "la vente hors périmètre a fuité")

    def test_un_gerant_ne_vise_pas_un_membre_hors_de_ses_entrepots(self):
        """Sans cette borne, un gérant lit l'activité d'une boutique qu'il ne dirige pas."""
        autre = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt C', code='WH-C',
        )
        etranger = make_user('etranger@vf.test', 'Etran', 'Ger')
        m = OrganizationMembership.objects.create(
            user=etranger, organization=self.org,
            role=OrganizationMembership.Role.CASHIER, is_active=True,
        )
        # ⚠ `set()` ET NON `add()` : un signal donne désormais l'entrepôt
        # principal à tout membre borné qui naît sans affectation. Ajouter
        # par-dessus laisserait ce caissier dans le dépôt du gérant, et le
        # test constaterait une autorisation au lieu d'un refus.
        m.assigned_warehouses.set([autre])

        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        reponse = self.client.get(
            self.url, {'period': 'last_30_days', 'user': str(etranger.id)}
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('user', reponse.data)

    def test_se_viser_soi_meme_est_toujours_licite(self):
        """Et ne change rien au résultat : c'est déjà le périmètre du demandeur."""
        sans = self._ca(self.d['manager'])
        avec = self._ca(self.d['manager'], user=str(self.d['manager'].id))
        self.assertEqual(avec, Decimal('0.00'))
        self.assertGreater(sans, avec)

    def test_le_stock_ignore_l_utilisateur_au_lieu_de_rendre_zero(self):
        """
        Un onglet qui affiche zéro parce qu'un filtre d'un autre onglet a traîné
        se lit comme une perte de données.
        """
        url = '/api/v1/reports/statistics/stock/'
        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        sans = self.client.get(url)
        avec = self.client.get(url, {'user': str(self.d['cashier_a'].id)})
        self.assertEqual(sans.status_code, status.HTTP_200_OK)
        self.assertEqual(avec.status_code, status.HTTP_200_OK)
        self.assertEqual(sans.data, avec.data)

    def test_le_document_porte_le_meme_perimetre_que_l_ecran(self):
        """
        C'est la raison de mettre le filtre dans les `_scope_*` plutôt que dans
        chaque action : autrement l'écran et son fichier décrivent deux
        périmètres, et chacun est cohérent avec lui-même.

        ⚠ On compare des MONTANTS, pas la présence d'une référence. Une première
        version cherchait « VT-A-1 » dans le CSV de l'onglet `sales`, qui
        n'énumère aucune vente : elle passait sur le code d'origine, donc elle
        ne démontrait rien.
        """
        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        params = {
            'period': 'last_30_days',
            'warehouse': str(self.wh_b.id),
            'tab': 'overview',
            'export_format': 'csv',
        }
        doc = self.client.get('/api/v1/reports/statistics/export/', params)
        self.assertEqual(doc.status_code, status.HTTP_200_OK)
        contenu = (
            b''.join(doc.streaming_content)
            if getattr(doc, 'streaming', False)
            else doc.content
        ).decode('utf-8', 'replace')
        # 500 est le total du dépôt B. 607 serait celui de tout le périmètre du
        # gérant : le voir ici voudrait dire que le document ignore le filtre.
        self.assertIn('500', contenu)
        self.assertNotIn('607', contenu)
