#!/usr/bin/env python3
"""Crypto Narrative Fundamentals — mcap-weighted metrics per narrative.

Parallel de fetch_tradfi_fundamentals.py mais pour la crypto.

Sources :
- CoinGecko /coins/markets (batch) : mcap, FDV, volume, circulating, perf 7d/30d/1y
- DeFiLlama /protocols : TVL par gecko_id match
- DeFiLlama /overview/fees?dataType=dailyFees : FRAIS 1y par gecko_id, soit ce
  que PAIENT les utilisateurs. C'est le dénominateur du rapport prix/frais.
- DeFiLlama /overview/fees?dataType=dailyRevenue : REVENU 1y par gecko_id, soit
  ce que GARDE le protocole (le reste va aux fournisseurs de liquidité, aux
  validateurs, ou est brûlé). Ajouté le 05/09/2026 : le rapport s'appelait
  « P/S » alors que son dénominateur était des frais. Écart mesuré le même jour
  sur la chaîne Ethereum : 214,7 M$ payés contre 69,5 M$ gardés, soit un
  facteur 3,1. Coût mesuré de la collecte supplémentaire : 3,8 Mo et 0,4 s pour
  l'appel groupé, plus 25 appels de chaîne à ~0,3 s.

Doctrine des rapports (05/09/2026, alignée sur la fiche du jeton) : un rapport
dont le dénominateur s'est effondré ne veut rien dire, mais on ne le supprime
pas — on le NOTE. Trois états dans `ps_ttm_statut` / `mc_tvl_statut` :
« mesurable », « non_mesurable », « sans_objet », chacun avec son motif en clair.
La valeur brute reste publiée pour que le lecteur voie ce qui a été écarté.

Métriques par narrative (pondérées par market cap) :
- mcap_total_b, fdv_total_b, volume_total_b
- circ_pct : circulating / FDV (garde-fou : jamais > 100 %, cf. garantir_offre_coherente)
- vol_mcap_pct : volume 24h / market cap (proxy liquidité)
- tvl_total_b, mc_tvl : MC / TVL (valorisation vs TVL du secteur DeFi)
- ps_ttm : MC_panier_avec_denominateur / dénominateur_TTM. Le dénominateur est
  publié tel quel dans rev_m_1y_total, décomposé en rev_m_1y_crypto (frais
  on-chain DeFiLlama) et rev_m_1y_actions (chiffre d'affaires Yahoo), et sa
  nature est dite par ps_ttm_denominateur ("frais" | "chiffre d'affaires" | "mixte")
- revenu_m_1y_total : ce que les protocoles GARDENT (dailyRevenue), à ne pas
  confondre avec rev_m_1y_total qui est le dénominateur du rapport
- perf_7d, perf_30d, perf_1y
- dominance_pct : mcap_top_token / mcap_total × 100
- n_tokens, n_with_tvl, n_with_rev

Output :
- narratives_fundamentals_cache.json
- narratives_fundamentals_cache.js (window.__NARRATIVES_FUNDAMENTALS__)
"""
# ── Global timeout safeguard (30 min) — auto-tué si bloqué sur un I/O réseau,
#    libère le lock pour le prochain cycle launchd. Sans ça, un script bloqué
#    monopolise indéfiniment le verrou et empêche tous les refresh suivants.
import signal as _signal, sys as _sys
def _global_timeout_handler(signum, frame):
    print(f"[fatal] global timeout (30 min) reached — aborting to free lock for next launchd cycle.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(30 * 60)
except Exception:
    pass

import json
import os
import random
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "narratives_fundamentals_cache.json"
OUT_JS   = CACHE_DIR / "narratives_fundamentals_cache.js"
TRACKER_CACHE = CACHE_DIR / "narratives_cache.json"   # written by fetch_narratives.py
LOCK_FILE = CACHE_DIR / "narratives_fundamentals.lock"

# Le cache précédent est TOUJOURS relu sur le fichier de production, même en
# banc d'essai : sans ça le rattrapage des jetons absents de CoinGecko perdrait
# sa seule source et le banc d'essai testerait un chemin qui n'existe pas.
CACHE_PRECEDENT = CACHE_DIR / "narratives_fundamentals_cache.json"

# ── Banc d'essai ────────────────────────────────────────────────────────
# NARRATIFS_FUNDA_BANC_ESSAI=1 rejoue les lignes CoinGecko et le chiffre
# d'affaires des actions depuis le cache précédent au lieu d'appeler les API.
# Pourquoi : le quota CoinGecko gratuit est déjà consommé par les autres
# collecteurs du site ; vérifier une modification de l'agrégation coûtait
# sinon 3 lots de 80 jetons pris sur ce quota, et une exécution ratée à
# mi-course laissait le site avec un cache incomplet.
# Le banc d'essai écrit dans des fichiers SÉPARÉS et prend un verrou séparé :
# il ne peut ni écraser le cache servi en ligne, ni bloquer le cycle launchd.
BANC_ESSAI = os.environ.get("NARRATIFS_FUNDA_BANC_ESSAI") == "1"
if BANC_ESSAI:
    OUT_JSON  = CACHE_DIR / "narratives_fundamentals_banc_essai.json"
    OUT_JS    = CACHE_DIR / "narratives_fundamentals_banc_essai.js"
    LOCK_FILE = CACHE_DIR / "narratives_fundamentals_banc_essai.lock"

# ── Seuils d'absurdité des rapports sectoriels (défaut 2) ───────────────
# Mesurés sur la distribution réelle des 25 narratifs, cache du 04/09/2026.
#
# prix/frais — valeurs observées, décroissantes : 126 256 · 1 140,6 · 445,8 ·
# 102,5 · 71,0 · 69,4 · 66,0 · 40,2 · 28,9 · 27,7 · 24,6 · 15,6 · 11,8 · 11,7 ·
# 5,8 · 4,0 · 3,6 · 2,9 · 2,0 · 1,7 · 0,7 (n = 21, médiane 24,6).
# Le plus grand écart entre deux valeurs consécutives de toute la distribution
# est ×110,7, entre Ethereum (1 140,6) et Jetons de paiement (126 256) ; le
# deuxième plus grand n'est que ×4,35 (102,5 → 445,8). La distribution désigne
# donc elle-même UN seul aberrant. On pose le seuil dans cet intervalle, à
# 10 000× : des frais annuels valant 0,01 % de la capitalisation, soit dix mille
# ans pour rembourser le prix. Ethereum, dont l'effondrement des frais est un
# fait mesuré et non un artefact de collecte, reste « mesurable » — c'est
# voulu : le seuil écarte les dénominateurs absents, pas les vérités gênantes.
SEUIL_ABSURDITE_PS_TTM = 10000.0

# capitalisation/TVL — valeurs observées, décroissantes : 369,35 · 349,00 ·
# 43,43 · 42,86 · 20,88 · 16,84 · 15,27 · 13,00 · 7,36 · 7,17 · 5,38 · 4,79 ·
# 2,87 · 1,72 · 1,58 · 1,31 · 0,60 · 0,31 · 0,16 · 0,13 · 0,06 · 0,02
# (n = 22, médiane 5,08). Plus grand écart consécutif : ×8,03 entre IA & agents
# (43,43) et Jetons de paiement (349,00) ; deuxième plus grand : ×2,05. Seuil
# posé dans cet intervalle, à 100× : la valeur verrouillée y pèse 1 % de la
# capitalisation. Le panier n'est pas une place de finance décentralisée, le
# rapport ne renseigne plus sur sa valorisation.
SEUIL_ABSURDITE_MC_TVL = 100.0

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def acquire_singleton_lock():
    import fcntl, os
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[lock] another fetch_narratives_fundamentals instance is running — exiting", file=sys.stderr)
        fd.close()
        return None
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def _fr_nombre(x, decimales=0):
    """Écrit un nombre à la française : espace fine pour les milliers, virgule
    décimale. Les motifs partent à l'écran ; « 126,256x » y serait lu comme
    cent-vingt-six virgule deux-cent-cinquante-six."""
    if x is None:
        return "—"
    return ("{:,.%df}" % decimales).format(x).replace(",", "\u202f").replace(".", ",")


def garantir_offre_coherente(t, journal):
    """Aucune sortie ne publie une capitalisation supérieure à sa valeur
    pleinement diluée, ni une offre en circulation au-delà de 100 %.

    Pourquoi ce garde-fou EN PLUS du correctif de valeur pleinement diluée :
    la capitalisation vient du tracker (instant T) et la valeur pleinement
    diluée de CoinGecko (instant T'). Le correctif rejoue le rapport
    offre_totale/offre_en_circulation sur le couple CoinGecko — cohérent avec
    lui-même — mais il ne s'applique QUE si les deux valeurs CoinGecko sont
    strictement positives, et rien ne protège les lignes qui n'empruntent pas
    ce chemin. Mesuré le 05/09/2026 sur le cache servi en ligne, produit par le
    miroir en nuage qui n'a jamais reçu le correctif : 22 jetons avec
    capitalisation > valeur pleinement diluée (Monero à 1,0184, ce qui est
    impossible) et 2 jetons au-delà de 100 % d'offre (USTB 100,6 %, KAG 101,3 %).

    Aucune valeur n'est inventée : la valeur pleinement diluée est simplement
    ramenée à son plancher de définition — elle compte au moins les jetons déjà
    en circulation. Chaque correction est journalisée avec son écart réel.
    """
    raisons = []
    mcap = t.get("mcap_b") or 0
    fdv = t.get("fdv_b") or 0
    if mcap > 0 and 0 < fdv < mcap:
        ecart_pct = (mcap / fdv - 1.0) * 100.0
        t["fdv_b"] = round(mcap, 3)
        t["circ_pct"] = 100.0
        journal["fdv_relevee"] += 1
        raisons.append("valeur pleinement diluée relevée au niveau de la capitalisation "
                       "(écart de collecte %s %%)" % _fr_nombre(ecart_pct, 2))
    circ = t.get("circ_pct")
    if circ is not None and circ > 100.0:
        t["circ_pct"] = 100.0
        journal["offre_ramenee"] += 1
        raisons.append("offre en circulation ramenée de %s %% à 100 %%" % _fr_nombre(circ, 1))
    if raisons:
        t["_offre_corrigee"] = " ; ".join(raisons)
        journal["jetons_corriges"] += 1
        journal["detail"].append({
            "symbol": t.get("symbol"), "id": t.get("id"), "raison": t["_offre_corrigee"],
        })
    return t


def _statut_et_motif(valeur, seuil, n_avec, n_total, couverture_pct,
                     noms, motif_sans_objet=None):
    """Trois états, comme la fiche du jeton : « mesurable », « non_mesurable »,
    « sans_objet ». La valeur brute n'est JAMAIS supprimée — le lecteur doit
    pouvoir la voir et lire pourquoi elle est écartée.

    - sans_objet    : la grandeur ne s'applique pas à ce panier par nature
                      (une action cotée n'a pas de valeur verrouillée on-chain).
    - non_mesurable : la grandeur s'applique, mais le dénominateur manque
                      (aucun constituant ne le fournit) ou s'est effondré
                      au-delà de son seuil d'absurdité.
    - mesurable     : sinon. Le motif dit alors sur quelle part du panier.
    """
    if motif_sans_objet:
        return "sans_objet", motif_sans_objet
    # `noms` = (sujet, objet affirmatif, objet négatif). Trois formes du même
    # dénominateur, parce que le français ne les interchange pas : « LE TOTAL DES
    # FRAIS ne représente que… », « fournissent DES FRAIS… », « ne fournit pas
    # DE FRAIS… ». Une seule forme partagée donnait des motifs faux à l'écran
    # (« 8 constituants fournissent de chiffre d'affaires publié ») — et le
    # lecteur juge la rigueur d'un chiffre sur la phrase qui l'entoure.
    nom_sujet, nom_affirmatif, nom_negatif = noms
    if valeur is None:
        if n_total <= 0:
            return "sans_objet", "Panier vide : le rapport n'a pas de sujet."
        return ("non_mesurable",
                "Aucun des %d constituants du panier ne fournit %s : le rapport n'a "
                "pas de dénominateur." % (n_total, nom_negatif))
    if valeur > seuil:
        return ("non_mesurable",
                "Dénominateur effondré : %s ne représente que %s %% de la "
                "capitalisation retenue (rapport de %s×, au-delà du seuil "
                "d'absurdité de %s×). La valeur brute reste affichée, elle ne "
                "mesure plus une valorisation."
                % (nom_sujet, _fr_nombre(100.0 / valeur, 4),
                   _fr_nombre(valeur, 0), _fr_nombre(seuil, 0)))
    # ⚠ UN RAPPORT PEUT ÊTRE SOUS SON SEUIL ET NE DÉCRIRE QUE 2 % DU PANIER.
    # Première version : le statut ne regardait QUE le plafond. « Immobilisation
    # liquide » sortait donc à 0,6× étiqueté « mesurable » — un rapport bâti sur
    # 0,571 Md$ des 24,6 Md$ du panier, soit 2,3 % de sa capitalisation, quand
    # son propre motif imprimait déjà ce chiffre deux lignes plus bas. Le
    # lecteur lisait « le panier se paie 0,6 fois ses frais » là où la phrase
    # exacte est « les 2 % du panier dont on connaît les frais se paient 0,6
    # fois ». Ce n'est pas la même affirmation, et la seconde ne se déduit pas
    # de la première.
    #
    # Le seuil de couverture est le même que celui déjà en vigueur à l'écran :
    # le tableau avertit sous 25 % depuis toujours (« ratio extrapolé d'une
    # minorité de tokens »). On ne l'invente pas, on le fait remonter du
    # commentaire d'infobulle jusqu'au statut, là où il décide de quelque chose.
    COUVERTURE_MINIMALE = 25.0
    couv = couverture_pct or 0.0
    verbe_c = "constituant sur %d fournit" if n_avec == 1 else "constituants sur %d fournissent"
    if couv < COUVERTURE_MINIMALE:
        return ("non_mesurable",
                ("Couverture insuffisante : %d " + verbe_c + " %s, soit %s %% "
                 "seulement de la capitalisation du panier — en dessous de %s %%, "
                 "le rapport décrit cette minorité, pas le panier. La valeur brute "
                 "(%s×) reste affichée.")
                % (n_avec, n_total, nom_affirmatif, _fr_nombre(couv, 1),
                   _fr_nombre(COUVERTURE_MINIMALE, 0), _fr_nombre(valeur, 2)))
    # « 1 constituants » : le lecteur voit d'abord la faute, ensuite le chiffre.
    return ("mesurable",
            ("%d " + verbe_c + " %s, soit %s %% de la capitalisation du panier.")
            % (n_avec, n_total, nom_affirmatif, _fr_nombre(couv, 0)))


def load_previous_cache():
    if not CACHE_PRECEDENT.exists():
        return {}
    try:
        with CACHE_PRECEDENT.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] could not load prev funda cache: {e}", file=sys.stderr)
        return {}


def load_tracker_data():
    """Read narratives_cache.json (price + mcap + momentum). Returns:
        {
          'tokens':   {cg_id: {price, mcap, volume, perf_7d, perf_30d}},
          'momentum': {narrative_name: {score, signal, rank, rel_mom_90d, breadth_30d, ...}},
          'updated':  '...'
        }
    Single source of truth for prices/mcap. Empty if cache missing — funda
    will still run but with no cross-data."""
    if not TRACKER_CACHE.exists():
        print(f"[warn] {TRACKER_CACHE.name} missing — funda will use only CoinGecko prices", file=sys.stderr)
        return {"tokens": {}, "momentum": {}, "updated": None}
    try:
        with TRACKER_CACHE.open("r", encoding="utf-8") as f:
            tc = json.load(f)
    except Exception as e:
        print(f"[warn] cannot read {TRACKER_CACHE.name}: {e}", file=sys.stderr)
        return {"tokens": {}, "momentum": {}, "updated": None}
    tokens = {}
    stocks_by_narrative = {}   # {narrative_name: [{symbol, name, mcap_b, perf_*, ...}, …]}
    momentum = {}
    for n in tc.get("narratives", []):
        sec = n.get("narrative")
        if sec:
            momentum[sec] = {
                "score":            n.get("score"),
                "rank":             n.get("rank"),
                "signal":           n.get("signal"),
                "signal_reason":    n.get("signal_reason"),
                "trend_age_days":   n.get("trend_age_days"),
                "rel_mom_90d":      n.get("rel_mom_90d"),
                "rel_mom_180d":     n.get("rel_mom_180d"),
                "breadth_30d":      n.get("breadth_30d"),
                "price_momentum":   n.get("price_momentum"),
                "perf_90d_w":       n.get("perf_90d_w"),
                "perf_180d_w":      n.get("perf_180d_w"),
            }
        n_stocks = []
        for t in n.get("tokens", []):
            tid = t.get("id") or t.get("symbol")
            if t.get("is_stock"):
                # Stock entry → injecté plus bas dans l'agrégation du narratif
                # afin que le panier "fundamentals" reflète EXACTEMENT le panier
                # "tracker" (single source of truth — mêmes constituants partout).
                mcap_usd = t.get("mcap") or 0
                if mcap_usd <= 0:
                    continue
                n_stocks.append({
                    "id":         "stock:" + (t.get("symbol") or tid),
                    "symbol":     t.get("symbol") or tid,
                    "name":       t.get("name") or t.get("symbol") or tid,
                    "image":      t.get("image"),
                    "is_stock":   True,
                    "price":      t.get("price"),
                    "mcap_b":     round(mcap_usd / 1e9, 3),
                    "fdv_b":      round(mcap_usd / 1e9, 3),  # actions : pas de FDV crypto-style → on prend mcap
                    "vol_b":      round((t.get("volume") or 0) / 1e9, 3),
                    "circ_pct":   100.0,                     # toutes les actions outstanding sont en circulation
                    "vol_mcap_pct": None,
                    "tvl_b":      None,                      # pas de TVL on-chain pour une action
                    "mc_tvl":     None,
                    "rev_m_1y":   None,                      # revenue boursier ≠ revenue on-chain (couvert par PER tradfi)
                    "ps_ttm":     None,
                    "perf_7d":    t.get("perf_7d"),
                    "perf_30d":   t.get("perf_30d"),
                    "perf_1y":    t.get("perf_1y"),
                    "_price_source": "tracker_stock",
                })
            elif tid and tid not in tokens:
                tokens[tid] = {
                    "price":    t.get("price"),
                    "mcap":     t.get("mcap"),
                    "volume":   t.get("volume"),
                    "perf_7d":  t.get("perf_7d"),
                    "perf_30d": t.get("perf_30d"),
                    "_stale":   t.get("_stale"),
                    "_source":  t.get("_source"),
                }
        if sec and n_stocks:
            stocks_by_narrative[sec] = n_stocks
    n_stocks_total = sum(len(v) for v in stocks_by_narrative.values())
    print(f"[info] tracker data loaded: {len(tokens)} tokens + {n_stocks_total} stocks (across {len(stocks_by_narrative)} narratives), {len(momentum)} narratives (cache mtime: {tc.get('updated')})")
    return {"tokens": tokens, "stocks_by_narrative": stocks_by_narrative, "momentum": momentum, "updated": tc.get("updated")}

try:
    from fetch_narratives import NARRATIVES
except Exception as e:
    print(f"[fatal] Cannot import NARRATIVES: {e}", file=sys.stderr)
    sys.exit(1)


def _http_get_json(url, timeout=45, attempts=4, sleep=2.0, rate_limit_sleep=35.0):
    """Simple GET with retry + backoff.

    For 429 (rate limit), sleep at least `rate_limit_sleep` seconds
    (CoinGecko free tier = 30/min, so wait >= 30s before retry).
    """
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = rate_limit_sleep * (i + 1)
                print(f"[429] rate-limited, sleeping {wait:.0f}s (attempt {i+1}/{attempts})", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code >= 500:
                time.sleep(sleep * (2 ** i))
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(sleep * (2 ** i))
    raise RuntimeError(f"GET {url}: {last_err}")


# ─── CoinGecko batch fetch ──────────────────────────────────────────────
def fetch_coingecko_markets(cg_ids):
    """Returns dict cg_id -> market data."""
    out = {}
    # CoinGecko API free : per_page max 250, but we use 80 per batch to be safe
    batch_size = 80
    for i in range(0, len(cg_ids), batch_size):
        batch = cg_ids[i:i+batch_size]
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            + "?vs_currency=usd"
            + "&ids=" + urllib.parse.quote(",".join(batch))
            + f"&per_page={batch_size}&page=1"
            + "&price_change_percentage=7d,30d,1y"
        )
        try:
            data = _http_get_json(url, timeout=30)
        except Exception as e:
            print(f"[warn] CG batch #{i//batch_size}: {e}", file=sys.stderr)
            continue
        for row in data or []:
            cid = row.get("id")
            if not cid:
                continue
            out[cid] = row
        print(f"[cg] batch {i//batch_size + 1}: got {len(data or [])} rows")
        time.sleep(5.0)  # polite rate-limit (free tier ~30/min, stay safe)
    return out


# ─── DeFiLlama TVL + Revenue ────────────────────────────────────────────
def fetch_defillama_protocols():
    """Returns list of /protocols rows (raw)."""
    try:
        return _http_get_json("https://api.llama.fi/protocols", timeout=60) or []
    except Exception as e:
        print(f"[warn] DefiLlama /protocols: {e}", file=sys.stderr)
        return []


def fetch_defillama_chains_tvl():
    """Returns (out, web_map) tuple :
       - out      : gecko_id -> chain_tvl_usd
       - web_map  : gecko_id -> DeFiLlama UI chain name (pour /chain/{X} audit link)
    """
    try:
        rows = _http_get_json("https://api.llama.fi/v2/chains", timeout=60) or []
    except Exception as e:
        print(f"[warn] DefiLlama /v2/chains: {e}", file=sys.stderr)
        return {}, {}
    out = {}
    web_map = {}
    for c in rows:
        gid = c.get("gecko_id")
        tvl = c.get("tvl") or 0
        if not gid or tvl <= 0:
            continue
        out[gid] = out.get(gid, 0) + tvl
        # On capture le name UI tel qu'utilisé par DeFiLlama (ex: "Ripple",
        # "Stellar", "Ethereum") pour permettre au frontend de construire un
        # lien /chain/{name} où l'utilisateur retrouve la TVL en headline.
        # On garde le PREMIER name vu (cas Ethereum partage gecko avec L2,
        # mais on veut /chain/Ethereum, pas /chain/Base).
        if gid not in web_map:
            web_map[gid] = c.get("name") or gid
    L2_TVL_INHERITS_ETH = {"base", "linea", "scroll", "blast", "ink"}
    for c in rows:
        slug = (c.get("name") or "").lower().replace(" ", "-")
        if slug in L2_TVL_INHERITS_ETH and not c.get("gecko_id"):
            tvl = c.get("tvl") or 0
            if tvl > 0:
                out["ethereum"] = out.get("ethereum", 0) + tvl
    n = sum(1 for v in out.values() if v > 1e8)
    print(f"[info] chain TVL captured for {n} chains (>$0.1B)", file=sys.stderr)
    return out, web_map


def build_tvl_map(protocols_rows):
    """gecko_id -> (total_tvl_usd, primary_slug).

    DefiLlama découpe chaque protocole en plusieurs slugs (uniswap-v3, uniswap-v2,
    aave-v3, aave-v2, etc.) et le `gecko_id` n'est rempli QUE sur certaines
    versions — d'où le bug où UNI/AAVE/CRV/MKR/PANCAKE n'avaient pas de TVL alors
    qu'ils en ont massivement.

    Fix : on agrège les TVL de TOUTES les versions qui partagent le même
    `parentProtocol` ou le même `gecko_id`. Pour la majorité des protocoles, le
    champ `parentProtocol` (ex : "parent#aave") regroupe les versions.

    Stratégie en 3 passes :
    1. Index direct par gecko_id : prends le TVL le + élevé par gecko_id
    2. Pour les protocoles avec parentProtocol mais pas de gecko_id : agrège
       sous le gecko_id du "parent" (déterminé par d'autres slugs du même parent)
    3. Heuristique slug → gecko_id pour les top protocoles non matchés

    Filtre catégorie : on EXCLUT
      - "CEX" : les exchanges centralisés (OKX/Gate/Bitkub avec gecko_id pollue
        Exchange Tokens avec $25B+ de réserves CEX qui ne sont PAS de la TVL DeFi)
      - "Chain" : la TVL de la chaîne elle-même, déjà couverte par ses protocoles
    """
    # Catégories à EXCLURE du calcul TVL (pas de la "Total Value Locked" DeFi).
    EXCLUDED_CATEGORIES = {"CEX", "Chain"}

    # ── Heuristique slug → gecko_id pour les grands protocoles connus ──
    # Utilisée UNIQUEMENT si aucun matching via direct gecko_id ni via parent.
    # NB: éviter le double counting — un slug qui a été matché par parent ne
    # DOIT PAS être re-matché ici (sinon AAVE V3 → +14B compté 2× → 28B au lieu de 14B).
    SLUG_TO_GECKO = {
        # token-symbol → gecko_id (CoinGecko canonical id)
        "uniswap": "uniswap",
        "uniswap-labs": "uniswap",
        "uniswap-v2": "uniswap",
        "uniswap-v3": "uniswap",
        "uniswap-v4": "uniswap",
        "aave": "aave",
        "aave-v1": "aave",
        "aave-v2": "aave",
        "aave-v3": "aave",
        "compound": "compound-governance-token",
        "compound-v3": "compound-governance-token",
        "compound-finance": "compound-governance-token",
        "curve": "curve-dao-token",
        "curve-finance": "curve-dao-token",
        "curve-dex": "curve-dao-token",
        "pancakeswap-amm": "pancakeswap-token",
        "pancakeswap": "pancakeswap-token",
        "pancakeswap-amm-v3": "pancakeswap-token",
        "makerdao": "maker",
        "sky-lending": "sky",
        "sky": "sky",
        "morpho-blue": "morpho",
        "morpho-aave-v3": "morpho",
        "aerodrome-slipstream": "aerodrome-finance",
        "aerodrome-v1": "aerodrome-finance",
        "raydium": "raydium",
        "raydium-amm": "raydium",
        "raydium-clmm": "raydium",
        "jupiter": "jupiter-exchange-solana",
        "jupiter-aggregator": "jupiter-exchange-solana",
        "balancer": "balancer",
        "balancer-v2": "balancer",
        "balancer-v3": "balancer",
        "1inch-aggregation-protocol": "1inch",
        "1inch-network": "1inch",
        "ethena": "ethena",
        "ethena-usde": "ethena",
        "frax-finance": "frax-share",
        "frax-ether": "frax-share",
        "rocket-pool": "rocket-pool",
        "lido": "lido-dao",
        "convex-finance": "convex-finance",
        # Ajouts 2026-05-21 (audit) : protocoles top mcap sans gecko_id ni parent
        # OU avec un gecko_id sur un slug minoritaire (perte d'agrégation).
        "jito": "jito-governance-token",
        "jito-restaking": "jito-governance-token",
        "pyth-network": "pyth-network",
        "chainlink-ccip": "chainlink",
        "chainlink-data-feeds": "chainlink",
        "chainlink": "chainlink",
        "frax": "frax-share",
        "frax-finance-staked-frax": "frax-share",
        "liquity": "liquity",
        "liquity-v2": "liquity",
        "ondo-finance": "ondo-finance",
        "ondo-flux-finance": "ondo-finance",
    }

    # ── Pass 1 : index parentProtocol → gecko_id (depuis les versions qui en ont) ──
    parent_to_gecko = {}
    for p in protocols_rows:
        parent = p.get("parentProtocol")
        gid = p.get("gecko_id")
        if parent and gid and parent not in parent_to_gecko:
            parent_to_gecko[parent] = gid

    # Override manuel pour les parents dont AUCUNE version n'a de gecko_id
    # renseigné dans /protocols (sinon ils tombent dans la heuristique slug,
    # qui ne couvre pas tous les slugs leaf comme `jito-liquid-staking`).
    # Audit complet 2026-05-21 : on a parcouru toutes les revenue > $5M/an
    # côté DefiLlama et ajouté les parents qui MAPPENT à des tokens présents
    # dans notre taxonomie (CAKE, AERO, ETHFI, DRIFT, GMX, PUMP, BONK, etc.).
    MANUAL_PARENT_TO_GECKO = {
        # Liquid staking / restaking
        "parent#jito":          "jito-governance-token",
        "parent#ether-fi":      "ether-fi",
        "parent#stader":        "stader-labs",
        # Oracles
        "parent#pyth":          "pyth-network",
        "parent#chainlink":     "chainlink",
        # DEXs majeurs (revenue $100M+/an perdue sans ces mappings)
        "parent#pancakeswap":   "pancakeswap-token",
        "parent#aerodrome":     "aerodrome-finance",
        "parent#raydium":       "raydium",
        "parent#meteora":       "meteora",
        "parent#fluid":         "instadapp",
        # Perp DEX
        "parent#drift":         "drift-protocol",
        "parent#gmx":           "gmx",
        "parent#lighter":       "lighter",
        # Memecoin launchpads / trading
        "parent#pump":          "pump-fun",
        "parent#bonkfun":       "bonk",
        # Stablecoin issuers / CDP
        "parent#sky":           "sky",
        "parent#maker":         "sky",
        "parent#ena":           "ethena",
        "parent#ethena":        "ethena",
        # RWA
        "parent#ondo-finance":  "ondo-finance",
        # Perp DEX onchain
        "parent#hyperliquid":   "hyperliquid",
    }
    for parent, gid in MANUAL_PARENT_TO_GECKO.items():
        if parent not in parent_to_gecko:
            parent_to_gecko[parent] = gid

    # ── Pass unique : résolution gecko_id par ligne avec priorité claire ──
    # Pour CHAQUE row on détermine UN seul gecko_id final (ou skip), puis on
    # somme. Cela évite tout double-counting (cf. bug AAVE compté 2× via Pass 2
    # + Pass 3 dans la version précédente).
    # Priorité de résolution : (1) gecko_id direct, (2) parent#X → gecko_id,
    # (3) slug map manuel, (4) name lower-case match avec un autre row de même
    # parent. La priorité (4) couvre les cas où le parent a gecko_id mais une
    # version isolée n'en a pas et n'est pas dans le slug map.
    aggregated = {}  # gecko_id -> {'tvl': total, 'slug': primary, '_max_slug_tvl': float}
    for p in protocols_rows:
        # Skip protocoles non-DeFi qui pollueraient les agrégats sectoriels.
        cat = p.get("category") or ""
        if cat in EXCLUDED_CATEGORIES:
            continue

        gid = p.get("gecko_id")
        if not gid:
            parent = p.get("parentProtocol")
            if parent and parent in parent_to_gecko:
                gid = parent_to_gecko[parent]
        if not gid:
            slug = (p.get("slug") or "").lower()
            gid = SLUG_TO_GECKO.get(slug)
        if not gid:
            continue

        tvl = p.get("tvl") or 0
        if tvl <= 0:
            continue

        if gid not in aggregated:
            aggregated[gid] = {"tvl": 0.0, "slug": p.get("slug"), "_max_slug_tvl": 0}
        aggregated[gid]["tvl"] += tvl
        if tvl > aggregated[gid]["_max_slug_tvl"]:
            aggregated[gid]["_max_slug_tvl"] = tvl
            aggregated[gid]["slug"] = p.get("slug")

    # Convert to legacy format (gecko_id -> (tvl, slug))
    out = {}
    for gid, info in aggregated.items():
        out[gid] = (info["tvl"], info["slug"])
    return out


def build_dlid_to_gecko(protocols_rows):
    """defillama_id (str) -> gecko_id, en appliquant la MÊME logique de
    résolution que build_tvl_map : direct → parent → slug map manuel.

    Pourquoi : le précédent code ne mappait que les ~30% de protocoles
    DefiLlama qui ont un `gecko_id` direct. AAVE V3 (~$125M revenue/an)
    a gecko_id=None — donc il était IGNORÉ — alors qu'AAVE V2 ($1.24M/an)
    était capturé. Résultat : on rapportait AAVE rev=$1.24M au lieu de
    ~$125M. Même bug pour HYPE ($941M perdu), SKY ($250M), CAKE, UNI…
    """
    # Categories à exclure du calcul revenue (cf. build_tvl_map)
    EXCLUDED_CATEGORIES = {"CEX", "Chain"}

    # Manual parent → gecko (même liste exhaustive que build_tvl_map).
    MANUAL_PARENT_TO_GECKO = {
        "parent#jito":          "jito-governance-token",
        "parent#ether-fi":      "ether-fi",
        "parent#stader":        "stader-labs",
        "parent#pyth":          "pyth-network",
        "parent#chainlink":     "chainlink",
        "parent#pancakeswap":   "pancakeswap-token",
        "parent#aerodrome":     "aerodrome-finance",
        "parent#raydium":       "raydium",
        "parent#meteora":       "meteora",
        "parent#fluid":         "instadapp",
        "parent#drift":         "drift-protocol",
        "parent#gmx":           "gmx",
        "parent#lighter":       "lighter",
        "parent#pump":          "pump-fun",
        "parent#bonkfun":       "bonk",
        "parent#sky":           "sky",
        "parent#maker":         "sky",
        "parent#ena":           "ethena",
        "parent#ethena":        "ethena",
        "parent#ondo-finance":  "ondo-finance",
        "parent#hyperliquid":   "hyperliquid",
    }
    SLUG_TO_GECKO = {
        "uniswap":"uniswap","uniswap-labs":"uniswap","uniswap-v2":"uniswap","uniswap-v3":"uniswap","uniswap-v4":"uniswap",
        "aave":"aave","aave-v1":"aave","aave-v2":"aave","aave-v3":"aave",
        "compound":"compound-governance-token","compound-v3":"compound-governance-token","compound-finance":"compound-governance-token",
        "curve":"curve-dao-token","curve-finance":"curve-dao-token","curve-dex":"curve-dao-token",
        "pancakeswap-amm":"pancakeswap-token","pancakeswap":"pancakeswap-token","pancakeswap-amm-v3":"pancakeswap-token",
        "morpho-blue":"morpho","morpho-aave-v3":"morpho",
        "aerodrome-slipstream":"aerodrome-finance","aerodrome-v1":"aerodrome-finance",
        "raydium":"raydium","raydium-amm":"raydium","raydium-clmm":"raydium",
        "jupiter":"jupiter-exchange-solana","jupiter-aggregator":"jupiter-exchange-solana",
        "balancer":"balancer","balancer-v2":"balancer","balancer-v3":"balancer",
        "1inch-aggregation-protocol":"1inch","1inch-network":"1inch",
        "ethena":"ethena","ethena-usde":"ethena",
        "frax-finance":"frax-share","frax-ether":"frax-share","frax":"frax-share","frax-finance-staked-frax":"frax-share",
        "rocket-pool":"rocket-pool","lido":"lido-dao","convex-finance":"convex-finance",
        "jito":"jito-governance-token","jito-restaking":"jito-governance-token","jito-liquid-staking":"jito-governance-token",
        "pyth-network":"pyth-network","chainlink-ccip":"chainlink","chainlink-data-feeds":"chainlink","chainlink":"chainlink",
        "liquity":"liquity","liquity-v2":"liquity","ondo-finance":"ondo-finance","ondo-flux-finance":"ondo-finance",
        "sky-lending":"sky","sky":"sky","sky-money":"sky","sky-rwa":"sky","makerdao":"sky",
    }

    # Build parent→gecko from versions with gecko_id, then merge manual
    parent_to_gecko = {}
    for p in protocols_rows:
        par = p.get("parentProtocol")
        gid = p.get("gecko_id")
        if par and gid and par not in parent_to_gecko:
            parent_to_gecko[par] = gid
    for par, gid in MANUAL_PARENT_TO_GECKO.items():
        if par not in parent_to_gecko:
            parent_to_gecko[par] = gid

    out = {}  # dlid (str) -> gecko_id
    for p in protocols_rows:
        cat = p.get("category") or ""
        if cat in EXCLUDED_CATEGORIES:
            continue
        did = p.get("id")
        if did is None:
            continue
        gid = p.get("gecko_id")
        if not gid:
            par = p.get("parentProtocol")
            if par and par in parent_to_gecko:
                gid = parent_to_gecko[par]
        if not gid:
            slug = (p.get("slug") or "").lower()
            gid = SLUG_TO_GECKO.get(slug)
        if gid:
            out[str(did)] = gid
    return out


def _agreger_bulk(data, dlid_to_gecko):
    """Somme par gecko_id le total1y de l'appel groupé /overview/fees, quel que
    soit le dataType demandé.

    Factorisé le 05/09/2026 : on interroge désormais DEUX grandeurs sur le même
    endpoint (frais payés, puis revenu gardé). Garder deux boucles jumelles,
    c'était garantir qu'un correctif futur n'atterrisse que sur l'une des deux
    et que les deux chiffres cessent d'être comparables sans que personne le voie.
    """
    out, sources = {}, {}
    for p in ((data or {}).get("protocols") or []):
        did = str(p.get("defillamaId") or "")
        # Entrée chain-level : l'appel groupé renvoie un total1y de chaîne
        # PARTIEL/FAUX (ETH 129M, SOL 38M, BNB 22M) très inférieur à la réalité.
        # On l'IGNORE ici ; l'endpoint dédié /overview/fees/{slug} plus bas
        # donne la vraie valeur (ETH 1156M, SOL 1706M, BNB 345M).
        if did.startswith("chain#") or p.get("category") == "Chain":
            continue
        if not did:
            continue
        gid = dlid_to_gecko.get(did)
        if not gid:
            continue
        rev = p.get("total1y")
        if rev is None or rev <= 0:
            r30 = p.get("total30d") or 0
            if r30 > 0:
                rev = r30 * 12
        if rev is None or rev <= 0:
            continue
        # SOMME (pas max) — chaque version d'un protocole contribue.
        out[gid] = out.get(gid, 0) + rev
        if gid not in sources:
            sources[gid] = {"kind": "protocol",
                            "slug": p.get("slug") or "",
                            "web":  p.get("slug") or ""}
    return out, sources


def _total1y_chain_native(cd, slug, autoriser_repli):
    """total1y de la CHAÎNE elle-même dans une réponse /overview/fees/{slug}.

    `autoriser_repli` : pour les frais on accepte de retomber sur le total1y de
    l'endpoint (cas Stellar/XRPL où le natif EST le seul « protocole »). Pour le
    revenu gardé on le REFUSE : ce total-là couvre tout l'écosystème déployé, et
    l'attribuer au jeton de la chaîne fabriquerait un revenu qu'elle ne touche
    pas. Une donnée absente reste absente.
    """
    native_rev = 0
    native_entry = None
    for p in ((cd or {}).get("protocols") or []):
        p_id = str(p.get("defillamaId") or "")
        if p_id == "chain#%s" % slug or (p.get("name") or "").lower() == slug.lower():
            native_rev = p.get("total1y") or 0
            if native_rev <= 0:
                r30 = p.get("total30d") or 0
                if r30 > 0:
                    native_rev = r30 * 365.0 / 30.0
            native_entry = p
            break
    if native_rev <= 0 and autoriser_repli:
        native_rev = (cd or {}).get("total1y") or 0
        if native_rev <= 0:
            r30 = (cd or {}).get("total30d") or 0
            if r30 > 0:
                native_rev = r30 * 365.0 / 30.0
    return native_rev, native_entry


def fetch_defillama_revenue(dlid_to_gecko):
    """Returns (fees_map, source_map, chain_breakdowns, revenu_net_map) per gecko_id.

    - fees_map        : gecko_id -> frais_1y_usd (ce que PAIENT les utilisateurs)
    - revenu_net_map  : gecko_id -> revenu_1y_usd (ce que GARDE le protocole)
    - source_map  : gecko_id -> {"kind": "protocol"|"chain", "slug": <api_slug>, "web": <ui_slug>}
      Permet au frontend de construire un lien d'audit qui pointe EXACTEMENT
      sur la page DeFiLlama où la valeur est affichée (chain page si revenu
      L1 type XRPL/Stellar/Ethereum, /protocol/{slug} si protocole DeFi).

    Aggregation = SOMME des revenue de toutes les versions partageant le
    même gecko_id (au lieu de max). Sans ça, AAVE V2 ($1.24M) écrasait
    AAVE V3 ($125M) → revenue rapporté = $1.24M au lieu de $126.5M.
    """
    # Mapping chain slug API → (gecko_id, DeFiLlama web slug pour /chain/{X}).
    # Le web slug est tel qu'utilisé dans l'URL UI : https://defillama.com/chain/Ripple
    # (où la section "Fees & Revenue" expose le total1y montré dans notre tableau).
    #
    # IMPORTANT méthodo P/S (fix 2026-06-04) : on n'inclut PAS les L2s sans
    # token propre (Base/Linea/Scroll/Blast) dans le revenu ETH. Les fees
    # sequencer L2 reviennent au sequencer, pas à ETH le token ; les compter
    # comme du revenu ETH = double-count + écart de 19% avec ce que DeFiLlama
    # UI / Token Terminal affichent pour Ethereum L1 seul. Convention standard.
    # (TVL inheritance est gardée séparément dans fetch_defillama_chains_tvl
    # car l'argument économique y est plus direct : capital bridgé sur ETH.)
    CHAIN_INFO = {
        "ethereum":       ("ethereum",              "Ethereum"),
        "tron":           ("tron",                  "Tron"),
        "solana":         ("solana",                "Solana"),
        "bsc":            ("binancecoin",           "BSC"),
        "polygon":        ("polygon-ecosystem-token","Polygon"),
        "avalanche":      ("avalanche-2",           "Avalanche"),
        "arbitrum":       ("arbitrum",              "Arbitrum"),
        "op-mainnet":     ("optimism",              "Optimism"),
        "sui":            ("sui",                   "Sui"),
        "ton":            ("the-open-network",      "TON"),
        "near":           ("near",                  "Near"),
        "icp":            ("internet-computer",     "ICP"),
        "injective":      ("injective-protocol",    "Injective"),
        "filecoin":       ("filecoin",              "Filecoin"),
        "starknet":       ("starknet",              "Starknet"),
        "stellar":        ("stellar",               "Stellar"),
        "xrpl":           ("ripple",                "Ripple"),
        "canton":         ("canton-network",        "Canton"),
        "hyperliquid-l1": ("hyperliquid",           "Hyperliquid L1"),
        "pulsechain":     ("pulsechain",            "PulseChain"),
        "hedera":         ("hedera-hashgraph",      "Hedera"),
        "algorand":       ("algorand",              "Algorand"),
        "cosmos":         ("cosmos",                "Cosmos"),
        "polkadot":       ("polkadot",              "Polkadot"),
        "cardano":        ("cardano",               "Cardano"),
    }
    CHAIN_SLUG_TO_GECKO = {slug: info[0] for slug, info in CHAIN_INFO.items()}

    BASE = ("https://api.llama.fi/overview/fees?excludeTotalDataChart=true"
            "&excludeTotalDataChartBreakdown=true&dataType=")
    try:
        data = _http_get_json(BASE + "dailyFees", timeout=60)
    except Exception as e:
        # ⚠ NE PAS « PUBLIER SANS FRAIS ». Une première version rendait ici
        # quatre dictionnaires vides pour corriger une erreur d'arité — et
        # transformait un plantage qui PRÉSERVAIT le cache en une dégradation
        # silencieuse qui l'ÉCRASE. Panne simulée en relecture : le collecteur
        # continuait, sortait en code 0, et publiait un cache où 19 narratifs
        # sur 25 perdaient leur prix/frais ; Ethereum passait de 1 138,9× à
        # 246,1× parce que le dénominateur ne gardait que la part actions.
        # Le contrôle de cohérence, lui, déclarait le tout bon : les deux
        # grandeurs restaient cohérentes ENTRE ELLES, toutes les deux fausses.
        #
        # Les frais sont le dénominateur de la grandeur centrale de ce cache.
        # Sans eux, il n'y a pas de collecte partielle, il y a une collecte
        # ratée — et un échec franc laisse en place le cache de la veille, qui
        # est juste. C'est exactement ce que la garde de témoins du dépôt
        # protège ailleurs ; ici, on ne lui donne même pas l'occasion.
        raise RuntimeError(
            "DefiLlama /overview/fees (dailyFees) muet : %s. On n'écrit RIEN — "
            "publier un cache sans frais remplacerait le prix/frais de 19 "
            "narratifs sur 25 par un rapport calculé sur les seules actions, "
            "sans qu'aucun contrôle ne s'en aperçoive." % e)
    out, sources = _agreger_bulk(data, dlid_to_gecko)
    chain_count = 0

    # ── Le multiple s'appelait « P/S » mais son dénominateur est des FRAIS ──
    # dailyFees = ce que paient les utilisateurs ; dailyRevenue = ce que garde
    # le protocole (le reste va aux fournisseurs de liquidité, aux validateurs,
    # ou est brûlé). Les deux diffèrent d'un facteur 3,1 sur la chaîne Ethereum
    # (214,7 M$ payés contre 69,5 M$ gardés, mesuré le 05/09/2026). On collecte
    # donc les deux et on publie les deux : le rapport garde les frais comme
    # dénominateur — c'est ce qui se compare au chiffre d'affaires d'une action —
    # mais le revenu gardé s'affiche à côté, sous son propre nom.
    # Coût mesuré : 3,8 Mo, 0,4 s pour l'appel groupé.
    revenu_net = {}
    # Les gecko_id vus comme CHAÎNE, quelle que soit la valeur retenue.
    chaines_vues = set()
    try:
        data_net = _http_get_json(BASE + "dailyRevenue", timeout=60)
        revenu_net, _ = _agreger_bulk(data_net, dlid_to_gecko)
    except Exception as e:
        # Pas de repli inventé : sans cet appel, le revenu gardé est simplement
        # absent et le front-end l'affichera absent.
        print(f"[warn] DefiLlama /overview/fees (dailyRevenue): {e} — revenu gardé absent",
              file=sys.stderr)

    # ── Revenu CHAIN NATIVE UNIQUEMENT (fix méthodo 2026-06-04) ────────
    # Pour un L1 (ETH/SOL/BNB/TRX/XRP/XLM/BTC/LTC…), seul le revenu de la
    # chain elle-même est attribuable au token. Les fees des protocoles
    # déployés (Aave, Uniswap, Jupiter…) reviennent aux holders de leurs
    # propres tokens, pas à ETH/SOL. Avant on sommait chain.total1y (=
    # écosystème entier) ce qui surestimait massivement (ETH 5.82B vs
    # 318M réels chain native).
    #
    # Méthode : pour chaque slug, on cherche dans protocols[] l'entrée
    # defillamaId="chain#{slug}" (le chain native) et on prend SON total1y.
    chain_rev = {}        # gecko_id -> frais $ (chain native uniquement)
    chain_primary = {}    # gecko_id -> (api_slug, web_name) du chain qui contribue
    chain_breakdowns = {} # gecko_id -> [{name, slug, kind:'chain', rev_usd, url}]
    chain_net = {}        # gecko_id -> revenu gardé $ (chain native uniquement)
    for slug, (gid, web_name) in CHAIN_INFO.items():
        BASE_CHAIN = (f"https://api.llama.fi/overview/fees/{slug}"
                      "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
                      "&dataType=")
        try:
            cd = _http_get_json(BASE_CHAIN + "dailyFees", timeout=40)
        except Exception:
            cd = None
        if not cd:
            continue
        native_rev, native_entry = _total1y_chain_native(cd, slug, autoriser_repli=True)
        # Second appel, même chaîne : le revenu gardé. Coût mesuré ~0,3 s par
        # chaîne, 25 chaînes. On le fait ici plutôt que dans une boucle séparée
        # pour que les deux grandeurs décrivent le MÊME instant de collecte.
        try:
            cn = _http_get_json(BASE_CHAIN + "dailyRevenue", timeout=40)
            net_rev, _ = _total1y_chain_native(cn, slug, autoriser_repli=False)
            # ⚠ ZÉRO GARDÉ EST UN FAIT, PAS UNE ABSENCE. On enregistre la
            # chaîne même à zéro : c'est ce qui permet, plus bas, d'écraser
            # l'agrégat des PROTOCOLES par la valeur native. Sans cette ligne,
            # Hyperliquid — dont la chaîne ne garde rien en natif — publiait
            # les 712 M$ de son écosystème comme revenu gardé de HYPE.
            chain_net[gid] = chain_net.get(gid, 0.0) + net_rev
            chaines_vues.add(gid)
        except Exception as e:
            # L'appel groupé équivalent journalise, celui-ci se taisait : une
            # chaîne dont le revenu gardé échoue affichait « — » sans jamais
            # dire pourquoi, et l'absence se lisait comme un zéro mesuré.
            print(f"[warn] revenu gardé de la chaîne {slug} : {e}", file=sys.stderr)
        if native_rev > 0:
            chain_rev[gid] = chain_rev.get(gid, 0) + native_rev
            if gid not in chain_primary:
                chain_primary[gid] = (slug, web_name)
            chain_count += 1
            # Breakdown réduit à une seule ligne : le chain native lui-même.
            # C'est volontaire — on ne veut PAS lister les protocoles déployés
            # car leurs fees ne sont pas attribuables au token chain.
            bd = chain_breakdowns.setdefault(gid, [])
            bd.append({
                "name":    (native_entry.get("name") if native_entry else web_name) or web_name,
                "slug":    slug,
                "kind":    "chain",
                "rev_usd": int(round(native_rev)),
                "url":     f"https://defillama.com/chain/{web_name}",
            })
        time.sleep(0.3)
    for gid, rev in chain_rev.items():
        out[gid] = rev  # chain native uniquement, pas ecosystem
        slug, web = chain_primary[gid]
        sources[gid] = {"kind": "chain", "slug": slug, "web": web}
    # ⚠ LA VALEUR NATIVE ÉCRASE L'AGRÉGAT DES PROTOCOLES, MÊME QUAND ELLE VAUT
    # ZÉRO. `revenu_net` part de l'appel groupé, qui somme les PROTOCOLES ; une
    # première version ne l'écrasait que `if net_rev > 0`, si bien qu'une chaîne
    # ne gardant rien en natif conservait le total de son écosystème. Mesuré à
    # la source sur les 25 chaînes : Hyperliquid L1 garde 0,00 M$ en natif pour
    # 754,06 M$ d'écosystème, et le cache publiait 712,37 M$ pour HYPE. Le
    # commentaire promettait « chaîne native, pas écosystème » ; le code ne le
    # faisait qu'à moitié.
    for gid in chaines_vues:
        revenu_net[gid] = chain_net.get(gid, 0.0)
    if chain_count:
        print(f"[info] frais : {chain_count} chaînes (NATIVE only, pas ecosystem) — ETH/SOL/BNB/TRX/XRP réels", file=sys.stderr)
    print(f"[info] revenu gardé (dailyRevenue) : {len(revenu_net)} gecko_id, "
          f"dont {len(chain_net)} chaînes natives", file=sys.stderr)
    return out, sources, chain_breakdowns, revenu_net


# ─── Aggregation ────────────────────────────────────────────────────────
def _safe_div(a, b):
    if not b:
        return None
    try:
        return a / b
    except Exception:
        return None


def build_token_fund(cg_row, tvl_map, rev_map, rev_sources=None, tvl_chain_web=None,
                    rev_breakdowns=None, net_rev_map=None):
    """Build per-token fundamentals from CG + DL data.

    rev_sources     : {gecko_id -> {"kind","slug","web"}} produit par
                      fetch_defillama_revenue() pour router audit revenue/PS.
    tvl_chain_web   : {gecko_id -> DeFiLlama chain UI name} produit par
                      fetch_defillama_chains_tvl(), pour router audit TVL des
                      chains L1 natives (XRPL/Stellar/Ethereum/Solana…) vers
                      /chain/{name} où la TVL est en headline.
    rev_breakdowns  : {gecko_id -> [{name,slug,kind,rev_usd,url}…]} la
                      décomposition du revenue chain en composantes (chain
                      native + chaque protocole), permet au modal d'afficher
                      la ventilation auditable au lieu d'un chiffre opaque.
    """
    rev_sources = rev_sources or {}
    tvl_chain_web = tvl_chain_web or {}
    rev_breakdowns = rev_breakdowns or {}
    net_rev_map = net_rev_map or {}
    cid = cg_row.get("id")
    mcap = cg_row.get("market_cap") or 0
    fdv = cg_row.get("fully_diluted_valuation") or mcap
    vol = cg_row.get("total_volume") or 0
    tvl_tuple = tvl_map.get(cid)
    tvl = tvl_tuple[0] if tvl_tuple else None
    # Slug DefiLlama du protocole primaire (pour deep-link audit TVL/revenue/P-S
    # vers defillama.com/protocol/<slug>). "chain" = placeholder L1 sans page
    # protocole → on laisse None (le front-end retombe sur CoinGecko).
    dl_slug = tvl_tuple[1] if tvl_tuple else None
    if dl_slug in (None, "", "chain"):
        dl_slug = None
    rev = rev_map.get(cid)
    # Revenu GARDÉ par le protocole (dailyRevenue), distinct des frais PAYÉS
    # par les utilisateurs (dailyFees) qui restent le dénominateur du rapport.
    revenu_net = net_rev_map.get(cid)
    rev_src = rev_sources.get(cid) if rev else None
    # Plus de seuil revenue<$100k (consigne 2026-06-03 : laisser passer toutes
    # les valeurs même petites). Le frontend les affichera telles quelles.
    circ_pct = None
    if mcap and fdv and fdv > 0:
        circ_pct = (mcap / fdv) * 100
    vol_mcap_pct = None
    if mcap and mcap > 0:
        vol_mcap_pct = (vol / mcap) * 100
    mc_tvl = None
    if tvl and tvl > 0 and mcap and mcap > 0:
        mc_tvl = mcap / tvl
        # Plus de cap 20x : on garde le ratio brut, l'utilisateur juge.
    ps_ttm = None
    if rev and rev > 0 and mcap and mcap > 0:
        ps_ttm = mcap / rev
    return {
        "id": cid,
        "symbol": (cg_row.get("symbol") or "").upper(),
        "name": cg_row.get("name") or cid,
        "image": cg_row.get("image"),
        "dl_slug": dl_slug,
        # Source d'audit du revenue : protocol|chain + slugs DeFiLlama
        #  - rev_source_slug = slug API (utilisé pour construire l'URL API JSON
        #    qui affiche `total1y` exactement = la valeur cachée)
        #  - rev_source_web  = slug UI (utilisé pour les fallbacks UI)
        "rev_source_kind": rev_src["kind"] if rev_src else None,
        "rev_source_slug": rev_src["slug"] if rev_src else None,
        "rev_source_web":  rev_src["web"]  if rev_src else None,
        # Décomposition du revenue par composante (chain native + protocoles
        # déployés). Le modal l'affiche en tableau auditable où chaque ligne
        # = un lien vers la page DeFiLlama UI où la valeur est lisible.
        "rev_breakdown":   rev_breakdowns.get(cid) if rev else None,
        # Si la TVL vient de la chain L1 native (pas d'un protocole DeFi mappé),
        # on expose le nom UI DeFiLlama pour router /chain/{name} sur la cellule TVL.
        "tvl_chain_web":   tvl_chain_web.get(cid) if (tvl and not dl_slug) else None,
        "price": cg_row.get("current_price"),
        "mcap_b": round((mcap or 0) / 1e9, 3),
        "fdv_b":  round((fdv or 0) / 1e9, 3),
        "vol_b":  round((vol or 0) / 1e9, 3),
        "circ_pct": round(circ_pct, 1) if circ_pct is not None else None,
        "vol_mcap_pct": round(vol_mcap_pct, 2) if vol_mcap_pct is not None else None,
        "tvl_b": round((tvl or 0) / 1e9, 3) if tvl else None,
        "mc_tvl": round(mc_tvl, 2) if mc_tvl is not None else None,
        # rev_m_1y = FRAIS payés par les utilisateurs (dénominateur du rapport).
        "rev_m_1y": round(rev / 1e6, 2) if rev else None,
        # revenu_m_1y = ce que le protocole GARDE. Absent si DeFiLlama ne le
        # publie pas pour ce gecko_id — absent, pas replié sur les frais.
        "revenu_m_1y": round(revenu_net / 1e6, 2) if revenu_net else None,
        "ps_ttm": round(ps_ttm, 2) if ps_ttm is not None else None,
        "perf_7d":  cg_row.get("price_change_percentage_7d_in_currency"),
        "perf_30d": cg_row.get("price_change_percentage_30d_in_currency"),
        "perf_1y":  cg_row.get("price_change_percentage_1y_in_currency"),
    }


_WEIGHTED_METRICS = [
    "circ_pct", "vol_mcap_pct", "mc_tvl", "ps_ttm",
    "perf_7d", "perf_30d", "perf_1y",
]

# Winsorisation : on clippe aux p5/p95 (unweighted, sur les tokens du narrative)
# pour éviter qu'un single small cap dégueule la moyenne pondérée
# (ex: ZK/Privacy perf_1y=+802% tiré par un token + des autres -50%).
_WINSOR_PERCENTILES = (5, 95)
_WINSOR_PERF_METRICS = {"perf_7d", "perf_30d", "perf_1y"}


def _percentile(values_sorted, p):
    if not values_sorted:
        return None
    if len(values_sorted) == 1:
        return values_sorted[0]
    k = (len(values_sorted) - 1) * (p / 100.0)
    lo = int(k); hi = min(lo + 1, len(values_sorted) - 1)
    return values_sorted[lo] * (1 - (k - lo)) + values_sorted[hi] * (k - lo)


def aggregate_narrative(tokens):
    """Aggregate token-level fundamentals into sector-level metrics.

    Strategie : on sépare le panier en CRYPTO et STOCKS. Les métriques universelles
    (mcap, vol, perf) sont agrégées sur le panier complet. Les métriques crypto-
    spécifiques (circ/fdv, mc/tvl, p/s ttm) sont calculées UNIQUEMENT sur le sous-
    ensemble crypto — sinon les actions polluent les ratios (ex: stocks ont fdv=mcap
    par construction → tirent circ/fdv à 100% artificiellement).

    Améliorations 2026-05-21 (audit) :
      - Winsorisation 5/95 sur perf_7d/30d/1y (single-token blow-ups clippés)
      - Médiane + min + max + n exposés en `{metric}_median/_min/_max/_n`
      - Outliers loggés dans `outliers_clipped` (token, métrique, raw → clipped)
    """
    if not tokens:
        return {}
    # Drop "defunct" tokens : mcap=0 AND fdv≈0 (CoinGecko returns them as
    # zombie entries — e.g. MKR, MC, LOOKS, RGT, AZUR after deprecation).
    # They show up in modals as "—" everywhere and confuse the user.
    tokens = [t for t in tokens
              if (t.get("mcap_b") or 0) > 0
              or (t.get("fdv_b") or 0) > 0.01
              or t.get("is_stock")]
    crypto = [t for t in tokens if not t.get("is_stock")]
    stocks = [t for t in tokens if t.get("is_stock")]

    # ── Universal aggregates (full basket: crypto + stocks) ──
    mcap_total = sum((t.get("mcap_b") or 0) for t in tokens)
    vol_total  = sum((t.get("vol_b")  or 0) for t in tokens)

    # ── Crypto-only aggregates (for crypto-specific ratios) ──
    cmcap = sum((t.get("mcap_b") or 0) for t in crypto)
    cfdv  = sum((t.get("fdv_b")  or 0) for t in crypto)
    ctvl  = sum((t.get("tvl_b")  or 0) for t in crypto if t.get("tvl_b"))

    # FDV total affiché = crypto fdv + stocks mcap (les actions sont 100% en
    # circulation par construction → fdv = mcap pour elles). Permet d'avoir un
    # mcap_total ≤ fdv_total cohérent.
    fdv_total = cfdv + sum((t.get("mcap_b") or 0) for t in stocks)

    # ── Le dénominateur du rapport, calculé UNE SEULE FOIS ──────────────
    # Le revenu publié ne sommait que les jetons crypto, tandis que le rapport
    # publié divisait par le panier COMPLET, actions comprises. Mesuré le
    # 05/09/2026 sur le cache en ligne : 5 narratifs sur 25 divergeaient, et
    # « Mineurs de bitcoin » affichait « — » en revenus à côté de 15,6× en
    # prix/revenus, alors que son dénominateur réel valait 4 590,8 M$. Depuis
    # qu'une colonne « Revenus » figure au tableau, l'incohérence était sous les
    # yeux du lecteur. Une seule boucle produit désormais le total ET ses deux
    # moitiés : elles ne peuvent plus diverger.
    #
    # Les deux moitiés restent SÉPARÉES parce que ce ne sont pas les mêmes
    # revenus : les frais on-chain d'un protocole ne sont pas le chiffre
    # d'affaires comptable d'une action cotée. Les additionner sans le dire
    # serait un autre défaut ; ps_ttm_denominateur dit lequel domine.
    mcap_with_rev_full = 0.0
    rev_crypto_m = 0.0
    rev_actions_m = 0.0
    n_with_rev = 0
    for t in tokens:
        m = t.get("mcap_b") or 0
        if m <= 0:
            continue
        if t.get("is_stock"):
            rev_m_t = t.get("_stock_revenue_m")
            # Garde anti reventes brutes : une action dont le chiffre d'affaires
            # Yahoo dépasse 3× sa capitalisation (Galaxy Digital, ventes crypto
            # comptabilisées brutes) n'est pas comparable — on l'exclut.
            if rev_m_t and rev_m_t > 3 * m * 1000:
                rev_m_t = None
            if rev_m_t and rev_m_t > 0:
                rev_actions_m += rev_m_t
                mcap_with_rev_full += m
                n_with_rev += 1
        else:
            rev_m_t = t.get("rev_m_1y")
            if rev_m_t and rev_m_t > 0:
                rev_crypto_m += rev_m_t
                mcap_with_rev_full += m
                n_with_rev += 1
    revenue_total_m = rev_crypto_m + rev_actions_m

    # Ce que les protocoles GARDENT, à côté de ce que les utilisateurs PAIENT.
    # Crypto seulement : une action n'a pas de « revenu gardé » on-chain.
    revenu_net_m = sum((t.get("revenu_m_1y") or 0) for t in crypto
                       if (t.get("revenu_m_1y") or 0) > 0)

    out = {
        "mcap_total_b": round(mcap_total, 2),
        "fdv_total_b":  round(fdv_total, 2),
        "vol_total_b":  round(vol_total, 3),
        "tvl_total_b":  round(ctvl, 3) if ctvl else None,
        # Ce champ EST le dénominateur de ps_ttm ci-dessous. Le contrôle
        # test_narratifs_coherence.py échoue si les deux se remettent à diverger.
        "rev_m_1y_total":   round(revenue_total_m, 2) if revenue_total_m > 0 else None,
        "rev_m_1y_crypto":  round(rev_crypto_m, 2) if rev_crypto_m > 0 else None,
        "rev_m_1y_actions": round(rev_actions_m, 2) if rev_actions_m > 0 else None,
        "revenu_m_1y_total": round(revenu_net_m, 2) if revenu_net_m > 0 else None,
    }

    # circ_fdv_pct : CRYPTO-ONLY car les actions sont 100% par définition (toutes
    # les shares outstanding sont en circulation). Inclure les stocks tirerait la
    # ratio à 100% artificiellement et masquerait les unlocks à venir crypto.
    if crypto and cfdv > 0 and cmcap > 0:
        out["circ_fdv_pct"] = round(min(cmcap / cfdv * 100, 100.0), 1)
    else:
        out["circ_fdv_pct"] = None  # narratifs stocks-only (Bitcoin Miners, Web3 Exchanges)

    # vol/mcap : OK sur le panier complet (les deux scalent avec stocks)
    out["vol_mcap_pct"]  = round((vol_total / mcap_total * 100), 2) if mcap_total > 0 else None

    # MC/TVL : ratio brut Σmcap_with_tvl / ΣTVL, sans aucun seuil (consigne
    # 2026-06-03 : afficher toute la data). On expose la couverture mcap dans
    # le tooltip pour que l'utilisateur juge la représentativité — pas de
    # masquage, pas de cap, pas de minimum de tokens.
    cmcap_with_tvl = sum((t.get("mcap_b") or 0) for t in crypto if (t.get("tvl_b") or 0) > 0)
    coverage_tvl = (cmcap_with_tvl / cmcap) if cmcap > 0 else 0
    if ctvl and ctvl > 0 and cmcap_with_tvl > 0:
        out["mc_tvl"] = round(cmcap_with_tvl / ctvl, 2)
        out["mc_tvl_coverage_mcap_pct"] = round(coverage_tvl * 100, 1)
    else:
        out["mc_tvl"] = None
        out["mc_tvl_coverage_mcap_pct"] = round(coverage_tvl * 100, 1) if cmcap else 0

    # Statut du rapport capitalisation/valeur verrouillée. Mesuré le 05/09/2026 :
    # « Bitcoin institutionnel » publiait 369×, « Jetons de paiement » 349×. Le
    # seuil et sa justification sont en tête de fichier (SEUIL_ABSURDITE_MC_TVL).
    # « Sans objet » est réservé au cas structurel : un panier composé
    # uniquement d'actions cotées n'a aucune valeur verrouillée à mesurer, ce
    # n'est pas une donnée manquante, c'est une grandeur qui ne s'applique pas.
    _sans_objet_tvl = None
    if not crypto and stocks:
        _sans_objet_tvl = ("Panier composé uniquement d'actions cotées : une action "
                           "n'a pas de valeur verrouillée sur une chaîne.")
    out["mc_tvl_statut"], out["mc_tvl_motif"] = _statut_et_motif(
        out["mc_tvl"], SEUIL_ABSURDITE_MC_TVL,
        sum(1 for t in crypto if (t.get("tvl_b") or 0) > 0), len(crypto),
        out["mc_tvl_coverage_mcap_pct"],
        ("la valeur verrouillée sur la chaîne",
         "une valeur verrouillée sur la chaîne",
         "de valeur verrouillée sur la chaîne"),
        motif_sans_objet=_sans_objet_tvl)
    # Prix/frais : Σmcap_des_constituants_avec_dénominateur / Σdénominateur, sur
    # le PANIER COMPLET (crypto + actions). Sans les actions, « Places d'échange
    # et fintech » (100 % HOOD/COIN/XYZ/GLXY) affichait « — » alors que les
    # quatre publient leur chiffre d'affaires. Idem « Mineurs de bitcoin ».
    # Le numérateur et le dénominateur viennent de la MÊME boucle, plus haut.
    coverage_rev_full = (mcap_with_rev_full / mcap_total) if mcap_total > 0 else 0
    n_total_basket = len(tokens)
    out["ps_ttm_n_tokens"] = n_with_rev
    out["ps_ttm_n_total"] = n_total_basket
    if revenue_total_m > 0 and mcap_with_rev_full > 0:
        out["ps_ttm"] = round(mcap_with_rev_full * 1000 / revenue_total_m, 1)
        out["ps_ttm_coverage_mcap_pct"] = round(coverage_rev_full * 100, 1)
    else:
        out["ps_ttm"] = None
        out["ps_ttm_coverage_mcap_pct"] = round(coverage_rev_full * 100, 1) if mcap_total else 0

    # Nature du dénominateur : le multiple s'appelait « P/S » alors que sa part
    # crypto est constituée de FRAIS — ce que paient les utilisateurs — et non
    # d'un chiffre d'affaires. Les deux ne se comparent pas ; un panier mixte
    # doit le dire au lecteur au lieu de les fondre sous un seul mot.
    if revenue_total_m <= 0:
        out["ps_ttm_denominateur"] = None
    elif rev_actions_m >= 0.95 * revenue_total_m:
        out["ps_ttm_denominateur"] = "chiffre d'affaires"
    elif rev_crypto_m >= 0.95 * revenue_total_m:
        out["ps_ttm_denominateur"] = "frais"
    else:
        out["ps_ttm_denominateur"] = "mixte"

    # Statut du rapport prix/frais. Mesuré le 05/09/2026 : « Jetons de paiement »
    # publiait 126 256×, parce que XRP déclare 0,1 M$ de frais annuels pour
    # 88,4 Md$ de capitalisation. Le seuil et sa justification sont en tête de
    # fichier (SEUIL_ABSURDITE_PS_TTM). La valeur brute reste dans ps_ttm.
    # Aucun anglicisme dans ce qui part à l'écran : « on-chain » devient
    # « prélevés sur la chaîne ».
    _MIXTE = ("le total des frais de chaîne et des chiffres d'affaires publiés",
              "des frais de chaîne ou un chiffre d'affaires publié",
              "de frais de chaîne ni de chiffre d'affaires publié")
    _noms_ps = {
        "frais": ("le total des frais prélevés sur la chaîne",
                  "des frais prélevés sur la chaîne",
                  "de frais prélevés sur la chaîne"),
        "chiffre d'affaires": ("le chiffre d'affaires publié des actions du panier",
                               "un chiffre d'affaires publié",
                               "de chiffre d'affaires publié"),
        "mixte": _MIXTE,
    }.get(out["ps_ttm_denominateur"], _MIXTE)
    out["ps_ttm_statut"], out["ps_ttm_motif"] = _statut_et_motif(
        out["ps_ttm"], SEUIL_ABSURDITE_PS_TTM, n_with_rev, n_total_basket,
        out["ps_ttm_coverage_mcap_pct"], _noms_ps)

    # Mcap-weighted perf — AUCUN seuil de couverture (consigne 2026-06-03 :
    # afficher toute la data). On calcule sur les tokens disponibles, même si
    # ça ne couvre que 6 % du mcap : la couverture est exposée via *_n et le
    # tooltip dominant_pct alerte si un token domine.
    # WINSORISATION 5/95 conservée : c'est une transformation (clip outlier),
    # pas un seuil qui supprime de la data — le headline reste affiché.
    outliers_clipped = []  # {metric, symbol, raw_value, clipped_to}
    for metric in ["perf_7d", "perf_30d", "perf_1y"]:
        # Collect raw (value, weight, symbol) triples
        triples = []
        for t in tokens:
            v = t.get(metric)
            w = t.get("mcap_b") or 0
            if v is None or w <= 0:
                continue
            triples.append((v, w, t.get("symbol")))

        if not triples:
            out[metric] = None
            out[f"{metric}_median"] = None
            out[f"{metric}_min"] = None
            out[f"{metric}_max"] = None
            out[f"{metric}_n"] = 0
            continue

        # Expose audit stats (unweighted)
        vals_sorted = sorted(v for v, _, _ in triples)
        out[f"{metric}_median"] = round(_percentile(vals_sorted, 50), 2)
        out[f"{metric}_min"] = round(vals_sorted[0], 2)
        out[f"{metric}_max"] = round(vals_sorted[-1], 2)
        out[f"{metric}_n"] = len(triples)

        # Winsorize : clip extreme single-token outliers to p5/p95
        if metric in _WINSOR_PERF_METRICS and len(vals_sorted) >= 5:
            lo = _percentile(vals_sorted, _WINSOR_PERCENTILES[0])
            hi = _percentile(vals_sorted, _WINSOR_PERCENTILES[1])
            # Expose thresholds so perAuditNarrFund modal can clip per-token
            # values before computing contrib (Σ contribs = headline reproduit).
            # Sans ça même bug que côté TradFi : modal montrait raw_contrib qui
            # ne s'additionnaient pas au headline winsorisé.
            out[f"{metric}_winsor_lo"] = round(lo, 2)
            out[f"{metric}_winsor_hi"] = round(hi, 2)
            clipped = []
            for (v, w, sym) in triples:
                cv = min(max(v, lo), hi)
                if abs(cv - v) > 1e-6:
                    outliers_clipped.append({
                        "metric": metric, "symbol": sym,
                        "raw_value": round(v, 2), "clipped_to": round(cv, 2),
                    })
                clipped.append((cv, w))
        else:
            clipped = [(v, w) for v, w, _ in triples]

        num = sum(v * w for v, w in clipped)
        den = sum(w for _, w in clipped)
        out[metric] = round(num / den, 2) if den > 0 else None

    # Dominance : mcap of top token / total
    sorted_tokens = sorted(tokens, key=lambda t: -(t.get("mcap_b") or 0))
    top = sorted_tokens[0] if sorted_tokens else None
    out["dominant_sym"] = top.get("symbol") if top else None
    out["dominant_name"] = top.get("name") if top else None
    out["dominant_pct"] = round((top.get("mcap_b") / mcap_total * 100), 1) if top and mcap_total > 0 else None

    # Completeness counts (per-metric coverage — for frontend gating)
    out["n_tokens"] = len(tokens)
    out["n_with_tvl"] = sum(1 for t in tokens if t.get("tvl_b"))
    out["n_with_rev"] = sum(1 for t in tokens if t.get("rev_m_1y"))
    out["coverage"] = {
        "circ_fdv_pct": sum(1 for t in tokens if t.get("circ_pct") is not None and not t.get("is_stock")),
        "vol_mcap_pct": sum(1 for t in tokens if t.get("vol_mcap_pct") is not None),
        "mc_tvl":       out["n_with_tvl"],
        "ps_ttm":       out["n_with_rev"],
        "perf_7d":      sum(1 for t in tokens if t.get("perf_7d")  is not None),
        "perf_30d":     sum(1 for t in tokens if t.get("perf_30d") is not None),
        "perf_1y":      sum(1 for t in tokens if t.get("perf_1y")  is not None),
    }

    # Outliers clipped by winsorisation — for frontend audit modal
    out["outliers_clipped"] = outliers_clipped

    return out


def rejouer_coingecko_depuis_cache(prev_cache):
    """Reconstruit des lignes /coins/markets à partir du cache précédent.

    Banc d'essai uniquement. Rejoue les valeurs TELLES QUELLES, incohérences
    comprises : c'est justement l'entrée adverse dont on a besoin pour vérifier
    que le correctif de valeur pleinement diluée et le garde-fou d'offre
    attrapent les 22 jetons à capitalisation > valeur pleinement diluée mesurés
    sur le cache en ligne du 04/09/2026.
    """
    lignes = {}
    for n in (prev_cache.get("narratives") or []):
        for t in (n.get("tokens") or []):
            tid = t.get("id")
            if not tid or t.get("is_stock") or tid in lignes:
                continue
            lignes[tid] = {
                "id": tid,
                "symbol": (t.get("symbol") or "").lower(),
                "name": t.get("name"),
                "image": t.get("image"),
                "current_price": t.get("price"),
                "market_cap": (t.get("mcap_b") or 0) * 1e9,
                "fully_diluted_valuation": ((t.get("fdv_b") or 0) * 1e9) or None,
                "total_volume": (t.get("vol_b") or 0) * 1e9,
                "price_change_percentage_7d_in_currency":  t.get("perf_7d"),
                "price_change_percentage_30d_in_currency": t.get("perf_30d"),
                "price_change_percentage_1y_in_currency":  t.get("perf_1y"),
            }
    return lignes


def rejouer_chiffre_affaires_actions(prev_cache):
    """Chiffre d'affaires des actions relu dans le cache précédent (banc d'essai).
    Évite de solliciter Yahoo pour un contrôle qui ne teste pas Yahoo."""
    out = {}
    for n in (prev_cache.get("narratives") or []):
        for t in (n.get("tokens") or []):
            if t.get("is_stock") and t.get("symbol") and t.get("_stock_revenue_m"):
                out[t["symbol"]] = {"revenue_usd": t["_stock_revenue_m"] * 1e6,
                                    "perf_1y": t.get("perf_1y")}
    return out


# ─── Main ───────────────────────────────────────────────────────────────
def main():
    # Single-instance lock + load prev cache + load tracker data
    _lock = acquire_singleton_lock()
    if _lock is None:
        sys.exit(0)
    prev_cache = load_previous_cache()
    tracker = load_tracker_data()
    tracker_tokens = tracker["tokens"]
    tracker_stocks = tracker.get("stocks_by_narrative", {})
    tracker_momentum = tracker["momentum"]

    t0 = time.time()
    all_ids = set()
    for narr, meta in NARRATIVES.items():
        for tid in meta.get("tokens", []):
            all_ids.add(tid)
    all_ids = sorted(all_ids)
    print(f"[info] {len(all_ids)} unique CG ids across {len(NARRATIVES)} narratives")

    # 1. CoinGecko batch (still needed for FDV + perf_1y + image; price/mcap/perf_30d
    #    will be OVERRIDDEN by tracker values for consistency)
    if BANC_ESSAI:
        # Le quota CoinGecko gratuit est déjà consommé par les autres collecteurs
        # du site : un banc d'essai qui l'attaque fait échouer la collecte réelle
        # qui suit, et l'erreur ressemble à un bug du site, pas à un quota.
        cg_data = rejouer_coingecko_depuis_cache(prev_cache)
        print(f"[banc] CoinGecko REJOUÉ depuis le cache précédent : "
              f"{len(cg_data)}/{len(all_ids)} lignes — aucun appel réseau CoinGecko")
    else:
        print("[info] fetching CoinGecko /coins/markets ...")
        cg_data = fetch_coingecko_markets(all_ids)
        print(f"[info] CG: {len(cg_data)}/{len(all_ids)} fetched ({time.time()-t0:.0f}s)")

    # 2. DeFiLlama TVL + id→gecko map (single /protocols call)
    print("[info] fetching DefiLlama /protocols ...")
    protos = fetch_defillama_protocols()
    tvl_map = build_tvl_map(protos)
    dlid_to_gecko = build_dlid_to_gecko(protos)
    print(f"[info] DL TVL: {len(tvl_map)} gecko-mapped, {len(dlid_to_gecko)} defillama id mappings ({time.time()-t0:.0f}s)")

    # 2b. DeFiLlama chains TVL — pour les L1 natifs (ETH, SOL, BNB, BTC, TRON,
    # HYPE, ARB, POL, AVAX, etc.) dont la chain TVL est tracée séparément.
    # On SOMME chain_tvl + protocol_tvl par gecko_id ; ainsi ETH affiche les
    # $43B chain-locked + ses protocoles éventuels (mais aucun protocole n'a
    # gecko_id=ethereum, donc en pratique = chain TVL pure).
    print("[info] fetching DefiLlama /v2/chains for L1 chain TVL ...")
    chain_tvl_map, tvl_chain_web_map = fetch_defillama_chains_tvl()
    for gid, ct in chain_tvl_map.items():
        if gid in tvl_map:
            existing_tvl, existing_slug = tvl_map[gid]
            tvl_map[gid] = (existing_tvl + ct, existing_slug)
        else:
            tvl_map[gid] = (ct, "chain")

    # 3. DeFiLlama revenue (joined on defillamaId → gecko_id) + source attribution + breakdown
    print("[info] fetching DefiLlama /overview/fees ...")
    rev_map, rev_sources, rev_breakdowns, net_rev_map = fetch_defillama_revenue(dlid_to_gecko)
    print(f"[info] DL frais: {len(rev_map)} gecko-mapped, revenu gardé: {len(net_rev_map)} "
          f"({time.time()-t0:.0f}s)")

    # 4. Per-token fundamentals (build_token_fund uses CG row, then OVERRIDE
    #    price/mcap/perf with tracker values when available — guarantees the
    #    funda tab and the Narrative Tracker tab show identical numbers)
    token_fund = {}
    overridden = 0
    for cid in all_ids:
        row = cg_data.get(cid)
        if not row:
            continue
        tf = build_token_fund(row, tvl_map, rev_map, rev_sources, tvl_chain_web_map,
                              rev_breakdowns, net_rev_map)
        tt = tracker_tokens.get(cid)
        if tt:
            # Override visible fields with tracker values (single source of truth)
            if tt.get("price") is not None:
                tf["price"] = tt["price"]
            if tt.get("mcap"):
                # Le tracker fait foi pour le prix, donc pour la capitalisation.
                # Mais la FDV vient de CoinGecko, collectee a un autre instant :
                # la laisser telle quelle produit un ratio mcap/FDV superieur a
                # 1 des que le cours a monte entre les deux passages. Mesure
                # avant correction : 32 jetons sur 200, BTC a 1,0032.
                #
                # Le rapport offre_totale / offre_en_circulation est une donnee
                # de protocole : il ne bouge pas d'une collecte a l'autre. On le
                # lit sur le couple CoinGecko — coherent avec lui-meme — et on
                # l'applique a la capitalisation fraiche.
                _ancien_mcap = tf.get("mcap_b") or 0
                _ancienne_fdv = tf.get("fdv_b") or 0
                tf["mcap_b"] = round(tt["mcap"] / 1e9, 3)
                if _ancien_mcap > 0 and _ancienne_fdv > 0:
                    _rapport = _ancienne_fdv / _ancien_mcap
                    # Aucun rapport d'offres n'est inferieur a 1 : la FDV compte
                    # au moins les jetons deja en circulation.
                    if _rapport < 1.0:
                        _rapport = 1.0
                    tf["fdv_b"] = round(tt["mcap"] / 1e9 * _rapport, 3)
                    tf["circ_pct"] = round(100.0 / _rapport, 1)
            if tt.get("volume") is not None:
                tf["vol_b"] = round((tt["volume"] or 0) / 1e9, 3)
            if tt.get("perf_7d") is not None:
                tf["perf_7d"] = tt["perf_7d"]
            if tt.get("perf_30d") is not None:
                tf["perf_30d"] = tt["perf_30d"]
            tf["_price_source"] = "tracker"
            overridden += 1
        else:
            tf["_price_source"] = "coingecko_only"
        token_fund[cid] = tf
    print(f"[info] {len(token_fund)} tokens with fundamentals ({overridden} overridden by tracker_cache)")

    # 4b. Gap-fill from previous funda cache (last known good fundamentals
    #     when CG dropped a token: keeps TVL/revenue + most metrics, refresh
    #     price/mcap/perf from tracker if available)
    prev_token_fund = {}
    for n in (prev_cache.get("narratives", []) if prev_cache else []):
        for t in n.get("tokens", []):
            tid = t.get("id")
            if tid:
                prev_token_fund[tid] = t
    stale_filled = 0
    for tid in all_ids:
        if tid in token_fund:
            continue
        prev = prev_token_fund.get(tid)
        if not prev:
            continue
        merged = {**prev, "_stale_funda": True}
        tt = tracker_tokens.get(tid, {})
        if tt.get("price") is not None:
            merged["price"] = tt["price"]
        if tt.get("mcap"):
            # Meme piege que plus haut, sur le chemin de rattrapage.
            _am = merged.get("mcap_b") or 0
            _af = merged.get("fdv_b") or 0
            merged["mcap_b"] = round(tt["mcap"] / 1e9, 3)
            if _am > 0 and _af > 0:
                _r = max(_af / _am, 1.0)
                merged["fdv_b"] = round(tt["mcap"] / 1e9 * _r, 3)
                merged["circ_pct"] = round(100.0 / _r, 1)
        if tt.get("perf_30d") is not None:
            merged["perf_30d"] = tt["perf_30d"]
        token_fund[tid] = merged
        stale_filled += 1
    if stale_filled:
        print(f"[info] gap-filled {stale_filled} tokens from previous funda cache (marked _stale_funda=true)")

    # ── Garde-fou d'offre : rien d'incohérent ne sort d'ici ─────────────
    # Passe unique sur les objets QUI SERONT SÉRIALISÉS (token_fund est la même
    # référence que les paniers construits plus bas) : aucun jeton ne peut donc
    # échapper au contrôle, quel que soit le chemin qui l'a produit — CoinGecko
    # frais, rattrapage depuis le cache précédent, ou surcharge par le tracker.
    journal_offre = {"jetons_corriges": 0, "fdv_relevee": 0, "offre_ramenee": 0, "detail": []}
    for _t in token_fund.values():
        garantir_offre_coherente(_t, journal_offre)
    if journal_offre["jetons_corriges"]:
        print(f"[garde] offre : {journal_offre['jetons_corriges']} jetons corrigés "
              f"({journal_offre['fdv_relevee']} valeurs pleinement diluées relevées, "
              f"{journal_offre['offre_ramenee']} offres ramenées à 100 %) — "
              + ", ".join(d["symbol"] or d["id"] for d in journal_offre["detail"][:12]),
              file=sys.stderr)
    else:
        print("[garde] offre : aucun jeton incohérent (0 mcap > FDV, 0 offre > 100 %)",
              file=sys.stderr)

    # 4c. Fetch perf_1y pour les STOCKS crypto-side (MSTR, COIN, HOOD, MARA, RIOT,
    #     IREN, BITF, etc.) — tradfi_cache.json ne calcule que 7d/30d sur 60 jours
    #     d'historique. Sans ça, les narratives 100 % stocks (Bitcoin Miners,
    #     Web3 Exchanges & Fintech) affichaient `—` pour perf_1y partout.
    #     Yahoo expose le 52WeekChange directement, on l'utilise.
    all_stock_symbols = set()
    for narr, sts in tracker_stocks.items():
        for st in sts:
            sym = st.get("symbol")
            if sym:
                all_stock_symbols.add(sym)
    # Pour chaque stock, on récupère aussi :
    #   - perf_1y (52WeekChange)
    #   - priceToSalesTrailing12Months → ps_ttm équité
    #   - totalRevenue → revenue $ TTM
    # Ainsi les narratives à stocks (Web3 Exchanges, Bitcoin Miners) ou mixtes
    # (Bitcoin Institutional, Stablecoins) peuvent avoir P/S sectoriel.
    stock_fundamentals = {}  # symbol -> {perf_1y, ps_ttm, revenue_usd}
    if all_stock_symbols and BANC_ESSAI:
        stock_fundamentals = rejouer_chiffre_affaires_actions(prev_cache)
        _couvertes = len(all_stock_symbols & set(stock_fundamentals))
        print(f"[banc] chiffre d'affaires des actions REJOUÉ depuis le cache : "
              f"{_couvertes}/{len(all_stock_symbols)} actions du panier courant "
              f"— aucun appel Yahoo")
    elif all_stock_symbols:
        try:
            import yfinance as yf

            def _fetch_one_stock(sym):
                """Fetch (52WeekChange, ps_ttm, totalRevenue) for one stock.
                Retries .info with backoff because Yahoo quoteSummary rate-limits
                hard (HTTP 429) under concurrent load — single-shot misses 50%+ of
                tickers silently and leaves Bitcoin Miners / Web3 Exchanges with
                no Perf 1Y or revenue data."""
                info = {}
                for attempt in range(3):
                    try:
                        info = yf.Ticker(sym).info or {}
                        if len(info) >= 5:
                            break
                    except Exception:
                        if attempt == 2:
                            return sym, {}
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.8))
                entry = {}
                p = info.get("52WeekChange")
                if p is not None:
                    try: entry["perf_1y"] = float(p) * 100
                    except Exception: pass
                ps = info.get("priceToSalesTrailing12Months")
                if ps is not None:
                    try:
                        psf = float(ps)
                        if 0 < psf < 500: entry["ps_ttm"] = psf
                    except Exception: pass
                rev = info.get("totalRevenue")
                if rev is not None:
                    try:
                        rv = float(rev)
                        if rv > 0: entry["revenue_usd"] = rv
                    except Exception: pass
                return sym, entry

            print(f"[info] fetching perf_1y + ps_ttm + revenue for {len(all_stock_symbols)} stocks via Yahoo (yfinance + backoff)…")
            t0 = time.time()
            failed = []
            with ThreadPoolExecutor(max_workers=6) as ex:
                futures = {ex.submit(_fetch_one_stock, s): s for s in sorted(all_stock_symbols)}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    try:
                        sym, entry = fut.result(timeout=45)
                    except Exception:
                        entry = {}
                    if entry:
                        stock_fundamentals[sym] = entry
                    if not entry or "revenue_usd" not in entry:
                        failed.append(sym)

            # Yahooquery bulk fallback for tickers yfinance missed — financial_data
            # gives totalRevenue/52WeekChange in batches of 40 with one HTTP call,
            # immune to quoteSummary throttling that breaks single-ticker .info.
            if failed:
                try:
                    from yahooquery import Ticker as _YQT
                    print(f"[info] yahooquery fallback for {len(failed)} stocks missing data…")
                    YQ_CHUNK = 40
                    for i in range(0, len(failed), YQ_CHUNK):
                        batch = failed[i:i + YQ_CHUNK]
                        try:
                            yq = _YQT(batch, asynchronous=False, validate=False)
                            fd = yq.financial_data if isinstance(yq.financial_data, dict) else {}
                            ks = yq.key_stats       if isinstance(yq.key_stats, dict) else {}
                            sd = yq.summary_detail  if isinstance(yq.summary_detail, dict) else {}
                        except Exception as e:
                            print(f"[warn] yq stock chunk {i // YQ_CHUNK + 1} failed: {e}", file=sys.stderr)
                            continue
                        for sym in batch:
                            existing = stock_fundamentals.get(sym, {})
                            f = fd.get(sym) if isinstance(fd.get(sym), dict) else {}
                            k = ks.get(sym) if isinstance(ks.get(sym), dict) else {}
                            s = sd.get(sym) if isinstance(sd.get(sym), dict) else {}
                            if "revenue_usd" not in existing:
                                rev = f.get("totalRevenue")
                                try:
                                    if rev is not None and float(rev) > 0:
                                        existing["revenue_usd"] = float(rev)
                                except Exception: pass
                            if "perf_1y" not in existing:
                                p = k.get("52WeekChange")
                                try:
                                    if p is not None:
                                        existing["perf_1y"] = float(p) * 100
                                except Exception: pass
                            if "ps_ttm" not in existing:
                                ps = s.get("priceToSalesTrailing12Months") or k.get("priceToSalesTrailing12Months")
                                try:
                                    if ps is not None:
                                        psf = float(ps)
                                        if 0 < psf < 500: existing["ps_ttm"] = psf
                                except Exception: pass
                            if existing:
                                stock_fundamentals[sym] = existing
                except ImportError:
                    print("[warn] yahooquery unavailable — skipping bulk fallback")

            n_ps = sum(1 for v in stock_fundamentals.values() if "ps_ttm" in v)
            n_p1 = sum(1 for v in stock_fundamentals.values() if "perf_1y" in v)
            n_rv = sum(1 for v in stock_fundamentals.values() if "revenue_usd" in v)
            print(f"[info] stock fundamentals fetched : {n_p1}/{len(all_stock_symbols)} perf_1y, "
                  f"{n_ps}/{len(all_stock_symbols)} ps_ttm, {n_rv}/{len(all_stock_symbols)} revenue "
                  f"in {time.time()-t0:.0f}s")
        except ImportError:
            print("[warn] yfinance unavailable — stock fundamentals left as None")

    # Inject into tracker_stocks lists (mutate in place)
    for narr, sts in tracker_stocks.items():
        for st in sts:
            sym = st.get("symbol")
            f = stock_fundamentals.get(sym, {})
            if "perf_1y" in f and st.get("perf_1y") is None:
                st["perf_1y"] = f["perf_1y"]
            if "ps_ttm" in f:
                st["_stock_ps_ttm"] = f["ps_ttm"]
            if "revenue_usd" in f:
                # Convertir en M$ pour homogénéité avec rev_m_1y crypto (qui est en M)
                st["_stock_revenue_m"] = f["revenue_usd"] / 1e6
            # Les actions passent par le même garde-fou : elles arrivent avec
            # fdv = mcap et circ = 100 %, mais rien ne garantit qu'un futur
            # chemin d'alimentation le respecte.
            garantir_offre_coherente(st, journal_offre)

    # 5. Per-narrative aggregation + momentum cross-injection from tracker.
    #    On INJECTE les stocks proxies (CRCL, PYPL, MSTR, COIN, MARA, RIOT, etc.)
    #    qui figurent déjà dans le panier de l'onglet Narrative_Tracker. Le but :
    #    UNE SEULE SOURCE DE VÉRITÉ pour les paniers narratifs entre les pages.
    #    Ces actions n'ont pas de TVL/revenue on-chain mais contribuent au mcap
    #    total et à la perf pondérée — exactement comme dans le tracker.
    narratives_out = []
    n_defunct_dropped = 0
    for narr, meta in NARRATIVES.items():
        tokens = [token_fund[tid] for tid in meta.get("tokens", []) if tid in token_fund]
        # Append stocks (proxies actions cotées) — déjà au format dict identique
        stocks = tracker_stocks.get(narr, [])
        basket = tokens + stocks
        # Filter defunct zombie tokens (mcap=0 AND fdv≈0). CoinGecko keeps
        # returning these for deprecated tokens (MKR/maker, MC/merit-circle,
        # LOOKS, RGT, AZUR) — they appear with "—" everywhere and confuse
        # the audit modal. Stocks are kept (their mcap is real).
        basket_clean = []
        for t in basket:
            if t.get("is_stock"):
                # Afficher revenu TTM (Yahoo totalRevenue, $M) + P/S dans le modal,
                # cohérent avec l'agrégat (qui utilise _stock_revenue_m). Sans ça les
                # narratifs 100% actions (Web3 Exchanges, Bitcoin Miners, PYPL/CRCL en
                # Stablecoins) montraient "—" partout malgré un P/S agrégé. Garde anti
                # gross-pass-through : revenu > 3× mcap (Galaxy Digital 58B$ rev / 5.7B$
                # mcap = ventes crypto brutes) → non comparable, pas de P/S.
                rev_m = t.get("_stock_revenue_m")
                mcap_m = (t.get("mcap_b") or 0) * 1000
                if rev_m and rev_m > 0 and not (mcap_m > 0 and rev_m > 3 * mcap_m):
                    t["rev_m_1y"] = round(rev_m, 2)
                    if mcap_m > 0:
                        t["ps_ttm"] = round(mcap_m / rev_m, 1)
                basket_clean.append(t); continue
            # Drop tokens with mcap=0 — they contribute nothing to mcap-weighted
            # aggregates and clutter the audit modal with "—" everywhere.
            # MKR/maker is the textbook case : CoinGecko returns the legacy id
            # with mcap=0 (Maker was rebranded to Sky) but kept a stale fdv=0.15.
            mcap = t.get("mcap_b") or 0
            if mcap > 0:
                basket_clean.append(t)
            else:
                n_defunct_dropped += 1
        agg = aggregate_narrative(basket_clean)
        mom = tracker_momentum.get(narr, {})
        narratives_out.append({
            "narrative": narr,
            "icon": meta.get("icon"),
            "color": meta.get("color"),
            **agg,
            # ── Momentum (from narratives_cache.json — same source as Tracker tab) ──
            "momentum": {k: v for k, v in mom.items() if v is not None} if mom else {},
            "tokens": basket_clean,
            "n_stocks": len(stocks),
        })
    if n_defunct_dropped:
        print(f"[info] dropped {n_defunct_dropped} defunct zombie tokens (mcap=0 AND fdv≈0)", file=sys.stderr)

    narratives_out.sort(key=lambda n: (n.get("mcap_total_b") or 0), reverse=True)

    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M %Z") or
                   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fetch_ts": int(time.time()),
        "sources": [
            "narratives_cache.json (price + mcap + volume + perf_7d/30d + momentum) — single source of truth, written by fetch_narratives.py",
            "CoinGecko /coins/markets (FDV, perf_1y, fallback price/mcap when tracker missing)",
            "DefiLlama /protocols (TVL par gecko_id)",
            "DefiLlama /overview/fees?dataType=dailyFees (FRAIS TTM par gecko_id — ce que PAIENT les utilisateurs, dénominateur de ps_ttm)",
            "DefiLlama /overview/fees?dataType=dailyRevenue (REVENU TTM par gecko_id — ce que GARDE le protocole, publié dans revenu_m_1y_total)",
            "Yahoo totalRevenue (chiffre d'affaires TTM des actions cotées du panier — publié dans rev_m_1y_actions)",
        ],
        "tracker_cache_updated": tracker.get("updated"),
        "n_narratives": len(narratives_out),
        "n_tokens_total": len(all_ids),
        "n_tokens_fetched": len(token_fund),
        "n_overridden_by_tracker": overridden,
        "n_stale_filled": stale_filled,
        "n_with_tvl": sum(1 for t in token_fund.values() if t.get("tvl_b")),
        "n_with_rev": sum(1 for t in token_fund.values() if t.get("rev_m_1y")),
        "n_with_revenu_net": sum(1 for t in token_fund.values() if t.get("revenu_m_1y")),
        # Seuils publiés avec le cache : le lecteur (et le contrôle
        # test_narratifs_coherence.py) doivent pouvoir rejouer la décision
        # sans relire le code du collecteur.
        "seuils_absurdite": {
            "ps_ttm": SEUIL_ABSURDITE_PS_TTM,
            "mc_tvl": SEUIL_ABSURDITE_MC_TVL,
        },
        # Journal du garde-fou d'offre : combien de jetons sont sortis avec une
        # capitalisation supérieure à leur valeur pleinement diluée, ou plus de
        # 100 % d'offre en circulation, et ont dû être corrigés. Un chiffre qui
        # monte est le signal qu'une source a décroché, pas un détail cosmétique.
        "garde_offre": {
            "jetons_corriges":      journal_offre["jetons_corriges"],
            "fdv_relevee":          journal_offre["fdv_relevee"],
            "offre_ramenee_a_100":  journal_offre["offre_ramenee"],
            "detail":               journal_offre["detail"][:60],
        },
        "banc_essai": BANC_ESSAI,
        "narratives": narratives_out,
    }

    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[ok] wrote {OUT_JSON}")
    with OUT_JS.open("w", encoding="utf-8") as f:
        f.write("window.__NARRATIVES_FUNDAMENTALS__=" +
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {OUT_JS}")

    print("\n[recap] les 25 narratifs — prix/frais, statut, dénominateur :")
    print(f"  {'narratif':<30}{'mcap Md$':>10}{'prix/frais':>12}{'statut':>16}"
          f"{'denominateur':>20}{'frais M$':>11}{'gardé M$':>11}")
    for n in narratives_out:
        ps = n.get("ps_ttm")
        rev = n.get("rev_m_1y_total")
        net = n.get("revenu_m_1y_total")
        print(f"  {n['narrative']:<30}{(n.get('mcap_total_b') or 0):>10.1f}"
              f"{(f'{ps:.1f}' if ps else '—'):>12}{(n.get('ps_ttm_statut') or '—'):>16}"
              f"{(n.get('ps_ttm_denominateur') or '—'):>20}"
              f"{(f'{rev:.1f}' if rev else '—'):>11}{(f'{net:.1f}' if net else '—'):>11}")
    _non_mes = [n['narrative'] for n in narratives_out if n.get('ps_ttm_statut') == 'non_mesurable' and n.get('ps_ttm')]
    _non_mes_tvl = [n['narrative'] for n in narratives_out if n.get('mc_tvl_statut') == 'non_mesurable' and n.get('mc_tvl')]
    print(f"\n[recap] prix/frais écartés comme non mesurables : {_non_mes or 'aucun'}")
    print(f"[recap] capitalisation/TVL écartés comme non mesurables : {_non_mes_tvl or 'aucun'}")


if __name__ == "__main__":
    main()
