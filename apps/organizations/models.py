import uuid
from django.db import models
from django.conf import settings
from apps.core.models import TimeStampedModel, UUIDModel, SoftDeleteModel


class Organization(TimeStampedModel, UUIDModel, SoftDeleteModel):
    """
    Tenant model - represents a business/company using the SaaS.
    All business data is scoped to an organization.
    """
    
    class BusinessType(models.TextChoices):
        BOUTIQUE = 'boutique', 'Boutique'
        SUPERMARKET = 'supermarket', 'Supermarché'
        PHARMACY = 'pharmacy', 'Pharmacie'
        DEPOT = 'depot', 'Dépôt'
        RESTAURANT = 'restaurant', 'Restaurant'
        OTHER = 'other', 'Autre'

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    business_type = models.CharField(
        max_length=20,
        choices=BusinessType.choices,
        default=BusinessType.BOUTIQUE
    )
    
    logo = models.ImageField(upload_to='organizations/logos/', null=True, blank=True)
    
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default='RDC')
    
    tax_id = models.CharField(max_length=50, blank=True)
    rccm = models.CharField(max_length=50, blank=True)
    id_nat = models.CharField(max_length=50, blank=True)
    
    currency = models.CharField(max_length=3, default='CDF')
    timezone = models.CharField(max_length=50, default='Africa/Kinshasa')
    
    is_active = models.BooleanField(default=True)
    
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'organizations'
        ordering = ['name']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active']),
        ]
        permissions = [
            ('manage_organization', 'Can manage organization'),
        ]

    def __str__(self):
        return self.name

    def get_active_subscription(self):
        """Get the current active subscription."""
        return self.subscriptions.filter(
            status='active'
        ).order_by('-created_at').first()


class OrganizationMembership(TimeStampedModel, UUIDModel):
    """
    Links users to organizations with specific roles.
    A user can belong to multiple organizations.
    
    Rôles:
    - owner (Admin) : créateur de la boutique, toutes les permissions
    - manager (Gérant) : gestion complète, peut créer magasiniers et caissiers
    - stock_keeper (Magasinier) : gestion du stock, inventaires, transferts
    - cashier (Caissier) : ventes, consultation produits/prix
    """
    
    class Role(models.TextChoices):
        OWNER = 'owner', 'Admin'
        MANAGER = 'manager', 'Gérant'
        STOCK_KEEPER = 'stock_keeper', 'Magasinier'
        CASHIER = 'cashier', 'Caissier'

    # Hiérarchie des rôles (index = niveau, plus haut = plus de pouvoir)
    ROLE_HIERARCHY = {
        'owner': 4,
        'manager': 3,
        'stock_keeper': 2,
        'cashier': 1,
    }

    # Rôles que chaque rôle peut créer/gérer
    MANAGEABLE_ROLES = {
        'owner': ['manager', 'stock_keeper', 'cashier'],
        'manager': ['stock_keeper', 'cashier'],
        'stock_keeper': [],
        'cashier': [],
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CASHIER
    )
    
    is_active = models.BooleanField(default=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invitations_sent'
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'organization_memberships'
        unique_together = ['user', 'organization']
        indexes = [
            models.Index(fields=['user', 'organization']),
            models.Index(fields=['role']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.organization.name} ({self.role})"

    @property
    def role_level(self):
        """Retourne le niveau hiérarchique du rôle."""
        return self.ROLE_HIERARCHY.get(self.role, 0)

    def can_manage_role(self, target_role):
        """Vérifie si ce membre peut créer/modifier un utilisateur avec le rôle cible."""
        return target_role in self.MANAGEABLE_ROLES.get(self.role, [])

    def is_above(self, other_membership):
        """Vérifie si ce membre est hiérarchiquement au-dessus d'un autre."""
        return self.role_level > other_membership.role_level


class Branch(TimeStampedModel, UUIDModel, SoftDeleteModel):
    """
    Physical location/branch of an organization.
    Supports multi-location businesses.
    """
    
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='branches'
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=20)
    
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    
    is_main = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_branches'
    )

    class Meta:
        db_table = 'branches'
        unique_together = ['organization', 'code']
        verbose_name_plural = 'branches'
        indexes = [
            models.Index(fields=['organization', 'is_active']),
        ]

    def __str__(self):
        return f"{self.organization.name} - {self.name}"


class OrganizationInvitation(TimeStampedModel, UUIDModel):
    """Pending invitations to join an organization."""
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'En attente'
        ACCEPTED = 'accepted', 'Acceptée'
        DECLINED = 'declined', 'Refusée'
        EXPIRED = 'expired', 'Expirée'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='invitations'
    )
    email = models.EmailField()
    role = models.CharField(
        max_length=20,
        choices=OrganizationMembership.Role.choices,
        default=OrganizationMembership.Role.CASHIER
    )
    
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='org_invitations_sent'
    )
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'organization_invitations'
        indexes = [
            models.Index(fields=['email', 'status']),
            models.Index(fields=['token']),
        ]

    def __str__(self):
        return f"Invitation: {self.email} to {self.organization.name}"
