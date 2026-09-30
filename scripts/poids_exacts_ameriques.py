"""Poids exacts du jour du S&P/TSX Composite et du S&P/BMV IPC.

Source : avoirs publiés par BlackRock des ETF qui répliquent physiquement ces indices :
  - S&P/TSX Composite : iShares Core S&P/TSX Capped Composite (XIC, BlackRock Canada).
    XIC suit la version « Capped » (plafond 10 %). Les membres sont les mêmes ; les poids sont
    identiques tant qu'aucun titre ne dépasse 10 % (RY pesait 7,8 % au 29/09/2026).
  - S&P/BMV IPC : iShares NAFTRAC (BlackRock Mexique).

Chaque fonction rend (date_iso, lignes, source_url). Chaque ligne est
{"ticker", "isin", "nom", "poids"}, et la somme des poids vaut 100.
  - Le poids est recalculé depuis la valeur de marché, plus précise que la colonne
    « Weight » arrondie à 2 décimales, puis normalisé sur les seuls membres de l'indice.
  - Sont écartés : liquidités, futures, fonds monétaires, droits et bons de souscription,
    CVR, lignes internes de BlackRock (ticker numérique, ex. « 2299955D ») et titres
    suspendus que le fonds garde (valeur nulle ou poids < 0,001 %).
  - L'ISIN vient du flux JSON du même fonds, joint par ticker. Le CSV n'a pas d'ISIN.

Paramètre facultatif date="AAAA-MM-JJ" : avoirs historiques (paramètre asOfDate).
  - Disponibles en fin de mois ouvrée depuis 2006-11 (XIC) et 2009-11 (NAFTRAC).
  - Sans date, on prend les avoirs publiés les plus récents (J-1 ouvré en général).

Dépendances : bibliothèque standard + requests.

Nuage (GitHub Actions) : non testé depuis une IP de centre de données. blackrock.com est
derrière Akamai. Si la réponse n'est pas un CSV (page HTML, 403), le module lève
SourceRefusee au lieu de rendre des poids faux.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import time

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_BASE = {
    "tsx": "https://www.blackrock.com/ca/investors/en/products/239837/"
           "ishares-sptsx-capped-composite-index-etf/1464253357814.ajax",
    "ipc": "https://www.blackrock.com/mx/intermediarios/productos/251895/"
           "ishares-naftrac-fund/1501904811835.ajax",
}
_CSV_NAME = {"tsx": "XIC_holdings", "ipc": "NAFTRAC_holdings"}
_EQUITY = {"Equity", "Renta Variable", "Renda Variável", "Acciones", "Actions"}
_EXCLU_NOM = re.compile(r"\b(RTS|RIGHTS?|WTS|WARRANTS?|CVR|DERECHOS?|CUPON|SUBSCRIPTION|RECEIPTS?)\b", re.I)
_TICKER_INTERNE = re.compile(r"^\d{4,}[A-Z]?$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_MOIS = {"jan": 1, "ene": 1, "feb": 2, "fév": 2, "fev": 2, "mar": 3, "apr": 4, "abr": 4, "avr": 4,
         "may": 5, "mai": 5, "jun": 6, "juin": 6, "jul": 7, "juil": 7, "aug": 8, "ago": 8, "aoû": 8,
         "sep": 9, "set": 9, "oct": 10, "out": 10, "nov": 11, "dec": 12, "dic": 12, "dez": 12, "déc": 12}


class SourceRefusee(RuntimeError):
    """La source n'a pas renvoyé le fichier attendu (HTML, 403, fichier vide)."""


def _get(url: str, params: dict, tentatives: int = 3) -> str:
    derniere = None
    for i in range(tentatives):
        try:
            r = requests.get(url, params=params, timeout=45,
                             headers={"User-Agent": UA, "Accept": "text/csv,application/json,*/*"})
            if r.status_code == 200:
                return r.content.decode("utf-8-sig", "replace")
            derniere = f"HTTP {r.status_code}"
            if r.status_code in (401, 403):
                break
        except requests.RequestException as e:  # réseau
            derniere = repr(e)
        time.sleep(2 * (i + 1))
    raise SourceRefusee(f"{url} : {derniere}")


def _num(s: str) -> float | None:
    s = (s or "").strip().replace("\xa0", "")
    if s in ("", "-", "--"):
        return None
    if re.fullmatch(r"-?[\d.]+,\d+", s):  # format brésilien 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _date_entete(txt: str) -> str | None:
    for ligne in txt.splitlines()[:4]:
        if "as of" in ligne.lower() or "fecha" in ligne.lower():
            m = re.search(r'"([^"]+)"', ligne)
            if not m or m.group(1).strip() == "-":
                return None
            v = m.group(1).strip().lower().replace(".", "")
            m1 = re.fullmatch(r"([a-zéû]+) (\d{1,2}), (\d{4})", v)          # Sep 29, 2026
            m2 = re.fullmatch(r"(\d{1,2})[- ]([a-zéû]+)[- ](\d{4})", v)       # 29-sep-2026 / 29 set 2026
            if m1:
                mo, d, y = m1.groups()
            elif m2:
                d, mo, y = m2.groups()
            else:
                return None
            mois = _MOIS.get(mo[:4]) or _MOIS.get(mo[:3])
            return _dt.date(int(y), mois, int(d)).isoformat() if mois else None
    return None


def _isins(indice: str, params: dict) -> dict:
    """ticker -> ISIN depuis le flux JSON du fonds (facultatif : {} en cas d'échec)."""
    p = {"tab": "all", "fileType": "json"}
    if "asOfDate" in params:
        p["asOfDate"] = params["asOfDate"]
    try:
        import json
        d = json.loads(_get(_BASE[indice], p, tentatives=2))
    except Exception:
        return {}
    out = {}
    for row in d.get("aaData", []):
        if not row or not isinstance(row[0], str):
            continue
        isin = next((c for c in row if isinstance(c, str) and _ISIN.match(c)), None)
        if isin:
            out[row[0]] = isin
    return out


def _lire(indice: str, date: str | None = None):
    params = {"fileType": "csv", "fileName": _CSV_NAME[indice], "dataType": "fund"}
    if date:
        params["asOfDate"] = date.replace("-", "")
    txt = _get(_BASE[indice], params)
    if txt.lstrip().lower().startswith("<!doctype") or "<html" in txt[:500].lower():
        raise SourceRefusee(f"{indice} : page HTML au lieu du CSV")
    date_iso = _date_entete(txt)
    if date_iso is None:
        raise SourceRefusee(f"{indice} : aucun avoir publié pour la date demandée ({date})")
    rows = [r for r in csv.reader(io.StringIO(txt)) if len(r) >= 6]
    entete, data = rows[0], rows[1:]
    ix = {k: i for i, k in enumerate(entete)}
    for k in ("Ticker", "Name", "Asset Class", "Market Value"):
        if k not in ix:
            raise SourceRefusee(f"{indice} : colonne {k!r} absente, format changé : {entete}")
    ip = ix.get("Price")
    garde, ecarte = [], []
    for r in data:
        tk, nom, cl = r[ix["Ticker"]].strip(), r[ix["Name"]].strip(), r[ix["Asset Class"]].strip()
        mv = _num(r[ix["Market Value"]])
        px = _num(r[ip]) if ip is not None else None
        raison = None
        if cl not in _EQUITY:
            raison = f"classe {cl}"
        elif _TICKER_INTERNE.match(tk):
            raison = "ligne interne (ticker numérique)"
        elif _EXCLU_NOM.search(nom):
            raison = "droit/bon/CVR"
        elif mv is None or mv <= 0 or (px is not None and px <= 0):
            raison = "valeur nulle (titre suspendu)"
        (ecarte if raison else garde).append((tk, nom, mv, raison))
    tot = sum(g[2] for g in garde)
    # résidus : titres suspendus gardés par le fonds, poids négligeable
    garde2 = [g for g in garde if 100 * g[2] / tot >= 0.001]
    ecarte += [(g[0], g[1], g[2], "poids < 0,001 %") for g in garde if g not in garde2]
    tot = sum(g[2] for g in garde2)
    isin = _isins(indice, params)
    lignes = [{"ticker": tk, "isin": isin.get(tk), "nom": nom, "poids": round(100 * mv / tot, 6)}
              for tk, nom, mv, _ in sorted(garde2, key=lambda g: -g[2])]
    url = requests.Request("GET", _BASE[indice], params=params).prepare().url
    return date_iso, lignes, url, ecarte


def tsx(date: str | None = None):
    """S&P/TSX Composite via XIC. Rend (date_iso, lignes, source_url)."""
    d, l, u, _ = _lire("tsx", date)
    return d, l, u


def ipc(date: str | None = None):
    """S&P/BMV IPC via NAFTRAC. Rend (date_iso, lignes, source_url)."""
    d, l, u, _ = _lire("ipc", date)
    return d, l, u


def lignes_ecartees(indice: str, date: str | None = None):
    """Pour contrôle : ce qui a été retiré et pourquoi."""
    return _lire(indice, date)[3]


def vers_yahoo(ticker: str, indice: str) -> str:
    """Symbole Yahoo Finance correspondant : 'BBD.B' -> 'BBD-B.TO' ; 'WALMEX*' -> 'WALMEX.MX' ;
    'LIVEPOLC.1' -> 'LIVEPOLC-1.MX' ; 'PE&OLES*' -> 'PE&OLES.MX' (penser à encoder & en %26)."""
    t = ticker.strip().rstrip("*")
    if indice == "tsx":
        return t.replace(".", "-") + ".TO"
    if indice == "ipc":
        return t.replace(".", "-") + ".MX"
    raise ValueError(indice)


if __name__ == "__main__":
    for nom, f in (("tsx", tsx), ("ipc", ipc)):
        d, l, u = f()
        s = sum(x["poids"] for x in l)
        print(nom, d, len(l), "titres, somme", round(s, 6), "| ISIN manquants :",
              sum(1 for x in l if not x["isin"]), "|", u)
        print("   top 3 :", [(x["ticker"], round(x["poids"], 3)) for x in l[:3]])
        print("   écartés :", lignes_ecartees(nom))


# Table lue par fetch_indices_fiches.py (code d'indice → lecteur sans argument).
LECTEURS = {"tsx": tsx, "ipc": ipc}
