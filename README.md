# scf-data — collecte de données, hors machine locale

Ce dépôt collecte les données publiques qui alimentent un site d'analyse de marché.
Il tourne **entièrement sur GitHub Actions** : aucune machine personnelle dans la boucle.

## Pourquoi il existe

La collecte tournait sur un ordinateur portable via `launchd`. Un portable dort, et
macOS ne rattrape pas les créneaux manqués : chaque nuit de veille figeait les données
publiées, parfois 10 à 19 heures. Cinq récidives du même symptôme ont montré que le
problème n'était pas dans les garde-fous mais dans le postulat — **une machine qui
dort ne peut pas garantir une donnée fraîche**.

## Organisation

| Dossier | Rôle |
|---|---|
| `tools/` | outillage transverse (sonde des sources, orchestrateur) |
| `scripts/` | collecteurs, un par source |
| `cache/` | sorties, réécrites par les workflows et servies au site |
| `.github/workflows/` | cadences |

## Ce qui n'est PAS ici

Tout ce qui touche à des données personnelles (portefeuille, profil, convictions)
reste dans un dépôt privé séparé. Ce dépôt-ci ne contient que de la donnée déjà
publique et les traitements qui la produisent.

## Sonde des sources

`tools/probe_sources.py` teste ~36 sources et dit lesquelles répondent. Il tourne
à l'identique en local et sur un runner : c'est la **comparaison** des deux qui
révèle ce qu'un changement d'IP coûte (géoblocage, quotas, anti-bot).

```bash
python tools/probe_sources.py
```
