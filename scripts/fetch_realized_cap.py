#!/usr/bin/env python3
"""fetch_realized_cap.py — Realized Cap du bitcoin + régimes de tendance.

Alimente la carte « Realized Cap » de l'onglet Indicateur (Indicateur.Rmd) :
la capitalisation réalisée, son prix de revient moyen, le MVRV, et le découpage
de l histoire en deux regimes (hausse / pas de hausse) — l'équivalent maison du
graphe « BTC: Realized Cap » de CryptoQuant, mais avec la formule visible.

────────────────────────────────────────────────────────────────────────────
CE QUE MESURE LA REALIZED CAP
────────────────────────────────────────────────────────────────────────────
La capitalisation de marché valorise TOUS les bitcoins au dernier prix coté.
La realized cap valorise chaque bitcoin au prix auquel il a bougé pour la
DERNIÈRE fois on-chain : c'est la somme du coût d'acquisition du marché, donc
l'argent réellement immobilisé dans le réseau. Elle ne réagit pas au prix mais
aux TRANSFERTS : elle monte quand des coins changent de main plus haut qu'ils
n'avaient été acquis (capital qui entre), elle stagne quand plus personne ne
bouge, elle baisse quand des coins se déplacent à perte (capital qui sort).

────────────────────────────────────────────────────────────────────────────
SOURCE — pourquoi cette reconstruction plutôt qu'un appel direct
────────────────────────────────────────────────────────────────────────────
Coin Metrics sert `CapRealUSD` directement, mais PAS sur le palier gratuit
(HTTP 403 « not available with supplied credentials »). En revanche le palier
communautaire sert `CapMVRVCur` et `CapMrktCurUSD`, et par définition :

        MVRV = capitalisation de marché / realized cap
   =>   realized cap = CapMrktCurUSD / CapMVRVCur

C'est une identité, pas une approximation. Contrôlé le 2026-08-27 sur la
dernière valeur disponible :

    reconstruction (Coin Metrics)  1 063,97 G$
    CryptoQuant  /market-data/capitalization  1 064,23 G$   (écart 0,02 %)
    bitcoin-data.com /v1/realized-cap         1 063,57 G$   (écart 0,04 %)

Les trois s'accordent à 5 points de base près, et seule la reconstruction
remonte à 2010 (CryptoQuant plafonne à 29 jours sur le palier gratuit,
bitcoin-data.com à 4 ans).

Prix de revient moyen (realized price) = realized cap / offre en circulation.
MVRV redonné tel quel par Coin Metrics — le front le recalcule d'ailleurs comme
prix / prix de revient, ce qui donne le même nombre (les deux se simplifient
par l'offre) et évite d'embarquer une quatrième série.

────────────────────────────────────────────────────────────────────────────
LES DEUX RÉGIMES — un découpage RÉTROSPECTIF, et pourquoi il doit l'être
────────────────────────────────────────────────────────────────────────────
Deux catégories, comme la légende du graphe de référence de CryptoQuant :
« Realized Cap (Uptrend) » et « Realized Cap (No change or Downtrend) ».

    g = RC[t+90] / RC[t-180] - 1        (fenêtre CENTRÉE)
    on ENTRE en hausse si g > 35 %, on n'en SORT que si g < 25 %
    les 90 derniers jours : RC > moyenne 60 j (mesure de bord, causale)

Deux seuils, comme un thermostat. Un seuil unique ne peut pas marcher : le vert
de la référence court du DÉBUT de la jambe jusqu'au SOMMET, or la croissance
décélère bien avant le sommet — le vert s'éteignait cinq mois trop tôt en 2025.

Fenêtre CENTRÉE, donc non causale : la couleur du jour t se décide en partie
sur ce que la realized cap fait dans les 90 jours QUI SUIVENT. Ce n'est pas une
facilité, c'est une nécessité démontrée — et le front l'écrit sous le graphe.

La démonstration, en deux chiffres relevés sur la capture de référence :

    juillet 2019, RESTE GRIS  : +18,4 % sur 90 j, +14,5 % au-dessus de sa MM200
    octobre 2023, PASSE VERT  :  -0,1 % sur 90 j, -2,3 % sur un an

La référence colore en vert un moment où la realized cap est PLUS PLATE que
celui qu'elle laisse en gris. Aucune mesure rétrospective ne peut produire ça :
au jour le jour, juillet 2019 monte plus vite qu'octobre 2023. Ce qui les
sépare, c'est la SUITE — la hausse de 2019 est retombée, celle de 2023 a duré
deux ans. Plus de 3 000 réglages causaux ont été essayés (pente, moyennes
mobiles, croisements, plus-haut historique, seuils en dollars) : le meilleur
laisse 24 mois d'écart et met du vert en 2019. Une fenêtre centrée en laisse 13.

Autre indice, même conclusion : les blocs verts de la référence s'arrêtent
AVANT les sommets de la realized cap (gris dès décembre 2021, alors que le
sommet est en avril 2022). Un indicateur en temps réel ne peut pas savoir qu'un
sommet approche.

Calage retenu (fenêtre -180 j / +90 j, entrée +35 %, sortie +25 %) contre les
six basculements relevés AU PIXEL sur la capture haute résolution — graduations
de l'axe converties à raison de 12,3 pixels par mois :

    2018-01 -> trouvé 2018-06-11  (5 mois ; bloc haut de 9 % de l'écran)
    2020-10 -> trouvé 2020-09-08  (1 mois)
    2021-10 -> trouvé 2021-10-26  (exact)
    2023-10 -> trouvé 2023-12-23  (2 mois)
    2025-12 -> trouvé 2025-11-03  (1 mois)
    2026-08 -> trouvé 2026-08-23  (exact, par la mesure de bord)

Quatre mois d'écart cumulé sur les cinq basculements pleinement visibles, dont
deux exacts. Réglage choisi par balayage de plus de 200 000 combinaisons
(fenêtre avant, fenêtre arrière, seuil d'entrée, seuil de sortie), en pondérant
double les basculements visibles.

CONSÉQUENCE À ASSUMER : les 90 derniers jours n'ont pas leur futur, ils passent
par la mesure de bord et seront RECALCULÉS par la règle principale quand la
donnée aura avancé. Le cache le signale (`edge_from`, `in_edge`) et le panneau
l'affiche. Cet indicateur lit le passé ; il ne prédit rien.

Tout le reste du panneau — realized cap, prix de revient, MVRV, écart au
record, variations — est exact et sans look-ahead.

Usage : python3 fetch_realized_cap.py [--force] [--dry-run]
"""
import json
import shutil
import sys
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

import requests

# ─────────────────────────────── Config ────────────────────────────────
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_JSON = CACHE_DIR / "realized_cap_cache.json"
CACHE_JS = CACHE_DIR / "realized_cap_cache.js"
SITE_DIR = Path.home() / "Desktop" / "Site_Crypto_Finance"
CACHE_MAX_HOURS = 6

CM = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
START = "2010-07-18"          # première cotation servie par Coin Metrics
METRICS = "CapMrktCurUSD,CapMVRVCur,PriceUSD,SplyCur"

# Fenêtre centrée du régime, calée sur la capture de référence (cf docstring).
LOOKBACK = 180                # jours regardés en arrière
LOOKAHEAD = 90                # jours regardés en avant  → non causal, assumé
# DEUX seuils, comme un thermostat. Un seuil unique ne peut pas marcher : le
# vert de la référence court du DÉBUT de la jambe jusqu'au SOMMET, or la
# croissance décélère bien avant le sommet — le vert s'éteignait 5 mois trop tôt.
THRESHOLD_IN = 0.35           # on ENTRE en hausse au-dessus de ce seuil
THRESHOLD_OUT = 0.25          # on RESTE en hausse tant qu'on ne retombe pas dessous
# La frange sans futur (les LOOKAHEAD derniers jours) bascule sur une mesure
# causale courte, comme tout filtre centré au bord droit. C'est ce qui produit
# la bande verte fine du bord droit de la référence.
EDGE_MA = 60                  # realized cap au-dessus de sa moyenne 60 j → hausse
ROC_WINDOW = 90               # fenêtre de la variation publiée (informative, pas le régime)
BLOCK_MIN_DAYS = 45           # blocs plus courts : colorés dans le graphe, hors de la frise

# Deux catégories seulement, comme la légende de CryptoQuant. Palette reprise
# des tons du site (TONE_C dans Indicateur.Rmd) : vert « pos », gris neutre.
REG_META = {
    1: {"code": "h", "label": "Hausse", "tone": "pos", "color": "#26de81",
        "legend": "Realized Cap · hausse"},
    0: {"code": "s", "label": "Stagnation ou baisse", "tone": "eq", "color": "#7d87a4",
        "legend": "Realized Cap · pas de hausse"},
}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) CapitalAntifragile/1.0"}


def log(m):
    sys.stderr.write("[realcap] %s\n" % m)


def wait_for_network(host="community-api.coinmetrics.io", max_wait=120):
    """Anti-course « réveil de la machine » : launchd relance le fetcher avant que
    le DNS soit reconnecté → échec immédiat et cache figé en silence. On attend
    que le nom résolve plutôt que d'échouer sur-le-champ (cf fetch_btc_cycle)."""
    import socket
    waited, delay = 0, 3
    while True:
        try:
            socket.getaddrinfo(host, 443)
            if waited:
                log("réseau prêt après %ds d'attente" % waited)
            return True
        except socket.gaierror:
            if waited >= max_wait:
                log("WARN réseau/DNS indisponible après %ds — on tente quand même" % waited)
                return False
            time.sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 15)


def _session():
    s = requests.Session()
    s.headers.update(UA)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=4, connect=4, read=2, backoff_factor=2,
                      status_forcelist=(429, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]))
        s.mount("https://", HTTPAdapter(max_retries=retry))
    except Exception:
        pass
    return s


# ──────────────────────────────── Fetch ────────────────────────────────
def fetch_series(sess):
    """Série quotidienne (date, market_cap, mvrv, prix, offre), triée asc.

    Une ligne dont une seule métrique manque est écartée : les quatre valeurs
    sont solidaires (la realized cap est un quotient, le prix de revient un
    autre), et publier un jour à moitié rempli casserait la contiguïté sur
    laquelle repose le format compact du cache."""
    out = []
    url, params = CM, {
        "assets": "btc", "metrics": METRICS, "frequency": "1d",
        "start_time": START, "page_size": 10000,
    }
    for _ in range(60):                      # garde-fou de pagination
        r = sess.get(url, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        for row in j.get("data", []):
            try:
                mc = float(row["CapMrktCurUSD"])
                mv = float(row["CapMVRVCur"])
                px = float(row["PriceUSD"])
                sp = float(row["SplyCur"])
            except (KeyError, TypeError, ValueError):
                continue
            if mc <= 0 or mv <= 0 or sp <= 0:
                continue
            out.append((row["time"][:10], mc, mv, px, sp))
        url = j.get("next_page_url")
        if not url:
            break
        params = None
    out.sort(key=lambda x: x[0])
    return out


# ─────────────────────────────── Régimes ───────────────────────────────
def regimes(rc):
    """Régime de chaque jour, en DEUX morceaux — comme tout filtre centré.

    1. L'HISTOIRE (jusqu'à LOOKAHEAD jours de la fin) : fenêtre centrée
       -LOOKBACK / +LOOKAHEAD. En tête de série on borne le regard arrière à
       l'origine plutôt que de laisser les premiers mois sans couleur : la
       realized cap y explose, le régime n'a aucun doute, et un trou en 2010 se
       verrait sur la vue MAX.

    2. LA FRANGE (les LOOKAHEAD derniers jours) : le futur manque, donc la
       fenêtre centrée est aveugle. On bascule sur une mesure CAUSALE courte —
       la realized cap au-dessus de sa moyenne EDGE_MA jours. C'est le
       traitement standard du bord droit d'un filtre centré, et c'est ce qui
       produit la bande verte fine que montre la référence : sans elle, aucun
       basculement récent ne pourrait apparaître avant trois mois.

    Conséquence à assumer : la couleur de la frange est calculée autrement que
    celle de l'histoire, et elle sera recalculée par la fenêtre centrée quand la
    donnée aura avancé. Le cache l'expose (`edge_from`), le panneau l'affiche.

    Retourne (reg, ath_idx, edge_from)."""
    n = len(rc)
    reg = [None] * n
    ath_idx = [0] * n
    ma = [None] * n
    best_i = 0
    s = 0.0
    for i in range(n):
        if rc[i] >= rc[best_i]:
            best_i = i
        ath_idx[i] = best_i
        s += rc[i]
        if i >= EDGE_MA:
            s -= rc[i - EDGE_MA]
        if i >= EDGE_MA - 1:
            ma[i] = s / EDGE_MA
    edge_from = max(0, n - LOOKAHEAD)
    state = 0
    for i in range(n):
        if i < edge_from:
            g = rc[i + LOOKAHEAD] / rc[max(0, i - LOOKBACK)] - 1
            # Thermostat : on entre au-dessus de IN, on ne sort que sous OUT.
            # Entre les deux, on garde l'état — c'est ce qui fait tenir le vert
            # jusqu'au sommet au lieu de l'éteindre dès que la hausse ralentit.
            if g > THRESHOLD_IN:
                state = 1
            elif g < THRESHOLD_OUT:
                state = 0
            reg[i] = state
        else:
            reg[i] = 1 if (ma[i] is not None and rc[i] > ma[i]) else 0
    return reg, ath_idx, edge_from


def blocks(dates, reg, rc, depuis="2011-01-01", min_days=21):
    """Blocs de régime consécutifs, pour la frise « historique des régimes ».

    Les blocs plus courts que min_days sont écartés de la LISTE affichée mais
    restent dans la série `reg` : la frise raconte les grands mouvements, le
    graphe garde la couleur exacte de chaque jour."""
    out, cur, start, i0 = [], None, None, 0
    for i, d in enumerate(dates):
        if reg[i] is None:
            continue
        if reg[i] != cur:
            if cur is not None:
                out.append((cur, start, dates[i - 1], i0, i - 1))
            cur, start, i0 = reg[i], d, i
    if cur is not None:
        out.append((cur, start, dates[-1], i0, len(dates) - 1))
    res = []
    for r, a, z, ia, iz in out:
        if z < depuis:
            continue
        n = (date.fromisoformat(z) - date.fromisoformat(a)).days + 1
        if n < min_days:
            continue
        res.append({
            "r": r, "a": a, "z": z, "d": n,
            "chg": round((rc[iz] / rc[ia] - 1) * 100, 1),
        })
    return res


# ──────────────────────────────── Payload ──────────────────────────────
def sig(v, n=6):
    """Arrondi à n chiffres significatifs. La realized cap couvre 8 ordres de
    grandeur (10 k$ en 2010 → 1 000 G$ en 2026) : un arrondi à décimale fixe
    soit écrase les débuts, soit gonfle le cache de zéros inutiles."""
    if v == 0:
        return 0.0
    return float("%.*g" % (n, v))


def build(rows):
    dates = [r[0] for r in rows]
    rc = [r[1] / r[2] for r in rows]                    # market cap / MVRV
    px = [r[3] for r in rows]
    rp = [rc[i] / rows[i][4] for i in range(len(rows))]  # realized cap / offre
    reg, ath_idx, edge_from = regimes(rc)

    # Contiguïté : le format compact (date de départ + un point par jour) ne
    # tolère aucun trou. On la vérifie plutôt que de la supposer.
    d0 = date.fromisoformat(dates[0])
    for i, d in enumerate(dates):
        if date.fromisoformat(d) != d0 + timedelta(days=i):
            raise AssertionError("trou dans la série à %s (attendu %s)"
                                 % (d, d0 + timedelta(days=i)))

    n = len(rows)
    last = n - 1
    cur_reg = reg[last]
    meta = REG_META[cur_reg]

    # Depuis quand le régime en cours tient-il ?
    since = last
    while since > 0 and reg[since - 1] == cur_reg:
        since -= 1

    def roc(days):
        return round((rc[last] / rc[last - days] - 1) * 100, 2) if last >= days else None

    ath_i = ath_idx[last]

    current = {
        "date": dates[last],
        "rc": rc[last],
        "rc_g": round(rc[last] / 1e9, 2),
        "realized_price": round(rp[last], 2),
        "price": round(px[last], 2),
        "mvrv": round(px[last] / rp[last], 4),
        "supply": round(rows[last][4]),
        "roc30": roc(30),
        "roc90": roc(90),
        "roc365": roc(365),
        "regime": cur_reg,
        "label": meta["label"],
        "tone": meta["tone"],
        "color": meta["color"],
        "since": dates[since],
        "days": (date.fromisoformat(dates[last]) - date.fromisoformat(dates[since])).days + 1,
        "ath_rc_g": round(rc[ath_i] / 1e9, 2),
        "ath_date": dates[ath_i],
        "ath_days": (date.fromisoformat(dates[last]) - date.fromisoformat(dates[ath_i])).days,
        "drawdown": round((rc[last] / rc[ath_i] - 1) * 100, 2),
        # La frange sans futur est calculée autrement : le dire est la
        # contrepartie du choix d'une fenêtre centrée.
        "edge_from": dates[edge_from],
        "edge_days": n - edge_from,
        "edge_ma": EDGE_MA,
        "in_edge": last >= edge_from,
        "lookahead": LOOKAHEAD,
        "lookback": LOOKBACK,
        "threshold_in_pct": round(THRESHOLD_IN * 100),
        "threshold_out_pct": round(THRESHOLD_OUT * 100),
    }

    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Coin Metrics community API (CapMrktCurUSD, CapMVRVCur, PriceUSD, SplyCur)",
        "formula": "realized cap = capitalisation de marche / MVRV",
        "regime_rule": ("RETROSPECTIF — g = RC[t+%d] / RC[t-%d] - 1 ; entree en hausse "
                        "si g > %d %%, sortie si g < %d %% ; les %d derniers jours "
                        "basculent sur RC > moyenne %d j"
                        % (LOOKAHEAD, LOOKBACK, round(THRESHOLD_IN * 100),
                           round(THRESHOLD_OUT * 100), LOOKAHEAD, EDGE_MA)),
        "start": dates[0],
        "end": dates[last],
        "n": n,
        # Séries parallèles indexées sur `start` + 1 point par jour : pas de
        # tableau de dates à embarquer (~90 Ko économisés sur 5 900 points).
        "rc": [sig(v / 1e9) for v in rc],     # milliards de dollars
        "p": [sig(v) for v in px],            # prix BTC en dollars
        "rp": [sig(v) for v in rp],           # prix de revient moyen en dollars
        "reg": "".join(REG_META[r]["code"] if r is not None else "." for r in reg),
        "params": {
            "rule": "centred",
            "causal": False,
            "lookback": LOOKBACK,
            "lookahead": LOOKAHEAD,
            "threshold_in_pct": round(THRESHOLD_IN * 100),
        "threshold_out_pct": round(THRESHOLD_OUT * 100),
            "edge_ma": EDGE_MA,
            "roc_window": ROC_WINDOW,
            "block_min_days": BLOCK_MIN_DAYS,
        },
        "palette": {REG_META[k]["code"]: REG_META[k]["color"] for k in REG_META},
        "legends": {REG_META[k]["code"]: REG_META[k]["legend"] for k in REG_META},
        "current": current,
        "blocks": blocks(dates, reg, rc, min_days=BLOCK_MIN_DAYS),
    }


def sanity(p):
    c = p["current"]
    assert p["n"] >= 4000, "série trop courte (%d points)" % p["n"]
    assert len(p["rc"]) == len(p["p"]) == len(p["rp"]) == len(p["reg"]) == p["n"], \
        "séries désalignées"
    lag = (datetime.now(timezone.utc).date() - date.fromisoformat(c["date"])).days
    assert lag <= 5, "dernière donnée vieille de %d jours (%s)" % (lag, c["date"])
    assert c["rc_g"] > 100, "realized cap invraisemblable (%s G$)" % c["rc_g"]
    assert 0.3 < c["mvrv"] < 10, "MVRV hors bornes (%s)" % c["mvrv"]
    assert min(p["rc"]) > 0, "realized cap nulle ou négative dans la série"
    assert c["regime"] in (0, 1), "régime courant invalide"
    assert set(p["reg"]) <= {"h", "s", "."}, "code de régime inattendu dans la série"
    assert "." not in p["reg"], "jours sans régime — la fenêtre centrée doit tout couvrir"
    # Garde-fou d'allure : la référence montre six grands aplats. Au-delà d'une
    # dizaine, c'est qu'un réglage a raccourci la fenêtre et rehaché l'histoire.
    assert len(p["blocks"]) <= 14, "trop de blocs (%d) — la règle hache l'histoire" % len(p["blocks"])
    assert c["edge_days"] == LOOKAHEAD, "frange de bord incohérente (%d j)" % c["edge_days"]


def guard_against_shrink(p):
    """Ne jamais remplacer un cache par une version amputée : une réponse
    partielle de l'API (page manquante, coupure en cours de pagination) doit
    laisser l'ancien cache en place plutôt que raboter l'historique."""
    if not CACHE_JSON.exists():
        return
    try:
        old = json.loads(CACHE_JSON.read_text())
    except Exception:
        return
    if old.get("n", 0) > p["n"] + 3:
        raise AssertionError("nouvelle série plus courte que l'ancienne (%d < %d)"
                             % (p["n"], old["n"]))


def write_outputs(p):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_j = CACHE_DIR / ".realized_cap.tmp.json"
    tmp_j.write_text(json.dumps(p, separators=(",", ":"), ensure_ascii=False))
    tmp_j.replace(CACHE_JSON)
    js = ("/* realized_cap_cache.js — %s — fetch_realized_cap.py */\n"
          "window.__REALIZED_CAP__=%s;\n"
          % (p["updated"], json.dumps(p, separators=(",", ":"), ensure_ascii=False)))
    tmp_s = CACHE_DIR / ".realized_cap.tmp.js"
    tmp_s.write_text(js)
    tmp_s.replace(CACHE_JS)
    if SITE_DIR.exists():
        for name in ("realized_cap_cache.js",):
            link, target = SITE_DIR / name, CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    dry = "--dry-run" in sys.argv
    if CACHE_JSON.exists() and CACHE_JS.exists() and "--force" not in sys.argv and not dry:
        age = (time.time() - CACHE_JSON.stat().st_mtime) / 3600
        if age < CACHE_MAX_HOURS:
            log("cache frais (%.1fh) — skip (--force pour forcer)" % age)
            return
    t0 = time.time()
    wait_for_network()
    try:
        rows = fetch_series(_session())
        payload = build(rows)
    except Exception as e:
        log("FATAL build: %s" % e)
        sys.exit(2)
    try:
        guard_against_shrink(payload)
        sanity(payload)
    except AssertionError as e:
        log("FATAL sanity: %s — cache NON écrit (préservation)" % e)
        sys.exit(3)
    c = payload["current"]
    if dry:
        log("dry-run : %d points %s → %s" % (payload["n"], payload["start"], payload["end"]))
        log("  realized cap %s G$ · prix de revient %s $ · MVRV %s"
            % (c["rc_g"], c["realized_price"], c["mvrv"]))
        log("  régime %s depuis le %s (%d j) · roc90 %s %%"
            % (c["label"], c["since"], c["days"], c["roc90"]))
        return
    write_outputs(payload)
    log("OK %.1fs · %d points · realized cap %s G$ · régime %s depuis %s · %d Ko"
        % (time.time() - t0, payload["n"], c["rc_g"], c["label"], c["since"],
           CACHE_JS.stat().st_size // 1024))


if __name__ == "__main__":
    main()
