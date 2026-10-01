#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emprunts d'État — taux par pays et par échéance (module de fetch_obligations.py).

UNE SOURCE OFFICIELLE PAR PAYS, la plus fraîche qui soit ouverte (testé le
01/10/2026) : Trésor US + FRED, Bundesbank, Banque de France (catalogue),
Banco de España, Bank of England, ministère des Finances japonais, Banque du
Canada, Reserve Bank of Australia, Banque nationale suisse, BCE.

⚠ TROIS FAMILLES DE TAUX, jamais mélangées sur une même courbe :
  titre de référence (Espagne, Canada), échéance constante interpolée (TEC
  France, Trésor US, Japon, Australie), courbe modélisée (Bundesbank, BoE,
  BCE). La famille est publiée avec chaque série.
⚠ DATER LA SOURCE : chaque taux porte la date de SA dernière observation
  (J, J+1, J+2, une semaine pour l'Australie, un mois pour les séries BCE
  mensuelles) — jamais l'heure de la collecte.
⚠ VIDE PLUTÔT QUE PLAUSIBLE : pas de 30 ans espagnol, pas de quotidien italien
  à jour, pas de rendement réel français : la fiche le dit, elle ne reprend
  jamais la valeur d'un voisin.
⚠ La France n'expose sans clé que ses DEUX dernières valeurs quotidiennes :
  la série quotidienne s'ACCUMULE de passage en passage (relue dans la fiche
  précédente), l'histoire vient du 10 ans quotidien de la Banque nationale de
  Belgique (1993 → avec trois mois de retard) et du mensuel de la BCE.
"""
import csv
import os
import io
import json
import re
import urllib.parse
from datetime import date, datetime

from oblig_net import (get, get_txt, get_json, fred, log, estnb, compacter, en_colonnes, hebdo, mensuel,
                       variations, centile, extremes, valeur_au, il_y_a)

BBK_H = {"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}
ZONE_EURO = {"fr", "de", "it", "es", "nl", "be", "pt", "gr", "at", "ie", "fi"}

# code → nom, devise, zone, fiches (échéances qui ont une fiche), source affichée
PAYS = [
    {"code": "us", "nom": "États-Unis", "devise": "USD", "iso2": "US", "iso3": "USA", "titre": "Treasuries", "banque": "Trésor américain"},
    {"code": "de", "nom": "Allemagne", "devise": "EUR", "iso2": "DE", "iso3": "DEU", "titre": "Bund", "banque": "Bundesbank"},
    {"code": "fr", "nom": "France", "devise": "EUR", "iso2": "FR", "iso3": "FRA", "titre": "OAT", "banque": "Banque de France"},
    {"code": "it", "nom": "Italie", "devise": "EUR", "iso2": "IT", "iso3": "ITA", "titre": "BTP", "banque": "BCE"},
    {"code": "es", "nom": "Espagne", "devise": "EUR", "iso2": "ES", "iso3": "ESP", "titre": "Bonos", "banque": "Banco de España"},
    {"code": "gb", "nom": "Royaume-Uni", "devise": "GBP", "iso2": "GB", "iso3": "GBR", "titre": "Gilts", "banque": "Bank of England"},
    {"code": "jp", "nom": "Japon", "devise": "JPY", "iso2": "JP", "iso3": "JPN", "titre": "JGB", "banque": "ministère des Finances"},
    {"code": "ca", "nom": "Canada", "devise": "CAD", "iso2": "CA", "iso3": "CAN", "titre": "Obligations du Canada", "banque": "Banque du Canada"},
    {"code": "au", "nom": "Australie", "devise": "AUD", "iso2": "AU", "iso3": "AUS", "titre": "ACGB", "banque": "Reserve Bank of Australia"},
    {"code": "ch", "nom": "Suisse", "devise": "CHF", "iso2": "CH", "iso3": "CHE", "titre": "Confédération", "banque": "Banque nationale suisse"},
    {"code": "nl", "nom": "Pays-Bas", "devise": "EUR", "iso2": "NL", "iso3": "NLD", "titre": "DSL", "banque": "BCE"},
    {"code": "be", "nom": "Belgique", "devise": "EUR", "iso2": "BE", "iso3": "BEL", "titre": "OLO", "banque": "BCE"},
    {"code": "at", "nom": "Autriche", "devise": "EUR", "iso2": "AT", "iso3": "AUT", "titre": "RAGB", "banque": "BCE"},
    {"code": "ie", "nom": "Irlande", "devise": "EUR", "iso2": "IE", "iso3": "IRL", "titre": "IGB", "banque": "BCE"},
    {"code": "pt", "nom": "Portugal", "devise": "EUR", "iso2": "PT", "iso3": "PRT", "titre": "OT", "banque": "BCE"},
    {"code": "gr", "nom": "Grèce", "devise": "EUR", "iso2": "GR", "iso3": "GRC", "titre": "GGB", "banque": "BCE"},
    {"code": "cn", "nom": "Chine", "devise": "CNY", "iso2": "CN", "iso3": "CHN", "titre": "CGB", "banque": "OCDE"},
    {"code": "in", "nom": "Inde", "devise": "INR", "iso2": "IN", "iso3": "IND", "titre": "G-Sec", "banque": "OCDE"},
    {"code": "ez", "nom": "Zone euro", "devise": "EUR", "iso2": "U2", "iso3": None, "titre": "courbe de la zone euro", "banque": "BCE"},
]
FICHES = (2, 5, 10, 30)   # les échéances qui ont une fiche, quand elles existent


def _f(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _trier(pts):
    d = {}
    for a, v in pts:
        if a and estnb(v):
            d[a[:10]] = v
    return sorted(d.items())


# ══ SOURCES ══════════════════════════════════════════════════════════════════
def src_us():
    ids = {0.083: "DGS1MO", 0.25: "DGS3MO", 0.5: "DGS6MO", 1: "DGS1", 2: "DGS2", 3: "DGS3", 5: "DGS5",
           7: "DGS7", 10: "DGS10", 20: "DGS20", 30: "DGS30"}
    out = {m: fred(s) for m, s in ids.items()}
    # Le Trésor publie le jour même, FRED le lendemain : l'année en cours du
    # Trésor complète FRED (colonnes lues par leur NOM, elles changent d'une année à l'autre).
    an = date.today().year
    for a in (an - 1, an):
        t = get_txt("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                    "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve&field_tdr_date_value=%d&page&_format=csv" % (a, a))
        if not t:
            continue
        cols = {"1 Mo": 0.083, "3 Mo": 0.25, "6 Mo": 0.5, "1 Yr": 1, "2 Yr": 2, "3 Yr": 3, "5 Yr": 5, "7 Yr": 7,
                "10 Yr": 10, "20 Yr": 20, "30 Yr": 30}
        for r in csv.DictReader(io.StringIO(t)):
            try:
                d = datetime.strptime(r["Date"], "%m/%d/%Y").date().isoformat()
            except (KeyError, ValueError):
                continue
            for c, m in cols.items():
                v = _f(r.get(c))
                if v is not None:
                    out[m].append((d, v))
    out = {m: _trier(p) for m, p in out.items() if p}
    reel = {5: fred("DFII5"), 10: fred("DFII10"), 30: fred("DFII30")}
    pm = {5: fred("T5YIE"), 10: fred("T10YIE")}
    return {"series": out, "famille": "rendements « par » à échéance constante (Trésor américain)",
            "source": "Trésor américain (courbe par) et FRED", "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
            "reel": {m: p for m, p in reel.items() if p}, "point_mort": {m: p for m, p in pm.items() if p},
            "reel_source": "Trésor américain, obligations indexées (TIPS), via FRED"}


def _bbk(flux, cle):
    t = get_txt("https://api.statistiken.bundesbank.de/rest/data/%s/%s" % (flux, cle), entetes=BBK_H, timeout=150)
    if not t:
        return {}
    out = {}
    for r in csv.DictReader(io.StringIO(t.lstrip("﻿")), delimiter=";"):
        v = _f(r.get("OBS_VALUE"))
        if v is None:
            continue
        k = r.get("BBK_STD_MAT") or r.get("BBK_STD_RESTLZ") or ""
        m = re.search(r"R(\d{2})XX", r.get("KEY", "") or "") or re.search(r"R(\d{2})XX", k)
        cle_m = int(m.group(1)) if m else 0
        out.setdefault(cle_m, []).append((r["TIME_PERIOD"], v))
    return {k: _trier(v) for k, v in out.items()}


def src_de():
    mats = [1, 2, 3, 5, 7, 10, 15, 20, 30]
    s = {}
    for m in mats:
        r = _bbk("BBSIS", "D.I.ZAR.ZI.EUR.S1311.B.A604.R%02dXX.R.A.A._Z._Z.A" % m)
        p = r.get(m) or (list(r.values())[0] if r else [])
        if p:
            s[m] = p
    lm = _bbk("BBSIS", "M.I.ZAR.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A")
    long10 = lm.get(10) or (list(lm.values())[0] if lm else [])
    return {"series": s, "famille": "rendements « par » tirés de la courbe des taux (Bundesbank, lissée)",
            "source": "Bundesbank (structure par terme)", "source_url": "https://www.bundesbank.de/en/statistics/money-and-capital-markets/interest-rates-and-yields",
            "long": {10: [(d + "-15" if len(d) == 7 else d, v) for d, v in long10]}, "long_source": "Bundesbank, mensuel depuis 1972"}


def _irs(pays):
    t = get_txt("https://data-api.ecb.europa.eu/service/data/IRS/M.%s.L.L40.CI.0000.EUR.N.Z?format=csvdata" % pays.upper(),
                entetes={"Accept": "text/csv"})
    if not t:
        return []
    return [(r["TIME_PERIOD"] + "-15", _f(r["OBS_VALUE"])) for r in csv.DictReader(io.StringIO(t)) if _f(r.get("OBS_VALUE")) is not None]


_NBB = {}


def _nbb(pays):
    if not _NBB:
        t = get_txt("https://nsidisseminate-stat.nbb.be/rest/data/BE2,DF_IROLOYLD,1.0/D.FR+IT+BE+NL?dimensionAtObservation=AllDimensions",
                    entetes=BBK_H, timeout=150)
        if t:
            for r in csv.DictReader(io.StringIO(t.lstrip("﻿"))):
                v = _f(r.get("OBS_VALUE"))
                if v is not None:
                    _NBB.setdefault(r.get("IROLOYLD_AREA"), []).append((r["TIME_PERIOD"], v))
        _NBB["_ok"] = True
    return _trier(_NBB.get(pays.upper(), []))


def _cle_webstat():
    k = os.environ.get("WEBSTAT_API_KEY")
    if k:
        return k.strip()
    f = os.path.expanduser("~/.webstat_key")
    return open(f).read().strip() if os.path.isfile(f) else None


def _webstat_tec(mats):
    """Historique QUOTIDIEN des TEC 1 à 30 ans (Banque de France, depuis 2004),
    en un export. Exige la clé gratuite Webstat (en-tête « Apikey »)."""
    cle = _cle_webstat()
    if not cle:
        return None
    cles = ['FM.D.FR.EUR.FR2.BB.FRMOYTEC%d.HSTA' % m for m in mats]
    u = ("https://webstat.banque-france.fr/api/explore/v2.1/catalog/datasets/observations/exports/json?" +
         urllib.parse.urlencode({"where": "series_key in (" + ",".join('"%s"' % k for k in cles) + ")",
                                 "select": "series_key,time_period,obs_value"}))
    d = get_json(u, accept="application/json", timeout=300, entetes={"Authorization": "Apikey " + cle})
    if not isinstance(d, list) or not d:
        return None
    out = {}
    for o in d:
        m = re.search(r"FRMOYTEC(\d+)\.", o.get("series_key") or "")
        v = o.get("obs_value")
        if m and isinstance(v, (int, float)):
            out.setdefault(int(m.group(1)), []).append((o["time_period"][:10], float(v)))
    return {m: _trier(p) for m, p in out.items()}


def _decoder(serie):
    """Série compacte d'un fichier précédent ({d0, dj, v} ou {d, v}) → [(date, v)]."""
    if not serie:
        return []
    if "d0" in serie:
        o, out = date.fromisoformat(serie["d0"]).toordinal(), []
        for dj, v in zip(serie.get("dj", []), serie.get("v", [])):
            o += dj
            out.append((date.fromordinal(o).isoformat(), v))
        return out
    return list(zip(serie.get("d", []), serie.get("v", [])))


def src_fr(precedent):
    """TEC 1 à 30 ans de la Banque de France.
    AVEC la clé Webstat : tout l'historique quotidien depuis 2004, en un export.
    SANS clé : les deux dernières valeurs du catalogue public, ajoutées à la série
    du passage précédent — jamais reconstruite à partir de rien, sans quoi un
    passage sans clé effacerait vingt ans d'histoire."""
    mats = (1, 2, 3, 5, 7, 10, 15, 20, 25, 30)
    precedent = precedent or {}
    wb = None
    try:
        wb = _webstat_tec(mats)
    except Exception as e:  # noqa: BLE001
        log("[warn] Webstat : " + str(e)[:120])
    ids = " or ".join('dataset_id="fm-d-fr-eur-fr2-bb-frmoytec%d-hsta"' % m for m in mats)
    j = get_json("https://webstat.banque-france.fr/api/explore/v2.1/catalog/datasets?" +
                 urllib.parse.urlencode({"limit": 50, "where": ids}), accept="application/json")
    nouveaux = {}
    for x in (j or {}).get("results", []):
        c = (x.get("metas") or {}).get("custom") or {}
        m = re.search(r"tec(\d+)", x.get("dataset_id", ""))
        if not m:
            continue
        vals = [_f(v) for v in (c.get("series_last_two_obs_values") or "").split(",")]
        d_der = (c.get("series_last_time_period_date") or "")[:10]
        if vals and vals[-1] is not None and d_der:
            nouveaux[int(m.group(1))] = {"der": (d_der, vals[-1]), "avant": vals[-2] if len(vals) > 1 else None}
    acc_prec = precedent.get("accumule") or {}
    s = {}
    for m in mats:
        base = []
        if wb and wb.get(m):
            base = list(wb[m])
        else:
            base = _decoder((((precedent.get("maturites") or {}).get(str(m))) or {}).get("serie"))
            base += [tuple(p) for p in (acc_prec.get(str(m)) or [])]
        n = nouveaux.get(m)
        if n:
            base.append(n["der"])
        s[m] = _trier(base)
    # Avant 2004 (le 10 ans) : le quotidien de la Banque nationale de Belgique, depuis 1993.
    hist = _nbb("FR")
    if hist and s.get(10):
        premier = s[10][0][0]
        s[10] = _trier([p for p in hist if p[0] < premier] + s[10])
    avec_cle = bool(wb)
    return {"series": {m: p for m, p in s.items() if p}, "famille": "taux à échéance constante (TEC), interpolés entre deux OAT",
            "source": "Banque de France (TEC)", "source_url": "https://webstat.banque-france.fr/",
            # les 120 derniers jours de chaque échéance : de quoi reprendre sans clé
            "accumule": {str(m): [list(x) for x in p if x[0] >= il_y_a(p[-1][0], 120)] for m, p in s.items() if p},
            "avant_dernier": {m: n["avant"] for m, n in nouveaux.items() if n.get("avant") is not None},
            "long": {10: fred("IRLTLT01FRM156N")}, "long_source": "OCDE via FRED, mensuel depuis 1960",
            "webstat": avec_cle,
            "note": ("Historique quotidien : Banque de France (TEC) depuis 2004 ; avant, le 10 ans de référence relevé par la Banque nationale de Belgique (depuis 1993)."
                     if avec_cle else
                     "Historique quotidien du 10 ans : Banque nationale de Belgique (publié avec environ trois mois de retard), puis relevés quotidiens de la Banque de France.")}


def src_mensuel_ue(code, nbb=False):
    irs = _irs(code)
    s = {10: irs}
    hist = _nbb(code) if nbb else []
    return {"series": {10: irs} if irs else {}, "mensuel": True,
            "famille": "taux long de référence 10 ans (moyenne du mois, critère de convergence)",
            "source": "BCE (taux longs de convergence)", "source_url": "https://data.ecb.europa.eu/data/datasets/IRS",
            "quotidien_historique": {10: hist} if hist else {},
            "long": {10: fred("IRLTLT01%sM156N" % code.upper())}, "long_source": "OCDE via FRED, mensuel"}


def src_es():
    b = get("https://www.bde.es/webbe/es/estadisticas/compartido/datos/csv/ti_1_3.csv", timeout=150)
    if not b:
        return None
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        s = b.decode("latin-1")
    rows = list(csv.reader(io.StringIO(s)))
    codes = rows[0] if rows else []
    mois = dict(ENE=1, FEB=2, MAR=3, ABR=4, MAY=5, JUN=6, JUL=7, AGO=8, SEP=9, OCT=10, NOV=11, DIC=12)
    noms = {"D_DTES00B7": 0.25, "D_DTES00S7": 0.5, "D_DTES00U7": 1, "D_G0B1F0ZN": 3, "D_G0B1F0ZO": 5,
            "D_G0B1F0ZP": 10, "D_G0B1F0ZQ": 15}
    out = {m: [] for m in noms.values()}
    for r in rows:
        mm = re.match(r"(\d{1,2}) ([A-Z]{3}) ?(\d{4})$", r[0].strip()) if r else None
        if not mm or mm.group(2) not in mois:
            continue
        d = date(int(mm.group(3)), mois[mm.group(2)], int(mm.group(1))).isoformat()
        for j, c in enumerate(codes):
            if c in noms and j < len(r):
                v = _f(r[j])
                if v is not None:
                    out[noms[c]].append((d, v))
    return {"series": {m: _trier(p) for m, p in out.items() if p},
            "famille": "rendements du marché secondaire, titres de référence (Banco de España)",
            "source": "Banco de España", "source_url": "https://www.bde.es/webbe/es/estadisticas/temas/tipos-interes.html",
            "long": {10: fred("IRLTLT01ESM156N")}, "long_source": "OCDE via FRED, mensuel depuis 1980",
            "absent": {30: "La Banco de España ne publie pas de 30 ans."}}


def src_gb():
    codes = {"IUDSNZC": 5, "IUDMNZC": 10, "IUDLNZC": 20, "IUDMRZC": "r10", "IUDMIZC": "i10", "IUDSRZC": "r5", "IUDSIZC": "i5"}
    u = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes"
         "&Datefrom=04/Jan/1982&Dateto=now&SeriesCodes=%s&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N" % ",".join(codes))
    t = get_txt(u, timeout=150)
    if not t or not t.startswith("DATE"):
        return None
    s, reel, pm = {}, {}, {}
    for r in csv.DictReader(io.StringIO(t)):
        try:
            d = datetime.strptime(r.pop("DATE"), "%d %b %Y").date().isoformat()
        except (ValueError, KeyError):
            continue
        for c, m in codes.items():
            v = _f(r.get(c))
            if v is None:
                continue
            if isinstance(m, int):
                s.setdefault(m, []).append((d, v))
            elif m.startswith("r"):
                reel.setdefault(int(m[1:]), []).append((d, v))
            else:
                pm.setdefault(int(m[1:]), []).append((d, v))
    return {"series": {m: _trier(p) for m, p in s.items()}, "famille": "taux zéro-coupon tirés de la courbe des gilts (Bank of England, modélisée)",
            "source": "Bank of England (courbe des gilts)", "source_url": "https://www.bankofengland.co.uk/statistics/yield-curves",
            "reel": {m: _trier(p) for m, p in reel.items()}, "point_mort": {m: _trier(p) for m, p in pm.items()},
            "reel_source": "Bank of England, gilts indexés (zéro-coupon réel)",
            "long": {10: fred("IRLTLT01GBM156N")}, "long_source": "OCDE via FRED, mensuel depuis 1960",
            "absent": {2: "La Bank of England ne publie en série que les échéances 5, 10 et 20 ans.",
                       30: "La Bank of England ne publie en série que les échéances 5, 10 et 20 ans."}}


def src_jp():
    out = {}
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    for u in (base + "historical/jgbcme_all.csv", base + "jgbcme.csv"):
        b = get(u, timeout=150)
        if not b:
            continue
        rows = list(csv.reader(io.StringIO(b.decode("utf-8", "replace"))))
        if len(rows) < 3:
            continue
        hdr = rows[1]
        for r in rows[2:]:
            if not r or not re.match(r"\d{4}/\d{1,2}/\d{1,2}$", r[0]):
                continue
            d = datetime.strptime(r[0], "%Y/%m/%d").date().isoformat()
            for h, v in zip(hdr[1:], r[1:]):
                mm = re.match(r"(\d+)Y", h.strip())
                x = _f(v)
                if mm and x is not None:
                    out.setdefault(int(mm.group(1)), []).append((d, x))
    return {"series": {m: _trier(p) for m, p in out.items() if m in (1, 2, 3, 5, 7, 10, 15, 20, 30, 40)},
            "famille": "taux à échéance constante (ministère des Finances)", "source": "Ministère des Finances du Japon",
            "source_url": "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"}


def src_ca():
    t = get_txt("https://www.bankofcanada.ca/valet/observations/group/bond_yields_benchmark/csv?start_date=2001-01-01", timeout=150)
    if not t:
        return None
    i = t.find('"date"')
    if i < 0:
        return None
    cols = {"BD.CDN.2YR.DQ.YLD": 2, "BD.CDN.3YR.DQ.YLD": 3, "BD.CDN.5YR.DQ.YLD": 5, "BD.CDN.7YR.DQ.YLD": 7,
            "BD.CDN.10YR.DQ.YLD": 10, "BD.CDN.LONG.DQ.YLD": 30, "BD.CDN.RRB.DQ.YLD": "r"}
    s, reel = {}, []
    for r in csv.DictReader(io.StringIO(t[i:])):
        d = r.get("date")
        for c, m in cols.items():
            v = _f(r.get(c))
            if v is None or not d:
                continue
            if m == "r":
                reel.append((d, v))
            else:
                s.setdefault(m, []).append((d, v))
    return {"series": {m: _trier(p) for m, p in s.items()}, "famille": "rendements des titres de référence (Banque du Canada) ; « 30 ans » = référence long terme",
            "source": "Banque du Canada", "source_url": "https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/",
            "reel": {30: _trier(reel)} if reel else {}, "reel_source": "obligations à rendement réel (Banque du Canada)",
            "long": {10: fred("IRLTLT01CAM156N")}, "long_source": "OCDE via FRED, mensuel depuis 1955"}


def src_au():
    b = get("https://www.rba.gov.au/statistics/tables/csv/f2-data.csv", ua=None, timeout=150)   # ⚠ RBA : l'agent par défaut passe, un agent de projet non
    if not b:
        return None
    rows = list(csv.reader(io.StringIO(b.decode("utf-8-sig", "replace"))))
    ids = next((r for r in rows if r and r[0] == "Series ID"), None)
    if not ids:
        return None
    lab = {"FCMYGBAG2D": 2, "FCMYGBAG3D": 3, "FCMYGBAG5D": 5, "FCMYGBAG10D": 10, "FCMYGBAGID": "r"}
    s, reel = {}, []
    for r in rows:
        if not r or not re.match(r"\d{2}-[A-Z][a-z]{2}-\d{4}$", r[0]):
            continue
        d = datetime.strptime(r[0], "%d-%b-%Y").date().isoformat()
        for i, v in zip(ids[1:], r[1:]):
            x = _f(v)
            if i in lab and x is not None:
                if lab[i] == "r":
                    reel.append((d, x))
                else:
                    s.setdefault(lab[i], []).append((d, x))
    return {"series": {m: _trier(p) for m, p in s.items()}, "famille": "taux à échéance constante, interpolés (RBA)",
            "source": "Reserve Bank of Australia", "source_url": "https://www.rba.gov.au/statistics/tables/",
            "reel": {10: _trier(reel)} if reel else {}, "reel_source": "obligations indexées (RBA)",
            "long": {10: fred("IRLTLT01AUM156N")}, "long_source": "OCDE via FRED, mensuel depuis 1969",
            "note": "La RBA met sa série à jour environ une fois par semaine."}


def src_ch(precedent):
    t = get_txt("https://www.snb.ch/public/rss/en/interestRates")
    acc = [tuple(p) for p in ((precedent or {}).get("10") or [])]
    if t:
        for it in re.findall(r"<item>(.*?)</item>", t, re.S):
            if re.search(r"<title>CH: [-\d.]+ R10 ", it):
                v = re.search(r"<cb:value[^>]*>([-\d.]+)</cb:value>", it)
                p = re.search(r"<cb:period>([\d-]+)</cb:period>", it)
                if v and p:
                    acc.append((p.group(1)[:10], float(v.group(1))))
    acc = _trier(acc)
    # Histoire : cube BNS (figé au 31/07/2025), puis la série accumulée.
    hist = []
    c = get_txt("https://data.snb.ch/api/cube/rendoblid/data/csv/en", timeout=150)
    if c:
        lignes = c.splitlines()
        k = next((i for i, l in enumerate(lignes) if l.startswith('"Date"') or l.startswith("Date")), None)
        if k is not None:
            for r in csv.DictReader(io.StringIO("\n".join(lignes[k:])), delimiter=";"):
                if (r.get("D0") or "").strip('"') in ("10J", "10Y"):
                    v = _f(r.get("Value"))
                    if v is not None:
                        hist.append((r["Date"][:10], v))
    premier = acc[0][0] if acc else "9999"
    s10 = _trier([p for p in hist if p[0] < premier] + acc)
    return {"series": {10: s10} if s10 else {}, "famille": "rendement de la Confédération à 10 ans (BNS)",
            "source": "Banque nationale suisse", "source_url": "https://data.snb.ch/",
            "accumule": {"10": [list(p) for p in acc]},
            "long": {10: fred("IRLTLT01CHM156N")}, "long_source": "OCDE via FRED, mensuel depuis 1955",
            "note": "La BNS ne publie en flux que les cinq derniers jours ; entre le 31/07/2025 (fin de son historique) et le début des relevés, la série quotidienne est vide."}


def src_ez():
    s, aaa = {}, {}
    mats = {"3M": 0.25, "1Y": 1, "2Y": 2, "3Y": 3, "5Y": 5, "7Y": 7, "10Y": 10, "15Y": 15, "20Y": 20, "30Y": 30}
    for courbe, cible in (("G_N_C", s), ("G_N_A", aaa)):
        t = get_txt("https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.%s.SV_C_YM.%s?format=csvdata&detail=dataonly"
                    % (courbe, "+".join("PY_" + k for k in mats)), entetes={"Accept": "text/csv"}, timeout=180)
        if not t:
            continue
        for r in csv.DictReader(io.StringIO(t)):
            k = (r.get("DATA_TYPE_FM") or "").replace("PY_", "")
            v = _f(r.get("OBS_VALUE"))
            if k in mats and v is not None:
                cible.setdefault(mats[k], []).append((r["TIME_PERIOD"], v))
    return {"series": {m: _trier(p) for m, p in s.items()}, "famille": "courbe modélisée de la BCE, toutes émissions d'États de la zone euro (rendements « par »)",
            "source": "BCE (courbes des taux de la zone euro)", "source_url": "https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_area_yield_curves/html/index.en.html",
            "aaa": {m: _trier(p) for m, p in aaa.items()}}


def src_oecd(iso3):
    t = get_txt("https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,/%s.M.IRLT.......?" % iso3 +
                urllib.parse.urlencode({"startPeriod": "1990-01", "dimensionAtObservation": "AllDimensions", "format": "csvfile"}), timeout=120)
    if not t:
        return None
    p = [(r["TIME_PERIOD"] + "-15", _f(r["OBS_VALUE"])) for r in csv.DictReader(io.StringIO(t)) if _f(r.get("OBS_VALUE")) is not None]
    return {"series": {10: _trier(p)}, "mensuel": True, "famille": "taux long 10 ans (moyenne du mois, OCDE)",
            "source": "OCDE (statistiques financières)", "source_url": "https://data-explorer.oecd.org/"}


# ══ CONSTRUCTION ══════════════════════════════════════════════════════════════
def lib_mat(m):
    if m < 1:
        return "%d mois" % round(m * 12)
    return "%d an%s" % (m, "s" if m > 1 else "")


def courbe_a(series, iso, tol_jours=10):
    """Points (échéance, taux) à la date la plus proche AU PLUS TARD de `iso`."""
    pts = []
    for m, p in series.items():
        if not p:
            continue
        v = valeur_au(p, iso)
        dd = None
        for d0, _ in p:
            if d0 <= iso:
                dd = d0
            else:
                break
        if v is not None and dd and (date.fromisoformat(iso) - date.fromisoformat(dd)).days <= tol_jours:
            pts.append([m, round(v, 3)])
    return sorted(pts)


def moyennes_mensuelles(pts):
    """Moyenne de chaque mois, datée du 15 (comme les séries mensuelles de la BCE)."""
    acc = {}
    for d, v in pts:
        acc.setdefault(d[:7], []).append(v)
    return [(k + "-15", sum(x) / len(x)) for k, x in sorted(acc.items())]


def ecart_series(a, b):
    """Écart a − b (pb) aux dates communes."""
    db = dict(b)
    return [(d, round((v - db[d]) * 100, 1)) for d, v in a if d in db]


def construire(journal, precedent=None):
    """Rend (synthèse par pays, détail {code: {...}}, fiches [])."""
    precedent = precedent or {}
    brut = {}
    for p in PAYS:
        c = p["code"]
        try:
            if c == "us":
                r = src_us()
            elif c == "de":
                r = src_de()
            elif c == "fr":
                r = src_fr(precedent.get("fr"))
            elif c == "es":
                r = src_es()
            elif c == "gb":
                r = src_gb()
            elif c == "jp":
                r = src_jp()
            elif c == "ca":
                r = src_ca()
            elif c == "au":
                r = src_au()
            elif c == "ch":
                r = src_ch((precedent.get("ch") or {}).get("accumule"))
            elif c == "ez":
                r = src_ez()
            elif c in ("cn", "in"):
                r = src_oecd(p["iso3"])
            else:
                r = src_mensuel_ue(c, nbb=c in ("it", "nl", "be"))
        except Exception as e:  # noqa: BLE001
            journal.append("%s : %s" % (c, str(e)[:140]))
            r = None
        if not r or not r.get("series"):
            # Reprise : la fiche précédente, marquée comme telle.
            anc = precedent.get(c)
            if anc and anc.get("_synth"):
                journal.append("%s : source vide, reprise du passage précédent" % c)
                brut[c] = {"reprise": anc}
            else:
                journal.append("%s : aucune donnée" % c)
            continue
        brut[c] = r

    # La référence de chaque pays : le Bund pour la zone euro, le Trésor ailleurs.
    de, us = (brut.get("de") or {}).get("series", {}), (brut.get("us") or {}).get("series", {})
    synth, detail, fiches = [], {}, []
    for p in PAYS:
        c = p["code"]
        r = brut.get(c)
        if not r:
            continue
        if "reprise" in r:
            anc = r["reprise"]
            s = dict(anc["_synth"], reprise_du=anc["_synth"].get("reprise_du") or (anc.get("genere_le") or "")[:10])
            synth.append(s)
            detail[c] = dict(anc, _synth=s)
            fiches.extend(anc.get("_fiches") or [])
            continue
        S = r["series"]
        ref_code = "de" if ((c in ZONE_EURO or c == "ez") and c != "de") else ("us" if c != "us" else "de")
        ref = de if ref_code == "de" else us
        if r.get("mensuel"):
            # ⚠ Un pays MENSUEL (moyenne du mois) se compare à la MOYENNE du mois
            #   de la référence, jamais à son cours d'un seul jour.
            ref = {m: moyennes_mensuelles(p) for m, p in ref.items()}
        mats = sorted(S)
        d_det = {"code": c, "nom": p["nom"], "devise": p["devise"], "famille": r.get("famille"), "source": r.get("source"),
                 "source_url": r.get("source_url"), "mensuel": bool(r.get("mensuel")), "note": r.get("note"),
                 "absent": {str(k): v for k, v in (r.get("absent") or {}).items()}, "maturites": {}}
        s_mats = {}
        for m in mats:
            pts = S[m]
            if not pts:
                continue
            der_d, der_v = pts[-1]
            info = {"taux": round(der_v, 3), "date": der_d, "var": variations(pts, 100, bool(r.get("mensuel"))), "depuis": pts[0][0],
                    "centile_10a": centile(pts, 3652), "centile_tout": centile(pts), "ext_10a": extremes(pts, 3652),
                    "ext_tout": extremes(pts)}
            if r.get("mensuel"):
                info["mensuel"] = True
            # écart à la référence, même échéance
            if ref.get(m) and c != ref_code:
                ec = ecart_series(pts, ref[m])
                if ec:
                    info["ecart_ref_pb"] = ec[-1][1]
                    info["ecart_ref_date"] = ec[-1][0]
                    info["ecart_var"] = variations([(d0, v / 100) for d0, v in ec], 100, bool(r.get("mensuel")))
                    info["ecart_centile_10a"] = centile(ec, 3652)
                    d_det.setdefault("ecarts", {})[str(m)] = en_colonnes(compacter(ec, 800), 1)
            # La série entière seulement pour les échéances qui ont une fiche : les
            # autres ne servent qu'aux courbes, déjà calculées ici.
            d_det["maturites"][str(m)] = dict(info, serie=en_colonnes(compacter(pts, 800), 3)) if m in FICHES else info
            s_mats[str(m)] = {k: info[k] for k in ("taux", "date", "var", "ecart_ref_pb", "centile_10a", "mensuel") if k in info}
        # historique long (mensuel) du 10 ans
        lg = (r.get("long") or {}).get(10)
        if lg:
            d_det["long10"] = dict(en_colonnes(mensuel(lg), 3), source=r.get("long_source"))
        qh = (r.get("quotidien_historique") or {}).get(10)
        if qh:
            d_det["quotidien_historique10"] = dict(en_colonnes(compacter(qh, 800), 3), source="Banque nationale de Belgique (10 ans de référence, quotidien, publié avec environ trois mois de retard)")
        # la courbe : aujourd'hui, il y a un mois, il y a un an, il y a 5 ans
        if len(mats) >= 3 and not r.get("mensuel"):
            der = max(S[m][-1][0] for m in mats if S[m])
            cb = {}
            for cle, j in (("auj", 0), ("1m", 30), ("1a", 365), ("5a", 1826)):
                iso = il_y_a(der, j) if j else der
                cb[cle] = {"date": iso, "points": courbe_a(S, iso)}
            d_det["courbe"] = cb
            if c == "ez" and r.get("aaa"):
                d_det["courbe_aaa"] = {"date": der, "points": courbe_a(r["aaa"], der)}
        for k in ("reel", "point_mort"):
            if r.get(k):
                d_det[k] = {str(m): dict(en_colonnes(compacter(v, 800), 3), taux=round(v[-1][1], 3), date=v[-1][0], var=variations(v, 100))
                            for m, v in r[k].items() if v}
        if r.get("reel_source"):
            d_det["reel_source"] = r["reel_source"]
        if r.get("accumule"):
            d_det["accumule"] = r["accumule"]
        # pentes
        def tx(m):
            return S[m][-1][1] if S.get(m) else None
        pente = {}
        if tx(10) is not None and tx(2) is not None:
            p210 = [(d0, v) for d0, v in ecart_series(S[10], S[2])]
            pente["10_2_pb"] = round((tx(10) - tx(2)) * 100)
            pente["10_2_var"] = variations([(d0, v / 100) for d0, v in p210], 100)
            pente["10_2_centile"] = centile(p210, 3652)
            d_det["pente_10_2"] = en_colonnes(compacter(p210, 800), 1)
        if tx(30) is not None and tx(10) is not None:
            pente["30_10_pb"] = round((tx(30) - tx(10)) * 100)
        cm = min(mats) if mats else None
        if cm is not None and cm < 1 and tx(10) is not None:
            pente["10_court_pb"] = round((tx(10) - tx(cm)) * 100)
            pente["court"] = lib_mat(cm)
        d_det["pente"] = pente
        s = {"code": c, "nom": p["nom"], "devise": p["devise"], "titre": p["titre"], "iso2": p["iso2"], "iso3": p["iso3"],
             "zone_euro": c in ZONE_EURO, "reference": ref_code if c != ref_code else None, "source": r.get("source"),
             "banque": p["banque"], "mensuel": bool(r.get("mensuel")), "maturites": s_mats, "pente": pente,
             "famille": r.get("famille")}
        if d_det.get("reel"):
            s["reel10"] = (d_det["reel"].get("10") or {}).get("taux")
        if d_det.get("point_mort"):
            s["pm10"] = (d_det["point_mort"].get("10") or {}).get("taux")
        synth.append(s)
        d_det["_synth"] = s
        f_pays = []
        for m in FICHES:
            if str(m) in s_mats and c != "ez":
                f = {"code": "%s-%d" % (c, m), "pays": c, "maturite": m, "nom": "%s %d ans" % (p["nom"], m),
                     "taux": s_mats[str(m)]["taux"], "date": s_mats[str(m)]["date"],
                     "var_1m_pb": s_mats[str(m)]["var"].get("1m"), "var_1a_pb": s_mats[str(m)]["var"].get("1a"),
                     "ecart_ref_pb": s_mats[str(m)].get("ecart_ref_pb"), "mensuel": bool(r.get("mensuel"))}
                f_pays.append(f)
        if c == "ez":
            for m in (2, 10, 30):
                if str(m) in s_mats:
                    f_pays.append({"code": "ez-%d" % m, "pays": "ez", "maturite": m, "nom": "Zone euro %d ans" % m,
                                   "taux": s_mats[str(m)]["taux"], "date": s_mats[str(m)]["date"],
                                   "var_1m_pb": s_mats[str(m)]["var"].get("1m"), "var_1a_pb": s_mats[str(m)]["var"].get("1a")})
        fiches.extend(f_pays)
        d_det["_fiches"] = f_pays
        detail[c] = d_det
    return synth, detail, fiches


if __name__ == "__main__":
    import sys, time
    t0 = time.time()
    j = []
    s, d, f = construire(j)
    for x in s:
        m = x["maturites"]
        print(x["code"], {k: (v["taux"], v["date"], v.get("ecart_ref_pb")) for k, v in m.items() if k in ("2", "10", "30")}, x.get("pente"))
    print(len(f), "fiches ;", round(time.time() - t0), "s ; journal", j)
    json.dump({k: {kk: vv for kk, vv in v.items()} for k, v in d.items()}, open("sortie/_test_souverains.json", "w"))
