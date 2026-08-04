#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch données « Finance de marché américaine — financer l'économie ou rémunérer le capital ? »

Onglet AUTONOME (pas un chapitre These_*), look Thèse. Démonstration chiffrée,
datée et AUDITABLE : la bourse américaine finance-t-elle encore l'économie réelle
et l'innovation, ou sert-elle surtout à rémunérer les détenteurs de capital
(rachats d'actions, dividendes) ?

PRINCIPE DE RIGUEUR : tous les graphiques proviennent de séries LIVE et auditables
— Financial Accounts de la Fed (Z.1) via FRED, BEA via FRED, World Bank via FRED,
et SEC EDGAR (XBRL) pour les rachats par société. Aucune série « seed » saisie à la
main : les chiffres externes (rachats bruts S&P 500, etc.) ne figurent qu'en texte cité.

Colonne vertébrale (toutes validées sur l'API FRED, 2026-06-30) :
  NCBCEBQ027S  émission nette d'actions, corporates US (Z.1)  — M$ SAAR, depuis 1946
               (positif = la bourse finance ; négatif = rachats nets). SÉRIE PIVOT.
  GDP, NFCPATAX (profits nets des corporates), BOGZ1FA106121075Q (dividendes nets),
  NCBGCFQ027S (investissement productif), Y006/Y001/B985/B009 + FPI (compo. de
  l'investissement, tangible→intangible), BCNSDODNS (dette corporate), BAA
  (rendement Baa, depuis 1919), DDOM01USA644NWDB (nb de sociétés cotées US, WB).
  SEC EDGAR : us-gaap:PaymentsForRepurchaseOfCommonStock par CIK (méga-caps).

Cache : ~/Library/Caches/site_crypto_finance/finance_americaine_cache.{json,js}
        window.__FINANCE_AMERICAINE__ = {...}
"""
import os
import json
import sys
import time
import gzip
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred  # noqa: E402

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "finance_americaine_cache.json"
CACHE_JS = CACHE_DIR / "finance_americaine_cache.js"
RAW = CACHE_DIR / "finance_americaine_raw"
RAW.mkdir(exist_ok=True)

SEC_UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")

# ── Palette (rendu sur charbon) ───────────────────────────────────────────
BLUE, PURPLE, GREEN, RED = "#5eaff6", "#a78bfa", "#26a69a", "#ef5350"
GOLD, GOLDL, BRONZE, TEAL = "#fbbf24", "#F5C54B", "#b08d57", "#4dd0c4"


def fred_url(sid):
    return f"https://fred.stlouisfed.org/series/{sid}"


def serie(label, dates, values, color, fmt, dash=False, axis=None):
    s = {"label": label, "color": color, "fmt": fmt, "dash": dash,
         "dates": dates, "values": [round(v, 3) for v in values]}
    if axis:
        s["axis"] = axis
    return s


def panel(key, title, verdict, fmt, series, sources, note=None, kind="lines"):
    return {"key": key, "title": title, "verdict": verdict, "fmt": fmt,
            "kind": kind, "series": series, "sources": sources, "note": note}


def src(sid, computed=None, provider="FRED", start=None, title=None, url=None):
    d = {"id": sid, "provider": provider, "url": url or fred_url(sid)}
    if start:
        d["start"] = start
    if title:
        d["title"] = title
    if computed:
        d["computed"] = computed
    return d


def last(series_obj):
    return series_obj["values"][-1] if series_obj and series_obj["values"] else None


def val_at(dates, values, year):
    y = str(year)
    cand = [v for d, v in zip(dates, values) if d[:4] <= y]
    return cand[-1] if cand else (values[0] if values else None)


def as_pct_gdp(s, gmap, scale=1.0):
    out_d, out_v = [], []
    for d, v in zip(s["dates"], s["values"]):
        g = gmap.get(d)
        if g:
            out_d.append(d)
            out_v.append(round(v * scale / g * 100, 3))
    return out_d, out_v


def to_bn(s, scale):
    """Série brute → milliards $ (scale=0.001 si M$, 1.0 si déjà Md$)."""
    return {"dates": list(s["dates"]), "values": [v * scale for v in s["values"]]}


def combine(a, b, fn):
    """Aligne deux séries {dates,values} par date et applique fn(va, vb)."""
    bm = dict(zip(b["dates"], b["values"]))
    out_d, out_v = [], []
    for d, va in zip(a["dates"], a["values"]):
        vb = bm.get(d)
        if vb is not None:
            out_d.append(d)
            out_v.append(fn(va, vb))
    return {"dates": out_d, "values": out_v}


def ratio_pct(num, den):
    return combine(num, den, lambda n, d: round(n / d * 100, 3) if d else 0.0)


def smooth_series(s, w=4):
    """Moyenne mobile sur w periodes (les flux Z.1 trimestriels SAAR sont
    bruites : un lissage 4 trim. revele la tendance structurelle)."""
    vals = s["values"]
    out = []
    for i in range(len(vals)):
        win = vals[max(0, i - w + 1):i + 1]
        out.append(round(sum(win) / len(win), 3))
    return {"dates": list(s["dates"]), "values": out}


# ════════════════════════════════════════════════════════════════════════
#  SEC EDGAR — rachats d'actions réels par méga-cap (§10 concentration)
# ════════════════════════════════════════════════════════════════════════
MEGACAPS = [
    ("AAPL", "0000320193", "Apple"),
    ("MSFT", "0000789019", "Microsoft"),
    ("GOOGL", "0001652044", "Alphabet"),
    ("META", "0001326801", "Meta"),
    ("NVDA", "0001045810", "NVIDIA"),
    ("AMZN", "0001018724", "Amazon"),
    ("JPM", "0000019617", "JPMorgan Chase"),
    ("BAC", "0000070858", "Bank of America"),
    ("WFC", "0000072971", "Wells Fargo"),
    ("XOM", "0000034088", "ExxonMobil"),
    ("CVX", "0000093410", "Chevron"),
    ("ORCL", "0001341439", "Oracle"),
    ("CSCO", "0000858877", "Cisco"),
    ("AVGO", "0001730168", "Broadcom"),
    ("HD", "0000354950", "Home Depot"),
    ("V", "0001403161", "Visa"),
    ("MA", "0001141391", "Mastercard"),
    ("PG", "0000080424", "Procter & Gamble"),
    ("JNJ", "0000200406", "Johnson & Johnson"),
    ("COST", "0000909832", "Costco"),
    ("WMT", "0000104169", "Walmart"),
]
SEC_CONCEPTS = [
    "PaymentsForRepurchaseOfCommonStock",
    "PaymentsForRepurchaseOfCommonStockAndEmployeeTaxesPaid",
    "PaymentsForRepurchaseOfEquity",
]
# Panier LARGE d'éditeurs de logiciels / SaaS US (marge nette agrégée du secteur).
# Microsoft, Amazon, Apple, Alphabet EXCLUS volontairement : conglomérats dont la
# marge ne reflète PAS le pur logiciel. CIK résolus via la table officielle SEC
# (ticker->CIK) pour éviter toute erreur de saisie ; les non-déclarants 10-K/us-gaap
# (SAP, Shopify… en 20-F/IFRS) tombent d'eux-mêmes.
SOFTWARE_TICKERS = [
    # Éditeurs matures / rentables
    "ORCL", "ADBE", "CRM", "INTU", "ADSK", "NOW", "CDNS", "SNPS", "ANSS",
    "PTC", "TYL", "MANH", "PAYC", "FICO", "SSNC", "ROP", "VRSN", "AKAM",
    # SaaS / croissance
    "WDAY", "PANW", "FTNT", "CRWD", "ZS", "DDOG", "SNOW", "HUBS", "OKTA",
    "MDB", "TWLO", "DOCU", "BILL", "ZM", "TEAM", "NET", "PLTR", "U", "DBX",
    "GTLB", "PATH", "S", "ESTC", "FROG", "APPF", "PCTY", "BSY", "DT",
]
# Concepts de CA (l'ASC 606 a fait migrer le tag vers ~2018) — essayés dans l'ordre.
REV_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]


def _sec_get(url, retry=3):
    for i in range(retry):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": SEC_UA,
                              "Accept-Encoding": "gzip, deflate",
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE401
            # 404 = concept/série inexistant (permanent) : inutile de réessayer.
            if getattr(e, "code", None) == 404:
                return None
            if i == retry - 1:
                sys.stderr.write(f"[sec] fail {url[:80]} {e}\n")
            time.sleep(1.0)
    return None


def _annual_days(a, b):
    ya, ma, da = map(int, a.split("-"))
    yb, mb, db = map(int, b.split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days


def _concept_annual(cik, concept):
    """Retourne {annee:int -> (val, end, filed)} pour les périodes ANNUELLES
    (durée ~1 an) rapportées dans des 10-K. Clé = année de FIN de période — un
    10-K liste 3 exercices, donc on dédoublonne par fin de période (pas par
    l'année du dépôt, qui mélangerait les comparatifs)."""
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}"
           f"/us-gaap/{concept}.json")
    raw_path = RAW / f"CIK{cik}_{concept}.json"
    body = _sec_get(url)
    if body and '"units"' in body:
        raw_path.write_text(body)
    elif raw_path.exists():
        body = raw_path.read_text()  # fallback dernière copie connue
    if not body:
        return {}
    try:
        d = json.loads(body)
    except Exception:
        return {}
    out = {}
    for unit, facts in (d.get("units") or {}).items():
        if not unit.startswith("USD"):
            continue
        for f in facts:
            if f.get("form") not in ("10-K", "10-K/A"):
                continue
            st, en, val = f.get("start"), f.get("end"), f.get("val")
            if not st or not en or val is None:
                continue
            try:
                if not (350 <= _annual_days(st, en) <= 380):
                    continue
            except Exception:
                continue
            yr = int(en[:4])
            prev = out.get(yr)
            # garde le dépôt le plus récent (gère les re-dépôts amendés)
            if prev is None or (f.get("filed") or "") > prev[2]:
                out[yr] = (float(val), en, f.get("filed") or "")
    return out


def fetch_top_buyers():
    rows = []
    for ticker, cik, name in MEGACAPS:
        annual = {}
        for c in SEC_CONCEPTS:
            annual = _concept_annual(cik, c)
            if annual:
                break
            time.sleep(0.2)
        if not annual:
            sys.stderr.write(f"[sec] {ticker}: aucun rachat tagué\n")
            continue
        fy = max(annual)
        val, fy_end, filed = annual[fy]
        # historique 5 ans pour micro-tendance éventuelle
        hist = sorted([(y, annual[y][0]) for y in annual])[-12:]
        rows.append({"ticker": ticker, "cik": cik, "name": name,
                     "buybacks_usd": round(val, 0), "fy": fy, "fy_end": fy_end,
                     "hist": [[str(y), round(v / 1e9, 2)] for y, v in hist]})
        time.sleep(0.25)
    if not rows:
        return None
    rows.sort(key=lambda r: -r["buybacks_usd"])
    total = sum(r["buybacks_usd"] for r in rows)
    top10 = rows[:10]
    top10_sum = sum(r["buybacks_usd"] for r in top10)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "n": len(rows),
        "total_bn": round(total / 1e9, 1),
        "top10_share": round(top10_sum / total, 4) if total else None,
        "rows": [{"ticker": r["ticker"], "name": r["name"], "cik": r["cik"],
                  "value_bn": round(r["buybacks_usd"] / 1e9, 2),
                  "fy": r["fy"], "fy_end": r["fy_end"], "hist": r["hist"]}
                 for r in rows],
    }


# ════════════════════════════════════════════════════════════════════════
#  ZOOM Mag 7 / IA-tech — CAPEX vs R&D vs rachats (SEC EDGAR, live)
# ════════════════════════════════════════════════════════════════════════
MAG7 = [
    ("AAPL", "0000320193", "Apple"),
    ("MSFT", "0000789019", "Microsoft"),
    ("GOOGL", "0001652044", "Alphabet"),
    ("AMZN", "0001018724", "Amazon"),
    ("NVDA", "0001045810", "NVIDIA"),
    ("META", "0001326801", "Meta"),
    ("TSLA", "0001318605", "Tesla"),
]
CAPEX_CONCEPTS = ["PaymentsToAcquirePropertyPlantAndEquipment",
                  "PaymentsToAcquireProductiveAssets"]


def _merge_annual(cik, concepts):
    """Fusionne plusieurs concepts XBRL par année (les sociétés changent de balise
    au fil du temps — ex. Amazon/NVIDIA passent de PropertyPlantAndEquipment à
    ProductiveAssets). Pour une année donnée, on garde le dépôt le plus récent."""
    merged = {}
    for c in concepts:
        a = _concept_annual(cik, c)
        for y, (v, e, fl) in a.items():
            prev = merged.get(y)
            if prev is None or (fl or "") > prev[2]:
                merged[y] = (v, e, fl)
        time.sleep(0.12)
    return merged


def fetch_mag7():
    """CAPEX, R&D et rachats agrégés des 7 géants tech, par exercice (SEC 10-K)."""
    capex_y, rd_y, bb_y, capex_co, rd_co = {}, {}, {}, {}, {}
    n_ok = 0
    for ticker, cik, name in MAG7:
        cap = _merge_annual(cik, CAPEX_CONCEPTS)
        rd = _concept_annual(cik, "ResearchAndDevelopmentExpense")
        bb = _merge_annual(cik, SEC_CONCEPTS)
        if not (cap or rd or bb):
            sys.stderr.write(f"[mag7] {ticker}: rien\n")
            continue
        n_ok += 1
        for y, (v, _e, _f) in cap.items():
            capex_y[y] = capex_y.get(y, 0) + v
            capex_co.setdefault(y, set()).add(ticker)
        for y, (v, _e, _f) in rd.items():
            rd_y[y] = rd_y.get(y, 0) + v
            rd_co.setdefault(y, set()).add(ticker)
        for y, (v, _e, _f) in bb.items():
            bb_y[y] = bb_y.get(y, 0) + v
        time.sleep(0.2)
    if n_ok < 5:
        return None
    # années où ≥6 des 7 ont publié leur CAPEX (agrégat fiable)
    years = sorted(y for y in capex_y if 2015 <= y <= 2026 and len(capex_co.get(y, ())) >= 6)
    if len(years) < 4:
        return None
    rd_n = max((len(rd_co.get(y, ())) for y in years), default=0)
    return {
        "n": n_ok,
        "rd_n": rd_n,
        "years": years,
        "dates": [f"{y}-12-31" for y in years],
        "capex": [round(capex_y[y] / 1e9, 1) for y in years],
        "rd": [round(rd_y.get(y, 0) / 1e9, 1) for y in years],
        "buybacks": [round(bb_y.get(y, 0) / 1e9, 1) for y in years],
    }


def _ticker_cik_map():
    """Table officielle SEC ticker->CIK (10 chiffres), avec fallback fichier."""
    url = "https://www.sec.gov/files/company_tickers.json"
    raw = RAW / "company_tickers.json"
    body = _sec_get(url)
    if body and '"cik_str"' in body:
        raw.write_text(body)
    elif raw.exists():
        body = raw.read_text()
    if not body:
        return {}
    try:
        d = json.loads(body)
    except Exception:
        return {}
    out = {}
    for row in d.values():
        t = str(row.get("ticker", "")).upper()
        if t:
            out[t] = str(row.get("cik_str", "")).zfill(10)
    return out


def fetch_software_margins():
    """Marge nette AGRÉGÉE (pondérée par le chiffre d'affaires) d'un LARGE panier
    d'éditeurs de logiciels / SaaS US, reconstruite depuis les 10-K (SEC XBRL).
    marge(année) = Σ résultat net / Σ CA sur les sociétés présentes cette année.
    Pondération CA = marge économique du secteur (pas la marge de la société médiane).
    Historique XBRL ~2009+. Exercices fiscaux variables (bucket = année de fin)."""
    tmap = _ticker_cik_map()
    if not tmap:
        sys.stderr.write("[soft-margin] table ticker->CIK indisponible\n")
        return None
    rev_sum, ni_sum, n_co, used = {}, {}, {}, []
    for ticker in SOFTWARE_TICKERS:
        cik = tmap.get(ticker)
        if not cik:
            continue
        rev = {}
        for c in REV_CONCEPTS:
            for y, (v, en, fl) in _concept_annual(cik, c).items():
                if y not in rev and v is not None:
                    rev[y] = float(v)
            time.sleep(0.12)
        ni = _concept_annual(cik, "NetIncomeLoss")
        time.sleep(0.15)
        matched = set(rev) & set(ni)
        if matched:
            used.append(ticker)
        for y in matched:
            r = rev[y]
            if r and r > 0:
                rev_sum[y] = rev_sum.get(y, 0.0) + r
                ni_sum[y] = ni_sum.get(y, 0.0) + ni[y][0]
                n_co[y] = n_co.get(y, 0) + 1
    years = sorted(y for y in rev_sum if n_co.get(y, 0) >= 6)
    # Retire la ou les dernières années sous-déclarées (tous les 10-K pas encore
    # déposés → composition biaisée, marge non comparable).
    while len(years) >= 2 and n_co[years[-1]] < 0.6 * n_co[years[-2]]:
        years.pop()
    if len(years) < 5:
        sys.stderr.write(f"[soft-margin] trop peu d'années ({len(years)})\n")
        return None
    return {
        "years": years,
        "dates": [f"{y}-12-31" for y in years],
        "margin": [round(ni_sum[y] / rev_sum[y] * 100, 1) for y in years],
        "n": [n_co[y] for y in years],
        "n_basket": len(used),
        "companies": used,
    }


# ════════════════════════════════════════════════════════════════════════
def main():
    financement, usages, marche = [], [], []
    sources_meta = []

    def add_meta(name, provider, url, note=""):
        sources_meta.append({"name": name, "provider": provider,
                             "url": url, "note": note})

    gdp = fetch_fred("GDP", start="1946-01-01")
    if not gdp:
        sys.stderr.write("[fa] GDP indisponible — abandon\n")
        sys.exit(1)
    gmap = dict(zip(gdp["dates"], gdp["values"]))

    # ── SÉRIE PIVOT : émission nette d'actions (Z.1) ──────────────────────
    nes = fetch_fred("NCBCEBQ027S", start="1946-01-01")
    if not nes:
        sys.stderr.write("[fa] NCBCEBQ027S (pivot) indisponible — cache NON écrit\n")
        sys.exit(1)
    add_meta("Émission nette d'actions (corporates non-fin.)", "FRED Z.1",
             fred_url("NCBCEBQ027S"),
             "M$ SAAR ; positif = capital levé, négatif = rachats nets ; depuis 1946.")

    nes_bn = to_bn(nes, 0.001)                       # $bn (taux annualisé)
    nes_pct_d, nes_pct_v = as_pct_gdp(nes, gmap, 0.001)
    # Rachats nets (net equity repurchases) = − émission nette. Positif = rachats nets,
    # négatif = émission nette (la bourse finance). Mesure académique standard du payout.
    netrep_bn = {"dates": nes_bn["dates"], "values": [-v for v in nes_bn["values"]]}
    netrep_recent = (round(sum(netrep_bn["values"][-4:]) / 4, 0)
                     if len(netrep_bn["values"]) >= 4 else netrep_bn["values"][-1])
    nes_bn_sm = smooth_series(nes_bn)
    netrep_sm = smooth_series(netrep_bn)
    nes_pct_sm = smooth_series({"dates": nes_pct_d, "values": nes_pct_v})

    # ── KPI / verdict pivot ───────────────────────────────────────────────
    nes_last_pct = nes_pct_v[-1] if nes_pct_v else None
    # moyenne glissante 10 ans (≈40 trimestres) du NES %PIB
    nes_avg10 = (round(sum(nes_pct_v[-40:]) / len(nes_pct_v[-40:]), 2)
                 if len(nes_pct_v) >= 8 else None)
    # Basculement DURABLE : année à partir de laquelle la moyenne glissante 10 ans du
    # NES/PIB devient négative et le reste (pas un creux ponctuel des années 1960).
    def _durable_neg_year(dates, values, win=40):
        n = len(values)
        ta = [sum(values[max(0, i - win + 1):i + 1]) / len(values[max(0, i - win + 1):i + 1])
              for i in range(n)]
        last_cross = None
        for i in range(1, n):
            if ta[i] < 0 and ta[i - 1] >= 0:
                last_cross = i
        if last_cross is not None and all(v < 0 for v in ta[last_cross:]):
            return dates[last_cross][:4]
        for i in range(n):
            if ta[i] < 0 and all(v < 0 for v in ta[i:]):
                return dates[i][:4]
        return dates[-1][:4]
    first_neg_year = _durable_neg_year(nes_pct_d, nes_pct_v)

    # ════════ §3 — Net Equity Supply en niveau ($bn) ════════
    financement.append(panel(
        "ipo", "Émission nette d'actions des entreprises américaines (Md$, taux annualisé)",
        (f"Dernier point&nbsp;: <b>{nes_bn_sm['values'][-1]:,.0f} Md$</b>. "
         f"Au-dessus de zéro, les entreprises lèvent du capital sur le marché&nbsp;; "
         f"en dessous, elles en retirent (rachats nets &gt; émissions). "
         f"Le basculement durable sous zéro date de ~{first_neg_year}.")
        .replace(",", " "),
        "bn",
        [serie("Émission nette d'actions", nes_bn_sm["dates"], nes_bn_sm["values"], GOLD, "bn")],
        [src("NCBCEBQ027S", computed="÷ 1000 → Md$ (taux annualisé)",
             start="1946", title="Nonfinancial Corporate Business; Corporate Equities; Liability, Transactions")],
        note="SAAR (taux annualisé) ; annuel avant 1952, trimestriel ensuite."))

    # ════════ §6 — Net Equity Supply en % du PIB (graphe SIGNATURE) ════════
    financement.append(panel(
        "nes", "Émission nette d'actions, en % du PIB — la bourse finance-t-elle, ou retire-t-elle&nbsp;?",
        (f"Moyenne des 10 dernières années&nbsp;: <b>{nes_avg10:+.2f}% du PIB</b>. "
         f"Depuis ~{first_neg_year}, le marché actions est, net, un canal de "
         f"<b>sortie</b> de capital (zone rouge), pas d'entrée. La fonction "
         f"« la bourse finance l'économie » s'est inversée.")
        if nes_avg10 is not None else "",
        "pct1",
        [serie("Émission nette d'actions / PIB", nes_pct_sm["dates"], nes_pct_sm["values"], GOLDL, "pct1")],
        [src("NCBCEBQ027S", computed="× 0,001 ÷ GDP × 100", start="1946"),
         src("GDP", start="1947")],
        note="Vert = le marché finance les entreprises ; rouge = il en retire du capital.",
        kind="nes"))


    # §2 (declin du nombre de societes cotees) est traite en TEXTE cite dans le
    # corps (World Bank / Doidge-Karolyi-Stulz 2017) : aucune serie 'nombre total
    # de cotees' live et a jour n'existe sur FRED.

    # ════════ §4 — La grande bascule : redistribuer vs investir (% du PIB) ════════
    div_raw = fetch_fred("BOGZ1FA106121075Q", start="1946-01-01")  # M$ SAAR, nonfin corp
    div = to_bn(div_raw, 0.001) if div_raw else None
    inv = fetch_fred("NCBGCFQ027S", start="1946-01-01")       # M$
    if div and inv:
        add_meta("Dividendes nets (corporates non-fin.)", "FRED Z.1",
                 fred_url("BOGZ1FA106121075Q"), "M$ SAAR -> Md$ ; meme perimetre que les profits/NES.")
        add_meta("Investissement productif (corporates non-fin.)", "FRED Z.1",
                 fred_url("NCBGCFQ027S"), "M$ → Md$.")
        # payout = dividendes + rachats nets ($bn)
        payout_bn = combine(div, netrep_sm, lambda a, b: a + b)
        pay_d, pay_v = as_pct_gdp(payout_bn, gmap, 1.0)
        inv_d, inv_v = as_pct_gdp(inv, gmap, 0.001)
        p1 = pay_v[-1] if pay_v else None
        i1 = inv_v[-1] if inv_v else None
        p0 = val_at(pay_d, pay_v, 1980)
        financement.append(panel(
            "bascule",
            "Ce qui sort vers les actionnaires vs ce qui est investi (% du PIB)",
            (f"Les versements aux actionnaires (dividendes + rachats nets) sont "
             f"passés de ~{p0:.1f}% à <b>{p1:.1f}% du PIB</b> depuis 1980, tandis "
             f"que l'investissement productif des entreprises reste autour de "
             f"{i1:.0f}%. L'investissement n'a pas disparu — mais la part rendue au "
             f"capital a, elle, fortement progressé, et sur le marché actions le flux "
             f"net s'est inversé (voir le chapitre suivant)."),
            "pct1",
            [serie("Dividendes + rachats nets", pay_d, pay_v, RED, "pct1"),
             serie("Investissement productif", inv_d, inv_v, GREEN, "pct1")],
            [src("BOGZ1FA106121075Q", computed="+ rachats nets (−NCBCEBQ027S), ÷ PIB", title="Dividendes nets, corporates non-fin."),
             src("NCBCEBQ027S"), src("NCBGCFQ027S", computed="× 0,001 ÷ PIB")]))

    # ════════ §5 — Ampleur des rachats : rachats nets vs dividendes ($bn) ════════
    if div:
        financement.append(panel(
            "buybacks",
            "Rachats d'actions nets vs dividendes versés (Md$, taux annualisé)",
            (f"Les rachats nets (mesure flux-de-fonds, tout le secteur) atteignent "
             f"<b>{netrep_recent:,.0f} Md$</b> en rythme annuel (moyenne 4 trim.), à comparer "
             f"aux {div['values'][-1]:,.0f} Md$ de dividendes. Au niveau du seul "
             f"S&amp;P 500, les rachats <i>bruts</i> dépassent 900&nbsp;Md$/an "
             f"(source S&amp;P Dow Jones Indices).").replace(",", " "),
            "bn",
            [serie("Rachats d'actions nets", netrep_sm["dates"], netrep_sm["values"], GOLD, "bn"),
             serie("Dividendes nets", div["dates"], div["values"], PURPLE, "bn")],
            [src("NCBCEBQ027S", computed="rachats nets = max(0, −NES) ÷ 1000"),
             src("BOGZ1FA106121075Q", title="Dividendes nets, corporates non-fin.")],
            note="Mesure NETTE (émissions retranchées) ; les rachats bruts S&P 500 sont plus élevés."))

    # ════════ §7 — Usage des profits : CAPEX / R&D / dividendes / rachats ════════
    profits = fetch_fred("NFCPATAX", start="1946-01-01")      # Md$
    rnd = fetch_fred("Y006RC1Q027SBEA", start="1946-01-01")   # Md$
    if profits:
        add_meta("Profits après impôt (corporates non-fin.)", "FRED / BEA",
                 fred_url("NFCPATAX"), "Md$.")
        prof = {"dates": profits["dates"], "values": profits["values"]}
        series7 = []
        # buybacks / profits
        bb_pp = ratio_pct(netrep_sm, prof)
        series7.append(serie("Rachats nets / profits", bb_pp["dates"], bb_pp["values"], GOLD, "pct0"))
        # dividends / profits
        if div:
            dv_pp = ratio_pct(div, prof)
            series7.append(serie("Dividendes / profits", dv_pp["dates"], dv_pp["values"], PURPLE, "pct0"))
        # capex / profits
        if inv:
            cx_pp = ratio_pct(to_bn(inv, 0.001), prof)
            series7.append(serie("Investissement / profits", cx_pp["dates"], cx_pp["values"], GREEN, "pct0"))
        # R&D / profits
        s7_src = [src("NCBCEBQ027S"), src("BOGZ1FA106121075Q", title="Dividendes nets, corporates non-fin."), src("NCBGCFQ027S"), src("NFCPATAX")]
        if rnd:
            add_meta("Investissement en R&D (BEA)", "FRED / BEA",
                     fred_url("Y006RC1Q027SBEA"), "Md$, périmètre économie entière.")
            rd_pp = ratio_pct(rnd, prof)
            series7.append(serie("R&D / profits*", rd_pp["dates"], rd_pp["values"], BLUE, "pct0"))
            s7_src.append(src("Y006RC1Q027SBEA", title="R&D investment (BEA)"))
        usages.append(panel(
            "usages",
            "À quoi servent les profits&nbsp;? (% des profits après impôt)",
            ("Le compte y est : les entreprises rendent une part majeure de leurs "
             "profits aux actionnaires (dividendes ≈ 60&nbsp;%, rachats nets ≈ 5-10&nbsp;% "
             "des profits selon le trimestre). L'investissement productif <i>brut</i> "
             "reste élevé (supérieur aux profits, car financé aussi par "
             "l'amortissement et la dette) : il ne s'effondre pas — mais il ne "
             "progresse plus, là où la rémunération du capital, elle, s'envole. "
             "*R&D mesurée sur l'économie entière (périmètre BEA), un cran plus "
             "large que les seules corporates non-financières."),
            "pct0", series7, s7_src,
            note="Ratios rapportés aux profits nets des corporates non-financières."))

    # ════════ §8 — Qualité de l'investissement : tangible → intangible ════════
    software = None
    fpi = fetch_fred("FPI", start="1946-01-01")
    ipp = fetch_fred("Y001RC1Q027SBEA", start="1946-01-01")
    soft = fetch_fred("B985RC1Q027SBEA", start="1959-01-01")
    struc = fetch_fred("B009RC1Q027SBEA", start="1946-01-01")
    if fpi and ipp:
        add_meta("Investissement fixe privé total", "FRED / BEA", fred_url("FPI"), "Md$.")
        add_meta("Propriété intellectuelle (IPP : logiciels + R&D)", "FRED / BEA",
                 fred_url("Y001RC1Q027SBEA"), "Md$.")
        series8 = []
        ipp_sh = ratio_pct(ipp, fpi)
        series8.append(serie("Immatériel (logiciels + R&D)", ipp_sh["dates"], ipp_sh["values"], GOLD, "pct0"))
        s8_src = [src("Y001RC1Q027SBEA", computed="÷ FPI × 100"), src("FPI")]
        if struc:
            st_sh = ratio_pct(struc, fpi)
            series8.append(serie("Structures (usines, bâtiments)", st_sh["dates"], st_sh["values"], BRONZE, "pct0"))
            s8_src.append(src("B009RC1Q027SBEA", title="Structures investment"))
        if soft:
            sf_sh = ratio_pct(soft, fpi)
            series8.append(serie("dont logiciels", sf_sh["dates"], sf_sh["values"], TEAL, "pct0", dash=True))
            s8_src.append(src("B985RC1Q027SBEA", title="Software investment"))
            # Bloc autonome pour l'encart « Aller plus loin » (déflation du code) :
            # logiciel en absolu (Md$) = effet-volume/Jevons, et en part.
            software = {
                "soft_dates": soft["dates"], "soft_bn": soft["values"],   # Md$
                "pct_dates": sf_sh["dates"], "pct": sf_sh["values"],      # % invest. fixe
            }
            if rnd:
                software["rnd_dates"] = rnd["dates"]
                software["rnd_bn"] = rnd["values"]                        # Md$
        usages.append(panel(
            "invest_qualite",
            "Composition de l'investissement : la bascule vers l'immatériel (% de l'investissement fixe)",
            ("La part de l'immatériel (logiciels, R&D, propriété intellectuelle) "
             "grimpe pendant que celle des structures recule&nbsp;: l'investissement "
             "ne disparaît pas, il change de <i>nature</i> — vers le numérique, "
             "l'IA, le cloud."),
            "pct0", series8, s8_src,
            note="Logiciels disponibles depuis 1959."))

    # ════════ §9 — Rachats financés par la dette (double axe) ════════
    debt = fetch_fred("BCNSDODNS", start="1946-01-01")        # M$
    baa = fetch_fred("BAA", start="1919-01-01")               # % mensuel
    if debt:
        add_meta("Dette des corporates non-financières", "FRED Z.1",
                 fred_url("BCNSDODNS"), "M$ → % du PIB.")
        dbt_d, dbt_v = as_pct_gdp(debt, gmap, 0.001)
        nbuy_d, nbuy_v = as_pct_gdp(netrep_sm, gmap, 1.0)
        series9 = [
            serie("Dette corporate / PIB", dbt_d, dbt_v, RED, "pct0"),
            serie("Rachats nets / PIB", nbuy_d, nbuy_v, GOLD, "pct1"),
        ]
        s9_src = [src("BCNSDODNS", computed="× 0,001 ÷ PIB"),
                  src("NCBCEBQ027S", computed="rachats nets ÷ PIB")]
        if baa:
            series9.append(serie("Taux Baa (corp., axe droit)", baa["dates"], baa["values"],
                                 BLUE, "pct1", axis="y2"))
            s9_src.append(src("BAA", start="1919", title="Moody's Seasoned Baa Corporate Bond Yield"))
            add_meta("Rendement obligataire Baa (Moody's)", "FRED",
                     fred_url("BAA"), "%, mensuel, depuis 1919.")
        usages.append(panel(
            "debt_buybacks",
            "Dette des entreprises, rachats nets et coût du crédit",
            ("Quand le crédit est bon marché (taux Baa bas), la dette corporate "
             "et les rachats grimpent de concert. <b>Corrélation, pas preuve</b>&nbsp;: "
             "l'argent est fongible — on ne peut pas affirmer comptablement que "
             "tel rachat est « financé par la dette », seulement que les deux "
             "progressent ensemble dans un argent peu cher."),
            "pct0", series9, s9_src, kind="dual",
            note="Axe gauche : % du PIB. Axe droit : rendement Baa (%)."))

    # ════════ §11 — Impact sur les marchés : part des profits rendue ════════
    if profits and div:
        netpay = combine(div, netrep_sm, lambda a, b: a + b)
        np_pp = ratio_pct(netpay, prof)
        np1 = np_pp["values"][-1] if np_pp["values"] else None
        np_avg10 = (round(sum(np_pp["values"][-40:]) / len(np_pp["values"][-40:]), 0)
                    if len(np_pp["values"]) >= 8 else None)
        marche.append(panel(
            "marche_impact",
            "Part des profits rendue aux actionnaires (dividendes + rachats nets, % des profits)",
            (f"Sur 10 ans, les entreprises rendent en moyenne <b>{np_avg10:.0f}%</b> "
             f"de leurs profits aux actionnaires (dividendes + rachats nets) — "
             f"dernier point {np1:.0f}%. Cette demande structurelle d'actions "
             f"(le rachat réduit le flottant et soutient le BPA) est un soutien "
             f"mécanique des cours. La « part de la hausse imputable aux rachats » "
             f"reste, elle, une <i>estimation</i> et non une donnée mesurée."),
            "pct0",
            [serie("Payout total / profits", np_pp["dates"], np_pp["values"], GOLDL, "pct0")],
            [src("BOGZ1FA106121075Q", computed="+ rachats nets ÷ profits", title="Dividendes nets, corporates non-fin."),
             src("NCBCEBQ027S"), src("NFCPATAX")],
            note="Net payout ratio = (dividendes + rachats nets) ÷ profits après impôt."))

    # ════════ §10 — Concentration : SEC EDGAR (live) ════════
    sys.stderr.write("[fa] SEC EDGAR — rachats par méga-cap…\n")
    topbuyers = fetch_top_buyers()
    if topbuyers:
        add_meta("Rachats d'actions par société (méga-caps)", "SEC EDGAR (XBRL)",
                 "https://www.sec.gov/edgar/search/",
                 "us-gaap:PaymentsForRepurchaseOfCommonStock, 10-K/FY, depuis ~2009.")

    # ════════ ZOOM — Mag 7 / IA-tech : CAPEX vs R&D vs rachats (SEC) ════════
    sys.stderr.write("[fa] SEC EDGAR — Mag 7 (CAPEX/R&D/rachats)…\n")
    mag7 = fetch_mag7()
    if mag7:
        add_meta("Mag 7 — CAPEX, R&D & rachats (live)", "SEC EDGAR (XBRL)",
                 "https://www.sec.gov/edgar/search/",
                 "PaymentsToAcquirePropertyPlantAndEquipment · ResearchAndDevelopmentExpense · "
                 "PaymentsForRepurchaseOfCommonStock, 10-K, agrégé par exercice.")

    # ════════ Marge nette agrégée des éditeurs de logiciels (SEC) ════════
    sys.stderr.write("[fa] SEC EDGAR — marges logiciels…\n")
    soft_margins = fetch_software_margins()
    if soft_margins:
        add_meta("Marge nette des éditeurs de logiciels (secteur)", "SEC EDGAR (XBRL)",
                 "https://www.sec.gov/edgar/search/",
                 f"NetIncomeLoss ÷ Revenues, 10-K, panier de {soft_margins['n_basket']} éditeurs "
                 "logiciels/SaaS US (hors conglomérats type Microsoft), pondéré par le CA.")

    # ── KPIs de tête ──────────────────────────────────────────────────────
    kpi = {
        "nes_pct_gdp": nes_last_pct,
        "nes_avg10": nes_avg10,
        "netrep_bn": round(netrep_recent, 0),
        "payout_pct_profits": (np_avg10 if (profits and div) else None),
        "top10_buybacks_bn": (round(sum(r["value_bn"] for r in topbuyers["rows"][:10]), 0)
                              if topbuyers else None),
    }

    # ── Synthèse §13 (verdicts calculés sur les vraies valeurs) ───────────
    def yn(cond, yes, no):
        return yes if cond else no

    questions = [
        {"q": "La bourse finance-t-elle encore, net, les entreprises&nbsp;?",
         "answer": yn(nes_avg10 is not None and nes_avg10 < 0, "NON (net)", "OUI"),
         "basis": (f"Émission nette d'actions = {nes_avg10:+.2f}% du PIB en moyenne "
                   f"sur 10 ans (négatif depuis ~{first_neg_year}).") if nes_avg10 is not None else "",
         "anchor": "#fa-sec-nes"},
        {"q": "La bourse accueille-t-elle plus d'entreprises&nbsp;?",
         "answer": "EN BAISSE",
         "basis": "~4 300 sociétés cotées aux États-Unis aujourd'hui contre ~8 100 en 1996 "
                  "(World Bank ; Doidge-Karolyi-Stulz, « The U.S. Listing Gap », 2017).",
         "anchor": "#fa-sec-financement"},
        {"q": "Les profits servent-ils d'abord à investir&nbsp;?",
         "answer": "PARTIELLEMENT",
         "basis": (f"Rachats + dividendes ≈ {kpi['payout_pct_profits']:.0f}% des profits."
                   if kpi["payout_pct_profits"] is not None else ""),
         "anchor": "#fa-sec-usages"},
        {"q": "L'investissement disparaît-il&nbsp;?",
         "answer": "NON — IL CHANGE",
         "basis": "Bascule du tangible (structures) vers l'immatériel (logiciels, R&D, IA).",
         "anchor": "#fa-sec-invest"},
        {"q": "Le phénomène est-il concentré&nbsp;?",
         "answer": yn(topbuyers and topbuyers["top10_share"] and topbuyers["top10_share"] > 0.5,
                      "TRÈS", "DIFFUS"),
         "basis": (f"Le top-10 = {topbuyers['top10_share'] * 100:.0f}% des rachats du panel suivi."
                   if topbuyers and topbuyers["top10_share"] else ""),
         "anchor": "#fa-sec-concentration"},
    ]
    proportion = (
        "La finance de marché américaine continue de financer une partie de "
        "l'innovation — mais via le capital-risque et le private equity, plus via "
        "la cote. Sur le marché coté, le flux net s'est inversé&nbsp;: les "
        "entreprises y rendent, net, plus de capital qu'elles n'en lèvent. La "
        "question n'est donc plus « financer OU rémunérer », mais dans quelle "
        "<b>proportion</b> — et celle-ci penche désormais nettement vers la "
        "rémunération des détenteurs de capital."
    )

    refs = [
        {"a": "Lazonick (2014, HBR)", "t": "« Profits Without Prosperity » — rachats vs investissement"},
        {"a": "Federal Reserve — Financial Accounts (Z.1)", "t": "émission nette d'actions, comptes de flux"},
        {"a": "S&P Dow Jones Indices (Silverblatt)", "t": "rachats & dividendes du S&P 500 (mesure indice, brut)"},
        {"a": "J. Ritter (Univ. of Florida)", "t": "base de données IPO (nombre & montants, depuis 1980)"},
        {"a": "Doidge, Karolyi & Stulz (2017)", "t": "« The U.S. Listing Gap » — déclin du nombre de cotées"},
        {"a": "SEC — Rule 10b-18 (1982)", "t": "safe harbor légalisant de fait les rachats d'actions"},
    ]

    payload = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "meta": {"generator": "fetch_finance_americaine.py", "sources": sources_meta},
        "kpi": kpi,
        "financement": financement,
        "usages": usages,
        "marche": marche,
        "topbuyers": topbuyers,
        "mag7": mag7,
        "software": software,
        "soft_margins": soft_margins,
        "conclusion": {"questions": questions, "proportion": proportion},
        "refs": refs,
    }

    n_panels = len(financement) + len(usages) + len(marche)
    if n_panels < 3:
        sys.stderr.write(f"[fa] trop peu de panneaux ({n_panels}) — cache NON écrit\n")
        sys.exit(1)

    CACHE_FILE.write_text(json.dumps(payload, separators=(",", ":")))
    CACHE_JS.write_text("window.__FINANCE_AMERICAINE__=" +
                        json.dumps(payload, separators=(",", ":")) + ";\n")
    sys.stderr.write(
        f"[fa] OK — {n_panels} panneaux · topbuyers:{topbuyers['n'] if topbuyers else 0} "
        f"· NES10:{nes_avg10} · netrep:{kpi['netrep_bn']} -> {CACHE_FILE.name}\n")


if __name__ == "__main__":
    main()
