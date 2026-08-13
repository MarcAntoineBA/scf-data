#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_sec_rachats.py — LES RACHATS D'ACTIONS, PRIS À LA SOURCE COMPTABLE.

CE QU'IL APPORTE. `These_Financiarisation` cite les rachats d'actions à côté du spread haut
rendement comme l'une de ses deux mesures vivantes. Le spread est collecté ; les rachats, non.
Ils le sont maintenant, et depuis les états financiers déposés à la SEC — pas depuis une
estimation de fournisseur.

LA MESURE QUI DÉCIDE N'EST PAS LE MONTANT, C'EST LE RENDEMENT. « Apple a racheté pour 90 Md$ »
ne se compare à rien. Rapporté à la capitalisation, ça devient un **rendement de rachat**
comparable entre sociétés, entre sectors, et à lui-même dans le temps. C'est cette division —
et rien d'autre — qui transforme un gros nombre en information.

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
LA DISTINCTION QUI COÛTE CHER ICI : RACHAT BRUT ≠ RACHAT NET
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
Une société peut racheter 10 Md$ d'actions ET en émettre 8 Md$ pour rémunérer ses salariés. Le
communiqué titre sur les 10 ; l'actionnaire ne récupère que 2. Sur les valeurs technologiques,
où la rémunération en titres est massive, l'écart est structurel et non anecdotique.
On collecte donc les DEUX flux (`PaymentsForRepurchaseOfCommonStock` et les émissions) et on
publie le net quand les deux existent. Quand l'émission manque, le brut sort **nommé comme
brut**, jamais présenté comme un net.

CE QU'ON NE SAURA PAS, ET QUI EST DÉCLARÉ : le tag XBRL n'est pas universel. Une société qui
regroupe ses rachats dans une autre ligne comptable sera comptée « sans donnée », pas
« sans rachat ». Confondre les deux inventerait des sociétés vertueuses.

Aucune dépendance nouvelle : `urllib` + `json`. Même patron que `fetch_sec_inities.py`.
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")
UNIVERS = os.path.join(ICI, "stock_universe.json")
SORTIE = os.path.join(CACHE_DIR, "sec_rachats_cache.json")
SORTIE_JS = os.path.join(CACHE_DIR, "sec_rachats_cache.js")

UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research"),
      "Accept-Encoding": "gzip, deflate"}
DEBIT = 0.11
TRIMESTRES = 4        # fenêtre glissante : quatre trimestres = un an de rachats

# Les rachats sortent en flux de trésorerie de financement. Le tag principal couvre l'immense
# majorité ; les deux autres existent chez des sociétés qui distinguent leurs programmes.
TAGS_RACHAT = ["PaymentsForRepurchaseOfCommonStock",
               "PaymentsForRepurchaseOfEquity"]
# L'autre jambe : ce que la société ÉMET. Sans elle, le « rachat » publié est brut.
TAGS_EMISSION = ["ProceedsFromIssuanceOfCommonStock",
                 "StockIssuedDuringPeriodValueNewIssues"]

_dernier = [0.0]


def _freine():
    d = time.time() - _dernier[0]
    if d < DEBIT:
        time.sleep(DEBIT - d)
    _dernier[0] = time.time()


def http(url):
    dernier = None
    for n in range(3):
        _freine()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                d = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                import gzip
                d = gzip.decompress(d)
            elif enc == "deflate":
                import zlib
                d = zlib.decompress(d, -zlib.MAX_WBITS)
            return json.loads(d.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None          # tag absent chez cette société : normal, pas une panne
            dernier = f"HTTP {e.code}"
            if e.code in (429, 403):
                time.sleep(2 * (n + 1))
        except Exception as e:
            dernier = f"{type(e).__name__}: {e}"
            time.sleep(1 + n)
    if dernier:
        print(f"[rachats] échec {url[-60:]} — {dernier}", file=sys.stderr)
    return None


def douze_mois(cik, tags):
    """La somme sur DOUZE MOIS d'un concept, et la NATURE de cette somme.

    ⚠ DÉFAUT MESURÉ, ET IL PRODUISAIT UN CHIFFRE FAUX D'APPARENCE JUSTE.
    XBRL mélange les durées de période, et beaucoup de sociétés déclarent en CUMUL DEPUIS LE
    DÉBUT D'EXERCICE : 3 mois au T1, 6 au T2, 9 au T3, 12 en clôture. Ne garder que les
    périodes de ~90 jours ne laisse alors survivre QUE LE PREMIER TRIMESTRE de chaque exercice.
    Constaté sur Apple : les « quatre trimestres » retenus étaient
    2025-12-27, 2024-12-28, 2023-12-30, 2022-12-31 — quatre T1 de quatre années différentes,
    additionnés et publiés comme un an de rachats. Le total tombait près du vrai chiffre annuel
    par coïncidence, ce qui rendait l'erreur invisible à la relecture.

    On procède donc par ordre de fiabilité décroissante, et on DIT lequel a servi :
      1. quatre vrais trimestres consécutifs (~90 j) couvrant ≈ un an  → « TTM »
      2. sinon la période annuelle la plus récente (350-380 j)         → « exercice »
    Un « exercice » peut avoir jusqu'à douze mois de retard : la nature sort avec la valeur,
    pour qu'on ne compare jamais un TTM d'une société à l'exercice d'une autre sans le savoir.
    """
    for tag in tags:
        d = http(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik.zfill(10)}"
                 f"/us-gaap/{tag}.json")
        if not d:
            continue
        trim, ann, brut_periodes = {}, {}, {}
        for u in (d.get("units") or {}).get("USD") or []:
            deb, fin = u.get("start"), u.get("end")
            if not deb or not fin or u.get("val") is None:
                continue
            nj = _jours(deb, fin)
            if nj <= 0 or nj > 400:
                continue
            # TOUTES les périodes sont gardées : la dérivation par différence a besoin des
            # cumuls intermédiaires (6 et 9 mois), que l'ancien filtre jetait.
            cle = (deb, fin)
            prec = brut_periodes.get(cle)
            if not prec or (u.get("filed") or "") > (prec.get("filed") or ""):
                brut_periodes[cle] = u
            cible = trim if 80 <= nj <= 100 else (ann if 350 <= nj <= 380 else None)
            if cible is not None:
                pc = cible.get(fin)
                if not pc or (u.get("filed") or "") > (pc.get("filed") or ""):
                    cible[fin] = u
        tous = list(brut_periodes.values())

        # ── LES TRIMESTRES DÉRIVÉS DES CUMULS ────────────────────────────────────────────────
        # Sans cette étape, 10 sociétés sur 11 tombaient sur le repli « exercice », c'est-à-dire
        # une donnée vieille de douze mois sur une mesure que l'onglet qualifie de vivante.
        # Un déclarant en cumul publie, dans un même exercice, des périodes qui PARTENT TOUTES
        # DU MÊME JOUR et finissent plus tard : 3 mois, 6, 9, 12. La différence de deux cumuls
        # consécutifs est donc exactement le trimestre qui les sépare.
        # On regroupe par date de DÉBUT — c'est ce qui identifie un exercice sans avoir à
        # deviner les bornes fiscales, qui varient d'une société à l'autre.
        derives, anomalies = {}, 0
        par_debut = {}
        for u in tous:
            par_debut.setdefault(u["start"], []).append(u)
        for debut, lot in par_debut.items():
            lot = sorted(lot, key=lambda u: u["end"])
            precedent = None
            for u in lot:
                if precedent is None:
                    # Le premier cumul d'un exercice EST le premier trimestre, s'il en a la durée.
                    if 80 <= _jours(debut, u["end"]) <= 100:
                        derives[u["end"]] = {"end": u["end"], "val": u["val"],
                                             "filed": u.get("filed"), "origine": "natif"}
                    precedent = u
                    continue
                ecart = _jours(precedent["end"], u["end"])
                if 80 <= ecart <= 100:
                    v = u["val"] - precedent["val"]
                    # Un rachat trimestriel NÉGATIF n'existe pas : c'est le signe que les deux
                    # cumuls viennent de dépôts qui ne se recouvrent pas (retraitement). On ne
                    # le corrige pas en douce, on le compte et on le déclare.
                    if v < 0:
                        anomalies += 1
                    else:
                        derives.setdefault(u["end"], {"end": u["end"], "val": v,
                                                      "filed": u.get("filed"),
                                                      "origine": "dérivé"})
                precedent = u

        # Un trimestre publié tel quel prime toujours sur un trimestre dérivé.
        for fin, u in trim.items():
            derives[fin] = {"end": fin, "val": u["val"], "filed": u.get("filed"),
                            "origine": "natif"}

        qs = sorted(derives.values(), key=lambda u: u["end"], reverse=True)
        if len(qs) >= TRIMESTRES:
            fins = [datetime.date.fromisoformat(q["end"]) for q in qs[:TRIMESTRES]]
            # Quatre trimestres ne font une année que s'ils se SUIVENT : on exige que le plus
            # ancien des quatre soit à moins de 400 jours du plus récent.
            if (fins[0] - fins[-1]).days <= 400:
                total = sum(q["val"] for q in qs[:TRIMESTRES])
                return (tag, "TTM", total, [q["end"] for q in qs[:TRIMESTRES]], qs,
                        {"anomalies": anomalies,
                         "derives": sum(1 for q in qs[:TRIMESTRES] if q["origine"] == "dérivé"),
                         "controle": _controle(qs, ann)})
        a = sorted(ann.values(), key=lambda u: u["end"], reverse=True)
        if a:
            return tag, "exercice", a[0]["val"], [a[0]["end"]], a, {"anomalies": anomalies}
    return None, None, None, [], [], {}


def _jours(d1, d2):
    try:
        return (datetime.date.fromisoformat(d2) - datetime.date.fromisoformat(d1)).days
    except (ValueError, TypeError):
        return -1


def _controle(qs, ann):
    """CONTRÔLE ARITHMÉTIQUE : la somme des quatre trimestres qui couvrent un exercice publié
    doit retomber sur le montant annuel publié.

    C'est la seule façon de savoir si la différence de cumuls est juste. Sans lui, une erreur
    de bornes produirait un TTM plausible et faux — exactement le défaut qu'on vient de corriger
    sur ce fichier, et qui était passé inaperçu parce que le total tombait près du vrai."""
    if not ann:
        return None
    a = sorted(ann.values(), key=lambda u: u["end"], reverse=True)[0]
    fin_a, deb_a = a["end"], a.get("start")
    couvrants = [q for q in qs if deb_a and deb_a < q["end"] <= fin_a]
    if len(couvrants) != TRIMESTRES:
        return {"verifiable": False, "pourquoi": f"{len(couvrants)} trimestre(s) couvrent "
                                                 f"l'exercice clos le {fin_a}, il en faut 4"}
    somme = sum(q["val"] for q in couvrants)
    ecart = abs(somme - a["val"])
    # ⚠ Un exercice à ZÉRO rend l'écart RELATIF indéfini — et la première version marquait
    # alors « discordant », c'est-à-dire qu'elle accusait la dérivation d'une faute là où il n'y
    # avait rien à diviser. Home Depot, qui n'a rien racheté, ressortait ainsi en défaut.
    # Quand les deux côtés valent zéro, ils concordent parfaitement ; on juge en ABSOLU.
    if not a["val"]:
        return {"verifiable": True, "exercice_publie": 0, "somme_trimestres": round(somme),
                "ecart_pct": None, "ecart_absolu": round(ecart),
                "concordant": ecart < 1_000_000,
                "note": "exercice nul : concordance jugée en absolu, l'écart relatif n'existe pas"}
    rel = round(100 * ecart / a["val"], 2)
    return {"verifiable": True, "exercice_publie": round(a["val"]),
            "somme_trimestres": round(somme), "ecart_pct": rel,
            "concordant": rel < 1.0}


def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    univers = json.load(open(UNIVERS, encoding="utf-8"))
    pool = (univers.get("us") or {}).get("pool") or []
    if not pool:
        print("[rachats] univers US vide", file=sys.stderr)
        return 1

    tab = http("https://www.sec.gov/files/company_tickers.json")
    if not tab:
        print("[rachats] table CIK injoignable — abandon", file=sys.stderr)
        return 1
    ciks = {(v.get("ticker") or "").upper(): str(v.get("cik_str")) for v in tab.values()}

    societes, lacunes, natures = [], [], {}
    controles, discordants = {}, []
    sans_cik, sans_donnee, sans_emission, sans_mcap = [], [], 0, 0

    for e in pool:
        tk = (e.get("t") or "").upper()
        cik = ciks.get(tk)
        if not cik:
            sans_cik.append(tk)
            continue
        tag, nature, rachats, periodes, brut, ctl = douze_mois(cik, TAGS_RACHAT)
        if rachats is None:
            sans_donnee.append(tk)
            continue
        natures[nature] = natures.get(nature, 0) + 1

        # La période PRÉCÉDENTE, prise de la même façon que la courante — comparer un TTM à un
        # exercice donnerait une variation qui ne mesure que le changement de méthode.
        rachats_avant = None
        if nature == "TTM" and len(brut) >= TRIMESTRES * 2:
            rachats_avant = sum(q["val"] for q in brut[TRIMESTRES:TRIMESTRES * 2])
        elif nature == "exercice" and len(brut) >= 2:
            rachats_avant = brut[1]["val"]

        _, nature_e, emissions, _, _, _ = douze_mois(cik, TAGS_EMISSION)
        # Un net n'a de sens que si les deux jambes couvrent la MÊME nature de période.
        if emissions is not None and nature_e != nature:
            emissions = None
        if emissions is None:
            sans_emission += 1

        # mc0 est en MILLIARDS de dollars dans l'univers du site (NVDA ≈ 5424,5).
        mcap = e.get("mc0")
        mcap_usd = mcap * 1e9 if isinstance(mcap, (int, float)) and mcap > 0 else None
        if not mcap_usd:
            sans_mcap += 1

        def rendement(x):
            return round(100 * x / mcap_usd, 2) if (mcap_usd and x is not None) else None

        net = (rachats - emissions) if emissions is not None else None
        c = (ctl or {}).get("controle") or {}
        if c.get("verifiable"):
            controles["verifiables"] = controles.get("verifiables", 0) + 1
            if c.get("concordant"):
                controles["concordants"] = controles.get("concordants", 0) + 1
            else:
                discordants.append(f"{tk} (écart {c.get('ecart_pct')} %)")
        societes.append({
            "ticker": tk, "nom": e.get("n"), "secteur": e.get("s"), "cik": cik, "tag": tag,
            # « TTM » = quatre trimestres consécutifs ; « exercice » = dernier exercice clos,
            # jusqu'à douze mois de retard. Comparer deux sociétés sans lire ce champ, c'est
            # comparer deux fenêtres différentes.
            "nature_periode": nature,
            # Le controle arithmetique de la derivation : somme des trimestres contre exercice
            # publie. `concordant: false` signale une derivation douteuse SUR CETTE societe.
            "controle_derivation": (ctl or {}).get("controle"),
            "trimestres_derives": (ctl or {}).get("derives"),
            "periodes_utilisees": periodes,
            "rachats_brut_usd": round(rachats),
            "emissions_usd": (round(emissions) if emissions is not None else None),
            "rachats_net_usd": (round(net) if net is not None else None),
            "rendement_rachat_brut_pct": rendement(rachats),
            "rendement_rachat_net_pct": rendement(net),
            "rachats_brut_annee_precedente_usd": (round(rachats_avant)
                                                  if rachats_avant is not None else None),
            "variation_pct": (round(100 * (rachats - rachats_avant) / rachats_avant, 1)
                              if rachats_avant else None),
            "capitalisation_usd": (round(mcap_usd) if mcap_usd else None),
            "n_periodes_disponibles": len(brut),
        })

    if sans_cik:
        lacunes.append(f"{len(sans_cik)} titre(s) sans CIK : ils ne déposent pas aux États-Unis")
    if sans_donnee:
        lacunes.append(
            f"{len(sans_donnee)} société(s) SANS DONNÉE de rachat dans les tags "
            f"{', '.join(TAGS_RACHAT)} — c'est « pas de ligne comptable à ce nom », PAS « pas de "
            f"rachat ». Les confondre inventerait des sociétés vertueuses : "
            + ", ".join(sans_donnee[:10]) + ("…" if len(sans_donnee) > 10 else ""))
    if sans_emission:
        lacunes.append(
            f"{sans_emission} société(s) sans donnée d'ÉMISSION : leur rachat est publié en BRUT "
            f"et le net est nul (None), pas zéro. Une société qui rachète 10 Md$ en émettant "
            f"8 Md$ pour ses salariés ne rend que 2 Md$ à l'actionnaire")
    if sans_mcap:
        lacunes.append(f"{sans_mcap} société(s) sans capitalisation dans l'univers : aucun "
                       f"rendement calculable, seulement un montant — donc incomparable")
    if discordants:
        lacunes.append(
            f"{len(discordants)} société(s) dont la somme des trimestres dérivés NE RETOMBE PAS "
            f"sur l'exercice publié (écart > 1 %) : leur TTM est douteux et doit être lu comme "
            f"tel — " + ", ".join(discordants[:8]))
    if controles.get("verifiables"):
        lacunes.append(
            f"contrôle arithmétique : {controles.get('concordants', 0)}/"
            f"{controles['verifiables']} société(s) vérifiables voient la somme de leurs "
            f"trimestres dérivés retomber sur l'exercice publié à moins de 1 % près")
    lacunes.append("données TRIMESTRIELLES déposées : le dernier trimestre publié a jusqu'à "
                   "trois mois de retard sur aujourd'hui. Un programme lancé ce mois-ci est "
                   "invisible ici")

    societes.sort(key=lambda s: -(s["rendement_rachat_brut_pct"] or 0))

    sortie = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR — XBRL companyconcept (flux de trésorerie de financement)",
        "duree_s": round(time.time() - t0, 1),
        "fenetre_trimestres": TRIMESTRES,
        "exhaustivite": {
            "univers_us": len(pool), "sans_cik": len(sans_cik),
            "sans_donnee_rachat": len(sans_donnee), "sans_donnee_emission": sans_emission,
            "sans_capitalisation": sans_mcap, "societes_publiees": len(societes),
            "tags_rachat": TAGS_RACHAT, "tags_emission": TAGS_EMISSION,
            "natures_de_periode": natures,
            "controle_derivation": controles,
        },
        "lacunes": lacunes,
        "societes": societes,
    }

    for chemin, prefixe in ((SORTIE, None), (SORTIE_JS, "window.__SEC_RACHATS__=")):
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if prefixe:
                f.write(prefixe)
            json.dump(sortie, f, ensure_ascii=False, separators=(",", ":"))
            if prefixe:
                f.write(";")
        os.replace(tmp, chemin)

    avec_net = sum(1 for s in societes if s["rachats_net_usd"] is not None)
    print(f"[rachats] {len(societes)} société(s) publiée(s) sur {len(pool)} — "
          f"{avec_net} avec un rachat NET calculable, {len(sans_donnee)} sans ligne comptable "
          f"— {sortie['duree_s']} s")
    for l in lacunes[:3]:
        print(f"[rachats] lacune : {l[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
