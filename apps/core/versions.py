"""
Comparer deux versions d'application, et fabriquer l'adresse d'un magasin.

┌──────────────────────────────────────────────────────────────────────────────┐
│ MODULE PUR. AUCUN IMPORT D'APPLICATION, AUCUNE REQUÊTE.                      │
│                                                                              │
│ Ces règles sont opposées à un formulaire d'administration ET lues sur le      │
│ chemin de réveil d'un terminal. Deux écritures finiraient par diverger, et la │
│ divergence se paierait en parc bloqué devant un magasin qui n'a rien de plus  │
│ neuf à servir.                                                               │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ **LE SERVEUR NE REND JAMAIS DE VERDICT DE VERSION.** Il descend un SEUIL, et
c'est le terminal qui compare, dans le miroir de ce module
(`mobile/vf-marchand/src/features/mise-a-jour/porte.ts`). La raison n'est pas le
confort : un terminal sur lequel on vient de poser un APK à la main, hors ligne,
resterait bloqué par un verdict serveur périmé, sans aucun moyen de le lever.
Lui seul connaît la version qu'il exécute vraiment.
"""
import re

#: Un seuil se saisit en TROIS segments, et rien d'autre.
#:
#: Le laxisme serait un piège ici : « 1.2 » se compare à « 1.2.0 » sans ambiguïté
#: pour la machine, mais celui qui l'a tapé croit peut-être avoir écrit « la
#: dernière 1.2 ». On refuse à la SAISIE plutôt que de deviner au réveil d'un
#: terminal, où plus personne ne peut corriger.
SEMVER = re.compile(r'^\d+\.\d+\.\d+$')

#: Le préfixe numéro-pointé de tête. Ce qui suit est IGNORÉ (« 1.2.3-rc.1 »,
#: « 1.2.3 (42) ») : on ne publie pas de version candidate au magasin, et
#: ordonner un suffixe qu'on ne sait pas lire reviendrait à bloquer sur
#: l'illisible - exactement ce que ce mécanisme ne doit jamais faire.
_PREFIXE_NUMERIQUE = re.compile(r'^\d+(?:\.\d+)*')

#: Au-delà, `int` reste exact en Python mais le MIROIR JavaScript perd la
#: précision (`Number.MAX_SAFE_INTEGER`). On refuse des deux côtés, sinon le
#: serveur et le terminal pourraient ordonner deux versions différemment, en
#: silence.
MAX_SEGMENT = 9007199254740991


def segments_version(valeur):
    """Les segments ENTIERS d'une version, ou `None` si elle n'est pas lisible."""
    if not valeur:
        return None
    correspondance = _PREFIXE_NUMERIQUE.match(str(valeur).strip())
    if not correspondance:
        return None
    segments = [int(s) for s in correspondance.group(0).split('.')]
    if any(s > MAX_SEGMENT for s in segments):
        return None
    return segments


def comparer_versions(a, b):
    """
    `-1`, `0`, `1`, ou **`None` quand l'une des deux n'est pas lisible**.

    La plus courte est complétée par des ZÉROS : « 1.2 » vaut « 1.2.0 ». La
    compléter autrement, ou tronquer la plus longue, ferait dire à la comparaison
    autre chose que ce qui a été saisi.

    ⚠ La comparaison est ENTIÈRE, segment par segment. En chaînes, « 1.10.0 »
    passerait sous « 1.9.0 » et le seuil s'appliquerait à l'envers.
    """
    gauche, droite = segments_version(a), segments_version(b)
    if gauche is None or droite is None:
        return None

    taille = max(len(gauche), len(droite))
    gauche = gauche + [0] * (taille - len(gauche))
    droite = droite + [0] * (taille - len(droite))

    for x, y in zip(gauche, droite):
        if x != y:
            return -1 if x < y else 1
    return 0


#: Une fiche Play, dont on sait extraire le nom de paquet.
_FICHE_PLAY = re.compile(
    r'^https?://play\.google\.com/store/apps/details\?(?:[^#]*&)?id=([A-Za-z0-9_.]+)'
)


def urls_magasin(plateforme, url):
    """
    Les adresses à essayer, **DANS L'ORDRE**, pour atteindre la fiche de l'application.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LA DÉRIVATION VIT ICI, ET SURTOUT PAS DANS LE TERMINAL.                  │
    │                                                                          │
    │ Un client qui fabriquerait `market://details?id=` depuis son propre nom  │
    │ de paquet enverrait les APK de préproduction vers une fiche              │
    │ `com.cdir.ventefacile.preview` qui n'existe pas. Et certains terminaux   │
    │ de comptoir n'ont aucun magasin : leur adresse est celle d'un revendeur  │
    │ ou d'un APK direct, et doit pouvoir changer sans publier de version.     │
    └──────────────────────────────────────────────────────────────────────────┘

    `market://` d'abord, pour que l'application du magasin s'ouvre directement
    plutôt qu'un sélecteur ou un navigateur. Le repli `https` ferme la marche :
    il ouvre le magasin par lien universel sur iOS, et au pire une page web sur
    un terminal qui n'a pas le Play Store.

    Une URL qui n'est pas une fiche Play est descendue **telle quelle**, sans
    interprétation : c'est ce qui permet de servir un lien d'APK ou une galerie
    tierce sans toucher au code.
    """
    url = (url or '').strip()
    if not url:
        return []

    if plateforme == 'android':
        correspondance = _FICHE_PLAY.match(url)
        if correspondance:
            return [f'market://details?id={correspondance.group(1)}', url]

    return [url]
