#!/usr/bin/env python3
"""poids_exacts_europe.py — poids exacts des membres de 6 indices européens.

Source : les avoirs publiés chaque jour par un ETF qui RÉPLIQUE PHYSIQUEMENT
l'indice (pas un ETF « pays » MSCI, pas un swap).

    indice    ETF                                             fournisseur
    cac40     Amundi CAC 40 UCITS ETF Dist   FR0007052782     Amundi (API JSON)
    dax40     iShares Core DAX UCITS ETF (DE) DE0005933931    iShares (API JSON BlackRock)
    ftse100   iShares Core FTSE 100 UCITS ETF IE0005042456    iShares
    ftsemib   iShares FTSE MIB UCITS ETF EUR  IE00B1XNH568    iShares
    ibex35    Amundi IBEX 35 UCITS ETF Dist   FR0010251744    Amundi
    smi       iShares SMI ETF (CH)            CH0008899764    iShares

Chaque fonction rend (date_iso, lignes, source_url) avec
    lignes = [{"isin": str|None, "ticker": str|None, "nom": str, "poids": float}, ...]
triées par poids décroissant, poids en % RENORMALISÉS sur les seules lignes
actions (liquidités, futures et appels de marge retirés) : somme = 100.
Les lignes d'un même émetteur sont regroupées (actions « prime de fidélité »
d'Air Liquide, L'Oréal, Engie chez Amundi).

Les fonctions iShares acceptent date="AAAA-MM-JJ" (jour ouvré) pour obtenir les
avoirs à une date PASSÉE (DAX et SMI via un autre ETF dès 2006, FTSE 100 dès
11/2006, FTSE MIB dès 07/2007 ; voir recherche/europe.json). Amundi ne publie
que la composition du jour.

Dépendances : bibliothèque standard + requests. Aucun identifiant, aucun cookie.
Testé le 30/09/2026 depuis une IP résidentielle : les deux API répondent aussi
avec l'User-Agent par défaut de requests et sans en-tête particulier (Amundi :
POST JSON, Content-Type application/json). NON testé depuis une IP de nuage :
blackrock.com / ishares.com sont servis par Akamai (edgekey.net) — risque de
refus des IP de centres de données ; www.amundietf.fr est servi directement
(IP Azure, pas de CDN anti-robot visible).

Usage : python3 poids_exacts_europe.py          -> teste les 6 indices
        python3 poids_exacts_europe.py dax40 2015-06-30
"""
import datetime
import re
import sys
import time
import warnings

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 45
ESSAIS = 3

ISHARES_JSON = ("https://www.blackrock.com/varnish-api/uk-retail01-product-data/"
                "product-data/api/v2/get-product-data")
ISHARES_CSV = ("https://www.blackrock.com/varnish-api/uk-retail01-product-data/"
               "product-data/api/v1/get-fund-document")
AMUNDI = "https://www.amundietf.fr/mapi/ProductAPI/getProductsData"

# portfolioId iShares, site et locale qui acceptent ce fonds, taille attendue
ISHARES = {
    "dax40":   {"pid": "251464", "site": "ishares-uk",    "locale": "en_GB", "n": 40},
    "ftse100": {"pid": "251795", "site": "ishares-uk",    "locale": "en_GB", "n": 100},
    "ftsemib": {"pid": "251805", "site": "ishares-uk",    "locale": "en_GB", "n": 40},
    "smi":     {"pid": "261154", "site": "ch-ishares-v2", "locale": "en_CH", "n": 20},
}
AMUNDI_ETF = {
    "cac40":  {"isin": "FR0007052782", "n": 40},
    "ibex35": {"isin": "FR0010251744", "n": 35},
}


# ------------------------------------------------------------------ HTTP
def _session(session):
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/csv, */*"})
    return s


def _requete(session, methode, url, **kw):
    """GET/POST avec reprises ; refuse une page HTML (blocage anti-robot, page d'erreur)."""
    derniere = None
    for essai in range(ESSAIS):
        try:
            r = session.request(methode, url, timeout=TIMEOUT, **kw)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and "html" not in ctype:
                return r
            derniere = f"HTTP {r.status_code} ({ctype[:40]}) : {r.text[:160]!r}"
        except requests.RequestException as e:
            derniere = repr(e)
        time.sleep(2 * (essai + 1))
    raise RuntimeError(f"{url} : échec après {ESSAIS} essais — {derniere}")


# ------------------------------------------------------------------ outils
def _finaliser(lignes, attendu, indice):
    """Regroupe par ISIN (sinon nom), renormalise à 100, trie, contrôle la taille."""
    agg = {}
    for l in lignes:
        cle = l["isin"] or l["nom"]
        if cle in agg:
            agg[cle]["poids"] += l["poids"]
        else:
            agg[cle] = dict(l)
    tot = sum(l["poids"] for l in agg.values())
    if tot <= 0:
        raise RuntimeError(f"{indice} : aucune ligne action")
    out = [dict(l, poids=round(100.0 * l["poids"] / tot, 5)) for l in agg.values()]
    out.sort(key=lambda l: -l["poids"])
    if attendu is not None and len(out) != attendu:
        warnings.warn(f"{indice} : {len(out)} lignes actions pour {attendu} membres attendus "
                      "(reliquat hors indice ou changement de composition en cours)")
    return out


def _date_param(date):
    if date is None:
        return None
    if isinstance(date, (datetime.date, datetime.datetime)):
        return date.strftime("%Y%m%d")
    return str(date).replace("-", "")


# ------------------------------------------------------------------ iShares
def _ishares(indice, date=None, session=None, exclure_isin=()):
    cfg = ISHARES[indice]
    s = _session(session)
    params = {"appType": "PRODUCT_PAGE", "appSubType": "ISHARES", "targetSite": cfg["site"],
              "locale": cfg["locale"], "portfolioId": cfg["pid"], "component": "holdings",
              "userType": "individual"}
    d = _date_param(date)
    if d:
        params["asOfDate"] = d
    r = _requete(s, "GET", ISHARES_JSON, params=params)
    url = r.url
    dp = (r.json()["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]
          ["dataPointsByNameMap"])
    asof = str(dp["asOfDate"]["value"])
    poids = dp["holdingPercent"]["value"] or []
    if d and (asof != d or not poids):
        # l'API renvoie la date du jour avec une liste vide quand la date demandée n'existe pas
        raise RuntimeError(f"{indice} : pas d'avoirs iShares au {date} (week-end, férié ou trou "
                           "connu 12/2011, 01-06/2017)")
    col = lambda k: dp[k]["value"] or [None] * len(poids)
    lignes = []
    for isin, tic, nom, w, cl in zip(col("isin"), col("ticker"), col("issueName"), poids,
                                      col("assetClass")):
        if cl != "Equity" or not isinstance(w, (int, float)) or isin in exclure_isin:
            continue
        lignes.append({"isin": isin or None, "ticker": tic or None, "nom": nom, "poids": float(w)})
    date_iso = f"{asof[:4]}-{asof[4:6]}-{asof[6:8]}"
    # taille attendue contrôlée seulement pour la composition du jour (elle a varié dans le passé)
    return date_iso, _finaliser(lignes, None if d else cfg["n"], indice), url


# ------------------------------------------------------------------ Amundi
_FIDELITE = re.compile(r"\s+PRIME\s+(DE\s+)?FIDELITE(\s+\d{4})?\s*$")


def _norm(nom):
    return re.sub(r"[^A-Z0-9 ]", " ", (nom or "").upper()).split()


def _amundi(indice, session=None):
    cfg = AMUNDI_ETF[indice]
    s = _session(session)
    corps = {"context": {"countryCode": "FRA", "languageCode": "fr", "userProfileName": "RETAIL"},
             "productIds": [cfg["isin"]], "productType": "PRODUCT",
             "composition": {"compositionFields": ["date", "type", "bbg", "isin", "name",
                                                   "weight"]}}
    r = _requete(s, "POST", AMUNDI, json=corps, headers={"Content-Type": "application/json"})
    comp = (r.json()["products"][0].get("composition") or {}).get("compositionData") or []
    brut, dates = [], set()
    for x in comp:
        c = x.get("compositionCharacteristics") or {}
        if not str(c.get("type") or "").startswith("EQUITY"):
            continue
        dates.add(c.get("date"))
        bbg = c.get("bbg") or ""
        brut.append({"isin": c.get("isin"), "ticker": bbg.split()[0] if bbg else None,
                     "nom": c.get("name") or "", "poids": 100.0 * float(x.get("weight") or 0)})
    # rattacher les lignes « prime de fidélité » à la ligne ordinaire du même émetteur
    ordinaires = [l for l in brut if not _FIDELITE.search(l["nom"])]
    lignes = list(ordinaires)
    for l in brut:
        m = _FIDELITE.search(l["nom"])
        if not m:
            continue
        base = _norm(l["nom"][:m.start()])
        cand = [o for o in ordinaires if _norm(o["nom"])[:len(base)] == base]
        if len(cand) == 1:
            cand[0]["poids"] += l["poids"]
        else:
            warnings.warn(f"{indice} : ligne « {l['nom']} » non rattachée ({len(cand)} candidats)")
            lignes.append(l)
    date_iso = max(d for d in dates if d) if dates else None
    if len(dates) > 1:
        warnings.warn(f"{indice} : plusieurs dates dans le fichier Amundi {sorted(dates)}")
    return date_iso, _finaliser(lignes, cfg["n"], indice), AMUNDI + f"  [POST productIds={cfg['isin']}]"


# ------------------------------------------------------------------ API publique
def cac40(session=None):
    """CAC 40 — Amundi CAC 40 UCITS ETF Dist (FR0007052782). Composition du jour seulement."""
    return _amundi("cac40", session=session)


def ibex35(session=None):
    """IBEX 35 — Amundi IBEX 35 UCITS ETF Dist (FR0010251744). Composition du jour seulement."""
    return _amundi("ibex35", session=session)


def dax40(date=None, session=None):
    """DAX 40 — iShares Core DAX UCITS ETF (DE). date='AAAA-MM-JJ' pour une date passée."""
    return _ishares("dax40", date=date, session=session)


def ftse100(date=None, session=None, exclure_isin=None):
    """FTSE 100 — iShares Core FTSE 100 UCITS ETF. L'ETF garde un reliquat EVRAZ
    (GB00B71N6K86, 0,0002 %), sortie de l'indice le 21/03/2022 et suspendue : exclue par
    défaut pour les dates >= 21/03/2022 (exclure_isin=() pour la garder). Résultat : 100
    lignes au 29/09/2026."""
    if exclure_isin is None:
        d = _date_param(date)
        exclure_isin = ("GB00B71N6K86",) if (d is None or d >= "20220321") else ()
    return _ishares("ftse100", date=date, session=session, exclure_isin=exclure_isin)


def ftsemib(date=None, session=None):
    """FTSE MIB — iShares FTSE MIB UCITS ETF EUR (Dist)."""
    return _ishares("ftsemib", date=date, session=session)


def smi(date=None, session=None):
    """SMI — iShares SMI ETF (CH) (disponible depuis 11/2014)."""
    return _ishares("smi", date=date, session=session)


INDICES = {"cac40": cac40, "dax40": dax40, "ftse100": ftse100, "ftsemib": ftsemib,
           "ibex35": ibex35, "smi": smi}


def tous(session=None):
    """{code: (date_iso, lignes, url) | Exception} pour les 6 indices."""
    s = _session(session)
    res = {}
    for code, f in INDICES.items():
        try:
            res[code] = f(session=s)
        except Exception as e:  # une source en panne ne bloque pas les autres
            res[code] = e
    return res


def _test():
    ok = True
    s = _session(None)
    for code, f in INDICES.items():
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                date_iso, lignes, url = f(session=s)
            tot = sum(l["poids"] for l in lignes)
            sans_isin = sum(1 for l in lignes if not l["isin"])
            assert abs(tot - 100) < 0.01, tot
            assert all(set(l) == {"isin", "ticker", "nom", "poids"} for l in lignes)
            print(f"OK  {code:8} {date_iso}  {len(lignes):3} lignes  somme={tot:.3f}  "
                  f"sans ISIN={sans_isin}  top={lignes[0]['nom']} {lignes[0]['poids']:.2f} %")
            for x in w:
                print(f"    avertissement : {x.message}")
        except Exception as e:
            ok = False
            print(f"ERR {code:8} {e}")
    # un appel historique pour vérifier le paramètre de date
    try:
        d, l, _ = dax40("2010-01-29", session=s)
        print(f"OK  dax40 historique {d} {len(l)} lignes, top {l[0]['nom']} {l[0]['poids']:.2f} %")
    except Exception as e:
        ok = False
        print(f"ERR dax40 historique {e}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        f = INDICES[sys.argv[1]]
        args = sys.argv[2:3]
        d, lignes, url = f(*args) if args else f()
        print(d, url)
        for l in lignes:
            print(f"{l['poids']:8.4f}  {l['isin']!s:12}  {l['ticker']!s:8}  {l['nom']}")
    else:
        sys.exit(0 if _test() else 1)
