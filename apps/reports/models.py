from django.db import models
from django.conf import settings
from apps.core.models import TenantModel


class ReportTemplate(TenantModel):
    """
    Custom report templates.
    """
    
    class ReportType(models.TextChoices):
        SALES = 'sales', 'Ventes'
        PURCHASES = 'purchases', 'Achats'
        INVENTORY = 'inventory', 'Inventaire'
        FINANCIAL = 'financial', 'Financier'
        CUSTOMER = 'customer', 'Clients'
        SUPPLIER = 'supplier', 'Fournisseurs'
        PRODUCT = 'product', 'Produits'
        CUSTOM = 'custom', 'Personnalisé'

    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    report_type = models.CharField(
        max_length=20,
        choices=ReportType.choices
    )
    
    query_config = models.JSONField(default=dict)
    
    columns = models.JSONField(default=list)
    
    filters = models.JSONField(default=list)
    
    grouping = models.JSONField(default=list)
    
    sorting = models.JSONField(default=list)
    
    chart_config = models.JSONField(default=dict, blank=True)
    
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_reports'
    )

    class Meta:
        db_table = 'report_templates'
        unique_together = ['organization', 'code']

    def __str__(self):
        return self.name


class SavedReport(TenantModel):
    """
    Saved/scheduled reports.
    """
    
    class Frequency(models.TextChoices):
        ONCE = 'once', 'Une fois'
        DAILY = 'daily', 'Quotidien'
        WEEKLY = 'weekly', 'Hebdomadaire'
        MONTHLY = 'monthly', 'Mensuel'

    name = models.CharField(max_length=255)
    
    template = models.ForeignKey(
        ReportTemplate,
        on_delete=models.CASCADE,
        related_name='saved_reports'
    )
    
    parameters = models.JSONField(default=dict)
    
    is_scheduled = models.BooleanField(default=False)
    frequency = models.CharField(
        max_length=20,
        choices=Frequency.choices,
        default=Frequency.ONCE
    )
    schedule_time = models.TimeField(null=True, blank=True)
    schedule_day = models.PositiveSmallIntegerField(null=True, blank=True)
    
    recipients = models.JSONField(default=list)
    
    last_run = models.DateTimeField(null=True, blank=True)
    next_run = models.DateTimeField(null=True, blank=True)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='saved_reports'
    )

    class Meta:
        db_table = 'saved_reports'

    def __str__(self):
        return self.name


class ReportExport(TenantModel):
    """
    Exported report files.
    """
    
    class ExportFormat(models.TextChoices):
        PDF = 'pdf', 'PDF'
        EXCEL = 'excel', 'Excel'
        CSV = 'csv', 'CSV'

    class Status(models.TextChoices):
        PENDING = 'pending', 'En attente'
        PROCESSING = 'processing', 'En cours'
        COMPLETED = 'completed', 'Terminé'
        FAILED = 'failed', 'Échoué'

    saved_report = models.ForeignKey(
        SavedReport,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='exports'
    )
    template = models.ForeignKey(
        ReportTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    export_format = models.CharField(
        max_length=10,
        choices=ExportFormat.choices
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    parameters = models.JSONField(default=dict)
    
    file = models.FileField(upload_to='reports/exports/', null=True, blank=True)
    file_size = models.PositiveIntegerField(null=True, blank=True)
    
    error_message = models.TextField(blank=True)
    
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='report_exports'
    )
    
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'report_exports'
        ordering = ['-created_at']

    def __str__(self):
        return f"Export {self.id} - {self.export_format}"


class Dashboard(TenantModel):
    """
    Custom dashboards.
    """
    
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    
    layout = models.JSONField(default=dict)
    
    is_default = models.BooleanField(default=False)
    is_shared = models.BooleanField(default=False)
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dashboards'
    )

    class Meta:
        db_table = 'dashboards'

    def __str__(self):
        return self.name


class DashboardWidget(TenantModel):
    """
    Widgets on dashboards.
    """
    
    class WidgetType(models.TextChoices):
        METRIC = 'metric', 'Métrique'
        CHART = 'chart', 'Graphique'
        TABLE = 'table', 'Tableau'
        LIST = 'list', 'Liste'

    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.CASCADE,
        related_name='widgets'
    )
    
    name = models.CharField(max_length=255)
    widget_type = models.CharField(
        max_length=20,
        choices=WidgetType.choices
    )
    
    config = models.JSONField(default=dict)
    
    position_x = models.PositiveSmallIntegerField(default=0)
    position_y = models.PositiveSmallIntegerField(default=0)
    width = models.PositiveSmallIntegerField(default=1)
    height = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = 'dashboard_widgets'

    def __str__(self):
        return f"{self.dashboard.name} - {self.name}"
