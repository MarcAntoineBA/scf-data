#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_atlas_budget.py — Répartition du budget de l'État par fonction (COFOG)
pour l'onglet Atlas Économique. Source UNIQUE : FMI, dataflow GFS_COFOG
(« Government Expenditures by Function »), ~183 pays, 3 unités :
  POGDP_PT = % du PIB · POTO_PT = % du budget total · XDC = valeur en monnaie nationale.
Produit `atlas_budget_cache.{json,js}` (chargé en LAZY par le front à la 1re ouverture
du bloc « Budget de l'État »). 10 fonctions principales + sous-fonctions niveau 2.

Clé SDMX : COUNTRY.SECTOR(S13).GFS_GRP(G2MF).INDICATOR(GFxx_T).TYPE_OF_TRANSFORMATION.FREQUENCY(A)

Copies : Desktop (source) + public/ + ~/Library/Caches/site_crypto_finance/ (json+js).
Auto : launchd (mêmes cadences que atlasdetail). Flags : --only FRA,USA  --limit N
"""
import os
import sys, os, re, json, time, argparse

try:
    from curl_cffi import requests as rq
    def _sess():
        return rq.Session(impersonate="chrome120", timeout=60)
except Exception:
    import requests as rq
    def _sess():
        s = rq.Session(); s.headers.update({"User-Agent": "Mozilla/5.0"}); return s

BASE = "https://api.imf.org/external/sdmx/2.1/data/IMF.STA,GFS_COFOG"
HOME = os.path.expanduser("~")
# repo Desktop = destination servie (:8000) + public ; chemins ABSOLUS pour survivre au
# lancement depuis Application Support (launchd), pas relatifs au script.
REPO = os.path.join(HOME, "Desktop", "Site_Crypto_Finance")
CACHES = [
    os.path.join(REPO, "atlas_budget_cache"),
    os.path.join(REPO, "public", "atlas_budget_cache"),
    os.path.join(HOME, "Library", "Caches", "site_crypto_finance", "atlas_budget_cache"),
]

# libellés FR des 10 fonctions principales (COFOG — classification internationale figée)
COFOG_FR = {
    "01": "Services publics généraux", "02": "Défense", "03": "Ordre et sécurité publics",
    "04": "Affaires économiques", "05": "Protection de l'environnement",
    "06": "Logement et équipements collectifs", "07": "Santé",
    "08": "Loisirs, culture et culte", "09": "Éducation", "10": "Protection sociale",
}
# libellés FR des sous-fonctions niveau 2 (les plus courantes ; repli = libellé EN nettoyé)
SUB_FR = {
    "011": "Organes exécutifs & législatifs, aff. financières", "012": "Aide économique extérieure",
    "013": "Services généraux", "014": "Recherche fondamentale", "015": "R&D services généraux",
    "016": "Services publics généraux n.c.a.", "017": "Opérations sur la dette publique",
    "018": "Transferts entre administrations",
    "021": "Défense militaire", "022": "Défense civile", "023": "Aide militaire extérieure",
    "024": "R&D défense", "025": "Défense n.c.a.",
    "031": "Services de police", "032": "Services de protection civile (incendie)",
    "033": "Tribunaux", "034": "Administration pénitentiaire", "035": "R&D ordre & sécurité",
    "036": "Ordre & sécurité n.c.a.",
    "041": "Affaires générales économiques & de l'emploi", "042": "Agriculture, sylviculture, pêche",
    "043": "Combustibles & énergie", "044": "Industries extractives & manufacturières",
    "045": "Transports", "046": "Communications", "047": "Autres branches",
    "048": "R&D affaires économiques", "049": "Affaires économiques n.c.a.",
    "051": "Gestion des déchets", "052": "Gestion des eaux usées", "053": "Lutte contre la pollution",
    "054": "Protection de la biodiversité", "055": "R&D environnement", "056": "Environnement n.c.a.",
    "061": "Logement", "062": "Équipements collectifs", "063": "Alimentation en eau",
    "064": "Éclairage public", "065": "R&D logement", "066": "Logement n.c.a.",
    "071": "Produits, appareils & équipements médicaux", "072": "Services ambulatoires",
    "073": "Services hospitaliers", "074": "Services de santé publique",
    "075": "R&D santé", "076": "Santé n.c.a.",
    "081": "Services récréatifs & sportifs", "082": "Services culturels",
    "083": "Radiodiffusion & édition", "084": "Culte & autres services communautaires",
    "085": "R&D loisirs & culture", "086": "Loisirs, culture & culte n.c.a.",
    "091": "Enseignement primaire & préprimaire", "092": "Enseignement secondaire",
    "093": "Enseignement post-secondaire non supérieur", "094": "Enseignement supérieur",
    "095": "Enseignement non défini par niveau", "096": "Services annexes à l'éducation",
    "097": "R&D éducation", "098": "Éducation n.c.a.",
    "101": "Maladie & invalidité", "102": "Vieillesse", "103": "Survivants",
    "104": "Famille & enfants", "105": "Chômage", "106": "Logement social",
    "107": "Exclusion sociale n.c.a.", "108": "R&D protection sociale", "109": "Protection sociale n.c.a.",
}


def sig4(x):
    """Arrondi 4 chiffres significatifs (valeurs monétaires) ; petits nombres -> 2 déc."""
    import math
    if x is None:
        return None
    x = float(x)
    if x == 0:
        return 0
    if abs(x) < 100:
        return round(x, 2)
    r = round(x, -int(math.floor(math.log10(abs(x)))) + 3)
    return int(r) if abs(r) >= 1000 or r == int(r) else r


def r2(x):
    if x is None:
        return None
    r = round(float(x), 2)
    return int(r) if r == int(r) else r


def pack(ym, rnd):
    """{année:val} -> {'s':première,'v':[...]} (trous=null, bords trimmés). None si <1 pt."""
    if not ym:
        return None
    y0, y1 = min(ym), max(ym)
    v = [(rnd(ym[y]) if y in ym and ym[y] is not None else None) for y in range(y0, y1 + 1)]
    if not any(x is not None for x in v):
        return None
    return {"s": y0, "v": v}


def load_atlas_countries():
    """ISO3 -> currency depuis assets/js/atlas/atlas_countries_meta.js (best-effort)."""
    cands = [os.path.join(REPO, "assets", "js", "atlas", "atlas_countries_meta.js"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "js", "atlas", "atlas_countries_meta.js")]
    path = next((p for p in cands if os.path.exists(p)), cands[0])
    cur = {}
    try:
        t = open(path, encoding="utf-8").read()
        # pour chaque "FRA":{  chercher "ccy":"EUR" dans la fenêtre qui suit (gère les {} imbriqués)
        for m in re.finditer(r'"([A-Z]{3})"\s*:\s*\{', t):
            a3 = m.group(1)
            win = t[m.end():m.end() + 500]
            cm = re.search(r'"(?:ccy|cur|currency|devise)"\s*:\s*"([^"]+)"', win)
            if cm:
                cur[a3] = cm.group(1)
    except Exception as e:
        sys.stderr.write(f"[WARN] meta devises illisible: {e}\n")
    return cur


def fetch_sector(sess, sector, start=2007):
    """UNE requête en masse par unité pour un secteur (S13=toutes admin, S1311=État central).
    Renvoie {a3: {(cofog_sans_T, unit): {année:val}}}."""
    raw = {}
    for unit in ("POGDP_PT", "POTO_PT", "XDC"):
        url = f"{BASE}/.{sector}.G2MF..{unit}.A?startPeriod={start}&format=sdmx-2.1"
        txt = None
        for att in range(4):
            try:
                r = sess.get(url, timeout=180)
                if r.status_code == 200 and "<Series" in r.text:
                    txt = r.text
                    break
            except Exception:
                time.sleep(3 + att * 3)
        if not txt:
            sys.stderr.write(f"[WARN] {sector}/{unit} : échec du fetch en masse\n")
            continue
        n = 0
        for sm in re.finditer(r'<Series\s+([^>]+)>(.*?)</Series>', txt, re.S):
            a = dict(re.findall(r'(\w+)="([^"]+)"', sm.group(1)))
            c = a.get("COUNTRY", "")
            m = re.fullmatch(r'GF(\d+)_T', a.get("INDICATOR", ""))
            if not (c and m):
                continue
            code = m.group(1)  # "01" (fonction) ou "071" (sous-fonction niv.2)
            ym = {}
            for om in re.finditer(r'TIME_PERIOD="(\d+)"\s+OBS_VALUE="([-0-9.eE]+)"', sm.group(2)):
                try:
                    ym[int(om.group(1))] = float(om.group(2))
                except ValueError:
                    pass
            if ym:
                raw.setdefault(c, {})[(code, unit)] = ym
                n += 1
        print(f"[GFS_COFOG] {sector}/{unit} : {n} séries, {len(raw)} pays cumulés")
    return raw


def fetch_all(sess, start=2007):
    """Deux périmètres : S13 (administrations publiques) + S1311 (État central budgétaire)."""
    return {"gg": fetch_sector(sess, "S13", start),
            "cg": fetch_sector(sess, "S1311", start)}


def assemble_country(raw):
    """{(code,unit):{année:val}} d'un pays -> entrée budget, ou None si vide."""
    if not raw:
        return None
    funcs = {}
    latest = 0
    for fc in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
        g = raw.get((fc, "POGDP_PT")); t = raw.get((fc, "POTO_PT")); x = raw.get((fc, "XDC"))
        if not (g or t or x):
            continue
        entry = {}
        pg = pack(g, r2);  pt = pack(t, r2);  px = pack(x, sig4)
        if pg: entry["g"] = pg
        if pt: entry["t"] = pt
        if px: entry["x"] = px
        if not entry:
            continue
        for ser in (g, t, x):
            if ser:
                latest = max(latest, max(ser))
        # sous-postes niveau 2 (ex GF017 = « Opérations sur la dette publique »).
        # ⚠ le FMI code certains sous-postes en niveau 3 à 4 chiffres finissant par 0
        #   (GF0170 = GF017) → on ROLL-UP les codes 3-ET-4 chiffres vers la clé niveau-2.
        buckets = {}   # "017" -> [codes bruts]
        for code in set(k[0] for k in raw if k[0].startswith(fc) and len(k[0]) in (3, 4)):
            buckets.setdefault(code[:3], []).append(code)

        def sub_series(key2, codes, unit):
            """Série niveau-2 : le total direct s'il existe, sinon somme des enfants niveau-3."""
            if key2 in codes and (key2, unit) in raw:
                return raw[(key2, unit)]
            childs = [raw[(c, unit)] for c in codes if len(c) == 4 and (c, unit) in raw]
            if not childs:
                return raw.get((key2, unit))
            merged = {}
            for ym in childs:
                for y, v in ym.items():
                    merged[y] = merged.get(y, 0) + v
            return merged or None

        subs = []
        for key2 in sorted(buckets):
            codes = buckets[key2]
            st = sub_series(key2, codes, "POTO_PT")
            sx = sub_series(key2, codes, "XDC")
            sg = sub_series(key2, codes, "POGDP_PT")
            sub = {"c": key2, "l": SUB_FR.get(key2) or key2}
            spt = pack(st, r2); spx = pack(sx, sig4); spg = pack(sg, r2)
            if spt: sub["t"] = spt
            if spg: sub["g"] = spg
            if spx: sub["x"] = spx
            if len(sub) > 2:
                subs.append(sub)
        if subs:
            entry["s"] = subs
        funcs[fc] = entry
    if not funcs:
        return None
    return {"ly": latest, "f": funcs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cur = load_atlas_countries()
    atlas_set = set(cur.keys()) if cur else None
    only = set(x.strip().upper() for x in args.only.split(",")) if args.only else None

    sess = _sess()
    raw = fetch_all(sess)       # {"gg":{a3:{(code,unit):ym}}, "cg":{...}}
    gg_raw, cg_raw = raw["gg"], raw["cg"]
    countries, n_ok, n_cg = {}, 0, 0
    for a3 in sorted(set(gg_raw) | set(cg_raw)):
        if len(a3) != 3 or not a3.isalpha():
            continue            # vrais pays ISO3 seulement (pas agrégats/zones)
        if only and a3 not in only:
            continue
        gg = assemble_country(gg_raw.get(a3, {}))
        if not gg or len(gg["f"]) < 5:
            continue            # seuil qualité : camembert exploitable (≥5 fonctions)
        e = {"gg": gg}
        cg = assemble_country(cg_raw.get(a3, {}))
        if cg and len(cg["f"]) >= 5:
            e["cg"] = cg; n_cg += 1
        if a3 in cur:
            e["cur"] = cur[a3]
        countries[a3] = e
        n_ok += 1
        print(f"{a3}: gg {len(gg['f'])} fct ({gg['ly']})"
              + (f" · cg {len(cg['f'])} fct ({cg['ly']})" if "cg" in e else " · cg —")
              + ("" if a3 in cur else "  [devise ?]"))

    out = {
        "meta": {
            "source": "FMI · Government Finance Statistics (COFOG)",
            "url": "https://data.imf.org",
            "dataflow": "IMF.STA:GFS_COFOG",
            "sector": "Administrations publiques (S13)",
            "n_countries": n_ok,
            "n_central": n_cg,
            "perimeters": {"gg": "Administrations publiques (S13)", "cg": "État central budgétaire (S1311)"},
            "cofog_fr": COFOG_FR,
        },
        "countries": countries,
    }
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    hdr = "/* atlas_budget_cache.js — FMI GFS COFOG — répartition budgétaire par fonction */"
    for base in CACHES:
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            with open(base + ".json", "w", encoding="utf-8") as f:
                f.write(blob)
            with open(base + ".js", "w", encoding="utf-8") as f:
                f.write(hdr + "\nwindow.__ATLAS_BUDGET__ = " + blob + ";\n")
            print(f"écrit {base}.json ({len(blob)//1024} Ko)")
        except Exception as e:
            sys.stderr.write(f"[WARN] écriture {base}: {e}\n")
    print(f"OK — {n_ok} pays (toutes admin) dont {n_cg} avec État central (COFOG).")


if __name__ == "__main__":
    main()
