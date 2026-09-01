"""
Parité de l'OUVERTURE d'une session de caisse.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE FONDS D'OUVERTURE EST HÉRITÉ PAR DEVISE, ET LE JOURNAL NE LE FAISAIT PAS.│
│                                                                              │
│ Le handler créait la session à la main, avec le seul `opening_balance`       │
│ scalaire et AUCUNE ligne `RegisterSessionCurrencyBalance`. Conséquence à la  │
│ clôture : `close_register_session` retombe sur                               │
│ `{devise principale: opening_balance}`, donc le tiroir en dollars part de    │
│ zéro et le Z annonce un écart tous les soirs - sur une caisse ouverte depuis │
│ un terminal, et seulement celle-là.                                          │
│                                                                              │
│ Le périmètre entrepôt n'était pas vérifié non plus : un caissier pouvait     │
│ ouvrir la caisse d'un dépôt qui ne lui est pas assigné.                      │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`** : c'est le caissier qui ouvre un tiroir.
"""
from decimal import Decimal
from uuid import uuid4

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.sales.models import Register, RegisterSession, RegisterSessionCurrencyBalance
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency

OPERATIONS = '/api/v1/sync/operations/'


class OuvertureParityTests(APITestCase):
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

        # Une caisse d'un entrepôt NON assigné au caissier.
        self.depot_ferme = Warehouse.objects.create(
            organization=self.org, name='Dépôt 2', code='D2',
        )
        self.caisse_fermee = Register.objects.create(
            organization=self.org, branch=self.branch, warehouse=self.depot_ferme,
            name='Caisse 2', code='C2',
        )
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, payload, op_id):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': 'register_session.open', 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-08-31T09:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _cloturer_avec_deux_devises(self, session):
        """Ferme la session en laissant un fonds compté dans les DEUX devises."""
        RegisterSessionCurrencyBalance.objects.filter(session=session).delete()
        for devise, montant in (('CDF', Decimal('50000.00')), ('USD', Decimal('120.00'))):
            RegisterSessionCurrencyBalance.objects.create(
                organization=self.org, session=session, currency=devise,
                opening_balance=Decimal('0.00'),
                expected_balance=montant, counted_balance=montant,
            )
        session.status = 'closed'
        session.save(update_fields=['status'])

    def test_le_journal_herite_le_fonds_PAR_DEVISE_comme_la_vue(self):
        precedente = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self._cloturer_avec_deux_devises(precedente)

        op = 'f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1'
        reponse = self._journal({'id': op, 'register': str(self.register.id)}, op)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))

        session = RegisterSession.objects.get(id=op)
        fonds = {
            cb.currency: cb.opening_balance
            for cb in session.currency_balances.all()
        }
        self.assertEqual(
            fonds, {'CDF': Decimal('50000.00'), 'USD': Decimal('120.00')},
            "Le fonds d'ouverture n'a pas été hérité par devise : le Z du soir "
            "annoncera un écart sur toute devise secondaire.",
        )
        self.assertEqual(session.opening_balance, Decimal('50000.00'))

    def test_le_journal_et_la_vue_ouvrent_A_L_IDENTIQUE(self):
        precedente = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self._cloturer_avec_deux_devises(precedente)

        # Par la vue.
        vue = self.client.post(
            '/api/v1/register-sessions/open/',
            {'register': str(self.register.id)}, format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_201_CREATED, vue.data)
        par_la_vue = RegisterSession.objects.get(id=vue.data['id'])
        attendu = {
            cb.currency: cb.opening_balance for cb in par_la_vue.currency_balances.all()
        }

        # On la ferme pour libérer la caisse, sans toucher aux fonds.
        self._cloturer_avec_deux_devises(par_la_vue)

        op = 'f2f2f2f2-f2f2-4f2f-8f2f-f2f2f2f2f2f2'
        self.assertEqual(
            self._journal({'id': op, 'register': str(self.register.id)}, op)
            .data['results'][0]['verdict'],
            'applied',
        )
        par_le_journal = RegisterSession.objects.get(id=op)
        obtenu = {
            cb.currency: cb.opening_balance
            for cb in par_le_journal.currency_balances.all()
        }
        self.assertEqual(obtenu, attendu)

    def test_une_caisse_hors_perimetre_est_refusee_des_deux_cotes(self):
        vue = self.client.post(
            '/api/v1/register-sessions/open/',
            {'register': str(self.caisse_fermee.id)}, format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_404_NOT_FOUND, vue.data)

        op = 'f3f3f3f3-f3f3-4f3f-8f3f-f3f3f3f3f3f3'
        verdict = self._journal(
            {'id': op, 'register': str(self.caisse_fermee.id)}, op
        ).data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        self.assertEqual(
            RegisterSession.objects.filter(register=self.caisse_fermee).count(), 0,
        )

    def test_une_session_deja_ouverte_nomme_QUI_et_QUAND(self):
        """
        Le refus le plus coûteux d'une ouverture hors ligne : il emporte avec
        lui toutes les ventes qui s'y rattachaient. Le message doit permettre
        de comprendre, pas seulement de constater.
        """
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_b,
            opening_balance=Decimal('0'), status='open',
        )
        op = 'f4f4f4f4-f4f4-4f4f-8f4f-f4f4f4f4f4f4'
        verdict = self._journal(
            {'id': op, 'register': str(self.register.id)}, op
        ).data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        detail = verdict['errors']['detail']
        self.assertIn('Caisse 1', detail)
        self.assertIn(self.cashier_b.full_name, detail)

    def test_le_FONDS_COMPTE_par_le_caissier_arrive_au_serveur(self):
        """
        LE SCALAIRE `opening_balance` EST LE SEUL CHAMP QUE LE TERMINAL ENVOIE.

        Son écran d'ouverture ne demande qu'un montant (« Fond de caisse »), en
        devise principale. Le handler ne relayait que `opening_balances`, au
        pluriel : le fonds était jeté en silence, la session ouvrait à zéro, et
        le Z du soir annonçait un excédent EXACTEMENT égal au fonds - tous les
        soirs, sur toutes les caisses ouvertes depuis un terminal.
        """
        op = 'f6f6f6f6-f6f6-4f6f-8f6f-f6f6f6f6f6f6'
        reponse = self._journal(
            {'id': op, 'register': str(self.register.id), 'opening_balance': '75000'},
            op,
        )
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))

        session = RegisterSession.objects.get(id=op)
        self.assertEqual(
            session.opening_balance, Decimal('75000.00'),
            "Le fonds compté par le caissier n'est pas arrivé au serveur.",
        )
        self.assertEqual(
            {cb.currency: cb.opening_balance for cb in session.currency_balances.all()},
            {'CDF': Decimal('75000.00')},
            "Sans ligne par devise, la clôture repart du scalaire et l'écart "
            "réapparaît dès qu'une seconde devise entre au tiroir.",
        )

    def test_le_fonds_saisi_SURCHARGE_l_heritage_de_la_veille(self):
        """
        Le caissier qui compte son tiroir corrige ce que la veille a laissé.

        Sans surcharge, le scalaire serait ignoré dès qu'une session précédente
        existe - c'est-à-dire tous les jours sauf le premier, donc en pratique
        toujours.
        """
        precedente = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        self._cloturer_avec_deux_devises(precedente)

        op = 'f7f7f7f7-f7f7-4f7f-8f7f-f7f7f7f7f7f7'
        self.assertEqual(
            self._journal(
                {'id': op, 'register': str(self.register.id), 'opening_balance': '1200'},
                op,
            ).data['results'][0]['verdict'],
            'applied',
        )
        session = RegisterSession.objects.get(id=op)
        fonds = {cb.currency: cb.opening_balance for cb in session.currency_balances.all()}
        # La devise SECONDAIRE garde son héritage : le scalaire ne vise que la
        # principale, et l'appliquer partout écraserait un tiroir jamais compté.
        self.assertEqual(fonds, {'CDF': Decimal('1200.00'), 'USD': Decimal('120.00')})

    def test_la_ventilation_l_emporte_sur_le_scalaire(self):
        """Même ordre qu'à la clôture : `counted_balance` puis `counted_balances`."""
        op = 'f8f8f8f8-f8f8-4f8f-8f8f-f8f8f8f8f8f8'
        self.assertEqual(
            self._journal(
                {
                    'id': op, 'register': str(self.register.id),
                    'opening_balance': '999',
                    'opening_balances': [{'currency': 'CDF', 'amount': '4000'}],
                },
                op,
            ).data['results'][0]['verdict'],
            'applied',
        )
        session = RegisterSession.objects.get(id=op)
        self.assertEqual(session.opening_balance, Decimal('4000.00'))

    def test_la_vue_accepte_le_scalaire_COMME_le_journal(self):
        """
        Un seul corps, deux surfaces : le contrat d'entrée doit être le même.

        Le laisser au seul journal ferait diverger le jour où le back-office
        gagnerait un champ « fond de caisse » - et rien ne le signalerait, la
        valeur étant simplement ignorée par le serializer.
        """
        vue = self.client.post(
            '/api/v1/register-sessions/open/',
            {'register': str(self.register.id), 'opening_balance': '75000'},
            format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_201_CREATED, vue.data)
        session = RegisterSession.objects.get(id=vue.data['id'])
        self.assertEqual(session.opening_balance, Decimal('75000.00'))
        self.assertEqual(
            {cb.currency: cb.opening_balance for cb in session.currency_balances.all()},
            {'CDF': Decimal('75000.00')},
        )

    def test_le_rejeu_de_SA_PROPRE_ouverture_rend_un_succes(self):
        """Sans quoi un renvoi après coupure refuserait la session en cours."""
        op = 'f5f5f5f5-f5f5-4f5f-8f5f-f5f5f5f5f5f5'
        charge = {'id': op, 'register': str(self.register.id)}
        self.assertEqual(
            self._journal(charge, op).data['results'][0]['verdict'], 'applied',
        )
        # Même identifiant d'opération : `is_settled` rejoue le verdict.
        self.assertIn(
            self._journal(charge, op).data['results'][0]['verdict'],
            ('applied', 'duplicate'),
        )
        self.assertEqual(RegisterSession.objects.filter(id=op).count(), 1)


class ClotureParLeJournalTests(APITestCase):
    """
    Parité de la CLÔTURE, qui n'était couverte par aucun test.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ AUCUNE CLÔTURE VENUE D'UN TERMINAL N'A JAMAIS ABOUTI.                    │
    │                                                                          │
    │ `close_register_session` écrit une trace `UserActivity` et reçoit `ip` et │
    │ `agent` de la VUE, qui les tire de la requête HTTP. Le journal n'a pas de │
    │ requête de navigateur : le handler ne les passait pas, ils valaient donc  │
    │ `None`, et `user_agent` est une colonne NON NULLE. `IntegrityError`,      │
    │ verdict `rejected`, quarantaine.                                          │
    │                                                                          │
    │ RELEVÉ SUR L'ÉMULATEUR, pas déduit : Z imprimé sous son numéro définitif, │
    │ tiroir compté dans les deux devises, écran annonçant « la session se      │
    │ fermera à la prochaine synchronisation »... et le serveur refusant. Le    │
    │ caissier est rentré chez lui, la caisse est restée ouverte, et le Z qu'il │
    │ tient ne correspond à aucune session close.                               │
    │                                                                          │
    │ Le lot 3 avait pourtant écrit « LA CLÔTURE passe par le journal, et ce    │
    │ n'est pas un luxe ». Elle y passait, et échouait à chaque fois : ses      │
    │ tests ne couvraient que l'OUVERTURE.                                      │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        cdf = Currency.objects.get(code='CDF')
        OrganizationCurrency.objects.create(
            organization=self.org, currency=cdf, is_primary=True,
            exchange_rate=Decimal('1.000000'), is_active=True,
        )
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        # Rôle BORNÉ : c'est le caissier qui compte son tiroir et rentre.
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _cloturer_par_le_journal(self, op_id=None):
        # L'identifiant d'opération est un UUID : c'est lui qui porte
        # l'idempotence, et le serveur le lit comme tel.
        op_id = op_id or str(uuid4())
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': 'register_session.close', 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-08-31T20:00:00Z',
                'payload': {
                    'id': op_id,
                    'session': str(self.session.id),
                    'notes': '',
                    'counted_balances': [{'currency': 'CDF', 'amount': '0'}],
                },
            }]},
            format='json', **self._headers(),
        )

    def test_la_cloture_venue_du_journal_ABOUTIT(self):
        reponse = self._cloturer_par_le_journal()
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        verdict = reponse.data['results'][0]
        self.assertEqual(
            verdict['verdict'], 'applied',
            f"La clôture est refusée : {verdict.get('error')}",
        )

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'closed')
        self.assertEqual(self.session.closed_by_id, self.cashier_a.id)

    def test_la_trace_d_audit_est_ECRITE_sans_navigateur(self):
        """
        L'audit ne doit pas dépendre d'un en-tête HTTP.

        `user_agent` est une colonne non nulle : la valeur vide du modèle
        (`blank=True`) est la bonne réponse pour un acte qui n'a pas de
        navigateur. `ip_address`, lui, est nullable et reste nul.
        """
        from apps.users.models import UserActivity

        self._cloturer_par_le_journal()

        trace = UserActivity.objects.filter(
            resource_type='register_session', resource_id=str(self.session.id),
        ).first()
        self.assertIsNotNone(trace, "La clôture n'a laissé aucune trace d'audit.")
        self.assertEqual(trace.user_agent, '')
        self.assertIsNone(trace.ip_address)
        self.assertEqual(trace.details['event'], 'session_closed')

    def test_rejouer_la_MEME_cloture_reste_un_succes(self):
        """
        Le renvoi après coupure ne doit pas condamner la journée.

        `duplicate` est un succès : c'est ce qui rend le renvoi sûr.
        """
        meme_id = str(uuid4())
        self._cloturer_par_le_journal(meme_id)
        seconde = self._cloturer_par_le_journal(meme_id)

        self.assertIn(
            seconde.data['results'][0]['verdict'], ('applied', 'duplicate'),
            seconde.data['results'][0],
        )
