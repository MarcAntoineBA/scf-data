#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Où chaque société réalise son chiffre d'affaires — par pays et par zone.

CE QUE LE LECTEUR DEMANDE
« Carrefour fait-elle son argent en France ? Est-elle exposée au Brésil ? » La
réponse est dans le rapport annuel de chaque société : la norme (IFRS 8.33,
ASC 280-10-50-41) l'oblige à ventiler son chiffre d'affaires par zone
géographique. Ce collecteur va la chercher là, et nulle part ailleurs.

TROIS SOURCES, TOUTES GRATUITES ET SANS CLÉ
  · SEC EDGAR — sociétés américaines ET étrangères qui y déposent (20-F, 40-F :
    TSMC, ASML, SAP, Toyota…). L'API `companyfacts` ne porte AUCUNE dimension ;
    la ventilation est dans l'instance XBRL du 10-K / 20-F, sur l'axe
    `srt:StatementGeographicalAxis` ou sur les segments quand ceux-ci sont des
    zones (Apple : Americas, Europe, Greater China…).
  · AMF (info-financiere.gouv.fr) — sociétés françaises. Le rapport annuel ESEF
    y est déposé en paquet ; la ventilation est dans le TABLEAU d'une note
    (bloc de texte balisé), rarement en faits dimensionnels.
  · filings.xbrl.org — autres européennes (ESEF), même méthode.

⚠ CE QUE LA DONNÉE EST, ET CE QU'ELLE N'EST PAS
  · les zones sont CELLES DE LA SOCIÉTÉ, avec ses frontières. « EMEA » reste
    EMEA : la redécouper en Europe et Afrique serait inventer ;
  · selon les sociétés, la zone est celle du client ou celle de l'entité qui
    vend. NVIDIA range à Taïwan ce qu'elle facture à un assembleur taïwanais ;
  · une société qui ne ventile pas (ou pas sous forme lisible par machine) est
    DITE non ventilée, jamais « 100 % domestique ».

LA MISE À JOUR : ON NE RELIT QUE CE QUI A CHANGÉ
  · SEC : l'index des fondamentaux (collecte quotidienne) porte le numéro du
    dernier dépôt annuel de chaque société. S'il diffère de celui qu'on a lu,
    un nouveau rapport est paru : on relit. Aucune requête sinon.
  · AMF / filings.xbrl.org : on interroge la liste des dépôts d'une société
    quand sa dernière vérification a plus de `--recheck-jours`, ou quand son
    exercice est clos depuis assez longtemps pour qu'un rapport soit attendu.
  · l'état vit DANS les paquets publiés (versionnés dans git, donc présents sur
    le runner) : chaque fiche porte l'identifiant du dépôt lu et sa date de
    vérification. Pas de fichier d'état à part qui pourrait diverger.

Le travail est borné par `--budget` secondes : la première passe (plusieurs
milliers de sociétés) s'étale sur quelques jours, dans l'ordre des plus grosses
capitalisations ; ensuite, seuls les nouveaux rapports coûtent quelque chose.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import glob
import json
import os
import sys
import threading
import time
import traceback

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

CACHE_DIR = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")

PAQUETS = 512
PREFIXE = "geo_revenus_"
SORTIE_AGREGATS = "geo_agregats.json"
SORTIE_INDEX = "geo_revenus_index.json"
FICHIER_LEI = "geo_lei.json"          # ISIN -> LEI, relevé une fois pour toutes

# Places européennes dont les sociétés déposent un rapport ESEF.
PLACES_ESEF = {
    "PA": "FR", "AS": "NL", "BR": "BE", "LS": "PT", "IR": "IE", "MI": "IT",
    "MC": "ES", "DE": "DE", "F": "DE", "VI": "AT", "HE": "FI", "CO": "DK",
    "ST": "SE", "OL": "NO", "WA": "PL", "AT": "GR", "PR": "CZ", "BD": "HU",
    "LU": "LU", "SW": "CH", "IC": "IS", "TL": "EE", "RG": "LV", "VS": "LT",
}
# La Suisse ne dépose pas d'ESEF ; la liste la garde pour le pays du siège,
# l'extraction la saute (elle n'a pas de source).
SANS_ESEF = {"CH"}


def journal(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ── L'EMPREINTE ─────────────────────────────────────────────────────────────
def _empreinte(sym):
    """Le paquet où ranger une société.

    ⚠ Corps recopié à l'identique de `_empreinte()` (actionnariat) et de
    `empreinte()` dans la page : une empreinte qui divergerait entre Python et
    JavaScript produirait des cartes vides sans aucun message d'erreur.
    """
    t = (sym or "?").upper()
    h = 0
    for c in t:
        h = (h * 31 + ord(c)) % 4294967296
    return "%03d" % (h % PAQUETS)


def lire_json(nom, defaut=None):
    p = os.path.join(CACHE_DIR, nom)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return defaut


def ecrire_json(nom, doc):
    p = os.path.join(CACHE_DIR, nom)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)


# ── L'ÉTAT : LES PAQUETS DÉJÀ PUBLIÉS ───────────────────────────────────────
def lire_etat():
    """Toutes les fiches déjà publiées, paquet par paquet.

    Un paquet illisible compte comme vide pour SES sociétés seulement : elles
    seront relues. On ne repart jamais d'une page blanche pour un fichier cassé.
    """
    etat = {}
    for i in range(PAQUETS):
        d = lire_json("%s%03d.json" % (PREFIXE, i), {}) or {}
        for sym, rec in (d.get("societes") or {}).items():
            etat[sym] = rec
    return etat


def ecrire_paquets(etat, meta):
    """Écrit les 512 paquets, TOUS, y compris les vides.

    Sur Cloudflare Pages une adresse inconnue rend 200 et la page d'accueil :
    un paquet absent reviendrait en HTML et se lirait « pas de donnée ». Un
    paquet vide dit la même chose, mais il la dit vraiment.
    """
    seaux = {"%03d" % i: {} for i in range(PAQUETS)}
    for sym, rec in etat.items():
        seaux[_empreinte(sym)][sym] = rec
    for cle, lot in seaux.items():
        ecrire_json("%s%s.json" % (PREFIXE, cle), dict(meta, societes=lot))
    return len(seaux)


# ── L'UNIVERS ───────────────────────────────────────────────────────────────
def lire_marche():
    """Les 37 000 lignes de la collecte de marché : ISIN, pays, secteur,
    industrie, capitalisation — tout ce qu'il faut pour choisir, ordonner et
    agréger. Chaque ligne est un tableau aligné sur `champs`."""
    marche = {}
    for f in sorted(glob.glob(os.path.join(CACHE_DIR, "marche_[0-9][0-9].json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        ch = d.get("champs") or []
        pos = {c: i for i, c in enumerate(ch)}
        for sym, row in (d.get("societes") or {}).items():
            def g(c):
                i = pos.get(c)
                return row[i] if (i is not None and i < len(row)) else None
            marche[sym] = {
                "nom": g("name"), "pays": g("country"), "isin": g("isin"),
                "secteur": g("sector"), "industrie": g("industry"),
                "capi_usd": g("marketCapUsd"), "capi": g("marketCap"),
                "ca": g("revenue"), "devise_cours": g("priceCurrency"),
            }
    return marche


def univers_sec():
    """Les sociétés que la collecte SEC connaît, avec leur dernier dépôt annuel.

    L'index des fondamentaux porte déjà tout ce qu'il faut : le CIK (pas de
    table ticker -> CIK à retélécharger, ni d'homonyme à craindre), le numéro
    du dernier dépôt annuel — c'est LUI qui dit qu'un nouveau rapport est paru —
    et, pour les étrangères, le miroir sous la cotation principale.

    Rend (sociétés, miroirs) où miroirs = {symbole principal: ticker SEC}.
    Le lecteur européen ouvre ASML.AS, pas le certificat ASML : la fiche y est
    écrite une seconde fois, comme le font les fondamentaux."""
    d = lire_json("sec_fundamentals_index.json", {}) or {}
    out, miroirs = {}, {}
    for sym, x in (d.get("societes") or {}).items():
        if x.get("miroir_de"):
            miroirs[sym] = x["miroir_de"]
            continue
        if "." in sym or not x.get("cik"):
            continue
        out[sym] = {"cik": int(str(x["cik"]).lstrip("0") or 0), "accn": x.get("accn"),
                    "depose_le": x.get("depose_le"), "fin_exercice": x.get("fin_exercice"),
                    "nom": x.get("nom_sec")}
    return out, miroirs


def univers_europe(marche):
    """Les sociétés cotées sur une place européenne, avec un ISIN — l'ordre de
    passage (par capitalisation) est décidé plus loin, dans `planifier`."""
    out = {}
    for sym, m in marche.items():
        if "." not in sym:
            continue
        suf = sym.rsplit(".", 1)[1]
        if suf not in PLACES_ESEF:
            continue
        isin = m.get("isin") or ""
        if len(isin) != 12:
            continue
        out[sym] = dict(m, place=suf)
    return out


# ── LA NORMALISATION D'UN RELEVÉ ────────────────────────────────────────────
def exercice_depuis(r):
    """Le format de publication d'un exercice, à partir de celui de l'extracteur."""
    zones = []
    total = r.get("total")
    for z in r.get("zones") or []:
        v = z.get("valeur")
        if not isinstance(v, (int, float)):
            continue
        # Une ligne « Non alloué » à 0,0 % n'apprend rien au lecteur.
        if isinstance(total, (int, float)) and total and abs(v) / abs(total) < 0.0005:
            continue
        zones.append({
            "lib": libelle_affiche(z),
            "lib_source": z.get("libelle_source"),
            "code": z.get("iso"),
            "region": z.get("region"),
            "v": round(float(v), 0),
        })
    vue = r.get("vue") or {}
    return {
        "fin": r.get("exercice_fin"),
        "total": r.get("total"),
        "axe": vue.get("axe") or vue.get("origine"),
        "zones": zones,
    }


def fiche_depuis(r, sym, pays_iso, source, depot_id):
    exs = [exercice_depuis(r)]
    for h in r.get("historique") or []:
        if h.get("zones"):
            exs.append(exercice_depuis(h))
    exs = [e for e in exs if e["zones"]]
    return {
        "statut": "ok" if exs else "non_ventile",
        "source": source,
        "document": r.get("forme") or r.get("document") or "rapport annuel",
        "url": r.get("url_depot"),
        "depose_le": r.get("date_depot"),
        "devise": r.get("devise"),
        "pays_iso": pays_iso,
        "depot_id": depot_id,
        "verifie_le": dt.date.today().isoformat(),
        "exercices": exs[:6],
    }


# ── LES AGRÉGATS : SECTEURS, MÉTIERS, PAYS ──────────────────────────────────
def taux_usd():
    """Valeur en dollars d'une unité de chaque devise (BCE, sans clé).

    Les parts sont des rapports, sans unité ; le taux ne sert qu'à PESER les
    sociétés entre elles. Un taux du jour suffit donc — et en cas de panne, les
    sociétés sans taux sont écartées de la moyenne, et comptées."""
    try:
        import requests
        r = requests.get("https://api.frankfurter.dev/v1/latest?base=USD", timeout=20)
        rates = r.json().get("rates") or {}
        t = {k: 1.0 / v for k, v in rates.items() if v}
        t["USD"] = 1.0
        return t
    except Exception:
        return {"USD": 1.0}


def agreger(etat, marche, taux):
    """La répartition d'un GROUPE = moyenne des répartitions de ses sociétés,
    pondérée par leur chiffre d'affaires en dollars.

    Une société sans ventilation n'entre PAS dans la moyenne (et n'est pas
    comptée domestique par défaut) : le groupe dit combien il en couvre."""
    groupes = {"secteurs": {}, "industries": {}, "pays": {}}
    n_sans_taux = 0

    def acc(fam, cle, poids, regions, dom_p, pays_part):
        g = groupes[fam].setdefault(cle, {"poids": 0.0, "reg": {}, "n": 0,
                                          "dom_poids": 0.0, "dom_hors": 0.0, "n_dom": 0,
                                          "pays": {}})
        g["poids"] += poids
        g["n"] += 1
        for k, p in regions.items():
            g["reg"][k] = g["reg"].get(k, 0.0) + poids * p
        if dom_p is not None:
            g["dom_poids"] += poids
            g["dom_hors"] += poids * (100.0 - dom_p)
            g["n_dom"] += 1
        for k, p in pays_part.items():
            g["pays"][k] = g["pays"].get(k, 0.0) + poids * p

    totaux = {"secteurs": {}, "industries": {}, "pays": {}}
    for sym, m in marche.items():
        # Le dénominateur de couverture : toutes les sociétés du groupe.
        ca_usd = None
        dev = m.get("devise_cours")
        if isinstance(m.get("ca"), (int, float)) and dev in taux:
            ca_usd = m["ca"] * taux[dev]
        for fam, cle in (("secteurs", m.get("secteur")), ("industries", m.get("industrie")),
                         ("pays", iso_du_pays(m.get("pays")))):
            if not cle:
                continue
            t = totaux[fam].setdefault(cle, {"n": 0, "ca": 0.0})
            t["n"] += 1
            if ca_usd and ca_usd > 0:
                t["ca"] += ca_usd

    for sym, rec in etat.items():
        if rec.get("miroir_de"):
            continue                  # compté une fois, sous le ticker qui dépose
        m = marche.get(sym)
        pr = parts_regions(rec)
        if not m or not pr:
            continue
        e = rec["exercices"][0]
        base = e.get("total") or sum(z["v"] for z in e["zones"])
        dev = rec.get("devise")
        if dev not in taux:
            n_sans_taux += 1
            continue
        poids = base * taux[dev]
        for fam, cle in (("secteurs", m.get("secteur")), ("industries", m.get("industrie")),
                         ("pays", iso_du_pays(m.get("pays")))):
            if cle:
                acc(fam, cle, poids, pr["regions"], pr["dom"], pr["pays"])

    out = {}
    for fam, gs in groupes.items():
        out[fam] = {}
        for cle, g in gs.items():
            if g["poids"] <= 0:
                continue
            t = totaux[fam].get(cle) or {}
            regions = {k: round(v / g["poids"], 2) for k, v in g["reg"].items()}
            pays = sorted(((k, v / g["poids"]) for k, v in g["pays"].items()),
                          key=lambda x: -x[1])[:12]
            out[fam][cle] = {
                "n": g["n"], "n_total": t.get("n"),
                "couverture_ca_p": round(100.0 * g["poids"] / t["ca"], 1) if t.get("ca") else None,
                "regions": regions,
                "hors_domicile_p": round(g["dom_hors"] / g["dom_poids"], 1) if g["n_dom"] >= 3 and g["dom_poids"] > 0 else None,
                "n_domicile": g["n_dom"],
                # Les pays nommés : seuls ceux que les sociétés publient comme
                # tels. Leur somme ne fait pas 100 % — le reste est en zones.
                "pays": [{"code": k, "p": round(v, 1)} for k, v in pays if v >= 0.5],
            }
    return out, n_sans_taux


def parts_regions(rec):
    """La répartition du dernier exercice d'une fiche, en parts (%), par région
    et par pays nommé, plus la part domestique quand le pays du siège est une
    zone publiée. None si la fiche n'est pas exploitable."""
    if not rec or rec.get("statut") != "ok":
        return None
    e = (rec.get("exercices") or [None])[0]
    if not e or not e.get("zones"):
        return None
    base = e.get("total") or sum(z["v"] for z in e["zones"])
    if not base or base <= 0:
        return None
    regions, pays = {}, {}
    dom, somme = None, 0.0
    for z in e["zones"]:
        p = 100.0 * z["v"] / base
        somme += z["v"]
        k = z.get("region") or "RDM"
        regions[k] = regions.get(k, 0.0) + p
        if z.get("code"):
            pays[z["code"]] = pays.get(z["code"], 0.0) + p
            if z["code"] == rec.get("pays_iso"):
                dom = (dom or 0.0) + p
    ecart = 100.0 * (base - somme) / base
    if ecart > 1.5:
        regions["RDM"] = regions.get("RDM", 0.0) + ecart
    return {"regions": regions, "pays": pays, "dom": dom}


def agreger_indices(etat):
    """Les indices : la répartition de chaque membre, pondérée par son POIDS
    dans l'indice — c'est ainsi que se lit l'exposition d'un indice (celle du
    porteur d'un ETF qui le réplique). La composition vient de la collecte des
    fiches d'indices (`indice_<code>.json`)."""
    out = {}
    for f in sorted(glob.glob(os.path.join(CACHE_DIR, "indice_*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        ch = d.get("champs") or []
        if "sym" not in ch or "poids" not in ch:
            continue
        i_sym, i_p = ch.index("sym"), ch.index("poids")
        code = d.get("code") or os.path.basename(f)[7:-5]
        tot_p, cov_p, n, n_tot = 0.0, 0.0, 0, 0
        reg, pays = {}, {}
        dom_p, dom_hors, n_dom = 0.0, 0.0, 0
        for row in d.get("lignes") or []:
            sym, w = row[i_sym], row[i_p]
            if not isinstance(w, (int, float)) or w <= 0:
                continue
            n_tot += 1
            tot_p += w
            pr = parts_regions(etat.get(sym))
            if not pr:
                continue
            n += 1
            cov_p += w
            for k, p in pr["regions"].items():
                reg[k] = reg.get(k, 0.0) + w * p
            for k, p in pr["pays"].items():
                pays[k] = pays.get(k, 0.0) + w * p
            if pr["dom"] is not None:
                dom_p += w
                dom_hors += w * (100.0 - pr["dom"])
                n_dom += 1
        if cov_p <= 0:
            continue
        out[code] = {
            "nom": (d.get("synthese") or {}).get("nom"),
            "n": n, "n_total": n_tot,
            "couverture_poids_p": round(100.0 * cov_p / tot_p, 1) if tot_p else None,
            "regions": {k: round(v / cov_p, 2) for k, v in reg.items()},
            "hors_domicile_p": round(dom_hors / dom_p, 1) if n_dom >= 3 and dom_p > 0 else None,
            "n_domicile": n_dom,
            "pays": [{"code": k, "p": round(v / cov_p, 1)}
                     for k, v in sorted(pays.items(), key=lambda x: -x[1])[:12] if v / cov_p >= 0.5],
        }
    return out


_ISO_CACHE = {}


def iso_du_pays(nom):
    """« France » -> FR, « United States » -> US, via le classeur de libellés de
    `geo_zones` — le même qui range les zones publiées : un seul dictionnaire
    des pays pour tout le collecteur."""
    if not nom:
        return None
    if nom in _ISO_CACHE:
        return _ISO_CACHE[nom]
    try:
        import geo_zones as GZ
        r = GZ.classify(nom)
        iso = r.get("iso") if r.get("kind") == "pays" else None
    except Exception:
        iso = None
    _ISO_CACHE[nom] = iso
    return iso


# Les zones que les sociétés américaines et européennes publient en anglais.
# Le libellé source reste dans la fiche (infobulle) ; celui-ci est l'affiché.
TRAD_ZONES = {
    "americas": "Amériques", "the americas": "Amériques", "north america": "Amérique du Nord",
    "latin america": "Amérique latine", "south america": "Amérique du Sud",
    "other americas": "Autres Amériques", "rest of americas": "Reste des Amériques",
    "europe": "Europe", "rest of europe": "Reste de l'Europe", "other europe": "Autres pays d'Europe",
    "emea": "Europe, Moyen-Orient, Afrique", "europe middle east and africa": "Europe, Moyen-Orient, Afrique",
    "europe the middle east and africa": "Europe, Moyen-Orient, Afrique",
    "rest of emea": "Reste de la zone EMEA", "middle east and africa": "Moyen-Orient et Afrique",
    "middle east africa": "Moyen-Orient et Afrique", "africa": "Afrique", "middle east": "Moyen-Orient",
    "asia": "Asie", "asia pacific": "Asie-Pacifique", "apac": "Asie-Pacifique", "apj": "Asie-Pacifique-Japon",
    "rest of asia pacific": "Reste de l'Asie-Pacifique", "other asia pacific": "Autres pays d'Asie-Pacifique",
    "rest of apj": "Reste de l'Asie-Pacifique-Japon", "greater china": "Grande Chine",
    "china including hong kong": "Chine (y compris Hong Kong)", "asia pacific and japan": "Asie-Pacifique et Japon",
    "rest of world": "Reste du monde", "rest of the world": "Reste du monde", "other": "Autres pays",
    "others": "Autres pays", "other countries": "Autres pays", "all other countries": "Autres pays",
    "all other": "Autres pays", "other foreign countries": "Autres pays étrangers",
    "other international": "Autres pays étrangers", "international": "International",
    "foreign": "Étranger", "non us": "Hors États-Unis", "outside the united states": "Hors États-Unis",
    "domestic": "Marché intérieur", "oceania": "Océanie", "australia and new zealand": "Australie et Nouvelle-Zélande",
    # Les rapports ESEF sont souvent dans la langue du pays.
    "resto dell unione europea": "Reste de l'Union européenne", "resto dell europa": "Reste de l'Europe",
    "resto del mondo": "Reste du monde", "altri paesi": "Autres pays", "europa": "Europe",
    "nord america": "Amérique du Nord", "america latina": "Amérique latine", "centro sud america": "Amérique centrale et du Sud",
    "asia e oceania": "Asie et Océanie", "altri": "Autres pays", "ubriges europa": "Reste de l'Europe",
    "ubrige welt": "Reste du monde", "sonstige": "Autres pays", "sonstige lander": "Autres pays",
    "nordamerika": "Amérique du Nord", "asien": "Asie", "resto de europa": "Reste de l'Europe",
    "resto del mundo": "Reste du monde", "otros": "Autres pays", "norteamerica": "Amérique du Nord",
    "rest van europa": "Reste de l'Europe", "rest van de wereld": "Reste du monde", "overige landen": "Autres pays",
    "asia africa australasia": "Asie, Afrique, Australasie", "canada and latin america": "Canada et Amérique latine",
    "asia pacific excluding japan": "Asie-Pacifique hors Japon", "rest of asia": "Reste de l'Asie",
    "united states and canada": "États-Unis et Canada", "us and canada": "États-Unis et Canada",
    "other european countries": "Autres pays d'Europe", "european union": "Union européenne",
    "ovriga varlden": "Reste du monde", "ovriga europa": "Reste de l'Europe", "nordamerika och sydamerika": "Amériques",
}


def _recoller(t):
    """« Moyen- Orient », « Asie- Pacifique » : un mot coupé en fin de ligne
    dans le tableau du rapport garde son espace dans le texte extrait."""
    import re
    t = re.sub(r"(\w)- (\w)", r"\1-\2", t or "")
    t = re.sub(r"\s*\((?:\d|[a-z])\)\s*$", "", t)      # renvoi de note : « North America(1) »
    t = re.sub(r"(\w)\((?:\d|[a-z])\)", r"\1", t)
    return " ".join(t.split())


def libelle_affiche(z):
    """Le libellé à afficher : la traduction de `geo_zones` pour un pays, la
    table ci-dessus pour une zone anglaise ou locale, sinon le libellé de la
    société, recollé."""
    if z.get("libelle_fr"):
        return _recoller(z["libelle_fr"])
    src = _recoller(z.get("libelle_source") or "")
    try:
        import geo_zones as GZ
        if z.get("iso") and z["iso"] in GZ.ISO_FR and z.get("type") == "pays":
            return GZ.ISO_FR[z["iso"]]
        cle = GZ.norm(src)
    except Exception:
        cle = src.lower()
    return TRAD_ZONES.get(cle.strip(), src)


# ── LEI : L'IDENTIFIANT QU'EXIGENT LES DÉPÔTS EUROPÉENS ────────────────────
_verrou_lei = threading.Lock()
_verrou_gleif = threading.Lock()


def lei_de(isin, table):
    """ISIN -> LEI par l'API publique de la GLEIF (sans clé, 60 requêtes/min).

    Relevé une fois et gardé : un LEI ne change pas. Un échec est gardé aussi,
    daté, pour ne pas redemander chaque jour ce que la GLEIF ignore."""
    with _verrou_lei:
        x = table.get(isin)
    if x is not None:
        if x.get("lei") or age_jours(x.get("le")) < 60:
            return x.get("lei")
    lei = None
    try:
        import requests
        with _verrou_gleif:           # 60 requêtes/minute : une à la fois
            time.sleep(1.05)
            r = requests.get("https://api.gleif.org/api/v1/lei-records",
                             params={"filter[isin]": isin, "page[size]": 1}, timeout=30)
        if r.status_code == 200:
            data = r.json().get("data") or []
            if data:
                lei = data[0].get("id")
        else:
            return None               # panne passagère : on ne mémorise rien
    except Exception:
        return None
    with _verrou_lei:
        table[isin] = {"lei": lei, "le": dt.date.today().isoformat()}
    return lei


# ── LE TRAVAIL D'UNE SOCIÉTÉ ────────────────────────────────────────────────
def conserver_si_meilleur(ancien, nouveau, depot_id):
    """Un relevé qui échoue ne remplace jamais un relevé réussi.

    Une source en panne ce jour-là, un rapport au format inhabituel : on garde
    la dernière répartition connue (elle porte son exercice, la carte le dit)
    et on note seulement qu'on a vu passer ce dépôt, pour ne pas le relire
    chaque jour."""
    if nouveau.get("statut") == "ok" or not ancien or ancien.get("statut") != "ok":
        return nouveau
    garde = dict(ancien)
    if depot_id:
        garde["depot_vu"] = depot_id
    garde["verifie_le"] = dt.date.today().isoformat()
    garde["dernier_echec"] = nouveau.get("motif") or nouveau.get("statut")
    return garde


def travailler_sec(GX, sym, x, marche):
    m = marche.get(sym) or {}
    dom = iso_du_pays(m.get("pays")) or "US"
    try:
        r = GX.sec_extract(sym, cik=x["cik"], domicile=dom)
    except Exception as e:
        return {"statut": "echec", "motif": "%s: %s" % (type(e).__name__, str(e)[:160]),
                "source": "SEC EDGAR", "depot_id": x.get("accn"), "pays_iso": dom,
                "verifie_le": dt.date.today().isoformat()}
    depot = r.get("accession") or x.get("accn")
    if r.get("erreur"):
        return {"statut": "non_ventile", "motif": r["erreur"], "source": "SEC EDGAR",
                "depot_vu": x.get("accn"),
                "document": ((r.get("forme") or "rapport annuel") + " " + (r.get("date_depot") or "")[:4]).strip(),
                "url": r.get("url_depot"), "depose_le": r.get("date_depot"),
                "pays_iso": dom, "depot_id": depot, "verifie_le": dt.date.today().isoformat()}
    f = fiche_depuis(r, sym, dom, "SEC EDGAR", depot)
    f["document"] = "%s %s" % (r.get("forme") or "10-K", (r.get("exercice_fin") or "")[:4])
    # ⚠ Le dépôt que l'INDEX désigne peut différer de celui qu'on a lu (un
    # amendement « /A » sans états, un rapport sans XBRL) : on le note comme
    # vu, sans quoi la société serait relue chaque jour pour rien.
    if x.get("accn") and x["accn"] != depot:
        f["depot_vu"] = x["accn"]
    return f


def travailler_europe(GX, sym, m, table_lei, ancien):
    """Français : AMF d'abord (le paquet ESEF officiel, par ISIN), puis
    filings.xbrl.org. Autres européens : filings.xbrl.org par LEI."""
    pays = PLACES_ESEF.get(m["place"])
    dom = iso_du_pays(m.get("pays")) or pays
    isin = m["isin"]
    francaise = isin.startswith("FR") or pays == "FR"
    essais = []
    if francaise:
        essais.append(("amf", isin))
    lei = lei_de(isin, table_lei)
    if lei:
        essais.append(("xbrlorg", lei))
    if not essais:
        return {"statut": "echec", "motif": "LEI introuvable pour cet ISIN", "source": "ESEF",
                "pays_iso": dom, "verifie_le": dt.date.today().isoformat()}
    dernier = None
    for src, ident in essais:
        nom_src = "AMF" if src == "amf" else "filings.xbrl.org"
        try:
            # Le dépôt le plus récent d'abord : s'il est celui qu'on a déjà lu,
            # rien n'a changé et l'on s'arrête là — une seule requête légère.
            if src == "amf":
                fl = GX.amf_esef_filings(ident)
                depot = fl[0]["url"] if fl else None
            else:
                fl = GX.xbrlorg_filings(ident)
                depot = str(fl[0]["id"]) if fl else None
            if not depot:
                dernier = dernier or {"statut": "non_ventile", "motif": "aucun rapport ESEF déposé",
                                      "source": nom_src}
                continue
            if ancien and ancien.get("statut") in ("ok", "non_ventile") and \
               depot in (ancien.get("depot_id"), ancien.get("depot_vu")):
                garde = dict(ancien)
                garde["verifie_le"] = dt.date.today().isoformat()
                return garde
            if src == "amf":
                r = GX.amf_extract(ident, symbole=sym, domicile=dom)
                nom_src = "AMF · rapport annuel ESEF"
            else:
                r = GX.xbrlorg_extract(ident, symbole=sym, domicile=dom)
                nom_src = "filings.xbrl.org · rapport annuel ESEF"
            if r.get("erreur"):
                dernier = {"statut": "non_ventile", "motif": r["erreur"], "source": nom_src,
                           "url": r.get("url_depot"), "depose_le": r.get("date_depot"),
                           "document": ("rapport annuel " + (r.get("date_depot") or "")[:4]).strip(),
                           "depot_id": depot}
                continue
            f = fiche_depuis(r, sym, dom, nom_src, depot)
            f["document"] = "rapport annuel %s" % ((r.get("exercice_fin") or "")[:4])
            if lei:
                f["lei"] = lei
            return f
        except Exception as e:
            dernier = {"statut": "echec", "motif": "%s: %s" % (type(e).__name__, str(e)[:160]),
                       "source": nom_src}
    dernier = dernier or {"statut": "echec", "motif": "aucune source"}
    dernier.update({"pays_iso": dom, "verifie_le": dt.date.today().isoformat()})
    return dernier


# ── L'ORDONNANCEMENT ────────────────────────────────────────────────────────
def age_jours(date_iso):
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(date_iso)[:10])).days
    except Exception:
        return 10 ** 6


def rapport_attendu(rec):
    """Un nouveau rapport annuel est-il plausible ? Un an et quarante-cinq
    jours après la clôture du dernier exercice lu — c'est la fenêtre où les
    européennes publient (fin février à fin avril pour une clôture au 31
    décembre) — et pas relu depuis trois jours."""
    ex = (rec.get("exercices") or [{}])[0] if rec else {}
    fin = ex.get("fin")
    if not fin:
        return False
    try:
        cloture = dt.date.fromisoformat(fin[:10])
    except Exception:
        return False
    prochaine = cloture + dt.timedelta(days=365 + 45)
    return dt.date.today() >= prochaine and age_jours(rec.get("verifie_le")) >= 3


def planifier(etat, sec, eur, marche, recheck):
    """Priorité 0 : nouveau dépôt certain (SEC) ou attendu (Europe).
    Priorité 1 : jamais relevée — par capitalisation décroissante.
    Priorité 2 : revérification périodique (échecs, non ventilées)."""
    taches = []
    for sym, x in sec.items():
        rec = etat.get(sym)
        capi = (marche.get(sym) or {}).get("capi_usd") or 0
        if rec is None:
            p = 1
        elif x.get("accn") and x["accn"] not in (rec.get("depot_id"), rec.get("depot_vu")):
            p = 0
        elif rec.get("statut") == "echec" and age_jours(rec.get("verifie_le")) >= 7:
            p = 2
        elif age_jours(rec.get("verifie_le")) >= recheck * 3:
            p = 2
        else:
            continue
        taches.append((p, -(capi or 0), "sec", sym))
    for sym, m in eur.items():
        rec = etat.get(sym)
        if rec and rec.get("miroir_de"):
            continue
        if PLACES_ESEF.get(m["place"]) in SANS_ESEF:
            continue
        capi = m.get("capi_usd") or 0
        if rec is None:
            p = 1
        elif rec.get("statut") == "ok" and rapport_attendu(rec):
            p = 0
        elif rec.get("statut") == "echec" and age_jours(rec.get("verifie_le")) >= 7:
            p = 2
        elif age_jours(rec.get("verifie_le")) >= recheck:
            p = 2
        else:
            continue
        taches.append((p, -(capi or 0), "esef", sym))
    taches.sort()
    return taches


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--budget", type=int, default=2100,
                    help="secondes de travail au plus (défaut 35 min) ; le reste attend demain")
    ap.add_argument("--parallele", type=int, default=6)
    ap.add_argument("--recheck-jours", type=int, default=30)
    ap.add_argument("--seulement", default="", help="symboles séparés par des virgules (essai)")
    ap.add_argument("--sans-europe", action="store_true")
    ap.add_argument("--sans-sec", action="store_true")
    ap.add_argument("--max", type=int, default=0, help="nombre de sociétés au plus (essai)")
    a = ap.parse_args()

    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="geo_")
    os.environ["GEO_CACHE"] = tmp
    import geo_extraction as GX

    t0 = time.time()
    marche = lire_marche()
    etat = lire_etat()
    n_avant = sum(1 for r in etat.values() if r.get("statut") == "ok")
    sec, miroirs = ({}, {}) if a.sans_sec else univers_sec()
    eur = {} if a.sans_europe else univers_europe(marche)
    table_lei = lire_json(FICHIER_LEI, {}) or {}
    journal("univers : %d SEC, %d miroirs, %d européennes ; %d fiches déjà publiées (%d ventilées)"
            % (len(sec), len(miroirs), len(eur), len(etat), n_avant))
    if not marche:
        journal("ERREUR : collecte de marché introuvable dans %s" % CACHE_DIR)
        sys.exit(2)

    taches = planifier(etat, sec, eur, marche, a.recheck_jours)
    if a.seulement:
        voulu = [s.strip().upper() for s in a.seulement.split(",") if s.strip()]
        taches = [(0, 0, "sec" if s in sec else "esef", s) for s in voulu if s in sec or s in eur]
    if a.max:
        taches = taches[:a.max]
    journal("à faire : %d (nouveaux dépôts %d, jamais relevées %d, revérifications %d)"
            % (len(taches), sum(1 for t in taches if t[0] == 0), sum(1 for t in taches if t[0] == 1),
               sum(1 for t in taches if t[0] == 2)))

    verrou = threading.Lock()
    compte = {"ok": 0, "non_ventile": 0, "echec": 0, "inchange": 0}
    arret = t0 + a.budget

    def un(t):
        _, _, genre, sym = t
        if time.time() > arret:
            return None
        ancien = etat.get(sym)
        try:
            if genre == "sec":
                rec = travailler_sec(GX, sym, sec[sym], marche)
            else:
                rec = travailler_europe(GX, sym, eur[sym], table_lei, ancien)
        except Exception as e:
            rec = {"statut": "echec", "motif": "%s: %s" % (type(e).__name__, str(e)[:160]),
                   "verifie_le": dt.date.today().isoformat()}
            traceback.print_exc()
        if rec is not ancien:
            rec = conserver_si_meilleur(ancien, rec, rec.get("depot_id"))
        with verrou:
            if ancien is not None and (rec is ancien or (
                    rec.get("statut") == "ok" and rec.get("depot_id") == ancien.get("depot_id"))):
                compte["inchange"] += 1
            else:
                compte[rec.get("statut") if rec.get("statut") in compte else "echec"] += 1
            etat[sym] = rec
        return sym

    faits = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.parallele)) as ex:
        for s in ex.map(un, taches):
            if s:
                faits += 1
                if faits % 50 == 0:
                    journal("  %d/%d · %s" % (faits, len(taches), compte))

    # Les miroirs : la fiche d'ASML.AS est celle d'ASML, dite comme telle.
    for principal, us in miroirs.items():
        src = etat.get(us)
        if src and src.get("statut") == "ok":
            mi = dict(src)
            mi["miroir_de"] = us
            etat[principal] = mi

    meta = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "SEC EDGAR (10-K, 20-F, 40-F), AMF et filings.xbrl.org (ESEF)",
        "note": "Zones telles que publiées par chaque société dans son rapport annuel.",
    }
    ecrire_paquets(etat, meta)
    ecrire_json(FICHIER_LEI, table_lei)

    taux = taux_usd()
    agr, n_sans_taux = agreger(etat, marche, taux)
    agr["indices"] = agreger_indices(etat)
    agr.update(meta)
    agr["n_sans_taux"] = n_sans_taux
    ecrire_json(SORTIE_AGREGATS, agr)

    par_statut, par_source = {}, {}
    for r in etat.values():
        par_statut[r.get("statut")] = par_statut.get(r.get("statut"), 0) + 1
        if r.get("statut") == "ok" and not r.get("miroir_de"):
            s = (r.get("source") or "?").split(" ")[0]
            par_source[s] = par_source.get(s, 0) + 1
    ecrire_json(SORTIE_INDEX, dict(meta, fiches=len(etat), par_statut=par_statut,
                                   par_source=par_source, ce_passage=compte,
                                   restant=max(0, len(taches) - faits),
                                   duree_s=round(time.time() - t0, 1)))
    journal("fini en %.0f s : %s ; %d fiches, %s" % (time.time() - t0, compte, len(etat), par_statut))
    shutil.rmtree(tmp, ignore_errors=True)
    # Un passage qui n'a rien pu relever du tout alors qu'il avait du travail
    # est une panne, pas un succès : on le dit à l'orchestrateur.
    if faits >= 10 and compte["echec"] == faits:
        sys.exit(2)


if __name__ == "__main__":
    main()
