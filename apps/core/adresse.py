"""
L'adresse IP de celui qui appelle, relevée en UN endroit.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `X-Forwarded-For` SE FALSIFIE, ET HUIT COPIES LE CROYAIENT SUR PAROLE.      │
│                                                                              │
│ Le relevé était écrit huit fois (cinq `_get_client_ip` dans les vues de     │
│ comptes, `client_ip` des appareils, deux `REMOTE_ADDR` bruts), et toutes    │
│ prenaient la PREMIÈRE entrée de `X-Forwarded-For`. Or le premier qui écrit  │
│ cet en-tête est le client lui-même : n'importe qui pouvait faire inscrire   │
│ l'adresse de son choix au journal d'audit, dans l'historique de connexion   │
│ d'un marchand, et demain dans le compteur de verrouillage.                  │
└──────────────────────────────────────────────────────────────────────────────┘

Trois sources, dans cet ordre :

1. **L'en-tête signé du back-office.** Next appelle l'API depuis son propre
   serveur : sans lui, toutes les connexions web arrivent de la même adresse,
   celle de Next. Il transmet donc l'adresse du navigateur dans
   `X-VF-Client-IP`, accompagnée du secret partagé `VF_EDGE_KEY`. Sans le bon
   secret, l'en-tête est ignoré : un client qui l'écrirait lui-même n'obtient
   rien.
2. **`X-Forwarded-For`, lu depuis la DROITE.** Chaque proxy de confiance ajoute
   l'adresse de son interlocuteur en fin de chaîne : avec `TRUSTED_PROXY_COUNT`
   proxys devant Django, l'entrée au rang `-TRUSTED_PROXY_COUNT` est la
   dernière qu'un tiers ne peut pas écrire. C'est la règle de DRF
   (`NUM_PROXIES`), que les limites de débit appliquent avec le même réglage.
3. **`REMOTE_ADDR`**, quand aucun proxy n'est déclaré (le développement).

⚠ Une valeur qui n'est pas une adresse IP est écartée, jamais recopiée : le
journal d'audit et `UserActivity.ip_address` sont des `GenericIPAddressField`,
et une chaîne arbitraire y lèverait à l'écriture, c'est-à-dire au milieu de
l'acte qu'elle accompagne.
"""
import hmac
import ipaddress

from django.conf import settings


def _ip_valide(valeur):
    if not valeur:
        return None
    valeur = valeur.strip()
    try:
        ipaddress.ip_address(valeur)
    except ValueError:
        return None
    return valeur


def _de_la_bordure(meta):
    """L'adresse que le serveur du back-office atteste, ou `None`."""
    cle = getattr(settings, 'VF_EDGE_KEY', '')
    fournie = meta.get('HTTP_X_VF_EDGE_KEY', '')
    if not cle or not fournie or not hmac.compare_digest(fournie, cle):
        return None
    return _ip_valide(meta.get('HTTP_X_VF_CLIENT_IP'))


def _de_la_chaine(meta, proxys):
    if proxys <= 0:
        return None
    chaine = [e.strip() for e in meta.get('HTTP_X_FORWARDED_FOR', '').split(',') if e.strip()]
    if not chaine:
        return None
    return _ip_valide(chaine[-min(proxys, len(chaine))])


def adresse_client(request):
    """
    L'adresse de l'appelant, ou `None` si aucune source n'en donne une valide.

    Accepte `None` : un service appelé hors d'une requête (tâche, commande) n'a
    pas d'adresse, et le dire vaut mieux que lever au milieu de l'acte.
    """
    if request is None:
        return None
    meta = request.META
    return (
        _de_la_bordure(meta)
        or _de_la_chaine(meta, getattr(settings, 'TRUSTED_PROXY_COUNT', 0))
        or _ip_valide(meta.get('REMOTE_ADDR'))
    )
