"""
Ce qui garantit qu'un membre borné a toujours au moins un entrepôt.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ON FERME LE CAS, ON NE LE RATTRAPE PLUS À LA MAIN.                          │
│                                                                              │
│ Un rôle borné sans affectation ne voit RIEN : c'est désormais la réponse des │
│ trois surfaces, tirage compris. C'est la bonne réponse - un périmètre par    │
│ défaut doit se fermer, pas s'ouvrir - mais elle laisse un membre mal         │
│ configuré devant un écran vide.                                              │
│                                                                              │
│ `assign_default_warehouse` existait pour cela et n'avait AUCUN appelant :    │
│ un membre créé demain retombait dans le trou. Le corps vit ici, la commande  │
│ et le signal l'appellent tous deux.                                          │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from apps.inventory.models import Warehouse


def entrepot_principal(organization):
    """
    L'entrepôt à donner par défaut : ``is_default``, sinon le premier actif.

    Rend ``None`` quand l'organisation n'en a aucun - une jeune organisation,
    ou une configuration à reprendre. On ne fabrique rien dans ce cas.
    """
    qs = Warehouse.objects.filter(organization=organization, is_deleted=False)
    return (
        qs.filter(is_default=True).order_by('name').first()
        or qs.filter(is_active=True).order_by('name').first()
        or qs.order_by('name').first()
    )


def assurer_entrepot_du_membre(membership) -> bool:
    """
    Donne l'entrepôt principal à un membre borné qui n'en a aucun.

    Rend ``True`` si une affectation a été posée. Idempotente : un second appel
    ne trouve plus de membre sans entrepôt.

    ⚠ LE PROPRIÉTAIRE EST ÉPARGNÉ, et ce n'est pas un détail : son accès vient
    de son RÔLE, `accessible_warehouse_ids` rendant `None` pour lui. Lui poser
    une affectation le ferait passer de « tous les dépôts » à « celui-là », et
    on rétrécirait son périmètre en croyant l'élargir.

    ⚠ ON NE COMPTE QUE LES ENTREPÔTS VIVANTS. Un membre dont le seul dépôt est
    supprimé a bien une affectation, et aucun accès : c'est le cas que
    `accessible_warehouse_ids` écarte par son `is_deleted=False`, et il doit
    être rattrapé ici comme une absence.
    """
    from apps.organizations.models import OrganizationMembership

    if membership.role == OrganizationMembership.Role.OWNER:
        return False
    if membership.assigned_warehouses.filter(is_deleted=False).exists():
        return False

    principal = entrepot_principal(membership.organization)
    if principal is None:
        return False

    membership.assigned_warehouses.add(principal)
    # Le périmètre du membre vient de changer : son jeton de périmètre bascule,
    # et son terminal retirera les tables concernées à la prochaine
    # synchronisation. Sans ce mécanisme, les lignes du dépôt nouvellement
    # accessible seraient derrière son curseur, donc perdues pour toujours.
    return True
