"""
Tests for WatermelonDB sync API.
"""
import uuid
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from apps.organizations.models import Organization, OrganizationMembership
from apps.users.models import User
from apps.products.models import Category, Brand, Product
from apps.contacts.models import Customer
from apps.inventory.models import Warehouse, Stock, StockMovement
from apps.sales.models import PaymentMethod, Sale, SaleItem, Payment

from .utils import parse_timestamp, get_server_timestamp, datetime_to_ms
from .services import SyncPullService, SyncPushService, SyncService


class TimestampUtilsTests(TestCase):
    """Tests for timestamp utility functions."""
    
    def test_parse_timestamp_none(self):
        """None returns None (initial sync)."""
        self.assertIsNone(parse_timestamp(None))
    
    def test_parse_timestamp_zero(self):
        """Zero returns None (initial sync)."""
        self.assertIsNone(parse_timestamp(0))
        self.assertIsNone(parse_timestamp('0'))
    
    def test_parse_timestamp_iso_string(self):
        """ISO string is parsed correctly."""
        result = parse_timestamp('2024-01-15T10:30:00Z')
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)
    
    def test_parse_timestamp_milliseconds(self):
        """Unix timestamp in milliseconds is parsed correctly."""
        # 2024-01-15 10:30:00 UTC in milliseconds
        ms = 1705315800000
        result = parse_timestamp(ms)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)
    
    def test_parse_timestamp_seconds(self):
        """Unix timestamp in seconds is parsed correctly."""
        # 2024-01-15 10:30:00 UTC in seconds
        secs = 1705315800
        result = parse_timestamp(secs)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)
    
    def test_get_server_timestamp(self):
        """Server timestamp is returned in milliseconds."""
        ts = get_server_timestamp()
        self.assertIsInstance(ts, int)
        # Should be a reasonable timestamp (after 2020)
        self.assertGreater(ts, 1577836800000)
    
    def test_datetime_to_ms(self):
        """Datetime is converted to milliseconds correctly."""
        dt = timezone.now()
        ms = datetime_to_ms(dt)
        self.assertIsInstance(ms, int)
        # Round-trip should be close
        parsed = parse_timestamp(ms)
        self.assertAlmostEqual(dt.timestamp(), parsed.timestamp(), places=0)


class SyncPullServiceTests(TestCase):
    """Tests for SyncPullService."""
    
    @classmethod
    def setUpTestData(cls):
        """Set up test data."""
        cls.user = User.objects.create_user(
            email='test@example.com',
            password='testpass123',
            first_name='Test',
            last_name='User'
        )
        cls.organization = Organization.objects.create(
            name='Test Org',
            slug='test-org'
        )
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.organization,
            role='owner'
        )
        
        # Create some test data
        cls.category = Category.objects.create(
            organization=cls.organization,
            name='Test Category',
            slug='test-category'
        )
        cls.brand = Brand.objects.create(
            organization=cls.organization,
            name='Test Brand',
            slug='test-brand'
        )
        cls.product = Product.objects.create(
            organization=cls.organization,
            name='Test Product',
            slug='test-product',
            sku='TEST-001',
            category=cls.category,
            brand=cls.brand,
            selling_price=Decimal('1000.00')
        )
        cls.customer = Customer.objects.create(
            organization=cls.organization,
            code='CUST-001',
            name='Test Customer'
        )
    
    def test_initial_pull_returns_all_data(self):
        """Initial pull (last_pulled_at=None) returns all non-deleted records."""
        service = SyncPullService(self.organization, self.user)
        result = service.get_changes(None)
        
        self.assertIn('changes', result)
        self.assertIn('timestamp', result)
        self.assertIn('schema_version', result)
        
        # Check that data is returned
        self.assertGreater(len(result['changes']['categories']['created']), 0)
        self.assertGreater(len(result['changes']['products']['created']), 0)
        self.assertGreater(len(result['changes']['customers']['created']), 0)
        
        # Updated and deleted should be empty for initial sync
        self.assertEqual(len(result['changes']['categories']['updated']), 0)
        self.assertEqual(len(result['changes']['categories']['deleted']), 0)
    
    def test_delta_pull_returns_only_changes(self):
        """Delta pull returns only records changed since last_pulled_at."""
        # Get initial timestamp
        initial_ts = get_server_timestamp()
        
        # Create a new product after the timestamp
        import time
        time.sleep(0.1)  # Ensure timestamp difference
        
        new_product = Product.objects.create(
            organization=self.organization,
            name='New Product',
            slug='new-product',
            sku='NEW-001',
            selling_price=Decimal('500.00')
        )
        
        service = SyncPullService(self.organization, self.user)
        result = service.get_changes(initial_ts)
        
        # New product should be in created
        created_ids = [p['id'] for p in result['changes']['products']['created']]
        self.assertIn(str(new_product.id), created_ids)
        
        # Original product should not be in created (it was created before)
        self.assertNotIn(str(self.product.id), created_ids)
    
    def test_pull_specific_tables(self):
        """Pull can be filtered to specific tables."""
        service = SyncPullService(self.organization, self.user)
        result = service.get_changes(None, tables=['products', 'customers'])
        
        # Only requested tables should be present
        self.assertIn('products', result['changes'])
        self.assertIn('customers', result['changes'])
        
        # Other tables should not be present
        self.assertNotIn('categories', result['changes'])
        self.assertNotIn('brands', result['changes'])
    
    def test_deleted_records_returned_in_delta(self):
        """Soft-deleted records are returned in deleted array."""
        # Create a product specifically for this test
        product_to_delete = Product.objects.create(
            organization=self.organization,
            name='Product To Delete',
            slug='product-to-delete',
            sku='DEL-001',
            selling_price=Decimal('500.00')
        )
        
        # Get initial timestamp after product creation
        import time
        time.sleep(0.1)
        initial_ts = get_server_timestamp()
        time.sleep(0.1)
        
        # Soft delete the product
        product_to_delete.soft_delete()
        
        service = SyncPullService(self.organization, self.user)
        result = service.get_changes(initial_ts)
        
        # Product should be in deleted
        self.assertIn(str(product_to_delete.id), result['changes']['products']['deleted'])


class SyncPushServiceTests(TestCase):
    """Tests for SyncPushService."""
    
    @classmethod
    def setUpTestData(cls):
        """Set up test data."""
        cls.user = User.objects.create_user(
            email='push@example.com',
            password='testpass123',
            first_name='Push',
            last_name='User'
        )
        cls.organization = Organization.objects.create(
            name='Push Org',
            slug='push-org'
        )
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.organization,
            role='owner'
        )
        cls.warehouse = Warehouse.objects.create(
            organization=cls.organization,
            name='Main Warehouse',
            code='WH-001',
            is_default=True
        )
        cls.payment_method = PaymentMethod.objects.create(
            organization=cls.organization,
            name='Cash',
            code='CASH',
            method_type='cash',
            is_default=True
        )
    
    def test_push_create_customer(self):
        """Push can create new customer records."""
        service = SyncPushService(self.organization, self.user)
        
        customer_id = str(uuid.uuid4())
        changes = {
            'customers': {
                'created': [
                    {
                        'id': customer_id,
                        'code': 'PUSH-001',
                        'name': 'Pushed Customer',
                        'customer_type': 'individual',
                        'phone': '+243123456789',
                        'credit_limit': '0.00',
                        'current_balance': '0.00',
                    }
                ],
                'updated': [],
                'deleted': [],
            }
        }
        
        result = service.apply_changes(changes, get_server_timestamp())
        
        self.assertTrue(result['success'])
        self.assertEqual(result['stats']['created'], 1)
        
        # Verify customer was created
        customer = Customer.objects.get(id=customer_id)
        self.assertEqual(customer.name, 'Pushed Customer')
        self.assertEqual(customer.organization, self.organization)
    
    def test_push_update_customer(self):
        """Push can update existing customer records."""
        # Create a customer first
        customer = Customer.objects.create(
            organization=self.organization,
            code='UPD-001',
            name='Original Name'
        )
        
        service = SyncPushService(self.organization, self.user)
        
        changes = {
            'customers': {
                'created': [],
                'updated': [
                    {
                        'id': str(customer.id),
                        'name': 'Updated Name',
                        'phone': '+243999999999',
                    }
                ],
                'deleted': [],
            }
        }
        
        result = service.apply_changes(changes, get_server_timestamp())
        
        self.assertTrue(result['success'])
        self.assertEqual(result['stats']['updated'], 1)
        
        # Verify customer was updated
        customer.refresh_from_db()
        self.assertEqual(customer.name, 'Updated Name')
        self.assertEqual(customer.phone, '+243999999999')
    
    def test_push_delete_customer(self):
        """Push can soft-delete customer records."""
        # Create a customer first
        customer = Customer.objects.create(
            organization=self.organization,
            code='DEL-001',
            name='To Delete'
        )
        
        service = SyncPushService(self.organization, self.user)
        
        changes = {
            'customers': {
                'created': [],
                'updated': [],
                'deleted': [str(customer.id)],
            }
        }
        
        result = service.apply_changes(changes, get_server_timestamp())
        
        self.assertTrue(result['success'])
        self.assertEqual(result['stats']['deleted'], 1)
        
        # Verify customer was soft-deleted
        customer.refresh_from_db()
        self.assertTrue(customer.is_deleted)
        self.assertIsNotNone(customer.deleted_at)
    
    def test_push_idempotent_create(self):
        """Creating the same record twice doesn't create duplicates."""
        service = SyncPushService(self.organization, self.user)
        
        customer_id = str(uuid.uuid4())
        changes = {
            'customers': {
                'created': [
                    {
                        'id': customer_id,
                        'code': 'IDEM-001',
                        'name': 'Idempotent Customer',
                        'customer_type': 'individual',
                    }
                ],
                'updated': [],
                'deleted': [],
            }
        }
        
        # Push twice
        result1 = service.apply_changes(changes, get_server_timestamp())
        
        service2 = SyncPushService(self.organization, self.user)
        result2 = service2.apply_changes(changes, get_server_timestamp())
        
        # Both should succeed
        self.assertTrue(result1['success'])
        self.assertTrue(result2['success'])
        
        # Only one customer should exist
        count = Customer.objects.filter(id=customer_id).count()
        self.assertEqual(count, 1)
    
    def test_push_read_only_tables_rejected(self):
        """Push to read-only tables is skipped."""
        service = SyncPushService(self.organization, self.user)
        
        changes = {
            'categories': {
                'created': [
                    {
                        'id': str(uuid.uuid4()),
                        'name': 'Should Not Create',
                        'slug': 'should-not-create',
                    }
                ],
                'updated': [],
                'deleted': [],
            }
        }
        
        result = service.apply_changes(changes, get_server_timestamp())
        
        # Should succeed but with no creates
        self.assertTrue(result['success'])
        self.assertEqual(result['stats']['created'], 0)


class SyncAPITests(APITestCase):
    """Tests for sync API endpoints."""
    
    @classmethod
    def setUpTestData(cls):
        """Set up test data."""
        cls.user = User.objects.create_user(
            email='api@example.com',
            password='testpass123',
            first_name='API',
            last_name='User'
        )
        cls.organization = Organization.objects.create(
            name='API Org',
            slug='api-org'
        )
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.organization,
            role='owner'
        )
        
        # Create some test data
        cls.customer = Customer.objects.create(
            organization=cls.organization,
            code='API-001',
            name='API Customer'
        )
    
    def setUp(self):
        """Set up each test."""
        self.client.force_authenticate(user=self.user)
    
    def test_pull_requires_auth(self):
        """Pull endpoint requires authentication."""
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/v1/sync/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
    
    def test_pull_requires_organization(self):
        """Pull endpoint requires organization header (403 si absent ou non-membre)."""
        response = self.client.get('/api/v1/sync/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_pull_initial_sync(self):
        """Initial sync returns all data."""
        response = self.client.get(
            '/api/v1/sync/',
            HTTP_X_ORGANIZATION_ID=str(self.organization.id)
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('changes', response.data)
        self.assertIn('timestamp', response.data)
        self.assertIn('customers', response.data['changes'])
    
    def test_pull_delta_sync(self):
        """Delta sync with timestamp."""
        response = self.client.get(
            '/api/v1/sync/',
            {'last_pulled_at': '0'},
            HTTP_X_ORGANIZATION_ID=str(self.organization.id)
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
    
    def test_pull_specific_tables(self):
        """Pull specific tables only."""
        response = self.client.get(
            '/api/v1/sync/',
            {'tables': 'customers,products'},
            HTTP_X_ORGANIZATION_ID=str(self.organization.id)
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('customers', response.data['changes'])
        self.assertIn('products', response.data['changes'])
    
    def test_push_creates_records(self):
        """Push endpoint creates new records."""
        customer_id = str(uuid.uuid4())
        
        response = self.client.post(
            '/api/v1/sync/',
            {
                'changes': {
                    'customers': {
                        'created': [
                            {
                                'id': customer_id,
                                'code': 'PUSH-API-001',
                                'name': 'API Pushed Customer',
                                'customer_type': 'individual',
                            }
                        ],
                        'updated': [],
                        'deleted': [],
                    }
                },
                'last_pulled_at': get_server_timestamp(),
            },
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.organization.id)
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['push']['success'])
        self.assertEqual(response.data['push']['stats']['created'], 1)
        
        # Verify customer exists
        self.assertTrue(Customer.objects.filter(id=customer_id).exists())
    
    def test_sync_status_endpoint(self):
        """Sync status endpoint returns table info."""
        response = self.client.get(
            '/api/v1/sync/status/',
            HTTP_X_ORGANIZATION_ID=str(self.organization.id)
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('tables', response.data)
        self.assertIn('schema_version', response.data)
        self.assertIn('customers', response.data['tables'])


class MultiTenantSyncTests(TestCase):
    """Tests for multi-tenant isolation in sync."""
    
    @classmethod
    def setUpTestData(cls):
        """Set up test data for two organizations."""
        cls.user1 = User.objects.create_user(
            email='user1@example.com',
            password='testpass123'
        )
        cls.user2 = User.objects.create_user(
            email='user2@example.com',
            password='testpass123'
        )
        
        cls.org1 = Organization.objects.create(
            name='Org 1',
            slug='org-1'
        )
        cls.org2 = Organization.objects.create(
            name='Org 2',
            slug='org-2'
        )
        
        OrganizationMembership.objects.create(
            user=cls.user1,
            organization=cls.org1,
            role='owner'
        )
        OrganizationMembership.objects.create(
            user=cls.user2,
            organization=cls.org2,
            role='owner'
        )
        
        # Create customers in each org
        cls.customer1 = Customer.objects.create(
            organization=cls.org1,
            code='ORG1-001',
            name='Org 1 Customer'
        )
        cls.customer2 = Customer.objects.create(
            organization=cls.org2,
            code='ORG2-001',
            name='Org 2 Customer'
        )
    
    def test_pull_only_returns_own_org_data(self):
        """Pull only returns data from the requesting organization."""
        service = SyncPullService(self.org1, self.user1)
        result = service.get_changes(None)
        
        # Should contain org1's customer
        customer_ids = [c['id'] for c in result['changes']['customers']['created']]
        self.assertIn(str(self.customer1.id), customer_ids)
        
        # Should NOT contain org2's customer
        self.assertNotIn(str(self.customer2.id), customer_ids)
    
    def test_push_cannot_modify_other_org_data(self):
        """Push cannot modify data from another organization."""
        service = SyncPushService(self.org1, self.user1)
        
        # Try to update org2's customer from org1
        changes = {
            'customers': {
                'created': [],
                'updated': [
                    {
                        'id': str(self.customer2.id),
                        'name': 'Hacked Name',
                    }
                ],
                'deleted': [],
            }
        }
        
        result = service.apply_changes(changes, get_server_timestamp())
        
        # Should succeed (no error) but create instead of update
        # because the record doesn't exist in org1
        
        # Org2's customer should be unchanged
        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.name, 'Org 2 Customer')

    def test_push_create_with_cross_tenant_fk_is_rejected(self):
        """Push refusé si un FK pointe vers un objet d'une autre organisation."""
        from apps.products.models import Category

        # Catégorie créée dans org2 — on tente de la référencer depuis org1
        cat_other = Category.objects.create(
            organization=self.org2, name='Cat Org2', slug='cat-org2',
        )
        service = SyncPushService(self.org1, self.user1)

        changes = {
            'products': {
                'created': [
                    {
                        'id': str(uuid.uuid4()),
                        'name': 'Produit malveillant',
                        'sku': 'EVIL-001',
                        'category_id': str(cat_other.id),
                        'cost_price': '100.00',
                        'selling_price': '150.00',
                    }
                ],
                'updated': [],
                'deleted': [],
            }
        }

        result = service.apply_changes(changes, get_server_timestamp())

        # Doit avoir échoué (erreur enregistrée), pas créé
        self.assertEqual(result['stats']['created'], 0)
        self.assertEqual(result['stats']['errors'], 1)
        self.assertTrue(
            any('appartient pas' in e.get('error', '') for e in result['errors'])
        )


class SyncMembershipGuardTests(APITestCase):
    """Tests pour la vérification de la membership dans get_organization_from_request."""

    @classmethod
    def setUpTestData(cls):
        cls.outsider = User.objects.create_user(
            email='outsider@example.com', password='pw',
        )
        cls.member = User.objects.create_user(
            email='member@example.com', password='pw',
        )
        cls.org = Organization.objects.create(name='Closed Org', slug='closed-org')
        OrganizationMembership.objects.create(
            user=cls.member, organization=cls.org, role='owner'
        )

    def test_pull_rejected_for_non_member(self):
        """Un utilisateur authentifié mais non-membre reçoit 403 sur pull."""
        self.client.force_authenticate(user=self.outsider)
        response = self.client.get(
            '/api/v1/sync/',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pull_accepted_for_active_member(self):
        """Un membre actif accède bien à son organisation."""
        self.client.force_authenticate(user=self.member)
        response = self.client.get(
            '/api/v1/sync/',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_pull_rejected_when_membership_inactive(self):
        """Une membership désactivée empêche l'accès."""
        membership = OrganizationMembership.objects.get(
            user=self.member, organization=self.org
        )
        membership.is_active = False
        membership.save()
        self.addCleanup(
            lambda: OrganizationMembership.objects.filter(
                user=self.member, organization=self.org
            ).update(is_active=True)
        )

        self.client.force_authenticate(user=self.member)
        response = self.client.get(
            '/api/v1/sync/',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SyncConflictResolutionTests(TestCase):
    """
    Résolution de conflit Last-Write-Wins (sur `sync_updated_at`).

    Désormais ACTIVE : les modèles writable héritent de `SyncableModel`, donc
    `_update_record` appelle `should_accept_sync_update`. Un update poussé avec
    un horodatage plus ANCIEN que la version serveur est rejeté et compté en
    conflit ; un update plus RÉCENT est appliqué. C'est la garantie sur laquelle
    s'appuie la remontée de conflits côté mobile (performSync → SyncStatusCard).
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='lww@example.com', password='pw')
        cls.org = Organization.objects.create(name='LWW Org', slug='lww-org')
        OrganizationMembership.objects.create(
            user=cls.user, organization=cls.org, role='owner'
        )

    def _push_update(self, customer, name, incoming_iso=None):
        record = {'id': str(customer.id), 'name': name}
        if incoming_iso is not None:
            record['updated_at'] = incoming_iso
        changes = {
            'customers': {'created': [], 'updated': [record], 'deleted': []}
        }
        return SyncPushService(self.org, self.user).apply_changes(
            changes, get_server_timestamp()
        )

    def test_stale_update_is_rejected_as_conflict(self):
        """Un update plus ancien que la version serveur est rejeté (conflit)."""
        customer = Customer.objects.create(
            organization=self.org, code='LWW-001', name='Original',
        )
        customer.refresh_from_db()  # récupère le sync_updated_at posé par save()
        older = customer.sync_updated_at - timedelta(minutes=10)

        result = self._push_update(customer, 'Stale Update', older.isoformat())

        self.assertEqual(result['stats']['conflicts'], 1)
        self.assertEqual(result['stats']['updated'], 0)
        customer.refresh_from_db()
        self.assertEqual(customer.name, 'Original')  # inchangé

    def test_newer_update_is_accepted(self):
        """Un update plus récent que la version serveur est appliqué."""
        customer = Customer.objects.create(
            organization=self.org, code='LWW-002', name='Original',
        )
        customer.refresh_from_db()
        newer = customer.sync_updated_at + timedelta(minutes=10)

        result = self._push_update(customer, 'Fresh Update', newer.isoformat())

        self.assertEqual(result['stats']['conflicts'], 0)
        self.assertEqual(result['stats']['updated'], 1)
        customer.refresh_from_db()
        self.assertEqual(customer.name, 'Fresh Update')
        self.assertTrue(customer.synced_from_client)

    def test_update_without_timestamp_is_applied(self):
        """Sans horodatage entrant, l'update est appliqué (pas de conflit possible)."""
        customer = Customer.objects.create(
            organization=self.org, code='LWW-003', name='Original',
        )

        result = self._push_update(customer, 'No Timestamp', incoming_iso=None)

        self.assertEqual(result['stats']['conflicts'], 0)
        self.assertEqual(result['stats']['updated'], 1)
        customer.refresh_from_db()
        self.assertEqual(customer.name, 'No Timestamp')


class SyncRoundTripTests(APITestCase):
    """
    Aller-retour complet create→push→pull via HTTP : un client crée un
    enregistrement hors-ligne, le pousse, puis un pull delta le lui renvoie.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='rt@example.com', password='pw')
        cls.org = Organization.objects.create(name='RT Org', slug='rt-org')
        OrganizationMembership.objects.create(
            user=cls.user, organization=cls.org, role='owner'
        )

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    def test_create_push_then_pull_returns_record(self):
        before = get_server_timestamp()
        customer_id = str(uuid.uuid4())

        # 1) PUSH — création hors-ligne envoyée au serveur
        push = self.client.post(
            '/api/v1/sync/',
            {
                'changes': {
                    'customers': {
                        'created': [{
                            'id': customer_id,
                            'code': 'RT-001',
                            'name': 'Round Trip',
                            'customer_type': 'individual',
                        }],
                        'updated': [],
                        'deleted': [],
                    }
                },
                'last_pulled_at': before,
                'pull_after_push': False,
            },
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(push.status_code, status.HTTP_200_OK)
        self.assertTrue(push.data['push']['success'])
        self.assertEqual(push.data['push']['stats']['created'], 1)

        # 2) PULL delta depuis `before` — le client retrouve sa création
        pull = self.client.get(
            '/api/v1/sync/',
            {'last_pulled_at': before},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(pull.status_code, status.HTTP_200_OK)
        created = pull.data['changes']['customers']['created']
        record = next((c for c in created if c['id'] == customer_id), None)
        self.assertIsNotNone(record, 'Le client poussé doit revenir au pull delta')
        self.assertEqual(record['name'], 'Round Trip')


class SyncSalePushRoundTripTests(TestCase):
    """
    Scénario POS hors-ligne : push d'une vente complète (vente + ligne + paiement
    + mouvement de stock) en une seule transaction, comme le fait `createSale()`
    côté mobile. Vérifie la persistance, le remplissage serveur de `sold_by`,
    l'application au stock et l'idempotence par `reference_id`.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='salepush@example.com', password='pw')
        cls.org = Organization.objects.create(name='Sale Org', slug='sale-org')
        OrganizationMembership.objects.create(
            user=cls.user, organization=cls.org, role='owner'
        )
        cls.warehouse = Warehouse.objects.create(
            organization=cls.org, name='Main', code='WH-1', is_default=True
        )
        cls.payment_method = PaymentMethod.objects.create(
            organization=cls.org, name='Cash', code='CASH',
            method_type='cash', is_default=True
        )
        cls.category = Category.objects.create(
            organization=cls.org, name='Cat', slug='cat'
        )
        cls.product = Product.objects.create(
            organization=cls.org, name='Prod', slug='prod', sku='PRD-1',
            category=cls.category, selling_price=Decimal('1000.00')
        )

    def _sale_changes(self, *, sale_id, item_id, payment_id, movement_id):
        return {
            'sales': {'created': [{
                'id': sale_id, 'reference': 'VT-RT-0001', 'status': 'completed',
                'sale_type': 'retail', 'warehouse_id': str(self.warehouse.id),
                'subtotal': '3000.00', 'total': '3000.00',
                'amount_paid': '3000.00', 'amount_due': '0.00',
            }], 'updated': [], 'deleted': []},
            'sale_items': {'created': [{
                'id': item_id, 'sale_id': sale_id, 'product_id': str(self.product.id),
                'quantity': '3.000', 'unit_price': '1000.00',
            }], 'updated': [], 'deleted': []},
            'payments': {'created': [{
                'id': payment_id, 'sale_id': sale_id,
                'payment_method_id': str(self.payment_method.id),
                'amount': '3000.00', 'status': 'completed',
            }], 'updated': [], 'deleted': []},
            'stock_movements': {'created': [{
                'id': movement_id, 'product_id': str(self.product.id),
                'warehouse_id': str(self.warehouse.id), 'movement_type': 'sale',
                'quantity': '3.000', 'quantity_before': '10.000',
                'quantity_after': '7.000', 'reference_type': 'sale',
                'reference_id': sale_id,
            }], 'updated': [], 'deleted': []},
        }

    def test_push_full_sale_persists_and_decrements_stock(self):
        # Stock initial de 10 unités
        Stock.objects.create(
            organization=self.org, product=self.product, variant=None,
            warehouse=self.warehouse, location=None, quantity=Decimal('10.000')
        )

        sale_id = str(uuid.uuid4())
        changes = self._sale_changes(
            sale_id=sale_id, item_id=str(uuid.uuid4()),
            payment_id=str(uuid.uuid4()), movement_id=str(uuid.uuid4()),
        )

        result = SyncPushService(self.org, self.user).apply_changes(
            changes, get_server_timestamp()
        )

        self.assertEqual(result['stats']['errors'], 0, msg=result['errors'])
        self.assertTrue(result['success'])
        self.assertEqual(result['stats']['created'], 4)

        # Persistance + sold_by rempli côté serveur
        sale = Sale.objects.get(id=sale_id)
        self.assertEqual(sale.reference, 'VT-RT-0001')
        self.assertEqual(sale.sold_by_id, self.user.id)
        self.assertEqual(SaleItem.objects.filter(sale=sale).count(), 1)
        self.assertEqual(Payment.objects.filter(sale=sale).count(), 1)

        # Stock décrémenté : 10 - 3 = 7
        stock = Stock.objects.get(
            product=self.product, warehouse=self.warehouse,
            variant=None, location=None,
        )
        self.assertEqual(stock.quantity, Decimal('7.000'))

    def test_stock_movement_is_idempotent_by_reference_id(self):
        """
        Rejouer la même vente (même `reference_id`, autre UUID de mouvement) ne
        décrémente le stock qu'une seule fois : un retry réseau du client ne
        double pas les sorties de stock. Garanti par la dédup `reference_id`
        dans `_handle_stock_movement_create` (et le fait que `_prepare_record_data`
        ne jette plus la colonne non-relationnelle `reference_id`).
        """
        Stock.objects.create(
            organization=self.org, product=self.product, variant=None,
            warehouse=self.warehouse, location=None, quantity=Decimal('10.000')
        )
        sale_id = str(uuid.uuid4())

        # 1er envoi
        SyncPushService(self.org, self.user).apply_changes(
            self._sale_changes(
                sale_id=sale_id, item_id=str(uuid.uuid4()),
                payment_id=str(uuid.uuid4()), movement_id=str(uuid.uuid4()),
            ),
            get_server_timestamp(),
        )

        # 2e envoi : nouveau mouvement (UUID différent) mais MÊME reference_id.
        # Le 1er id-check est contourné ; la dédup par reference_id doit jouer.
        StockMovement_count_before = StockMovement.objects.filter(
            organization=self.org, reference_id=sale_id
        ).count()

        SyncPushService(self.org, self.user).apply_changes(
            {'stock_movements': {'created': [{
                'id': str(uuid.uuid4()), 'product_id': str(self.product.id),
                'warehouse_id': str(self.warehouse.id), 'movement_type': 'sale',
                'quantity': '3.000', 'quantity_before': '7.000',
                'quantity_after': '4.000', 'reference_type': 'sale',
                'reference_id': sale_id,
            }], 'updated': [], 'deleted': []}},
            get_server_timestamp(),
        )

        # Le stock reste à 7 (pas de seconde décrémentation)
        stock = Stock.objects.get(
            product=self.product, warehouse=self.warehouse,
            variant=None, location=None,
        )
        self.assertEqual(stock.quantity, Decimal('7.000'))
        # Aucun mouvement supplémentaire dédupliqué n'a été matérialisé
        self.assertEqual(
            StockMovement.objects.filter(
                organization=self.org, reference_id=sale_id
            ).count(),
            StockMovement_count_before,
        )

    def test_server_recomputes_totals_ignoring_client_values(self):
        """
        Le serveur recalcule totaux/statut à partir des lignes et paiements
        RÉELS, en ignorant les valeurs envoyées par le client (parité web +
        sécurité). Le client ment ici (total=999999, payé=0, statut=draft) ;
        le serveur rétablit total=3000, payé=3000, statut=completed.
        """
        sale_id = str(uuid.uuid4())
        changes = {
            'sales': {'created': [{
                'id': sale_id, 'reference': 'VT-RECOMP-1', 'status': 'draft',
                'sale_type': 'retail', 'warehouse_id': str(self.warehouse.id),
                'subtotal': '5.00', 'tax_amount': '0.00', 'discount_amount': '0.00',
                'total': '999999.00', 'amount_paid': '0.00', 'amount_due': '999999.00',
                'change_amount': '0.00', 'exchange_rate': '1.00',
            }], 'updated': [], 'deleted': []},
            'sale_items': {'created': [{
                'id': str(uuid.uuid4()), 'sale_id': sale_id,
                'product_id': str(self.product.id),
                'quantity': '3.000', 'unit_price': '1000.00',
            }], 'updated': [], 'deleted': []},
            'payments': {'created': [{
                'id': str(uuid.uuid4()), 'sale_id': sale_id,
                'payment_method_id': str(self.payment_method.id),
                'amount': '3000.00', 'status': 'completed',
            }], 'updated': [], 'deleted': []},
        }

        result = SyncPushService(self.org, self.user).apply_changes(
            changes, get_server_timestamp()
        )

        self.assertEqual(result['stats']['errors'], 0, msg=result['errors'])
        sale = Sale.objects.get(id=sale_id)
        self.assertEqual(sale.subtotal, Decimal('3000.00'))
        self.assertEqual(sale.total, Decimal('3000.00'))
        self.assertEqual(sale.amount_paid, Decimal('3000.00'))
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.assertEqual(sale.status, 'completed')

    def test_nested_payload_does_not_crash(self):
        """
        Filet de sécurité : un payload à l'ancienne (items/payments IMBRIQUÉS
        dans la vente — donc portant les noms des relations inverses) ne fait
        plus échouer la création. Les tableaux imbriqués sont ignorés ; le
        mobile pousse désormais ces records séparément.
        """
        sale_id = str(uuid.uuid4())
        changes = {'sales': {'created': [{
            'id': sale_id, 'reference': 'VT-NESTED-1', 'status': 'completed',
            'sale_type': 'retail', 'warehouse_id': str(self.warehouse.id),
            'subtotal': '1000.00', 'tax_amount': '0.00', 'discount_amount': '0.00',
            'total': '1000.00', 'amount_paid': '1000.00', 'amount_due': '0.00',
            'change_amount': '0.00', 'exchange_rate': '1.00',
            'items': [{
                'id': str(uuid.uuid4()), 'product_id': str(self.product.id),
                'quantity': '1', 'unit_price': '1000',
            }],
            'payments': [{'id': str(uuid.uuid4()), 'amount': '1000'}],
        }], 'updated': [], 'deleted': []}}

        result = SyncPushService(self.org, self.user).apply_changes(
            changes, get_server_timestamp()
        )

        self.assertEqual(result['stats']['errors'], 0, msg=result['errors'])
        self.assertTrue(Sale.objects.filter(id=sale_id).exists())
        # Les lignes imbriquées sont ignorées (poussées séparément dans le vrai flux).
        self.assertEqual(SaleItem.objects.filter(sale_id=sale_id).count(), 0)

    def test_decimal_fields_without_decimal_point_are_coerced(self):
        """
        Régression : le mobile envoie les décimales en chaîne, y compris les
        entiers SANS point (quantity='1', unit_price='13409'). Le backend doit
        les convertir en Decimal d'après le TYPE du champ — sinon `SaleItem.save()`
        fait `'1' * '13409'` (str*str) et la ligne n'est jamais créée (la vente
        se synchronise alors sans aucune ligne, total recalculé à 0).
        """
        sale_id = str(uuid.uuid4())
        item_id = str(uuid.uuid4())
        changes = {
            'sales': {'created': [{
                'id': sale_id, 'reference': 'VT-INT-DEC', 'status': 'completed',
                'sale_type': 'retail', 'warehouse_id': str(self.warehouse.id),
                'subtotal': '13409', 'total': '13409', 'amount_paid': '13409',
            }], 'updated': [], 'deleted': []},
            'sale_items': {'created': [{
                'id': item_id, 'sale_id': sale_id, 'product_id': str(self.product.id),
                'quantity': '1', 'unit_price': '13409',  # ENTIERS sans point
            }], 'updated': [], 'deleted': []},
            'payments': {'created': [{
                'id': str(uuid.uuid4()), 'sale_id': sale_id,
                'payment_method_id': str(self.payment_method.id),
                'amount': '13409', 'status': 'completed',
            }], 'updated': [], 'deleted': []},
        }

        result = SyncPushService(self.org, self.user).apply_changes(
            changes, get_server_timestamp()
        )

        self.assertEqual(result['stats']['errors'], 0, msg=result['errors'])
        item = SaleItem.objects.filter(sale_id=sale_id).first()
        self.assertIsNotNone(item, "La ligne doit être créée malgré l'absence de point décimal")
        self.assertEqual(item.quantity, Decimal('1'))
        self.assertEqual(item.unit_price, Decimal('13409'))
        # Recalcul serveur autoritaire : total = 1 × 13409
        sale = Sale.objects.get(id=sale_id)
        self.assertEqual(sale.total, Decimal('13409.00'))
        self.assertEqual(sale.status, 'completed')
