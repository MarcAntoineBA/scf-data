"""Poids exacts du jour des indices d'Asie-Pacifique (Nikkei 225, KOSPI, TAIEX,
Hang Seng, CSI 300, Nifty 50, S&P/ASX 200).

Dépendances : bibliothèque standard + requests. xlrd n'est nécessaire QUE pour
csi300(source="officiel") (fichier .xls mensuel de China Securities Index) ; la
source par défaut du CSI 300 (ETF 2846) s'en passe.

Chaque fonction rend (date_iso, lignes, source_url) où lignes est une liste de
{"code_place": str, "isin": str|None, "nom": str, "poids": float}, poids en %,
somme ramenée à 100 sur les seules actions (liquidités et dérivés des ETF exclus).

Nature des sources (détail dans recherche/audit_asie.json) :
  nikkei225  officiel : facteur d'ajustement Nikkei du jour x cours de clôture
             (cours pris dans l'ETF iShares 1329 ; le calcul redonne les poids de
             l'ETF à 0,000 pt près). Repli : fichier officiel mensuel.
  kospi      recalcul de la méthode officielle (capitalisation totale des actions
             ordinaires) : liste KIND + capitalisations Naver. Variante "etf" :
             portefeuille de l'ETF KODEX 코스피 (717 titres, échantillonné).
  taiex      officiel : table TAIFEX.
  hsi        ETF Tracker Fund 2800 (réplication physique ; identique à la factsheet
             officielle au centième). Repli : iShares 3115.
  csi300     ETF iShares Core CSI 300 (2846, 300 codes = liste officielle).
             Variante "officiel" : 000300closeweight.xls (fin de mois, xlrd).
  nifty50    officiel : fichier quotidien NSE Indices (2 décimales) + ISIN NSE.
  asx200     ETF SPDR STW (réplication physique, ISIN). Repli : iShares IOZ.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from html import unescape

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 60

URL = {
    "nikkei_paf": "https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_225_price_adjustment_factor_en.csv",
    "nikkei_mensuel": "https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_weight_en.csv",
    "etf_1329": "https://www.blackrock.com/jp/individual/ja/products/251897/ishares-nikkei-225-etf/1480664184455.ajax?fileType=csv&fileName=1329_holdings&dataType=fund",
    "kind_kospi": "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType=stockMkt",
    "naver_kospi": "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page={page}&pageSize=100",
    "kodex_kospi": "https://www.samsungfund.com/api/v1/kodex/product-pdf/2ETF52.do?gijunYMD={date}",
    "taifex": "https://www.taifex.com.tw/cht/2/weightedPropertion",
    "etf_2800": "https://rbwm-api.hsbc.com.hk/pws-hk-hase-hsvm2-papi-prod-proxy/v1/hsvm/csv/trahkfund/holdings?mode=daily",
    "etf_3115": "https://www.blackrock.com/hk/en/products/284479/fund/1478358625333.ajax?fileType=csv&fileName=3115_holdings&dataType=fund",
    "etf_2846": "https://www.blackrock.com/hk/en/products/251754/ishares-csi-300-a-share-index-etf/1478358625333.ajax?fileType=csv&fileName=2846_holdings&dataType=fund",
    "csi_closeweight": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/closeweight/000300closeweight.xls",
    "nifty_jour": "https://liveindexsa.niftyindices.com/jsonfiles/Sector/SectorialIndexDataNIFTY%2050_Sector.js",
    "nifty_liste": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "etf_stw": "https://www.ssga.com/library-content/products/fund-data/etfs/apac/holdings-daily-au-en-stw.xlsx",
    "etf_ioz": "https://www.blackrock.com/au/products/251852/ishares-core-sp-asx-200-etf/1478358644060.ajax?fileType=csv&fileName=IOZ_holdings&dataType=fund",
}

_MOIS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_session = None


def _s() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    return _session


def _get(url: str, essais: int = 3, **kw) -> requests.Response:
    err = None
    for i in range(essais):
        try:
            r = _s().get(url, timeout=TIMEOUT, **kw)
            if r.status_code == 200 and r.content:
                return r
            err = RuntimeError(f"HTTP {r.status_code} ({len(r.content)} octets) sur {url}")
        except requests.RequestException as e:  # réseau, délai
            err = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"échec de téléchargement : {url} -> {err}")


def _num(x) -> float:
    return float(str(x).replace(",", "").replace("%", "").strip())


def _normalise(lignes: list[dict]) -> list[dict]:
    tot = sum(l["poids"] for l in lignes)
    if tot <= 0:
        raise RuntimeError("somme des poids nulle")
    for l in lignes:
        l["poids"] = round(l["poids"] / tot * 100, 6)
    lignes.sort(key=lambda l: -l["poids"])
    return lignes


def _verifie(nom: str, res, n_min: int, n_max: int):
    date, lignes, url = res
    s = sum(l["poids"] for l in lignes)
    if not (n_min <= len(lignes) <= n_max):
        raise RuntimeError(f"{nom} : {len(lignes)} lignes hors de [{n_min}, {n_max}] ({url})")
    if abs(s - 100) > 0.05:
        raise RuntimeError(f"{nom} : somme {s:.3f} ≠ 100")
    if len({l['code_place'] for l in lignes}) != len(lignes):
        raise RuntimeError(f"{nom} : codes en double")
    return res


# ---------------------------------------------------------------- BlackRock
def _blackrock(url: str, code=lambda t: t):
    """CSV d'avoirs iShares (US/HK/JP/AU) -> (date_iso, lignes actions)."""
    txt = _get(url).content.decode("utf-8-sig", errors="replace")
    lignes_txt = txt.splitlines()
    i = next(k for k, l in enumerate(lignes_txt) if l.startswith("Ticker,"))
    entete = lignes_txt[:i]
    date = None
    for l in entete:
        m = re.search(r'(\d{1,2})-([A-Za-z]{3})[a-z]*-(\d{4})', l)
        if m:
            date = _dt.date(int(m.group(3)), _MOIS[m.group(2).lower()], int(m.group(1)))
            break
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', l)
        if m:
            date = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            break
    out = {}
    for r in csv.DictReader(io.StringIO("\n".join(lignes_txt[i:]))):
        if r.get("Asset Class") not in ("Equity", "株式"):
            continue
        try:
            mv = _num(r["Market Value"])
        except (ValueError, KeyError):
            continue
        c = code(r["Ticker"].strip())
        if c in out:
            out[c]["poids"] += mv
        else:
            out[c] = {"code_place": c, "isin": None, "nom": r["Name"].strip(), "poids": mv,
                      "_prix": r.get("Price")}
    if not out:
        raise RuntimeError(f"aucune action dans {url}")
    return (date.isoformat() if date else None), list(out.values())


# ---------------------------------------------------------------- Nikkei 225
def nikkei225(source: str = "jour"):
    """source="jour" : poids = cours x facteur d'ajustement officiel (PAF), exact.
    source="mensuel" : fichier officiel de fin de mois."""
    if source == "jour":
        try:
            raw = _get(URL["nikkei_paf"]).content.decode("cp932", errors="replace")
            paf = {}
            date_paf = None
            for r in csv.DictReader(io.StringIO(raw)):
                c = (r.get("Code") or "").strip()
                if not re.fullmatch(r"[0-9A-Z]{4}", c):
                    continue
                paf[c] = (_num(r["Price Adjustment Factor"]), r["Company Name"].strip())
                date_paf = r["Date of Data"]
            date_px, etf = _blackrock(URL["etf_1329"])
            px = {l["code_place"]: _num(l["_prix"]) for l in etf if l.get("_prix")}
            manq = sorted(set(paf) - set(px))
            if manq:
                raise RuntimeError(f"cours manquants pour {manq}")
            lignes = [{"code_place": c, "isin": None, "nom": n, "poids": px[c] * f}
                      for c, (f, n) in paf.items()]
            return _verifie("nikkei225", (date_px, _normalise(lignes),
                            URL["nikkei_paf"] + " x cours " + URL["etf_1329"]), 223, 226)
        except Exception as e:  # repli sur le fichier officiel mensuel
            print(f"[nikkei225] calcul du jour impossible ({e}) : repli sur le fichier mensuel")
    raw = _get(URL["nikkei_mensuel"]).content.decode("cp932", errors="replace")
    lignes, date = [], None
    for r in csv.DictReader(io.StringIO(raw)):
        c = (r.get("Code") or "").strip()
        if not re.fullmatch(r"[0-9A-Z]{4}", c):
            continue
        date = r["Date of Data"].replace("/", "-")
        lignes.append({"code_place": c, "isin": None, "nom": r["Company Name"].strip(),
                       "poids": _num(r["Weight"])})
    return _verifie("nikkei225", (date, _normalise(lignes), URL["nikkei_mensuel"]), 223, 226)


# ---------------------------------------------------------------- KOSPI
def _kind_kospi() -> dict:
    html = _get(URL["kind_kospi"]).content.decode("euc-kr", errors="replace")
    codes = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        tds = [unescape(re.sub(r"<[^>]+>", "", t)).strip()
               for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(tds) >= 3 and re.fullmatch(r"[0-9A-Z]{6}", tds[2]):
            codes[tds[2]] = tds[0]
    if len(codes) < 700:
        raise RuntimeError(f"liste KIND incomplète ({len(codes)})")
    return codes


def kospi(source: str = "capi"):
    """source="capi" : capitalisation totale des actions ordinaires cotées au KOSPI
    (liste KIND, capitalisations Naver) = méthode de l'indice, tous les membres.
    source="etf" : portefeuille de l'ETF KODEX 코스피 (échantillonné, 717 titres)."""
    if source == "etf":
        demain = (_dt.datetime.utcnow() + _dt.timedelta(hours=9) + _dt.timedelta(days=1)).strftime("%Y.%m.%d")
        url = URL["kodex_kospi"].format(date=demain)
        d = _get(url).json()["pdf"]
        lignes = [{"code_place": x["itmNo"], "isin": None, "nom": x["secNm"], "poids": _num(x["evalA"])}
                  for x in d["list"] if re.fullmatch(r"[0-9A-Z]{6}", x.get("itmNo") or "") and x.get("ratio")]
        g = d["gijunYMD"]
        return _verifie("kospi", (f"{g[:4]}-{g[4:6]}-{g[6:]}", _normalise(lignes), url), 600, 1000)
    membres = _kind_kospi()
    lignes, date = [], None
    for page in range(1, 40):
        d = _get(URL["naver_kospi"].format(page=page)).json()
        st = d.get("stocks") or []
        for s in st:
            if s.get("stockEndType") != "stock" or s["itemCode"] not in membres:
                continue
            mv = s.get("marketValueRaw") or (_num(s["marketValue"]) * 1e8)
            lignes.append({"code_place": s["itemCode"], "isin": None, "nom": s["stockName"], "poids": float(mv)})
            date = date or (s.get("localTradedAt") or "")[:10]
        if len(st) < 100:
            break
        time.sleep(0.3)
    manq = set(membres) - {l["code_place"] for l in lignes}
    if len(manq) > 5:
        raise RuntimeError(f"capitalisation Naver manquante pour {len(manq)} membres KIND")
    return _verifie("kospi", (date, _normalise(lignes), URL["naver_kospi"].format(page="N") + " + " + URL["kind_kospi"]), 700, 1000)


# ---------------------------------------------------------------- TAIEX
def taiex():
    html = _get(URL["taifex"]).content.decode("utf-8", errors="replace")
    m = re.search(r"資料日期：\s*(\d{4})/(\d{1,2})/(\d{1,2})", html)
    date = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat() if m else None
    lignes = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        tds = [unescape(re.sub(r"<[^>]+>", "", t)).strip()
               for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        for j in range(0, len(tds) - 3, 4):  # 2 blocs de 4 colonnes : rang, code, nom, poids
            rang, code, nom, poids = tds[j:j + 4]
            if rang.replace(",", "").isdigit() and re.fullmatch(r"\w{4,6}", code) and poids.endswith("%"):
                lignes[code] = {"code_place": code, "isin": None, "nom": nom, "poids": _num(poids)}
    return _verifie("taiex", (date, _normalise(list(lignes.values())), URL["taifex"]), 900, 1300)


# ---------------------------------------------------------------- Hang Seng
def hsi(source: str = "2800"):
    if source == "2800":
        try:
            txt = _get(URL["etf_2800"]).content.decode("utf-8-sig", errors="replace")
            L = txt.splitlines()
            m = re.search(r"(\d{2})(\d{2})(\d{4})", L[2])
            date = _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            i = next(k for k, l in enumerate(L) if l.startswith("ISIN,"))
            lignes = []
            for r in csv.DictReader(io.StringIO("\n".join(L[i:]))):
                t = (r.get("Ticker") or "").strip()
                if not t.endswith("-HK"):
                    continue
                lignes.append({"code_place": t[:-3].zfill(4), "isin": r["ISIN"].strip(),
                               "nom": r["Security Name"].strip(), "poids": _num(r["Base Market Value"])})
            return _verifie("hsi", (date, _normalise(lignes), URL["etf_2800"]), 45, 120)
        except Exception as e:
            print(f"[hsi] 2800 indisponible ({e}) : repli sur iShares 3115")
    date, lignes = _blackrock(URL["etf_3115"], code=lambda t: t.zfill(4))
    for l in lignes:
        l.pop("_prix", None)
    return _verifie("hsi", (date, _normalise(lignes), URL["etf_3115"]), 45, 120)


# ---------------------------------------------------------------- CSI 300
def csi300(source: str = "etf"):
    """source="etf" : iShares Core CSI 300 (2846), quotidien, sans xlrd.
    source="officiel" : 000300closeweight.xls (fin de mois) ; nécessite xlrd."""
    if source == "officiel":
        import xlrd  # noqa: dépendance optionnelle
        wb = xlrd.open_workbook(file_contents=_get(URL["csi_closeweight"]).content)
        sh = wb.sheet_by_index(0)
        hdr = [str(c).strip() for c in sh.row_values(0)]
        ic = next(k for k, h in enumerate(hdr) if "Constituent Code" in h)
        inm = next(k for k, h in enumerate(hdr) if "Name(Eng)" in h and "Constituent" in h)
        iw = next(k for k, h in enumerate(hdr) if "weight" in h.lower())
        lignes, date = [], None
        for r in range(1, sh.nrows):
            v = sh.row_values(r)
            code = str(v[ic]).split(".")[0].zfill(6)
            lignes.append({"code_place": code, "isin": None, "nom": str(v[inm]).strip(), "poids": float(v[iw])})
            date = str(v[0]).split(".")[0]
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        return _verifie("csi300", (date, _normalise(lignes), URL["csi_closeweight"]), 295, 305)
    date, lignes = _blackrock(URL["etf_2846"], code=lambda t: t.zfill(6))
    for l in lignes:
        l.pop("_prix", None)
    return _verifie("csi300", (date, _normalise(lignes), URL["etf_2846"]), 295, 305)


# ---------------------------------------------------------------- Nifty 50
def nifty50():
    js = _get(URL["nifty_jour"]).text
    items = re.findall(r'"label":"([A-Z0-9&\-]+) [0-9.]+%","weight":([0-9.]+),"id":"\d+","date":"(\d{2})-(\d{2})-(\d{4})"', js)
    if not items:
        raise RuntimeError("format du fichier NSE inattendu")
    liste = {}
    try:
        for r in csv.DictReader(io.StringIO(_get(URL["nifty_liste"]).content.decode("utf-8-sig", errors="replace"))):
            liste[r["Symbol"].strip()] = (r["ISIN Code"].strip(), r["Company Name"].strip())
    except Exception as e:
        print(f"[nifty50] liste ISIN indisponible ({e})")
    lignes = [{"code_place": s, "isin": liste.get(s, (None, None))[0], "nom": liste.get(s, (None, s))[1] or s,
               "poids": float(w)} for s, w, *_ in items]
    d, mth, y = items[0][2:]
    return _verifie("nifty50", (f"{y}-{mth}-{d}", _normalise(lignes), URL["nifty_jour"]), 49, 52)


# ---------------------------------------------------------------- S&P/ASX 200
def _xlsx_lignes(contenu: bytes) -> list[list[str]]:
    """Lecteur XLSX minimal (1re feuille), bibliothèque standard seulement."""
    z = zipfile.ZipFile(io.BytesIO(contenu))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    partagees = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            partagees.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    feuille = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    out = []
    for row in ET.fromstring(z.read(feuille)).iter("{%s}row" % ns["m"]):
        cells = {}
        for c in row.findall("m:c", ns):
            ref = re.match(r"([A-Z]+)", c.get("r")).group(1)
            col = 0
            for ch in ref:
                col = col * 26 + ord(ch) - 64
            v = c.find("m:v", ns)
            if c.get("t") == "s" and v is not None:
                val = partagees[int(v.text)]
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter("{%s}t" % ns["m"]))
            else:
                val = v.text if v is not None else ""
            cells[col - 1] = val
        if cells:
            out.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return out


def asx200(source: str = "stw"):
    if source == "stw":
        try:
            rows = _xlsx_lignes(_get(URL["etf_stw"]).content)
            date = None
            for r in rows[:6]:
                m = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", " ".join(r))
                if m:
                    date = _dt.date(int(m.group(3)), _MOIS[m.group(2).lower()], int(m.group(1))).isoformat()
            h = next(k for k, r in enumerate(rows) if "ISIN" in r and "Ticker" in r)
            hdr = rows[h]
            iI, iT, iN, iW = (hdr.index(x) for x in ("ISIN", "Ticker", "Name", "Weight (%)"))
            lignes = []
            for r in rows[h + 1:]:
                if len(r) <= iW or not str(r[iT]).endswith("-AU"):
                    continue
                lignes.append({"code_place": r[iT][:-3], "isin": r[iI] or None, "nom": r[iN], "poids": _num(r[iW])})
            return _verifie("asx200", (date, _normalise(lignes), URL["etf_stw"]), 190, 215)
        except Exception as e:
            print(f"[asx200] STW indisponible ({e}) : repli sur iShares IOZ")
    date, lignes = _blackrock(URL["etf_ioz"])
    for l in lignes:
        l.pop("_prix", None)
    return _verifie("asx200", (date, _normalise(lignes), URL["etf_ioz"]), 190, 215)


FONCTIONS = {"nikkei225": nikkei225, "kospi": kospi, "taiex": taiex, "hsi": hsi,
             "csi300": csi300, "nifty50": nifty50, "asx200": asx200}


def tous() -> dict:
    res = {}
    for k, f in FONCTIONS.items():
        try:
            res[k] = f()
        except Exception as e:
            res[k] = e
    return res


if __name__ == "__main__":
    import sys
    choix = sys.argv[1:] or list(FONCTIONS)
    ok = True
    for k in choix:
        t0 = time.time()
        try:
            date, lignes, url = FONCTIONS[k]()
            top = ", ".join(f"{l['code_place']} {l['poids']:.2f}" for l in lignes[:3])
            n_isin = sum(1 for l in lignes if l["isin"])
            print(f"OK  {k:9s} {date}  {len(lignes):5d} lignes  somme={sum(l['poids'] for l in lignes):.4f}  "
                  f"isin={n_isin}  top3: {top}  ({time.time() - t0:.1f}s)")
        except Exception as e:
            ok = False
            print(f"ÉCHEC {k}: {e}")
    sys.exit(0 if ok else 1)


# Table lue par fetch_indices_fiches.py (code d'indice → lecteur sans argument).
LECTEURS = {"nikkei225": nikkei225, "kospi": kospi, "taiex": taiex, "hsi": hsi,
            "csi300": csi300, "nifty50": nifty50, "asx200": asx200}
