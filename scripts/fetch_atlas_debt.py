#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_atlas_debt.py — « Dette souveraine » de l'Atlas Économique.
Donnée FRAÎCHE et généralisée sur la dynamique de la dette (là où la COFOG est en retard) :

  • Taux 10 ans — QUOTIDIEN US (FRED DGS10) + benchmark euro AAA (BCE, ≈ Bund) ;
                  MENSUEL ~25 pays (FRED/OCDE IRLTLT01…) → comparaison + tendance.
  • Spreads vs Bund (dérivés).
  • Coût MOYEN (intérêts/dette, FMI WEO) vs coût MARGINAL (taux 10 ans) → pression future.
  • Dette/PIB + charge d'intérêts + solde, trajectoires FMI WEO jusqu'à 2031.
  • Mur des échéances : maturité moyenne (curé DMO/OCDE, indicatif) → roulement annuel + besoin de financement brut.

Produit `atlas_debt_cache.{json,js}` (window.__ATLAS_DEBT__), chargé en lazy par la vue.
Copies : Desktop + public/ + ~/Library/Caches/site_crypto_finance/. Auto : launchd.

MODES (2026-07-28 — « taux live, actualisés automatiquement, sans bug ») :
  --full   run complet (WEO + séries mensuelles FRED + live TE)  ~2-3 min
  --live   run LÉGER : ne rafraîchit que ce qui bouge dans la journée (taux du jour TE,
           benchmarks quotidiens FRED/BCE) et REPREND la structure (WEO, maturités,
           dette/PIB) du cache existant → quelques dizaines de secondes, donc lançable
           toutes les 2 h sans marteler FRED/FMI.
  --auto   (défaut launchd) : --live si la partie structurelle a moins de STRUCT_MAX_H,
           sinon --full. Un seul job launchd suffit.
"""
import os
import sys, os, re, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _fred_helpers import fetch_fred
except Exception:
    fetch_fred = None
try:
    from curl_cffi import requests as rq
    def _sess():
        return rq.Session(impersonate="chrome120", timeout=45)
except Exception:
    import requests as rq
    def _sess():
        s = rq.Session(); s.headers.update({"User-Agent": "Mozilla/5.0"}); return s

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "Desktop", "Site_Crypto_Finance")
CACHES = [
    os.path.join(REPO, "atlas_debt_cache"),
    os.path.join(REPO, "public", "atlas_debt_cache"),
    os.path.join(HOME, "Library", "Caches", "site_crypto_finance", "atlas_debt_cache"),
]

# ISO3 -> (ISO2 FRED, nom FR, région courte). Taux 10 ans « long-term govt bond yield » OCDE.
COUNTRIES = {
    "USA": ("US", "États-Unis", "Amérique"),  "DEU": ("DE", "Allemagne", "Europe"),
    "FRA": ("FR", "France", "Europe"),         "ITA": ("IT", "Italie", "Europe"),
    "ESP": ("ES", "Espagne", "Europe"),        "GBR": ("GB", "Royaume-Uni", "Europe"),
    "JPN": ("JP", "Japon", "Asie"),            "NLD": ("NL", "Pays-Bas", "Europe"),
    "BEL": ("BE", "Belgique", "Europe"),       "AUT": ("AT", "Autriche", "Europe"),
    "PRT": ("PT", "Portugal", "Europe"),       "GRC": ("GR", "Grèce", "Europe"),
    "IRL": ("IE", "Irlande", "Europe"),        "FIN": ("FI", "Finlande", "Europe"),
    "CAN": ("CA", "Canada", "Amérique"),       "AUS": ("AU", "Australie", "Océanie"),
    "CHE": ("CH", "Suisse", "Europe"),         "SWE": ("SE", "Suède", "Europe"),
    "NOR": ("NO", "Norvège", "Europe"),        "DNK": ("DK", "Danemark", "Europe"),
    "POL": ("PL", "Pologne", "Europe"),        "CZE": ("CZ", "Tchéquie", "Europe"),
    "KOR": ("KR", "Corée du Sud", "Asie"),     "MEX": ("MX", "Mexique", "Amérique"),
    "NZL": ("NZ", "Nouvelle-Zélande", "Océanie"), "ISR": ("IL", "Israël", "Moyen-Orient"),
    "HUN": ("HU", "Hongrie", "Europe"),
}

# Maturité moyenne de la dette négociable (années) — sources DMO/OCDE, indicatif ~2024-2025.
# Sert au « mur des échéances » : roulement annuel ≈ dette / maturité. Best-effort, majors.
AVG_MATURITY = {
    "USA": 6.0, "DEU": 6.9, "FRA": 8.6, "ITA": 7.8, "ESP": 8.0, "GBR": 14.0, "JPN": 9.4,
    "NLD": 8.9, "BEL": 10.7, "AUT": 11.0, "PRT": 7.5, "GRC": 19.0, "IRL": 10.0, "FIN": 7.5,
    "CAN": 6.5, "AUS": 7.0, "SWE": 6.0, "POL": 5.0, "KOR": 9.5, "MEX": 8.0, "HUN": 5.5,
}

CUR_YEAR = 2026
FORECAST_FROM = 2026

# Âge max (heures) de la partie STRUCTURELLE (WEO, séries mensuelles) avant qu'un
# run --auto ne repasse en --full. Les taux du jour, eux, sont rafraîchis à chaque run.
STRUCT_MAX_H = 12.0

# Slugs TradingEconomics pour le taux 10 ans LIVE (valeur du jour)
TE_SLUG = {
    "USA": "united-states", "DEU": "germany", "FRA": "france", "ITA": "italy", "ESP": "spain",
    "GBR": "united-kingdom", "JPN": "japan", "NLD": "netherlands", "BEL": "belgium",
    "AUT": "austria", "PRT": "portugal", "GRC": "greece", "IRL": "ireland", "FIN": "finland",
    "CAN": "canada", "AUS": "australia", "CHE": "switzerland", "SWE": "sweden", "NOR": "norway",
    "DNK": "denmark", "POL": "poland", "CZE": "czech-republic", "KOR": "south-korea",
    "MEX": "mexico", "NZL": "new-zealand", "ISR": "israel", "HUN": "hungary",
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def te_yield(sess, slug, tries=2):
    """Taux 10 ans courant depuis TradingEconomics : (valeur, date ISO). (None,None) si échec.
    2 tentatives : le scraping TE échoue parfois ponctuellement en rafale, et un pays
    manquant ferait retomber son taux sur le mensuel OCDE (valeur visiblement différente)."""
    for i in range(tries):
        try:
            t = sess.get(f"https://tradingeconomics.com/{slug}/government-bond-yield", timeout=25).text
            m = re.search(r'"last"\s*:\s*(\d+\.\d+)', t) or re.search(r'<td id="p">\s*(\d+\.\d+)', t)
            if m:
                val = round(float(m.group(1)), 2)
                dm = re.search(r'on ([A-Z][a-z]+) (\d{1,2}), (\d{4})', t)
                date = (f"{dm.group(3)}-{_MONTHS.get(dm.group(1), 1):02d}-{int(dm.group(2)):02d}"
                        if dm else None)
                return val, date
        except Exception:
            pass
        if i + 1 < tries:
            time.sleep(1.5)
    return None, None


def fetch_live_all():
    """Taux du jour TE pour les 27 pays -> {a3: (val, date_iso)}. Pacing 0,4 s."""
    sess_te = _sess()
    live = {}
    for a3, slug in TE_SLUG.items():
        v, dt = te_yield(sess_te, slug)
        if v is not None:
            live[a3] = (v, dt or time.strftime("%Y-%m-%d"))
        time.sleep(0.4)
    return live


def merge_live_point(hist, date, val):
    """Insère le point LIVE dans la série mensuelle.
    PIÈGE CORRIGÉ (2026-07-28) : l'ancienne règle n'ajoutait le point que si le mois
    différait → pour les US (mensuel dérivé du quotidien, donc toujours le mois courant)
    le graphe et le Δ3M restaient sur la valeur mensuelle (4,69) alors que l'entête
    affichait la valeur du jour (4,63). On REMPLACE désormais le point du mois courant."""
    h = [list(p) for p in (hist or [])]
    if h and h[-1][0][:7] == date[:7]:
        h[-1] = [date, val]
    else:
        h.append([date, val])
    return h


def apply_live(entry, val, date, fresh, de_v):
    """Applique un taux du jour à une fiche pays : série, deltas, coût marginal, spread."""
    y = entry.setdefault("y10", {})
    y["hist"] = merge_live_point(y.get("hist"), date, val)[-84:]
    y["v"] = val
    y["date"] = date
    y["live"] = True
    y["fresh"] = bool(fresh)          # False = repli sur la dernière valeur connue
    y["src"] = "TradingEconomics" if fresh else "TradingEconomics (dernière valeur connue)"
    y["c1m"] = chg(y["hist"], 1)
    y["c3m"] = chg(y["hist"], 3)
    y["c12m"] = chg(y["hist"], 12)
    entry["cost_marg"] = val
    entry["spread_bund"] = round((val - de_v) * 100) if de_v is not None else None


def load_prev():
    """Cache existant (1re copie lisible) — sert de repli par pays et de base au mode --live."""
    for base in CACHES:
        try:
            with open(base + ".json", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("countries"):
                return d
        except Exception:
            pass
    return None


def r2(x):
    if x is None:
        return None
    r = round(float(x), 2)
    return int(r) if r == int(r) else r


def r1(x):
    if x is None:
        return None
    r = round(float(x), 1)
    return int(r) if r == int(r) else r


def pack_daily(dates, vals, keep_days=900):
    """Séries quotidiennes -> [[YYYY-MM-DD, val]] (dernières keep_days, trous retirés)."""
    out = []
    for d, v in zip(dates, vals):
        if v is not None:
            out.append([d, r2(v)])
    return out[-keep_days:]


def chg(series_pairs, months):
    """Variation (pb) entre la dernière valeur et ~`months` mois avant (séries [date,val])."""
    if not series_pairs:
        return None
    last_d, last_v = series_pairs[-1]
    ly, lm = int(last_d[:4]), int(last_d[5:7])
    tgt = (ly * 12 + lm - months)
    best = None
    for d, v in series_pairs:
        ym = int(d[:4]) * 12 + int(d[5:7])
        if ym <= tgt:
            best = v
    return round((last_v - best) * 100) if best is not None else None   # points de base


def fred_series(sid, start="2019-01-01"):
    if not fetch_fred:
        return None
    try:
        o = fetch_fred(sid, start=start)
        return list(zip(o["dates"], o["values"]))
    except Exception as e:
        sys.stderr.write(f"[WARN] FRED {sid}: {e}\n")
        return None


def ecb_daily(key, start="2023-01-01"):
    """Série quotidienne BCE (CSV). -> [[date,val]]."""
    import csv, io
    s = _sess()
    u = f"https://data-api.ecb.europa.eu/service/data/{key}?startPeriod={start}&format=csvdata"
    try:
        r = s.get(u, timeout=60)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        out = []
        for row in rows:
            d = row.get("TIME_PERIOD"); v = row.get("OBS_VALUE")
            if d and v not in (None, ""):
                try:
                    out.append([d, r2(float(v))])
                except ValueError:
                    pass
        out.sort()
        return out
    except Exception as e:
        sys.stderr.write(f"[WARN] BCE {key}: {e}\n")
        return None


def imf_weo(code):
    """DataMapper WEO {a3: {année: val}}."""
    s = _sess()
    fix = {"UVK": "XKX", "WBG": "PSE"}
    try:
        d = s.get(f"https://www.imf.org/external/datamapper/api/v1/{code}", timeout=90).json()
        vals = (d or {}).get("values", {}).get(code, {})
        out = {}
        for k, years in vals.items():
            a3 = fix.get(k, k)
            ym = {}
            for ys, v in years.items():
                try:
                    ym[int(ys)] = float(v)
                except (TypeError, ValueError):
                    pass
            if ym:
                out[a3] = ym
        return out
    except Exception as e:
        sys.stderr.write(f"[WARN] WEO {code}: {e}\n")
        return {}


def last_le(ym, cap):
    """Dernière valeur d'année <= cap (réel, pas prévision)."""
    if not ym:
        return None, None
    ys = [y for y in ym if y <= cap]
    if not ys:
        return None, None
    y = max(ys)
    return y, ym[y]


def pack_years(ym, rnd=r2):
    if not ym:
        return None
    y0, y1 = min(ym), max(ym)
    v = [rnd(ym[y]) if y in ym and ym[y] is not None else None for y in range(y0, y1 + 1)]
    return {"s": y0, "v": v}


def fetch_bench():
    """Benchmarks quotidiens (US FRED DGS10, courbe AAA BCE) -> (bench, us_daily).
    bench=None si les DEUX sources échouent (l'appelant garde alors le bench précédent)."""
    us_daily = fred_series("DGS10", start="2019-01-01") or []
    euro_daily = ecb_daily("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y") or []
    if not us_daily and not euro_daily:
        return None, []
    us_pairs = pack_daily([d for d, _ in us_daily], [v for _, v in us_daily])
    bench = {
        "us": {"d": us_pairs, "last": us_pairs[-1] if us_pairs else None,
               "c1m": chg(us_pairs, 1), "c3m": chg(us_pairs, 3), "c12m": chg(us_pairs, 12)},
        "euro_aaa": {"d": euro_daily[-900:], "last": euro_daily[-1] if euro_daily else None,
                     "c1m": chg(euro_daily, 1), "c3m": chg(euro_daily, 3), "c12m": chg(euro_daily, 12)},
    }
    return bench, us_daily


def build_full(prev):
    print("→ Taux 10 ans quotidiens (US FRED, euro BCE)…")
    bench, us_daily = fetch_bench()
    if bench is None:
        bench = (prev or {}).get("bench") or {}
        sys.stderr.write("[WARN] benchmarks FRED+BCE indisponibles — benchmarks précédents conservés\n")

    print("→ Taux 10 ans mensuels ~25 pays (FRED/OCDE)…")
    y10 = {}   # a3 -> [[date,val]] mensuel
    for a3, (iso2, _, _) in COUNTRIES.items():
        if a3 == "USA":
            # mensualiser le quotidien US (dernier point de chaque mois) pour comparabilité
            mo = {}
            for d, v in us_daily:
                if v is not None:
                    mo[d[:7]] = v
            y10[a3] = sorted([[k + "-01", r2(v)] for k, v in mo.items()])
        else:
            sr = fred_series(f"IRLTLT01{iso2}M156N", start="2019-01-01")
            if sr:
                y10[a3] = [[d, r2(v)] for d, v in sr if v is not None]
        time.sleep(0.2)

    print("→ Trajectoires FMI WEO (dette, intérêts, solde, croissance)…")
    weo_debt = imf_weo("GGXWDG_NGDP")       # dette brute %PIB
    weo_ie = imf_weo("ie")                    # intérêts %PIB (histoire)
    weo_fisc = imf_weo("GGXCNL_NGDP")        # solde global %PIB (→2031)
    weo_prim = imf_weo("GGXONLB_G01_GDP_PT") # solde primaire %PIB (→2031)
    weo_g = imf_weo("NGDP_RPCH")             # croissance réelle %
    weo_defl = imf_weo("NGDP_D")             # déflateur (pour croissance nominale) — best-effort

    # Bund de référence pour les spreads (dernier mensuel commun)
    def latest_pair(a3):
        return y10[a3][-1] if y10.get(a3) else None
    de = latest_pair("DEU")
    de_v = de[1] if de else None

    # ── Taux 10 ans LIVE (valeur du jour) via TradingEconomics ──
    print("→ Taux 10 ans LIVE (TradingEconomics)…")
    live = fetch_live_all()
    fresh = set(live)
    # Repli par pays sur la DERNIÈRE VALEUR LIVE CONNUE (cache précédent) plutôt que sur
    # le mensuel OCDE : sinon un pays raté par TE affiche brutalement un taux vieux de
    # 6 semaines (FRA 3,68 au lieu de 3,93) et le compteur « N/27 pays » chute.
    for a3, pe in ((prev or {}).get("countries") or {}).items():
        if a3 in live or a3 not in TE_SLUG:
            continue
        py = pe.get("y10") or {}
        if py.get("live") and py.get("v") is not None and py.get("date"):
            live[a3] = (py["v"], py["date"])
    print(f"   {len(fresh)}/{len(TE_SLUG)} pays rafraîchis ({len(live)} live avec repli)")
    de_live = live.get("DEU")
    de_v = de_live[0] if de_live else de_v   # spreads calculés sur le Bund LIVE

    prev_c = (prev or {}).get("countries") or {}
    countries = {}
    for a3, (iso2, nm, region) in COUNTRIES.items():
        yr = y10.get(a3)
        if not yr:
            # FRED muet pour ce pays (panne transitoire vécue le 2026-07-19) : on repart
            # de la série du cache précédent au lieu de perdre le pays.
            pyr = ((prev_c.get(a3) or {}).get("y10") or {}).get("hist")
            if not pyr:
                continue
            yr = [list(p) for p in pyr]
        mcur = yr[-1]
        lv = live.get(a3)
        is_live = bool(lv)
        cur_v = lv[0] if is_live else mcur[1]
        cur_d = (lv[1] or mcur[0]) if is_live else mcur[0]
        if is_live:
            yr = merge_live_point(yr, cur_d, cur_v)
        cur = [cur_d, cur_v]
        # dette/intérêts/solde
        dy, dv = last_le(weo_debt.get(a3, {}), CUR_YEAR)
        iy, iv = last_le(weo_ie.get(a3, {}), CUR_YEAR)
        # coût moyen = intérêts / dette * 100
        cost_avg = round(iv / dv * 100, 2) if (iv is not None and dv) else None
        cost_marg = cur[1]
        # solde primaire & global (pour besoin de financement)
        fy, fv = last_le(weo_fisc.get(a3, {}), CUR_YEAR)   # solde global (négatif = déficit)
        deficit = -fv if fv is not None else None
        # maturité & roulement
        mat = AVG_MATURITY.get(a3)
        rollover = round(dv / mat, 1) if (mat and dv) else None
        gfn = round((rollover or 0) + max(0.0, deficit or 0.0), 1) if (rollover is not None or deficit is not None) else None
        # spread vs Bund
        spread = round((cur[1] - de_v) * 100) if de_v is not None else None
        entry = {
            "nm": nm, "region": region,
            "y10": {"v": cur[1], "date": cur[0], "live": is_live, "fresh": a3 in fresh,
                    "src": ("TradingEconomics" if a3 in fresh else
                            ("TradingEconomics (dernière valeur connue)" if is_live else "OCDE (mensuel)")),
                    "hist": yr[-84:], "c1m": chg(yr, 1), "c3m": chg(yr, 3), "c12m": chg(yr, 12)},
            "spread_bund": spread,
            "debt_gdp": {"v": r1(dv), "year": dy, "hist": pack_years(weo_debt.get(a3, {}), r1)},
            "int_gdp": {"v": r2(iv), "year": iy},
            "cost_avg": cost_avg, "cost_marg": cost_marg,
            "fiscal": {"v": r1(fv), "year": fy, "hist": pack_years(weo_fisc.get(a3, {}), r1)},
            "avg_maturity": mat, "rollover_pct": rollover, "gfn_gdp": gfn,
        }
        countries[a3] = entry
        print(f"  {a3} {nm}: 10a {cur[1]}% ({cur[0]}) spread {spread}pb · dette {r1(dv)}%PIB · coût moy {cost_avg} vs marg {cost_marg}")

    out = {
        "meta": stamp_meta(countries, len(fresh), "full", int(time.time())),
        "bench": bench,
        "countries": countries,
    }
    return out, len(fresh)


SOURCES_TXT = ("Taux 10 ans : TradingEconomics (valeur du jour, LIVE) — repli OCDE/FRED (mensuel). "
               "Benchmarks quotidiens : FRED (US) · BCE (courbe AAA). Trajectoires : FMI · WEO. "
               "Maturité : DMO/OCDE (indicatif).")


def stamp_meta(countries, n_fresh, mode, struct_ts):
    """Méta HORODATÉE : le front s'en sert pour afficher « màj il y a X min » et pour
    basculer la pastille en « valeurs figées » si le cache décroche (au lieu d'afficher
    un point vert « taux du jour » sur des chiffres vieux de 3 jours)."""
    ld = ""
    for c in countries.values():
        y = c.get("y10") or {}
        if y.get("live") and (y.get("date") or "") > ld:
            ld = y.get("date") or ""
    return {
        "sources": SOURCES_TXT,
        "cur_year": CUR_YEAR,
        "n_countries": len(countries),
        "n_live": sum(1 for c in countries.values() if (c.get("y10") or {}).get("live")),
        "n_fresh": n_fresh,
        "live_date": ld,
        "built_at": time.strftime("%Y-%m-%d %H:%M"),   # heure LOCALE (lisible dans les logs)
        "built_ts": int(time.time()),                   # epoch → « il y a X min » côté front
        "struct_ts": struct_ts,                         # dernier run COMPLET (WEO/mensuel)
        "mode": mode,
        "refresh_min": 120,                             # cadence launchd annoncée au front
        "bench_note": "Taux pays = valeur du jour (TradingEconomics) quand dispo, sinon dernier point mensuel.",
    }


def build_live(prev):
    """Run léger : taux du jour + benchmarks quotidiens, structure WEO reprise du cache."""
    print("→ Benchmarks quotidiens (US FRED, euro BCE)…")
    bench, _ = fetch_bench()
    if bench is None:
        bench = prev.get("bench") or {}
        sys.stderr.write("[WARN] benchmarks indisponibles — benchmarks précédents conservés\n")

    print("→ Taux 10 ans LIVE (TradingEconomics)…")
    live = fetch_live_all()
    fresh = set(live)
    print(f"   {len(fresh)}/{len(TE_SLUG)} pays rafraîchis")

    # ── SAFEGUARD : un run live qui ne rafraîchit (presque) rien ne doit RIEN écrire.
    # Sinon on republierait les mêmes chiffres avec un horodatage neuf = mensonge de
    # fraîcheur (le mtime redevient frais et le watchdog croit le job réparé).
    min_fresh = max(10, int(0.4 * len(TE_SLUG)))
    if len(fresh) < min_fresh:
        sys.stderr.write(f"[ABORT] live : {len(fresh)}/{len(TE_SLUG)} pays rafraîchis "
                         f"(< {min_fresh}) — TradingEconomics probablement bloqué. "
                         "Cache existant CONSERVÉ, aucune écriture.\n")
        sys.exit(1)

    countries = json.loads(json.dumps(prev.get("countries") or {}))   # copie profonde
    de_v = live["DEU"][0] if "DEU" in live else (countries.get("DEU", {}).get("y10") or {}).get("v")
    n_kept = 0
    for a3, e in countries.items():
        lv = live.get(a3)
        if lv:
            apply_live(e, lv[0], lv[1], True, de_v)
        else:
            # pays raté ce coup-ci : on garde sa dernière valeur connue (et on recalcule
            # juste son spread sur le Bund du jour pour rester cohérent avec le tableau)
            y = e.get("y10") or {}
            if y.get("v") is not None:
                y["fresh"] = False
                y["src"] = "TradingEconomics (dernière valeur connue)"
                e["spread_bund"] = round((y["v"] - de_v) * 100) if de_v is not None else None
            n_kept += 1
    if n_kept:
        print(f"   {n_kept} pays sur dernière valeur connue")
    return ({"meta": stamp_meta(countries, len(fresh), "live",
                                int((prev.get("meta") or {}).get("struct_ts") or 0)),
             "bench": bench, "countries": countries},
            len(fresh))


def write_out(out):
    countries = out["countries"]
    # ── SAFEGUARD anti-régression : ne JAMAIS écraser un cache valide par un run vide ──
    # (2026-07-19 : FRED a échoué transitoirement → cache 0 pays écrasé → vue « 0/0 pays ».)
    MIN_COUNTRIES = 10
    if len(countries) < MIN_COUNTRIES:
        sys.stderr.write(
            f"[ABORT] {len(countries)} pays (< {MIN_COUNTRIES}) — sources probablement en panne "
            "(FRED/OCDE/TE). Cache existant CONSERVÉ, aucune écriture.\n")
        sys.exit(1)

    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    hdr = "/* atlas_debt_cache.js — Dette souveraine (FRED/BCE/FMI) */"
    for base in CACHES:
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            # écriture ATOMIQUE : un lecteur (site local, deploy) ne doit jamais tomber
            # sur un JSON à moitié écrit → JSON.parse cassé côté page.
            for ext, txt in ((".json", blob),
                             (".js", hdr + "\nwindow.__ATLAS_DEBT__ = " + blob + ";\n")):
                tmp = base + ext + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(txt)
                os.replace(tmp, base + ext)
            print(f"écrit {base}.json ({len(blob)//1024} Ko)")
        except Exception as e:
            sys.stderr.write(f"[WARN] écriture {base}: {e}\n")


def main():
    mode = "auto"
    for a in sys.argv[1:]:
        if a in ("--full", "--live", "--auto"):
            mode = a[2:]
    prev = load_prev()
    if mode == "auto":
        st = int(((prev or {}).get("meta") or {}).get("struct_ts") or 0)
        age_h = (time.time() - st) / 3600.0 if st else 1e9
        n_prev = len((prev or {}).get("countries") or {})
        mode = "live" if (prev and n_prev >= 10 and age_h < STRUCT_MAX_H) else "full"
        print(f"→ mode {mode} (structure vieille de "
              f"{'∞' if age_h > 1e6 else round(age_h, 1)} h, {n_prev} pays en cache)")
    if mode == "live" and not prev:
        mode = "full"

    out, n_fresh = build_live(prev) if mode == "live" else build_full(prev)
    write_out(out)
    m = out["meta"]
    print(f"OK — {m['n_countries']} pays · {n_fresh} taux rafraîchis · "
          f"{m['n_live']} live · mode {m['mode']} · {m['built_at']}.")


if __name__ == "__main__":
    main()
