#!/usr/bin/env python3
"""fetch_prediction_markets.py — données de l'onglet « Prediction Markets ».

Suit ce que le marché PENSE vraiment (probabilités implicites, peau dans le jeu —
skin in the game, très talébien) sur une watchlist de thèmes CONFIGURABLES
(prediction_markets_themes.json). Le CLARITY Act n'est qu'un exemple : tout est
piloté par le JSON de config, jamais hardcodé.

Source Phase 1+2 : Polymarket (gratuit, sans clé)
  - Gamma /public-search?q=KEYWORD  → events + markets imbriqués :
       question, outcomePrices, oneDay/Week/MonthPriceChange (Δ natifs), volume,
       liquidity, tags, clobTokenIds, endDate, slug.
  - CLOB  /prices-history            → série temporelle (sparklines + modal détail).

Écrit dans ~/Library/Caches/site_crypto_finance/ (NON protégé TCC, comme les
autres fetchers du site) :
  - prediction_markets_cache.json
  - prediction_markets_cache.js        (window.__PREDICTION_MARKETS__ = {...};)
  - prediction_markets_outcomes.json
  - prediction_markets_outcomes.js     (window.__PM_OUTCOMES__ = {...};)
  - prediction_markets_history.jsonl   (snapshot quotidien append-only, future-proof
                                        pour composite / divergence Phase 4)

Lancé par launchd (scf.predmarkets) plusieurs fois/jour.
  - Prix + Δ : rafraîchis à CHAQUE run (appels search, peu coûteux).
  - Historique CLOB : re-fetché seulement si > BACKFILL_MAX_HOURS (run léger sinon,
    réutilise l'historique du cache précédent → quasi gratuit).

Garde-fous (cf. patterns du repo) :
  - SIGALRM 25 min : auto-tué si bloqué sur I/O réseau → libère le lock launchd.
  - Retry + backoff exponentiel, sleep entre requêtes paginées.
  - Parsing 100% défensif : tout champ API peut être absent/null.
  - Merge-preserve : si le run échoue à récupérer des données, on NE remplace PAS
    un cache valide par du vide (anti-écrasement-à-null, cf. global_markets).

Run: python3 fetch_prediction_markets.py [--force] [--force-history]
"""

# ── Global timeout safeguard (40 min) ──────────────────────────────────────
# Relevé de 25 à 40 min le 2026-07-28 avec l'historique de TOUTES les issues :
# le run quotidien qui refait le backfill enchaîne ~660 appels CLOB de plus
# (~12 min). À 25 min il serait tué AVANT d'écrire quoi que ce soit — on aurait
# remplacé une donnée manquante par un fetcher qui ne finit jamais. Les 5 autres
# runs de la journée réutilisent le cache et durent toujours ~2 min.
_GLOBAL_TIMEOUT_MIN = 55
import os
import signal as _signal, sys as _sys, os as _os, threading as _threading, time as _time

def _global_timeout_handler(signum, frame):
    print(f"[fatal] global timeout ({_GLOBAL_TIMEOUT_MIN} min) — abort pour libérer le lock launchd.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(_GLOBAL_TIMEOUT_MIN * 60)
except Exception:
    pass

# ── Second garde-fou : un FIL, pas seulement un signal ────────────────────────
# Panne observée le 2026-07-28 : le fetcher est resté figé 43 min dans une lecture
# SSL (pile `_ssl__SSLSocket_read` → `PySSL_select`) alors que SIGALRM était bien
# armé à 40 min. Un gestionnaire de signal Python ne s'exécute qu'entre deux
# bytecodes ; tant que l'interpréteur est immobilisé dans du C bloquant, il n'est
# JAMAIS appelé — et le timeout de requests ne rattrape pas ce cas non plus.
# Un fil de veille, lui, tourne : CPython relâche le GIL pendant les entrées-
# sorties bloquantes. Il termine le processus par os._exit(), qui court-circuite
# tout l'interpréteur et ne peut donc pas être avalé à son tour.
# À retenir : dans ce dépôt, une alarme INTERNE ne suffit jamais à garantir
# qu'un fetcher réseau se termine.
# Le fil compare l'horloge MURALE à chaque réveil au lieu de dormir d'un bloc :
# un `sleep` long ne ticque pas pendant la veille macOS, il se réveillerait donc
# bien après l'échéance (piège déjà documenté pour fetch_tradfi). Il ne remplace
# pas le wrapper externe fetch_prediction_markets_with_timeout.sh, il le double.
def _hard_watchdog():
    deadline = _time.time() + (_GLOBAL_TIMEOUT_MIN + 2) * 60
    while _time.time() < deadline:
        _time.sleep(20)
    print(f"[fatal] watchdog : échéance dépassée, processus figé "
          f"(I/O bloquante) — arrêt brutal.", file=_sys.stderr, flush=True)
    _os._exit(2)
_threading.Thread(target=_hard_watchdog, daemon=True).start()

import json, os, socket, sys, time, math, re, threading, unicodedata
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.stderr.write("[PM] requests non installé. pip install requests\n")
    sys.exit(1)

# ── Chemins ────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).resolve().parent
THEMES_FILE = SCRIPT_DIR / "prediction_markets_themes.json"

CACHE_DIR     = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON    = CACHE_DIR / "prediction_markets_cache.json"
CACHE_JS      = CACHE_DIR / "prediction_markets_cache.js"
CACHE_LIGHT   = CACHE_DIR / "prediction_markets_cache_light.json"   # inline de la page (sans historiques)
OUTCOMES_JSON = CACHE_DIR / "prediction_markets_outcomes.json"
OUTCOMES_JS   = CACHE_DIR / "prediction_markets_outcomes.js"
TOKENS_JSON   = CACHE_DIR / "prediction_markets_tokens.json"   # liste des jetons CLOB, lue par /live/pm
HISTORY_JSONL = CACHE_DIR / "prediction_markets_history.jsonl"

# ── Paramètres ──────────────────────────────────────────────────────────────
CACHE_MAX_HOURS    = 4        # fraîcheur cache global (skip si plus récent, sauf --force)
BACKFILL_MAX_HOURS = 20       # re-fetch historique CLOB seulement au-delà
DEFAULT_TOP_N      = 8        # marchés gardés / thème
RESOLVED_THRESHOLD = 0.97     # favori d'un marché à issues EXCLUSIVES au-delà → course jouée, exclu
SETTLED_THRESHOLD  = 0.995    # binaire réellement soldé au carnet (≥99,5% ou ≤0,5%) → exclu.
                              # Volontairement bien plus strict que RESOLVED_THRESHOLD : à 0,97
                              # on retirait des risques extrêmes vivants cotés 2-3 % (invasion de
                              # Taïwan, chute du régime iranien), qui sont l'information même que
                              # cette page suit. Cf. is_resolved.
MIN_INCLUDE_VOL    = 1000     # < ce volume $ → marché mort, exclu totalement (pas de peau dans le jeu)
MIN_VOL_COMPOSITE  = 10000    # < ce volume $ → exclu du composite, affiché « faible liquidité »
MOVER_MIN_VOL      = 5000     # volume mini pour entrer dans les Movers 24h
LIQ_TIER_HIGH      = 250000   # seuils badge fiabilité (volume $)
LIQ_TIER_MID       = 25000
HISTORY_FETCH_CAP  = 480      # plafond d'appels prices-history / run. Relevé de 70
                              # le 2026-07-25 avec l'élargissement de la watchlist
                              # (~64 → ~180 marchés) : sous l'ancien plafond, les
                              # marchés au-delà du 70e n'auraient jamais eu de
                              # sparkline. ~0,3 s/appel → ~1 min, très en deçà du
                              # garde-fou global de 25 min.
                              # 2026-08-03 : 220 → 480 avec le balayage par
                              # liquidité (~370 marchés). Un plafond en dessous du
                              # nombre de marchés ne se voit pas : les derniers
                              # gardent simplement une sparkline vide, en silence.
SEARCH_SLEEP       = 0.25
HISTORY_SLEEP      = 0.30

# ── Historique de TOUTES les issues (pas seulement la favorite) ──────────────
# Un marché à issues multiples ne dit rien d'intéressant si on ne trace que le
# favori : sur « Next French Presidential Election », Bardella est passé de 34 %
# à 3 % et Glucksmann de 43 % à 2 % pendant que Le Pen montait — invisible avec
# une seule courbe. On historise donc chaque issue (1 clob_token = 1 série).
OUTCOME_HISTORY_CAP = 2200    # plafond d'appels prices-history / run pour les issues.
                              # ~770 issues + les seconds appels au pas fin des
                              # marchés courts : à 900 le plafond tombait pile dans
                              # la zone utile et les derniers marchés gardaient un
                              # bloc périmé, en silence. 1400 laisse de la marge.
                              # 2026-08-03 : 2200, avec le palier de volume
                              # `outcome_full_min_volume` qui borne la dépense
                              # (~1 250 issues sur les marchés > 500 k$).
OUTCOME_SLEEP       = 0.20    # un peu plus court que HISTORY_SLEEP : ~660 appels/jour
OUTCOME_MAX_POINTS  = 400     # points max par série après ré-échantillonnage.
                              # 400 et non 300 pour qu'un marché d'UN AN garde le
                              # pas journalier (365 points) au lieu de basculer à
                              # 2 jours ; au-delà de 400 la finesse ne se voit plus
                              # sur 700 px de large.
# Pas de la grille régulière, du plus fin au plus grossier. Le pas est choisi
# d'après l'AMPLITUDE de l'événement : un marché hebdo garde une résolution
# horaire, un marché de 2 ans passe au pas journalier. L'historique n'est jamais
# tronqué (cf. exigence « max d'historique »), seule sa finesse s'adapte.
OUTCOME_STEPS = [3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400]
# fidelity=720 (1 point/12 h) suffit pour un marché de plusieurs mois, mais sur un
# marché hebdomadaire il ne rend que ~16 points : un escalier, pas une courbe. On
# redemande alors au pas horaire — pour ces marchés-là uniquement.
OUTCOME_FINE_FIDELITY = 60      # minutes
OUTCOME_MIN_POINTS    = 60      # en dessous de ce nombre de points…
OUTCOME_SHORT_SPAN_D  = 21      # …et si l'historique couvre moins de 21 jours

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
UA    = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) CapitalAntifragile/1.0"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})


# ── Résolution DNS de secours (détournement FAI) ────────────────────────────
# Panne du 2026-07-16 → 2026-07-25 (9 jours de données figées, en silence) : les
# FAI français détournent le DNS de *.polymarket.com vers le serveur de blocage
# de l'ANJ. Le certificat servi est alors « *.anj.fr » → toutes les requêtes
# meurent en SSLCertVerificationError (hostname mismatch).
#
# On ne désactive JAMAIS la vérification TLS (verify=False accepterait n'importe
# quel intercepteur, ce serait remplacer une panne par un trou de sécurité). On
# demande les VRAIES adresses IP à un résolveur DNS-over-HTTPS public, puis on
# les épingle pour les seuls hôtes Polymarket : le certificat continue d'être
# validé contre le nom d'hôte réel, donc une IP erronée fait échouer la
# connexion au lieu de la falsifier.
#
# Activation PARESSEUSE (antifragile) : tant que le DNS normal fonctionne, rien
# ne change ; le contournement ne s'arme qu'après une erreur de certificat, et
# si le blocage disparaît le fetcher repasse tout seul en direct.
DOH_ENDPOINTS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
PINNED_HOSTS  = ("gamma-api.polymarket.com", "clob.polymarket.com")
_PINNED_IPS   = {}
_ORIG_GETADDRINFO  = socket.getaddrinfo
_dns_bypass_active = False


def _is_ipv4(s):
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


def _doh_resolve(host):
    """Adresses IPv4 réelles d'un hôte via DoH public. [] si aucun résolveur ne répond."""
    for ep in DOH_ENDPOINTS:
        try:
            r = requests.get(ep, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"}, timeout=10)
            if r.status_code != 200:
                continue
            ips = [a.get("data") for a in (r.json().get("Answer") or [])
                   if a.get("type") == 1 and _is_ipv4(a.get("data") or "")]
            if ips:
                return ips
        except Exception:
            continue
    return []


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    ips = _PINNED_IPS.get(host)
    if ips:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
                for ip in ips]
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


def enable_dns_bypass():
    """Épingle les IP réelles Polymarket. True si au moins un hôte a été résolu."""
    global _dns_bypass_active
    if _dns_bypass_active:
        return True
    got = {}
    for h in PINNED_HOSTS:
        ips = _doh_resolve(h)
        if ips:
            got[h] = ips
    if not got:
        sys.stderr.write("[PM] DNS de secours indisponible (résolveurs DoH injoignables).\n")
        return False
    _PINNED_IPS.update(got)
    socket.getaddrinfo = _patched_getaddrinfo
    _dns_bypass_active = True
    sys.stderr.write("[PM] DNS détourné par le FAI → résolution via DoH public : "
                     + ", ".join(f"{h}→{v[0]}" for h, v in got.items())
                     + " (vérification TLS maintenue)\n")
    return True


def _looks_like_dns_hijack(err_text):
    t = (err_text or "").lower()
    return ("certificate_verify_failed" in t or "hostname mismatch" in t
            or "certificate is not valid for" in t)


# ── HTTP avec retry/backoff ─────────────────────────────────────────────────
def _get_with_deadline(url, params, timeout, hard):
    """Requête bornée par une échéance DURE, indépendante du socket.

    Panne du 2026-07-28, reproduite deux fois : après ~20 min de collecte, une
    connexion keep-alive vers clob.polymarket.com se retrouve à demi-fermée et
    la lecture suivante attend INDÉFINIMENT dans `PySSL_select` — le socket est
    en mode bloquant, donc ni le `timeout=` de requests ni SIGALRM ne reprennent
    la main (pile relevée à l'échantillonneur, 43 min de gel, 8 s de CPU).

    On ne fait donc plus confiance au timeout du socket : la requête part dans un
    fil DÉMON et l'appelant abandonne à l'échéance. Le fil resté coincé mourra
    avec le processus ; il ne bloque plus la collecte, qui continue au marché
    suivant. C'est la même leçon que [[project_tradfi_external_watchdog]] :
    dans ce dépôt, un timeout annoncé par une bibliothèque réseau doit être
    doublé d'une borne qu'on contrôle."""
    box = {}
    def run():
        try:
            box["r"] = SESSION.get(url, params=params, timeout=timeout)
        except BaseException as e:      # noqa: BLE001 — remonté tel quel à l'appelant
            box["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(hard)
    if th.is_alive():
        raise TimeoutError(f"socket figé > {hard}s — requête abandonnée")
    if "e" in box:
        raise box["e"]
    return box["r"]


def http_get(url, params=None, tries=4, timeout=20):
    last = None
    for i in range(tries):
        try:
            r = _get_with_deadline(url, params, timeout, timeout + 10)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(0.6 * (2 ** i))
                continue
            last = f"HTTP {r.status_code}"
            break
        except Exception as e:
            last = str(e)
            # Erreur de certificat = signature du détournement DNS : on arme le
            # contournement DoH et on réessaie immédiatement (une seule fois pour
            # tout le run, les appels suivants partent direct sur la bonne IP).
            if _looks_like_dns_hijack(last) and not _dns_bypass_active:
                if enable_dns_bypass():
                    continue
            time.sleep(0.6 * (2 ** i))
    sys.stderr.write(f"  [http] échec {url} ({params}) : {last}\n")
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────
def deaccent(s):
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()

def safe_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None

def parse_json_str(v):
    """outcomePrices / outcomes / clobTokenIds sont des STRINGS JSON chez Polymarket."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []

def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ── Config thèmes ─────────────────────────────────────────────────────────────
def load_themes():
    if THEMES_FILE.exists():
        try:
            cfg = json.loads(THEMES_FILE.read_text())
            themes = cfg.get("themes", [])
            defaults = cfg.get("defaults", {})
            if themes:
                return themes, defaults, cfg.get("sweep", {}), cfg.get("rules", {})
        except Exception as e:
            sys.stderr.write(f"[PM] themes.json illisible ({e}) — fallback défauts intégrés.\n")
    # Fallback minimal si le JSON manque
    return ([
        {"id": "us_crypto_regulation", "label": "Régulation crypto US",
         "keywords": ["CLARITY Act", "crypto market structure bill", "stablecoin bill"],
         "category": "regulation"},
        {"id": "fed_policy", "label": "Politique de la Fed",
         "keywords": ["Fed rate cut", "FOMC decision"], "category": "macro"},
        {"id": "tail_risks", "label": "Risques extrêmes",
         "keywords": ["US recession", "government shutdown"], "category": "tail_risk"},
    ], {}, {}, {})


# ── Parsing d'un event Polymarket → entrée normalisée ──────────────────────────
def parse_event(ev):
    """Transforme un event Gamma en entrée du dashboard. None si inexploitable."""
    if not isinstance(ev, dict):
        return None
    if ev.get("closed") is True or ev.get("active") is False:
        return None

    title = (ev.get("title") or "").strip()
    if not title:
        return None
    slug = ev.get("slug") or ""
    markets_raw = ev.get("markets") or []
    if not markets_raw:
        return None

    sub_markets = []
    for m in markets_raw:
        if not isinstance(m, dict):
            continue
        if m.get("closed") is True or m.get("active") is False:
            continue
        outcomes = parse_json_str(m.get("outcomes"))
        prices   = parse_json_str(m.get("outcomePrices"))
        tokens   = parse_json_str(m.get("clobTokenIds"))
        if not prices:
            continue
        # Probabilité « Yes » : index de l'outcome Yes, sinon premier prix
        yes_idx = 0
        for i, o in enumerate(outcomes):
            if isinstance(o, str) and o.strip().lower() in ("yes", "oui"):
                yes_idx = i
                break
        prob = safe_float(prices[yes_idx]) if yes_idx < len(prices) else safe_float(prices[0])
        if prob is None:
            continue
        label = (m.get("groupItemTitle") or "").strip() or "Oui"
        token = tokens[yes_idx] if (tokens and yes_idx < len(tokens)) else (tokens[0] if tokens else None)
        sub_markets.append({
            "label": label,
            "_desc": (m.get("description") or "").strip(),
            "_end": m.get("endDate") or m.get("endDateIso"),
            "question": (m.get("question") or "").strip(),
            "prob": round(prob, 4),
            "delta_24h": safe_float(m.get("oneDayPriceChange")),
            "delta_7d":  safe_float(m.get("oneWeekPriceChange")),
            "delta_30d": safe_float(m.get("oneMonthPriceChange")),
            "volume":    safe_float(m.get("volume")) or safe_float(m.get("volumeNum")),
            "clob_token": str(token) if token else None,
        })

    if not sub_markets:
        return None

    is_multi = len(sub_markets) > 1
    # Headline = marché en tête (proba la plus haute) pour les events multi-candidats ;
    # pour un binaire Yes/No c'est simplement le marché unique.
    head = max(sub_markets, key=lambda s: (s["prob"] if s["prob"] is not None else -1))

    volume    = safe_float(ev.get("volume"))
    volume24  = safe_float(ev.get("volume24hr"))
    liquidity = safe_float(ev.get("liquidity"))
    tags = []
    for t in (ev.get("tags") or []):
        if isinstance(t, dict) and t.get("label"):
            tags.append(t["label"])
        elif isinstance(t, str):
            tags.append(t)

    vol_for_tier = volume or 0
    tier = "high" if vol_for_tier >= LIQ_TIER_HIGH else ("mid" if vol_for_tier >= LIQ_TIER_MID else "low")

    # Haystack de matching (titre + tags + description), déaccentué. Popé avant export.
    full_desc = (ev.get("description") or "").strip()
    hay = deaccent(title + " " + " ".join(tags) + " " + full_desc[:600])

    # Conditions de résolution — le contrat du pari (ce qui le fait gagner ou
    # perdre, la source qui tranche, l'heure limite). Elles arrivent dans la MÊME
    # réponse que les prix : zéro appel réseau supplémentaire. Stockées sous des
    # clés « _ » car elles partent dans un fichier séparé, pas dans le cache
    # principal qui, lui, est inliné dans la page (cf. write_rules).
    # Une issue n'est retenue que si SON texte diffère de celui de l'événement :
    # sur un marché à 12 candidats, les 12 descriptions sont le même contrat au
    # nom près, les republier douze fois n'apprendrait rien et pèserait douze fois.
    sub_desc = []
    sub_ends = []
    for s in sub_markets:
        d = s.pop("_desc", "")
        se = s.pop("_end", None)
        if se:
            sub_ends.append(str(se))
        if d and d != full_desc:
            sub_desc.append({"l": s["label"], "t": d})

    # ÉCHÉANCE — la plus lointaine des issues ENCORE OUVERTES, pas celle de
    # l'événement. Mesuré le 2026-08-03 : sur « Kharg Island no longer under
    # Iranian control by...? » (69 M$), l'événement annonce le 31/03 alors que
    # ses paliers vivants courent jusqu'au 31/12 — la date de l'événement reste
    # figée sur le premier palier, échu depuis. La page affichait donc une
    # échéance dépassée sur un marché qui cote encore, et le filtre « fini »
    # l'écartait pour de bon. Les issues déjà closes sont ignorées en amont
    # (elles ne sont pas dans sub_markets), donc ce max est bien la date à
    # laquelle le pari cesse réellement d'exister.
    end_date = max(sub_ends) if sub_ends else ev.get("endDate")

    return {
        "id": str(ev.get("id") or slug or title),
        "_hay": hay,
        "_tokens": set(re.findall(r"[a-z0-9]+", hay)),
        "_rules": full_desc,
        "_rules_subs": sub_desc,
        "_rules_src": (ev.get("resolutionSource") or "").strip(),
        "question": title,
        "slug": slug,
        "url": f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com",
        "is_multi": is_multi,
        # negRisk = issues mutuellement exclusives (un seul gagnant). Distingue un
        # marché à candidats d'une échelle de dates, où une issue à 99 % est la
        # normale et non une résolution (cf. is_resolved).
        "neg_risk": bool(ev.get("negRisk") or ev.get("enableNegRisk")),
        "prob": head["prob"],
        "prob_label": head["label"] if is_multi else "Oui",
        "delta_24h": head["delta_24h"],
        "delta_7d":  head["delta_7d"],
        "delta_30d": head["delta_30d"],
        "volume": volume,
        "volume_24h": volume24,
        "liquidity": liquidity,
        "end_date": end_date,
        "tags": tags[:10],
        "source": "polymarket",
        "n_markets": len(sub_markets),
        "liquidity_tier": tier,
        "head_token": head.get("clob_token"),
        "markets": sorted(sub_markets, key=lambda s: -(s["prob"] or 0))[:12],
        "history": [],            # rempli par backfill
        "hist_updated": None,
    }


# ── Matching thème → events ────────────────────────────────────────────────────
def _word_matches(kw_word, tokens):
    """Match mot-à-mot tolérant aux pluriels/stems, sans sur-match des tokens courts.
    Exact pour len<4 (etf, agi) ; préfixe pour len>=4 (default↔defaults)."""
    if kw_word in tokens:
        return True
    if len(kw_word) >= 4:
        for t in tokens:
            if t.startswith(kw_word) or (len(t) >= 4 and kw_word.startswith(t)):
                return True
    return False

def relevance_score(theme, entry):
    """nb de keywords RÉELLEMENT matchés × log10(volume). 0 si exclu ou aucun keyword.
    Règle simple et explicite : un keyword matche si TOUS ses mots significatifs
    (len>2) sont présents dans titre+tags+description. La précision se règle via
    le JSON de config (keywords plus spécifiques + 'exclude'), pas dans le code."""
    hay = entry.get("_hay", "")
    tokens = entry.get("_tokens", set())
    for ex in theme.get("exclude", []):
        ex = deaccent(ex)
        if ex and ex in hay:
            return 0.0
    hits = 0
    for kw in theme.get("keywords", []):
        words = [w for w in deaccent(kw).split() if len(w) > 2]
        if words and all(_word_matches(w, tokens) for w in words):
            hits += 1
    if hits == 0:
        return 0.0  # public-search est flou → exiger un vrai match (anti faux-positifs)
    vol = entry.get("volume") or 1
    return hits * math.log10(max(vol, 10))


def is_resolved(entry, threshold, settled=0.995):
    """Marché « fini » : son issue est déjà tranchée → plus aucune information.

    CORRIGÉ 2026-08-03, deux faux positifs qui coûtaient cher dès qu'on est passé
    de 156 à ~370 marchés :

    1. « Improbable » n'est pas « tranché ». L'ancienne règle retirait tout
       binaire sous 1 - 0,97 = 3 %. Elle a écarté « Will China invade Taiwan by
       September 30, 2026? » (2 %), « Will the Iranian regime fall by September
       30? » (2 %), « Will the U.S. invade Greenland in 2026? » (3 %) — soit
       exactement les risques extrêmes que cette page existe pour suivre. Une
       probabilité de 2 % sur un événement à venir est une INFORMATION, pas une
       résolution. On ne retire donc plus que ce qui est réellement soldé au
       carnet (≥ 99,5 % ou ≤ 0,5 %) ou dont l'échéance est passée.

    2. Un événement à issues multiples NON mutuellement exclusives (échelle de
       dates : « … by August 31 / September 30 / December 31 ») a par
       construction une issue lointaine proche de 100 %. Prendre le favori pour
       juge y déclarait « fini » un marché parfaitement vivant. La règle du
       favori ne vaut donc que pour les marchés à issues EXCLUSIVES (negRisk,
       drapeau de la source) ; ailleurs, il faut que TOUTES les issues soient
       soldées."""
    subs = entry.get("markets") or []
    # Échéance dépassée : plus rien à apprendre, quel que soit le prix affiché.
    end = entry.get("end_date")
    if end:
        try:
            dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            if dt < datetime.now(timezone.utc):
                return True
        except (ValueError, TypeError):
            pass
    p = entry.get("prob")
    if p is None:
        return False
    if not entry.get("is_multi"):
        return p >= settled or p <= (1.0 - settled)
    if entry.get("neg_risk"):
        return p >= threshold                    # issues exclusives : le favori tranche
    return all((s.get("prob") is not None and (s["prob"] >= settled or s["prob"] <= (1.0 - settled)))
               for s in subs) if subs else False


def sweep_events(cfg):
    """Balaye TOUS les événements actifs par volume décroissant (endpoint /events).

    Pourquoi (2026-08-03) : la watchlist de mots-clés ne voit que ce que ses
    mots-clés attrapent. « Netanyahu out by...? » (124 M$), « Brazil Presidential
    Election » (118 M$) ou « Venezuela leader end of 2026? » (95 M$) n'étaient
    dans AUCUN thème — invisibles sur la page alors qu'ils comptent parmi les
    plus gros paris du monde. Le balayage inverse la logique : on part de ce que
    le marché finance réellement, et on ne garde que ce qui franchit un plancher
    de volume ET de liquidité (la demande du user : « tous les marchés avec une
    plutôt bonne liquidité »). ~20 appels pour ~2 000 événements.

    Le plancher porte sur les DEUX grandeurs, jamais sur le seul volume : un
    marché peut avoir brassé des millions il y a six mois et n'avoir plus aucun
    carnet aujourd'hui — sa probabilité affichée ne serait plus qu'un souvenir.
    """
    if not cfg.get("enabled", True):
        return []
    pages = int(cfg.get("pages", 20))
    size = int(cfg.get("page_size", 100))
    out = {}
    for i in range(pages):
        data = http_get(f"{GAMMA}/events", params={
            "closed": "false", "archived": "false", "active": "true",
            "order": "volume", "ascending": "false", "limit": size, "offset": i * size})
        if not isinstance(data, list) or not data:
            break
        for ev in data:
            if isinstance(ev, dict) and ev.get("id") is not None:
                out[str(ev["id"])] = ev
        time.sleep(SEARCH_SLEEP)
        if len(data) < size:
            break
    sys.stderr.write(f"[PM] balayage : {len(out)} événements actifs récupérés (tri volume)\n")
    return list(out.values())


def prepare_routes(cfg):
    """(routes, tags exclus) déaccentués une fois pour toutes (2 000 × 13 comparaisons)."""
    routes = [(r.get("theme_id"), {deaccent(t) for t in (r.get("tags") or [])})
              for r in (cfg.get("routes") or [])]
    excl = {deaccent(t) for t in (cfg.get("exclude_tags") or [])}
    return routes, excl


def route_sweep_entry(entry, routes, excl_tags):
    """tag Polymarket → id de thème. None = hors sujet (écarté).

    La PREMIÈRE route qui matche gagne : l'ordre du JSON est significatif (Iran
    avant géopolitique, élections nommées avant géopolitique, « politics »
    générique en dernier). Un événement qui ne matche aucune route est écarté —
    le routage sert donc aussi de filtre « sujet sérieux » : ce qui n'appartient
    à aucun domaine suivi par le site n'entre pas."""
    tags = {deaccent(t) for t in (entry.get("tags") or [])}
    if tags & excl_tags:
        return None
    for theme_id, rtags in routes:
        if tags & rtags:
            return theme_id
    return None


def collect_for_theme(theme):
    """1 appel public-search / keyword, dédup events par id."""
    seen = {}
    for kw in theme.get("keywords", []):
        data = http_get(f"{GAMMA}/public-search", params={"q": kw, "limit_per_type": 12, "events_status": "active"})
        time.sleep(SEARCH_SLEEP)
        events = (data or {}).get("events", []) if isinstance(data, dict) else []
        for ev in events:
            entry = parse_event(ev)
            if not entry:
                continue
            if entry["id"] not in seen:
                seen[entry["id"]] = entry
    return list(seen.values())


# ── Backfill historique CLOB ───────────────────────────────────────────────────
def fetch_history(token, sleep_s=None, fidelity=720):
    """Série [[t_seconds, prob], ...] downsamplée (fidelity 720 ≈ 1 point/12h)."""
    if not token:
        return []
    data = http_get(f"{CLOB}/prices-history", params={"market": token, "interval": "max", "fidelity": fidelity})
    time.sleep(HISTORY_SLEEP if sleep_s is None else sleep_s)
    pts = (data or {}).get("history", []) if isinstance(data, dict) else []
    out = []
    for p in pts:
        t = p.get("t"); v = safe_float(p.get("p"))
        if t is None or v is None:
            continue
        out.append([int(t), round(v, 4)])
    return out


# ── Historique de toutes les issues : grille commune + encodage compact ───────
def pick_grid(series_raw):
    """(t0, step, n) : grille régulière commune à toutes les issues d'un événement.

    Une grille PARTAGÉE est ce qui rend le graphique lisible : à un même index
    correspond un même instant pour toutes les courbes, donc l'infobulle peut
    classer les 12 candidats à une date donnée. Les séries brutes, elles, ne sont
    pas alignées (chaque issue est cotée à sa propre seconde et les issues créées
    plus tard commencent plus tard)."""
    firsts = [s[0][0] for s in series_raw if s]
    lasts  = [s[-1][0] for s in series_raw if s]
    if not firsts:
        return None
    tmin, tmax = min(firsts), max(lasts)
    span = max(tmax - tmin, 3600)
    step = next((s for s in OUTCOME_STEPS if span / s <= OUTCOME_MAX_POINTS), OUTCOME_STEPS[-1])
    t0 = (tmin // step) * step
    n = int((tmax - t0) // step) + 1
    return t0, step, max(2, min(n, OUTCOME_MAX_POINTS + 4))


def resample(raw, t0, step, n):
    """Série brute → n valeurs entières en pour mille sur la grille (t0, step).

    Escalier (dernière cotation connue), et None AVANT la première cotation :
    une issue ajoutée en cours de route ne doit pas être tracée à plat depuis
    l'origine, ce serait inventer une probabilité qui n'a jamais été cotée."""
    v = [None] * n
    for t, p in raw:
        k = int((t - t0) // step)
        if 0 <= k < n:
            v[k] = int(round(p * 1000))
    last = None
    for k in range(n):
        if v[k] is None:
            v[k] = last          # None tant que last est None → trou honnête avant cotation
        else:
            last = v[k]
    return v


def needs_finer(raw):
    """Historique trop grossier pour un marché court (cf. OUTCOME_FINE_FIDELITY)."""
    if not raw or len(raw) >= OUTCOME_MIN_POINTS:
        return False
    return (raw[-1][0] - raw[0][0]) <= OUTCOME_SHORT_SPAN_D * 86400


def build_outcome_block(entry, raw_by_label, now_s=None):
    """Bloc compact d'un événement : {t0, st, n, ns, s:[{l, v:[‰]}]}.

    ~10 Ko pour 12 candidats sur 256 jours, contre ~118 Ko en [[t, prob]] brut :
    la grille régulière supprime les horodatages (reconstruits en JS à partir de
    t0 + k*st) et les probabilités tiennent en un entier de 3 chiffres.

    ANCRAGE SUR LE PRIX LIVE (2026-07-28) : le point final de chaque courbe est
    le prix Gamma, celui-là même qu'affiche la page. Mesuré sur « WTI ↓ $75 » :
    Gamma cotait 41,5 % (carnet 41–42, dernier échange 42, +27 pts en 24 h) alors
    que le dernier point de prices-history disait encore 14,5 % — et 17 % même en
    demandant le pas de 10 minutes. L'endpoint d'historique RETARDE sur les
    marchés qui bougent vite. Sans cet ancrage, le graphique se terminerait à
    14 % sous un titre annonçant 41 % : deux chiffres contradictoires au même
    écran. Le point ajouté n'est pas inventé, c'est la cotation du moment issue
    de la même source que le reste de la page."""
    order = [m.get("label") for m in (entry.get("markets") or [])]
    live  = {m.get("label"): m.get("prob") for m in (entry.get("markets") or [])}
    series_raw = []
    for lab in order:
        raw = raw_by_label.get(lab) or []
        p = live.get(lab)
        if raw and now_s and p is not None and now_s > raw[-1][0]:
            raw = raw + [[int(now_s), float(p)]]
        series_raw.append(raw)
    grid = pick_grid(series_raw)
    if not grid:
        return None
    t0, step, n = grid
    s = []
    for lab, raw in zip(order, series_raw):
        if not raw:
            continue
        s.append({"l": lab, "v": resample(raw, t0, step, n)})
    if len(s) < 2:
        return None      # une seule courbe = rien de plus que le graphique existant
    # ns = nombre d'issues du marché AU MOMENT DE LA CONSTRUCTION, distinct de
    # len(s) : une issue sans historique CLOB ne donne pas de courbe. Sans ce
    # champ, la fraîcheur se testerait sur len(s) != nb d'issues et un marché
    # dont une seule issue n'a jamais coté serait re-téléchargé à CHAQUE run.
    blk = {"t0": t0, "st": step, "n": n, "ns": len(order), "s": s}
    # miss = l'issue en tête n'a AUCUN historique chez CLOB, on vient de le
    # vérifier. Sans cette trace, la règle de fraîcheur « le favori doit avoir sa
    # courbe » relancerait la collecte des 12 issues à chaque run, pour rien.
    head_lab = entry.get("prob_label")
    if head_lab and not any(x["l"] == head_lab for x in s):
        blk["miss"] = head_lab
    return blk


def block_is_fresh(pb, pts, n_subs, head_lab, now_s):
    """Peut-on réutiliser le bloc d'historique d'un événement sans rien refetcher ?

    Trois conditions, chacune payée par un bug :
      - âge < BACKFILL_MAX_HOURS (la collecte des issues coûte ~900 appels CLOB,
        elle ne se fait qu'une fois par jour) ;
      - même nombre d'issues qu'aujourd'hui ;
      - le bloc contient la courbe de l'issue EN TÊTE. C'est elle que la page
        imprime en grand chiffre, et un graphique sans la courbe du chiffre
        affiché contredit sa propre page. Le compte d'issues ne suffit pas :
        sur une échelle de prix (« ↑ $80 / ↓ $75 … ») le favori change
        d'identité sans que leur nombre bouge (constaté le 2026-08-04 sur WTI
        août et Bitcoin août, dont la favorite n'avait aucune courbe).
        `miss` = on a déjà constaté que la source ne publie pas cet historique,
        inutile de rejouer 12 appels à chaque run.
    """
    if not pb or not pts:
        return False
    if (now_s - pts) / 3600 >= BACKFILL_MAX_HOURS:
        return False
    if pb.get("ns", len(pb.get("s") or [])) != n_subs:
        return False
    if head_lab:
        have = {s.get("l") for s in (pb.get("s") or [])}
        if head_lab not in have and pb.get("miss") != head_lab:
            return False
    return True


def load_prev_outcomes():
    if OUTCOMES_JSON.exists():
        try:
            data = json.loads(OUTCOMES_JSON.read_text())
            return data.get("events") or {}, data.get("event_ts") or {}
        except Exception:
            return {}, {}
    return {}, {}


def write_outcomes(events, event_ts, now_s, n_fetched, n_entries, quiet=False):
    """Écrit prediction_markets_outcomes.{json,js}.

    Fichier SÉPARÉ du cache principal, à dessein : le .Rmd inline le cache
    principal dans le HTML (pattern cache-first du site), et y verser ~800
    séries ferait grossir la page de plusieurs centaines de Ko pour une donnée
    qui ne sert qu'à l'ouverture d'un drawer. Ici c'est un <script> à part,
    chargé en différé ; si le fichier manque, la page retombe proprement sur le
    graphique à une seule courbe.

    Collecte vide → on NE réécrit RIEN (même logique de merge-preserve que
    mark_cache_stale : mieux vaut un fichier daté qu'un fichier vide)."""
    if not events:
        sys.stderr.write("[PM] issues : aucun bloc produit — fichier existant conservé.\n")
        return
    payload = {
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "updated_ts": now_s,
        "source": "Polymarket (CLOB prices-history — toutes les issues)",
        "notes": "v = probabilité en pour mille ; instant du point k = t0 + k*st (secondes UTC). null = issue pas encore cotée.",
        "n_events": len(events),
        "n_series": sum(len(b.get("s") or []) for b in events.values()),
        "events": events,
        "event_ts": event_ts,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUTCOMES_JSON.write_text(body)
    OUTCOMES_JS.write_text("window.__PM_OUTCOMES__=" + body + ";")
    if quiet:
        return
    sys.stderr.write(
        f"[PM] issues : {payload['n_series']} séries sur {payload['n_events']}/{n_entries} marchés "
        f"({n_fetched} fetch CLOB, {round(len(body)/1024)} Ko)\n")


# ── Conditions de résolution ───────────────────────────────────────────────────
# « Il manque les détails, ce qui valide ou pas le pari » (demande user
# 2026-08-03). Sur beaucoup de marchés, le titre ne suffit pas : « Government
# shutdown by October 1? » se règle sur une définition précise (lapse in
# appropriations constatée à 23 h 59 ET, agences suspendant des opérations non
# exceptées), et « Iran leader end of 2026? » sur qui « détient effectivement le
# pouvoir » selon des sources nommées. Deux paris au titre presque identique
# peuvent se régler différemment ; sans le contrat, la probabilité affichée est
# un chiffre sans énoncé.
#
# Le texte est publié VERBATIM : c'est lui qui fait foi, et le paraphraser
# reviendrait à réécrire un contrat. Le résumé français ne s'y substitue jamais,
# il s'y ajoute (cf. write_rules / summarize_missing).
CLAUDE_BIN_CANDIDATES = (Path.home() / ".local" / "bin" / "claude", Path("/usr/local/bin/claude"))
RULES_JSON = CACHE_DIR / "prediction_markets_rules.json"
RULES_JS   = CACHE_DIR / "prediction_markets_rules.js"
RULES_AI   = CACHE_DIR / "prediction_markets_rules_ai.json"   # empreinte → résumé (persistant)


def rules_hash(text):
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def load_ai_summaries():
    if RULES_AI.exists():
        try:
            return json.loads(RULES_AI.read_text())
        except Exception:
            return {}
    return {}


def claude_bin():
    for p in CLAUDE_BIN_CANDIDATES:
        if p.exists():
            return str(p)
    import shutil
    return shutil.which("claude")


SUM_PROMPT = """Tu résumes les CONDITIONS DE RÉSOLUTION de marchés de prédiction Polymarket pour un lecteur francophone non spécialiste.

Pour chaque marché numéroté ci-dessous, écris 2 à 3 phrases courtes en français qui disent, dans cet ordre :
1. ce qui fait gagner le pari (« Oui » / l'issue) — le critère exact, chiffré s'il est chiffré ;
2. ce qui le fait perdre, ou le piège à connaître (délai, définition restrictive, cas ambigus explicitement tranchés) ;
3. qui tranche et à quelle date limite, si le texte le précise.

Règles absolues :
- N'invente RIEN. Si le texte ne précise pas un point, ne le mentionne pas.
- Ne recopie pas le titre, ne commente pas la probabilité, pas de formule d'introduction.
- Reste factuel et court (55 mots maximum par marché).

Réponds UNIQUEMENT par un objet JSON de la forme {"1": "…", "2": "…"} avec un texte par numéro, sans bloc de code.

MARCHÉS :
"""


def summarize_batch(items, model, timeout_s):
    """items = [(clé, titre, texte)] → {clé: résumé FR}. {} si l'IA locale échoue.

    L'IA tourne via le CLI Claude (ABONNEMENT local), jamais via l'API : le
    compte API n'a pas de crédit et, surtout, aucune donnée du site ne doit
    dépendre d'une facturation à l'appel. `ANTHROPIC_API_KEY` est retiré de
    l'environnement, sinon le CLI bascule sur l'API payante.
    Échec = pas de résumé, jamais un résumé inventé : la page affiche alors le
    texte d'origine seul, ce qui reste correct."""
    import subprocess
    exe = claude_bin()
    if not exe or not items:
        return {}
    body = SUM_PROMPT + "\n\n".join(
        f"### {i+1}. {t}\n{txt}" for i, (_k, t, txt) in enumerate(items))
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([exe, "-p", "--model", model, "--output-format", "json"],
                           input=body, capture_output=True, text=True,
                           timeout=timeout_s, env=env)
    except Exception as e:
        sys.stderr.write(f"  [rules] CLI Claude indisponible ({e})\n")
        return {}
    if p.returncode != 0:
        sys.stderr.write(f"  [rules] CLI Claude code {p.returncode} : {(p.stderr or '')[:200]}\n")
        return {}
    try:
        env_out = json.loads(p.stdout)
        if env_out.get("is_error"):
            sys.stderr.write(f"  [rules] réponse en erreur : {str(env_out.get('result'))[:160]}\n")
            return {}
        raw = env_out.get("result") or ""
    except Exception:
        raw = p.stdout
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for i, (k, _t, _x) in enumerate(items):
        v = parsed.get(str(i + 1)) or parsed.get(i + 1)
        if isinstance(v, str) and v.strip():
            out[k] = " ".join(v.split())
    return out


def summarize_missing(blocks, cfg):
    """Complète les résumés manquants, par lots, sous budget de temps.

    Deux bornes, parce qu'un run doit rester prévisible : `per_run` (nombre de
    marchés résumés par exécution) et `budget_seconds`. Les marchés non traités
    le seront aux exécutions suivantes — le cache d'empreintes est persistant,
    donc rien n'est refait deux fois et le texte inchangé n'est jamais re-résumé.
    Appelé APRÈS l'écriture du cache principal : si l'IA locale traîne ou meurt,
    les probabilités du site sont déjà publiées."""
    if not cfg.get("summarize", True):
        return {}, 0
    cache = load_ai_summaries()
    todo = []
    for eid, b in blocks.items():
        h = b.get("h")
        if h and h not in cache and b.get("t"):
            todo.append((h, b.get("q") or "", b["t"][:int(cfg.get("max_chars", 4000))]))
    # dédoublonner par empreinte (deux marchés jumeaux partagent leur contrat)
    seen, uniq = set(), []
    for h, q, t in todo:
        if h in seen:
            continue
        seen.add(h)
        uniq.append((h, q, t))
    if not uniq:
        return cache, 0
    per_run = int(cfg.get("per_run", 60))
    batch = max(1, int(cfg.get("batch_size", 6)))
    deadline = time.time() + int(cfg.get("budget_seconds", 600))
    model = cfg.get("model", "sonnet")
    done = 0
    sys.stderr.write(f"[PM] règles : {len(uniq)} texte(s) sans résumé, "
                     f"plafond {per_run}/run, budget {int(cfg.get('budget_seconds', 600))}s\n")
    for i in range(0, min(len(uniq), per_run), batch):
        if time.time() > deadline:
            sys.stderr.write("[PM] règles : budget de temps atteint, suite au prochain run.\n")
            break
        got = summarize_batch(uniq[i:i + batch], model, min(300, max(60, int(deadline - time.time()))))
        if not got:
            sys.stderr.write("[PM] règles : lot sans résultat — on s'arrête là pour ce run.\n")
            break
        cache.update(got)
        done += len(got)
        try:
            RULES_AI.write_text(json.dumps(cache, ensure_ascii=False))   # sauvegarde au fil de l'eau
        except OSError:
            pass
    sys.stderr.write(f"[PM] règles : {done} résumé(s) produit(s), {len(cache)} en cache\n")
    return cache, done


def build_rules_blocks(entries):
    """{id: {q, src, end, t, subs, h}} — le contrat de chaque marché."""
    out = {}
    for e in entries:
        txt = (e.get("_rules") or "").strip()
        subs = e.get("_rules_subs") or []
        src = (e.get("_rules_src") or "").strip()
        if not txt and not subs:
            continue
        b = {"q": e.get("question"), "t": txt, "h": rules_hash(txt) if txt else None}
        if src:
            b["src"] = src
        if e.get("end_date"):
            b["end"] = e["end_date"]
        if subs:
            b["subs"] = subs[:14]
        out[e["id"]] = b
    return out


def write_rules(blocks, summaries, now_s):
    """Écrit prediction_markets_rules.{json,js}.

    Fichier SÉPARÉ et chargé en différé, comme l'historique des issues : ~1 Ko de
    contrat par marché n'a rien à faire dans le HTML inliné alors que la donnée
    ne s'affiche qu'à l'ouverture d'un drawer.
    Collecte vide → on ne réécrit rien (merge-preserve) : mieux vaut un fichier
    daté qu'un fichier vide."""
    if not blocks:
        sys.stderr.write("[PM] règles : aucun contrat collecté — fichier existant conservé.\n")
        return
    n_sum = 0
    for b in blocks.values():
        s = summaries.get(b.get("h"))
        if s:
            b["sum"] = s
            n_sum += 1
    payload = {
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "updated_ts": now_s,
        "source": "Polymarket (Gamma API — champ description, verbatim)",
        "notes": "t = conditions de résolution originales (anglais, font foi) ; "
                 "sum = résumé français généré en local, jamais substitué au texte ; "
                 "subs = conditions propres à une issue quand elles diffèrent.",
        "n_events": len(blocks),
        "n_summaries": n_sum,
        "events": blocks,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    RULES_JSON.write_text(body)
    RULES_JS.write_text("window.__PM_RULES__=" + body + ";")
    sys.stderr.write(f"[PM] règles : {len(blocks)} contrats, {n_sum} résumés FR "
                     f"({round(len(body)/1024)} Ko)\n")


# ── Snapshot quotidien (append-only, 1/jour) ───────────────────────────────────
def append_daily_snapshot(entries):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        if HISTORY_JSONL.exists():
            with open(HISTORY_JSONL, "rb") as f:
                # lire la dernière ligne efficacement
                try:
                    f.seek(-4096, 2)
                except OSError:
                    f.seek(0)
                tail = f.read().decode("utf-8", "ignore").strip().splitlines()
            if tail:
                try:
                    if json.loads(tail[-1]).get("date") == today:
                        return  # déjà snapshotté aujourd'hui
                except Exception:
                    pass
        ts = datetime.now(timezone.utc).isoformat()
        with open(HISTORY_JSONL, "a") as f:
            for e in entries:
                f.write(json.dumps({
                    "date": today, "ts": ts, "source": e["source"],
                    "market_id": e["id"], "theme_id": e.get("theme_id"),
                    "question": e["question"], "prob_yes": e["prob"],
                    "volume": e["volume"], "liquidity": e["liquidity"],
                }, ensure_ascii=False) + "\n")
    except Exception as ex:
        sys.stderr.write(f"  [snapshot] non-fatal: {ex}\n")


# ── Signaux dérivés ────────────────────────────────────────────────────────────
def compute_movers(entries, n=5):
    cands = [e for e in entries if (e.get("volume") or 0) >= MOVER_MIN_VOL and e.get("delta_24h") is not None]
    cands.sort(key=lambda e: -abs(e["delta_24h"]))
    movers = []
    for e in cands[:n]:
        movers.append({
            # `id` (2026-08-04) : sans lui, la couche live de la page ne peut pas
            # relier une ligne du ticker au marché correspondant, et le ticker
            # resterait sur les probabilités de la dernière collecte pendant que
            # le tableau juste en dessous affiche le direct.
            "id": e.get("id"),
            "question": e["question"], "theme_id": e.get("theme_id"),
            "theme_label": e.get("theme_label"), "prob": e["prob"],
            # prob_label : sur un marché à issues multiples, `prob` est la part de
            # l'issue favorite — sans son nom, le pourcentage du ticker n'a pas de
            # sujet, exactement le défaut corrigé dans le tableau le 2026-07-26.
            "prob_label": e.get("prob_label"),
            "is_multi": e.get("is_multi"),
            "delta_24h": e["delta_24h"], "url": e["url"],
            "direction": "up" if e["delta_24h"] >= 0 else "down",
        })
    return movers

LIVE_KEY_LEN = 12   # longueur de la clé courte d'un jeton CLOB dans /live/pm


def write_tokens(entries, now_s):
    """Liste des jetons CLOB affichés → lue par le relais /live/pm (Pages Function).

    POURQUOI un fichier à part (2026-08-04) : la page rafraîchit ses probabilités
    à la minute via un relais qui interroge `clob.polymarket.com/midpoints`. Ce
    relais doit connaître les jetons à demander. Il pourrait les lire dans
    `prediction_markets_cache_light.json`, mais ce fichier pèse ~710 Ko : le
    parser à chaque défaut de cache brûlerait le budget CPU d'un Worker pour
    une donnée qui tient en 140 Ko. On publie donc la liste nue.

    La clé courte (12 derniers chiffres) sert de dictionnaire compact dans la
    réponse du relais : un identifiant CLOB fait 77 chiffres, les répéter 1 700
    fois coûterait 130 Ko par rafraîchissement. Unicité VÉRIFIÉE ici — le relais
    la revérifie de son côté et écarte les doublons plutôt que de publier un prix
    attribué au mauvais marché.
    """
    toks, seen = [], set()
    for e in entries:
        for m in (e.get("markets") or []):
            t = m.get("clob_token")
            if t and t not in seen:
                seen.add(t)
                toks.append(str(t))
    keys = {}
    dupes = 0
    for t in toks:
        k = t[-LIVE_KEY_LEN:]
        if k in keys:
            dupes += 1
        keys[k] = t
    payload = {"updated_ts": now_s, "key_len": LIVE_KEY_LEN, "n": len(toks), "t": toks}
    TOKENS_JSON.write_text(json.dumps(payload, separators=(",", ":")))
    msg = f"[PM] jetons live : {len(toks)} ({round(TOKENS_JSON.stat().st_size/1024)} Ko)"
    if dupes:
        msg += f" ⚠ {dupes} collision(s) de clé courte — écartées par le relais"
    sys.stderr.write(msg + "\n")


def compute_tail_risk(entries):
    """Moyenne pondérée par le volume des probabilités de choc.

    ⚠️ BINAIRES UNIQUEMENT (corrigé 2026-07-25). Sur un marché à issues
    multiples, `prob` est la part de l'issue FAVORITE, pas la probabilité de
    l'événement redouté — l'injecter dans une jauge de risque n'a pas de sens.
    Cas réel qui a révélé le défaut : « Another US government shutdown & House
    Winner 2026? » (question COMBINÉE, favorite à 86 %, 329k$ = 92 % du poids
    du composite) faisait afficher un Tail Risk de 79,8 au lieu de ~11 — soit
    « risque extrême élevé » en rouge sur la foi d'un seul marché mal lu.
    Ne comptent donc que les marchés binaires, où prob = P(l'événement)."""
    tr = [e for e in entries if e.get("category") == "tail_risk" and e.get("prob") is not None
          and not e.get("is_multi")
          and (e.get("volume") or 0) >= MIN_VOL_COMPOSITE]
    if not tr:
        return None
    wsum = sum((e.get("volume") or 0) for e in tr) or 1
    score = sum(e["prob"] * (e.get("volume") or 0) for e in tr) / wsum
    comps = sorted([{"question": e["question"], "prob": e["prob"], "url": e["url"]} for e in tr],
                   key=lambda x: -x["prob"])[:6]
    return {"score": round(score * 100, 1), "n": len(tr), "components": comps}


# ── Main ────────────────────────────────────────────────────────────────────────
def load_prev_cache():
    if CACHE_JSON.exists():
        try:
            return json.loads(CACHE_JSON.read_text())
        except Exception:
            return None
    return None


def mark_cache_stale(reason):
    """Collecte vide → on NE remplace PAS les données (merge-preserve), mais on
    REFUSE de laisser un cache figé passer pour frais.

    Garde-fou né du bug du 2026-07-16→25 : le fetcher échouait sur 100 % de ses
    requêtes (DNS détourné), conservait proprement l'ancien cache… et ne le
    disait à personne. La page affichait « Dernière MAJ 16/07 14:18 » comme un
    horodatage normal pendant 9 jours.

    Deux effets :
      1. drapeau `fetch_failed` réinjecté dans le cache → la page affiche un
         bandeau « données figées » au lieu d'un horodatage muet ;
      2. mtime d'origine RESTAURÉ après réécriture — sinon le watchdog de
         fraîcheur verrait un fichier tout neuf, croirait le cache réparé et
         cesserait de relancer le fetcher (on transformerait l'alerte en
         camouflage)."""
    prev = load_prev_cache()
    if not prev:
        return
    now = datetime.now()
    prev["fetch_failed"] = True
    prev["fetch_failed_reason"] = reason
    prev["last_attempt"] = now.strftime("%d/%m/%Y %H:%M")
    prev["last_attempt_ts"] = int(now.replace(tzinfo=None).timestamp())
    bodies = (
        (CACHE_JSON, json.dumps(prev, ensure_ascii=False, indent=2)),
        (CACHE_JS, "window.__PREDICTION_MARKETS__=" +
                   json.dumps(prev, separators=(",", ":"), ensure_ascii=False) + ";"),
    )
    for path, body in bodies:
        try:
            st = path.stat()
            path.write_text(body)
            os.utime(path, (st.st_atime, st.st_mtime))   # mtime préservé → watchdog continue d'alerter
        except OSError as e:
            sys.stderr.write(f"[PM] marquage « figé » impossible sur {path.name} : {e}\n")
    sys.stderr.write(f"[PM] cache marqué « données figées » ({reason}) — mtime préservé pour le watchdog.\n")


def main():
    force = "--force" in sys.argv
    force_hist = "--force-history" in sys.argv
    # --no-summary : collecte les données et publie les conditions VERBATIM, sans
    # passer par l'IA locale. Utile pour un run de contrôle rapide et pour
    # séparer les deux préoccupations quand on remplit les résumés à part.
    no_summary = "--no-summary" in sys.argv

    if CACHE_JSON.exists() and not force:
        age_h = (datetime.now().timestamp() - CACHE_JSON.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[PM] Cache frais ({age_h:.1f}h < {CACHE_MAX_HOURS}h). --force pour forcer.\n")
            return

    themes, defaults, sweep_cfg, rules_cfg = load_themes()
    top_n = int(defaults.get("top_n", DEFAULT_TOP_N))
    resolved_thr = safe_float(defaults.get("resolved_prob_threshold")) or RESOLVED_THRESHOLD
    settled_thr  = safe_float(defaults.get("settled_prob_threshold")) or SETTLED_THRESHOLD
    sys.stderr.write(f"[PM] {len(themes)} thèmes, top_n={top_n}, seuil résolu={resolved_thr:.2f}\n")
    n_resolved = 0

    prev = load_prev_cache() or {}
    prev_hist = {}
    for e in (prev.get("entries") or []):
        if e.get("history"):
            prev_hist[e["id"]] = {"history": e["history"], "hist_updated": e.get("hist_updated")}

    # 1) Collecte + matching par thème, assignation au meilleur thème (dédup global)
    best = {}   # entry_id -> (score, theme, entry)
    for th in themes:
        sys.stderr.write(f"  → thème « {th['label']} »\n")
        found = collect_for_theme(th)
        for entry in found:
            if (entry.get("volume") or 0) < MIN_INCLUDE_VOL:
                continue  # marché mort (pas de volume = pas de skin in the game)
            if is_resolved(entry, resolved_thr, settled_thr):
                n_resolved += 1
                continue  # marché « fini » (≈100%) : retiré automatiquement
            score = relevance_score(th, entry)
            if score <= 0:
                continue
            cur = best.get(entry["id"])
            if cur is None or score > cur[0]:
                best[entry["id"]] = (score, th, entry)

    # 1bis) Balayage par liquidité — l'exhaustivité, là où la watchlist n'apporte
    #       que la couverture éditoriale. Les deux se cumulent : un événement déjà
    #       attrapé par un mot-clé GARDE son thème (plus spécifique : « Iran &
    #       Ormuz » plutôt que « Géopolitique »), le balayage ne fait qu'ajouter.
    themes_by_id = {th["id"]: th for th in themes}
    routes, excl_tags = prepare_routes(sweep_cfg)
    sweep_min_vol = safe_float(sweep_cfg.get("min_volume")) or 0
    sweep_min_liq = safe_float(sweep_cfg.get("min_liquidity")) or 0
    swept = {}
    n_sweep_seen = n_sweep_thin = n_sweep_offtopic = 0
    if routes:
        for ev in sweep_events(sweep_cfg):
            entry = parse_event(ev)
            if not entry:
                continue
            n_sweep_seen += 1
            if (entry.get("volume") or 0) < sweep_min_vol or (entry.get("liquidity") or 0) < sweep_min_liq:
                n_sweep_thin += 1
                continue
            if is_resolved(entry, resolved_thr, settled_thr):
                n_resolved += 1
                continue
            tid = route_sweep_entry(entry, routes, excl_tags)
            if not tid or tid not in themes_by_id:
                n_sweep_offtopic += 1
                continue
            swept[entry["id"]] = (themes_by_id[tid], entry)
        sys.stderr.write(
            f"[PM] balayage : {len(swept)} marchés retenus "
            f"(seuils {sweep_min_vol:,.0f}$ vol / {sweep_min_liq:,.0f}$ liq · "
            f"{n_sweep_thin} trop peu liquides, {n_sweep_offtopic} hors domaine)\n")

    # 2) Garder top_n par thème (watchlist), puis ajouter le balayage sans plafond
    #    — c'est le seuil de liquidité qui filtre, pas un top N arbitraire.
    by_theme = {}
    for score, th, entry in best.values():
        entry = dict(entry)
        entry["theme_id"] = th["id"]
        entry["theme_label"] = th.get("label")
        entry["category"] = th.get("category", "other")
        entry["relevance"] = round(score, 2)
        entry["sel"] = "watchlist"
        by_theme.setdefault(th["id"], []).append(entry)

    entries = []
    for th in themes:
        lst = by_theme.get(th["id"], [])
        lst.sort(key=lambda e: -e["relevance"])
        entries.extend(lst[:top_n])
    n_watchlist = len(entries)

    # Dédup contre la sélection RÉELLE, pas contre tout ce que les mots-clés ont
    # vu. Piège corrigé le 2026-08-03 : écarter les ids présents dans `best`
    # revenait à bloquer un marché qu'un thème avait trouvé PUIS recalé au top_n —
    # il disparaissait alors des deux côtés, et le balayage rendait 102 marchés
    # au lieu de 200. Le seul critère qui vaille est « est-il déjà publié ? ».
    selected_ids = {e["id"] for e in entries}
    sweep_by_theme = {}
    for th, entry in swept.values():
        if entry["id"] in selected_ids:
            continue
        entry = dict(entry)
        entry["theme_id"] = th["id"]
        entry["theme_label"] = th.get("label")
        entry["category"] = th.get("category", "other")
        entry["relevance"] = 0.0
        entry["sel"] = "sweep"
        sweep_by_theme.setdefault(th["id"], []).append(entry)
    for th in themes:
        lst = sweep_by_theme.get(th["id"], [])
        lst.sort(key=lambda e: -(e.get("volume") or 0))
        entries.extend(lst)
        by_theme.setdefault(th["id"], []).extend(lst)

    sys.stderr.write(f"[PM] {n_resolved} marché(s) « fini(s) » (≈100%) retiré(s) automatiquement\n")
    sys.stderr.write(f"[PM] sélection : {n_watchlist} watchlist + {len(entries)-n_watchlist} balayage "
                     f"= {len(entries)} marchés\n")

    if not entries:
        sys.stderr.write("[PM] AUCUN marché trouvé — merge-preserve : cache existant conservé.\n")
        if prev:
            sys.stderr.write("[PM] (cache précédent intact, pas d'écrasement à vide)\n")
        mark_cache_stale("aucun marché récupéré — source Polymarket injoignable")
        sys.exit(3)   # code d'échec explicite : visible dans les logs launchd

    # 3) Backfill historique (réutilise le cache si frais ; plafonné)
    #    - historique de la favorite  → sparkline + colonne du tableau
    #    - historique de CHAQUE issue → graphique multi-courbes du drawer
    now_s = int(datetime.now(timezone.utc).timestamp())
    outcome_full_min_vol = safe_float(defaults.get("outcome_full_min_volume")) or 0
    prev_blocks, prev_block_ts = load_prev_outcomes()
    fetched = 0
    out_fetched = 0
    n_done = 0
    out_events = {}
    out_ts = {}
    for e in entries:
        ph = prev_hist.get(e["id"])
        reuse = False
        if ph and not force_hist:
            hist = ph["history"]
            if hist:
                last_t = hist[-1][0]
                if (now_s - last_t) / 3600 < BACKFILL_MAX_HOURS:
                    e["history"] = hist
                    e["hist_updated"] = ph.get("hist_updated")
                    reuse = True
        if not reuse and fetched < HISTORY_FETCH_CAP:
            hist = fetch_history(e.get("head_token"))
            if hist:
                e["history"] = hist
                e["hist_updated"] = now_s
                fetched += 1
            elif ph:  # échec fetch → garder l'ancien historique
                e["history"] = ph["history"]
                e["hist_updated"] = ph.get("hist_updated")
        elif not reuse and ph:
            e["history"] = ph["history"]
            e["hist_updated"] = ph.get("hist_updated")

        # 3bis) Historique de toutes les issues (événements multi-issues seulement)
        #       PALIER DE VOLUME (2026-08-03) : chaque issue coûte un appel réseau.
        #       À 370 marchés, tout collecter ferait un run quotidien de plus d'une
        #       heure — donc un run tué en vol, donc pas de données du tout. Les
        #       marchés sous le palier gardent la courbe de leur favori (le
        #       graphique du drawer retombe proprement dessus, cf. outSeries).
        subs = e.get("markets") or []
        if len(subs) > 1 and (e.get("volume") or 0) >= outcome_full_min_vol:
            pb, pts = prev_blocks.get(e["id"]), prev_block_ts.get(e["id"])
            fresh = (not force_hist
                     and block_is_fresh(pb, pts, len(subs), e.get("prob_label"), now_s))
            if fresh:
                out_events[e["id"]] = pb
                out_ts[e["id"]] = pts
            elif out_fetched + len(subs) <= OUTCOME_HISTORY_CAP:
                raw_by_label = {}
                head_tok = e.get("head_token")
                for m in subs:
                    tok = m.get("clob_token")
                    if tok and tok == head_tok and e.get("history") and not needs_finer(e["history"]):
                        raw_by_label[m.get("label")] = e["history"]   # déjà en main, pas de 2e appel
                        continue
                    raw = fetch_history(tok, sleep_s=OUTCOME_SLEEP)
                    out_fetched += 1
                    if needs_finer(raw):        # marché court → on redemande au pas horaire
                        fine = fetch_history(tok, sleep_s=OUTCOME_SLEEP, fidelity=OUTCOME_FINE_FIDELITY)
                        out_fetched += 1
                        if len(fine) > len(raw):
                            raw = fine
                    if raw:
                        raw_by_label[m.get("label")] = raw
                block = build_outcome_block(e, raw_by_label, now_s)
                if block:
                    out_events[e["id"]] = block
                    out_ts[e["id"]] = now_s
                    # Point de sauvegarde : la collecte des issues dure ~12 min et
                    # peut être tuée en vol (blocage réseau, cf. watchdog). Écrire
                    # au fil de l'eau fait perdre au pire quelques marchés au lieu
                    # de la totalité du travail.
                    n_done += 1
                    if n_done % 25 == 0:
                        # Le point de sauvegarde écrit l'ANCIEN état recouvert par
                        # ce qui est déjà collecté : sans cela il publierait un
                        # fichier amputé des marchés pas encore traités, et une
                        # mort en vol effacerait les blocs qu'on voulait préserver.
                        write_outcomes(dict(prev_blocks, **out_events),
                                       dict(prev_block_ts, **out_ts),
                                       now_s, out_fetched, len(entries), quiet=True)
                elif pb:                       # échec réseau → on garde l'ancien bloc
                    out_events[e["id"]] = pb
                    out_ts[e["id"]] = pts or 0
            elif pb:                           # plafond atteint → merge-preserve
                out_events[e["id"]] = pb
                out_ts[e["id"]] = pts or 0
        elif len(subs) > 1 and prev_blocks.get(e["id"]):
            # Sous le palier de volume, mais un bloc existe déjà (marché plus actif
            # hier qu'aujourd'hui) : on le garde. Le palier décide de ce qu'on
            # COLLECTE, jamais de ce qu'on jette.
            out_events[e["id"]] = prev_blocks[e["id"]]
            out_ts[e["id"]] = prev_block_ts.get(e["id"]) or 0

        e.pop("head_token", None)
        e.pop("_hay", None)
        e.pop("_tokens", None)
    sys.stderr.write(f"[PM] historique : {fetched} fetch CLOB, {len(entries)-fetched} réutilisés/plafonnés\n")
    write_outcomes(out_events, out_ts, now_s, out_fetched, len(entries))

    # 3ter) Conditions de résolution — le contrat de chaque pari. Extraites de la
    #       réponse Gamma déjà en main (aucun appel réseau) et publiées dans un
    #       fichier séparé. On les retire des entrées AVANT l'écriture du cache
    #       principal, qui est inliné dans la page.
    rules_blocks = build_rules_blocks(entries) if rules_cfg.get("enabled", True) else {}
    for e in entries:
        e.pop("_rules", None)
        e.pop("_rules_subs", None)
        e.pop("_rules_src", None)

    # 4) Signaux dérivés
    movers = compute_movers(entries)
    tail_risk = compute_tail_risk(entries)

    # 5) Snapshot quotidien (future-proof)
    append_daily_snapshot(entries)

    # 6) Stats + payload
    total_vol = sum((e.get("volume") or 0) for e in entries)
    # Le compte d'un thème est le nombre de marchés RÉELLEMENT publiés (watchlist
    # plafonnée à top_n + balayage sans plafond) : compter by_theme[:top_n]
    # sous-estimerait tout ce que le balayage apporte.
    n_by_theme = {}
    for e in entries:
        n_by_theme[e.get("theme_id")] = n_by_theme.get(e.get("theme_id"), 0) + 1
    theme_meta = [{"id": th["id"], "label": th.get("label"), "category": th.get("category", "other"),
                   "count": n_by_theme.get(th["id"], 0)} for th in themes]

    payload = clean_nan({
        "entries": entries,
        "themes": theme_meta,
        "movers": movers,
        "tail_risk": tail_risk,
        "stats": {
            "n_markets": len(entries),
            "n_themes": sum(1 for t in theme_meta if t["count"] > 0),
            "total_volume": round(total_vol),
        },
        # `updated` DÉRIVE de `updated_ts` (2026-08-04). Les deux étaient calculés
        # à des instants différents — l'horodatage machine au début de la phase
        # d'historique, le libellé à la fin de l'écriture — d'où un écart de
        # plusieurs minutes sur un run long : la page affichait « collecte 13:15 ·
        # il y a 11 min ». Une date lisible et son horodatage ne peuvent pas
        # désigner deux moments différents.
        "updated": datetime.fromtimestamp(now_s).strftime("%d/%m/%Y %H:%M"),
        "updated_ts": now_s,
        "source": "Polymarket (Gamma API · CLOB prices-history)",
        "notes": "Prix = probabilité implicite payée par de l'argent réel. Δ = points de probabilité.",
    })

    # Cache complet NON indenté depuis le 2026-08-03 : l'indentation coûtait
    # 4 Mo sur 6,7 (352 marchés) pour un fichier que seule une machine lit — la
    # page charge le .js, et son rafraîchissement doux tire l'instantané allégé.
    # Pour le relire à l'œil : python3 -m json.tool prediction_markets_cache.json
    CACHE_JSON.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    CACHE_JS.write_text("window.__PREDICTION_MARKETS__=" +
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";")

    # Instantané ALLÉGÉ, destiné au seul inline de la page (cf. Prediction_Markets.Rmd).
    # Mesuré le 2026-08-03 : à 335 marchés le cache pèse 2,7 Mo minifié dont 72 %
    # d'historiques de sparklines — inliné tel quel (et indenté) le HTML passait
    # de 2,6 à ~6,6 Mo. Or l'inline ne sert qu'au tout premier affichage : le
    # fichier .js complet est chargé dans la foulée et réécrit window.__PREDICTION_MARKETS__
    # avec les historiques. On retire donc `history` de l'inline — les sparklines
    # apparaissent au chargement du .js, quelques dizaines de ms plus tard, au lieu
    # de faire payer 4 Mo à chaque visiteur.
    light = dict(payload)
    light["entries"] = [{k: v for k, v in e.items() if k not in ("history", "hist_updated")}
                        for e in payload["entries"]]
    light["light"] = True
    CACHE_LIGHT.write_text(json.dumps(light, separators=(",", ":"), ensure_ascii=False))
    write_tokens(entries, now_s)
    sys.stderr.write(f"[PM] inline allégé : {round(CACHE_LIGHT.stat().st_size/1024)} Ko "
                     f"(cache complet {round(CACHE_JS.stat().st_size/1024)} Ko)\n")
    sys.stderr.write(f"[PM] OK : {len(entries)} marchés, {len(movers)} movers, "
                     f"tail_risk={'oui' if tail_risk else 'non'}, vol total ${total_vol:,.0f}\n")
    sys.stderr.write(f"[PM] Écrit {CACHE_JSON.name} + {CACHE_JS.name}\n")

    # 7) Conditions de résolution — EN DERNIER, à dessein.
    #    Le verbatim est publié tout de suite (coût nul, aucun appel réseau), puis
    #    les résumés français manquants sont produits par l'IA locale et le
    #    fichier est réécrit. Cet ordre garantit qu'une IA lente, absente ou en
    #    quota n'empêche jamais la publication des probabilités : au pire la page
    #    affiche les conditions en anglais, ce qui reste juste.
    if rules_blocks:
        write_rules(dict(rules_blocks), load_ai_summaries(), now_s)
        if not no_summary:
            summaries, n_new = summarize_missing(rules_blocks, rules_cfg)
            if n_new:
                write_rules(rules_blocks, summaries, now_s)


if __name__ == "__main__":
    main()
