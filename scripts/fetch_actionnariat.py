#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L'actionnariat des sociétés américaines, par les SCHEDULE 13G.

POURQUOI CETTE SOURCE ET PAS UNE AUTRE

Un SCHEDULE 13G est la déclaration obligatoire de quiconque franchit 5 % du
capital d'une société cotée américaine. C'est donc, littéralement, « qui possède
cette société ». Trois propriétés en font la bonne source :

  · le POURCENTAGE est un champ (`classPercent`), pas une phrase à interpréter.
    Depuis la modernisation de 2024, le dépôt est structuré ;
  · l'ÉMETTEUR est identifié par son CIK (`issuerCik`), donc la jointure avec
    nos paquets est exacte — pas d'appariement par nom, pas d'homonymes ;
  · c'est GRATUIT et sans clé, comme tout EDGAR.

Le formulaire 13F, que ce dépôt collecte par ailleurs, ne répond PAS à la même
question : il liste les positions américaines d'un gérant au-dessus de cent
millions de dollars, sans jamais dire quelle part du capital elles représentent,
et il ignore les détenteurs qui ne sont pas des gérants — fondateurs, familles,
États. Vingt gérants suivis ne font pas un actionnariat.

⚠ CE QUE 13G NE DIT PAS, ET QU'IL FAUDRA ÉCRIRE SUR LA FICHE
  · seuls les détenteurs de PLUS DE 5 % déposent. Le flottant diffus n'y est pas,
    et la somme des parts déclarées ne fait jamais 100 % ;
  · un 13G se dépose à l'anniversaire ou lors d'un changement notable : une part
    peut donc dater de plusieurs mois ;
  · les dirigeants qui détiennent moins de 5 % relèvent du formulaire 4, déjà
    collecté ailleurs.

LE COÛT, MESURÉ AVANT D'ÉCRIRE

L'index trimestriel d'EDGAR liste chaque dépôt sous ses DEUX parties, l'émetteur
et le déclarant. On filtre donc sur le CIK avant la moindre requête. Mesuré le
28/08/2026 sur quatre trimestres : 56 102 dépôts au total, 21 072 concernant nos
sociétés, 19 008 documents distincts à lire, pour couvrir 90,1 % des 3 459
sociétés américaines. Sans ce filtre, il aurait fallu lire les 56 102.

⚠ LE FREIN N'EST PAS LE GOULOT, et je l'avais cru. La première version,
séquentielle, annonçait trente-quatre minutes en supposant que le frein de
0,11 s bornait la cadence. Mesuré en marche : 0,62 s par document, dont une
demi-seconde d'attente réseau — trois heures vingt. Huit fils qui se partagent
le MÊME frein ramènent cela à 0,135 s par document, sans jamais dépasser la
limite de la SEC. La leçon vaut au-delà de ce fichier : un débit estimé depuis
le frein qu'on s'impose ignore le temps de réponse d'en face.
"""
import argparse
import gzip
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")
SORTIE = os.path.join(CACHE_DIR, "actionnariat_cache.json")
SORTIE_JS = os.path.join(CACHE_DIR, "actionnariat_cache.js")
INDEX_SEC = os.path.join(CACHE_DIR, "sec_fundamentals_index.json")

UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research"),
      "Accept-Encoding": "gzip, deflate"}
DEBIT = 0.11          # le garde-fou d'EDGAR, identique aux autres collecteurs
_dernier = [0.0]
_verrou = threading.Lock()


def _freine():
    """Le frein de politesse, PARTAGÉ entre tous les fils.

    ⚠ C'est ce qui distingue ce collecteur du collecteur international, qui LÈVE
    son frein pendant sa phase parallèle et laisse le nombre de fils borner la
    cadence. Ce serait ici une faute : la SEC plafonne à dix requêtes par
    seconde et répond 403 au-delà — huit fils sans frein en feraient seize.

    Avec un frein global de 0,11 s entre deux DÉPARTS de requête, la cadence
    reste sous neuf par seconde quel que soit le nombre de fils, et ceux-ci ne
    servent qu'à recouvrir l'attente réseau. Mesuré : un document met environ
    0,62 s à revenir, dont 0,5 s d'attente pure — séquentiellement, dix-neuf
    mille documents demandaient trois heures et vingt minutes ; le frein partagé
    les ramène à trente-cinq minutes sans jamais dépasser la limite.
    """
    with _verrou:
        d = time.time() - _dernier[0]
        if d < DEBIT:
            time.sleep(DEBIT - d)
        _dernier[0] = time.time()


def http(url, essais=3):
    for k in range(essais):
        _freine()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    b = gzip.decompress(b)
            return b.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if k == essais - 1:
                return None
            time.sleep(1.5 * (k + 1))
        except Exception:
            if k == essais - 1:
                return None
            time.sleep(1.5 * (k + 1))
    return None


def trimestres(n, saut=0):
    """Les n trimestres qui précèdent, après en avoir sauté .

    Le saut existe pour une raison précise : un détenteur passif ne redépose son
    13G qu au franchissement d un seuil, pas chaque annee. Mesure sur Apple :
    deux depots seulement en quatre trimestres, tous deux de Vanguard — BlackRock
    en detient pourtant sept pour cent, mais sa derniere declaration est plus
    ancienne. Remonter plus loin est donc necessaire, et relire les trimestres
    deja lus serait une heure perdue.
    """
    a, m = date.today().year, date.today().month
    q = (m - 1) // 3 + 1
    for _ in range(saut):
        q -= 1
        if q == 0:
            q, a = 4, a - 1
    out = []
    for _ in range(n):
        out.append((a, q))
        q -= 1
        if q == 0:
            q, a = 4, a - 1
    return out


def _champ(xml, nom):
    m = re.search(r"<%s>([^<>]*)</%s>" % (nom, nom), xml)
    if not m:
        return None
    # Les entités XML se décodent ici et pas plus loin : sans cela le lecteur
    # verrait « JPMORGAN CHASE &amp; CO. » sur la fiche, et la clé de
    # normalisation traiterait « AMP » comme un mot du nom.
    return html.unescape(m.group(1)).strip()


def _nb(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def _cle_detenteur(nom):
    """Le nom d'un détenteur, ramené à une clé stable.

    Un même gérant dépose sous des raisons sociales voisines d'un trimestre à
    l'autre — « Vanguard Group Inc », « The Vanguard Group, Inc. », « Vanguard
    Portfolio Management ». Sans normalisation, la même maison apparaîtrait trois
    fois sur la même fiche, chacune avec sa part, et la somme n'aurait aucun sens.

    ⚠ LES MOTIFS SONT ENCADRÉS D'ESPACES, ET LA CHAÎNE AUSSI.
    Sans l'espace finale, « THE » ne mordait pas sur « THE VANGUARD GROUP » —
    qui sortait donc sous la clé « THE VANGUARD » quand « Vanguard Capital
    Management » sortait sous « VANGUARD ». Mesuré à la première collecte : la
    même maison figurait deux fois sur les fiches d'Apple, Microsoft, NVIDIA et
    JPMorgan, et les deux parts s'additionnaient dans le total déclaré.
    """
    s = (nom or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = " " + " ".join(s.split()) + " "
    for mot in (" INC ", " LLC ", " LP ", " LTD ", " CORP ", " CORPORATION ",
                " COMPANY ", " CO ", " GROUP ", " THE ", " PLC ", " SA ", " NV ",
                " AG ", " TRUST ", " HOLDINGS ", " HOLDING ", " MANAGEMENT ",
                " ADVISORS ", " ADVISERS ", " CAPITAL ", " PARTNERS ",
                " ASSOCIATES ", " INVESTMENTS ", " INVESTMENT ", " PORTFOLIO ",
                " ASSET ", " ASSETS "):
        while mot in s:
            s = s.replace(mot, " ")
    return " ".join(s.split())[:40]


TYPES = {
    "IA": "gérant d’actifs", "BD": "courtier", "IN": "personne physique",
    "CO": "société", "IC": "société d’investissement", "IV": "fonds",
    "EP": "fonds de retraite", "HC": "holding", "SA": "conseil d’épargne",
    "FI": "établissement financier", "PN": "société de personnes",
    "CP": "compagnie d’assurance", "OO": "autre",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trimestres", type=int, default=4,
                    help="nombre de trimestres d'index à parcourir")
    ap.add_argument("--limite", type=int, default=0,
                    help="s'arrêter après N documents (mise au point)")
    ap.add_argument("--saut", type=int, default=0,
                    help="trimestres a ignorer avant de commencer")
    ap.add_argument("--parallele", type=int, default=8,
                    help="fils de lecture ; le frein reste global")
    args = ap.parse_args()

    # ── Notre univers, par CIK ────────────────────────────────────────────
    if not os.path.exists(INDEX_SEC):
        print("[fatal] %s absent : lancer d'abord fetch_sec_fundamentals.py"
              % INDEX_SEC, file=sys.stderr)
        return 2
    with open(INDEX_SEC, encoding="utf-8") as fh:
        soc = json.load(fh).get("societes") or {}
    par_cik = {}
    for sym, v in soc.items():
        c = v.get("cik")
        if c:
            par_cik[str(int(c))] = sym
    print("[info] univers : %d sociétés américaines avec un CIK" % len(par_cik))

    # ── Les dépôts à lire ─────────────────────────────────────────────────
    # L'index porte chaque dépôt sous ses deux parties. On garde la ligne dès
    # que son CIK est chez nous, puis on déduplique par numéro de dépôt : le
    # document dira lui-même qui est l'émetteur.
    a_lire = {}
    for annee, q in trimestres(args.trimestres, args.saut):
        url = ("https://www.sec.gov/Archives/edgar/full-index/%d/QTR%d/form.idx"
               % (annee, q))
        idx = http(url)
        if not idx:
            print("[warn] %dQ%d indisponible" % (annee, q), file=sys.stderr)
            continue
        n = 0
        for l in idx.split("\n"):
            if not l[:12].strip().startswith("SCHEDULE 13G"):
                continue
            bouts = l.strip().split()
            if len(bouts) < 3:
                continue
            chemin = bouts[-1]
            depose = bouts[-2]
            parts = chemin.split("/")
            if len(parts) < 4:
                continue
            try:
                cik = str(int(parts[2]))
            except ValueError:
                continue
            if cik not in par_cik:
                continue
            accn = parts[3].replace(".txt", "")
            # Le plus récent gagne quand le même dépôt revient d'un index à l'autre.
            if accn not in a_lire or depose > a_lire[accn][1]:
                a_lire[accn] = (parts[2], depose)
            n += 1
        print("[info] %dQ%d : %d ligne(s) retenue(s)" % (annee, q, n))

    total = len(a_lire)
    print("[info] %d dépôt(s) distinct(s) à lire — environ %.0f minutes"
          % (total, total * DEBIT / 60))
    if not total:
        print("[fatal] aucun dépôt : index vide ou univers sans CIK", file=sys.stderr)
        return 1

    # ── Lecture des documents ─────────────────────────────────────────────
    par_soc = {}
    lus = [0]
    vides = [0]
    t0 = time.time()
    _pose = threading.Lock()

    def lire(item):
        """Descend un document et en tire une ligne, ou None.

        Fonction PURE côté réseau : elle ne touche à aucun état partagé, ce qui
        permet de l'exécuter dans plusieurs fils sans verrou autre que celui du
        frein. Le rangement, lui, est fait par l'appelant, sous verrou.
        """
        accn, cik_brut, depose = item
        url = ("https://www.sec.gov/Archives/edgar/data/%s/%s/primary_doc.xml"
               % (cik_brut, accn.replace("-", "")))
        xml = http(url)
        if not xml:
            return None
        icik = _champ(xml, "issuerCik")
        if not icik:
            return "vide"
        try:
            icik = str(int(icik))
        except ValueError:
            return "vide"
        sym = par_cik.get(icik)
        if not sym:
            return "vide"                 # l'émetteur n'est pas chez nous
        pct = _nb(_champ(xml, "classPercent"))
        nom = (_champ(xml, "reportingPersonName")
               or _champ(xml, "filingPersonName") or "").strip()
        if pct is None or not nom:
            return "vide"
        # Une déclaration à zéro est une SORTIE du seuil de 5 %, pas une
        # détention : le déclarant signale qu'il n'est plus tenu de déclarer.
        # L'afficher reviendrait à présenter un vendeur comme un actionnaire.
        if pct <= 0:
            return "vide"
        titres = _nb(_champ(xml, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")) \
            or _nb(_champ(xml, "amountBeneficiallyOwned"))
        typ = (_champ(xml, "typeOfReportingPerson")
               or _champ(xml, "typeOfPersonFiling") or "").strip().upper()
        return sym, {
            "detenteur": nom,
            "part_pct": round(pct, 2),
            "titres": int(titres) if titres else None,
            "nature": TYPES.get(typ, typ.lower() or None),
            "depose_le": depose,
            "accn": accn,
        }

    items = [(a, c, d) for a, (c, d) in sorted(a_lire.items())]
    if args.limite:
        items = items[:args.limite]

    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=max(1, args.parallele)) as ex:
        for k, r in enumerate(ex.map(lire, items), 1):
            if k % 1000 == 0:
                print("[info] %d/%d — %d position(s), %.0f s"
                      % (k, len(items), sum(len(v) for v in par_soc.values()),
                         time.time() - t0))
            if r is None:
                vides[0] += 1
                continue
            lus[0] += 1
            if r == "vide":
                continue
            sym, ligne = r
            with _pose:
                d = par_soc.setdefault(sym, {})
                cle = _cle_detenteur(ligne["detenteur"])
                # LA PLUS GRANDE PART, et non la plus récente : deux entités
                # d'une même maison déclarent la MÊME détention sous deux
                # raisons sociales — « Vanguard Portfolio Management » et
                # « Vanguard Capital Management » chez AvalonBay. Garder la
                # plus récente en choisirait une au hasard ; les additionner
                # doublerait la part. On garde le total du groupe, qui est la
                # plus grande des deux.
                vu = d.get(cle)
                if vu is None or ligne["part_pct"] > vu["part_pct"] or (
                        ligne["part_pct"] == vu["part_pct"]
                        and ligne["depose_le"] > vu["depose_le"]):
                    d[cle] = ligne

    # ── Fusion avec la collecte précédente ────────────────────────────────
    #
    # ⚠ AU DÉTENTEUR, PAS À LA SOCIÉTÉ.
    # La cadence quotidienne ne lit qu'un trimestre d'index : la plupart des
    # déclarations connues n'y sont pas, puisqu'un 13G se dépose une fois l'an.
    # Fusionner au niveau de la SOCIÉTÉ — « je garde l'ancien bloc si la société
    # est absente du nouveau » — reviendrait à écraser tout l'actionnariat d'un
    # titre dès qu'UN seul de ses porteurs redépose. Vanguard renouvelle sa
    # déclaration, et BlackRock, State Street et le fondateur disparaissent.
    #
    # C'est l'appauvrissement silencieux dans sa forme la plus commune : la
    # collecte réussit, le fichier grossit même, et la fiche perd les trois
    # quarts de ce qu'elle savait. On fusionne donc détenteur par détenteur, et
    # le plus récent gagne.
    repris_soc = repris_det = 0
    if os.path.exists(SORTIE):
        try:
            with open(SORTIE, encoding="utf-8") as fh:
                ancien = json.load(fh).get("societes") or {}
            for sym, bloc in ancien.items():
                d = par_soc.setdefault(sym, {})
                if not d:
                    repris_soc += 1
                for x in (bloc.get("detenteurs") or []):
                    cle = _cle_detenteur(x.get("detenteur"))
                    vu = d.get(cle)
                    if vu is None or (x.get("depose_le") or "") > (vu.get("depose_le") or ""):
                        if vu is None:
                            repris_det += 1
                        d[cle] = x
        except Exception as e:
            print("[warn] cache précédent illisible : %s" % e, file=sys.stderr)

    sortie = {}
    for sym, d in par_soc.items():
        lignes = sorted(d.values(), key=lambda x: -(x.get("part_pct") or 0))
        somme = sum(x.get("part_pct") or 0 for x in lignes)
        sortie[sym] = {
            "detenteurs": lignes,
            "n": len(lignes),
            "part_declaree_pct": round(somme, 2),
            "dernier_depot": max((x.get("depose_le") or "") for x in lignes) if lignes else None,
        }

    doc = {
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "source": "SEC EDGAR — SCHEDULE 13G (déclarations de franchissement de 5 %)",
        "note": ("Seuls les détenteurs de plus de 5 % du capital déposent : la somme "
                 "des parts déclarées ne fait jamais 100 %, et le flottant diffus "
                 "n'y figure pas. Un 13G se dépose à l'anniversaire ou lors d'un "
                 "changement notable, donc une part peut dater de plusieurs mois."),
        "societes": sortie,
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SORTIE, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    with open(SORTIE_JS, "w", encoding="utf-8") as fh:
        fh.write("window.__ACTIONNARIAT__=")
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";")

    n_det = sum(v["n"] for v in sortie.values())
    print("[ok] %d document(s) lu(s), %d illisible(s) — %.0f s"
          % (lus[0], vides[0], time.time() - t0))
    print("[ok] %d sociétés, %d détenteur(s) déclaré(s)" % (len(sortie), n_det))
    print("[ok] reprises de la collecte précédente : %d société(s) entière(s), "
          "%d détenteur(s) que ce passage n'a pas revus" % (repris_soc, repris_det))
    print("[ok] %s : %d Ko" % (os.path.basename(SORTIE),
                               os.path.getsize(SORTIE) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
