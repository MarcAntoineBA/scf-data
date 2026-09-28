#!/usr/bin/env python3
"""Historique P/E + revenus par action (TradFi) -> series temporelles + agregats secteur.

Sources (gratuites, auditables, AUCUN Yahoo pour le fonda — cf project_yahoo_curl_cffi_required):
  - US (sans suffixe)        : macrotrends.net (P/E trimestriel ~20 ans, revenus ~15 ans)
                               + lien audit SEC EDGAR (companyfacts) par CIK.
  - International (.HK/.L/...) : stockanalysis.com __data.json (devalue, trimestriel ~5 ans).
      compte de resultat : /financials/income-statement/  (⚠ PAS /financials/ : depuis
      l'ete 2026 cette route est la page « Financials Overview », financialData = -1)
      P/E                : /financials/ratios/  (+ colonne TTM = P/E au cours de la
      derniere seance, date par details.lastTradingDay — l'equivalent du dernier point
      macrotrends cote US)

Sortie : tradfi_hist_cache.json / .js (window.__TRADFI_HIST__), charge en runtime par
Comparaison_PER_Crypto_TradFi.html. Chaque serie porte sa source_url (auditabilite).

Dates (NE PAS confondre) :
  updated / fetch_ts : dernier passage qui a REELLEMENT collecte au moins une valeur.
                       Un passage qui ne ramene rien ne les touche pas.
  donnees_du         : date de la DONNEE (par entree : dernier point P/E ou revenus ;
                       en tete : la plus ancienne des medianes par source).
  _ts (par entree)   : dernier fetch reussi de cette entree.
Code de sortie : 0 ok ; 3 = RIEN collecte (cache NON reecrit) ; 4 = cache ecrit mais
une source en panne (defi anti-robot, 429, changement de schema) ; 2 = timeout global.

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
    "MI": "bit", "AS": "ams", "BR": "ebr", "HE": "hel", "CO": "cph", "OL": "osl",
    "ST": "sto", "TO": "tsx", "T": "tyo", "KS": "krx", "TW": "tpe", "NS": "nse",
    "SI": "sgx", "AX": "asx", "SS": "sha", "SZ": "she", "JK": "idx", "JO": "jse",
    "BK": "bkk", "KL": "klse", "SR": "tadawul", "AE": "dfm", "MX": "bmv",
    "SA": "bvmf", "PS": "pse",
}
# OL/BK/AE corriges (ose/set/adx rendaient HTTP 200 SANS noeud de donnees, en silence) :
# osl/bkk/dfm sont les prefixes que l'univers resolu (univers_actions.json) porte deja.

# Alertes de niveau SOURCE (defi anti-robot, 429 en serie, changement de schema) :
# ecrites dans le cache ET sur stderr, et elles rendent un code de sortie non nul.
ALERTES = []
# Statuts HTTP par source pour CE passage (publies dans le cache : requetes_http).
REQ_HTTP = {"macrotrends": {}, "stockanalysis": {}}

def _compte(src, statut):
    k = str(statut)
    REQ_HTTP[src][k] = REQ_HTTP[src].get(k, 0) + 1

def _alerte(msg):
    if msg not in ALERTES:
        ALERTES.append(msg)
        print(f"  [ALERTE] {msg}", file=_sys.stderr, flush=True)

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

# stockanalysis : requetes espacees + disjoncteurs. Avant : aucune pause (HTTP 429 mesures
# apres ~840 requetes d'affilee) et tout statut != 200 rendu None SANS TRACE.
_sa_last = [0.0]
_sa_streak = {"429": 0, "403": 0, "schema": 0}
_sa_giveup = [False]
_SA_GAP = 0.6            # s mini entre 2 requetes stockanalysis
_SA_COOLDOWN = 20.0      # pause sur 429/503
_SA_GIVEUP_429 = 5       # 429/503 d'affilee -> abandon de la source pour CE passage
_SA_GIVEUP_403 = 3       # 403 d'affilee (anti-robot) -> abandon pour CE passage
_SA_GIVEUP_SCHEMA = 10   # appels d'affilee en erreur de schema -> la source a change de format
_sa_schema_tickers = set()   # valeurs touchees par une erreur de schema pendant ce passage

def _sa_request(url):
    """GET stockanalysis poli. Rend la reponse (200) ou None ; tout echec est TRACE."""
    if _sa_giveup[0]:
        return None
    for attempt in range(3):
        gap = time.time() - _sa_last[0]
        if gap < _SA_GAP:
            time.sleep(_SA_GAP - gap + random.uniform(0, 0.2))
        try:
            r = SESS.get(url, headers=UA, timeout=20, allow_redirects=True)
            _sa_last[0] = time.time()
            st = r.status_code
            _compte("stockanalysis", st)
            if st == 200:
                _sa_streak["429"] = _sa_streak["403"] = 0
                return r
            if st == 403:
                _sa_streak["403"] += 1
                print(f"  [stockanalysis] HTTP 403 {url}", file=_sys.stderr, flush=True)
                if _sa_streak["403"] >= _SA_GIVEUP_403:
                    _sa_giveup[0] = True
                    _alerte(f"stockanalysis : {_SA_GIVEUP_403} x HTTP 403 d'affilee (blocage anti-robot ?) "
                            f"-> source abandonnee pour ce passage")
                return None
            if st in (404, 410):
                _sa_streak["429"] = _sa_streak["403"] = 0
                print(f"  [stockanalysis] HTTP {st} {url}", file=_sys.stderr, flush=True)
                return None  # page absente : ticker/place inconnus de la source
            if st in (429, 503):
                _sa_streak["429"] += 1
                if _sa_streak["429"] >= _SA_GIVEUP_429:
                    _sa_giveup[0] = True
                    _alerte(f"stockanalysis : {_SA_GIVEUP_429} x HTTP {st} d'affilee "
                            f"-> source abandonnee pour ce passage")
                    return None
                print(f"  [stockanalysis] HTTP {st} -> pause {_SA_COOLDOWN:.0f}s", file=_sys.stderr, flush=True)
                time.sleep(_SA_COOLDOWN)
                continue
            print(f"  [stockanalysis] HTTP {st} {url}", file=_sys.stderr, flush=True)
        except Exception as e:
            _sa_last[0] = time.time()
            _compte("stockanalysis", type(e).__name__)
            print(f"  [stockanalysis] {type(e).__name__} {url}", file=_sys.stderr, flush=True)
        if attempt < 2:
            time.sleep(1.2 * (attempt + 1))
    return None

def _sa_schema_ok():
    _sa_streak["schema"] = 0

def _sa_schema_ko(ticker=None):
    if ticker: _sa_schema_tickers.add(ticker)
    _sa_streak["schema"] += 1
    if _sa_streak["schema"] >= _SA_GIVEUP_SCHEMA and not _sa_giveup[0]:
        _sa_giveup[0] = True
        _alerte(f"stockanalysis : {_SA_GIVEUP_SCHEMA} appels d'affilee en erreur de SCHEMA "
                f"-> la source a change de format, abandon pour ce passage")

_MOIS = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
         'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}

def _sa_date(s):
    """« Sep 25, 2026 » -> « 2026-09-25 » ; None si illisible (on n'invente pas de date)."""
    m = re.match(r"^\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})\s*$", str(s or ""))
    if not m or m.group(1) not in _MOIS:
        return None
    iso = f"{int(m.group(3)):04d}-{_MOIS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return iso if iso <= datetime.now().strftime("%Y-%m-%d") else None

# ───────────────────────── MACROTRENDS (US) ─────────────────────────
_slugs = {}
_mt_last = [0.0]        # throttle : macrotrends bloque 2 requetes trop rapprochees
_mt_429_streak = [0]    # 429 consecutifs
_mt_giveup = [False]    # apres trop de 429, on abandonne macrotrends pour CE run (US -> prochain --resume)
_MT_BASE_GAP = 3.0      # gap mini entre 2 requetes macrotrends
_MT_COOLDOWN = 30.0     # pause sur 429 (rate-limit IP)
_MT_GIVEUP_AT = 8       # 429 consecutifs -> giveup
_mt_403_streak = [0]    # 403 consecutifs. Mesure 27/09/2026 : 403 + page Cloudflare « Just a
_MT_403_GIVEUP_AT = 3   # moment » sur TOUTES les pages -> 3 d'affilee = blocage, pas « page absente »

def _mt_request(url):
    """GET macrotrends avec throttle 3s + cooldown 30s sur 429. None si echec/giveup.
    Tout statut != 200 est TRACE sur stderr (avant : 403/404 rendaient None en silence)."""
    if _mt_giveup[0]:
        return None
    gap = time.time() - _mt_last[0]
    if gap < _MT_BASE_GAP:
        time.sleep(_MT_BASE_GAP - gap + random.uniform(0, 0.4))
    for attempt in range(3):
        try:
            r = SESS.get(url, headers=UA, timeout=20, allow_redirects=True)
            _mt_last[0] = time.time()
            _compte("macrotrends", r.status_code)
            if r.status_code == 200:
                _mt_429_streak[0] = 0; _mt_403_streak[0] = 0
                return r
            if r.status_code == 403:
                _mt_429_streak[0] = 0; _mt_403_streak[0] += 1
                defi = ("Just a moment" in r.text[:4000]) or bool(r.headers.get("cf-mitigated"))
                print(f"  [macrotrends] HTTP 403{' (defi anti-robot Cloudflare)' if defi else ''} {url}",
                      file=_sys.stderr, flush=True)
                if _mt_403_streak[0] >= _MT_403_GIVEUP_AT:
                    _mt_giveup[0] = True
                    _alerte(f"macrotrends : {_MT_403_GIVEUP_AT} x HTTP 403 d'affilee"
                            f"{' (defi anti-robot Cloudflare « Just a moment »)' if defi else ''}"
                            f" -> US abandonne pour ce passage, historiques US NON rafraichis")
                return None
            if r.status_code in (404, 410):
                _mt_429_streak[0] = 0; _mt_403_streak[0] = 0
                print(f"  [macrotrends] HTTP {r.status_code} {url}", file=_sys.stderr, flush=True)
                return None  # page absente
            if r.status_code in (429, 402, 503):
                _mt_429_streak[0] += 1
                if _mt_429_streak[0] >= _MT_GIVEUP_AT:
                    _mt_giveup[0] = True
                    _alerte(f"macrotrends : {_MT_GIVEUP_AT} x HTTP {r.status_code} consecutifs -> US "
                            f"abandonne pour ce passage (rattrape au prochain --resume)")
                    return None
                print(f"  [macrotrends] {r.status_code} -> cooldown {_MT_COOLDOWN:.0f}s "
                      f"(streak {_mt_429_streak[0]})", file=_sys.stderr, flush=True)
                time.sleep(_MT_COOLDOWN)
                continue
            print(f"  [macrotrends] HTTP {r.status_code} {url}", file=_sys.stderr, flush=True)
        except Exception as e:
            _compte("macrotrends", type(e).__name__)
            print(f"  [macrotrends] {type(e).__name__} {url}", file=_sys.stderr, flush=True)
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
    """Retourne (financialData, details) ou (None, None).
    financialData : dict (datekey, revenue, epsdil, pe...) ; details : dict de la page
    (currency, lastTradingDay, reportingFrequency...).
    quarterly=True -> ajoute ?p=quarterly (densite ~20 trimestres vs ~6 annuels).
    Garde-fou schema : leve si la structure attendue a change (anti-regression silencieuse)."""
    url = f"https://stockanalysis.com/{path}/__data.json" + ("?p=quarterly" if quarterly else "")
    r = _sa_request(url)
    if r is None: return None, None
    try:
        d = r.json()
    except Exception:
        print(f"  [stockanalysis] reponse non JSON : {url}", file=_sys.stderr, flush=True)
        return None, None
    nodes = d.get("nodes", [])
    arr = None
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get("data"), list) and len(n["data"]) > 50:
            cand = n["data"]
            if isinstance(cand[0], dict) and "financialData" in cand[0]:
                arr = cand; break
    if arr is None:
        # HTTP 200 sans noeud de donnees = ticker/place inconnus de la source. Muet jusqu'ici :
        # c'est ce silence qui a cache pendant des mois les codes de place ose/set/adx faux.
        print(f"  [stockanalysis] HTTP 200 sans noeud financialData : {url}", file=_sys.stderr, flush=True)
        return None, None
    root = arr[0]
    fd = _devalue_resolve(arr, root["financialData"])
    if not isinstance(fd, dict) or "datekey" not in fd:
        # Mesure 27/09/2026 : sur /financials/ la page est devenue « Financials Overview »
        # (statement="overview") et financialData vaut -1 (= undefined en devalue).
        stmt = _devalue_resolve(arr, root["statement"]) if "statement" in root else None
        raise RuntimeError(f"SCHEMA stockanalysis change ({path}): statement={stmt!r} "
                           f"financialData keys={list(fd)[:8] if isinstance(fd,dict) else fd}")
    det = _devalue_resolve(arr, root["details"]) if "details" in root else None
    return fd, (det if isinstance(det, dict) else {})

_CHEMINS_UNIVERS = None


def _chemin_resolu(ticker):
    """Le chemin de la societe chez la source, tel que l'univers l'a RESOLU.

    La table SA_EXCHANGE ci-dessus est juste, mais elle ne porte que trente et
    une places, ecrites a la main pour les huit cent douze titres suivis
    (couverture mesuree : 100 %, zero trou). Les chemins resolus, eux, en
    portent quatre-vingt-quatorze — et sur l'univers etendu vers lequel le
    chantier avance, 23,5 % des societes tombent hors de la table : onze mille
    cent trente, sans historique, SANS UN MOT, parce que fetch_intl rend None
    quand le suffixe est inconnu.

    Ces chemins sont deja resolus, deja verifies, deja sur le disque. On cesse
    de deviner ce qu'on sait.
    """
    global _CHEMINS_UNIVERS
    if _CHEMINS_UNIVERS is None:
        _CHEMINS_UNIVERS = {}
        f = CACHES / "univers_actions.json"
        try:
            with f.open(encoding="utf-8") as fh:
                u = json.load(fh)
            for t in u.get("titres", []):
                sym = t.get("yahoo") or t.get("sa")
                if sym and t.get("principal") and t.get("sa"):
                    _CHEMINS_UNIVERS[sym.upper()] = t["sa"]
        except Exception:
            # Pas de fichier d'univers : on retombe sur la table, qui a
            # l'avantage de fonctionner seule.
            _CHEMINS_UNIVERS = {}
    return _CHEMINS_UNIVERS.get(str(ticker).upper())


def fetch_intl(ticker, suffix, prefixe=None):
    """`prefixe` impose le chemin chez la source (« stocks/aapl » pour une valeur
    americaine : cf raccord_us). Sans lui, chemin resolu puis table des places."""
    if prefixe:
        exch, base_tkr = prefixe.split("/", 1)
    else:
        # Le chemin resolu d'abord, la table ensuite : l'un couvre quatre-vingt-
        # quatorze places, l'autre trente et une.
        chemin = _chemin_resolu(ticker)
        if chemin and "/" in chemin:
            exch, base_resolu = chemin.split("/", 1)
        else:
            exch, base_resolu = SA_EXCHANGE.get(suffix), None
        if not exch: return None
        # stockanalysis utilise un point pour les class-shares nordiques (VOLV-B -> VOLV.B,
        # ATCO-A -> ATCO.A, NDA-FI -> NDA.FI). Le symbole univers (Yahoo) garde le tiret.
        base_tkr = base_resolu or ticker.split(".")[0].replace("-", ".")
    # Compte de resultat : /financials/income-statement/. L'ancienne route /financials/
    # est devenue la page « Financials Overview » (financialData = -1) : 0 valeur
    # internationale collectee depuis le 21/07/2026.
    racine = f"{exch}/{base_tkr}" if prefixe else f"quote/{exch}/{base_tkr}"
    qpath = f"{racine}/financials/income-statement"
    # income statement (revenue, eps) — quarterly d'abord (densite ~20 pts), fallback annuel
    fd, inc_q = None, False
    for q in (True, False):
        try:
            fd, _det = _sa_financial_data(qpath, quarterly=q)
            if fd: _sa_schema_ok()
        except RuntimeError as e:
            print(f"  [schema] {ticker}: {e}", file=_sys.stderr); fd = None
            _sa_schema_ko(ticker)
        if fd and isinstance(fd.get("datekey"), list) and len(fd["datekey"]) >= 4:
            inc_q = q
            break
    # Plus de `return None` ici : sans compte de resultat, le P/E (page ratios, qui seul
    # alimente l'agregat secteur) reste recuperable.
    fd = fd or {}
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
    # Cles EPS renommees par la source : epsdil / epsBasic (eps / epsDiluted absentes).
    # ⚠ EPS de PERIODE (trimestre ou semestre), pas un TTM, malgre le nom du champ.
    eps = next((series(k) for k in ("epsdil", "eps", "epsDiluted", "epsBasic") if k in fd),
               {"dates": [], "vals": []})
    # P/E : page ratios (peRatio) — quarterly d'abord, fallback annuel
    pe = {"dates": [], "vals": []}
    fr, fr_det, pe_q, pe_ttm_du = None, None, False, None
    for q in (True, False):
        try:
            fr, fr_det = _sa_financial_data(f"{racine}/financials/ratios", quarterly=q)
            if fr: _sa_schema_ok()
        except RuntimeError as e:
            print(f"  [schema] {ticker} (ratios): {e}", file=_sys.stderr); fr = None
            _sa_schema_ko(ticker)
        if fr and isinstance(fr.get("datekey"), list) and len(fr["datekey"]) >= 4:
            pe_q = q
            break
    if fr:
        rdates = fr.get("datekey") or []
        pev = fr.get("peRatio") or fr.get("pe")
        if isinstance(pev, list):
            ds, vs, ttm = [], [], None
            for dt, v in zip(rdates, pev):
                if v is None: continue
                if dt == "TTM": ttm = v; continue
                ds.append(dt); vs.append(v)
            ds, vs = ds[::-1], vs[::-1]
            # P/E COURANT : colonne TTM = cours de la derniere seance / benefice des 12 derniers
            # mois, date par la seance dont il porte le cours (details.lastTradingDay). C'est
            # l'equivalent exact du dernier point macrotrends cote US ; sans lui la jambe
            # internationale reste bloquee a la derniere cloture d'exercice. Date illisible :
            # on ecarte le point plutot que d'inventer une date.
            d_ttm = _sa_date((fr_det or {}).get("lastTradingDay"))
            if ttm is not None and d_ttm and (not ds or d_ttm > ds[-1]):
                ds.append(d_ttm); vs.append(ttm); pe_ttm_du = d_ttm
            pe = {"dates": ds, "vals": vs}
    if not rev["vals"] and not pe["vals"]:
        return None
    url = f"https://stockanalysis.com/{racine}/financials/"
    out = {
        "src": "stockanalysis",
        "pe": pe, "eps_ttm": eps, "revenue": rev, "revenue_unit": "native",
        "source_url": {"pe": url + "ratios/" + ("?p=quarterly" if pe_q else ""),
                       "revenue": url + "income-statement/" + ("?p=quarterly" if inc_q else ""),
                       "audit_sec": None},
    }
    if pe_ttm_du:
        out["pe_ttm_du"] = pe_ttm_du   # dernier point P/E = TTM au cours de cette seance
    return out

# ─────────────────────── RACCORD US (macrotrends -> stockanalysis) ───────────────────────
# POURQUOI (28/09/2026). macrotrends, seule source de l'historique americain (depuis 2006),
# repond 403 + defi Cloudflare « Just a moment » a TOUTES les requetes depuis mi-septembre.
# On ne contourne pas un anti-robot (regle du site). stockanalysis sert les memes valeurs
# americaines, mais sur 20 trimestres seulement (depuis 2021) : le remplacer ferait perdre
# quinze ans d'historique. On PROLONGE donc la serie macrotrends avec les points
# stockanalysis POSTERIEURS a son dernier point — et SEULEMENT si les deux sources disent
# la meme chose sur leurs trimestres communs. Mesure sur 37 valeurs (28/09) :
#   · chiffre d'affaires : 28/35 identiques a moins de 1 % ; les ecarts sont des
#     definitions (banques : 19 a 36 % ; Altria : droits d'accise) ou des DEVISES
#     (ADR chinois publies en yuans chez stockanalysis) -> refuses par le controle ;
#   · P/E : 9/32 a moins de 3 %, ecart median 6 % — les deux sources ne le calculent pas
#     pareil. Raccorder partout creerait une marche artificielle au point de jonction.
# D'ou un raccord SERIE PAR SERIE : chacune est prolongee si elle concorde, sinon elle
# reste figee a son dernier point macrotrends, avec sa vraie date. Le resultat du controle
# est publie dans l'entree (`raccord`) : un lecteur peut refaire le calcul.
# L'EPS n'est jamais raccorde : macrotrends publie un EPS sur 12 mois, stockanalysis un
# EPS de trimestre — meme nom, pas la meme grandeur.
RACCORD_TOL = {"revenue": 0.01, "pe": 0.03}
RACCORD_MIN_COMMUNS = 8
RACCORD_FENETRE_J = 20          # deux dates « sont le meme trimestre » a vingt jours pres

def _valeur_proche(serie, date, fenetre=RACCORD_FENETRE_J):
    t = datetime.strptime(date, "%Y-%m-%d")
    best = None
    for d, v in zip(serie.get("dates") or [], serie.get("vals") or []):
        if v is None: continue
        e = abs((datetime.strptime(d, "%Y-%m-%d") - t).days)
        if e <= fenetre and (best is None or e < best[0]):
            best = (e, v)
    return best[1] if best else None

def raccorder(ancien, neuf):
    """Prolonge chaque serie de `ancien` (macrotrends) par les points de `neuf`
    (stockanalysis) posterieurs a son dernier point, si les deux sources concordent sur
    RACCORD_MIN_COMMUNS trimestres communs au moins (ecart median <= RACCORD_TOL).
    Rend (entree, nb_points_ajoutes). N'invente rien : pas de mise a l'echelle, pas
    d'interpolation — une serie qui ne concorde pas n'est pas prolongee."""
    out = {k: v for k, v in ancien.items() if k not in ("raccord",)}
    rapport, ajoutes = {}, 0
    for cle, tol in RACCORD_TOL.items():
        A = ancien.get(cle) or {"dates": [], "vals": []}
        N = neuf.get(cle) or {"dates": [], "vals": []}
        # Revenus : macrotrends en millions de dollars, stockanalysis en dollars.
        echelle = 1e-6 if cle == "revenue" else 1.0
        ecarts = []
        for d, v in zip(N.get("dates") or [], N.get("vals") or []):
            if v is None or v <= 0: continue
            va = _valeur_proche(A, d)
            if va is not None and va > 0:
                ecarts.append(abs(v * echelle / va - 1))
        ecarts.sort()
        med = ecarts[len(ecarts) // 2] if ecarts else None
        ok = len(ecarts) >= RACCORD_MIN_COMMUNS and med is not None and med <= tol
        r = {"communs": len(ecarts), "ecart_median_pct": None if med is None else round(100 * med, 2),
             "seuil_pct": round(100 * tol, 1), "raccorde": ok}
        if ok:
            dern = A["dates"][-1] if A.get("dates") else ""
            plus = [(d, round(v * echelle, 4)) for d, v in zip(N["dates"], N["vals"])
                    if v is not None and d > dern]
            if plus:
                out[cle] = {"dates": list(A["dates"]) + [d for d, _ in plus],
                            "vals": list(A["vals"]) + [v for _, v in plus]}
                r["depuis"] = plus[0][0]
                r["points_ajoutes"] = len(plus)
                ajoutes += len(plus)
        rapport[cle] = r
    rapport["verifie_le"] = datetime.now().strftime("%Y-%m-%d")
    rapport["source"] = (neuf.get("source_url") or {}).get("pe")
    out["raccord"] = rapport
    if any(rapport[c].get("points_ajoutes") for c in RACCORD_TOL):
        out["src"] = "macrotrends+stockanalysis"
        out["source_url"] = dict(ancien.get("source_url") or {},
                                 raccord_pe=(neuf.get("source_url") or {}).get("pe"),
                                 raccord_revenue=(neuf.get("source_url") or {}).get("revenue"))
    return out, ajoutes

def raccord_us(ticker, ancien):
    """macrotrends muet pour cette valeur : on tente le prolongement par stockanalysis.
    Sans historique macrotrends (valeur nouvelle), la serie stockanalysis seule est prise,
    etiquetee comme telle (5 ans au lieu de 20 : c'est ce qui existe)."""
    neuf = fetch_intl(ticker, "US", prefixe=f"stocks/{ticker.lower().replace('-', '.')}")
    if not neuf:
        return None, 0
    if not isinstance(ancien, dict) or ancien.get("src", "").split("+")[0] != "macrotrends":
        if neuf.get("revenue", {}).get("vals"):
            neuf["revenue"] = {"dates": neuf["revenue"]["dates"],
                               "vals": [round(v / 1e6, 4) for v in neuf["revenue"]["vals"]]}
            neuf["revenue_unit"] = "M USD"
        return neuf, len(neuf["pe"]["vals"]) + len(neuf["revenue"]["vals"])
    return raccorder(ancien, neuf)

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

# ───────────────────────── fraicheur de la DONNEE ─────────────────────────
def _donnees_du(h):
    """Date du point le plus recent de l'entree (P/E ou revenus) : la date de la DONNEE,
    pas celle du passage du collecteur (_ts)."""
    ds = [s["dates"][-1] for s in (h.get("pe"), h.get("revenue"))
          if isinstance(s, dict) and s.get("dates")]
    return max(ds) if ds else None

def _fraicheur(symboles, hist_by_symbol, collectes):
    """Par source : n, collectes_ce_passage, min / mediane / max de donnees_du.
    En tete, `donnees_du` = la PLUS ANCIENNE des medianes par source : une source figee
    (ex. l'international bloque au 31/03 pendant que l'americain suit le cours du jour)
    doit se voir, pas etre masquee par la plus fraiche."""
    par = {}
    for sym in symboles:
        h = hist_by_symbol.get(sym)
        if not isinstance(h, dict): continue
        par.setdefault(h.get("src") or "?", []).append((h.get("donnees_du"), sym in collectes))
    out = {}
    for src, L in sorted(par.items()):
        ds = sorted(d for d, _ in L if d)
        out[src] = {"n": len(L), "collectes_ce_passage": sum(1 for _, c in L if c),
                    "donnees_du_min": ds[0] if ds else None,
                    "donnees_du_mediane": ds[len(ds) // 2] if ds else None,
                    "donnees_du_max": ds[-1] if ds else None}
    meds = [v["donnees_du_mediane"] for v in out.values() if v["donnees_du_mediane"]]
    return (min(meds) if meds else None), out

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
    collectes = set()        # valeurs REELLEMENT rafraichies pendant ce passage
    rafraich_tentes = [0]    # tentatives sur des valeurs DEJA en cache (echues)
    _SP500_PE[0] = fetch_sp500_pe()  # serie S&P 500 pour le mode relatif (fallback : ancienne valeur cache)
    print(f"[sp500] {len(_SP500_PE[0]['dates']) if _SP500_PE[0] else 0} pts mensuels", flush=True)

    def build_and_write():
        # ⚠ AVANT : `updated` = heure du passage, TOUJOURS — y compris quand 0 valeur avait
        # ete collectee (7 passages d'affilee ok=0 kept=464 au 27/09/2026) : la page affichait
        # « maj » du jour sur des donnees figees. `updated`/`fetch_ts` ne bougent desormais
        # que si ce passage a collecte au moins une valeur ; sinon on reporte les precedents.
        if counters["ok"] > 0 or not prev.get("updated"):
            updated, fetch_ts = datetime.now().strftime("%Y-%m-%d %H:%M"), int(time.time())
        else:
            updated, fetch_ts = prev.get("updated"), prev.get("fetch_ts")
        for h in hist_by_symbol.values():
            if isinstance(h, dict):
                h["donnees_du"] = _donnees_du(h)
        symboles = {st["symbol"] for sec in universe for st in sec["stocks"]}
        donnees_du, fraicheur = _fraicheur(symboles, hist_by_symbol, collectes)
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
            "updated": updated,
            "fetch_ts": fetch_ts,
            "donnees_du": donnees_du,
            "fraicheur": fraicheur,
            "alertes": list(ALERTES),
            "requetes_http": REQ_HTTP,
            "methodology": {
                "us": "macrotrends.net : P/E trimestriel (~16a depuis 2010), revenus (~15a) ; audit SEC EDGAR par ticker",
                "us_raccord": "macrotrends bloque (defi anti-robot) depuis mi-septembre 2026 : chaque serie US est PROLONGEE par stockanalysis seulement si les deux sources concordent sur au moins 8 trimestres communs (ecart median <= 1 % pour le CA, <= 3 % pour le P/E) ; sinon elle reste figee a son dernier point macrotrends. Controle publie par entree dans `raccord`. L'EPS n'est jamais raccorde (12 mois chez macrotrends, trimestre chez stockanalysis).",
                "intl": "stockanalysis.com __data.json (devalue) : revenus/EPS (income-statement) et P/E (ratios) trimestriels (~5a) ; dernier point P/E = TTM au cours de la derniere seance (pe_ttm_du)",
                "agg": "P/E secteur = ΣMarketCap/ΣEarnings (moy. harmonique ponderee mcap) ; point emis seulement si couverture mcap >= 50%",
                "no_yahoo": "aucun appel Yahoo pour le fonda (429 + historique court)",
                "dates": "updated/fetch_ts = dernier passage ayant collecte au moins une valeur ; donnees_du = date de la donnee (en tete : plus ancienne des medianes par source, detail dans fraicheur)",
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
        if sym in prev_stocks:
            rafraich_tentes[0] += 1
        try:
            h = fetch_us(sym) if suffix == "US" else fetch_intl(sym, suffix)
        except Exception as e:
            print(f"  [err] {sym}: {e}", file=_sys.stderr); h = None
        # macrotrends muet (defi anti-robot) : prolongement par stockanalysis, controle a
        # l'appui (cf RACCORD US). Rien d'ajoute = l'entree precedente, mais DATEE du
        # controle (`_ts`) pour ne pas re-sonder 364 valeurs a chaque passage.
        if suffix == "US" and not h:
            try:
                hr, n_aj = raccord_us(sym, prev_stocks.get(sym))
            except Exception as e:
                print(f"  [raccord] {sym}: {e}", file=_sys.stderr); hr, n_aj = None, 0
            if hr is not None and n_aj:
                h = hr
            elif hr is not None and isinstance(prev_stocks.get(sym), dict):
                hr["_ts"] = now
                hist_by_symbol[sym] = hr; counters["kept"] += 1
                r = hr.get("raccord") or {}
                print(f"  RACC {sym:12s} controle fait, rien a prolonger "
                      f"(CA {r.get('revenue', {}).get('ecart_median_pct')} % / P/E "
                      f"{r.get('pe', {}).get('ecart_median_pct')} % sur les trimestres communs)", flush=True)
                continue
        # Garde anti-appauvrissement : une serie P/E vide ne remplace pas une serie P/E
        # existante (c'est le P/E, et lui seul, qui alimente l'agregat secteur).
        old = prev_stocks.get(sym)
        if (h and not h["pe"]["vals"] and isinstance(old, dict)
                and (old.get("pe") or {}).get("vals")):
            print(f"  [regress] {sym}: P/E vide au nouveau fetch, ancienne serie conservee",
                  file=_sys.stderr, flush=True)
            h = None
        if h:
            h["_ts"] = now
            hist_by_symbol[sym] = h; counters["ok"] += 1; collectes.add(sym)
            print(f"  OK {sym:14s} src={h['src']:13s} pe={len(h['pe']['vals']):3d} rev={len(h['revenue']['vals']):3d}"
                  f" dernier_pe={h['pe']['dates'][-1] if h['pe']['dates'] else '-'}", flush=True)
        elif sym in prev_stocks:
            # KEEP : l'entree precedente est reprise TELLE QUELLE (son _ts et sa donnees_du
            # restent ceux du dernier fetch reussi -> elle sera retentee au prochain passage).
            hist_by_symbol[sym] = prev_stocks[sym]; counters["kept"] += 1
            print(f"  KEEP {sym:12s} (echec, cache preserve, donnees du {_donnees_du(prev_stocks[sym])})", flush=True)
        else:
            counters["fail"] += 1
            print(f"  -- {sym:14s} pas de donnee ({suffix})", flush=True)
        # checkpoint tous les 30 tickers : un kill (watchdog/erreur) ne perd jamais tout.
        # Rien de neuf -> rien a proteger : on ne reecrit pas (le temoin mtime du watchdog
        # doit refleter une vraie collecte).
        if (i + 1) % 30 == 0 and counters["ok"] > 0:
            build_and_write()
            print(f"  [checkpoint] {i+1}/{len(symbols)} ecrit (ok={counters['ok']} fail={counters['fail']})", flush=True)

    if len(_sa_schema_tickers) >= 3:
        _alerte(f"stockanalysis : erreur de SCHEMA sur {len(_sa_schema_tickers)} valeurs "
                f"(ex. {', '.join(sorted(_sa_schema_tickers)[:4])}) -> format change ?")
    # Blocage sans disjoncteur (moins de 3 valeurs echues) : des 403 et pas un seul 200.
    for src, c in REQ_HTTP.items():
        deja = any(a.startswith(src + " :") for a in ALERTES)
        if c.get("403") and not c.get("200") and not deja:
            _alerte(f"{src} : {c['403']} x HTTP 403 et AUCUNE reponse 200 sur ce passage "
                    f"(blocage anti-robot ?)")
    tentes = counters["ok"] + counters["kept"] + counters["fail"]
    bilan = f"ok={counters['ok']} kept={counters['kept']} fail={counters['fail']} (tentes={tentes})"
    # RIEN COLLECTE = 0 valeur ET une cause de niveau source (disjoncteur, schema) ou au moins
    # 10 valeurs echues toutes en echec. Un passage « calme » (rien d'echu hormis les valeurs
    # jamais couvertes, qui echouent a chaque fois) n'est PAS une panne.
    if counters["ok"] == 0 and (ALERTES or rafraich_tentes[0] >= 10 or (tentes and not prev_stocks)):
        msg = (f"[ALERTE] RIEN COLLECTE : {bilan} ; {OUT_JSON.name} NON reecrit, "
               f"updated reste {prev.get('updated')!r}"
               + (f", donnees du {prev['donnees_du']}" if prev.get("donnees_du") else "")
               + (f" ; causes : {' | '.join(ALERTES)}" if ALERTES else ""))
        print("\n" + msg, flush=True)
        print(msg, file=_sys.stderr, flush=True)
        _sys.exit(3)

    n = build_and_write()
    print(f"\nEcrit {OUT_JSON.name} : {bilan} sectors={n}"
          + ("" if counters["ok"] else f" — rien de neuf, updated conserve ({prev.get('updated')})"), flush=True)
    if ALERTES:
        msg = f"[ALERTE] source(s) en panne, cache ecrit PARTIELLEMENT a jour ({bilan}) : " + " | ".join(ALERTES)
        print(msg, flush=True)
        print(msg, file=_sys.stderr, flush=True)
        _sys.exit(4)

if __name__ == "__main__":
    main()
