#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_atlas_france_budget.py — Budget de l'État français (Phase 1 de « Finances France »).
Source OFFICIELLE et AUDITABLE : Ministère de l'Économie et des Finances, portail
data.economie.gouv.fr (Opendatasoft). Chaque chiffre = dataset nommé + année + horodatage + lien.

  • Dépenses du budget général en drill-down Ministère→Mission→Programme→Action (CP + AE).
  • Recettes du budget général par type et ligne.
  • Évolution des missions (plusieurs millésimes, best-effort selon millésimes publiés).

Produit `atlas_france_budget.{json,js}` (window.__ATLAS_FR_BUDGET__), chargé en lazy.
Copies Desktop + public/ + Library/Caches. Auto : launchd (annuel/mensuel).
"""
import os
import sys, os, json, time
from datetime import datetime, timezone

try:
    from curl_cffi import requests as rq
    def _sess():
        return rq.Session(impersonate="chrome120", timeout=120)
except Exception:
    import requests as rq
    def _sess():
        s = rq.Session(); s.headers.update({"User-Agent": "Mozilla/5.0"}); return s

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "Desktop", "Site_Crypto_Finance")
CACHES = [
    os.path.join(REPO, "atlas_france_budget"),
    os.path.join(REPO, "public", "atlas_france_budget"),
    os.path.join(HOME, "Library", "Caches", "site_crypto_finance", "atlas_france_budget"),
]
PORTAL = "https://data.economie.gouv.fr"

# Dépenses par destination (Mission→Programme→Action), par millésime publié.
# ⚠ SCHÉMAS HÉTÉROGÈNES entre années → noms de champs explicites par millésime.
# Codes de mission LOLF stables d'une année à l'autre (45/46 communs) → évolution par CODE.
DEPENSES = {
    2025: dict(ds="plf25-depenses-2025-selon-destination", loi="PLF 2025",
               mcode="mission", mlib="libelle_mission", pcode="programme", plib="libelle_programme",
               acode="action", alib="libelle_action", sacode="sous_action", salib="libelle_sous_action",
               minc="ministere", minl="libelle_ministere", titre="titre",
               cp="credit_de_paiement", ae="autorisation_engagement", bg="typebudget"),
    2024: dict(ds="plf-2024-depenses-2024-selon-nomenclatures-destination-et-nature", loi="PLF 2024",
               mcode="code_mission", mlib="mission", pcode="programme", plib="libelle_programme",
               acode="action", alib="libelle_action", cp="cp_plf", ae="ae_plf", bg="type_mission"),
    2023: dict(ds="plf-2023-credits_destination_nature", loi="PLF 2023",
               mcode="mission", mlib=None, pcode="programme", plib=None,
               acode="action", alib=None, cp="cp", ae="ae", bg="typebudget"),
}
RECETTES_DS = "plf25-recettes-du-budget-general"
RECETTES_YEAR = 2025


def ods_export(sess, ds):
    """Tous les enregistrements d'un dataset ODS via l'API exports/json."""
    for att in range(4):
        try:
            r = sess.get(f"{PORTAL}/api/explore/v2.1/catalog/datasets/{ds}/exports/json?limit=-1", timeout=150)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(3 + att * 3)
    sys.stderr.write(f"[WARN] export {ds} échec\n")
    return None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def pick(d, *names):
    """Premier champ non vide parmi plusieurs noms possibles (schémas hétérogènes)."""
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def is_bg(d, conf):
    v = d.get(conf["bg"])
    return v in ("BG", None, "")


def build_tree(records, conf):
    """Records dépenses (BG) -> arbre Mission→Programme→Action, CP+AE cumulés."""
    missions = {}
    tot_cp = tot_ae = 0.0
    for d in records:
        if not is_bg(d, conf):
            continue
        m_lib = d.get(conf["mlib"]) if conf["mlib"] else None
        p_lib = d.get(conf["plib"]) if conf["plib"] else None
        a_lib = d.get(conf["alib"]) if conf["alib"] else None
        m_code = str(d.get(conf["mcode"]) or "")
        p_code = str(d.get(conf["pcode"]) or "")
        a_code = str(d.get(conf["acode"]) or "")
        m_lib = m_lib or m_code
        cp = num(d.get(conf["cp"])); ae = num(d.get(conf["ae"]))
        if not m_lib:
            continue
        tot_cp += cp; tot_ae += ae
        M = missions.setdefault(m_lib, {"code": m_code, "lib": m_lib, "cp": 0.0, "ae": 0.0, "programmes": {}})
        M["cp"] += cp; M["ae"] += ae
        if p_lib:
            P = M["programmes"].setdefault(p_lib, {"code": p_code, "lib": p_lib, "cp": 0.0, "ae": 0.0, "actions": {}})
            P["cp"] += cp; P["ae"] += ae
            if a_lib:
                A = P["actions"].setdefault(a_lib, {"code": a_code, "lib": a_lib, "cp": 0.0, "ae": 0.0})
                A["cp"] += cp; A["ae"] += ae
    # dict -> listes triées par CP décroissant, arrondi
    def rnd(x):
        return round(x, 0)
    out = []
    for M in sorted(missions.values(), key=lambda m: -m["cp"]):
        progs = []
        for P in sorted(M["programmes"].values(), key=lambda p: -p["cp"]):
            acts = [{"code": A["code"], "lib": A["lib"], "cp": rnd(A["cp"]), "ae": rnd(A["ae"])}
                    for A in sorted(P["actions"].values(), key=lambda a: -a["cp"])]
            progs.append({"code": P["code"], "lib": P["lib"], "cp": rnd(P["cp"]), "ae": rnd(P["ae"]), "actions": acts})
        out.append({"code": M["code"], "lib": M["lib"], "cp": rnd(M["cp"]), "ae": rnd(M["ae"]), "programmes": progs})
    return out, tot_cp, tot_ae


# Nature de la dépense (titre LOLF)
TITRE_FR = {"1": "Dotations des pouvoirs publics", "2": "Personnel", "3": "Fonctionnement",
            "4": "Charge de la dette", "5": "Investissement",
            "6": "Intervention (aides & transferts)", "7": "Opérations financières"}
TYPEBUDGET_FR = {"BG": "Budget général", "CAS": "Comptes d'affectation spéciale",
                 "CCF": "Comptes de concours financiers", "BA": "Budgets annexes"}


def _rnd0(x):
    return round(x, 0)


def build_generic(records, levels, cpf, aef):
    """Arbre hiérarchique générique. `levels` = liste de fonctions (rec)->(code,lib) par niveau.
    Renvoie (liste triée d'arbres {code,lib,cp,ae,children?}, total_cp, total_ae)."""
    root = {"cp": 0.0, "ae": 0.0, "kids": {}}
    for d in records:
        cp = num(d.get(cpf)); ae = num(d.get(aef))
        if cp == 0 and ae == 0:
            continue
        node = root
        node["cp"] += cp; node["ae"] += ae
        for lv in levels:
            code, lib = lv(d)
            if not lib:
                break   # ligne sans ce niveau : reste agrégée au niveau parent
            ch = node["kids"].setdefault(lib, {"code": str(code or ""), "lib": lib, "cp": 0.0, "ae": 0.0, "kids": {}})
            ch["cp"] += cp; ch["ae"] += ae
            node = ch

    def emit(node):
        out = []
        for ch in sorted(node["kids"].values(), key=lambda x: -x["cp"]):
            e = {"code": ch["code"], "lib": ch["lib"], "cp": _rnd0(ch["cp"]), "ae": _rnd0(ch["ae"])}
            if ch["kids"]:
                e["children"] = emit(ch)
            out.append(e)
        return out
    return emit(root), root["cp"], root["ae"]


def build_axes(records, conf):
    """3 axes d'analyse (destination / nature / ministère) sur un jeu de records."""
    cpf, aef = conf["cp"], conf["ae"]
    def f(codeK, libK):
        return lambda d: (d.get(codeK), d.get(libK))
    dest_levels = [f(conf["mcode"], conf["mlib"]), f(conf["pcode"], conf["plib"]),
                   f(conf["acode"], conf["alib"]), f(conf["sacode"], conf["salib"])]
    nat_levels = [lambda d: (d.get(conf["titre"]), TITRE_FR.get(str(d.get(conf["titre"])), "Autre nature")),
                  f(conf["mcode"], conf["mlib"])]
    min_levels = [f(conf["minc"], conf["minl"]), f(conf["mcode"], conf["mlib"]), f(conf["pcode"], conf["plib"])]
    dest, tcp, tae = build_generic(records, dest_levels, cpf, aef)
    nat, _, _ = build_generic(records, nat_levels, cpf, aef)
    mins, _, _ = build_generic(records, min_levels, cpf, aef)
    return {"destination": dest, "nature": nat, "ministere": mins}, tcp, tae


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dataset_url(ds):
    return f"{PORTAL}/explore/dataset/{ds}/information/"


def main():
    sess = _sess()

    # ── DÉPENSES (millésime le plus récent = arbre complet) ──
    year = max(DEPENSES)
    conf = DEPENSES[year]
    print(f"→ Dépenses de l'État {year} ({conf['ds']})…")
    recs = ods_export(sess, conf["ds"]) or []
    bg_recs = [d for d in recs if d.get(conf["bg"]) == "BG"]
    # 3 axes × 2 périmètres (budget général / tous périmètres)
    axes_bg, tot_cp, tot_ae = build_axes(bg_recs, conf)
    axes_all, tot_cp_all, tot_ae_all = build_axes(recs, conf)
    tree = axes_bg["destination"]   # arbre destination BG (pour évolution + Sankey)
    code2lib = {m["code"]: m["lib"] for m in tree if m["code"]}
    # totaux par périmètre (typebudget)
    perim = {}
    for d in recs:
        tb = d.get(conf["bg"]) or "?"
        perim.setdefault(tb, 0.0)
        perim[tb] += num(d.get(conf["cp"]))
    perim_out = [{"code": k, "lib": TYPEBUDGET_FR.get(k, k), "cp": round(v, 0)}
                 for k, v in sorted(perim.items(), key=lambda x: -x[1])]
    print(f"   destination BG {len(tree)} missions · CP {tot_cp/1e9:.1f} Md€ · tous périmètres {tot_cp_all/1e9:.1f} Md€")
    print(f"   axes : destination(4 niv) · nature({len(axes_bg['nature'])}) · ministère({len(axes_bg['ministere'])})")

    # ── ÉVOLUTION missions (CP) par CODE de mission (stable LOLF) ──
    evo = {}   # code -> {année: cp}
    for y, c in sorted(DEPENSES.items()):
        data_y = recs if y == year else (ods_export(sess, c["ds"]) or [])
        if y != year:
            print(f"→ Évolution {y} ({c['ds']})…")
        for d in data_y:
            if not is_bg(d, c):
                continue
            code = str(d.get(c["mcode"]) or "")
            if not code:
                continue
            evo.setdefault(code, {}).setdefault(str(y), 0.0)
            evo[code][str(y)] += num(d.get(c["cp"]))
    evolution = {}
    for code, yrs in evo.items():
        lib = code2lib.get(code)
        if not lib:
            continue   # code absent du millésime courant → ignoré
        evolution[lib] = {y: round(v, 0) for y, v in yrs.items()}

    # ── RECETTES ──
    print(f"→ Recettes de l'État {RECETTES_YEAR} ({RECETTES_DS})…")
    rrecs = ods_export(sess, RECETTES_DS) or []
    rtypes = {}
    rtot = 0.0
    for d in rrecs:
        typ = pick(d, "type_de_recettes", "type_recettes", "type") or "Autres"
        lib = pick(d, "libelle", "libelle_ligne", "intitule") or ""
        code = str(pick(d, "code_ligne_recettes", "code_ligne", "ligne") or "")
        montant = num(pick(d, "montant_recettes_plf", "montant_recettes_lfi", "montant", "montant_recettes"))
        if montant == 0:
            continue
        rtot += montant
        T = rtypes.setdefault(typ, {"type": typ, "total": 0.0, "lignes": []})
        T["total"] += montant
        T["lignes"].append({"code": code, "lib": lib, "montant": round(montant, 0)})
    rec_out = []
    for T in sorted(rtypes.values(), key=lambda t: -t["total"]):
        T["lignes"].sort(key=lambda x: -x["montant"])
        rec_out.append({"type": T["type"], "total": round(T["total"], 0), "lignes": T["lignes"][:40]})
    print(f"   recettes total {rtot/1e9:.1f} Md€ · {len(rec_out)} types")

    out = {
        "meta": {
            "generated": now_iso(),
            "source_name": "Ministère de l'Économie et des Finances (data.economie.gouv.fr)",
            "portal": PORTAL,
            "note_rd": "« Remboursements et dégrèvements » (~147 Md€) sont des restitutions d'impôts (non une dépense discrétionnaire) : ils gonflent le total brut. Budget net ≈ total − R&D.",
        },
        "depenses": {
            "year": year, "loi": conf["loi"], "unit": "CP — crédits de paiement (€)",
            "dataset": conf["ds"], "dataset_url": dataset_url(conf["ds"]),
            "fetched": now_iso(),
            "total_cp": round(tot_cp, 0), "total_ae": round(tot_ae, 0),
            "total_cp_all": round(tot_cp_all, 0),
            "missions": tree,   # rétro-compat (Sankey) = destination BG
            "axes": {"bg": axes_bg, "all": axes_all},
            "axes_meta": {"destination": "Par mission → programme → action → sous-action",
                          "nature": "Par nature (titre LOLF : personnel, fonctionnement…)",
                          "ministere": "Par ministère → mission → programme"},
            "perimetres": perim_out,
        },
        "recettes": {
            "year": RECETTES_YEAR, "dataset": RECETTES_DS, "dataset_url": dataset_url(RECETTES_DS),
            "fetched": now_iso(), "total": round(rtot, 0), "types": rec_out,
        },
        "evolution": {"unit": "CP (€)", "years": sorted(str(y) for y in DEPENSES),
                      "datasets": {str(y): DEPENSES[y]["ds"] for y in DEPENSES}, "missions": evolution},
    }
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    hdr = "/* atlas_france_budget.js — Budget de l'État (data.economie.gouv.fr) */"
    for base in CACHES:
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            open(base + ".json", "w", encoding="utf-8").write(blob)
            open(base + ".js", "w", encoding="utf-8").write(hdr + "\nwindow.__ATLAS_FR_BUDGET__ = " + blob + ";\n")
            print(f"écrit {base}.json ({len(blob)//1024} Ko)")
        except Exception as e:
            sys.stderr.write(f"[WARN] écriture {base}: {e}\n")
    print("OK — budget de l'État France.")


if __name__ == "__main__":
    main()
