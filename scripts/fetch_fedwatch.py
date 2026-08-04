#!/usr/bin/env python3
"""CME FedWatch — reproduction maison des probabilites de taux Fed.

Pourquoi maison : CME bloque le scraping (403 + interdit par les ToS) et leur API
FedWatch officielle est payante. On reconstruit la donnee a partir des 30-Day Fed
Funds futures (contrats ZQ) que Yahoo Finance expose gratuitement par mois
(ZQ<MoisCode><YY>.CBT) — prix identiques au strip CME a la base de points pres.

Methodo (vue "Aggregated" du FedWatch, celle du screenshot user) :
  - Le contrat ZQ d'un mois settle a 100 - (EFFR moyen du mois).
  - Pour chaque reunion FOMC, on decompose le contrat du mois de reunion en une
    portion pre-reunion (taux courant connu) + post-reunion (inconnu) via le split
    des jours autour de la date de decision -> taux directeur implicite post-reunion.
  - Ce taux cumule implicite est reparti lineairement entre les 2 fourchettes cibles
    25 bps adjacentes (midpoint = borne_basse + 12.5 bps) -> probabilites par reunion.

Sorties (ecrites dans ~/Library/Caches/site_crypto_finance/) :
  fedwatch_cache.json  +  fedwatch_cache.js -> window.__FEDWATCH_LIVE__

Lance par launchd scf.fedwatch.refresh (toutes les 2h). 100% urllib,
aucune dependance externe (meme philosophie que fetch_macro_fred_data.py).
"""
import json
import sys
import math
import time
import calendar
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "fedwatch_cache.json"
OUT_JS   = CACHE_DIR / "fedwatch_cache.js"
CACHE_MAX_HOURS = 48  # merge-preserve si run partiel

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance/1.0"

# Codes de mois CME (Jan..Dec)
MONTH_CODE = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}

# Volume cumule minimal (15 dernieres seances) pour considerer un contrat ZQ comme
# REELLEMENT cote sur Yahoo. Les echeances lointaines (>~14 mois) tombent a ~0 volume :
# Yahoo y publie quand meme un "close" fantome (mark de reglement) non negociable qui
# fausse la decomposition (ex. ZQU27 = 0 vol/jour -> 3.88% aberrant). Falaise nette
# observee (~23 000 vs 1) -> seuil large et robuste. Au-dela, l'horizon est tronque.
LIQ_MIN_VOL = 500

# Calendrier FOMC : dates de DECISION (2e jour de reunion). Source : federalreserve.gov
# + screenshot CME user. Extensible — ajouter les annees au fil de l'eau.
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28",
    "2027-09-15", "2027-10-27", "2027-12-08",
]

FR_MONTH = ["", "Jan", "Fev", "Mar", "Avr", "Mai", "Juin", "Juil", "Aout",
            "Sep", "Oct", "Nov", "Dec"]


# ─────────────────────────── HTTP helpers ───────────────────────────
def http_get_text(url, timeout=20, max_retries=5):
    """GET avec retry exponentiel (cold-boot launchd / DNS flaps)."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(k in msg for k in ("nodename", "servname", "timed out",
                                      "refused", "unreachable", "name or service",
                                      "temporarily")):
                wait = 4 * (2 ** attempt)
                sys.stderr.write(f"[NET] retry {attempt+1}/{max_retries} after {wait}s: {e}\n")
                time.sleep(wait)
                continue
            raise
    raise last_err


def fetch_yahoo_contract(symbol):
    """Historique quotidien complet d'un contrat/symbole Yahoo.
    Retourne (dates[], values[], volumes[]) tries chronologiquement, ou ([],[],[]) si echec.
    Le volume sert a ecarter les echeances ZQ lointaines quasi non-cotees (prints fantomes)."""
    try:
        sym_enc = symbol.replace("=", "%3D")
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}"
               f"?range=max&interval=1d")
        d = json.loads(http_get_text(url, timeout=25))
        r = d.get("chart", {}).get("result", [{}])[0]
        ts = r.get("timestamp", []) or []
        q = r.get("indicators", {}).get("quote", [{}])[0]
        n = len(ts)
        close = ((q.get("close", []) or []) + [None] * n)[:n]
        vol = ((q.get("volume", []) or []) + [None] * n)[:n]
        out_d, out_v, out_vol = [], [], []
        for t, c, v in zip(ts, close, vol):
            if c is None:
                continue
            out_d.append(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"))
            out_v.append(round(float(c), 4))
            out_vol.append(int(v) if v is not None else 0)
        return out_d, out_v, out_vol
    except Exception as e:
        sys.stderr.write(f"[YAHOO] {symbol}: {e}\n")
        return [], [], []


def fetch_fred_series(series_id, start="2005-01-01"):
    """FRED via helper partage (API officielle, cle gratuite)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _fred_helpers import fetch_fred as _fred_api
        obs = _fred_api(series_id, start=start)
        if obs is None:
            return [], []
        return obs["dates"], obs["values"]
    except Exception as e:
        sys.stderr.write(f"[FRED] {series_id}: {e}\n")
        return [], []


# ─────────────────────────── FedWatch algo ───────────────────────────
def contract_symbol(year, month):
    return f"ZQ{MONTH_CODE[month]}{year % 100:02d}.CBT"


def price_asof(series, asof):
    """Dernier prix <= asof (str YYYY-MM-DD) dans series={'dates','values','vols'}.
    Privilegie le dernier close avec volume>0 (ecarte les marks fantomes des echeances
    peu liquides) ; retombe sur le dernier close dispo si aucune seance n'a echange."""
    dts = series["dates"]; vals = series["values"]; vols = series.get("vols") or []
    last_any = None; last_traded = None
    for i, (dd, vv) in enumerate(zip(dts, vals)):
        if dd > asof:
            break
        last_any = vv
        if i < len(vols) and vols[i]:
            last_traded = vv
    return last_traded if last_traded is not None else last_any


def is_liquid(series, lookback=15):
    """Le contrat est-il REELLEMENT negocie ? Somme du volume des `lookback` dernieres
    seances >= LIQ_MIN_VOL. Sert a tronquer l'horizon a la portee fiable (au-dela, les
    contrats ZQ ne s'echangent quasiment plus sur Yahoo -> prix non exploitables)."""
    if not series:
        return False
    vols = series.get("vols") or []
    return sum(v for v in vols[-lookback:] if v) >= LIQ_MIN_VOL


def step_dist(delta):
    """Distribution du pas (en multiples de 25 bps) d'UNE reunion dont le mouvement
    attendu est `delta` (en %). On repartit |delta|/25bps entre les deux pas entiers
    encadrants -> c'est la regle CME (proba de hausse/baisse a une reunion = mouvement
    attendu / 25 bps). Ex. delta=+0.075 % -> {0:0.70, +1:0.30} ; delta=-0.105 % ->
    {-1:0.42, 0:0.58}. Renvoie {pas_entier: proba} (pas pouvant etre negatif = baisse)."""
    s = delta / 0.25
    lo = math.floor(s)
    return {lo: (lo + 1 - s), lo + 1: (s - lo)}


def bucket_label(lower):
    return f"{int(round(lower*100))}-{int(round((lower+0.25)*100))}"


def _next_month(y, mo):
    return (y + 1, 1) if mo == 12 else (y, mo + 1)


def compute_path(strip, meetings, r0_mid, asof):
    """Calcule la trajectoire des taux post-reunion + probas, as-of une date donnee.
    strip : {(year,month): {'dates','values'}}. Retourne liste de meetings enrichis.

    Robustesse : pour une reunion en fin de mois (peu de jours apres -> decomposition
    numeriquement instable), on lit le taux post-reunion directement sur le contrat du
    1er mois SANS reunion qui suit (entierement au taux prevalant) plutot que de
    decomposer. C'est l'astuce standard du calcul FedWatch."""
    meeting_months = {(m["y"], m["m"]) for m in meetings}

    def avg_month(yy, mm):
        s = strip.get((yy, mm))
        if not s or not is_liquid(s):
            return None
        p = price_asof(s, asof)
        return None if p is None else 100.0 - p

    out = []
    r_prev = r0_mid
    for m in meetings:
        y, mo, day = m["y"], m["m"], m["d"]
        avg = avg_month(y, mo)
        if avg is None:
            break  # contrat du mois de reunion non liquide -> horizon tronque (au-dela = bruit)
        N = calendar.monthrange(y, mo)[1]
        d_after = N - day
        ny, nmo = _next_month(y, mo)
        avg_next = avg_month(ny, nmo) if (ny, nmo) not in meeting_months else None

        if d_after < 8 and avg_next is not None:
            r_post = avg_next                      # reunion fin de mois -> mois clean suivant
        else:
            d_before = min(max(day, 1), N - 1)
            r_post = (avg * N - r_prev * d_before) / (N - d_before)
            if avg_next is not None and abs(r_post - r_prev) > 0.6:
                r_post = avg_next                  # garde-fou instabilite
        out.append({
            "date": m["date"], "y": y, "m": mo,
            "label": f"{day} {FR_MONTH[mo]} {str(y)[2:]}",
            "month_label": f"{FR_MONTH[mo]} {y}",
            "implied_rate": round(r_post, 4),
            "prev_rate": round(r_prev, 4),
            "move_bps": round((r_post - r_prev) * 100, 1),
        })
        r_prev = r_post

    # ── Vue "Conditional" de CME : arbre binomial de pas +/-25 bps ──────────────
    # Chaque reunion deplace le taux d'un pas de 25 bps avec proba |Δ|/25bps (signe de Δ
    # = sens du mouvement attendu). On CONVOLUE en avancant -> distribution complete qui
    # s'elargit avec l'horizon (vs l'ancienne interpolation 2 fourchettes = vue "Aggregated").
    # Niveau 0 = fourchette courante (lower0). bucket_lower(niveau n) = lower0 + n*0.25.
    lower0 = round(r0_mid - 0.125, 3)
    dist = {0: 1.0}
    r_tree = r0_mid
    for mt in out:
        sd = step_dist(mt["implied_rate"] - r_tree)
        nd = {}
        for lvl, p in dist.items():
            for st, ps in sd.items():
                if ps <= 0:
                    continue
                nd[lvl + st] = nd.get(lvl + st, 0.0) + p * ps
        dist = nd
        r_tree = mt["implied_rate"]
        mt["dist"] = {round(lower0 + lvl * 0.25, 3): pr for lvl, pr in dist.items()}
    return out


def build_meetings_payload(path_today, r0_mid):
    """Construit la grille rectangulaire (heatmap) + meta par reunion a partir de la
    distribution conditionnelle (arbre binomial) calculee dans compute_path."""
    if not path_today:
        return [], []
    # Colonnes = union des fourchettes ayant une proba non negligeable (>=0.1%), contigues.
    PROB_MIN = 0.001
    lowers = set()
    for mt in path_today:
        for lo, pct in mt.get("dist", {}).items():
            if pct > PROB_MIN:
                lowers.add(round(lo, 3))
    if not lowers:
        lowers = {round(r0_mid - 0.125, 3)}
    lo_min, lo_max = min(lowers), max(lowers)
    cols = []
    x = lo_min
    while x <= lo_max + 1e-9:
        cols.append(round(x, 3))
        x += 0.25
    col_labels = [bucket_label(c) for c in cols]

    meetings_out = []
    for mt in path_today:
        bmap = {round(lo, 3): pct for lo, pct in mt.get("dist", {}).items()}
        row = {bucket_label(c): round(bmap.get(round(c, 3), 0.0) * 100, 2) for c in cols}
        meetings_out.append({
            "date": mt["date"], "label": mt["label"], "month_label": mt["month_label"],
            "implied_rate": mt["implied_rate"], "prev_rate": mt["prev_rate"],
            "move_bps": mt["move_bps"],
            "cuts_cum_bps": round((mt["implied_rate"] - r0_mid) * 100, 1),
            "probs": row,
        })
    return col_labels, meetings_out


# ─────────────────────────── main ───────────────────────────
def main():
    today = date.today()
    today_s = today.strftime("%Y-%m-%d")
    sys.stderr.write(f"[START] FedWatch fetch {today_s}\n")

    # 1) Range cible courant (FRED DFEDTARU/DFEDTARL) + EFFR (DFF)
    du_d, du_v = fetch_fred_series("DFEDTARU", start="2020-01-01")
    dl_d, dl_v = fetch_fred_series("DFEDTARL", start="2020-01-01")
    upper = du_v[-1] if du_v else 3.75
    lower = dl_v[-1] if dl_v else 3.50
    r0_mid = (upper + lower) / 2.0
    asof_range = du_d[-1] if du_d else today_s
    dff_d, dff_v = fetch_fred_series("DFF", start="2006-01-01")
    effr = dff_v[-1] if dff_v else r0_mid
    sys.stderr.write(f"[RANGE] {lower}-{upper} (mid={r0_mid}) EFFR={effr}\n")

    # 2) Reunions futures uniquement
    meetings = []
    for ds in FOMC_DATES:
        dt = datetime.strptime(ds, "%Y-%m-%d").date()
        if dt >= today:
            meetings.append({"date": ds, "y": dt.year, "m": dt.month, "d": dt.day})
    if not meetings:
        sys.stderr.write("[WARN] aucune reunion future — etendre FOMC_DATES\n")

    # 3) Strip de contrats : du mois courant -> +18 mois (couvre toutes les reunions)
    strip = {}
    months_seq = []
    y, mo = today.year, today.month
    for _ in range(19):
        months_seq.append((y, mo))
        mo += 1
        if mo > 12:
            mo = 1; y += 1
    for (yy, mm) in months_seq:
        sym = contract_symbol(yy, mm)
        dts, vals, vols = fetch_yahoo_contract(sym)
        if dts:
            strip[(yy, mm)] = {"dates": dts, "values": vals, "vols": vols, "symbol": sym}
        time.sleep(0.25)
    n_liq = sum(1 for s in strip.values() if is_liquid(s))
    sys.stderr.write(f"[STRIP] {len(strip)}/{len(months_seq)} contrats Yahoo OK "
                     f"({n_liq} liquides >= {LIQ_MIN_VOL} vol)\n")

    # 4) Trajectoire + probas as-of aujourd'hui et overlays passes
    d_w1 = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    d_m1 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    path_today = compute_path(strip, meetings, r0_mid, today_s)
    path_w1 = compute_path(strip, meetings, r0_mid, d_w1)
    path_m1 = compute_path(strip, meetings, r0_mid, d_m1)

    col_labels, meetings_out = build_meetings_payload(path_today, r0_mid)

    # 5) Strip d'affichage (prix courant + taux implicite) — contrats LIQUIDES seulement,
    #    pour ne pas afficher les marks fantomes des echeances lointaines non cotees.
    futures = []
    for (yy, mm) in months_seq:
        s = strip.get((yy, mm))
        if not s or not is_liquid(s):
            continue
        px = price_asof(s, today_s)
        if px is None:
            continue
        futures.append({
            "contract": s["symbol"].replace(".CBT", ""),
            "label": f"{FR_MONTH[mm]} {yy}", "y": yy, "m": mm,
            "price": px, "implied_rate": round(100.0 - px, 4),
        })

    # 6) Historique "max" : taux directeur effectif reel (DFF) + projection implicite
    policy_hist = {"dates": dff_d, "dff": dff_v}
    proj_path = ([{"date": asof_range, "rate": round(r0_mid, 4)}] +
                 [{"date": mt["date"], "rate": mt["implied_rate"]} for mt in path_today])

    # 7) Series d'anticipation par horizon : pour les contrats Dec N et Dec N+1, evolution
    #    du nb net de mouvements 25 bps prices par cette echeance (implicite - taux reel DFF).
    cuts_hist = build_cuts_hist(strip, dff_d, dff_v, today)

    # 8) Trajectoires pour le chart (today + overlays)
    def path_xy(p):
        return [{"date": mt["date"], "label": mt["label"], "rate": mt["implied_rate"]}
                for mt in p]
    rate_path = {"today": path_xy(path_today), "w1": path_xy(path_w1), "m1": path_xy(path_m1)}

    # 9) Sanity-check : abandonner si le run est vide (preserve l'ancien cache)
    if not meetings_out or not futures:
        sys.stderr.write("[ABORT] run vide (no meetings/futures) — cache existant preserve\n")
        if OUT_JSON.exists():
            return
        # premier run jamais : ecrire quand meme pour ne pas laisser un 404

    payload = {
        "current_range": {
            "lower": lower, "upper": upper, "mid": round(r0_mid, 4),
            "effr": effr, "label": bucket_label(round(lower, 3)), "asof": asof_range,
        },
        "columns": col_labels,
        "meetings": meetings_out,
        "futures": futures,
        "rate_path": rate_path,
        "proj_path": proj_path,
        "policy_hist": policy_hist,
        "cuts_hist": cuts_hist,
        "fetch_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M CEST"),
        "fetch_iso": datetime.now().isoformat(),
        "coverage": {"contracts_ok": len(strip), "contracts_total": len(months_seq),
                     "contracts_liquid": sum(1 for s in strip.values() if is_liquid(s)),
                     "meetings": len(meetings_out),
                     "horizon": meetings_out[-1]["month_label"] if meetings_out else None},
        "sources": {
            "futures": "30-Day Fed Funds futures (ZQ) via Yahoo Finance /v8/finance/chart",
            "range": "FRED DFEDTARU/DFEDTARL (Federal Reserve)",
            "effr": "FRED DFF (effective fed funds rate)",
        },
        "disclaimer": ("Estimation calculee a partir des Fed Funds futures (methodo CME "
                       "FedWatch 'Conditional' : arbre binomial de pas +/-25 bps par reunion). "
                       "Non affiliee a CME Group. Reference officielle : "
                       "cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"),
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUT_JS.write_text(
        "(function(){window.__FEDWATCH_LIVE__=" + compact + ";})();",
        encoding="utf-8")
    sys.stderr.write(
        f"[DONE] {len(meetings_out)} reunions · {len(futures)} contrats · "
        f"range {lower}-{upper} · {OUT_JSON.stat().st_size//1024} KB\n")


def build_cuts_hist(strip, dff_d, dff_v, today):
    """Pour les contrats Dec de l'annee courante et suivante, evolution dans le temps du
    nb net de mouvements 25 bps prices par cette echeance = (taux implicite du contrat a
    la date t - taux directeur reel DFF a t) / 0.25. Montre comment l'anticipation de
    resserrement/assouplissement a migre. Profondeur = duree de cotation du contrat (~1-2 ans)."""
    def dff_asof(ds):
        chosen = None
        for d_, v_ in zip(dff_d, dff_v):
            if d_ <= ds:
                chosen = v_
            else:
                break
        return chosen

    series = []
    for yr in (today.year, today.year + 1):
        key = (yr, 12)
        s = strip.get(key)
        if not s or not is_liquid(s):
            continue  # contrat Dec lointain non cote -> serie d'anticipation non fiable
        pts_d, pts_v = [], []
        for d_, v_ in zip(s["dates"], s["values"]):
            base = dff_asof(d_)
            if base is None:
                continue
            pts_d.append(d_)
            pts_v.append(round(((100.0 - v_) - base) / 0.25, 2))
        if len(pts_d) > 5:
            series.append({"label": f"Fin {yr}", "dates": pts_d, "values": pts_v})
    note = ("Nb net de mouvements 25 bps prices par l'echeance (positif = hausses, negatif "
            "= baisses) vs le taux directeur effectif reel (FRED DFF), au fil du temps.")
    return {"series": series, "note": note}


if __name__ == "__main__":
    main()
