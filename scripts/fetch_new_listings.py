#!/usr/bin/env python3
"""RADAR NOUVELLES COTATIONS (IPO) & NOUVEAUX ENTRANTS TOP-CAPI — MULTI-PLACES.

═══════════════════════════════════════════════════════════════════════════════
POURQUOI CE SCRIPT EXISTE (2026-07-28)
═══════════════════════════════════════════════════════════════════════════════
CXMT / 长鑫存储 (688825.SS) s'est cotée le 27/07/2026 et est devenue la PREMIÈRE
capitalisation chinoise (~3 143 Md CNY ≈ 440 Md$) — sans jamais apparaître nulle
part sur le site.

Cause racine : les pools d'actions (stock_universe.json) sont des listes ÉCRITES
À LA MAIN, figées au 06/06/2026 (copiées de companiesmarketcap). Le Top 100 par
zone est bien re-classé en live à chaque run de fetch_stock_bubble.py, MAIS
uniquement À L'INTÉRIEUR de cette liste morte → une société absente du pool est
invisible quelle que soit sa capitalisation. Aucun mécanisme n'existait pour
faire entrer une société nouvellement cotée.

Ce n'était pas un cas isolé : l'audit du 28/07/2026 a montré que 21 des 60
premières capis chinoises manquaient au pool, dont ~10 réellement absentes du
site (sans ligne Hong Kong de substitution) — MetaX 688802 (cotée 17/12/2025,
42 Md$) et 盛合晶微 688820 (cotée 21/04/2026, 33 Md$) avaient déjà été ratées.
Côté US le pool = S&P 100, or un indice met des ANNÉES à intégrer une IPO :
CoreWeave, Circle, Coinbase, Robinhood, Reddit étaient absents.

═══════════════════════════════════════════════════════════════════════════════
CE QU'IL FAIT
═══════════════════════════════════════════════════════════════════════════════
1. Interroge chaque place à sa source NATIVE (classement COMPLET par capi, pas
   une liste figée) :
     • Chine A-shares (SH/SZ/STAR/ChiNext/BSE) → Eastmoney clist  (donne la DATE
       DE COTATION f26 — détection IPO exacte, gratuit, sans clé)
     • US (7 000+ titres NYSE/NASDAQ/AMEX)      → screener Nasdaq (donne ipoyear)
     • HK / Europe / Inde / Japon                → screener Yahoo par place
2. Convertit en USD, dédoublonne par SOCIÉTÉ (une double cotation SH+HK ne doit
   pas créer deux bulles), diffe contre les univers du site.
3. Valide chaque inconnu via Yahoo chart meta → `firstTradeDate` = date de
   première cotation réelle, quelle que soit la place → distingue :
     • NOUVELLE COTATION (IPO < 12 mois)
     • NOUVEL ENTRANT (société ancienne jamais captée / montée en capi)
4. AUTO-AJOUT dans les univers MÉCANIQUES (stock_universe.json → cartes à bulles
   us/cn/eu/in) dès que la capi dépasse le plancher d'entrée de la zone.
5. ALERTE pour les univers CURÉS (TradFi Tracker, 862 titres classés à la main
   par secteur/narratif) → new_listings_pending_curation.json, avec proposition
   de région/secteur, à valider.
6. Écrit new_listings_cache.{json,js} → bandeau d'alerte sur l'Accueil.

SORTIES (~/Library/Caches/site_crypto_finance/) :
  new_listings_cache.json / .js        → window.__NEW_LISTINGS__ (bandeau Accueil)
  new_listings_pending_curation.json   → à intégrer au TradFi Tracker
  stock_universe.json (MODIFIÉ en place, backup .bak_YYYYmmdd_HHMM avant écriture)

SÉCURITÉS (mémoire projet) :
  • Jamais d'écrasement par du vide : une place qui échoue est IGNORÉE (le pool
    existant est conservé tel quel), jamais interprétée comme « plus rien ».
  • Écriture ATOMIQUE (tmp + os.replace) sur stock_universe.json — un run tué en
    plein vol ne peut pas laisser un univers tronqué (cf. gel snapshot 06/2026).
  • Backup horodaté avant toute modification de l'univers.
  • Pré-vol réseau : DNS mort → exit rapide, launchd retente plus tard.
  • Un candidat non validé par Yahoo (ticker inconnu, pas de prix) n'est JAMAIS
    ajouté — un pool pollué casse les cartes à bulles.

Usage :
  python3 fetch_new_listings.py                 # run normal (auto-ajout activé)
  python3 fetch_new_listings.py --dry-run       # détecte et rapporte, n'écrit pas l'univers
  python3 fetch_new_listings.py --zones cn,us   # restreint les places
  python3 fetch_new_listings.py --force         # ignore le TTL de cache
"""
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

try:
    from curl_cffi import requests as cr          # Yahoo bloque requests stdlib (429)
except Exception:                                  # pragma: no cover
    cr = None

HERE = Path(__file__).resolve().parent
UNIVERSE = HERE / "stock_universe.json"
TRADFI_SRC = HERE / "fetch_tradfi.py"              # univers curé (lecture seule ici)

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "new_listings_cache.json"
OUT_JS = CACHE_DIR / "new_listings_cache.js"
PENDING = CACHE_DIR / "new_listings_pending_curation.json"
BUBBLE_CACHE = CACHE_DIR / "stock_bubble_cache.json"

# TTL VOLONTAIREMENT INFÉRIEUR à l'intervalle launchd (6 h) : chaque fire doit
# réellement écrire. Avec un TTL plus long, les fires intermédiaires sortiraient
# sans toucher le cache témoin — et le watchdog de fraîcheur, qui juge un job à la
# date de son cache, aurait fini par déclarer SOURCE_BROKEN un radar en parfait
# état (il relance, le script skippe, le mtime ne bouge pas, 3 fois → « cassé »).
CACHE_MAX_HOURS = 5
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Accept": "application/json"}

# ── Fenêtres & planchers ────────────────────────────────────────────────────
IPO_WINDOW_DAYS = 365          # « nouvelle cotation » = premier échange < 12 mois
BANNER_DAYS = 45               # ce qui reste affiché dans le bandeau Accueil
ALERT_FLOOR_USD_B = 5.0        # on ALERTE dès 5 Md$ pour une cotation récente
ADD_FLOOR_FALLBACK_B = 15.0    # plancher d'AUTO-AJOUT si le top-100 live est illisible
# Taille max d'un pool après ajout. Le runtime n'affiche que le Top 100 ; au-delà,
# le pool est un simple VIVIER de candidats re-classés en live. On le garde large
# car l'élagage se fait sur `mc0`, une photo de capi parfois ancienne : trop serrer
# reviendrait à éjecter une société sur une valeur périmée. Coût d'un candidat
# supplémentaire : une cotation dans un batch de 10 (négligeable).
POOL_MAX = 150
VALIDATE_MAX_PER_ZONE = 45     # validations Yahoo par place et par run
VALIDATE_MAX_SECONDS = 420     # garde-fou temps par place (7 min)
META_BACKFILL_PER_RUN = 12     # fiches enrichies (secteur/domaine) par run

def log(m):
    sys.stderr.write(f"[NewListings] {m}\n")
    sys.stderr.flush()


# ════════════════════════════════════════════════════════════════════════════
# Pré-vol réseau — une panne DNS ne doit pas produire « aucun nouvel entrant »
# (silence indistinguable d'un vrai « rien de neuf » = le bug qu'on corrige).
# ════════════════════════════════════════════════════════════════════════════
def preflight():
    for host in ("query1.finance.yahoo.com", "push2.eastmoney.com"):
        try:
            socket.setdefaulttimeout(6)
            socket.gethostbyname(host)
            return True
        except Exception:
            continue
    log("PRÉ-VOL KO : DNS injoignable — abandon (retry au prochain fire launchd)")
    return False


# ════════════════════════════════════════════════════════════════════════════
# FX → USD (usd par unité de devise MAJOR), avec bornes de plausibilité
# ════════════════════════════════════════════════════════════════════════════
SANE_FX = {"EUR": (0.9, 1.4), "GBP": (1.0, 1.7), "CHF": (0.9, 1.5), "SEK": (0.07, 0.13),
           "DKK": (0.12, 0.18), "NOK": (0.07, 0.13), "PLN": (0.18, 0.32),
           "HKD": (0.11, 0.14), "CNY": (0.11, 0.17), "INR": (0.009, 0.014),
           "JPY": (0.004, 0.010), "CAD": (0.6, 0.9), "AUD": (0.55, 0.85)}


def fetch_fx():
    majors = list(SANE_FX.keys())
    out = {"USD": 1.0}

    def spark(sy):
        u = ("https://query1.finance.yahoo.com/v8/finance/spark?symbols="
             + urllib.parse.quote(sy) + "&range=5d&interval=1d")
        try:
            return requests.get(u, headers=UA, timeout=20).json()
        except Exception as e:
            log(f"FX err {e}")
            return {}

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
            if x and SANE_FX[c][0] <= x <= SANE_FX[c][1]:
                v = x
        if v is None:
            o = d2.get(f"{c}=X")
            if o:
                x = last(o)
                if x and x > 0 and SANE_FX[c][0] <= 1.0 / x <= SANE_FX[c][1]:
                    v = 1.0 / x
        if v is None:
            log(f"FX {c} : aucune valeur plausible — place ignorée ce run (pas de conversion à l'aveugle)")
        out[c] = v
    out["GBp"] = (out.get("GBP") / 100.0) if out.get("GBP") else None   # pence
    return out


# ════════════════════════════════════════════════════════════════════════════
# Normalisation société (dédup double cotation SH/HK, ADR, .NS/.BO…)
# Reprise de build_stock_universe.py pour rester cohérent avec l'univers existant.
# ════════════════════════════════════════════════════════════════════════════
NAME_STRIP = re.compile(r"\b(S\.A\.|PLC|Plc|Limited|Ltd\.?|Holdings|Holding|Group|Inc\.?|"
                        r"Corporation|Corp\.?|AG|SE|N\.V\.|NV|S\.p\.A\.|SpA|ASA|AB|Oyj|"
                        r"Company|Co\.?|& Co\.?|Public|Industries|International|"
                        r"Incorporated|Class [A-Z]|Common Stock|Ordinary Shares|"
                        r"ADR|American Depositary Shares?)\b", re.I)


# Suffixes de CLASSE / TYPE DE TITRE en fin de libellé. « ROCHE PS » (certificat
# de participation Roche coté à Vienne, 359 Md$) échappait au dédoublonnage et
# entrait dans le pool européen comme une société à part entière, alors que Roche
# y figure déjà. Idem pour les lignes « REG SHS », « CDI », « ORD », « -B ».
CLASS_TOKEN = re.compile(r"[\s\-]+(ps|pref|pfd|prf|reg|shs|cdi|npv|ord|part|cert|"
                         r"genussschein|spons|sponsored|new|rg|[abc])$", re.I)


KEY_LEN = 18      # 14 collait « China Merchants Bank » et « China Merchants Port »

# Mots de liaison sans valeur identifiante. Sans eux, deux libellés de la MÊME
# société ne se rejoignaient pas : le pool porte « Industrial and Commerc »
# (ICBC via 1398.HK) tandis que Yahoo renvoie « INDUSTRIAL & COMMERCIAL BK OF C »
# pour l'action A 601398 → aucun préfixe commun à cause du « and », et ICBC était
# signalée comme la plus grosse capi chinoise manquante alors qu'elle est au pool.
STOPWORDS = {"and", "the", "of", "for", "de", "du", "des", "la", "le", "et", "und"}


def _clean_words(n):
    n = (n or "").lower()
    n = NAME_STRIP.sub("", n)
    prev = None
    while prev != n:                       # « xxx reg shs » → « xxx »
        prev = n
        n = CLASS_TOKEN.sub("", n.strip())
    return [w for w in re.split(r"[^a-z0-9]+", n) if w and w not in STOPWORDS]


def norm_company(n):
    return "".join(_clean_words(n))[:KEY_LEN]


def word_keys(n):
    """Préfixes CUMULATIFS par mot : « Meta Platforms Inc » → {meta, metaplatforms}.
    Découper par MOT (et non caractère par caractère) est ce qui distingue une
    vraie cotation miroir d'une société homonyme : « Bayerische Motoren Werke »
    ne commence par aucun mot égal à « bayer », donc BMW n'est pas confondue avec
    Bayer — alors qu'une comparaison caractère par caractère les fusionnait."""
    ws = _clean_words(n)
    out, acc = set(), ""
    for w in ws:
        acc += w
        out.add(acc[:KEY_LEN])
    return out


def short_key(n):
    """Clé COURTE (8 car.) pour rattraper les variantes de libellé d'une même
    société entre places : « Montage Technology » (Shanghai) vs « MONTAGE TECH »
    (Hong Kong) ne partagent pas leurs 14 premiers caractères, et échappaient donc
    au dédoublonnage → deux bulles pour une seule entreprise.
    8 caractères peuvent en revanche coller deux sociétés DISTINCTES (« China
    Merchants Bank » vs « China Merchants Port ») : une collision courte ne
    supprime donc jamais un candidat en silence, elle le route vers l'alerte
    humaine (cf. `suspect` dans main)."""
    n = (n or "").lower()
    n = NAME_STRIP.sub("", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n[:8]


CHAR_PREFIX_MIN = 8


def is_known_company(name, full_keys, word_union):
    """Vrai si `name` désigne une société DÉJÀ présente sous un autre libellé —
    « Amazon.com Inc » (Xetra) vs « Amazon » (pool US), « MONTAGE TECH » (Hong
    Kong) vs « Montage Technology » (Shanghai). Sert de pré-filtre AVANT toute
    requête réseau : les places européennes sont saturées de lignes miroir
    américaines (Apple, Berkshire, Intel… à Francfort, Vienne, Varsovie) qui
    épuisaient sinon le budget de validation.

    Deux règles, volontairement conservatrices :
      1. un préfixe par MOT du candidat est le nom COMPLET d'une société connue
         (ou l'inverse) — « meta » ↔ « Meta Platforms » ;
      2. à défaut, préfixe caractère mais seulement si les DEUX clés font ≥ 8
         caractères — « montagetech » ↔ « montagetechnology ».
    Ce qu'elles laissent passer À DESSEIN : « Zijin Gold » face à « Zijin Mining »
    (scission cotée en 2025, 39 Md$) et « China Merchants Port » face à « China
    Merchants Bank » — des sociétés bel et bien distinctes."""
    k = norm_company(name)
    if not k:
        return False
    if k in full_keys:
        return True
    if word_keys(name) & full_keys:          # règle 1, sens candidat → connu
        return True
    if k in word_union:                      # règle 1, sens connu → candidat
        return True
    if len(k) >= CHAR_PREFIX_MIN:            # règle 2
        for j in full_keys:
            if len(j) >= CHAR_PREFIX_MIN and (k.startswith(j) or j.startswith(k)):
                return True
    return False


SECTOR_MAP = {
    "Technology": "Tech", "Communication Services": "Tech", "Telecommunications": "Tech",
    "Financial Services": "Finance", "Finance": "Finance", "Healthcare": "Health",
    "Health Care": "Health", "Consumer Defensive": "Consumer", "Consumer Cyclical": "Consumer",
    "Consumer Discretionary": "Consumer", "Consumer Staples": "Consumer", "Energy": "Energy",
    "Industrials": "Industrial", "Industrial": "Industrial", "Basic Materials": "Materials",
    "Basic Industries": "Materials", "Utilities": "Utilities", "Real Estate": "REIT",
    "Miscellaneous": "—", "Capital Goods": "Industrial", "Consumer Services": "Consumer",
    "Public Utilities": "Utilities", "Transportation": "Industrial",
}


def short_sector(s):
    return SECTOR_MAP.get((s or "").strip(), (s or "—").strip() or "—")


def clean_name(n, t):
    if not n:
        return t
    n = NAME_STRIP.sub("", n)
    n = re.sub(r"[\s,\.]+$", "", n).strip()
    n = re.sub(r"\s{2,}", " ", n)
    return n[:22] if n else t


# ════════════════════════════════════════════════════════════════════════════
# SOURCES NATIVES PAR PLACE
# Chacune retourne [{sym, name, mcap_native, ccy, listed(YYYY-MM-DD|None), sector}]
# ════════════════════════════════════════════════════════════════════════════

# ── Chine continentale : Eastmoney (autorité, donne la date de cotation) ─────
EM_FS_CN = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"   # SZ, ChiNext, SH, STAR, BSE


def src_cn(limit=150):
    u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2"
         "&fid=f20&fs=%s&fields=f12,f13,f14,f20,f26" % (limit, urllib.parse.quote(EM_FS_CN, safe="+:,")))
    d = requests.get(u, headers=UA, timeout=25).json()
    rows = ((d.get("data") or {}).get("diff")) or []
    out = []
    for r in rows:
        code = str(r.get("f12") or "")
        mkt = r.get("f13")                       # 1 = Shanghai, 0 = Shenzhen/Beijing
        mc = r.get("f20")
        if not code or not isinstance(mc, (int, float)) or mc <= 0:
            continue
        sfx = ".SS" if mkt == 1 else ".SZ"
        d26 = str(r.get("f26") or "")
        listed = f"{d26[:4]}-{d26[4:6]}-{d26[6:8]}" if len(d26) == 8 else None
        out.append({"sym": code + sfx, "name": str(r.get("f14") or code),
                    "mcap_native": float(mc), "ccy": "CNY", "listed": listed, "sector": None})
    return out


# ── US : screener Nasdaq (7 000+ titres, capi + secteur + année d'IPO) ───────
# PIÈGE (vérifié le 28/07/2026) : le screener mélange dans le même flux, avec la
# capi de la MAISON MÈRE, des titres qui ne sont pas des actions ordinaires —
# obligation « AT&T 5.350% Global Notes due 2066 » (TBB, 143 Md$), hybride
# « Comcast Holdings ZONES » (CCZ, 234 Md$), lignes de dépôt Alphabet GOOGM/GOOGN
# (587 Md$ chacune, doublons de GOOGL) — et des ADR de sociétés étrangères
# (Sony, SK hynix, MUFG) qui n'ont rien à faire dans la zone US. Sans ces deux
# filtres, la carte à bulles US se remplit de doublons et de dettes.
US_TYPE_OK = re.compile(r"\b(Common Stock|Ordinary Shares|Common Shares)\b", re.I)
# N.B. on ne blackliste QUE des types de titres. Pas de mot générique comme
# « Trust » ou « Fund » : ils figurent dans de vrais noms de sociétés (Northern
# Trust, membre du S&P 100) et les rejetteraient à tort. L'exigence positive
# US_TYPE_OK (« Common Stock »/« Ordinary Shares ») écarte déjà ETF, fonds,
# obligations et actions de préférence, dont le libellé se termine autrement.
US_TYPE_BAD = re.compile(r"American Depositary|Depositary Shares|Depositary Receipt|"
                         r"\bNotes\b|\bZONES\b|Preferred|Warrant|\bDebenture", re.I)
# Domiciles acceptés pour la zone US : États-Unis + Irlande (sociétés américaines
# redomiciliées — Accenture, Seagate, Eaton… déjà présentes dans le pool). Une
# société japonaise ou suisse n'entre PAS dans la zone US : elle sera captée par
# le screener de sa propre place (JPX, EBS…).
US_COUNTRY_OK = {"United States", "Ireland"}


def src_us(limit=150):
    u = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"
    d = requests.get(u, headers=UA, timeout=35).json()
    rows = ((d.get("data") or {}).get("rows")) or []
    out, drop_type, drop_ctry = [], 0, 0
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        name = (r.get("name") or sym).strip()
        try:
            mc = float(r.get("marketCap") or 0)
        except (TypeError, ValueError):
            mc = 0.0
        if not sym or mc <= 0 or "^" in sym or "/" in sym:
            continue
        if US_TYPE_BAD.search(name) or not US_TYPE_OK.search(name):
            drop_type += 1
            continue
        if (r.get("country") or "").strip() not in US_COUNTRY_OK:
            drop_ctry += 1
            continue
        yr = (r.get("ipoyear") or "").strip()
        out.append({"sym": sym, "name": name, "mcap_native": mc, "ccy": "USD",
                    "listed": f"{yr}-01-01" if yr.isdigit() else None,
                    "sector": r.get("sector")})
    out.sort(key=lambda x: x["mcap_native"], reverse=True)
    log(f"  Nasdaq : {len(rows)} lignes → {len(out)} actions ordinaires US "
        f"({drop_type} titres non-actions écartés, {drop_ctry} sociétés étrangères)")
    return out[:limit]


# ── HK / Europe / Inde / Japon : screener Yahoo par place ───────────────────
# Codes place Yahoo VÉRIFIÉS le 28/07/2026 (la Suisse est EBS, PAS SWX/VTX/ZRH
# qui renvoient tous total=0 — piège : un code faux ne lève aucune erreur, il
# rend juste une place silencieusement vide).
YAHOO_EXCHANGES = {
    "hk": [("HKG", "HKD")],
    "in": [("NSI", "INR")],
    "jp": [("JPX", "JPY")],
    "eu": [("PAR", "EUR"), ("AMS", "EUR"), ("GER", "EUR"), ("MIL", "EUR"),
           ("MCE", "EUR"), ("BRU", "EUR"), ("LIS", "EUR"), ("HEL", "EUR"),
           ("VIE", "EUR"), ("EBS", "CHF"), ("LSE", "GBp"), ("STO", "SEK"),
           ("CPH", "DKK"), ("OSL", "NOK"), ("WSE", "PLN")],
}

_YS = {"session": None, "crumb": None}


def yahoo_session():
    """Session curl_cffi + crumb (Yahoo refuse requests stdlib → 429)."""
    if _YS["session"] is not None:
        return _YS["session"], _YS["crumb"]
    if cr is None:
        log("curl_cffi absent → screener Yahoo indisponible (HK/EU/IN/JP ignorés)")
        return None, None
    try:
        s = cr.Session(impersonate="chrome120")
        s.get("https://fc.yahoo.com", timeout=15)
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).text.strip()
        if not crumb or len(crumb) > 32:
            log("crumb Yahoo illisible")
            return None, None
        _YS["session"], _YS["crumb"] = s, crumb
        return s, crumb
    except Exception as e:
        log(f"session Yahoo KO : {type(e).__name__} {e}")
        return None, None


def yahoo_screen(exchange, size=100):
    s, crumb = yahoo_session()
    if s is None:
        return []
    body = {"size": size, "offset": 0, "sortField": "intradaymarketcap", "sortType": "DESC",
            "quoteType": "EQUITY",
            "query": {"operator": "AND", "operands": [
                {"operator": "EQ", "operands": ["exchange", exchange]}]},
            "userId": "", "userIdType": "guid"}
    try:
        r = s.post("https://query1.finance.yahoo.com/v1/finance/screener?crumb=" + crumb,
                   json=body, timeout=30, headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            log(f"  screener {exchange} : HTTP {r.status_code}")
            return []
        return r.json()["finance"]["result"][0].get("quotes") or []
    except Exception as e:
        log(f"  screener {exchange} : {type(e).__name__} {e}")
        return []


# Cotations croisées à écarter d'emblée (Apple sur Xetra/Milan/Vienne, CDR de
# Toronto, lignes IOB…). Le filtre DÉFINITIF reste le pays de domiciliation
# vérifié à la validation, mais écarter tôt évite des requêtes inutiles.
XLIST_PAT = re.compile(r"\bCDR\b|\bADR\b|DEPOSITARY", re.I)


# PIÈGE ÉCARTÉ (28/07/2026) : le champ `region` des cotations du screener n'est PAS
# le pays du siège — il vaut 'US' pour TOUTES les lignes, quelle que soit la place
# (c'est la locale de la requête). S'en servir comme filtre de domicile vidait
# chaque place à 100 % et déclarait les 4 zones « en échec ». Le rattachement à une
# zone se joue donc en deux temps : pré-filtre par NOM (is_known_company, gratuit)
# puis contrôle du pays via .info à la validation, sur les seuls survivants.
def src_yahoo_zone(zone, limit=150):
    out = []
    for exch, exp_ccy in YAHOO_EXCHANGES[zone]:
        quotes = yahoo_screen(exch, size=100)
        kept = 0
        for q in quotes:
            sym = q.get("symbol") or ""
            mc = q.get("marketCap") or 0
            ccy = q.get("currency") or exp_ccy
            name = q.get("longName") or q.get("shortName") or sym
            if not sym or not mc or mc <= 0:
                continue
            if ccy != exp_ccy:            # Toyota coté à Londres en JPY → pas une valeur UK
                continue
            if XLIST_PAT.search(name):
                continue
            if re.match(r"^\d[A-Z]{2,}", sym):   # « 1AAPL.MI » = ligne miroir Milan
                continue
            # Date de première cotation fournie par le screener : évite un appel
            # chart pour trancher IPO / société ancienne.
            ftd = q.get("firstTradeDateMilliseconds")
            listed = None
            if ftd:
                try:
                    listed = datetime.fromtimestamp(ftd / 1000).strftime("%Y-%m-%d")
                except Exception:
                    listed = None
            out.append({"sym": sym, "name": name, "mcap_native": float(mc), "ccy": ccy,
                        "listed": listed, "sector": q.get("sector")})
            kept += 1
        log(f"  {exch}: {len(quotes)} cotations → {kept} retenues")
        time.sleep(0.4)
    return out


ZONE_SOURCE = {"cn": src_cn, "us": src_us,
               "hk": lambda l=150: src_yahoo_zone("hk", l),
               "in": lambda l=150: src_yahoo_zone("in", l),
               "jp": lambda l=150: src_yahoo_zone("jp", l),
               "eu": lambda l=150: src_yahoo_zone("eu", l)}

# Zone du site qui reçoit l'auto-ajout. hk → pool « cn » (le pool chinois du site
# contient déjà les lignes .HK : Tencent, ICBC…). jp → aucune carte à bulles :
# alerte + curation TradFi uniquement.
ZONE_TO_POOL = {"cn": "cn", "hk": "cn", "us": "us", "eu": "eu", "in": "in", "jp": None}

ZONE_LABEL = {"cn": "Chine (A-shares)", "hk": "Hong Kong", "us": "États-Unis",
              "eu": "Europe", "in": "Inde", "jp": "Japon"}

# ── Domicile attendu par zone ───────────────────────────────────────────────
# PIÈGE (vérifié le 28/07/2026) : une place accueille des sociétés ÉTRANGÈRES en
# cotation secondaire. Le screener de Hong Kong remonte Fast Retailing (Uniqlo,
# japonaise, 145 Md$), Prudential (britannique) et Manulife (canadienne) — toutes
# en HKD, donc invisibles au filtre devise. Sans contrôle du pays, elles
# atterrissaient dans la carte à bulles « Chine ». Une société est rattachée à la
# zone de son SIÈGE, pas de la place qui l'affiche.
EU_COUNTRIES = {"Germany", "France", "Netherlands", "Spain", "Italy", "Belgium",
                "Portugal", "Finland", "Austria", "Switzerland", "United Kingdom",
                "Sweden", "Denmark", "Norway", "Poland", "Ireland", "Luxembourg",
                "Jersey", "Guernsey", "Isle of Man", "Greece", "Czechia", "Hungary"}
ZONE_COUNTRIES = {
    "cn": {"China", "Hong Kong", "Macau"},
    "hk": {"China", "Hong Kong", "Macau"},
    "us": {"United States", "Ireland"},
    "in": {"India"},
    "jp": {"Japan"},
    "eu": EU_COUNTRIES,
}

# Le contrôle du domicile ne s'applique QUE là où la place accueille des sociétés
# étrangères. Il est délibérément DÉSACTIVÉ pour :
#   • cn — une action A (Shanghai/Shenzhen) est par construction une cotation du
#     marché chinois : une société étrangère ne peut pas y être cotée. Le filtre
#     y faisait des dégâts : BeiGene (688235.SS, STAR Market) est juridiquement
#     redomiciliée en SUISSE et se faisait éjecter de la carte Chine — alors que
#     son action A est chinoise par nature.
#   • us — déjà filtré à la source sur le champ `country` du screener Nasdaq.
DOMICILE_CHECKED = {"hk", "eu", "in", "jp"}


# ════════════════════════════════════════════════════════════════════════════
# VALIDATION d'un candidat : Yahoo chart meta = autorité (existence, devise,
# prix, DATE DE PREMIÈRE COTATION) + yfinance .info (secteur, domaine, pays).
# ════════════════════════════════════════════════════════════════════════════
def chart_meta(sym):
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         + urllib.parse.quote(sym) + "?range=1mo&interval=1d")
    for k in range(2):
        try:
            r = requests.get(u, headers=UA, timeout=18)
            if r.status_code == 200:
                res = (r.json().get("chart") or {}).get("result")
                if res and res[0].get("meta"):
                    m = res[0]["meta"]
                    return {"ccy": m.get("currency"), "price": m.get("regularMarketPrice"),
                            "exch": m.get("fullExchangeName"),
                            "first": m.get("firstTradeDate")}
        except Exception:
            pass
        time.sleep(1.0 + k)
    return None


def yf_info(sym):
    try:
        import yfinance as yf
        sess = cr.Session(impersonate="chrome120") if cr is not None else None
        t = yf.Ticker(sym, session=sess) if sess is not None else yf.Ticker(sym)
        i = t.info
        if i and (i.get("longName") or i.get("shortName") or i.get("marketCap")):
            return i
    except Exception as e:
        log(f"  .info {sym} : {str(e)[:60]}")
    return None


def domain_from_site(url):
    if not url:
        return None
    d = re.sub(r"^https?://", "", str(url).strip()).split("/")[0]
    return re.sub(r"^www\.", "", d) or None


# ════════════════════════════════════════════════════════════════════════════
# Univers existants du site (ce contre quoi on diffe)
# ════════════════════════════════════════════════════════════════════════════
def load_universe():
    try:
        return json.loads(UNIVERSE.read_text())
    except Exception as e:
        log(f"FATAL univers illisible ({UNIVERSE}) : {e}")
        return None


def tradfi_tickers():
    """Tickers du TradFi Tracker (dict STOCKS écrit en dur) — lecture textuelle :
    on ne veut ni importer le module (effets de bord réseau) ni le parser en AST."""
    try:
        src = TRADFI_SRC.read_text()
        i = src.index("STOCKS = {")
        j = src.index("\n}\n", i)
        return set(re.findall(r'"([A-Za-z0-9\.\-]{1,12})"\s*:\s*\{', src[i:j]))
    except Exception as e:
        log(f"TradFi STOCKS illisible : {e}")
        return set()


def known_keys(uni, tradfi):
    """Index de tout ce que le site connaît déjà : tickers ET sociétés (pour que
    la ligne Shanghai d'une société déjà présente via Hong Kong ne soit pas
    signalée comme « nouveau géant »)."""
    tickers, companies, words, shorts, domains = set(), set(), set(), set(), set()
    for z, blk in (uni or {}).items():
        for r in blk.get("pool", []):
            t = r.get("t", "")
            tickers.add(t)
            tickers.add(t.split(".")[0])
            companies.add(norm_company(r.get("n")))
            words |= word_keys(r.get("n"))
            shorts.add(short_key(r.get("n")))
            if r.get("d"):
                domains.add(str(r["d"]).lower().removeprefix("www."))
    for t in tradfi:
        tickers.add(t)
        tickers.add(t.split(".")[0])
    for s in (companies, words, shorts, domains):
        s.discard("")
    return tickers, companies, words, shorts, domains


# ════════════════════════════════════════════════════════════════════════════
# SANTÉ DES POOLS — le miroir du problème IPO : un ticker qui MEURT
# ────────────────────────────────────────────────────────────────────────────
# Découvert le 28/07/2026 : MMC (Marsh & McLennan) et FI (Fiserv) ne cotaient
# plus — les deux sociétés ont changé de ticker (→ MRSH, → FISV). Or le runtime
# fait du « merge-preserve » : un titre absent du spark garde sa DERNIÈRE valeur
# connue. Un ticker mort ne disparaît donc jamais de la carte, il y reste FIGÉ
# pour toujours, avec un prix et une variation périmés — sans aucun signal.
# On détecte, on confirme sur 2 runs, puis on retire.
# ════════════════════════════════════════════════════════════════════════════
DEAD_STRIKES_REQUIRED = 2
DEAD_ZONE_ABORT_RATIO = 0.20      # >20 % de la zone muette = panne réseau, pas des morts


def audit_pool_health(uni):
    """Retourne {zone: [tickers sans cotation]} — prudent par construction."""
    out = {}
    for z, blk in uni.items():
        tk = [r["t"] for r in blk.get("pool", [])]
        if not tk:
            continue
        got = set()
        for i in range(0, len(tk), 10):
            batch = tk[i:i + 10]
            u = ("https://query1.finance.yahoo.com/v8/finance/spark?symbols="
                 + urllib.parse.quote(",".join(batch)) + "&range=5d&interval=1d")
            try:
                d = requests.get(u, headers=UA, timeout=25).json()
                for s, o in (d or {}).items():
                    if o and [c for c in (o.get("close") or []) if c is not None]:
                        got.add(s)
            except Exception:
                pass
            time.sleep(0.25)
        miss = [t for t in tk if t not in got]
        if len(miss) > max(3, len(tk) * DEAD_ZONE_ABORT_RATIO):
            log(f"  santé {z} : {len(miss)}/{len(tk)} muets → panne réseau suspectée, "
                f"AUCUN retrait (on ne supprime jamais sur un doute)")
            continue
        out[z] = miss
        if miss:
            log(f"  santé {z} : {len(miss)} ticker(s) sans cotation → {', '.join(miss)}")
    return out


def zone_floor(pool_zone):
    """Plancher d'auto-ajout = capi du 100e du Top live de la zone. En dessous,
    ajouter au pool ne changerait rien à l'affichage (le runtime coupe à 100)."""
    try:
        z = json.loads(BUBBLE_CACHE.read_text()).get("zones", {}).get(pool_zone, {})
        st = z.get("stocks") or []
        if len(st) >= 50:
            return max(1.0, float(st[-1].get("mc") or 0))
    except Exception:
        pass
    return ADD_FLOOR_FALLBACK_B


# ════════════════════════════════════════════════════════════════════════════
CLASS_SYM_ROOT = 3          # préfixe de symbole considéré comme « même émetteur »
CLASS_MCAP_TOL = 0.03       # écart de capi toléré entre deux lignes du même émetteur


def collapse_share_classes(cands):
    """Regroupe les LIGNES D'ACTIONS d'un même émetteur en un seul candidat.

    Cas réel (28/07/2026, place suisse) : Lindt & Sprüngli remontait trois fois —
    LISN.SW (« Chocoladefabriken Lindt & Sprüngli »), LISNE.SW (« LINDT N
    2.LINIE ») et LISPE.SW (« LINDT PS 2.LINIE ») — à 127,4 / 127,0 / 126,1 Md$.
    Trois bulles pour une seule entreprise, et aucun filtre par NOM ne pouvait les
    réunir : Yahoo donne tantôt la raison sociale, tantôt la marque.

    Signature retenue, indépendante du libellé : même racine de symbole (3 car.)
    ET capitalisations à moins de 3 % l'une de l'autre — car Yahoo attache à
    chaque ligne la capi de la SOCIÉTÉ, pas celle de la ligne. On conserve la plus
    grosse. Deux sociétés distinctes au symbole voisin (SAN.MC Santander 175 Md$
    vs SAN.PA Sanofi 110 Md$) ne sont pas regroupées : leurs capis divergent."""
    def base_of(c):
        return re.split(r"[.\-]", c["sym"])[0]

    groups = []                                      # [[candidats d'un émetteur]]
    for c in cands:                                  # déjà triés par capi desc
        root = re.sub(r"[^A-Za-z]", "", base_of(c))[:CLASS_SYM_ROOT].upper()
        placed = False
        if root:
            for g in groups:
                gr = re.sub(r"[^A-Za-z]", "", base_of(g[0]))[:CLASS_SYM_ROOT].upper()
                ref = max(g[0]["mcap_usd_b"], 1e-9)
                if gr == root and abs(g[0]["mcap_usd_b"] - c["mcap_usd_b"]) / ref <= CLASS_MCAP_TOL:
                    g.append(c)
                    placed = True
                    break
        if not placed:
            groups.append([c])

    out, dropped = [], []
    for g in groups:
        # Représentant = symbole le plus COURT (la ligne primaire : LISN avant
        # LISNE/LISPE), départage par capi décroissante.
        g_sorted = sorted(g, key=lambda c: (len(base_of(c)), -c["mcap_usd_b"]))
        keep = g_sorted[0]
        out.append(keep)
        dropped.extend((c["sym"], keep["sym"]) for c in g_sorted[1:])
    out.sort(key=lambda x: x["mcap_usd_b"], reverse=True)
    if dropped:
        log("  lignes d'actions regroupées (même émetteur) : "
            + ", ".join(f"{a}→{b}" for a, b in dropped[:8])
            + ("…" if len(dropped) > 8 else ""))
    return out, dropped


# ════════════════════════════════════════════════════════════════════════════
# MÉMOIRE DES REJETS — sans elle, le radar piétine
# ────────────────────────────────────────────────────────────────────────────
# Le budget de validation prend les PLUS GROSSES capis d'abord. Or en Europe les
# 45 premières sont des cotations miroir américaines, rejetées à chaque fois et
# jamais ajoutées au pool : au run suivant, le classement est identique, on
# revalide les mêmes 45, et les 240 candidats suivants ne sont JAMAIS examinés.
# On mémorise donc chaque rejet pour libérer le budget au run d'après.
# Péremption à 90 jours : une société redomiciliée, renommée ou nouvellement
# éligible doit pouvoir être réexaminée — un rejet n'est pas un bannissement.
REJECT_TTL_DAYS = 90


def load_rejects(prev_state):
    out = {}
    for sym, rec in (prev_state.get("rejected") or {}).items():
        try:
            d = datetime.strptime(rec.get("date", ""), "%Y-%m-%d")
        except ValueError:
            continue
        if (datetime.now() - d).days <= REJECT_TTL_DAYS:
            out[sym] = rec
    return out


def scan_zone(zone, fx, tickers, companies, words, rejects=None, limit=150):
    """Retourne (candidats_bruts_inconnus, ok) — ok=False si la place a échoué
    (→ on ne conclura RIEN pour cette place, surtout pas « rien de neuf »)."""
    log(f"— {ZONE_LABEL[zone]} —")
    try:
        rows = ZONE_SOURCE[zone](limit)
    except Exception as e:
        log(f"  source KO : {type(e).__name__} {e}")
        return [], False
    if not rows:
        log("  source vide → place ignorée (aucune conclusion tirée)")
        return [], False

    rejects = rejects or {}
    cands, mirrors, skipped = [], [], 0
    for r in rows:
        f = fx.get(r["ccy"])
        if not f:
            continue
        mc_usd = r["mcap_native"] * f / 1e9
        base = r["sym"].split(".")[0]
        if r["sym"] in tickers or base in tickers:
            continue
        if r["sym"] in rejects:
            skipped += 1                  # déjà tranché lors d'un run précédent
            continue
        if norm_company(r["name"]) in companies:
            continue                      # déjà présent via une autre cotation
        if is_known_company(r["name"], companies, words):
            mirrors.append(r["sym"])      # « Amazon.com Inc » (Xetra) vs « Amazon » (pool US)
            continue
        cands.append({**r, "mcap_usd_b": round(mc_usd, 1), "zone": zone})
    cands.sort(key=lambda x: x["mcap_usd_b"], reverse=True)
    cands, classes = collapse_share_classes(cands)
    log(f"  {len(rows)} titres classés → {len(cands)} inconnus du site"
        + (f" ({len(mirrors)} lignes miroir de sociétés déjà connues écartées : "
           f"{', '.join(mirrors[:6])}{'…' if len(mirrors) > 6 else ''})" if mirrors else "")
        + (f" | {skipped} déjà tranchés lors d'un run précédent" if skipped else ""))
    return cands, True


def validate(c, fx):
    """Enrichit + tranche : IPO ou simple nouvel entrant ? Domicile cohérent ?"""
    meta = chart_meta(c["sym"])
    if not meta or not meta.get("price"):
        log(f"  ✗ {c['sym']} : non validé par Yahoo (ticker inconnu / pas de prix) — écarté")
        return None
    first = meta.get("first")
    listed = None
    if first:
        try:
            listed = datetime.fromtimestamp(first).strftime("%Y-%m-%d")
        except Exception:
            listed = None
    listed = listed or c.get("listed")

    age_days = None
    if listed:
        try:
            age_days = (datetime.now() - datetime.strptime(listed, "%Y-%m-%d")).days
        except ValueError:
            age_days = None

    info = yf_info(c["sym"]) or {}
    country = info.get("country")
    ccy = meta.get("ccy") or c["ccy"]
    f = fx.get(ccy) or fx.get(c["ccy"])
    price_native = meta["price"]

    # Actions en circulation : l'univers stocke `so` car le runtime calcule
    # mcap = so × prix live (aucun appel capi au runtime). Priorité à .info ;
    # à défaut on déduit so = capi_source / prix (exact par construction).
    so = info.get("sharesOutstanding")
    if not so and price_native:
        so = c["mcap_native"] / price_native

    return {**c,
            "listed": listed, "age_days": age_days,
            "is_ipo": bool(age_days is not None and age_days <= IPO_WINDOW_DAYS),
            "ccy": ccy, "price_native": price_native, "exch": meta.get("exch"),
            "country": country,
            "name_clean": clean_name(info.get("longName") or info.get("shortName") or c["name"], c["sym"]),
            "sector": short_sector(info.get("sector") or c.get("sector")),
            "domain": domain_from_site(info.get("website")),
            "so": int(so) if so else None,
            "mcap_usd_b": round(c["mcap_native"] * f / 1e9, 1) if f else c["mcap_usd_b"]}


# ════════════════════════════════════════════════════════════════════════════
# Écriture univers — atomique + backup (jamais d'univers tronqué)
# ════════════════════════════════════════════════════════════════════════════
def write_universe(uni):
    bak = UNIVERSE.with_suffix(f".json.bak_{datetime.now():%Y%m%d_%H%M}")
    try:
        if UNIVERSE.exists() and not bak.exists():
            bak.write_text(UNIVERSE.read_text())
    except Exception as e:
        log(f"backup univers impossible ({e}) — écriture ANNULÉE par précaution")
        return False
    tmp = UNIVERSE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(uni, ensure_ascii=False, indent=1))
    os.replace(tmp, UNIVERSE)
    log(f"univers mis à jour (backup : {bak.name})")
    return True


def bake_logos():
    """Lance fetch_stock_logos.py --only-missing (sous-processus borné).

    Sous-processus et non import : le script de logos a son propre pool de threads
    et écrit des fichiers ; un échec de sa part ne doit pas faire tomber le radar,
    dont le travail (univers + bandeau) est déjà accompli à ce stade."""
    import subprocess
    script = HERE / "fetch_stock_logos.py"
    if not script.exists():
        log(f"  logos : {script.name} introuvable — bake ignoré")
        return
    log("  logos : bake des nouveaux titres…")
    try:
        r = subprocess.run([sys.executable, str(script), "--only-missing"],
                           capture_output=True, text=True, timeout=1800)
        tail = [l for l in (r.stderr or "").strip().splitlines() if l][-3:]
        for l in tail:
            log(f"    {l}")
        if r.returncode != 0:
            log(f"  logos : code retour {r.returncode} — à relancer à la main")
    except subprocess.TimeoutExpired:
        log("  logos : délai dépassé (15 min) — bake interrompu, relance au prochain run")
    except Exception as e:
        log(f"  logos : {type(e).__name__} {e}")


def add_to_pool(uni, pool_zone, v):
    pool = uni[pool_zone]["pool"]
    entry = {"t": v["sym"], "n": v["name_clean"], "s": v["sector"],
             "d": v["domain"], "ccy": v["ccy"],
             "mc0": v["mcap_usd_b"], "so": v["so"]}
    pool.append(entry)
    pool.sort(key=lambda r: (r.get("mc0") or 0), reverse=True)
    if len(pool) > POOL_MAX:
        uni[pool_zone]["pool"] = pool[:POOL_MAX]
    uni[pool_zone]["generated"] = datetime.now().isoformat()
    return entry


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    force = "--force" in argv
    zones = ["cn", "us", "hk", "eu", "in", "jp"]
    for a in argv:
        if a.startswith("--zones"):
            val = a.split("=", 1)[1] if "=" in a else (argv[argv.index(a) + 1] if argv.index(a) + 1 < len(argv) else "")
            zones = [z.strip() for z in val.split(",") if z.strip() in ZONE_TO_POOL]

    if OUT_JSON.exists() and not force and not dry:
        age_h = (datetime.now().timestamp() - OUT_JSON.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            log(f"cache frais ({age_h:.1f} h) — skip")
            return
    if not preflight():
        sys.exit(1)

    uni = load_universe()
    if not uni:
        sys.exit(1)
    tradfi = tradfi_tickers()
    tickers, companies, words, shorts, domains = known_keys(uni, tradfi)
    log(f"univers connu : {len(tickers)} tickers, {len(companies)} sociétés "
        f"({sum(len(uni[z]['pool']) for z in uni)} en pools + {len(tradfi)} TradFi)")

    fx = fetch_fx()
    floors = {z: zone_floor(z) for z in ("us", "cn", "eu", "in")}
    log("planchers d'auto-ajout (capi du 100e) : "
        + ", ".join(f"{z}={v:.0f} Md$" for z, v in floors.items()))

    # ── Santé des pools : retrait des tickers morts (renommage, radiation) ──
    prev_state = {}
    try:
        prev_state = json.loads(OUT_JSON.read_text())
    except Exception:
        pass
    strikes = dict(prev_state.get("dead_strikes") or {})
    removed = []
    log("— santé des pools existants —")
    health = audit_pool_health(uni)
    still_dead = set()
    for z, miss in health.items():
        for t in miss:
            strikes[t] = int(strikes.get(t, 0)) + 1
            still_dead.add(t)
        # un ticker qui recote annule ses strikes (faux positif réseau)
        for t in list(strikes):
            if t not in still_dead and any(r["t"] == t for r in uni.get(z, {}).get("pool", [])):
                strikes.pop(t, None)
    for z in list(uni.keys()):
        if z not in health:
            continue
        kept_pool = []
        for r in uni[z]["pool"]:
            n = int(strikes.get(r["t"], 0))
            if r["t"] in health[z] and n >= DEAD_STRIKES_REQUIRED:
                removed.append({"t": r["t"], "n": r.get("n"), "zone": z})
                log(f"  ⊘ RETIRÉ {z} : {r['t']} ({r.get('n')}) — sans cotation depuis {n} runs")
                strikes.pop(r["t"], None)
            else:
                if r["t"] in health[z]:
                    log(f"  … {r['t']} ({r.get('n')}) muet — strike {n}/{DEAD_STRIKES_REQUIRED}, "
                        f"retrait au prochain run s'il reste muet")
                kept_pool.append(r)
        uni[z]["pool"] = kept_pool

    # Index RECONSTRUIT après les retraits. Sans cela, une société renommée reste
    # invisible : MMC (Marsh & McLennan) est retiré parce qu'il ne cote plus, mais
    # l'index contient encore « MMC » et « marshmcl » → son successeur MRSH est
    # écarté comme doublon, et la société disparaît purement et simplement de la
    # carte. Un renommage doit se traduire par un remplacement, pas une perte.
    if removed:
        tickers, companies, words, shorts, domains = known_keys(uni, tradfi)
        log(f"  index reconstruit après retrait(s) : {len(tickers)} tickers")

    rejects = load_rejects(prev_state)
    if rejects:
        log(f"mémoire des rejets : {len(rejects)} symboles déjà tranchés (péremption "
            f"{REJECT_TTL_DAYS} j)")

    def reject(sym, reason):
        rejects[sym] = {"reason": reason, "date": datetime.now().strftime("%Y-%m-%d")}

    added, alerts, pending, failed = [], [], [], []
    for zone in zones:
        cands, ok = scan_zone(zone, fx, tickers, companies, words, rejects)
        if not ok:
            failed.append(zone)
            continue
        pool_zone = ZONE_TO_POOL[zone]
        floor = floors.get(pool_zone, ADD_FLOOR_FALLBACK_B) if pool_zone else ALERT_FLOOR_USD_B
        # On ne valide que ce qui peut franchir un seuil : soit l'auto-ajout,
        # soit l'alerte IPO. Inutile de requêter Yahoo pour des small caps.
        worth = [c for c in cands if c["mcap_usd_b"] >= min(floor, ALERT_FLOOR_USD_B)]
        if not worth:
            log("  rien au-dessus des seuils")
            continue
        log(f"  {len(worth)} candidat(s) à valider…")
        # Budget de validation par place. Il est BORNÉ (chaque validation coûte 2
        # requêtes Yahoo) mais la troncature est TRACÉE : un plafond silencieux
        # ferait exactement ce qu'on corrige — passer à côté d'une cotation sans
        # que personne ne le sache.
        if len(worth) > VALIDATE_MAX_PER_ZONE:
            log(f"  budget : {VALIDATE_MAX_PER_ZONE} validations sur {len(worth)} candidats "
                f"(les plus grosses capis d'abord ; reliquat au prochain run)")
        t0 = time.time()
        for c in worth[:VALIDATE_MAX_PER_ZONE]:
            if time.time() - t0 > VALIDATE_MAX_SECONDS:
                log(f"  budget temps atteint ({VALIDATE_MAX_SECONDS}s) — place interrompue, "
                    f"reprise au prochain run")
                break
            v = validate(c, fx)
            if not v:
                reject(c["sym"], "non validé par Yahoo (ticker inconnu / pas de prix)")
                continue
            # Un candidat SANS nom vérifiable (Yahoo renvoie le symbole lui-même,
            # ex. « NESR.DE » pour Nestlé sur Xetra) ne peut être dédoublonné par
            # nom : l'ajouter reviendrait à parier. Cas réel rencontré le
            # 28/07/2026 — Nestlé serait entrée une 2e fois dans le pool européen.
            if re.sub(r"[^A-Z0-9]", "", v["name_clean"].upper()) == \
               re.sub(r"[^A-Z0-9]", "", v["sym"].upper()):
                alerts.append(v)
                log(f"  ⚠ nom introuvable chez Yahoo (non ajouté) : {v['sym']} "
                    f"${v['mcap_usd_b']} Md — doublon indétectable, arbitrage requis")
                reject(v["sym"], "nom introuvable — doublon indétectable")
                continue
            # DOMAINE = signature la plus fiable d'une même société. Yahoo renvoie
            # souvent un ACRONYME pour la ligne de Hong Kong (« VGT », « CCTC »)
            # là où le pool porte la raison sociale (« Victory Giant Technology »,
            # « Chaozhou Three-Circle ») : aucun rapprochement par nom n'était
            # possible, et ces deux sociétés se sont retrouvées EN DOUBLE sur la
            # carte Chine le 28/07/2026 — deux bulles, le même logo. Le site web,
            # lui, était identique dans les deux cas.
            cand_dom = str(v["domain"]).lower().removeprefix("www.") if v["domain"] else None
            if cand_dom and cand_dom in domains:
                log(f"  ↷ {v['sym']} : même site que {cand_dom} → doublon d'une "
                    f"société déjà présente")
                reject(v["sym"], f"même domaine qu'une société du pool ({cand_dom})")
                continue
            # Le nom propre remonté par Yahoo peut révéler un doublon que le
            # libellé de la source native masquait.
            if is_known_company(v["name_clean"], companies, words):
                log(f"  ↷ {v['sym']} : {v['name_clean']} déjà connu sous une autre cotation")
                reject(v["sym"], f"doublon de {v['name_clean']}")
                continue
            # Domicile : rejette les cotations secondaires étrangères (Fast
            # Retailing à Hong Kong, Apple sur Xetra) — la société appartient à
            # la zone de son siège, où son propre screener la captera.
            if zone in DOMICILE_CHECKED:
                allowed = ZONE_COUNTRIES.get(zone, set())
                if not v["country"]:
                    alerts.append(v)
                    log(f"  ⚠ domicile inconnu (non ajouté, à trancher) : {v['sym']} {v['name_clean']}")
                    reject(v["sym"], "domicile inconnu")
                    continue
                if v["country"] not in allowed:
                    log(f"  ↷ {v['sym']} : {v['name_clean']} domiciliée « {v['country']} » "
                        f"→ hors zone {ZONE_LABEL[zone]} (cotation secondaire)")
                    reject(v["sym"], f"siège {v['country']} — hors zone {zone}")
                    continue
            # Collision de nom courte = doublon PROBABLE mais pas certain : on
            # n'ajoute pas à l'aveugle, on demande un arbitrage humain.
            suspect = short_key(v["name_clean"]) in shorts
            tag = "IPO" if v["is_ipo"] else "entrant"
            line = (f"{v['sym']:<13} {v['name_clean'][:22]:<24} ${v['mcap_usd_b']:>7,.1f} Md  "
                    f"[{tag}] cotée {v['listed'] or '?'}")
            if suspect:
                v["suspect"] = True          # exclu du bandeau tant que non tranché
                alerts.append(v)
                log(f"  ⚠ DOUBLON POSSIBLE (non ajouté, à trancher) : {line}")
                reject(v["sym"], "doublon possible — arbitrage humain requis")
                pending.append({"sym": v["sym"], "name": v["name_clean"],
                                "mcap_usd_b": v["mcap_usd_b"], "zone": zone,
                                "reason": "doublon possible avec une société déjà présente",
                                "region_suggest": None, "sector_suggest": v["sector"],
                                "domain": v["domain"], "listed": v["listed"],
                                "is_ipo": v["is_ipo"],
                                "detected": datetime.now().strftime("%Y-%m-%d")})
                continue
            if pool_zone and v["mcap_usd_b"] >= floor and v["so"]:
                if not dry:
                    add_to_pool(uni, pool_zone, v)
                added.append(v)
                log(f"  ✓ AJOUTÉ pool {pool_zone} : {line}")
            elif v["is_ipo"] and v["mcap_usd_b"] >= ALERT_FLOOR_USD_B:
                alerts.append(v)
                log(f"  ⚠ ALERTE (sous le plancher pool) : {line}")
            # Univers curé : tout ce qui est gros mérite un examen TradFi
            if v["sym"] not in tradfi and v["mcap_usd_b"] >= max(ALERT_FLOOR_USD_B, 10.0):
                pending.append({"sym": v["sym"], "name": v["name_clean"],
                                "mcap_usd_b": v["mcap_usd_b"], "zone": zone,
                                "region_suggest": {"cn": "China", "hk": "China", "us": "US",
                                                   "eu": "Europe", "in": "India",
                                                   "jp": "Japan"}[zone],
                                "sector_suggest": v["sector"], "domain": v["domain"],
                                "listed": v["listed"], "is_ipo": v["is_ipo"],
                                "detected": datetime.now().strftime("%Y-%m-%d")})
            tickers.add(v["sym"])
            companies.add(norm_company(v["name_clean"]))
            words |= word_keys(v["name_clean"])
            shorts.add(short_key(v["name_clean"]))
            if v.get("domain"):
                domains.add(str(v["domain"]).lower().removeprefix("www."))

    # ── Rattrapage des métadonnées manquantes ───────────────────────────────
    # Yahoo ne connaît ni le secteur ni le site d'une société cotée depuis 48 h :
    # CXMT est entrée avec s="—" et aucun domaine. Sans ce rattrapage, l'étiquette
    # resterait vide POUR TOUJOURS (une entrée n'est enrichie qu'à son ajout) —
    # la bulle serait grise et sans logo alors que l'information existe quelques
    # jours plus tard. On complète au fil des runs, quelques titres à la fois.
    backfilled = 0
    for z, blk in uni.items():
        for r in blk.get("pool", []):
            if backfilled >= META_BACKFILL_PER_RUN:
                break
            if (r.get("s") not in (None, "", "—")) and r.get("d"):
                continue
            info = yf_info(r["t"]) or {}
            sec = short_sector(info.get("sector"))
            dom = domain_from_site(info.get("website"))
            touched = False
            if sec and sec != "—" and r.get("s") in (None, "", "—"):
                r["s"] = sec
                touched = True
            if dom and not r.get("d"):
                r["d"] = dom
                touched = True
            if touched:
                backfilled += 1
                log(f"  métadonnées complétées : {r['t']} → secteur {r.get('s')}, "
                    f"domaine {r.get('d')}")
    if backfilled:
        log(f"  {backfilled} fiche(s) enrichie(s)")

    if (added or removed or backfilled) and not dry:
        write_universe(uni)
    elif (added or removed or backfilled) and dry:
        log(f"[dry-run] {len(added)} ajout(s) / {len(removed)} retrait(s) / "
            f"{backfilled} enrichissement(s) NON écrits")

    # ── Logos des nouveaux titres ───────────────────────────────────────────
    # Le client affiche « logo local ou RIEN » (les 404 de DuckDuckGo renvoient un
    # globe générique qu'un <img> ne peut pas distinguer d'un vrai logo). Un titre
    # ajouté sans passage par le bake reste donc DÉFINITIVEMENT sans logo sur la
    # carte : c'est ce qui est arrivé aux 65 sociétés entrées le 28/07/2026, le
    # bake étant jusque-là une étape manuelle. Il est désormais déclenché ici.
    if added and not dry:
        bake_logos()

    # ── Bandeau Accueil : cotations récentes + entrants majeurs ──────────────
    banner = []
    for v in added + alerts:
        # Un doublon possible non tranché n'est PAS annoncé comme une nouvelle
        # cotation : le bandeau doit rester fiable, sinon on cesse de le lire.
        if v.get("suspect"):
            continue
        if v["is_ipo"] and (v["age_days"] is None or v["age_days"] <= BANNER_DAYS):
            banner.append({"t": v["sym"], "n": v["name_clean"], "mc": v["mcap_usd_b"],
                           "zone": ZONE_LABEL[v["zone"]], "listed": v["listed"],
                           "days": v["age_days"], "sector": v["sector"]})
    banner.sort(key=lambda x: x["mc"], reverse=True)

    prev = {}
    try:
        prev = json.loads(OUT_JSON.read_text())
    except Exception:
        pass
    # Mémoire glissante : une IPO détectée hier reste au bandeau jusqu'à BANNER_DAYS
    # (sinon l'alerte disparaît au run suivant et on rate à nouveau l'événement).
    seen = {b["t"]: b for b in (prev.get("banner") or [])}
    for b in banner:
        seen[b["t"]] = b
    keep = []
    for b in seen.values():
        try:
            d = (datetime.now() - datetime.strptime(b["listed"], "%Y-%m-%d")).days
        except Exception:
            d = 0
        if d <= BANNER_DAYS:
            b["days"] = d
            keep.append(b)
    keep.sort(key=lambda x: x["mc"], reverse=True)

    payload = {"updated": datetime.now().isoformat(),
               "updated_fr": datetime.now().strftime("%d/%m/%Y %H:%M"),
               "banner": keep,
               "added": [{"t": v["sym"], "n": v["name_clean"], "mc": v["mcap_usd_b"],
                          "zone": v["zone"], "listed": v["listed"], "ipo": v["is_ipo"]}
                         for v in added],
               "alerts": [{"t": v["sym"], "n": v["name_clean"], "mc": v["mcap_usd_b"],
                           "zone": v["zone"], "listed": v["listed"]} for v in alerts],
               "removed": removed,
               "rejected": rejects,
               "dead_strikes": strikes,
               "zones_failed": failed}
    if not dry:
        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        OUT_JS.write_text("window.__NEW_LISTINGS__=" +
                          json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
        # Curation TradFi : on ACCUMULE (une suggestion non traitée ne doit pas
        # disparaître silencieusement au run suivant).
        old = []
        try:
            old = json.loads(PENDING.read_text()).get("pending", [])
        except Exception:
            pass
        have = {p["sym"] for p in old}
        old.extend([p for p in pending if p["sym"] not in have])
        old.sort(key=lambda p: p["mcap_usd_b"], reverse=True)
        PENDING.write_text(json.dumps({"updated": datetime.now().isoformat(),
                                       "pending": old}, ensure_ascii=False, indent=1))

    log("═" * 62)
    log(f"RÉSULTAT : {len(added)} ajout(s) univers · {len(removed)} retrait(s) · "
        f"{len(alerts)} alerte(s) · {len(pending)} en attente de curation TradFi · "
        f"{len(keep)} au bandeau")
    if failed:
        log(f"PLACES EN ÉCHEC (aucune conclusion) : {', '.join(failed)}")
    for v in added:
        log(f"  + {v['sym']} {v['name_clean']} ${v['mcap_usd_b']} Md — {ZONE_LABEL[v['zone']]}")


if __name__ == "__main__":
    main()
