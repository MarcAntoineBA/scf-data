#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_13f.py — CE QUE LES GÉRANTS QUI COMPTENT ONT FAIT DU TRIMESTRE.

CE QU'UN 13F EST, ET CE QU'IL N'EST PAS. C'est une PHOTO, pas un flux : les actions
américaines détenues en position longue au dernier jour du trimestre, déposées jusqu'à
45 jours après. Il ne montre ni les ventes à découvert, ni les dérivés, ni le hors-US, ni le
cash. Un gérant peut avoir tout soldé le lendemain de la photo.
→ On ne publie donc JAMAIS « X détient Y » comme une recommandation. On publie ce qui a CHANGÉ
  entre deux photos, avec l'âge de la photo écrit à côté.

CE QUI A DÉCIDÉ DE LA LISTE. Vingt gérants, groupés par ce qu'ils INFORMENT — et le groupe
n'est pas décoratif, c'est lui qui dit comment lire le mouvement :
  · allocateur   — Berkshire, Markel : de la conviction à horizon d'années. Un achat pèse.
  · macro        — Bridgewater, Soros, Duquesne : leur book EST une opinion sur le régime.
  · activiste    — Pershing, Third Point, Elliott, Starboard : une entrée est un CATALYSEUR
                   daté, pas un avis de valorisation.
  · croissance   — Coatue, Tiger, Lone Pine, Whale Rock, Altimeter, Baillie Gifford : ceux qui
                   portent le thème IA que le desk surveille.
  · systematique — Renaissance, Two Sigma, AQR, Millennium, Citadel : du FLUX, jamais une
                   conviction. Leurs lignes tournent par construction ; les lire comme un avis
                   serait la faute type sur cet onglet.

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
TROIS PIÈGES MESURÉS EN CONSTRUISANT
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
1 · LA TABLE D'INFORMATION N'A PAS DE NOM NORMALISÉ. « form13fInfoTable.xml » chez les uns,
    « 56757.xml » chez Berkshire. La seule règle stable : c'est le .xml qui n'est PAS
    `primary_doc.xml` (lequel ne porte que l'en-tête du dépôt).
2 · LE MÊME ÉMETTEUR APPARAÎT SUR PLUSIEURS LIGNES. Berkshire déclare Ally Financial en CINQ
    lignes, découpées par pouvoir de gestion. Lire ligne à ligne publierait cinq positions
    distinctes sur le même titre. On agrège par CUSIP, toujours.
3 · UN CIK FAUX NE LÈVE RIEN. La résolution des gérants exige un dépôt 13F-HR de moins de
    200 jours ET une raison sociale qui ressemble au nom demandé — sans quoi « State Street »
    se résolvait vers « Bell Bank » et « Trian » vers une entité arrêtée en 2011.

Aucune dépendance nouvelle : `urllib` + `xml.etree`.
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
CACHE_DIR = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")
GERANTS = os.path.join(RACINE, "gerants_13f.json")
if not os.path.exists(GERANTS):
    GERANTS = os.path.expanduser("~/Desktop/scf-data/gerants_13f.json")
UNIVERS = os.path.join(ICI, "stock_universe.json")
SORTIE = os.path.join(CACHE_DIR, "gerants_13f_cache.json")
SORTIE_JS = os.path.join(CACHE_DIR, "gerants_13f_cache.js")

UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research"),
      "Accept-Encoding": "gzip, deflate"}
DEBIT = 0.11
TOP_POSITIONS = 40        # les positions publiées par gérant, les plus grosses d'abord
MOUV_MINI_USD = 25_000_000   # sous ce montant, un mouvement n'est pas un mouvement
FRAICHEUR_MAX_J = 200

_dernier = [0.0]


def _freine():
    d = time.time() - _dernier[0]
    if d < DEBIT:
        time.sleep(DEBIT - d)
    _dernier[0] = time.time()


def http(url, brut=False, essais=3):
    dernier = None
    for n in range(essais):
        _freine()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                d = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                import gzip
                d = gzip.decompress(d)
            elif enc == "deflate":
                import zlib
                d = zlib.decompress(d, -zlib.MAX_WBITS)
            return d if brut else json.loads(d.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            dernier = f"HTTP {e.code}"
            if e.code in (429, 403):
                time.sleep(2 * (n + 1))
        except Exception as e:
            dernier = f"{type(e).__name__}: {e}"
            time.sleep(1 + n)
    if dernier:
        print(f"[13f] échec {url[-58:]} — {dernier}", file=sys.stderr)
    return None


def _t(el, tag, ns):
    n = el.find(f"i:{tag}", ns) if ns else el.find(tag)
    if n is None:
        n = el.find(f".//i:{tag}", ns) if ns else el.find(f".//{tag}")
    return (n.text or "").strip() if n is not None and n.text else None


def positions(cik, acc):
    """Les positions d'un dépôt, AGRÉGÉES PAR CUSIP (cf. piège n°2 de l'en-tête)."""
    ix = http(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json")
    if not ix:
        return None
    noms = [x["name"] for x in (ix.get("directory") or {}).get("item") or []]
    tbl = next((n for n in noms
                if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"), None)
    if not tbl:
        return None
    raw = http(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{tbl}", brut=True)
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    ns = {"i": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    lignes = root.findall(".//i:infoTable", ns) if ns else root.findall(".//infoTable")

    par_cusip = {}
    for e in lignes:
        cusip = (_t(e, "cusip", ns) or "").upper()
        if not cusip:
            continue
        try:
            val = float(_t(e, "value", ns) or 0)
        except ValueError:
            val = 0.0
        try:
            titres = float(_t(e, "sshPrnamt", ns) or 0)
        except ValueError:
            titres = 0.0
        # ── LA CLASSE D'ACTIONS, SANS QUOI DEUX LIGNES SE RESSEMBLENT À TORT ─────────────────
        # Alphabet ressortait DEUX FOIS dans l'encombrement — 18 gérants d'un côté, 9 de
        # l'autre — sous le même nom « ALPHABET INC ». Ce ne sont pas des doublons : ce sont
        # les classes A et C, deux titres distincts avec deux CUSIP. L'agrégation par CUSIP
        # est donc juste ; c'est l'AFFICHAGE qui mentait en les nommant pareil.
        classe = _t(e, "titleOfClass", ns)
        nom = _t(e, "nameOfIssuer", ns)
        if classe and classe.upper() not in ("COM", "COMMON", "COMMON STOCK"):
            nom = f"{nom} ({classe})"
        p = par_cusip.setdefault(cusip, {"cusip": cusip, "nom": nom,
                                         "valeur": 0.0, "titres": 0.0, "lignes": 0})
        p["valeur"] += val
        p["titres"] += titres
        p["lignes"] += 1
    return par_cusip


def _norm(s):
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", (s or "").upper())
    for m in (" INC", " CORP", " CO", " LTD", " PLC", " CLASS A", " CL A", " COM",
              " HOLDINGS", " GROUP", " THE", " NV", " SA", " AG"):
        s = s.replace(m, " ")
    return re.sub(r"\s+", " ", s).strip()


def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    if not os.path.exists(GERANTS):
        print(f"[13f] table des gérants introuvable : {GERANTS}", file=sys.stderr)
        return 1
    table = json.load(open(GERANTS, encoding="utf-8"))

    # La table nom→ticker de l'univers, pour rattacher un émetteur du 13F à un titre suivi.
    tickers = {}
    try:
        u = json.load(open(UNIVERS, encoding="utf-8"))
        for zone in u.values():
            for e in (zone.get("pool") or []):
                if e.get("n"):
                    tickers[_norm(e["n"])] = (e.get("t") or "").upper()
    except Exception:
        pass

    aujourdhui = datetime.date.today()
    gerants, lacunes = [], []
    sans_depot, sans_precedent, perimes = [], [], []
    detentions = {}          # cusip -> {nom, gerants:[...], valeur_totale}

    for g in table.get("gerants", []):
        cik, nom = str(g.get("cik") or ""), g.get("nom")
        if not cik:
            continue
        sub = http(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
        if not sub:
            sans_depot.append(nom)
            continue
        rec = (sub.get("filings") or {}).get("recent") or {}
        formes = rec.get("form") or []
        idx = [i for i, f in enumerate(formes) if f == "13F-HR"]
        if not idx:
            sans_depot.append(nom)
            continue

        i0 = idx[0]
        age = (aujourdhui - datetime.date.fromisoformat(rec["filingDate"][i0])).days
        if age > FRAICHEUR_MAX_J:
            perimes.append(f"{nom} ({age} j)")
            continue

        cur = positions(cik, rec["accessionNumber"][i0].replace("-", ""))
        if not cur:
            sans_depot.append(nom)
            continue
        prev = None
        if len(idx) > 1:
            prev = positions(cik, rec["accessionNumber"][idx[1]].replace("-", ""))
        if prev is None:
            sans_precedent.append(nom)
            prev = {}

        # ── CE QUI A CHANGÉ, ET RIEN D'AUTRE ─────────────────────────────────────────────────
        entrees, sorties, renforts, allegements = [], [], [], []
        for cu, p in cur.items():
            av = prev.get(cu)
            d = p["valeur"] - (av["valeur"] if av else 0.0)
            if not av and p["valeur"] >= MOUV_MINI_USD:
                entrees.append({**p, "delta_usd": round(p["valeur"])})
            elif av and abs(d) >= MOUV_MINI_USD:
                (renforts if d > 0 else allegements).append({**p, "delta_usd": round(d)})
        for cu, av in prev.items():
            if cu not in cur and av["valeur"] >= MOUV_MINI_USD:
                sorties.append({**av, "delta_usd": -round(av["valeur"])})

        def orne(lst):
            for x in lst:
                x["ticker"] = tickers.get(_norm(x.get("nom")))
                x["valeur"] = round(x["valeur"])
                x.pop("titres", None)
            return sorted(lst, key=lambda x: -abs(x["delta_usd"]))[:12]

        top = sorted(cur.values(), key=lambda p: -p["valeur"])[:TOP_POSITIONS]
        total = sum(p["valeur"] for p in cur.values())
        for p in top:
            cle = p["cusip"]
            d = detentions.setdefault(cle, {"cusip": cle, "nom": p["nom"],
                                            "ticker": tickers.get(_norm(p["nom"])),
                                            "gerants": [], "valeur_totale": 0.0})
            d["gerants"].append(nom)
            d["valeur_totale"] += p["valeur"]

        gerants.append({
            "nom": nom, "groupe": g.get("groupe"), "cik": cik,
            "periode": rec["reportDate"][i0], "depose_le": rec["filingDate"][i0],
            "age_depot_j": age,
            "positions_totales": len(cur), "valeur_declaree_usd": round(total),
            "concentration_top10_pct": (round(100 * sum(p["valeur"] for p in top[:10]) / total, 1)
                                        if total else None),
            "entrees": orne(entrees), "sorties": orne(sorties),
            "renforts": orne(renforts), "allegements": orne(allegements),
            "top_positions": [{"nom": p["nom"], "cusip": p["cusip"],
                               "ticker": tickers.get(_norm(p["nom"])),
                               "valeur_usd": round(p["valeur"]),
                               "poids_pct": (round(100 * p["valeur"] / total, 2) if total else None)}
                              for p in top[:15]],
            "comparaison_possible": bool(prev),
        })

    # ── L'ENCOMBREMENT : combien de ces gérants tiennent le MÊME titre ────────────────────────
    # C'est la mesure que le desk n'a nulle part ailleurs. Un nom que quinze gérants tiennent
    # n'est pas une idée, c'est une position de consensus — et une sortie de porte étroite.
    encombrement = sorted(
        ({"cusip": d["cusip"], "nom": d["nom"], "ticker": d["ticker"],
          "n_gerants": len(d["gerants"]), "gerants": d["gerants"][:10],
          "valeur_cumulee_usd": round(d["valeur_totale"])}
         for d in detentions.values() if len(d["gerants"]) >= 2),
        key=lambda x: (-x["n_gerants"], -x["valeur_cumulee_usd"]))

    if table.get("introuvables"):
        lacunes.append("gérant(s) NON résolus, donc absents de toute mesure : "
                       + ", ".join(table["introuvables"])
                       + " — leur silence ici n'est pas une absence de mouvement")
    if sans_depot:
        lacunes.append(f"{len(sans_depot)} gérant(s) dont le dépôt n'a pas pu être lu : "
                       + ", ".join(sans_depot[:8]))
    if perimes:
        lacunes.append(f"{len(perimes)} gérant(s) dont le dernier 13F a plus de "
                       f"{FRAICHEUR_MAX_J} j — écartés : " + ", ".join(perimes[:6]))
    if sans_precedent:
        lacunes.append(f"{len(sans_precedent)} gérant(s) sans trimestre précédent lisible : "
                       f"leurs positions sont publiées mais AUCUN mouvement n'est calculable "
                       f"— " + ", ".join(sans_precedent[:6]))
    sans_ticker = sum(1 for d in detentions.values() if not d["ticker"])
    if sans_ticker:
        lacunes.append(f"{sans_ticker} émetteur(s) sur {len(detentions)} non rattachés à un "
                       f"ticker de l'univers : le rapprochement se fait par RAISON SOCIALE, et "
                       f"un émetteur hors univers ou nommé autrement reste sans code")
    lacunes.append(f"un 13F est une PHOTO au {gerants[0]['periode'] if gerants else '?'}, "
                   f"déposée jusqu'à 45 jours après : le gérant a pu tout solder depuis. "
                   f"Il ne montre ni ventes à découvert, ni dérivés, ni hors-US, ni cash")
    lacunes.append(f"les mouvements sous {MOUV_MINI_USD:,} $ ne sont pas publiés, et les "
                   f"positions au-delà du rang {TOP_POSITIONS} par gérant non plus — les "
                   f"totaux, eux, portent sur l'intégralité du dépôt"
                   .replace(",", " "))

    sortie = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR — formulaires 13F-HR (positions longues en actions US)",
        "duree_s": round(time.time() - t0, 1),
        "seuil_mouvement_usd": MOUV_MINI_USD,
        "exhaustivite": {
            "gerants_declares": len(table.get("gerants") or []),
            "gerants_lus": len(gerants),
            "gerants_sans_depot": len(sans_depot),
            "gerants_perimes": len(perimes),
            "emetteurs_distincts": len(detentions),
            "emetteurs_partages": len(encombrement),
            "top_positions_par_gerant": TOP_POSITIONS,
        },
        "lacunes": lacunes,
        "gerants": gerants,
        "encombrement": encombrement[:60],
    }

    for chemin, prefixe in ((SORTIE, None), (SORTIE_JS, "window.__GERANTS_13F__=")):
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if prefixe:
                f.write(prefixe)
            json.dump(sortie, f, ensure_ascii=False, separators=(",", ":"))
            if prefixe:
                f.write(";")
        os.replace(tmp, chemin)

    n_mouv = sum(len(g["entrees"]) + len(g["sorties"]) + len(g["renforts"])
                 + len(g["allegements"]) for g in gerants)
    print(f"[13f] {len(gerants)} gérant(s) lu(s) sur {len(table.get('gerants') or [])} — "
          f"{n_mouv} mouvement(s) au-dessus du seuil, {len(encombrement)} émetteur(s) tenus "
          f"par au moins deux gérants — {sortie['duree_s']} s")
    for l in lacunes[:3]:
        print(f"[13f] lacune : {l[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
