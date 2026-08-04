#!/usr/bin/env python3
"""Historique P/E + revenus par action (TradFi) -> series temporelles + agregats secteur.

Sources (gratuites, auditables, AUCUN Yahoo pour le fonda — cf project_yahoo_curl_cffi_required):
  - US (sans suffixe)        : macrotrends.net (P/E trimestriel ~20 ans, revenus ~15 ans)
                               + lien audit SEC EDGAR (companyfacts) par CIK.
  - International (.HK/.L/...) : stockanalysis.com __data.json (devalue, annuel ~5-6 ans).

Sortie : tradfi_hist_cache.json / .js (window.__TRADFI_HIST__), charge en runtime par
Comparaison_PER_Crypto_TradFi.html. Chaque serie porte sa source_url (auditabilite).

Agregation secteur : moyenne ponderee mcap (poids = mcap courant du cache fonda, source
unique) des P/E de chaque constituant a chaque date trimestrielle. coverage = part du mcap
secteur disposant d'une donnee a cette date. Cohérent avec le snapshot winsorise 5/95.

CLI:
  --sample          : ~12 valeurs temoins (US + plusieurs places) pour valider.
  --limit N         : limite a N actions (debug).
  --sectors "A,B"   : restreint a certains secteurs.
"""
# ── Global timeout : 50 min (gros scrape multi-source). Auto-tue si bloque sur I/O,
#    libère le lock pour le prochain cycle launchd (cf fetch_pe_hist pattern).
import os
import signal as _signal, sys as _sys
def _to(signum, frame):
    print("[fatal] global timeout (75 min) — abort to free launchd lock.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _to); _signal.alarm(75 * 60)
except Exception:
    pass

import json, re, time, random, argparse, warnings
from pathlib import Path
from datetime import datetime, timezone
warnings.filterwarnings("ignore")

try:
    from curl_cffi import requests as _cr
    SESS = _cr.Session(impersonate="chrome120")
except Exception:
    import requests as _rq
    SESS = _rq.Session()

UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")}

CACHES = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHES.mkdir(parents=True, exist_ok=True)
FUND_CACHE = CACHES / "tradfi_fundamentals_cache.json"
OUT_JSON   = CACHES / "tradfi_hist_cache.json"
OUT_JS     = CACHES / "tradfi_hist_cache.js"
SLUG_CACHE = CACHES / "tradfi_hist_slugs.json"
HTML_FILES = [Path(__file__).parent / "Comparaison_PER_Crypto_TradFi.html"]

# Yahoo suffix -> code place stockanalysis (best-effort ; lon/hkg/jse verifies).
SA_EXCHANGE = {
    "L": "lon", "HK": "hkg", "PA": "epa", "DE": "etr", "SW": "swx", "MC": "bme",
    "MI": "bit", "AS": "ams", "BR": "ebr", "HE": "hel", "CO": "cph", "OL": "ose",
    "ST": "sto", "TO": "tsx", "T": "tyo", "KS": "krx", "TW": "tpe", "NS": "nse",
    "SI": "sgx", "AX": "asx", "SS": "sha", "SZ": "she", "JK": "idx", "JO": "jse",
    "BK": "set", "KL": "klse", "SR": "tadawul", "AE": "adx", "MX": "bmv",
    "SA": "bvmf", "PS": "pse",
}

# ───────────────────────── helpers HTTP ─────────────────────────
def _get(url, allow_redirects=True, tries=3):
    """200 -> response. 404/403/410 -> None IMMEDIAT (page absente, inutile de retry).
    5xx + exceptions reseau -> retry court."""
    for a in range(tries):
        try:
            r = SESS.get(url, headers=UA, timeout=20, allow_redirects=allow_redirects)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 403, 410):
                return None  # definitif : ticker/place inexistant -> coverage gap
        except Exception:
            pass
        if a < tries - 1:
            time.sleep(1.2 * (a + 1) + random.uniform(0, 0.6))
    return None

# ───────────────────────── MACROTRENDS (US) ─────────────────────────
_slugs = {}
_mt_last = [0.0]        # throttle : macrotrends bloque 2 requetes trop rapprochees
_mt_429_streak = [0]    # 429 consecutifs
_mt_giveup = [False]    # apres trop de 429, on abandonne macrotrends pour CE run (US -> prochain --resume)
_MT_BASE_GAP = 3.0      # gap mini entre 2 requetes macrotrends
_MT_COOLDOWN = 30.0     # pause sur 429 (rate-limit IP)
_MT_GIVEUP_AT = 8       # 429 consecutifs -> giveup

def _mt_request(url):
    """GET macrotrends avec throttle 3s + cooldown 30s sur 429. None si echec/giveup.
    Retourne (response|None, status_int|None)."""
    if _mt_giveup[0]:
        return None
    gap = time.time() - _mt_last[0]
    if gap < _MT_BASE_GAP:
        time.sleep(_MT_BASE_GAP - gap + random.uniform(0, 0.4))
    for attempt in range(3):
        try:
            r = SESS.get(url, headers=UA, timeout=20, allow_redirects=True)
            _mt_last[0] = time.time()
            if r.status_code == 200:
                _mt_429_streak[0] = 0
                return r
            if r.status_code in (404, 403, 410):
                _mt_429_streak[0] = 0
                return None  # page absente
            if r.status_code in (429, 402, 503):
                _mt_429_streak[0] += 1
                if _mt_429_streak[0] >= _MT_GIVEUP_AT:
                    _mt_giveup[0] = True
                    print(f"  [macrotrends] {_MT_GIVEUP_AT} x 429 consecutifs -> giveup US pour ce run "
                          f"(rattrape au prochain --resume)", file=_sys.stderr, flush=True)
                    return None
                print(f"  [macrotrends] {r.status_code} -> cooldown {_MT_COOLDOWN:.0f}s "
                      f"(streak {_mt_429_streak[0]})", file=_sys.stderr, flush=True)
                time.sleep(_MT_COOLDOWN)
                continue
        except Exception:
            time.sleep(3)
        _mt_last[0] = time.time()
    return None

def _load_slugs():
    global _slugs
    try:
        raw = json.loads(SLUG_CACHE.read_text())
        _slugs = {k: v for k, v in raw.items() if v}  # ignore les null persistes
    except Exception:
        _slugs = {}

def _mt_parse(text):
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", text, re.DOTALL):
        if not re.search(r"\d{4}-\d{2}-\d{2}", tr): continue
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
        if cells and re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            out.append(cells)
    return out

def fetch_us(ticker):
    """US via macrotrends (2 requetes). Le slug est appris depuis la redirection de la
    requete pe-ratio (pas de requete slug separee) puis reutilise pour revenue."""
    # macrotrends utilise un point pour les class-shares (BRK-B -> BRK.B, BF-B -> BF.B).
    # Le symbole univers (style Yahoo) garde le tiret -> on convertit pour l'URL seulement.
    mt = ticker.replace("-", ".")
    slug = _slugs.get(ticker)
    # 1) pe-ratio : si slug inconnu, on passe /x/ et macrotrends 301-redirige vers le bon slug
    r_pe = _mt_request(f"https://www.macrotrends.net/stocks/charts/{mt}/{slug or 'x'}/pe-ratio")
    if r_pe is None: return None
    if not slug:
        m = re.search(rf"/stocks/charts/{re.escape(mt)}/([^/]+)/", str(r_pe.url))
        slug = m.group(1) if (m and m.group(1) != "x") else None
        if slug:
            _slugs[ticker] = slug
            try: SLUG_CACHE.write_text(json.dumps(_slugs))
            except Exception: pass
    if not slug: return None
    pe_rows = _mt_parse(r_pe.text)                     # [date, price, eps_ttm, pe]
    # 2) revenue
    r_rev = _mt_request(f"https://www.macrotrends.net/stocks/charts/{mt}/{slug}/revenue")
    rev_rows = _mt_parse(r_rev.text) if r_rev is not None else []  # [date, $rev_millions]
    def num(s):
        s = s.replace("$", "").replace(",", "").replace("%", "").strip()
        try: return float(s)
        except Exception: return None
    pe = {"dates": [], "vals": []}
    eps = {"dates": [], "vals": []}
    for c in reversed(pe_rows):  # chrono croissant
        if len(c) >= 4 and num(c[3]) is not None:
            pe["dates"].append(c[0]); pe["vals"].append(num(c[3]))
        if len(c) >= 3 and num(c[2]) is not None:
            eps["dates"].append(c[0]); eps["vals"].append(num(c[2]))
    rev = {"dates": [], "vals": []}
    for c in reversed(rev_rows):
        if len(c) >= 2 and num(c[1]) is not None:
            rev["dates"].append(c[0]); rev["vals"].append(num(c[1]))  # $M
    if not pe["vals"] and not rev["vals"]:
        return None
    base = f"https://www.macrotrends.net/stocks/charts/{mt}/{slug}"
    return {
        "src": "macrotrends",
        "pe": pe, "eps_ttm": eps, "revenue": rev, "revenue_unit": "M USD",
        "source_url": {"pe": f"{base}/pe-ratio", "revenue": f"{base}/revenue",
                       "audit_sec": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker={mt}&type=10-K"},
    }

# ───────────────────────── STOCKANALYSIS (international) ─────────────────────────
def _devalue_resolve(arr, idx, _depth=0):
    """Deref devalue : un index pointe vers arr[index] ; dict/list resolus recursivement."""
    if _depth > 40: return None
    v = arr[idx] if isinstance(idx, int) and 0 <= idx < len(arr) else idx
    if isinstance(v, dict):
        return {k: _devalue_resolve(arr, j, _depth + 1) for k, j in v.items()}
    if isinstance(v, list):
        return [_devalue_resolve(arr, j, _depth + 1) for j in v]
    return v

def _sa_financial_data(path, quarterly=False):
    """Retourne le dict financialData (datekey, fiscalYear, revenue, eps...) ou None.
    quarterly=True -> ajoute ?p=quarterly (densite ~20 trimestres vs ~6 annuels).
    Garde-fou schema : leve si la structure attendue a change (anti-regression silencieuse)."""
    url = f"https://stockanalysis.com/{path}/__data.json" + ("?p=quarterly" if quarterly else "")
    r = _get(url)
    if r is None: return None
    try:
        d = r.json()
    except Exception:
        return None
    nodes = d.get("nodes", [])
    arr = None
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get("data"), list) and len(n["data"]) > 50:
            cand = n["data"]
            if isinstance(cand[0], dict) and "financialData" in cand[0]:
                arr = cand; break
    if arr is None:
        return None
    root = arr[0]
    fd = _devalue_resolve(arr, root["financialData"])
    if not isinstance(fd, dict) or "datekey" not in fd:
        raise RuntimeError(f"SCHEMA stockanalysis change ({path}): financialData keys={list(fd)[:8] if isinstance(fd,dict) else fd}")
    return fd

def fetch_intl(ticker, suffix):
    exch = SA_EXCHANGE.get(suffix)
    if not exch: return None
    # stockanalysis utilise un point pour les class-shares nordiques (VOLV-B -> VOLV.B,
    # ATCO-A -> ATCO.A, NDA-FI -> NDA.FI). Le symbole univers (Yahoo) garde le tiret.
    base_tkr = ticker.split(".")[0].replace("-", ".")
    qpath = f"quote/{exch}/{base_tkr}/financials"
    # income statement (revenue, eps) — quarterly d'abord (densite ~20 pts), fallback annuel
    fd = None
    for q in (True, False):
        try:
            fd = _sa_financial_data(qpath, quarterly=q)
        except RuntimeError as e:
            print(f"  [schema] {ticker}: {e}", file=_sys.stderr); fd = None
        if fd and isinstance(fd.get("datekey"), list) and len(fd["datekey"]) >= 4:
            break
    if not fd: return None
    dates = fd.get("datekey") or []
    def series(key):
        vals = fd.get(key)
        if not isinstance(vals, list): return {"dates": [], "vals": []}
        ds, vs = [], []
        for dt, v in zip(dates, vals):
            if dt == "TTM" or v is None: continue
            ds.append(dt); vs.append(v)
        ds, vs = ds[::-1], vs[::-1]  # chrono croissant
        return {"dates": ds, "vals": vs}
    rev = series("revenue")
    eps = series("eps") if "eps" in fd else series("epsDiluted")
    # P/E : page ratios (peRatio) — quarterly d'abord, fallback annuel
    pe = {"dates": [], "vals": []}
    fr = None
    for q in (True, False):
        try:
            fr = _sa_financial_data(f"quote/{exch}/{base_tkr}/financials/ratios", quarterly=q)
        except RuntimeError:
            fr = None
        if fr and isinstance(fr.get("datekey"), list) and len(fr["datekey"]) >= 4:
            break
    if fr:
        rdates = fr.get("datekey") or []
        pev = fr.get("peRatio") or fr.get("pe")
        if isinstance(pev, list):
            ds, vs = [], []
            for dt, v in zip(rdates, pev):
                if dt == "TTM" or v is None: continue
                ds.append(dt); vs.append(v)
            pe = {"dates": ds[::-1], "vals": vs[::-1]}
    if not rev["vals"] and not pe["vals"]:
        return None
    url = f"https://stockanalysis.com/quote/{exch}/{base_tkr}/financials/"
    return {
        "src": "stockanalysis",
        "pe": pe, "eps_ttm": eps, "revenue": rev, "revenue_unit": "native",
        "source_url": {"pe": url + "ratios/", "revenue": url, "audit_sec": None},
    }

# ───────────────────────── agregation secteur ─────────────────────────
def quarter_grid(start_year=2010):
    now = datetime.now(timezone.utc)
    qs = []
    for y in range(start_year, now.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            iso = f"{y:04d}-{m:02d}-{d:02d}"
            if iso <= now.strftime("%Y-%m-%d"):
                qs.append(iso)
    return qs

def _val_asof(series, qdate):
    """Derniere valeur de la serie a/avant qdate (forward-fill ≤ 200 jours)."""
    best = None
    for dt, v in zip(series["dates"], series["vals"]):
        if dt <= qdate:
            best = (dt, v)
        else:
            break
    if best is None: return None
    # ne pas forward-fill au-dela de ~1 an (eviter de figer une vieille valeur)
    d0 = datetime.strptime(best[0], "%Y-%m-%d"); d1 = datetime.strptime(qdate, "%Y-%m-%d")
    if (d1 - d0).days > 400: return None
    return best[1]

COV_FLOOR = 0.50   # ne pas emettre un point secteur sous 50% de la mcap couverte
                   # (evite les eres "US-seul" trompeuses ou 1 micro-cap represente le secteur)

def aggregate_sector(stock_entries, grid, total_sector_mcap):
    """stock_entries: [{mcap_b, hist:{pe:{dates,vals}}}].

    P/E secteur = ΣMarketCap / ΣEarnings = moyenne HARMONIQUE des P/E ponderee mcap
    (= Σw / Σ(w/pe)). C'est la methodologie indicielle standard (S&P) :
      - currency-safe : pe est sans dimension, donc additionner w/pe (w en USD) est valide
        meme avec des constituants en devises differentes ;
      - ne rejette PAS les hauts P/E : une mega-cap a PE 358 (TSLA) contribue w/358 (peu
        de benefices) au lieu d'etre ejectee par un cap brutal — fini les faux krachs
        quand un poids lourd franchit un seuil ;
      - les P/E <= 0 (pertes, placeholder macrotrends 0.00) restent non representables ->
        exclus (limite de la source) ; >5000 = bruit de donnee (benefices ~nuls) -> exclus.

    Garde-fou bas (PE < 3 exclus) : la moyenne harmonique est hyper-sensible aux P/E
    tres bas (un titre a PE 1 contribue 1/1 d'earnings, dominant le secteur). Or les P/E
    < 3 sont quasi toujours des erreurs de source (ex. colonne PE cassee de macrotrends
    pour BKNG ~1.1 au lieu de ~30) ou des gains one-off (badwill, deferred-tax) non
    representatifs. On exclut donc < 3 ; les vrais creux cycliques (PE 3-5) restent.

    coverage = part du mcap TOTAL du secteur disposant d'un P/E valide a cette date.
    Un point n'est emis que si coverage >= COV_FLOOR (serie temporelle comparable)."""
    out_dates, out_pe, out_cov = [], [], []
    total_mcap = total_sector_mcap or 1.0
    for q in grid:
        inv_earn, covered = 0.0, 0.0   # inv_earn = Σ(w/pe) ~ benefices agreges (USD)
        for s in stock_entries:
            w = s["mcap_b"] or 0
            if w <= 0: continue
            pe = _val_asof(s["hist"]["pe"], q) if s["hist"].get("pe") else None
            if pe is not None and 3 <= pe < 5000:
                inv_earn += w / pe; covered += w
        cov = covered / total_mcap
        if covered > 0 and inv_earn > 0 and cov >= COV_FLOOR:
            out_dates.append(q); out_pe.append(round(covered / inv_earn, 2))
            out_cov.append(round(cov, 3))
    return {"dates": out_dates, "pe": out_pe, "coverage": out_cov}

# ───────────────────────── main ─────────────────────────
def load_universe():
    d = json.loads(FUND_CACHE.read_text())
    secs = []
    for s in d["sectors"]:
        stocks = [{"symbol": st["symbol"], "name": st.get("name"),
                   "mcap_b": st.get("mcap_b") or 0,
                   "suffix": st["symbol"].split(".")[-1] if "." in st["symbol"] else "US"}
                  for st in s["stocks"]]
        secs.append({"narrative": s["narrative"], "icon": s.get("icon"),
                     "color": s.get("color"), "stocks": stocks})
    return secs

# S&P 500 P/E historique (multpl.com mensuel) -> stocke DANS le cache tradfi_hist pour que le mode
# "relatif au S&P 500" du comparateur soit auto-suffisant (pas de dependance au fetch pe_hist).
_SP500_PE = [None]
def fetch_sp500_pe():
    try:
        r = _get("https://www.multpl.com/s-p-500-pe-ratio/table/by-month")
        if r is None: return None
        html = r.text
        MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                  'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
        pts = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
            dm = re.search(r'<td>\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})\s*</td>', row)
            if not dm: continue
            mon, day, yr = dm.group(1), int(dm.group(2)), int(dm.group(3))
            vm = re.search(r'(\d+\.\d+)', row[dm.end():])
            if not vm: continue
            pe = float(vm.group(1))
            if not (3 < pe < 250): continue
            pts.append((f"{yr:04d}-{MONTHS[mon]:02d}-{day:02d}", pe))
        pts.sort()
        if len(pts) < 24: return None
        return {"dates": [p[0] for p in pts], "pe": [p[1] for p in pts]}
    except Exception as e:
        print(f"  [sp500] err: {e}", file=_sys.stderr)
        return None

SAMPLE = ["AAPL", "JPM", "NVDA",            # US
          "SHEL.L", "MC.PA", "SAP.DE",       # EU
          "0700.HK", "RY.TO", "MTN.JO",      # HK / Canada / Afrique du Sud
          "RELIANCE.NS", "005930.KS", "BHP.AX"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sectors", type=str, default="")
    ap.add_argument("--resume", action="store_true",
                    help="reprend : conserve les stocks deja en cache, ne fetch que les manquants")
    args = ap.parse_args()
    _load_slugs()

    universe = load_universe()
    if args.sectors:
        keep = {x.strip() for x in args.sectors.split(",")}
        universe = [s for s in universe if s["narrative"] in keep]

    # cache existant (merge-preserve si echec / resume apres kill)
    try: prev = json.loads(OUT_JSON.read_text())
    except Exception: prev = {"stocks": {}, "sectors": []}
    prev_stocks = prev.get("stocks", {})

    grid = quarter_grid(2010)
    hist_by_symbol = dict(prev_stocks) if args.resume else {}
    counters = {"ok": 0, "kept": 0, "fail": 0}
    _SP500_PE[0] = fetch_sp500_pe()  # serie S&P 500 pour le mode relatif (fallback : ancienne valeur cache)
    print(f"[sp500] {len(_SP500_PE[0]['dates']) if _SP500_PE[0] else 0} pts mensuels", flush=True)

    def build_and_write():
        sectors_out = []
        for sec in universe:
            entries = [{"mcap_b": st["mcap_b"], "hist": hist_by_symbol[st["symbol"]]}
                       for st in sec["stocks"] if st["symbol"] in hist_by_symbol]
            total_mcap = sum(st["mcap_b"] for st in sec["stocks"] if st["mcap_b"])
            agg = aggregate_sector(entries, grid, total_mcap) if entries else {"dates": [], "pe": [], "coverage": []}
            sectors_out.append({
                "narrative": sec["narrative"], "icon": sec["icon"], "color": sec["color"],
                "mcap_b": round(total_mcap, 1),   # mcap secteur (tri/selection par defaut cote front)
                "agg_pe": agg, "n_stocks": len(sec["stocks"]), "n_with_hist": len(entries),
                "stocks": [{"symbol": st["symbol"], "name": st["name"], "mcap_b": st["mcap_b"],
                            "has_hist": st["symbol"] in hist_by_symbol} for st in sec["stocks"]],
            })
        payload = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "fetch_ts": int(time.time()),
            "methodology": {
                "us": "macrotrends.net : P/E trimestriel (~16a depuis 2010), revenus (~15a) ; audit SEC EDGAR par ticker",
                "intl": "stockanalysis.com __data.json (devalue) : revenus/EPS/PE annuel (~5-6a)",
                "agg": "P/E secteur = ΣMarketCap/ΣEarnings (moy. harmonique ponderee mcap) ; point emis seulement si couverture mcap >= 50%",
                "no_yahoo": "aucun appel Yahoo pour le fonda (429 + historique court)",
            },
            "n_ok": counters["ok"], "n_kept": counters["kept"], "n_fail": counters["fail"],
            "sp500_pe": _SP500_PE[0] or prev.get("sp500_pe"),
            "stocks": hist_by_symbol, "sectors": sectors_out,
        }
        OUT_JSON.write_text(json.dumps(payload))
        OUT_JS.write_text("window.__TRADFI_HIST__ = " + json.dumps(payload) + ";")
        return len(sectors_out)

    # univers de symboles UNIQUES (un meme ticker peut etre dans 2 secteurs) -> 1 fetch chacun
    uniq = {}
    for sec in universe:
        for st in sec["stocks"]:
            uniq.setdefault(st["symbol"], st["suffix"])
    symbols = list(uniq.items())
    if args.sample:
        symbols = [(s, sf) for s, sf in symbols if s in SAMPLE]
    if args.limit:
        symbols = symbols[:args.limit]
    print(f"[plan] {len(symbols)} symboles uniques ({'resume, ' if args.resume else ''}deja en cache: {len(hist_by_symbol)})", flush=True)

    REFRESH_AFTER = 10 * 24 * 3600  # re-fetch une entree cache plus vieille que 10 jours
    now = int(time.time())
    for i, (sym, suffix) in enumerate(symbols):
        if args.resume and sym in hist_by_symbol:
            ts = hist_by_symbol[sym].get("_ts", 0) if isinstance(hist_by_symbol[sym], dict) else 0
            if now - ts < REFRESH_AFTER:
                continue  # deja recupere recemment -> skip (fill progressif + refresh roulant)
        try:
            h = fetch_us(sym) if suffix == "US" else fetch_intl(sym, suffix)
        except Exception as e:
            print(f"  [err] {sym}: {e}", file=_sys.stderr); h = None
        if h:
            h["_ts"] = now
            hist_by_symbol[sym] = h; counters["ok"] += 1
            print(f"  OK {sym:14s} src={h['src']:13s} pe={len(h['pe']['vals']):3d} rev={len(h['revenue']['vals']):3d}", flush=True)
        elif sym in prev_stocks:
            hist_by_symbol[sym] = prev_stocks[sym]; counters["kept"] += 1
            print(f"  KEEP {sym:12s} (echec, cache preserve)", flush=True)
        else:
            counters["fail"] += 1
            print(f"  -- {sym:14s} pas de donnee ({suffix})", flush=True)
        # checkpoint tous les 30 tickers : un kill (watchdog/erreur) ne perd jamais tout
        if (i + 1) % 30 == 0:
            build_and_write()
            print(f"  [checkpoint] {i+1}/{len(symbols)} ecrit (ok={counters['ok']} fail={counters['fail']})", flush=True)

    n = build_and_write()
    print(f"\nEcrit {OUT_JSON.name} : ok={counters['ok']} kept={counters['kept']} fail={counters['fail']} sectors={n}", flush=True)

if __name__ == "__main__":
    main()
