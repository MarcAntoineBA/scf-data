#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inventory.py — Dresse la carte de migration du parc de collecte.

Ne tourne QUE sur la machine d'origine (il lit les plists launchd) : le résultat,
`jobs.json`, est commité et devient la source de vérité des workflows. Le runner ne
relit jamais les plists.

Pour chaque job : le script exécuté, sa cadence, les fichiers qu'il produit, et sa
CATÉGORIE — c'est elle qui décide de sa destination :

  public   → dépôt public, minutes GitHub illimitées (donnée déjà publique sur le site)
  perso    → dépôt privé (portefeuille, profil, convictions : rien de tout ça en public)
  locale   → reste sur la machine : publication, serveurs, surveillance. Ces jobs
             n'ont pas de raison d'exister ailleurs, ou disparaissent avec la migration.

Le classement se fait par liste EXPLICITE, jamais par heuristique : se tromper de
catégorie ferait fuiter un fichier personnel dans un dépôt public — un risque qu'on
ne confie pas à un motif de nom de fichier.
"""

import json
import os
import plistlib
import re
import sys
from glob import glob

LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
APPSUP = os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")
HEALTH = os.path.expanduser("~/Library/Caches/site_crypto_finance/freshness_health.json")

# ── Classement explicite ──────────────────────────────────────────────────────
# Tout ce qui touche au patrimoine, au profil ou aux convictions de l'utilisateur.
PERSO = {
    "com.bassetti.portefeuille",        # positions et montants détenus
    "com.bassetti.advisor",             # profil d'investisseur, convictions
    "com.bassetti.advisor.flows",
    "com.bassetti.advisor.server",
}

# Infrastructure de la machine : publication, serveurs locaux, surveillance du parc.
# Ces jobs ne migrent pas — la plupart perdent leur raison d'être une fois la
# collecte dans le cloud (les watchdogs surveillaient un parc qui dormait).
LOCALE = {
    "com.bassetti.snapshot", "com.bassetti.cfdeploy", "com.bassetti.fastpublish",
    "com.bassetti.siteserver", "com.bassetti.syncthing", "com.bassetti.syncthing.watchdog",
    "com.bassetti.wakekicker", "com.bassetti.freshness.watchdog", "com.bassetti.cfwatchdog",
    "com.bassetti.syncprod", "com.bassetti.savoir",
    # Module « savoir/souveraineté » : dépôt privé, base D1, contenu non publié tel quel.
    "com.bassetti.souv_carte", "com.bassetti.souv_grind", "com.bassetti.souv_links",
    # RENDU DE PAGE, pas de la collecte : ces jobs appellent rmarkdown::render. Ils
    # exigent R, qui n'existe pas sur un runner — et le rendu des pages reste de toute
    # façon sur la machine, ce n'est pas ce qu'on migre. Constaté en conditions réelles :
    # « /usr/local/bin/Rscript: No such file or directory ».
    "com.bassetti.bulleai.refresh", "com.bassetti.ecosysteme.refresh",
}

# Un job peut MÊLER collecte et rendu. On ne veut alors migrer que la collecte : le
# cloud rafraîchit la donnée, la machine continue de fabriquer la page. Sans cette
# distinction, il faudrait choisir entre laisser la donnée figée ou installer R sur
# un runner pour rien.
SCRIPT_OVERRIDES = {
    "com.bassetti.l1valuation": "fetch_l1_valuation.py",   # au lieu du refresh complet
}

CACHE_RE = re.compile(r"[\w\-.]+_(?:cache|live|light|data)[\w\-.]*\.(?:js|json)|"
                      r"[\w\-]+\.(?:js|json)(?=[\"'])")


def schedule_of(d):
    """Cadence lisible + nombre d'exécutions par jour."""
    if d.get("StartInterval"):
        s = d["StartInterval"]
        return f"toutes les {s/3600:.2g} h", round(86400 / s, 2)
    sc = d.get("StartCalendarInterval")
    if sc:
        n = len(sc) if isinstance(sc, list) else 1
        return f"{n} créneau(x)/jour", float(n)
    if d.get("KeepAlive"):
        return "permanent", 0.0
    return "inconnue", 0.0


def outputs_of(script_name, witness):
    """Fichiers produits : le cache témoin du bilan de fraîcheur (fiable, mesuré),
    complété par ce qu'on lit dans le script (utile mais bruité — on dédoublonne)."""
    out = set()
    if witness:
        out.add(witness)
    path = os.path.join(APPSUP, script_name) if script_name else None
    if path and os.path.exists(path):
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            txt = ""
        for m in re.finditer(r"[\"']([\w\-.]+_(?:cache|live|light|alert)[\w\-.]*\.(?:js|json))[\"']", txt):
            out.add(m.group(1))
    return sorted(out)


def main():
    witness = {}
    if os.path.exists(HEALTH):
        for r in json.load(open(HEALTH)).get("rows", []):
            witness[r["job"]] = r.get("cache")

    jobs = []
    for p in sorted(glob(os.path.join(LAUNCH_AGENTS, "com.bassetti.*.plist"))):
        try:
            d = plistlib.load(open(p, "rb"))
        except Exception as e:
            print(f"  ! plist illisible {os.path.basename(p)} : {e}", file=sys.stderr)
            continue
        label = d.get("Label") or os.path.basename(p)[:-6]
        args = d.get("ProgramArguments", [])
        script = next((os.path.basename(a) for a in args
                       if a.endswith((".py", ".sh"))), "")
        script = SCRIPT_OVERRIDES.get(label, script)
        sched, per_day = schedule_of(d)
        cat = "perso" if label in PERSO else "locale" if label in LOCALE else "public"
        # `jobs.json` part dans un dépôt public : l'étiquette launchd complète
        # (« com.<compte>.treasury ») y révélerait le compte de l'utilisateur pour
        # rien. Seul l'identifiant court est publié — c'est la seule partie utile.
        # On retire le PRÉFIXE seulement : couper au dernier point ferait entrer en
        # collision « gtrends.refresh » et « gtrends.serpapi », deux jobs distincts.
        jobs.append(dict(id=re.sub(r"^com\.[^.]+\.", "", label),
                         script=script, schedule=sched, per_day=per_day,
                         category=cat, outputs=outputs_of(script, witness.get(label)),
                         witness=witness.get(label)))

    by_cat = {}
    for j in jobs:
        by_cat.setdefault(j["category"], []).append(j)

    print(f"{len(jobs)} jobs")
    for cat in ("public", "perso", "locale"):
        lst = by_cat.get(cat, [])
        runs = sum(j["per_day"] for j in lst)
        print(f"  {cat:7} {len(lst):3} jobs   {runs:7.0f} exécutions/jour")

    unknown = [j for j in jobs if j["category"] == "public" and not j["script"]]
    if unknown:
        print(f"\n  ! {len(unknown)} job(s) sans script identifié : "
              + ", ".join(j["id"] for j in unknown))

    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jobs.json")
    with open(dest, "w") as f:
        json.dump(dict(jobs=sorted(jobs, key=lambda j: (j["category"], j["id"]))), f,
                  indent=1, ensure_ascii=False)
    print(f"\n→ {os.path.normpath(dest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
