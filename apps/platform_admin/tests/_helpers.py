"""
Les comptes d'un test d'administration : un de chaque rôle, plus les deux cas
limites que la refonte devait trancher.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.platform_admin.models import PlatformRole
from apps.platform_admin.services import PlatformAdminService
from apps.sales.tests._helpers import make_org_with_users, make_user


class BaseAdminTest(APITestCase):
    def setUp(self):
        # Le throttle de DRF et le cache des réglages persistent d'un test à
        # l'autre en Redis : sans purge, un test lit ce qu'un autre a écrit.
        cache.clear()
        self.__dict__.update(make_org_with_users())

        # Le compte de démarrage : superutilisateur, sans profil.
        self.racine = make_user('racine@pa.test', 'Ra', 'Cine')
        self.racine.is_superuser = True
        self.racine.is_staff = True
        self.racine.save(update_fields=['is_superuser', 'is_staff', 'updated_at'])

        self.super_admin = make_user('super@pa.test', 'Su', 'Per')
        PlatformAdminService.grant(self.super_admin, PlatformRole.SUPER_ADMIN, granted_by=self.racine)
        self.support = make_user('support@pa.test', 'Sup', 'Port')
        PlatformAdminService.grant(self.support, PlatformRole.SUPPORT, granted_by=self.racine)

        # `is_staff` posé à la main, sans rôle : il ne doit plus ouvrir la porte.
        self.staff_nu = make_user('staffnu@pa.test', 'Staff', 'Nu')
        self.staff_nu.is_staff = True
        self.staff_nu.save(update_fields=['is_staff', 'updated_at'])

    def en(self, user):
        self.client.force_authenticate(user=user)
        return self.client
