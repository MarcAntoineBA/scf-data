#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache « Atlas Économique — Transition écologique ».

Page consommatrice : Atlas_Economique.html (variable window.__ATLAS_CLIMAT__),
vue plein écran « 🌍 Transition écologique » + ~20 métriques de coloration de la
carte du monde (groupe « Climat & transition » du sélecteur de métriques).

Sorties : ~/Library/Caches/site_crypto_finance/climat_cache.json
          ~/Library/Caches/site_crypto_finance/climat_cache.js
Job scf-data : `climat` (cadence daily). Logs /tmp/climat.*.log.

═══ SOURCES — toutes publiques, sans clé, réinterrogeables à la main ═══════════

  1. NASA GISS  GISTEMP v4 — anomalie de température mondiale, 1880 → présent
     https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv
     Publiée avec une base 1951-1980. On la RÉ-ÉTALONNE sur la moyenne 1880-1899
     calculée à partir de la série elle-même, pour parler en « depuis l'ère
     pré-industrielle » comme le fait le GIEC. Le décalage appliqué est écrit
     dans le cache (`global.temp.shift`) : personne n'a à nous croire sur parole.

  2. NOAA GML   CO2 atmosphérique de Mauna Loa (courbe de Keeling)
     mensuel : https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_mm_mlo.csv
     hebdo   : https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_weekly_mlo.csv
     L'hebdo donne le point le plus frais (≈ 10 jours de décalage).

  3. ND-GAIN    Notre Dame Global Adaptation Initiative — Country Index
     https://gain.nd.edu/our-work/country-index/download-data/
     LA référence de place pour « quels pays encaissent le choc ». 192 pays,
     1995 → 2024, deux axes : vulnérabilité (subir) et préparation (encaisser).
     La vulnérabilité se décompose en 6 secteurs (eau, alimentation, santé,
     écosystèmes, habitat, infrastructures), la préparation en 3 (économique,
     gouvernance, social) — d'où le radar par pays de la vue.
     ⚠ L'URL du zip porte un identifiant d'actif qui CHANGE à chaque millésime.
     On lit donc la page de téléchargement pour en extraire le lien courant, et
     on ne retombe sur la constante que si la page a changé de forme.

  4. OWID       Our World in Data — plusieurs jeux, tous en CSV nu :
     - owid-co2-data.csv (Global Carbon Budget) : émissions territoriales et de
       consommation, cumulées, par habitant, par unité de PIB, par combustible.
     - annual-temperature-anomalies : réchauffement OBSERVÉ pays par pays
       (Copernicus ERA5, base 1991-2020). C'est la carte « qui chauffe ».
     - electricity-prod-source-stacked : production électrique par source (TWh),
       d'où les parts solaire+éolien / charbon / bas-carbone.
     - electric-car-sales-share, levelized-cost-of-energy, solar-pv-prices,
       installed-solar-pv-capacity, cumulative-installed-wind-energy-...,
       damage-costs-from-natural-disasters, number-of-natural-disaster-events,
       emissions-weighted-carbon-price.

  5. Banque mondiale (API v2, sans clé)
     - EN.POP.EL5M.ZS : population vivant sous 5 m d'altitude (% du total)
     - ER.H2O.FWST.ZS : stress hydrique (prélèvements / ressources disponibles)

  6. hydro_cache.json (produit par fetch_hydrocarbures.py, LU EN LOCAL, jamais
     téléchargé) : réserves prouvées de pétrole (Gbbl) et de gaz (bcm) par pays,
     converties en CO2 « déjà inscrit dans les bilans ». Facteurs d'émission de
     l'EPA (Greenhouse Gas Equivalencies) :
        1 baril de pétrole  → 0,43 tCO2
        1 000 m3 de gaz     → 1,95 tCO2   (0,0551 tCO2/Mcf, 1 Mcf = 28,317 m3)
     Si le fichier est absent, la métrique est simplement OMISE — jamais estimée.

═══ CE QUE CE CACHE NE FAIT PAS ═══════════════════════════════════════════════
  · Aucune projection, aucun scénario, aucun modèle : que de l'observé publié.
  · Aucune valeur n'est interpolée ni reportée d'un pays sur un autre.
  · Chaque valeur `latest` porte sa source ET son millésime. Une valeur sans
    source ne doit jamais entrer ici.

Interpréteur : n'importe quel python3 ≥ 3.8 (stdlib seule).
"""
import csv
import io
import json
import re
import ssl
import sys
import time
import zipfile
import datetime as dt
from pathlib import Path
from urllib import request
from urllib.error import URLError, HTTPError

# ── Contact nominatif : plusieurs de ces serveurs (NOAA, NASA) répondent 403 à
#    un agent anonyme, et c'est leur droit. On se nomme.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36 "
      "SiteCryptoFinance/1.0 (+marcantoine.bassetti@gmail.com)")

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "climat_cache.json"
OUT_JS = CACHE_DIR / "climat_cache.js"
HYDRO_JSON = CACHE_DIR / "hydro_cache.json"

DRY = "--dry-run" in sys.argv
CTX = ssl.create_default_context()

# ── Budget carbone restant : ANCRE PUBLIÉE, décrémentée par les émissions
#    observées depuis. Les deux moitiés du calcul sont exposées dans le cache,
#    donc vérifiables. Source : Forster et al., « Indicators of Global Climate
#    Change 2024 », Earth System Science Data (mise à jour annuelle du GIEC AR6).
#    Ce sont des budgets pour 50 % de chance de tenir la cible.
BUDGET_ANCHOR_YEAR = 2025          # budgets exprimés au 1er janvier de cette année
BUDGET_15C_GT = 130.0              # GtCO2 pour +1,5 °C (50 %)
BUDGET_20C_GT = 1050.0             # GtCO2 pour +2,0 °C (50 %)
BUDGET_SRC = "Forster et al., Indicators of Global Climate Change (ESSD)"
BUDGET_URL = "https://essd.copernicus.org/articles/17/2641/2025/"

# ── Facteurs d'émission (EPA, Greenhouse Gas Equivalencies Calculator) ────────
TCO2_PER_BBL = 0.43                # tCO2 par baril de pétrole brut
TCO2_PER_1000M3_GAS = 1.95         # tCO2 par 1 000 m3 de gaz naturel
EPA_URL = "https://www.epa.gov/energy/greenhouse-gases-equivalencies-calculator-calculations-and-references"

NDGAIN_PAGE = "https://gain.nd.edu/our-work/country-index/download-data/"
NDGAIN_FALLBACK = "https://gain.nd.edu/assets/647440/ndgain_countryindex_2026.zip"

OWID_CO2 = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"
GISTEMP = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv"
MLO_M = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_mm_mlo.csv"
MLO_W = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_weekly_mlo.csv"

ERRORS = []      # sources tombées : listées dans le cache ET sur stdout


def log(msg):
    print(msg, flush=True)


def fail(source, exc):
    """Une source qui tombe n'emporte pas le run : elle est consignée."""
    ERRORS.append({"source": source, "err": f"{type(exc).__name__}: {exc}"})
    log(f"  ⚠ {source} : {type(exc).__name__}: {exc}")


def http(url, tries=4, timeout=90):
    last = None
    for i in range(tries):
        try:
            rq = request.Request(url, headers={
                "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"})
            with request.urlopen(rq, timeout=timeout, context=CTX) as r:
                return r.read()
        except (URLError, HTTPError, ssl.SSLError, TimeoutError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"échec après {tries} essais : {last}")


def http_text(url, **kw):
    return http(url, **kw).decode("utf-8", "replace")


def num(x):
    """Chaîne → float, ou None. Tolère '', '***', 'NA', les espaces."""
    if x is None:
        return None
    s = str(x).strip()
    if not s or s in ("***", "NA", "N/A", "-", "nan", "NaN"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if v != v else v          # écarte les NaN


def pack(series):
    """{année: valeur} → {'s': première année, 'v': [...]} avec des trous à None.

    Format identique aux séries packées de l'Atlas : le front sait déjà les lire.
    """
    ys = sorted(k for k, v in series.items() if v is not None)
    if not ys:
        return None
    s, e = ys[0], ys[-1]
    return {"s": s, "v": [series.get(y) for y in range(s, e + 1)]}


def entry(v, unit, src, date, note=None):
    """Une valeur `latest` complète. Sans source ni millésime, on n'écrit rien."""
    if v is None:
        return None
    d = {"v": round(v, 6) if isinstance(v, float) else v, "u": unit, "src": src, "d": str(date)}
    if note:
        d["n"] = note
    return d


# ═════════════════════════════════════════════════════════════════════════════
#  1. NASA GISTEMP — anomalie de température mondiale
# ═════════════════════════════════════════════════════════════════════════════
def get_gistemp():
    txt = http_text(GISTEMP)
    lines = [l for l in txt.split("\n") if l.strip()]
    # ligne 0 = titre « Land-Ocean: Global Means », ligne 1 = en-têtes
    hdr = [h.strip() for h in lines[1].split(",")]
    i_year, i_jd = hdr.index("Year"), hdr.index("J-D")
    raw = {}
    for l in lines[2:]:
        c = l.split(",")
        if len(c) <= i_jd:
            continue
        y, v = num(c[i_year]), num(c[i_jd])
        if y is not None and v is not None:
            raw[int(y)] = v
    if len(raw) < 100:
        raise RuntimeError(f"série trop courte ({len(raw)} ans)")
    # Ré-étalonnage pré-industriel : moyenne 1880-1899 de la série elle-même.
    base = [raw[y] for y in range(1880, 1900) if y in raw]
    shift = sum(base) / len(base) if base else 0.0
    ser = {y: round(v - shift, 4) for y, v in raw.items()}
    p = pack(ser)
    p.update({
        "src": "NASA GISS · GISTEMP v4", "url": GISTEMP,
        "base": "1880-1899 (recalculée depuis la série)",
        "orig_base": "1951-1980",
        "shift": round(shift, 4),
        "unit": "°C",
    })
    last = max(ser)
    log(f"  GISTEMP : {p['s']}→{last}, dernière anomalie {ser[last]:+.2f} °C "
        f"(décalage pré-industriel appliqué {shift:+.3f})")
    return p


# ═════════════════════════════════════════════════════════════════════════════
#  2. NOAA Mauna Loa — CO2 atmosphérique
# ═════════════════════════════════════════════════════════════════════════════
def get_mauna_loa():
    txt = http_text(MLO_M)
    rows = [l for l in txt.split("\n") if l.strip() and not l.startswith("#")]
    hdr = [h.strip() for h in rows[0].split(",")]
    try:
        i_y, i_m = hdr.index("year"), hdr.index("month")
        i_avg = hdr.index("average")
        i_tr = hdr.index("deseasonalized") if "deseasonalized" in hdr else None
    except ValueError as e:
        raise RuntimeError(f"en-têtes inattendus : {hdr}") from e
    t, v, trend = [], [], []
    for l in rows[1:]:
        c = l.split(",")
        if len(c) <= i_avg:
            continue
        y, m, a = num(c[i_y]), num(c[i_m]), num(c[i_avg])
        if None in (y, m, a) or a < 0:
            continue
        t.append(f"{int(y):04d}-{int(m):02d}")
        v.append(round(a, 2))
        tv = num(c[i_tr]) if i_tr is not None and len(c) > i_tr else None
        trend.append(round(tv, 2) if tv is not None and tv > 0 else None)
    if len(v) < 500:
        raise RuntimeError(f"série trop courte ({len(v)} mois)")
    out = {"t": t, "v": v, "trend": trend, "unit": "ppm",
           "src": "NOAA GML · Mauna Loa (courbe de Keeling)", "url": MLO_M}
    # Point le plus frais : la série hebdomadaire (≈ 10 jours de décalage).
    try:
        wt = http_text(MLO_W)
        wrows = [l for l in wt.split("\n") if l.strip() and not l.startswith("#")]
        whdr = [h.strip() for h in wrows[0].split(",")]
        iy, im, idd = whdr.index("year"), whdr.index("month"), whdr.index("day")
        iav = whdr.index("average")
        i1y = whdr.index("1 year ago") if "1 year ago" in whdr else None
        for l in reversed(wrows[1:]):
            c = l.split(",")
            if len(c) <= iav:
                continue
            a = num(c[iav])
            if a is None or a < 0:
                continue
            ago = num(c[i1y]) if i1y is not None and len(c) > i1y else None
            out["last"] = {
                "v": round(a, 2),
                "d": f"{int(num(c[iy])):04d}-{int(num(c[im])):02d}-{int(num(c[idd])):02d}",
                "yoy": round(a - ago, 2) if ago and ago > 0 else None,
                "src": "NOAA GML · moyenne hebdomadaire", "url": MLO_W,
            }
            break
    except Exception as e:                                   # noqa: BLE001
        fail("NOAA Mauna Loa (hebdo)", e)
    lastv = out.get("last", {}).get("v") or v[-1]
    log(f"  Mauna Loa : {len(v)} mois, {t[0]}→{t[-1]}, dernier point {lastv} ppm")
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  3. ND-GAIN — vulnérabilité & préparation
# ═════════════════════════════════════════════════════════════════════════════
NDG_FILES = {
    # clé de sortie          chemin dans le zip                    libellé
    "ndgain":     ("gain/gain.csv",                        "indice ND-GAIN"),
    "nd_vuln":    ("vulnerability/vulnerability.csv",      "vulnérabilité"),
    "nd_ready":   ("readiness/readiness.csv",              "préparation"),
    "nd_v_food":  ("vulnerability/food.csv",               "vuln. alimentation"),
    "nd_v_water": ("vulnerability/water.csv",              "vuln. eau"),
    "nd_v_health": ("vulnerability/health.csv",            "vuln. santé"),
    "nd_v_eco":   ("vulnerability/ecosystems.csv",         "vuln. écosystèmes"),
    "nd_v_habitat": ("vulnerability/habitat.csv",          "vuln. habitat humain"),
    "nd_v_infra": ("vulnerability/infrastructure.csv",     "vuln. infrastructures"),
    "nd_v_expo":  ("vulnerability/exposure.csv",           "exposition physique"),
    "nd_v_sens":  ("vulnerability/sensitivity.csv",        "sensibilité"),
    "nd_v_cap":   ("vulnerability/capacity.csv",           "capacité d'adaptation"),
    "nd_r_econ":  ("readiness/economic.csv",               "prépa. économique"),
    "nd_r_gov":   ("readiness/governance.csv",             "prépa. gouvernance"),
    "nd_r_soc":   ("readiness/social.csv",                 "prépa. sociale"),
}
# Séries longues conservées (les autres n'ont que leur dernière valeur : le
# cache doit rester sous le mégaoctet, et 15 séries × 30 ans × 192 pays ne le
# permettent pas).
NDG_HIST = ("ndgain", "nd_vuln", "nd_ready")


def ndgain_zip_url():
    """L'identifiant d'actif change à chaque millésime : on lit la page."""
    try:
        html = http_text(NDGAIN_PAGE, timeout=60)
        m = re.findall(r'href="([^"]*ndgain[^"]*\.zip)"', html, re.I)
        if m:
            u = m[0]
            if u.startswith("/"):
                u = "https://gain.nd.edu" + u
            log(f"  ND-GAIN : lien courant trouvé sur la page → {u}")
            return u
    except Exception as e:                                   # noqa: BLE001
        fail("ND-GAIN (page de téléchargement)", e)
    log(f"  ND-GAIN : repli sur la constante → {NDGAIN_FALLBACK}")
    return NDGAIN_FALLBACK


def get_ndgain():
    url = ndgain_zip_url()
    z = zipfile.ZipFile(io.BytesIO(http(url, timeout=180)))
    names = [n for n in z.namelist() if not n.startswith("__MACOSX") and n.endswith(".csv")]

    def find(suffix):
        hits = [n for n in names if n.replace("\\", "/").endswith("/" + suffix)]
        return hits[0] if hits else None

    out, years_seen = {}, set()
    for key, (suffix, lab) in NDG_FILES.items():
        path = find(suffix)
        if not path:
            fail(f"ND-GAIN {suffix}", FileNotFoundError("absent du zip"))
            continue
        rows = list(csv.reader(io.StringIO(z.read(path).decode("utf-8", "replace"))))
        if not rows:
            continue
        hdr = rows[0]
        yr_cols = [(i, int(h)) for i, h in enumerate(hdr) if re.fullmatch(r"\d{4}", h.strip())]
        if not yr_cols:
            continue
        years_seen.update(y for _, y in yr_cols)
        for r in rows[1:]:
            if len(r) < 2 or not r[0].strip():
                continue
            a3 = r[0].strip().upper()
            ser = {}
            for i, y in yr_cols:
                if i < len(r):
                    v = num(r[i])
                    if v is not None:
                        ser[y] = round(v, 4)
            if not ser:
                continue
            d = out.setdefault(a3, {})
            ly = max(ser)
            d[key] = {"v": ser[ly], "y": ly, "lab": lab}
            if key in NDG_HIST:
                d[key + "_h"] = pack(ser)
    log(f"  ND-GAIN : {len(out)} pays, {len(NDG_FILES)} indices, "
        f"millésime {max(years_seen) if years_seen else '?'}")
    return out, (max(years_seen) if years_seen else None), url


# ═════════════════════════════════════════════════════════════════════════════
#  4. OWID — CO2 (Global Carbon Budget) et graphers thématiques
# ═════════════════════════════════════════════════════════════════════════════
OWID_COLS = {
    "co2_tot":      "co2",
    "co2_pc":       "co2_per_capita",
    "co2_cum":      "cumulative_co2",
    "co2_share":    "share_global_co2",
    "co2_cum_share": "share_global_cumulative_co2",
    "co2_cons_pc":  "consumption_co2_per_capita",
    "co2_gdp":      "co2_per_gdp",
    "co2_coal":     "coal_co2",
    "co2_oil":      "oil_co2",
    "co2_gas":      "gas_co2",
    "temp_contrib": "temperature_change_from_co2",
    "ghg_pc":       "total_ghg",
}


def get_owid_co2():
    """14 Mo de CSV, lus en flux. Renvoie (par-pays, série mondiale)."""
    txt = http_text(OWID_CO2, timeout=240)
    rd = csv.DictReader(io.StringIO(txt))
    per, world, hist_pc = {}, {}, {}
    for row in rd:
        iso = (row.get("iso_code") or "").strip()
        y = num(row.get("year"))
        if y is None:
            continue
        y = int(y)
        # ⚠ 2026 : les agrégats d'OWID (World, continents, catégories de revenu)
        # ont un `iso_code` VIDE — ils portaient `OWID_WRL` auparavant. On teste
        # donc le nom, en gardant l'ancien code en repli.
        if (row.get("country") or "").strip() == "World" or iso == "OWID_WRL":
            w = world.setdefault(y, {})
            for k in ("co2", "coal_co2", "oil_co2", "gas_co2", "cement_co2",
                      "flaring_co2", "other_industry_co2", "consumption_co2"):
                v = num(row.get(k))
                if v is not None:
                    w[k] = round(v, 3)
            continue
        if len(iso) != 3 or not iso.isalpha():
            continue                       # agrégats OWID_* et régions : écartés
        d = per.setdefault(iso, {})
        for key, col in OWID_COLS.items():
            v = num(row.get(col))
            if v is not None:
                d[key] = {"v": round(v, 4), "y": y}      # écrasé → reste le dernier
        v = num(row.get("co2_per_capita"))
        if v is not None:
            hist_pc.setdefault(iso, {})[y] = round(v, 3)
    for iso, ser in hist_pc.items():
        p = pack(ser)
        if p:
            per[iso]["co2_pc_h"] = p
    # Tendance des émissions sur 10 ans (%/an composé) — calculée, pas importée.
    for iso, d in per.items():
        h = hist_pc.get(iso) or {}
        ys = sorted(h)
        if len(ys) >= 11:
            y1 = ys[-1]
            y0 = y1 - 10
            if y0 in h and h[y0] and h[y0] > 0 and h[y1] is not None:
                cagr = ((h[y1] / h[y0]) ** 0.1 - 1) * 100
                d["co2_trend10"] = {"v": round(cagr, 3), "y": y1}
    # Une série mondiale absente ne doit PAS emporter les 200 séries par pays :
    # ce log a déjà fait tomber toute la source une fois (min() sur un dict vide).
    span = f"{min(world)}→{max(world)}" if world else "ABSENTE"
    log(f"  OWID CO2 : {len(per)} pays · série mondiale {span}")
    return per, world


def owid_grapher(slug, timeout=90):
    url = f"https://ourworldindata.org/grapher/{slug}.csv?csvType=full"
    return list(csv.DictReader(io.StringIO(http_text(url, timeout=timeout)))), url


def get_elec_mix():
    """Production électrique par source (TWh) → parts par pays + mix mondial."""
    rows, url = owid_grapher("electricity-prod-source-stacked", timeout=150)
    SRCS = ["Other renewables", "Bioenergy", "Solar", "Wind", "Hydropower",
            "Nuclear", "Gas", "Oil", "Coal"]
    LOWC = ["Other renewables", "Bioenergy", "Solar", "Wind", "Hydropower", "Nuclear"]
    per, world = {}, {}
    for r in rows:
        iso = (r.get("Code") or "").strip()
        y = num(r.get("Year"))
        if y is None:
            continue
        y = int(y)
        vals = {s: (num(r.get(s)) or 0.0) for s in SRCS}
        tot = sum(vals.values())
        if r.get("Entity") == "World":
            world[y] = {s: round(vals[s], 2) for s in SRCS}
            continue
        if len(iso) != 3 or not iso.isalpha() or tot <= 0:
            continue
        per.setdefault(iso, {})[y] = {
            "sw": round((vals["Solar"] + vals["Wind"]) / tot * 100, 3),
            "coal": round(vals["Coal"] / tot * 100, 3),
            "low": round(sum(vals[s] for s in LOWC) / tot * 100, 3),
            "tot": round(tot, 2),
        }
    out = {}
    for iso, byyear in per.items():
        ly = max(byyear)
        out[iso] = {"y": ly, **byyear[ly],
                    "sw_h": pack({y: v["sw"] for y, v in byyear.items()})}
    log(f"  OWID mix électrique : {len(out)} pays · monde {min(world)}→{max(world)}")
    return out, world, url


def get_wb(indicator, label):
    """Banque mondiale, dernière valeur non vide de chaque pays (mrnev=1)."""
    url = (f"https://api.worldbank.org/v2/country/all/indicator/{indicator}"
           f"?format=json&per_page=20000&mrnev=1")
    j = json.loads(http_text(url, timeout=90))
    if not isinstance(j, list) or len(j) < 2 or not j[1]:
        raise RuntimeError("réponse vide")
    out = {}
    for r in j[1]:
        a3 = (r.get("countryiso3code") or "").strip().upper()
        v = num(r.get("value"))
        if len(a3) == 3 and v is not None:
            out[a3] = {"v": round(v, 4), "y": r.get("date")}
    log(f"  BM {indicator} ({label}) : {len(out)} pays")
    return out, url


def get_simple_grapher(slug, col=None, world_only=False, entity=None):
    """Un grapher OWID à une colonne → {iso3: {v, y}} ou {année: valeur}."""
    rows, url = owid_grapher(slug)
    if not rows:
        raise RuntimeError("CSV vide")
    if col is None:
        keys = [k for k in rows[0].keys() if k not in ("Entity", "Code", "Year", "Month")]
        if not keys:
            raise RuntimeError(f"aucune colonne de valeur : {list(rows[0].keys())}")
        col = keys[0]
    if world_only or entity:
        want = entity or "World"
        ser = {}
        for r in rows:
            if (r.get("Entity") or "").strip() != want:
                continue
            y, v = num(r.get("Year")), num(r.get(col))
            if y is not None and v is not None:
                ser[int(y)] = round(v, 6)
        p = pack(ser)
        if p:
            p["url"] = url
        log(f"  OWID {slug} [{want}] : {len(ser)} points")
        return p
    out = {}
    for r in rows:
        iso = (r.get("Code") or "").strip()
        y, v = num(r.get("Year")), num(r.get(col))
        if len(iso) == 3 and iso.isalpha() and y is not None and v is not None:
            prev = out.get(iso)
            if not prev or int(y) >= prev["y"]:
                out[iso] = {"v": round(v, 4), "y": int(y)}
    log(f"  OWID {slug} : {len(out)} pays")
    return out, url


def get_multi_grapher(slug, entity="World"):
    """Un grapher à plusieurs colonnes, restreint à une entité → {col: packé}."""
    rows, url = owid_grapher(slug)
    if not rows:
        raise RuntimeError("CSV vide")
    cols = [k for k in rows[0].keys() if k not in ("Entity", "Code", "Year", "Month")]
    acc = {c: {} for c in cols}
    for r in rows:
        if (r.get("Entity") or "").strip() != entity:
            continue
        y = num(r.get("Year"))
        if y is None:
            continue
        for c in cols:
            v = num(r.get(c))
            if v is not None:
                acc[c][int(y)] = round(v, 6)
    out = {c: pack(s) for c, s in acc.items() if s}
    out["url"] = url
    log(f"  OWID {slug} [{entity}] : {len([k for k in out if k != 'url'])} séries")
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  5. Carbone verrouillé dans les réserves prouvées (lecture LOCALE)
# ═════════════════════════════════════════════════════════════════════════════
def get_carbon_locked():
    if not HYDRO_JSON.exists():
        log("  carbone verrouillé : hydro_cache.json absent → métrique OMISE")
        return {}, None
    try:
        h = json.loads(HYDRO_JSON.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        fail("hydro_cache.json (lecture)", e)
        return {}, None
    out, tot_oil, tot_gas = {}, 0.0, 0.0
    for a3, c in (h.get("countries") or {}).items():
        lat = c.get("latest") or {}
        o, g = lat.get("oil_res"), lat.get("gas_res")
        gt_o = gt_g = None
        if o and o.get("v") is not None and o.get("u") == "Gbbl":
            gt_o = o["v"] * TCO2_PER_BBL                     # Gbbl × t/bbl = GtCO2
            tot_oil += gt_o
        if g and g.get("v") is not None and g.get("u") == "bcm":
            gt_g = g["v"] * TCO2_PER_1000M3_GAS / 1000.0     # bcm × t/1000m3 ÷ 1000
            tot_gas += gt_g
        if gt_o is None and gt_g is None:
            continue
        d = (o or g or {}).get("d", "?")
        out[a3] = {
            "v": round((gt_o or 0) + (gt_g or 0), 4),
            "oil": round(gt_o, 4) if gt_o is not None else None,
            "gas": round(gt_g, 4) if gt_g is not None else None,
            "y": d,
        }
    log(f"  carbone verrouillé : {len(out)} pays · pétrole {tot_oil:.0f} + "
        f"gaz {tot_gas:.0f} = {tot_oil + tot_gas:.0f} GtCO2")
    return out, {"oil": round(tot_oil, 2), "gas": round(tot_gas, 2),
                 "total": round(tot_oil + tot_gas, 2),
                 "f_oil": TCO2_PER_BBL, "f_gas": TCO2_PER_1000M3_GAS,
                 "src": "réserves prouvées CIA Factbook (via hydro_cache) × facteurs EPA",
                 "url": EPA_URL,
                 "n": "hors charbon : aucune de nos sources ne publie les réserves "
                      "prouvées de charbon par pays. Le total réel est donc "
                      "nettement supérieur."}


# ═════════════════════════════════════════════════════════════════════════════
#  Assemblage
# ═════════════════════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    log("[climat] collecte…")
    G, C = {}, {}          # global, countries
    srcs = {}

    def put(a3, key, val):
        if val is None:
            return
        C.setdefault(a3, {}).setdefault("latest", {})[key] = val

    def puth(a3, key, packed):
        if packed:
            C.setdefault(a3, {}).setdefault("hist", {})[key] = packed

    # ── 1. Température mondiale ──────────────────────────────────────────────
    log("· NASA GISTEMP")
    try:
        G["temp"] = get_gistemp()
        srcs["GISTEMP"] = {"lab": "NASA GISS · GISTEMP v4", "url": GISTEMP}
    except Exception as e:                                   # noqa: BLE001
        fail("NASA GISTEMP", e)

    # ── 2. CO2 atmosphérique ─────────────────────────────────────────────────
    log("· NOAA Mauna Loa")
    try:
        G["co2ppm"] = get_mauna_loa()
        srcs["NOAA"] = {"lab": "NOAA GML · Mauna Loa", "url": MLO_M}
    except Exception as e:                                   # noqa: BLE001
        fail("NOAA Mauna Loa", e)

    # ── 3. ND-GAIN ───────────────────────────────────────────────────────────
    log("· ND-GAIN")
    ndg_year = None
    try:
        ndg, ndg_year, ndg_url = get_ndgain()
        srcs["NDGAIN"] = {"lab": f"ND-GAIN Country Index {ndg_year or ''}".strip(),
                          "url": NDGAIN_PAGE, "zip": ndg_url}
        for a3, d in ndg.items():
            for key, (_, lab) in NDG_FILES.items():
                e = d.get(key)
                if e:
                    put(a3, key, entry(e["v"], "score", "ND-GAIN", e["y"]))
                h = d.get(key + "_h")
                if h:
                    puth(a3, key, h)
    except Exception as e:                                   # noqa: BLE001
        fail("ND-GAIN", e)

    # ── 4. OWID CO2 (Global Carbon Budget) ───────────────────────────────────
    log("· OWID — Global Carbon Budget")
    try:
        per, world = get_owid_co2()
        srcs["GCB"] = {"lab": "Global Carbon Budget (via Our World in Data)",
                       "url": "https://github.com/owid/co2-data"}
        UNITS = {"co2_tot": "MtCO₂", "co2_pc": "tCO₂/hab", "co2_cum": "MtCO₂",
                 "co2_share": "%", "co2_cum_share": "%", "co2_cons_pc": "tCO₂/hab",
                 "co2_gdp": "kgCO₂/$", "co2_coal": "MtCO₂", "co2_oil": "MtCO₂",
                 "co2_gas": "MtCO₂", "temp_contrib": "°C", "ghg_pc": "MtCO₂e",
                 "co2_trend10": "%/an"}
        for a3, d in per.items():
            for key, e in d.items():
                if key.endswith("_h"):
                    puth(a3, key[:-2], e)
                    continue
                put(a3, key, entry(e["v"], UNITS.get(key, ""), "GCB/OWID", e["y"]))
    except Exception as e:                                   # noqa: BLE001
        fail("OWID CO2 (par pays)", e)
        world = {}
    # Série mondiale : bloc SÉPARÉ. Elle a déjà emporté les 200 séries par pays
    # une fois (les agrégats OWID ont perdu leur iso_code en 2026) — plus jamais.
    try:
        ys = sorted(world)
        if not ys:
            raise RuntimeError("série mondiale d'émissions absente du CSV OWID")
        rng = range(ys[0], ys[-1] + 1)
        G["emis"] = {
            "s": ys[0],
            "total": [world.get(y, {}).get("co2") for y in rng],
            "coal": [world.get(y, {}).get("coal_co2") for y in rng],
            "oil": [world.get(y, {}).get("oil_co2") for y in rng],
            "gas": [world.get(y, {}).get("gas_co2") for y in rng],
            "cement": [world.get(y, {}).get("cement_co2") for y in rng],
            "flaring": [world.get(y, {}).get("flaring_co2") for y in rng],
            "cons": [world.get(y, {}).get("consumption_co2") for y in rng],
            "unit": "MtCO₂", "src": "Global Carbon Budget (via OWID)",
            "url": "https://github.com/owid/co2-data",
        }
    except Exception as e:                                   # noqa: BLE001
        fail("OWID CO2 (série mondiale)", e)

    # ── 5. Réchauffement OBSERVÉ par pays ────────────────────────────────────
    log("· OWID — réchauffement observé par pays")
    try:
        warm, url = get_simple_grapher("annual-temperature-anomalies")
        srcs["ERA5"] = {"lab": "Copernicus ERA5 (via OWID) · base 1991-2020", "url": url}
        for a3, e in warm.items():
            put(a3, "warming", entry(e["v"], "°C", "ERA5/OWID", e["y"],
                                     "écart à la moyenne 1991-2020"))
    except Exception as e:                                   # noqa: BLE001
        fail("OWID réchauffement par pays", e)

    # ── 6. Mix électrique ────────────────────────────────────────────────────
    log("· OWID — mix électrique")
    try:
        mix, wmix, url = get_elec_mix()
        srcs["EMBER"] = {"lab": "Ember & Energy Institute (via OWID)", "url": url}
        for a3, d in mix.items():
            put(a3, "solar_wind", entry(d["sw"], "%", "Ember/OWID", d["y"]))
            put(a3, "coal_elec", entry(d["coal"], "%", "Ember/OWID", d["y"]))
            put(a3, "lowcarb_elec", entry(d["low"], "%", "Ember/OWID", d["y"]))
            puth(a3, "solar_wind", d.get("sw_h"))
        ys = sorted(wmix)
        G["wmix"] = {"s": ys[0], "unit": "TWh", "src": "Ember/EI via OWID", "url": url,
                     "series": {k: [wmix.get(y, {}).get(k) for y in range(ys[0], ys[-1] + 1)]
                                for k in ("Solar", "Wind", "Hydropower", "Nuclear",
                                          "Bioenergy", "Other renewables",
                                          "Gas", "Oil", "Coal")}}
    except Exception as e:                                   # noqa: BLE001
        fail("OWID mix électrique", e)

    # ── 7. Banque mondiale : exposition côtière & stress hydrique ────────────
    log("· Banque mondiale")
    for ind, key, unit, lab in (
        ("EN.POP.EL5M.ZS", "low_elev", "%", "population sous 5 m"),
        ("ER.H2O.FWST.ZS", "water_stress", "%", "stress hydrique"),
    ):
        try:
            d, url = get_wb(ind, lab)
            srcs["WB_" + key] = {"lab": f"Banque mondiale · {ind}", "url":
                                 f"https://data.worldbank.org/indicator/{ind}"}
            for a3, e in d.items():
                put(a3, key, entry(e["v"], unit, "Banque mondiale", e["y"]))
        except Exception as e:                               # noqa: BLE001
            fail(f"Banque mondiale {ind}", e)

    # ── 8. Part des VE dans les ventes ───────────────────────────────────────
    log("· OWID — véhicules électriques")
    try:
        ev, url = get_simple_grapher("electric-car-sales-share")
        srcs["IEA_EV"] = {"lab": "AIE — Global EV Outlook (via OWID)", "url": url}
        for a3, e in ev.items():
            put(a3, "ev_share", entry(e["v"], "%", "AIE/OWID", e["y"]))
        G["ev_world"] = get_simple_grapher("electric-car-sales-share", world_only=True)
    except Exception as e:                                   # noqa: BLE001
        fail("OWID part des VE", e)

    # ── 9. Prix du carbone ───────────────────────────────────────────────────
    log("· OWID — prix du carbone")
    try:
        cp, url = get_simple_grapher("emissions-weighted-carbon-price")
        srcs["CPRICE"] = {"lab": "Banque mondiale — State & Trends of Carbon Pricing "
                                 "(via OWID)", "url": url}
        for a3, e in cp.items():
            put(a3, "carbon_price", entry(e["v"], "$/tCO₂", "BM/OWID", e["y"]))
        G["cprice_world"] = get_simple_grapher("emissions-weighted-carbon-price",
                                               world_only=True)
    except Exception as e:                                   # noqa: BLE001
        fail("OWID prix du carbone", e)

    # ── 10. Coûts de la transition (courbe d'apprentissage) ──────────────────
    log("· OWID — coûts")
    for slug, key, ent in (("levelized-cost-of-energy", "lcoe", "World"),
                           ("solar-pv-prices", "pv_price", "World"),
                           ("installed-solar-pv-capacity", "solar_cap", "World"),
                           ("cumulative-installed-wind-energy-capacity-gigawatts",
                            "wind_cap", "World")):
        try:
            G[key] = get_multi_grapher(slug, entity=ent)
        except Exception as e:                               # noqa: BLE001
            fail(f"OWID {slug}", e)

    # ── 11. Catastrophes naturelles ──────────────────────────────────────────
    log("· OWID — catastrophes")
    for slug, key, ent in (("damage-costs-from-natural-disasters", "dis_dmg", "All disasters"),
                           ("number-of-natural-disaster-events", "dis_n", "All disasters")):
        try:
            G[key] = get_multi_grapher(slug, entity=ent)
        except Exception as e:                               # noqa: BLE001
            fail(f"OWID {slug}", e)

    # ── 12. Carbone verrouillé dans les réserves ─────────────────────────────
    log("· carbone verrouillé (réserves prouvées)")
    locked, locked_tot = get_carbon_locked()
    for a3, e in locked.items():
        v = entry(e["v"], "GtCO₂", "Factbook × EPA", e["y"],
                  "réserves prouvées converties en CO₂")
        if v:
            v["oil"] = e["oil"]
            v["gas"] = e["gas"]
        put(a3, "carbon_locked", v)
    if locked_tot:
        G["locked"] = locked_tot
        srcs["LOCKED"] = {"lab": "Réserves prouvées × facteurs d'émission EPA",
                          "url": EPA_URL}

    # ── 13. Budget carbone restant ───────────────────────────────────────────
    #  On part de l'ancre publiée et on retire les émissions RÉELLEMENT observées
    #  depuis. Les deux termes sont dans le cache : le calcul est refaisable.
    try:
        em = G.get("emis")
        rate = spent = None
        if em and em.get("total"):
            tot = em["total"]
            years = [em["s"] + i for i in range(len(tot))]
            vals = {y: v for y, v in zip(years, tot) if v is not None}
            last_y = max(vals)
            rate = vals[last_y] / 1000.0                     # MtCO2 → GtCO2 / an
            spent = sum(v for y, v in vals.items() if y >= BUDGET_ANCHOR_YEAR) / 1000.0
            rem15 = BUDGET_15C_GT - spent
            rem20 = BUDGET_20C_GT - spent
            G["budget"] = {
                "anchor_y": BUDGET_ANCHOR_YEAR,
                "b15": BUDGET_15C_GT, "b20": BUDGET_20C_GT,
                "spent_since": round(spent, 2),
                "rem15": round(rem15, 2), "rem20": round(rem20, 2),
                "rate": round(rate, 3), "rate_y": last_y,
                "yrs15": round(rem15 / rate, 2) if rate else None,
                "yrs20": round(rem20 / rate, 2) if rate else None,
                "src": BUDGET_SRC, "url": BUDGET_URL,
                "n": "budgets pour 50 % de chance de tenir la cible, au 1er janvier "
                     f"{BUDGET_ANCHOR_YEAR}, diminués des émissions observées depuis "
                     "(Global Carbon Budget). Hors rétroactions non linéaires.",
            }
            log(f"  budget carbone : {rem15:.0f} GtCO2 restants pour +1,5 °C "
                f"au rythme de {rate:.1f} Gt/an → {rem15 / rate:.1f} ans")
    except Exception as e:                                   # noqa: BLE001
        fail("budget carbone", e)

    # ── Garde-fou anti-écrasement ────────────────────────────────────────────
    n_ndg = sum(1 for d in C.values() if "nd_vuln" in (d.get("latest") or {}))
    n_co2 = sum(1 for d in C.values() if "co2_pc" in (d.get("latest") or {}))
    log(f"\n[climat] {len(C)} pays · ND-GAIN {n_ndg} · CO2 {n_co2} · "
        f"{len(ERRORS)} source(s) en échec")
    if (n_ndg < 100 or n_co2 < 100) and OUT_JSON.exists():
        log("[climat] ABANDON : collecte trop pauvre et un cache valide existe. "
            "Rien n'est écrit (on ne remplace jamais du bon par du vide).")
        for e in ERRORS:
            log(f"   · {e['source']} → {e['err']}")
        sys.exit(1)
    if n_ndg < 50 or n_co2 < 50:
        log("[climat] ABANDON : collecte trop pauvre, aucun cache existant.")
        sys.exit(2)

    payload = {
        "meta": {
            "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "version": 1,
            "ndgain_year": ndg_year,
            "n_countries": len(C),
            "sources": srcs,
            "errors": ERRORS,
            "method": {
                "temp": "GISTEMP ré-étalonnée sur 1880-1899 (décalage écrit dans "
                        "global.temp.shift).",
                "budget": "ancre publiée moins les émissions observées depuis.",
                "locked": f"réserves prouvées × {TCO2_PER_BBL} tCO₂/baril et "
                          f"{TCO2_PER_1000M3_GAS} tCO₂/1 000 m³ (EPA). Hors charbon.",
                "trend10": "taux de croissance annuel composé des émissions par "
                           "habitant sur les 10 dernières années disponibles.",
                "no_estimate": "aucune valeur n'est interpolée, reportée ni estimée : "
                               "un pays absent d'une source reste vide.",
            },
        },
        "global": G,
        "countries": C,
    }

    if DRY:
        log("\n[climat] --dry-run : rien écrit.")
        log(json.dumps({k: (len(v) if isinstance(v, dict) else v)
                        for k, v in G.items()}, ensure_ascii=False, indent=1)[:1500])
        return
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUT_JSON.write_text(blob, encoding="utf-8")
    OUT_JS.write_text(
        f"/* climat_cache.js — généré {payload['meta']['updated_at']} — "
        f"ND-GAIN {ndg_year} · {len(C)} pays */\n"
        f"window.__ATLAS_CLIMAT__ = {blob};\n",
        encoding="utf-8",
    )
    log(f"\n[climat] écrit {OUT_JSON} ({len(blob) / 1024:.0f} Ko) + {OUT_JS} "
        f"en {time.time() - t0:.0f}s")
    if ERRORS:
        log("[climat] sources en échec (consignées dans meta.errors) :")
        for e in ERRORS:
            log(f"   · {e['source']} → {e['err']}")


if __name__ == "__main__":
    main()
