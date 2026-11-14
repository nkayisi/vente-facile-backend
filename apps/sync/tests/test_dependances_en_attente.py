"""
Une dépendance qui attend n'est pas une dépendance refusée.

Le cas qui l'a fait écrire : un caissier crée un client depuis la feuille
d'encaissement, HORS LIGNE, et encaisse aussitôt une vente à son nom. Les deux
actes partent ensemble, la vente dépendant de la création. Si la création est
temporisée (`retry`) ou retenue (`blocked`), rejouer la vente maintenant la
ferait refuser pour un client introuvable - et ce refus serait DÉFINITIF, sur
une vente dont le ticket est déjà sorti.

Le rôle est celui du COMPTOIR (caissier), jamais le propriétaire.
"""
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.db import OperationalError

from apps.contacts.models import Customer
from apps.sales.models import Sale
from apps.sync import operations
from apps.sync.models import SyncOperation

from .test_operations_parity import _OperationsBaseTest

CLIENT = '0c11e17e-0000-4000-8000-000000000001'
CREATION = CLIENT  # le terminal emploie le même identifiant pour l'acte et le client
VENTE = '0c11e17e-0000-4000-8000-000000000002'


class DependancesEnAttenteTests(_OperationsBaseTest):
    def _creation(self):
        return self._op(
            'customer.create',
            {
                'id': CLIENT,
                'name': 'Client du comptoir',
                'phone': '0811111111',
                'customer_type': 'individual',
                'company_name': '',
                'tax_id': '',
                'email': '',
                'address': '',
                'allow_credit': True,
                'credit_limit': '0',
                'notes': '',
                'is_active': True,
            },
            CREATION,
            seq=1,
        )

    def _vente(self):
        return self._op(
            'sale.create',
            self._cart(customer=CLIENT),
            VENTE,
            seq=2,
            depends_on=[CREATION],
        )

    def test_le_client_cree_au_comptoir_et_sa_vente_passent_ensemble(self):
        resp = self._send([self._creation(), self._vente()])
        verdicts = [r['verdict'] for r in resp.data['results']]
        self.assertEqual(verdicts, ['applied', 'applied'])

        client = Customer.objects.get(pk=CLIENT)
        self.assertEqual(client.name, 'Client du comptoir')
        self.assertEqual(Sale.objects.filter(customer=client).count(), 1)

    def test_une_creation_temporisee_temporise_la_vente_sans_la_refuser(self):
        def temporise(ctx, payload):
            raise OperationalError('verrou')

        with patch.dict(operations.HANDLERS, {'customer.create': temporise}):
            resp = self._send([self._creation(), self._vente()])

        creation, vente = resp.data['results']
        self.assertEqual(creation['verdict'], 'retry')
        self.assertEqual(vente['verdict'], 'retry')
        self.assertEqual(vente['errors']['code'], 'dependency_pending')
        # Aucune trace : rien n'a été tenté, et le renvoi doit rester possible.
        self.assertFalse(SyncOperation.objects.filter(pk=VENTE).exists())
        self.assertFalse(Sale.objects.filter(customer_id=CLIENT).exists())

        # Au renvoi, la création passe et la vente avec elle.
        resp = self._send([self._creation(), self._vente()])
        self.assertEqual([r['verdict'] for r in resp.data['results']], ['applied', 'applied'])

    def test_une_creation_bloquee_retient_la_vente_au_lieu_de_la_refuser(self):
        def bloque(ctx, payload):
            raise PermissionDenied('droit manquant')

        with patch.dict(operations.HANDLERS, {'customer.create': bloque}):
            resp = self._send([self._creation(), self._vente()])

        creation, vente = resp.data['results']
        self.assertEqual(creation['verdict'], 'blocked')
        self.assertEqual(vente['verdict'], 'blocked')
        self.assertFalse(
            SyncOperation.objects.filter(pk=VENTE).exists(),
            "une vente retenue n'a pas été tentée : elle ne doit laisser aucune trace",
        )

    def test_une_dependance_refusee_condamne_toujours_ce_qui_en_depend(self):
        # Le contrôle : seul le REFUS se propage en refus.
        creation = self._creation()
        creation['payload']['name'] = ''  # le serveur exige un nom
        resp = self._send([creation, self._vente()])

        premiere, vente = resp.data['results']
        self.assertEqual(premiere['verdict'], 'rejected')
        self.assertEqual(vente['verdict'], 'rejected')
        self.assertEqual(vente['errors']['code'], 'dependency_rejected')
