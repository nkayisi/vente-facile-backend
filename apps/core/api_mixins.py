"""
Mixins DRF pour les ViewSets multi-tenant.
"""
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from guardian.shortcuts import assign_perm

from apps.organizations.models import Organization


class TenantViewSetMixin:
    """
    Mixin de base pour tous les ViewSets multi-tenant.
    
    Fonctionnalités:
    - Filtre automatiquement le queryset par organization
    - Attribue automatiquement l'organization lors de la création
    - Attribue les permissions guardian lors de la création
    - Gère le soft delete si le modèle le supporte
    """
    
    def get_organization(self):
        """
        Récupère l'organisation depuis le header X-Organization-ID.
        Vérifie que l'utilisateur est bien membre de cette organisation.
        """
        org_id = self.request.headers.get('X-Organization-ID')
        if not org_id:
            return None
        
        return get_object_or_404(
            Organization.objects.filter(
                memberships__user=self.request.user,
                memberships__is_active=True
            ),
            id=org_id
        )

    def get_queryset(self):
        """
        Filtre le queryset par organisation.
        Utilise select_related/prefetch_related si définis.
        """
        queryset = super().get_queryset()
        organization = self.get_organization()
        
        if organization and hasattr(queryset.model, 'organization'):
            queryset = queryset.filter(organization=organization)
        
        # Exclure les objets soft-deleted par défaut
        if hasattr(queryset.model, 'is_deleted'):
            queryset = queryset.filter(is_deleted=False)
        
        # Appliquer select_related si défini
        select_related = getattr(self, 'select_related_fields', None)
        if select_related:
            queryset = queryset.select_related(*select_related)
        
        # Appliquer prefetch_related si défini
        prefetch_related = getattr(self, 'prefetch_related_fields', None)
        if prefetch_related:
            queryset = queryset.prefetch_related(*prefetch_related)
        
        return queryset

    def perform_create(self, serializer):
        """
        Lors de la création:
        - Attribue automatiquement l'organisation
        - Attribue les permissions guardian à l'utilisateur
        """
        organization = self.get_organization()
        instance = serializer.save(organization=organization)

        # Attribuer les permissions guardian
        self._assign_guardian_permissions(instance)

        return instance

    def perform_update(self, serializer):
        """Mise à jour standard."""
        return serializer.save()

    def perform_destroy(self, instance):
        """
        Suppression: utilise soft delete si disponible.
        """
        if hasattr(instance, 'soft_delete'):
            instance.soft_delete()
        else:
            instance.delete()

    def _assign_guardian_permissions(self, instance):
        """
        Attribue les permissions guardian sur l'objet créé.
        Par défaut: view, change, delete pour le créateur.
        """
        model_name = instance._meta.model_name
        permissions = [
            f'view_{model_name}',
            f'change_{model_name}',
            f'delete_{model_name}',
        ]

        for perm in permissions:
            assign_perm(perm, self.request.user, instance)


class WarehouseScopedQuerysetMixin:
    """
    Restreint le queryset aux entrepôts assignés au membership (non-owner).

    - ``warehouse_scope_field`` : champ direct ou relation Django pour filtrer
      (ex. ``warehouse_id``, ``id``, ``register__warehouse_id``,
      ``original_sale__warehouse_id``).
    - ``warehouse_scope_include_null`` : si ``True``, les lignes avec la
      relation à ``NULL`` restent visibles (utile pour les mouvements de
      caisse non rattachés à une vente).
    """

    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = False

    def _filter_queryset_by_membership_warehouses(self, queryset):
        from apps.core.warehouse_scope import (
            get_membership_for_request,
            filter_queryset_by_related_warehouse,
        )

        membership = get_membership_for_request(self.request)
        if not membership:
            return queryset
        field = getattr(self, 'warehouse_scope_field', 'warehouse_id')
        include_null = getattr(self, 'warehouse_scope_include_null', False)
        return filter_queryset_by_related_warehouse(
            queryset, membership, field, include_null=include_null
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        return self._filter_queryset_by_membership_warehouses(queryset)


class WarehouseAssertCreateMixin:
    """
    Vérifie automatiquement que le ``warehouse`` envoyé en création/mise à jour
    est dans le périmètre du membership courant.

    - ``warehouse_write_field`` : nom du champ ``warehouse`` dans
      ``serializer.validated_data`` (par défaut ``'warehouse'``).
    - ``warehouse_write_required`` : si ``False``, ``None`` est toléré pour
      les rôles non-owner uniquement quand ``warehouse_write_allow_none``
      est ``True`` aussi.
    - ``warehouse_write_allow_none`` : autorise un warehouse ``None`` si la
      logique métier le permet.
    """

    warehouse_write_field = 'warehouse'
    warehouse_write_required = True
    warehouse_write_allow_none = False

    def _assert_warehouse_on_save(self, serializer):
        from apps.core.warehouse_scope import assert_warehouse_allowed_for_request

        field = getattr(self, 'warehouse_write_field', 'warehouse')
        wh = serializer.validated_data.get(field)
        if wh is None and not getattr(self, 'warehouse_write_required', True):
            return
        wh_id = getattr(wh, 'id', None) if wh is not None else None
        assert_warehouse_allowed_for_request(
            self.request,
            wh_id,
            allow_none=getattr(self, 'warehouse_write_allow_none', False),
        )

    def perform_create(self, serializer):
        self._assert_warehouse_on_save(serializer)
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        if (
            getattr(self, 'warehouse_write_field', 'warehouse')
            in serializer.validated_data
        ):
            self._assert_warehouse_on_save(serializer)
        return super().perform_update(serializer)


class BulkActionMixin:
    """
    Mixin pour les actions en masse (bulk operations).
    """
    
    def bulk_delete(self, request):
        """Suppression en masse."""
        ids = request.data.get('ids', [])
        if not ids:
            return Response(
                {'error': 'Aucun ID fourni'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(id__in=ids)
        count = queryset.count()
        
        for obj in queryset:
            self.perform_destroy(obj)
        
        return Response({'deleted': count})

    def bulk_update(self, request):
        """Mise à jour en masse."""
        ids = request.data.get('ids', [])
        data = request.data.get('data', {})
        
        if not ids or not data:
            return Response(
                {'error': 'IDs et données requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(id__in=ids)
        
        # Filtrer les champs autorisés
        allowed_fields = getattr(self, 'bulk_update_fields', [])
        if allowed_fields:
            data = {k: v for k, v in data.items() if k in allowed_fields}
        
        count = queryset.update(**data)
        return Response({'updated': count})


class AuditMixin:
    """
    Mixin pour l'audit des créations/modifications.
    """
    
    def perform_create(self, serializer):
        """Ajoute created_by lors de la création."""
        extra_kwargs = {}
        
        if hasattr(serializer.Meta.model, 'created_by'):
            extra_kwargs['created_by'] = self.request.user
        
        organization = self.get_organization()
        if organization:
            extra_kwargs['organization'] = organization
        
        instance = serializer.save(**extra_kwargs)
        self._assign_guardian_permissions(instance)
        return instance

    def perform_update(self, serializer):
        """Ajoute updated_by lors de la modification."""
        extra_kwargs = {}
        
        if hasattr(serializer.Meta.model, 'updated_by'):
            extra_kwargs['updated_by'] = self.request.user
        
        return serializer.save(**extra_kwargs)


class SearchMixin:
    """
    Mixin pour la recherche avancée.
    """
    search_fields = []
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        search = self.request.query_params.get('search', None)
        if search and self.search_fields:
            from django.db.models import Q
            query = Q()
            for field in self.search_fields:
                query |= Q(**{f'{field}__icontains': search})
            queryset = queryset.filter(query)
        
        return queryset


class FilterMixin:
    """
    Mixin pour le filtrage par paramètres URL.
    """
    filter_fields = {}
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        for param, field in self.filter_fields.items():
            value = self.request.query_params.get(param, None)
            if value is not None:
                if value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                queryset = queryset.filter(**{field: value})
        
        return queryset


class ActionPaginationMixin:
    """
    Mixin pour paginer les résultats des actions personnalisées (@action).
    Utilise les paramètres page et page_size de la requête.
    """
    DEFAULT_PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100
    
    def paginate_action_results(self, data, request, serializer_class=None):
        """
        Pagine une liste de données pour une action personnalisée.
        
        Args:
            data: Liste ou QuerySet à paginer
            request: L'objet request DRF
            serializer_class: Optionnel, serializer à utiliser pour les résultats
            
        Returns:
            Response avec les données paginées
        """
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', self.DEFAULT_PAGE_SIZE))
        
        # Limiter la taille de page
        page_size = min(page_size, self.MAX_PAGE_SIZE)
        
        # Convertir en liste si c'est un QuerySet
        if hasattr(data, 'count'):
            total_count = data.count()
        else:
            total_count = len(data)
        
        # Calculer les indices
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        
        # Extraire la page de données
        if hasattr(data, '__getitem__'):
            paginated_data = data[start_idx:end_idx]
        else:
            paginated_data = list(data)[start_idx:end_idx]
        
        # Sérialiser si un serializer est fourni
        if serializer_class:
            paginated_data = serializer_class(paginated_data, many=True).data
        
        # Calculer le nombre total de pages
        total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0
        
        return Response({
            'results': paginated_data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        })


class ExportResponseMixin:
    """
    Sert un rapport en téléchargement, dans le format demandé.

    Centralise ce que `apps/products/views.py` recopiait à chaque action
    d'export : validation du format, en-tête `Content-Type` et
    `Content-Disposition` horodaté. Le nom de fichier porte la date afin qu'un
    gestionnaire qui exporte deux fois dans la journée ne récupère pas deux
    fichiers homonymes dans son dossier de téléchargements.
    """

    #: Formats acceptés par les actions d'export du ViewSet.
    export_formats = ('pdf', 'xlsx')

    #: Nom du paramètre de requête portant le format demandé.
    #:
    #: Surtout PAS `format` : DRF réserve ce nom à la négociation de contenu
    #: (`URL_FORMAT_OVERRIDE`), et comme le projet n'expose que le renderer JSON,
    #: un `?format=pdf` déclencherait un 404 dans la négociation avant même
    #: d'atteindre la vue. Le piège est silencieux et coûte cher à diagnostiquer.
    export_format_param = 'export_format'

    def get_export_format(self, request, default='pdf'):
        """
        Lit et valide le format demandé.

        Accepte `excel` comme alias de `xlsx` : c'est le mot que porte le bouton
        côté interface, et l'écart entre les deux vocabulaires est une source
        d'erreurs 400 incompréhensibles pour l'utilisateur.
        """
        raw = (
            request.query_params.get(self.export_format_param) or default
        ).lower().strip()
        if raw == 'excel':
            raw = 'xlsx'
        if raw not in self.export_formats:
            raise ValidationError({
                self.export_format_param: (
                    f"Format invalide. Attendu : {', '.join(self.export_formats)}."
                )
            })
        return raw

    def export_file_response(self, buffer, basename, fmt):
        """Enveloppe un buffer rendu dans une réponse de téléchargement."""
        from apps.core.exports import CONTENT_TYPES

        stamp = timezone.localtime().strftime('%Y%m%d_%H%M')
        filename = f"{basename}_{stamp}.{fmt}"

        response = HttpResponse(
            buffer.getvalue(),
            content_type=CONTENT_TYPES.get(fmt, 'application/octet-stream'),
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        # Sans cet en-tête, le navigateur ne voit pas le nom du fichier lorsqu'il
        # est servi à travers une requête XHR depuis une autre origine.
        response['Access-Control-Expose-Headers'] = 'Content-Disposition'
        return response

    def render_export(self, spec, basename, fmt):
        """Rend la description de rapport puis la sert en pièce jointe."""
        from apps.core.exports import render_report

        buffer = render_report(spec, fmt)
        return self.export_file_response(buffer, basename, fmt)
