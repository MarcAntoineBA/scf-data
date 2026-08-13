#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_sec_inities.py — CE QUE LES DIRIGEANTS FONT DE LEUR PROPRE ARGENT.

LE TROU QUE CE COLLECTEUR BOUCHE. L'onglet `Positionnement_Actions` connaît le COT, le gamma et
les ventes à découvert — c'est-à-dire ce que font les SPÉCULATEURS. Il ne sait rien de ce que
font les gens qui dirigent les entreprises. Or l'achat d'initié est l'une des rares mesures dont
la valeur informative soit documentée, et elle est publique, gratuite, et déposée sous deux jours
ouvrés.

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
LA DISTINCTION QUI DÉCIDE DE TOUT : LE CODE DE TRANSACTION
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
Le tout premier Form 4 lu pendant la construction, chez NVIDIA, donnait ceci :
    code=A  sens=A  titres=1262  prix=0
    code=A  sens=A  titres=1148  prix=0
Un lecteur pressé écrit « une administratrice a pris 2 410 titres ». C'est FAUX : le code `A`
et le prix 0 disent qu'il s'agit d'une ATTRIBUTION — de la rémunération, pas une conviction.

    P  achat au marché ......... le SIGNAL. Quelqu'un a sorti son argent.
    S  vente ................... souvent programmée à l'avance (plan 10b5-1) : dire d'une vente
                                 qu'elle est un avis, c'est prêter une intention à un calendrier.
    A  attribution ............. rémunération. Aucune décision d'achat.
    M  exercice d'options ...... conversion, pas acquisition d'exposition nouvelle.
    F  titres retenus pour l'impôt · G  donation — ni l'un ni l'autre n'est un avis de marché.
Les cinq sont COMPTÉS et publiés séparément. Aucun n'est additionné aux achats.

ET LA SECONDE DISTINCTION : UN ACHAT ISOLÉ N'EST PAS UN SIGNAL. Un dirigeant qui achète pour
20 000 $ peut solder un crédit d'impôt. PLUSIEURS dirigeants qui achètent dans la même fenêtre,
c'est une autre chose — d'où la GRAPPE, comptée en initiés DISTINCTS, jamais en transactions.

DEUX PIÈGES D'EDGAR, PAYÉS PENDANT LA CONSTRUCTION :
 1. `primaryDocument` vaut souvent « xslF345X06/wk-form4_xxx.xml ». Ce préfixe désigne la vue
    HTML que la SEC fabrique en appliquant une feuille XSL ; le fichier n'est PAS du XML et le
    parseur meurt sur une balise mal fermée. Le XML brut est le même nom SANS le préfixe.
 2. La SEC répond 403 à toute requête sans contact nominatif. La valeur vit dans le secret
    `SCF_CONTACT_UA`, jamais dans le code : dans un dépôt public, une adresse est récoltée dès
    l'indexation.

AUCUNE DÉPENDANCE NOUVELLE. Le schéma du Form 4 est stable et simple ; `xml.etree` suffit.
`edgartools` aurait fait gagner du temps d'écriture contre 35 dépendances transitives (dont
pyarrow, ~40 Mo) sur un dépôt qui en déclare onze, chacune avec sa raison. Mauvais échange ici.
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
CACHE_DIR = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")
UNIVERS = os.path.join(ICI, "stock_universe.json")
SORTIE = os.path.join(CACHE_DIR, "sec_inities_cache.json")
SORTIE_JS = os.path.join(CACHE_DIR, "sec_inities_cache.js")
DEJA_VUS = os.path.join(CACHE_DIR, "sec_inities_vus.json")

UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research"),
      "Accept-Encoding": "gzip, deflate"}

CACHE_VERSION = 3      # ↑ à chaque changement de la LECTURE d'un dépôt (cf. main)
FENETRE_J = 120        # profondeur regardée : un trimestre plein, plus une marge
GRAPPE_J = 45          # fenêtre de la grappe : au-delà, deux achats ne se répondent plus
DEBIT = 0.11           # la SEC autorise 10 requêtes/s ; on reste dessous, exprès
MAX_F4_PAR_SOCIETE = 40    # borne dure : une société très active ne doit pas manger la course

CODES = {
    "P": "achat au marché", "S": "vente", "A": "attribution",
    "M": "exercice d'options", "F": "titres retenus pour l'impôt", "G": "donation",
    "C": "conversion", "D": "cession à l'émetteur", "X": "exercice de bon",
    # « J » = autre acquisition/cession, à préciser en note du dépôt. Rencontré chez JPM au
    # premier essai. On le nomme au lieu de le laisser sortir en lettre nue : un code non
    # traduit ressemble à un défaut d'affichage, alors que c'est une catégorie réelle.
    "J": "autre (précisé en note du dépôt)", "I": "acquisition par héritage",
    "W": "acquisition ou cession par testament",
}

_dernier = [0.0]


def _freine():
    d = time.time() - _dernier[0]
    if d < DEBIT:
        time.sleep(DEBIT - d)
    _dernier[0] = time.time()


def http(url, brut=False, essais=3):
    """GET avec freinage, reprise, et DÉCOMPRESSION EXPLICITE.

    ⚠ DEUX DÉFAUTS PAYÉS ICI, ET LE SECOND EST LE PLUS GRAVE.
    1. `urllib` ne décompresse PAS tout seul. On annonçait `Accept-Encoding: gzip`, la SEC
       renvoyait du gzip, et `json.loads` mourait sur des octets binaires. Il faut lire
       `Content-Encoding` et décompresser à la main — ou ne pas demander de compression. On la
       garde : `company_tickers.json` pèse ~1 Mo et les index de dépôts sont volumineux.
    2. Le `except Exception: return None` d'origine renvoyait None SANS RIEN DIRE. Le collecteur
       affichait « table CIK injoignable » alors que la table répondait parfaitement — le
       diagnostic désignait le réseau quand le défaut était dans le décodage. Un échec muet
       coûte plus cher que l'échec lui-même : la raison sort maintenant sur stderr.
    """
    dernier = None
    for n in range(essais):
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
            return d if brut else json.loads(d.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                    # absence normale, pas une panne
            dernier = f"HTTP {e.code}"
            if e.code in (429, 403) and n < essais - 1:
                time.sleep(2 * (n + 1))
                continue
        except Exception as e:
            dernier = f"{type(e).__name__}: {e}"
            if n < essais - 1:
                time.sleep(1 + n)
    if dernier:
        print(f"[inities] échec {url[:88]} — {dernier}", file=sys.stderr)
    return None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def txt(e, chemin):
    n = e.find(chemin)
    return (n.text or "").strip() if n is not None and n.text else None


def lire_form4(cik, acc, doc):
    """Un Form 4 → la liste de ses transactions non dérivées.

    On ne lit QUE les transactions non dérivées : ce sont les seules qui portent une acquisition
    ou une cession d'actions ordinaires. Les dérivées (options, RSU) sont comptées à part —
    les mélanger gonflerait les volumes d'exposition qui n'existent pas encore."""
    # cf. en-tête, piège n°1 : le préfixe `xsl…/` désigne la vue HTML, pas le XML.
    doc_xml = doc.split("/")[-1] if doc.startswith("xsl") else doc
    raw = http(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc_xml}", brut=True)
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    nom = txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    # ── L'ÉMETTEUR N'EST PAS UN INITIÉ, ET IL DÉPOSE POURTANT DES FORM 4 ──────────────────────
    # Mesuré au premier essai : « GOLDMAN SACHS GROUP INC » apparaissait en ACHETEUR de
    # 10 006 titres pour 5 503 $, soit 0,55 $ l'action — un prix absurde pour GS. Le déclarant
    # était la société elle-même, sur ses propres titres. Ce n'est pas un dirigeant qui engage
    # son argent, et le compter comme tel invente exactement le signal qu'on cherche à mesurer.
    # Le test est EXACT : on compare les CIK, pas les noms.
    cik_decl = txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerCik")
    cik_emet = txt(root, ".//issuer/issuerCik")
    est_emetteur = bool(cik_decl and cik_emet and cik_decl.lstrip("0") == cik_emet.lstrip("0"))
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    roles = []
    if rel is not None:
        for c in rel:
            if (c.text or "").strip() in ("1", "true"):
                roles.append(c.tag)
        t = txt(rel, "officerTitle")
        if t:
            roles.append(t)

    ops = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = txt(t, ".//transactionCoding/transactionCode")
        sh = num(txt(t, ".//transactionAmounts/transactionShares/value"))
        px = num(txt(t, ".//transactionAmounts/transactionPricePerShare/value"))
        sens = txt(t, ".//transactionAmounts/transactionAcquiredDisposedCode/value")
        date = txt(t, ".//transactionDate/value")
        if not code or sh is None:
            continue
        ops.append({"code": code, "titres": sh, "prix": px, "sens": sens,
                    "date": date, "initie": nom, "roles": roles, "emetteur": est_emetteur})
    n_deriv = len(root.findall(".//derivativeTransaction"))
    return {"ops": ops, "n_derivees": n_deriv, "initie": nom, "roles": roles,
            "emetteur": est_emetteur, "cik_emetteur": (cik_emet or "").lstrip("0")}


def table_cik():
    """La table ticker → CIK, publiée par la SEC. Sans elle, aucun rapprochement possible
    entre l'univers du site (des tickers) et EDGAR (des CIK)."""
    d = http("https://www.sec.gov/files/company_tickers.json")
    if not d:
        return {}
    out = {}
    for v in d.values():
        t = (v.get("ticker") or "").upper()
        if t:
            out[t] = str(v.get("cik_str"))
    return out


def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    ajd = datetime.date.today()
    limite = ajd - datetime.timedelta(days=FENETRE_J)

    univers = json.load(open(UNIVERS, encoding="utf-8"))
    pool = (univers.get("us") or {}).get("pool") or []
    if not pool:
        print("[inities] univers US vide — rien à faire", file=sys.stderr)
        return 1

    ciks = table_cik()
    if not ciks:
        print("[inities] table CIK injoignable — abandon", file=sys.stderr)
        return 1

    # Un dépôt déjà lu ne se relit pas : la SEC n'aime pas qu'on retire chaque jour ce qui n'a
    # pas bougé, et une course quotidienne qui refait 1 200 requêtes pour trois nouveautés est
    # une course mal écrite. Le cache est borné par la fenêtre (élagué plus bas).
    # ── VERSION DU CACHE, ET POURQUOI ELLE EST OBLIGATOIRE ────────────────────────────────────
    # Le cache garde des dépôts DÉJÀ ANALYSÉS. Quand la lecture change — ici, l'ajout du drapeau
    # « l'émetteur dépose sur ses propres titres » — les entrées anciennes n'ont pas le nouveau
    # champ et échappent silencieusement à la correction : le défaut resterait en place pendant
    # 120 jours, le temps que la fenêtre les évacue. Même précaution que
    # `EDGAR_CACHE_VERSION` dans fetch_treasury_companies.py.
    try:
        brut = json.load(open(DEJA_VUS, encoding="utf-8"))
        vus = brut.get("depots") or {} if brut.get("version") == CACHE_VERSION else {}
        if not vus and brut:
            print("[inities] cache d'une version antérieure — relecture complète")
    except Exception:
        vus = {}

    societes, lacunes = [], []
    sans_cik, sans_depot, f4_lus, f4_caches, f4_illisibles, tronques = [], 0, 0, 0, 0, 0
    autre_emetteur = 0
    total_codes = {}

    for e in pool:
        tk = (e.get("t") or "").upper()
        cik = ciks.get(tk)
        if not cik:
            sans_cik.append(tk)
            continue

        sub = http(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
        if not sub:
            sans_depot += 1
            continue
        rec = (sub.get("filings") or {}).get("recent") or {}
        formes = rec.get("form") or []

        idx = []
        for i, f in enumerate(formes):
            if f != "4":
                continue
            try:
                d = datetime.date.fromisoformat(rec["filingDate"][i])
            except (ValueError, KeyError, IndexError):
                continue
            if d >= limite:
                idx.append((i, d))
        if len(idx) > MAX_F4_PAR_SOCIETE:
            tronques += 1
            idx = idx[:MAX_F4_PAR_SOCIETE]

        ops = []
        n_deriv = 0
        for i, d in idx:
            acc = rec["accessionNumber"][i].replace("-", "")
            cle = acc
            if cle in vus:
                r = vus[cle]
                f4_caches += 1
            else:
                r = lire_form4(cik, acc, rec["primaryDocument"][i])
                if r is None:
                    f4_illisibles += 1
                    continue
                r["d"] = d.isoformat()
                vus[cle] = r
                f4_lus += 1
            # ── LE DÉPÔT PARLE-T-IL BIEN DE CETTE SOCIÉTÉ ? ───────────────────────────────────
            # DÉFAUT MESURÉ, ET IL FAUSSAIT LES CHIFFRES. L'index EDGAR d'une société ne
            # contient pas seulement les Form 4 déposés SUR ses titres : il contient aussi ceux
            # où elle est DÉCLARANTE sur les titres d'une AUTRE société — cas courant pour une
            # banque détenant plus de 10 % d'un émetteur.
            # Constaté : « GOLDMAN SACHS GROUP INC » ressortait acheteur de 10 006 titres pour
            # 5 503 $, soit 0,55 $ l'action. Ce n'étaient pas des actions Goldman Sachs. Sans ce
            # contrôle, l'activité d'initiés d'une société est attribuée à une autre — et rien
            # ne le signale, puisque les deux dépôts sont parfaitement valides.
            if r.get("cik_emetteur") and r["cik_emetteur"] != cik.lstrip("0"):
                autre_emetteur += 1
                continue
            for o in (r.get("ops") or []):
                ops.append(o)
            n_deriv += r.get("n_derivees") or 0

        if not ops and not n_deriv:
            continue

        # ── LE COMPTE, PAR CODE, SANS JAMAIS MÉLANGER ─────────────────────────────────────────
        par_code, achats, ventes = {}, [], []
        n_emetteur = 0
        for o in ops:
            c = o["code"]
            par_code[c] = par_code.get(c, 0) + 1
            total_codes[c] = total_codes.get(c, 0) + 1
            # Les opérations de l'émetteur sur ses propres titres sont comptées à part : elles
            # existent, elles ne disent rien de ce que pensent les dirigeants (cf. lire_form4).
            if o.get("emetteur"):
                n_emetteur += 1
                continue
            if c == "P":
                achats.append(o)
            elif c == "S":
                ventes.append(o)

        def montant(lst):
            return round(sum((x["titres"] or 0) * (x["prix"] or 0) for x in lst))

        acheteurs = sorted({a["initie"] for a in achats if a["initie"]})
        vendeurs = sorted({v["initie"] for v in ventes if v["initie"]})

        # ── LA GRAPPE : des initiés DISTINCTS, dans une même fenêtre ──────────────────────────
        # On compte des PERSONNES, pas des lignes : un dirigeant qui achète en cinq fois n'est
        # pas cinq dirigeants qui achètent. C'est toute la différence entre un signal et un
        # calendrier d'exécution.
        grappe, grappe_fin = 0, None
        if achats:
            dates = sorted((a["date"], a["initie"]) for a in achats if a["date"])
            for j, (dj, _) in enumerate(dates):
                try:
                    d0 = datetime.date.fromisoformat(dj)
                except ValueError:
                    continue
                fenetre = {n for dd, n in dates
                           if n and 0 <= (datetime.date.fromisoformat(dd) - d0).days <= GRAPPE_J}
                if len(fenetre) > grappe:
                    grappe, grappe_fin = len(fenetre), dj

        societes.append({
            "ticker": tk, "nom": e.get("n"), "secteur": e.get("s"), "cik": cik,
            "achats_marche": {"n": len(achats), "titres": round(sum(a["titres"] for a in achats)),
                              "montant_usd": montant(achats), "inities": acheteurs},
            "ventes": {"n": len(ventes), "titres": round(sum(v["titres"] for v in ventes)),
                       "montant_usd": montant(ventes), "inities": vendeurs},
            "grappe_acheteurs": grappe,
            "grappe_depuis": grappe_fin,
            "par_code": {CODES.get(k, k): v for k, v in sorted(par_code.items())},
            "operations_de_l_emetteur": n_emetteur,
            "n_derivees": n_deriv,
            "n_form4": len(idx),
        })

    # ── ÉLAGAGE DU CACHE ──────────────────────────────────────────────────────────────────────
    # Sans lui le fichier grossit indéfiniment de dépôts sortis de la fenêtre — le même défaut
    # que le journal en ajout seul écarté côté Kalshi.
    avant = len(vus)
    vus = {k: v for k, v in vus.items()
           if not v.get("d") or v["d"] >= limite.isoformat()}
    with open(DEJA_VUS + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"version": CACHE_VERSION, "depots": vus}, f,
                  ensure_ascii=False, separators=(",", ":"))
    os.replace(DEJA_VUS + ".tmp", DEJA_VUS)

    # ── LES LACUNES ───────────────────────────────────────────────────────────────────────────
    if sans_cik:
        lacunes.append(f"{len(sans_cik)} titre(s) de l'univers US sans CIK dans la table SEC — "
                       f"ils ne déposent pas aux États-Unis ou leur ticker diffère : "
                       + ", ".join(sans_cik[:12]) + ("…" if len(sans_cik) > 12 else ""))
    if sans_depot:
        lacunes.append(f"{sans_depot} société(s) dont l'index EDGAR n'a pas répondu — absentes "
                       f"de ce passage, pas de leur univers")
    if f4_illisibles:
        lacunes.append(f"{f4_illisibles} Form 4 illisible(s) (XML absent ou malformé) : leurs "
                       f"transactions ne sont comptées nulle part")
    if tronques:
        lacunes.append(f"{tronques} société(s) plafonnée(s) à {MAX_F4_PAR_SOCIETE} dépôts sur la "
                       f"fenêtre — les plus anciens de la fenêtre ne sont pas lus")
    lacunes.append("les VENTES ne sont pas un avis : une part inconnue vient de plans 10b5-1 "
                   "programmés des mois à l'avance. Le Form 4 porte une case pour le dire, elle "
                   "n'est pas exploitée ici — on publie donc le compte, jamais une lecture")
    lacunes.append("aucune donnée avant " + limite.isoformat() + f" (fenêtre de {FENETRE_J} j) : "
                   "un initié qui achetait régulièrement avant cette date est invisible")

    societes.sort(key=lambda s: (-s["grappe_acheteurs"], -s["achats_marche"]["montant_usd"]))

    sortie = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR — formulaires 4 (transactions d'initiés), lecture publique",
        "duree_s": round(time.time() - t0, 1),
        "fenetre_jours": FENETRE_J,
        "fenetre_grappe_jours": GRAPPE_J,
        "codes": CODES,
        "exhaustivite": {
            "univers_us": len(pool),
            "societes_avec_cik": len(pool) - len(sans_cik),
            "societes_avec_activite": len(societes),
            "form4_lus_ce_passage": f4_lus,
            "form4_repris_du_cache": f4_caches,
            "form4_illisibles": f4_illisibles,
            "form4_ecartes_autre_emetteur": autre_emetteur,
            "cache_avant": avant, "cache_apres": len(vus),
            "transactions_par_code": {CODES.get(k, k): v
                                      for k, v in sorted(total_codes.items(),
                                                         key=lambda kv: -kv[1])},
        },
        "lacunes": lacunes,
        "societes": societes,
    }

    for chemin, prefixe in ((SORTIE, None), (SORTIE_JS, "window.__SEC_INITIES__=")):
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if prefixe:
                f.write(prefixe)
            json.dump(sortie, f, ensure_ascii=False, separators=(",", ":"))
            if prefixe:
                f.write(";")
        os.replace(tmp, chemin)

    ex = sortie["exhaustivite"]
    n_achats = sum(1 for s in societes if s["achats_marche"]["n"])
    n_grappes = sum(1 for s in societes if s["grappe_acheteurs"] >= 2)
    print(f"[inities] {ex['societes_avec_activite']} société(s) avec activité sur "
          f"{ex['univers_us']} — {n_achats} avec au moins un ACHAT au marché, "
          f"{n_grappes} avec une grappe (≥2 initiés distincts)")
    print(f"[inities] {f4_lus} Form 4 lus, {f4_caches} repris du cache, "
          f"{f4_illisibles} illisibles — {sortie['duree_s']} s")
    for l in lacunes[:4]:
        print(f"[inities] lacune : {l[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
