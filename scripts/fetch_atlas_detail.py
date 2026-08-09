#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache « Atlas Économique — DÉTAIL » : grand dossier par pays (217 pays).

Page consommatrice : Atlas_Economique.html → grand dossier (lazy fetch de
`atlas_detail_cache.json`, sinon window.__ATLAS_DETAIL__ inliné par le .js).
Sorties : ~/Library/Caches/site_crypto_finance/atlas_detail_cache.json
          ~/Library/Caches/site_crypto_finance/atlas_detail_cache.js  (window.__ATLAS_DETAIL__=…)
Logos    : assets/logos/atlas/<ISO3>/<ticker|slug>.<svg|png>  (repo Desktop en run
           manuel, sinon ~/Library/Caches/site_crypto_finance/atlas_logos ; env ATLAS_LOGO_DIR).
Label launchd prévu : scf.atlasdetail (StartInterval 86400, logs /tmp/atlasdetail.*.log).

Ce fetcher est le PENDANT LOURD de fetch_atlas_econ.py (dont il réutilise le style :
http_get(_json)+retry/backoff, _pack_series, sig4/r1/r2, résolution multi-chemins du
meta, double sortie .json/.js, garde anti-écrasement + garde PAR SOURCE). Il ne touche
PAS atlas_econ_cache.* .

SCHÉMA (SPEC v2 §2) — par pays : hist{métrique:{s,v}} + hist_meta{forecast_from}
+ trade{sectors_va,openness,eci,top_exports,partners_exp,overview_text} + companies{…}.

SOURCES (toutes testées 2026-07-05, AUCUNE clé/token — l'utilisateur refuse les clés) :
  §3a HISTO
    WB    séries longues date=1960:CUR paginées, 9 indicateurs (gdp, gdp_pc, reserves,
          pop, pop_gr, life, gini, urban, unemp) — ⚠ 502 Azure intermittent → retry/backoff.
    WGI   GOV_WGI_{RL,GE,PV,CC}.EST &source=3, séries longues (depuis 1996).
    IMF   DataMapper NGDP_RPCH/PCPIPCH/LUR/BCA_NGDPD/GGXWDG_NGDP/GGXCNL_NGDP — série
          COMPLÈTE avec prévisions →2031 ; forecast_from = année courante.
    OWID  freedom-score-fh.csv + liberal-democracy-index.csv (toutes années) + CPI
          historique (grapher corruption-perception-index si dispo).
    PISA  OCDE via miroir OWID grapher MULTI-DIMENSIONS academic-performance.csv
          ?subject={mathematics|reading|science}&sex=both — 1 appel par matière
          (⚠ « math » = HTTP 500, il faut « mathematics » ; pas de grapher dédié
          pour les sciences). 2000→2022, ~86 pays de l'Atlas. Composite = moyenne
          des 3 matières, uniquement les vagues où les 3 existent (donc 2006+).
          Moyenne OCDE captée comme repère → meta.pisa_oecd.
  §3b COMMERCE
    WB    structure NV.AGR/IND/IND.MANF/SRV.TOTL.ZS + ouverture NE.EXP/IMP/TRD.GNFS.ZS
          + TX.VAL.TECH.MF.ZS + BX.KLT.DINV.WD.GD.ZS (mrnev=1).
    OEC   tesseract complexity_eci_a_hs22_hs4 → ECI + rang (2022, 131 pays ; id=cont2+ISO3).
    HARV  CSV Harvard Atlas locaux (scratchpad) hs4.csv (top exports+RCA+part mondiale,
          year=max, tri par valeur) + bilat.csv (partenaires). --skip-harvard réutilise.
    FBK   CIA World Factbook JSON (domaine public) Economy>Economic overview (verbatim) ;
          GEC→ISO3 via geonames fips ; GEC→région via git tree factbook.json.
  §3c ENTREPRISES (hybride, honnête, jamais d'invention)
    CMC   companiesmarketcap.com pages pays (classement mcap USD, logo on-page) ~65 pays.
    QLV   Wikidata via QLever qlever.dev/api/wikidata (PAS WDQS) : mcap datée (USA + repli),
          revenu (pays maigres), + enrichissement QID/ISIN/secteur/logo P154.
    WIKI  Wikipedia « List of largest companies in X » (marchés frontière, ex. Nigéria).
    coverage: full(≥8 mcap) | partial | thin(banques/état, métrique alt) ; companies:null sinon.
  §3d LOGOS (local, sans clé) : Wikidata P154 (Special:FilePath, svg) → CMC
          /img/company-logos/64/<TICKER>.png → Google favicon t3.gstatic.com. Idempotent.

GARDE-FOUS : garde globale (aucune source majeure OK & cache existant → pas d'écrasement) ;
  garde PAR SOURCE (source en échec → bloc recopié du cache précédent + sources_failed) —
  implémentée en partant d'une copie du cache précédent, écrasée seulement par les sources OK.

CLI : --dry-run, --no-logos, --only FRA,USA,… , --skip-harvard, --reprice (Yahoo, opt-in).

Interpréteur : /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 (curl_cffi présent).
Meta pays : assets/js/atlas/atlas_countries_meta.js (override env ATLAS_META_PATH).
"""

import argparse
import copy
import csv
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# ── productivité marginale de la dette (formule partagée) ─────────────────────
# atlas_mpd.py vit à côté de ce script. Sous launchd, le CWD n'est PAS le dossier
# du script : sans ce sys.path, l'import casse en production et pas en local.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from atlas_mpd import inject_mpd  # noqa: E402
# ── volet « Prix & inflation » (structure et dynamique des prix) ──────────────
# Même règle que ci-dessus : le module vit à côté du script, l'import passe par
# le sys.path fixé juste au-dessus.
from atlas_prix import fetch_oecd_prices, inject_prix  # noqa: E402

# ── timeout global (sécurité launchd) ─────────────────────────────────────────
# 75 min et non 50 : le run du 2026-08-08 a été TUÉ À 50 MIN alors qu'il avait
# déjà tout collecté — il ne lui restait qu'à écrire. Le cache est resté sur sa
# version de la veille, sans qu'aucune source soit en panne. Même erreur que le
# lot 6 h de scf-data, dont le plafond valait exactement la durée du job : un
# plafond SANS MARGE transforme un run lent en panne silencieuse.
# ⚠ Ce n'est pas une licence à faire grossir le job : le volet Prix a été rendu
# moins coûteux en parallèle (une requête OCDE de moins). Si on repasse près de
# 75 min, réduire le travail, pas remonter le plafond.
import signal as _signal

GLOBAL_TIMEOUT_MIN = int(os.environ.get("ATLAS_DETAIL_TIMEOUT_MIN", "75"))


def _timeout_handler(signum, frame):
    sys.stderr.write(f"[fatal] global timeout ({GLOBAL_TIMEOUT_MIN} min) — abort\n")
    sys.exit(2)


try:
    _signal.signal(_signal.SIGALRM, _timeout_handler)
    _signal.alarm(GLOBAL_TIMEOUT_MIN * 60)
except Exception:
    pass

# ── curl_cffi obligatoire (Yahoo/IMF/QLever bloquent urllib) ──────────────────
try:
    from curl_cffi import requests as cr
except ImportError:
    sys.stderr.write(
        "[fatal] curl_cffi introuvable — utiliser "
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3\n"
    )
    sys.exit(2)

# ── chemins ───────────────────────────────────────────────────────────────────
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "atlas_detail_cache.json"
OUT_JS = CACHE_DIR / "atlas_detail_cache.js"
REF_DIR = CACHE_DIR / "atlas_detail_ref"      # tables de correspondance mises en cache
REF_DIR.mkdir(parents=True, exist_ok=True)

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_DESKTOP = Path.home() / "Desktop" / "Site_Crypto_Finance"

_META_CANDIDATES = [
    os.environ.get("ATLAS_META_PATH"),
    str(_SCRIPT_DIR / "assets" / "js" / "atlas" / "atlas_countries_meta.js"),
    str(_REPO_DESKTOP / "assets" / "js" / "atlas" / "atlas_countries_meta.js"),
]
META_PATH = next((p for p in _META_CANDIDATES if p and Path(p).exists()), _META_CANDIDATES[1])

# Répertoire des CSV Harvard (déjà téléchargés par les agents recherche).
_HARVARD_CANDIDATES = [
    os.environ.get("ATLAS_HARVARD_DIR"),
    str(_SCRIPT_DIR / "atlas_harvard"),
    str(_REPO_DESKTOP / "atlas_harvard"),
]
HARVARD_DIR = next((p for p in _HARVARD_CANDIDATES if p and Path(p).exists()
                    and (Path(p) / "hs4.csv").exists()), None)

# Répertoire logos : env > repo Desktop (run manuel) > Caches (launchd).
if os.environ.get("ATLAS_LOGO_DIR"):
    LOGO_DIR = Path(os.environ["ATLAS_LOGO_DIR"])
elif os.access(_REPO_DESKTOP / "assets" / "logos", os.W_OK):
    LOGO_DIR = _REPO_DESKTOP / "assets" / "logos" / "atlas"
else:
    LOGO_DIR = CACHE_DIR / "atlas_logos"

NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()
CUR_YEAR = NOW.year
FORECAST_FROM = CUR_YEAR


# ── HTTP (repris de fetch_atlas_econ.py) ──────────────────────────────────────

def http_get(url, timeout=60, retries=4, binary=False, sleep_base=1.6,
             method="GET", data=None, headers=None):
    """GET/POST via curl_cffi impersonate chrome120, backoff 1.6^n. None si échec.

    ⚠ WB renvoie parfois HTTP 400/502 avec un corps JSON valide → accepté si non-binaire
    et parse en JSON (les vraies erreurs sont rejetées en aval par la structure)."""
    last = None
    for attempt in range(retries):
        try:
            if method == "POST":
                r = cr.post(url, data=data, headers=headers, impersonate="chrome120",
                            timeout=timeout)
            else:
                r = cr.get(url, headers=headers, impersonate="chrome120", timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
            if not binary and r.text and r.text.lstrip()[:1] in ("{", "["):
                try:
                    json.loads(r.text)
                    return r.text
                except ValueError:
                    pass
            if r.status_code in (404, 403):
                break
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(sleep_base ** (attempt + 1))
    if last and "404" not in last:
        sys.stderr.write(f"[WARN] échec {method} {url[:120]} : {last}\n")
    return None


def http_get_json(url, timeout=60, retries=4, method="GET", data=None, headers=None):
    t = http_get(url, timeout=timeout, retries=retries, method=method, data=data, headers=headers)
    if t is None:
        return None
    try:
        return json.loads(t.lstrip("﻿"))
    except ValueError:
        return None


# ── arrondis ──────────────────────────────────────────────────────────────────

def sig4(x):
    if x is None:
        return None
    x = float(x)
    if x == 0:
        return 0
    r = round(x, -int(math.floor(math.log10(abs(x)))) + 3)
    return int(r) if abs(r) >= 1000 or r == int(r) else r


def r1(x):
    if x is None:
        return None
    x = float(x)
    if abs(x) >= 1000:
        return int(round(x))
    r = round(x, 1)
    return int(r) if r == int(r) else r


def r2(x):
    if x is None:
        return None
    r = round(float(x), 2)
    return int(r) if r == int(r) else r


def r3(x):
    if x is None:
        return None
    r = round(float(x), 3)
    return int(r) if r == int(r) else r


def _pack_series(year_map, rnd=None):
    """{année: val} → {'s': première année, 'v': [...]} (trous=null, bords trimmés)."""
    if not year_map:
        return None
    y0, y1_ = min(year_map), max(year_map)
    v = []
    for y in range(y0, y1_ + 1):
        val = year_map.get(y)
        v.append(rnd(val) if (rnd and val is not None) else val)
    if len([x for x in v if x is not None]) < 2:
        return None
    return {"s": y0, "v": v}


def _slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def _safe_fname(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:48] or "logo"


# ── meta pays ────────────────────────────────────────────────────────────────

def load_meta():
    src = Path(META_PATH).read_text(encoding="utf-8")
    m = re.search(r"window\.__ATLAS_META__\s*=\s*", src)
    if not m:
        sys.stderr.write(f"[fatal] window.__ATLAS_META__ introuvable dans {META_PATH}\n")
        sys.exit(2)
    payload = src[m.end():].strip().rstrip(";")
    return json.loads(payload)


# ── tables de correspondance (fetchées une fois, cachées) ─────────────────────

def _cached_fetch(name, url, ttl_days=30, binary=False):
    """Récupère url, met en cache REF_DIR/name. Repli sur le cache si le fetch échoue."""
    p = REF_DIR / name
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl_days * 86400:
        return p.read_bytes() if binary else p.read_text(encoding="utf-8")
    got = http_get(url, timeout=60, binary=binary)
    if got is not None:
        (p.write_bytes if binary else p.write_text)(got if binary else got)
        return got
    if p.exists():
        sys.stderr.write(f"[WARN] {name} : fetch KO, cache {name} réutilisé\n")
        return p.read_bytes() if binary else p.read_text(encoding="utf-8")
    return None


def load_geonames():
    """(iso2name, fips2iso3) depuis geonames countryInfo.txt."""
    t = _cached_fetch("geonames_countryInfo.txt",
                      "https://download.geonames.org/export/dump/countryInfo.txt")
    iso2name, fips2iso = {}, {}
    if not t:
        return iso2name, fips2iso
    for line in t.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split("\t")
        if len(c) > 4 and c[1]:
            iso2name[c[1]] = c[4]
            if c[3]:
                fips2iso[c[3].lower()] = c[1]
    return iso2name, fips2iso


def load_iso2qid():
    """ISO3 (P298) → Wikidata QID via QLever (une requête)."""
    q = ("PREFIX wd: <http://www.wikidata.org/entity/>\n"
         "PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n"
         "SELECT ?c ?iso WHERE { ?c wdt:P298 ?iso . ?c wdt:P31 wd:Q6256 . }")
    p = REF_DIR / "iso2qid.json"
    d = http_get_json("https://qlever.dev/api/wikidata", timeout=60, method="POST",
                      data={"query": q}, headers={"Accept": "application/sparql-results+json"})
    out = {}
    if d and "results" in d:
        for b in d["results"]["bindings"]:
            out[b["iso"]["value"]] = b["c"]["value"].rsplit("/", 1)[-1]
        p.write_text(json.dumps(out), encoding="utf-8")
    elif p.exists():
        out = json.loads(p.read_text(encoding="utf-8"))
        sys.stderr.write("[WARN] iso2qid : QLever KO, cache réutilisé\n")
    return out


def load_factbook_regions():
    """GEC → région (dossier) depuis l'arbre git de factbook.json."""
    d = http_get_json("https://api.github.com/repos/factbook/factbook.json/git/trees/"
                      "master?recursive=1", timeout=60)
    p = REF_DIR / "factbook_gec2region.json"
    out = {}
    if d and "tree" in d:
        for t in d["tree"]:
            path = t.get("path", "")
            if path.endswith(".json") and "/" in path:
                reg, fn = path.split("/", 1)
                if reg in ("meta", "world", "oceans", "antarctica") or "/" in fn:
                    continue
                out[fn[:-5].lower()] = reg
        p.write_text(json.dumps(out), encoding="utf-8")
    elif p.exists():
        out = json.loads(p.read_text(encoding="utf-8"))
        sys.stderr.write("[WARN] factbook regions : git tree KO, cache réutilisé\n")
    return out


def load_hs4_en():
    """{code HS4 → description EN courte}."""
    t = _cached_fetch("harmonized-system.csv",
                      "https://raw.githubusercontent.com/datasets/harmonized-system/"
                      "master/data/harmonized-system.csv")
    out = {}
    if not t:
        return out
    for row in csv.DictReader(io.StringIO(t)):
        if row.get("level") == "4":
            desc = (row.get("description") or "").split(";")[0].split(",")[0].strip()
            out[row["hscode"].zfill(4)] = desc[:60]
    return out


def load_wb_regions():
    """{iso3: region_id WB} depuis l'API country (champ region.id ∈ EAS/ECS/LCN/MEA/
    NAC/SAS/SSF ; « NA » = agrégats WB, écartés). Sert à répartir les pays dans les 7
    régions pour les treemaps agrégés. Réutilise le cache wb_country_en.json (fetch_te_slugs)."""
    raw = _cached_fetch("wb_country_en.json",
                        "https://api.worldbank.org/v2/country?per_page=400&format=json",
                        ttl_days=30)
    out = {}
    if raw:
        try:
            d = json.loads(raw)
            for r in (d[1] or []):
                reg = (r.get("region") or {}).get("id")
                if reg and reg != "NA":
                    out[r["id"]] = reg
        except (ValueError, IndexError, TypeError):
            out = {}
    return out


def load_fx():
    """{devise ISO → taux : 1 unité locale = N USD}. open.er-api.com + repli codé."""
    d = http_get_json("https://open.er-api.com/v6/latest/USD", timeout=30)
    fx = dict(_FX_FALLBACK)
    if d and d.get("result") == "success":
        for ccy, per_usd in d.get("rates", {}).items():
            try:
                if per_usd and float(per_usd) > 0:
                    fx[ccy] = 1.0 / float(per_usd)   # USD par unité locale
            except (TypeError, ValueError):
                continue
        print(f"[FX] open.er-api.com : {len(d.get('rates', {}))} devises")
    else:
        sys.stderr.write("[WARN] FX : open.er-api.com KO → table de repli\n")
    fx["USD"] = 1.0
    return fx


# Repli FX minimal (USD par unité locale) si l'API tombe.
_FX_FALLBACK = {
    "USD": 1.0, "EUR": 1.14, "GBP": 1.33, "JPY": 0.0062, "CNY": 0.147, "INR": 0.0105,
    "NGN": 0.00073, "BRL": 0.192, "VND": 0.0000381, "SGD": 0.774, "KRW": 0.00073,
    "CHF": 1.24, "CAD": 0.73, "AUD": 0.655, "HKD": 0.128, "SAR": 0.267, "AED": 0.272,
    "ZAR": 0.055, "MXN": 0.054, "RUB": 0.0116, "TRY": 0.025, "THB": 0.031,
    "IDR": 0.0000615, "MYR": 0.236, "PHP": 0.0177, "SEK": 0.105, "NOK": 0.0985,
    "DKK": 0.153, "PLN": 0.272, "TWD": 0.0341, "ILS": 0.297,
}

# GEC/factbook codes qui diffèrent de FIPS geonames (complément).
GEC_ISO_EXTRA = {
    "vm": "VNM", "gm": "DEU", "uk": "GBR", "ee": "EST", "ei": "IRL", "sw": "SWE",
    "sz": "CHE", "au": "AUT", "as": "AUS", "ja": "JPN", "ks": "KOR", "sn": "SGP",
    "ni": "NGA", "cg": "COD", "cf": "CAF", "iz": "IRQ", "ir": "IRN", "rs": "RUS",
    " up": "UKR", "da": "DNK", "bo": "BLR", "lo": "SVK", "si": "SVN", "ez": "CZE",
    "hr": "HRV", "gg": "GEO", "am": "ARM", "aj": "AZE", "kz": "KAZ", "kg": "KGZ",
    "ti": "TJK", "tx": "TKM", "uz": "UZB", "sf": "ZAF", "wa": "NAM", "wz": "SWZ",
    "bc": "BWA", "mi": "MWI", "za": "ZMB", "zi": "ZWE", "mz": "MOZ", "ke": "KEN",
    "tz": "TZA", "ug": "UGA", "et": "ETH", "su": "SDN", "od": "SSD", "up": "UKR",
}


# ══ §3a — HISTORIQUE MAX ═══════════════════════════════════════════════════════

WB_HIST = {
    "gdp": ("NY.GDP.MKTP.CD", sig4),
    "gdp_real": ("NY.GDP.MKTP.KD", sig4),        # PIB réel, prix constants 2015 US$ (évolution du niveau)
    "gdp_ppp": ("NY.GDP.MKTP.PP.CD", sig4),       # PIB total en PPA (int. $ courants)
    "gdp_pc": ("NY.GDP.PCAP.PP.CD", sig4),
    "reserves": ("FI.RES.TOTL.CD", sig4),
    "pop": ("SP.POP.TOTL", sig4),
    "pop_gr": ("SP.POP.GROW", r1),
    "life": ("SP.DYN.LE00.IN", r1),
    "gini": ("SI.POV.GINI", r1),
    "urban": ("SP.URB.TOTL.IN.ZS", r1),
    "unemp_wb": ("SL.UEM.TOTL.ZS", r1),
    "exports_gdp": ("NE.EXP.GNFS.ZS", r1),
    "imports_gdp": ("NE.IMP.GNFS.ZS", r1),
    "trade_gdp": ("NE.TRD.GNFS.ZS", r1),
    # v5 — démographie/vieillissement, santé, société numérique, profondeur financière
    "tfr": ("SP.DYN.TFRT.IN", r2),            # fécondité (naissances/femme)
    "dep_tot": ("SP.POP.DPND", r1),           # dépendance totale (% pop. 15-64)
    "dep_old": ("SP.POP.DPND.OL", r1),        # dépendance vieillesse
    "dep_yg": ("SP.POP.DPND.YG", r1),         # dépendance jeunes
    "imr": ("SP.DYN.IMRT.IN", r1),            # mortalité infantile (‰)
    "health_gdp": ("SH.XPD.CHEX.GD.ZS", r1),  # dépenses de santé (% PIB)
    "internet": ("IT.NET.USER.ZS", r1),       # accès Internet (% population)
    "mktcap_gdp": ("CM.MKT.LCAP.GD.ZS", r1),  # capitalisation boursière (% PIB, ⚠ lacunaire)
    "credit_gdp": ("FS.AST.PRVT.GD.ZS", r1),  # crédit au secteur privé (% PIB)
    # Productivité marginale de la dette (cf. atlas_mpd.py) — SERT UNIQUEMENT AU CALCUL.
    # PIB nominal en MONNAIE LOCALE : indispensable pour que le taux de change
    # n'entre pas dans le ratio (numérateur et dénominateur portent sur des années
    # différentes, une dévaluation contaminerait le résultat). Retirée du cache
    # après calcul par inject_mpd() : aucun graphe ne l'affiche.
    "ngdp_lcu": ("NY.GDP.MKTP.CN", sig4),     # PIB nominal, unités de monnaie locale
    # E — profondeur : effort régalien/innovation, précarité jeunes, vulnérabilité externe
    "military": ("MS.MIL.XPND.GD.ZS", r2),    # dépenses militaires (% PIB)
    "rd": ("GB.XPD.RSDV.GD.ZS", r2),          # dépenses R&D (% PIB)
    "youth_unemp": ("SL.UEM.1524.ZS", r1),    # chômage des 15-24 ans (%)
    "extdebt": ("DT.DOD.DECT.GN.ZS", r1),     # dette extérieure (% RNB, ⚠ pays en dév. seulement)
    # v12 — moteurs de la demande & dépendance aux ressources
    "invest": ("NE.GDI.FTOT.ZS", r1),         # formation brute de capital fixe (% PIB) = taux d'investissement
    "savings": ("NY.GNS.ICTR.ZS", r1),        # épargne brute (% PIB)
    "resrents": ("NY.GDP.TOTL.RT.ZS", r2),    # rente des ressources naturelles (% PIB, ⚠ millésime ~2021-2023)
    # v16 — MONTANTS en valeur (USD courants) en regard des métriques %PIB (bascule % ↔ valeur)
    "savings_v": ("NY.GNS.ICTR.CD", sig4),    # épargne brute (USD courants)
    "invest_v": ("NE.GDI.FTOT.CD", sig4),     # FBCF / investissement (USD courants)
    "military_v": ("MS.MIL.XPND.CD", sig4),   # dépenses militaires (USD courants)
    "mktcap_gdp_v": ("CM.MKT.LCAP.CD", sig4), # capitalisation boursière (USD courants)
    "extdebt_v": ("DT.DOD.DECT.CD", sig4),    # dette extérieure totale (USD courants, pays en dév.)
    "cab_v": ("BN.CAB.XOKA.CD", sig4),        # balance courante (USD courants, signé)
    # Potentiel futur — qualité de l'éducation (Harmonized Test Scores, échelle calibrée PISA/TIMSS)
    "eduq": ("HD.HCI.HLOS", r1),              # score d'apprentissage harmonisé (~300-625) = « PISA » large couverture
    # Travail & revenus — qui travaille, combien produit une heure, ce que gagnent les gens.
    # (estimations modélisées OIT diffusées par la BM : millésime CUR_YEAR, très récent)
    "emp_ratio": ("SL.EMP.TOTL.SP.ZS", r1),   # ratio emploi / population 15+
    "lfpr": ("SL.TLF.CACT.ZS", r1),           # taux d'activité 15+
    "lfpr_fe": ("SL.TLF.CACT.FE.ZS", r1),     # taux d'activité des femmes
    "lfpr_ma": ("SL.TLF.CACT.MA.ZS", r1),     # taux d'activité des hommes
    "neet": ("SL.UEM.NEET.ZS", r1),           # jeunes ni en emploi ni en études (NEET)
    "vuln_emp": ("SL.EMP.VULN.ZS", r1),       # emploi vulnérable (proxy d'informalité)
    "selfemp": ("SL.EMP.SELF.ZS", r1),        # emploi indépendant
    "emp_agr": ("SL.AGR.EMPL.ZS", r1),        # emploi agriculture (les 3 parts somment à 100)
    "emp_ind": ("SL.IND.EMPL.ZS", r1),        # emploi industrie
    "emp_srv": ("SL.SRV.EMPL.ZS", r1),        # emploi services
    "gdp_emp": ("SL.GDP.PCAP.EM.KD", sig4),   # PIB par personne employée (PPA const. 2021)
    "gni_pc": ("NY.GNP.PCAP.PP.CD", sig4),    # RNB / habitant (PPA) = le REVENU, ≠ PIB/hab
    "cons_pc": ("NE.CON.PRVT.PC.KD", sig4),   # consommation des ménages / hab (const. 2015)
    # ── v18 — VOLET PRIX & INFLATION (cf. atlas_prix.py pour le sens de chacune) ──
    # Le déflateur mesure le prix de tout ce que le pays PRODUIT, l'IPC celui de
    # ce qu'il CONSOMME : l'écart entre les deux porte les termes de l'échange.
    "deflator": ("NY.GDP.DEFL.KD.ZG", r1),    # déflateur du PIB, variation annuelle (%)
    "m2_gr": ("FM.LBL.BMNY.ZG", r1),          # croissance de la masse monétaire M2 (%)
    "rinr": ("FR.INR.RINR", r1),              # taux d'intérêt réel (déflaté du déflateur)
    # SERVENT UNIQUEMENT AU CALCUL — retirées du cache par inject_prix().
    # Un taux de change brut et un facteur de conversion PPA ne racontent rien à
    # un lecteur ; ce sont leurs dérivées (dépréciation, niveau des prix) qui parlent.
    "fx_lcu": ("PA.NUS.FCRF", sig4),          # taux de change officiel (monnaie locale / USD)
    "ppp_lcu": ("PA.NUS.PPP", sig4),          # facteur de conversion PPA (monnaie locale / int. $)
}
WGI_HIST = {"wgi_rl": "GOV_WGI_RL.EST", "wgi_ge": "GOV_WGI_GE.EST",
            "wgi_pv": "GOV_WGI_PV.EST", "wgi_cc": "GOV_WGI_CC.EST"}


def _wb_series(code, source=None, timeout=120):
    """Série longue 1960:CUR paginée → {iso3: {année: val brute}}. None si échec total."""
    raw, page, pages = {}, 1, 1
    src = f"&source={source}" if source else ""
    while page <= pages:
        url = (f"https://api.worldbank.org/v2/country/all/indicator/{code}"
               f"?format=json&date=1960:{CUR_YEAR}&per_page=18000&page={page}{src}")
        d = http_get_json(url, timeout=timeout, retries=5)
        if not d or len(d) < 2 or not d[1]:
            return raw or None
        pages = int(d[0].get("pages", 1))
        for row in d[1]:
            a3 = row.get("countryiso3code")
            v = row.get("value")
            if not a3 or v is None:
                continue
            try:
                raw.setdefault(a3, {})[int(row["date"])] = float(v)
            except (TypeError, ValueError):
                continue
        page += 1
    return raw or None


# Parts bornées 0-100 du volet Travail : des grandeurs structurelles qui bougent de
# quelques points par an au plus. Une observation isolée qui saute de >25 points par
# rapport à TOUT son voisinage est une rupture de série côté source, pas un fait
# (cas vécu : Biélorussie NEET 5,1 % en 2022 → 65,7 % en 2024, publié par la BM/OIT).
# On la retire — et on l'ÉCRIT sur stderr, pour que la coupe reste auditable.
SPIKE_GUARD_KEYS = ("neet", "emp_ratio", "lfpr", "lfpr_fe", "lfpr_ma",
                    "vuln_emp", "selfemp", "emp_agr", "emp_ind", "emp_srv")
SPIKE_MAX_JUMP = 25.0


def drop_isolated_spikes(ym, field, a3=""):
    """{année: val} → même dict privé des points isolés aberrants (écart > 25 pts avec
    le point précédent ET le suivant ; en fin de série, avec le précédent seul)."""
    if len(ym) < 3:
        return ym
    ys = sorted(ym)
    out, dropped = dict(ym), []
    for i, y in enumerate(ys):
        prev = ym[ys[i - 1]] if i > 0 else None
        nxt = ym[ys[i + 1]] if i < len(ys) - 1 else None
        v = ym[y]
        far_prev = prev is not None and abs(v - prev) > SPIKE_MAX_JUMP
        far_next = nxt is not None and abs(v - nxt) > SPIKE_MAX_JUMP
        if (far_prev and far_next) or (far_prev and nxt is None):
            del out[y]
            dropped.append((y, v))
    if dropped:
        sys.stderr.write(f"[SPIKE] {field} {a3}: point(s) écarté(s) {dropped} "
                         f"(rupture de série > {SPIKE_MAX_JUMP} pts)\n")
    return out


def fetch_wb_hist(a3set):
    """({iso3: {métrique: {s,v}}}, {agrégat: {métrique: {s,v}}}, ok).

    B — la réponse country/all/indicator contient DÉJÀ WLD + 7 régions WB (261 entités) :
    on capte les lignes des agrégats (AGG_SET) EN PLUS des pays, dans le MÊME code que les
    pays → benchmarks Monde/continent aux mêmes unités (ex. gdp_pc PPP identique). AUCUN
    appel supplémentaire. Les agrégats sont renvoyés séparément (jamais routés vers les pays)."""
    out, agg_out, n_ok = {}, {}, 0
    for field, (code, rnd) in WB_HIST.items():
        # WB (Azure) renvoie des 502 intermittents → reprises avec backoff.
        raw = None
        for attempt in range(4):
            raw = _wb_series(code)
            if raw:
                break
            time.sleep(3 * (attempt + 1))
        if not raw:
            sys.stderr.write(f"[WARN] WB hist {code} vide (après reprises)\n")
            continue
        n_ok += 1
        key = "unemp" if field == "unemp_wb" else field
        cnt, acnt = 0, 0
        for a3, ym in raw.items():
            if key in SPIKE_GUARD_KEYS:
                ym = drop_isolated_spikes(ym, key, a3)
            if a3 in a3set:
                s = _pack_series(ym, rnd=rnd)
                if s:
                    out.setdefault(a3, {})[key] = s
                    cnt += 1
            elif a3 in AGG_SET:
                s = _pack_series(ym, rnd=rnd)
                if s:
                    agg_out.setdefault(a3, {})[key] = s
                    acnt += 1
        print(f"[WB.hist] {code}: {cnt} pays" + (f" +{acnt} agrégats" if acnt else ""))
    return out, agg_out, (n_ok >= 5)


def fetch_wgi_hist(a3set):
    """({iso3:{wgi_*:{s,v}}}, {agrégat:{wgi_*:{s,v}}}, ok). B — capte aussi les agrégats
    (si la source WGI publie WLD/régions ; sinon agg_out vide → dégradation propre)."""
    out, agg_out, n_ok = {}, {}, 0
    for field, code in WGI_HIST.items():
        raw = _wb_series(code, source=3, timeout=90)
        if not raw:
            sys.stderr.write(f"[WARN] WGI hist {code} vide\n")
            continue
        n_ok += 1
        cnt, acnt = 0, 0
        for a3, ym in raw.items():
            if a3 in a3set:
                s = _pack_series(ym, rnd=r2)
                if s:
                    out.setdefault(a3, {})[field] = s
                    cnt += 1
            elif a3 in AGG_SET:
                s = _pack_series(ym, rnd=r2)
                if s:
                    agg_out.setdefault(a3, {})[field] = s
                    acnt += 1
        print(f"[WGI.hist] {code}: {cnt} pays" + (f" +{acnt} agrégats" if acnt else ""))
    return out, agg_out, (n_ok >= 2)


IMF_CODES = {"growth": "NGDP_RPCH", "infl": "PCPIPCH", "unemp_imf": "LUR",
             "cab": "BCA_NGDPD", "debt": "GGXWDG_NGDP", "fiscal": "GGXCNL_NGDP",
             # Inflation FIN DE PÉRIODE (décembre contre décembre). Le pendant
             # indispensable de PCPIPCH (moyenne annuelle) : quand la fin de
             # période passe SOUS la moyenne, la désinflation a déjà commencé et
             # la moyenne mettra un an à le dire. Détail dans atlas_prix.py.
             "infl_eop": "PCPIEPCH"}
IMF_A3_FIX = {"UVK": "XKX", "WBG": "PSE"}


def _imf_raw(code, a3set):
    """{iso3: {année: val}} depuis DataMapper (avec fix ISO3). {} si vide."""
    d = http_get_json(f"https://www.imf.org/external/datamapper/api/v1/{code}", timeout=90)
    vals = (d or {}).get("values", {}).get(code)
    if not vals:
        sys.stderr.write(f"[WARN] IMF {code} vide\n")
        return {}
    out = {}
    for k, years in vals.items():
        a3 = IMF_A3_FIX.get(k, k)
        if a3 not in a3set:
            continue
        ym = {}
        for ys, v in years.items():
            try:
                ym[int(ys)] = float(v)
            except (TypeError, ValueError):
                continue
        if ym:
            out[a3] = ym
    return out


def fetch_imf_hist(a3set):
    """({iso3:{métrique:{s,v}}}, forecast_from, ok). Séries complètes avec prévisions."""
    out, n_ok = {}, 0
    overall_raw = {}   # {a3: {année: solde global %PIB}} — capté pour dériver la charge d'intérêts
    for field, code in IMF_CODES.items():
        d = http_get_json(f"https://www.imf.org/external/datamapper/api/v1/{code}", timeout=90)
        vals = (d or {}).get("values", {}).get(code)
        if not vals:
            sys.stderr.write(f"[WARN] IMF {code} vide\n")
            continue
        n_ok += 1
        key = "unemp" if field == "unemp_imf" else field
        cnt = 0
        for k, years in vals.items():
            a3 = IMF_A3_FIX.get(k, k)
            if a3 not in a3set:
                continue
            ym = {}
            for ys, v in years.items():
                try:
                    ym[int(ys)] = float(v)
                except (TypeError, ValueError):
                    continue
            if field == "fiscal":
                overall_raw[a3] = ym
            s = _pack_series(ym, rnd=r1)
            if s:
                out.setdefault(a3, {})[key] = s
                cnt += 1
        print(f"[IMF.hist] {code}: {cnt} pays")

    # ── Charge d'intérêts de la dette (dérivée WEO → RÉCENTE + prévisions jusqu'à 2031) ──
    # Identité comptable IMF : intérêts nets = solde primaire − solde global.
    #   int_gdp  = GGXONLB_G01_GDP_PT − GGXCNL_NGDP           (% du PIB)
    #   int_exp  = int_gdp / G_X_G01_GDP_PT × 100             (% des dépenses publiques)
    # On préfère cette dérivation au code direct `ie` (Fiscal Monitor historique), qui
    # remonte à 1880 mais s'arrête à 2024 sans prévision — donc « pas assez récent ».
    primary_raw = _imf_raw("GGXONLB_G01_GDP_PT", a3set)   # solde primaire %PIB (→2031)
    exp_raw = _imf_raw("G_X_G01_GDP_PT", a3set)           # dépenses publiques %PIB (→2031)
    n_ig = n_ie = 0
    for a3, prim in primary_raw.items():
        ovr = overall_raw.get(a3)
        if not ovr:
            continue
        ig = {y: prim[y] - ovr[y] for y in prim if y in ovr}   # intérêts nets %PIB
        s = _pack_series(ig, rnd=r2)
        if s:
            out.setdefault(a3, {})["int_gdp"] = s
            n_ig += 1
        ex = exp_raw.get(a3, {})
        ie = {y: ig[y] / ex[y] * 100.0 for y in ig if y in ex and ex[y]}   # part du budget
        s = _pack_series(ie, rnd=r1)
        if s:
            out.setdefault(a3, {})["int_exp"] = s
            n_ie += 1
    print(f"[IMF.hist] int_gdp (prim−global): {n_ig} pays · int_exp: {n_ie} pays")
    return out, FORECAST_FROM, (n_ok >= 3)


OWID_A3_FIX = {"OWID_KOS": "XKX"}


def fetch_owid_series(slug, column, a3set, rnd):
    """{iso3: {s,v}} toutes années. None si échec."""
    t = http_get(f"https://ourworldindata.org/grapher/{slug}.csv?useColumnShortNames=true",
                 timeout=90)
    if t is None:
        return None
    rdr = csv.DictReader(io.StringIO(t))
    if column not in (rdr.fieldnames or []):
        sys.stderr.write(f"[WARN] OWID {slug}: colonne {column} absente ({rdr.fieldnames})\n")
        return None
    raw = {}
    for row in rdr:
        a3 = OWID_A3_FIX.get(row.get("code", ""), row.get("code", ""))
        if a3 not in a3set:
            continue
        try:
            raw.setdefault(a3, {})[int(row["year"])] = float(row[column])
        except (TypeError, ValueError, KeyError):
            continue
    out = {}
    for a3, ym in raw.items():
        s = _pack_series(ym, rnd=rnd)
        if s:
            out[a3] = s
    print(f"[OWID] {slug}: {len(out)} pays")
    return out or None


# v5 — 7 séries OWID (démographie / énergie & climat / développement humain). La colonne
# de valeur varie d'une série à l'autre → détectée via le header (useColumnShortNames).
# rnd : co2pc/enintensity r2 ; energypc sig4 (grands kWh) ; renew/medage/schooling r1 ; hdi r3.
# tuples (slug, clé, arrondi, colonne_override|None) — override = colonne connue (sinon auto).
OWID_V5 = [
    ("co2-emissions-per-capita", "co2pc", r2, None),
    ("per-capita-energy-use", "energypc", sig4, None),
    ("energy-intensity", "enintensity", r2, None),
    ("share-electricity-renewables", "renew_elec", r1, None),
    ("human-development-index", "hdi", r3, None),
    ("median-age", "medage", r1, None),
    ("average-years-of-schooling", "schooling", r1, "mys__sex_total"),  # UNDP (→2023) ; l'ancien long-run s'arretait a 2020
    # E — part du revenu (avant impôt) détenue par les 10 % les plus riches (WID via OWID)
    ("income-share-top-10-before-tax-wid", "top10", r1,
     "share_top_10__welfare_type_before_tax__extrapolated_no"),
    # Potentiel futur — part du travail dans le PIB (ILO SDG 10.4.1). Sert d'EXPOSANT
    # Cobb-Douglas mesuré : beta_travail = labsh/100 ; alpha_capital = 1 - beta.
    ("labor-share-of-gdp", "labsh", r1, "_10_4_1__sl_emp_gtotl"),
    # Travail & revenus — heures travaillées par an et par actif (Penn World Table / OIT).
    # Couverture ~130 pays seulement (série la plus étroite du volet) → le front filtre
    # proprement les pays sans donnée ; sert aussi à dériver le PIB par HEURE travaillée.
    ("annual-working-hours-per-worker", "hours", r1, "working_hours_omm"),
    # Taux de PRÉLÈVEMENTS OBLIGATOIRES : impôts + cotisations sociales, toutes
    # administrations, en % du PIB (Government Revenue Dataset, UNU-WIDER).
    # 190 pays, 1980→2023 (132 pays au millésime 2023) ; recoupé avec les Revenue
    # Statistics de l'OCDE : FRA 43,8 · DEU 37,5 · USA 24,8 · JPN 34,9.
    # ⚠ NE PAS confondre avec les deux autres ratios qui circulent pour un même pays :
    #   - recettes fiscales de l'État CENTRAL (WB GC.TAX.TOTL.GD.ZS) : FRA 22,8 %,
    #     DEU 10,9 % — exclut collectivités ET cotisations, incomparable entre
    #     pays fédéraux et unitaires ;
    #   - recettes publiques TOTALES (IMF WEO GGR_G01_GDP_PT) : FRA 51,8 % —
    #     ajoute le non fiscal (pétrole, dividendes, redevances).
    # Le CSV porte aussi une colonne `owid_region` (texte) : _owid_value_column
    # l'écarte déjà, le col_override rend le choix explicite et robuste.
    ("total-tax-revenues-gdp", "taxrev", r1, "tax_inc_sc"),
]
# OWID entités agrégées → code AGG. Seul « World » (OWID_WRL) s'aligne proprement sur WLD ;
# les continents OWID (Afrique/Asie/…) ne recouvrent pas les 7 régions WB → laissés de côté.
OWID_AGG_ENTITY = {"World": "WLD"}


def _owid_value_column(fieldnames):
    """Colonne de valeur OWID (useColumnShortNames) : hors entity/code/year et hors colonnes
    'region' (métadonnées texte, ex. owid_region) ; préfère 'estimates' si plusieurs variantes
    (âge médian : estimations vs projections → on garde l'observé)."""
    cand = [f for f in (fieldnames or [])
            if f.lower() not in ("entity", "code", "year") and "region" not in f.lower()]
    if not cand:
        return None
    for f in cand:
        if "estimate" in f.lower():
            return f
    return cand[0]


def fetch_owid_v5(a3set):
    """7 séries v5 (colonne auto-détectée). → ({iso3:{clé:{s,v}}},
    {AGG:{clé:{'latest':(v,yr),'hist':{s,v}}}}, ok). Agrégats = World→WLD (dernier non-null)."""
    out, agg = {}, {}
    n_ok = 0
    for slug, key, rnd, col_override in OWID_V5:
        # fh/vdem/cpi (fetch_owid_hist) tournent AVANT → le CDN OWID est déjà chaud ici.
        t = http_get(f"https://ourworldindata.org/grapher/{slug}.csv?useColumnShortNames=true",
                     timeout=120, retries=2)
        if t is None:
            sys.stderr.write(f"[WARN] OWID v5 {slug} : fetch KO\n")
            continue
        rdr = csv.DictReader(io.StringIO(t))
        if col_override and col_override in (rdr.fieldnames or []):
            col = col_override                       # colonne connue (top10) — robuste
        else:
            col = _owid_value_column(rdr.fieldnames)
            if col_override:
                sys.stderr.write(f"[WARN] OWID v5 {slug} : colonne {col_override} absente "
                                 f"→ auto-détection {col}\n")
        if not col:
            sys.stderr.write(f"[WARN] OWID v5 {slug} : colonne valeur introuvable "
                             f"({rdr.fieldnames})\n")
            continue
        raw, agg_raw = {}, {}
        for row in rdr:
            code = OWID_A3_FIX.get(row.get("code", ""), row.get("code", ""))
            ent = (row.get("Entity") or row.get("entity") or "").strip()
            v = row.get(col)
            if v in (None, ""):
                continue
            try:
                val, yr = float(v), int(row["year"])
            except (TypeError, ValueError, KeyError):
                continue
            if code in a3set:
                raw.setdefault(code, {})[yr] = val
            agc = OWID_AGG_ENTITY.get(ent)
            if agc:
                agg_raw.setdefault(agc, {})[yr] = val
        cnt = 0
        for a3, ym in raw.items():
            s = _pack_series(ym, rnd=rnd)
            if s:
                out.setdefault(a3, {})[key] = s
                cnt += 1
        for agc, ym in agg_raw.items():
            s = _pack_series(ym, rnd=rnd)
            val, yr = _last_nonnull(ym)
            if s or val is not None:
                agg.setdefault(agc, {})[key] = {
                    "latest": (rnd(val), yr) if val is not None else None, "hist": s}
        if cnt:
            n_ok += 1
        print(f"[OWID.v5] {slug} → {key} ({col}): {cnt} pays"
              + (f" +agg{sorted(agg_raw)}" if agg_raw else ""))
    return out, agg, (n_ok >= 4)


def fetch_owid_hist(a3set):
    """{iso3: {fh|vdem|cpi + 7 séries v5: {s,v}}} + agrégats OWID (World→WLD) + ok."""
    out = {}
    fh = fetch_owid_series("freedom-score-fh", "total_score", a3set, lambda v: int(round(v)))
    vdem = fetch_owid_series("liberal-democracy-index", "libdem_vdem__estimate_best", a3set, r2)
    cpi = fetch_owid_series("corruption-perception-index", "cpi_score", a3set,
                            lambda v: int(round(v)))
    for src, key in ((fh, "fh"), (vdem, "vdem"), (cpi, "cpi")):
        if src:
            for a3, s in src.items():
                out.setdefault(a3, {})[key] = s
    v5_out, v5_agg, v5_ok = fetch_owid_v5(a3set)
    for a3, d in v5_out.items():
        out.setdefault(a3, {}).update(d)
    return out, v5_agg, bool(fh or vdem or v5_ok)


# ── PISA — scores OCDE des élèves de 15 ans (miroir OWID) ─────────────────────
# 3 matières + composite (moyenne des 3, uniquement les vagues où les 3 existent)
# + moyenne OCDE (repère, stockée dans meta.pisa_oecd).
#
# ⚠ PIÈGES VÉRIFIÉS (2026-07-26) :
#  - le grapher OWID est MULTI-DIMENSIONS : le CSV n'expose qu'UNE matière par appel
#    → 1 appel par matière avec ?subject=…&sex=both (les params de dimension SONT
#    respectés par l'endpoint .csv) ;
#  - le slug de la matière maths est « mathematics » — « math » renvoie HTTP 500 ;
#  - il n'existe PAS de grapher dédié pour les sciences (404) : seul ce grapher
#    multi-dimensions couvre les 3 matières ;
#  - la Banque mondiale (LO.PISA.*) s'arrête à PISA 2018 → écartée, ce miroir va à 2022 ;
#  - couverture ~86 pays de l'Atlas (PISA = pays participants) ; la CHINE est ABSENTE
#    (PISA n'y couvre que 4 provinces B-S-J-Z, non représentatives du pays) ;
#  - les lignes « OECD average » / « PISA participants average » n'ont PAS de code ISO3.
PISA_URL = ("https://ourworldindata.org/grapher/academic-performance.csv"
            "?subject={subj}&sex=both&useColumnShortNames=true")
PISA_SUBJECTS = [("pisa_math", "mathematics", "pisa_math_all_average"),
                 ("pisa_read", "reading", "pisa_reading_all_average"),
                 ("pisa_sci", "science", "pisa_science_all_average")]
PISA_KEYS = ["pisa", "pisa_math", "pisa_read", "pisa_sci"]
PISA_OECD_ENTITY = "OECD average"
PISA_OECD_CODE = "__OECD__"
# GARDE-FOU DE RECENCE (même logique que fetch_atlas_econ.py) : vague suivante administrée
# en mai 2025, résultats OCDE le 8 septembre 2026. Passée cette date sans nouvelle vague
# dans les données, on publie une note d'attente plutôt que de laisser croire que 2022 est
# la dernière mesure existante.
PISA_NEXT_WAVE = 2025
PISA_NEXT_RELEASE = "2026-09-08"


def pisa_vintage(pisa_hist):
    """(dernière vague présente, note d'attente|None) depuis les séries {s,v}."""
    newest = None
    for rec in (pisa_hist or {}).values():
        for s in rec.values():
            for i in range(len(s["v"]) - 1, -1, -1):
                if s["v"][i] is not None:
                    y = s["s"] + i
                    if newest is None or y > newest:
                        newest = y
                    break
    if newest is None or newest >= PISA_NEXT_WAVE or NOW.date().isoformat() < PISA_NEXT_RELEASE:
        return newest, None
    sys.stderr.write(f"[PISA-RECENCE] vague {PISA_NEXT_WAVE} publiée le {PISA_NEXT_RELEASE} "
                     f"mais la source en est encore à {newest} — note d'attente publiée\n")
    return newest, {"wave": PISA_NEXT_WAVE, "released": PISA_NEXT_RELEASE,
                    "cached_wave": newest}


def _pisa_raw():
    """{clé_matière: {iso3|__OECD__: {année: score}}} — 1 appel OWID par matière.
    Une matière manquante = clé absente (le caller décide si la source est OK)."""
    out = {}
    for key, subj, col in PISA_SUBJECTS:
        t = http_get(PISA_URL.format(subj=subj), timeout=120, retries=3)
        if t is None:
            sys.stderr.write(f"[WARN] PISA {subj} : fetch KO\n")
            continue
        rdr = csv.DictReader(io.StringIO(t))
        flds = rdr.fieldnames or []
        use = col
        if use not in flds:
            cand = [f for f in flds if f.startswith("pisa")]
            if not cand:
                sys.stderr.write(f"[WARN] PISA {subj} : colonne valeur absente ({flds})\n")
                continue
            sys.stderr.write(f"[WARN] PISA {subj} : {col} absente → {cand[0]}\n")
            use = cand[0]
        d = {}
        for row in rdr:
            ent = (row.get("entity") or row.get("Entity") or "").strip()
            code = OWID_A3_FIX.get(row.get("code", ""), row.get("code", ""))
            tgt = code if len(code) == 3 else (PISA_OECD_CODE if ent == PISA_OECD_ENTITY else None)
            if not tgt:
                continue
            v = row.get(use)
            if v in (None, ""):
                continue
            try:
                d.setdefault(tgt, {})[int(row["year"])] = float(v)
            except (TypeError, ValueError, KeyError):
                continue
        if len(d) < 40:
            sys.stderr.write(f"[WARN] PISA {subj} : seulement {len(d)} entités — ignoré\n")
            continue
        out[key] = d
        print(f"[PISA] {subj} ({use}) : {len(d)} entités")
    return out


def _pisa_pack(ym):
    """Comme _pack_series mais accepte UNE seule vague : ~11 pays n'ont participé qu'à
    PISA 2022 (Cambodge, Ouzbékistan…) — les jeter priverait la fiche d'une valeur
    pourtant publiée. Le graphe affiche alors un point unique."""
    if not ym:
        return None
    y0, y1 = min(ym), max(ym)
    return {"s": y0, "v": [r1(ym[y]) if y in ym else None for y in range(y0, y1 + 1)]}


def _pisa_composite(ymaps):
    """{année: moyenne des 3 matières} — SEULEMENT les vagues où les 3 sont présentes
    (2000 = lecture seule, 2003 = maths+lecture → pas de composite avant 2006)."""
    if len(ymaps) < 3:
        return {}
    years = set(ymaps[0])
    for m in ymaps[1:]:
        years &= set(m)
    return {y: sum(m[y] for m in ymaps) / 3.0 for y in years}


def fetch_pisa(a3set):
    """({iso3: {pisa*, {s,v}}}, {clé: {s,v}} moyenne OCDE, ok).

    ok = les 3 matières récupérées (sinon garde par source : l'ancien bloc est conservé,
    on ne publie JAMAIS un composite calculé sur 2 matières)."""
    raw = _pisa_raw()
    ok = len(raw) == 3
    if not ok:
        sys.stderr.write(f"[WARN] PISA : {len(raw)}/3 matières — source KO\n")
    out, oecd = {}, {}
    targets = set(a3set) | {PISA_OECD_CODE}
    for tgt in sorted(targets):
        subj_maps, packed = [], {}
        for key, _subj, _col in PISA_SUBJECTS:
            ym = (raw.get(key) or {}).get(tgt)
            if not ym:
                continue
            subj_maps.append(ym)
            s = _pisa_pack(ym)
            if s:
                packed[key] = s
        comp = _pisa_composite(subj_maps)
        if comp:
            s = _pisa_pack(comp)
            if s:
                packed["pisa"] = s
        if not packed:
            continue
        if tgt == PISA_OECD_CODE:
            oecd = packed
        else:
            out[tgt] = packed
    print(f"[PISA] {len(out)} pays" + (" + moyenne OCDE" if oecd else " (moyenne OCDE absente)"))
    return out, oecd, ok


# ── MIX ÉLECTRIQUE (OWID share-elec-by-source) ────────────────────────────────
# 9 parts (% de la production d'électricité) par pays+année + agrégat World→WLD.
# Ordre bas→haut du stack front (fossiles puis renouvelables) — coal…other.
ELECMIX_COLS = [
    ("coal", "coal_share_of_electricity__pct"),
    ("oil", "oil_share_of_electricity__pct"),
    ("gas", "gas_share_of_electricity__pct"),
    ("nuclear", "nuclear_share_of_electricity__pct"),
    ("hydro", "hydro_share_of_electricity__pct"),
    ("bio", "bioenergy_share_of_electricity__pct"),
    ("wind", "wind_share_of_electricity__pct"),
    ("solar", "solar_share_of_electricity__pct"),
    ("other", "other_renewables_excluding_bioenergy_share_of_electricity__pct"),
]


def fetch_elec_mix(a3set):
    """Mix électrique OWID → ({code: {'s':annéeDébut, coal:[…], oil:[…], …, other:[…]}}, ok).
    Parts % arrondies r1, 9 tableaux alignés sur 's', années de tête toutes nulles retirées.
    Couvre les pays de a3set ET World→WLD (via OWID_AGG_ENTITY). ok = ≥30 entités (garde amont)."""
    t = http_get("https://ourworldindata.org/grapher/share-elec-by-source.csv"
                 "?useColumnShortNames=true", timeout=120, retries=3)
    if t is None:
        sys.stderr.write("[WARN] OWID elec-mix : fetch KO\n")
        return {}, False
    rdr = csv.DictReader(io.StringIO(t))
    missing = [c for _, c in ELECMIX_COLS if c not in (rdr.fieldnames or [])]
    if missing:
        sys.stderr.write(f"[WARN] OWID elec-mix : colonnes absentes {missing}\n")
        return {}, False
    raw = {}                                   # {code: {année: {source: part|None}}}
    for row in rdr:
        code = OWID_A3_FIX.get(row.get("code", ""), row.get("code", ""))
        ent = (row.get("entity") or row.get("Entity") or "").strip()
        target = code if code in a3set else OWID_AGG_ENTITY.get(ent)
        if target is None:
            continue
        try:
            yr = int(row["year"])
        except (TypeError, ValueError, KeyError):
            continue
        d, any_v = {}, False
        for key, col in ELECMIX_COLS:
            v = row.get(col)
            if v in (None, ""):
                d[key] = None
                continue
            try:
                d[key] = r1(float(v))
                any_v = True
            except (TypeError, ValueError):
                d[key] = None
        if any_v:                              # ignore les lignes 100 % vides
            raw.setdefault(target, {})[yr] = d
    out = {}
    for code, ymap in raw.items():
        years = sorted(ymap)                   # déjà purgé des années sans aucune donnée
        if len(years) < 2:
            continue
        s, e = years[0], years[-1]
        entry = {"s": s}
        for key, _col in ELECMIX_COLS:
            entry[key] = [(ymap.get(y) or {}).get(key) for y in range(s, e + 1)]
        out[code] = entry
    ok = len(out) >= 30
    print(f"[OWID.elecmix] mix électrique : {len(out)} entités"
          + (" (dont WLD)" if "WLD" in out else "") + f" · ok={ok}")
    return out, ok


# ══ §3b — COMMERCE ════════════════════════════════════════════════════════════

WB_STRUCT = {
    "agr": "NV.AGR.TOTL.ZS", "ind": "NV.IND.TOTL.ZS",
    "manf": "NV.IND.MANF.ZS", "srv": "NV.SRV.TOTL.ZS",
}
WB_OPEN = {
    "exports_gdp": "NE.EXP.GNFS.ZS", "imports_gdp": "NE.IMP.GNFS.ZS",
    "trade_gdp": "NE.TRD.GNFS.ZS", "hitech_exp_pct_manuf": "TX.VAL.TECH.MF.ZS",
    "fdi_in_gdp": "BX.KLT.DINV.WD.GD.ZS",
}


def _wb_mrnev(code):
    """dernière valeur non vide par pays → {iso3: (val, année)}."""
    d = http_get_json(f"https://api.worldbank.org/v2/country/all/indicator/{code}"
                      f"?format=json&mrnev=1&per_page=400", retries=3)
    if not d or len(d) < 2 or not d[1]:
        rows, page, pages = [], 1, 1
        while page <= pages:
            d = http_get_json(f"https://api.worldbank.org/v2/country/all/indicator/{code}"
                              f"?format=json&mrnev=1&per_page=300&page={page}")
            if not d or len(d) < 2 or not d[1]:
                return None
            pages = int(d[0].get("pages", 1))
            rows += d[1]
            page += 1
        d = [None, rows]
    out = {}
    for row in d[1]:
        a3, v = row.get("countryiso3code"), row.get("value")
        if a3 and v is not None:
            try:
                out[a3] = (float(v), int(row["date"]))
            except (TypeError, ValueError):
                continue
    return out


def fetch_wb_trade(a3set):
    """({iso3: {'sectors_va':…, 'openness':…}}, ok)."""
    struct = {f: _wb_mrnev(c) for f, c in WB_STRUCT.items()}
    opening = {f: _wb_mrnev(c) for f, c in WB_OPEN.items()}
    n_ok = sum(1 for v in list(struct.values()) + list(opening.values()) if v)
    out = {}
    for a3 in a3set:
        sec, yr_s = {}, None
        for f, m in struct.items():
            if m and a3 in m:
                sec[f] = r1(m[a3][0])
                yr_s = max(yr_s or 0, m[a3][1])
        if sec:
            sec["year"] = yr_s
            out.setdefault(a3, {})["sectors_va"] = sec
        op, yr_o = {}, None
        for f, m in opening.items():
            if m and a3 in m:
                op[f] = r1(m[a3][0])
                yr_o = max(yr_o or 0, m[a3][1])
        if op:
            op["year"] = yr_o
            out.setdefault(a3, {})["openness"] = op
    print(f"[WB.trade] structure/ouverture : {len(out)} pays ({n_ok}/9 indicateurs)")
    return out, (n_ok >= 5)


OEC_YEARS = [2022, 2021, 2020]


def fetch_oec_eci(a3set):
    """{iso3: {'value','rank','n_ranked','year'}} pour ~131 pays classés. None si échec."""
    for yr in OEC_YEARS:
        d = http_get_json("https://api-v2.oec.world/tesseract/data.jsonrecords?"
                          "cube=complexity_eci_a_hs22_hs4&drilldowns=Country,ECI+Rank"
                          f"&measures=ECI&Year={yr}", timeout=60)
        rows = (d or {}).get("data")
        if not rows:
            continue
        n = len(rows)
        out = {}
        for row in rows:
            cid = row.get("Country ID", "")
            a3 = cid[-3:].upper() if len(cid) >= 3 else None
            if a3 not in a3set:
                continue
            out[a3] = {"value": r3(row.get("ECI")), "rank": int(row.get("ECI Rank")),
                       "n_ranked": n, "year": yr}
        print(f"[OEC] ECI {yr}: {len(out)} pays classés (/{n})")
        return out
    return None


# Secteur d'un code HS4 (chapitre = 2 premiers chiffres). DOIT rester identique au
# mapping front aeHsSector (_atlas_economique_body.html) pour un treemap cohérent.
_HS_SECTOR_RANGES = [
    (1, 15, "agri"), (16, 24, "food"), (25, 27, "energy"), (28, 38, "chem"),
    (39, 40, "plastic"), (41, 63, "textile"), (64, 67, "misc"), (68, 83, "metals"),
    (84, 85, "machines"), (86, 89, "transport"), (90, 99, "misc"),
]


def _hs_sector(hs4):
    try:
        ch = int(str(hs4)[:2])
    except (ValueError, TypeError):
        return "other"
    for lo, hi, k in _HS_SECTOR_RANGES:
        if lo <= ch <= hi:
            return k
    return "other"


def _by_sector(pairs):
    """(hs4, ev)* → {secteur: total arrondi} trié desc (tail incluse, pour dimensionner
    chaque secteur à sa vraie taille dans le treemap)."""
    d = {}
    for hs4, ev in pairs:
        if ev and ev > 0:
            k = _hs_sector(hs4)
            d[k] = d.get(k, 0.0) + ev
    return {k: sig4(v) for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if v > 0}


def _top_per_sector(pairs, n=15):
    """(val, hs4)* → ([[hs4, sig4(val)] top n PAR SECTEUR trié desc], {secteur: nb produits}).
    Détail équilibré : chaque secteur montre ses n plus gros produits (survolables) ;
    sector_n permet au front d'étiqueter « Autres · secteur · +K produits »."""
    bysec = {}
    for val, hs4 in pairs:
        if val and val > 0:
            bysec.setdefault(_hs_sector(hs4), []).append((float(val), hs4))
    prods, sector_n = [], {}
    for sec, lst in bysec.items():
        lst.sort(reverse=True)
        sector_n[sec] = len(lst)
        prods.extend(lst[:n])
    prods.sort(reverse=True)
    return [[hs4, sig4(v)] for v, hs4 in prods], sector_n


def fetch_harvard_top(a3set, skip, iso2region=None):
    """({iso3: {'top_exports','export_products','export_total_usd','partners_exp',…}},
    {agrégat: {'export_products','export_total_usd','year'}}, ok).

    En plus des top_exports (7) et partenaires : compose le TREEMAP des exportations
    (composition détaillée façon Harvard Atlas) — top ~28 produits HS4 par valeur + total
    de TOUS les produits, par pays ET par agrégat (WLD = tous les pays de per_c ; 7 régions
    WB via iso2region). Réutilise le MÊME chargement CSV que les top_exports."""
    if skip or not HARVARD_DIR:
        if not HARVARD_DIR:
            sys.stderr.write("[WARN] Harvard : CSV introuvables → bloc commerce Harvard sauté\n")
        return {}, {}, False
    hs4_path = Path(HARVARD_DIR) / "hs4.csv"
    bilat_path = Path(HARVARD_DIR) / "bilat.csv"
    # top exports (year=max)
    per_c = {}          # iso3 → list[(ev, hs4, rca, wms)]  (ev>0 : top_exports/treemap)
    per_c_imp = {}      # iso3 → list[(iv, hs4)]  (iv>0 : treemap IMPORTATIONS)
    per_c_div = {}      # iso3 → list[(hs4, rca, distance, pci)]  (E : diversification, tous produits)
    max_year = 0
    with open(hs4_path, newline="") as f:
        rdr = csv.reader(f)
        next(rdr, None)
        for row in rdr:
            # country_iso3_code[1], product_hs22_code[3], year[4], export_value[5],
            # global_market_share[7], export_rca[8]
            try:
                a3, hs4, yr = row[1], row[3], int(row[4])
            except (IndexError, ValueError):
                continue
            if a3 not in a3set:
                continue
            if yr > max_year:
                max_year = yr
    with open(hs4_path, newline="") as f:
        rdr = csv.reader(f)
        next(rdr, None)
        for row in rdr:
            try:
                a3, hs4, yr = row[1], row[3], int(row[4])
            except (IndexError, ValueError):
                continue
            if yr != max_year or a3 not in a3set:
                continue
            # 9999/XXXX = résidus statistiques Harvard (non spécifié/confidentiel), exclus.
            resid = hs4 in ("9999", "XXXX", "")
            # E — diversification : capte export_rca[8]/distance[9]/pci[11] de TOUS les produits
            # (y c. ceux à faible export), hors résidus → repérage des opportunités de montée en gamme.
            if not resid:
                try:
                    per_c_div.setdefault(a3, []).append(
                        (hs4, float(row[8]), float(row[9]), float(row[11])))
                except (IndexError, ValueError):
                    pass
                # treemap IMPORTATIONS : import_value[6] (indépendant de l'export)
                try:
                    iv = float(row[6])
                    if iv > 0:
                        per_c_imp.setdefault(a3, []).append((iv, hs4))
                except (IndexError, ValueError):
                    pass
            # top_exports / treemap : produits réellement exportés (ev>0)
            try:
                ev = float(row[5])
            except (IndexError, ValueError):
                continue
            if ev <= 0 or resid:
                continue
            try:
                wms = float(row[7])
            except (IndexError, ValueError):
                wms = None
            try:
                rca = float(row[8])
            except (IndexError, ValueError):
                rca = None
            per_c.setdefault(a3, []).append((ev, hs4, rca, wms))
    # partners (year=max)
    partners = {}
    if bilat_path.exists():
        pm_year = 0
        with open(bilat_path, newline="") as f:
            rdr = csv.reader(f)
            next(rdr, None)
            for row in rdr:
                try:
                    if row[1] in a3set:
                        pm_year = max(pm_year, int(row[4]))
                except (IndexError, ValueError):
                    continue
        agg = {}            # iso3 → {partner_iso3: ev}
        with open(bilat_path, newline="") as f:
            rdr = csv.reader(f)
            next(rdr, None)
            for row in rdr:
                # country_iso3_code[1], partner_iso3_code[3], year[4], export_value[5]
                try:
                    a3, pa3, yr = row[1], row[3], int(row[4])
                    if yr != pm_year or a3 not in a3set:
                        continue
                    ev = float(row[5])
                except (IndexError, ValueError):
                    continue
                if ev > 0 and pa3:
                    agg.setdefault(a3, {})[pa3] = agg.get(a3, {}).get(pa3, 0.0) + ev
        for a3, pm in agg.items():
            tot = sum(pm.values())
            if tot <= 0:
                continue
            top = sorted(pm.items(), key=lambda kv: -kv[1])[:5]
            partners[a3] = {"list": [{"iso3": p, "share": r3(v / tot)} for p, v in top],
                            "year": pm_year}
    # ── E — Opportunités de diversification (Harvard) : produits pas encore en avantage
    # (export_rca < 1) mais PROCHES des savoir-faire du pays (distance < médiane du pays) ET
    # à forte valeur stratégique (pci élevé). Top ~6 triés par pci décroissant → pistes de
    # montée en gamme façon « adjacent possible » de l'Atlas de la complexité. ──
    diversif = {}
    n_div_prod = 0
    for a3, rows in per_c_div.items():
        dists = sorted(d for (_h, _r, d, _p) in rows)
        n = len(dists)
        if n < 6:                       # trop peu de produits pour une médiane robuste
            continue
        med = dists[n // 2] if n % 2 else (dists[n // 2 - 1] + dists[n // 2]) / 2.0
        opps = [(hs4, pci, dist) for (hs4, rca, dist, pci) in rows if rca < 1 and dist < med]
        opps.sort(key=lambda t: -t[1])  # pci décroissant (produits les plus complexes d'abord)
        top = opps[:6]
        if top:
            diversif[a3] = [{"hs4": hs4, "pci": r2(pci), "distance": r2(dist)}
                            for hs4, pci, dist in top]
            n_div_prod += len(top)

    out = {}
    for a3, lst in per_c.items():
        lst.sort(reverse=True)
        tot = sum(x[0] for x in lst) or 1.0
        top = []
        for ev, hs4, rca, wms in lst[:7]:
            top.append({"hs4": hs4, "share": r3(ev / tot),
                        "rca": r2(rca) if rca is not None else None,
                        "world_mkt_share": r3(wms) if wms is not None else None})
        entry = {"top_exports": top, "year": max_year}
        # treemap : top ~35 produits + total + total PAR SECTEUR (tail incluse → chaque
        # secteur dimensionné à sa vraie taille façon Harvard, pas de bloc « Autres » géant)
        eprods, esn = _top_per_sector(((ev, hs4) for ev, hs4, rca, wms in lst), 15)
        entry["export_products"] = eprods
        entry["export_sector_n"] = esn
        entry["export_total_usd"] = sig4(tot)
        entry["export_by_sector"] = _by_sector((hs4, ev) for ev, hs4, rca, wms in lst)
        # treemap IMPORTATIONS (même structure : top 15/secteur + total + par secteur)
        imp = per_c_imp.get(a3)
        if imp:
            iprods, isn = _top_per_sector(imp, 15)
            entry["import_products"] = iprods
            entry["import_sector_n"] = isn
            entry["import_total_usd"] = sig4(sum(iv for iv, _h in imp))
            entry["import_by_sector"] = _by_sector((hs4, iv) for iv, hs4 in imp)
            entry["import_year"] = max_year
        if a3 in partners:
            entry["partners_exp"] = partners[a3]["list"]
            entry["partners_year"] = partners[a3]["year"]
        if a3 in diversif:
            entry["diversif"] = diversif[a3]
        out[a3] = entry
    print(f"[Harvard] diversification : {len(diversif)} pays "
          f"({n_div_prod} opportunités, top6/pays)")

    # ── Agrégats Monde + 7 régions : somme export_value par HS4 sur les pays membres ──
    # (WLD = tous les pays présents ; régions via la région WB de chaque pays). Réutilise
    # per_c (déjà borné à a3set, résidus 9999/XXXX exclus) — aucun re-parcours du CSV.
    agg_out = {}
    if iso2region is not None:
        agg_hs4 = {code: {} for code in AGG_CODES}       # code → {hs4: total_ev}
        agg_imp = {code: {} for code in AGG_CODES}       # code → {hs4: total_iv}
        for a3, lst in per_c.items():
            reg = iso2region.get(a3)                      # ∈ EAS/ECS/LCN/MEA/NAC/SAS/SSF
            for ev, hs4, rca, wms in lst:
                agg_hs4["WLD"][hs4] = agg_hs4["WLD"].get(hs4, 0.0) + ev
                if reg in agg_hs4:                        # reg n'est jamais "WLD" (API)
                    agg_hs4[reg][hs4] = agg_hs4[reg].get(hs4, 0.0) + ev
        for a3, lst in per_c_imp.items():
            reg = iso2region.get(a3)
            for iv, hs4 in lst:
                agg_imp["WLD"][hs4] = agg_imp["WLD"].get(hs4, 0.0) + iv
                if reg in agg_imp:
                    agg_imp[reg][hs4] = agg_imp[reg].get(hs4, 0.0) + iv
        for code, hm in agg_hs4.items():
            if not hm:
                continue
            eprods, esn = _top_per_sector(((ev, hs4) for hs4, ev in hm.items()), 15)
            entry = {
                "export_products": eprods,
                "export_sector_n": esn,
                "export_total_usd": sig4(sum(hm.values())),
                "export_by_sector": _by_sector(hm.items()),
                "year": max_year,
            }
            im = agg_imp.get(code)
            if im:
                iprods, isn = _top_per_sector(((iv, hs4) for hs4, iv in im.items()), 15)
                entry["import_products"] = iprods
                entry["import_sector_n"] = isn
                entry["import_total_usd"] = sig4(sum(im.values()))
                entry["import_by_sector"] = _by_sector(im.items())
                entry["import_year"] = max_year
            agg_out[code] = entry
        print(f"[Harvard] treemap agrégats : {len(agg_out)} entités "
              f"(WLD {len(agg_hs4['WLD'])} HS4)")
    print(f"[Harvard] top exports : {len(per_c)} pays (année {max_year}), "
          f"partenaires : {len(partners)} pays")
    return out, agg_out, bool(per_c)


def fetch_factbook(a3set, iso2gec_region):
    """{iso3: (overview_text, url, region)}. Best-effort par pays."""
    out = {}
    n = 0
    for a3 in sorted(a3set):
        rg = iso2gec_region.get(a3)
        if not rg:
            continue
        gec, region = rg
        url = (f"https://raw.githubusercontent.com/factbook/factbook.json/master/"
               f"{region}/{gec}.json")
        d = http_get_json(url, timeout=30, retries=2)
        if not d:
            continue
        econ = d.get("Economy", {})
        ov = econ.get("Economic overview", {})
        txt = ov.get("text", "") if isinstance(ov, dict) else ""
        txt = re.sub(r"<[^>]+>", "", txt).strip()
        if len(txt) > 1200:            # borne la taille : coupe à la fin de phrase la plus proche
            cut = txt.rfind(". ", 700, 1200)
            txt = txt[:(cut + 1) if cut > 0 else 1200].rstrip() + (" […]" if cut <= 0 else "")
        if txt:
            out[a3] = (txt, url, region)
            n += 1
        time.sleep(0.05)
    print(f"[Factbook] overview : {n} pays")
    return out


# Résumés Factbook traduits en français (sidecar produit par translate_atlas_overviews.py,
# {md5(texte_en): texte_fr}). Rechargé à chaque run → le FR survit aux rafraîchissements
# sans rappeler l'IA. Les nouveaux/modifiés restent en anglais jusqu'au prochain passage
# du traducteur.
_OVFR = {"loaded": False, "map": {}}


def _overview_fr(txt):
    if not _OVFR["loaded"]:
        _OVFR["loaded"] = True
        p = Path(__file__).resolve().parent / "atlas_overview_fr.json"
        if not p.exists():
            p = Path.home() / "Desktop" / "Site_Crypto_Finance" / "atlas_overview_fr.json"
        try:
            _OVFR["map"] = json.loads(p.read_text())
        except Exception:
            _OVFR["map"] = {}
    import hashlib
    return _OVFR["map"].get(hashlib.md5(txt.encode("utf-8")).hexdigest())


# ══ §3e — PYRAMIDE DES ÂGES + AGRÉGATS MONDE/CONTINENTS + SLUGS TE (SPEC v3) ═══
#
# §A PYRAMIDE : 34 codes SP.POP.{bande}.{MA|FE}.5Y (17 bandes × MA/FE), valeur =
#   % de la population DU MÊME SEXE (les 17 bandes M somment à ~100, idem F).
#   ⚠ mrnev renvoie 400 sur ces codes → on passe par date=1960:CUR (jamais mrnev).
#   Cloudflare bloque les rafales de curl → curl_cffi (http_get) + petite tempo.
#   L'appel country/all inclut WLD + les 7 régions → pyramides d'agrégats gratuites.
# §B AGRÉGATS : WLD + EAS/ECS/LCN/MEA/NAC/SAS/SSF (WB réalisé ; prévisions FMI
#   WEOWORLD pour WLD uniquement). mrnev cassé → dernier non-null d'une série mrv/date.
# §C SLUGS : meta.slugs = {a3: slug TradingEconomics} (nom EN WB slugifié + overrides).

PYRAMID_BANDS = ["0004", "0509", "1014", "1519", "2024", "2529", "3034", "3539",
                 "4044", "4549", "5054", "5559", "6064", "6569", "7074", "7579", "80UP"]
# Années-instantanés stockées (léger). PYRAMID_SNAP_MIN = repli si le cache gonfle.
PYRAMID_SNAP_YEARS = [1960, 1970, 1980, 1990, 2000, 2010, 2020]
PYRAMID_SNAP_MIN = [1960, 1980, 2000]


def fetch_pyramid(snap=None):
    """34 appels all-countries (SP.POP.{bande}.{MA|FE}.5Y, source=2, date=1960:CUR).
    → (raw, ok, snap) avec raw = {'m': {bande: {iso3:{an:val}}}, 'f': {…}}."""
    snap = PYRAMID_SNAP_YEARS if snap is None else snap
    raw = {"m": {}, "f": {}}
    n_ok = 0
    for sex, suf in (("m", "MA"), ("f", "FE")):
        for band in PYRAMID_BANDS:
            code = f"SP.POP.{band}.{suf}.5Y"
            ser = _wb_series(code, source=2, timeout=120)   # {iso3:{an:val brute}}
            raw[sex][band] = ser or {}
            if ser:
                n_ok += 1
            else:
                sys.stderr.write(f"[WARN] pyramide {code} vide\n")
            time.sleep(0.6)                                 # tempo anti-Cloudflare
    ok = n_ok >= 30                                         # 30/34 bandes = suffisant
    print(f"[pyramid] {n_ok}/34 bandes récupérées")
    return raw, ok, snap


def build_pyramid_entry(a3, raw, snap):
    """Pyramide d'une entité (pays OU agrégat) : années-instantanés {m:[17],f:[17]}.
    Valide somme M≈100 & F≈100 (tolérance 98–102). None si aucune année complète."""
    avail = set()
    for band in PYRAMID_BANDS:
        avail |= set(raw["m"].get(band, {}).get(a3, {}))
        avail |= set(raw["f"].get(band, {}).get(a3, {}))
    if not avail:
        return None
    latest = max(avail)
    want = sorted(set(snap) | {latest}, reverse=True)       # dernière année en tête
    years = {}
    for yr in want:
        m = [raw["m"].get(b, {}).get(a3, {}).get(yr) for b in PYRAMID_BANDS]
        f = [raw["f"].get(b, {}).get(a3, {}).get(yr) for b in PYRAMID_BANDS]
        if any(v is None for v in m) or any(v is None for v in f):
            continue
        if not (98.0 <= sum(m) <= 102.0 and 98.0 <= sum(f) <= 102.0):
            continue
        years[str(yr)] = {"m": [r2(v) for v in m], "f": [r2(v) for v in f]}
    if not years:
        return None
    latest_kept = max(int(y) for y in years)
    return {"years": years, "latest": latest_kept,
            "src": {"provider": "wb_pop", "unit": "% du sexe",
                    "source": "Banque mondiale · World Population Prospects (ONU)",
                    "year": latest_kept}}


# --- Agrégats Monde + 7 continents ---------------------------------------------
AGG_CODES = ["WLD", "EAS", "ECS", "LCN", "MEA", "NAC", "SAS", "SSF"]
AGG_SET = set(AGG_CODES)     # lookup O(1) (utilisé par fetch_wb_hist/fetch_wgi_hist en amont)
AGG_META = [
    {"code": "WLD", "name": "Monde", "emoji": "🌍"},
    {"code": "NAC", "name": "Amérique du Nord", "emoji": "🌎"},
    {"code": "LCN", "name": "Amérique latine & Caraïbes", "emoji": "🌎"},
    {"code": "ECS", "name": "Europe & Asie centrale", "emoji": "🌍"},
    {"code": "MEA", "name": "Moyen-Orient & Afrique du Nord (+Afgh./Pak.)", "emoji": "🌍"},
    {"code": "SSF", "name": "Afrique subsaharienne", "emoji": "🌍"},
    {"code": "SAS", "name": "Asie du Sud", "emoji": "🌏"},
    {"code": "EAS", "name": "Asie de l'Est & Pacifique", "emoji": "🌏"},
]
AGG_NAME = {a["code"]: a["name"] for a in AGG_META}
AGG_EMOJI = {a["code"]: a["emoji"] for a in AGG_META}
# indicateurs WB réalisés (série longue + dernier non-null). PCAP.CD (pas PP) pour agrégats.
AGG_WB = {
    "gdp": ("NY.GDP.MKTP.CD", sig4),
    "gdp_pc": ("NY.GDP.PCAP.CD", sig4),
    "pop": ("SP.POP.TOTL", sig4),
    "life": ("SP.DYN.LE00.IN", r1),
    "urban": ("SP.URB.TOTL.IN.ZS", r1),
    "growth": ("NY.GDP.MKTP.KD.ZG", r1),        # croissance réelle (régions/monde)
    "infl": ("FP.CPI.TOTL.ZG", r1),
    # v5 — WLD + 7 régions disponibles pour chacun (latest + hist ; graceful si absent)
    "tfr": ("SP.DYN.TFRT.IN", r2),
    "dep_tot": ("SP.POP.DPND", r1),
    "dep_old": ("SP.POP.DPND.OL", r1),
    "dep_yg": ("SP.POP.DPND.YG", r1),
    "imr": ("SP.DYN.IMRT.IN", r1),
    "health_gdp": ("SH.XPD.CHEX.GD.ZS", r1),
    "internet": ("IT.NET.USER.ZS", r1),
    "mktcap_gdp": ("CM.MKT.LCAP.GD.ZS", r1),
    "credit_gdp": ("FS.AST.PRVT.GD.ZS", r1),
}
AGG_TRADE = dict(WB_STRUCT)                      # agr/ind/manf/srv (VA % PIB)
AGG_OPEN = {"exports_gdp": "NE.EXP.GNFS.ZS", "imports_gdp": "NE.IMP.GNFS.ZS",
            "trade_gdp": "NE.TRD.GNFS.ZS"}
IMF_WEO = {"growth": "NGDP_RPCH", "infl": "PCPIPCH", "debt": "GGXWDG_NGDP"}


def _agg_series(code, timeout=90):
    """Série 1960:CUR pour les 8 agrégats en 1 appel (liste ;) → {area:{an:val}}."""
    areas = ";".join(AGG_CODES)
    url = (f"https://api.worldbank.org/v2/country/{areas}/indicator/{code}"
           f"?format=json&date=1960:{CUR_YEAR}&per_page=4000")
    d = http_get_json(url, timeout=timeout, retries=4)
    if not d or len(d) < 2 or not d[1]:
        return {}
    out = {}
    for row in d[1]:
        a, v = row.get("countryiso3code"), row.get("value")
        if not a or v is None:
            continue
        try:
            out.setdefault(a, {})[int(row["date"])] = float(v)
        except (TypeError, ValueError):
            continue
    time.sleep(0.35)
    return out


def _last_nonnull(ym):
    """(val, année) du dernier point non-null d'un {an:val}. (None, None) si vide."""
    if not ym:
        return None, None
    yr = max(ym)
    return ym[yr], yr


def fetch_imf_weo_world():
    """Prévisions FMI WEOWORLD (growth/infl/debt) → {champ:{an:val}}. {} si échec."""
    out = {}
    for field, code in IMF_WEO.items():
        d = http_get_json(f"https://www.imf.org/external/datamapper/api/v1/{code}", timeout=90)
        w = (d or {}).get("values", {}).get(code, {}).get("WEOWORLD")
        if not w:
            continue
        ym = {}
        for ys, v in w.items():
            try:
                ym[int(ys)] = float(v)
            except (TypeError, ValueError):
                continue
        if ym:
            out[field] = ym
        time.sleep(0.3)
    return out


def fetch_aggregates(raw_pyramid, snap):
    """Construit les 8 entités agrégées (WLD + 7 régions). → ({code: entry}, ok).
    PAS de companies/mkt/gov/rating/eci (le front saute proprement les blocs absents)."""
    wb = {}
    for field, (code, _r) in AGG_WB.items():
        wb[field] = _agg_series(code)
    n_ok = sum(1 for v in wb.values() if v.get("WLD"))
    struct = {f: _agg_series(c) for f, c in AGG_TRADE.items()}
    opening = {f: _agg_series(c) for f, c in AGG_OPEN.items()}
    weo = fetch_imf_weo_world()

    out = {}
    for code in AGG_CODES:
        latest, hist = {}, {}
        for field, (icode, rnd) in AGG_WB.items():
            ym = wb.get(field, {}).get(code, {})
            val, yr = _last_nonnull(ym)
            if val is not None:
                latest[field] = [rnd(val), yr, 0]
                s = _pack_series(ym, rnd=rnd)
                if s:
                    hist[field] = s
        # structure VA + ouverture (dernier non-null)
        sec = {}
        for f in AGG_TRADE:
            val, yr = _last_nonnull(struct.get(f, {}).get(code, {}))
            if val is not None:
                sec[f] = r1(val)
                sec["year"] = max(sec.get("year", 0), yr)
        op = {}
        for f in AGG_OPEN:
            val, yr = _last_nonnull(opening.get(f, {}).get(code, {}))
            if val is not None:
                op[f] = r1(val)
                op["year"] = max(op.get("year", 0), yr)
        for f in AGG_OPEN:                 # historique d'ouverture (graphe d'évolution Monde/continents)
            s = _pack_series(opening.get(f, {}).get(code, {}), rnd=r1)
            if s:
                hist[f] = s
        trade = {}
        if sec:
            trade["sectors_va"] = sec
        if op:
            trade["openness"] = op

        # WLD : overlay prévisions FMI (WLD only) ; réalisé WB conservé en *_wb
        hist_meta = None
        if code == "WLD" and weo:
            if "growth" in latest:
                latest["growth_wb"] = latest["growth"]
            if "infl" in latest:
                latest["infl_wb"] = latest["infl"]
            if hist.get("growth"):
                hist["growth_wb"] = hist["growth"]
            for field, ym in weo.items():
                fval = ym.get(FORECAST_FROM)
                fyr = FORECAST_FROM if fval is not None else max(ym)
                latest[field] = [r1(ym[fyr]), fyr, 1]
                s = _pack_series(ym, rnd=r1)
                if s:
                    hist[field] = s                 # série FMI (réalisé + prévision →2031)
            hist_meta = {"forecast_from": FORECAST_FROM}

        entry = {"is_aggregate": True, "name": AGG_NAME[code], "emoji": AGG_EMOJI[code]}
        if latest:
            entry["latest"] = latest
        if hist:
            entry["hist"] = hist
        if hist_meta:
            entry["hist_meta"] = hist_meta
        if trade:
            entry["trade"] = trade
        pyr = build_pyramid_entry(code, raw_pyramid, snap)
        if pyr:
            entry["pyramid"] = pyr
        out[code] = entry
    ok = n_ok >= 5
    print(f"[aggregates] {len(out)} entités ({n_ok}/7 indicateurs WB, "
          f"WEO={'oui' if weo else 'non'})")
    return out, ok


# --- Slugs TradingEconomics : nom EN WB slugifié + overrides (le front masque si absent)
TE_SLUG_OVERRIDE = {
    "USA": "united-states", "GBR": "united-kingdom", "KOR": "south-korea",
    "PRK": "north-korea", "ARE": "united-arab-emirates", "CZE": "czech-republic",
    "RUS": "russia", "VNM": "vietnam", "EGY": "egypt", "IRN": "iran",
    "SYR": "syria", "VEN": "venezuela", "LAO": "laos", "SVK": "slovakia",
    "KGZ": "kyrgyzstan", "BRN": "brunei", "MKD": "macedonia", "TUR": "turkey",
    "CIV": "ivory-coast", "COD": "congo", "COG": "republic-of-congo",
    "GMB": "gambia", "BHS": "bahamas", "YEM": "yemen", "HKG": "hong-kong",
    "MAC": "macau", "CPV": "cape-verde", "SWZ": "swaziland", "TLS": "east-timor",
    "FSM": "micronesia", "SOM": "somalia", "PRI": "puerto-rico",
    "KNA": "saint-kitts-and-nevis", "LCA": "saint-lucia",
    "VCT": "saint-vincent-and-the-grenadines", "SXM": "sint-maarten",
    "MAF": "saint-martin", "VIR": "united-states-virgin-islands",
}


def fetch_te_slugs(a3set):
    """meta.slugs = {a3: slug TradingEconomics} (nom EN WB slugifié + overrides)."""
    raw = _cached_fetch("wb_country_en.json",
                        "https://api.worldbank.org/v2/country?per_page=400&format=json",
                        ttl_days=30)
    names = {}
    if raw:
        try:
            d = json.loads(raw)
            names = {r["id"]: r.get("name", "") for r in (d[1] or [])}
        except (ValueError, IndexError, TypeError):
            names = {}
    out = {}
    for a3 in a3set:
        if a3 in TE_SLUG_OVERRIDE:
            out[a3] = TE_SLUG_OVERRIDE[a3]
        elif names.get(a3):
            out[a3] = _slug(names[a3])
    print(f"[slugs] {len(out)}/{len(a3set)} slugs TradingEconomics")
    return out


# --- Notations souveraines 3 agences (scraping TradingEconomics) — SPEC v5 bloc 4 ----------
# Page /{slug}/rating : une table « Agency | Rating | Outlook | Date », rangées historiques
# triées par date décroissante → la 1re occurrence par agence = la note courante.
# FRAGILE (scraping) : garde large, tempo, skip propre si la table est absente.

def _te_agency_key(name):
    a = (name or "").lower()
    if "moody" in a:
        return "moody"
    if "s&" in a or "standard" in a or a.strip() == "sp":
        return "sp"
    if "fitch" in a:
        return "fitch"
    if "dbrs" in a:
        return "dbrs"
    return None


def _te_date_key(s):
    """'Oct 24 2025' → timestamp float (pour trier), None si non parsable."""
    try:
        return datetime.strptime((s or "").strip(), "%b %d %Y").timestamp()
    except Exception:  # noqa: BLE001
        return None


def _te_date_iso(s):
    try:
        return datetime.strptime((s or "").strip(), "%b %d %Y").date().isoformat()
    except Exception:  # noqa: BLE001
        return (s or "").strip() or None


def _te_unescape(s):
    import html as _html
    return (_html.unescape(s or "").strip() or None)


def _parse_te_rating(html_text, url):
    """→ {sp,moody,fitch,dbrs,outlook,as_of,source_url} ou None si pas de table de notes."""
    best, order = {}, []
    for tb in re.findall(r"<table[^>]*>(.*?)</table>", html_text, re.S):
        if not any(k in tb for k in ("Moody", "S&amp;P", "S&P", "Fitch", "DBRS")):
            continue
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            cells = [c for c in cells if c]
            if len(cells) < 4:
                continue
            ag = _te_agency_key(cells[0])
            if not ag or ag in best:      # date-desc → 1re occurrence = note la plus récente
                continue
            grade = _te_unescape(cells[1])
            if not grade:
                continue
            best[ag] = {"grade": grade, "outlook": _te_unescape(cells[2]),
                        "date": cells[3], "k": _te_date_key(cells[3])}
            order.append(ag)
    grades = {ag: best[ag]["grade"] for ag in ("sp", "moody", "fitch", "dbrs") if ag in best}
    if not grades:
        return None
    latest = max(order, key=lambda a: best[a]["k"] if best[a]["k"] is not None else -1.0)
    grades["outlook"] = best[latest]["outlook"]
    grades["as_of"] = _te_date_iso(best[latest]["date"])
    grades["source_url"] = url
    return grades


def fetch_te_ratings(slugs, only=None):
    """Notations souveraines via TradingEconomics /{slug}/rating (~120 pays notés).
    → {a3: {sp,moody,fitch,dbrs,outlook,as_of,source_url}}. Les non-notés → absents."""
    out, n_ok, n_try = {}, 0, 0
    for a3, slug in sorted(slugs.items()):
        if only and a3 not in only:
            continue
        n_try += 1
        url = f"https://tradingeconomics.com/{slug}/rating"
        try:
            html_text = http_get(url, timeout=30, retries=2)
            if not html_text or "rating" not in html_text.lower():
                continue
            r = _parse_te_rating(html_text, url)
            if r and any(r.get(k) for k in ("sp", "moody", "fitch")):
                out[a3] = r
                n_ok += 1
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[WARN] TE rating {a3}/{slug} : {e!r}\n")
        time.sleep(0.4)
    print(f"[ratings] {n_ok}/{n_try} pays notés (S&P/Moody's/Fitch/DBRS)")
    return out


# ══ §3c — ENTREPRISES ═════════════════════════════════════════════════════════

# Valeur = slug unique (dir == phrase) OU tuple (dir, phrase) pour les pays dont
# l'URL diffère : les noms en « the- » ont un répertoire court mais une phrase « the-… »
# (vérifié : the-usa, the-uk, the-netherlands, the-uae, the-philippines, the-czech-republic).
CMC_SLUG_OVERRIDE = {
    "USA": ("usa", "the-usa"), "GBR": ("united-kingdom", "the-uk"),
    "NLD": ("netherlands", "the-netherlands"), "ARE": ("united-arab-emirates", "the-uae"),
    "PHL": ("philippines", "the-philippines"), "CZE": ("czech-republic", "the-czech-republic"),
    "KOR": "south-korea", "VNM": "vietnam", "RUS": "russia", "SAU": "saudi-arabia",
    "ZAF": "south-africa", "HKG": "hong-kong", "TWN": "taiwan", "NZL": "new-zealand",
    "IRL": "ireland",
}
# Pays effectivement présents sur companiesmarketcap (essayés en priorité ; les autres
# tombent en 404 et basculent QLever/Wikipedia — l'essai reste bon marché).
CMC_KNOWN = {
    "FRA", "DEU", "GBR", "CHE", "NLD", "ITA", "ESP", "SWE", "DNK", "FIN", "NOR", "BEL",
    "IRL", "AUT", "POL", "PRT", "GRC", "RUS", "TUR", "LUX", "CZE", "HUN",
    "USA", "CHN", "JPN", "IND", "KOR", "TWN", "HKG", "SGP", "IDN", "THA", "MYS", "PHL", "VNM",
    "AUS", "NZL", "SAU", "ARE", "ISR", "QAT", "KWT", "ZAF", "EGY", "NGA", "MAR",
    "BRA", "MEX", "CAN", "CHL", "ARG", "COL", "PER",
}


def cmc_fetch(iso2name, a3):
    """Classement companiesmarketcap (mcap USD). → list[dict] ou None."""
    slug = CMC_SLUG_OVERRIDE.get(a3, _EMPTY)
    if slug is _EMPTY:
        nm = iso2name.get(a3)
        slug = _slug(nm) if nm else None
    if not slug:
        return None, None
    sdir, sphrase = slug if isinstance(slug, tuple) else (slug, slug)
    url = f"https://companiesmarketcap.com/{sdir}/largest-companies-in-{sphrase}-by-market-cap/"
    html = http_get(url, timeout=30, retries=2)
    if not html or "marketcap-table" not in html:
        return None, None
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        rank = re.search(r'rank-td[^>]*data-sort="(\d+)"', row)
        name = re.search(r'company-name">([^<]+)<', row)
        code = re.search(r'company-code"><span[^>]*></span>([^<]+)<', row)
        mcap = re.search(r'<td class="td-right" data-sort="(\d+)"', row)
        if not (rank and name and mcap):
            continue
        out.append({
            "rank": int(rank.group(1)),
            "name": name.group(1).strip(),
            "ticker": code.group(1).strip() if code else None,
            "mcap_usd": int(mcap.group(1)),
        })
    if len(out) < 3:
        return None, None
    return out, url


_EMPTY = object()

QLV_PREFIX = ("PREFIX wd: <http://www.wikidata.org/entity/>\n"
              "PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n"
              "PREFIX p: <http://www.wikidata.org/prop/>\n"
              "PREFIX psv: <http://www.wikidata.org/prop/statement/value/>\n"
              "PREFIX pq: <http://www.wikidata.org/prop/qualifier/>\n"
              "PREFIX wikibase: <http://wikiba.se/ontology#>\n"
              "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n")


def qlever_query(sparql, timeout=120):
    return http_get_json("https://qlever.dev/api/wikidata", timeout=timeout, method="POST",
                         data={"query": sparql},
                         headers={"Accept": "application/sparql-results+json"})


def _qg(b, k):
    v = b.get(k)
    return v["value"] if v else ""


def qlever_country(qid, prop, use_cache=True, ttl_days=3):
    """Sociétés d'un pays avec dernière valeur datée (P2226 mcap ou P2139 revenu).

    → list[dict{qid,label,amt,unit,date,logo,isin,ticker,industry,website}]. Filtre
    entreprise (P31/P279* Q4830453). Dédup dernière valeur. Réponse cachée (REF_DIR)."""
    cache_p = REF_DIR / f"qlv_{qid}_{prop}.json"
    if use_cache and cache_p.exists() and (time.time() - cache_p.stat().st_mtime) < ttl_days * 86400:
        try:
            return json.loads(cache_p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    q = QLV_PREFIX + f"""
SELECT ?company ?label ?amt ?unitLabel ?date ?logo ?isin ?ticker ?indLabel ?website WHERE {{
  {{ SELECT ?company (MAX(?d) AS ?date) WHERE {{
      ?company wdt:P17 wd:{qid} . ?company wdt:P31/wdt:P279* wd:Q4830453 .
      ?company p:{prop} ?s . ?s pq:P585 ?d . }} GROUP BY ?company }}
  ?company p:{prop} ?st . ?st pq:P585 ?date . ?st psv:{prop} ?v . ?v wikibase:quantityAmount ?amt .
  OPTIONAL {{ ?v wikibase:quantityUnit ?u . ?u rdfs:label ?unitLabel . FILTER(LANG(?unitLabel)="en") }}
  OPTIONAL {{ ?company rdfs:label ?label . FILTER(LANG(?label)="en") }}
  OPTIONAL {{ ?company wdt:P154 ?logo . }}
  OPTIONAL {{ ?company wdt:P946 ?isin . }}
  OPTIONAL {{ ?company wdt:P249 ?ticker . }}
  OPTIONAL {{ ?company wdt:P452 ?ind . ?ind rdfs:label ?indLabel . FILTER(LANG(?indLabel)="en") }}
  OPTIONAL {{ ?company wdt:P856 ?website . }}
}}"""
    d = qlever_query(q, timeout=90)
    if not d or "results" not in d:
        return []
    by = {}
    for b in d["results"]["bindings"]:
        c = _qg(b, "company")
        if not c:
            continue
        if c not in by:
            by[c] = {"qid": c.rsplit("/", 1)[-1], "label": _qg(b, "label"),
                     "amt": _qg(b, "amt"), "unit": _qg(b, "unitLabel"), "date": _qg(b, "date")[:10],
                     "logo": _qg(b, "logo"), "isin": _qg(b, "isin"), "ticker": _qg(b, "ticker"),
                     "industry": _qg(b, "indLabel"), "website": _qg(b, "website")}
        else:
            for k in ("logo", "isin", "ticker", "industry", "label", "website"):
                if _qg(b, k) and not by[c].get(k):
                    by[c][k] = _qg(b, k)
    rows = list(by.values())
    cache_p.write_text(json.dumps(rows), encoding="utf-8")
    time.sleep(0.15)
    return rows


def load_global_mcap(ttl_days=1):
    """UNE requête QLever : toutes les sociétés à mcap datée (dernière par société) +
    pays + logo P154 + site. Sert d'index d'enrichissement (logo/qid/website) pour TOUS
    les pays et de source primaire de repli. Filtre entreprise appliqué en aval.

    → ({qid_pays: [rows]}, {nom_normalisé: row})."""
    cache_p = REF_DIR / "global_mcap.json"
    rows = None
    if cache_p.exists() and (time.time() - cache_p.stat().st_mtime) < ttl_days * 86400:
        try:
            rows = json.loads(cache_p.read_text(encoding="utf-8"))
        except ValueError:
            rows = None
    if rows is None:
        q = QLV_PREFIX + """
SELECT ?company ?label ?country ?amt ?unitLabel ?date ?logo ?website WHERE {
  { SELECT ?company (MAX(?d) AS ?date) WHERE {
      ?company wdt:P17 ?c . ?company p:P2226 ?s . ?s pq:P585 ?d . } GROUP BY ?company }
  ?company wdt:P17 ?country .
  ?company p:P2226 ?st . ?st pq:P585 ?date . ?st psv:P2226 ?v . ?v wikibase:quantityAmount ?amt .
  OPTIONAL { ?v wikibase:quantityUnit ?u . ?u rdfs:label ?unitLabel . FILTER(LANG(?unitLabel)="en") }
  OPTIONAL { ?company rdfs:label ?label . FILTER(LANG(?label)="en") }
  OPTIONAL { ?company wdt:P154 ?logo . }
  OPTIONAL { ?company wdt:P856 ?website . }
}"""
        d = qlever_query(q, timeout=180)
        by_c = {}
        if d and "results" in d:
            for b in d["results"]["bindings"]:
                c = _qg(b, "company")
                if not c:
                    continue
                row = {"qid": c.rsplit("/", 1)[-1], "label": _qg(b, "label"),
                       "country": _qg(b, "country").rsplit("/", 1)[-1], "amt": _qg(b, "amt"),
                       "unit": _qg(b, "unitLabel"), "date": _qg(b, "date")[:10],
                       "logo": _qg(b, "logo"), "website": _qg(b, "website")}
                if c not in by_c or (row["logo"] and not by_c[c]["logo"]):
                    by_c[c] = row
            rows = list(by_c.values())
            cache_p.write_text(json.dumps(rows), encoding="utf-8")
            print(f"[QLever] global mcap : {len(rows)} sociétés")
        elif cache_p.exists():
            rows = json.loads(cache_p.read_text(encoding="utf-8"))
            sys.stderr.write("[WARN] global mcap : QLever KO, cache réutilisé\n")
        else:
            rows = []
            sys.stderr.write("[WARN] global mcap : QLever KO, aucun cache\n")
    by_country, by_name = {}, {}
    for r in rows:
        by_country.setdefault(r["country"], []).append(r)
        if r.get("label"):
            by_name.setdefault(_norm(r["label"]), r)
    return by_country, by_name


# unités monétaires Wikidata (P2226/P2139) → devise ISO pour conversion USD
UNIT_TO_CCY = {
    "United States dollar": "USD", "euro": "EUR", "pound sterling": "GBP", "pound": "GBP",
    "Japanese yen": "JPY", "yen": "JPY", "renminbi": "CNY", "yuan": "CNY",
    "Indian rupee": "INR", "Nigerian naira": "NGN", "naira": "NGN", "Brazilian real": "BRL",
    "Swiss franc": "CHF", "Canadian dollar": "CAD", "Australian dollar": "AUD",
    "Hong Kong dollar": "HKD", "South Korean won": "KRW", "won": "KRW",
    "Saudi riyal": "SAR", "South African rand": "ZAR", "rand": "ZAR", "Mexican peso": "MXN",
    "Danish krone": "DKK", "Swedish krona": "SEK", "Norwegian krone": "NOK",
    "New Taiwan dollar": "TWD", "Singapore dollar": "SGD", "Russian ruble": "RUB",
    "Turkish lira": "TRY", "Thai baht": "THB", "baht": "THB", "Indonesian rupiah": "IDR",
    "Malaysian ringgit": "MYR", "Philippine peso": "PHP", "Israeli new shekel": "ILS",
    "Polish złoty": "PLN", "United Arab Emirates dirham": "AED", "Qatari riyal": "QAR",
    "Kuwaiti dinar": "KWD", "Egyptian pound": "EGP", "Moroccan dirham": "MAD",
    "New Zealand dollar": "NZD", "Chilean peso": "CLP", "Colombian peso": "COP",
    "Peruvian sol": "PEN", "Argentine peso": "ARS",
}


def _qlv_usd(item, fx):
    """Montant en USD depuis (amt, unit). None si non convertible."""
    try:
        amt = float(item["amt"])
    except (TypeError, ValueError):
        return None
    ccy = UNIT_TO_CCY.get(item.get("unit") or "")
    if ccy is None:
        # certains montants Wikidata sont déjà en USD sans unité étiquetée
        ccy = "USD" if not item.get("unit") else None
    if ccy is None:
        return None
    rate = fx.get(ccy)
    return amt * rate if rate else None


def wikipedia_companies(country_name):
    """Wikipedia « List of largest companies in <pays> ». Préfère mcap USD, sinon revenu.

    → (list[dict{rank,name,ticker,industry,value_usd,metric}], url, metric) ou (None,…)."""
    page = f"List of largest companies in {country_name}"
    url = ("https://en.wikipedia.org/w/api.php?action=parse&format=json&formatversion=2"
           f"&prop=text&page={page.replace(' ', '_')}")
    d = http_get_json(url, timeout=30, retries=2)
    if not d or "parse" not in d:
        return None, None, None
    html = d["parse"]["text"]
    tables = re.findall(r'<table[^>]*wikitable[^>]*>(.*?)</table>', html, re.S)
    best = None
    for tbl in tables:
        rows = re.findall(r"<tr>(.*?)</tr>", tbl, re.S)
        if len(rows) < 4:
            continue
        hdr = [re.sub(r"<[^>]+>", "", c).strip().lower()
               for c in re.findall(r"<th[^>]*>(.*?)</th>", rows[0], re.S)]
        # repère la colonne valeur (mcap USD > revenu USD)
        val_col, metric, is_usd = None, None, False
        for i, h in enumerate(hdr):
            if "market cap" in h and ("us$" in h or "usd" in h or "$" in h):
                val_col, metric, is_usd = i, "market_cap", True
                break
        if val_col is None:
            for i, h in enumerate(hdr):
                if "revenue" in h and ("us$" in h or "usd" in h or "$" in h):
                    val_col, metric, is_usd = i, "revenue", True
                    break
        if val_col is None:
            continue
        name_col = next((i for i, h in enumerate(hdr) if h in ("company", "name")), 1)
        tk_col = next((i for i, h in enumerate(hdr) if "ticker" in h), None)
        ind_col = next((i for i, h in enumerate(hdr) if "industry" in h or "sector" in h), None)
        items = []
        for row in rows[1:]:
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if len(cells) <= val_col or len(cells) <= name_col:
                continue
            name = cells[name_col].strip()
            raw = re.sub(r"[^\d.]", "", cells[val_col].replace(",", ""))
            if not name or not raw:
                continue
            try:
                v = float(raw)
            except ValueError:
                continue
            # heuristique d'échelle : « US$ millions » domine ces tables
            usd = v * 1e6
            items.append({
                "rank": len(items) + 1, "name": name,
                "ticker": cells[tk_col].strip() if tk_col is not None and len(cells) > tk_col else None,
                "industry": cells[ind_col].strip() if ind_col is not None and len(cells) > ind_col else None,
                "value_usd": usd, "metric": metric,
            })
        if items and (best is None or (metric == "market_cap" and best[2] != "market_cap")):
            best = (items, url.split("&page=")[0] + f" ({page})", metric)
            if metric == "market_cap":
                break
    if not best:
        return None, None, None
    items, _, metric = best
    return items, f"https://en.wikipedia.org/wiki/{page.replace(' ', '_')}", metric


def _norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(inc|corp|corporation|co|ltd|plc|sa|se|ag|nv|group|holdings?|company|"
               r"the|&|and|limited)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def enrich_items(items, *indexes):
    """Ajoute qid/isin/secteur/logo P154/site aux items (match par nom normalisé).
    `indexes` = dictionnaires {nom_normalisé: row} essayés dans l'ordre."""
    for it in items:
        key = _norm(it["name"])
        r = next((idx.get(key) for idx in indexes if idx and idx.get(key)), None)
        if not r:
            continue
        if r.get("qid") and not it.get("wikidata"):
            it["wikidata"] = r["qid"]
        if r.get("isin") and not it.get("isin"):
            it["isin"] = r["isin"]
        if r.get("industry") and not it.get("sector"):
            it["sector"] = r["industry"]
        if r.get("logo") and not it.get("_p154"):
            it["_p154"] = r["logo"]
        if r.get("website") and not it.get("_site"):
            it["_site"] = r["website"]


def build_companies(a3, meta_c, iso2name, iso2qid, fx, glob_by_country, glob_names,
                    old_entry, only_set, args):
    """Construit le bloc companies d'un pays selon la stratégie hybride honnête."""
    qid = iso2qid.get(a3)
    glob_rows = glob_by_country.get(qid, []) if qid else []
    glob_idx = {_norm(r["label"]): r for r in glob_rows if r.get("label")}
    items, metric, coverage, src_name, src_url = None, None, None, None, None

    # 1. companiesmarketcap (classement mcap USD)
    cmc, cmc_url = (None, None)
    if a3 in CMC_KNOWN or a3 in CMC_SLUG_OVERRIDE:
        cmc, cmc_url = cmc_fetch(iso2name, a3)
    if cmc:
        items = [{"rank": r["rank"], "name": r["name"], "ticker": r.get("ticker"),
                  "size_value_usd": r["mcap_usd"], "metric": "market_cap"} for r in cmc[:12]]
        metric, coverage = "market_cap", ("full" if len(items) >= 8 else "partial")
        src_name, src_url = "companiesmarketcap.com", cmc_url
        enrich_items(items, glob_idx, glob_names)   # logo P154/qid/site (index global, 0 appel)

    # 2. QLever mcap (USA & pays sans CMC mais Wikidata riche en mcap datée récente).
    #    L'index global (1 requête) tranche l'éligibilité ; on ne fait UNE requête live
    #    par pays (isin/ticker/secteur) que pour ces rares cas.
    if items is None and qid and len(_filter_recent_mcap(glob_rows)) >= 5:
        live = qlever_country(qid, "P2226")
        recent = _filter_recent_mcap(live) or _filter_recent_mcap(glob_rows)
        if len(recent) >= 5:
            recent.sort(key=lambda r: -(float(r["amt"]) if r["amt"] else 0))
            items = []
            for i, r in enumerate(recent[:12]):
                items.append({"rank": i + 1, "name": r["label"] or r["qid"],
                              "ticker": r.get("ticker") or None, "wikidata": r["qid"],
                              "isin": r.get("isin") or None, "sector": r.get("industry") or None,
                              "size_value_usd": _round_usd(float(r["amt"])),
                              "size_date": r["date"], "metric": "market_cap",
                              "_p154": r.get("logo") or None, "_site": r.get("website") or None})
            metric, coverage = "market_cap", ("full" if len(items) >= 8 else "partial")
            src_name, src_url = "Wikidata (QLever)", f"https://www.wikidata.org/wiki/{qid}"

    # 3. Wikipedia « List of largest companies in X » (marchés frontière)
    if items is None:
        nm = iso2name.get(a3)
        wk, wk_url, wk_metric = wikipedia_companies(nm) if nm else (None, None, None)
        if wk:
            items = [{"rank": w["rank"], "name": w["name"], "ticker": w.get("ticker"),
                      "sector": w.get("industry"), "size_value_usd": _round_usd(w["value_usd"]),
                      "metric": wk_metric, "size_date": None} for w in wk[:12]]
            metric = wk_metric
            coverage = "partial" if wk_metric == "market_cap" else "thin"
            src_name, src_url = "Wikipedia", wk_url
            enrich_items(items, glob_idx, glob_names)

    # 4. Pays maigres : Wikidata revenu (banques/entreprises d'État)
    if items is None and qid:
        rev = qlever_country(qid, "P2139")
        rev = [r for r in rev if _qlv_usd(r, fx)]
        if rev:
            rev.sort(key=lambda r: -(_qlv_usd(r, fx) or 0))
            items = []
            for i, r in enumerate(rev[:10]):
                items.append({"rank": i + 1, "name": r["label"] or r["qid"], "wikidata": r["qid"],
                              "ticker": r.get("ticker") or None, "isin": r.get("isin") or None,
                              "sector": r.get("industry") or None,
                              "size_value_usd": _round_usd(_qlv_usd(r, fx)),
                              "size_native": {"value": sig4(float(r["amt"])),
                                              "currency": UNIT_TO_CCY.get(r.get("unit") or "", r.get("unit"))},
                              "size_date": r["date"], "metric": "revenue",
                              "_p154": r.get("logo") or None, "_site": r.get("website") or None})
            metric, coverage = "revenue", "thin"
            src_name, src_url = "Wikidata (QLever)", f"https://www.wikidata.org/wiki/{qid}"

    if items is None:
        return None

    # re-pricing Yahoo PAR DÉFAUT (mcap fraîche, même jour, devise native, horodatée) —
    # homogénéise les valeurs quelle que soit la source de classement. --no-reprice pour couper.
    if not getattr(args, "no_reprice", False) and metric == "market_cap":
        _reprice_yahoo(items, fx)

    # tri final par valeur, top 10
    items = [it for it in items if it.get("size_value_usd")]
    items.sort(key=lambda it: -it["size_value_usd"])
    items = items[:10]
    for i, it in enumerate(items):
        it["rank"] = i + 1
        if not it.get("size_date"):
            it["size_date"] = TODAY

    return {"items": items, "ranking_metric": metric, "coverage": coverage,
            "source_name": src_name, "source_url": src_url, "as_of": TODAY,
            "retrieved": NOW.isoformat(timespec="seconds")}


def _filter_recent_mcap(rows):
    """mcap Wikidata : vire le stale (date < CUR-3) et l'aberrant (> 8000 Md$ ou <= 0)."""
    out = []
    for r in rows:
        try:
            amt = float(r["amt"])
        except (TypeError, ValueError):
            continue
        if amt <= 0 or amt > 8e12:
            continue
        yr = r.get("date", "")[:4]
        if yr.isdigit() and int(yr) < CUR_YEAR - 3:
            continue
        out.append(r)
    return out


def _round_usd(x):
    return sig4(x) if x else None


_YQ = {"session": None, "crumb": None}


def _yahoo_session():
    """Session curl_cffi authentifiée (cookie A1 + crumb). L'endpoint quote v7 renvoie
    401 sans crumb depuis ~2024. Construite une fois par run, mémoïsée."""
    if _YQ["session"] is not None:
        return _YQ["session"], _YQ["crumb"]
    s = cr.Session(impersonate="chrome120")
    for warm in ("https://fc.yahoo.com", "https://finance.yahoo.com"):
        try:
            s.get(warm, timeout=15)
        except Exception:
            pass
    crumb = None
    try:
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).text.strip()
    except Exception:
        crumb = None
    if not crumb or len(crumb) > 40 or "<" in crumb:
        crumb = None
    _YQ["session"], _YQ["crumb"] = s, crumb
    return s, crumb


def _reprice_yahoo(items, fx):
    """Remplace size_value_usd par la mcap Yahoo fraîche + datée + devise native quand un
    ticker résout (session à crumb, requêtes par lots de 40). Échec (crumb/rate-limit/ticker
    absent) → données source intactes (jamais dégradées)."""
    import urllib.parse as _up
    tickers = [it.get("ticker") for it in items if it.get("ticker")]
    if not tickers:
        return
    s, crumb = _yahoo_session()
    if not crumb:
        sys.stderr.write("[WARN] reprice : crumb Yahoo indisponible → valeurs source conservées\n")
        return
    quotes = {}
    for i in range(0, len(tickers), 40):
        syms = _up.quote(",".join(tickers[i:i + 40]), safe="")
        url = (f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}"
               f"&crumb={_up.quote(crumb, safe='')}")
        for attempt in range(3):
            try:
                r = s.get(url, impersonate="chrome120", timeout=25)
                if r.status_code == 200:
                    for q in r.json().get("quoteResponse", {}).get("result", []):
                        if q.get("symbol"):
                            quotes[q["symbol"]] = q
                    break
            except Exception:
                pass
            time.sleep(1.2 * (attempt + 1))
        time.sleep(0.4)
    for it in items:
        q = quotes.get(it.get("ticker"))
        if not q:
            continue
        mc, ccy, ts = q.get("marketCap"), q.get("currency"), q.get("regularMarketTime")
        rate = fx.get(ccy) if ccy else None
        if mc and rate:
            it["size_value_usd"] = _round_usd(mc * rate)
            it["size_native"] = {"value": sig4(mc), "currency": ccy}
            if ts:
                it["size_date"] = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            it["repriced"] = "yahoo"


# ══ §3d — LOGOS ═══════════════════════════════════════════════════════════════

def _write_logo(iso3, base, content, ext):
    d = LOGO_DIR / iso3
    d.mkdir(parents=True, exist_ok=True)
    fn = f"{_safe_fname(base)}.{ext}"
    (d / fn).write_bytes(content)
    return f"assets/logos/atlas/{iso3}/{fn}"


def _existing_logo(iso3, base):
    d = LOGO_DIR / iso3
    for ext in ("svg", "png", "webp"):
        p = d / f"{_safe_fname(base)}.{ext}"
        if p.exists() and p.stat().st_size > 200:
            return f"assets/logos/atlas/{iso3}/{p.name}"
    return None


def download_logo(iso3, item):
    """Télécharge le logo (idempotent). Priorité P154 svg → CMC png → favicon.
    → dict {local, source, source_url, license} ou None."""
    base = item.get("ticker") or item.get("name") or "logo"
    existing = _existing_logo(iso3, base)
    # 1. Wikidata P154 (Special:FilePath)
    p154 = item.get("_p154")
    if p154:
        url = p154 if p154.startswith("http") else \
            f"https://commons.wikimedia.org/wiki/Special:FilePath/{p154}"
        ext = "svg" if url.lower().endswith(".svg") else "png"
        if not existing:
            content = http_get(url if ext == "svg" else url + "?width=128",
                               timeout=20, retries=2, binary=True)
            if content and len(content) > 200:
                local = _write_logo(iso3, base, content, ext)
                return {"local": local, "source": "wikidata_p154", "source_url": url,
                        "license": "Wikimedia Commons (voir fichier)"}
        elif existing:
            return {"local": existing, "source": "wikidata_p154", "source_url": url,
                    "license": "Wikimedia Commons (voir fichier)"}
    # 2. companiesmarketcap PNG (keyless)
    tk = item.get("ticker")
    if tk:
        if existing and existing.endswith(".png"):
            return {"local": existing, "source": "companiesmarketcap",
                    "source_url": f"https://companiesmarketcap.com/img/company-logos/64/{tk}.png",
                    "license": "companiesmarketcap.com"}
        url = f"https://companiesmarketcap.com/img/company-logos/64/{tk}.png"
        if not existing:
            content = http_get(url, timeout=20, retries=1, binary=True)
            if content and len(content) > 200:
                local = _write_logo(iso3, base, content, "png")
                return {"local": local, "source": "companiesmarketcap", "source_url": url,
                        "license": "companiesmarketcap.com"}
    if existing:
        return {"local": existing, "source": "cache", "source_url": None, "license": None}
    # 3. Google favicon (repli universel) quand un site est connu (P856)
    site = item.get("_site")
    if site:
        dom = re.sub(r"^https?://", "", site).split("/")[0]
        if dom:
            furl = f"https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&size=64&url=https://{dom}"
            content = http_get(furl, timeout=15, retries=1, binary=True)
            if content and len(content) > 300:      # >300 : évite le favicon "vide" 16×16
                local = _write_logo(iso3, base, content, "png")
                return {"local": local, "source": "google_favicon",
                        "source_url": f"https://{dom}", "license": None}
    return None


# ══ ASSEMBLAGE ════════════════════════════════════════════════════════════════

HIST_OWNER = {
    "WB": ["gdp", "gdp_real", "gdp_ppp", "gdp_pc", "reserves", "pop", "pop_gr",
           "life", "gini", "urban",
           "exports_gdp", "imports_gdp", "trade_gdp",
           # v5 — sinon le hist frais est SILENCIEUSEMENT jeté par la fusion par source
           "tfr", "dep_tot", "dep_old", "dep_yg", "imr", "health_gdp",
           "internet", "mktcap_gdp", "credit_gdp",
           # Entrée du calcul MPD (retirée du cache après coup, mais doit passer la fusion)
           "ngdp_lcu",
           # E — profondeur (idem : à inscrire ici sinon jetées)
           "military", "rd", "youth_unemp", "extdebt",
           # v12 — investissement, épargne, rente des ressources
           "invest", "savings", "resrents",
           # v16 — montants en valeur (USD courants)
           "savings_v", "invest_v", "military_v", "mktcap_gdp_v", "extdebt_v", "cab_v",
           # Potentiel futur — qualité de l'éducation
           "eduq",
           # Travail & revenus (idem : à inscrire ici sinon jetées par la fusion par source)
           "emp_ratio", "lfpr", "lfpr_fe", "lfpr_ma", "neet", "vuln_emp", "selfemp",
           "emp_agr", "emp_ind", "emp_srv", "gdp_emp", "gni_pc", "cons_pc",
           # v18 — volet Prix (idem : sans cette ligne, la série fraîche est
           # SILENCIEUSEMENT jetée par la fusion par source)
           "deflator", "m2_gr", "rinr", "fx_lcu", "ppp_lcu"],
    "WGI": ["wgi_rl", "wgi_ge", "wgi_pv", "wgi_cc"],
    "IMF": ["growth", "infl", "cab", "debt", "fiscal", "int_gdp", "int_exp",
            # v18 — inflation fin de période
            "infl_eop"],
    # v5 — 7 séries OWID + E top10 (idem : à inscrire ici sinon jetées par la fusion par source)
    "OWID": ["fh", "vdem", "cpi",
             "co2pc", "energypc", "enintensity", "renew_elec", "hdi", "medage", "schooling",
             "top10",
             # Potentiel futur — part du travail (exposant Cobb-Douglas mesuré)
             "labsh",
             # Travail & revenus — heures travaillées par an et par actif
             "hours",
             # Prélèvements obligatoires (UNU-WIDER GRD) — idem : sans cette ligne
             # la série fraîche est SILENCIEUSEMENT jetée par la fusion par source.
             "taxrev"],
    # PISA (OCDE via miroir OWID) — source séparée d'OWID : une panne du grapher
    # multi-dimensions ne doit pas jeter fh/vdem/cpi (et réciproquement).
    "PISA": PISA_KEYS,
}

HS4_FR = {
    "2710": "Produits pétroliers raffinés", "9999": "Marchandises non spécifiées",
    "XXXX": "Produits non classés", "7108": "Or", "2709": "Pétrole brut",
    "8703": "Automobiles", "2711": "Gaz de pétrole & hydrocarbures",
    "3004": "Médicaments", "8517": "Téléphones & équipements télécom",
    "8708": "Pièces automobiles", "0303": "Poissons congelés", "8544": "Fils & câbles isolés",
    "7204": "Ferrailles", "8471": "Ordinateurs", "8901": "Navires de transport",
    "8542": "Circuits intégrés électroniques", "1801": "Fèves de cacao",
    "2603": "Minerais de cuivre", "7102": "Diamants", "2716": "Énergie électrique",
    "7113": "Bijouterie", "0901": "Café", "0304": "Filets de poisson",
    "9018": "Instruments médicaux", "3002": "Sang & produits immunologiques",
    "2601": "Minerai de fer", "1207": "Graines oléagineuses", "0302": "Poissons frais",
    "0306": "Crustacés", "7601": "Aluminium brut", "4407": "Bois scié", "7403": "Cuivre affiné",
    "7404": "Déchets de cuivre", "2701": "Houille (charbon)", "8411": "Turbines & turboréacteurs",
    "6110": "Pulls & articles en maille", "8903": "Yachts & bateaux de plaisance",
    "6203": "Costumes & vestes homme", "7202": "Ferro-alliages", "3102": "Engrais azotés",
    "8704": "Camions & véhicules de transport", "6109": "T-shirts", "6204": "Costumes & robes femme",
    "2208": "Spiritueux", "2616": "Minerais de métaux précieux", "0307": "Mollusques",
    "5201": "Coton", "2523": "Ciment", "1001": "Blé", "8807": "Pièces d'aéronefs",
    "8802": "Aéronefs", "1511": "Huile de palme", "4011": "Pneumatiques neufs",
    "8905": "Navires spéciaux (plateformes)", "1201": "Soja", "1701": "Sucre", "0803": "Bananes",
    "4403": "Bois brut", "2905": "Alcools & dérivés", "1006": "Riz",
    "0801": "Noix de coco & du Brésil", "0804": "Dattes, figues, ananas",
    "1301": "Gommes & résines", "8537": "Tableaux électriques", "2301": "Farines de viande/poisson",
    "9021": "Appareils orthopédiques", "0202": "Viande bovine congelée",
    "4202": "Bagages & maroquinerie", "8536": "Appareillage électrique",
    "6403": "Chaussures en cuir", "8906": "Autres navires", "2304": "Tourteaux de soja",
    "1005": "Maïs", "0406": "Fromages", "2402": "Cigares & cigarettes",
    "1604": "Poisson préparé/conserve", "8504": "Transformateurs électriques",
    "1509": "Huile d'olive", "7602": "Déchets d'aluminium", "3901": "Polymères d'éthylène",
    "0802": "Fruits à coque", "2202": "Boissons (eaux, sodas)", "6104": "Ensembles femme (maille)",
    "2608": "Minerais de zinc", "8421": "Centrifugeuses & filtres", "0713": "Légumes secs",
    "0902": "Thé", "3104": "Engrais potassiques", "8507": "Batteries & accumulateurs",
    "7501": "Mattes de nickel", "2606": "Bauxite (minerai d'aluminium)", "2713": "Coke de pétrole",
    "7207": "Demi-produits en acier", "0204": "Viande ovine/caprine", "8904": "Remorqueurs",
    "0810": "Fruits frais", "1507": "Huile de soja", "7203": "Fer réduit direct",
    "6205": "Chemises homme", "2517": "Cailloux & graviers", "9401": "Sièges",
    "9403": "Meubles", "4703": "Pâte à papier chimique", "1513": "Huile de coco/coprah",
    "7402": "Cuivre non affiné", "2615": "Minerais de niobium/vanadium", "2609": "Minerais d'étain",
    "3301": "Huiles essentielles", "8473": "Pièces de machines de bureau", "0603": "Fleurs coupées",
    "2309": "Aliments pour animaux", "0305": "Poisson séché/fumé", "1101": "Farine de blé",
    "0106": "Animaux vivants", "4001": "Caoutchouc naturel", "2510": "Phosphates de calcium",
    "8529": "Pièces d'émetteurs/récepteurs", "2614": "Minerais de titane", "2401": "Tabac brut",
    "0201": "Viande bovine fraîche", "0806": "Raisins", "6406": "Parties de chaussures",
    "8414": "Pompes à air/vide", "2814": "Ammoniac", "8523": "Supports enregistrés",
    "9102": "Montres", "9701": "Peintures & œuvres d'art", "4907": "Billets & timbres",
    "7308": "Constructions métalliques", "1905": "Produits de boulangerie",
    "8481": "Robinetterie & vannes", "8479": "Machines spécialisées", "8443": "Imprimantes",
    "3808": "Pesticides & désinfectants", "8525": "Caméras & émetteurs vidéo",
    "3923": "Emballages plastiques", "7010": "Bouteilles en verre", "8409": "Pièces de moteurs",
    "2818": "Alumine & corindon", "8802b": "", "0805": "Agrumes", "0808": "Pommes & poires",
    "5208": "Tissus de coton", "1602": "Préparations de viande", "3105": "Engrais composés",
    "0207": "Viande de volaille", "1905b": "", "2204": "Vins", "2402b": "",
    "8483": "Arbres de transmission & engrenages", "2917": "Acides carboxyliques",
    "8408": "Moteurs diesel", "8431": "Pièces d'engins de levage", "7219": "Acier inox laminé",
    "7208": "Acier laminé à chaud", "3920": "Plaques plastiques", "8302": "Garnitures métalliques",
    "2915": "Acides gras", "3902": "Polymères de propylène", "8407": "Moteurs à explosion",
}


def hs4_label(code, hs4_en):
    fr = HS4_FR.get(code)
    en = hs4_en.get(code, "")
    return fr or en or None, en or None


def assemble(meta, old, args):
    countries_meta = meta["countries"]
    a3set_all = set(countries_meta)
    only_set = set(x.strip().upper() for x in args.only.split(",")) if args.only else None
    a3set = {a3 for a3 in a3set_all if (only_set is None or a3 in only_set)}
    old_countries = (old or {}).get("countries", {})

    print(f"[atlas_detail] {len(a3set)} pays ciblés"
          + (f" (--only {sorted(a3set)})" if only_set else "") + "\n")

    # tables de correspondance
    iso2name, fips2iso = load_geonames()
    iso2qid = load_iso2qid()
    hs4_en = load_hs4_en()
    iso2region = load_wb_regions()
    fx = load_fx()
    glob_by_country, glob_names = load_global_mcap()
    gec2region = load_factbook_regions()
    # ISO3 → (gec, region)
    iso2gec_region = {}
    for gec, region in gec2region.items():
        a3 = GEC_ISO_EXTRA.get(gec) or fips2iso.get(gec)
        if a3 and a3 in a3set:
            iso2gec_region[a3] = (gec, region)

    ok, failed = [], []

    # ── §3a HISTO ──
    wb_hist, agg_hist_wb, wb_ok = fetch_wb_hist(a3set)
    (ok if wb_ok else failed).append("WB")
    wgi_hist, agg_hist_wgi, wgi_ok = fetch_wgi_hist(a3set)
    (ok if wgi_ok else failed).append("WGI")
    imf_hist, forecast_from, imf_ok = fetch_imf_hist(a3set)
    (ok if imf_ok else failed).append("IMF")
    owid_hist, owid_agg, owid_ok = fetch_owid_hist(a3set)
    (ok if owid_ok else failed).append("OWID")
    pisa_hist, pisa_oecd, pisa_ok = fetch_pisa(a3set)
    (ok if pisa_ok else failed).append("PISA")
    pisa_wave, pisa_pending = pisa_vintage(pisa_hist)
    elec_mix, elecmix_ok = fetch_elec_mix(a3set)
    (ok if elecmix_ok else failed).append("ELECMIX")
    # Volet Prix : la STRUCTURE de l'inflation (sous-jacente / alimentation /
    # énergie), mensuelle chez l'OCDE, annualisée par atlas_prix.py pour se
    # superposer à l'IPC du FMI. Source à part : une panne de l'OCDE ne doit
    # rien effacer d'autre — et ne DOIT PAS effacer le volet lui-même (garde
    # par source, comme les autres blocs).
    oecd_prix_hist, oecd_prix_last, oecd_prix_ok = fetch_oecd_prices(a3set)
    (ok if oecd_prix_ok else failed).append("OECD_PRIX")

    # ── §3b COMMERCE ──
    wb_trade, wbt_ok = fetch_wb_trade(a3set)
    (ok if wbt_ok else failed).append("WB_TRADE")
    oec = fetch_oec_eci(a3set)
    (ok if oec else failed).append("OEC")
    harvard, harvard_agg, harv_ok = fetch_harvard_top(a3set, args.skip_harvard, iso2region)
    (ok if harv_ok else failed).append("HARVARD")
    factbook = fetch_factbook(a3set, iso2gec_region)
    (ok if factbook else failed).append("FACTBOOK")

    # ── §3e PYRAMIDE + AGRÉGATS + SLUGS (SPEC v3) ──
    pyr_raw, pyr_ok, pyr_snap = fetch_pyramid()
    (ok if pyr_ok else failed).append("PYRAMID")
    agg_entries, agg_ok = fetch_aggregates(pyr_raw, pyr_snap)
    (ok if agg_ok else failed).append("AGGREGATES")
    te_slugs = fetch_te_slugs(a3set)
    (ok if te_slugs else failed).append("SLUGS")
    # ── §3f NOTATIONS 3 AGENCES (SPEC v5) — scraping TE, best-effort ──
    if not getattr(args, "skip_ratings", False):
        te_ratings = fetch_te_ratings(te_slugs, only=only_set)
        (ok if len(te_ratings) >= (1 if only_set else 40) else failed).append("RATINGS")
    else:
        te_ratings = {}

    # ── assemblage par pays (base = copie de l'ancien → garde par source) ──
    hist_src_ok = {"WB": wb_ok, "WGI": wgi_ok, "IMF": imf_ok, "OWID": owid_ok,
                   "PISA": pisa_ok}
    countries, logo_stats, cov_stats = {}, {"wikidata_p154": 0, "companiesmarketcap": 0,
                                            "google_favicon": 0, "cache": 0}, {}
    comp_fail = 0

    for a3 in sorted(a3set):
        old_e = old_countries.get(a3, {})
        entry = copy.deepcopy(old_e) if old_e else {}

        # HIST : fusion par source (source OK → frais, sinon on garde l'ancien)
        hist = dict(entry.get("hist", {}))
        for src, keys in HIST_OWNER.items():
            if hist_src_ok.get(src):
                fresh = {"WB": wb_hist, "WGI": wgi_hist, "IMF": imf_hist,
                         "OWID": owid_hist, "PISA": pisa_hist}[src].get(a3, {})
                for k in keys:
                    if k in fresh:
                        hist[k] = fresh[k]
                    elif k in hist:
                        del hist[k]
        # unemp : IMF prioritaire, repli WB
        if imf_ok and imf_hist.get(a3, {}).get("unemp"):
            hist["unemp"] = imf_hist[a3]["unemp"]
        elif wb_ok and wb_hist.get(a3, {}).get("unemp"):
            hist["unemp"] = wb_hist[a3]["unemp"]
        if hist:
            entry["hist"] = hist
            entry["hist_meta"] = {"forecast_from": forecast_from}

        # TRADE
        trade = dict(entry.get("trade", {}))
        if wbt_ok:
            wt = wb_trade.get(a3, {})
            for k in ("sectors_va", "openness"):
                if k in wt:
                    trade[k] = wt[k]
                elif k in trade:
                    del trade[k]
        if oec is not None:
            if a3 in oec:
                trade["eci"] = oec[a3]
            else:
                trade["eci"] = None      # non classé (assumé)
        if harv_ok and a3 in harvard:
            h = harvard[a3]
            texp = []
            for te in h["top_exports"]:
                lab, lab_en = hs4_label(te["hs4"], hs4_en)
                d = {"hs4": te["hs4"], "share": te["share"], "rca": te["rca"],
                     "world_mkt_share": te["world_mkt_share"]}
                if lab:
                    d["label"] = lab
                if lab_en and lab_en != lab:
                    d["label_en"] = lab_en
                texp.append(d)
            trade["top_exports"] = texp
            trade["top_exports_src"] = {"source": "Harvard Growth Lab — Atlas of Economic "
                                        "Complexity (CC0)", "year": h["year"],
                                        "url": "https://atlas.hks.harvard.edu"}
            # treemap composition des exportations (top ~28 produits + total)
            if h.get("export_products"):
                trade["export_products"] = h["export_products"]
                trade["export_total_usd"] = h["export_total_usd"]
                trade["export_year"] = h["year"]
                if h.get("export_by_sector"):
                    trade["export_by_sector"] = h["export_by_sector"]
                if h.get("export_sector_n"):
                    trade["export_sector_n"] = h["export_sector_n"]
            # treemap composition des IMPORTATIONS (même source Harvard, col import_value)
            if h.get("import_products"):
                trade["import_products"] = h["import_products"]
                trade["import_total_usd"] = h["import_total_usd"]
                trade["import_year"] = h.get("import_year") or h["year"]
                if h.get("import_by_sector"):
                    trade["import_by_sector"] = h["import_by_sector"]
                if h.get("import_sector_n"):
                    trade["import_sector_n"] = h["import_sector_n"]
                trade["import_src"] = {"source": "Harvard Growth Lab — Atlas of Economic "
                                       "Complexity (CC0)", "year": h.get("import_year") or h["year"],
                                       "url": "https://atlas.hks.harvard.edu"}
            if h.get("partners_exp"):
                trade["partners_exp"] = h["partners_exp"]
                trade["partners_exp_src"] = {"source": "Harvard Atlas (bilatéral)",
                                             "year": h.get("partners_year"),
                                             "url": "https://atlas.hks.harvard.edu"}
            # E — opportunités de diversification (produits proches & complexes, rca<1)
            if h.get("diversif"):
                trade["diversif"] = h["diversif"]
                trade["diversif_src"] = {"source": "Harvard Growth Lab — Atlas of Economic "
                                         "Complexity (CC0)", "year": h["year"],
                                         "url": "https://atlas.hks.harvard.edu"}
        elif args.skip_harvard and not harv_ok:
            pass  # garde l'ancien top_exports/partners du deepcopy
        if a3 in factbook:
            txt, url, region = factbook[a3]
            trade["overview_text"] = txt
            fr = _overview_fr(txt)
            if fr:
                trade["overview_fr"] = fr
            trade["overview_src"] = {"source": "CIA World Factbook (domaine public)",
                                     "field": "Economy > Economic overview", "year": CUR_YEAR,
                                     "url": url}
        if trade:
            entry["trade"] = trade

        # COMPANIES
        try:
            comp = build_companies(a3, countries_meta[a3], iso2name, iso2qid, fx,
                                   glob_by_country, glob_names, old_e, only_set, args)
            if comp is not None:
                if not args.no_logos:
                    for it in comp["items"]:
                        logo = download_logo(a3, it)
                        it.pop("_p154", None)
                        it.pop("_site", None)
                        if logo:
                            it["logo"] = logo
                            logo_stats[logo["source"]] = logo_stats.get(logo["source"], 0) + 1
                        else:
                            it["logo"] = None
                else:
                    for it in comp["items"]:
                        it.pop("_p154", None)
                        it.pop("_site", None)
                entry["companies"] = comp
                cov_stats[comp["coverage"]] = cov_stats.get(comp["coverage"], 0) + 1
            elif "companies" in entry:
                # pas de données fraîches ET pas d'ancien fiable → null honnête
                if not old_e.get("companies"):
                    entry["companies"] = None
        except Exception as e:  # noqa: BLE001
            comp_fail += 1
            sys.stderr.write(f"[WARN] companies {a3} : {e!r} → ancien bloc conservé\n")

        # PYRAMIDE (garde par source : pyr KO → on garde l'ancienne du deepcopy)
        if pyr_ok:
            pyr = build_pyramid_entry(a3, pyr_raw, pyr_snap)
            if pyr:
                entry["pyramid"] = pyr

        # MIX ÉLECTRIQUE (garde amont : fetch KO/<30 pays → elecmix_ok False → ancien conservé)
        if elecmix_ok and a3 in elec_mix:
            entry["elec_mix"] = elec_mix[a3]

        # NOTATIONS 3 AGENCES (garde : frais si dispo, sinon l'ancien du deepcopy est conservé)
        if a3 in te_ratings:
            entry["rating3"] = te_ratings[a3]

        countries[a3] = entry
        print(f"  {a3}: hist={len(entry.get('hist', {}))} trade={'oui' if entry.get('trade') else 'non'} "
              f"companies={(entry.get('companies') or {}).get('coverage', '—') if entry.get('companies') else 'null'}")

    if comp_fail:
        failed.append("COMPANIES")

    # ── AGRÉGATS Monde/continents (garde par source) ──
    if agg_ok:
        for code in AGG_CODES:
            countries[code] = agg_entries[code]
            # treemap agrégat (Harvard) : frais si dispo, sinon préserve l'ancien
            # (les entrées agrégées sont reconstruites, pas deepcopy → injection explicite).
            if harv_ok and harvard_agg.get(code):
                tr = countries[code].setdefault("trade", {})
                tr["export_products"] = harvard_agg[code]["export_products"]
                tr["export_total_usd"] = harvard_agg[code]["export_total_usd"]
                tr["export_year"] = harvard_agg[code]["year"]
                if harvard_agg[code].get("export_by_sector"):
                    tr["export_by_sector"] = harvard_agg[code]["export_by_sector"]
                if harvard_agg[code].get("export_sector_n"):
                    tr["export_sector_n"] = harvard_agg[code]["export_sector_n"]
                if harvard_agg[code].get("import_products"):
                    tr["import_products"] = harvard_agg[code]["import_products"]
                    tr["import_total_usd"] = harvard_agg[code]["import_total_usd"]
                    tr["import_year"] = harvard_agg[code].get("import_year") or harvard_agg[code]["year"]
                    if harvard_agg[code].get("import_by_sector"):
                        tr["import_by_sector"] = harvard_agg[code]["import_by_sector"]
                    if harvard_agg[code].get("import_sector_n"):
                        tr["import_sector_n"] = harvard_agg[code]["import_sector_n"]
            else:
                old_tr = (old_countries.get(code, {}) or {}).get("trade", {})
                if old_tr.get("export_products"):
                    tr = countries[code].setdefault("trade", {})
                    tr["export_products"] = old_tr["export_products"]
                    tr["export_total_usd"] = old_tr.get("export_total_usd")
                    tr["export_year"] = old_tr.get("export_year")
    else:
        for code in AGG_CODES:
            if code in old_countries:
                countries[code] = copy.deepcopy(old_countries[code])
        sys.stderr.write("[GUARD] agrégats KO → anciens conservés\n")

    # ── OWID agrégats (World→WLD) : latest + hist des 7 séries v5 (co2pc…schooling) ──
    for code, series in (owid_agg or {}).items():
        e = countries.get(code)
        if not e:
            continue
        lt = e.setdefault("latest", {})
        hh = e.setdefault("hist", {})
        for key, d in series.items():
            if d.get("latest") and d["latest"][0] is not None:
                lt[key] = [d["latest"][0], d["latest"][1], 0]
            if d.get("hist"):
                hh[key] = d["hist"]

    # ── B — BENCHMARKS : hist WB/WGI des agrégats sur TOUTES les métriques (Monde + 7 régions) ──
    # Les lignes agrégées captées par fetch_wb_hist/fetch_wgi_hist (MÊMES codes que les pays →
    # unités identiques) sont fusionnées dans le hist de chaque agrégat. Étend le hist bien au-delà
    # du sous-ensemble AGG_WB : ajoute pop_gr, unemp, gini*, gdp_pc PPP, military, rd, youth_unemp,
    # wgi_*… quand la source publie l'agrégat (*absents chez WB, ex. gini/reserves = propre).
    def _last_packed(s):
        v, y0 = s.get("v") or [], s.get("s")
        for i in range(len(v) - 1, -1, -1):
            if v[i] is not None:
                return v[i], y0 + i
        return None, None
    for code in AGG_CODES:
        e = countries.get(code)
        if not e:
            continue
        hh = e.setdefault("hist", {})
        if wb_ok:
            for k, s in agg_hist_wb.get(code, {}).items():
                hh[k] = s                         # même code que les pays → benchmark cohérent
            # gdp_pc : les pays affichent le PPP → aligner aussi le latest de l'agrégat sur le PPP
            # (sinon carte « Monde » nominale ≠ graphe/benchmark PPP). PPP dispo pour les 8 agrégats.
            ppp = agg_hist_wb.get(code, {}).get("gdp_pc")
            if ppp:
                lv, ly = _last_packed(ppp)
                if lv is not None:
                    e.setdefault("latest", {})["gdp_pc"] = [lv, ly, 0]
        if wgi_ok:
            for k, s in agg_hist_wgi.get(code, {}).items():
                hh[k] = s
    print(f"[benchmarks] hist agrégats étendu — WLD : {len(countries.get('WLD', {}).get('hist', {}))} "
          f"métriques · régions ex. ECS : {len(countries.get('ECS', {}).get('hist', {}))}")

    # ── OWID mix électrique (World→WLD) : injecté dans l'agrégat (garde amont via elecmix_ok) ──
    if elecmix_ok and elec_mix.get("WLD") and countries.get("WLD") is not None:
        countries["WLD"]["elec_mix"] = elec_mix["WLD"]

    # ── meta.hs4_labels : union des codes HS4 de tous les export_products (pays+agrégats) ──
    # Priorité au libellé FR courant (HS4_FR), repli EN (harmonized-system.csv, HS 2022 4-digits).
    hs4_union = set()
    for e in countries.values():
        tr = e.get("trade", {}) or {}
        for pair in tr.get("export_products", []) or []:
            hs4_union.add(pair[0])
        for d in tr.get("diversif", []) or []:      # E — libeller aussi les produits d'opportunité
            hs4_union.add(d["hs4"])
    hs4_labels, n_lab_fr, n_lab_en = {}, 0, 0
    for code in hs4_union:
        fr = HS4_FR.get(code)
        if fr:
            hs4_labels[code] = fr
            n_lab_fr += 1
        elif hs4_en.get(code):
            hs4_labels[code] = hs4_en[code]
            n_lab_en += 1
    print(f"[hs4_labels] {len(hs4_labels)} libellés ({n_lab_fr} FR + {n_lab_en} EN) "
          f"sur {len(hs4_union)} codes de l'union")

    # ── SÉRIE DÉRIVÉE : productivité marginale de la dette ────────────────────
    # Doit tourner APRÈS la fusion des sources (elle croise IMF `debt`, WB
    # `credit_gdp` et WB `ngdp_lcu`) et AVANT l'écriture. Retire ngdp_lcu au
    # passage. Formule et pièges documentés dans atlas_mpd.py.
    inject_mpd(countries)

    # ── VOLET PRIX & INFLATION ────────────────────────────────────────────────
    # Même contrainte d'ordre que le MPD : APRÈS la fusion par source (les
    # dérivées croisent IMF `infl` et WB `fx_lcu`/`ppp_lcu`) et AVANT l'écriture.
    # Retire fx_lcu et ppp_lcu au passage : séries de travail, jamais affichées.
    inject_prix(countries, oecd_prix_hist, oecd_prix_last, oecd_prix_ok,
                forecast_from=forecast_from)

    payload = {
        "meta": {
            "updated_at": NOW.isoformat(timespec="seconds"),
            "updated_at_unix": int(time.time()),
            "sources_ok": ok, "sources_failed": failed,
            "n_countries": len([k for k in countries if k not in AGG_CODES]),
            "n_aggregates": len([k for k in countries if k in AGG_CODES]),
            "forecast_from": forecast_from,
            "aggregates": AGG_META,
            # B — région WB de chaque pays (EAS/ECS/LCN/MEA/NAC/SAS/SSF) → le front trouve le
            # continent d'un pays pour superposer la courbe de référence du continent.
            "region_of": {a3: iso2region[a3] for a3 in sorted(a3set) if a3 in iso2region},
            "slugs": te_slugs,
            "hs4_labels": hs4_labels,
            # Repère PISA « moyenne OCDE » (série par matière + composite) — sert de
            # courbe de référence sur les cartes PISA du dossier pays. Conservé du cache
            # précédent si la source PISA est tombée (garde par source).
            "pisa_oecd": (pisa_oecd if pisa_oecd
                          else ((old or {}).get("meta", {}).get("pisa_oecd") or {})),
        },
        "sources": {
            "WB": {"label": "Banque mondiale · WDI", "url": "https://data.worldbank.org"},
            "WGI": {"label": "Banque mondiale · WGI",
                    "url": "https://info.worldbank.org/governance/wgi/"},
            "IMF": {"label": "FMI · WEO (DataMapper)", "url": "https://www.imf.org/external/datamapper",
                    "forecast_from": forecast_from},
            "OWID": {"label": "Our World in Data (FH / V-Dem / CPI)",
                     "url": "https://ourworldindata.org"},
            "PISA": dict({"label": "OCDE · PISA (via Our World in Data)",
                          "url": "https://ourworldindata.org/grapher/academic-performance",
                          "note": "enquête triennale sur les élèves de 15 ans ; pays "
                                  "participants uniquement (la Chine n'est pas couverte au "
                                  "niveau national)"},
                         **({"latest_wave": pisa_wave} if pisa_wave else {}),
                         **({"pending_wave": pisa_pending} if pisa_pending else {})),
            "OEC": {"label": "Observatory of Economic Complexity — ECI",
                    "url": "https://oec.world"},
            "HARVARD": {"label": "Harvard Growth Lab — Atlas of Economic Complexity (CC0)",
                        "url": "https://atlas.hks.harvard.edu"},
            "FACTBOOK": {"label": "CIA World Factbook (domaine public)",
                         "url": "https://www.cia.gov/the-world-factbook/"},
            "COMPANIES": {"label": "companiesmarketcap / Wikidata (QLever) / Wikipedia",
                          "note": "coverage full|partial|thin — jamais d'invention"},
        },
        "countries": countries,
    }
    return payload, ok, failed, logo_stats, cov_stats


# ══ AUDIT ═════════════════════════════════════════════════════════════════════

def audit(payload):
    c = payload["countries"]
    print("\n" + "─" * 96)
    print("AUDIT — dossiers détaillés")
    print("─" * 96)
    for a3 in [x for x in ["FRA", "USA", "NGA", "BRA", "VNM", "DEU", "CHN", "IND", "JPN", "SGP"]
               if x in c]:
        e = c[a3]
        tr = e.get("trade", {})
        sec = tr.get("sectors_va", {})
        eci = tr.get("eci")
        te = (tr.get("top_exports") or [{}])[0]
        pe = (tr.get("partners_exp") or [{}])[0]
        comp = e.get("companies")
        print(f"\n### {a3}")
        print(f"  hist métriques ({len(e.get('hist', {}))}): {sorted(e.get('hist', {}))}")
        if sec:
            print(f"  secteurs VA: agr={sec.get('agr')} ind={sec.get('ind')} "
                  f"manf={sec.get('manf')} srv={sec.get('srv')} ({sec.get('year')})")
        if eci:
            print(f"  ECI: {eci['value']} rang {eci['rank']}/{eci['n_ranked']} ({eci['year']})")
        else:
            print(f"  ECI: non classé")
        if te:
            print(f"  top export #1: {te.get('hs4')} {te.get('label') or te.get('label_en')} "
                  f"share={te.get('share')} rca={te.get('rca')} wms={te.get('world_mkt_share')}")
        if pe:
            print(f"  partenaire #1: {pe.get('iso3')} {pe.get('share')}")
        ov = tr.get("overview_text")
        if ov:
            print(f"  overview: {ov[:90]}…")
        if comp:
            it0 = comp["items"][0] if comp["items"] else {}
            nlogo = sum(1 for it in comp["items"] if it.get("logo"))
            print(f"  entreprises: {len(comp['items'])} · {comp['ranking_metric']} · "
                  f"coverage={comp['coverage']} · src={comp['source_name']}")
            print(f"    #1: {it0.get('name')} {it0.get('ticker') or ''} "
                  f"${(it0.get('size_value_usd') or 0)/1e9:.1f}B ({it0.get('size_date')}) "
                  f"logos={nlogo}/{len(comp['items'])}")
        else:
            print(f"  entreprises: non disponible")

    # ── PYRAMIDE + AGRÉGATS (SPEC v3) ──
    print("\nPYRAMIDE (recoupements SPEC : FRA 0-4 H≈5.26/F≈4.73, JPN 80+ F≈13.02, "
          "NER 0-4 H≈17.56, WLD 0-4 H≈7.97)")
    for a3, band, sex in [("FRA", "0004", "m"), ("FRA", "0004", "f"),
                          ("JPN", "80UP", "f"), ("NER", "0004", "m"), ("WLD", "0004", "m")]:
        e = c.get(a3, {})
        pyr = e.get("pyramid")
        if not pyr:
            print(f"  {a3} {band} {sex}: PAS DE PYRAMIDE")
            continue
        ly = str(pyr["latest"])
        idx = PYRAMID_BANDS.index(band)
        arr = pyr["years"].get(ly, {}).get(sex, [])
        val = arr[idx] if len(arr) > idx else None
        sm = sum(pyr["years"][ly]["m"])
        sf = sum(pyr["years"][ly]["f"])
        print(f"  {a3} {band} {sex} {ly} = {val}  (ΣM={sm:.2f} ΣF={sf:.2f}, "
              f"années={sorted(pyr['years'], reverse=True)})")
    n_pyr = sum(1 for e in c.values() if e.get("pyramid"))
    print(f"  couverture pyramide : {n_pyr} entités")

    print("\nAGRÉGATS (recoupements SPEC : WLD PIB≈118.35 T$, pop≈8.215 Md, vie 73.48, "
          "croiss. WB 2.92 / FMI 2026 3.1, infl FMI 4.4, dette FMI 95.3)")
    for code in AGG_CODES:
        e = c.get(code, {})
        lt = e.get("latest", {})
        has_pyr = "oui" if e.get("pyramid") else "non"
        gdp = lt.get("gdp", [None])[0]
        print(f"  {code} {e.get('name', '?')}: gdp={gdp} pop={lt.get('pop', [None])[0]} "
              f"life={lt.get('life')} growth={lt.get('growth')} infl={lt.get('infl')} "
              f"hist={len(e.get('hist', {}))} pyramid={has_pyr}")
    wld = c.get("WLD", {}).get("latest", {})
    print(f"  WLD détail : growth_wb={wld.get('growth_wb')} debt={wld.get('debt')} "
          f"gdp_pc={wld.get('gdp_pc')} urban={wld.get('urban')}")

    # ── TREEMAP exportations (SPEC v4) ──
    labels = payload.get("meta", {}).get("hs4_labels", {})
    print("\nTREEMAP export_products (recoupements SPEC : FRA 8802 Aéronefs / 3004 Médicaments "
          "/ 8703 Autos ; WLD électronique 8517/8542, pétrole 2709, autos 8703)")
    for code in ["FRA", "WLD", "USA", "DEU", "ECS", "EAS", "SSF"]:
        e = c.get(code, {})
        tr = e.get("trade", {})
        ep = tr.get("export_products") or []
        if not ep:
            print(f"  {code}: PAS DE export_products")
            continue
        tot = tr.get("export_total_usd") or 1
        top5 = ", ".join(f"{hs4}·{(labels.get(hs4) or '?')[:16]} {v / tot * 100:.1f}%"
                         for hs4, v in ep[:5])
        print(f"  {code} ({tr.get('export_year')}): {len(ep)} prod · "
              f"total ${tot / 1e9:.0f}B · top: {top5}")
    n_ep = sum(1 for e in c.values() if (e.get("trade") or {}).get("export_products"))
    print(f"  couverture export_products : {n_ep} entités · hs4_labels : {len(labels)}")

    # ── v5 : Démographie/vieillissement · Énergie & climat · Développement · Finance ──
    def _last_from_hist(h):
        if not h or not h.get("v"):
            return None, None
        s, v = h["s"], h["v"]
        for i in range(len(v) - 1, -1, -1):
            if v[i] is not None:
                return v[i], s + i
        return None, None

    def _val_yr(e, key):
        lt = e.get("latest", {}) or {}
        if isinstance(lt.get(key), list) and lt[key] and lt[key][0] is not None:
            return lt[key][0], (lt[key][1] if len(lt[key]) > 1 else None)
        return _last_from_hist((e.get("hist", {}) or {}).get(key))

    print("\nv5 — recoupements SPEC (FRA : IDH~0.92 · CO₂/hab~3.97 t · fécondité 1.61 · "
          "dép. vieillesse 36.8 · crédit privé/PIB 107.6 · mortalité inf. 3.4)")
    for code in ["FRA", "USA", "NGA", "WLD"]:
        e = c.get(code, {})
        if not e:
            print(f"  {code}: absent")
            continue
        hdi_v, hdi_y = _val_yr(e, "hdi")
        co2_v, co2_y = _val_yr(e, "co2pc")
        tfr_v, tfr_y = _val_yr(e, "tfr")
        old_v, old_y = _val_yr(e, "dep_old")
        cr_v, cr_y = _val_yr(e, "credit_gdp")
        mk_v, mk_y = _val_yr(e, "mktcap_gdp")
        imr_v, imr_y = _val_yr(e, "imr")
        print(f"  {code}: IDH={hdi_v}({hdi_y}) CO₂/hab={co2_v}t({co2_y}) féc={tfr_v}({tfr_y}) "
              f"dép_old={old_v}%({old_y}) mortalité={imr_v}‰({imr_y})")
        print(f"       crédit privé/PIB={cr_v}%({cr_y}) · mktcap/PIB={mk_v}%({mk_y} — millésime)")
        rt = e.get("rating3")
        if rt:
            print(f"       rating3: S&P={rt.get('sp')} Moody's={rt.get('moody')} "
                  f"Fitch={rt.get('fitch')} DBRS={rt.get('dbrs')} · {rt.get('outlook')} "
                  f"({rt.get('as_of')})")
        else:
            print("       rating3: —")

    def _has(key):
        return sum(1 for e in c.values()
                   if (e.get("hist", {}) or {}).get(key) or (e.get("latest", {}) or {}).get(key))
    n_rt = sum(1 for e in c.values() if e.get("rating3"))
    print(f"  couverture v5 : tfr={_has('tfr')} dep_old={_has('dep_old')} imr={_has('imr')} "
          f"health_gdp={_has('health_gdp')} internet={_has('internet')} · "
          f"co2pc={_has('co2pc')} energypc={_has('energypc')} renew_elec={_has('renew_elec')} "
          f"hdi={_has('hdi')} medage={_has('medage')} schooling={_has('schooling')} · "
          f"mktcap_gdp={_has('mktcap_gdp')} credit_gdp={_has('credit_gdp')} · rating3={n_rt}")

    # ── E — PROFONDEUR : dépenses militaires · R&D · chômage jeunes · dette ext. · top 10 % ──
    print("\nE — profondeur (recoupements SPEC : FRA militaire 2,05 %PIB · R&D 2,18 %PIB · "
          "chômage jeunes 18,9 % · top 10 % revenu 34,4 %)")
    for code in ["FRA", "USA", "NGA", "WLD", "ECS"]:
        e = c.get(code, {})
        if not e:
            print(f"  {code}: absent")
            continue
        mil_v, mil_y = _val_yr(e, "military")
        rd_v, rd_y = _val_yr(e, "rd")
        yu_v, yu_y = _val_yr(e, "youth_unemp")
        ed_v, ed_y = _val_yr(e, "extdebt")
        t10_v, t10_y = _val_yr(e, "top10")
        print(f"  {code}: militaire={mil_v}%({mil_y}) R&D={rd_v}%({rd_y}) "
              f"chôm.jeunes={yu_v}%({yu_y}) dette_ext={ed_v}%RNB({ed_y}) top10={t10_v}%({t10_y})")
    print(f"  couverture E : military={_has('military')} rd={_has('rd')} "
          f"youth_unemp={_has('youth_unemp')} extdebt={_has('extdebt')} top10={_has('top10')}")

    # ── B — BENCHMARKS : les agrégats (Monde + régions) ont-ils un hist par métrique ? ──
    print("\nB — benchmarks (SPEC : WLD doit avoir un hist pour co2pc/hdi + les métriques WB à "
          "agrégat ; gini/reserves/wgi/extdebt = sans agrégat WB → dégradation propre)")
    bench_keys = ["gdp", "gdp_pc", "pop", "pop_gr", "life", "urban", "unemp", "gini",
                  "tfr", "dep_old", "imr", "health_gdp", "internet", "mktcap_gdp", "credit_gdp",
                  "military", "rd", "youth_unemp", "extdebt", "co2pc", "hdi", "top10",
                  "wgi_rl", "exports_gdp", "trade_gdp", "growth", "infl"]
    for code in ["WLD", "ECS", "EAS"]:
        h = (c.get(code, {}) or {}).get("hist", {}) or {}
        present = [k for k in bench_keys if k in h]
        absent = [k for k in bench_keys if k not in h]
        print(f"  {code}: {len(h)} métriques en hist · benchmark OK {present}")
        print(f"       sans agrégat : {absent}")
    # gdp_pc PPP cohérent latest↔hist (SPEC : WLD ~23 379 $ PPP, pas le nominal ~13 k$)
    wld = c.get("WLD", {})
    lt_pc = (wld.get("latest", {}) or {}).get("gdp_pc")
    h_pc = (wld.get("hist", {}) or {}).get("gdp_pc")
    hv, hy = _last_from_hist(h_pc) if h_pc else (None, None)
    print(f"  WLD gdp_pc : latest={lt_pc} · hist_dernier={hv}({hy}) "
          f"→ {'COHÉRENT PPP' if (lt_pc and abs(lt_pc[0]-(hv or 0))<1) else 'À VÉRIFIER'}")

    # ── E — DIVERSIFICATION (Harvard) : opportunités FRA plausibles (complexes & proches) ──
    print("\nE — diversification (SPEC : FRA → produits complexes proches des savoir-faire, rca<1)")
    for code in ["FRA", "DEU", "VNM"]:
        dv = ((c.get(code, {}) or {}).get("trade", {}) or {}).get("diversif")
        if not dv:
            print(f"  {code}: PAS DE DIVERSIF")
            continue
        items = ", ".join(f"{d['hs4']}·{(labels.get(d['hs4']) or '?')[:20]} "
                          f"(pci={d['pci']},dist={d['distance']})" for d in dv[:4])
        print(f"  {code} ({len(dv)} opp.) : {items}")
    n_div = sum(1 for e in c.values() if (e.get("trade") or {}).get("diversif"))
    n_reg = len(payload.get("meta", {}).get("region_of", {}))
    print(f"  couverture diversif : {n_div} pays · meta.region_of : {n_reg} pays")

    # ── MIX ÉLECTRIQUE (OWID share-elec-by-source) ──
    print("\nMIX ÉLECTRIQUE (recoupements SPEC : FRA nucléaire ~65 % + solaire/éolien en hausse · "
          "WLD charbon ~35 % décroissant · POL/IND charbon dominant)")
    _MIX_KEYS = ["coal", "oil", "gas", "nuclear", "hydro", "bio", "wind", "solar", "other"]
    for code in ["FRA", "WLD", "POL", "IND"]:
        mx = c.get(code, {}).get("elec_mix")
        if not mx:
            print(f"  {code}: PAS DE MIX")
            continue
        s = mx["s"]
        n = len(mx.get("coal") or [])
        yr = s + n - 1
        last = {k: (mx[k][-1] if mx.get(k) and mx[k][-1] is not None else None) for k in _MIX_KEYS}
        tot = sum(v for v in last.values() if v is not None)
        top = sorted(((k, v) for k, v in last.items() if v is not None),
                     key=lambda kv: -kv[1])[:4]
        coal0 = (mx.get("coal") or [None])[0]
        print(f"  {code} ({s}→{yr}, {n} ans) : "
              + " ".join(f"{k}={v}%" for k, v in top)
              + f" · Σ={tot:.0f}% · charbon {coal0}→{last['coal']}% · solaire+éolien "
              + f"{(last.get('solar') or 0) + (last.get('wind') or 0):.1f}%")
    n_mix = sum(1 for e in c.values() if e.get("elec_mix"))
    print(f"  couverture elec_mix : {n_mix} entités")
    print("\n" + "─" * 96)


# ══ ÉCRITURE ══════════════════════════════════════════════════════════════════

def write_outputs(payload):
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUT_JSON.write_text(blob, encoding="utf-8")
    OUT_JS.write_text(
        f"/* atlas_detail_cache.js — généré {payload['meta']['updated_at']} — "
        f"OK:{','.join(payload['meta']['sources_ok'])} */\n"
        f"window.__ATLAS_DETAIL__ = {blob};\n", encoding="utf-8")
    print(f"[atlas_detail] écrit {OUT_JSON} ({len(blob)/1024/1024:.2f} Mo) + {OUT_JS}")
    return len(blob)


def load_old_cache():
    if OUT_JSON.exists():
        try:
            return json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except ValueError:
            sys.stderr.write("[WARN] cache précédent illisible\n")
    return None


def main():
    ap = argparse.ArgumentParser(description="Fetcher Atlas Économique — DÉTAIL (217 pays)")
    ap.add_argument("--dry-run", action="store_true", help="fetch complet, n'écrit pas le cache")
    ap.add_argument("--no-logos", action="store_true", help="ne télécharge pas les logos")
    ap.add_argument("--only", default=None, help="sous-ensemble ISO3 (ex: FRA,USA,NGA)")
    ap.add_argument("--skip-harvard", action="store_true",
                    help="ne relit pas les CSV Harvard (garde l'ancien top_exports)")
    ap.add_argument("--skip-ratings", action="store_true",
                    help="ne scrape pas les notations TradingEconomics (rapide)")
    ap.add_argument("--reprice", action="store_true",
                    help="re-date les mcap via Yahoo (opt-in, lent)")
    args = ap.parse_args()

    t0 = time.time()
    meta = load_meta()
    print(f"[atlas_detail] meta : {len(meta['countries'])} pays ({META_PATH})")
    print(f"[atlas_detail] logos → {LOGO_DIR}")
    print(f"[atlas_detail] Harvard → {HARVARD_DIR or 'ABSENT'}\n")
    old = load_old_cache()

    payload, ok, failed, logo_stats, cov_stats = assemble(meta, old, args)
    audit(payload)

    n_hist = sum(1 for e in payload["countries"].values() if e.get("hist"))
    n_eci = sum(1 for e in payload["countries"].values()
                if (e.get("trade") or {}).get("eci"))
    n_texp = sum(1 for e in payload["countries"].values()
                 if (e.get("trade") or {}).get("top_exports"))
    n_comp = sum(1 for e in payload["countries"].values() if e.get("companies"))
    n_pyr = sum(1 for e in payload["countries"].values() if e.get("pyramid"))
    n_mix = sum(1 for e in payload["countries"].values() if e.get("elec_mix"))
    n_agg = payload["meta"].get("n_aggregates", 0)
    n_slugs = len(payload["meta"].get("slugs", {}))
    print(f"\n[couverture] hist={n_hist} · trade.eci={n_eci} · trade.top_exports={n_texp} "
          f"· companies={n_comp} {dict(cov_stats)}")
    print(f"[couverture v3] pyramid={n_pyr} · elec_mix={n_mix} · aggregates={n_agg} · slugs={n_slugs}")
    print(f"[logos] {sum(logo_stats.values())} téléchargés/réutilisés {dict(logo_stats)}")
    print(f"[sources] OK={ok}\n[sources] FAILED={failed}")

    # garde globale : si tout l'historique + commerce tombent et cache existe → pas d'écrasement
    major = {"WB", "IMF", "OEC", "HARVARD"}
    if OUT_JSON.exists() and not args.only and len(major & set(ok)) < 2:
        sys.stderr.write(f"[GUARD] trop peu de sources majeures OK ({ok}) — "
                         f"cache précédent conservé\n")
        sys.exit(1)

    if args.dry_run:
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        print(f"\n[atlas_detail] DRY-RUN : rien d'écrit ({len(blob)/1024/1024:.2f} Mo, "
              f"{time.time()-t0:.0f}s)")
        return

    size = write_outputs(payload)
    if size > 3.2e6:
        sys.stderr.write(f"[WARN] sortie {size/1e6:.2f} Mo > 3.2 Mo cible — réduire les "
                         f"années-instantanés de la pyramide (PYRAMID_SNAP_MIN) ou resserrer "
                         f"les arrondis v5\n")
    print(f"[atlas_detail] OK · {len(ok)} sources OK, {len(failed)} KO · "
          f"{payload['meta']['n_countries']} pays · {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
