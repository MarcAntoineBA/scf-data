#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · 03 La contrainte physique (Jancovici).

Sources (toutes en direct, sans clé nouvelle) :
  1. FRED · DCOILWTICO — WTI quotidien depuis 1986 → moyenne HEBDOMADAIRE pour
     le graphe (≈ 2 100 points) + dernier cours quotidien. Repli : EIA RWTC
     mensuel, puis le cache précédent.
  2. World Bank · NY.GDP.MKTP.KD — PIB mondial (USD constants 2015), annuel.
  3. Our World in Data · annual-co2-emissions-per-country (Global Carbon Budget),
     CO₂ fossile + industrie, monde, annuel.
  4. Our World in Data · energy-mix (Energy Institute, Statistical Review of
     World Energy, dernière édition) : énergie primaire mondiale (« total energy
     supply ») et parts par source, annuel. L'édition et la date de mise à jour
     sont lues dans les métadonnées OWID (pas écrites à la main).
     ⚠ 2026 : OWID a remplacé « primary-energy-cons » par « energy-mix »
     (redirection 301 vers un CSV de colonnes différentes) → l'ancienne collecte
     échouait en silence et le graphe retombait sur 17 points codés en dur (2023).
  5. EIA · International Energy Data — production de pétrole brut (y c.
     condensats), monde et États-Unis, en Mb/j, annuel (années pleines).

Statique (aucune API) : EROI par source (Hall, Lambert & Balogh 2014 ;
Murphy & Hall 2010 pour l'historique US).

Règles : repli = la DERNIÈRE valeur réellement collectée (cache précédent),
jamais une constante périmée en silence ; horodatage du dernier succès par
source ; jamais d'année partielle présentée comme pleine.

Sortie : these_energie_cache.json + .js  (window.__THESE_ENERGIE__)
"""
import csv
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _fred_helpers import fetch_fred
except Exception:  # pragma: no cover
    fetch_fred = None

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_energie_cache.json"
OUT_JS   = CACHE_DIR / "these_energie_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseEnergie/3.0"
TWH_TO_EJ = 0.0036
EIA_KEY = os.environ.get("EIA_API_KEY") or "DEMO_KEY"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

# ════════════════════════════════════════════════════════════════
# FALLBACKS DE DERNIER RECOURS (premier passage sans cache précédent
# ET source en panne). Toujours marqués stale=True dans le cache.
# ════════════════════════════════════════════════════════════════
WORLD_GDP_CONST_USD_BN_FALLBACK = [
    (1965, 14326), (1970, 17800), (1975, 22500), (1980, 27600),
    (1985, 31700), (1990, 37800), (1995, 43400), (2000, 52800),
    (2005, 63100), (2010, 72500), (2015, 82400), (2020, 82800),
    (2023, 93800),
]
WORLD_CO2_GT_FALLBACK = [
    (1965, 11.3), (1970, 14.9), (1975, 16.4), (1980, 18.7),
    (1985, 19.7), (1990, 22.6), (1995, 23.6), (2000, 25.1),
    (2005, 29.6), (2010, 33.0), (2015, 35.5), (2020, 35.2),
    (2021, 36.9), (2022, 37.5), (2023, 38.1), (2024, 38.6),
]

# EROI (Energy Return On Investment) — STATIQUE, valeurs moyennes publiées.
# Vérifiées le 04/10/2026 :
#  - Hall, Lambert & Balogh 2014, Energy Policy 64:141-152 (moyennes par source :
#    charbon ≈ 46, hydro ≈ 84, nucléaire ≈ 14, éolien ≈ 18, PV ≈ 10,
#    sables bitumineux ≈ 4, pétrole & gaz monde ≈ 18 en 2006, gaz Canada ≈ 20 en 2009)
#  - Murphy & Hall 2010 (Annals NYAS 1185) pour l'historique US 1930 / 1970.
#  - Pétrole de schiste US : ≈ 5, estimation de la littérature (Bakken) NON
#    reprise de Hall 2014 → marquée "estimation".
# (libellé, eroi, période/repère, source courte, estimation?)
EROI_BY_SOURCE = [
    ("Pétrole et gaz, États-Unis — années 1930", 100, "1930", "Murphy & Hall 2010", False),
    ("Pétrole et gaz, États-Unis — années 1970",  30, "1970", "Murphy & Hall 2010", False),
    ("Pétrole et gaz, monde — 2006",               18, "2006", "Hall et al. 2014", False),
    ("Sables bitumineux (Canada)",                  4, "",     "Hall et al. 2014", False),
    ("Pétrole de schiste US (fracturation)",        5, "",     "estimation (Bakken)", True),
    ("Gaz naturel (Canada, 2009)",                 20, "2009", "Hall et al. 2014", False),
    ("Charbon",                                    46, "",     "Hall et al. 2014", False),
    ("Nucléaire",                                  14, "",     "Hall et al. 2014", False),
    ("Hydraulique",                                84, "",     "Hall et al. 2014", False),
    ("Éolien",                                     18, "",     "Hall et al. 2014", False),
    ("Solaire photovoltaïque",                     10, "",     "Hall et al. 2014", False),
]

# Parts du mix : sources OWID (grapher energy-mix, metric=share)
MIX_SOURCES = [
    # (param OWID, libellé FR, catégorie)
    ("oil",              "Pétrole",              "fossile"),
    ("coal",             "Charbon",              "fossile"),
    ("gas",              "Gaz naturel",          "fossile"),
    ("nuclear",          "Nucléaire",            "bas-carbone"),
    ("hydro",            "Hydraulique",          "renouvelable"),
    ("wind",             "Éolien",               "renouvelable"),
    ("solar",            "Solaire",              "renouvelable"),
    ("other_renewables", "Autres renouvelables", "renouvelable"),
    ("biofuels",         "Biocarburants",        "renouvelable"),
]
OWID_GRAPHER = "https://ourworldindata.org/grapher/"

# ════════════════════════════════════════════════════════════════
# HTTP
# ════════════════════════════════════════════════════════════════

def http_get(url, timeout=30, max_retries=4, accept="text/csv,application/json,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            last_err = e
            if e.code in (400, 401, 403, 404):
                break
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt)); continue
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


def owid_world(slug, query=""):
    """{year: value} pour l'entité World d'un graphe OWID (CSV complet)."""
    url = (OWID_GRAPHER + slug + ".csv?" + (query + "&" if query else "")
           + "v=1&csvType=full&useColumnShortNames=true")
    txt = http_get(url, timeout=45, accept="text/csv")
    rd = csv.reader(io.StringIO(txt))
    header = next(rd)
    if len(header) < 4 or header[0] != "entity":
        raise ValueError(f"CSV OWID inattendu ({slug}) : {header[:4]}")
    out = {}
    for row in rd:
        if len(row) < 4 or row[0] != "World" or row[3] == "":
            continue
        try:
            out[int(row[2])] = float(row[3])
        except ValueError:
            continue
    if not out:
        raise ValueError(f"OWID {slug} : aucune ligne World")
    return out


def owid_meta(slug, query=""):
    """Édition (citation) + date de mise à jour lues dans les métadonnées OWID."""
    url = OWID_GRAPHER + slug + ".metadata.json" + ("?" + query if query else "")
    try:
        d = json.loads(http_get(url, timeout=30, accept="application/json"))
        col = next(iter((d.get("columns") or {}).values()), {})
        return {"citation": (d.get("chart") or {}).get("citation") or col.get("citationShort"),
                "last_updated": col.get("lastUpdated"), "next_update": col.get("nextUpdate"),
                "timespan": col.get("timespan")}
    except Exception as e:
        sys.stderr.write(f"[OWID meta {slug}] {e}\n")
        return {}


# ════════════════════════════════════════════════════════════════
# COLLECTEURS
# ════════════════════════════════════════════════════════════════

def weekly_from_daily(dates, values):
    """Moyenne par semaine ISO (lundi→dimanche) ; la semaine est datée de son
    DERNIER jour coté. La semaine en cours est incluse (moyenne des jours déjà
    cotés) : c'est une moyenne de cours, pas un total — pas de biais d'année."""
    buckets = {}
    order = []
    for d, v in zip(dates, values):
        y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
        k = date(y, m, dd).isocalendar()[:2]
        if k not in buckets:
            buckets[k] = [d, []]
            order.append(k)
        buckets[k][0] = d
        buckets[k][1].append(v)
    wd = [buckets[k][0] for k in order]
    wv = [round(sum(buckets[k][1]) / len(buckets[k][1]), 2) for k in order]
    return wd, wv


def fetch_wti():
    """FRED DCOILWTICO (quotidien) → hebdo + dernier cours. Repli EIA mensuel."""
    daily = None
    if fetch_fred:
        daily = fetch_fred("DCOILWTICO", start="1986-01-01")
    if not daily:
        try:  # fredgraph.csv, sans clé
            txt = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO&cosd=1986-01-01",
                           timeout=45, accept="text/csv")
            ds, vs = [], []
            for row in csv.reader(io.StringIO(txt)):
                if len(row) == 2 and row[0][:2] in ("19", "20"):
                    try:
                        vs.append(float(row[1])); ds.append(row[0])
                    except ValueError:
                        pass
            if ds:
                daily = {"dates": ds, "values": vs}
        except Exception as e:
            sys.stderr.write(f"[FRED csv DCOILWTICO] {e}\n")
    if daily and len(daily["dates"]) > 1000:
        wd, wv = weekly_from_daily(daily["dates"], daily["values"])
        return {"dates": wd, "values": wv, "freq": "hebdo",
                "last_daily": {"date": daily["dates"][-1], "value": daily["values"][-1]},
                "source_url": "https://fred.stlouisfed.org/series/DCOILWTICO",
                "source_label": "FRED · DCOILWTICO (EIA, cours quotidien) — moyenne hebdomadaire",
                "stale": False, "fetched_at": NOW_ISO}, "FRED:DCOILWTICO"
    # Repli : EIA RWTC mensuel
    params = [("frequency", "monthly"), ("data[0]", "value"),
              ("facets[series][]", "RWTC"), ("sort[0][column]", "period"),
              ("sort[0][direction]", "asc"), ("length", "5000"), ("api_key", EIA_KEY)]
    try:
        data = json.loads(http_get("https://api.eia.gov/v2/petroleum/pri/spt/data/?" + urlencode(params),
                                   timeout=30, accept="application/json"))["response"]["data"]
        rows = [(r["period"] + "-01", float(r["value"])) for r in data if r.get("value") is not None]
        if rows:
            return {"dates": [d for d, _ in rows], "values": [v for _, v in rows], "freq": "mois",
                    "source_url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=M",
                    "source_label": "EIA · RWTC (moyenne mensuelle)", "stale": False,
                    "fetched_at": NOW_ISO}, "EIA:RWTC"
    except Exception as e:
        sys.stderr.write(f"[EIA WTI] {e}\n")
    return None, "FRED:DCOILWTICO"


def fetch_world_gdp_worldbank():
    url = ("https://api.worldbank.org/v2/country/WLD/indicator/NY.GDP.MKTP.KD"
           f"?format=json&per_page=200&date=1965:{NOW.year}")
    try:
        d = json.loads(http_get(url, timeout=30, accept="application/json"))
        if not isinstance(d, list) or len(d) < 2 or not d[1]:
            return None
        rows = sorted((int(r["date"]), r["value"] / 1e9) for r in d[1] if r["value"])
        return [{"year": y, "gdp_bn_usd": round(v, 0)} for y, v in rows] or None
    except Exception as e:
        sys.stderr.write(f"[WorldBank GDP] {e}\n")
        return None


def fetch_world_co2_owid():
    try:
        ym = owid_world("annual-co2-emissions-per-country")
        rows = [{"year": y, "co2_gt": round(v / 1e9, 2)} for y, v in sorted(ym.items()) if y >= 1965]
        if not rows:
            return None, {}
        meta = owid_meta("annual-co2-emissions-per-country")
        meta.update({"last_year": rows[-1]["year"], "fetched_at": NOW_ISO,
                     "source_url": "https://ourworldindata.org/co2-emissions",
                     "perimetre": "CO₂ fossile et industrie (ciment), hors usage des sols"})
        return rows, meta
    except Exception as e:
        sys.stderr.write(f"[OWID CO2] {e}\n")
        return None, {}


def fetch_world_energy_owid():
    """Énergie primaire mondiale (total energy supply, Energy Institute) en EJ."""
    try:
        ym = owid_world("energy-mix", "metric=total&source=total")
        rows = [{"year": y, "ej": round(v * TWH_TO_EJ, 1)} for y, v in sorted(ym.items()) if y >= 1965]
        if not rows:
            return None, {}
        meta = owid_meta("energy-mix", "metric=total&source=total")
        meta.update({"last_year": rows[-1]["year"], "fetched_at": NOW_ISO,
                     "methode": "total energy supply (Energy Institute), biomasse traditionnelle exclue",
                     "source_url": "https://ourworldindata.org/grapher/energy-mix?metric=total&source=total"})
        return rows, meta
    except Exception as e:
        sys.stderr.write(f"[OWID energy] {e}\n")
        return None, {}


def fetch_energy_mix_owid():
    """Parts (%) de chaque source dans l'énergie primaire mondiale, toutes années
    depuis 1965 (pour le graphe : dernière année + repère 2000)."""
    shares = {}
    try:
        for param, label, cat in MIX_SOURCES:
            ym = owid_world("energy-mix", f"metric=share&source={param}")
            shares[param] = {y: v for y, v in ym.items() if y >= 1965}
            time.sleep(0.4)
    except Exception as e:
        sys.stderr.write(f"[OWID mix] {e}\n")
        return None
    years = sorted(set.intersection(*[set(s) for s in shares.values()]))
    if not years:
        return None
    last = years[-1]
    tot = sum(shares[p][last] for p, _, _ in MIX_SOURCES)
    if not 98.5 <= tot <= 101.5:   # garde : la somme des parts doit faire ~100 %
        sys.stderr.write(f"[OWID mix] somme des parts {tot:.1f} % ≠ 100 → rejet\n")
        return None
    hist = {"years": years,
            "shares": {p: [round(shares[p][y], 3) for y in years] for p, _, _ in MIX_SOURCES}}
    mix = [{"source": label, "key": p, "pct": round(shares[p][last], 2), "category": cat}
           for p, label, cat in MIX_SOURCES]
    meta = owid_meta("energy-mix", "metric=share&source=oil")
    meta.update({"year": last, "fetched_at": NOW_ISO,
                 "methode": "parts de l'énergie primaire (total energy supply, Energy Institute)",
                 "source_url": "https://ourworldindata.org/grapher/energy-mix"})
    return mix, hist, meta


def fetch_oil_production_eia():
    """EIA International : brut + condensats (productId 57), production
    (activityId 1), Monde (WORL) et États-Unis (USA), milliers de b/j, annuel."""
    params = [("api_key", EIA_KEY), ("frequency", "annual"), ("data[0]", "value"),
              ("facets[productId][]", "57"), ("facets[activityId][]", "1"),
              ("facets[countryRegionId][]", "WORL"), ("facets[countryRegionId][]", "USA"),
              ("facets[unit][]", "TBPD"), ("sort[0][column]", "period"),
              ("sort[0][direction]", "asc"), ("length", "500")]
    url = "https://api.eia.gov/v2/international/data/?" + urlencode(params)
    try:
        data = json.loads(http_get(url, timeout=40, accept="application/json"))["response"]["data"]
        world, us = {}, {}
        for r in data:
            if r.get("value") in (None, "", "NA", "--"):
                continue
            try:
                y = int(r["period"]); v = float(r["value"]) / 1000.0
            except (ValueError, TypeError, KeyError):
                continue
            (world if r["countryRegionId"] == "WORL" else us)[y] = v
        years = sorted(y for y in world if y in us and y < NOW.year)   # années pleines
        if len(years) < 20:
            return None
        return {"years": years,
                "world": [round(world[y], 2) for y in years],
                "us": [round(us[y], 2) for y in years],
                "unit": "Mb/j", "produit": "pétrole brut, condensats compris",
                "source_url": "https://www.eia.gov/international/data/world/petroleum-and-other-liquids/annual-crude-and-lease-condensate-production",
                "source_label": "EIA · International Energy Data (brut + condensats)",
                "fetched_at": NOW_ISO}
    except Exception as e:
        sys.stderr.write(f"[EIA oil production] {e}\n")
        return None


# ════════════════════════════════════════════════════════════════
# BUILD
# ════════════════════════════════════════════════════════════════

def previous_cache():
    try:
        return json.loads(OUT_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_payload():
    prev = previous_cache() or {}
    ok, failed, repris = [], [], []

    def keep(key, new, name):
        """Nouvelle valeur si la source a répondu ; sinon celle du cache précédent."""
        if new:
            ok.append(name)
            return new
        failed.append(name)
        if prev.get(key):
            repris.append(key)
            return prev[key]
        return None

    # 1. WTI
    wti, wti_name = fetch_wti()
    wti = keep("wti_oil", wti, wti_name)

    # 2. PIB mondial
    gdp = keep("world_gdp_const", fetch_world_gdp_worldbank(), "WorldBank:NY.GDP.MKTP.KD")
    if not gdp:
        gdp = [{"year": y, "gdp_bn_usd": v, "stale": True} for y, v in WORLD_GDP_CONST_USD_BN_FALLBACK]

    # 3. CO2
    co2, co2_meta = fetch_world_co2_owid()
    co2 = keep("world_co2_gt", co2, "OWID:co2")
    co2_meta = co2_meta or prev.get("world_co2_meta") or {}
    if not co2:
        co2 = [{"year": y, "co2_gt": v} for y, v in WORLD_CO2_GT_FALLBACK]
        co2_meta = {"stale": True, "last_year": 2024}

    # 4. Énergie primaire
    energy, en_meta = fetch_world_energy_owid()
    if energy:
        ok.append("OWID:energy-mix(total)")
    else:
        failed.append("OWID:energy-mix(total)")
        # repli : le cache précédent SEULEMENT s'il vient de la même série
        if (prev.get("world_energy_meta") or {}).get("last_year"):
            energy, en_meta = prev["world_energy_ej"], prev["world_energy_meta"]
            repris.append("world_energy_ej")

    # 5. Mix
    m = fetch_energy_mix_owid()
    if m:
        ok.append("OWID:energy-mix(parts)")
        mix, mix_hist, mix_meta = m
    else:
        failed.append("OWID:energy-mix(parts)")
        mix, mix_hist, mix_meta = (prev.get("world_energy_mix"), prev.get("world_energy_mix_history"),
                                   prev.get("world_energy_mix_meta"))
        if mix_meta:
            repris.append("world_energy_mix")
        else:
            mix, mix_hist, mix_meta = None, None, None

    # 6. Production de pétrole
    oil = keep("oil_production", fetch_oil_production_eia(), "EIA:international(brut)")

    eroi = [{"label": l, "eroi_now": v, "periode": p, "source": s, "estimation": est}
            for l, v, p, s, est in EROI_BY_SOURCE]

    last_ok = dict((prev.get("meta") or {}).get("sources_last_ok") or {})
    for name in ok:
        last_ok[name] = NOW_ISO

    payload = {
        "meta": {
            # generated_at = instant de la collecte ; clé lue en priorité par la
            # synchro cache → dépôt (voir fetch_economie_physique.py, incident 23/09).
            "generated_at":    NOW_ISO,
            "updated_at":      NOW.isoformat(),
            "updated_at_unix": int(NOW.timestamp()),
            "sources_ok":      ok,
            "sources_failed":  failed,
            "sources_last_ok": last_ok,
            "blocs_repris_du_cache": repris,
            "doc_version":     "3.0",
        },
        "wti_oil":            wti,
        "world_energy_ej":    energy,
        "world_energy_meta":  en_meta,
        "world_gdp_const":    gdp,
        "world_co2_gt":       co2,
        "world_co2_meta":     co2_meta,
        "world_energy_mix":   mix,
        "world_energy_mix_history": mix_hist,
        "world_energy_mix_meta":    mix_meta,
        "oil_production":     oil,
        "eroi_by_source":     eroi,
        "eroi_meta": {
            "verifie_le": "2026-10-04",
            "sources": ["Hall, Lambert & Balogh 2014, Energy Policy 64:141-152",
                        "Murphy & Hall 2010, Annals of the New York Academy of Sciences 1185"],
            "source_url": "https://doi.org/10.1016/j.enpol.2013.05.049",
            "note": "Moyennes publiées ; les estimations varient fortement selon les études et le périmètre du calcul.",
        },
    }
    return payload, ok, failed


def write_outputs(payload):
    """Écriture atomique dans ~/Library/Caches. La recopie vers le dossier du
    site est faite par fast_publish.sh (synchro_caches_vers_depot.py) : plus de
    lien symbolique ici (Cloudflare refuse les liens)."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    tmp = OUT_JSON.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8"); tmp.replace(OUT_JSON)
    js = (f"/* these_energie_cache.js — generated {payload['meta']['generated_at']} */\n"
          f"window.__THESE_ENERGIE__ = {body};\n")
    tmp = OUT_JS.with_suffix(".js.tmp")
    tmp.write_text(js, encoding="utf-8"); tmp.replace(OUT_JS)


def main():
    t0 = time.time()
    try:
        payload, ok, failed = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    if not ok:
        sys.stderr.write("[these_energie] aucune source n'a répondu — cache NON réécrit\n")
        sys.exit(1)
    write_outputs(payload)
    sys.stdout.write(f"[these_energie] OK · {len(ok)} live, {len(failed)} en échec "
                     f"({', '.join(failed) or '—'}) · {time.time() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
