#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_cloud_sources.py — Sources de repli pour ce qui refuse les IP de datacenter.

POURQUOI
La collecte quitte un ordinateur portable (qui dort, donc qui fige les données) pour
des runners. Trois sources refusent ces IP, mesuré, pas supposé :
  · Binance renvoie 451 aux IP américaines — les runners GitHub sont aux États-Unis ;
  · Farside et Investing.com renvoient 403 aux IP de datacenter (filtrage anti-robot).

PRINCIPE : ON N'ÉCHANGE PAS UNE SOURCE, ON EN AJOUTE UNE
Chaque fonction essaie D'ABORD la source d'origine, et ne bascule qu'en cas de refus.
Sur la machine d'origine, où Binance répond, le comportement reste EXACTEMENT celui
d'avant — aucune régression possible. Sur un runner, le repli prend le relais.

PRINCIPE : LE REPLI REND LA FORME DE L'ORIGINAL
Les collecteurs et les pages lisent des champs Binance (`lastFundingRate`,
`sumOpenInterest`). Traduire la réponse OKX vers cette forme évite de toucher aux
collecteurs ET au front. Le prix à payer est ici, dans un seul fichier, au lieu d'être
disséminé dans cinq scripts et autant de pages.

PRINCIPE : LA PROVENANCE EST TRAÇABLE
Chaque appel note la source réellement servie dans `LAST_SOURCE`. Une donnée qui vient
d'un repli n'est pas fausse, mais elle n'est pas identique : pouvoir le dire évite de
chercher pendant des heures pourquoi une valeur a bougé de 3 %.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Provenance du dernier appel, par fonction. Lu par les collecteurs pour l'afficher.
LAST_SOURCE = {}

# Refus qui justifient un repli. Un 500 ou un délai dépassé n'en font pas partie :
# ce sont des incidents passagers, et basculer de source à chaque hoquet rendrait la
# donnée incohérente d'un run à l'autre sans raison.
FALLBACK_CODES = (401, 403, 451)


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _try_binance(path):
    """Renvoie la réponse Binance, ou None si l'IP est refusée."""
    if os.environ.get("SCF_FORCE_FALLBACK"):
        return None                      # permet de tester le repli là où Binance marche
    try:
        return _get_json("https://fapi.binance.com" + path)
    except urllib.error.HTTPError as e:
        if e.code in FALLBACK_CODES:
            return None
        raise
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _okx(path):
    d = _get_json("https://www.okx.com/api/v5/" + path)
    if d.get("code") not in ("0", 0):
        raise RuntimeError(f"OKX a refusé : {d.get('msg') or d.get('error_message')}")
    return d.get("data") or []


def _inst(symbol):
    """BTCUSDT → BTC-USDT-SWAP (contrat perpétuel équivalent chez OKX)."""
    base = symbol.upper().replace("USDT", "")
    return f"{base}-USDT-SWAP", base


def _note(fn, source):
    LAST_SOURCE[fn] = source
    return source


# ── Taux de financement, instantané ───────────────────────────────────────────
def premium_index(symbol="BTCUSDT"):
    """Forme Binance : {"symbol", "lastFundingRate", "nextFundingTime", "time"}."""
    d = _try_binance(f"/fapi/v1/premiumIndex?symbol={symbol}")
    if d is not None:
        _note("premium_index", "binance")
        return d

    inst, _ = _inst(symbol)
    rows = _okx(f"public/funding-rate?instId={inst}")
    if not rows:
        return None
    r = rows[0]
    _note("premium_index", "okx")
    return {
        "symbol": symbol,
        "lastFundingRate": r.get("fundingRate", ""),
        "nextFundingTime": int(r.get("nextFundingTime") or 0),
        "time": int(r.get("ts") or time.time() * 1000),
        "source": "okx",
    }


# ── Intérêt ouvert, historique ────────────────────────────────────────────────
def open_interest_hist(symbol="BTCUSDT", period="1h", limit=48):
    """Forme Binance : [{"symbol","sumOpenInterest","sumOpenInterestValue","timestamp"}].

    OKX ne publie pas le nombre de contrats sur son historique agrégé, seulement la
    valeur en dollars. On renseigne donc `sumOpenInterestValue` (la grandeur que les
    pages affichent) et on laisse `sumOpenInterest` vide plutôt que d'inventer une
    conversion : un chiffre fabriqué serait indiscernable d'un chiffre mesuré.
    """
    d = _try_binance(f"/futures/data/openInterestHist?symbol={symbol}"
                     f"&period={period}&limit={limit}")
    if d is not None:
        _note("open_interest_hist", "binance")
        return d

    _, ccy = _inst(symbol)
    okx_period = {"5m": "5m", "1h": "1H", "4h": "1H", "1d": "1D"}.get(period.lower(), "1H")
    rows = _okx(f"rubik/stat/contracts/open-interest-volume?ccy={ccy}&period={okx_period}")
    out = []
    for ts, oi_usd, _vol in rows[:limit]:
        out.append({"symbol": symbol, "sumOpenInterest": "",
                    "sumOpenInterestValue": str(oi_usd), "timestamp": int(ts),
                    "source": "okx"})
    out.reverse()                        # Binance renvoie du plus ancien au plus récent
    _note("open_interest_hist", "okx")
    return out


# ── Ratio comptes acheteurs / vendeurs ────────────────────────────────────────
def long_short_ratio(symbol="BTCUSDT", period="4h", limit=12):
    """Forme Binance : [{"symbol","longShortRatio","longAccount","shortAccount","timestamp"}].

    OKX n'accepte que 5m, 1H et 1D : une demande en 4h est servie en 1H. La part
    longue/courte est déduite du ratio (r/(1+r)), ce qui est exact par définition.
    """
    d = _try_binance(f"/futures/data/globalLongShortAccountRatio?symbol={symbol}"
                     f"&period={period}&limit={limit}")
    if d is not None:
        _note("long_short_ratio", "binance")
        return d

    _, ccy = _inst(symbol)
    okx_period = {"5m": "5m", "1h": "1H", "1d": "1D"}.get(period.lower(), "1H")
    rows = _okx(f"rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period={okx_period}")
    out = []
    for ts, ratio in rows[:limit]:
        r = float(ratio)
        out.append({"symbol": symbol, "longShortRatio": str(r),
                    "longAccount": f"{r / (1 + r):.4f}", "shortAccount": f"{1 / (1 + r):.4f}",
                    "timestamp": int(ts), "source": "okx"})
    out.reverse()
    _note("long_short_ratio", "okx")
    return out


# ── Ratio des volumes agressifs ───────────────────────────────────────────────
def taker_ratio(symbol="BTCUSDT", period="1h", limit=24):
    """Forme Binance : [{"buySellRatio","buyVol","sellVol","timestamp"}].

    Attention à l'ordre des colonnes OKX : [horodatage, volume VENDEUR, volume ACHETEUR].
    Les inverser retournerait le signal — un marché acheteur se lirait vendeur.
    """
    d = _try_binance(f"/futures/data/takerlongshortRatio?symbol={symbol}"
                     f"&period={period}&limit={limit}")
    if d is not None:
        _note("taker_ratio", "binance")
        return d

    _, ccy = _inst(symbol)
    okx_period = {"5m": "5m", "1h": "1H", "1d": "1D"}.get(period.lower(), "1H")
    rows = _okx(f"rubik/stat/taker-volume?ccy={ccy}&instType=SPOT&period={okx_period}")
    out = []
    for ts, sell_vol, buy_vol in rows[:limit]:
        sv, bv = float(sell_vol), float(buy_vol)
        out.append({"buySellRatio": f"{(bv / sv) if sv else 0:.4f}",
                    "buyVol": str(bv), "sellVol": str(sv),
                    "timestamp": int(ts), "source": "okx"})
    out.reverse()
    _note("taker_ratio", "okx")
    return out


# ── Historique du financement ─────────────────────────────────────────────────
def funding_history(symbol="BTCUSDT", start_ms=None, limit=1000):
    """Forme Binance : [{"symbol","fundingRate","fundingTime"}], du plus ancien au plus récent.

    OKX plafonne à 100 points par appel : on pagine vers le passé jusqu'à la borne
    demandée. Sans cette pagination, un backtest qui croit lire deux ans n'en lirait
    que trente jours — et se tromperait en silence, ce qui est pire que d'échouer.
    """
    q = f"/fapi/v1/fundingRate?symbol={symbol}&limit={min(limit, 1000)}"
    if start_ms:
        q += f"&startTime={int(start_ms)}"
    d = _try_binance(q)
    if d is not None:
        _note("funding_history", "binance")
        return d

    inst, _ = _inst(symbol)
    out, before = [], None
    while len(out) < limit:
        path = f"public/funding-rate-history?instId={inst}&limit=100"
        if before:
            path += f"&after={before}"    # `after` = strictement plus ancien que cet horodatage
        rows = _okx(path)
        if not rows:
            break
        for r in rows:
            ts = int(r["fundingTime"])
            if start_ms and ts < int(start_ms):
                rows = []
                break
            out.append({"symbol": symbol, "fundingRate": r["fundingRate"],
                        "fundingTime": ts, "source": "okx"})
        if not rows:
            break
        before = str(min(int(r["fundingTime"]) for r in rows))
        time.sleep(0.15)                 # OKX limite à 20 appels/2 s sur cette route
    out.sort(key=lambda x: x["fundingTime"])
    _note("funding_history", "okx")
    return out[:limit]


# ── Pages protégées (Farside) ─────────────────────────────────────────────────
def guarded_html(url, impersonate_first=False):
    """Récupère une page filtrée par un pare-feu applicatif.

    CONTRE-INTUITIF, MAIS MESURÉ : depuis une IP de datacenter, l'usurpation TLS
    « chrome120 » se fait REFUSER (403) là où une requête honnête passe (200). Une
    empreinte de navigateur venant d'une adresse de serveur est plus suspecte qu'un
    client qui ne prétend rien. Depuis une adresse résidentielle, c'est l'inverse.
    On essaie donc les deux, dans l'ordre qui convient à l'endroit où l'on tourne.
    """
    def plain():
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    def disguised():
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome120", timeout=30)
        if r.status_code != 200:
            raise urllib.error.HTTPError(url, r.status_code, "refusé", None, None)
        return r.text

    ordre = [("deguise", disguised), ("honnete", plain)] if impersonate_first \
        else [("honnete", plain), ("deguise", disguised)]

    erreurs = []
    for nom, fn in ordre:
        try:
            html = fn()
            if html and len(html) > 500:
                _note("guarded_html", nom)
                return html
            erreurs.append(f"{nom}: réponse trop courte ({len(html or '')} o)")
        except Exception as e:
            erreurs.append(f"{nom}: {type(e).__name__} {e}"[:90])
    raise RuntimeError(f"{url} inaccessible — " + " · ".join(erreurs))
