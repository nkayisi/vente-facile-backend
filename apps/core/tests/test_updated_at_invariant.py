"""
`updated_at` avance à CHAQUE écriture, `update_fields` compris.

Ce n'est pas un test de confort : le tirage pagine sur un curseur
``(updated_at, id)``. Une ligne dont l'horodatage ne bouge pas est invisible au
tirage, définitivement, sur tous les terminaux à la fois.

Le défaut a été constaté en production de développement : `CustomerBalance`
portait un `updated_at` figé depuis deux semaines pendant que son montant
changeait à chaque règlement. Le terminal affichait donc la dette du client
telle qu'elle était le jour où la ligne a été créée - et un caissier décide
d'accorder du crédit sur ce chiffre.

Django n'ajoute pas les champs `auto_now` à `update_fields` (mesuré sur 5.2).
Le correctif vit dans `TimeStampedModel.save`, donc à UN seul endroit : le
corriger appel par appel laisserait le treizième à écrire.
"""
from decimal import Decimal

from django.test import TestCase

from apps.contacts.models import Customer, CustomerBalance
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users


class UpdatedAtSuitToujoursTests(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

    def _bouge(self, instance, **ecriture):
        """Écrit par `update_fields` et rend (avant, après)."""
        avant = instance.updated_at
        for champ, valeur in ecriture.items():
            setattr(instance, champ, valeur)
        instance.save(update_fields=list(ecriture))
        instance.refresh_from_db()
        return avant, instance.updated_at

    def test_a_balance_written_by_update_fields_becomes_visible_to_the_pull(self):
        customer = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='09',
        )
        solde = CustomerBalance.objects.create(
            organization=self.org, customer=customer,
            currency='CDF', amount=Decimal('0.00'),
        )
        avant, apres = self._bouge(solde, amount=Decimal('5000.00'))
        self.assertGreater(apres, avant)

    def test_a_stock_written_by_update_fields_becomes_visible_to_the_pull(self):
        """
        Le cas le plus coûteux : un stock invisible au tirage fait vendre le
        terminal sur des quantités périmées.
        """
        produit = Product.objects.create(
            organization=self.org, name='Article', slug='a', sku='A1',
            selling_price=Decimal('100'), cost_price=Decimal('80'),
        )
        stock = Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal('10.000'),
        )
        avant, apres = self._bouge(stock, quantity=Decimal('7.000'))
        self.assertGreater(apres, avant)

    def test_created_at_never_moves(self):
        """`auto_now_add` ne doit PAS être entraîné : une date de création
        qui avance ferait remonter les lignes anciennes dans tous les tris."""
        customer = Customer.objects.create(
            organization=self.org, name='Client', code='C2', phone='09',
        )
        origine = customer.created_at
        customer.name = 'Client renommé'
        customer.save(update_fields=['name'])
        customer.refresh_from_db()
        self.assertEqual(customer.created_at, origine)

    def test_a_full_save_still_moves_updated_at(self):
        """Le chemin ordinaire n'est pas cassé par le correctif."""
        customer = Customer.objects.create(
            organization=self.org, name='Client', code='C3', phone='09',
        )
        avant = customer.updated_at
        customer.name = 'Autre'
        customer.save()
        customer.refresh_from_db()
        self.assertGreater(customer.updated_at, avant)

    def test_the_caller_list_is_not_mutated(self):
        """
        `update_fields` peut être une liste que l'appelant réutilise. La muter
        lui ferait écrire un champ de plus au tour suivant, sans qu'il le sache.
        """
        customer = Customer.objects.create(
            organization=self.org, name='Client', code='C4', phone='09',
        )
        champs = ['name']
        customer.name = 'Encore un autre'
        customer.save(update_fields=champs)
        self.assertEqual(champs, ['name'])
