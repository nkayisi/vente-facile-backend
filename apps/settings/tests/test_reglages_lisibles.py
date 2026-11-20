"""
Les réglages de ticket se LISENT par tout membre, ils ne s'écrivent que par le
propriétaire.

C'est le caissier qui imprime. Réservée à `settings.view`, la lecture lui
répondait 403 : ses tickets du back-office sortaient sans l'en-tête ni le pied
du marchand, en 58 mm par défaut, et sans bloc de cagnotte (vu au navigateur le
2026-11-20). Rôle BORNÉ : le caissier.
"""
from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import OrganizationSettings

URL = '/api/v1/settings/organization-settings/'


class ReglagesLisiblesTests(APITestCase):
    def setUp(self):
        self.__dict__.update(make_org_with_users())
        OrganizationSettings.objects.update_or_create(
            organization=self.org,
            defaults={'receipt_header': 'Merci de votre visite', 'receipt_paper_width': 80},
        )
        self.entete = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        self.client.force_authenticate(user=self.cashier_a)

    def test_le_caissier_lit_les_reglages_de_ticket(self):
        rep = self.client.get(URL, **self.entete)
        self.assertEqual(rep.status_code, status.HTTP_200_OK, rep.content)
        self.assertEqual(rep.data['receipt_header'], 'Merci de votre visite')
        self.assertEqual(rep.data['receipt_paper_width'], 80)

    def test_le_caissier_ne_les_modifie_pas(self):
        reglages = OrganizationSettings.objects.get(organization=self.org)
        rep = self.client.patch(
            f'{URL}{reglages.id}/', {'receipt_header': 'Pirate'}, format='json', **self.entete,
        )
        self.assertEqual(rep.status_code, status.HTTP_403_FORBIDDEN, rep.content)
        rep = self.client.post(URL, {'receipt_header': 'Pirate'}, format='json', **self.entete)
        self.assertEqual(rep.status_code, status.HTTP_403_FORBIDDEN, rep.content)
        reglages.refresh_from_db()
        self.assertEqual(reglages.receipt_header, 'Merci de votre visite')
