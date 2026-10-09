#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QUI ACHÈTE LA DETTE, QUI LA VEND (module de fetch_obligations.py).

Une fiche d'État disait QUI DÉTIENT sa dette, en quatre grandes classes. Elle
dit maintenant, à partir de sources officielles et ouvertes :
  zone_euro()   pour les 10 États de la zone euro : qui détient sa dette, secteur
                par secteur (Eurosystème, banques, assureurs, fonds…), ET ce que
                chaque secteur a ACHETÉ ou VENDU net chaque trimestre depuis 2021
                — BCE, statistiques de détention de titres par secteur (SHSS).
                Le reste du monde (« hors zone euro ») se lit PAR DIFFÉRENCE avec
                l'encours et les émissions nettes de la BCE (GFS).
  france_bdf()  la France en plus : les titres de l'État, détenteurs résidents
                secteur par secteur et non-résidents, depuis 2008 (Banque de
                France, série DET2 — valeur de marché).
  etats_unis()  les États-Unis : détenteurs et achats nets par secteur (Fed,
                comptes financiers Z.1, hedge funds compris), la détention pays
                par pays (Trésor, TIC) et toutes les adjudications récentes avec
                la part prise par chaque catégorie de soumissionnaires.
  japon()       les adjudications du ministère des Finances japonais.
  entreprises_us()  qui détient les obligations d'entreprises aux États-Unis (Z.1).

⚠ SHSS couvre les détenteurs DE LA ZONE EURO seulement : le reste du monde est
  calculé (encours − détentions de la zone euro ; émissions nettes − achats de
  la zone euro). Il absorbe donc aussi les écarts de mesure : on l'écrit.
⚠ « Achats nets » = variation de la détention en VALEUR DE REMBOURSEMENT
  (valeur faciale), pour TOUS les détenteurs, reste du monde compris : une baisse
  des cours ne passe pas pour une vente, et la somme des barres retombe
  exactement sur la variation de la dette. (Mêler les transactions SHSS, au
  prix payé, et l'encours GFS, en valeur faciale, faussait le reste du monde de
  10 à 20 Md€ par trimestre pour la France : titres rachetés sous le pair.)
⚠ L'Eurosystème (S121) qui « vend » : il laisse surtout arriver à échéance
  sans réinvestir (fin des rachats) ; il ne vend presque rien sur le marché.
⚠ Z.1 : les MÉNAGES sont le secteur calculé PAR DIFFÉRENCE par la Fed (tout ce
  qui n'est pas attribué ailleurs) — on l'écrit sur le libellé.
⚠ TIC pays par pays : la garde des titres compte, pas le propriétaire (la
  Belgique, le Luxembourg, les îles Caïmans gardent pour d'autres) ; et la
  variation de la détention inclut l'effet des prix (valeur de marché).
⚠ Les adjudications FRANÇAISES ne sont pas lues : le site de l'Agence France
  Trésor oppose un contrôle anti-robot (Cloudflare) — on ne le contourne pas.
⚠ CDS (contrats d'assurance contre le défaut) : aucune source à la fois ouverte
  ET autorisée en collecte automatique (DTCC est payant ; LCH publie l'encours
  compensé par pays mais ses conditions interdisent les robots).
"""
import csv
import io
import json
import os
import re
import urllib.parse
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from oblig_net import get, get_txt, get_json, log, estnb

ECB = "https://data-api.ecb.europa.eu/service/data/"
UA_ECB = {"Accept": "text/csv"}
EURO = {"de": "DE", "fr": "FR", "it": "IT", "es": "ES", "nl": "NL", "be": "BE", "at": "AT", "ie": "IE", "pt": "PT", "gr": "GR"}

# Les secteurs détenteurs de la zone euro (SHSS), du plus « officiel » au plus « privé »
SECTEURS_ZE = [
    ("S121", "Eurosystème (BCE et banques centrales)"),
    ("S122", "Banques"),
    ("S128", "Assureurs"),
    ("S129", "Fonds de pension"),
    ("S124", "Fonds d'investissement"),
    ("S123", "Fonds monétaires"),
    ("S125A", "Autres sociétés financières"),
    ("S13", "Administrations publiques"),
    ("S11", "Entreprises"),
    ("S1M", "Ménages"),
]
HORS_ZE = ("hors_ze", "Investisseurs hors zone euro")

FRAIS_H = 12      # au-delà, on relit les sources (elles ne bougent qu'une fois par trimestre ou par mois)


def _age_h(bloc):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(bloc["genere_le"].replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return 1e9


def _csv(url, timeout=240):
    t = get_txt(url, accept="text/csv", timeout=timeout, entetes=UA_ECB)
    if not t or not t.lstrip().startswith(("KEY", '"KEY')):
        return []
    return list(csv.DictReader(io.StringIO(t)))


def _trim(m):
    """'2026-05' → '2026-Q2'."""
    a, mm = m.split("-")[:2]
    return "%s-Q%d" % (a, (int(mm) - 1) // 3 + 1)


def _r(x, n=1):
    return round(x, n) if estnb(x) else None


def _classement(t, achats, libelles, n_trim=1):
    """Les acheteurs et vendeurs nets sur les `n_trim` derniers trimestres COMPLETS
    (tous les secteurs connus, reste du monde compris)."""
    for i in range(len(t) - 1, n_trim - 2, -1):
        fen = range(i - n_trim + 1, i + 1)
        if all(estnb((achats.get(HORS_ZE[0]) or achats.get("_reste") or [None] * len(t))[j]) for j in fen) or \
                not any(k in achats for k in (HORS_ZE[0], "_reste")):
            lignes = []
            for k, s in achats.items():
                v = [s[j] for j in fen]
                if all(estnb(x) for x in v):
                    lignes.append([k, round(sum(v), 1)])
            lignes.sort(key=lambda x: -x[1])
            return {"de": t[i - n_trim + 1], "a": t[i], "lignes": lignes}
    return None


# ── 1. ZONE EURO : SHSS (détentions et achats par secteur) + GFS (encours, émissions) ──
def zone_euro(journal):
    pays = "+".join(EURO.values())
    inv = {v: k for k, v in EURO.items()}
    # SHSS : détenteurs de la zone euro (U2) des titres des administrations (S13) de chaque pays
    shss = _csv(ECB + "SHSS/Q.N.U2.%s..S13.N.A.LE.F3.T._Z.XDC._T.F.V.N._T?format=csvdata&detail=dataonly" % pays)
    if not shss:
        journal.append("SHSS (BCE) : vide")
        return {}
    det = defaultdict(lambda: defaultdict(dict))     # pays → (secteur, STO, VAL) → trimestre → M€
    for r in shss:
        c = inv.get(r.get("COUNTERPART_AREA"))
        try:
            v = float(r["OBS_VALUE"])
        except (TypeError, ValueError, KeyError):
            continue
        if c:
            det[c][(r["REF_SECTOR"], r["STO"], r["VALUATION"])][r["TIME_PERIOD"]] = v
    # GFS mensuel : encours (valeur faciale) et transactions (émissions nettes) des titres publics
    deb = "%d-12" % (date.today().year - 7)
    gfs = _csv(ECB + "GFS/M.N.%s.W0.S13.S1.N.L.LE.F3.T._Z.EUR._T.F.V.N._T?format=csvdata&detail=dataonly&startPeriod=%s" % (pays, deb))
    enc = defaultdict(dict)
    for r in gfs:
        c = inv.get(r.get("REF_AREA"))
        try:
            v = float(r["OBS_VALUE"])
        except (TypeError, ValueError, KeyError):
            continue
        if not c:
            continue
        m = r["TIME_PERIOD"]
        if r["STO"] == "LE" and m[5:7] in ("03", "06", "09", "12"):
            enc[c][_trim(m)] = v
    if not gfs:
        journal.append("GFS mensuel (BCE) : vide — reste du monde non calculé")
    out = {}
    for c, d in det.items():
        t = sorted(d.get(("S1", "LE", "F"), {}))
        if len(t) < 4:
            continue
        stock = {k: [_r(d.get((k, "LE", "F"), {}).get(q, None) / 1e3 if estnb(d.get((k, "LE", "F"), {}).get(q)) else None) for q in t]
                 for k, _ in SECTEURS_ZE}
        s1 = [d[("S1", "LE", "F")].get(q) for q in t]
        # ce que la liste des secteurs ne couvre pas (au-delà de 1 % : écrit à part)
        reste = []
        for j in range(len(t)):
            som = sum(stock[k][j] for k, _ in SECTEURS_ZE if estnb(stock[k][j]))
            reste.append(s1[j] / 1e3 - som if estnb(s1[j]) else None)
        # (toujours gardé quand il n'est pas nul : sans lui, la somme des barres ne
        #  retombait plus sur la variation de la dette — 2,7 Md€ d'écart pour la France)
        if any(estnb(x) and abs(x) >= 0.05 for x in reste):
            stock["_reste_ze"] = [_r(x) for x in reste]
        # le reste du monde, par différence avec l'encours total (valeur faciale, BCE GFS)
        total, hz_s = [], []
        for j, q in enumerate(t):
            e = enc[c].get(q)
            total.append(_r(e / 1e3) if estnb(e) else None)
            x = (e - s1[j]) / 1e3 if estnb(e) and estnb(s1[j]) else None
            hz_s.append(_r(x) if estnb(x) and 0 <= x <= e / 1e3 else None)
        stock[HORS_ZE[0]] = hz_s

        def delta(s):
            return [None] + [_r(s[j] - s[j - 1]) if estnb(s[j]) and estnb(s[j - 1]) else None for j in range(1, len(s))]
        achat = {k: delta(v) for k, v in stock.items()}
        emis = delta(total)
        stock = {k: v for k, v in stock.items() if any(estnb(x) for x in v)}
        achat = {k: v for k, v in achat.items() if any(estnb(x) for x in v)}
        libs = dict(SECTEURS_ZE + [HORS_ZE, ("_reste_ze", "Autres (zone euro)")])
        out[c] = {"t": t, "unite": "Md€", "encours": stock, "achats": achat, "encours_total": total, "emissions_nettes": emis,
                  "zone_euro_total": [_r(x / 1e3) if estnb(x) else None for x in s1],
                  "libelles": {k: libs[k] for k in set(stock) | set(achat)},
                  "trimestre": _classement(t, achat, libs, 1), "annee": _classement(t, achat, libs, 4),
                  "source": "BCE — détention de titres par secteur (SHSS) ; encours total : statistiques de finances publiques (GFS)",
                  "url": "https://data.ecb.europa.eu/data/datasets/SHSS",
                  "valeur": "valeur de remboursement ; achats nets = variation de la détention, hors effet des prix"}
    return out


# ── 2. FRANCE : les titres de l'État, qui les détient (Banque de France, DET2) ──
BDF_SECTEURS = [("S121", "Banque de France"), ("S122", "Banques"), ("S128", "Assureurs"), ("S129", "Fonds de pension"),
                ("S124", "OPC (fonds) non monétaires"), ("S123", "Fonds monétaires"), ("S125+S126+S127", "Autres sociétés financières"),
                ("S13", "Administrations publiques"), ("S11", "Entreprises"), ("S1M", "Ménages")]


def _cle_webstat():
    k = os.environ.get("WEBSTAT_API_KEY")
    if k:
        return k.strip()
    f = os.path.expanduser("~/.webstat_key")
    return open(f).read().strip() if os.path.isfile(f) else None


def france_bdf(journal):
    cle = _cle_webstat()
    if not cle:
        journal.append("Banque de France (DET2) : pas de clé Webstat")
        return None
    secs = ["S11", "S121", "S122", "S123", "S124", "S125", "S126", "S127", "S128", "S129", "S13", "S1M"]
    gab = "DET2.Q.N.FR.%s.S13111.%s.N.L.LE.F3.%s._Z.XDC._T.M.V.N._T"
    cles = [gab % ("W2", s, m) for s in secs for m in ("L", "S")] + [gab % (w, "S1", m) for w in ("W0", "W1") for m in ("L", "S")]
    u = ("https://webstat.banque-france.fr/api/explore/v2.1/catalog/datasets/observations/exports/json?" +
         urllib.parse.urlencode({"where": "series_key in (" + ",".join('"%s"' % k for k in cles) + ")",
                                 "select": "series_key,time_period,obs_value"}))
    d = get_json(u, accept="application/json", timeout=240, entetes={"Authorization": "Apikey " + cle})
    if not isinstance(d, list) or not d:
        journal.append("Banque de France (DET2) : vide")
        return None
    v = defaultdict(lambda: defaultdict(float))     # (zone, secteur) → trimestre → €
    for o in d:
        p = (o.get("series_key") or "").split(".")
        x = o.get("obs_value")
        if len(p) < 12 or not isinstance(x, (int, float)):
            continue
        v[(p[4], p[6])][(o.get("time_period") or "")[:7]] += x
    t = sorted(v.get(("W0", "S1"), {}))
    if len(t) < 8:
        return None
    tot = [v[("W0", "S1")][q] for q in t]
    ser = {}
    for code, _ in BDF_SECTEURS:
        s = [sum(v[("W2", c)].get(q, 0.0) for c in code.split("+")) for q in t]
        ser[code] = [round(100 * a / b, 2) if b else None for a, b in zip(s, tot)]
    ser["non_residents"] = [round(100 * v[("W1", "S1")].get(q, 0) / b, 2) if b else None for q, b in zip(t, tot)]
    libs = dict(BDF_SECTEURS + [("non_residents", "Non-résidents")])
    return {"t": t, "parts": ser, "encours_md": [round(b / 1e6, 1) for b in tot],          # ⚠ DET2 est en MILLIERS d'euros
            "non_residents_md": [round(v[("W1", "S1")].get(q, 0) / 1e6, 1) for q in t],
            "libelles": libs, "source": "Banque de France — détention des titres négociables de l'État (DET2)",
            "url": "https://webstat.banque-france.fr/", "valeur": "valeur de marché ; titres de l'État seul (OAT et BTF)"}


# ── 3. ÉTATS-UNIS ──
Z1_ZIP = "https://www.federalreserve.gov/releases/z1/current/z1_csv_files.zip"
# ⚠ Les détenteurs sont en valeur de MARCHÉ (LM…) et leur total est l'actif de tous les
#   secteurs (FL893…). La dette au PAIR (FL313161105) les dépasse de l'« écart
#   d'instrument » (903…) : l'inclure dans « autres » gonflait la somme de 1 900 Md$.
Z1_TRESOR = [
    ("fed", "Réserve fédérale", ["713061103"]),
    ("etrangers", "Investisseurs étrangers", ["263061105"]),
    ("fonds_mon", "Fonds monétaires", ["633061105"]),
    ("fonds", "Fonds communs et ETF", ["653061105", "553061103", "563061103"]),
    ("hedge", "Fonds spéculatifs (hedge funds)", ["623061103"]),
    ("banques", "Banques", ["763061100", "753061103", "743061103", "473061105", "733061103"]),
    ("courtiers", "Courtiers (broker-dealers)", ["663061105"]),
    ("assureurs", "Assureurs", ["513061105", "543061105"]),
    ("pensions", "Fonds de pension", ["573061105", "343061105", "223061143"]),
    ("collectivites", "États fédérés et collectivités", ["213061103"]),
    ("entreprises", "Entreprises", ["103061103", "113061003"]),
    ("menages", "Ménages et divers (par différence)", ["153061105"]),
    ("autres", "Autres (agences, titrisation, compensation)", ["403061105", "673061103", "503061123"]),
]
Z1_ENTREPRISES = [
    ("assureurs", "Assureurs", ["513063005", "543063005"]),
    ("fonds", "Fonds communs, ETF, fonds fermés", ["653063005", "553063003", "563063003", "463063005"]),
    ("etrangers", "Investisseurs étrangers", ["263063005"]),
    ("pensions", "Fonds de pension", ["573063005", "343063005", "223063045"]),
    ("banques", "Banques", ["763063005", "753063005", "743063005", "473063005", "733063003"]),
    ("hedge", "Fonds spéculatifs (hedge funds)", ["623063005"]),
    ("dette_privee", "Fonds de dette privée", ["443063000"]),
    ("courtiers", "Courtiers (broker-dealers)", ["663063005"]),
    ("menages", "Ménages et divers (par différence)", ["153063005"]),
    ("autres", "Autres", ["123063003", "313063005", "213063003", "713011525", "633063005", "403063005", "613063003", "643063073", "503063005"]),
]


def _z1_table(zf, nom):
    """{code sans préfixe ni suffixe : {trimestre 'AAAA-Qn': M$}} pour une table du Z.1."""
    try:
        t = zf.read("csv/%s.csv" % nom).decode("utf-8", "replace")
    except KeyError:
        return {}
    rows = list(csv.reader(io.StringIO(t)))
    if not rows:
        return {}
    tete, out = rows[0], defaultdict(dict)
    for r in rows[1:]:
        if not r or ":Q" not in r[0]:
            continue
        q = r[0].replace(":", "-")
        for i in range(1, min(len(r), len(tete))):
            m = re.match(r"^(FL|LM|FU|FA)(\d{9})\.Q$", tete[i])
            if not m:
                continue
            try:
                out[(m.group(1), m.group(2))][q] = float(r[i])
            except ValueError:
                pass
    return out


def _z1_groupes(niv, flux, groupes, total_code, depuis="2000-Q1"):
    t = sorted(q for q in niv.get(("FL", total_code), {}) if q >= depuis)
    if not t:
        return None

    def niveau(code, q):
        for p in ("LM", "FL"):
            if (p, code) in niv and q in niv[(p, code)]:
                return niv[(p, code)][q]
        return None

    enc, ach, libs = {}, {}, {}
    for k, lib, codes in groupes:
        libs[k] = lib
        enc[k] = [_r(sum(x for x in (niveau(c, q) for c in codes) if estnb(x)) / 1e3) for q in t]
        ach[k] = [_r(sum(x for x in (flux.get(("FU", c), {}).get(q) for c in codes) if estnb(x)) / 1e3)
                  if any(estnb(flux.get(("FU", c), {}).get(q)) for c in codes) else None for q in t]
    tot = [_r(niv[("FL", total_code)][q] / 1e3) for q in t]
    return {"t": t, "unite": "Md$", "encours": enc, "achats": ach, "total": tot, "libelles": libs,
            "trimestre": _classement(t, ach, libs, 1), "annee": _classement(t, ach, libs, 4)}


def _tic(journal):
    """Détention des Treasuries pays par pays (Trésor, TIC) : 13 derniers mois
    (SLT, table 5) raccordés à l'historique (mfhhis01) — depuis 2010."""
    base = "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/"
    cur = get_txt(base + "slt_table5.txt", timeout=120)
    his = get_txt("https://ticdata.treasury.gov/Publish/mfhhis01.txt", timeout=120)
    if not cur:
        journal.append("TIC (table 5) : vide")
        return None
    MOIS = {m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

    def nom(s):
        s = re.sub(r"\s+\d+/$", "", s.strip().strip('"')).strip()
        return {"For. Official": "Of Which: Foreign Official", "Grand Total": "Grand Total"}.get(s, s)

    val = defaultdict(dict)
    lignes = cur.splitlines()
    tete = next((l.split("\t") for l in lignes if l.startswith("Country\t")), None)
    if not tete:
        return None
    mois = [x.strip() for x in tete[1:] if re.match(r"^\d{4}-\d{2}$", x.strip())]
    ordre = []
    for l in lignes:
        p = l.split("\t")
        if len(p) < 2 or p[0] in ("Country", "") or not re.match(r"^-?[\d.,]+$", (p[1] or "").strip()):
            continue
        n = nom(p[0])
        ordre.append(n)
        for m, x in zip(mois, p[1:]):
            try:
                val[n][m] = float(x.replace(",", ""))
            except ValueError:
                pass
    # l'historique : blocs d'une année, en-têtes « mois » puis « Country année… »
    if his:
        hl, mois_l = his.splitlines(), None
        for i, l in enumerate(hl):
            p = l.split("\t")
            if len(p) > 3 and p[1].strip() in MOIS and all((x.strip() in MOIS or not x.strip()) for x in p[1:]):
                mois_l = [x.strip() for x in p[1:]]
                continue
            if p[0] == "Country" and mois_l:
                ans = [x.strip() for x in p[1:]]
                cols = ["%s-%02d" % (a, MOIS[m]) if a.isdigit() and m in MOIS else None for m, a in zip(mois_l, ans)]
                for l2 in hl[i + 1:]:
                    q = l2.split("\t")
                    if q[0] == "Country" or (len(q) > 1 and q[1].strip() in MOIS):
                        break
                    if not q[0].strip() or q[0].startswith("-"):
                        continue
                    n = nom(q[0])
                    vus = set()
                    for c, x in zip(cols, q[1:]):
                        if not c or c in vus or c in val[n]:       # séries rompues : la plus récente (à gauche) d'abord
                            vus.add(c)
                            continue
                        vus.add(c)
                        try:
                            val[n][c] = float(x.replace(",", ""))
                        except ValueError:
                            pass
                mois_l = None
    # les 20 premiers détenteurs d'aujourd'hui + total + institutions officielles étrangères
    gardes = [n for n in ordre if n not in ("All Other", "Grand Total") and not n.startswith("Of Which")][:20]
    t = sorted(m for m in val.get("Grand Total", {}) if m >= "2010-01")
    if len(t) < 13:
        return None
    pays = {n: [val[n].get(m) for m in t] for n in gardes}
    der = t[-1]
    i12 = t.index(der) - 12 if t.index(der) >= 12 else None
    var = [[n, _r(val[n][der] - val[n][t[i12]])] for n in gardes if i12 is not None and estnb(val[n].get(der)) and estnb(val[n].get(t[i12]))]
    var.sort(key=lambda x: -x[1])
    return {"t": t, "pays": pays, "total": [val["Grand Total"].get(m) for m in t],
            "officiels": [val["Of Which: Foreign Official"].get(m) for m in t] if "Of Which: Foreign Official" in val else None,
            "variation_12m": {"de": t[i12] if i12 is not None else None, "a": der, "lignes": var},
            "unite": "Md$", "source": "Trésor américain — Treasury International Capital (TIC), principaux détenteurs étrangers",
            "url": "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.txt",
            "avertissement": "pays du dépositaire, pas toujours du propriétaire ; valeur de marché (la variation inclut l'effet des prix)"}


FD = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"


def _adjudications_us(journal):
    debut = (date.today() - timedelta(days=760)).isoformat()
    j = get_json(FD + "/v1/accounting/od/auctions_query?" + urllib.parse.urlencode(
        {"sort": "-auction_date", "filter": "security_type:in:(Note,Bond),auction_date:gte:%s" % debut, "page[size]": 500}),
        accept="application/json", timeout=120)
    rows = (j or {}).get("data") or []
    if not rows:
        journal.append("adjudications (Trésor) : vide")
        return None

    def f(x, k):
        v = x.get(k)
        try:
            return float(v) if v not in (None, "null", "") else None
        except ValueError:
            return None
    out = []
    for x in rows:
        ca = f(x, "comp_accepted")
        pd_, di, ind = f(x, "primary_dealer_accepted"), f(x, "direct_bidder_accepted"), f(x, "indirect_bidder_accepted")
        if not ca or not all(estnb(v) for v in (pd_, di, ind)) or abs(pd_ + di + ind - ca) > 0.02 * ca:
            continue
        genre = "tips" if x.get("inflation_index_security") == "Yes" else "frn" if x.get("floating_rate") == "Yes" else "fixe"
        out.append({"date": x["auction_date"], "duree": (x.get("original_security_term") or "").replace("-Year", " ans").replace("-year", " ans"),
                    "genre": genre, "reouverture": x.get("reopening") == "Yes",
                    "taux": f(x, "high_yield"), "couverture": f(x, "bid_to_cover_ratio"),
                    "montant_md": _r((f(x, "offering_amt") or 0) / 1e9, 1),
                    "courtiers_pct": _r(100 * pd_ / ca), "directs_pct": _r(100 * di / ca), "indirects_pct": _r(100 * ind / ca)})
    out.sort(key=lambda a: a["date"])
    return {"liste": out, "source": "Trésor américain (Fiscal Data, résultats des adjudications)",
            "url": "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/"}


def etats_unis(journal):
    out = {}
    try:
        raw = get(Z1_ZIP, timeout=240)
        if raw:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            niv, flux = _z1_table(zf, "F3_2_s"), _z1_table(zf, "F3_2_t_tu")
            # (depuis 2009 : avant, les courtiers et les ménages « par différence » détenaient
            #  des montants NÉGATIFS — positions vendeuses — qu'une aire empilée ne sait pas montrer)
            z = _z1_groupes(niv, flux, Z1_TRESOR, "893061105", depuis="2009-Q1")
            if z:
                z.update({"source": "Réserve fédérale — comptes financiers des États-Unis (Z.1), titres du Trésor",
                          "url": "https://www.federalreserve.gov/releases/z1/", "valeur": "achats nets = transactions du trimestre (non désaisonnalisées)"})
                out["z1"] = z
            ne, fe = _z1_table(zf, "F3_5_s"), _z1_table(zf, "F3_5_t_tu")
            ze = _z1_groupes(ne, fe, Z1_ENTREPRISES, "893063005")
            if ze:
                ze.update({"source": "Réserve fédérale — comptes financiers des États-Unis (Z.1), obligations d'entreprises et étrangères",
                           "url": "https://www.federalreserve.gov/releases/z1/"})
                out["entreprises"] = ze
        else:
            journal.append("Z.1 (Fed) : téléchargement vide")
    except Exception as e:  # noqa: BLE001
        journal.append("Z.1 (Fed) : " + str(e)[:140])
    for cle, fn in (("tic", _tic), ("adjudications", _adjudications_us)):
        try:
            r = fn(journal)
            if r:
                out[cle] = r
        except Exception as e:  # noqa: BLE001
            journal.append("%s : %s" % (cle, str(e)[:140]))
    return out


# ── 4. JAPON : les adjudications (ministère des Finances) ──
MOF_XLS = "https://www.mof.go.jp/english/policy/jgbs/auction/past_auction_results/Auction_Results_for_JGBs.xls"
MOF_FEUILLES = [("40年債", "40 ans"), ("30年債", "30 ans"), ("20年債", "20 ans"), ("10年債", "10 ans"), ("5年債", "5 ans"), ("2年債", "2 ans")]


def japon(journal):
    try:
        import xlrd  # noqa: PLC0415 — import tardif : son absence ne doit pas faire tomber la tâche
    except ImportError:
        journal.append("Japon (adjudications) : module xlrd absent")
        return None
    raw = get(MOF_XLS, timeout=120)
    if not raw:
        journal.append("Japon (adjudications) : vide")
        return None
    b = xlrd.open_workbook(file_contents=raw)
    debut = date.today() - timedelta(days=760)
    out = []
    for feuille, duree in MOF_FEUILLES:
        try:
            s = b.sheet_by_name(feuille)
        except xlrd.biffh.XLRDError:
            continue
        # ⚠ deux formats : adjudication au PRIX (30, 20, 10, 5, 2 ans : prix moyen et prix
        #   le plus bas → la « queue ») ; au RENDEMENT (40 ans : seul le rendement le plus haut)
        tete = [str(c.value) for c in s.row(3)]

        def col(*mots):
            return next((i for i, h in enumerate(tete) if all(m in h for m in mots)), None)
        c_dem, c_acc, c_off = col("Amounts of Compe"), col("Amounts of Bids Ac"), col("Offering Amount")
        c_tmoy, c_tmax = col("Yield at the Avera"), col("Yield at the Lowes") or col("Highest Accepted")
        c_pmoy, c_pmin = col("Weighted Average P"), col("Lowest Accepted Pr")
        if None in (c_dem, c_acc, c_tmax):
            journal.append("Japon : colonnes inattendues (%s)" % duree)
            continue
        for i in range(5, s.nrows):
            r = [c.value for c in s.row(i)]
            if not isinstance(r[1], float) or not isinstance(r[c_acc], float) or not r[c_acc]:
                continue
            d = xlrd.xldate_as_datetime(r[1], b.datemode).date()
            if d < debut:
                continue
            nb = lambda k: r[k] if k is not None and isinstance(r[k], float) else None  # noqa: E731
            queue = round(nb(c_pmoy) - nb(c_pmin), 2) if estnb(nb(c_pmoy)) and estnb(nb(c_pmin)) else None
            out.append({"date": d.isoformat(), "duree": duree, "taux": nb(c_tmax), "taux_moyen": nb(c_tmoy),
                        "couverture": round(r[c_dem] / r[c_acc], 2) if isinstance(r[c_dem], float) else None,
                        "queue_yen": queue,
                        "montant_md": round(nb(c_off) / 10, 0) if estnb(nb(c_off)) else None})   # 億円 → Md¥ (×0,1)
    out.sort(key=lambda a: a["date"])
    return {"liste": out, "source": "Ministère des Finances du Japon (résultats des adjudications)", "url": MOF_XLS,
            "unite_montant": "Md¥"} if out else None


# ── assemblage ──
def construire(journal, precedent=None):
    """{pays: bloc « demande »} + clé `_entreprises_us`. `precedent` = {pays: bloc du
    passage précédent} : réutilisé tel quel s'il a moins de FRAIS_H heures."""
    precedent = precedent or {}
    if precedent and all(_age_h(b) < FRAIS_H for b in precedent.values() if isinstance(b, dict)) and \
            os.environ.get("SCF_OBLIG_DEMANDE_FORCE") != "1" and len(precedent) >= 10:
        return precedent
    maintenant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = defaultdict(dict)
    for nom, fn in (("zone euro", lambda: zone_euro(journal)), ("France", lambda: {"fr": france_bdf(journal)}),
                    ("États-Unis", lambda: {"us": etats_unis(journal)}), ("Japon", lambda: {"jp": japon(journal)})):
        try:
            r = fn() or {}
            for c, x in r.items():
                if not x:
                    continue
                if nom == "zone euro":
                    out[c]["zone_euro"] = x
                elif nom == "France":
                    out[c]["bdf"] = x
                elif nom == "Japon":
                    out[c]["adjudications"] = x
                else:
                    out[c].update(x)
        except Exception as e:  # noqa: BLE001
            journal.append("demande (%s) : %s" % (nom, str(e)[:140]))
    # une source tombée : la partie du passage précédent est reprise, datée
    for c, prec in precedent.items():
        if not isinstance(prec, dict):
            continue
        for k, v in prec.items():
            if k in ("genere_le", "reprise"):
                continue
            if k not in out.get(c, {}):
                out[c][k] = v
                out[c].setdefault("reprise", {})[k] = (prec.get("genere_le") or "")[:10]
    for c in out:
        out[c]["genere_le"] = maintenant
    return dict(out)


if __name__ == "__main__":
    import sys
    j = []
    r = construire(j)
    print(json.dumps({c: {k: (list(v.keys()) if isinstance(v, dict) else v) for k, v in x.items()} for c, x in r.items()}, ensure_ascii=False, indent=1)[:3000])
    print("journal :", j, file=sys.stderr)
