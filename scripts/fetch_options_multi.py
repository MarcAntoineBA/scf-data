#!/usr/bin/env python3
"""Options crypto multi-place — onglet Structure de marché.

Remplace le collecteur mono-Deribit. Quatre places, toutes en accès libre :

  deribit   ~85 % de l'open interest. Publie l'IV mais PAS les grecques dans le
            résumé de carnet — on reconstruit gamma en Black-Scholes.
  bybit     complet en un appel : open interest, IV de marque, sous-jacent.
  okx       trois appels — tickers, open interest, résumé d'options — fusionnés
            par identifiant d'instrument. Publie delta ET gamma.
  binance   deux appels globaux (ticker, mark) plus un appel d'open interest PAR
            ÉCHÉANCE : leur API ne l'expose pas autrement.

╔══════════════════════════════════════════════════════════════════════════════╗
║ CE QUI EST COMPARABLE, ET CE QUI NE L'EST PAS                                ║
╚══════════════════════════════════════════════════════════════════════════════╝
Chaque place libelle son open interest à sa façon : Deribit et Bybit en COINS,
OKX en contrats avec un multiplicateur, Binance en coins. Rien n'est comparable
avant conversion. On ramène donc tout en DOLLARS de notionnel, seule unité qui
autorise une addition entre places.

Les volatilités implicites, elles, ne s'additionnent jamais : on publie une
FOURCHETTE par place et par symbole, pas une moyenne. Une moyenne d'IV entre des
échéances différentes ne veut rien dire.

Le GEX reste calculé sur Deribit SEUL. Mélanger des grecques publiées par trois
maisons, chacune avec sa convention de taux et son prix forward, produirait un
agrégat que personne ne pourrait auditer. Deribit portant l'essentiel de l'open
interest, la mesure reste représentative — et elle est dite telle quelle.

Sorties (~/Library/Caches/site_crypto_finance/) :
  crypto_options_cache.json + .js     window.__CRYPTO_OPTIONS__

Cadence horaire.
"""
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "crypto_options_cache.json"
OUT_JS = CACHE_DIR / "crypto_options_cache.js"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}

DERIBIT_DEVISES = ["BTC", "ETH", "SOL", "XRP"]
BYBIT_DEVISES = ["BTC", "ETH", "SOL"]
OKX_SOUS = [("BTC-USD", "BTC"), ("ETH-USD", "ETH")]
BINANCE_DEVISES = ["BTC", "ETH"]

MOIS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def get(url, essais=3, timeout=90):
    dernier = None
    for k in range(1, essais + 1):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read())
        except Exception as e:                                    # noqa: BLE001
            dernier = e
            if k < essais:
                time.sleep(3 * k)
    raise RuntimeError(f"{url.split('?')[0]} : {dernier}")


def f(x, defaut=0.0):
    try:
        v = float(x)
        return v if v == v else defaut          # écarte NaN
    except (TypeError, ValueError):
        return defaut


def jours_restants(dt):
    return max((dt - datetime.now(timezone.utc)).total_seconds(), 0) / 86400


# ── Adaptateurs : chacun rend une liste normalisée ───────────────────────────
# {sym, exp(datetime), strike, cp, oi_usd, vol_usd, iv, spot}

RX_DBT = re.compile(r"^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})-(\d+(?:d\d+)?)-([CP])$")


def deribit():
    out = []
    for dev in DERIBIT_DEVISES:
        try:
            lignes = get("https://www.deribit.com/api/v2/public/"
                         f"get_book_summary_by_currency?currency={dev}&kind=option")["result"]
        except Exception:                                         # noqa: BLE001
            continue
        for r in lignes:
            m = RX_DBT.match(r.get("instrument_name", ""))
            if not m:
                continue
            _, exp_txt, k, cp = m.groups()
            mm = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2})$", exp_txt)
            if not mm:
                continue
            try:
                exp = datetime(2000 + int(mm.group(3)), MOIS[mm.group(2)],
                               int(mm.group(1)), 8, tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            S = f(r.get("underlying_price"))
            oi = f(r.get("open_interest"))       # en COINS
            if S <= 0:
                continue
            # `volume_usd` de Deribit est la PRIME échangée, pas le notionnel :
            # l'additionner au turnover de Bybit, qui est du notionnel, reviendrait
            # à mélanger deux grandeurs sans rapport. On recalcule partout de la
            # même façon : quantité en coins × prix du sous-jacent.
            out.append(dict(place="deribit", sym=dev, exp=exp, strike=f(k.replace("d", ".")),
                            cp=cp, oi_usd=oi * S, vol_usd=f(r.get("volume")) * S,
                            iv=f(r.get("mark_iv")) / 100, spot=S, oi_coin=oi))
    return out


RX_BYB = re.compile(r"^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP])")


def bybit():
    out = []
    for dev in BYBIT_DEVISES:
        try:
            r = get("https://api.bybit.com/v5/market/tickers?category=option"
                    f"&baseCoin={dev}")["result"]["list"]
        except Exception:                                         # noqa: BLE001
            continue
        for t in r:
            m = RX_BYB.match(t.get("symbol", ""))
            if not m:
                continue
            _, exp_txt, k, cp = m.groups()
            mm = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2})$", exp_txt)
            if not mm:
                continue
            try:
                exp = datetime(2000 + int(mm.group(3)), MOIS[mm.group(2)],
                               int(mm.group(1)), 8, tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            S = f(t.get("underlyingPrice"))
            oi = f(t.get("openInterest"))        # en COINS
            if S <= 0:
                continue
            # Vérifié : le `turnover24h` de Bybit vaut déjà volume × sous-jacent,
            # donc du notionnel. On le recalcule tout de même à l'identique pour
            # que les quatre places passent par la même formule.
            out.append(dict(place="bybit", sym=dev, exp=exp, strike=f(k), cp=cp,
                            oi_usd=oi * S, vol_usd=f(t.get("volume24h")) * S,
                            iv=f(t.get("markIv")), spot=S, oi_coin=oi))
    return out


RX_OKX = re.compile(r"^([A-Z]+)-USD(?:_UM)?-(\d{6})-(\d+)-([CP])$")


def okx():
    out = []
    for uly, dev in OKX_SOUS:
        try:
            tick = {t["instId"]: t for t in get(
                f"https://www.okx.com/api/v5/market/tickers?instType=OPTION&uly={uly}")["data"]}
            oi = {t["instId"]: t for t in get(
                f"https://www.okx.com/api/v5/public/open-interest?instType=OPTION&uly={uly}")["data"]}
            som = {t["instId"]: t for t in get(
                f"https://www.okx.com/api/v5/public/opt-summary?uly={uly}")["data"]}
        except Exception:                                         # noqa: BLE001
            continue
        for iid, t in tick.items():
            m = RX_OKX.match(iid)
            if not m:
                continue
            _, exp_txt, k, cp = m.groups()
            try:
                exp = datetime(2000 + int(exp_txt[:2]), int(exp_txt[2:4]),
                               int(exp_txt[4:6]), 8, tzinfo=timezone.utc)
            except ValueError:
                continue
            s = som.get(iid, {})
            # `oiUsd` est déjà en dollars chez OKX — on ne le reconvertit pas.
            out.append(dict(place="okx", sym=dev, exp=exp, strike=f(k), cp=cp,
                            oi_usd=f(oi.get(iid, {}).get("oiUsd")),
                            vol_usd=f(t.get("volCcy24h")) * f(s.get("fwdPx")),
                            iv=f(s.get("markVol")), spot=f(s.get("fwdPx")),
                            oi_coin=f(oi.get(iid, {}).get("oiCcy"))))
    return out


RX_BIN = re.compile(r"^([A-Z]+)-(\d{6})-(\d+)-([CP])$")


def binance():
    out = []
    try:
        tick = {t["symbol"]: t for t in get("https://eapi.binance.com/eapi/v1/ticker")}
        mark = {t["symbol"]: t for t in get("https://eapi.binance.com/eapi/v1/mark")}
    except Exception:                                             # noqa: BLE001
        return out
    # Le `amount` de Binance est en quote, donc une prime. Il faut le prix de
    # l'indice pour convertir la quantité en notionnel, comme sur les trois
    # autres places.
    index = {}
    for dev in BINANCE_DEVISES:
        try:
            index[dev] = f(get("https://eapi.binance.com/eapi/v1/index"
                               f"?underlying={dev}USDT", essais=2, timeout=25).get("indexPrice"))
        except Exception:                                         # noqa: BLE001
            index[dev] = 0.0
    # L'open interest n'est exposé que par échéance : on relève d'abord les
    # échéances présentes dans les tickers, puis un appel par échéance.
    ech = {}
    for s in tick:
        m = RX_BIN.match(s)
        if m and m.group(1) in BINANCE_DEVISES:
            ech.setdefault(m.group(1), set()).add(m.group(2))
    oi = {}
    for dev, dates in ech.items():
        for d in sorted(dates):
            try:
                for r in get("https://eapi.binance.com/eapi/v1/openInterest"
                             f"?underlyingAsset={dev}&expiration={d}", essais=2, timeout=30):
                    oi[r["symbol"]] = f(r.get("sumOpenInterestUsd"))
            except Exception:                                     # noqa: BLE001
                continue
            time.sleep(0.15)
    for s, t in tick.items():
        m = RX_BIN.match(s)
        if not m or m.group(1) not in BINANCE_DEVISES:
            continue
        dev, exp_txt, k, cp = m.groups()
        try:
            exp = datetime(2000 + int(exp_txt[:2]), int(exp_txt[2:4]),
                           int(exp_txt[4:6]), 8, tzinfo=timezone.utc)
        except ValueError:
            continue
        mk = mark.get(s, {})
        px = index.get(dev, 0.0)
        out.append(dict(place="binance", sym=dev, exp=exp, strike=f(k), cp=cp,
                        oi_usd=oi.get(s, 0.0), vol_usd=f(t.get("volume")) * px,
                        iv=f(mk.get("markIV")), spot=px, oi_coin=0.0))
    return out


# ── Gamma : Deribit seul, reconstruit en Black-Scholes ───────────────────────
def gamma_bs(S, K, sigma, T):
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + sigma * sigma * T / 2) / (sigma * math.sqrt(T))
    return math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (S * sigma * math.sqrt(T))


def detail_deribit(lignes, dev):
    L = [x for x in lignes if x["place"] == "deribit" and x["sym"] == dev and x["spot"] > 0]
    if not L:
        return None
    spot = L[0]["spot"]
    par_strike, oi_c_k, oi_p_k = {}, {}, {}
    gex = oi_c = oi_p = vol_c = vol_p = 0.0
    ech = {}
    for x in L:
        T = jours_restants(x["exp"]) / 365.25
        g = gamma_bs(x["spot"], x["strike"], x["iv"], T)
        v = g * x["oi_coin"] * x["spot"] ** 2 * 0.01 * (1 if x["cp"] == "C" else -1)
        par_strike[x["strike"]] = par_strike.get(x["strike"], 0.0) + v
        gex += v
        if x["cp"] == "C":
            oi_c += x["oi_coin"]; vol_c += x["vol_usd"]
            oi_c_k[x["strike"]] = oi_c_k.get(x["strike"], 0.0) + x["oi_coin"]
        else:
            oi_p += x["oi_coin"]; vol_p += x["vol_usd"]
            oi_p_k[x["strike"]] = oi_p_k.get(x["strike"], 0.0) + x["oi_coin"]
        cle = x["exp"].date().isoformat()
        e = ech.setdefault(cle, {"date": cle, "jours": round(jours_restants(x["exp"]), 1),
                                 "oi_usd": 0.0, "atm_iv": None, "_e": None})
        e["oi_usd"] += x["oi_usd"]
        d = abs(x["strike"] - x["spot"])
        if x["iv"] > 0 and (e["_e"] is None or d < e["_e"]):
            e["_e"] = d; e["atm_iv"] = round(x["iv"] * 100, 2)
    strikes = sorted(set(list(oi_c_k) + list(oi_p_k)))
    mini = meilleur = None
    for s in strikes:
        d = sum((s - k) * o for k, o in oi_c_k.items() if s > k) \
          + sum((k - s) * o for k, o in oi_p_k.items() if s < k)
        if mini is None or d < mini:
            mini, meilleur = d, s
    cum, bascule = 0.0, None
    for k in sorted(par_strike):
        a = cum; cum += par_strike[k]
        if (a < 0 <= cum) or (a > 0 >= cum):
            bascule = k
    for e in ech.values():
        e.pop("_e", None); e["oi_usd"] = round(e["oi_usd"])
    return {
        "spot": round(spot, 2), "gex_m": round(gex / 1e6, 3),
        "regime": "long" if gex > 0 else "short",
        "bascule": bascule,
        "bascule_ecart_pct": None if not bascule else round((bascule / spot - 1) * 100, 2),
        "max_pain": meilleur,
        "max_pain_ecart_pct": None if not meilleur else round((meilleur / spot - 1) * 100, 2),
        "pc_oi": round(oi_p / oi_c, 4) if oi_c else None,
        "pc_volume": round(vol_p / vol_c, 4) if vol_c else None,
        "profil": sorted((k, round(v / 1e6, 4)) for k, v in par_strike.items()
                         if abs(k - spot) / spot <= 0.5),
        "oi_par_strike": [[k, round(oi_c_k.get(k, 0), 2), round(oi_p_k.get(k, 0), 2)]
                          for k in strikes if abs(k - spot) / spot <= 0.5],
        "murs": [{"strike": k, "gex_m": round(v / 1e6, 3),
                  "ecart_pct": round((k / spot - 1) * 100, 2),
                  "type": "call" if v > 0 else "put"}
                 for k, v in sorted(((k, v) for k, v in par_strike.items()
                                     if abs(k - spot) / spot <= 0.35),
                                    key=lambda t: -abs(t[1]))[:10]],
        "terme": sorted(ech.values(), key=lambda e: e["date"]),
    }


def agreger(lignes, cles):
    """Somme les dollars, et rend une FOURCHETTE d'IV — jamais une moyenne."""
    g = {}
    for x in lignes:
        k = tuple(x[c] for c in cles)
        e = g.setdefault(k, {"oi_usd": 0.0, "vol_usd": 0.0, "n": 0,
                             "iv": [], "exp": set(), "places": set(), "syms": set()})
        e["oi_usd"] += x["oi_usd"]; e["vol_usd"] += x["vol_usd"]; e["n"] += 1
        e["exp"].add(x["exp"].date().isoformat())
        e["places"].add(x["place"]); e["syms"].add(x["sym"])
        if 0 < x["iv"] < 5 and x["oi_usd"] > 0:
            e["iv"].append(x["iv"] * 100)
    for e in g.values():
        iv = sorted(e.pop("iv"))
        e["iv_min"] = round(iv[len(iv) // 20], 1) if iv else None
        e["iv_max"] = round(iv[-max(1, len(iv) // 20)], 1) if iv else None
        e["expiries"] = len(e.pop("exp"))
        e["places_n"] = len(e["places"]); e["places"] = sorted(e["places"])
        e["symboles"] = sorted(e.pop("syms"))
        e["oi_usd"] = round(e["oi_usd"]); e["vol_usd"] = round(e["vol_usd"])
    return g


def main():
    lignes, rates = [], []
    for nom, fn in (("deribit", deribit), ("bybit", bybit), ("okx", okx), ("binance", binance)):
        t0 = time.time()
        try:
            r = fn()
            lignes += r
            print(f"  {nom:<8} {len(r):>5} instruments · {time.time()-t0:4.1f}s")
        except Exception as e:                                    # noqa: BLE001
            rates.append(f"{nom} : {type(e).__name__} {e}")
            print(f"  {nom:<8} ÉCHEC : {e}")

    if not lignes:
        sys.exit("aucune place n'a répondu — fichiers laissés intacts")

    par_place = agreger(lignes, ["place"])
    par_sym = agreger(lignes, ["sym"])
    tout = agreger(lignes, [])

    detail = {}
    for dev in DERIBIT_DEVISES:
        d = detail_deribit(lignes, dev)
        if d:
            detail[dev] = d

    t = list(tout.values())[0] if tout else {}
    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": ["deribit.com", "api.bybit.com", "okx.com", "eapi.binance.com"],
        "total": {"oi_usd": t.get("oi_usd", 0), "vol_usd": t.get("vol_usd", 0),
                  "instruments": t.get("n", 0), "places": len(par_place)},
        "places": [dict(id=k[0], **v) for k, v in
                   sorted(par_place.items(), key=lambda kv: -kv[1]["oi_usd"])],
        "symboles": [dict(sym=k[0], **v) for k, v in
                     sorted(par_sym.items(), key=lambda kv: -kv[1]["oi_usd"])],
        "detail": detail,
        "convention": {
            "volume": "volume 24 h ramené en notionnel partout — quantité en coins × prix "
                      "du sous-jacent. Deribit publie une PRIME sous le nom volume_usd et "
                      "Bybit un notionnel sous le nom turnover : les additionner tels quels "
                      "mélangerait deux grandeurs sans rapport",
            "unite": "open interest ramené en DOLLARS de notionnel : chaque place le "
                     "libelle autrement (coins chez Deribit et Bybit, contrats chez OKX), "
                     "rien n'est comparable avant conversion",
            "iv": "fourchette du 5e au 95e centile pondérée par la présence, JAMAIS une "
                  "moyenne : moyenner des volatilités d'échéances différentes n'a pas de sens",
            "gamma": "calculé sur Deribit SEUL, en Black-Scholes depuis l'IV publiée. "
                     "Mélanger des grecques de trois maisons, chacune avec sa convention "
                     "de taux et son prix forward, donnerait un agrégat inauditable",
            "hypothese": "dealers longs des calls, shorts des puts — convention, non observée",
            "max_pain": "prix minimisant ce que les vendeurs verseraient à l'échéance. "
                        "Curiosité de marché, pas prévision",
        },
        "echecs": rates,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for path, contenu in ((OUT_JSON, body), (OUT_JS, "window.__CRYPTO_OPTIONS__=" + body + ";\n")):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(contenu)
        tmp.replace(path)

    print(f"\n  TOTAL  OI {t.get('oi_usd',0)/1e9:.2f} Md$ · volume 24 h "
          f"{t.get('vol_usd',0)/1e6:.0f} M$ · {t.get('n',0)} instruments · {len(par_place)} places")
    for p in payload["places"]:
        print(f"    {p['id']:<8} OI {p['oi_usd']/1e9:>6.2f} Md$ · vol {p['vol_usd']/1e6:>7.1f} M$"
              f" · {p['n']:>5} instr · IV {p['iv_min']}–{p['iv_max']} %")
    print(f"  {len(body)//1024} Ko")
    for r in rates:
        print(f"  échec : {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
