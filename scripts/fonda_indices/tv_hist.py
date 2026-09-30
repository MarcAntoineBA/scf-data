#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tv_hist.py — vingt exercices annuels par société, d'une seule source pour tous les
pays : l'écran public de TradingView (champs « _fy_h », du plus récent au plus ancien).

Pourquoi : les états détaillés de l'univers international ne remontent qu'à 2021
pour la plupart des sociétés (cinq exercices). Sans profondeur, pas de série
2006-2025 pour le CAC 40, le Nikkei ou le Nifty.

Ce que la source donne, vérifié le 01/10/2026 sur 25 cotations de 20 places :
  · bénéfice net, chiffre d'affaires, BPA dilué, dividende par action de
    l'exercice — CONVERTIS dans la devise de cotation (HSBC à Hong Kong : en HKD),
    et dans l'unité principale (Londres cote en pence, les fondamentaux sont en
    livres) ;
  · BPA et dividende AJUSTÉS des divisions (Tencent 2006 : 0,114 HKD = 0,57 / 5),
    donc sur la même base que la clôture ajustée de Yahoo ;
  · l'étiquette d'exercice suit la règle du moteur : un exercice clos entre juin Y
    et mai Y+1 porte l'étiquette Y (Toyota mars 2026 → 2025, BHP juin 2026 → 2026).
Pas de fonds propres historiques ni de nombre d'actions : l'actif net reste lu
dans les états détaillés (cinq ans hors États-Unis).
"""
import json
import os
import time

import requests

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ICI, "tv_cache")
COLS = ["net_income_fy_h", "total_revenue_fy_h", "earnings_per_share_diluted_fy_h", "earnings_per_share_basic_fy_h",
        "dps_common_stock_prim_issue_fy_h", "fiscal_period_fy_h", "fiscal_period_end_fy", "currency",
        "fundamental_currency_code", "description"]
PLACE = {"PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT", "LS": "EURONEXT", "DE": "XETR", "F": "FWB",
         "L": "LSE", "T": "TSE", "HK": "HKEX", "SS": "SSE", "SZ": "SZSE", "NS": "NSE", "BO": "BSE", "KS": "KRX",
         "KQ": "KRX", "TW": "TWSE", "TWO": "TPEX", "SA": "BMFBOVESPA", "TO": "TSX", "V": "TSXV", "AX": "ASX",
         "MI": "MIL", "MC": "BME", "SW": "SIX", "MX": "BMV", "CO": "OMXCOP", "ST": "OMXSTO", "HE": "OMXHEX",
         "OL": "OSL", "VI": "VIE", "IR": "EURONEXT", "JO": "JSE"}


def candidats(sym):
    """Symboles TradingView plausibles pour un symbole Yahoo, du plus probable au moins."""
    if "." in sym and sym.rsplit(".", 1)[1] in PLACE:
        base, suf = sym.rsplit(".", 1)
        p = PLACE[suf]
        b = base.replace("-", ".")
        out = [p + ":" + b]
        if suf == "HK":
            out = [p + ":" + str(int(base))] if base.isdigit() else out
        if suf in ("NS", "BO"):
            # TradingView : « BAJAJ_AUTO » pour BAJAJ-AUTO, « M&M » tel quel
            out = [p + ":" + base.replace("-", "_"), p + ":" + base.replace("&", "_"), p + ":" + base.replace("&", "")]
        if suf == "MX":
            # TradingView sépare la série : « AMX/B », « KOF/UBL », « TLEVISA/CPO »
            b0 = base.rstrip("*").replace("-", "")
            out += [p + ":" + b0[:-k] + "/" + b0[-k:] for k in (1, 3, 2) if len(b0) > k + 1] + [p + ":" + b0[:-1]]
        if suf == "L":
            # TradingView termine d'un point les codes de Londres de deux lettres
            # ou moins (« LSE:BP. », « LSE:NG. »)
            out += [p + ":" + b + ".", p + ":" + b.rstrip(".")]
        if suf in ("PA", "AS", "BR", "LS", "IR"):
            out += ["EURONEXT:" + b]
        if suf == "DE":
            out += ["FWB:" + b]
        return list(dict.fromkeys(out))
    b = sym.replace("-", ".")
    return ["NYSE:" + b, "NASDAQ:" + b, "AMEX:" + b, "CBOE:" + b]


FORCE = {}   # symbole Yahoo → symbole TradingView trouvé par l'ISIN (prioritaire)
YAHOO_DE_PLACE = {"XETR": ".DE", "FWB": ".F", "LSE": ".L", "SIX": ".SW", "MIL": ".MI", "BME": ".MC", "TSX": ".TO",
                  "BMV": ".MX", "BMFBOVESPA": ".SA", "NSE": ".NS", "BSE": ".BO", "TSE": ".T", "HKEX": ".HK",
                  "SSE": ".SS", "SZSE": ".SZ", "ASX": ".AX", "KRX": ".KS", "TWSE": ".TW", "TPEX": ".TWO",
                  "NYSE": "", "NASDAQ": "", "AMEX": "", "OMXHEX": ".HE", "OMXSTO": ".ST", "OMXCOP": ".CO", "OSL": ".OL"}
EURONEXT_PAYS = {"FR": ".PA", "NL": ".AS", "BE": ".BR", "PT": ".LS", "IE": ".IR", "LU": ".PA"}


def _lire(f):
    for _ in range(5):
        try:
            return json.load(open(f)) if os.path.exists(f) else {}
        except ValueError:
            time.sleep(1)   # écriture concurrente en cours
    return {}


def _ecrire(f, d):
    """Écriture ATOMIQUE : deux constructions simultanées se partagent ce cache
    (une lecture tombée au milieu d'une écriture a cassé un passage le 01/10)."""
    t = "%s.%d.tmp" % (f, os.getpid())
    json.dump(d, open(t, "w"))
    os.replace(t, f)


def recherche_isin(isin, bourse=None):
    """Symbole TradingView d'un ISIN (recherche publique de symboles), la bourse
    voulue d'abord. Cache disque."""
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, "isin.json")
    memo = _lire(f)
    if isin not in memo:
        res = []
        for e in range(3):
            try:
                r = requests.get("https://symbol-search.tradingview.com/symbol_search/v3/",
                                 params={"text": isin, "search_type": "stock", "hl": 0},
                                 headers={"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com",
                                          "Referer": "https://www.tradingview.com/"}, timeout=30)
                if r.status_code == 200:
                    res = [(x.get("exchange"), x.get("symbol"), x.get("isin")) for x in r.json().get("symbols") or []
                           if x.get("isin") == isin and x.get("type") == "stock"]
                    break
            except Exception:
                pass
            time.sleep(2 * (e + 1))
        memo = dict(_lire(f), **{isin: res})
        time.sleep(0.3)
        _ecrire(f, memo)
    res = memo[isin]
    if not res:
        return None
    res = sorted(res, key=lambda x: 0 if x[0] == bourse else 1)
    return "%s:%s" % (res[0][0], res[0][1])


def yahoo_de_tv(tv, isin=None):
    """Symbole Yahoo d'une cotation TradingView (« SIX:RO » → « RO.SW »)."""
    b, s = tv.split(":", 1)
    if b == "EURONEXT":
        suf = EURONEXT_PAYS.get((isin or "FR")[:2], ".PA")
    else:
        suf = YAHOO_DE_PLACE.get(b)
    if suf is None:
        return None
    if b == "LSE":
        s = s.rstrip(".").replace(".", "-")
    elif b in ("TSX", "NYSE", "NASDAQ", "AMEX"):
        s = s.replace(".", "-")
    elif b in ("NSE", "BSE"):
        s = s.replace("_", "-")
    elif b == "HKEX":
        s = s.zfill(4)
    return s + suf


def _scan(tickers):
    for e in range(4):
        try:
            r = requests.post("https://scanner.tradingview.com/global/scan",
                              json={"symbols": {"tickers": tickers}, "columns": COLS},
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            if r.status_code == 200:
                return {row["s"]: dict(zip(COLS, row["d"])) for row in r.json().get("data") or []}
        except Exception:
            pass
        time.sleep(3 * (e + 1))
    return None


def charger(symboles):
    """{symbole Yahoo: données TV ou {}} — cache disque (7 jours), requêtes par 200."""
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, "tv.json")
    memo = _lire(f)
    # une absence ne vaut « fraîche » que si aucune piste nouvelle (ISIN) n'est apparue
    frais = lambda s: (s in memo and time.time() - memo[s].get("_t", 0) < 7 * 86400
                       and (memo[s].get("_tv") or s not in FORCE))
    reste = [s for s in dict.fromkeys(symboles) if s and not frais(s)]
    for rang in range(6):  # 1er candidat, puis les variantes pour ceux qui manquent
        demande = {}
        for s in reste:
            c = ([FORCE[s]] if s in FORCE else []) + candidats(s)
            if rang < len(c):
                demande[c[rang]] = s
        if not demande:
            break
        cles = list(demande)
        for i in range(0, len(cles), 200):
            res = _scan(cles[i:i + 200])
            if res is None:
                continue
            for tv, d in res.items():
                if d.get("net_income_fy_h") or d.get("earnings_per_share_diluted_fy_h"):
                    memo[demande[tv]] = dict(d, _tv=tv, _t=time.time())
        reste = [s for s in reste if not frais(s)]
    for s in reste:
        memo[s] = {"_t": time.time()}
    _ecrire(f, dict(_lire(f), **memo))
    return {s: memo.get(s) or {} for s in symboles if s}


def exercices(d):
    """[{an, eps, ni, rev, dps}] du plus ancien au plus récent (année = étiquette = règle du moteur)."""
    ans = d.get("fiscal_period_fy_h") or []
    col = lambda k: d.get(k) or []
    out = []
    for i, a in enumerate(ans):
        if a is None:
            continue
        v = lambda k: col(k)[i] if i < len(col(k)) else None
        eps = v("earnings_per_share_diluted_fy_h")
        if eps is None:
            eps = v("earnings_per_share_basic_fy_h")
        out.append({"an": int(a), "eps": eps, "ni": v("net_income_fy_h"), "rev": v("total_revenue_fy_h"),
                    "dps": v("dps_common_stock_prim_issue_fy_h")})
    return sorted(out, key=lambda x: x["an"])


def actions_implicites(ex):
    """Nombre d'actions (base actuelle) = bénéfice / BPA, quand le rapport est net ;
    sinon celui de l'exercice voisin (un bénéfice proche de zéro rend le rapport fou)."""
    sh = {}
    for x in ex:
        if x["ni"] and x["eps"] and (x["ni"] > 0) == (x["eps"] > 0) and abs(x["eps"]) > 1e-9:
            sh[x["an"]] = x["ni"] / x["eps"]
    ans = sorted(sh)
    # écarte les rapports aberrants (> ×1,5 de la médiane des voisins)
    ok = {}
    for i, a in enumerate(ans):
        vois = [sh[b] for b in ans[max(0, i - 2):i + 3] if b != a]
        if not vois:
            ok[a] = sh[a]
            continue
        vois.sort()
        med = vois[len(vois) // 2]
        if 1 / 1.5 <= sh[a] / med <= 1.5:
            ok[a] = sh[a]
    out = {}
    for x in ex:
        a = x["an"]
        if a in ok:
            out[a] = ok[a]
        elif ok:
            b = min(ok, key=lambda k: abs(k - a))
            if abs(b - a) <= 2:
                out[a] = ok[b]
    return out
