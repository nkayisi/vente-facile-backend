import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone


class UserManager(BaseUserManager):
    """Custom user manager for email-based authentication."""
    
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model with email authentication.
    Users can belong to multiple organizations.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    
    avatar = models.ImageField(upload_to='users/avatars/', null=True, blank=True)
    
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_email_verified = models.BooleanField(default=False)
    
    date_joined = models.DateTimeField(default=timezone.now)
    last_login = models.DateTimeField(null=True, blank=True)
    
    active_organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_users'
    )
    
    preferences = models.JSONField(default=dict, blank=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        db_table = 'users'
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return self.email

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.email

    def get_organizations(self):
        """Get all organizations the user belongs to."""
        return self.memberships.filter(is_active=True).select_related('organization')

    def get_role_in_organization(self, organization):
        """Get user's role in a specific organization."""
        membership = self.memberships.filter(
            organization=organization,
            is_active=True
        ).first()
        return membership.role if membership else None

    def has_organization_permission(self, organization, permission):
        """Check if user has a specific permission in an organization."""
        from guardian.shortcuts import get_perms
        return permission in get_perms(self, organization)


class UserActivity(models.Model):
    """Tracks user activity for audit purposes."""
    
    class ActionType(models.TextChoices):
        LOGIN = 'login', 'Connexion'
        LOGOUT = 'logout', 'Déconnexion'
        CREATE = 'create', 'Création'
        UPDATE = 'update', 'Modification'
        DELETE = 'delete', 'Suppression'
        VIEW = 'view', 'Consultation'
        EXPORT = 'export', 'Export'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='activities'
    )
    organization = models.ForeignKey(
        'organizations.Organization',
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    
    action = models.CharField(max_length=20, choices=ActionType.choices)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=100, blank=True)
    
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'user_activities'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['organization', 'created_at']),
            models.Index(fields=['action']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.action} - {self.resource_type}"


class Device(models.Model):
    """
    Terminal enrôlé, porteur d'une session longue.

    Motif. La paire JWT de la plateforme dure 30 minutes pour l'accès et 7 jours
    pour le rafraîchissement, avec rotation et liste noire. C'est un bon réglage
    pour un navigateur ; c'est intenable pour un point de vente en RDC, où une
    boutique peut rester une semaine sans réseau. Passé ce délai, le caissier se
    retrouvait devant un écran de connexion qu'il ne pouvait pas franchir sans
    connexion, avec la caisse ouverte et des clients devant lui.

    Le jeton d'appareil résout cela sans affaiblir le reste : il **n'authentifie
    aucun endpoint métier**, il sert uniquement à réobtenir une paire JWT. Son
    rayon d'action est donc minimal, et il se révoque depuis le back-office.

    Un appareil est lié à UNE organisation : le cache de permissions hors ligne
    est par nature à portée d'organisation, et changer d'organisation impose de
    repartir d'une base locale vide.
    """

    class Platform(models.TextChoices):
        ANDROID = 'android', 'Android'
        IOS = 'ios', 'iOS'

    # Autonomie glissante. Repoussée à chaque session ouverte et à chaque envoi
    # réussi : un terminal qui se synchronise chaque semaine n'expire jamais.
    # Une expiration fixe ferait mourir tout le parc le même jour, un mois après
    # le déploiement.
    TTL_DAYS = 30
    # Plafond absolu depuis l'enrôlement : au-delà, mot de passe exigé. Sans lui
    # un parc devient immortel et un terminal perdu le reste aussi.
    ABSOLUTE_TTL_DAYS = 180

    # Alphabet du code d'appareil : 32 signes, sans I ni O, qui se confondent
    # avec 1 et 0 quand un commerçant lit un numéro sur un ticket thermique.
    CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    CODE_LENGTH = 4

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        'users.User', on_delete=models.CASCADE, related_name='devices'
    )
    organization = models.ForeignKey(
        'organizations.Organization', on_delete=models.CASCADE, related_name='devices'
    )

    # « Caisse 1 », « Tablette du magasinier ». Modifiable : c'est ce que le
    # gérant lit dans la liste des appareils pour décider d'une révocation.
    name = models.CharField(max_length=120)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    model = models.CharField(max_length=120, blank=True)
    os_version = models.CharField(max_length=40, blank=True)
    app_version = models.CharField(max_length=40, blank=True)

    # Suffixe des numéros de documents émis hors ligne : VT-20260828-K7QM-0042.
    # Attribué par le serveur, donc unique par organisation sans tirage au sort
    # côté client, et lisible sur le papier pour rattacher un ticket à sa caisse.
    device_code = models.CharField(max_length=CODE_LENGTH)

    # Empreinte SHA-256 du jeton. Pas de sel : le jeton porte déjà 256 bits
    # d'entropie, un sel n'ajouterait rien et empêcherait la recherche par index.
    token_hash = models.CharField(max_length=64, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    expires_at = models.DateTimeField()

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='revoked_devices'
    )

    class Meta:
        db_table = 'user_devices'
        ordering = ['-last_seen_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'device_code'],
                name='unique_device_code_per_org',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'user']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"{self.name} ({self.device_code})"

    # ------------------------------------------------------------------ jetons

    @staticmethod
    def hash_token(raw_token):
        """Empreinte d'un jeton brut. Seule l'empreinte est stockée."""
        import hashlib
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @staticmethod
    def generate_token():
        """Jeton brut de 256 bits. Retourné UNE seule fois, à l'enrôlement."""
        import secrets
        return secrets.token_urlsafe(32)

    @classmethod
    def generate_device_code(cls):
        import secrets
        return ''.join(
            secrets.choice(cls.CODE_ALPHABET) for _ in range(cls.CODE_LENGTH)
        )

    # ------------------------------------------------------------------- durée

    @classmethod
    def default_expiry(cls):
        from datetime import timedelta
        return timezone.now() + timedelta(days=cls.TTL_DAYS)

    @property
    def absolute_deadline(self):
        """Au-delà, plus aucun glissement : mot de passe exigé."""
        from datetime import timedelta
        return self.created_at + timedelta(days=self.ABSOLUTE_TTL_DAYS)

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self):
        """Un appareil révoqué ou expiré n'ouvre plus de session."""
        return not self.is_revoked and not self.is_expired

    def touch(self, ip=None):
        """
        Repousse l'échéance et note le passage.

        Appelée à chaque ouverture de session et à chaque envoi d'opérations
        accepté. Le plafond absolu n'est jamais franchi : au-delà, l'appareil
        s'éteint et exige un mot de passe.
        """
        from datetime import timedelta

        now = timezone.now()
        self.expires_at = min(now + timedelta(days=self.TTL_DAYS), self.absolute_deadline)
        self.last_seen_at = now
        if ip:
            self.last_ip = ip
        self.save(update_fields=['expires_at', 'last_seen_at', 'last_ip'])

    def revoke(self, by=None):
        if self.is_revoked:
            return
        self.revoked_at = timezone.now()
        self.revoked_by = by
        self.save(update_fields=['revoked_at', 'revoked_by'])
