"""
Aucun signal n'est utilisé dans le projet — toutes les opérations qui
seraient naturellement candidates à un signal sont déclenchées explicitement
depuis les serializers, viewsets ou services dédiés.

Historique : ce fichier contenait des récepteurs ``@receiver`` pour générer
les SKU produits, les références de ventes / d'ordres d'achat, et pour
mettre à jour le stock lors d'une vente ou d'une réception. Ces signaux
n'ont jamais été enregistrés (``apps.CoreConfig`` ne définit pas de
``ready()`` qui importe ce module), donc ils étaient du code mort. Mais
laisser le code mort en place était dangereux : un futur dev ou outil
automatisé risquait d'ajouter l'import et de réactiver une logique de
décrément de stock concurrente avec ``SaleStockService`` (résultant en un
double décrément).

Logique remplaçante explicite :
- Génération de références → ``apps.core.utils.ReferenceGenerator`` appelée
  dans chaque serializer / view qui crée un objet versioné.
- Décrément stock lors d'une vente → ``apps.sales.services.SaleStockService``.
- Incrément stock lors d'une réception → ``apps.purchases.services.GoodsReceiptStockService``.
- Attribution des permissions à un membre → ``apps.core.services.PermissionService.assign_role_permissions``
  appelée explicitement dans ``apps.organizations.views``.

Si un nouveau besoin émerge, privilégier un service explicite plutôt qu'un
signal, pour conserver la transactionnalité et l'idempotence visibles dans
le call site.
"""
