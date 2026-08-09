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
    # Même famille, découverte au deuxième passage : ces cinq-là fabriquent une page
    # et RIEN d'autre. Leur donnée est déjà collectée par un job distinct (le calcul
    # de corrélations, le radar de backtest, les tendances de recherche…), donc les
    # laisser sur la machine ne fige aucune donnée — ça évite juste d'installer R
    # sur un runner pour produire un fichier que le runner ne publie pas.
    "com.bassetti.correlations.refresh", "com.bassetti.backtest.refresh",
    "com.bassetti.gtrends.refresh", "com.bassetti.macrotrends.refresh",
    "com.bassetti.modelesvalo.refresh",
}

# Un job peut MÊLER collecte et rendu. On ne veut alors migrer que la collecte : le
# cloud rafraîchit la donnée, la machine continue de fabriquer la page. Sans cette
# distinction, il faudrait choisir entre laisser la donnée figée ou installer R sur
# un runner pour rien.
# ── CADENCES : classées par VITESSE RÉELLE DE LA SOURCE ──────────────────────
# Les cadences d'origine venaient de la machine, où elles étaient conservatrices pour
# une raison qui n'existe plus : batterie, veille, processeur partagé. Sur des serveurs
# gratuits et illimités, la seule question qui vaille est « à quelle vitesse cette
# source change-t-elle ? ». Un site de marché doit afficher le funding à l'heure, pas
# à six heures — mais interroger la Banque mondiale toutes les dix minutes ne ferait
# que du bruit et des refus.
#
# TROIS CONTRAINTES QUI BORNENT L'ACCÉLÉRATION, mesurées et non supposées :
#   · quota strict — les tendances de recherche passent par un service à 250 requêtes
#     PAR MOIS. Les accélérer les casserait en quelques jours ;
#   · durée d'exécution — le lot d'actions met ~50 min : le passer sous l'heure ferait
#     se chevaucher deux exécutions sur les mêmes fichiers ;
#   · politesse — treize collecteurs tapent la même API gratuite de cotations crypto,
#     limitée à la minute. Les grouper trop serré provoque des refus, donc des trous.
CADENCE_OVERRIDES = {
    # ── 5 min : ce qui bouge en séance ────────────────────────────────────────
    "com.bassetti.moneyflow": "5min",         # flux intrajournaliers
    "com.bassetti.stockbubble": "5min",       # cotations des actions suivies

    # ── 5 min : guetteurs de publication. Ce sont EUX qui portent la valeur sur un
    # site de marché — un chiffre macro ou une dépêche n'ont d'intérêt qu'à l'instant
    # où ils tombent. Ils repassent en plus par une rafale interne (voir REPETITIONS
    # dans l'orchestrateur) qui ramène le délai réel sous la minute et demie.
    "com.bassetti.macrocal": "5min",          # chiffres macro à l'instant de leur sortie
    "com.bassetti.news": "5min",
    "com.bassetti.fjnews": "5min",
    "com.bassetti.earningscal": "5min",       # résultats publiés dans la minute

    # ── 10 min ────────────────────────────────────────────────────────────────
    "com.bassetti.treasury": "10min",         # dépôts SEC + valorisation au spot

    # ── 1 h : marchés et dérivés. Étaient à 6 h, sans raison autre que le Mac ──
    "com.bassetti.radardata": "1h",           # financement, intérêt ouvert, ratios
    "com.bassetti.globalmarkets": "1h",       # indices mondiaux
    "com.bassetti.cryptoytd": "1h",
    "com.bassetti.cyclecache": "1h",
    "com.bassetti.btccycle.live": "1h",
    "com.bassetti.mag7hist": "1h",
    "com.bassetti.perdata": "1h",
    "com.bassetti.pehist": "1h",
    "com.bassetti.crypto-pe-hist": "1h",
    "com.bassetti.fedwatch.refresh": "1h",    # probabilités de taux, très réactives
    "com.bassetti.cryptoetf": "1h",           # flux ETF quotidiens, publiés en journée
    "com.bassetti.atlasdebt": "1h",           # taux souverains à 10 ans
    "com.bassetti.energymacro": "1h",         # pétrole, spreads de raffinage
    "com.bassetti.leveredetf": "1h",
    "com.bassetti.tradficycle": "1h",
    "com.bassetti.predmarkets": "1h",         # cotes de marchés de prédiction
    "com.bassetti.defiengagement.refresh": "1h",
    "com.bassetti.newlistings": "1h",
    "com.bassetti.l1valuation": "1h",
    "com.bassetti.backtestradar": "1h",
    "com.bassetti.macro-corr": "1h",
    "com.bassetti.macrofred.refresh": "1h",   # séries officielles, publiées en journée
    "com.bassetti.fredstress.refresh": "1h",  # indicateurs de tension financière

    # ── 6 h : lourd, ou source qui ne publie pas plus vite ─────────────────────
    "com.bassetti.tradfi": "6h",              # ~50 min d'exécution : plancher physique
    "com.bassetti.tradfifund": "6h",
    "com.bassetti.tradfihist": "6h",
    "com.bassetti.tradfi-growth": "6h",
    "com.bassetti.narrfund": "6h",
    "com.bassetti.radarv3.refresh": "6h",

    "com.bassetti.fluxphysiques": "6h",
    "com.bassetti.hydrocarbures": "6h",
    "com.bassetti.narratives": "6h",
    "com.bassetti.sentiment.fng": "6h",
    "com.bassetti.ecosysteme.indicators": "6h",
    "com.bassetti.l1history": "6h",
    "com.bassetti.ipocal": "6h",
    "com.bassetti.atlasmaritime": "6h",       # ~11 min d'exécution

    # ── quotidien : sources qui ne publient qu'une fois par jour, ou moins ─────
    "com.bassetti.gtrends.serpapi": "daily",  # QUOTA 250/mois — surtout ne pas monter
    "com.bassetti.gtrendshype": "daily",      # même service, même quota
    "com.bassetti.atlaseco": "daily",         # Banque mondiale, FMI : trimestriel
    "com.bassetti.atlasquarterly": "daily",
    "com.bassetti.atlasdetail": "daily",
    "com.bassetti.atlasbudget": "daily",
    "com.bassetti.buffettcash": "daily",      # dépôts trimestriels
    "com.bassetti.financiarisation": "daily",
    "com.bassetti.ecophysique": "daily",
    "com.bassetti.globalhist": "daily",
    "com.bassetti.pehistglobal": "daily",
    "com.bassetti.creditprive": "daily",
    "com.bassetti.finance_americaine": "daily",
    "com.bassetti.ai_adoption": "daily",
    "com.bassetti.btccycle": "daily",         # doublon lent de la version live

    # ── 6 h : pages de thèse. Elles agrègent des séries macro qui bougent peu en
    # séance, mais leurs chiffres de marché méritaient mieux que deux fois par jour.
    "com.bassetti.these_bitcoin.refresh": "6h",
    "com.bassetti.these_bulleia.refresh": "6h",
    "com.bassetti.these_dette.refresh": "6h",
    "com.bassetti.these_dollar.refresh": "6h",
    "com.bassetti.these_effondrement.refresh": "6h",
    "com.bassetti.these_energie.refresh": "6h",
    "com.bassetti.these_stagnation.refresh": "6h",
    "com.bassetti.these_web3.refresh": "6h",

    # ── hebdomadaire : données structurelles, publiées quelques fois par an ────
    "com.bassetti.frbudget": "weekly",           # budget de l'État
    "com.bassetti.tradfiallocation.refresh": "weekly",
}

SCRIPT_OVERRIDES = {
    "com.bassetti.l1valuation": "fetch_l1_valuation.py",   # au lieu du refresh complet
    # Ces quatre-là sont enveloppés dans un script dont l'unique raison d'être est
    # d'EMPÊCHER LE MAC DE S'ENDORMIR pendant un fetch long (`caffeinate`, échéance à
    # l'horloge murale, anti-zombie). Sur un serveur qui ne dort pas, cette précaution
    # n'a plus d'objet — et `caffeinate` n'existe pas sous Linux. On appelle donc
    # directement le collecteur qu'elles protégeaient.
    "com.bassetti.news": "fetch_news.py",
    "com.bassetti.tradfi": "fetch_tradfi.py",
    "com.bassetti.predmarkets": "fetch_prediction_markets.py",
    "com.bassetti.fjnews": "fetch_fj_news.py",
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


# ── Collecteurs nés après la migration ────────────────────────────────────────
# Ceux-là n'ont jamais eu de plist launchd : ils sont écrits directement pour les
# workflows. Sans cette liste, une régénération de `jobs.json` les effacerait
# silencieusement — le fichier ne connaît que ce que les plists racontent, et ces
# collecteurs-là ne racontent rien à personne. Toute nouveauté vient donc ici.
SANS_PLIST = [
    dict(id="orderflow.funding", script="fetch_loris_funding.py", args=[],
         schedule="toutes les 5 min", per_day=288.0, cadence="5min",
         category="public",
         outputs=["orderflow_funding_cache.js", "orderflow_funding_cache.json",
                  "orderflow_funding_hist_1d.json", "orderflow_funding_hist_1h.json",
                  "orderflow_funding_hist_5m.json"],
         witness="orderflow_funding_cache.json"),
    dict(id="tradfi.gamma", script="fetch_cboe_gamma.py", args=[],
         schedule="1 fois/jour", per_day=1.0, cadence="daily",
         category="public",
         outputs=["tradfi_gamma_cache.js", "tradfi_gamma_cache.json",
                  "tradfi_gamma_hist.json"],
         witness="tradfi_gamma_cache.json"),
]


def main():
    witness = {}
    if os.path.exists(HEALTH):
        for r in json.load(open(HEALTH)).get("rows", []):
            witness[r["job"]] = r.get("cache")

    jobs = [dict(j) for j in SANS_PLIST]
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
        # Les arguments qui SUIVENT le script. On les perdait : seul le nom du fichier
        # était retenu, et le runner lançait donc une commande différente de celle qui
        # tourne sur la machine d'origine depuis toujours. Vingt et un collecteurs sont
        # concernés, et le silence était total — un `--force` absent fait sortir sans
        # rien faire, un `--resume` absent fait repartir de zéro et perdre l'historique
        # accumulé (constaté : 138 valeurs publiées au lieu de 781 pour l'historique
        # fondamental TradFi). On prend la queue entière, pas les seuls tirets : des
        # options portent une valeur (`--region all`).
        i_script = next((k for k, a in enumerate(args)
                         if a.endswith((".py", ".sh"))), None)
        job_args = list(args[i_script + 1:]) if i_script is not None else []
        sched, per_day = schedule_of(d)
        cadence = CADENCE_OVERRIDES.get(label)
        cat = "perso" if label in PERSO else "locale" if label in LOCALE else "public"
        # `jobs.json` part dans un dépôt public : l'étiquette launchd complète
        # (« com.<compte>.treasury ») y révélerait le compte de l'utilisateur pour
        # rien. Seul l'identifiant court est publié — c'est la seule partie utile.
        # On retire le PRÉFIXE seulement : couper au dernier point ferait entrer en
        # collision « gtrends.refresh » et « gtrends.serpapi », deux jobs distincts.
        jobs.append(dict(id=re.sub(r"^com\.[^.]+\.", "", label),
                         script=script, args=job_args, schedule=sched, per_day=per_day,
                         cadence=cadence,
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
