#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache « Atlas Économique » — profils macro de 217 pays (Banque mondiale).

Page consommatrice : Atlas_Economique.html (variable window.__ATLAS_ECON__).
Sorties : ~/Library/Caches/site_crypto_finance/atlas_econ_cache.json
          ~/Library/Caches/site_crypto_finance/atlas_econ_cache.js  (1 ligne, commentaire daté)
Label launchd prévu : scf.atlaseco (StartInterval 21600, logs /tmp/atlaseco.*.log)

SOURCES (toutes testées le 2026-07-04, aucune clé API) :
  WB    Banque mondiale WDI  https://api.worldbank.org/v2/country/all/indicator/{code}
        ?format=json&mrnev=1&per_page=400 — 10 indicateurs (PIB, PIB/hab PPA, réserves,
        réserves en mois d'imports, population, croissance pop, espérance de vie, Gini,
        urbanisation, chômage-fallback) + série longue PIB date=1960:2026 (paginée).
        Chaque réponse porte `lastupdated` → meta.sources.WB.
  IMF   FMI WEO via DataMapper https://www.imf.org/external/datamapper/api/v1/{code}
        6 indicateurs : NGDP_RPCH, PCPIPCH, LUR, BCA_NGDPD, GGXWDG_NGDP, GGXCNL_NGDP.
        ⚠ le filtre pays dans l'URL est IGNORÉ (1 appel = 229 économies) ; exige un
        User-Agent navigateur (urllib → 403, curl_cffi impersonate chrome120 OK).
        Codes spéciaux : UVK→XKX, WBG→PSE ; agrégats (AFQ, EUQ…) filtrés par le meta.
        Historique 1980→2031 (prévisions incluses) ; forecast_from = année courante.
  WGI   Gouvernance Banque mondiale — codes GOV_WGI_{RL,GE,PV,CC}.EST, ⚠ &source=3
        obligatoire (sans lui : « indicator not found »).
  FH    Freedom House via OWID https://ourworldindata.org/grapher/freedom-score-fh.csv
        ?useColumnShortNames=true (colonne total_score ; le xlsx officiel FH exige un mail).
  VDEM  V-Dem via OWID liberal-democracy-index.csv (l'EIU est 403 « non-redistributable »).
        Code spécial OWID_KOS→XKX.
  PISA  OCDE via miroir OWID grapher MULTI-DIMENSIONS academic-performance.csv
        ?subject={mathematics|reading|science}&sex=both — 1 appel par matière (⚠ « math »
        renvoie HTTP 500, il faut « mathematics » ; pas de grapher dédié aux sciences ;
        la BM LO.PISA.* s'arrête à 2018). Dernière vague seulement (l'historique est dans
        atlas_detail_cache) ; ~86 pays participants, Chine non couverte.
  CPI   Transparency International — ⚠ nom de fichier variable selon l'année :
        essaie CPI{Y}_Results.xlsx puis CPI{Y}-Results-and-trends.xlsx pour Y et Y-1.
        ⚠ fichier en OOXML STRICT (namespace purl.oclc.org) → openpyxl NE LE LIT PAS,
        parser zipfile+ElementTree namespace-agnostique maison.
  BIS   Taux directeurs https://stats.bis.org/api/v1/data/WS_CBPOL/M./all?format=csv
        (⚠ dataflow WS_CBPOL, pas WS_CBPOL_D ; CSV/XML seulement ; ~38 banques centrales ;
        zone euro = REF_AREA « XM » → recopié sur les 21 pays euro:true du meta).
  FITCH ratingshistory.info — dernier fichier « YYYYMMDD Fitch Ratings Sovereign.csv »
        listé sur la home ; note émetteur = dernière action « Long Term Issuer Default
        Rating » par obligor (action WO = retirée → pas de note). Source FRAGILE :
        ⚠ flux réglementaire NRSRO publié avec ~12 mois de décalage (vérifié : le fichier
        du 2026-05-01 s'arrête aux actions d'avril 2025) → la date de l'action est
        TOUJOURS stockée et affichée ; note dans meta.sources.FITCH.
  YF    Yahoo Finance v8 chart — ⚠ curl_cffi impersonate="chrome120" OBLIGATOIRE
        (requests/urllib = 429). Indices : liste idx_t de atlas_countries_meta.js
        (source de vérité unique) range=2y&interval=1d → last/d1/ytd/y1/spark 52 hebdos.
        Devises : 1 appel par devise DISTINCTE (149) ; EUR/GBP/AUD/NZD → {CCY}USD=X
        mode usd_per, autres → USD{CCY}=X mode per_usd, USD → pas de fx.
        sleep 0.4 s entre appels, backoff 1.6^n, 4 tentatives.

GARDE-FOUS :
  - garde globale anti-écrasement : si < 5 sources OK et cache existant → exit 1 sans écrire ;
  - garde par source : source en échec → bloc recopié du cache précédent + sources_failed ;
  - garde par ticker/devise Yahoo : échec individuel → reprise de l'ancien mkt du pays ;
  - audit interne avant écriture (asserts d'ordre de grandeur + tableau de contrôle
    FRA/USA/JPN/NGA/ARG sur stdout) — échec d'assert → exit 1, cache précédent conservé.

CLI : --dry-run (fetch complet mais n'écrit rien) ;
      --markets-only (ne rafraîchit que YF + BIS, recopie le reste du cache précédent).

Interpréteur : /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
(curl_cffi 0.13.0 présent ; /opt/anaconda3/bin/python3 n'a PAS curl_cffi).
Meta pays : assets/js/atlas/atlas_countries_meta.js (override env ATLAS_META_PATH).
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ── timeout global (sécurité launchd) ─────────────────────────────────────────
import signal as _signal


def _timeout_handler(signum, frame):
    sys.stderr.write("[fatal] global timeout (25 min) — abort\n")
    sys.exit(2)


try:
    _signal.signal(_signal.SIGALRM, _timeout_handler)
    _signal.alarm(25 * 60)
except Exception:
    pass

# ── curl_cffi obligatoire (Yahoo/IMF bloquent urllib) ─────────────────────────
try:
    from curl_cffi import requests as cr
except ImportError:
    sys.stderr.write(
        "[fatal] curl_cffi introuvable — utiliser "
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3\n"
    )
    sys.exit(2)

# ── chemins ────────────────────────────────────────────────────────────────────
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "atlas_econ_cache.json"
OUT_JS = CACHE_DIR / "atlas_econ_cache.js"
# Meta pays : env > à côté du script > repo Desktop (le script tourne depuis
# Application Support via launchd — piège dual-dirs — mais la source de vérité
# git reste le Desktop).
_META_CANDIDATES = [
    os.environ.get("ATLAS_META_PATH"),
    str(Path(__file__).resolve().parent / "assets" / "js" / "atlas" / "atlas_countries_meta.js"),
    str(Path.home() / "Desktop" / "Site_Crypto_Finance" / "assets" / "js" / "atlas" / "atlas_countries_meta.js"),
]
META_PATH = next((p for p in _META_CANDIDATES if p and Path(p).exists()), _META_CANDIDATES[1])

NOW = datetime.now(timezone.utc)
CUR_YEAR = NOW.year          # année WEO courante = première année de prévision
FORECAST_FROM = CUR_YEAR

# ── HTTP ───────────────────────────────────────────────────────────────────────

def http_get(url, timeout=60, retries=4, binary=False, sleep_base=1.6):
    """GET via curl_cffi impersonate chrome120, backoff 1.6^n. None si échec.

    ⚠ Piège VÉRIFIÉ : l'API Banque mondiale renvoie parfois HTTP 400 avec un corps
    JSON parfaitement valide (ex. FI.RES.TOTL.MO, SP.URB.TOTL.IN.ZS avec mrnev=1).
    → un corps non-binaire qui parse en JSON est accepté malgré le statut ;
    les vraies erreurs JSON ([{"message":…}], {"chart":{"error":…}}) sont
    rejetées en aval par les contrôles de structure de chaque fetch_*."""
    last = None
    for attempt in range(retries):
        try:
            r = cr.get(url, impersonate="chrome120", timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
            if not binary and r.text and r.text.lstrip()[:1] in ("{", "["):
                try:
                    json.loads(r.text)
                    sys.stderr.write(f"[WARN] {last} mais corps JSON valide, accepté : "
                                     f"{url[:110]}\n")
                    return r.text
                except ValueError:
                    pass
            if r.status_code in (404, 403):
                break  # inutile d'insister
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(sleep_base ** (attempt + 1))
    sys.stderr.write(f"[WARN] échec GET {url[:120]} : {last}\n")
    return None


def http_get_json(url, timeout=60, retries=4):
    t = http_get(url, timeout=timeout, retries=retries)
    if t is None:
        return None
    try:
        return json.loads(t.lstrip("﻿"))  # l'API WB préfixe parfois un BOM
    except ValueError as e:
        sys.stderr.write(f"[WARN] JSON invalide {url[:120]} : {e}\n")
        return None


# ── arrondis (JSON < ~900 Ko) ──────────────────────────────────────────────────

def sig4(x):
    """4 chiffres significatifs ; int si la partie décimale est nulle."""
    if x is None:
        return None
    x = float(x)
    if x == 0:
        return 0
    r = round(x, -int(math.floor(math.log10(abs(x)))) + 3)
    return int(r) if abs(r) >= 1000 or r == int(r) else r


def r1(x):
    """1 décimale ; int au-delà de 1000 (hyperinflations…)."""
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


# ── meta pays (source de vérité : chantier GEO) ────────────────────────────────

def load_meta():
    src = Path(META_PATH).read_text(encoding="utf-8")
    m = re.search(r"window\.__ATLAS_META__\s*=\s*", src)
    if not m:
        sys.stderr.write(f"[fatal] window.__ATLAS_META__ introuvable dans {META_PATH}\n")
        sys.exit(2)
    payload = src[m.end():].strip().rstrip(";")
    return json.loads(payload)


# ══ SOURCE WB — Banque mondiale WDI ═══════════════════════════════════════════

WB_LATEST = {
    "gdp": ("NY.GDP.MKTP.CD", sig4),
    "gdp_real": ("NY.GDP.MKTP.KD", sig4),   # PIB réel, prix constants 2015 US$ (niveau, pas croissance)
    "gdp_ppp": ("NY.GDP.MKTP.PP.CD", sig4),  # PIB total en PPA (int. $ courants)
    "gdp_pc": ("NY.GDP.PCAP.PP.CD", sig4),
    "reserves": ("FI.RES.TOTL.CD", sig4),
    "reserves_mo": ("FI.RES.TOTL.MO", r1),
    "pop": ("SP.POP.TOTL", sig4),
    "pop_gr": ("SP.POP.GROW", r1),
    "life": ("SP.DYN.LE00.IN", r1),
    "gini": ("SI.POV.GINI", r1),
    "urban": ("SP.URB.TOTL.IN.ZS", r1),
    "unemp_wb": ("SL.UEM.TOTL.ZS", r1),
}


def _wb_mrnev(code):
    """Appel mrnev=1 avec repli per_page=300 paginé.

    ⚠ Piège VÉRIFIÉ (2026-07-04) : pour certains indicateurs (SP.URB.TOTL.IN.ZS),
    per_page=400 déclenche un 400 « Request Error » XML systématique côté WB,
    alors que per_page=300 répond 200 avec les mêmes données."""
    d = http_get_json(f"https://api.worldbank.org/v2/country/all/indicator/{code}"
                      f"?format=json&mrnev=1&per_page=400", retries=2)
    if d and len(d) >= 2 and d[1]:
        return d
    rows, meta0, page, pages = [], None, 1, 1
    while page <= pages:
        d = http_get_json(f"https://api.worldbank.org/v2/country/all/indicator/{code}"
                          f"?format=json&mrnev=1&per_page=300&page={page}")
        if not d or len(d) < 2 or not d[1]:
            return None
        meta0 = d[0]
        pages = int(d[0].get("pages", 1))
        rows += d[1]
        page += 1
    sys.stderr.write(f"[WARN] WB {code} : per_page=400 KO, repli per_page=300 OK\n")
    return [meta0, rows]


def fetch_wb_latest(a3set):
    """{a3: {champ: [val, année, 0]}} + lastupdated. None si échec total."""
    out, lastupdated = {}, None
    n_fail = 0
    for field, (code, rnd) in WB_LATEST.items():
        d = _wb_mrnev(code)
        if not d or len(d) < 2 or not d[1]:
            n_fail += 1
            sys.stderr.write(f"[WARN] WB {code} vide\n")
            continue
        lastupdated = d[0].get("lastupdated") or lastupdated
        for row in d[1]:
            a3 = row.get("countryiso3code")
            if a3 not in a3set or row.get("value") is None:
                continue
            out.setdefault(a3, {})[field] = [rnd(row["value"]), int(row["date"]), 0]
        print(f"[WB] {code} : {sum(1 for v in out.values() if field in v)} pays")
    if n_fail == len(WB_LATEST):
        return None, None
    return out, lastupdated


def fetch_wb_gdp_hist(a3set):
    """{a3: {'s': année, 'v': [...]}} — PIB nominal 1960→. None si échec."""
    raw = {}
    page, pages = 1, 1
    while page <= pages:
        url = ("https://api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.CD"
               f"?format=json&date=1960:{CUR_YEAR}&per_page=20000&page={page}")
        d = http_get_json(url, timeout=120)
        if not d or len(d) < 2 or not d[1]:
            return None
        pages = int(d[0].get("pages", 1))
        for row in d[1]:
            a3 = row.get("countryiso3code")
            if a3 not in a3set or row.get("value") is None:
                continue
            raw.setdefault(a3, {})[int(row["date"])] = sig4(row["value"])
        page += 1
    out = {}
    for a3, years in raw.items():
        series = _pack_series(years)
        if series:
            out[a3] = series
    print(f"[WB] hist PIB : {len(out)} pays")
    return out


def _pack_series(year_map, rnd=None):
    """dict {année: val} → {'s': première année, 'v': [...]} (trous = null, bords trimmés)."""
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


# ══ SOURCE IMF — FMI WEO DataMapper ═══════════════════════════════════════════

IMF_CODES = {
    "growth": "NGDP_RPCH",
    "infl": "PCPIPCH",
    "unemp": "LUR",
    "cab": "BCA_NGDPD",
    "debt": "GGXWDG_NGDP",
    "fiscal": "GGXCNL_NGDP",
}
IMF_A3_FIX = {"UVK": "XKX", "WBG": "PSE"}
IMF_HIST = ("growth", "infl", "debt")


def fetch_imf(a3set):
    """(latest {a3:{champ:[v,année,flag]}}, hist {a3:{champ:{'s','v'}}}). None si échec total."""
    latest, hist = {}, {}
    n_ok = 0
    for field, code in IMF_CODES.items():
        d = http_get_json(f"https://www.imf.org/external/datamapper/api/v1/{code}", timeout=90)
        vals = (d or {}).get("values", {}).get(code)
        if not vals:
            sys.stderr.write(f"[WARN] IMF {code} vide\n")
            continue
        n_ok += 1
        for k, years in vals.items():
            a3 = IMF_A3_FIX.get(k, k)
            if a3 not in a3set:
                continue
            ymap = {}
            for ys, v in years.items():
                try:
                    y, v = int(ys), float(v)
                except (TypeError, ValueError):
                    continue
                ymap[y] = v
            if not ymap:
                continue
            # latest : année WEO courante, sinon on recule (jamais au-delà)
            pick = None
            for y in range(CUR_YEAR, 1979, -1):
                if y in ymap:
                    pick = y
                    break
            if pick is not None:
                latest.setdefault(a3, {})[field] = [
                    r1(ymap[pick]), pick, 1 if pick >= FORECAST_FROM else 0]
            if field in IMF_HIST:
                series = _pack_series(ymap, rnd=r1)
                if series:
                    hist.setdefault(a3, {})[field] = series
        print(f"[IMF] {code} : {sum(1 for v in latest.values() if field in v)} pays")
    if n_ok == 0:
        return None, None
    return latest, hist


# ══ SOURCE WGI — gouvernance (source=3 OBLIGATOIRE) ═══════════════════════════

WGI_CODES = {"wgi_rl": "GOV_WGI_RL.EST", "wgi_ge": "GOV_WGI_GE.EST",
             "wgi_pv": "GOV_WGI_PV.EST", "wgi_cc": "GOV_WGI_CC.EST"}


def fetch_wgi(a3set):
    out, lastupdated = {}, None
    n_ok = 0
    for field, code in WGI_CODES.items():
        url = (f"https://api.worldbank.org/v2/country/all/indicator/{code}"
               f"?format=json&source=3&mrnev=1&per_page=400")
        d = http_get_json(url)
        if not d or len(d) < 2 or not d[1]:
            sys.stderr.write(f"[WARN] WGI {code} vide\n")
            continue
        n_ok += 1
        lastupdated = d[0].get("lastupdated") or lastupdated
        for row in d[1]:
            a3 = row.get("countryiso3code")
            if a3 not in a3set or row.get("value") is None:
                continue
            out.setdefault(a3, {})[field] = [r2(row["value"]), int(row["date"]), 0]
        print(f"[WGI] {code} : {sum(1 for v in out.values() if field in v)} pays")
    if n_ok == 0:
        return None, None
    return out, lastupdated


# ══ SOURCES FH / VDEM — OWID ══════════════════════════════════════════════════

OWID_A3_FIX = {"OWID_KOS": "XKX"}


def fetch_owid(slug, column, a3set, rnd):
    """{a3: [val, année, 0]} — dernière année par pays. None si échec."""
    t = http_get(f"https://ourworldindata.org/grapher/{slug}.csv?useColumnShortNames=true",
                 timeout=90)
    if t is None:
        return None
    rdr = csv.DictReader(io.StringIO(t))
    if column not in (rdr.fieldnames or []):
        sys.stderr.write(f"[WARN] OWID {slug} : colonne {column} absente ({rdr.fieldnames})\n")
        return None
    best = {}
    for row in rdr:
        a3 = OWID_A3_FIX.get(row.get("code", ""), row.get("code", ""))
        if a3 not in a3set:
            continue
        try:
            y, v = int(row["year"]), float(row[column])
        except (TypeError, ValueError, KeyError):
            continue
        if a3 not in best or y > best[a3][1]:
            best[a3] = [rnd(v), y, 0]
    print(f"[OWID] {slug} : {len(best)} pays")
    return best or None


# ══ SOURCE PISA — OCDE via miroir OWID ════════════════════════════════════════
# Scores des élèves de 15 ans, 3 matières + composite (moyenne des 3).
# ⚠ PIÈGES VÉRIFIÉS (2026-07-26) : grapher MULTI-DIMENSIONS → 1 appel par matière via
# ?subject=…&sex=both ; le slug maths est « mathematics » (« math » = HTTP 500) ; aucun
# grapher dédié pour les sciences ; la Banque mondiale (LO.PISA.*) s'arrête à 2018.
# Couverture ~86 pays (participants) — la Chine n'est PAS couverte (4 provinces seulement).
# L'historique complet vit dans atlas_detail_cache (fetch_atlas_detail.py) ; ici on ne
# garde que la dernière vague, pour la carte et la fiche.
PISA_URL = ("https://ourworldindata.org/grapher/academic-performance.csv"
            "?subject={subj}&sex=both&useColumnShortNames=true")
PISA_SUBJECTS = [("pisa_math", "mathematics", "pisa_math_all_average"),
                 ("pisa_read", "reading", "pisa_reading_all_average"),
                 ("pisa_sci", "science", "pisa_science_all_average")]
PISA_KEYS = ["pisa", "pisa_math", "pisa_read", "pisa_sci"]
# GARDE-FOU DE RECENCE. PISA est triennal : la vague suivante a été administrée en mai 2025
# et l'OCDE publie ses résultats le 8 septembre 2026. Passée cette date, si le miroir n'a
# TOUJOURS pas ingéré la vague, il faut le DIRE (sources.PISA.pending_wave → note affichée)
# au lieu de laisser croire que 2022 est la dernière mesure existante.
PISA_NEXT_WAVE = 2025
PISA_NEXT_RELEASE = "2026-09-08"


def pisa_vintage(pisa_map):
    """(dernière vague présente, note d'attente|None) — la note n'apparaît qu'une fois la
    date de publication OCDE passée sans que la vague soit arrivée dans les données."""
    years = [v[1] for rec in (pisa_map or {}).values() for v in rec.values() if v]
    newest = max(years) if years else None
    if newest is None or newest >= PISA_NEXT_WAVE or NOW.date().isoformat() < PISA_NEXT_RELEASE:
        return newest, None
    sys.stderr.write(f"[PISA-RECENCE] vague {PISA_NEXT_WAVE} publiée le {PISA_NEXT_RELEASE} "
                     f"mais la source en est encore à {newest} — note d'attente publiée\n")
    return newest, {"wave": PISA_NEXT_WAVE, "released": PISA_NEXT_RELEASE,
                    "cached_wave": newest}


def fetch_pisa(a3set):
    """{a3: {pisa|pisa_math|pisa_read|pisa_sci: [score, année, 0]}}. None si échec.

    Composite = moyenne des 3 matières de la DERNIÈRE vague où les 3 existent (jamais
    sur 2 matières : les vagues 2000/2003 sont incomplètes). Chaque matière porte sa
    propre dernière année."""
    raw = {}
    for key, subj, col in PISA_SUBJECTS:
        t = http_get(PISA_URL.format(subj=subj), timeout=90)
        if t is None:
            sys.stderr.write(f"[WARN] PISA {subj} : fetch KO\n")
            return None
        rdr = csv.DictReader(io.StringIO(t))
        flds = rdr.fieldnames or []
        use = col
        if use not in flds:
            cand = [f for f in flds if f.startswith("pisa")]
            if not cand:
                sys.stderr.write(f"[WARN] PISA {subj} : colonne valeur absente ({flds})\n")
                return None
            sys.stderr.write(f"[WARN] PISA {subj} : {col} absente → {cand[0]}\n")
            use = cand[0]
        d = {}
        for row in rdr:
            a3 = OWID_A3_FIX.get(row.get("code", ""), row.get("code", ""))
            if a3 not in a3set:
                continue
            v = row.get(use)
            if v in (None, ""):
                continue
            try:
                d.setdefault(a3, {})[int(row["year"])] = float(v)
            except (TypeError, ValueError, KeyError):
                continue
        if len(d) < 40:
            sys.stderr.write(f"[WARN] PISA {subj} : {len(d)} pays seulement — source KO\n")
            return None
        raw[key] = d
        print(f"[PISA] {subj} ({use}) : {len(d)} pays")
    out = {}
    for a3 in a3set:
        maps = [raw[k].get(a3) or {} for k in ("pisa_math", "pisa_read", "pisa_sci")]
        rec = {}
        for (key, _s, _c), ym in zip(PISA_SUBJECTS, maps):
            if ym:
                y = max(ym)
                rec[key] = [r1(ym[y]), y, 0]
        common = set(maps[0]) & set(maps[1]) & set(maps[2])
        if common:
            y = max(common)
            rec["pisa"] = [r1(sum(m[y] for m in maps) / 3.0), y, 0]
        if rec:
            out[a3] = rec
    print(f"[PISA] {len(out)} pays retenus (composite : "
          f"{sum(1 for r in out.values() if 'pisa' in r)})")
    return out or None


# ══ SOURCE CPI — Transparency International (OOXML strict) ════════════════════

def _xlsx_rows(content, sheet_name_prefix):
    """Parser xlsx namespace-agnostique (le fichier TI est en OOXML strict,
    namespace purl.oclc.org → openpyxl échoue dessus, vérifié)."""
    def ln(tag):
        return tag.rsplit("}", 1)[-1]

    z = zipfile.ZipFile(io.BytesIO(content))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    relmap = {r.get("Id"): r.get("Target") for r in rels}
    target = None
    for s in wb.iter():
        if ln(s.tag) == "sheet" and (s.get("name") or "").startswith(sheet_name_prefix):
            rid = [v for k, v in s.attrib.items() if ln(k) == "id"][0]
            target = relmap[rid]
            break
    if target is None:
        return None
    if not target.startswith("xl/"):
        target = "xl/" + target.lstrip("/")
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        ss = ["".join(t.text or "" for t in si.iter() if ln(t.tag) == "t")
              for si in root if ln(si.tag) == "si"]
    rows = []
    for row in ET.fromstring(z.read(target)).iter():
        if ln(row.tag) != "row":
            continue
        vals = {}
        for c in row:
            if ln(c.tag) != "c":
                continue
            ref, typ = c.get("r", ""), c.get("t")
            v = next((x for x in c if ln(x.tag) == "v"), None)
            mcol = re.match(r"[A-Z]+", ref)
            if mcol is None or v is None or v.text is None:
                continue
            vals[mcol.group(0)] = ss[int(v.text)] if typ == "s" else v.text
        if vals:
            rows.append(vals)
    return rows


def fetch_cpi_ti(a3set):
    """{a3: [score, année, 0]} + nom de fichier utilisé. (None, None) si échec.
    ⚠ nom de fichier TI variable → 4 candidats (année courante puis N-1)."""
    candidates = []
    for y in (CUR_YEAR, CUR_YEAR - 1):
        candidates += [(f"CPI{y}_Results.xlsx", y), (f"CPI{y}-Results-and-trends.xlsx", y)]
    for fname, year in candidates:
        content = http_get(f"https://images.transparencycdn.org/images/{fname}",
                           timeout=120, retries=2, binary=True)
        if content is None:
            continue
        try:
            rows = _xlsx_rows(content, f"CPI{year}")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[WARN] TI {fname} illisible : {e}\n")
            continue
        if not rows:
            continue
        # ligne d'en-tête = celle qui contient "ISO3"
        header = next((r for r in rows if "ISO3" in r.values()), None)
        if header is None:
            continue
        iso_col = next(k for k, v in header.items() if v == "ISO3")
        score_col = next((k for k, v in header.items()
                          if isinstance(v, str) and re.fullmatch(rf"CPI {year} score", v.strip())),
                         None)
        if score_col is None:
            continue
        out = {}
        for r in rows:
            a3 = r.get(iso_col)
            if a3 not in a3set or score_col not in r:
                continue
            try:
                out[a3] = [int(round(float(r[score_col]))), year, 0]
            except ValueError:
                continue
        if out:
            print(f"[CPI] {fname} : {len(out)} pays (année {year})")
            return out, fname
    return None, None


# ══ SOURCE BIS — taux directeurs (zone euro = XM) ═════════════════════════════

def fetch_bis(meta_countries):
    """{a3: (taux, 'YYYY-MM')} — dernier mois observé par banque centrale."""
    start = f"{NOW.year}-{NOW.month:02d}"
    y, mth = NOW.year, NOW.month - 3
    while mth <= 0:
        mth += 12
        y -= 1
    start = f"{y}-{mth:02d}"
    t = http_get(f"https://stats.bis.org/api/v1/data/WS_CBPOL/M./all?format=csv"
                 f"&startPeriod={start}", timeout=90)
    if t is None:
        return None
    best = {}  # REF_AREA(a2) -> (month, value)
    for row in csv.DictReader(io.StringIO(t)):
        area, period, val = row.get("REF_AREA"), row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if not area or not period or val in (None, ""):
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        if area not in best or period > best[area][0]:
            best[area] = (period, v)
    if not best:
        return None
    a2_to_a3 = {v["a2"]: k for k, v in meta_countries.items()}
    out = {}
    for area, (period, v) in best.items():
        if area == "XM":
            for a3, mm in meta_countries.items():
                if mm.get("euro"):
                    out[a3] = (r2(v), period)
        elif area in a2_to_a3:
            out[a2_to_a3[area]] = (r2(v), period)
    print(f"[BIS] taux directeurs : {len(best)} zones → {len(out)} pays (start {start})")
    return out


# ══ SOURCE FITCH — ratingshistory.info (FRAGILE, décalage ~12 mois) ═══════════

FITCH_NAME_TO_A3 = {
    "Andorra": "AND", "Angola": "AGO", "Argentina": "ARG", "Armenia": "ARM",
    "Aruba": "ABW", "Australia": "AUS", "Austria": "AUT", "Azerbaijan": "AZE",
    "Bahrain": "BHR", "Bangladesh": "BGD", "Barbados": "BRB", "Belarus": "BLR",
    "Belgium": "BEL", "Benin": "BEN", "Bermuda": "BMU", "Bolivia": "BOL",
    "Brazil": "BRA", "Bulgaria": "BGR", "Cabo Verde": "CPV", "Cameroon": "CMR",
    "Canada": "CAN", "Chad": "TCD", "Chile": "CHL", "China": "CHN",
    "Colombia": "COL", "Congo, Republic of": "COG", "Costa Rica": "CRI",
    "Cote d'Ivoire": "CIV", "Croatia": "HRV", "Cyprus": "CYP",
    "Czech Republic": "CZE", "Denmark": "DNK", "Dominican Republic": "DOM",
    "Ecuador": "ECU", "Egypt": "EGY", "El Salvador": "SLV", "Estonia": "EST",
    "Ethiopia": "ETH", "Finland": "FIN", "France": "FRA", "Gabon": "GAB",
    "Georgia": "GEO", "Germany": "DEU", "Ghana": "GHA", "Greece": "GRC",
    "Guatemala": "GTM", "Hashemite Kingdom of Jordan": "JOR",
    "Hong Kong, China": "HKG", "Hungary": "HUN", "Iceland": "ISL", "India": "IND",
    "Indonesia": "IDN", "Iraq": "IRQ", "Ireland": "IRL", "Israel": "ISR",
    "Italy": "ITA", "Jamaica": "JAM", "Japan": "JPN", "Kazakhstan": "KAZ",
    "Kenya": "KEN", "Korea": "KOR", "Kosovo": "XKX", "Kuwait": "KWT",
    "Kyrgyzstan": "KGZ", "Laos": "LAO", "Latvia": "LVA", "Lebanon": "LBN",
    "Lesotho": "LSO", "Lithuania": "LTU", "Luxembourg": "LUX",
    "Macao, China": "MAC", "Malaysia": "MYS", "Maldives": "MDV", "Malta": "MLT",
    "Mexico": "MEX", "Moldova": "MDA", "Mongolia": "MNG", "Morocco": "MAR",
    "Mozambique": "MOZ", "Namibia": "NAM", "Nepal": "NPL", "Netherlands": "NLD",
    "New Zealand": "NZL", "Nicaragua": "NIC", "Nigeria": "NGA",
    "North Macedonia, Republic of": "MKD", "Norway": "NOR", "Oman": "OMN",
    "Pakistan": "PAK", "Panama": "PAN", "Paraguay": "PRY", "Peru": "PER",
    "Philippines": "PHL", "Poland": "POL", "Portugal": "PRT", "Qatar": "QAT",
    "Romania": "ROU", "Russia": "RUS", "Rwanda": "RWA", "San Marino": "SMR",
    "Saudi Arabia": "SAU", "Serbia": "SRB", "Seychelles": "SYC",
    "Singapore": "SGP", "Slovakia": "SVK", "Slovenia": "SVN",
    "South Africa": "ZAF", "Spain": "ESP", "Sri Lanka": "LKA", "Suriname": "SUR",
    "Sweden": "SWE", "Switzerland": "CHE", "Tanzania": "TZA", "Thailand": "THA",
    "The Commonwealth of The Bahamas": "BHS", "Tunisia": "TUN", "Turkiye": "TUR",
    "Turkmenistan": "TKM", "Uganda": "UGA", "Ukraine": "UKR",
    "United Arab Emirates": "ARE", "United Kingdom": "GBR",
    "United States of America": "USA", "Uruguay": "URY",
    "Uzbekistan, Republic of": "UZB", "Venezuela": "VEN", "Vietnam": "VNM",
    "Zambia": "ZMB",
    # non mappés sciemment : supranationaux (BEI, BAD…), émirats infranationaux
    # (Abu Dhabi, Ras Al Khaimah), Taiwan (hors liste WB des 217).
}


def fetch_fitch(a3set):
    """{a3: {'fitch': note, 'date': 'YYYY-MM-DD'[, 'outlook']}} + nom fichier."""
    home = http_get("https://ratingshistory.info/", timeout=90)
    if home is None:
        return None, None
    files = sorted(set(re.findall(r"([0-9]{8} Fitch Ratings Sovereign\.csv)", home)))
    if not files:
        sys.stderr.write("[WARN] Fitch : aucun fichier Sovereign listé\n")
        return None, None
    fname = files[-1]
    t = http_get("https://ratingshistory.info/api/public/" + fname.replace(" ", "%20"),
                 timeout=180)
    if t is None:
        return None, None
    last_action = {}  # a3 -> row
    try:
        for row in csv.DictReader(io.StringIO(t)):
            if row.get("rating_type") != "Long Term Issuer Default Rating":
                continue
            name = (row.get("obligor_name") or "").strip().strip('"')
            a3 = FITCH_NAME_TO_A3.get(name)
            if a3 is None or a3 not in a3set:
                continue
            d = row.get("rating_action_date") or ""
            if a3 not in last_action or d > last_action[a3]["rating_action_date"]:
                last_action[a3] = row
    except csv.Error as e:
        sys.stderr.write(f"[WARN] Fitch CSV illisible : {e}\n")
        return None, None
    out = {}
    for a3, row in last_action.items():
        rating = (row.get("rating") or "").strip()
        if row.get("rating_action_class") == "WO" or rating in ("", "WD", "NR"):
            continue  # note retirée → pas de note (jamais d'invention)
        entry = {"fitch": rating, "date": row["rating_action_date"]}
        outlook = (row.get("rating_outlook") or "").strip()
        if outlook:
            entry["outlook"] = outlook
        out[a3] = entry
    print(f"[FITCH] {fname} : {len(out)} souverains notés")
    return (out or None), fname


# ══ SOURCE YF — Yahoo Finance (indices + devises) ═════════════════════════════

YF_SLEEP = 0.4
USD_PER = {"EUR", "GBP", "AUD", "NZD"}  # cotés en USD pour 1 unité locale


def _yf_chart(symbol, rng):
    """3 tentatives query1→query2→query1 : Yahoo renvoie parfois un 404 JSON
    transitoire pour un ticker valide (vérifié sur ^IPSA/^CASE30/WIG20.WA)."""
    enc = symbol.replace("^", "%5E").replace("=", "%3D")
    for attempt, host in enumerate(("query1", "query2", "query1")):
        d = http_get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                          f"{enc}?range={rng}&interval=1d", timeout=30, retries=2)
        time.sleep(YF_SLEEP)
        try:
            res = d["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
            if len(pairs) < 10:
                return None
            return res["meta"], pairs
        except (TypeError, KeyError, IndexError):
            if attempt < 2:
                time.sleep(2.0 + 3.0 * attempt)
    return None


def _pct(a, b):
    if not b:
        return None
    return r1((a / b - 1.0) * 100.0)


def fetch_yf_index(ticker, name):
    got = _yf_chart(ticker, "2y")
    if got is None:
        return None
    meta, pairs = got
    last = meta.get("regularMarketPrice") or pairs[-1][1]
    ts_last = meta.get("regularMarketTime") or pairs[-1][0]
    closes = [c for _, c in pairs]
    # d1 : si la dernière barre est la séance courante (regularMarketTime dans
    # les 24 h de son ouverture), la veille = closes[-2] ; sinon closes[-1].
    # (comparaison de PRIX float32/float64 = piège → d1 était toujours 0)
    if len(closes) >= 2 and 0 <= ts_last - pairs[-1][0] < 86400:
        prev = closes[-2]
    else:
        prev = closes[-1]
    d1 = _pct(last, prev)
    prev_year_closes = [c for t, c in pairs
                        if datetime.fromtimestamp(t, tz=timezone.utc).year < CUR_YEAR]
    ytd = _pct(last, prev_year_closes[-1]) if prev_year_closes else None
    cutoff = time.time() - 365 * 86400
    y1_closes = [c for t, c in pairs if t <= cutoff]
    y1 = _pct(last, y1_closes[-1]) if y1_closes else None
    weekly = {}
    for t, c in pairs:
        iso = datetime.fromtimestamp(t, tz=timezone.utc).isocalendar()
        weekly[(iso[0], iso[1])] = c  # dernière clôture de la semaine
    spark = [sig4(c) for _, c in sorted(weekly.items())][-52:]
    return {"t": ticker, "n": name, "last": r2(last),
            "d1": d1, "ytd": ytd, "y1": y1, "spark": spark, "ts": int(ts_last)}


def fetch_yf_fx(ccy):
    """(dict fx sans 'ccy', None) — mode usd_per ou per_usd selon la devise."""
    if ccy in USD_PER:
        ticker, mode = f"{ccy}USD=X", "usd_per"
    else:
        ticker, mode = f"USD{ccy}=X", "per_usd"
    got = _yf_chart(ticker, "1y")
    if got is None:
        return None
    meta, pairs = got
    last = meta.get("regularMarketPrice") or pairs[-1][1]
    y1 = _pct(last, pairs[0][1])
    return {"t": ticker, "mode": mode, "last": sig4(last), "y1": y1}


def fetch_yf_markets(meta_countries, old_countries):
    """({a3: mkt}, n_fail, n_total). Échec individuel → reprise ancien cache."""
    idx_list = [(a3, m["idx_t"], m["idx_n"]) for a3, m in sorted(meta_countries.items())
                if m.get("idx_t")]
    ccys = sorted({m["ccy"] for m in meta_countries.values() if m.get("ccy") and m["ccy"] != "USD"})
    n_total = len(idx_list) + len(ccys)
    n_fail = 0

    idx_by_a3 = {}
    for a3, ticker, name in idx_list:
        got = fetch_yf_index(ticker, name)
        if got is None:
            n_fail += 1
            old_idx = ((old_countries.get(a3) or {}).get("mkt") or {}).get("idx")
            if old_idx and old_idx.get("t") == ticker:
                idx_by_a3[a3] = old_idx
                sys.stderr.write(f"[WARN] YF {ticker} KO → ancien cache repris\n")
            else:
                sys.stderr.write(f"[WARN] YF {ticker} KO, pas d'ancien cache\n")
        else:
            idx_by_a3[a3] = got

    fx_by_ccy = {}
    for ccy in ccys:
        got = fetch_yf_fx(ccy)
        if got is None:
            n_fail += 1
            # reprise : premier pays du cache précédent portant cette devise
            for a3, m in meta_countries.items():
                if m.get("ccy") == ccy:
                    old_fx = ((old_countries.get(a3) or {}).get("mkt") or {}).get("fx")
                    if old_fx:
                        fx_by_ccy[ccy] = {k: v for k, v in old_fx.items() if k != "ccy"}
                        sys.stderr.write(f"[WARN] YF fx {ccy} KO → ancien cache repris\n")
                    break
        else:
            fx_by_ccy[ccy] = got

    out = {}
    for a3, m in meta_countries.items():
        mkt = {}
        if a3 in idx_by_a3:
            mkt["idx"] = idx_by_a3[a3]
        ccy = m.get("ccy")
        if ccy in fx_by_ccy:
            mkt["fx"] = dict(ccy=ccy, **fx_by_ccy[ccy])
        if mkt:
            out[a3] = mkt
    print(f"[YF] marchés : {len(idx_by_a3)}/{len(idx_list)} indices, "
          f"{len(fx_by_ccy)}/{len(ccys)} devises, {n_fail} échec(s)")
    return out, n_fail, n_total


# ══ ASSEMBLAGE ════════════════════════════════════════════════════════════════

LATEST_KEYS = ["gdp", "gdp_real", "gdp_ppp", "gdp_pc", "growth", "infl",
               "unemp", "cab", "reserves", "reserves_mo", "debt", "fiscal",
               "rate", "wgi_rl", "wgi_ge", "wgi_pv", "wgi_cc", "fh", "cpi",
               "vdem", "pop", "pop_gr", "life", "gini", "urban",
               "pisa", "pisa_math", "pisa_read", "pisa_sci"]

# champs restaurés depuis l'ancien cache quand une source tombe
SOURCE_FIELDS = {
    "WB": ["gdp", "gdp_real", "gdp_ppp", "gdp_pc", "reserves", "reserves_mo",
           "pop", "pop_gr", "life", "gini", "urban"],
    "IMF": ["growth", "infl", "cab", "debt", "fiscal"],
    "WGI": ["wgi_rl", "wgi_ge", "wgi_pv", "wgi_cc"],
    "FH": ["fh"],
    "VDEM": ["vdem"],
    "CPI": ["cpi"],
    "BIS": ["rate"],
    "PISA": PISA_KEYS,
}


def build_sources_registry(wb_lastupdated, wgi_lastupdated, cpi_file, fitch_file):
    weo = ("WEO avril" if 4 <= NOW.month <= 9
           else "WEO octobre") + f" {NOW.year if NOW.month >= 4 else NOW.year - 1}"
    reg = {
        "WB": {"label": "Banque mondiale · WDI", "url": "https://data.worldbank.org"},
        "IMF": {"label": "FMI · WEO", "url": "https://www.imf.org/external/datamapper",
                "vintage": weo, "forecast_from": FORECAST_FROM},
        "WGI": {"label": "Banque mondiale · WGI",
                "url": "https://info.worldbank.org/governance/wgi/"},
        "FH": {"label": "Freedom House (via OWID)",
               "url": "https://ourworldindata.org/grapher/freedom-score-fh"},
        "VDEM": {"label": "V-Dem (via OWID)",
                 "url": "https://ourworldindata.org/grapher/liberal-democracy-index"},
        "CPI": {"label": "Transparency International CPI",
                "url": "https://www.transparency.org/en/cpi"},
        "PISA": {"label": "OCDE · PISA (via OWID)",
                 "url": "https://ourworldindata.org/grapher/academic-performance",
                 "note": "enquête triennale sur les élèves de 15 ans — dernière vague "
                         "publiée 2022 ; pays participants uniquement (la Chine n'est pas "
                         "couverte au niveau national)"},
        "BIS": {"label": "BIS · taux directeurs", "url": "https://stats.bis.org"},
        "FITCH": {"label": "Fitch (via ratingshistory.info)",
                  "url": "https://ratingshistory.info", "fragile": True,
                  "note": "flux réglementaire publié avec ~12 mois de décalage — "
                          "la date de l'action fait foi"},
        "YF": {"label": "Yahoo Finance", "url": "https://finance.yahoo.com"},
    }
    if wb_lastupdated:
        reg["WB"]["lastupdated"] = wb_lastupdated
    if wgi_lastupdated:
        reg["WGI"]["lastupdated"] = wgi_lastupdated
    if cpi_file:
        reg["CPI"]["file"] = cpi_file
    if fitch_file:
        reg["FITCH"]["file"] = fitch_file
    return reg


def load_old_cache():
    if OUT_JSON.exists():
        try:
            return json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except ValueError:
            sys.stderr.write("[WARN] cache précédent illisible\n")
    return None


def assemble_full(meta, old):
    countries_meta = meta["countries"]
    a3set = set(countries_meta)
    old_countries = (old or {}).get("countries", {})
    ok, failed = [], []

    wb_latest, wb_lastupdated = fetch_wb_latest(a3set)
    wb_hist = fetch_wb_gdp_hist(a3set)
    (ok if wb_latest and wb_hist else failed).append("WB")
    imf_latest, imf_hist = fetch_imf(a3set)
    (ok if imf_latest else failed).append("IMF")
    wgi, wgi_lastupdated = fetch_wgi(a3set)
    (ok if wgi else failed).append("WGI")
    fh = fetch_owid("freedom-score-fh", "total_score", a3set,
                    lambda v: int(round(v)))
    (ok if fh else failed).append("FH")
    vdem = fetch_owid("liberal-democracy-index", "libdem_vdem__estimate_best", a3set, r2)
    (ok if vdem else failed).append("VDEM")
    cpi, cpi_file = fetch_cpi_ti(a3set)
    (ok if cpi else failed).append("CPI")
    pisa = fetch_pisa(a3set)
    (ok if pisa else failed).append("PISA")
    bis = fetch_bis(countries_meta)
    (ok if bis else failed).append("BIS")
    fitch, fitch_file = fetch_fitch(a3set)
    (ok if fitch else failed).append("FITCH")
    mkt, yf_fail, yf_total = fetch_yf_markets(countries_meta, old_countries)
    yf_ok = bool(mkt) and (yf_total == 0 or yf_fail / yf_total < 0.5)
    (ok if yf_ok else failed).append("YF")

    countries = {}
    for a3 in sorted(a3set):
        latest = {k: None for k in LATEST_KEYS}
        for src_map in (wb_latest, imf_latest, wgi):
            if src_map and a3 in src_map:
                for k, v in src_map[a3].items():
                    if k == "unemp_wb":
                        continue
                    latest[k] = v
        # chômage : IMF LUR prioritaire, fallback WB (flag 0)
        unemp_src = "IMF" if latest["unemp"] is not None else None
        if latest["unemp"] is None and wb_latest and a3 in wb_latest:
            wbu = wb_latest[a3].get("unemp_wb")
            if wbu:
                latest["unemp"] = wbu
                unemp_src = "WB"
        if fh and a3 in fh:
            latest["fh"] = fh[a3]
        if vdem and a3 in vdem:
            latest["vdem"] = vdem[a3]
        if cpi and a3 in cpi:
            latest["cpi"] = cpi[a3]
        if pisa and a3 in pisa:
            for k, v in pisa[a3].items():
                latest[k] = v
        entry = {"latest": latest}
        # src = source RÉELLEMENT retenue quand elle dépend du pays. Le lien d'audit ⓘ
        # doit la suivre : la série FMI LUR ne couvre que 122 pays, donc pointer le FMI
        # pour un pays servi par le repli Banque mondiale ouvre une page sans sa donnée.
        if unemp_src:
            entry["src"] = {"unemp": unemp_src}
        if bis and a3 in bis:
            val, month = bis[a3]
            latest["rate"] = [val, int(month[:4]), 0]
            entry["rate_m"] = month
        hist = {}
        if imf_hist and a3 in imf_hist:
            hist.update(imf_hist[a3])
        if wb_hist and a3 in wb_hist:
            hist["gdp"] = wb_hist[a3]
        if hist:
            entry["hist"] = hist
        if mkt and a3 in mkt:
            entry["mkt"] = mkt[a3]
        if fitch and a3 in fitch:
            entry["rating"] = fitch[a3]
        countries[a3] = entry

    # ── garde par source : recopie du cache précédent ──────────────────────────
    for src in failed:
        fields = SOURCE_FIELDS.get(src, [])
        n_rest = 0
        for a3, entry in countries.items():
            old_e = old_countries.get(a3) or {}
            old_latest = old_e.get("latest") or {}
            for f in fields:
                if entry["latest"].get(f) is None and old_latest.get(f) is not None:
                    entry["latest"][f] = old_latest[f]
                    n_rest += 1
            if src == "BIS" and "rate_m" not in entry and "rate_m" in old_e:
                entry["rate_m"] = old_e["rate_m"]
            if src == "WB" and "gdp" not in entry.get("hist", {}) \
                    and "gdp" in (old_e.get("hist") or {}):
                entry.setdefault("hist", {})["gdp"] = old_e["hist"]["gdp"]
            if src == "IMF":
                for hk in IMF_HIST:
                    if hk not in entry.get("hist", {}) and hk in (old_e.get("hist") or {}):
                        entry.setdefault("hist", {})[hk] = old_e["hist"][hk]
            if src == "FITCH" and "rating" not in entry and "rating" in old_e:
                entry["rating"] = old_e["rating"]
            if src == "YF" and "mkt" not in entry and "mkt" in old_e:
                entry["mkt"] = old_e["mkt"]
        if n_rest:
            sys.stderr.write(f"[GUARD] source {src} KO → {n_rest} valeurs reprises "
                             f"du cache précédent\n")
    # unemp : ni IMF ni WB → ancien cache
    if "IMF" in failed and "WB" in failed:
        for a3, entry in countries.items():
            old_latest = (old_countries.get(a3) or {}).get("latest") or {}
            if entry["latest"].get("unemp") is None and old_latest.get("unemp") is not None:
                entry["latest"]["unemp"] = old_latest["unemp"]

    sources = build_sources_registry(wb_lastupdated, wgi_lastupdated, cpi_file, fitch_file)
    # millésime PISA affichable + note d'attente si la vague suivante tarde à arriver
    pisa_wave, pisa_pending = pisa_vintage(pisa)
    if pisa_wave:
        sources["PISA"]["latest_wave"] = pisa_wave
    if pisa_pending:
        sources["PISA"]["pending_wave"] = pisa_pending
    if old and failed:
        # une source recopiée garde ses métadonnées d'origine
        for src in failed:
            if src in (old.get("sources") or {}):
                sources[src] = old["sources"][src]
    markets_ts = NOW.isoformat(timespec="seconds") if (yf_ok or bis) else \
        (old or {}).get("meta", {}).get("markets_updated_at")
    payload = {
        "meta": {
            "updated_at": NOW.isoformat(timespec="seconds"),
            "updated_at_unix": int(time.time()),
            "markets_updated_at": markets_ts,
            "sources_ok": ok, "sources_failed": failed,
            "n_countries": len(countries),
        },
        "sources": sources,
        "countries": countries,
    }
    return payload, len(ok), len(failed)


def assemble_markets_only(meta, old):
    if not old or "countries" not in old:
        sys.stderr.write("[fatal] --markets-only exige un cache précédent complet\n")
        sys.exit(2)
    countries_meta = meta["countries"]
    countries = old["countries"]
    ok, failed = [], []
    bis = fetch_bis(countries_meta)
    (ok if bis else failed).append("BIS")
    mkt, yf_fail, yf_total = fetch_yf_markets(countries_meta, countries)
    yf_ok = bool(mkt) and (yf_total == 0 or yf_fail / yf_total < 0.5)
    (ok if yf_ok else failed).append("YF")
    for a3, entry in countries.items():
        if bis and a3 in bis:
            val, month = bis[a3]
            entry.setdefault("latest", {})["rate"] = [val, int(month[:4]), 0]
            entry["rate_m"] = month
        if mkt and a3 in mkt:
            entry["mkt"] = mkt[a3]
    old_ok = [s for s in old["meta"].get("sources_ok", []) if s not in ("BIS", "YF")]
    old_failed = [s for s in old["meta"].get("sources_failed", []) if s not in ("BIS", "YF")]
    payload = {
        "meta": {
            "updated_at": old["meta"].get("updated_at"),
            "updated_at_unix": old["meta"].get("updated_at_unix"),
            "markets_updated_at": NOW.isoformat(timespec="seconds"),
            "sources_ok": old_ok + ok, "sources_failed": old_failed + failed,
            "n_countries": len(countries),
        },
        "sources": old.get("sources", {}),
        "countries": countries,
    }
    return payload, len(old_ok + ok), len(old_failed + failed)


# ══ AUDIT INTERNE (fb-audit-valeurs) ══════════════════════════════════════════

CONTROL = ["FRA", "USA", "JPN", "NGA", "ARG"]


def _fmt(v):
    if v is None:
        return "·"
    val, y, f = v
    s = f"{val:,}".replace(",", " ") if isinstance(val, int) and abs(val) >= 10000 else str(val)
    return f"{s} ({y}{'p' if f else ''})"


def audit(payload, markets_only=False):
    c = payload["countries"]
    errs = []

    def chk(cond, msg):
        if not cond:
            errs.append(msg)

    def g(a3, k):
        v = (c.get(a3) or {}).get("latest", {}).get(k)
        return v[0] if v else None

    if not markets_only:
        chk(g("USA", "gdp") and 20e12 < g("USA", "gdp") < 40e12,
            f"USA gdp hors 20-40e12 : {g('USA', 'gdp')}")
        chk(g("FRA", "debt") and 100 < g("FRA", "debt") < 130,
            f"FRA debt hors 100-130 : {g('FRA', 'debt')}")
        chk(g("USA", "pop") and 320e6 < g("USA", "pop") < 360e6,
            f"USA pop hors 320-360M : {g('USA', 'pop')}")
        chk(g("FRA", "gdp_pc") and 40000 < g("FRA", "gdp_pc") < 90000,
            f"FRA gdp_pc suspect : {g('FRA', 'gdp_pc')}")
        for a3 in CONTROL:
            v = g(a3, "growth")
            chk(v is None or abs(v) < 30, f"{a3} growth hors ±30 : {v}")
        # PISA : échelle calibrée sur 500 (moyenne OCDE de la 1ʳᵉ vague) — hors 250-650
        # = série cassée (ou mauvaise colonne du grapher multi-dimensions).
        chk(g("FRA", "pisa") is None or 400 < g("FRA", "pisa") < 550,
            f"FRA pisa hors 400-550 : {g('FRA', 'pisa')}")
        for a3, entry in c.items():
            for k in PISA_KEYS:
                v = entry["latest"].get(k)
                chk(v is None or 250 <= v[0] <= 650, f"{a3} {k} hors bornes PISA : {v}")
            for k, lo, hi in (("fh", 0, 100), ("cpi", 0, 100), ("vdem", 0, 1)):
                v = entry["latest"].get(k)
                chk(v is None or lo <= v[0] <= hi, f"{a3} {k} hors bornes : {v}")
            v = entry["latest"].get("growth")
            if v is not None and abs(v[0]) >= 30:
                sys.stderr.write(f"[AUDIT-WARN] {a3} growth extrême (réel FMI) : {v[0]}\n")
    # marchés : dernier prix > 0
    for a3, entry in c.items():
        idx = (entry.get("mkt") or {}).get("idx")
        if idx is not None:
            chk(idx.get("last") and idx["last"] > 0, f"{a3} idx.last invalide : {idx.get('last')}")
        fx = (entry.get("mkt") or {}).get("fx")
        if fx is not None:
            chk(fx.get("last") and fx["last"] > 0, f"{a3} fx.last invalide : {fx.get('last')}")
    if errs:
        for e in errs:
            sys.stderr.write(f"[AUDIT-FAIL] {e}\n")
        return False

    # tableau de contrôle
    rows = ["gdp", "gdp_pc", "growth", "infl", "unemp", "cab", "debt", "fiscal",
            "rate", "wgi_rl", "fh", "cpi", "vdem", "pop", "life", "gini", "urban",
            "pisa", "pisa_math", "pisa_read", "pisa_sci"]
    print("\n── TABLEAU DE CONTRÔLE (valeur (année, p=prévision FMI)) " + "─" * 30)
    print(f"{'':12s}" + "".join(f"{a3:>22s}" for a3 in CONTROL))
    for k in rows:
        print(f"{k:12s}" + "".join(
            f"{_fmt(c.get(a3, {}).get('latest', {}).get(k)):>22s}" for a3 in CONTROL))
    for a3 in CONTROL:
        entry = c.get(a3, {})
        idx = (entry.get("mkt") or {}).get("idx") or {}
        fx = (entry.get("mkt") or {}).get("fx") or {}
        rat = entry.get("rating") or {}
        print(f"{a3} : idx {idx.get('n')} last={idx.get('last')} d1={idx.get('d1')}% "
              f"ytd={idx.get('ytd')}% | fx {fx.get('t')} {fx.get('mode')} last={fx.get('last')} "
              f"y1={fx.get('y1')}% | Fitch {rat.get('fitch')} ({rat.get('date')}) "
              f"| taux {entry.get('latest', {}).get('rate')} m={entry.get('rate_m')}")
    print("─" * 100 + "\n")
    return True


# ══ ÉCRITURE ══════════════════════════════════════════════════════════════════

def write_outputs(payload):
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUT_JSON.write_text(blob, encoding="utf-8")
    OUT_JS.write_text(
        f"/* atlas_econ_cache.js — généré {payload['meta']['updated_at']} — "
        f"sources OK : {','.join(payload['meta']['sources_ok'])} */\n"
        f"window.__ATLAS_ECON__ = {blob};\n",
        encoding="utf-8",
    )
    print(f"[atlas_econ] écrit {OUT_JSON} ({len(blob)/1024:.0f} Ko) + {OUT_JS}")


def wait_for_network(max_wait=240):
    """Attend que le réseau soit prêt avant de commencer à fetcher.

    ANTI WAKE-RACE (cause du STALE du 18→20/07/2026) : les fetchers en
    StartInterval sont REJOUÉS au réveil du Mac, mais launchd les lance AVANT que
    le WiFi soit remonté → « Could not resolve host » sur TOUTES les sources → run
    gâché, puis 6 h d'attente. Résultat : cache figé pendant des jours alors que
    la machine est en ligne la plupart du temps.

    On sonde quelques hôtes fiables et on réessaie jusqu'à max_wait secondes.
    Retourne False si toujours hors-ligne → le run s'arrête PROPREMENT sans toucher
    au cache (le watchdog horaire relancera dès que le réseau est là)."""
    probes = ("https://api.worldbank.org/v2/country?format=json&per_page=1",
              "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d")
    waited, delay = 0, 3
    while True:
        for u in probes:
            try:
                r = cr.get(u, impersonate="chrome120", timeout=8)
                if r.status_code and r.status_code < 500:
                    if waited:
                        sys.stderr.write(f"[net] réseau prêt après {waited}s\n")
                    return True
            except Exception:  # noqa: BLE001
                pass
        if waited >= max_wait:
            sys.stderr.write(f"[net] toujours hors-ligne après {waited}s — abandon "
                             f"propre, cache conservé (le watchdog réessaiera)\n")
            return False
        time.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 20)


def main():
    ap = argparse.ArgumentParser(description="Fetcher Atlas Économique (217 pays)")
    ap.add_argument("--dry-run", action="store_true", help="fetch complet, n'écrit rien")
    ap.add_argument("--markets-only", action="store_true",
                    help="ne rafraîchit que Yahoo + BIS (reste = cache précédent)")
    args = ap.parse_args()

    # Anti wake-race launchd : ne pas gâcher le run si le WiFi n'est pas encore là.
    if not wait_for_network():
        sys.exit(0)  # sortie propre : cache intact, prochain cycle réessaiera

    t0 = time.time()
    meta = load_meta()
    print(f"[atlas_econ] meta : {len(meta['countries'])} pays ({META_PATH})")
    old = load_old_cache()

    if args.markets_only:
        payload, n_ok, n_fail = assemble_markets_only(meta, old)
    else:
        payload, n_ok, n_fail = assemble_full(meta, old)

    if not audit(payload, markets_only=args.markets_only):
        sys.stderr.write("[GUARD] audit interne en échec — cache précédent conservé\n")
        sys.exit(1 if OUT_JSON.exists() else 2)

    # garde globale anti-écrasement
    if not args.markets_only and n_ok < 5 and OUT_JSON.exists():
        sys.stderr.write(f"[GUARD] seulement {n_ok} source(s) OK ({n_fail} KO) — "
                         f"cache précédent conservé\n")
        sys.exit(1)

    if args.dry_run:
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        print(f"[atlas_econ] DRY-RUN : rien d'écrit ({len(blob)/1024:.0f} Ko, "
              f"{n_ok} sources OK, {n_fail} KO, {time.time()-t0:.0f}s)")
        return
    write_outputs(payload)
    print(f"[atlas_econ] OK · {n_ok} sources OK, {n_fail} KO, "
          f"{payload['meta']['n_countries']} pays · {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
