#!/usr/bin/env python3
"""Couche TIMEFRAMES des deux Narrative Trackers (crypto + TradFi).

POURQUOI CE SCRIPT EXISTE
Les deux trackers ne connaissaient qu'une seule fenêtre : le score structurel
« Alpha ZEN » (momentum relatif 90 j, breadth 30 j). Impossible de répondre à
« quel narratif tient le mieux DANS LA SÉANCE » ou « qui a pris le leadership
depuis 4 heures ». Les pipelines existants (fetch_narratives.py, fetch_tradfi.py)
sont 100 % journaliers, et leur historique est même dégradé en hebdomadaire
au-delà de 90 jours : ils ne peuvent structurellement pas répondre.

Ce script ajoute une couche INDÉPENDANTE, sans toucher aux deux fetchers
existants (donc sans risque de régression sur le score de référence) : elle
produit, pour chaque narratif/secteur et pour SIX fenêtres, les mêmes métriques
que le score structurel — perf, momentum relatif au benchmark, breadth, signal
LONG/FLAT, durée de régime, score composite — plus les séries d'indice
nécessaires aux vues Rotation et Corrélations.

  Sorties :  narratives_tf_cache.{json,js}   (25 narratifs crypto, benchmark BTC)
             tradfi_tf_cache.{json,js}       (39 secteurs actions, benchmark SPY)

LES FENÊTRES
  1H · 4H   variation glissante ancrée sur les barres horaires
  1J        OUVERTURE → dernier cours (demande explicite) : pour une action, sa
            séance sur SA place de cotation ; pour une crypto, la journée UTC
            (00:00 → maintenant), convention des bougies journalières des exchanges.
  3J · 1S · 30J   même ancrage, comptées en SÉANCES pour les actions (5 = une
            semaine, 21 = un mois) et en JOURS pour la crypto (7, 30) — soit la
            même durée calendaire des deux côtés.
  90J       le score STRUCTUREL existant, repris verbatim des caches principaux.
            Il n'est PAS recalculé ici : c'est la référence, elle ne bouge pas.

SOURCE
Yahoo Finance chart v8 (OHLC), un appel par symbole, 12 threads. Mesuré :
979 symboles en ~12 s en range=6mo. L'endpoint `spark` (lots de 20, bien plus
économe) ne renvoie QUE le close — or « ouverture → clôture » exige l'open.
Deux étages, comme fetch_stock_bubble.py : base profonde par TTL (6 h en horaire,
12 h en journalier) + complément court (range=5d) fusionné à CHAQUE run. Sans ça,
retélécharger 6 mois d'horaire toutes les 15 min ferait 3,4 Go/jour pour rien.

Les rendements sont des RATIOS : aucune conversion de devise (une action de Tokyo
et une de Paris se comparent directement). Seules les pondérations utilisent la
capitalisation, déjà en USD dans les caches.

DEUX PIÈGES TRAITÉS ICI, TOUS DEUX DÉJÀ RENCONTRÉS SUR LE PIPELINE JOURNALIER
1. Grille commune. Construire un indice sur l'union des horodatages de ses
   constituants fait qu'une barre n'agrège qu'un seul titre (leurs horloges ne
   coïncident pas). Test de non-régression : la volatilité annualisée doit
   DIFFÉRER d'un narratif à l'autre ; si tous sortent au même niveau, la
   construction est cassée.
2. Intraday multi-fuseaux — propre à cette couche. À 03:00 UTC, Tokyo cote et
   New York dort. Un forward-fill ferait « participer » les titres américains
   avec un rendement nul, ce qui diluerait mécaniquement le mouvement asiatique.
   Donc : sur les fenêtres intraday, un constituant ne compte dans une barre que
   s'il y a IMPRIMÉ un cours, et les poids sont renormalisés barre par barre sur
   ceux qui cotent réellement. La couverture effective est publiée (n_cov/n_tot)
   pour que la page puisse l'afficher au lieu de la masquer.
"""
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# ── Garde-fou global : un run bloqué sur le réseau ne doit jamais monopoliser
#    le verrou. 20 min suffisent largement (un run nominal dure ~40 s).
import signal as _signal
def _timeout(signum, frame):
    print("[fatal] timeout global (20 min) — abandon pour libérer le verrou.", file=sys.stderr)
    sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _timeout)
    _signal.alarm(20 * 60)
except Exception:
    pass

try:
    from curl_cffi import requests as creq
except ImportError:
    print("[fatal] curl_cffi requis (Yahoo répond 429 à urllib). pip install curl_cffi",
          file=sys.stderr)
    sys.exit(1)

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DESKTOP = Path.home() / "Desktop" / "Site_Crypto_Finance"

NARR_CACHE    = CACHE_DIR / "narratives_cache.json"
TRADFI_CACHE  = CACHE_DIR / "tradfi_cache.json"
YF_SYMBOL_MAP = CACHE_DIR / "yf_crypto_symbol_map.json"   # cg_id -> symbole Yahoo
LOCK_FILE     = CACHE_DIR / "trackers_tf.lock"

DEEP_HOURLY_RANGE = "6mo"    # 876 barres pour une action, 4346 pour une crypto
DEEP_DAILY_RANGE  = "2y"     # de quoi calculer une MM50 sur des barres 1S
DEEP_HOURLY_TTL_H = 6.0
DEEP_DAILY_TTL_H  = 12.0
LIGHT_RANGE       = "5d"
WORKERS           = 12
BAR_RETURN_CAP    = 0.30     # ±30 %/barre, comme l'indice journalier existant

# Fraîcheur exigée d'un constituant pour compter dans une fenêtre INTRADAY.
#
# Mesurée par rapport à la DERNIÈRE BARRE DE L'UNIVERS, jamais par rapport à
# l'horloge murale. La différence est décisive : un samedi, aucun titre n'a coté
# depuis 30 h et un seuil en temps réel viderait entièrement les vues 1H/4H
# (constaté au premier run : couverture 0 %). Ancré sur l'univers, le samedi
# affiche « la dernière heure cotée » de vendredi, ce qui a un sens ; et pendant
# la séance américaine, Tokyo — 14 h de retard — reste écarté, ce qui est le
# comportement voulu : mélanger une heure fraîche et une heure de la veille sous
# l'étiquette « 1H » serait faux.
STALE_TOLERANCE_S   = {"1h": 6 * 3600, "4h": 12 * 3600}
STALE_MAX_DAYS_DAILY = 10   # au-delà : titre suspendu/radié, on l'écarte

# Poids du composite — IDENTIQUES à fetch_tradfi.py, qui applique la formule
# documentée sur LES DEUX pages (55 / 22,5 / 22,5, poids news retiré).
W_REL, W_BREADTH, W_PRICE = 0.55, 0.225, 0.225

# ═════════════════════════════════════════════════════════════════════════
# DÉFINITION DES FENÊTRES
# `n_*` = nombre de barres en arrière pour trouver l'OUVERTURE de la fenêtre.
# `prev` = le cran en dessous dans l'échelle. Il porte les jambes courtes du
# composite — voir ÉCHELLE PROPORTIONNELLE juste en dessous.
# ═════════════════════════════════════════════════════════════════════════
TF_SPEC = [
    {"id": "1h",  "label": "1H",  "grain": "h", "n_crypto": 1,  "n_stock": 1,  "prev": None,
     "desc": "Variation sur la dernière heure cotée (barres horaires)."},
    {"id": "4h",  "label": "4H",  "grain": "h", "n_crypto": 4,  "n_stock": 4,  "prev": "1h",
     "desc": "Variation sur les 4 dernières heures cotées."},
    {"id": "1d",  "label": "1J",  "grain": "d", "n_crypto": 1,  "n_stock": 1,  "prev": "4h",
     "desc": "Ouverture → dernier cours. Action : sa séance sur sa place de cotation. "
             "Crypto : journée UTC en cours (00:00 → maintenant)."},
    {"id": "3d",  "label": "3J",  "grain": "d", "n_crypto": 3,  "n_stock": 3,  "prev": "1d",
     "desc": "Depuis l'ouverture d'il y a 3 séances (actions) / 3 jours UTC (crypto)."},
    {"id": "1w",  "label": "1S",  "grain": "d", "n_crypto": 7,  "n_stock": 5,  "prev": "3d",
     "desc": "Une semaine calendaire : 5 séances (actions) / 7 jours UTC (crypto)."},
    {"id": "30d", "label": "30J", "grain": "d", "n_crypto": 30, "n_stock": 21, "prev": "1w",
     "desc": "Un mois calendaire : 21 séances (actions) / 30 jours UTC (crypto)."},
    {"id": "1y",  "label": "1A",  "grain": "d", "n_crypto": 365, "n_stock": 252, "prev": "30d",
     "desc": "Un an calendaire : 252 séances (actions) / 365 jours UTC (crypto)."},
]
STRUCT_TF = {
    "id": "90d", "label": "90J",
    "desc": "Score structurel Alpha ZEN existant : momentum relatif 90 j (55 %), "
            "breadth 30 j (22,5 %), momentum prix 7 j+30 j (22,5 %). Non recalculé "
            "par cette couche — c'est la référence du site.",
}

# ═════════════════════════════════════════════════════════════════════════
# ÉCHELLE PROPORTIONNELLE DES TROIS JAMBES  (refonte du 2026-08-11)
#
# LE PROBLÈME MESURÉ. La v1 mesurait les trois jambes SUR LA MÊME fenêtre, ce
# qui paraissait la lecture la plus honnête (« score 30J = tout sur 30 jours »).
# C'est en fait ce qui vidait le composite de sa substance : sur la même
# fenêtre, les trois jambes disent la même chose. Corrélations de rang mesurées
# sur les caches réels, avant correctif :
#
#     fenêtre     breadth~rel     prix~rel      score~rel
#     1H → 1A     0,39 → 0,85   0,87 → 1,00   0,97 → 0,99
#     90J struct       0,22          ~0,5          0,87
#
# Un score à 0,97-0,99 de corrélation avec sa PREMIÈRE jambe est un score à une
# seule dimension : les poids 22,5 % / 22,5 % ne décidaient plus rien. Et si le
# 90 J structurel, lui, garde trois dimensions réelles (breadth~rel = 0,22),
# c'est précisément parce qu'il mesure ses jambes sur des horizons DIFFÉRENTS :
# 90 j pour la tendance, 30 j pour l'ampleur, 7 j + 30 j pour le timing.
#
# LA RÈGLE RETENUE. On généralise ce rapport au lieu de le figer sur 90 j :
# chaque jambe descend d'un cran dans l'échelle. Pour une fenêtre W d'indice i :
#
#     tendance (rel)  → W           l'horizon qu'on a choisi de lire
#     ampleur (breadth) → W(i-1)    ≈ W/3, comme 30 j sous 90 j
#     timing (prix)   → 0,5×W(i-1) + 0,5×W(i-2)   comme 7 j + 30 j sous 90 j
#
# La construction est ainsi AUTO-SIMILAIRE : à W = 90 J elle redonne exactement
# 90 / 30 / (7+30), c'est-à-dire le score de référence du site, inchangé. Et à
# toutes les autres fenêtres, les trois jambes retrouvent des horizons distincts
# donc une information distincte (collinéarité retombée à 0,18-0,83 en
# simulation, le niveau du structurel), pour un classement qui ne bouge que
# modérément (ρ 0,78-0,96 avec l'ancien).
#
# LE PLANCHER DE L'ÉCHELLE. La fenêtre 1H n'a aucun cran en dessous, la 4H n'en
# a qu'un. On ne fabrique pas de barres plus fines pour l'occasion : les jambes
# manquantes retombent sur la fenêtre elle-même, et ce repli est PUBLIÉ dans
# `legs` (`floor: true`) pour que la page puisse le dire au lieu de laisser
# croire à trois horizons là où il n'y en a qu'un.
# ═════════════════════════════════════════════════════════════════════════
def leg_windows(idx):
    """Fenêtres portant les trois jambes du composite, pour la fenêtre d'indice
    `idx` dans TF_SPEC. Renvoie (id_rel, id_breadth, id_prix_a, id_prix_b) —
    `id_prix_b` vaut None quand l'échelle n'a qu'un seul cran en dessous."""
    here = TF_SPEC[idx]["id"]
    down1 = TF_SPEC[idx - 1]["id"] if idx >= 1 else None
    down2 = TF_SPEC[idx - 2]["id"] if idx >= 2 else None
    brd = down1 or here                    # plancher : la fenêtre elle-même
    return here, brd, brd, down2


def leg_meta(idx):
    """Description publiable des horizons de chaque jambe — la page ne doit
    jamais avoir à les redéduire, sous peine de réinventer une méthodologie
    hors de la source (piège déjà rencontré sur les seuils du desk)."""
    lab = {t["id"]: t["label"] for t in TF_SPEC}
    here, brd, pa, pb = leg_windows(idx)
    return {
        "rel":     {"tf": here, "label": lab[here], "floor": False},
        "breadth": {"tf": brd,  "label": lab[brd],  "floor": brd == here},
        "price":   {"tf": [pa] + ([pb] if pb else []),
                    "label": lab[pa] + (" + " + lab[pb] if pb else ""),
                    "floor": pb is None},
    }

# Longueur des séries d'indice exportées (barres).
#
# Les quatre fenêtres journalières PARTAGENT la série journalière : l'indice
# d'un narratif est le même objet quelle que soit la fenêtre avec laquelle on le
# mesure — seule la mesure change, pas l'indice. La publier quatre fois
# gonflerait le fichier de 600 Ko pour rien, d'où les alias.
SERIES_CAP = {"1h": 3000, "4h": 900, "1d": 520}
SERIES_ALIAS = {"3d": "1d", "1w": "1d", "30d": "1d", "1y": "1d"}
MA_LEN = 50


# ═════════════════════════════════════════════════════════════════════════
# RÉSEAU
# ═════════════════════════════════════════════════════════════════════════
class NetworkDown(Exception):
    pass


_consec_fail = 0
_fail_lock = threading.Lock()
MAX_CONSEC_FAIL = 25


def _note_fail():
    global _consec_fail
    with _fail_lock:
        _consec_fail += 1
        if _consec_fail >= MAX_CONSEC_FAIL:
            raise NetworkDown(f"{_consec_fail} requêtes KO consécutives")


def _note_ok():
    global _consec_fail
    with _fail_lock:
        _consec_fail = 0


def preflight():
    """Panne DNS : sortir tout de suite plutôt que de grinder 979 timeouts."""
    try:
        socket.getaddrinfo("query1.finance.yahoo.com", 443)
        return True
    except OSError as e:
        print(f"[fatal] DNS injoignable ({e}) — run abandonné, launchd retentera.",
              file=sys.stderr)
        return False


_tls = threading.local()


def _sess():
    if not hasattr(_tls, "s"):
        _tls.s = creq.Session(impersonate="chrome120")
    return _tls.s


def fetch_ohlc(symbol, rng, interval):
    """[(ts, open, close), ...] + méta. ([], None) si échec.

    Une barre sans close est ignorée ; une barre sans open (Yahoo en produit
    parfois) retombe sur son close, ce qui neutralise sa contribution au lieu de
    fabriquer une variation fantôme.
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval={interval}")
    try:
        r = _sess().get(url, timeout=25)
        res = ((r.json().get("chart", {}) or {}).get("result") or [None])[0]
        if not res or not res.get("timestamp"):
            _note_ok()          # réponse valide mais vide ≠ panne réseau
            return [], None
        ts = res["timestamp"]
        q = (res.get("indicators", {}).get("quote", [{}]) or [{}])[0] or {}
        closes = q.get("close") or []
        opens = q.get("open") or []
        meta = res.get("meta", {}) or {}
        out = []
        for i, t in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            if c is None or not (c > 0):
                continue
            o = opens[i] if i < len(opens) else None
            if o is None or not (o > 0):
                o = c
            out.append((int(t), float(o), float(c)))
        _note_ok()
        return out, {"gmtoffset": int(meta.get("gmtoffset") or 0),
                     "tz": meta.get("exchangeTimezoneName") or "UTC"}
    except NetworkDown:
        raise
    except Exception:
        _note_fail()
        return [], None


def fetch_many(symbols, rng, interval, label):
    out, lock, t0 = {}, threading.Lock(), time.time()

    def one(sym):
        bars, meta = fetch_ohlc(sym, rng, interval)
        if bars:
            with lock:
                out[sym] = {"bars": bars, "meta": meta or {"gmtoffset": 0, "tz": "UTC"}}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(one, symbols))
    print(f"[{label}] {len(out)}/{len(symbols)} symboles en {time.time() - t0:.1f}s "
          f"(range={rng}, interval={interval})")
    return out


# ═════════════════════════════════════════════════════════════════════════
# BASES DE BARRES — profond (TTL) + complément court fusionné à chaque run
# ═════════════════════════════════════════════════════════════════════════
def _load_store(path):
    if not path.exists():
        return {}, float("inf")
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    try:
        return json.load(open(path, "r", encoding="utf-8")), age_h
    except Exception:
        return {}, float("inf")


def _write_atomic(path, payload, js_var=None):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if js_var:
            f.write(f"window.{js_var}=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        if js_var:
            f.write(";")
    os.replace(tmp, path)


def normalize_intraday(bars):
    """Fusionne la pseudo-barre « prix courant » dans la barre horaire qu'elle
    prolonge.

    Yahoo ajoute, en fin de série intraday, une barre hors grille horodatée à
    l'instant de la requête (BTC : 21:00, 21:00, puis 21:42:51) — et, en clôture
    de séance actions, une barre à l'heure pile portant le tout dernier print.
    Comme le merge est indexé par horodatage, CHAQUE run en empilait une nouvelle :
    après trois runs, les « 4 dernières heures » de BTC ne couvraient plus que
    9 minutes, et le problème s'aggravait à chaque passage. Un run toutes les
    15 min en aurait ajouté 96 par jour.

    On ne jette pas ces barres — elles portent le cours le plus frais : leur
    clôture est reportée sur la dernière barre alignée, qui reste la seule à
    occuper la grille. Contrôlé sur les 798 titres : chacun n'a qu'un seul
    résidu dominant (9:30 ET → 1800 s, 9:15 IST → 2700 s…), stable au passage
    à l'heure d'été, donc l'alignement se déduit sans ambiguïté de la série.
    """
    if len(bars) < 10:
        return bars
    counts = {}
    for b in bars:
        counts[b[0] % 3600] = counts.get(b[0] % 3600, 0) + 1
    modal = max(counts, key=counts.get)
    if counts[modal] < 0.6 * len(bars):
        return bars          # grille indéterminée : on ne touche à rien
    out = []
    for ts, o, c in bars:
        if ts % 3600 == modal:
            out.append((ts, o, c))
        elif out:
            out[-1] = (out[-1][0], out[-1][1], c)   # le print le plus frais gagne
    return out


def _merge_bars(base, fresh):
    """Le complément écrase les barres qu'il couvre (la barre en cours change à
    chaque run) et laisse le reste intact. Un symbole absent du complément garde
    sa base : une panne partielle ne vide jamais l'historique."""
    for sym, d in fresh.items():
        if sym not in base:
            base[sym] = d
            continue
        by_ts = {b[0]: b for b in base[sym]["bars"]}
        for b in d["bars"]:
            by_ts[b[0]] = b
        base[sym]["bars"] = [by_ts[t] for t in sorted(by_ts)]
        if d.get("meta"):
            base[sym]["meta"] = d["meta"]
    return base


def build_store(symbols, path, deep_range, deep_ttl_h, interval, label):
    store, age_h = _load_store(path)
    missing = [s for s in symbols if s not in store]

    if age_h > deep_ttl_h or len(missing) > len(symbols) * 0.1:
        why = f"TTL dépassé ({age_h:.1f} h)" if age_h > deep_ttl_h \
            else f"{len(missing)} symboles absents"
        print(f"[{label}] rechargement de la base profonde — {why}")
        deep = fetch_many(symbols, deep_range, interval, f"{label}·deep")
        if deep:
            store = _merge_bars(store, deep)
    elif missing:
        print(f"[{label}] complément profond pour {len(missing)} symboles inconnus")
        store = _merge_bars(store, fetch_many(missing, deep_range, interval,
                                              f"{label}·deep+"))

    store = _merge_bars(store, fetch_many(symbols, LIGHT_RANGE, interval, f"{label}·light"))
    keep = set(symbols)
    store = {k: v for k, v in store.items() if k in keep}   # purge des sortants

    # Normalisation APRÈS fusion : elle nettoie aussi les pseudo-barres déjà
    # empilées par les runs précédents, donc le stock se répare tout seul.
    if interval == "1h":
        for d in store.values():
            d["bars"] = normalize_intraday(d["bars"])
    else:
        for d in store.values():          # une seule barre par jour local
            seen, uniq = set(), []
            off = (d.get("meta") or {}).get("gmtoffset", 0)
            for b in reversed(d["bars"]):
                day = (b[0] + off) // 86400
                if day in seen:
                    continue
                seen.add(day)
                uniq.append(b)
            d["bars"] = list(reversed(uniq))

    _write_atomic(path, store)
    return store


# ═════════════════════════════════════════════════════════════════════════
# BARRES DE FENÊTRE
# ═════════════════════════════════════════════════════════════════════════
def group_4h(bars, gmtoffset):
    """Paquets de 4 heures ancrés sur l'OUVERTURE DE LA JOURNÉE LOCALE, pas sur
    un multiple absolu d'epoch : sinon un paquet chevaucherait deux séances et la
    première heure de la journée serait noyée dans la veille. Pour une crypto
    (gmtoffset=0), l'ancrage tombe sur 00:00 UTC — 6 paquets par jour."""
    out, cur_day, chunk = [], None, []

    def flush():
        for i in range(0, len(chunk), 4):
            part = chunk[i:i + 4]
            out.append((part[0][0], part[0][1], part[-1][2]))

    for bar in bars:
        day = (bar[0] + gmtoffset) // 86400
        if day != cur_day:
            flush()
            cur_day, chunk = day, []
        chunk.append(bar)
    flush()
    return out


def group_from_end(bars, k):
    """Ré-échantillonne en paquets de k barres, alignés sur la FIN de la série.

    Aligner sur le début décalerait tout le découpage à chaque nouvelle barre :
    la dernière période — celle qui est en cours et qu'on regarde — changerait de
    contenu d'un run à l'autre. Alignée sur la fin, la période en cours reste la
    période en cours, et seule la plus ancienne est éventuellement tronquée.
    """
    if k <= 1 or len(bars) <= 1:
        return bars
    out = []
    i = len(bars)
    while i > 0:
        j = max(0, i - k)
        chunk = bars[j:i]
        out.append((chunk[0][0], chunk[0][1], chunk[-1][2]))
        i = j
    out.reverse()
    return out


def perf_window(bars, n, grain):
    """Rendement sur la fenêtre, avec DEUX ancrages selon le grain.

    Journalier — « ouverture de la barre n crans en arrière → dernier cours ».
    n=1 donne exactement « de l'ouverture à la clôture » de la séance en cours,
    l'ancrage demandé. Vérifié sur données réelles : la barre journalière Yahoo
    d'une action s'ouvre à l'ouverture de SA place (AAPL 13:30 UTC = 9:30 ET) et
    celle d'une crypto à 00:00 UTC.

    Intraday — « clôture d'il y a n barres → dernier cours ». On n'utilise PAS
    l'ouverture ici : Yahoo publie une barre EN COURS, partielle, dont l'ouverture
    date de quelques minutes seulement. S'ancrer dessus donnait une « variation
    1H » calculée sur 4 minutes, et une breadth à 0 % parce que sur une barre
    toute juste née la plupart des actifs ont close == open. La fenêtre exacte
    réellement mesurée est publiée dans `window`, elle n'est pas approximée.

    None si l'historique ne couvre pas la fenêtre : une fenêtre tronquée donne un
    chiffre faux, pas un chiffre approximatif.
    """
    if n < 1:
        return None
    if grain == "d":
        if len(bars) < n:
            return None
        ref = bars[-n][1]
    else:
        if len(bars) < n + 1:
            return None
        ref = bars[-n - 1][2]
    c = bars[-1][2]
    if not ref or ref <= 0 or not c or c <= 0:
        return None
    return (c / ref - 1.0) * 100.0


def window_bounds(bars, n, grain):
    """Bornes réelles de la fenêtre mesurée, pour affichage vérifiable."""
    if grain == "d":
        if len(bars) < n:
            return None
        return {"start": bars[-n][0], "end": bars[-1][0]}
    if len(bars) < n + 1:
        return None
    return {"start": bars[-n - 1][0], "end": bars[-1][0]}


# ═════════════════════════════════════════════════════════════════════════
# INDICE DE NARRATIF — grille commune, poids renormalisés sur ce qui cote
# ═════════════════════════════════════════════════════════════════════════
def build_index(members, grid, cap=BAR_RETURN_CAP):
    """Indice base 100 pondéré par capitalisation, construit sur les RENDEMENTS
    (comme un S&P 500), sur une GRILLE COMMUNE à tout l'univers.

    members: [(poids, {ts: (open, close)}), ...]

    Un constituant ne pèse dans une barre que s'il y a réellement imprimé un
    cours ; son rendement est mesuré depuis son dernier cours imprimé. Les poids
    sont renormalisés barre par barre sur ceux qui cotent. Conséquence voulue :
    à 03:00 UTC, un secteur mondial bouge au rythme de ses valeurs asiatiques
    seules, sans être dilué par les américaines endormies. Une barre où personne
    ne cote reporte simplement la valeur précédente.
    """
    if not members or len(grid) < 3:
        return []
    last_px = [None] * len(members)
    values = [100.0]
    for gi in range(len(grid)):
        ts = grid[gi]
        num = den = 0.0
        for mi, (w, series) in enumerate(members):
            bar = series.get(ts)
            if bar is None:
                continue
            close = bar[1]
            prev = last_px[mi]
            last_px[mi] = close
            if prev and prev > 0 and gi > 0:
                r = max(-cap, min(cap, close / prev - 1.0))
                num += r * w
                den += w
        if gi == 0:
            continue
        values.append(values[-1] * (1.0 + (num / den if den > 0 else 0.0)))
    return values


def ma_signal(values, breadth, rel=None, ma_len=MA_LEN):
    """Signal LONG/FLAT et durée du régime, transposés à la fenêtre courante :
    LONG si l'indice dépasse sa moyenne mobile 50 barres, que la breadth dépasse
    50 % ET que le narratif bat son benchmark. Compté en barres de la fenêtre
    active (12 barres en 4H = 2 jours de séance).

    LA TROISIÈME CONDITION EST NOUVELLE (2026-08-11) et répare un trou de fond.
    La soustraction du benchmark est un NOMBRE UNIQUE retranché à tout le monde :
    elle ne peut donc changer aucun classement. Mesuré sur les caches réels,
    rang(rel) ≡ rang(perf absolue), ρ = 1,000 sur les 8 fenêtres et les deux
    univers — le benchmark, présenté partout comme « le signal-roi », ne
    déplaçait en réalité pas une seule ligne. En le posant comme CONDITION du
    régime LONG, il décide enfin de quelque chose : un narratif qui monte moins
    vite que son marché n'est plus annoncé « en tendance ». Le score composite,
    lui, n'est pas touché — c'est un choix, pour ne pas déplacer la référence."""
    if len(values) < ma_len + 1:
        return None, f"moins de {ma_len} barres — MM50 impossible", 0
    ma = sum(values[-ma_len:]) / ma_len
    above = values[-1] > ma
    ok_breadth = breadth is not None and breadth > 50.0
    # rel inconnu (benchmark indisponible sur la fenêtre) : on ne bloque pas sur
    # une mesure absente, on retombe sur les deux conditions historiques.
    ok_rel = rel is None or rel > 0
    if above and ok_breadth and ok_rel:
        sig, why = "long", f"indice > MM50, breadth {breadth:.0f} % > 50 % et bat le benchmark"
    elif above and ok_breadth:
        sig, why = "flat", (f"indice > MM50 et breadth {breadth:.0f} % > 50 %, "
                            f"mais sous-performe le benchmark de {abs(rel):.1f} pt")
    elif above:
        sig, why = "flat", f"indice > MM50 mais breadth {breadth:.0f} % ≤ 50 %"
    elif ok_breadth:
        sig, why = "flat", "breadth favorable mais indice < MM50"
    else:
        sig, why = "flat", "indice < MM50 et breadth ≤ 50 %"

    age, ref = 0, None
    for i in range(len(values) - 1, ma_len - 2, -1):
        m = sum(values[i - ma_len + 1:i + 1]) / ma_len
        s = values[i] > m
        if ref is None:
            ref, age = s, 1
        elif s == ref:
            age += 1
        else:
            break
    return sig, why, age


def rank_normalize(values):
    """Rang en percentile → 0..100. Copie conforme du pipeline existant, pour que
    les scores des nouvelles fenêtres restent comparables aux anciens."""
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return [50.0] * len(values)
    valid.sort(key=lambda x: x[1])
    out = [50.0] * len(values)
    n = len(valid)
    for rank, (idx, _) in enumerate(valid):
        out[idx] = round(100.0 * rank / max(1, n - 1), 1) if n > 1 else 50.0
    return out


def annualized_vol(values, bars_per_year):
    """Test de non-régression de la construction d'indice : si TOUS les narratifs
    sortent à la même volatilité, c'est que chaque barre n'agrège qu'un seul
    constituant (piège de la grille commune)."""
    rets = [values[i] / values[i - 1] - 1.0
            for i in range(1, len(values)) if values[i - 1] > 0]
    if len(rets) < 20:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return round(100.0 * (var ** 0.5) * (bars_per_year ** 0.5), 1)


# ═════════════════════════════════════════════════════════════════════════
# TRAITEMENT D'UN UNIVERS
# ═════════════════════════════════════════════════════════════════════════
def asset_key(t, is_crypto):
    sym = (t.get("symbol") or "").strip()
    if not sym:
        return None
    if is_crypto and not t.get("is_stock"):
        return t.get("id") or sym
    return ("$" + sym) if is_crypto else sym


def resolve_symbols(narratives, is_crypto, symmap):
    """{clé interne: symbole Yahoo} pour tous les constituants."""
    out = {}
    for n in narratives:
        for t in n.get("tokens") or []:
            k = asset_key(t, is_crypto)
            if not k:
                continue
            sym = (t.get("symbol") or "").strip()
            if is_crypto and not t.get("is_stock"):
                out[k] = symmap.get(k) or f"{sym.upper()}-USD"
            else:
                out[k] = sym
    return out


BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365, "3d": 122, "1w": 52,
                 "30d": 12, "1y": 1}


def process_universe(name, cache_path, is_crypto, bench_sym, bench_label, stem):
    src = json.load(open(cache_path, "r", encoding="utf-8"))
    narratives = src.get("narratives") or []
    if not narratives:
        raise RuntimeError(f"{cache_path} ne contient aucun narratif")

    symmap = {}
    if YF_SYMBOL_MAP.exists():
        try:
            symmap = json.load(open(YF_SYMBOL_MAP, "r", encoding="utf-8"))
        except Exception:
            pass

    keymap = resolve_symbols(narratives, is_crypto, symmap)
    symbols = sorted(set(keymap.values()) | {bench_sym})
    print(f"\n═══ {name} · {len(narratives)} narratifs · {len(keymap)} constituants "
          f"· {len(symbols)} symboles Yahoo ═══")

    hourly = build_store(symbols, CACHE_DIR / f"tf_bars_hourly_{stem}.json",
                         DEEP_HOURLY_RANGE, DEEP_HOURLY_TTL_H, "1h", f"{name}/1h")
    daily = build_store(symbols, CACHE_DIR / f"tf_bars_daily_{stem}.json",
                        DEEP_DAILY_RANGE, DEEP_DAILY_TTL_H, "1d", f"{name}/1d")

    offsets = {s: (d.get("meta") or {}).get("gmtoffset", 0) for s, d in hourly.items()}
    for s, d in daily.items():
        offsets.setdefault(s, (d.get("meta") or {}).get("gmtoffset", 0))

    # Barres par symbole et par fenêtre, calculées une seule fois.
    bars_cache = {}
    all_syms_pre = set(keymap.values())

    def daily_bars(sym):
        return (daily.get(sym) or {}).get("bars") or []

    # ── Découpage TEMPOREL UNIVERSEL ─────────────────────────────────────
    # Chaque barre de fenêtre doit couvrir le MÊME intervalle pour tous les
    # constituants. Découper la série de chaque titre séparément ne le garantit
    # pas : leurs horloges diffèrent (9:30 à New York, 9:15 à Bombay, 00:00 en
    # crypto), donc les paquets ne coïncident pas et une barre de l'indice
    # n'agrège plus qu'une poignée de titres — c'est le défaut qui avait déjà
    # vidé l'indice sectoriel journalier de sa substance.
    # D'où un découpage défini une fois pour l'univers entier :
    #   · intraday — créneaux absolus de 1 h / 4 h ;
    #   · journalier — jours UTC de l'univers, groupés par paquets de n en
    #     partant du plus récent (la période EN COURS reste la période en cours).
    day_keys = sorted({b[0] // 86400 for s in all_syms_pre for b in daily_bars(s)})
    period_of = {}
    for tf in TF_SPEC:
        if tf["grain"] != "d":
            continue
        n = tf["n_crypto"] if is_crypto else tf["n_stock"]
        m, i = {}, len(day_keys)
        while i > 0:
            j = max(0, i - n)
            for dk in day_keys[j:i]:
                m[dk] = day_keys[j]
            i = j
        period_of[tf["id"]] = m

    def index_bars(sym, tf):
        """Barres du symbole projetées sur le découpage universel de la fenêtre.
        Ce sont elles qui construisent l'indice et portent la MM50."""
        key = (sym, tf["id"])
        if key in bars_cache:
            return bars_cache[key]
        if tf["grain"] == "h":
            raw = (hourly.get(sym) or {}).get("bars") or []
            span = 3600 * (4 if tf["id"] == "4h" else 1)
            slot_of = lambda ts: (ts // span) * span
        else:
            raw = daily_bars(sym)
            pm = period_of[tf["id"]]
            slot_of = lambda ts: pm.get(ts // 86400, ts // 86400) * 86400
        out, cur, o, c = [], None, None, None
        for ts, bo, bc in raw:
            s = slot_of(ts)
            if s != cur:
                if cur is not None:
                    out.append((cur, o, c))
                cur, o = s, bo
            c = bc
        if cur is not None:
            out.append((cur, o, c))
        bars_cache[key] = out
        return out

    def metric_bars(sym, tf):
        """Barres SERVANT LA MESURE : toujours brutes. En 4H, mesurer sur les
        barres regroupées ferait dépendre le résultat de l'endroit où tombe la
        découpe (un paquet de 4 h fraîchement ouvert ne contient qu'une heure)."""
        if tf["grain"] == "h":
            return (hourly.get(sym) or {}).get("bars") or []
        return (daily.get(sym) or {}).get("bars") or []

    # Ancre de fraîcheur : la barre la plus récente de TOUT l'univers, fenêtre
    # par fenêtre. Voir le commentaire de STALE_TOLERANCE_S — l'horloge murale
    # donnerait des vues intraday vides tous les week-ends.
    def cluster_anchor(stamps, tol):
        """Ancre de fraîcheur = fin du GROUPE LE PLUS DENSE de dernières barres.

        Prendre le simple maximum était faux, et ça s'est vu en production : un
        dimanche, seules les bourses du Golfe cotent. Six titres saoudiens
        suffisaient à tirer l'ancre au jour même, périmant les 808 autres —
        couverture 1H tombée de 67 % à 0,7 %. On cherche donc « le moment le plus
        récent où le gros du marché cotait » : on fait glisser une fenêtre de
        largeur `tol` sur les horodatages triés, on garde celle qui contient le
        plus de titres, et l'ancre est son horodatage le plus récent. Une poignée
        de places isolées ne peut plus décider pour tout l'univers.
        """
        if not stamps:
            return 0
        st = sorted(stamps)
        best_n, best_end, j = 0, st[-1], 0
        for i in range(len(st)):
            while st[i] - st[j] > tol:
                j += 1
            if i - j + 1 >= best_n:          # à égalité, on prend le plus récent
                best_n, best_end = i - j + 1, st[i]
        return best_end

    anchor = {}
    for tf in TF_SPEC:
        last = [metric_bars(s, tf)[-1][0] for s in set(keymap.values())
                if metric_bars(s, tf)]
        tol = STALE_TOLERANCE_S.get(tf["id"], STALE_MAX_DAYS_DAILY * 86400)
        anchor[tf["id"]] = cluster_anchor(last, tol)

    def is_fresh(bars, tf):
        """Un titre dont la place est fermée depuis longtemps ne doit pas compter
        dans une fenêtre intraday : on l'écarte, au lieu de le compter à 0 %, ce
        qui écraserait artificiellement la breadth dès qu'un marché ferme."""
        if not bars:
            return False
        age = anchor[tf["id"]] - bars[-1][0]
        if tf["grain"] == "h":
            return age <= STALE_TOLERANCE_S[tf["id"]]
        return age <= STALE_MAX_DAYS_DAILY * 86400

    # ── Benchmark + fenêtre exacte ───────────────────────────────────────
    # `window` est publié pour que la page affiche la fenêtre RÉELLEMENT mesurée
    # (« depuis l'ouverture de vendredi 15:30 ») au lieu d'une étiquette générique :
    # une fenêtre invérifiable est une fenêtre à laquelle on ne peut pas se fier.
    bench_perf, window = {}, {}
    for tf in TF_SPEC:
        n = tf["n_crypto"] if is_crypto else tf["n_stock"]
        b = metric_bars(bench_sym, tf)
        fresh = is_fresh(b, tf)
        bench_perf[tf["id"]] = perf_window(b, n, tf["grain"]) if fresh else None
        w = window_bounds(b, n, tf["grain"]) if fresh else None
        if w and tf["grain"] == "d":
            # Une barre journalière est horodatée à SON OUVERTURE : telle quelle,
            # la fenêtre 1J s'afficherait « 00:00 → 00:00 ». La borne haute est le
            # dernier cours réellement imprimé, que porte la couche horaire.
            w["end"] = max(w["end"], anchor.get("1h", 0))
        window[tf["id"]] = w
        if bench_perf[tf["id"]] is None:
            print(f"[warn] benchmark {bench_sym} indisponible en {tf['label']} "
                  f"(place fermée ou données absentes)", file=sys.stderr)

    result = {tf["id"]: {} for tf in TF_SPEC}
    assets = {tf["id"]: {} for tf in TF_SPEC}
    series = {tid: {"t": [], "v": {}} for tid in SERIES_CAP}
    # Indices conservés fenêtre par fenêtre : le signal LONG/FLAT ne peut être
    # tranché qu'APRÈS l'assemblage des jambes, puisqu'il se confirme sur la
    # jambe breadth — laquelle vit un cran plus bas dans l'échelle.
    index_vals = {tf["id"]: {} for tf in TF_SPEC}
    coverage, vols = {}, {}

    # ── Indice JOURNALIER de chaque narratif, calculé une seule fois ──────
    # Il sert deux fois : c'est la série publiée pour les quatre fenêtres
    # journalières (l'indice d'un narratif est le même objet quelle que soit la
    # fenêtre avec laquelle on le mesure), et c'est le filet de la MM50 pour la
    # fenêtre 30J, trop grossière pour porter 50 barres sur deux ans.
    all_syms = all_syms_pre
    daily_grid = sorted({b[0] for s in all_syms for b in daily_bars(s)})[-SERIES_CAP["1d"]:]
    daily_index = {}
    for narr in narratives:
        mem = []
        for t in narr.get("tokens") or []:
            k = asset_key(t, is_crypto)
            ysym = keymap.get(k) if k else None
            b = daily_bars(ysym) if ysym else []
            if len(b) < 5:
                continue
            mem.append((float(t.get("mcap") or 0) or 1.0, {x[0]: (x[1], x[2]) for x in b}))
        if mem:
            daily_index[narr["narrative"]] = build_index(mem, daily_grid)
    series["1d"]["t"] = daily_grid
    for nm, vv in daily_index.items():
        series["1d"]["v"][nm] = [round(v, 4) for v in vv]
        v = annualized_vol(vv, 252)
        if v is not None:
            vols.setdefault("1d", []).append(v)

    for tf in TF_SPEC:
        tid = tf["id"]
        n_back = tf["n_crypto"] if is_crypto else tf["n_stock"]
        pub_series = tid in SERIES_CAP and tid != "1d"

        # Grille commune à TOUT l'univers pour cette fenêtre : indispensable pour
        # que les séries exportées soient superposables (rotation, corrélations).
        grid = sorted({b[0] for sym in all_syms
                       for b in index_bars(sym, tf)})[-SERIES_CAP.get(tid, 600):]
        if pub_series:
            series[tid]["t"] = grid
        cov_ok = cov_tot = 0

        for narr in narratives:
            members, perfs, weights = [], [], []
            n_tot = n_cov = 0
            for t in narr.get("tokens") or []:
                k = asset_key(t, is_crypto)
                if not k:
                    continue
                n_tot += 1
                ysym = keymap.get(k)
                mb = metric_bars(ysym, tf) if ysym else []
                if not is_fresh(mb, tf):
                    continue
                p = perf_window(mb, n_back, tf["grain"])
                if p is None:
                    continue
                n_cov += 1
                assets[tid][k] = round(p, 2)
                w = float(t.get("mcap") or 0) or 1.0
                members.append((w, {x[0]: (x[1], x[2]) for x in index_bars(ysym, tf)}))
                perfs.append(p)
                weights.append(w)

            cov_ok += n_cov
            cov_tot += n_tot

            if not members:
                result[tid][narr["narrative"]] = {
                    "perf": None, "rel": None, "breadth": None, "px_mom": None,
                    "breadth_own": None,
                    "signal": None, "trend_age": 0, "n_cov": 0, "n_tot": n_tot,
                    "signal_basis": None,
                    "signal_reason": "aucun constituant coté sur cette fenêtre",
                }
                continue

            wsum = sum(weights) or 1.0
            perf_w = sum(p * w for p, w in zip(perfs, weights)) / wsum
            # Ampleur mesurée SUR CETTE fenêtre. Ce n'est pas forcément la jambe
            # « breadth » du composite de cette fenêtre : l'assemblage des jambes
            # se fait plus bas, une fois toutes les fenêtres mesurées, pour que
            # chacune puisse emprunter le cran du dessous (échelle proportionnelle).
            breadth_own = 100.0 * sum(1 for p in perfs if p > 0) / len(perfs)
            rel = (perf_w - bench_perf[tid]) if bench_perf[tid] is not None else None

            vals = build_index(members, grid)
            index_vals[tid][narr["narrative"]] = vals

            result[tid][narr["narrative"]] = {
                "perf": round(perf_w, 3),
                "rel": round(rel, 3) if rel is not None else None,
                "breadth": None,         # rempli plus bas (cran inférieur)
                "breadth_own": round(breadth_own, 1),
                "px_mom": None,          # rempli plus bas (crans inférieurs)
                "signal": None,          # rempli plus bas (dépend de la jambe breadth)
                "signal_reason": "",
                "signal_basis": None,
                "trend_age": 0,
                "n_cov": n_cov,
                "n_tot": n_tot,
            }
            if vals and pub_series:
                series[tid]["v"][narr["narrative"]] = [round(v, 4) for v in vals]
                v = annualized_vol(vals, BARS_PER_YEAR.get(tid, 252))
                if v is not None:
                    vols.setdefault(tid, []).append(v)

        coverage[tid] = {"n_cov": cov_ok, "n_tot": cov_tot,
                         "pct": round(100.0 * cov_ok / cov_tot, 1) if cov_tot else 0.0,
                         "bars": len(grid)}

    # ── Assemblage des trois jambes sur l'échelle proportionnelle ────────────
    # Chaque jambe descend d'un cran (cf ÉCHELLE PROPORTIONNELLE en tête de
    # fichier) : la tendance reste sur la fenêtre lue, l'ampleur et le timing
    # passent aux crans du dessous. C'est ce décalage qui rend les trois jambes
    # informatives ; les mesurer toutes sur la même fenêtre les rendait
    # redondantes (prix~rel montait jusqu'à 1,00).
    for i, tf in enumerate(TF_SPEC):
        tid = tf["id"]
        _, brd_tf, pa_tf, pb_tf = leg_windows(i)
        for nm, d in result[tid].items():
            if d["perf"] is None:
                continue
            # Ampleur : celle du cran inférieur. Absente (le narratif ne cotait
            # pas sur ce cran) → on retombe sur la sienne plutôt que de perdre
            # la jambe, et le repli reste lisible via `breadth_own`.
            b = result[brd_tf].get(nm, {}).get("breadth_own")
            d["breadth"] = b if b is not None else d["breadth_own"]
            # Timing : moyenne des deux crans inférieurs — transposition exacte
            # du 0,5×perf_7j + 0,5×perf_30j sous la fenêtre 90 j d'origine.
            pa = result[pa_tf].get(nm, {}).get("perf")
            pb = result[pb_tf].get(nm, {}).get("perf") if pb_tf else None
            if pa is not None and pb is not None:
                d["px_mom"] = round(0.5 * pa + 0.5 * pb, 3)
            elif pa is not None:
                d["px_mom"] = round(pa, 3)
            else:
                d["px_mom"] = d["perf"]

    # ── Signal LONG/FLAT ─────────────────────────────────────────────────────
    # Tranché maintenant, et pas dans la boucle de mesure : il se confirme sur la
    # jambe breadth, qui n'existe qu'une fois l'assemblage fait. Il exige aussi
    # désormais que le narratif batte son benchmark (cf ma_signal).
    for i, tf in enumerate(TF_SPEC):
        tid = tf["id"]
        for nm, d in result[tid].items():
            if d["perf"] is None:
                continue
            vals = index_vals[tid].get(nm)
            sig, why, age = (ma_signal(vals, d["breadth"], d["rel"]) if vals
                             else (None, "indice indisponible", 0))
            basis = tf["label"]
            if sig is None and tf["grain"] == "d":
                # 50 barres de 30 jours demanderaient quatre ans d'historique.
                # On retombe alors sur la MM50 JOURNALIÈRE — la définition du
                # signal structurel du site — plutôt que de ne rien afficher.
                dv = daily_index.get(nm)
                if dv:
                    sig, why, age = ma_signal(dv, d["breadth"], d["rel"])
                    basis = "jour"
                    why = (why or "") + " (filtre de tendance journalier : la fenêtre " \
                                         "est trop large pour porter 50 barres)"
            d["signal"], d["signal_reason"], d["signal_basis"] = sig, why, basis
            d["trend_age"] = age

    # Score composite et rang, fenêtre par fenêtre.
    #
    # Un narratif sans aucun constituant coté sur la fenêtre reste à None : lui
    # attribuer le 50 que renvoie rank_normalize pour une valeur absente
    # fabriquerait un score « moyen » à partir de rien, et il se glisserait au
    # milieu du classement comme s'il avait été mesuré.
    names = [n["narrative"] for n in narratives]
    for tf in TF_SPEC:
        tid = tf["id"]
        rel_rk = rank_normalize([result[tid][n]["rel"] for n in names])
        br_rk = rank_normalize([result[tid][n]["breadth"] for n in names])
        px_rk = rank_normalize([result[tid][n]["px_mom"] for n in names])
        for i, nm in enumerate(names):
            d = result[tid][nm]
            if d["perf"] is None:
                d["score_rel"] = d["score_breadth"] = d["score_price"] = None
                d["score"] = d["rank"] = None
                continue
            d["score_rel"], d["score_breadth"], d["score_price"] = rel_rk[i], br_rk[i], px_rk[i]
            d["score"] = round(W_REL * rel_rk[i] + W_BREADTH * br_rk[i] + W_PRICE * px_rk[i], 1)
        scored = [n for n in names if result[tid][n]["score"] is not None]
        for i, nm in enumerate(sorted(scored, key=lambda n: result[tid][n]["score"],
                                      reverse=True)):
            result[tid][nm]["rank"] = i + 1

    # Ampleur du marché : STRICTEMENT la mesure de l'historique ci-dessous, pour
    # que l'aiguille de la jauge tombe sur le dernier point de la courbe (piège
    # déjà rencontré sur la jauge journalière).
    breadth_hist = {
        tf["id"]: compute_breadth_history(
            narratives, keymap, metric_bars, tf,
            tf["n_crypto"] if is_crypto else tf["n_stock"], is_crypto, is_fresh)
        for tf in TF_SPEC
    }
    breadth_now = {tid: (h[-1]["breadth"] if h else None) for tid, h in breadth_hist.items()}

    for tid, vv in vols.items():
        if vv:
            print(f"  [vol {tid}] min {min(vv):.0f} % · médiane "
                  f"{sorted(vv)[len(vv) // 2]:.0f} % · max {max(vv):.0f} % "
                  f"({len(vv)} indices)")

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "universe": name,
        "bench_symbol": bench_sym,
        "bench_label": bench_label,
        "bench_perf": bench_perf,
        "window": window,
        # `legs` voyage AVEC la fenêtre : la page ne doit jamais redéduire sur
        # quel horizon chaque jambe a été mesurée, sinon les libellés finissent
        # par décrire une méthodologie qui n'est plus celle du calcul — ce qui
        # était précisément le défaut réparé le 2026-08-11.
        "tf_spec": [{"id": t["id"], "label": t["label"], "desc": t["desc"],
                     "n_back": t["n_crypto"] if is_crypto else t["n_stock"],
                     "grain": t["grain"], "prev": t["prev"],
                     "legs": leg_meta(i)} for i, t in enumerate(TF_SPEC)],
        "struct_tf": dict(STRUCT_TF, legs={
            "rel":     {"tf": "90d", "label": "90J", "floor": False},
            "breadth": {"tf": "30d", "label": "30J", "floor": False},
            "price":   {"tf": ["30d", "1w"], "label": "30J + 1S", "floor": False},
        }),
        "weights": {"rel": W_REL, "breadth": W_BREADTH, "price": W_PRICE},
        "ma_len": MA_LEN,
        "tf": result,
        "assets": assets,
        "series": series,
        "series_alias": SERIES_ALIAS,
        "breadth_now": breadth_now,
        "breadth_history": breadth_hist,
        "coverage": coverage,
        "source": "Yahoo Finance chart v8 (OHLC horaire + journalier)",
    }


def compute_breadth_history(narratives, keymap, metric_bars, tf, n_back, is_crypto,
                            is_fresh, max_pts=140):
    """Ampleur du marché dans le temps : % des constituants en hausse SUR LA
    FENÊTRE, la fenêtre glissant barre après barre.

    Le point délicat, et la raison pour laquelle cette fonction ne se contente
    pas de regarder chaque barre isolément : l'aiguille de la jauge mesure la
    fenêtre entière (30 jours, 4 heures…), donc une courbe qui mesurerait barre
    par barre ne finirait JAMAIS sur l'aiguille. C'est exactement la divergence
    qui avait déjà été corrigée sur la jauge journalière, et que la première
    version de cette couche avait réintroduite : en 1H elle affichait 0 %
    (sur une barre à peine née, close == open pour presque tout le monde) et
    donnait la MÊME valeur en 1J et en 30J (la longueur de fenêtre n'entrait
    nulle part dans le calcul).

    Ici le dernier point est la valeur de la jauge par construction : c'est
    littéralement la même mesure, au même instant.
    """
    per_ts = {}
    seen = set()
    for narr in narratives:
        for t in narr.get("tokens") or []:
            k = asset_key(t, is_crypto)
            if not k or k in seen:
                continue          # un actif peut appartenir à plusieurs narratifs
            seen.add(k)
            ysym = keymap.get(k)
            b = metric_bars(ysym, tf) if ysym else []
            if not is_fresh(b, tf):
                continue
            lo = max(n_back if tf["grain"] == "d" else n_back + 1, len(b) - max_pts)
            for i in range(lo, len(b)):
                ref = b[i - n_back][1] if tf["grain"] == "d" else b[i - n_back - 1][2]
                c = b[i][2]
                if not ref or ref <= 0 or not c or c <= 0:
                    continue
                slot = per_ts.setdefault(b[i][0], [0, 0])
                slot[1] += 1
                if c > ref:
                    slot[0] += 1
    out = []
    for ts in sorted(per_ts)[-max_pts:]:
        up, tot = per_ts[ts]
        if tot >= 5:
            out.append({"t": ts, "breadth": round(100.0 * up / tot, 2), "n": tot})
    return out


def publish(payload, stem, js_var):
    for base in (CACHE_DIR, DESKTOP):
        if not base.exists():
            continue
        _write_atomic(base / f"{stem}.json", payload)
        _write_atomic(base / f"{stem}.js", payload, js_var=js_var)
    size = len(json.dumps(payload, separators=(",", ":"))) / 1e6
    print(f"[out] {stem}.json + .js écrits ({size:.2f} Mo)")


def main():
    if not preflight():
        sys.exit(3)
    if LOCK_FILE.exists() and (time.time() - LOCK_FILE.stat().st_mtime) < 1200:
        print("[skip] un run est déjà en cours (verrou < 20 min).")
        sys.exit(0)
    LOCK_FILE.write_text(str(os.getpid()))

    ok = 0
    try:
        for name, path, is_crypto, bench, blabel, stem, var in (
            ("crypto", NARR_CACHE, True, "BTC-USD", "BTC",
             "narratives_tf_cache", "__NAR_TF__"),
            ("tradfi", TRADFI_CACHE, False, "SPY", "S&P 500",
             "tradfi_tf_cache", "__TRADFI_TF__"),
        ):
            if not path.exists():
                print(f"[warn] {path} absent — univers {name} ignoré", file=sys.stderr)
                continue
            try:
                payload = process_universe(name, path, is_crypto, bench, blabel, stem)
                publish(payload, stem, var)
                ok += 1
                for tf in TF_SPEC:
                    tid, cov = tf["id"], payload["coverage"][tf["id"]]
                    top = sorted(payload["tf"][tid].items(),
                                 key=lambda kv: kv[1].get("rank") or 999)[:3]
                    print(f"  {tf['label']:>4} · couverture {cov['pct']:5.1f} % "
                          f"({cov['n_cov']}/{cov['n_tot']}) · {cov['bars']} barres · top3 : "
                          + ", ".join(f"{k} ({v['score']:.0f})" for k, v in top))
            except NetworkDown as e:
                print(f"[fatal] réseau coupé : {e}", file=sys.stderr)
                break
            except Exception as e:
                import traceback
                print(f"[error] univers {name} : {e}", file=sys.stderr)
                traceback.print_exc()
    finally:
        try:
            LOCK_FILE.unlink()
        except Exception:
            pass

    if ok == 0:
        sys.exit(4)


if __name__ == "__main__":
    main()
