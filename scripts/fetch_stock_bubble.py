#!/usr/bin/env python3
"""Stock Bubble Map — MULTI-ZONE (US S&P100 / Chine / Europe / Inde).

Top 100 EVOLUTIF par zone : un pool de candidats (stock_universe.json, ~100-120/zone)
est re-classe par capitalisation USD LIVE a chaque run -> la composition et l'ordre du
Top 100 evoluent quand les capis bougent (pas de liste figee). mcap = so (actions en
circulation, bakees dans l'univers) * prix_live -> aucun appel mcap au runtime.

Prix & variations via Yahoo Finance spark v8 (les % sont des ratios = devise-agnostiques ;
le PRIX est converti en USD pour l'affichage, gestion GBp pence). FX via spark Yahoo.
Historique 5 ans daily par zone (fichiers splittes, lazy-load cote client).

Resilience : merge-preserve par zone (une zone ratee conserve son cache precedent), jamais
d'ecrasement par du vide.

Sorties (~/Library/Caches/site_crypto_finance/) :
  stock_bubble_cache.{json,js}  -> window.__STOCK_ZONES__ = {us,cn,eu,in}
                                   + window.__STOCK_DATA__ = us.stocks (alias legacy)
  stock_history_cache.js (us) + stock_history_{cn,eu,in}.js       (5 ans DAILY, TTL 4h)
  stock_intraday_{us,cn,eu,in}.js                                  (1 mois HORAIRE, chaque run)
Lu par index.html. Lance par launchd toutes les 30 min.

COUCHE INTRADAY (2026-07-27, plainte user « graphiques pas a jour et pas assez de points ») :
le daily seul donnait 20 points sur l'onglet 1M et 5 sur l'onglet 5J (un point par seance,
horodate a l'OUVERTURE 9h30 ET), et son dernier point restait la cloture de la veille tant
que Yahoo n'avait pas imprime la barre du jour -> pendant toute la seance le modal affichait
un graphe fige la veille alors que la bulle, elle, montrait le prix live. La couche horaire
(spark range=1mo&interval=1h, ~141 pts/mois/titre, 1 requete par batch de 10) donne 7x plus
de points ET un bord droit a <1h ; elle est rafraichie a CHAQUE run (fichier separe ~280 Ko
par zone, vs 2,4 Mo pour le daily 5 ans qui garde son TTL de 4h et ne bouge quasi pas)."""
import json, re, socket, sys, time, warnings, urllib.parse
from pathlib import Path
from datetime import datetime, timezone
import requests

warnings.filterwarnings("ignore")

# Coupe-circuit reseau (2026-07-20) : lors d'une panne DNS, chaque batch grindait
# 3 retries x timeout 25-30s -> un run de 3h13 pendant lequel launchd ne relance pas
# (donnees affichees jusqu'a ~8h de retard). Desormais : pre-vol DNS (exit rapide,
# launchd retente 30 min plus tard) + abandon du run apres N batches KO consecutifs.
class NetworkDown(Exception): pass
MAX_CONSEC_FAIL = 5
_consec_fail = 0

def _batch_failed():
    global _consec_fail
    _consec_fail += 1
    if _consec_fail >= MAX_CONSEC_FAIL:
        raise NetworkDown(f"{_consec_fail} batches KO consecutifs")

def _batch_ok():
    global _consec_fail
    _consec_fail = 0

def preflight():
    try:
        socket.getaddrinfo("query1.finance.yahoo.com", 443)
        return True
    except OSError as e:
        log(f"pre-vol DNS KO ({e}) — run annule, cache conserve"); return False

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "stock_bubble_cache.json"
CACHE_JS   = CACHE_DIR / "stock_bubble_cache.js"
HERE = Path(__file__).resolve().parent
UNIVERSE = HERE / "stock_universe.json"           # copie a cote du script (Desktop + Library)

CACHE_MAX_MINUTES = 20
HISTORY_MAX_MINUTES = 240        # 4h
TOP_N = 100                      # carte par defaut (fichier principal, logos + histo 5 ans bakes)
DEEP_N = 300                     # profondeur maximale du spectre (rangs 101-300 = fichier d'extension)
MIN_ZONE_OK = 60                 # seuil merge-preserve (zone consideree saine)

ZONE_ORDER = ["us", "cn", "eu", "in"]
# Plus de profondeur dans le libelle (« Top 100 X ») : le spectre est reglable cote
# client (100/200/300) et le titre de la carte annonce deja le nombre reellement affiche.
ZONE_SOURCE = {
    "us": "Yahoo Finance · actions US",
    "cn": "Yahoo Finance · actions Chine",
    "eu": "Yahoo Finance · actions Europe",
    "in": "Yahoo Finance · actions Inde",
}
# zone -> (global JS, fichier .js, fichier .json) pour l'historique splitte
HISTORY_FILES = {
    "us": ("__STOCK_HISTORY__",    "stock_history_cache.js", "stock_history_cache.json"),
    "cn": ("__STOCK_HISTORY_CN__", "stock_history_cn.js",    "stock_history_cn.json"),
    "eu": ("__STOCK_HISTORY_EU__", "stock_history_eu.js",    "stock_history_eu.json"),
    "in": ("__STOCK_HISTORY_IN__", "stock_history_in.js",    "stock_history_in.json"),
}
# Idem pour la couche HORAIRE (~1 mois) : fichiers separes, ecrits a chaque run.
INTRADAY_FILES = {
    "us": ("__STOCK_INTRADAY__",    "stock_intraday_us.js", "stock_intraday_us.json"),
    "cn": ("__STOCK_INTRADAY_CN__", "stock_intraday_cn.js", "stock_intraday_cn.json"),
    "eu": ("__STOCK_INTRADAY_EU__", "stock_intraday_eu.js", "stock_intraday_eu.json"),
    "in": ("__STOCK_INTRADAY_IN__", "stock_intraday_in.js", "stock_intraday_in.json"),
}
INTRADAY_RANGE    = "1mo"
INTRADAY_INTERVAL = "1h"
# Extension du spectre (rangs 101-300) : fichier SEPARE par zone, charge a la demande
# quand l'utilisateur demande « top 200 » ou « top 300 ». Volontairement sans historique
# 5 ans ni couche horaire bakee (le modal de ces titres tire sa courbe de /live/chart) :
# c'est ce qui garde le poids du depot et la duree du run raisonnables.
EXT_FILES = {
    "us": ("__STOCK_EXT_US__", "stock_ext_us.js", "stock_ext_us.json"),
    "cn": ("__STOCK_EXT_CN__", "stock_ext_cn.js", "stock_ext_cn.json"),
    "eu": ("__STOCK_EXT_EU__", "stock_ext_eu.js", "stock_ext_eu.json"),
    "in": ("__STOCK_EXT_IN__", "stock_ext_in.js", "stock_ext_in.json"),
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
def log(msg): sys.stderr.write(f"[StockBubble] {msg}\n"); sys.stderr.flush()


# ── FX -> USD (USD par unite de devise major ; GBp partage le FX GBP) ───────
def fetch_fx():
    majors = ["EUR", "GBP", "CHF", "SEK", "DKK", "NOK", "PLN", "HKD", "CNY", "INR"]
    SANE = {"EUR": (0.9, 1.4), "GBP": (1.0, 1.7), "CHF": (0.9, 1.5), "SEK": (0.07, 0.13),
            "DKK": (0.12, 0.18), "NOK": (0.07, 0.13), "PLN": (0.18, 0.32), "HKD": (0.11, 0.14),
            "CNY": (0.11, 0.17), "INR": (0.009, 0.014)}
    out = {"USD": 1.0}
    def spark(sy):
        u = "https://query1.finance.yahoo.com/v8/finance/spark?symbols=" + urllib.parse.quote(sy, safe=",") + "&range=5d&interval=1d"
        try: return requests.get(u, headers=HEADERS, timeout=20).json()
        except Exception as e: log(f"FX err {e}"); return {}
    d1 = spark(",".join(f"{c}USD=X" for c in majors))
    d2 = spark(",".join(f"{c}=X" for c in majors))
    def last(o):
        cl = [c for c in (o.get("close") or []) if c is not None]
        return cl[-1] if cl else None
    for c in majors:
        v = None
        o = d1.get(f"{c}USD=X")
        if o:
            x = last(o)
            if x and SANE[c][0] <= x <= SANE[c][1]: v = x
        if v is None:
            o = d2.get(f"{c}=X")
            if o:
                x = last(o)
                if x and x > 0 and SANE[c][0] <= 1.0/x <= SANE[c][1]: v = 1.0/x
        if v is None: log(f"FX {c}: aucune valeur plausible")
        out[c] = v
    out["GBp"] = out.get("GBP")
    log("FX USD/unit: " + ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in out.items()))
    return out

def _fx_for(ccy, fx):
    return fx.get("GBP") if ccy == "GBp" else fx.get(ccy)

def price_to_usd(p, ccy, fx):
    if p is None: return None
    f = _fx_for(ccy, fx)
    if f is None: return None
    scale = 0.01 if ccy == "GBp" else 1.0     # GBp = pence
    return p * scale * f

def mcap_usd_b(so, p_native, ccy, fx):
    """Capi USD (milliards) = actions * prix_native -> USD. GBp : prix en pence -> /100."""
    if not so or p_native is None: return None
    f = _fx_for(ccy, fx)
    if f is None: return None
    scale = 0.01 if ccy == "GBp" else 1.0
    return so * p_native * scale * f / 1e9


# ── Spark v8 : variations + historique ──────────────────────────────────────
def _ref_at(pairs, days_ago):
    """Derniere cloture datant d'AU MOINS `days_ago` jours (calendrier).

    Renvoie None si l'historique ne remonte pas assez loin (l'appelant se rabat
    alors sur la 1re cloture de la fenetre — jamais sur une valeur inventee)."""
    if not pairs: return None
    cutoff = pairs[-1][0] - days_ago * 86400
    ref = None
    for t, c in pairs:
        if t <= cutoff: ref = c
        else: break
    return ref

def _ref_before(pairs, ts_cutoff):
    """Derniere cloture ANTERIEURE a un horodatage absolu (None si aucune)."""
    ref = None
    for t, c in pairs:
        if t < ts_cutoff: ref = c
        else: break
    return ref

def _jan1_ts():
    """Horodatage UTC du 1er janvier de l'annee en cours (borne YTD)."""
    return datetime(datetime.now().year, 1, 1, tzinfo=timezone.utc).timestamp()

def compute_changes(obj):
    # Paires (ts, close) alignees : le filtrage des None doit garder l'horodatage,
    # sinon une seance trouee (Yahoo renvoie close:null, cf 24/07/2026) decale les
    # references et « 1J » devient en realite une variation sur 2 seances.
    _ts = obj.get("timestamp") or []
    _cl = obj.get("close") or []
    pairs = [(t, c) for t, c in zip(_ts, _cl) if c is not None]
    closes = [c for c in (obj.get("close") or []) if c is not None]
    # NOUVELLE COTATION (correctif 2026-07-28) : une societe cotee depuis 1 ou 2
    # seances n'a qu'UNE clôture chez Yahoo (la barre du jour n'est pas encore
    # imprimee). L'ancien seuil `< 2 -> None` la faisait disparaitre de la carte
    # SANS AUCUN SIGNAL, juste au moment ou elle est la plus interessante : c'est
    # ce qui restait a corriger apres l'affaire CXMT (688825.SS, 1re capi
    # chinoise cotee le 27/07/2026 — detectee par le radar, ajoutee au pool, et
    # malgre tout absente de la carte). Avec une seule clôture, on se rabat sur
    # `chartPreviousClose` comme reference de variation : le titre s'affiche des
    # son 1er jour, avec une variation exacte et non inventee.
    if len(closes) == 1:
        prev1 = obj.get("chartPreviousClose")
        cur1 = closes[0]
        if not prev1 or prev1 <= 0:
            return None                       # aucune reference -> on n'invente pas
        d = round((cur1 / prev1 - 1) * 100, 2)
        return {"px": round(cur1, 4), "d1": d, "d5": d, "m1": d, "m3": d, "ytd": d,
                "pc": round(prev1, 4), "c5": round(prev1, 4), "c1m": round(prev1, 4),
                "c3m": round(prev1, 4), "cy": round(prev1, 4)}
    if len(closes) < 2: return None
    cur = closes[-1]; n = len(closes); prev = obj.get("chartPreviousClose")
    # CLOTURES DE REFERENCE (2026-07-29) — le coeur du correctif « variations fausses ».
    # Elles sont FIXES pendant la seance (une cloture passee ne bouge plus) : en les
    # exposant au client, chaque % devient une fonction PURE du prix affiche
    # (pct = prix / ref - 1). Avant, prix et % etaient deux snapshots independants qui
    # divergeaient des que le prix bougeait -> l'utilisateur voyait un prix et une
    # variation qui ne se correspondaient plus, ni entre eux ni avec Yahoo/TradingView.
    ref_1d = closes[n-2]                                   # veille = seance precedente
    ref_5d = closes[n-6] if n >= 6 else ref_1d             # 5 seances (= « 5J »)
    ref_1m = _ref_at(pairs, 30) or closes[0]               # 1 mois CALENDAIRE (avant : 22 seances)
    ref_3m = _ref_at(pairs, 91) or closes[0]               # 3 mois CALENDAIRES
    # YTD : la fenetre spark est passee de `ytd` a `1y` (il faut >90 jours d'historique
    # pour la reference 3 mois, ce que `ytd` ne fournit pas en janvier-mars). Du coup
    # `chartPreviousClose` ne designe PLUS la cloture du 31/12 mais celle d'avant la
    # fenetre d'un an -> la reference YTD est desormais lue dans la SERIE, a la borne
    # du 1er janvier. Plus robuste : elle ne depend plus du sens que Yahoo donne a un
    # champ selon le `range` demande.
    ref_ytd = _ref_before(pairs, _jan1_ts())
    if not ref_ytd or ref_ytd <= 0:
        ref_ytd = prev if (prev and prev > 0) else closes[0]
    chg1d = round((cur / ref_1d - 1) * 100, 2)
    chg5d = round((cur / ref_5d - 1) * 100, 2)
    chg1m = round((cur / ref_1m - 1) * 100, 2)
    chg3m = round((cur / ref_3m - 1) * 100, 2)
    chg_ytd = round((cur / ref_ytd - 1) * 100, 2)
    return {"px": round(cur, 4), "d1": chg1d, "d5": chg5d, "m1": chg1m, "m3": chg3m, "ytd": chg_ytd,
            "pc": round(ref_1d, 4), "c5": round(ref_5d, 4),
            "c1m": round(ref_1m, 4), "c3m": round(ref_3m, 4), "cy": round(ref_ytd, 4)}

def fetch_spark(tickers, rng="ytd"):
    out = {}
    for i in range(0, len(tickers), 10):
        batch = tickers[i:i+10]
        sy = urllib.parse.quote(",".join(batch), safe=",")
        url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={sy}&range={rng}&interval=1d"
        got = False
        for k in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=25)
                if r.status_code == 200:
                    for s, obj in r.json().items():
                        if obj: out[s] = obj
                    got = True
                    break
                log(f"spark HTTP {r.status_code} (batch {batch[0]}+) retry {k+1}/3")
            except Exception as e:
                log(f"spark err {e} retry {k+1}/3")
            time.sleep(2 * (k + 1))
        _batch_ok() if got else _batch_failed()
        time.sleep(0.3)
    return out

def fetch_history(tickers, rng="5y", interval="1d"):
    out = {}
    for i in range(0, len(tickers), 10):
        batch = tickers[i:i+10]
        sy = urllib.parse.quote(",".join(batch), safe=",")
        url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={sy}&range={rng}&interval={interval}"
        got = False
        for k in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                if r.status_code == 200:
                    for s, obj in r.json().items():
                        if not obj: continue
                        ts = obj.get("timestamp") or []; cl = obj.get("close") or []
                        pts = [[t, round(float(c), 4)] for t, c in zip(ts, cl) if c is not None]
                        if pts: out[s] = pts
                    got = True
                    break
            except Exception as e:
                log(f"hist err {e} retry {k+1}/3")
            time.sleep(2 * (k + 1))
        _batch_ok() if got else _batch_failed()
        time.sleep(0.4)
    return out


# ── Anti-DOUBLON de societe : une entreprise = UNE bulle ───────────────────
# Constate le 2026-08-10 : ASML apparaissait DEUX FOIS sur la carte Europe
# (ASML.SW et ASMN.VI), et 11 autres societes avec elle (Roche, Nestle, LVMH,
# Novartis, L'Oreal, Novo Nordisk, ABB, Lindt, Prosus, Infineon, Munich Re) —
# plus 10 autres dans l'extension rangs 101-300 et 4 doubles cotations A+H en
# Chine. Cause : le dedup de build_stock_universe.py repose sur le NOM Yahoo,
# qui change d'une place a l'autre (« ASML Holding N.V » a Vienne contre
# « ASML HOLDING EO -,09 » a Zurich) — deux lignes miroir du meme titre.
#
# Cle retenue : le DOMAINE de la societe (stable d'une place a l'autre),
# CONFIRME par un second signal quantitatif — meme flottant (`so`) a 15 % pres
# OU meme capitalisation a 3 % pres. Les deux signaux sont necessaires :
#   • le domaine seul fusionnerait a tort des societes DISTINCTES qui partagent
#     un site (Sartorius AG / Sartorius Stedim Biotech, JSW Energy / JSW
#     Infrastructure, Heineken N.V. / Heineken Holding) ;
#   • `so` seul raterait Lindt N / Lindt PS (classes d'actions au nominal
#     different, donc flottants sans rapport) — que la capi, elle, rattrape.
# Verifie sur les 31 groupes « meme domaine » des 4 zones (rangs 1-300) :
# 26 vrais doublons fusionnes, 5 faux positifs correctement laisses intacts.
# Un titre SANS domaine n'est jamais fusionne (pas de preuve) — il est logue.
SO_TOL = 0.15      # ecart max de flottant pour reconnaitre la meme societe
MC_TOL = 0.03      # ecart max de capitalisation (rattrape les classes d'actions)
# Place PRIMAIRE preferee quand plusieurs cotations survivent. Le domicile
# (`pays`, issu de l'univers) prime ; sinon, ordre generique ou les places de
# cotation miroir notoires (Vienne, Varsovie) arrivent en dernier.
HOME_SUFFIX = {
    "Netherlands": ("AS",), "France": ("PA",), "Germany": ("DE",),
    "Switzerland": ("SW",), "United Kingdom": ("L",), "Spain": ("MC",),
    "Italy": ("MI",), "Sweden": ("ST",), "Denmark": ("CO",), "Finland": ("HE",),
    "Norway": ("OL",), "Belgium": ("BR",), "Portugal": ("LS",),
    "Austria": ("VI",), "Ireland": ("IR",), "Poland": ("WA",),
    "China": ("SS", "SZ"), "Hong Kong": ("HK",), "India": ("NS", "BO"),
}
VENUE_RANK = ["AS", "PA", "DE", "MI", "MC", "CO", "ST", "HE", "OL", "BR", "LS",
              "L", "SS", "SZ", "NS", "BO", "SW", "HK", "VI", "WA"]

# « 2. Linie » : ligne de negoce SECONDAIRE d'un meme titre a Zurich (artefact
# SIX/Yahoo). Elle ne peut pas etre une autre societe : meme domaine -> meme
# emetteur, quel que soit l'ecart de flottant (Lindt N 133 905 titres contre
# 1 084 452 sur sa 2e ligne).
DEUXIEME_LIGNE = re.compile(r"\b2\.\s*LINIE\b", re.I)

def _same_company(a, b):
    """Deux lignes du meme domaine designent-elles la MEME societe ?"""
    if DEUXIEME_LIGNE.search(a.get("n") or "") or DEUXIEME_LIGNE.search(b.get("n") or ""):
        return True
    sa, sb = a.get("so"), b.get("so")
    if sa and sb and abs(sa - sb) / max(sa, sb) <= SO_TOL: return True
    ma, mb = a.get("mc"), b.get("mc")
    if ma and mb and abs(ma - mb) / max(ma, mb) <= MC_TOL: return True
    return False

# Marques d'une raison sociale defiguree par la place de cotation : nominal de
# l'action (« EO -,09 », « EO 0,2 »), forme du titre (INH. = Inhaber, N/PS),
# 2e ligne de negoce… Un nom qui en porte passe apres un nom propre.
NOM_ABIME = re.compile(r"\bEO\s|\bINH\b|\bNAM\b|\b2\.\s*LINIE\b|\bPS\b|\bREG\b", re.I)

def _name_score(n, t):
    """Plus c'est BAS, meilleur est le libelle (min() choisit le plus propre).
    A egalite on prend le PLUS COURT : « ABB » vaut mieux que « ABB LTD. N »."""
    abime = 1 if NOM_ABIME.search(n) else 0
    ticker = 1 if n == t or n == t.split(".")[0] else 0          # « NESR.DE » en guise de nom
    capitales = 1 if n == n.upper() else 0                       # « GALDERMA GROUP N »
    return (abime, ticker, capitales, len(n), n)

def _listing_score(row, pays):
    """Plus c'est BAS, plus la cotation est primaire (donc gardee)."""
    t = row["t"]; sfx = t.rsplit(".", 1)[-1] if "." in t else ""
    home = HOME_SUFFIX.get(pays or "", ())
    return (0 if sfx in home else 1,
            VENUE_RANK.index(sfx) if sfx in VENUE_RANK else len(VENUE_RANK),
            -(row.get("mc") or 0), t)

def dedupe_listings(zone, rows, meta):
    """Une societe = une bulle. Retourne (rows_uniques, [(garde, [ecartes])])."""
    by_dom = {}
    out, merged = [], []
    for r in rows:
        d = r.get("d")
        if not d: out.append(r); continue
        by_dom.setdefault(d, []).append(r)
    for d, group in by_dom.items():
        clusters = []                       # regroupement par proche-en-proche
        for r in group:
            for c in clusters:
                if any(_same_company(r, x) for x in c): c.append(r); break
            else:
                clusters.append([r])
        for c in clusters:
            # Le domicile est une propriete de la SOCIETE, pas de la ligne : Yahoo
            # ne le renseigne que sur certaines places (Nestle est « Switzerland »
            # sur sa ligne allemande et vide sur la suisse). On le resout donc au
            # niveau du groupe, sinon la cotation miroir gagnerait sur la primaire.
            pays = next((( meta.get(r["t"]) or {}).get("pays") for r in c
                         if (meta.get(r["t"]) or {}).get("pays")), None)
            c.sort(key=lambda r: _listing_score(r, pays))
            garde = dict(c[0])
            # Le libelle affiche vient de la ligne au nom le plus PROPRE du groupe,
            # meme si la cotation retenue est une autre : les places miroir
            # renvoient des raisons sociales defigurees par le nominal du titre
            # (« ASML HOLDING EO -,09 » a Zurich pour « ASML Holding N.V »).
            garde["n"] = min(((r.get("n") or r["t"], r["t"]) for r in c),
                             key=lambda x: _name_score(*x))[0]
            out.append(garde)
            if len(c) > 1: merged.append((c[0]["t"], [x["t"] for x in c[1:]]))
    return out, merged


# ── Construction d'une zone : Top 100 evolutif par mcap USD live ────────────
def build_zone(zone, pool, fx, prev_stocks=None):
    tickers = [r["t"] for r in pool]
    meta = {r["t"]: r for r in pool}
    prev = {s["t"]: s for s in (prev_stocks or [])}
    spark = fetch_spark(tickers, "1y")
    rows = []
    for t in tickers:
        obj = spark.get(t)
        if not obj: continue
        ch = compute_changes(obj)
        if not ch: continue
        m = meta[t]; ccy = m.get("ccy", "USD")
        p_usd = price_to_usd(ch["px"], ccy, fx)
        mc = mcap_usd_b(m.get("so"), ch["px"], ccy, fx)
        if mc is None: mc = m.get("mc0")          # fallback : mcap snapshot de l'univers
        if not mc: continue                        # ni live ni fallback -> on saute (jamais de bulle nulle)
        row = {"t": t, "n": m.get("n", t), "s": m.get("s", "—"), "d": m.get("d"),
               "ccy": ccy, "p": round(p_usd, 2) if p_usd is not None else None,
               "d1": ch["d1"], "d5": ch["d5"], "m1": ch["m1"], "m3": ch["m3"], "ytd": ch["ytd"],
               "mc": round(mc)}
        # Socle du RECALCUL LIVE cote client (cf functions/live/quotes.js) : clotures de
        # reference en devise NATIVE (comme le prix renvoye par le proxy Yahoo) + actions
        # en circulation. Le client n'a alors besoin que du prix live pour reconstruire
        # 1J/5J/1M/YTD *et* la capi, tous coherents avec le prix qu'il affiche.
        row["pc"] = ch["pc"]; row["c5"] = ch["c5"]; row["c1m"] = ch["c1m"]
        row["c3m"] = ch["c3m"]; row["cy"] = ch["cy"]
        row["pn"] = ch["px"]                       # prix natif du snapshot (base du % de secours)
        if m.get("so"): row["so"] = m["so"]
        if m.get("lbl"): row["lbl"] = m["lbl"]   # label court d'affichage (bulle/movers) ex. CN: ICBC, CATL…
        rows.append(row)
    # merge-preserve par TITRE : un ticker absent du spark ce run garde sa derniere valeur
    got = {r["t"] for r in rows}
    backfill = 0
    for t in tickers:
        if t in got: continue
        if t in prev:
            rows.append(prev[t]); backfill += 1
    # une societe = une bulle (cotations miroir fusionnees avant le classement,
    # sinon le rang libere serait perdu au lieu de laisser monter le suivant)
    n_avant = len(rows)
    rows, merged = dedupe_listings(zone, rows, meta)
    if merged:
        log(f"  zone {zone}: {n_avant - len(rows)} cotation(s) miroir fusionnee(s): "
            + ", ".join(f"{k}<-{'+'.join(v)}" for k, v in merged[:14])
            + (" …" if len(merged) > 14 else ""))
    sans_dom = [r["t"] for r in rows if not r.get("d")]
    if sans_dom:
        log(f"  zone {zone}: sans domaine, doublon indetectable ({len(sans_dom)}): {sans_dom[:10]}")
    rows.sort(key=lambda r: r["mc"], reverse=True)
    top = rows[:DEEP_N]                  # classement profond : le Top 100 en est le prefixe
    live = len(rows) - backfill          # lignes issues du spark de CE run (pas du cache)
    log(f"  zone {zone}: {len(spark)}/{len(tickers)} spark"
        + (f" + {backfill} backfill" if backfill else "")
        + f" -> {len(rows)} valides -> classement {len(top)} (top100 + ext {max(0, len(top)-TOP_N)})"
        + (f" | #1 {top[0]['n']} ${top[0]['mc']}B" if top else ""))
    # Garde-fou anti-regression : aucune PAIRE reconnue « meme societe » ne doit
    # subsister dans ce qui part a l'ecran (le doublon ASML, lui, est alle
    # jusqu'a la carte Europe sans qu'aucun controle ne bronche).
    par_dom = {}
    for r in top:
        if r.get("d"): par_dom.setdefault(r["d"], []).append(r)
    alerte = [d for d, g in par_dom.items() if len(g) > 1
              and any(_same_company(a, b) for i, a in enumerate(g) for b in g[i+1:])]
    info = [d for d, g in par_dom.items() if len(g) > 1 and d not in alerte]
    if alerte:
        log(f"  ALERTE zone {zone}: doublon NON resolu (tolerances a revoir): {sorted(alerte)}")
    if info:
        # attendu : societes DISTINCTES partageant un site (Sartorius, JSW…)
        log(f"  zone {zone}: domaines partages par des societes distinctes (ok): {sorted(info)}")
    return top, live


# ── References INTRA-JOURNALIERES (1H / 4H) ────────────────────────────────
# Definition retenue, identique a celle des Narrative Trackers : « cloture d'il y a
# n BARRES -> dernier cours ». En barres et non en heures d'horloge, sinon la vue 1H
# d'un marche ferme mesurerait une plage sans aucune cotation et afficherait 0 %.
#
# PIEGE Yahoo (deja rencontre sur fetch_trackers_tf.py) : la serie intraday se termine
# par une pseudo-barre HORS GRILLE, horodatee a l'instant de la requete, qui porte le
# prix courant. Comptee comme une barre, elle decale toute la fenetre d'un cran (« 4h »
# ne couvrant plus que quelques minutes). On la replie donc sur la derniere barre alignee.
#
# CORRECTIF 2026-08-11 (signale par le user : « en 1H/4H ca affiche des variations alors
# que le marche est ferme »). Le seuil valait 1800 s, ce qui ne rattrapait QUE la
# pseudo-barre des marches ouverts. Or une place FERMEE termine sa serie par une DEMI-BARRE
# DE CLOTURE : NYSE 19:30 UTC puis 20:00 UTC, SSE 06:30 puis 07:00 — un ecart de
# EXACTEMENT 1800 s, que `< 1800` laisse passer. Elle etait donc comptee comme une barre
# pleine et decalait la fenetre du meme cran que la pseudo-barre. Mesure sur AAPL le
# 2026-08-11 : « 1H » couvrait 0 minute de cotation (d'ou une carte 1H uniformement grise,
# mediane +0,00 % sur les 100 valeurs US) et « 4H » 2 h 30 au lieu de 4 h.
# Regle : toute derniere barre qui n'occupe pas un creneau PLEIN (ecart < le pas de grille)
# est repliee, qu'elle soit une pseudo-barre ou une demi-barre de cloture.
HOUR_GRID = 3600          # pas de la grille (INTRADAY_INTERVAL = « 1h »)

def _normalize_hourly(pts):
    """Replie la derniere barre incomplete (pseudo-barre de marche ouvert OU demi-barre
    de cloture) sur la derniere barre ALIGNEE, qui porte alors le prix courant.
    La grille horaire reste ainsi reguliere."""
    if len(pts) >= 2 and (pts[-1][0] - pts[-2][0]) < HOUR_GRID:
        return pts[:-2] + [[pts[-2][0], pts[-1][1]]]
    return pts

def _intraday_refs(pts):
    """(ref_1h, ref_4h) en devise NATIVE, ou (None, None) si la serie est trop courte."""
    if not pts: return None, None
    p = _normalize_hourly([list(x) for x in pts])
    if len(p) < 2: return None, None
    r1 = p[-2][1]
    r4 = p[-5][1] if len(p) >= 5 else p[0][1]
    return r1, r4

def load_json(p):
    try: return json.loads(Path(p).read_text())
    except Exception: return None

def main():
    force = "--force" in sys.argv
    uni = load_json(UNIVERSE)
    if not uni:
        log(f"FATAL: univers introuvable ({UNIVERSE})"); sys.exit(1)
    if not preflight():
        sys.exit(1)                       # reseau HS -> retente au prochain fire launchd (30 min)

    prev = load_json(CACHE_FILE) or {}
    prev_zones = prev.get("zones", {})
    intra_by_zone = {}                    # zone -> series horaires du Top 100 (ecrites plus bas)

    # freshness prix
    refresh_prices = True
    if CACHE_FILE.exists() and not force:
        age = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 60
        if age < CACHE_MAX_MINUTES:
            refresh_prices = False
            log(f"prix cache frais ({age:.1f} min) — skip")

    if refresh_prices:
        fx = fetch_fx()
        zones = {}
        for z in ZONE_ORDER:
            if z not in uni or not uni[z].get("pool"):
                if z in prev_zones: zones[z] = prev_zones[z]   # zone absente univers -> garder cache
                continue
            try:
                top, live = build_zone(z, uni[z]["pool"], fx, prev_zones.get(z, {}).get("stocks"))
            except NetworkDown as e:
                log(f"ABORT run prix ({e}) — cache precedent conserve, retry au prochain fire")
                sys.exit(1)
            now = datetime.now().isoformat()
            # Timestamp HONNETE (2026-07-20) : `updated` n'est stampe frais que si la zone a
            # assez de lignes LIVE de CE run. Avant, le test portait sur len(top) qui compte le
            # backfill -> une zone 0/112 spark + 100 backfill (panne DNS) etait estampillee
            # fraiche et le badge STALE client ne se declenchait jamais (prix perimes affiches
            # comme frais). Une zone majoritairement backfill garde son `updated` precedent.
            if live >= MIN_ZONE_OK or z not in prev_zones:
                zones[z] = {"updated": now, "source": ZONE_SOURCE.get(z, ""), "stocks": top}
            elif len(top) >= MIN_ZONE_OK:
                log(f"  zone {z}: {live} live (<{MIN_ZONE_OK}) — donnees servies mais updated conserve (STALE honnete)")
                zones[z] = {"updated": prev_zones[z].get("updated", now),
                            "source": ZONE_SOURCE.get(z, ""), "stocks": top}
            else:
                log(f"  zone {z}: {len(top)} titres (<{MIN_ZONE_OK}) — merge-preserve cache precedent")
                zones[z] = prev_zones[z]
        now = datetime.now().isoformat()

        # ── Couche HORAIRE : elle doit passer AVANT l'ecriture du cache, car elle
        # fournit les clotures de reference 1H et 4H que le client utilisera pour
        # recalculer ces deux fenetres a partir du prix live. Le Top 100 tire un
        # mois d'historique (il alimente aussi le modal) ; les rangs 101-300 se
        # contentent de 5 jours, largement assez pour deux references et bien plus
        # leger a telecharger comme a stocker.
        for z in ZONE_ORDER:
            stocks = zones.get(z, {}).get("stocks", [])
            if not stocks: continue
            head = [s["t"] for s in stocks[:TOP_N]]
            tail = [s["t"] for s in stocks[TOP_N:]]
            try:
                got = fetch_history(head, INTRADAY_RANGE, INTRADAY_INTERVAL) if head else {}
                if tail:
                    got_tail = fetch_history(tail, "5d", INTRADAY_INTERVAL)
                else:
                    got_tail = {}
            except NetworkDown as e:
                log(f"intraday {z}: ABORT ({e}) — references 1H/4H du run precedent conservees")
                break
            intra_by_zone[z] = got                      # seul le Top 100 est publie comme historique
            got_tail = got_tail or {}
            n1h = 0
            for s in stocks:
                pts = got.get(s["t"]) or got_tail.get(s["t"])
                r1, r4 = _intraday_refs(pts)
                if r1: s["c1h"] = round(r1, 4); n1h += 1
                if r4: s["c4h"] = round(r4, 4)
            log(f"  intraday {z}: {len(got)}/{len(head)} top100 + {len(got_tail)}/{len(tail)} ext"
                f" -> {n1h}/{len(stocks)} references 1H")

        # Le fichier principal ne porte que le Top 100 ; les rangs 101-300 partent
        # dans un fichier d'extension par zone, charge seulement si l'utilisateur
        # elargit le spectre.
        deep = {z: zones[z].get("stocks", []) for z in zones}
        for z in zones:
            zones[z] = dict(zones[z], stocks=deep[z][:TOP_N])
        for z, rows in deep.items():
            ext = rows[TOP_N:]
            gname, jsname, jsonname = EXT_FILES[z]
            ext_json = CACHE_DIR / jsonname; ext_js = CACHE_DIR / jsname
            if not ext:
                log(f"  ext {z}: 0 titre au-dela du rang {TOP_N} (pool trop court) — fichier inchange")
                continue
            payload_ext = {"updated": now, "stocks": ext}
            ext_json.write_text(json.dumps(payload_ext, separators=(",", ":")))
            with open(ext_js, "w") as f:
                f.write(f"window.{gname}=" + json.dumps(payload_ext, separators=(",", ":")) + ";\n")
            log(f"  wrote {jsname} (rangs {TOP_N+1}-{TOP_N+len(ext)}, {ext_js.stat().st_size // 1024} Ko)")

        payload = {"updated": now, "fx": fx, "zones": zones}
        CACHE_FILE.write_text(json.dumps(payload, separators=(",", ":")))
        log(f"wrote {CACHE_FILE.name}")

        zones_js = json.dumps(zones, separators=(",", ":"))
        with open(CACHE_JS, "w") as f:
            f.write("window.__STOCK_ZONES__=" + zones_js + ";\n")
            # VRAI alias (2026-07-29), plus une copie : la couche live du client reecrit les
            # lignes de __STOCK_ZONES__.us EN PLACE. Avec une copie serialisee a part, le
            # bandeau defilant (accueil.js) et les autres consommateurs de __STOCK_DATA__
            # seraient restes sur les prix du cache pendant que la carte affichait le live —
            # exactement le genre de contradiction que la refonte doit supprimer.
            # Bonus : ~13 Ko de moins dans le fichier servi.
            f.write("window.__STOCK_DATA__=(window.__STOCK_ZONES__&&window.__STOCK_ZONES__.us"
                    "&&window.__STOCK_ZONES__.us.stocks)||[];\n")
            f.write(f"window.__STOCK_DATA_UPDATED__={json.dumps(now)};\n")
            f.write("window.__STOCK_FX__=" + json.dumps(fx, separators=(",", ":")) + ";\n")
        log(f"wrote {CACHE_JS.name}")
        cur_zones = zones
    else:
        cur_zones = prev_zones

    # ── Historique 5 ans par zone (sur le Top 100 selectionne), gated, splitte ──
    for z in ZONE_ORDER:
        gname, jsname, jsonname = HISTORY_FILES[z]
        hist_json = CACHE_DIR / jsonname; hist_js = CACHE_DIR / jsname
        if hist_json.exists() and not force:
            age = (datetime.now().timestamp() - hist_json.stat().st_mtime) / 60
            if age < HISTORY_MAX_MINUTES:
                continue
        tickers = [s["t"] for s in cur_zones.get(z, {}).get("stocks", [])]
        if not tickers: continue
        log(f"history {z}: {len(tickers)} tickers, 5y daily…")
        try:
            hist = fetch_history(tickers, "5y")
        except NetworkDown as e:
            log(f"history {z}: ABORT ({e}) — caches histo conserves"); break
        if len(hist) >= max(1, len(tickers) // 2):
            updated = datetime.now().isoformat()
            hist_json.write_text(json.dumps({"updated": updated, "histories": hist}, separators=(",", ":")))
            with open(hist_js, "w") as f:
                f.write(f"window.{gname}=" + json.dumps(hist, separators=(",", ":")) + ";\n")
                f.write(f"window.{gname}_UPDATED={json.dumps(updated)};\n")
            log(f"  wrote {jsname} ({len(hist)}/{len(tickers)})")
        else:
            log(f"  history {z} maigre ({len(hist)}/{len(tickers)}) — conserve cache precedent")

    # ── Couche HORAIRE ~1 mois par zone (chaque run, pas de TTL) ────────────────
    # Sert les onglets 5J et 1M du modal : 141 pts/mois/titre au lieu de 20, et un
    # bord droit a <1h au lieu de « derniere cloture connue ». Fichier separe et
    # LEGER (~280 Ko/zone) pour pouvoir etre reecrit toutes les 30 min sans faire
    # gonfler le snapshot git (le daily 5 ans pese 2,4 Mo -> reste a TTL 4h).
    for z in ZONE_ORDER:
        gname, jsname, jsonname = INTRADAY_FILES[z]
        intra_json = CACHE_DIR / jsonname; intra_js = CACHE_DIR / jsname
        tickers = [s["t"] for s in cur_zones.get(z, {}).get("stocks", [])]
        if not tickers: continue
        # Les series ont deja ete tirees plus haut (elles servaient a calculer les
        # references 1H/4H) : on ne redemande pas Yahoo une seconde fois. Elles ne
        # manquent que sur un run ou les prix etaient encore frais (<20 min).
        intra = intra_by_zone.get(z)
        if intra is None:
            log(f"intraday {z}: {len(tickers)} tickers, {INTRADAY_RANGE} {INTRADAY_INTERVAL}…")
            try:
                intra = fetch_history(tickers, INTRADAY_RANGE, INTRADAY_INTERVAL)
            except NetworkDown as e:
                log(f"intraday {z}: ABORT ({e}) — caches intraday conserves"); break
        # merge-preserve : un run maigre (Yahoo qui tousse) ne doit jamais ecraser
        # une couche horaire saine par du vide — meme regle que le daily.
        if len(intra) >= max(1, len(tickers) // 2):
            updated = datetime.now().isoformat()
            intra_json.write_text(json.dumps({"updated": updated, "histories": intra}, separators=(",", ":")))
            with open(intra_js, "w") as f:
                f.write(f"window.{gname}=" + json.dumps(intra, separators=(",", ":")) + ";\n")
                f.write(f"window.{gname}_UPDATED={json.dumps(updated)};\n")
            npts = sum(len(v) for v in intra.values()) // max(1, len(intra))
            log(f"  wrote {jsname} ({len(intra)}/{len(tickers)}, ~{npts} pts/titre, "
                f"{intra_js.stat().st_size // 1024} Ko)")
        else:
            log(f"  intraday {z} maigre ({len(intra)}/{len(tickers)}) — conserve cache precedent")


if __name__ == "__main__":
    main()
