"""
Utility functions for WatermelonDB sync.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


def parse_timestamp(timestamp_value):
    """
    Parse a timestamp from various formats to a timezone-aware datetime.
    
    Supports:
    - ISO 8601 string (e.g., "2024-01-15T10:30:00Z")
    - Unix timestamp in milliseconds (e.g., 1705315800000)
    - Unix timestamp in seconds (e.g., 1705315800)
    - None or 0 (returns None for initial sync)
    
    Args:
        timestamp_value: The timestamp to parse
        
    Returns:
        datetime or None: Parsed datetime or None for initial sync
    """
    if timestamp_value is None or timestamp_value == 0 or timestamp_value == '0':
        return None
    
    # If it's already a datetime, ensure it's timezone-aware
    if isinstance(timestamp_value, datetime):
        if timezone.is_naive(timestamp_value):
            return timezone.make_aware(timestamp_value)
        return timestamp_value
    
    # Try parsing as ISO string
    if isinstance(timestamp_value, str):
        try:
            # Handle ISO format with Z suffix
            if timestamp_value.endswith('Z'):
                timestamp_value = timestamp_value[:-1] + '+00:00'
            dt = datetime.fromisoformat(timestamp_value)
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt
        except ValueError:
            pass
        
        # Try parsing as numeric string
        try:
            timestamp_value = int(timestamp_value)
        except ValueError:
            try:
                timestamp_value = float(timestamp_value)
            except ValueError:
                logger.warning(f"Could not parse timestamp string: {timestamp_value}")
                return None
    
    # Handle numeric timestamps
    if isinstance(timestamp_value, (int, float)):
        # Determine if milliseconds or seconds based on magnitude
        # Timestamps after year 2001 in seconds are > 1_000_000_000
        # Timestamps in milliseconds are > 1_000_000_000_000
        if timestamp_value > 1_000_000_000_000:
            # Milliseconds
            timestamp_value = timestamp_value / 1000
        
        try:
            dt = datetime.fromtimestamp(timestamp_value, tz=dt_timezone.utc)
            return dt
        except (ValueError, OSError) as e:
            logger.warning(f"Could not parse numeric timestamp {timestamp_value}: {e}")
            return None
    
    logger.warning(f"Unknown timestamp format: {type(timestamp_value)} - {timestamp_value}")
    return None


def get_server_timestamp():
    """
    Get current server timestamp in milliseconds.
    
    Returns:
        int: Current time as Unix timestamp in milliseconds
    """
    return int(timezone.now().timestamp() * 1000)


def datetime_to_ms(dt):
    """
    Convert a datetime to Unix timestamp in milliseconds.
    
    Args:
        dt: datetime object
        
    Returns:
        int: Unix timestamp in milliseconds
    """
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def ms_to_datetime(ms):
    """
    Convert Unix timestamp in milliseconds to datetime.
    
    Args:
        ms: Unix timestamp in milliseconds
        
    Returns:
        datetime: Timezone-aware datetime
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def serialize_decimal(value):
    """
    Serialize a Decimal to a JSON-compatible format.
    
    Args:
        value: Decimal or numeric value
        
    Returns:
        str: String representation of the decimal
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return value


def deserialize_decimal(value, default=None):
    """
    Deserialize a value to Decimal.
    
    Args:
        value: String or numeric value
        default: Default value if conversion fails
        
    Returns:
        Decimal: Parsed decimal value
    """
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def uuid_to_str(uuid_value):
    """
    Convert a UUID to string.
    
    Args:
        uuid_value: UUID object or string
        
    Returns:
        str: String representation of UUID
    """
    if uuid_value is None:
        return None
    return str(uuid_value)


def is_valid_uuid(value):
    """
    Check if a value is a valid UUID string.
    
    Args:
        value: String to check
        
    Returns:
        bool: True if valid UUID
    """
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False


def chunk_list(lst, chunk_size):
    """
    Split a list into chunks of specified size.
    
    Args:
        lst: List to split
        chunk_size: Maximum size of each chunk
        
    Yields:
        Lists of up to chunk_size elements
    """
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


def get_model_class(app_label, model_name):
    """
    Get a Django model class by app label and model name.
    
    Args:
        app_label: Django app label (e.g., 'products')
        model_name: Model class name (e.g., 'Product')
        
    Returns:
        Model class or None
    """
    from django.apps import apps
    try:
        return apps.get_model(app_label, model_name)
    except LookupError:
        logger.error(f"Model not found: {app_label}.{model_name}")
        return None
