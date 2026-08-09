#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache « Atlas Économique — Trafic maritime » (IMF PortWatch).

Page consommatrice : Atlas_Economique.html (variable window.__ATLAS_MARITIME__),
vue plein écran « 🚢 Trafic maritime » + bloc ports dans le dossier pays.

Sorties : ~/Library/Caches/site_crypto_finance/atlas_maritime_cache.json
          ~/Library/Caches/site_crypto_finance/atlas_maritime_cache.js
Label launchd prévu : scf.atlasmaritime (StartInterval 43200, logs /tmp/atlasmaritime.*.log)

SOURCE UNIQUE — IMF PortWatch (portwatch.imf.org), API ArcGIS publique, aucune clé :
  services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/{layer}/FeatureServer/0/query
  Couches utilisées (schémas vérifiés le 2026-07-15) :
   - PortWatch_ports_database        : 2065 ports (statique : lat/lon, trafic par type,
                                       industries, part import/export maritime du pays)
   - PortWatch_chokepoints_database  : 28 détroits stratégiques (mêmes champs)
   - Daily_Ports_Data                : escales + volume import/export (tonnes) par port et
                                       par jour, 2019-01-01 → présent (~5 j de décalage).
                                       Agrégé côté SERVEUR par (année,mois) via groupBy+SUM.
   - Daily_Chokepoints_Data          : transits (n_total) + capacité (DWT) par détroit/jour,
                                       agrégé (portid,année,mois).
   - portwatch_disruptions_database  : événements de perturbation (séismes, blocages,
                                       crises — ex. Hormuz, mer Rouge) récents.
  ⚠ Le WLD agrégé (Daily_Trade_Data_WLD) est PÉRIMÉ (s'arrête 2025-04) → la série
    mondiale est reconstruite en sommant Daily_Ports_Data (frais).

NORMALISATION : les sommes mensuelles sont divisées par le nombre de jours du mois
(pour le dernier mois partiel : jours écoulés) → MOYENNE PAR JOUR. Cela évite la
fausse chute du dernier mois incomplet et se lit naturellement (« escales/jour »).

GARDE-FOUS : garde anti-écrasement (si couches manquantes et cache existant → exit),
asserts d'ordre de grandeur (Suez en fort recul post-2023, Shanghai/Singapour en tête),
tableau de contrôle sur stdout.

Interpréteur : n'importe quel python3 (stdlib seule : urllib, json).
"""
import json
import sys
import time
import calendar
import statistics
import datetime as dt
from pathlib import Path
from urllib import request, parse
from urllib.error import URLError, HTTPError

BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "atlas_maritime_cache.json"
OUT_JS = CACHE_DIR / "atlas_maritime_cache.js"

TOP_PORTS_SERIES = 200      # nb de ports avec série mensuelle précalculée
DRY = "--dry-run" in sys.argv


def _req(url, data=None, tries=4):
    """GET (ou POST si data) JSON ArcGIS avec reprises."""
    last = None
    for i in range(tries):
        try:
            body = parse.urlencode(data).encode() if data else None
            rq = request.Request(url, data=body, headers={"User-Agent": UA, "Accept": "application/json"})
            with request.urlopen(rq, timeout=60) as r:
                j = json.loads(r.read().decode("utf-8"))
            if isinstance(j, dict) and j.get("error"):
                raise RuntimeError(f"ArcGIS error: {j['error']}")
            return j
        except (URLError, HTTPError, RuntimeError, ValueError) as e:
            last = e
            time.sleep(1.4 * (i + 1))
    raise RuntimeError(f"échec requête après {tries} essais : {last}")


def query(layer, params, paginate=True, post=False):
    """Requête /query, pagination automatique via resultOffset."""
    p = {"f": "json", "resultRecordCount": 2000}
    p.update(params)
    url = f"{BASE}/{layer}/FeatureServer/0/query"
    feats, offset = [], 0
    while True:
        p["resultOffset"] = offset
        if post:
            j = _req(url, data=p)
        else:
            j = _req(url + "?" + parse.urlencode(p))
        rows = [f["attributes"] for f in j.get("features", [])]
        feats.extend(rows)
        if not paginate or not j.get("exceededTransferLimit") or not rows:
            break
        offset += len(rows)
    return feats


# ---------- axe des mois ----------
def month_key(y, m):
    return f"{int(y):04d}-{int(m):02d}"


def build_months(mkeys):
    ys = sorted(mkeys)
    y0, m0 = map(int, ys[0].split("-"))
    y1, m1 = map(int, ys[-1].split("-"))
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(month_key(y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def days_in_month(key, data_max):
    y, m = map(int, key.split("-"))
    dmax = dt.date.fromisoformat(data_max)
    if y == dmax.year and m == dmax.month:
        return dmax.day  # mois courant partiel → jours écoulés
    return calendar.monthrange(y, m)[1]


def month_complete(key, reliable_last):
    """Vrai si le mois est ENTIÈREMENT couvert (dernier jour calendaire ≤ reliable_last)."""
    y, m = map(int, key.split("-"))
    last_day = dt.date(y, m, calendar.monthrange(y, m)[1])
    return last_day <= dt.date.fromisoformat(reliable_last)


def fetch_reliable_last():
    """Dernière date FIABLE = dernier jour à couverture normale. PortWatch ingère les
    ports avec des décalages variables → les tout derniers jours sont creux, et max(date)
    peut renvoyer des lignes fantômes futures. On prend le dernier jour dont le volume
    mondial d'escales est normal (≥ 2000)."""
    cutoff = (dt.date.today() - dt.timedelta(days=75)).isoformat()
    rows = query("Daily_Ports_Data", {
        "where": f"date >= '{cutoff}'", "groupByFieldsForStatistics": "date",
        "outStatistics": json.dumps([STAT("portcalls", "pc")]), "orderByFields": "date",
    })
    good = [r["date"][:10] for r in rows if (r.get("pc") or 0) >= 2000 and r.get("date")]
    return max(good) if good else None


def fetch_reliable_last_choke(frac=0.6, days=75):
    """Dernière date FIABLE pour la table DÉTROITS — distincte de celle des ports.

    PortWatch ingère chaque table avec son propre décalage : Daily_Chokepoints_Data
    accuse plusieurs jours de retard SUPPLÉMENTAIRES sur Daily_Ports_Data. Utiliser
    la date fiable des ports pour découper les séries détroits laisse passer un
    dernier mois en sous-couverture.
      → Bug constaté le 2026-08-04 : juillet 2026 déclaré complet (couverture ports
        OK au 31/07) alors que les transits détroits n'étaient ingérés qu'aux ~4/5.
        Résultat : chg_1y affiché à -20/-25 % sur TOUS les détroits (Taïwan, Malacca,
        Gibraltar, Suez, Bab el-Mandeb…) au lieu de -2 à +12 % réels — jusqu'à
        39 points d'erreur, et le seul choc réel (Ormuz -85 %) noyé dans le bruit.

    Seuil RELATIF à la médiane de la fenêtre récente, pas absolu : un vrai
    effondrement de trafic (fermeture d'un détroit) déplace la médiane et ne
    déclenche donc pas de faux positif ; seule la sous-couverture d'ingestion,
    qui touche un jour isolé en fin de série, est filtrée.
    """
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = query("Daily_Chokepoints_Data", {
        "where": f"date >= '{cutoff}'", "groupByFieldsForStatistics": "date",
        "outStatistics": json.dumps([STAT("n_total", "n")]), "orderByFields": "date",
    })
    vals = [(r["date"][:10], r.get("n") or 0) for r in rows if r.get("date")]
    if not vals:
        return None
    med = statistics.median([v for _, v in vals])
    if not med:
        return None
    good = [d for d, v in vals if v >= frac * med]
    return max(good) if good else None


def per_day(series_sum, months, data_max, rnd=0):
    """Somme mensuelle → moyenne par jour, alignée à `months`."""
    out = []
    for k in months:
        v = series_sum.get(k)
        if v is None:
            out.append(None)
        else:
            d = days_in_month(k, data_max)
            x = v / d if d else None
            out.append(round(x, rnd) if x is not None else None)
    return out


def last_nn(arr):
    for i in range(len(arr) - 1, -1, -1):
        if arr[i] is not None:
            return i, arr[i]
    return None, None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a - b) / b * 100, 1)


# ---------- 1. Ports statiques ----------
def fetch_ports():
    fields = ("portid,portname,country,ISO3,continent,lat,lon,vessel_count_total,"
              "vessel_count_container,vessel_count_dry_bulk,vessel_count_general_cargo,"
              "vessel_count_RoRo,vessel_count_tanker,industry_top1,industry_top2,industry_top3,"
              "share_country_maritime_import,share_country_maritime_export,LOCODE")
    rows = query("PortWatch_ports_database", {"where": "1=1", "outFields": fields, "returnGeometry": "false"})
    ports = []
    for a in rows:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        ind = [x for x in (a.get("industry_top1"), a.get("industry_top2"), a.get("industry_top3")) if x]
        ports.append({
            "id": a["portid"], "name": a.get("portname") or a["portid"],
            "country": a.get("country") or "", "iso3": a.get("ISO3") or "",
            "cont": a.get("continent") or "", "lat": round(a["lat"], 4), "lon": round(a["lon"], 4),
            "tot": round(a.get("vessel_count_total") or 0, 1),
            "cnt": round(a.get("vessel_count_container") or 0, 1),
            "dry": round(a.get("vessel_count_dry_bulk") or 0, 1),
            "gen": round(a.get("vessel_count_general_cargo") or 0, 1),
            "roro": round(a.get("vessel_count_RoRo") or 0, 1),
            "tnk": round(a.get("vessel_count_tanker") or 0, 1),
            "ind": ind,
            "imp": round(a["share_country_maritime_import"], 4) if a.get("share_country_maritime_import") is not None else None,
            "exp": round(a["share_country_maritime_export"], 4) if a.get("share_country_maritime_export") is not None else None,
            "loco": a.get("LOCODE") or "",
        })
    ports.sort(key=lambda p: p["tot"], reverse=True)
    return ports


def fetch_chokepoints_static():
    fields = ("portid,portname,country,ISO3,continent,lat,lon,vessel_count_total,"
              "vessel_count_container,vessel_count_dry_bulk,vessel_count_general_cargo,"
              "vessel_count_RoRo,vessel_count_tanker,industry_top1,industry_top2,industry_top3")
    rows = query("PortWatch_chokepoints_database", {"where": "1=1", "outFields": fields, "returnGeometry": "false"})
    out = {}
    for a in rows:
        ind = [x for x in (a.get("industry_top1"), a.get("industry_top2"), a.get("industry_top3")) if x]
        out[a["portid"]] = {
            "id": a["portid"], "name": a.get("portname") or a["portid"],
            "country": a.get("country") or "", "iso3": a.get("ISO3") or "",
            "lat": round(a["lat"], 4), "lon": round(a["lon"], 4),
            "tot": round(a.get("vessel_count_total") or 0, 1),
            "cnt": round(a.get("vessel_count_container") or 0, 1),
            "dry": round(a.get("vessel_count_dry_bulk") or 0, 1),
            "gen": round(a.get("vessel_count_general_cargo") or 0, 1),
            "roro": round(a.get("vessel_count_RoRo") or 0, 1),
            "tnk": round(a.get("vessel_count_tanker") or 0, 1),
            "ind": ind,
        }
    return out


# ---------- 2. Séries mensuelles (groupBy serveur) ----------
STAT = lambda field, alias: {"statisticType": "sum", "onStatisticField": field, "outStatisticFieldName": alias}


def fetch_global_monthly():
    stats = [STAT("portcalls", "pc"), STAT("import", "imp"), STAT("export", "exp"),
             STAT("portcalls_container", "cnt"), STAT("portcalls_dry_bulk", "dry"),
             STAT("portcalls_general_cargo", "gen"), STAT("portcalls_roro", "roro"),
             STAT("portcalls_tanker", "tnk")]
    rows = query("Daily_Ports_Data", {
        "where": "1=1", "groupByFieldsForStatistics": "year,month",
        "outStatistics": json.dumps(stats), "orderByFields": "year,month",
    })
    g = {}
    for a in rows:
        k = month_key(a["year"], a["month"])
        g[k] = a
    return g


CK_TYPES = ["container", "dry_bulk", "general_cargo", "roro", "tanker"]


def fetch_choke_monthly():
    # total + capacité + décomposition par type (escales n_* et capacité capacity_*)
    stats = [STAT("n_total", "n"), STAT("capacity", "cap")]
    for tp in CK_TYPES:
        stats.append(STAT("n_" + tp, "n_" + tp))
        stats.append(STAT("capacity_" + tp, "c_" + tp))
    rows = query("Daily_Chokepoints_Data", {
        "where": "1=1", "groupByFieldsForStatistics": "portid,year,month",
        "outStatistics": json.dumps(stats), "orderByFields": "portid,year,month",
    })
    by = {}
    for a in rows:
        k = month_key(a["year"], a["month"])
        d = by.setdefault(a["portid"], {"n": {}, "cap": {},
                                        "calls": {tp: {} for tp in CK_TYPES},
                                        "vol": {tp: {} for tp in CK_TYPES}})
        d["n"][k] = a.get("n")
        d["cap"][k] = a.get("cap")
        for tp in CK_TYPES:
            d["calls"][tp][k] = a.get("n_" + tp)
            d["vol"][tp][k] = a.get("c_" + tp)
    return by


def fetch_ports_volume(reliable_last):
    """Tonnage (import+export) moyen par jour et par port sur les 365 derniers jours,
    pour TOUS les ports → classement/coloration par volume de marchandise (pas seulement
    par nombre de navires). Un groupBy(portid) serveur, ~2065 lignes."""
    cutoff = (dt.date.fromisoformat(reliable_last) - dt.timedelta(days=365)).isoformat()
    stats = [STAT("import", "imp"), STAT("export", "exp")]
    rows = query("Daily_Ports_Data", {
        "where": "date >= '%s' AND date <= '%s'" % (cutoff, reliable_last),
        "groupByFieldsForStatistics": "portid",
        "outStatistics": json.dumps(stats), "orderByFields": "portid",
    })
    out = {}
    for a in rows:
        tot = (a.get("imp") or 0) + (a.get("exp") or 0)
        out[a["portid"]] = round(tot / 365.0)  # tonnes/jour (moyenne sur 12 mois)
    return out


def fetch_ports_monthly(port_ids):
    stats = [STAT("portcalls", "pc"), STAT("import", "imp"), STAT("export", "exp")]
    ids = ",".join("'%s'" % i for i in port_ids)
    rows = query("Daily_Ports_Data", {
        "where": f"portid IN ({ids})", "groupByFieldsForStatistics": "portid,year,month",
        "outStatistics": json.dumps(stats), "orderByFields": "portid,year,month",
    }, post=True)
    by = {}
    for a in rows:
        k = month_key(a["year"], a["month"])
        by.setdefault(a["portid"], {"pc": {}, "imp": {}, "exp": {}})
        by[a["portid"]]["pc"][k] = a.get("pc")
        by[a["portid"]]["imp"][k] = a.get("imp")
        by[a["portid"]]["exp"][k] = a.get("exp")
    return by


def fetch_disruptions():
    fields = ("eventid,eventtype,eventname,alertlevel,country,fromdate,todate,severitytext,"
              "lat,long,affectedports,n_affectedports,pageid")
    rows = query("portwatch_disruptions_database", {
        "where": "1=1", "outFields": fields, "returnGeometry": "false",
        "orderByFields": "fromdate DESC",
    })
    out = []
    for a in rows:
        fd = a.get("fromdate")
        _ep = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()
        out.append({
            "type": a.get("eventtype") or "", "name": a.get("eventname") or "",
            "level": a.get("alertlevel") or "", "country": a.get("country") or "",
            "from": _ep(fd) if fd else None,
            "to": _ep(a["todate"]) if a.get("todate") else None,
            "sev": a.get("severitytext") or "", "nports": a.get("n_affectedports") or 0,
            "ports": (a.get("affectedports") or ""), "pageid": a.get("pageid") or "",
            "lat": round(a["lat"], 3) if a.get("lat") is not None else None,
            "lon": round(a["long"], 3) if a.get("long") is not None else None,
        })
    return out


# ---------- assemblage ----------
def main():
    t0 = time.time()
    print("[maritime] ports statiques…")
    ports = fetch_ports()
    print(f"  {len(ports)} ports")
    print("[maritime] détroits statiques…")
    choke_static = fetch_chokepoints_static()
    print(f"  {len(choke_static)} détroits")
    print("[maritime] série mondiale mensuelle (somme Daily_Ports_Data)…")
    gmon = fetch_global_monthly()
    print("[maritime] détroits mensuel…")
    cmon = fetch_choke_monthly()
    print("[maritime] top ports mensuel…")
    top_ids = [p["id"] for p in ports[:TOP_PORTS_SERIES]]
    pmon = fetch_ports_monthly(top_ids)
    print("[maritime] perturbations…")
    try:
        disruptions = fetch_disruptions()
    except Exception as e:
        print("  (perturbations indisponibles :", e, ")")
        disruptions = []

    if len(ports) < 500 or len(choke_static) < 10 or not gmon:
        print("[maritime] ERREUR : données incomplètes — écriture annulée.")
        sys.exit(1 if OUT_JSON.exists() else 2)

    print("[maritime] date fiable (couverture)…")
    reliable_last = fetch_reliable_last()
    all_months = build_months(set(gmon.keys()))
    if not reliable_last:
        # repli : dernier mois présent, jours calendaires pleins
        y, m = map(int, all_months[-1].split("-"))
        reliable_last = f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    # On ne garde que les mois ENTIÈREMENT couverts (drop du mois courant partiel).
    months = [k for k in all_months if month_complete(k, reliable_last)]
    if not months:
        months = all_months
    data_max = reliable_last
    print(f"  dernier jour fiable : {reliable_last} · {len(months)} mois complets "
          f"(sur {len(all_months)}) — mois partiels écartés : {all_months[len(months):]}")

    # Couverture DÉTROITS : décalage d'ingestion propre, plus long que celui des ports.
    # cf. fetch_reliable_last_choke() pour le détail du bug 2026-08.
    print("[maritime] date fiable détroits…")
    try:
        choke_reliable = fetch_reliable_last_choke() or reliable_last
    except Exception as e:
        print("  (couverture détroits indisponible :", e, ") → repli sur la date ports")
        choke_reliable = reliable_last
    choke_months = [k for k in months if month_complete(k, choke_reliable)]
    n_drop = len(months) - len(choke_months)
    print(f"  dernier jour fiable détroits : {choke_reliable} · "
          f"{n_drop} mois écarté(s) des séries détroits : {months[len(choke_months):] or '—'}")

    # tonnage (import+export) moyen/jour par port sur 12 mois → classement par volume
    print("[maritime] tonnage par port (365 j)…")
    try:
        port_vol = fetch_ports_volume(reliable_last)
    except Exception as e:
        print("  (tonnage ports indisponible :", e, ")")
        port_vol = {}
    for p in ports:
        p["vol"] = port_vol.get(p["id"])

    # --- global (moyenne/jour) ---
    def gseries(field, rnd=0):
        return per_day({k: v.get(field) for k, v in gmon.items()}, months, data_max, rnd)
    glob = {
        "months": months,
        "portcalls": gseries("pc"),
        "import": gseries("imp"),
        "export": gseries("exp"),
        "type": {
            "container": gseries("cnt"), "dry_bulk": gseries("dry"),
            "general_cargo": gseries("gen"), "roro": gseries("roro"), "tnk": gseries("tnk"),
        },
    }
    li, _ = last_nn(glob["portcalls"])
    lm = months[li] if li is not None else months[-1]
    ly = None
    if li is not None and li >= 12:
        ly = li - 12
    glob["latest"] = {
        "month": lm,
        "portcalls": glob["portcalls"][li] if li is not None else None,
        "import": glob["import"][li] if li is not None else None,
        "export": glob["export"][li] if li is not None else None,
    }
    glob["yoy"] = {
        "portcalls": pct(glob["portcalls"][li], glob["portcalls"][ly]) if ly is not None else None,
        "import": pct(glob["import"][li], glob["import"][ly]) if ly is not None else None,
        "export": pct(glob["export"][li], glob["export"][ly]) if ly is not None else None,
    }

    # --- détroits ---
    chokepoints = []

    def mask_choke(arr):
        """Neutralise les mois non couverts par l'ingestion DÉTROITS.
        On garde la longueur alignée sur `months` (le front indexe les séries
        détroits contre ce même axe) : les mois en sous-couverture passent à None,
        et last_nn() retient donc le dernier mois RÉELLEMENT complet."""
        return arr if not n_drop else arr[:len(choke_months)] + [None] * n_drop

    empty_cm = {"n": {}, "cap": {}, "calls": {tp: {} for tp in CK_TYPES}, "vol": {tp: {} for tp in CK_TYPES}}
    for pid, st in choke_static.items():
        cm = cmon.get(pid, empty_cm)
        n = mask_choke(per_day(cm["n"], months, data_max, 0))
        cap = mask_choke(per_day(cm["cap"], months, data_max, 0))
        calls = {tp: mask_choke(per_day(cm["calls"][tp], months, data_max, 0)) for tp in CK_TYPES}
        vol = {tp: mask_choke(per_day(cm["vol"][tp], months, data_max, -3)) for tp in CK_TYPES}
        li2, lv = last_nn(n)
        base_vals = [n[i] for i, k in enumerate(months) if k.startswith("2019") and n[i] is not None]
        base = round(sum(base_vals) / len(base_vals)) if base_vals else None
        yv = n[li2 - 12] if (li2 is not None and li2 >= 12) else None
        rec = dict(st)
        rec.update({
            "n": n, "cap": cap, "calls": calls, "vol": vol,
            "latest_n": lv, "latest_month": months[li2] if li2 is not None else None,
            "base2019": base, "chg_base": pct(lv, base), "chg_1y": pct(lv, yv),
        })
        chokepoints.append(rec)
    chokepoints.sort(key=lambda c: c["tot"], reverse=True)

    # --- séries ports (top N, moyenne/jour) ---
    port_series = {}
    for pid in top_ids:
        pm = pmon.get(pid)
        if not pm:
            continue
        port_series[pid] = {
            "pc": per_day(pm["pc"], months, data_max, 0),
            "imp": per_day(pm["imp"], months, data_max, -3),   # arrondi au millier de tonnes
            "exp": per_day(pm["exp"], months, data_max, -3),
        }

    now = dt.datetime.now().astimezone()
    payload = {
        "meta": {
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "updated_at_unix": int(now.timestamp()),
            "data_min": months[0], "data_max": months[-1], "data_max_date": data_max,
            "n_ports": len(ports), "n_chokepoints": len(chokepoints),
            "n_ports_series": len(port_series),
            "source": "IMF PortWatch", "url": "https://portwatch.imf.org",
        },
        "months": months,
        "global": glob,
        "ports": ports,
        "chokepoints": chokepoints,
        "port_series": port_series,
        "disruptions": disruptions[:60],
    }

    # ---------- audit ----------
    print("\n=== CONTRÔLE ===")
    print(f"couverture : {months[0]} → {months[-1]} ({len(months)} mois) · dernier jour {data_max}")
    print(f"mondial (dernier mois {glob['latest']['month']}) : "
          f"{glob['latest']['portcalls']:.0f} escales/j · "
          f"import {glob['latest']['import']/1e6:.1f} Mt/j · export {glob['latest']['export']/1e6:.1f} Mt/j · "
          f"YoY escales {glob['yoy']['portcalls']}%")
    print("top 5 ports (navires) :", ", ".join(f"{p['name']} ({p['tot']:.0f})" for p in ports[:5]))
    _byvol = sorted([p for p in ports if p.get("vol")], key=lambda p: p["vol"], reverse=True)
    print("top 5 ports (tonnage/j) :", ", ".join(f"{p['name']} ({p['vol']/1e3:.0f} kt)" for p in _byvol[:5]))
    suez = next((c for c in chokepoints if c["id"] == "chokepoint1"), None)
    bab = next((c for c in chokepoints if c["id"] == "chokepoint4"), None)
    cape = next((c for c in chokepoints if c["id"] == "chokepoint7"), None)
    pan = next((c for c in chokepoints if c["id"] == "chokepoint2"), None)
    for c in (suez, bab, cape, pan):
        if c:
            print(f"  {c['name']:22s} : {c['latest_n']} transits/j · base2019 {c['base2019']} · "
                  f"vs base {c['chg_base']}% · 1an {c['chg_1y']}%")
    print(f"perturbations : {len(disruptions)} (dont RED : {sum(1 for d in disruptions if d['level']=='RED')})")

    # asserts d'ordre de grandeur (mer Rouge : Suez & Bab el-Mandeb en fort recul depuis 2023)
    assert ports[0]["tot"] > ports[10]["tot"], "tri ports incohérent"
    assert glob["latest"]["portcalls"] and glob["latest"]["portcalls"] > 3000, "escales mondiales/j trop basses"
    # Suez : bande de plausibilité, pas une thèse géopolitique figée. Le seuil dur
    # « < -20 % » datait de la crise mer Rouge 2023-2025 ; le trafic se rétablit
    # (2026-08 : Suez -19,2 % vs 2019 et +10,5 % sur 1 an, Bab el-Mandeb +6,5 %),
    # ce qui faisait échouer le fetch sur une évolution RÉELLE. On ne teste plus
    # que la cohérence d'ordre de grandeur.
    if suez and suez["chg_base"] is not None:
        assert -70 < suez["chg_base"] < 15, \
            f"Suez hors bande de plausibilité vs 2019 : {suez['chg_base']}%"

    # --- garde-fou : signature du « mois partiel détroits » (bug 2026-08) ---
    # Un VRAI choc maritime est LOCALISÉ : en juin 2026, Ormuz faisait -85 % pendant
    # que Taïwan (+1,2 %), Gibraltar (-1,5 %) et le Cap (+1,1 %) restaient normaux.
    # Une chute simultanée >10 % M/M sur des détroits sans lien géographique, alors
    # que les escales mondiales sont normales, ne peut être qu'une sous-couverture.
    _mm = []
    for c in chokepoints[:12]:
        if c["id"] == "chokepoint6":      # Ormuz : choc réel, exclu du test
            continue
        i, _v = last_nn(c["n"])
        if i is None or i < 1 or not c["n"][i - 1]:
            continue
        _mm.append(100 * c["n"][i] / c["n"][i - 1] - 100)
    _gi, _ = last_nn(glob["portcalls"])
    _gmm = (100 * glob["portcalls"][_gi] / glob["portcalls"][_gi - 1] - 100) \
        if (_gi is not None and _gi >= 1 and glob["portcalls"][_gi - 1]) else 0.0
    if _mm:
        _bad = sum(1 for x in _mm if x < -10)
        assert not (_bad >= 3 and _gmm > -5), (
            f"MOIS PARTIEL DÉTROITS NON FILTRÉ : {_bad}/{len(_mm)} détroits majeurs à "
            f"<-10 % M/M alors que les escales mondiales font {_gmm:+.1f}%. "
            f"Dernier mois détroits retenu = {chokepoints[0]['latest_month']}, "
            f"date fiable détroits = {choke_reliable}. Voir fetch_reliable_last_choke().")
        print(f"garde-fou mois partiel : {_bad}/{len(_mm)} détroits <-10 % M/M · "
              f"mondial {_gmm:+.1f}% → OK")
    print("asserts OK")

    if DRY:
        print("\n[maritime] --dry-run : rien écrit.")
        return
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUT_JSON.write_text(blob, encoding="utf-8")
    OUT_JS.write_text(
        f"/* atlas_maritime_cache.js — généré {payload['meta']['updated_at']} — "
        f"IMF PortWatch · {len(ports)} ports · {len(chokepoints)} détroits */\n"
        f"window.__ATLAS_MARITIME__ = {blob};\n",
        encoding="utf-8",
    )
    print(f"\n[maritime] écrit {OUT_JSON} ({len(blob)/1024:.0f} Ko) + {OUT_JS} en {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
