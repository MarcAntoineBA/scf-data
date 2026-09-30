# -*- coding: utf-8 -*-
# Module d'extraction de fetch_geo_revenus.py (issu du prototype de recherche du 30/09/2026).
#!/usr/bin/env python3
"""
geo_revenue.py - repartition geographique du chiffre d'affaires, sources GRATUITES.

Trois collecteurs, une seule sortie JSON :

  sec     <TICKER>     SEC EDGAR : instance XBRL du dernier 10-K / 20-F / 40-F (ou 10-Q avec
                       --trimestriel) -> faits de revenus portant un axe geographique.
  amf     <LEI|ISIN>   France : API info-financiere.gouv.fr (AMF/DILA) -> paquet ESEF du dernier
                       rapport financier annuel / DEU -> tableaux des notes.
  xbrlorg <LEI>        Europe : filings.xbrl.org (xBRL-JSON, sinon le XHTML) -> tableaux des notes.

Deux methodes, parce que les deux mondes ne balisent pas pareil :
  * SEC (10-K, et etrangers en 20-F) : chaque montant par zone est un FAIT XBRL avec une
    dimension (srt:StatementGeographicalAxis, ifrs-full:GeographicalAreasAxis, ou l'axe des
    secteurs quand les secteurs sont des zones). Lecture directe, sans heuristique de mise en page.
  * ESEF (Europe) : seuls les etats primaires sont balises en detail ; les notes ne sont que des
    BLOCS de texte HTML. On lit donc les tableaux de ces blocs et on ne garde un tableau que si
      (1) une cellule vaut exactement un chiffre d'affaires balise du groupe (le total),
      (2) les zones retenues somment a ce total, les zones exclues n'etant que des sous-totaux,
      (3) les libelles sont geographiques (le non-geographique <= 15 % : holding, eliminations).
    Ce triple controle ecarte les tableaux par activite, d'investissements ou de filiales.

Sortie : {symbole, source, forme, exercice_debut, exercice_fin, devise, total, total_concept,
          zones:[{libelle_source, libelle_fr, membre, valeur, part, region, iso, type}],
          regroupements, sous_zones, non_affecte, regions, historique, autres_vues,
          url_depot, date_depot, controle, avertissements}

Dependances : Python 3.9+ et requests (le reste est stdlib : zipfile, xml.etree, html.parser).
SEC : User-Agent avec contact (variable SCF_CONTACT_UA), <= 10 requetes/s (on se tient a ~7/s).
"""
import argparse
import datetime as dt
import io
import itertools
import json
import os
import re
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geo_zones import classify, camel_label, is_sub_label, norm, REGIONS, ISO_FR  # noqa: E402

UA = os.environ.get("SCF_CONTACT_UA") or "SiteCryptoFinance geo-revenus"
CACHE = os.environ.get("GEO_CACHE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_proto")
os.makedirs(CACHE, exist_ok=True)

# ======================================================================================
# HTTP : une session, un delai minimal par hote, controle de taille (Content-Length)
# ======================================================================================
_S = requests.Session()
_S.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
_LAST = {}
_VERROU_HOTE = threading.Lock()
_MIN_GAP = {"www.sec.gov": 0.14, "data.sec.gov": 0.14}


def http_get(url, cache_name=None, binary=False, tries=4):
    """GET avec cache disque, pause par hote, reessais sur 429/5xx, verification de taille."""
    if cache_name:
        p = os.path.join(CACHE, cache_name)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            b = open(p, "rb").read()
            return b if binary else b.decode("utf-8", "replace")
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    for k in range(tries):
        # La pause se RÉSERVE sous verrou : plusieurs fils se partagent la
        # même limite de la SEC (10 requêtes/s), chacun prend son créneau.
        with _VERROU_HOTE:
            maintenant = time.time()
            creneau = max(maintenant, _LAST.get(host, 0) + _MIN_GAP.get(host, 0.3))
            _LAST[host] = creneau
        if creneau > maintenant:
            time.sleep(creneau - maintenant)
        r = _S.get(url, timeout=180)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(3 * (k + 1))
            continue
        r.raise_for_status()
        b = r.content
        cl = r.headers.get("Content-Length")
        if cl and r.headers.get("Content-Encoding") in (None, "", "identity") and int(cl) != len(b):
            time.sleep(1)                      # telechargement tronque : on recommence
            continue
        if cache_name == "sec_company_tickers.json":   # cache disque borne
            open(os.path.join(CACHE, cache_name), "wb").write(b)
        return b if binary else b.decode("utf-8", "replace")
    raise RuntimeError(f"echec GET {url}")


# ======================================================================================
# Outils communs
# ======================================================================================
def days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def _is_aggregate(value, pool, tol):
    """value est-elle la somme d'au moins 2 elements de pool ? (sous-total, zone agregee)
    On teste les petits sous-ensembles ET leurs complements : 'Total non-U.S.' de Boeing est la
    somme de 7 regions, soit 'tout le pool sauf les Etats-Unis'."""
    vals = [v for _, v in pool]
    n, tot = len(vals), sum(vals)
    for size in range(2, min(n, 6) + 1):
        for combo in itertools.combinations(vals, size):
            if abs(sum(combo) - value) <= tol:
                return True
    for size in range(0, min(n - 2, 6) + 1):      # complement : pool moins `size` elements
        for combo in itertools.combinations(vals, size):
            if abs(tot - sum(combo) - value) <= tol:
                return True
    return False


# une zone "enfant" peut etre publiee A COTE de sa zone mere (META : 'United States' en plus de
# 'US & Canada' ; 3M : 'China/Hong Kong' en plus de 'Asia Pacific') : region de l'enfant -> meres possibles
_PARENTS = {"AMN": {"AMN", "AMR"}, "AML": {"AML", "AMR"}, "AMR": {"AMR"}, "EUR": {"EUR", "EMEA"},
            "MOA": {"MOA", "EMEA"}, "EMEA": {"EMEA"}, "CHN": {"CHN", "APAC"}, "APAC": {"APAC"}}
for _v in _PARENTS.values():
    _v.add("RDM")        # 'Other countries' / 'International' peut contenir n'importe quelle zone (Sanofi : la Chine)


def _child_of(k, v, pool, region_of, tol):
    """k (valeur v) est-il une sous-zone d'un element de pool (meme region ou region englobante) ?"""
    rk = region_of(k)
    if not rk or rk not in _PARENTS:
        return None
    for pk, pv in pool:
        if pv + tol >= v and region_of(pk) in _PARENTS[rk]:
            return pk
    return None


def partitions(items, target, tol, max_n=16, region_of=None):
    """Decoupages valides de `target` par les items (cle, valeur), du plus fin au plus grossier.

    Valide = la somme vaut target (+/- tol) ET les items ECARTES s'expliquent :
      * sous-totaux : somme d'items retenus (SAP : 'Americas' = 'United States' + 'Rest of Americas') ;
      * decoupage alternatif : les ecartes somment eux aussi au total (Caterpillar publie 4 regions
        ET 'United States' / 'Outside United States' sur le meme axe) ;
      * sous-zones : plus petits qu'un item retenu de la meme region (META : 'United States' a cote
        de 'US & Canada').
    Cette exigence interdit de piocher 2 lignes au hasard dans un long tableau (filiales...) qui
    tomberaient par coincidence sur le total."""
    items = [(k, v) for k, v in items if v is not None]
    n = len(items)
    if n == 0:
        return
    if abs(sum(v for _, v in items) - target) <= tol:
        yield items
    if n > max_n:
        # trop de membres (AstraZeneca : 3 niveaux de zones sur un meme axe) : on retire d'abord
        # ceux qui sont la somme d'autres membres, puis on cherche sur ce qui reste
        pruned = [x for x in items if not _is_aggregate(x[1], [y for y in items if y is not x], max(tol, 0.002 * abs(x[1])))]
        if len(pruned) < n and len(pruned) <= max_n:
            yield from partitions(pruned, target, tol, max_n, region_of)
        return
    for size in range(n - 1, 1, -1):
        for combo in itertools.combinations(items, size):
            if abs(sum(v for _, v in combo) - target) > tol:
                continue
            kept = set(id(x) for x in combo)
            excluded = [x for x in items if id(x) not in kept]
            nonagg = [(k, v) for k, v in excluded if not _is_aggregate(v, combo, max(tol, 0.002 * abs(v)))]
            if not nonagg or abs(sum(v for _, v in nonagg) - target) <= tol:
                yield list(combo)
                continue
            if region_of:
                # les ecartes qui ne sont pas des sous-zones doivent former, a eux seuls, un autre
                # decoupage du total (Novartis : 'marches etablis' + 'emergents' a cote des regions)
                unexplained = [(k, v) for k, v in nonagg if not _child_of(k, v, combo, region_of, tol)]
                if not unexplained or abs(sum(v for _, v in unexplained) - target) <= tol:
                    yield list(combo)


def build_zones(parts, total, domicile=None, labels=None):
    """parts : [(membre_ou_libelle, valeur)] -> zones classees, parts en % du total."""
    out = []
    for key, val in parts:
        is_member = ":" in str(key) and " " not in str(key)
        lab = (labels or {}).get(key, key)
        if not is_member:     # puces et appels de note des tableaux : "●dont Italie", "Nordamerika 1)"
            lab = re.sub(r"^[\s●•·▪■◦\-–—*]+|\s*(\(\s*\w\s*\)|\d\)|\*+)\s*$", "", str(lab)).strip()
        c = classify(lab, member=key if is_member else None, domicile=domicile)
        if val < 0:           # une "zone" negative est une elimination / un ajustement (Saint-Gobain "Autres")
            c = dict(region=None, iso=None, kind="non_geo")
        out.append(dict(libelle_source=lab, libelle_fr=ISO_FR.get(c["iso"]) if c["kind"] == "pays" else None,
                        membre=key if is_member else None, valeur=val,
                        part=round(100.0 * val / total, 2) if total else None,
                        region=c["region"], iso=c["iso"], type=c["kind"]))
    out.sort(key=lambda z: -z["valeur"])
    return out


def geo_ok(zones, total):
    """Vue geographique = au moins 2 zones geo dont une vraie zone (pas seulement 'reste'),
    et le non-geographique (holding, eliminations, activite...) <= 15 % du total."""
    geo = [z for z in zones if z["type"] != "non_geo"]
    real = [z for z in geo if z["type"] != "reste"]
    nongeo = sum(abs(z["valeur"]) for z in zones if z["type"] == "non_geo")
    return len(geo) >= 2 and len(real) >= 1 and nongeo <= 0.15 * abs(total)


def region_summary(zones):
    """Somme par grande region normalisee (les zones composites gardent le code de la 1re region citee)."""
    agg = {}
    for z in zones:
        r = z["region"] or "NA"
        agg[r] = agg.get(r, 0) + z["valeur"]
    tot = sum(agg.values()) or 1
    return [dict(region=r, libelle=REGIONS.get(r, "Non affecté"), valeur=v, part=round(100 * v / tot, 2))
            for r, v in sorted(agg.items(), key=lambda kv: -kv[1])]


# ======================================================================================
# 1. SEC EDGAR
# ======================================================================================
SEC_REV = [  # par ordre de preference
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:SalesRevenueGoodsNet",
    "ifrs-full:Revenue",
    "ifrs-full:RevenueFromContractsWithCustomers",
    "ifrs-full:RevenueFromSaleOfGoods",
]
GEO_AXES = {"srt:StatementGeographicalAxis", "ifrs-full:GeographicalAreasAxis"}
SEG_AXES = {"us-gaap:StatementBusinessSegmentsAxis", "ifrs-full:SegmentsAxis"}
# autres dimensions tolerees a cote de l'axe geographique sans changer le perimetre
NEUTRAL_MEMBER = re.compile(r"(External|ThirdParty|Actual|OperatingSegments|Reportable)[A-Za-z]*Member$")
XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
# pays du siege des deposants etrangers (pour ifrs-full:CountryOfDomicileMember)
DOMICILE = {"TSM": "TW", "ASML": "NL", "SAP": "DE", "TM": "JP", "SONY": "JP", "NVS": "CH", "HMC": "JP",
            "TAK": "JP", "SNY": "FR", "UL": "GB", "AZN": "GB", "NVO": "DK", "BHP": "AU", "BUD": "BE",
            "DEO": "GB", "STLA": "NL", "BP": "GB", "TTE": "FR", "HSBC": "GB", "INFY": "IN", "RY": "CA",
            "SHOP": "CA", "BABA": "CN", "SHEL": "GB"}
# piege : un ticker peut pointer vers une NOUVELLE societe holding sans 10-K (XOM en 2026)
CIK_FALLBACK = {"XOM": [34088]}


def sec_ticker_map():
    d = json.loads(http_get("https://www.sec.gov/files/company_tickers.json", "sec_company_tickers.json"))
    return {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}


def sec_filings(cik, forms=("10-K", "20-F", "40-F"), refresh=True):
    """Depots recents d'un CIK. refresh=True : pas de cache. Pour detecter un nouveau depot,
    comparer le 1er numero d'accession renvoye au dernier traite (etat stocke cote collecteur)."""
    sub = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                              None if refresh else f"sec_sub_{cik}.json"))
    rec = sub["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form in forms:
            out.append(dict(form=form, acc=rec["accessionNumber"][i], filed=rec["filingDate"][i],
                            report=rec["reportDate"][i], primary=rec["primaryDocument"][i],
                            xbrl=bool(rec["isXBRL"][i])))
    return sub.get("name"), out


def sec_parse_instance(xb):
    """Instance XBRL -> (contextes, faits numeriques). Doublons : on garde le plus precis."""
    ns = {m.group(2).decode(): m.group(1).decode()
          for m in re.finditer(rb'xmlns:([\w\-\.]+)="([^"]+)"', xb[:300000])}

    def qn(tag):
        if tag.startswith("{"):
            uri, loc = tag[1:].split("}")
            return ns.get(uri, uri) + ":" + loc
        return tag
    root = ET.fromstring(xb)
    ctx = {}
    for c in root.findall(f"{XBRLI}context"):
        per = c.find(f"{XBRLI}period")
        s, e = per.find(f"{XBRLI}startDate"), per.find(f"{XBRLI}endDate")
        dims = {m.get("dimension"): m.text.strip() for m in c.iter(f"{XBRLDI}explicitMember")}
        typed = any(True for _ in c.iter(f"{XBRLDI}typedMember"))
        ctx[c.get("id")] = dict(start=s.text.strip() if s is not None else None,
                                end=e.text.strip() if e is not None else None, dims=dims, typed=typed)
    units = {}
    for u in root.findall(f"{XBRLI}unit"):
        meas = [x.text.strip() for x in u.iter(f"{XBRLI}measure") if x.text]
        units[u.get("id")] = meas[0] if len(meas) == 1 else "/".join(meas)
    best = {}
    for el in root:
        cref = el.get("contextRef")
        if cref is None or el.get("unitRef") is None:
            continue
        try:
            v = float((el.text or "").strip())
        except ValueError:
            continue
        d = el.get("decimals") or "0"
        fa = dict(concept=qn(el.tag), ctx=cref, unit=units.get(el.get("unitRef")), val=v,
                  dec=99 if d == "INF" else int(d))
        # meme fait publie 2 fois (ASML : 32 667,3 M a -5 et "32,7 Md" a -8) : le plus precis gagne
        k = (fa["concept"], cref, fa["unit"])
        if k not in best or fa["dec"] > best[k]["dec"]:
            best[k] = fa
    return ctx, list(best.values())


def sec_labels(cik, accn, names):
    """Libelles des membres (fichier *_lab.xml du depot) ; terse > standard."""
    lab = [n for n in names if n.endswith("_lab.xml")]
    if not lab:
        return {}
    xb = http_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{lab[0]}", f"sec_lab_{accn}.xml", binary=True)
    root = ET.fromstring(xb)
    XL = "{http://www.w3.org/1999/xlink}"
    loc2c, lab2t, arcs = {}, {}, []
    for el in root.iter():
        t = el.tag.split("}")[-1]
        if t == "loc":
            frag = el.get(f"{XL}href", "").split("#")[-1]            # ex. nvda_ChinaIncludingHongKongMember
            loc2c[el.get(f"{XL}label")] = frag.replace("_", ":", 1)
        elif t == "label":
            role = el.get(f"{XL}role", "")
            if role.endswith("/label") or role.endswith("terseLabel"):
                lab2t.setdefault(el.get(f"{XL}label"), []).append((role.endswith("terseLabel"), (el.text or "").strip()))
        elif t == "labelArc":
            arcs.append((el.get(f"{XL}from"), el.get(f"{XL}to")))
    out = {}
    for f, t in arcs:
        c = loc2c.get(f)
        if c and t in lab2t:
            txt = sorted(lab2t[t], key=lambda x: not x[0])[0][1]
            out[c] = re.sub(r"\s*\[member\]$", "", txt, flags=re.I)     # Novartis : "Europe [member]"
    return out


def _member_label(m, labels):
    """Libelle d'affichage d'un membre. Pour country:XX le libelle du depot est parfois celui d'un
    autre usage du membre (Caterpillar : 'U.S. Pension Benefits') -> nom du pays."""
    lab = labels.get(m) or camel_label(m)
    mm = re.match(r"^country:([A-Z]{2})$", m)
    if mm and (re.search(r"plan|pension|benefit|tax|subsidiar|operations", lab, re.I) or len(lab) <= 3):
        return ISO_FR.get(mm.group(1), lab)
    return lab


def sec_roles(cik, accn, names):
    """Linkbase de presentation (*_pre.xml) : {role (= un tableau du rapport) : membres cites}."""
    pre = [n for n in names if n.endswith("_pre.xml")]
    if not pre:
        return {}
    try:
        xb = http_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{pre[0]}", f"sec_pre_{accn}.xml", binary=True)
        root = ET.fromstring(xb)
    except Exception:
        return {}
    XL = "{http://www.w3.org/1999/xlink}"
    out = {}
    for link in root.iter():
        if link.tag.split("}")[-1] != "presentationLink":
            continue
        role = link.get(f"{XL}role", "")
        ms = out.setdefault(role, set())
        for loc in link:
            if loc.tag.split("}")[-1] == "loc":
                frag = loc.get(f"{XL}href", "").split("#")[-1]
                if frag.endswith("Member"):
                    ms.add(frag.replace("_", ":", 1))
    return out


def _sec_view(concept, ax, others, unit, per, mem, T, labels, domicile, neutral):
    """Une vue (un tableau) : cherche le decoupage le plus fin qui retombe sur le total T."""
    lab = {m: _member_label(m, labels) for m in mem}
    items = sorted(mem.items(), key=lambda kv: -kv[1])
    tol = max(0.0005 * abs(T), 1.0)       # faits XBRL exacts a l'arrondi pres : tolerance serree (0,05 %)

    def region_of(m):
        return classify(lab[m], member=m, domicile=domicile)["region"]
    chosen, zones, reconc, best_key = None, None, True, None
    for k, part in enumerate(partitions(items, T, tol, region_of=region_of)):
        z = build_zones(part, T, domicile=domicile, labels=lab)
        if not geo_ok(z, T):
            continue
        # a finesse egale, le decoupage qui laisse le moins dans 'reste du monde'
        n_real = sum(1 for x in z if x["type"] not in ("reste", "non_geo"))
        reste = sum(x["valeur"] for x in z if x["type"] == "reste")
        key = (n_real, -reste)
        if best_key is None or key > best_key:
            chosen, zones, best_key = part, z, key
        if k >= 40:
            break
    if chosen is None:
        # vue qui ne retombe pas sur le total (secteurs avec ventes intersecteurs, KO) : gardee
        # comme vue secondaire seulement si les autres dimensions sont neutres
        if not neutral:
            return None
        reconc, chosen = False, items
        denom = sum(v for _, v in items)
        if not 0.95 * T <= denom <= 1.25 * T:
            return None          # somme tres au-dessus du total = membres qui se chevauchent : inutilisable
        zones = build_zones(chosen, denom, domicile=domicile, labels=lab)
        if not geo_ok(zones, denom):
            return None
    kept = {m for m, _ in chosen}
    groups, subs = [], []
    for m, v in items:        # membres ecartes : regroupements (EMEA = Allemagne + reste) ou sous-zones
        if m in kept:
            continue
        tol_m = max(tol, 0.002 * abs(v))
        sub = next(iter(partitions([x for x in chosen], v, tol_m, max_n=10)), None) if reconc else None
        if sub and len(sub) >= 2:
            groups.append(dict(libelle_source=lab[m], membre=m, valeur=v, part=round(100 * v / T, 2),
                               enfants=[k for k, _ in sub]))
            continue
        parent = _child_of(m, v, chosen, region_of, tol) if reconc else None
        if parent:
            z = build_zones([(m, v)], T, domicile=domicile, labels=lab)[0]
            z["zone_mere"] = lab[parent]
            subs.append(z)
    return dict(concept=concept, axe=ax, autres_dimensions=[f"{a}={m}" for a, m in others],
                periode=per, total=T, devise=(unit or "").split(":")[-1],
                reconcilie=reconc, zones=zones, regroupements=groups, sous_zones=subs,
                n_geo=sum(1 for z in zones if z["type"] not in ("reste", "non_geo")),
                part_reste=sum(z["part"] or 0 for z in zones if z["type"] == "reste"))


def sec_extract(ticker, quarterly=False, filing_index=0, cik=None, domicile=None):
    t2c = sec_ticker_map() if not cik else {}
    forms = ("10-Q", "6-K") if quarterly else ("10-K", "20-F", "40-F")
    ciks = ([int(cik)] if cik else [t2c[ticker.upper()]]) + CIK_FALLBACK.get(ticker.upper(), [])
    name, fl, cik = None, [], None
    for cik in ciks:
        name, fl = sec_filings(cik, forms)
        fl = [f for f in fl if f["xbrl"]]                  # amendements (/A) exclus : souvent sans etats financiers
        if fl:
            break
    if not fl:
        return dict(symbole=ticker, source="SEC EDGAR", erreur="aucun depot XBRL de ce type")
    f = fl[filing_index]
    accn = f["acc"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/"
    idx = json.loads(http_get(base + "index.json", f"sec_idx_{accn}.json"))
    names = [it["name"] for it in idx["directory"]["item"]]
    inst = [n for n in names if n.endswith("_htm.xml")] or \
           [n for n in names if n.endswith(".xml") and not re.search(r"(_cal|_def|_lab|_pre)\.xml$|FilingSummary|MetaLinks", n)]
    if not inst:
        return dict(symbole=ticker, source="SEC EDGAR", erreur="instance XBRL introuvable", url_depot=base)
    ctx, facts = sec_parse_instance(http_get(base + inst[0], f"sec_inst_{accn}.xml", binary=True))
    labels = sec_labels(cik, accn, names)
    domicile = domicile or DOMICILE.get(ticker.upper(), "US" if f["form"].startswith("10-") else None)

    # ---- totaux (sans dimension) et vues dimensionnelles par (concept, axe, autres dimensions) ----
    totals, views, units = {}, {}, {}
    for fa in facts:
        if fa["concept"] not in SEC_REV:
            continue
        c = ctx[fa["ctx"]]
        if not c["start"] or c["typed"]:
            continue
        per, dims = (c["start"], c["end"]), c["dims"]
        if not dims:
            totals.setdefault((per, fa["unit"]), {})[fa["concept"]] = fa["val"]   # piege TSMC : meme CA aussi en USD (conversion de convenance)
            continue
        axes = [a for a in dims if a in GEO_AXES or a in SEG_AXES or
                re.search(r"Geograph|Region|Countr|Area", a.split(":")[-1])]
        if len(axes) != 1:
            continue
        ax = axes[0]
        others = tuple(sorted((a, m) for a, m in dims.items() if a != ax))
        views.setdefault((fa["concept"], ax, others, fa["unit"]), {}).setdefault(per, {})[dims[ax]] = fa["val"]

    lo, hi = (80, 100) if quarterly else (300, 380)
    # Un meme axe porte souvent PLUSIEURS tableaux (Novartis : par region, par pays, marches
    # etablis/emergents). Le linkbase de presentation dit quel membre appartient a quel tableau :
    # chaque tableau devient une vue separee, ce qui evite de melanger les niveaux.
    roles = sec_roles(cik, accn, names)
    results = []
    for (concept, ax, others, unit), byper in views.items():
        neutral = all(NEUTRAL_MEMBER.search(m) for _, m in others)
        for per, mem_all in byper.items():
            if not lo <= days(*per) <= hi:
                continue
            tot_d = totals.get((per, unit), {})
            T = tot_d.get(concept) or next((tot_d[c] for c in SEC_REV if c in tot_d), None)
            if not T:
                continue
            groups_m = {}
            for role, ms in roles.items():
                g = frozenset(m for m in mem_all if m in ms)
                if len(g) >= 2:
                    groups_m.setdefault(g, role)
            # membres absents de tout tableau a 2 membres ou plus (Novartis : 'United States' n'est
            # presente que seule) : on les rattache a chaque tableau
            orphans = frozenset(mem_all) - frozenset().union(*groups_m) if groups_m else frozenset()
            if orphans:
                for g, role in list(groups_m.items()):
                    groups_m.setdefault(g | orphans, role)
            groups_m.setdefault(frozenset(mem_all), None)          # repli : tous les membres de l'axe
            for gset, role in groups_m.items():
                r = _sec_view(concept, ax, others, unit, per, {m: mem_all[m] for m in gset}, T,
                              labels, domicile, neutral)
                if r:
                    r["tableau"] = (role or "").rsplit("/", 1)[-1] or None
                    results.append(r)
    if not results:
        return dict(symbole=ticker.upper(), societe=name, source=f"SEC EDGAR {f['form']}",
                    erreur="aucune repartition geographique balisee", url_depot=base + f["primary"],
                    date_depot=f["filed"], accession=f["acc"])

    last_end = max(r["periode"][1] for r in results)

    def rank(r):   # reconciliee, la plus fine, le moins de 'reste du monde', l'axe geographique plutot que les secteurs
        return (r["reconcilie"], r["n_geo"], -round(r["part_reste"] / 10), r["axe"] in GEO_AXES, -len(r["autres_dimensions"]))
    cur, seen = [], set()
    for r in sorted([r for r in results if r["periode"][1] == last_end], key=rank, reverse=True):
        k = tuple(sorted((z["membre"], z["valeur"]) for z in r["zones"]))
        if k not in seen:                      # meme decoupage trouve par un tableau et par le repli
            seen.add(k); cur.append(r)
    best = cur[0]
    hist = []
    for per in sorted({r["periode"] for r in results if r["periode"][1] != last_end}, reverse=True):
        same = [r for r in results if r["periode"] == per and r["axe"] == best["axe"] and r["concept"] == best["concept"]]
        if same:
            h = sorted(same, key=rank, reverse=True)[0]
            hist.append(dict(exercice_debut=per[0], exercice_fin=per[1], total=h["total"], zones=h["zones"]))
    return dict(
        symbole=ticker.upper(), societe=name, source=f"SEC EDGAR {f['form']} (XBRL, faits dimensionnels)",
        forme=f["form"], exercice_debut=best["periode"][0], exercice_fin=best["periode"][1],
        devise=best["devise"], total=best["total"], total_concept=best["concept"],
        vue=dict(axe=best["axe"], concept=best["concept"], autres_dimensions=best["autres_dimensions"],
                 tableau=best.get("tableau")),
        zones=best["zones"], regroupements=best["regroupements"], sous_zones=best["sous_zones"],
        regions=region_summary(best["zones"]),
        controle=dict(reconcilie=best["reconcilie"], somme_zones=sum(z["valeur"] for z in best["zones"])),
        autres_vues=[dict(axe=r["axe"], concept=r["concept"], reconcilie=r["reconcilie"],
                          zones=[(z["libelle_source"], z["valeur"], z["part"]) for z in r["zones"]]) for r in cur[1:]],
        historique=hist, url_depot=base + f["primary"], accession=f["acc"], date_depot=f["filed"])


# ======================================================================================
# 2. ESEF : lecture des tableaux des notes
# ======================================================================================
class _Tables(HTMLParser):
    """Tous les <table> d'un fragment (X)HTML : lignes de (texte, colspan)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack, self.row, self.cell, self.span = [], [], None, None, 1

    def handle_starttag(self, tag, attrs):
        tag = tag.split(":")[-1]
        if tag == "table":
            self.stack.append([])
        elif tag == "tr" and self.stack:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            try:
                self.span = max(1, min(20, int(dict(attrs).get("colspan") or 1)))
            except ValueError:
                self.span = 1
        elif tag in ("br", "p", "div") and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        tag = tag.split(":")[-1]
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append((" ".join("".join(self.cell).split()), self.span))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.stack:
            if any(t for t, _ in self.row):
                self.stack[-1].append(self.row)
            self.row = None
        elif tag == "table" and self.stack:
            t = self.stack.pop()
            if t:
                self.tables.append(t)

    def handle_data(self, d):
        if self.cell is not None:
            self.cell.append(d)


def html_tables(fragment):
    p = _Tables()
    try:
        p.feed(fragment)
    except Exception:
        pass
    return p.tables


DASHES = {"-", "–", "—", "−", "‒", "_", "n/a", "na", "nm", "–%"}


def parse_num(s, decimal_comma):
    """'2 730,0' / '(440)' / '1,234.5' / '−' -> float ; None si ce n'est pas un montant."""
    t = (s or "").replace("\xa0", " ").replace(" ", " ").replace(" ", " ").strip()
    t = re.sub(r"^[€$£¥]\s*|\s*[€$£¥]$", "", t)
    if not t:
        return None
    if t.lower() in DASHES:
        return 0.0
    if "%" in t:
        return None
    neg = False
    m = re.fullmatch(r"\((.*\d.*)\)", t)
    if m:                                  # "(1 581)" : negatif (et surtout pas une note "1)")
        neg, t = True, m.group(1).strip()
    else:                                  # appels de note finaux : "1 234 (1)", "1 234 (a)", "1 234*", "1 234 1)"
        t = re.sub(r"^(.*\d)\s*(\(\s*\d{1,2}\s*\)|\(\s*[a-z]\s*\)|\*+|\s\d\))$", r"\1", t)
    if t[:1] in "-–—−":
        neg, t = True, t[1:]
    t = t.strip("+ ").replace(" ", "")
    t = t.replace(".", "").replace(",", ".") if decimal_comma else t.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def guess_decimal_comma(tables):
    """Francais / allemand / nordique : virgule decimale. Anglais : virgule des milliers."""
    txt = " ".join(c for t in tables for r in t for c, _ in r)
    comma_dec = len(re.findall(r"\d,\d{1,2}(?!\d)", txt)) + len(re.findall(r"\d \d{3}(?!\d)", txt))
    dot_dec = len(re.findall(r"\d\.\d{1,2}(?!\d)", txt)) + len(re.findall(r"\d,\d{3}(?!\d)", txt))
    return comma_dec >= dot_dec


def match_total(v, targets):
    """v (unite du tableau) vaut-il un total balise ? -> (fin, concept, T, echelle)."""
    if v is None or v <= 0:
        return None
    for (end, concept, T) in targets:
        for s in (1.0, 1e3, 1e6, 1e9):
            # tolerance d'arrondi bornee a 0,5 % : un "3" en milliards ne "vaut" pas 3 132 M (Moncler)
            if abs(v * s - T) <= max(0.0015 * T, min(0.5 * s, 0.005 * T)):
                return end, concept, T, s
    return None


# libelle d'une ligne/colonne 'total' ou 'chiffre d'affaires' (dans plusieurs langues)
REVLIKE = re.compile(r"\b(total|totaal|totale|totalt|gesamt|summe|yhteensa|i alt|group|groupe|gruppo|konzern|"
                     r"consolidated|consolide|revenue|revenues|sales|ventes|chiffre d affaires|turnover|umsatz\w*|"
                     r"produits des activites ordinaires|ricavi|omsattning|liikevaihto|omzet|ingresos|net sales)\b")
UNITLIKE = re.compile(r"million|milliers|thousand|billion|milliard|mln|meur|\bm eur|eur m|in eur|en eur|\bnote\b|"
                      r"\bnotes\b|variation|change|growth|croissance|\bvar\b|organic|organique", re.I)
YEAR = re.compile(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)")


def _grid(rows):
    """Lignes (texte, colspan) -> grille : les textes s'etalent sur leur colspan (en-tetes), les
    montants restent dans la 1re cellule (pas de double comptage)."""
    out = []
    for r in rows:
        g = []
        for t, span in r:
            if span > 1 and parse_num(t, True) is None and parse_num(t, False) is None:
                g += [t] * span
            else:
                g += [t] + [""] * (span - 1)
        out.append(g)
    # en-tete SANS cellule de libelle (Hermes : "France | Europe | ... | Total" au-dessus de
    # "Chiffre d'affaires | 1 575 | ...") : decale d'un cran vers la droite
    has_num = [any(parse_num(c, True) is not None for c in g[1:]) for g in out]
    data_len = [len(g) for g, h in zip(out, has_num) if h]
    if data_len:
        L = max(set(data_len), key=data_len.count)
        out = [([""] + g) if (not h and len(g) == L - 1 and g and g[0].strip()) else g for g, h in zip(out, has_num)]
    w = max(len(r) for r in out)
    return [r + [""] * (w - len(r)) for r in out]


def scan_table(rows, targets, dc, domicile, origine, ti):
    """Cherche dans UN tableau une ventilation par zone qui retombe sur un total balise.
    Deux lectures : grille positionnelle (colspan) puis cellules non vides alignees a droite
    (tableaux issus de PDF ou les colonnes ne s'alignent pas)."""
    cands = []
    for mode in ("grille", "compacte"):
        if mode == "grille":
            grid = _grid(rows)
        else:
            grid = [[t for t, _ in r if t != ""] for r in rows]
            w = max(len(r) for r in grid)
            # lignes de donnees : libelle a gauche, montants alignes a droite ; en-tetes : tout a droite
            grid = [([r[0]] + [""] * (w - len(r)) + r[1:]
                     if any(parse_num(c, dc) is not None for c in r[1:]) else [""] * (w - len(r)) + r)
                    if r else [""] * w for r in grid]
        cands += _scan_grid(grid, targets, dc, domicile, f"{origine} / tableau {ti} ({mode})")
        if cands:
            break
    return cands


def _scan_grid(grid, targets, dc, domicile, origine):
    n, W = len(grid), len(grid[0]) if grid else 0
    nums = [[parse_num(c, dc) for c in r] for r in grid]
    for i in range(n):          # ligne d'annees ("2025 | 2024") : en-tete, pas des montants
        vals = [x for x in nums[i] if x is not None]
        if vals and all(x == int(x) and 1980 <= x <= 2100 for x in vals):
            nums[i] = [None] * W
    header = [all(x is None for x in nums[i]) for i in range(n)]
    labels = []
    for i in range(n):
        lab = ""
        for k in range(W):
            if nums[i][k] is not None:
                break
            if grid[i][k].strip():
                lab = grid[i][k].strip()
                break
        labels.append(lab)
    cands = []

    def region_of(lab):
        return classify(lab, domicile=domicile)["region"]

    def try_parts(items, subs, v, m, orient, whole_only=False):
        """1er decoupage valide ET geographique (le plus fin) -> candidat."""
        tol = max(0.002 * v, 0.5 * len(items) + 0.5)
        for part in partitions(items, v, tol, max_n=12, region_of=region_of):
            if whole_only and len(part) != len(items):
                continue            # total non libelle 'total' : on n'accepte que le bloc entier
            c = _cand(part, subs, v, m, orient, origine, domicile)
            if geo_ok(c["tous"], c["total"]):
                cands.append(c)
                return

    # ---------- orientation R : zones en lignes ----------
    for k in range(W):
        for i in range(n):
            m = match_total(nums[i][k], targets)
            if not m:
                continue
            tot_label_ok = labels[i] == "" or bool(REVLIKE.search(norm(labels[i])))
            for direction in (-1, 1):
                block, j = [], i + direction
                while 0 <= j < n and not header[j] and nums[j][k] is not None and labels[j]:
                    block.append((labels[j], nums[j][k]))
                    j += direction
                if len(block) < 2:
                    continue
                subs = [b for b in block if is_sub_label(b[0])]
                items = [b for b in block if not is_sub_label(b[0])]
                try_parts(items, subs, nums[i][k], m, "lignes", whole_only=not tot_label_ok)

    # ---------- orientation C : zones en colonnes ----------
    for i in range(n):
        numcols = [k for k in range(W) if nums[i][k] is not None]
        if len(numcols) < 3:
            continue
        col_label, col_year = {}, {}
        for k in numcols:              # en-tetes au-dessus, du plus proche au plus lointain
            lab, yr = "", None
            for j in range(i - 1, -1, -1):
                if not header[j]:
                    continue
                t = grid[j][k].strip()
                if not t:
                    continue
                if yr is None and YEAR.search(t) and len(t) <= 40:
                    yr = YEAR.search(t).group(1)
                    if re.fullmatch(r"\W*(19|20)\d\d\W*", t):
                        continue
                if not lab and not UNITLIKE.search(t) and not re.fullmatch(r"[\W\d]*", t):
                    lab = t
                if lab and yr:
                    break
            col_label[k], col_year[k] = lab, yr
        for t in numcols:
            v = nums[i][t]
            m = match_total(v, targets)
            if not m:
                continue
            if not (REVLIKE.search(norm(col_label[t])) or REVLIKE.search(norm(labels[i]))):
                continue
            cols = [k for k in numcols if k != t and col_label[k] and col_label[k] != col_label[t]
                    and (col_year[t] is None or col_year[k] == col_year[t])
                    and not re.search(r"^(total|totaal|totale|gesamt|summe|yhteensa)\b", norm(col_label[k]))]
            subs = [(col_label[k], nums[i][k]) for k in cols if is_sub_label(col_label[k])]
            items = [(col_label[k], nums[i][k]) for k in cols if not is_sub_label(col_label[k])]
            try_parts(items, subs, v, m, "colonnes")
    return cands


def _cand(part, subs, v, m, orient, origine, domicile):
    end, concept, T, s = m
    zones = build_zones([(lab, val * s) for lab, val in part], T, domicile=domicile)
    geo = [z for z in zones if z["type"] != "non_geo"]
    nong = [z for z in zones if z["type"] == "non_geo"]
    return dict(exercice_fin=end, total=T, total_concept=concept, echelle=s, orientation=orient,
                origine=origine, zones=geo, non_affecte=nong, tous=zones,
                sous_zones=build_zones([(lab, val * s) for lab, val in subs], T, domicile=domicile),
                n_geo=sum(1 for z in geo if z["type"] != "reste"))


ESEF_REV = re.compile(r"^ifrs-full:(Revenue|RevenueFromContractsWithCustomers|RevenueFromSaleOfGoods)$|"
                      r":(NetSales|Sales|Revenues?|ChiffreDAffaires\w*|Turnover)$")
BLOCK_PRIO = re.compile(r"Segment|GeographicalArea|Revenue|Disaggregation", re.I)


def esef_scan_blocks(blocks, targets, domicile):
    """blocks : [(concept, html)] -> candidats valides. Notes segments/revenus d'abord, puis le reste."""
    prio = [(c, h) for c, h in blocks if BLOCK_PRIO.search(c)]
    rest = [(c, h) for c, h in blocks if not BLOCK_PRIO.search(c)]
    for group in (prio, rest):
        cands = []
        for concept, html in group:
            tabs = html_tables(html)
            if not tabs:
                continue
            dc = guess_decimal_comma(tabs)
            for ti, rows in enumerate(tabs):
                cands += scan_table(rows, targets, dc, domicile, "bloc " + concept.split(":")[-1], ti)
        cands = [c for c in cands if geo_ok(c["tous"], c["total"])]
        if cands:
            return cands
    return []


def esef_result(symbole, source, cands, currency, url, date_depot, fin_balise, avert):
    if not cands:
        return dict(symbole=symbole, source=source, erreur="aucun tableau geographique qui retombe sur le CA balise",
                    url_depot=url, date_depot=date_depot, exercice_fin=fin_balise, avertissements=avert)
    # dedoublonnage (la meme ventilation apparait dans plusieurs notes)
    seen, uniq = set(), []
    for c in cands:
        k = (c["exercice_fin"], tuple((z["libelle_source"], round(z["valeur"])) for z in c["zones"]))
        if k not in seen:
            seen.add(k); uniq.append(c)
    last = max(c["exercice_fin"] for c in uniq)

    def rank(c):   # la plus fine ; puis le moins de non-affecte (Heineken : 'third party revenue')
        return (c["n_geo"], -sum(abs(z["valeur"]) for z in c["non_affecte"]), c["origine"].startswith("bloc"))
    cur = sorted([c for c in uniq if c["exercice_fin"] == last], key=rank, reverse=True)
    hist = []
    for end in sorted({c["exercice_fin"] for c in uniq if c["exercice_fin"] != last}, reverse=True):
        h = sorted([c for c in uniq if c["exercice_fin"] == end], key=rank, reverse=True)[0]
        hist.append(dict(exercice_fin=end, total=h["total"], zones=h["zones"]))
    b = cur[0]
    return dict(symbole=symbole, source=source, exercice_fin=b["exercice_fin"], devise=currency,
                total=b["total"], total_concept=b["total_concept"],
                vue=dict(orientation=b["orientation"], origine=b["origine"], echelle=b["echelle"]),
                zones=b["zones"], sous_zones=b["sous_zones"], non_affecte=b["non_affecte"],
                regions=region_summary(b["zones"]),
                controle=dict(reconcilie=True, somme_zones=sum(z["valeur"] for z in b["tous"])),
                autres_vues=[dict(origine=c["origine"], zones=[(z["libelle_source"], z["valeur"], z["part"]) for z in c["zones"]])
                             for c in cur[1:4]],
                historique=hist, url_depot=url, date_depot=date_depot, avertissements=avert)


# ---------- lecture d'un document iXBRL (XHTML) : totaux balises + blocs de notes ----------
IX = "{http://www.xbrl.org/2013/inlineXBRL}"


def _ix_value(el):
    txt = "".join(el.itertext()).strip()
    fmt = (el.get("format") or "").split(":")[-1].replace("-", "")
    if fmt in ("zerodash", "fixedzero", "fixedempty") or txt in DASHES:
        v = 0.0
    else:
        if "comma" in fmt and "decimal" in fmt:          # numcommadecimal : 1.234,5 / 1 234,5
            t = re.sub(r"[ .\xa0 ]", "", txt).replace(",", ".")
        else:                                            # numdotdecimal : 1,234.5
            t = re.sub(r"[ ,\xa0 ]", "", txt)
        try:
            v = float(t)
        except ValueError:
            return None
    v *= 10 ** int(el.get("scale") or 0)
    return -v if el.get("sign") == "-" else v


def ixbrl_parse(doc, lo=300, hi=380):
    """XHTML iXBRL -> (totaux [(fin, concept, valeur)], blocs [(concept, html)], devise)."""
    root = ET.fromstring(doc)
    ctx = {}
    for c in root.iter(f"{XBRLI}context"):
        per = c.find(f"{XBRLI}period")
        s, e = per.find(f"{XBRLI}startDate"), per.find(f"{XBRLI}endDate")
        dims = any(True for _ in c.iter(f"{XBRLDI}explicitMember")) or any(True for _ in c.iter(f"{XBRLDI}typedMember"))
        ctx[c.get("id")] = (s.text.strip() if s is not None else None, e.text.strip() if e is not None else None, dims)
    units = {}
    for u in root.iter(f"{XBRLI}unit"):
        meas = [x.text.strip() for x in u.iter(f"{XBRLI}measure") if x.text]
        units[u.get("id")] = meas[0].split(":")[-1] if meas else None
    targets, cur = [], None
    for el in root.iter(f"{IX}nonFraction"):
        name = el.get("name") or ""
        c = ctx.get(el.get("contextRef"))
        if not ESEF_REV.search(name) or not c or c[2] or not c[0]:
            continue
        if lo <= days(c[0], c[1]) <= hi:
            v = _ix_value(el)
            if v:
                targets.append((c[1], name, v))
                cur = units.get(el.get("unitRef"))
    conts = {c.get("id"): c for c in root.iter(f"{IX}continuation")}
    blocks = []
    for el in root.iter(f"{IX}nonNumeric"):
        name = el.get("name") or ""
        if not re.search(r"Explanatory|TextBlock|Disclosure", name):
            continue
        parts, nxt, guard = [ET.tostring(el, encoding="unicode")], el.get("continuedAt"), 0
        while nxt and nxt in conts and guard < 500:         # blocs coupes en morceaux (ix:continuation)
            parts.append(ET.tostring(conts[nxt], encoding="unicode"))
            nxt, guard = conts[nxt].get("continuedAt"), guard + 1
        html = "".join(parts)
        if "table" in html:
            blocks.append((name, html))
    return list(dict.fromkeys(targets)), blocks, cur


def esef_from_xhtml(doc, symbole, source, url, date_depot, domicile, lo=300, hi=380):
    targets, blocks, cur = ixbrl_parse(doc, lo, hi)
    avert = []
    cands = esef_scan_blocks(blocks, targets, domicile)
    if not cands and targets:              # repli : tous les tableaux du document (rapport de gestion)
        tabs = html_tables(doc.decode("utf-8", "replace"))
        dc = guess_decimal_comma(tabs)
        for ti, rows in enumerate(tabs):
            cands += scan_table(rows, targets, dc, domicile, "document entier", ti)
        cands = [c for c in cands if geo_ok(c["tous"], c["total"])]
        if cands:
            avert.append("ventilation trouvee hors des notes balisees (rapport de gestion ?) : a verifier")
    return esef_result(symbole, source, cands, cur, url, date_depot, max((t[0] for t in targets), default=None), avert)


# ---------- 2a. France : AMF / DILA (info-financiere.gouv.fr) ----------
AMF = "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records"


def amf_esef_filings(ident, depuis="2020-01-01"):
    """Paquets ESEF (zip / xbri) deposes a l'AMF pour un LEI ou un ISIN, du plus recent au plus
    ancien. Types : 003000 rapport financier annuel, 300005 document d'enregistrement universel."""
    # LEI : 20 caracteres ; ISIN : 12 (FR0000121014)
    field = "identificationsociete_iso_cd_lei" if len(ident) == 20 else "identificationsociete_iso_cd_isi"
    where = (f'{field}="{ident}" and informationdeposee_inf_dat_emt>="{depuis}" '
             f'and (url_de_recuperation like "%.zip" or url_de_recuperation like "%.xbri")')
    r = _S.get(AMF, params=dict(where=where, order_by="informationdeposee_inf_dat_emt desc", limit=50), timeout=60)
    r.raise_for_status()
    out = []
    for x in r.json().get("results", []):
        # piege : ce champ vaut tantot "003000", tantot la CHAINE '["300005", "003000"]'
        stp = re.findall(r"\d{6}", json.dumps(x.get("informationdeposee_inf_stp_inf")))
        if not {"003000", "300005", "002002"} & set(stp):
            continue
        out.append(dict(date=x["informationdeposee_inf_dat_emt"][:10], titre=x.get("informationdeposee_inf_tit_inf"),
                        url=x["url_de_recuperation"], lei=x.get("identificationsociete_iso_cd_lei"),
                        isin=x.get("identificationsociete_iso_cd_isi"), societe=x.get("identificationsociete_iso_nom_soc"),
                        id=x.get("uin_idt_uin"), types=stp))
    return out


def _package_report(raw, url):
    z = zipfile.ZipFile(io.BytesIO(raw))
    bad = z.testzip()                      # CRC : detecte un telechargement corrompu
    if bad:
        raise RuntimeError(f"archive corrompue ({bad}) : {url}")
    reps = [n for n in z.namelist() if re.search(r"/reports/[^/]+\.x?html?$", n)] or \
           [n for n in z.namelist() if n.endswith((".xhtml", ".html"))]
    return z.read(reps[0])


def amf_extract(ident, symbole=None, domicile="FR", index=0):
    fl = amf_esef_filings(ident)
    if not fl:
        return dict(symbole=symbole or ident, source="AMF info-financiere", erreur="aucun paquet ESEF trouve")
    # piege : un zip depose n'est pas toujours un paquet ESEF (Sanofi depose aussi son 20-F zippe) ;
    # on prend le plus recent qui contient bien un rapport XHTML
    errs = []
    for f in fl[index:index + 3]:
        try:
            raw = http_get(f["url"], "amf_" + f["url"].rsplit("/", 1)[-1], binary=True)
            doc = _package_report(raw, f["url"])
        except (IndexError, zipfile.BadZipFile, RuntimeError) as e:
            errs.append(f"{f['titre']} : {e!r}"[:160])
            continue
        r = esef_from_xhtml(doc, symbole or f.get("societe") or ident,
                            "AMF info-financiere.gouv.fr (paquet ESEF, tableaux des notes)", f["url"], f["date"], domicile)
        if errs:
            r.setdefault("avertissements", []).extend("paquet ignore : " + e for e in errs)
        return r
    return dict(symbole=symbole or ident, source="AMF info-financiere", erreur="aucun paquet ESEF lisible", avertissements=errs)


# ---------- 2b. filings.xbrl.org ----------
XORG = "https://filings.xbrl.org"


def xbrlorg_filings(lei):
    d = json.loads(http_get(f"{XORG}/api/entities/{lei}/filings?sort=-date_added&page[size]=50"))
    return [dict(id=f["id"], **f["attributes"]) for f in d["data"]]


def _xorg_targets_blocks(d, lo, hi):
    targets, blocks, cur, text_only = [], [], None, 0
    for fa in d["facts"].values():
        dims = fa["dimensions"]
        concept = dims.get("concept", "")
        extra = [k for k in dims if k not in ("concept", "entity", "period", "unit", "language", "noteId")]
        val = fa.get("value")
        if ESEF_REV.search(concept) and not extra and "/" in (dims.get("period") or ""):
            a, b = [x[:10] for x in dims["period"].split("/")]
            if lo <= days(a, b) <= hi:
                try:
                    end = (dt.date.fromisoformat(b) - dt.timedelta(days=1)).isoformat()   # fin exclusive en xBRL-JSON
                    targets.append((end, concept, float(val)))
                    cur = (dims.get("unit") or "").split(":")[-1]
                except (TypeError, ValueError):
                    pass
        elif isinstance(val, str) and len(val) > 300 and re.search(r"Explanatory|TextBlock|Disclosure", concept):
            if "<table" in val.lower():
                blocks.append((concept, val))
            else:
                text_only += 1
    return list(dict.fromkeys(targets)), blocks, cur, text_only


def xbrlorg_extract(lei, symbole=None, domicile=None, semestriel=False):
    lo, hi = (170, 200) if semestriel else (300, 380)
    fl = xbrlorg_filings(lei)
    if not fl:
        return dict(symbole=symbole or lei, source="filings.xbrl.org", erreur="aucun depot pour ce LEI")
    # plusieurs versions linguistiques du meme rapport (KONE : -fi et -en) : l'anglaise d'abord
    newest = fl[0]["date_added"][:10]
    fl = sorted(fl, key=lambda f: (f["date_added"][:7] >= newest[:7] and bool(re.search(r"[-_]en[-_.]", f["json_url"])),
                                   f["date_added"]), reverse=True)
    avert = []
    for f in fl[:4]:                        # le plus recent qui contient un exercice du bon type
        d = json.loads(http_get(XORG + f["json_url"], f"xorg_{f['id']}.json"))
        targets, blocks, cur, text_only = _xorg_targets_blocks(d, lo, hi)
        if targets:
            break
        avert.append(f"depot {f['fxo_id']} ignore : pas d'exercice {'semestriel' if semestriel else 'annuel'} balise")
    else:
        return dict(symbole=symbole or lei, source="filings.xbrl.org", erreur="aucun depot avec un CA balise", avertissements=avert)
    fin = max(t[0] for t in targets)
    if f.get("period_end") and f["period_end"] != fin:
        avert.append(f"period_end de l'API ({f['period_end']}) != exercice des faits ({fin}) : on se fie aux faits")
    url = XORG + (f.get("report_url") or f["json_url"])
    src = "filings.xbrl.org (ESEF, xBRL-JSON, tableaux des notes)"
    cands = esef_scan_blocks(blocks, targets, domicile)
    if not cands and (text_only or not blocks) and f.get("report_url"):
        # blocs balises escape="false" : le xBRL-JSON n'a garde que le TEXTE, les tableaux sont perdus.
        # On relit le XHTML original.
        doc = http_get(XORG + f["report_url"], f"xorg_{f['id']}.xhtml", binary=True)
        r = esef_from_xhtml(doc, symbole or lei, "filings.xbrl.org (ESEF, XHTML, tableaux des notes)", url,
                            (f.get("date_added") or "")[:10], domicile, lo, hi)
        r.setdefault("avertissements", [])[:0] = avert + ["xBRL-JSON sans tableaux : lecture du XHTML"]
        return r
    return esef_result(symbole or lei, src, cands, cur, url, (f.get("date_added") or "")[:10], fin, avert)


# ======================================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", choices=["sec", "amf", "xbrlorg"])
    ap.add_argument("ident", help="ticker SEC (NVDA) | LEI ou ISIN (amf) | LEI (xbrlorg)")
    ap.add_argument("--symbole", help="symbole a inscrire dans la sortie (ex. CA.PA)")
    ap.add_argument("--domicile", help="ISO du pays du siege (ex. FR, DE)")
    ap.add_argument("--trimestriel", action="store_true", help="SEC : dernier 10-Q au lieu du rapport annuel")
    ap.add_argument("--semestriel", action="store_true", help="xbrlorg : rapport semestriel (si depose en ESEF)")
    ap.add_argument("--out", help="fichier JSON de sortie")
    a = ap.parse_args()
    if a.source == "sec":
        res = sec_extract(a.ident, quarterly=a.trimestriel)
        if a.symbole:
            res["symbole"] = a.symbole
    elif a.source == "amf":
        res = amf_extract(a.ident, symbole=a.symbole, domicile=a.domicile or "FR")
    else:
        res = xbrlorg_extract(a.ident, symbole=a.symbole, domicile=a.domicile, semestriel=a.semestriel)
    for k in ("tous",):
        res.pop(k, None)
    js = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(js)
    print(js)


if __name__ == "__main__":
    main()
