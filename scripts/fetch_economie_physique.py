#!/usr/bin/env python3
"""Fetch données « Économie physique » — dashboard comparatif pays × secteurs +
les DEUX indicateurs physiques de Jean-Marc Jancovici.

Idée directrice (Jancovici) : le PIB monétaire mesure de plus en plus mal
l'économie réelle ; il faut regarder des FLUX PHYSIQUES en unités physiques
(tonnes, m², kWh). Les deux indicateurs phares qu'il cite (Le Point 2020,
Ouest-France 2024) :
  #1  les TONNES CHARGÉES DANS LES CAMIONS  → fret routier (Eurostat THS_T)
  #2  les MÈTRES CARRÉS CONSTRUITS dans l'année → permis de construire (Eurostat MIO_M2)

PANNEAUX & SOURCES (toutes vérifiées HTTP 200 + données réelles) :
  P1 Fret routier (tonnes)      Eurostat road_go_ta_tott (THS_T, LOADED) + FRED TRUCKD11 (US)
  P2 Surfaces construites (m²)   Eurostat sts_cobp_a (BPRM_SQM/MIO_M2) + FRED PERMIT×HOUSTSFLAA1FQ (US, reconstruit)
  P3 Production industrielle      Eurostat sts_inpr_m (indice C + sous-secteurs NACE) + FRED IPMAN (US)
  P3b Valeur ajoutée manufacturière  World Bank NV.IND.MANF.CD (niveau, monde incl. Chine/Inde)
  P4 Production physique (élec.)  Ember yearly (génération TWh par pays/source, monde)
  P5 Découplage PIB↔physique     World Bank NY.GDP.MKTP.KD (PIB volume) vs flux physiques

Trous de couverture ASSUMÉS dans l'UI (cf coverage[]):
  - Chine/Inde : présentes seulement en valeur ajoutée (WDI) et électricité (Ember),
    PAS sur les 2 indicateurs Jancovici (fret routier / m² indisponibles librement).
  - Japon m² : derrière une clé e-Stat → absent du panneau surfaces.
  - US m² : reconstruit (permis×surface unitaire) → approximation, pas comparable au m² UE en niveau.
  - Indices de production : non comparables EN NIVEAU entre pays (bases nationales) → base 100 / momentum.

Cache : ~/Library/Caches/site_crypto_finance/economie_physique_cache.{json,js}
Sortie JS : window.__ECO_PHYSIQUE__ = {...}
"""
import csv
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _fred_helpers import fetch_fred
except Exception:  # pragma: no cover — helper toujours présent en prod
    fetch_fred = None

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "economie_physique_cache.json"
CACHE_JS = CACHE_DIR / "economie_physique_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance/EcoPhysique"
SQFT_TO_M2 = 0.092903

# ──────────────────────────────────────────────────────────────────────────
# Référentiel pays — palette FIXE par entité (règle dataviz : même couleur partout)
# clé canonique | nom FR | iso2 Eurostat | iso3 (Ember) | iso2 World Bank | couleur | région
# ──────────────────────────────────────────────────────────────────────────
COUNTRIES = {
    # ── Europe (Eurostat) — disponibles pour fret + m² + production indicielle ──
    "FR": dict(name="France",          estat="FR",        iso3="FRA", wb="FR", color="#D4A017", region="Europe"),
    "DE": dict(name="Allemagne",       estat="DE",        iso3="DEU", wb="DE", color="#2EA05A", region="Europe"),
    "IT": dict(name="Italie",          estat="IT",        iso3="ITA", wb="IT", color="#5BC0BE", region="Europe"),
    "ES": dict(name="Espagne",         estat="ES",        iso3="ESP", wb="ES", color="#E07A5F", region="Europe"),
    "NL": dict(name="Pays-Bas",        estat="NL",        iso3="NLD", wb="NL", color="#E9C46A", region="Europe"),
    "PL": dict(name="Pologne",         estat="PL",        iso3="POL", wb="PL", color="#9A8C98", region="Europe"),
    "GB": dict(name="Royaume-Uni",     estat="UK",        iso3="GBR", wb="GB", color="#C9A227", region="Europe"),
    "BE": dict(name="Belgique",        estat="BE",        iso3="BEL", wb="BE", color="#C9ADA7", region="Europe"),
    "SE": dict(name="Suède",           estat="SE",        iso3="SWE", wb="SE", color="#8FB996", region="Europe"),
    "CZ": dict(name="Tchéquie",        estat="CZ",        iso3="CZE", wb="CZ", color="#D4A373", region="Europe"),
    "RO": dict(name="Roumanie",        estat="RO",        iso3="ROU", wb="RO", color="#B08968", region="Europe"),
    "AT": dict(name="Autriche",        estat="AT",        iso3="AUT", wb="AT", color="#DDA15E", region="Europe"),
    "PT": dict(name="Portugal",        estat="PT",        iso3="PRT", wb="PT", color="#9C6644", region="Europe"),
    "EU27": dict(name="UE-27",         estat="EU27_2020", iso3="EU27", wb="EUU", color="#F0ECE0", region="Europe"),
    # ── Reste du monde (World Bank + Ember) — valeur ajoutée, électricité, PIB ──
    "US": dict(name="États-Unis",      estat=None,        iso3="USA", wb="US", color="#F5C54B", region="Amériques"),
    "CA": dict(name="Canada",          estat=None,        iso3="CAN", wb="CA", color="#A7C957", region="Amériques"),
    "BR": dict(name="Brésil",          estat=None,        iso3="BRA", wb="BR", color="#6A994E", region="Amériques"),
    "MX": dict(name="Mexique",         estat=None,        iso3="MEX", wb="MX", color="#4C9F70", region="Amériques"),
    "AR": dict(name="Argentine",       estat=None,        iso3="ARG", wb="AR", color="#6CA0B4", region="Amériques"),
    "CN": dict(name="Chine",           estat=None,        iso3="CHN", wb="CN", color="#E04545", region="Asie-Pacifique"),
    "IN": dict(name="Inde",            estat=None,        iso3="IND", wb="IN", color="#F2CC8F", region="Asie-Pacifique"),
    "JP": dict(name="Japon",           estat=None,        iso3="JPN", wb="JP", color="#E76F51", region="Asie-Pacifique"),
    "KR": dict(name="Corée du Sud",    estat=None,        iso3="KOR", wb="KR", color="#81B29A", region="Asie-Pacifique"),
    "ID": dict(name="Indonésie",       estat=None,        iso3="IDN", wb="ID", color="#CB997E", region="Asie-Pacifique"),
    "VN": dict(name="Vietnam",         estat=None,        iso3="VNM", wb="VN", color="#DDBEA9", region="Asie-Pacifique"),
    "TH": dict(name="Thaïlande",       estat=None,        iso3="THA", wb="TH", color="#B98B73", region="Asie-Pacifique"),
    "TW": dict(name="Taïwan",          estat=None,        iso3="TWN", wb="TW", color="#A5A58D", region="Asie-Pacifique"),
    "AU": dict(name="Australie",       estat=None,        iso3="AUS", wb="AU", color="#BC6C25", region="Asie-Pacifique"),
    "RU": dict(name="Russie",          estat=None,        iso3="RUS", wb="RU", color="#8E1C1C", region="Eurasie"),
    "TR": dict(name="Turquie",         estat=None,        iso3="TUR", wb="TR", color="#CB904D", region="Eurasie"),
    "SA": dict(name="Arabie Saoudite", estat=None,        iso3="SAU", wb="SA", color="#6B705C", region="Moyen-Orient"),
    "ZA": dict(name="Afrique du Sud",  estat=None,        iso3="ZAF", wb="ZA", color="#BC8A5F", region="Afrique"),
    "NG": dict(name="Nigéria",         estat=None,        iso3="NGA", wb="NG", color="#97704F", region="Afrique"),
    "EG": dict(name="Égypte",          estat=None,        iso3="EGY", wb="EG", color="#C8B273", region="Afrique"),
    "DZ": dict(name="Algérie",         estat=None,        iso3="DZA", wb="DZ", color="#708D5B", region="Afrique"),
    "MA": dict(name="Maroc",           estat=None,        iso3="MAR", wb="MA", color="#A3784F", region="Afrique"),
}
# pays UE couverts par Eurostat pour fret + m² (indicateurs Jancovici)
EU_GEO = ["FR", "DE", "IT", "ES", "NL", "PL", "GB", "BE", "SE", "CZ", "RO", "AT", "PT", "EU27"]
# production industrielle indicielle (Eurostat sts_inpr_m) — majors UE seulement (graphe en lignes)
IP_GEO = ["FR", "DE", "IT", "ES", "NL", "PL", "EU27"]

ok, failed = [], []


def mark(name, good):
    (ok if good else failed).append(name)
    sys.stderr.write(("[OK]  " if good else "[FAIL] ") + name + "\n")


# ──────────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────────
def http_text(url, timeout=45, retries=3, accept="application/json"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last = None
    for a in range(retries):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except (HTTPError, URLError, TimeoutError, ConnectionResetError, OSError) as e:
            last = e
            if a < retries - 1:
                time.sleep(2 * (2 ** a))
    raise last


# ──────────────────────────────────────────────────────────────────────────
# Eurostat JSON-stat — parseur multi-dimensionnel général
# ──────────────────────────────────────────────────────────────────────────
EUROSTAT_BASE = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/"
                 "1.0/data/")


def estat_get(dataset, params):
    q = [("format", "JSON"), ("lang", "EN")]
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            for x in v:
                q.append((k, x))
        else:
            q.append((k, v))
    url = EUROSTAT_BASE + dataset + "?" + urlencode(q)
    return json.loads(http_text(url, accept="application/json"))


def estat_matrix(d, fixed=None):
    """Renvoie {geo_code: {time_code: float}} en épinglant les dimensions
    autres que geo/time aux codes de `fixed` (sinon 1ʳᵉ catégorie)."""
    fixed = fixed or {}
    ids = d["id"]
    sizes = d["size"]
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    idxmaps = {dim: d["dimension"][dim]["category"]["index"] for dim in ids}
    val = d["value"]
    base = 0
    for k, dim in enumerate(ids):
        if dim in ("geo", "time"):
            continue
        codes = idxmaps[dim]
        code = fixed.get(dim)
        if code is None or code not in codes:
            code = next(iter(codes))
        base += codes[code] * strides[k]
    gk, tk = ids.index("geo"), ids.index("time")
    out = {}
    for gcode, gpos in idxmaps["geo"].items():
        row = {}
        for tcode, tpos in idxmaps["time"].items():
            fi = base + gpos * strides[gk] + tpos * strides[tk]
            v = val.get(str(fi))
            if v is not None:
                try:
                    row[tcode] = float(v)
                except (TypeError, ValueError):
                    pass
        if row:
            out[gcode] = row
    return out


def annualize(timed, min_periods=1):
    """{ '2008' ou '2008-03': v } -> {year:int -> moyenne annuelle}.

    Écarte une année INFRA-ANNUELLE (clés type '2026-03') dont le nombre de
    sous-périodes (mois) est < min_periods — typiquement l'année courante
    incomplète, qui sinon serait tracée comme un point annuel plein faussé.
    Les séries déjà annuelles (clés '2024') ne sont jamais filtrées."""
    buckets = {}
    submonthly = set()
    for t, v in timed.items():
        ys = t[:4]
        try:
            y = int(ys)
        except ValueError:
            continue
        buckets.setdefault(y, []).append(v)
        if len(t) > 4:
            submonthly.add(y)
    out = {}
    for y, vs in buckets.items():
        if y in submonthly and len(vs) < min_periods:
            continue
        out[y] = sum(vs) / len(vs)
    return out


def yv(yearmap, since=2000):
    """{year:val} -> (years[], values[]) triés, depuis `since`."""
    rows = sorted((y, v) for y, v in yearmap.items() if y >= since)
    return [y for y, _ in rows], [round(v, 3) for _, v in rows]


# ──────────────────────────────────────────────────────────────────────────
# P1 — Fret routier (tonnes chargées) — indicateur Jancovici #1
# ──────────────────────────────────────────────────────────────────────────
def build_fret():
    estat_geo = [COUNTRIES[c]["estat"] for c in EU_GEO]
    rev = {COUNTRIES[c]["estat"]: c for c in EU_GEO}
    series = {}
    src = None
    try:
        d = estat_get("road_go_ta_tott", {
            "unit": "THS_T", "tra_type": "TOTAL", "tra_oper": "LOADED",
            "geo": estat_geo, "sinceTimePeriod": "2005",
        })
        mat = estat_matrix(d, {"unit": "THS_T", "tra_type": "TOTAL", "tra_oper": "LOADED"})
        for gcode, row in mat.items():
            canon = rev.get(gcode)
            if not canon:
                continue
            ys, vs = yv({int(t): v / 1000.0 for t, v in row.items()}, since=2005)  # Mt
            if ys:
                series[canon] = {"years": ys, "values": [round(v, 2) for v in vs]}
        src = d.get("updated")
        mark("Eurostat:road_go_ta_tott", bool(series))
    except Exception as e:
        mark("Eurostat:road_go_ta_tott", False)
        sys.stderr.write(f"  fret_eu: {e}\n")

    # Tonnes-KM (activité de transport, distance incluse) — pour la nuance
    # honnête : le tonnage baisse mais l'effort de transport (t-km) a monté.
    series_tkm = {}
    try:
        d2 = estat_get("road_go_ta_tott", {
            "unit": "MIO_TKM", "tra_type": "TOTAL", "tra_oper": "LOADED",
            "geo": estat_geo, "sinceTimePeriod": "2005",
        })
        mat2 = estat_matrix(d2, {"unit": "MIO_TKM", "tra_type": "TOTAL", "tra_oper": "LOADED"})
        for gcode, row in mat2.items():
            canon = rev.get(gcode)
            if not canon:
                continue
            ys, vs = yv({int(t): v / 1000.0 for t, v in row.items()}, since=2005)  # Md t-km
            if ys:
                series_tkm[canon] = {"years": ys, "values": [round(v, 1) for v in vs]}
        mark("Eurostat:road_go_ta_tott(tkm)", bool(series_tkm))
    except Exception as e:
        mark("Eurostat:road_go_ta_tott(tkm)", False)
        sys.stderr.write(f"  fret_tkm: {e}\n")

    # US — Truck Tonnage Index (FRED TRUCKD11), annualisé (indice)
    us = None
    if fetch_fred:
        o = fetch_fred("TRUCKD11", start="2000-01-01")
        if o:
            ym = annualize({d: v for d, v in zip(o["dates"], o["values"])}, min_periods=12)
            ys, vs = yv(ym, since=2005)
            us = {"years": ys, "values": [round(v, 2) for v in vs],
                  "source_url": o["source_url"]}
        mark("FRED:TRUCKD11", bool(us))

    return {
        "unit": "Mt (millions de tonnes chargées)",
        "series": series,
        "series_tkm": series_tkm,
        "us_index": us,
        "source_url": "https://ec.europa.eu/eurostat/databrowser/view/road_go_ta_tott",
        "source_label": "Eurostat · road_go_ta_tott (tonnes chargées) · FRED TRUCKD11 (US, indice)",
        "updated": src,
    }


# ──────────────────────────────────────────────────────────────────────────
# P2 — Surfaces construites (m²) — indicateur Jancovici #2
# ──────────────────────────────────────────────────────────────────────────
def build_surf():
    geo_set = ["FR", "DE", "IT", "ES", "NL", "PL", "EU27"]  # UK souvent absent du m²
    estat_geo = [COUNTRIES[c]["estat"] for c in geo_set]
    rev = {COUNTRIES[c]["estat"]: c for c in geo_set}
    series = {}
    upd = None
    try:
        d = estat_get("sts_cobp_a", {
            "indic_bt": "BPRM_SQM", "unit": "MIO_M2", "s_adj": "NSA",
            "cpa2_1": "CPA_F41001_41002", "geo": estat_geo, "sinceTimePeriod": "2000",
        })
        mat = estat_matrix(d, {"indic_bt": "BPRM_SQM", "unit": "MIO_M2",
                               "s_adj": "NSA", "cpa2_1": "CPA_F41001_41002"})
        for gcode, row in mat.items():
            canon = rev.get(gcode)
            if not canon:
                continue
            ys, vs = yv({int(t): v for t, v in row.items()}, since=2000)
            if ys:
                series[canon] = {"years": ys, "values": [round(v, 2) for v in vs]}
        upd = d.get("updated")
        mark("Eurostat:sts_cobp_a", bool(series))
    except Exception as e:
        mark("Eurostat:sts_cobp_a", False)
        sys.stderr.write(f"  surf_eu: {e}\n")

    # US reconstruit : permis maisons individuelles (PERMIT1, taux annuel SAAR en
    # milliers) × surface moyenne 1-family (sqft) → m². PERMIT1 étant un taux
    # annualisé désaisonnalisé, on MOYENNE les mois (pas de somme) pour obtenir le
    # nombre annuel d'unités. Cohérent : unités 1-family × surface 1-family.
    us = None
    if fetch_fred:
        permit = fetch_fred("PERMIT1", start="1999-01-01")          # SAAR milliers, mensuel, 1 logement
        area = fetch_fred("HOUSTSFLAA1FQ", start="1999-01-01")      # sqft / logement, trimestriel
        if permit and area:
            pm = {}
            for dt, v in zip(permit["dates"], permit["values"]):
                pm.setdefault(int(dt[:4]), []).append(v)
            # n'annualiser que les années PLEINES (12 mois) — exclut l'année courante partielle
            punits = {y: (sum(vs) / len(vs)) * 1000.0 for y, vs in pm.items() if len(vs) >= 12}
            asqft = annualize({dt: v for dt, v in zip(area["dates"], area["values"])})
            rows = {}
            for y, units in punits.items():
                if y in asqft:
                    rows[y] = units * asqft[y] * SQFT_TO_M2 / 1e6  # → millions de m²
            ys, vs = yv(rows, since=2000)
            if ys:
                us = {"years": ys, "values": [round(v, 1) for v in vs],
                      "source_url": "https://fred.stlouisfed.org/series/PERMIT1"}
        mark("FRED:PERMIT1×HOUSTSFLAA1FQ", bool(us))

    return {
        "unit": "millions de m² (surface utile autorisée)",
        "series": series,
        "us_reconstructed": us,
        "source_url": "https://ec.europa.eu/eurostat/databrowser/view/sts_cobp_a",
        "source_label": "Eurostat · sts_cobp_a (m² de permis) · US reconstruit (Census PERMIT × surface moyenne)",
        "updated": upd,
    }


# ──────────────────────────────────────────────────────────────────────────
# P3 — Production industrielle (indice + valeur ajoutée)
# ──────────────────────────────────────────────────────────────────────────
NACE_SECTORS = [
    ("C10-C12", "Agroalimentaire"),
    ("C13-C15", "Textile · habillement"),
    ("C19", "Raffinage pétrolier"),
    ("C20", "Chimie"),
    ("C21", "Pharmacie"),
    ("C22", "Plastique · caoutchouc"),
    ("C23", "Matériaux (ciment, verre)"),
    ("C24", "Métallurgie (acier)"),
    ("C26", "Électronique · informatique"),
    ("C28", "Machines · équipements"),
    ("C29", "Automobile"),
]


def build_ip():
    # indice total manufacturier (C), annualisé, par pays UE
    estat_geo = [COUNTRIES[c]["estat"] for c in IP_GEO]
    rev = {COUNTRIES[c]["estat"]: c for c in IP_GEO}
    total = {}
    upd = None
    try:
        d = estat_get("sts_inpr_m", {
            "indic_bt": "PRD", "nace_r2": "C", "s_adj": "SCA", "unit": "I21",
            "geo": estat_geo, "sinceTimePeriod": "2000-01",
        })
        mat = estat_matrix(d, {"indic_bt": "PRD", "nace_r2": "C", "s_adj": "SCA", "unit": "I21"})
        for gcode, row in mat.items():
            canon = rev.get(gcode)
            if not canon:
                continue
            ys, vs = yv(annualize(row, min_periods=12), since=2000)
            if ys:
                total[canon] = {"years": ys, "values": [round(v, 1) for v in vs]}
        upd = d.get("updated")
        mark("Eurostat:sts_inpr_m(C)", bool(total))
    except Exception as e:
        mark("Eurostat:sts_inpr_m(C)", False)
        sys.stderr.write(f"  ip_total: {e}\n")

    # US — IPMAN annualisé
    if fetch_fred:
        o = fetch_fred("IPMAN", start="2000-01-01")
        if o:
            ys, vs = yv(annualize({d: v for d, v in zip(o["dates"], o["values"])}, min_periods=12), since=2000)
            total["US"] = {"years": ys, "values": [round(v, 1) for v in vs]}
        mark("FRED:IPMAN", "US" in total)

    # sous-secteurs NACE pour EU27 (momentum sectoriel)
    sectors = []
    try:
        d = estat_get("sts_inpr_m", {
            "indic_bt": "PRD", "nace_r2": [c for c, _ in NACE_SECTORS],
            "s_adj": "SCA", "unit": "I21", "geo": "EU27_2020",
            "sinceTimePeriod": "2000-01",
        })
        ids = d["id"]
        for code, label in NACE_SECTORS:
            mat = estat_matrix(d, {"indic_bt": "PRD", "nace_r2": code, "s_adj": "SCA",
                                   "unit": "I21"})
            row = mat.get("EU27_2020")
            if not row:
                continue
            ys, vs = yv(annualize(row, min_periods=12), since=2000)
            if ys:
                sectors.append({"code": code, "label": label, "years": ys,
                                "values": [round(v, 1) for v in vs]})
        mark("Eurostat:sts_inpr_m(secteurs)", bool(sectors))
    except Exception as e:
        mark("Eurostat:sts_inpr_m(secteurs)", False)
        sys.stderr.write(f"  ip_sectors: {e}\n")

    return {
        "unit": "indice de production (2015 = 100)",
        "total": total,
        "sectors": {"geo": "EU27", "items": sectors},
        "source_url": "https://ec.europa.eu/eurostat/databrowser/view/sts_inpr_m",
        "source_label": "Eurostat · sts_inpr_m (indice volume, désais.) · FRED IPMAN (US)",
        "updated": upd,
    }


# ──────────────────────────────────────────────────────────────────────────
# P3b / P5 — World Bank : valeur ajoutée manufacturière (niveau) + PIB volume
# ──────────────────────────────────────────────────────────────────────────
def wb_series(indicator, since=2000):
    # EUU = code World Bank de l'UE-27 (inclus pour le PIB agrégé du découplage)
    codes = ";".join(COUNTRIES[c]["wb"] for c in COUNTRIES)
    url = (f"https://api.worldbank.org/v2/country/{codes}/indicator/{indicator}"
           f"?format=json&date={since}:2024&per_page=20000")
    txt = http_text(url, accept="application/json")
    arr = json.loads(txt)
    if not isinstance(arr, list) or len(arr) < 2 or not arr[1]:
        return {}
    # World Bank renvoie country.id en ISO2 pour les pays (US, FR…) MAIS "EU"
    # pour l'agrégat UE, alors que countryiso3code est non ambigu (USA, FRA, EUU).
    # On mappe donc sur l'ISO3 en priorité, avec repli sur l'id.
    rev = {}
    for c in COUNTRIES:
        rev[COUNTRIES[c]["wb"]] = c
        rev[COUNTRIES[c]["iso3"]] = c
    by = {}
    for r in arr[1]:
        v = r.get("value")
        if v is None:
            continue
        canon = rev.get(r.get("countryiso3code")) or rev.get((r.get("country") or {}).get("id"))
        if not canon:
            continue
        by.setdefault(canon, {})[int(r["date"])] = float(v)
    return by


def build_manuf_va():
    try:
        by = wb_series("NV.IND.MANF.CD")
        history = {}
        ranking = []
        for canon, ym in by.items():
            if canon == "EU27":
                continue  # agrégat exclu du classement-pays « atelier du monde »
            ys, vs = yv({y: v / 1e9 for y, v in ym.items()}, since=2000)  # Md USD
            if ys:
                history[canon] = {"years": ys, "values": [round(v, 1) for v in vs]}
                ranking.append({"code": canon, "name": COUNTRIES[canon]["name"],
                                "value": round(vs[-1], 1), "year": ys[-1]})
        ranking.sort(key=lambda x: -x["value"])
        mark("WorldBank:NV.IND.MANF.CD", bool(ranking))
        return {"unit": "Md USD courants", "ranking": ranking, "history": history,
                "source_url": "https://data.worldbank.org/indicator/NV.IND.MANF.CD",
                "source_label": "World Bank WDI · NV.IND.MANF.CD (valeur ajoutée manufacturière)"}
    except Exception as e:
        mark("WorldBank:NV.IND.MANF.CD", False)
        sys.stderr.write(f"  manuf_va: {e}\n")
        return {"unit": "Md USD courants", "ranking": [], "history": {}}


def build_gdp():
    try:
        by = wb_series("NY.GDP.MKTP.KD")
        history = {}
        for canon, ym in by.items():
            ys, vs = yv({y: v / 1e9 for y, v in ym.items()}, since=2000)  # Md USD const
            if ys:
                history[canon] = {"years": ys, "values": [round(v, 1) for v in vs]}
        mark("WorldBank:NY.GDP.MKTP.KD", bool(history))
        return {"unit": "Md USD constants 2015", "history": history,
                "source_url": "https://data.worldbank.org/indicator/NY.GDP.MKTP.KD",
                "source_label": "World Bank WDI · NY.GDP.MKTP.KD (PIB volume)"}
    except Exception as e:
        mark("WorldBank:NY.GDP.MKTP.KD", False)
        sys.stderr.write(f"  gdp: {e}\n")
        return {"unit": "Md USD constants 2015", "history": {}}


# ──────────────────────────────────────────────────────────────────────────
# P4 — Électricité (Ember yearly) : génération TWh par pays + mix par source
# ──────────────────────────────────────────────────────────────────────────
EMBER_URL = "https://files.ember-energy.org/public-downloads/yearly_full_release_long_format.csv"
EMBER_FUELS = ["Coal", "Gas", "Other Fossil", "Nuclear", "Hydro", "Wind",
               "Solar", "Bioenergy", "Other Renewables"]


def build_electricity():
    want = {COUNTRIES[c]["iso3"]: c for c in COUNTRIES if c != "EU27"}
    # iso3 -> {year -> {fuel -> twh}} et clean%
    gen = {}
    clean = {}
    try:
        req = Request(EMBER_URL, headers={"User-Agent": UA})
        resp = urlopen(req, timeout=120)
        stream = io.TextIOWrapper(resp, encoding="utf-8", errors="ignore")
        rd = csv.reader(stream)
        header = next(rd)
        col = {name: i for i, name in enumerate(header)}
        ciso = col["ISO 3 code"]
        ccat = col["Category"]
        csub = col["Subcategory"]
        cvar = col["Variable"]
        cunit = col["Unit"]
        cyear = col["Year"]
        cval = col["Value"]
        for row in rd:
            if len(row) <= cval:
                continue
            iso3 = row[ciso]
            canon = want.get(iso3)
            if not canon or row[ccat] != "Electricity generation":
                continue
            unit = row[cunit]
            sub = row[csub]
            var = row[cvar]
            try:
                year = int(row[cyear])
            except ValueError:
                continue
            if year < 2000:
                continue
            val = row[cval]
            if val in ("", "NA", None):
                continue
            try:
                val = float(val)
            except ValueError:
                continue
            if sub == "Fuel" and unit == "TWh" and var in EMBER_FUELS:
                gen.setdefault(canon, {}).setdefault(year, {})[var] = val
            elif sub == "Aggregate fuel" and unit == "%" and var == "Clean":
                clean.setdefault(canon, {})[year] = val
        resp.close()
        mark("Ember:yearly_electricity", bool(gen))
    except Exception as e:
        mark("Ember:yearly_electricity", False)
        sys.stderr.write(f"  electricity: {e}\n")

    history = {}     # total TWh par pays
    ranking = []
    mix = {}         # mix de la dernière année par pays
    clean_share = {}
    for canon, years in gen.items():
        tot = {y: round(sum(f.values()), 1) for y, f in years.items()}
        ys, vs = yv(tot, since=2000)
        if not ys:
            continue
        history[canon] = {"years": ys, "values": vs}
        last = ys[-1]
        ranking.append({"code": canon, "name": COUNTRIES[canon]["name"],
                        "value": tot[last], "year": last})
        mix[canon] = {k: round(v, 1) for k, v in sorted(
            years[last].items(), key=lambda kv: -kv[1])}
        if canon in clean and last in clean[canon]:
            clean_share[canon] = round(clean[canon][last], 1)
    ranking.sort(key=lambda x: -x["value"])
    return {
        "unit": "TWh (génération annuelle)",
        "ranking": ranking, "history": history, "mix": mix,
        "clean_share": clean_share, "fuels_order": EMBER_FUELS,
        "source_url": "https://ember-energy.org/data/yearly-electricity-data/",
        "source_label": "Ember · Yearly Electricity Data (génération par source)",
    }


# ──────────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "meta": {
            "updated_at": now.isoformat(),
            "updated_at_unix": int(now.timestamp()),
            "doc_version": "1.0",
            "sources_ok": ok, "sources_failed": failed,
        },
        "countries": {c: {"name": COUNTRIES[c]["name"], "iso3": COUNTRIES[c]["iso3"],
                          "color": COUNTRIES[c]["color"], "region": COUNTRIES[c]["region"]}
                      for c in COUNTRIES},
        "fret": build_fret(),
        "surf": build_surf(),
        "ip": build_ip(),
        "manuf_va": build_manuf_va(),
        "gdp": build_gdp(),
        "electricity": build_electricity(),
        "coverage": [
            "Couverture mondiale (valeur ajoutée, électricité, PIB) : ~35 pays dont Russie, "
            "Chine, Inde, Brésil, Mexique, Indonésie, Turquie, Arabie Saoudite et l'Afrique "
            "(Afrique du Sud, Nigéria, Égypte, Algérie, Maroc).",
            "Les DEUX indicateurs Jancovici (tonnes camions, m² bâtis) et l'indice de production "
            "industrielle ne sont disponibles QU'EN EUROPE (source Eurostat) : Russie, Chine, "
            "Inde, Brésil, Afrique n'ont pas de donnée libre comparable pour ces flux — ils "
            "n'apparaissent donc que dans les panneaux mondiaux (valeur ajoutée, électricité, PIB).",
            "Japon : surfaces construites en m² existantes mais derrière une clé e-Stat → absentes ici.",
            "États-Unis (m²) : reconstruit (permis × surface moyenne par logement) — approximation, "
            "non comparable EN NIVEAU au m² européen ; à lire en tendance (base 100).",
            "Fret : l'Europe publie des tonnes chargées (mesure exacte de Jancovici) ; "
            "ailleurs seules des tonnes-km existent → le récit « tonnes dans les camions » est strictement européen.",
            "Indices de production industrielle : non comparables en niveau entre pays "
            "(bases nationales différentes) → lecture en base 100 / momentum uniquement.",
        ],
    }

    # garde-fou : au moins les 2 indicateurs Jancovici doivent être présents
    if not payload["fret"]["series"] and not payload["surf"]["series"]:
        sys.stderr.write("[ECO_PHYSIQUE] aucun indicateur Jancovici — cache NON écrit\n")
        sys.exit(1)

    CACHE_FILE.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    CACHE_JS.write_text("window.__ECO_PHYSIQUE__=" +
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n")
    sys.stderr.write(f"[ECO_PHYSIQUE] OK — sources ok={len(ok)} fail={len(failed)} "
                     f"-> {CACHE_FILE.name}\n")


if __name__ == "__main__":
    main()
