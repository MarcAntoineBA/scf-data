#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MARCHÉ OBLIGATAIRE — collecteur du sous-onglet « Obligations » de l'Analyse
fondamentale (obligations.js côté site). Toutes les 6 heures.

ÉCRIT (dans ~/Library/Caches/site_crypto_finance, ou $SCF_OBLIG_OUT) :
  obligations.json / .js   la synthèse (window.__OBLIGATIONS__) : le marché, le
                           tableau des taux, le crédit, l'en-tête de chaque fiche.
  oblig_<pays>.json        le détail d'un pays : chaque échéance, sa courbe, ses
                           écarts, ses finances publiques, ses détenteurs, sa note.
  oblig_credit.json        le détail des cinq segments de crédit d'entreprise.
  oblig_vue.json           les séries longues de la vue d'ensemble.

MODULES : oblig_net (accès), oblig_marche (BIS), oblig_souverains (taux),
oblig_credit (entreprises), oblig_pays (fondamentaux).

⚠ Une source qui tombe ne vide jamais une fiche : on reprend le détail du
  passage précédent, marqué « repris du … » (`reprise_du`).
⚠ Les séries sont écrites en ÉCARTS DE JOURS (`d0` + `dj`) : 60 ans de taux
  américains tiennent en 120 Ko au lieu d'un mégaoctet (le dépôt de collecte
  publie en pièce jointe, et sert moins bien, tout fichier de plus d'1 Mo).
"""
import json
import os
import sys
import time
from datetime import date, datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import oblig_marche  # noqa: E402
import oblig_souverains  # noqa: E402
import oblig_credit  # noqa: E402
import oblig_pays  # noqa: E402
import oblig_frais  # noqa: E402
from oblig_net import log, compacter, mensuel, hebdo  # noqa: E402

CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
OUT_DIR = os.environ.get("SCF_OBLIG_OUT") or CACHE_DIR
GRAVURE = "assets/fonda/obligataire-tete.webp?v=1"


def lire_json(nom):
    for d in (OUT_DIR, CACHE_DIR):
        p = os.path.join(d, nom)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001
                pass
    return None


def ecrire(nom, obj, js_global=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    tmp = os.path.join(OUT_DIR, nom + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(s)
    os.replace(tmp, os.path.join(OUT_DIR, nom))
    if js_global:
        nj = nom.replace(".json", ".js")
        tmp = os.path.join(OUT_DIR, nj + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("window.%s=%s;\n" % (js_global, s))
        os.replace(tmp, os.path.join(OUT_DIR, nj))
    return len(s)


# ── Séries compactes ────────────────────────────────────────────────────────
def compacte(col, dec=3):
    """{"d": [...], "v": [...]} → {"d0": "AAAA-MM-JJ", "dj": [écarts en jours], "v": [...]}."""
    if not col or not col.get("d"):
        return None
    d = col["d"]
    if len(d[0]) == 7:            # mensuel « AAAA-MM »
        d = [x + "-15" for x in d]
    o = [date.fromisoformat(x[:10]).toordinal() for x in d]
    out = {"d0": d[0][:10], "dj": [0] + [o[i] - o[i - 1] for i in range(1, len(o))], "v": col["v"]}
    for k, v in col.items():
        if k not in ("d", "v"):
            out[k] = v
    return out


def compacter_arbre(x):
    """Remplace, partout dans l'objet, les séries {"d", "v"} par leur forme compacte."""
    if isinstance(x, dict):
        if "d" in x and "v" in x and isinstance(x["d"], list) and isinstance(x["v"], list) and x["d"] and isinstance(x["d"][0], str) \
                and len(x["d"]) == len(x["v"]) and len(x["d"]) > 12:
            return compacte(x)
        return {k: compacter_arbre(v) for k, v in x.items()}
    if isinstance(x, list):
        return [compacter_arbre(v) for v in x]
    return x


def nettoyer(x):
    """Les NaN/inf ne passent pas en JSON strict : remplacés par null."""
    if isinstance(x, float):
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    if isinstance(x, dict):
        return {str(k): nettoyer(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [nettoyer(v) for v in x]
    return x


def serie_vue(det, cle="10"):
    """Le 10 ans d'un pays pour la vue d'ensemble : hebdomadaire depuis 1990,
    raccordé au mensuel long quand le quotidien commence tard."""
    m = (det.get("maturites") or {}).get(cle) or {}
    s = m.get("serie") or {}
    pts = list(zip(s.get("d", []), s.get("v", [])))
    lg = det.get("long10") or {}
    lpts = list(zip(lg.get("d", []), lg.get("v", [])))
    if lpts and (not pts or pts[0][0] > "1991-01-01"):
        premier = pts[0][0] if pts else "9999"
        pts = [p for p in lpts if p[0] < premier] + pts
    pts = [p for p in pts if p[0] >= "1990-01-01"]
    if not pts:
        return None
    mens = det.get("mensuel")
    red = pts if mens else hebdo(pts)
    return {"d": [p[0] for p in red], "v": [p[1] for p in red], "mensuel": bool(mens)}


def main():
    t0 = time.time()
    journal = []
    maintenant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prec_synth = lire_json("obligations.json") or {}

    # ── 1. le marché (BIS) ──
    marche = None
    try:
        marche = oblig_marche.construire(journal)
    except Exception as e:  # noqa: BLE001
        journal.append("marché : " + str(e)[:160])
    if not marche and prec_synth.get("marche"):
        marche = dict(prec_synth["marche"], reprise_du=(prec_synth.get("genere_le") or "")[:10])
        journal.append("marché : repris du passage précédent")
    log("[info] marché : %.0f s" % (time.time() - t0))

    # ── 2. les taux d'État ──
    precedent = {}
    for p in oblig_souverains.PAYS:
        d = lire_json("oblig_%s.json" % p["code"])
        if d:
            precedent[p["code"]] = d
    souv, det_souv, fiches = oblig_souverains.construire(journal, precedent)
    log("[info] souverains : %.0f s" % (time.time() - t0))

    # ── 3. les fondamentaux des États ──
    pays = {}
    try:
        pays = oblig_pays.construire(journal)
    except Exception as e:  # noqa: BLE001
        journal.append("fondamentaux : " + str(e)[:160])
    log("[info] fondamentaux : %.0f s" % (time.time() - t0))

    # ── 3 bis. les données fraîches : dernier point publié, projections, dette
    #    totale, détenteurs dans le temps (toutes automatiques) ──
    prec_fraiche = {x["code"]: x.get("dette_fraiche") for x in (prec_synth.get("souverains") or []) if x.get("dette_fraiche")}
    fraiche, proj, editions, totale, histo = {}, {}, {}, {}, {}
    for nom, fn in (("dette fraîche", lambda: oblig_frais.dette_fraiche(journal, prec_fraiche)),
                    ("projections", lambda: oblig_frais.projections(journal)),
                    ("dette totale", lambda: oblig_frais.dette_totale(journal)),
                    ("détenteurs", lambda: oblig_frais.detenteurs(journal))):
        try:
            r = fn()
            if nom == "dette fraîche":
                fraiche = r
            elif nom == "projections":
                proj, editions = r
            elif nom == "dette totale":
                totale = r
            else:
                histo = r
        except Exception as e:  # noqa: BLE001
            journal.append("%s : %s" % (nom, str(e)[:140]))
    for c, x in pays.items():
        if c.startswith("_") or not isinstance(x, dict):
            continue
        if c in fraiche:
            x["dette_fraiche"] = fraiche[c]
        pj = dict(proj.get(c) or {})
        if (x.get("fmi") or {}).get("dette_pib"):
            pj["fmi"] = {"serie": x["fmi"]["dette_pib"], "definition": "dette brute des administrations (FMI)"}
        if pj:
            x["projections"] = pj
            x["projections_editions"] = dict(editions, fmi=x.get("fmi_edition"))
        if c in totale:
            x["dette_totale"] = totale[c]
        if c in histo:
            h = histo[c]
            D = x.get("detenteurs") or {}
            # L'historique long remplace les séries courtes ; la photo du dernier
            # point reste celle de la source la plus détaillée quand elle existe.
            D["series"] = h["series"]
            D["source_histo"] = h["source"]
            D["frequence"] = h["frequence"]
            if h.get("base"):
                D["base_histo"] = h["base"]
            if not D.get("parts"):
                der = max(max(v) for v in h["series"].values() if v)
                D["parts"] = {k: v.get(der) for k, v in h["series"].items() if v.get(der) is not None}
                D["periode"] = der
                D["source"] = h["source"]
            x["detenteurs"] = D
    log("[info] données fraîches : %.0f s" % (time.time() - t0))

    # ── 4. le crédit ──
    credit, det_credit, longues, emis = [], {}, {}, {}
    try:
        oblig_credit.DEFAUTS.update(oblig_frais.defauts_auto(journal, prec_synth.get("defauts") or oblig_credit.DEFAUTS))
    except Exception as e:  # noqa: BLE001
        journal.append("défauts : " + str(e)[:120])
    try:
        credit, det_credit, longues, emis = oblig_credit.construire(journal)
    except Exception as e:  # noqa: BLE001
        journal.append("crédit : " + str(e)[:160])
        pc = lire_json("oblig_credit.json")
        if pc and prec_synth.get("credit"):
            credit = [dict(x, reprise_du=(prec_synth.get("genere_le") or "")[:10]) for x in prec_synth["credit"]]
            det_credit, longues, emis = pc.get("segments", {}), pc.get("longues", {}), pc.get("emissions", {})
    log("[info] crédit : %.0f s" % (time.time() - t0))

    # ── Assemblage ──
    tailles = {}
    for s in souv:
        c = s["code"]
        f = pays.get(c) or {}
        R = f.get("resume") or {}
        if f.get("notation"):
            s["notation"] = {k: f["notation"][k] for k in ("composite", "cran", "lecture", "negatives", "positives")}
        s["dette_pib"] = R.get("dette_pib")
        if f.get("dette_fraiche"):
            s["dette_fraiche"] = f["dette_fraiche"]
        s["solde_pib"] = R.get("solde_pib")
        s["interets_recettes"] = R.get("interets_bruts_recettes") or R.get("interets_recettes")
        s["croissance_nominale"] = R.get("croissance_nominale")
        s["inflation"] = R.get("inflation")
        bc = f.get("banque_centrale") or (pays.get("ez") or {}).get("banque_centrale") if c == "ez" else f.get("banque_centrale")
        if bc:
            s["banque_centrale"] = {k: bc.get(k) for k in ("taux", "bas", "date", "nom", "source", "dernier_mouvement")}
        if (f.get("structure") or {}).get("maturite_residuelle"):
            s["maturite_dette"] = f["structure"]["maturite_residuelle"]
        det = det_souv.get(c)
        if det is None:
            continue
        det = dict(det)
        det.pop("_synth", None)
        det.pop("_fiches", None)
        det["genere_le"] = maintenant
        det["pays"] = f
        det["synthese"] = s
        # (le module relit `_synth` et `_fiches` au passage suivant, pour la reprise)
        det_rep = dict(det)
        det_rep["_synth"] = s
        det_rep["_fiches"] = [x for x in fiches if x["pays"] == c]
        tailles["oblig_%s.json" % c] = ecrire("oblig_%s.json" % c, nettoyer(compacter_arbre(det_rep)))

    # la vue d'ensemble : les 10 ans depuis 1990, les courbes du jour, les écarts au Bund
    vue = {"genere_le": maintenant, "dix_ans": {}, "courbes": {}, "ecarts_bund": {}}
    for c in ("us", "de", "fr", "it", "es", "gb", "jp", "ca", "au", "ch", "cn", "in", "nl", "be", "pt", "gr", "at", "ie"):
        d = det_souv.get(c)
        if not d:
            continue
        sv = serie_vue(d)
        if sv:
            vue["dix_ans"][c] = sv
        if d.get("courbe"):
            vue["courbes"][c] = d["courbe"]
        e = (d.get("ecarts") or {}).get("10")
        if e and c not in ("us", "cn", "in", "jp", "gb", "ca", "au", "ch"):
            pts = list(zip(e["d"], e["v"]))
            m = oblig_souverains.moyennes_mensuelles(pts) if not d.get("mensuel") else pts
            vue["ecarts_bund"][c] = {"d": [x[0] for x in m], "v": [round(x[1], 1) for x in m], "mensuel": True}
    if det_souv.get("ez", {}).get("courbe_aaa"):
        vue["courbe_aaa"] = det_souv["ez"]["courbe_aaa"]
    vue["credit_longues"] = longues
    tailles["oblig_vue.json"] = ecrire("oblig_vue.json", nettoyer(compacter_arbre(vue)))

    tailles["oblig_credit.json"] = ecrire("oblig_credit.json", nettoyer(compacter_arbre(
        {"genere_le": maintenant, "segments": det_credit, "longues": longues, "emissions": emis,
         "defauts": oblig_credit.DEFAUTS, "licences": oblig_credit.SOURCES_SOUS_LICENCE})))

    # fiches de crédit
    for x in credit:
        fiches.append({"code": x["code"], "genre": "credit", "nom": x["court"], "taux": x.get("rendement"),
                       "date": x.get("date"), "oas_pb": x.get("oas_pb") or x.get("ecart_pb"),
                       "var_1m_pb": (x.get("rdt_var") or {}).get("1m"), "var_1a_pb": (x.get("rdt_var") or {}).get("1a")})

    sources = [
        {"nom": "BIS", "quoi": "encours des titres de dette par pays et par émetteur (Debt securities statistics)",
         "url": "https://data.bis.org/topics/DSS", "date": (marche or {}).get("periode")},
        {"nom": "Trésor américain, FRED", "quoi": "taux des Treasuries, TIPS et points morts d'inflation", "url": "https://home.treasury.gov/"},
        {"nom": "Bundesbank", "quoi": "courbe des Bund ; rendements des obligations d'entreprises allemandes", "url": "https://www.bundesbank.de/"},
        {"nom": "Banque de France", "quoi": "taux à échéance constante (TEC) des OAT", "url": "https://webstat.banque-france.fr/"},
        {"nom": "BCE", "quoi": "courbes de la zone euro, taux longs mensuels, structure de la dette publique, émissions", "url": "https://data.ecb.europa.eu/"},
        {"nom": "Banco de España, Bank of England, ministère des Finances du Japon, Banque du Canada, RBA, BNS, OCDE",
         "quoi": "taux d'État de chaque pays", "url": None},
        {"nom": "FMI, Eurostat", "quoi": "dette, déficit, charge d'intérêts, croissance ; détenteurs de la dette", "url": "https://www.imf.org/external/datamapper/"},
        {"nom": "ICE BofA et Moody's via FRED", "quoi": "écarts de crédit et rendements des obligations d'entreprises (3 ans pour ICE)", "url": "https://fred.stlouisfed.org/"},
        {"nom": "Réserve fédérale", "quoi": "GZ spread (écart de crédit depuis 1973)", "url": "https://www.federalreserve.gov/econres/notes/feds-notes/"},
        {"nom": "Yahoo Finance", "quoi": "cours des ETF obligataires, dividendes réinvestis", "url": None},
        {"nom": "Agences de notation", "quoi": "notes S&P, Moody's, Fitch, relevées à la main et vérifiées ligne par ligne le %s" % oblig_pays.VERIFIE_LE, "url": None},
    ]
    synth = {"genere_le": maintenant, "gravure": GRAVURE, "marche": marche, "souverains": souv, "fiches": fiches,
             "credit": credit, "emissions": {k: v for k, v in emis.items()}, "sources": sources,
             "taux_directeurs": {c: (pays.get(c) or {}).get("banque_centrale") for c in ("us", "ez", "gb", "jp", "ch", "ca", "au", "cn", "in")},
             "defauts": oblig_credit.DEFAUTS, "notations_changements": pays.get("_changements_notes") or [],
             "projections_editions": editions, "journal": journal[:40], "duree_s": round(time.time() - t0)}
    synth["taux_directeurs"]["ez"] = (pays.get("ez") or {}).get("banque_centrale") or (pays.get("de") or {}).get("banque_centrale")
    tailles["obligations.json"] = ecrire("obligations.json", nettoyer(compacter_arbre(synth)), js_global="__OBLIGATIONS__")
    log("[info] écrit : " + ", ".join("%s %.0f Ko" % (k, v / 1024) for k, v in sorted(tailles.items())))
    log("[info] %d pays, %d fiches, %d segments de crédit, %.0f s ; journal : %s"
        % (len(souv), len(fiches), len(credit), time.time() - t0, journal))
    # Échec franc seulement si RIEN n'a été produit.
    return 0 if souv else 1


if __name__ == "__main__":
    sys.exit(main())
