"""
Exceptions métier communes.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN REFUS MÉTIER N'EST PAS UNE PANNE, ET LA DIFFÉRENCE SE PAIE CHER.         │
│                                                                              │
│ `apps/sync/operations.py::_classify` range une exception en verdict, et la   │
│ frontière qui compte est celle-ci : un refus DÉTERMINISTE ne doit jamais     │
│ être réessayé, une panne technique doit l'être. Tout ce que `_classify` ne   │
│ reconnaît pas tombe en `retry`.                                              │
│                                                                              │
│ Or deux refus parfaitement déterministes étaient levés en `ValueError` :     │
│ « Points insuffisants » et « La quantité totale est inférieure au contenu    │
│ des conditionnements vendus. » Un terminal les renvoyait donc indéfiniment,  │
│ à chaque synchronisation, pour une vente que le serveur refusera toujours.   │
│                                                                              │
│ Ranger `ValueError` en bloc du côté des refus aurait été pire : les erreurs  │
│ de programmation seraient parties en quarantaine, silencieusement, comme si  │
│ le marchand avait mal saisi. On NOMME donc le refus métier.                  │
└──────────────────────────────────────────────────────────────────────────────┘
"""


class RefusMetier(ValueError):
    """
    Un refus que la règle métier oppose, et qui se reproduira à l'identique.

    Hérite de `ValueError` pour ne rien casser des appelants existants, qui
    l'attrapaient déjà sous ce nom. Le chemin web y gagne un 400 là où il
    rendait un 500 ; le chemin du journal y gagne une quarantaine lisible là
    où il rejouait sans fin.
    """
