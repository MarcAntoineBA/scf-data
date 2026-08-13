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
        trim, ann = {}, {}
        for u in (d.get("units") or {}).get("USD") or []:
            deb, fin = u.get("start"), u.get("end")
            if not deb or not fin or u.get("val") is None:
                continue
            try:
                nj = (datetime.date.fromisoformat(fin) - datetime.date.fromisoformat(deb)).days
            except ValueError:
                continue
            cible = trim if 80 <= nj <= 100 else (ann if 350 <= nj <= 380 else None)
            if cible is None:
                continue
            prec = cible.get(fin)
            if not prec or (u.get("filed") or "") > (prec.get("filed") or ""):
                cible[fin] = u

        qs = sorted(trim.values(), key=lambda u: u["end"], reverse=True)
        if len(qs) >= TRIMESTRES:
            fins = [datetime.date.fromisoformat(q["end"]) for q in qs[:TRIMESTRES]]
            # Quatre trimestres ne font une année que s'ils se SUIVENT : on exige que le plus
            # ancien des quatre soit à moins de 400 jours du plus récent.
            if (fins[0] - fins[-1]).days <= 400:
                return tag, "TTM", sum(q["val"] for q in qs[:TRIMESTRES]), \
                       [q["end"] for q in qs[:TRIMESTRES]], qs
        a = sorted(ann.values(), key=lambda u: u["end"], reverse=True)
        if a:
            return tag, "exercice", a[0]["val"], [a[0]["end"]], a
    return None, None, None, [], []


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
    sans_cik, sans_donnee, sans_emission, sans_mcap = [], [], 0, 0

    for e in pool:
        tk = (e.get("t") or "").upper()
        cik = ciks.get(tk)
        if not cik:
            sans_cik.append(tk)
            continue
        tag, nature, rachats, periodes, brut = douze_mois(cik, TAGS_RACHAT)
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

        _, nature_e, emissions, _, _ = douze_mois(cik, TAGS_EMISSION)
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
        societes.append({
            "ticker": tk, "nom": e.get("n"), "secteur": e.get("s"), "cik": cik, "tag": tag,
            # « TTM » = quatre trimestres consécutifs ; « exercice » = dernier exercice clos,
            # jusqu'à douze mois de retard. Comparer deux sociétés sans lire ce champ, c'est
            # comparer deux fenêtres différentes.
            "nature_periode": nature,
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
