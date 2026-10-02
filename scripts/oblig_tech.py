#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
La dette des groupes tech, sous-ensemble des « entreprises non financières » de la
BRI (module de fetch_obligations.py, 02/10/2026).

QUESTION : la BRI ventile la dette par SECTEUR INSTITUTIONNEL (États, financières,
entreprises), jamais par métier. Or le débat de 2025-2026 porte sur les géants de la
tech qui empruntent pour leurs centres de données. On reconstitue donc leur dette
société par société, depuis les comptes qu'elles déposent à la SEC, et on la pose
DANS la bande « entreprises » du graphe mondial.

SOURCE UNIQUE : SEC EDGAR XBRL `companyfacts` (10-K / 10-Q, comptes audités).

PÉRIMÈTRE (choix argumenté, pas un classement automatique) :
  · les grands groupes tech AMÉRICAINS (siège et émission aux États-Unis : ils sont
    donc dans la bande « entreprises » des États-Unis de la BRI, qui compte par pays
    de résidence de l'émetteur) ;
  · dont la dette financière est, pour l'essentiel, faite d'OBLIGATIONS et de billets
    de trésorerie — les deux seuls instruments que la BRI compte comme « titres de
    dette ». Les comptes XBRL ne séparent pas obligations et prêts : on écarte donc
    les sociétés financées surtout par PRÊTS (CoreWeave, SpaceX-xAI : prêts adossés
    aux puces), par titrisation (Dell : sa filiale de financement), ou issues d'un
    rachat à crédit ;
  · le classement SIC de la SEC ne suffit pas (il range Thermo Fisher, Northrop ou
    EchoStar avec la tech) : la liste est tenue à la main, ci-dessous.
  Groupe « ia » : les cinq géants du cloud qui financent par la dette les centres de
  données de l'IA (Amazon, Microsoft, Alphabet, Meta, Oracle).

DEFINITION DE LA DETTE (reprise de l'ancien fetch_tech_debt.py, retiré le
06/09/2026, et de ses pièges payés) : dette à long terme + part courante + billets
de trésorerie, HORS locations. Les tags XBRL se chevauchent selon l'émetteur
(`ShortTermBorrowings` = papier commercial chez Microsoft, TOUTE la dette courte
chez IBM) : on désambiguïse par la VALEUR (deux postes égaux à 2 % près = même
dette). Le papier commercial n'est jamais reporté d'un trimestre sur l'autre.

⚠ Les entités successives sont recousues (Google Inc. avant Alphabet ; Avago puis
  Broadcom Ltd avant Broadcom Inc.) : sans cela, Broadcom entrerait d'un coup avec
  13 Md$ en 2016 et fabriquerait une marche.
⚠ HPE n'est comptée qu'à partir de son premier bilan propre (31/10/2015, veille de
  la scission) : avant, sa dette est dans les comptes de Hewlett-Packard, et ses
  comptes comparatifs la compteraient deux fois. Inversement, HP Inc. a refait son
  bilan du 31/10/2015 SANS HPE : sans le bilan d'HPE à cette date, la tech
  perdait 15 Md$ pendant un trimestre.
⚠ Trimestre CALENDAIRE : chaque bilan est rangé dans le trimestre qui contient sa
  date (Oracle clôt en mai, Nvidia en janvier). Un trimestre n'est publié que quand
  toutes les sociétés présentes au trimestre précédent l'ont déposé.
"""
import datetime as dt
import gzip
import json
import os
import time
import urllib.request

UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")
URL_CF = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:010d}.json"
SOURCE_URL = "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"

# (code, nom, CIK, groupe, prédécesseurs [CIK du plus récent au plus ancien], compté à partir de)
UNIVERS = [
    ("AMZN", "Amazon", 1018724, "ia", [], None),
    ("MSFT", "Microsoft", 789019, "ia", [], None),
    ("GOOGL", "Alphabet", 1652044, "ia", [1288776], None),
    ("META", "Meta", 1326801, "ia", [], None),
    ("ORCL", "Oracle", 1341439, "ia", [], None),
    ("AAPL", "Apple", 320193, "tech", [], None),
    ("NVDA", "Nvidia", 1045810, "tech", [], None),
    ("AVGO", "Broadcom", 1730168, "tech", [1649338, 1441634], None),
    ("IBM", "IBM", 51143, "tech", [], None),
    ("INTC", "Intel", 50863, "tech", [], None),
    ("CSCO", "Cisco", 858877, "tech", [], None),
    ("CRM", "Salesforce", 1108524, "tech", [], None),
    ("QCOM", "Qualcomm", 804328, "tech", [], None),
    ("TXN", "Texas Instruments", 97476, "tech", [], None),
    ("MU", "Micron", 723125, "tech", [], None),
    ("HPQ", "HP", 47217, "tech", [], None),
    ("HPE", "Hewlett Packard Enterprise", 1645590, "tech", [], "2015-10-31"),
    ("SNPS", "Synopsys", 883241, "tech", [], None),
    ("ADI", "Analog Devices", 6281, "tech", [], None),
    ("AMAT", "Applied Materials", 6951, "tech", [], None),
    ("ADBE", "Adobe", 796343, "tech", [], None),
    ("INTU", "Intuit", 896878, "tech", [], None),
    ("KLAC", "KLA", 319201, "tech", [], None),
    ("LRCX", "Lam Research", 707549, "tech", [], None),
    ("MRVL", "Marvell", 1835632, "tech", [1058057], None),
    ("AMD", "AMD", 2488, "tech", [], None),
    ("MCHP", "Microchip", 827054, "tech", [], None),
    ("MSI", "Motorola Solutions", 68505, "tech", [], None),
    ("WDAY", "Workday", 1327811, "tech", [], None),
    ("ADSK", "Autodesk", 769397, "tech", [], None),
    ("NTAP", "NetApp", 1002047, "tech", [], None),
    ("ON", "onsemi", 1097864, "tech", [], None),
]

# Dette à long terme, part NON COURANTE. L'ordre est une priorité PAR DATE : à une
# date donnée on prend le premier tag publié — jamais un tag reporté d'un trimestre
# antérieur quand un autre est frais (Alphabet est passé en 2020 de
# `LongTermDebtNoncurrent` à `LongTermDebtAndCapitalLeaseObligations` : l'ancien,
# reporté, cachait l'emprunt de 10 Md$ d'août 2020 pendant un an). Analog Devices
# n'a publié que `UnsecuredLongTermDebt` jusqu'en 2021 ; NetApp, Workday, Intuit
# n'avaient longtemps que des convertibles.
T_DEBT_LT = ["LongTermDebtNoncurrent", "LongTermNotesAndLoans", "LongTermNotesPayable",
             "LongTermDebtAndCapitalLeaseObligations", "UnsecuredLongTermDebt", "SeniorLongTermNotes",
             "ConvertibleDebtNoncurrent", "ConvertibleNotesPayable"]
# Postes COURTS, par définition us-gaap :
#   DebtCurrent = TOUTE la dette à moins d'un an (part courante + emprunts courts +
#     papier commercial). Quand il est publié, il fait foi seul : Qualcomm publie à la
#     fois DebtCurrent (2,04) et ses deux composantes (1,54 + 0,50) — les additionner
#     comptait 2 Md$ de trop ; HPE, 3,8 Md$ de trop.
#   part courante de la dette à terme (LongTermDebtCurrent…) ;
#   ShortTermBorrowings = emprunts courts, DISTINCTS de la part courante… sauf chez
#     IBM, qui y range aussi ses échéances courantes : deux postes égaux à 2 % près
#     désignent alors la même dette (désambiguïsation par la VALEUR).
#   papier commercial (titre de dette pour la BRI : compté).
T_DEBT_CUR_TOT = ["DebtCurrent"]
T_DEBT_CUR_LTD = ["LongTermDebtCurrent", "NotesPayableCurrent", "NotesAndLoansPayableCurrent",
                  "LongTermDebtAndCapitalLeaseObligationsCurrent", "UnsecuredDebtCurrent", "ConvertibleDebtCurrent"]
T_DEBT_STB = ["ShortTermBorrowings"]
T_DEBT_CP = ["CommercialPaper", "OtherShortTermBorrowings"]
T_DEBT_TOT = ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt"]
T_REF = ["DebtLongtermAndShorttermCombinedAmount"]


def _get(url, tries=3, timeout=120):
    cache = os.environ.get("SCF_SEC_CACHE")          # banc d'essai local seulement
    if cache:
        f = os.path.join(cache, url.rsplit("/", 1)[-1])
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
    der = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    b = gzip.decompress(b)
                if cache:
                    with open(os.path.join(cache, url.rsplit("/", 1)[-1]), "wb") as fh:
                        fh.write(b)
                return json.loads(b)
        except Exception as e:  # noqa: BLE001
            der = e
            time.sleep(2 * (i + 1))
    raise der


def _jours(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def _instants(gaap, tags):
    """Poste de bilan : date → valeur. Priorité de tag, puis dépôt le plus récent."""
    out = {}
    for prio, tag in enumerate(tags):
        for x in (gaap.get(tag) or {}).get("units", {}).get("USD", []):
            if "start" in x or x.get("val") is None or x.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
                continue
            k, cur = x["end"], out.get(x["end"])
            if cur is None or prio < cur[1] or (prio == cur[1] and x["filed"] > cur[2]):
                out[k] = (float(x["val"]), prio, x["filed"])
    return {k: v[0] for k, v in out.items()}


def _proche(d, cle, tol=45):
    if cle in d:
        return d[cle]
    best, bd = None, tol + 1
    for k, v in d.items():
        g = abs(_jours(min(k, cle), max(k, cle)))
        if g < bd:
            best, bd = v, g
    return best if bd <= tol else None


def dette_societe(gaap):
    """{date de bilan: dette financière hors locations (USD)} + contrôles."""
    b_lt, b_tot = _instants(gaap, T_DEBT_LT), _instants(gaap, T_DEBT_TOT)
    b_ctot, b_cltd = _instants(gaap, T_DEBT_CUR_TOT), _instants(gaap, T_DEBT_CUR_LTD)
    b_stb, b_cp = _instants(gaap, T_DEBT_STB), _instants(gaap, T_DEBT_CP)
    ref = _instants(gaap, T_REF)
    dates = sorted(set(b_lt) | set(b_tot) | set(b_ctot) | set(b_cltd))
    prev, out = {}, {}

    def stock(d, e, cle, maxage=400):
        v = _proche(d, e)
        if v is not None:
            prev[cle] = (e, v)
            return v
        p = prev.get(cle)
        if p and 0 <= _jours(p[0], e) <= maxage:
            return p[1]
        return None

    def meme(a, b):
        return a is not None and b is not None and abs(a - b) <= 0.02 * max(abs(a), abs(b), 1)

    for e in dates:
        if e < "2007-01-01":
            continue
        # Un poste FRAIS l'emporte toujours sur un poste reporté : Adobe n'a publié en
        # 2020 que `LongTermDebt` (4,11) ; reporter son ancien poste non courant (0,99)
        # lui faisait perdre 3 Md$ pendant trois trimestres.
        if _proche(b_lt, e) is None and _proche(b_tot, e) is not None:
            tot, ct = _proche(b_tot, e), _proche(b_ctot, e)
            # `LongTermDebt` est un TOTAL (courant compris) par définition — sauf s'il
            # est inférieur à la seule dette courante (Adobe, 30/08/2019 : 0,99 contre
            # 3,15) : c'est alors la part non courante.
            out[e] = tot + ct if ct is not None and tot < ct - 1e8 else tot
            continue
        lt = stock(b_lt, e, "lt")
        if lt is None:
            tot = stock(b_tot, e, "tot")       # un total seul, part courante comprise (Tesla)
            if tot is None:
                continue
            out[e] = tot
            continue
        # Les postes courts ne sont reportés que d'un trimestre ; le papier commercial,
        # jamais (souvent absent parce qu'il vaut zéro).
        ctot = stock(b_ctot, e, "ctot", maxage=0)
        cltd = stock(b_cltd, e, "cltd", maxage=120)
        # Cisco a déposé au 29/01/2011 sa dette TOTALE (15,24) sous `DebtCurrent` :
        # un poste « courant » égal à la dette entière est une erreur d'étiquette.
        if ctot is not None and meme(ctot, lt + (cltd or 0)) and ctot > 2 * (cltd or 0) + 1e9:
            ctot = None
        if ctot is not None:
            cour = ctot
        else:
            stb = stock(b_stb, e, "stb", maxage=120)
            cp = stock(b_cp, e, "cp", maxage=0)
            cour = cltd or 0
            if stb is not None and not meme(stb, cltd):
                cour += stb
            if cp is not None and not meme(cp, stb) and not meme(cp, cltd):
                cour += cp
        out[e] = lt + cour
    # AUDIT : à chaque date où l'émetteur publie lui-même sa dette consolidée, la
    # reconstitution doit y retomber à 3 % (ce poste est parfois le nominal), papier
    # commercial ajouté ou non (certains l'en excluent : AMD 2025).
    n = bad = 0
    ecarts = []
    for e, r in ref.items():
        if e in out and r > 1e9:
            n += 1
            cp = _proche(b_cp, e, 5) or _proche(b_stb, e, 5) or 0
            if not any(abs(out[e] - x) <= max(1e8, 0.03 * x) for x in (r, r + cp)):
                bad += 1
                ecarts.append((e, round(out[e] / 1e9, 2), round(r / 1e9, 2)))
    return out, {"n": n, "ecarts": bad, "detail": ecarts[-3:]}


def _trim(d):
    """Trimestre calendaire dont la FIN est la plus proche de la date de bilan : un
    exercice de 52/53 semaines clôt le 2 janvier ou le 28 mars ; Nvidia fin janvier
    (→ T4), Oracle fin mai (→ T2)."""
    x = dt.date.fromisoformat(d)
    cands = []
    for y in (x.year - 1, x.year, x.year + 1):
        for q in range(1, 5):
            cands.append((abs((dt.date(y, 3 * q, [31, 30, 30, 31][q - 1]) - x).days), "%d-Q%d" % (y, q)))
    return min(cands)[1]


def _qk(t):
    y, q = t.split("-Q")
    return int(y), int(q)


def _suivant(t):
    y, q = _qk(t)
    return "%d-Q%d" % (y + (q == 4), 1 if q == 4 else q + 1)


def _fin_trim(t):
    y, q = _qk(t)
    return dt.date(y, 3 * q, [31, 30, 30, 31][q - 1]).isoformat()


def construire(journal, precedent=None):
    """Séries trimestrielles (Md$) de la dette des groupes tech américains.
    `precedent` : le bloc du passage précédent, réutilisé s'il a moins de 20 h (la
    SEC ne change rien entre deux dépôts trimestriels ; inutile de retélécharger
    ~60 Mo toutes les six heures)."""
    if precedent and precedent.get("genere_le"):
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(precedent["genere_le"])).total_seconds() / 3600
            if age < 20 and os.environ.get("SCF_OBLIG_TECH_FORCE") != "1":
                return precedent
        except ValueError:
            pass
    par_soc, fiches, audit_n, audit_bad = {}, [], 0, 0
    for code, nom, cik, grp, preds, depuis in UNIVERS:
        serie = {}
        try:
            for i, c in enumerate([cik] + preds):
                d = _get(URL_CF.format(c))
                time.sleep(0.15)
                s, au = dette_societe((d.get("facts") or {}).get("us-gaap") or {})
                audit_n += au["n"]
                audit_bad += au["ecarts"]
                if au["ecarts"]:
                    journal.append("tech %s (%d) : %d écart(s) au total publié %s" % (code, c, au["ecarts"], au["detail"]))
                debut = min(serie) if serie else "9999"
                for e, v in s.items():
                    if e < debut:              # le successeur fait foi sur ses propres dates
                        serie[e] = v
        except Exception as e:  # noqa: BLE001
            journal.append("tech %s : %s" % (code, str(e)[:100]))
            continue
        if depuis:
            serie = {e: v for e, v in serie.items() if e >= depuis}
        if not serie:
            continue
        # un bilan par trimestre calendaire : le dernier de ce trimestre
        q = {}
        for e in sorted(serie):
            q[_trim(e)] = (e, serie[e])
        par_soc[code] = (grp, q)
        der = max(q, key=_qk)
        fiches.append({"code": code, "nom": nom, "groupe": grp, "cik": cik,
                       "md": round(q[der][1] / 1e9, 1), "au": q[der][0], "depuis": min(q, key=_qk)})
    if len(par_soc) < 0.8 * len(UNIVERS):
        raise ValueError("SEC : %d sociétés sur %d seulement" % (len(par_soc), len(UNIVERS)))

    tous = sorted({t for _, q in par_soc.values() for t in q}, key=_qk)
    # Début : le premier trimestre où les sociétés déjà cotées et déjà endettées sont
    # TOUTES là — avant, la somme monterait par entrées successives dans le XBRL, pas
    # par emprunts. On prend le premier trimestre où ≥ 95 % de la dette de 2010-T4
    # (mesurée société par société) est présente.
    ref_t = "2010-Q4"
    ref = {c: q[ref_t][1] for c, (_, q) in par_soc.items() if ref_t in q}
    tot_ref = sum(ref.values())
    debut = ref_t
    for t in tous:
        if _qk(t) > _qk(ref_t):
            break
        pres = sum(v for c, v in ref.items() if t in par_soc[c][1])
        if tot_ref and pres >= 0.95 * tot_ref:
            debut = t
            break
    aujourd = dt.date.today().isoformat()
    out = {"t": [], "tech": [], "ia": [], "n": []}
    reports = 0
    t = debut
    while _qk(t) <= _qk(tous[-1]):
        tot = ia = 0.0
        n = 0
        en_attente = []
        for c, (grp, q) in par_soc.items():
            avant = [x for x in q if _qk(x) < _qk(t)]
            apres = [x for x in q if _qk(x) > _qk(t)]
            if t in q:
                v = q[t][1]
            elif avant and apres:
                # trou dans la série (un 10-Q sans le poste, une société sans dette
                # qui ne le publie pas) : report du dernier bilan
                v = q[max(avant, key=_qk)][1]
                reports += 1
            elif avant:
                v = None
                en_attente.append(c)
            else:
                v = None
            if v is not None:
                tot += v
                n += 1
                if grp == "ia":
                    ia += v
        if en_attente:
            # dépôts pas encore faits : le trimestre n'est pas complet, on s'arrête
            if _jours(_fin_trim(t), aujourd) < 140:
                break
            journal.append("tech %s : %s ne publient plus" % (t, ",".join(en_attente)))
        out["t"].append(t)
        out["tech"].append(round(tot / 1e9, 1))
        out["ia"].append(round(ia / 1e9, 1))
        out["n"].append(n)
        t = _suivant(t)
    if reports:
        journal.append("tech : %d bilans trimestriels absents, dernier bilan reporté" % reports)
    fiches.sort(key=lambda f: -f["md"])
    return {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR, comptes déposés (10-K, 10-Q), données XBRL",
        "source_url": SOURCE_URL,
        "periode": out["t"][-1] if out["t"] else None,
        "serie": out,
        "societes": fiches,
        "n_univers": len(UNIVERS),
        "audit": {"controles": audit_n, "ecarts": audit_bad},
    }


def _prec(t):
    y, q = _qk(t)
    return "%d-Q%d" % (y - (q == 1), 4 if q == 1 else q - 1)


if __name__ == "__main__":
    j = []
    r = construire(j)
    print(json.dumps({k: v for k, v in r.items() if k != "serie"}, ensure_ascii=False, indent=1)[:4000])
    s = r["serie"]
    for i, t in enumerate(s["t"]):
        print(t, s["tech"][i], s["ia"][i], s["n"][i])
    print("journal", j)
