#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Données fraîches des États (module de fetch_obligations.py) — 01/10/2026.

TOUT SE MET À JOUR SEUL (exigence de MA) : chaque fonction lit la source la plus
fraîche qui soit ouverte, garde la PÉRIODE, la DÉFINITION et la SOURCE de chaque
valeur, et ne remplace jamais un point par un plus ancien (« jamais à reculons »).

  dette_fraiche()   dette publique / PIB au DERNIER point publié, pays par pays
                    (Insee, Bundesbank, Banco de España, Banco de Portugal, CBS,
                    ONS, Trésor US, BRI, Eurostat)
  projections()     FMI (WEO), OCDE (Economic Outlook), Commission (AMECO)
  dette_totale()    BRI, crédit total au secteur non financier (État, ménages,
                    entreprises) + titres des sociétés financières, et le PIB des
                    quatre derniers trimestres → productivité marginale de la dette
  detenteurs()      historique des détenteurs : BCE (zone euro, 1995→), FRED (US),
                    BoJ (Japon, 1997→), ONS (Royaume-Uni, 1987→)
  notations_auto()  ESMA European Rating Platform (base réglementaire, quotidienne)
  defauts_auto()    Moody's (billet mensuel First Trust), S&P Europe (AFME)

⚠ Les définitions ne se mélangent pas : Maastricht, dette fédérale, valeur de
  marché, titres seuls — chaque point porte la sienne, la fiche l'affiche.
"""
import csv
import gzip
import html as H
import io
import json
import math
import re
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime

from oblig_net import get, get_txt, get_json, fred, log

ISO3 = {"us": "USA", "de": "DEU", "fr": "FRA", "it": "ITA", "es": "ESP", "gb": "GBR", "jp": "JPN", "ca": "CAN",
        "au": "AUS", "ch": "CHE", "nl": "NLD", "be": "BEL", "at": "AUT", "ie": "IRL", "pt": "PRT", "gr": "GRC",
        "cn": "CHN", "in": "IND"}
ISO2 = {c: c.upper() for c in ISO3}
ISO2["gb"] = "GB"


def _txt(url, **kw):
    b = get(url, **kw)
    if b is None:
        return None
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b.decode("utf-8-sig", "replace")


def _json(url, **kw):
    t = _txt(url, **kw)
    try:
        return json.loads(t) if t else None
    except Exception:  # noqa: BLE001
        return None


def _f(x):
    try:
        v = float(str(x).replace(",", "."))
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _trim(p):
    """« 2026-04-01 » ou « 2026-Q2 » ou « 202602 » → « 2026-T2 »."""
    s = str(p)
    m = re.match(r"(\d{4})-?Q(\d)$", s) or re.match(r"(\d{4})KW0?(\d)$", s)
    if m:
        return "%s-T%s" % (m.group(1), m.group(2))
    m = re.match(r"(\d{4})0(\d)$", s)
    if m:
        return "%s-T%s" % (m.group(1), m.group(2))
    m = re.match(r"(\d{4})-(\d{2})-(01|28|29|30|31)$", s)
    if m:
        return "%s-T%d" % (m.group(1), (int(m.group(2)) - 1) // 3 + 1)
    m = re.match(r"(\d{4}) ([A-Z]{3})$", s)
    if m:
        mo = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"].index(m.group(2)) + 1
        return "%s-%02d" % (m.group(1), mo)
    return s


# ═══════════════════════════════════════════════════════════════════════════
# 1. DETTE PUBLIQUE / PIB, DERNIER POINT PUBLIÉ
# ═══════════════════════════════════════════════════════════════════════════
_PIB_Q = {}


def pib_trimestriel():
    """PIB nominal trimestriel, monnaie nationale, NON désaisonnalisé (OCDE QNA ;
    la Chine n'existe qu'en NSA) → {iso3: {« 2026-Q2 »: valeur}}."""
    if _PIB_Q:
        return _PIB_Q
    u = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA,/Q.N.%s.S1..B1GQ._Z._Z._Z.XDC.V.N.T0102"
         "?startPeriod=1995-Q1&dimensionAtObservation=AllDimensions" % "+".join(ISO3.values()))
    t = _txt(u, accept="application/vnd.sdmx.data+csv; charset=utf-8", timeout=180)
    for x in csv.DictReader(io.StringIO(t or "")):
        v = _f(x.get("OBS_VALUE"))
        if v is not None:
            _PIB_Q.setdefault(x["REF_AREA"], {})[x["TIME_PERIOD"]] = v * 10 ** int(x.get("UNIT_MULT") or 0)
    return _PIB_Q


def _pib4(iso3, jusqua=None):
    s = pib_trimestriel().get(iso3) or {}
    qs = sorted(q for q in s if not jusqua or q <= jusqua)[-4:]
    return (sum(s[q] for q in qs), qs[-1]) if len(qs) == 4 else (None, None)


def _point(v, periode, definition, source, publie=None, extra=None):
    d = {"valeur": round(v, 1), "periode": periode, "definition": definition, "source": source}
    if publie:
        d["publie"] = publie
    if extra:
        d.update(extra)
    return d


def _fr():
    t = _txt("https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/010777608?lastNObservations=2")
    obs = re.findall(r'TIME_PERIOD="([^"]+)" OBS_VALUE="([^"]+)"', t or "")
    lu = re.search(r'LAST_UPDATE="([^"]+)"', t or "")
    if obs:
        return _point(float(obs[0][1]), _trim(obs[0][0]), "Maastricht", "Insee", lu.group(1) if lu else None)


def _de():
    t = _txt("https://api.statistiken.bundesbank.de/rest/data/BBGFS1/Q.BQ9959?lastNObservations=1&format=csv&lang=en", entetes={"Accept": "text/csv"})
    rows = [r for r in csv.reader(io.StringIO(t or "")) if r and re.match(r"\d{4}-Q\d", r[0])]
    if rows and _f(rows[-1][1]) is not None:
        return _point(_f(rows[-1][1]), _trim(rows[-1][0]), "Maastricht", "Bundesbank")


def _es():
    j = _json("https://app.bde.es/bierest/resources/srdatosapp/listaSeries?idioma=es&series=DTNPDE2010_P0000P_PS_APU&rango=30M")
    if j:
        q = j[0]
        return _point(float(q["valores"][0]), _trim(q["fechas"][0][:10]), "Maastricht (PDE)", "Banco de España")


def _pt():
    j = _json("https://bpstat.bportugal.pt/data/v1/domains/28/datasets/e2bc3b33d169f2d0885cffb9183fb48e/?lang=EN&series_ids=12561507")
    if not j:
        return None
    tm = list(j["dimension"]["reference_date"]["category"]["index"])
    v = j["value"]
    vals = [(tm[i], (v[i] if isinstance(v, list) else v.get(str(i)))) for i in range(len(tm))]
    vals = [x for x in vals if x[1] is not None]
    if vals:
        return _point(float(vals[-1][1]), _trim(vals[-1][0][:10]), "Maastricht", "Banco de Portugal")


def _nl():
    j = _json("https://opendata.cbs.nl/ODataApi/odata/84118NED/TypedDataSet?$filter=InstitutioneleSectoren%20eq%20'A044938'&$select=Perioden,SchuldEMU_13")
    q = [x for x in (j or {}).get("value", []) if "KW" in x["Perioden"] and x.get("SchuldEMU_13") is not None]
    if q:
        return _point(float(q[-1]["SchuldEMU_13"]), _trim(q[-1]["Perioden"]), "Maastricht", "CBS")


def _gb():
    d = _json("https://www.ons.gov.uk/economy/governmentpublicsectorandtaxes/publicsectorfinance/timeseries/a3pw/pusf/data")
    if d and d.get("months"):
        m = d["months"][-1]
        return _point(float(m["value"]), _trim(m["date"]), "Maastricht (dette brute des administrations)", "ONS",
                      extra={"prochaine": (d.get("description") or {}).get("nextRelease")})


def _us():
    p = get_json("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny?sort=-record_date&page%5Bsize%5D=1",
                 accept="application/json")
    g = fred("GDP", "2024-01-01")
    if p and p.get("data") and g:
        x = p["data"][0]
        return _point(100 * float(x["tot_pub_debt_out_amt"]) / (g[-1][1] * 1e9), x["record_date"],
                      "dette fédérale totale ÷ PIB en rythme annuel", "Trésor américain (Debt to the Penny) et BEA",
                      extra={"pib_trimestre": _trim(g[-1][0])})








def _bis_g(pays):
    t = _txt("https://stats.bis.org/api/v1/data/WS_TC/Q.%s.G.A.N.770.A?format=csv&lastNObservations=1" % "+".join(p.upper() for p in pays))
    out = {}
    for x in csv.DictReader(io.StringIO(t or "")):
        v = _f(x.get("OBS_VALUE"))
        if v is not None:
            out[x["BORROWERS_CTY"].lower()] = _point(v, _trim(x["TIME_PERIOD"]), "crédit aux administrations, valeur nominale", "BRI")
    return out


def _eurostat_q(geos):
    u = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/gov_10q_ggdebt?format=JSON&unit=PC_GDP&sector=S13&na_item=GD&lastTimePeriod=1&"
         + "&".join("geo=" + g for g in geos))
    d = get_json(u, accept="application/json")
    out = {}
    try:
        gi = {v: k for k, v in d["dimension"]["geo"]["category"]["index"].items()}
        t = list(d["dimension"]["time"]["category"]["index"])[0]
        nt = d["size"][-1]
        for k, v in d["value"].items():
            g = gi[(int(k) // nt) % d["size"][-2]]
            out[{"EL": "gr"}.get(g, g.lower())] = _point(v, _trim(t), "Maastricht", "Eurostat", (d.get("updated") or "")[:10])
    except Exception:  # noqa: BLE001
        pass
    return out


def dette_fraiche(journal, precedent=None):
    """{pays: point} — la source la plus fraîche ; jamais plus ancienne que le passage précédent."""
    precedent = precedent or {}
    out = {}
    out.update(_eurostat_q(["FR", "DE", "IT", "ES", "NL", "BE", "AT", "IE", "PT", "EL"]))
    # Canada, Japon, Australie : la BRI (titres et prêts, valeur nominale,
    # consolidés) — les sources nationales mêlent passifs de retraite (Canada),
    # valeur de marché (Japon) ou titres non consolidés (Australie).
    out.update(_bis_g(["ch", "cn", "in", "ca", "jp", "au"]))
    for c, fn in (("fr", _fr), ("de", _de), ("es", _es), ("pt", _pt), ("nl", _nl), ("gb", _gb), ("us", _us)):
        try:
            p = fn()
            if p:
                out[c] = p
        except Exception as e:  # noqa: BLE001
            journal.append("dette fraîche %s : %s" % (c, str(e)[:100]))
    # Italie : la Banca d'Italia (mensuel) n'a pas d'API ; Eurostat trimestriel fait foi.
    # Jamais à reculons : un point plus ancien que celui du passage précédent est refusé.
    for c, p in list(out.items()):
        a = precedent.get(c)
        if a and str(a.get("periode", "")) > str(p.get("periode", "")) and a.get("source") == p.get("source"):
            out[c] = a
    for c, a in precedent.items():
        if c not in out:
            out[c] = dict(a, repris=True)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2. PROJECTIONS : FMI, OCDE, COMMISSION
# ═══════════════════════════════════════════════════════════════════════════
def projections(journal):
    out = defaultdict(dict)
    editions = {}
    # OCDE : DF_EO pointe toujours vers la dernière édition
    try:
        df = _txt("https://sdmx.oecd.org/public/rest/dataflow/OECD.ECO.MAD/DSD_EO@DF_EO")
        m = re.search(r'<common:Name xml:lang="en">([^<]+)', df or "")
        editions["ocde"] = m.group(1) if m else "OCDE Economic Outlook"
        t = _txt("https://sdmx.oecd.org/public/rest/data/OECD.ECO.MAD,DSD_EO@DF_EO,/%s.GGFLMQ+GGFLQ.A?startPeriod=2015&dimensionAtObservation=AllDimensions"
                 % "+".join(ISO3.values()), accept="application/vnd.sdmx.data+csv; charset=utf-8", timeout=180)
        par = defaultdict(lambda: defaultdict(dict))
        for x in csv.DictReader(io.StringIO(t or "")):
            v = _f(x.get("OBS_VALUE"))
            if v is not None:
                par[x["REF_AREA"]][x["MEASURE"]][int(x["TIME_PERIOD"])] = round(v, 1)
        i3c = {v: k for k, v in ISO3.items()}
        for i3, mes in par.items():
            c = i3c.get(i3)
            if not c:
                continue
            serie = mes.get("GGFLMQ") or mes.get("GGFLQ")
            out[c]["ocde"] = {"serie": serie, "definition": "Maastricht" if mes.get("GGFLMQ") else "passifs financiers bruts"}
    except Exception as e:  # noqa: BLE001
        journal.append("projections OCDE : " + str(e)[:100])
    # Commission européenne (AMECO, prévisions de printemps/automne)
    try:
        an = date.today().year
        pays = ["FRA", "DEU", "ITA", "ESP", "NLD", "BEL", "AUT", "IRL", "PRT", "GRC", "USA", "GBR", "JPN"]
        t = _txt("https://ec.europa.eu/economy_finance/ameco/wq/series?fullVariable=1.0.319.0.UDGG&countries=%s&years=%s&defaultCountries=0&Lastyear=0&Yearorder=ASC"
                 % (",".join(pays), ",".join(str(y) for y in range(an - 6, an + 3))), timeout=120)
        lignes = re.findall(r"<tr>(.*?)</tr>", t or "", re.S)
        tete = re.findall(r"<t[hd][^>]*>([^<]*)</t[hd]>", lignes[0]) if lignes else []
        annees = [int(x) for x in tete if re.fullmatch(r"\d{4}", x.strip())]
        i3c = {v: k for k, v in ISO3.items()}
        for tr in lignes[1:]:
            td = re.findall(r"<td[^>]*>([^<]*)</td>", tr)
            if len(td) < 3 + len(annees):
                continue
            pays_lib = td[0].strip()
            code = next((v for k, v in {"France": "fr", "Germany": "de", "Italy": "it", "Spain": "es", "Netherlands": "nl", "Belgium": "be", "Austria": "at",
                                        "Ireland": "ie", "Portugal": "pt", "Greece": "gr", "United States": "us", "United Kingdom": "gb", "Japan": "jp"}.items() if k in pays_lib), None)
            vals = [_f(x) for x in td[-len(annees):]]
            if code and sum(v is not None for v in vals) >= 3:
                out[code]["commission"] = {"serie": {a: round(v, 1) for a, v in zip(annees, vals) if v is not None}, "definition": "Maastricht"}
        page = _txt("https://economy-finance.ec.europa.eu/economic-research-and-databases/economic-databases/ameco-database_en")
        m = re.search(r"Last update:\s*([^<]+)", page or "")
        editions["commission"] = H.unescape(m.group(1).strip()) if m else "AMECO"
    except Exception as e:  # noqa: BLE001
        journal.append("projections Commission : " + str(e)[:100])
    return dict(out), editions


# ═══════════════════════════════════════════════════════════════════════════
# 3. DETTE TOTALE ET PRODUCTIVITÉ MARGINALE (BRI)
# ═══════════════════════════════════════════════════════════════════════════
def dette_totale(journal):
    """{pays: {t, pib4, g, h, n, c, fin (en monnaie nationale, Md), ratios %PIB, pm (productivité marginale)}}"""
    cles = "+".join(ISO2[c] for c in ISO3)
    t = _txt("https://stats.bis.org/api/v1/data/WS_TC/Q.%s.G+H+N+C.A.M+N.XDC+770.A?format=csv&startPeriod=1995-Q1" % cles, timeout=240)
    s = defaultdict(dict)
    for x in csv.DictReader(io.StringIO(t or "")):
        v = _f(x.get("OBS_VALUE"))
        if v is not None:
            s[(x["BORROWERS_CTY"].lower(), x["TC_BORROWERS"], x["VALUATION"], x["UNIT_TYPE"])][x["TIME_PERIOD"]] = v
    # titres des sociétés financières (monnaie nationale)
    fin = defaultdict(dict)
    t2 = _txt("https://stats.bis.org/api/v1/data/WS_NA_SEC_DSS/Q.N.%s.XW.S12.S1.N.L.LE.F3.T._Z.._T.N+M.V.N._T?format=csv&startPeriod=1995-Q1" % cles, timeout=240)
    for x in csv.DictReader(io.StringIO(t2 or "")):
        v = _f(x.get("OBS_VALUE"))
        if v is None or x.get("UNIT_MEASURE") == "USD" and x["REF_AREA"] != "US":
            continue
        k = x["REF_AREA"].lower()
        val = x.get("VALUATION")
        # nominal de préférence
        if val == "N" or x["TIME_PERIOD"] not in fin[k]:
            fin[k][x["TIME_PERIOD"]] = v * 10 ** (int(x.get("UNIT_MULT") or 9) - 9)
    out = {}
    for c in ISO3:
        C_x, C_p = s.get((c, "C", "M", "XDC")), s.get((c, "C", "M", "770"))
        if not C_x or not C_p:
            continue
        G_x = s.get((c, "G", "N", "XDC")) or s.get((c, "G", "M", "XDC")) or {}
        qs = sorted(q for q in C_x if q in C_p and C_p[q])
        if len(qs) < 12:
            continue
        pib4 = {q: C_x[q] / (C_p[q] / 100) for q in qs}
        def r(k, q):
            # ⚠ L'État en valeur NOMINALE (celle de Maastricht et de dette_g) :
            #   en valeur de marché, la chute des prix obligataires depuis 2022
            #   « allège » la dette de 10 points en France. Ménages et entreprises :
            #   la BRI ne publie que la valeur de marché (prêts au nominal).
            ordre = ("N", "M") if k == "G" else ("M", "N")
            d = s.get((c, k, ordre[0], "770")) or s.get((c, k, ordre[1], "770")) or {}
            return d.get(q)
        pg, ph, pn = [r("G", q) for q in qs], [r("H", q) for q in qs], [r("N", q) for q in qs]
        # Total non financier = somme des trois parts (cohérent avec l'empilement
        # affiché) ; à défaut d'une part, le total publié par la BRI.
        pc = [round(a + b + d, 1) if None not in (a, b, d) else C_p[q] for a, b, d, q in zip(pg, ph, pn, qs)]
        o = {"t": qs, "pib4": [round(pib4[q], 1) for q in qs],
             "dette_c": [round(x * pib4[q] / 100, 1) for x, q in zip(pc, qs)],
             "dette_g": [round(G_x[q], 1) if q in G_x else None for q in qs],
             "pct_g": pg, "pct_h": ph, "pct_n": pn, "pct_c": pc}
        F = fin.get(c, {})
        if F:
            o["dette_fin"] = [round(F[q], 1) if q in F else None for q in qs]
            o["pct_fin"] = [round(100 * F[q] / pib4[q], 1) if q in F else None for q in qs]
        # Productivité marginale : PIB supplémentaire par unité de dette supplémentaire,
        # sur un an (4 trimestres) et sur cinq ans (20 trimestres, plus stable).
        def pm(dette, n):
            res = []
            for i, q in enumerate(qs):
                if i < n or dette[i] is None or dette[i - n] is None:
                    res.append(None)
                    continue
                dd = dette[i] - dette[i - n]
                dg = o["pib4"][i] - o["pib4"][i - n]
                # Dette quasi stable (moins d'un point de PIB par an) : le rapport
                # explose (×15, ×22 : Irlande 2018, Grèce 2022) sans rien dire —
                # vide plutôt que faux.
                res.append(round(dg / dd, 3) if dd > 0.01 * o["pib4"][i] * n / 4 else None)
            return res
        tot = [(o["dette_c"][i] + (o["dette_fin"][i] or 0)) if o.get("dette_fin") and o["dette_fin"][i] is not None else o["dette_c"][i] for i in range(len(qs))]
        o["dette_tout"] = [round(x, 1) for x in tot]
        o["pm_public_1a"] = pm(o["dette_g"], 4)
        o["pm_public_5a"] = pm(o["dette_g"], 20)
        o["pm_total_1a"] = pm(o["dette_c"], 4)
        o["pm_total_5a"] = pm(o["dette_c"], 20)
        o["pm_tout_1a"] = pm(o["dette_tout"], 4)
        o["pm_tout_5a"] = pm(o["dette_tout"], 20)
        o["source"] = "BRI, crédit total au secteur non financier (WS_TC) et titres des sociétés financières (DSS)"
        o["valorisation"] = "État en valeur nominale ; ménages et entreprises : prêts au nominal, titres en valeur de marché"
        out[c] = o
    if not out:
        journal.append("dette totale BRI : vide")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 4. HISTORIQUE DES DÉTENTEURS
# ═══════════════════════════════════════════════════════════════════════════
BCE = {"de": "DE", "fr": "FR", "it": "IT", "es": "ES", "nl": "NL", "be": "BE", "at": "AT", "ie": "IE", "pt": "PT"}


def detenteurs(journal):
    """{pays: {series: {détenteur: {période: %}}, source, frequence}}"""
    out = {}
    lib = {("W1", "S1"): "non_residents", ("W2", "S121"): "banque_centrale", ("W2", "S12T"): "banques",
           ("W2", "S12P"): "assureurs_fonds", ("W2", "S1U"): "menages_entreprises"}
    for c, a in BCE.items():
        try:
            t = _txt("https://data-api.ecb.europa.eu/service/data/GFS/A.N.%s..S13..C.L.LE.GD.T._Z.XDC._T.F.V.N._T?format=csvdata&detail=dataonly" % a,
                     entetes={"Accept": "text/csv"}, timeout=120)
            g = defaultdict(dict)
            for x in csv.DictReader(io.StringIO(t or "")):
                v = _f(x.get("OBS_VALUE"))
                if v is not None:
                    g[(x["COUNTERPART_AREA"], x["COUNTERPART_SECTOR"])][x["TIME_PERIOD"]] = v
            tot = g.get(("W0", "S1")) or {}
            series = {}
            for k, nom in lib.items():
                if k in g:
                    series[nom] = {y: round(100 * g[k][y] / tot[y], 1) for y in g[k] if tot.get(y)}
            if len(series) >= 2:
                out[c] = {"series": series, "source": "BCE, statistiques de finances publiques (dette des administrations, par détenteur)", "frequence": "annuelle"}
        except Exception as e:  # noqa: BLE001
            journal.append("détenteurs %s : %s" % (c, str(e)[:80]))
    # États-Unis : OFS-2 via FRED (part de la dette détenue par le public)
    try:
        fi, fe, pu = dict(fred("FDHBFIN")), dict(fred("FDHBFRBN")), dict(fred("FYGFDPUN"))
        # ⚠ FDHBFRBN = la Réserve fédérale SEULE, en part de la dette détenue par le
        #   public (pas « Fed et comptes publics », catégorie du bulletin rapportée
        #   à la dette totale) : clés distinctes pour ne pas mélanger les deux bases.
        series = {"Foreign And International": {}, "Federal Reserve": {}, "Other Investors": {}}
        for d0 in sorted(set(fi) & set(pu)):
            p = pu[d0] / 1000.0
            if not p:
                continue
            q = "%s-Q%d" % (d0[:4], (int(d0[5:7]) - 1) // 3 + 1)
            series["Foreign And International"][q] = round(100 * fi[d0] / p, 1)
            if d0 in fe:
                series["Federal Reserve"][q] = round(100 * fe[d0] / p, 1)
                series["Other Investors"][q] = round(100 - 100 * (fi[d0] + fe[d0]) / p, 1)
        series = {k: v for k, v in series.items() if v}
        if series:
            out["us"] = {"series": series, "source": "Trésor américain (Treasury Bulletin, OFS-2) via FRED", "frequence": "trimestrielle",
                         "base": "en % de la dette détenue par le public (hors comptes internes de l'État fédéral)"}
    except Exception as e:  # noqa: BLE001
        journal.append("détenteurs us : " + str(e)[:80])
    # Japon : comptes financiers de la Banque du Japon (1997→)
    try:
        codes = {"FOF_FFAS700A311": "_total", "FOF_FFAS110A311": "banque_du_japon", "FOF_FFAS120A311": "banques",
                 "FOF_FFAS130A311": "assureurs", "FOF_FFAS430A311": "menages", "FOF_FFAS500A311": "non_residents"}
        d = get_json("https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en&db=FF&code=%s&startDate=199704" % ",".join(codes),
                     ua=None, accept="application/json", timeout=120)
        s = {codes[x["SERIES_CODE"]]: dict(zip(x["VALUES"]["SURVEY_DATES"], x["VALUES"]["VALUES"])) for x in (d or {}).get("RESULTSET", [])}
        tot = s.get("_total") or {}
        series = {}
        for k, v in s.items():
            if k == "_total":
                continue
            series[k] = {"%s-Q%d" % (str(p)[:4], int(str(p)[4:])): round(100 * x / tot[p], 1) for p, x in v.items() if x is not None and tot.get(p)}
        if series:
            out["jp"] = {"series": series, "source": "Banque du Japon, comptes financiers (titres d'État et FILP)", "frequence": "trimestrielle"}
    except Exception as e:  # noqa: BLE001
        journal.append("détenteurs jp : " + str(e)[:80])
    # Royaume-Uni : ONS (comptes financiers, gilts)
    try:
        codes = {"nyxq": "_total", "nldt": "non_residents", "nntv": "banques_et_boe", "nizb": "assureurs_fonds", "nist": "menages", "nlqj": "autres_financieres"}
        ser = {}
        for cd, nom in codes.items():
            j = _json("https://www.ons.gov.uk/economy/grossdomesticproductgdp/timeseries/%s/ukea/data" % cd)
            if j and j.get("quarters"):
                ser[nom] = {re.sub(r"(\d{4}) Q(\d)", r"\1-Q\2", x["date"]): _f(x["value"]) for x in j["quarters"] if _f(x.get("value")) is not None}
            time.sleep(0.4)
        tot = ser.get("_total") or {}
        series = {k: {p: round(100 * v / tot[p], 1) for p, v in d0.items() if tot.get(p)} for k, d0 in ser.items() if k != "_total"}
        if series and tot:
            out["gb"] = {"series": series, "source": "ONS, comptes financiers (gilts ; « banques » inclut la Banque d'Angleterre)", "frequence": "trimestrielle"}
    except Exception as e:  # noqa: BLE001
        journal.append("détenteurs gb : " + str(e)[:80])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 5. NOTATIONS : ESMA EUROPEAN RATING PLATFORM
# ═══════════════════════════════════════════════════════════════════════════
# Noms exacts des États dans l'ESMA, par agence (sondés le 01/10/2026 : 46 couples
# sur 54 trouvés tels quels ; les autres sont des essais, et Wikipédia prend le relais).
NOMS_ESMA = {"fr": {"sp": ["Republic of France"], "moodys": ["France, Government of"], "fitch": ["France"]}, "de": {"moodys": ["Germany, Government of"], "sp": ["Federal Republic of Germany"], "fitch": ["Germany"]}, "it": {"sp": ["Republic of Italy"], "moodys": ["Italy, Government of"], "fitch": ["Italy"]}, "es": {"sp": ["Kingdom of Spain"], "moodys": ["Spain, Government of"], "fitch": ["Spain"]}, "nl": {"moodys": ["Netherlands, Government of"], "fitch": ["Netherlands"], "sp": ["The Netherlands", "Kingdom of the Netherlands", "Netherlands"]}, "be": {"sp": ["Kingdom of Belgium"], "moodys": ["Belgium, Government of"], "fitch": ["Belgium"]}, "at": {"sp": ["Republic of Austria"], "fitch": ["Austria"], "moodys": ["Austria, Government of"]}, "ie": {"moodys": ["Ireland, Government of"], "sp": ["Ireland"], "fitch": ["Ireland"]}, "pt": {"sp": ["Republic of Portugal"], "moodys": ["Portugal, Government of"], "fitch": ["Portugal"]}, "gr": {"moodys": ["Greece, Government of"], "fitch": ["Greece"], "sp": ["Hellenic Republic"]}, "us": {"moodys": ["United States of America, Government of"], "fitch": ["United States of America"], "sp": ["United States of America", "United States"]}, "gb": {"moodys": ["United Kingdom, Government of"], "sp": ["United Kingdom of Great Britain and Northern Ireland"], "fitch": ["United Kingdom"]}, "jp": {"sp": ["Japan"], "fitch": ["Japan"], "moodys": ["Japan, Government of"]}, "ca": {"fitch": ["Canada"], "sp": ["Canada"], "moodys": ["Canada, Government of"]}, "au": {"sp": ["Commonwealth of Australia"], "moodys": ["Australia, Government of"], "fitch": ["Australia"]}, "ch": {"moodys": ["Switzerland, Government of"], "sp": ["Swiss Confederation"], "fitch": ["Switzerland"]}, "cn": {"fitch": ["China"], "moodys": ["China, Government of"], "sp": ["People's Republic of China"]}, "in": {"sp": ["Republic of India"], "fitch": ["India"], "moodys": ["India, Government of"]}, "fi": {"moodys": ["Finland, Government of"], "sp": ["Republic of Finland"], "fitch": ["Finland"]}, "sk": {"moodys": ["Slovakia, Government of"], "sp": ["Slovak Republic"], "fitch": ["Slovakia"]}, "lt": {"moodys": ["Lithuania, Government of"], "sp": ["Republic of Lithuania"], "fitch": ["Lithuania"]}, "si": {"moodys": ["Slovenia, Government of"], "sp": ["Republic of Slovenia"], "fitch": ["Slovenia"]}, "lv": {"moodys": ["Latvia, Government of"], "sp": ["Republic of Latvia"], "fitch": ["Latvia"]}, "ee": {"moodys": ["Estonia, Government of"], "sp": ["Republic of Estonia"], "fitch": ["Estonia"]}, "lu": {"moodys": ["Luxembourg, Government of"], "sp": ["Grand Duchy of Luxembourg"], "fitch": ["Luxembourg"]}, "mt": {"moodys": ["Malta, Government of"], "sp": ["Republic of Malta"], "fitch": ["Malta"]}, "cy": {"moodys": ["Cyprus, Government of"], "sp": ["Republic of Cyprus"], "fitch": ["Cyprus"]}, "hr": {"moodys": ["Croatia, Government of"], "sp": ["Republic of Croatia"], "fitch": ["Croatia"]}, "bg": {"moodys": ["Bulgaria, Government of"], "sp": ["Republic of Bulgaria"], "fitch": ["Bulgaria"]}}
AGENCE_ERP = {"sp": "Standard & Poor", "moodys": "Moody", "fitch": "Fitch"}
ECHELLES = {"sp": ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"],
            "fitch": ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"],
            "moodys": ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]}


def _date_action(s):
    """Date de la dernière action d'une ligne de la table — jamais une date FUTURE
    (la page belge annonce les prochaines revues : elles ne sont pas des actions)."""
    auj = date.today().isoformat()
    m = [d for d in re.findall(r"(\d{4}-\d{2}-\d{2})", s or "") if d <= auj]
    return max(m) if m else None


def _persp(txt, defaut):
    t = (txt or "").lower()
    return "négative" if "negative" in t else "positive" if "positive" in t else "en évolution" if "developing" in t else "stable" if "stable" in t else defaut


def _esma(nom, motif):
    q = {"q": 'type_s:parent AND ratedObjectCode:ISR AND issuerName:"%s"' % nom, "rows": 40, "wt": "json", "sort": "racValidityDatetime desc",
         "fl": "craName,issuerName,ratingValueLabel,racValidityDatetimeStr,lastActionTypeLabel,ratingStatusLabel,timeHorizonType,localForeignCurrencyCode"}
    d = get_json("https://registers.esma.europa.eu/solr/esma_registers_radar/select?" + urllib.parse.urlencode(q), accept="application/json", timeout=60)
    docs = [x for x in ((d or {}).get("response") or {}).get("docs") or []
            if x.get("issuerName") == nom and motif.lower() in (x.get("craName") or "").lower() and x.get("timeHorizonType") == "L"]
    docs.sort(key=lambda x: (x.get("racValidityDatetimeStr") or "", x.get("localForeignCurrencyCode") == "FC"), reverse=True)
    return docs[0] if docs else None


def _wikipedia(journal):
    """Lignes de la liste Wikipédia des notes souveraines : {(agence, pays): (note, perspective, date)}.
    Ne sert QUE de détecteur : une ligne n'est prise que si son action est postérieure à la nôtre."""
    agences = {"Standard & Poor's": "sp", "Fitch": "fitch", "Moody's": "moodys"}
    pays_w = {"France": "fr", "Germany": "de", "Italy": "it", "Spain": "es", "Netherlands": "nl", "Belgium": "be", "Austria": "at", "Ireland": "ie",
              "Portugal": "pt", "Greece": "gr", "United States": "us", "United Kingdom": "gb", "Japan": "jp", "Canada": "ca", "Australia": "au",
              "Switzerland": "ch", "China": "cn", "India": "in"}
    j = get_json("https://en.wikipedia.org/w/api.php?action=parse&page=List_of_countries_by_credit_rating&prop=wikitext|revid&format=json&formatversion=2",
                 accept="application/json", entetes={"User-Agent": "scf-data/1.0 (https://github.com/MarcAntoineBA/scf-data; collecteur obligataire)"})
    out = {}
    try:
        w = j["parse"]["wikitext"]
    except Exception:  # noqa: BLE001
        journal.append("notations Wikipédia : illisible")
        return out
    secs = [(m.start(), m.group(2).strip()) for m in re.finditer(r"^(={2,4})\s*(.*?)\s*\1\s*$", w, re.M)]
    for i, (pos, nom) in enumerate(secs):
        ag = agences.get(nom)
        if not ag:
            continue
        corps = w[pos:secs[i + 1][0] if i + 1 < len(secs) else len(w)]
        for row in corps.split("\n|-"):
            m = re.search(r"\{\{flag(?:icon)?\|([^}|]+)", row)
            r = re.search(r"\{\{Percentage bar\|[^|]*\|[^|]*\|([^|}]+)\|", row)
            dt = re.search(r"\|\|\s*(\d{1,2} [A-Z][a-z]+ \d{4})\s*(?:\|\||<ref|$)", row, re.M)
            if not (m and r and dt) or m.group(1).strip() not in pays_w:
                continue
            quand = None
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    quand = datetime.strptime(dt.group(1), fmt).date().isoformat()
                    break
                except ValueError:
                    pass
            if quand:
                out[(ag, pays_w[m.group(1).strip()])] = (r.group(1).strip(), _persp(row, None), quand)
    return out


def notations_auto(journal, table):
    """Met la table vérifiée à jour, sans intervention : l'ESMA (base réglementaire) d'abord ;
    une note n'est remplacée que si l'action est DATÉE APRÈS celle de la table. Pour les
    couples absents de l'ESMA, Wikipédia, sous la même règle, marqué « à confirmer ».
    Rend (table, changements)."""
    changes = []
    vus = set()
    for c, ag_tab in table.items():
        for ag, motif in AGENCE_ERP.items():
            x = None
            for nom in (NOMS_ESMA.get(c) or {}).get(ag, []):
                try:
                    x = _esma(nom, motif)
                except Exception as e:  # noqa: BLE001
                    journal.append("ESMA %s %s : %s" % (c, ag, str(e)[:60]))
                time.sleep(0.25)
                if x:
                    break
            if not x:
                continue
            vus.add((ag, c))
            d_erp = (x.get("racValidityDatetimeStr") or "")[:10]
            note = (x.get("ratingValueLabel") or "").strip()
            # Une note RETIRÉE (S&P sur l'Estonie, 31/12/2024) n'existe plus : elle sort
            # de la table si le retrait est postérieur à la ligne, et n'y entre jamais.
            if "withdraw" in ((x.get("ratingStatusLabel") or "") + (x.get("lastActionTypeLabel") or "")).lower():
                act = ag_tab.get(ag) or {}
                d_tab = _date_action(act.get("action"))
                if act and d_erp and (not d_tab or d_erp > d_tab):
                    changes.append({"pays": c, "agence": ag, "avant": act.get("note"), "apres": "retirée", "date": d_erp, "source": "ESMA"})
                    del ag_tab[ag]
                continue
            if note not in ECHELLES[ag]:
                continue
            act = ag_tab.get(ag) or {}
            d_tab = _date_action(act.get("action"))
            if d_erp and (not d_tab or d_erp > d_tab):
                persp = _persp(x.get("ratingStatusLabel"), act.get("perspective"))
                # Un CHANGEMENT : la note bouge, ou une perspective connue bouge.
                # Une perspective qui manquait au relevé et que l'ESMA complète
                # n'est pas un changement (« Aaa None → Aaa stable »).
                if note != act.get("note") or (act.get("perspective") and persp != act.get("perspective")):
                    changes.append({"pays": c, "agence": ag, "avant": " ".join(str(v) for v in (act.get("note"), act.get("perspective")) if v),
                                    "apres": " ".join(str(v) for v in (note, persp) if v), "date": d_erp, "source": "ESMA"})
                ag_tab[ag] = {"note": note, "perspective": persp, "action": "%s (%s)" % (d_erp, (x.get("lastActionTypeLabel") or "action").lower()),
                              "url": "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "type": "ESMA"}
    # Couples absents de l'ESMA : Wikipédia, sous la règle de la date.
    try:
        W = _wikipedia(journal)
        for (ag, c), (note, persp, quand) in W.items():
            if (ag, c) in vus or c not in table or note not in ECHELLES[ag]:
                continue
            act = table[c].get(ag) or {}
            d_tab = _date_action(act.get("action"))
            if d_tab and quand > d_tab:
                changes.append({"pays": c, "agence": ag, "avant": act.get("note"), "apres": note, "date": quand, "source": "Wikipédia"})
                table[c][ag] = {"note": note, "perspective": persp or act.get("perspective"), "action": "%s (selon Wikipédia, à confirmer)" % quand,
                                "url": "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "type": "Wikipédia (à confirmer)"}
    except Exception as e:  # noqa: BLE001
        journal.append("notations Wikipédia : " + str(e)[:80])
    return table, changes


# ═══════════════════════════════════════════════════════════════════════════
# 6. DÉFAUTS : MOODY'S (FIRST TRUST) ET S&P EUROPE (AFME)
# ═══════════════════════════════════════════════════════════════════════════
RX_M = re.compile(r"Moody.s reported that its trailing 12-month global speculative-grade default rate (?:stood at|was|rose to|fell to|declined to|increased to|edged (?:up|down) to)\s*([\d.]+)%\s*(?:at the end of|in)\s*([A-Z][a-z]+(?: \d{4})?)", re.I)
RX_US = re.compile(r"U\.S\. speculative-grade default rate (?:was|stood at|rose to|fell to|declined to|increased to)\s*([\d.]+)%\s*in\s*([A-Z][a-z]+)", re.I)
RX_PREV = re.compile(r"forecast[^.]{0,200}?([\d.]+)%[^.]{0,80}?(?:global)[^.]{0,120}?([\d.]+)%", re.I)
MOIS_EN = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


def _texte(t):
    return H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<script.*?</script>", "", t or ""))))


def defauts_auto(journal, precedent):
    out = {k: dict(v) for k, v in (precedent or {}).items()}
    try:
        lst = _txt("https://www.ftportfolios.com/blogs/InvestBlog/Factoid")
        liens = []
        for l in re.findall(r'href=["\']([^"\']*/InvestBlog/\d{4}/\d+/\d+/factoid[^"\']*)["\']', lst or "", re.I):
            if l not in liens:
                liens.append(l)
        for l in liens[:30]:
            jour = int(re.search(r"/\d{4}/\d+/(\d+)/", l).group(1))
            if not 8 <= jour <= 24:
                continue
            url = l if l.startswith("http") else "https://www.ftportfolios.com" + l
            t = _texte(_txt(url))
            g = RX_M.search(t)
            if not g:
                time.sleep(0.5)
                continue
            an_l = re.search(r"/(\d{4})/(\d+)/", l)
            mois_txt = g.group(2).split()[0]
            mois = MOIS_EN.get(mois_txt)
            an = int(an_l.group(1)) if an_l else date.today().year
            if mois and an_l and mois > int(an_l.group(2)):
                an -= 1
            au = "%d-%02d" % (an, mois) if mois else None
            v = float(g.group(1))
            if au and 0 < v < 20 and au >= (out.get("monde") or {}).get("au", ""):
                avant = out.get("monde") or {}
                out["monde"] = {"taux": v, "au": au, "quoi": "taux de défaut spéculatif mondial sur 12 mois (Moody's)", "url": url,
                                "precedent": {"taux": avant.get("taux"), "au": avant.get("au")} if avant.get("au") and avant.get("au") != au else avant.get("precedent"),
                                "moyenne_hist": avant.get("moyenne_hist", 4.2), "prevision": avant.get("prevision")}
                u = RX_US.search(t)
                if u:
                    avant = out.get("us") or {}
                    vu = float(u.group(1))
                    if 0 < vu < 20:
                        out["us"] = {"taux": vu, "au": au, "quoi": "taux de défaut spéculatif sur 12 mois, États-Unis (Moody's)", "url": url,
                                     "precedent": {"taux": avant.get("taux"), "au": avant.get("au")} if avant.get("au") and avant.get("au") != au else avant.get("precedent"),
                                     "prevision": avant.get("prevision")}
            break
    except Exception as e:  # noqa: BLE001
        journal.append("défauts Moody's : " + str(e)[:80])
    try:
        lst = _txt("https://www.afme.eu/publications/data-research/")
        liens = sorted(set(re.findall(r'href="(/publications/data-research/european-high-yield-leveraged-loan[^"]*q([1-4])-(\d{4})/)"', lst or "")),
                       key=lambda x: (x[2], x[1]))
        if liens:
            chemin, q, y = liens[-1]
            t = _texte(_txt("https://www.afme.eu" + chemin))
            m = re.search(r"S&P trailing 12-month speculative grade bond default rate \w+ from ([\d.]+)% in (Q[1-4] \d{4}) to ([\d.]+)% in (Q[1-4] \d{4})", t)
            if m:
                v = float(m.group(3))
                qq, yy = m.group(4).split()
                au = "%s-%02d" % (yy, int(qq[1]) * 3)
                if 0 < v < 20 and au >= (out.get("europe") or {}).get("au", ""):
                    qq0, yy0 = m.group(2).split()
                    out["europe"] = {"taux": v, "au": au, "quoi": "taux de défaut spéculatif européen sur 12 mois (S&P, repris par l'AFME)",
                                     "url": "https://www.afme.eu" + chemin, "precedent": {"taux": float(m.group(1)), "au": "%s-%02d" % (yy0, int(qq0[1]) * 3)},
                                     "moyenne_hist": None, "prevision": None}
    except Exception as e:  # noqa: BLE001
        journal.append("défauts AFME : " + str(e)[:80])
    return out


if __name__ == "__main__":
    import sys
    j = []
    quoi = sys.argv[1:] or ["dette", "proj", "total", "det", "notes", "defauts"]
    if "dette" in quoi:
        d = dette_fraiche(j)
        for c, p in sorted(d.items()):
            print(c, p["valeur"], p["periode"], p["source"], "|", p["definition"][:40])
    if "proj" in quoi:
        p, e = projections(j)
        print(e)
        for c in ("fr", "it", "us", "jp"):
            print(c, {k: {a: v for a, v in x["serie"].items() if a >= 2025} for k, x in p.get(c, {}).items()})
    if "total" in quoi:
        t0 = time.time()
        d = dette_totale(j)
        for c in ("fr", "us", "jp", "it"):
            o = d.get(c)
            if o:
                i = len(o["t"]) - 1
                print(c, o["t"][i], "PIB4", o["pib4"][i], "C%", o["pct_c"][i], "G%", o["pct_g"][i], "fin%", (o.get("pct_fin") or [None])[i],
                      "pm_pub_5a", o["pm_public_5a"][i], "pm_tot_5a", o["pm_total_5a"][i], "pm_tot_1a", o["pm_total_1a"][i])
        print(len(d), "pays", round(time.time() - t0), "s")
    if "det" in quoi:
        d = detenteurs(j)
        for c, o in d.items():
            s = o["series"]
            k = next(iter(s))
            print(c, list(s.keys()), min(s[k]), max(s[k]))
    if "defauts" in quoi:
        print(defauts_auto(j, {}))
    print("journal", j)
