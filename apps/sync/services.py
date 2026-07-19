"""
Services for WatermelonDB sync operations.
"""
import logging
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Sum, DecimalField
from django.utils import timezone
from django.apps import apps

from .constants import (
    SYNCABLE_MODELS, SYNCABLE_MODELS_MAP, READ_ONLY_TABLES,
    MAX_RECORDS_PER_TABLE, SCHEMA_VERSION
)
from .serializers import get_serializer_for_table
from .utils import (
    parse_timestamp, get_server_timestamp, datetime_to_ms,
    deserialize_decimal, uuid_to_str, is_valid_uuid
)

logger = logging.getLogger(__name__)


class SyncPullService:
    """
    Service for handling sync PULL operations.
    
    Retrieves changes since a given timestamp for WatermelonDB sync.
    """
    
    def __init__(self, organization, user=None):
        self.organization = organization
        self.user = user
    
    def get_changes(self, last_pulled_at, tables=None):
        """
        Get all changes since the given timestamp.
        
        Args:
            last_pulled_at: Timestamp of last pull (None for initial sync)
            tables: Optional list of table names to sync (None for all)
            
        Returns:
            dict: WatermelonDB-compatible sync response
        """
        timestamp = parse_timestamp(last_pulled_at)
        server_timestamp = get_server_timestamp()
        
        changes = {}
        
        for table_name, app_label, model_name, select_related in SYNCABLE_MODELS:
            # Skip if specific tables requested and this isn't one of them
            if tables and table_name not in tables:
                continue
            
            try:
                table_changes = self._get_table_changes(
                    table_name, app_label, model_name, 
                    select_related, timestamp
                )
                changes[table_name] = table_changes
            except Exception as e:
                logger.error(f"Error getting changes for {table_name}: {e}")
                changes[table_name] = {'created': [], 'updated': [], 'deleted': []}
        
        return {
            'changes': changes,
            'timestamp': server_timestamp,
            'schema_version': SCHEMA_VERSION,
        }
    
    def _get_table_changes(self, table_name, app_label, model_name, select_related, last_pulled_at):
        """
        Get changes for a specific table.
        
        Returns:
            dict with 'created', 'updated', 'deleted' lists
        """
        model = apps.get_model(app_label, model_name)
        serializer_class = get_serializer_for_table(table_name)
        
        if not serializer_class:
            logger.warning(f"No serializer found for {table_name}")
            return {'created': [], 'updated': [], 'deleted': []}
        
        # Check if model has organization field (tenant model)
        has_organization = hasattr(model, 'organization')
        has_soft_delete = hasattr(model, 'is_deleted')
        
        # Build base queryset - use _default_manager to bypass soft delete filter
        # We need to include deleted records for delta sync
        from django.db.models import QuerySet
        if has_soft_delete:
            # Use the base QuerySet class directly to avoid soft delete filter
            base_qs = QuerySet(model).using(model.objects.db)
            if has_organization:
                base_qs = base_qs.filter(organization=self.organization)
        else:
            if has_organization:
                base_qs = model.objects.filter(organization=self.organization)
            else:
                base_qs = model.objects.all()
        
        # Apply select_related for performance
        if select_related:
            base_qs = base_qs.select_related(*select_related)
        
        if last_pulled_at is None:
            # Initial sync - return all non-deleted records
            if has_soft_delete:
                created_qs = base_qs.filter(is_deleted=False)[:MAX_RECORDS_PER_TABLE]
            else:
                created_qs = base_qs[:MAX_RECORDS_PER_TABLE]
            
            created = serializer_class(created_qs, many=True).data
            return {
                'created': created,
                'updated': [],
                'deleted': [],
            }
        
        # Delta sync
        created = []
        updated = []
        deleted = []
        
        # Use sync_updated_at if available, otherwise updated_at
        has_sync_updated_at = hasattr(model, 'sync_updated_at')
        
        if has_soft_delete:
            # Get created records (created after last_pulled_at, not deleted)
            created_qs = base_qs.filter(
                created_at__gt=last_pulled_at,
                is_deleted=False
            )[:MAX_RECORDS_PER_TABLE]
            created = serializer_class(created_qs, many=True).data
            
            # Get updated records (updated after last_pulled_at, created before, not deleted)
            if has_sync_updated_at:
                from django.db.models import Q
                time_filter = Q(sync_updated_at__gt=last_pulled_at) | (
                    Q(sync_updated_at__isnull=True) & Q(updated_at__gt=last_pulled_at)
                )
                updated_qs = base_qs.filter(
                    time_filter,
                    created_at__lte=last_pulled_at,
                    is_deleted=False
                )[:MAX_RECORDS_PER_TABLE]
            else:
                updated_qs = base_qs.filter(
                    updated_at__gt=last_pulled_at,
                    created_at__lte=last_pulled_at,
                    is_deleted=False
                )[:MAX_RECORDS_PER_TABLE]
            updated = serializer_class(updated_qs, many=True).data
            
            # Get deleted records (deleted after last_pulled_at)
            deleted_qs = base_qs.filter(
                deleted_at__gt=last_pulled_at,
                is_deleted=True
            )[:MAX_RECORDS_PER_TABLE]
            deleted = [str(obj.id) for obj in deleted_qs]
        else:
            # Model without soft delete - only created and updated
            created_qs = base_qs.filter(
                created_at__gt=last_pulled_at
            )[:MAX_RECORDS_PER_TABLE]
            created = serializer_class(created_qs, many=True).data
            
            updated_qs = base_qs.filter(
                updated_at__gt=last_pulled_at,
                created_at__lte=last_pulled_at
            )[:MAX_RECORDS_PER_TABLE]
            updated = serializer_class(updated_qs, many=True).data
        
        return {
            'created': created,
            'updated': updated,
            'deleted': deleted,
        }


class SyncPushService:
    """
    Service for handling sync PUSH operations.
    
    Applies changes from WatermelonDB to the server database.
    """
    
    def __init__(self, organization, user):
        self.organization = organization
        self.user = user
        self.errors = []
        self.stats = {
            'created': 0,
            'updated': 0,
            'deleted': 0,
            'conflicts': 0,
            'errors': 0,
        }
    
    @transaction.atomic
    def apply_changes(self, changes, last_pulled_at):
        """
        Apply changes from client to server.
        
        Args:
            changes: Dict of table_name -> {created, updated, deleted}
            last_pulled_at: Timestamp when client last pulled
            
        Returns:
            dict: Result with stats and any errors
        """
        timestamp = parse_timestamp(last_pulled_at)

        # Process tables in dependency order
        for table_name, app_label, model_name, _ in SYNCABLE_MODELS:
            if table_name not in changes:
                continue
            
            # Skip read-only tables
            if table_name in READ_ONLY_TABLES:
                logger.warning(f"Skipping push to read-only table: {table_name}")
                continue
            
            table_changes = changes[table_name]
            
            try:
                self._apply_table_changes(
                    table_name, app_label, model_name,
                    table_changes, timestamp
                )
            except Exception as e:
                logger.error(f"Error applying changes to {table_name}: {e}")
                self.errors.append({
                    'table': table_name,
                    'error': str(e),
                })
                self.stats['errors'] += 1

        # Recalcul autoritaire des totaux/statut des ventes touchées (parité web).
        # Le client mobile calcule localement pour l'affichage immédiat, mais le
        # serveur fait foi : les valeurs poussées (subtotal/total/amount_paid/
        # status…) sont systématiquement recalculées à partir des lignes et des
        # paiements réellement enregistrés.
        self._recompute_affected_sales(changes)

        return {
            'success': len(self.errors) == 0,
            'stats': self.stats,
            'errors': self.errors,
            'timestamp': get_server_timestamp(),
        }

    def _recompute_affected_sales(self, changes):
        """
        Recalcule, côté serveur, les totaux et le statut de chaque vente touchée
        par ce push (création/mise à jour de la vente, de ses lignes ou de ses
        paiements). Réplique l'autorité du chemin web : on ne fait jamais
        confiance aux totaux fournis par le client.
        """
        sale_model = apps.get_model('sales', 'Sale')

        sale_ids = set()
        sales_changes = changes.get('sales', {})
        for rec in list(sales_changes.get('created', [])) + list(sales_changes.get('updated', [])):
            if isinstance(rec, dict) and rec.get('id'):
                sale_ids.add(str(rec['id']))
        for table in ('sale_items', 'payments'):
            tch = changes.get(table, {})
            for rec in list(tch.get('created', [])) + list(tch.get('updated', [])):
                if isinstance(rec, dict) and rec.get('sale_id'):
                    sale_ids.add(str(rec['sale_id']))

        if not sale_ids:
            return

        sales = sale_model.objects.filter(
            id__in=sale_ids, organization=self.organization
        )
        for sale in sales:
            # Ne pas écraser un statut terminal (annulée/remboursée).
            if sale.status in ('cancelled', 'refunded'):
                continue

            paid = sale.payments.filter(status='completed').aggregate(
                total=Sum('amount')
            )['total'] or Decimal('0.00')
            sale.amount_paid = paid.quantize(Decimal('0.01'))

            # Recalcule subtotal/tax/discount/total/amount_due/change depuis les
            # lignes réellement enregistrées (SaleItem.save() les a quantizées).
            sale.calculate_totals()

            # Statut basé sur le paiement (règle identique au serializer web).
            if sale.amount_paid >= sale.total:
                sale.status = 'completed'
            elif sale.amount_paid > 0:
                sale.status = 'partially_paid'
            else:
                sale.status = 'pending'

            sale.save()
    
    def _apply_table_changes(self, table_name, app_label, model_name, changes, last_pulled_at):
        """Apply changes for a specific table."""
        model = apps.get_model(app_label, model_name)
        
        # Process creates
        for record in changes.get('created', []):
            try:
                self._create_record(model, table_name, record)
            except Exception as e:
                logger.error(f"Error creating {table_name} record: {e}")
                self.errors.append({
                    'table': table_name,
                    'operation': 'create',
                    'id': record.get('id'),
                    'error': str(e),
                })
                self.stats['errors'] += 1
        
        # Process updates
        for record in changes.get('updated', []):
            try:
                self._update_record(model, table_name, record, last_pulled_at)
            except Exception as e:
                logger.error(f"Error updating {table_name} record: {e}")
                self.errors.append({
                    'table': table_name,
                    'operation': 'update',
                    'id': record.get('id'),
                    'error': str(e),
                })
                self.stats['errors'] += 1
        
        # Process deletes
        for record_id in changes.get('deleted', []):
            try:
                self._delete_record(model, table_name, record_id)
            except Exception as e:
                logger.error(f"Error deleting {table_name} record: {e}")
                self.errors.append({
                    'table': table_name,
                    'operation': 'delete',
                    'id': record_id,
                    'error': str(e),
                })
                self.stats['errors'] += 1
    
    def _create_record(self, model, table_name, data):
        """Create a new record from sync data."""
        record_id = data.get('id')
        has_organization = hasattr(model, 'organization')

        # Check if record already exists (idempotence).
        # Important : scopée à l'organisation. Sans ce filtre, un UUID qui
        # coïncide avec une row d'une autre org provoquerait un silent skip,
        # ce qui empêcherait à jamais ce client de créer un objet avec cet ID.
        if record_id and is_valid_uuid(record_id):
            existing_qs = model.objects.filter(id=record_id)
            if has_organization:
                existing_qs = existing_qs.filter(organization=self.organization)
            if existing_qs.exists():
                logger.info(f"Record {record_id} already exists in {table_name}, skipping create")
                return

            # Si l'ID existe dans une autre org, refuser la création
            # (collision UUID inter-tenants — extrêmement improbable mais
            # signalons-le proprement pour éviter une IntegrityError opaque).
            if has_organization and model.objects.filter(id=record_id).exists():
                raise ValueError(
                    f"Conflit d'UUID : l'identifiant {record_id} existe déjà "
                    f"dans une autre organisation."
                )

        # Prepare data for creation
        create_data = self._prepare_record_data(model, table_name, data)
        create_data['organization'] = self.organization
        
        # Set sync fields
        if hasattr(model, 'synced_from_client'):
            create_data['synced_from_client'] = True
        if hasattr(model, 'sync_updated_at'):
            create_data['sync_updated_at'] = timezone.now()
        
        # Handle special cases
        if table_name == 'sales':
            self._handle_sale_create(model, create_data, data)
        elif table_name == 'stock_movements':
            self._handle_stock_movement_create(model, create_data, data)
        else:
            if table_name == 'products':
                from apps.subscriptions.services import SubscriptionService

                SubscriptionService.assert_can_add_products(self.organization, 1)
            model.objects.create(**create_data)
        
        self.stats['created'] += 1
        logger.info(f"Created {table_name} record: {record_id}")
    
    def _update_record(self, model, table_name, data, last_pulled_at):
        """Update an existing record from sync data."""
        record_id = data.get('id')
        
        if not record_id or not is_valid_uuid(record_id):
            raise ValueError(f"Invalid record ID: {record_id}")
        
        # Get existing record
        try:
            existing = model.objects.select_for_update().get(
                id=record_id,
                organization=self.organization
            )
        except model.DoesNotExist:
            logger.warning(f"Record {record_id} not found in {table_name}, creating instead")
            self._create_record(model, table_name, data)
            return
        
        # Check for conflicts using sync_updated_at
        incoming_timestamp = data.get('_changed') or data.get('updated_at')
        if incoming_timestamp:
            incoming_dt = parse_timestamp(incoming_timestamp)
            if hasattr(existing, 'should_accept_sync_update'):
                if not existing.should_accept_sync_update(incoming_dt):
                    logger.info(f"Conflict detected for {table_name} {record_id}, rejecting update")
                    self.stats['conflicts'] += 1
                    return
        
        # Prepare and apply update
        update_data = self._prepare_record_data(model, table_name, data)
        
        for field, value in update_data.items():
            setattr(existing, field, value)
        
        if hasattr(existing, 'synced_from_client'):
            existing.synced_from_client = True
        if hasattr(existing, 'sync_updated_at'):
            existing.sync_updated_at = timezone.now()
        
        existing.save()
        self.stats['updated'] += 1
        logger.info(f"Updated {table_name} record: {record_id}")
    
    def _delete_record(self, model, table_name, record_id):
        """Soft delete a record."""
        if not record_id or not is_valid_uuid(record_id):
            raise ValueError(f"Invalid record ID: {record_id}")
        
        try:
            # Use with_deleted() if available to find already deleted records
            if hasattr(model.objects, 'with_deleted'):
                record = model.objects.with_deleted().get(
                    id=record_id,
                    organization=self.organization
                )
            else:
                record = model.objects.get(
                    id=record_id,
                    organization=self.organization
                )
        except model.DoesNotExist:
            logger.warning(f"Record {record_id} not found in {table_name} for deletion")
            return
        
        # Check if already deleted
        if hasattr(record, 'is_deleted') and record.is_deleted:
            logger.info(f"Record {record_id} already deleted in {table_name}")
            return
        
        # Soft delete
        if hasattr(record, 'soft_delete'):
            record.soft_delete()
        else:
            record.delete()
        
        self.stats['deleted'] += 1
        logger.info(f"Deleted {table_name} record: {record_id}")
    
    def _prepare_record_data(self, model, table_name, data):
        """
        Prepare record data for creation/update.

        Converts foreign key fields and handles special types.

        Sécurité : pour chaque FK qui pointe vers un modèle multi-tenant
        (avec un champ ``organization``), on vérifie que l'objet ciblé
        appartient bien à ``self.organization``. Sans ce contrôle, un client
        mobile pourrait pousser une vente référençant un Customer / Product
        d'une autre organisation.
        """
        prepared = {}

        # Champs CONCRETS uniquement (colonnes réelles). On exclut volontairement
        # les relations inverses (ex. `items`, `payments` sur Sale) : si le client
        # envoie un payload imbriqué, l'affecter à une relation inverse lèverait
        # « Direct assignment to the reverse side of a related set is prohibited »
        # et ferait échouer toute la création. On les ignore proprement à la place.
        field_names = {f.name for f in model._meta.concrete_fields}

        # Map of sync field names to model field names
        field_mapping = self._get_field_mapping(table_name)

        for key, value in data.items():
            # Skip internal fields + champs horodatage gérés par le modèle.
            # created_at/updated_at sont auto_now_add/auto_now, et sync_updated_at
            # est posé par le service : les accepter depuis le payload client
            # provoquait soit un crash (Decimal sur une chaîne ISO contenant un
            # '.'), soit l'écriture d'une chaîne dans un DateTimeField.
            if key in ('id', 'organization', '_changed', '_status',
                       'created_at', 'updated_at', 'sync_updated_at'):
                if key == 'id' and value and is_valid_uuid(value):
                    prepared['id'] = value
                continue

            # Map field name if needed
            model_field = field_mapping.get(key, key)

            # Skip if field doesn't exist on model
            if model_field not in field_names and not model_field.endswith('_id'):
                continue

            # Valeur explicitement nulle pour un champ concret NON-nullable
            # (hors FK `_id`, gérées plus bas) → on l'OMET pour laisser le défaut
            # du modèle s'appliquer (ex. `notes` est NOT NULL default ''). Le
            # client mobile envoie parfois `null` là où le modèle attend ''/un
            # défaut, ce qui provoquait « null value violates not-null constraint ».
            if value is None and not model_field.endswith('_id'):
                try:
                    if not model._meta.get_field(model_field).null:
                        continue
                except Exception:
                    pass

            # Handle foreign key fields (ending with _id)
            if key.endswith('_id'):
                fk_field = key[:-3]  # Remove _id suffix
                if fk_field in field_names:
                    # Vraie clé étrangère : valider l'appartenance à l'org.
                    if value and is_valid_uuid(value):
                        self._assert_fk_belongs_to_org(model, fk_field, value)
                        prepared[key] = value
                    else:
                        prepared[key] = None
                    continue
                if key not in field_names:
                    # Ni FK, ni colonne réelle : champ inconnu, on ignore.
                    continue
                # Sinon : colonne concrète non-relationnelle qui finit par `_id`
                # (ex: `reference_id`, un UUIDField). On NE doit PAS la jeter —
                # elle suit le traitement normal ci-dessous. Sans ça, la
                # déduplication des mouvements de stock par `reference_id` est
                # cassée et un retry réseau re-décrémente le stock.

            # Coercition décimale basée sur le TYPE du champ (et NON sur la
            # présence d'un '.'). Le mobile envoie les décimales en chaîne, y
            # compris les valeurs entières ('1', '13409') ; l'ancienne heuristique
            # « chaîne contenant un '.' » les laissait en str, ce qui cassait les
            # save() arithmétiques (ex. SaleItem.save : quantity * unit_price →
            # 'str' * 'str'). On convertit donc toute chaîne destinée à un
            # DecimalField, et seulement celle-là (les champs texte type `notes`
            # contenant un '.' ne sont pas touchés).
            if isinstance(value, str):
                try:
                    field_obj = model._meta.get_field(model_field)
                except Exception:
                    field_obj = None
                if isinstance(field_obj, DecimalField):
                    try:
                        prepared[model_field] = Decimal(value)
                        continue
                    except (ValueError, TypeError, InvalidOperation) as e:
                        logger.debug(f"Failed to parse decimal {value} for {model_field}: {e}")

            prepared[model_field] = value

        return prepared

    def _assert_fk_belongs_to_org(self, model, fk_field, fk_uuid):
        """
        Vérifie que la FK pointe vers un objet de la même organisation.

        Ne vérifie rien si le modèle cible n'est pas multi-tenant (User,
        Currency, etc.) — la cohérence est alors assurée par le modèle.
        """
        try:
            field = model._meta.get_field(fk_field)
        except Exception:
            return

        related_model = getattr(field, 'related_model', None)
        if related_model is None:
            return

        # Modèle non-tenant : pas de vérification (ex: User, Currency)
        related_field_names = {f.name for f in related_model._meta.get_fields()}
        if 'organization' not in related_field_names:
            return

        if not related_model.objects.filter(
            id=fk_uuid, organization=self.organization
        ).exists():
            raise ValueError(
                f"FK {fk_field}={fk_uuid} n'appartient pas à l'organisation "
                f"{self.organization.id} ou n'existe pas."
            )
    
    def _get_field_mapping(self, table_name):
        """Get field name mapping for a table."""
        # Most fields map directly, but some need special handling
        mappings = {
            'sales': {
                'sold_by_id': 'sold_by_id',
            },
            'sale_items': {
                'sale_id': 'sale_id',
                'product_id': 'product_id',
                'variant_id': 'variant_id',
                'batch_id': 'batch_id',
            },
            'payments': {
                'sale_id': 'sale_id',
                'payment_method_id': 'payment_method_id',
                'received_by_id': 'received_by_id',
            },
            'stock_movements': {
                'product_id': 'product_id',
                'variant_id': 'variant_id',
                'warehouse_id': 'warehouse_id',
                'batch_id': 'batch_id',
                'created_by_id': 'created_by_id',
            },
        }
        return mappings.get(table_name, {})
    
    def _handle_sale_create(self, model, create_data, original_data):
        """Handle special logic for creating a sale."""
        # Set sold_by to current user if not provided
        if 'sold_by_id' not in create_data or not create_data.get('sold_by_id'):
            create_data['sold_by_id'] = self.user.id
        
        # Create the sale
        sale = model.objects.create(**create_data)
        
        # Note: Stock movements and customer balance updates
        # should be handled by the mobile app sending separate
        # stock_movements records, or by a post-processing step
        
        return sale
    
    def _handle_stock_movement_create(self, model, create_data, original_data):
        """Handle special logic for creating a stock movement."""
        # Check for duplicate by reference_id (idempotence)
        reference_id = create_data.get('reference_id')
        if reference_id:
            existing = model.objects.filter(
                organization=self.organization,
                reference_id=reference_id
            ).first()
            if existing:
                logger.info(f"Stock movement with reference_id {reference_id} already exists, skipping")
                return existing
        
        # Set created_by to current user if not provided
        if 'created_by_id' not in create_data or not create_data.get('created_by_id'):
            create_data['created_by_id'] = self.user.id
        
        # Create the movement
        movement = model.objects.create(**create_data)
        
        # Update stock quantity
        self._apply_stock_movement(movement)
        
        return movement
    
    def _apply_stock_movement(self, movement):
        """Apply stock movement to update stock quantity."""
        from apps.inventory.models import Stock
        
        # Get or create stock record
        stock, created = Stock.objects.get_or_create(
            organization=self.organization,
            product=movement.product,
            variant=movement.variant,
            warehouse=movement.warehouse,
            location=None,
            defaults={'quantity': Decimal('0.000')}
        )
        
        # Determine direction based on movement type
        inbound_types = {
            'purchase', 'return_in', 'transfer_in', 
            'adjustment_in', 'initial', 'production_in'
        }
        
        if movement.movement_type in inbound_types:
            stock.quantity += movement.quantity
        else:
            stock.quantity -= movement.quantity
        
        stock.last_movement_at = timezone.now()
        stock.save()
        
        logger.info(f"Updated stock for {movement.product.name}: {stock.quantity}")


class SyncService:
    """
    Main sync service combining pull and push operations.
    """
    
    def __init__(self, organization, user):
        self.organization = organization
        self.user = user
    
    def pull(self, last_pulled_at, tables=None):
        """Pull changes from server."""
        service = SyncPullService(self.organization, self.user)
        return service.get_changes(last_pulled_at, tables)
    
    def push(self, changes, last_pulled_at):
        """Push changes to server."""
        service = SyncPushService(self.organization, self.user)
        return service.apply_changes(changes, last_pulled_at)
    
    def sync(self, last_pulled_at, changes=None, tables=None):
        """
        Full sync operation: push changes then pull updates.
        
        Args:
            last_pulled_at: Timestamp of last pull
            changes: Optional changes to push
            tables: Optional list of tables to sync
            
        Returns:
            dict: Combined result with push stats and pull changes
        """
        result = {
            'push': None,
            'pull': None,
        }
        
        # Push changes if provided
        if changes:
            result['push'] = self.push(changes, last_pulled_at)
            if not result['push']['success']:
                # Return early if push failed
                return result
        
        # Pull updates
        result['pull'] = self.pull(last_pulled_at, tables)
        
        return result
