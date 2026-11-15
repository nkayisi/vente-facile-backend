"""
Les règles d'un entrepôt : le créer, le modifier, le désigner principal, le
fermer et le supprimer.

┌──────────────────────────────────────────────────────────────────────────────┐
│ RIEN NE BORNAIT LA VIE D'UN ENTREPÔT, ET C'EST AINSI QU'IL « DISPARAISSAIT ».│
│                                                                              │
│ On pouvait supprimer l'entrepôt principal, un entrepôt qui porte du stock,  │
│ un entrepôt où une caisse est ouverte, ou le seul entrepôt d'un caissier -  │
│ qui ne voyait alors plus rien, sans que rien ne dise pourquoi. Plusieurs     │
│ entrepôts pouvaient se dire « principal ». Chaque refus nomme désormais ce   │
│ qui bloque : un « impossible » sans motif renvoie le marchand au support.    │
└──────────────────────────────────────────────────────────────────────────────┘

Le corps vit ici ; la vue n'en est que la porte. Un refus est un `RefusMetier`
dont le `code` dit la cause, rendu en 400 `{detail, code}`.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import status as http_status

from apps.core.exceptions import RefusAvecCorps, RefusMetier

#: Ce qu'un gérant modifie sur SES entrepôts. Le reste - activer, désactiver,
#: désigner le principal, changer la succursale, le code ou le responsable -
#: décide du périmètre des autres et reste au propriétaire.
CHAMPS_DU_GERANT = frozenset({'name', 'address'})


class EntrepotRefuse(RefusMetier):
    """Un geste sur un entrepôt que sa situation interdit."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class ReserveAuProprietaire(RefusAvecCorps):
    """403 : le gérant a touché un champ que seul le propriétaire écrit."""

    status_code = http_status.HTTP_403_FORBIDDEN
    default_code = 'reserve_au_proprietaire'

    def __init__(self, champs):
        detail = (
            "Seul le propriétaire modifie ces champs d'un entrepôt : "
            + ', '.join(champs) + '. Vous pouvez en changer le nom et l\'adresse.'
        )
        self.payload = {'detail': detail, 'code': self.default_code, 'fields': list(champs)}
        super().__init__(detail)


def annuaire(organization):
    """
    Tous les entrepôts de l'établissement, NOMMÉS, sans rien d'autre.

    Sert la destination d'un transfert, sur le web comme sur le terminal (charge
    de réveil). ⚠ Ni adresse, ni stock, ni responsable : l'expéditeur choisit où
    livrer, il n'obtient aucun accès à la boutique d'en face. Les inactifs y
    figurent avec leur état : un transfert ancien doit pouvoir nommer sa
    destination, mais aucun nouveau ne la vise (`exiger_entrepot_de_destination`).
    """
    from apps.inventory.models import Warehouse

    return [
        {'id': str(i), 'name': nom, 'is_active': actif}
        for i, nom, actif in Warehouse.objects.filter(
            organization=organization, is_deleted=False,
        ).order_by('-is_active', 'name').values_list('id', 'name', 'is_active')
    ]


def _noms(objets, champ='name', limite=5):
    noms = [str(getattr(o, champ, o)) for o in objets[:limite]]
    reste = len(objets) - len(noms)
    texte = ', '.join(noms)
    if reste <= 0:
        return texte
    return f"{texte} et {reste} {_accorder(reste, 'autre', 'autres')}"


def _accorder(n, singulier, pluriel):
    return singulier if n < 2 else pluriel


@transaction.atomic
def designer_principal(entrepot):
    """
    Désigne cet entrepôt comme le principal de son établissement.

    Il n'y en a qu'UN : désigner celui-ci retire le titre au précédent, dans la
    même transaction. Un entrepôt désactivé ne peut pas être principal - le
    principal est celui qu'on donne d'office à un membre nouveau.
    """
    from apps.core.bulk import bulk_update_rows
    from apps.inventory.models import Warehouse

    if not entrepot.is_active:
        raise EntrepotRefuse(
            f"L'entrepôt « {entrepot.name} » est désactivé : réactivez-le avant d'en faire "
            "l'entrepôt principal.",
            code='entrepot_inactif',
        )
    autres = Warehouse.objects.select_for_update().filter(
        organization_id=entrepot.organization_id, is_default=True, is_deleted=False,
    ).exclude(id=entrepot.id)
    bulk_update_rows(autres, is_default=False)
    if not entrepot.is_default:
        entrepot.is_default = True
        entrepot.save(update_fields=['is_default'])
    return entrepot


def _bloquants_de_fermeture(entrepot):
    """
    Ce qui interdit de fermer (désactiver ou supprimer) cet entrepôt, nommé.

    Rend une liste de `(code, message)`, vide si rien ne bloque.
    """
    from apps.inventory.models import InventorySession, StockTransfer
    from apps.sales.models import RegisterSession

    bloquants = []

    if entrepot.is_default:
        bloquants.append((
            'entrepot_principal',
            f"« {entrepot.name} » est l'entrepôt principal : désignez-en un autre d'abord.",
        ))

    sessions = list(
        RegisterSession.objects.filter(
            register__warehouse_id=entrepot.id, status=RegisterSession.Status.OPEN,
        ).select_related('register', 'opened_by')
    )
    if sessions:
        detail = ', '.join(
            f"{s.register.name} ({getattr(s.opened_by, 'full_name', '') or 'ouverte'})"
            for s in sessions[:5]
        )
        bloquants.append((
            'session_ouverte',
            f"{len(sessions)} {_accorder(len(sessions), 'caisse est ouverte', 'caisses sont ouvertes')} "
            f"dans cet entrepôt : {detail}. Clôturez-les d'abord.",
        ))

    transferts = list(
        StockTransfer.objects.filter(organization_id=entrepot.organization_id)
        .filter(_q_transfert_de(entrepot))
        .exclude(status__in=[StockTransfer.Status.COMPLETED, StockTransfer.Status.CANCELLED])
        .only('reference')
    )
    if transferts:
        bloquants.append((
            'transfert_en_route',
            f"{len(transferts)} {_accorder(len(transferts), 'transfert le concerne', 'transferts le concernent')} "
            f"encore ({_noms(transferts, 'reference')}) : recevez-les ou annulez-les d'abord.",
        ))

    inventaires = list(
        InventorySession.objects.filter(warehouse_id=entrepot.id).exclude(
            status__in=[InventorySession.Status.VALIDATED, InventorySession.Status.CANCELLED]
        ).only('reference')
    )
    if inventaires:
        bloquants.append((
            'inventaire_en_cours',
            f"Un inventaire n'y est pas terminé ({_noms(inventaires, 'reference')}) : "
            "validez-le ou annulez-le d'abord.",
        ))

    seuls = membres_dont_cest_le_seul_entrepot(entrepot)
    if seuls:
        noms = _noms([m.user for m in seuls], 'full_name')
        bloquants.append((
            'seul_entrepot_actif',
            f"C'est le seul entrepôt actif de {noms} : "
            f"{_accorder(len(seuls), 'affectez-le', 'affectez-les')} à un autre entrepôt d'abord.",
        ))
    return bloquants


def _q_transfert_de(entrepot):
    from django.db.models import Q

    return Q(source_warehouse_id=entrepot.id) | Q(destination_warehouse_id=entrepot.id)


def membres_dont_cest_le_seul_entrepot(entrepot):
    """
    Les membres non propriétaires dont CET entrepôt est le seul entrepôt ACTIF.

    Le fermer les laisserait sans aucun entrepôt où travailler : ils ne
    pourraient plus ni vendre ni ouvrir de caisse, et liraient « Votre entrepôt
    est désactivé ».
    """
    from django.db.models import Count, Q

    from apps.organizations.models import OrganizationMembership

    return list(
        OrganizationMembership.objects.filter(
            organization_id=entrepot.organization_id,
            is_active=True,
            assigned_warehouses__id=entrepot.id,
        )
        .exclude(role=OrganizationMembership.Role.OWNER)
        .annotate(
            _actifs_autres=Count(
                'assigned_warehouses',
                filter=Q(
                    assigned_warehouses__is_deleted=False,
                    assigned_warehouses__is_active=True,
                ) & ~Q(assigned_warehouses__id=entrepot.id),
                distinct=True,
            )
        )
        .filter(_actifs_autres=0)
        .select_related('user')
    )


def _refuser_si_bloque(bloquants, codes_ignores=()):
    retenus = [(c, m) for c, m in bloquants if c not in codes_ignores]
    if retenus:
        code, _ = retenus[0]
        raise EntrepotRefuse(' '.join(m for _, m in retenus), code=code)


@transaction.atomic
def desactiver(entrepot):
    """
    Ferme l'entrepôt à toute opération. Il reste lisible : son historique se
    relit, ses ventes passées se filtrent. Ses caisses (fermées) sont
    désactivées avec lui ; elles ne le sont pas à la réactivation, qui reste un
    geste délibéré par caisse.
    """
    from apps.core.bulk import bulk_update_rows
    from apps.sales.models import Register

    if not entrepot.is_active:
        return entrepot
    _refuser_si_bloque(_bloquants_de_fermeture(entrepot))
    bulk_update_rows(
        Register.objects.filter(warehouse_id=entrepot.id, is_active=True),
        is_active=False,
    )
    entrepot.is_active = False
    entrepot.save(update_fields=['is_active'])
    return entrepot


@transaction.atomic
def activer(entrepot):
    if not entrepot.is_active:
        entrepot.is_active = True
        entrepot.save(update_fields=['is_active'])
    return entrepot


@transaction.atomic
def supprimer(entrepot):
    """
    Supprime (en douceur) l'entrepôt. Refusé tant qu'il porte du stock, en plus
    de tout ce qui refuse la désactivation : on transfère ou on ajuste d'abord.
    """
    from apps.core.bulk import bulk_update_rows
    from apps.inventory.models import Stock
    from apps.sales.models import Register

    bloquants = _bloquants_de_fermeture(entrepot)
    rayons = Stock.objects.filter(warehouse_id=entrepot.id).exclude(quantity=0).count()
    if rayons:
        bloquants.append((
            'stock_restant',
            f"{rayons} {_accorder(rayons, 'rayon porte', 'rayons portent')} encore du stock : "
            "transférez-le ou ajustez-le d'abord.",
        ))
    _refuser_si_bloque(bloquants)
    bulk_update_rows(
        Register.objects.filter(warehouse_id=entrepot.id, is_active=True),
        is_active=False,
    )
    entrepot.soft_delete()
    return entrepot
