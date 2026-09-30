#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cours_divisions.py — cours de fin de mois AJUSTÉS des divisions et historique
des divisions, titre par titre (Yahoo Finance, toute la vie cotée).

⚠ Yahoo : la clôture « close » est ajustée des DIVISIONS (pas des dividendes) — donc sur la
base d'actions ACTUELLE, comme les nombres d'actions des fournisseurs qui
ajustent (stockanalysis, TradingView).
Cache disque : un fichier JSON par symbole, réutilisé 7 jours.
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

try:
    from curl_cffi import requests as _http
    _KW = {"impersonate": "chrome120"}
except ImportError:
    import requests as _http
    _KW = {}

CACHE = os.environ.get("COURS_CACHE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cours_cache")


def _lire_cache(sym):
    f = os.path.join(CACHE, urllib.parse.quote(sym, safe="") + ".json")
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < 7 * 86400:
        try:
            return json.load(open(f))
        except Exception:
            return None
    return None


def nettoyer(mois):
    """⚠ Yahoo publie des clôtures mensuelles FAUSSES, isolées (3i Group, Londres :
    19,81 en déc. 2023 entre 2 232 et 2 479 pence ; la vraie clôture était vers
    2 420 — ni des livres, ni des pence : P/E du FTSE 100 lu à 3,3). Un point à plus
    de ×4 (ou moins de ÷4) de ses deux voisins, quand ceux-ci s'accordent, est
    RETIRÉ, pas corrigé : un mois manquant vaut mieux qu'un cours deviné (le membre
    compte alors comme non chiffré, et la couverture le dit). Un krach suivi d'un
    rebond complet le mois suivant n'existe pas sur des clôtures de fin de mois."""
    ks = sorted(mois)
    out = dict(mois)
    for i in range(1, len(ks) - 1):
        a, x, b = out.get(ks[i - 1]), mois[ks[i]], mois[ks[i + 1]]
        if not (a and b and x) or not (0.7 <= a / b <= 1.43):
            continue
        r = x / ((a * b) ** 0.5)
        if r > 4 or r < 0.25:
            del out[ks[i]]
    # ⚠ PRÉFIXE ÉTRANGER : une cotation reprise par une autre société garde parfois
    # un historique sans rapport (LIN.DE, Linde plc à Francfort : 0,47 € jusqu'en
    # oct. 2009, puis 54 €). Un saut vers le HAUT de plus de ×10 en un mois, quand le
    # nouveau niveau tient (les deux mois suivants à moins de ×2), coupe la série :
    # ce qui précède est retiré. (Les krachs, vers le bas, sont réels : Wirecard.)
    ks = sorted(out)
    for i in range(len(ks) - 3, 0, -1):
        a, b = out[ks[i - 1]], out[ks[i]]
        if a and b and b / a > 10 and all(0.5 <= out[ks[i + j]] / b <= 2 for j in (1, 2)):
            for k in ks[:i]:
                del out[k]
            break
    return out


def cours(sym, essais=3):
    """{"mois": {"AAAA-MM": clôture ajustée des divisions}, "divisions": [[date, facteur]], "devise": …}
    ou None. Le facteur d'une division 4 pour 1 vaut 4."""
    c = _lire_cache(sym)
    if c is not None:
        if c and c.get("mois"):
            c["mois"] = nettoyer(c["mois"])
        return c or None
    # ⚠ `range=max&interval=1mo` rend des barres de TROIS MOIS sur une longue vie
    # cotée (Apple : la barre « 2015-12 » close fin février 2016) ; les bornes
    # explicites gardent le pas mensuel.
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=0&period2=%d&interval=1mo&events=split"
         % (urllib.parse.quote(sym, safe=""), int(time.time())))
    d = None
    for e in range(essais):
        try:
            r = _http.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=40, **_KW)
            if r.status_code == 200:
                d = r.json()
                break
            if r.status_code == 404:
                break
            time.sleep(2 * (e + 1) * (3 if r.status_code == 429 else 1))
        except Exception:
            time.sleep(2 * (e + 1))
    out = {}
    try:
        res = d["chart"]["result"][0]
        off = int((res.get("meta") or {}).get("gmtoffset") or 0)
        mois = {}
        for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
            if isinstance(c, (int, float)) and c > 0:
                mois[datetime.fromtimestamp(t + off + 43200, timezone.utc).strftime("%Y-%m")] = c
        div = []
        for s in ((res.get("events") or {}).get("splits") or {}).values():
            num, den = s.get("numerator"), s.get("denominator")
            if num and den:
                div.append([datetime.fromtimestamp(s["date"], timezone.utc).strftime("%Y-%m-%d"), num / den])
        out = {"mois": nettoyer(mois), "divisions": sorted(div), "devise": (res.get("meta") or {}).get("currency")}
    except Exception:
        out = {}
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, urllib.parse.quote(sym, safe="") + ".json"), "w") as fh:
        json.dump(out, fh)
    return out or None


if __name__ == "__main__":
    for s in sys.argv[1:]:
        c = cours(s)
        print(s, c and (len(c["mois"]), min(c["mois"]), c["divisions"], c["devise"]))
