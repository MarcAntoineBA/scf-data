#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LE CALENDRIER DE DÉVERROUILLAGE — quand tombe le reste de l'offre, et à qui.

POURQUOI CE COLLECTEUR EXISTE
La fiche d'un jeton affichait « offre en circulation 11,7 % » et s'arrêtait là.
Elle ne disait ni QUAND les 88,3 % restants arrivent sur le marché, ni à QUI ils
reviennent. C'est pourtant le fait le plus lourd que porte un jeton jeune :

    Monad, le 24 novembre 2026 à 05 h 48 UTC — mesuré le 05/09/2026 :
      Team                     10 700 000 000   initiés
      Investors                 4 925 000 000   investisseurs privés
      Investors                   410 416 667   investisseurs privés
      Category Labs Treasury      987 500 000   initiés
      Category Labs Treasury       82 291 667   initiés
      Team (linéaire)           0 → 104,1 M par semaine, sur 1 096 jours
    Soit 17,1 milliards de jetons d'un coup, 438,8 M$ au cours du jour, pour une
    capitalisation de 303 M$ : CENT QUARANTE-CINQ POUR CENT.

Aucune grandeur de la fiche ne le montrait, et aucun ratio ne pouvait le laisser
deviner. La distribution complète, sur les jetons de notre univers qui ont un
calendrier : médiane 5,1 % de la capitalisation sur quatre-vingt-dix jours,
neuvième décile 20,1 %, maximum 144,8 %.

LA PORTE EST GRATUITE, MAIS CE N'EST PAS CELLE DE LA DOCUMENTATION
Vérifié le 05/09/2026, les trois d'affilée :
    api.llama.fi/emissions                          → HTTP 402, « Upgrade to the paid API plan »
    api.llama.fi/emissionsProtocolsList             → HTTP 402
    defillama-datasets.llama.fi/emissionsProtocolsList → HTTP 200, 372 slugs, sans clé
Le jeu de données publie un fichier par protocole sous le même hôte. Mesuré :
355 fichiers sur 372 aboutissent, en 11,1 s à dix fils.

⚠ LE SLUG N'EST PAS L'IDENTIFIANT COINGECKO. Seulement 190 des 355 coïncident.
La carte slug → gecko_id doit être CONSTRUITE en ouvrant les fichiers, jamais
devinée : rattacher un jeton au calendrier d'un autre serait la pire panne que
ce collecteur puisse produire, et elle serait invisible.

CE QUE CETTE DONNÉE N'EST PAS
Elle est DOCUMENTAIRE. Elle vient des prospectus et des tableaux fournis par les
équipes, jamais du contrat. Vérifié : `realTimeData` est vide sur 101 fichiers
sur 101. Aucune de ces dates n'a été confrontée à la chaîne, et le champ
`sources` du JSON est vide partout — les notes citent les documents, jamais une
URL. La fiche doit le dire ; c'est la seule façon honnête de publier un
calendrier qu'on n'a pas vérifié soi-même.

⚠ ET LA TENDANCE EST AU VERROUILLAGE. Deux routes sont déjà passées en 402, et
le dépôt d'adaptateurs qui produisait ces chiffres — github.com/DefiLlama/
emissions-adapters — répond 404 : il était public, il ne l'est plus. On archive
donc, à chaque passage, une copie datée du cache COMPACT. Pas les 467 Mo bruts :
ce qu'on saurait relire dans un an.

LE PIÈGE QUI PRODUIT UN NOMBRE ABSURDE
`spcx` et `cbrs` sont des ACTIONS TOKENISÉES — SpaceX et Cerebras — dont la
source suit les blocages d'introduction en bourse comme du vesting. Leurs lots
s'appellent « Elon Musk », « 180-Day Lock-up », et leur note dit : « Share counts
are the "up to" amounts disclosed in the prospectus ». Le nombre d'ACTIONS du
prospectus multiplié par le prix du JETON donne des déverrouillages à des
centaines de milliards de dollars. C'est la même famille que le « 51 837 T$ »
qu'un stablecoin mal prixé avait déjà produit sur ce site.
Deux discriminants, tous les deux posés : l'identifiant contient
« tokenized-stock » ou « bstocks », ET tout déverrouillage au-delà d'un seuil de
capitalisation qu'on ÉCARTE en le nommant plutôt qu'en le supprimant.
"""

import gzip
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

RACINE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
ARCHIVE = os.path.join(CACHE, "archive_vesting")

BASE = "https://defillama-datasets.llama.fi"
LISTE = BASE + "/emissionsProtocolsList"
FICHIER = BASE + "/emissions/%s"
PAGE = "https://defillama.com/unlocks/%s"

# La carte slug → identifiant CoinGecko, persistée. Le balayage complet coûte
# 11 s et 467 Mo décompressés : on le refait quand la liste des protocoles
# CHANGE DE TAILLE, pas à chaque passage. Entre deux balayages, on ne tire que
# les fichiers dont on a besoin — une centaine.
CARTE = os.path.join(CACHE, "vesting_carte_slugs.json")

FILS = 10
ARCHIVES_GARDEES = 12

# ── Les catégories économiques, en français ──────────────────────────────
# La source les nomme en anglais et les mêle aux libellés de lots. Le lecteur
# doit lire « équipe et initiés », pas « insiders » — et surtout comprendre que
# ces six familles ne se valent pas : un déverrouillage d'initiés ne dit pas la
# même chose qu'un déverrouillage de récompenses de staking.
CATEGORIES_FR = {
    "insiders": "équipe et initiés",
    "privateSale": "investisseurs privés",
    "publicSale": "vente publique",
    "airdrop": "distribution gratuite",
    "noncirculating": "réserves du projet",
    "staking": "récompenses d’immobilisation",
    "farming": "récompenses de liquidité",
    "liquidity": "liquidité",
    "community": "communauté",
    "publicMint": "émission publique",
}
# Celles dont le déverrouillage change la NATURE de ce qu'on détient : des
# jetons qui n'avaient pas de propriétaire vendeur en acquièrent un.
CATEGORIES_INITIES = ("insiders", "privateSale")

# ⚠ SEUIL MESURÉ, PAS CHOISI. Distribution du déverrouillage à quatre-vingt-dix
# jours rapporté à la capitalisation, sur les jetons de notre univers qui ont un
# calendrier (n = 22 valorisables le 05/09/2026) : médiane 5,12 %, neuvième
# décile 20,05 %, et deux valeurs au-dessus — Plasma 84,3 % et Monad 144,8 %.
# Ces deux-là sont VRAIES : Monad déverrouille bien plus que sa capitalisation
# le 24 novembre, et c'est précisément le fait qu'on veut montrer.
# Le seuil n'est donc pas là pour écarter les fortes dilutions, il est là pour
# écarter les PRODUITS QUI N'ONT PAS DE SENS — un nombre d'actions de prospectus
# multiplié par un prix de jeton. À mille pour cent, aucune dilution réelle
# n'est concernée (le maximum observé en est le septième) et les actions
# tokenisées le dépassent de plusieurs ordres.
SEUIL_ABSURDITE_PART_CAPI = 1000.0

# Ce que la source déclare comme action tokenisée, dans l'identifiant lui-même.
MARQUEURS_ACTION_TOKENISEE = ("tokenized-stock", "bstocks", "-stock")


def _ua():
    return {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research"),
            "Accept-Encoding": "gzip"}


def _get(url, timeout=45, essais=3):
    """Une requête, décompressée. Même forme que `fetch_crypto_capture._get`.

    ⚠ L'EN-TÊTE GZIP N'EST PAS UN CONFORT. Sans lui la source sert le JSON brut :
    mesuré, 467 Mo pour les 372 fichiers au lieu de ~90 Mo sur le fil. Ce n'est
    pas la différence entre lent et rapide, c'est la différence entre une
    collecte qui tient dans son créneau et une qui le déborde.

    ⚠ ET UN CORPS TRONQUÉ SE NOMME. `IncompleteRead` remonte sinon en erreur de
    syntaxe JSON, qui ne dit rien de la vraie cause et ne mérite pas le même
    geste : une troncature se rejoue, un JSON mal formé non.
    """
    from http.client import IncompleteRead
    derniere = None
    for essai in range(essais):
        try:
            req = urllib.request.Request(url, headers=_ua())
            with urllib.request.urlopen(req, timeout=timeout) as r:
                brut = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    brut = gzip.decompress(brut)
            return json.loads(brut.decode("utf-8"))
        except IncompleteRead as e:
            derniere = "corps tronqué (%s octets lus)" % len(getattr(e, "partial", b""))
        except urllib.error.HTTPError as e:
            # Un 402 ou un 404 ne se rejouent pas : la porte est fermée, pas
            # encombrée. On le dit tout de suite plutôt que d'attendre trois fois.
            raise RuntimeError("%s → HTTP %s" % (url, e.code))
        except Exception as e:
            derniere = str(e)
        if essai < essais - 1:
            time.sleep(1.5 * (essai + 1))
    raise RuntimeError("%s : %s" % (url, derniere))


def univers_du_site():
    """Nos deux cents jetons, avec leur cours, leur capitalisation et leur profil.

    On prend le prix et la capitalisation DU MÊME cache : une part de
    capitalisation est un rapport, et deux instants différents au numérateur et
    au dénominateur en font un rapport faux. Aller chercher un cours plus frais
    ailleurs rendrait le chiffre plus récent et moins juste.
    """
    for base in (RACINE, CACHE):
        p = os.path.join(base, "crypto_fiches.js")
        if not os.path.exists(p):
            continue
        texte = open(p, encoding="utf-8").read()
        d = json.loads(texte[texte.index("{"):].rstrip().rstrip(";"))
        return {j["id"]: j for j in d.get("jetons") or []}
    raise RuntimeError(
        "crypto_fiches.js introuvable : le calendrier de déverrouillage se "
        "greffe sur l'univers des fiches, il n'en définit pas un second.")


def carte_slugs(slugs, forcer=False):
    """{slug: gecko_id}, construite en ouvrant les fichiers, jamais devinée.

    ⚠ 190 SLUGS SUR 355 SEULEMENT COÏNCIDENT AVEC L'IDENTIFIANT COINGECKO.
    Deviner rattacherait un jeton au calendrier d'un autre — un déverrouillage
    attribué au mauvais actif, sans qu'aucun contrôle ne puisse s'en apercevoir.
    C'est la panne la plus grave que ce collecteur puisse produire ; elle coûte
    onze secondes à éviter.
    """
    ancienne = {}
    if os.path.exists(CARTE) and not forcer:
        try:
            with open(CARTE, encoding="utf-8") as fh:
                doc = json.load(fh)
            ancienne = doc.get("carte") or {}
            # La liste a changé de taille : un protocole est né ou a disparu, et
            # la carte ne peut plus être tenue pour complète.
            if doc.get("n_protocoles") == len(slugs) and ancienne:
                return ancienne, False
        except Exception as e:
            print("[warn] carte des slugs illisible (%s) : on la rebâtit" % e,
                  file=sys.stderr)

    def tete(slug):
        try:
            d = _get(FICHIER % slug, essais=2)
            return slug, d.get("gecko_id")
        except Exception:
            return slug, None

    t0 = time.time()
    neuve = {}
    with ThreadPoolExecutor(max_workers=FILS) as ex:
        for slug, gid in ex.map(tete, slugs):
            if gid:
                neuve[slug] = gid
    print("[info] carte slug → identifiant : %d/%d en %.1f s"
          % (len(neuve), len(slugs), time.time() - t0))
    if len(neuve) < len(slugs) * 0.5:
        raise RuntimeError(
            "seulement %d slugs sur %d ont rendu un identifiant : la source ne "
            "répond pas normalement, on n'écrit RIEN plutôt que de publier un "
            "calendrier amputé de moitié." % (len(neuve), len(slugs)))
    os.makedirs(CACHE, exist_ok=True)
    tmp = CARTE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"n_protocoles": len(slugs), "construite_le": _iso(time.time()),
                   "carte": neuve}, fh, ensure_ascii=False)
    os.replace(tmp, CARTE)
    return neuve, True


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jour(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _mois(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def categorie_du_lot(nom, categories):
    """La catégorie économique d'un lot, depuis la table que la source publie."""
    for cat, libelles in (categories or {}).items():
        if nom in (libelles or []):
            return cat
    return None


def emission_mensuelle(lots):
    """La série mensuelle NETTE, dérivée des séries quotidiennes des lots.

    ⚠ « DÉVERROUILLÉ » EST UN CUMUL, PAS UN FLUX. Le champ `unlocked` d'un point
    quotidien est le total déverrouillé À CETTE DATE, pas ce qui l'a été ce
    jour-là. Le sommer sur un mois donnerait trente fois le stock. On prend donc
    la DERNIÈRE valeur du mois — c'est un solde — et l'émission du mois est la
    différence de deux soldes consécutifs.

    Les brûlages se retranchent : un jeton déverrouillé puis brûlé n'est pas sur
    le marché, et la fiche parle de ce qui pèse sur le cours.
    """
    par_mois = {}
    for lot in lots:
        for p in lot.get("data") or []:
            ts = p.get("timestamp")
            if not ts:
                continue
            m = _mois(ts)
            c = par_mois.setdefault(m, {})
            # Dernier point du mois, lot par lot : deux lots n'ont pas forcément
            # le même dernier jour, et prendre un solde global daté du mois
            # mêlerait des instants différents.
            cle = id(lot)
            if cle not in c or ts >= c[cle][0]:
                c[cle] = (ts, (p.get("unlocked") or 0.0) - (p.get("burned") or 0.0))
    mois = sorted(par_mois)
    if len(mois) < 2:
        return None
    soldes = [sum(v[1] for v in par_mois[m].values()) for m in mois]
    # Le premier mois n'a pas de mois précédent : son émission est son solde.
    flux = [soldes[0]] + [max(0.0, soldes[i] - soldes[i - 1]) for i in range(1, len(soldes))]
    return {"debut": mois[0], "valeurs": [round(x, 2) for x in flux],
            "soldes": [round(x, 2) for x in soldes]}


def deltas_futurs(lot, maintenant):
    """Les variations quotidiennes à venir d'un lot, en jetons.

    ⚠ CETTE FONCTION EXISTE PARCE QUE `unlockEvents` MENT PAR OMISSION.
    Mesuré le 05/09/2026 : Ethena a 5,07 MILLIARDS de jetons à déverrouiller
    jusqu'en avril 2028 — un tiers de son offre maximale — et son champ
    `unlockEvents` ne contient AUCUN événement futur. Onze événements, tous
    passés. Une fiche bâtie sur ce champ aurait écrit « aucune échéance à venir »
    sous un jeton dont un tiers de l'offre reste à sortir.
    La série quotidienne, elle, est complète : 4 053 points futurs, jusqu'au
    6 avril 2028. C'est elle qui fait foi ; `unlockEvents` ne sert plus qu'à
    nommer le bénéficiaire d'une falaise, ce que la série ne dit pas.
    """
    pts = sorted((p for p in (lot.get("data") or []) if p.get("timestamp")),
                 key=lambda p: p["timestamp"])
    out = []
    prec = None
    for p in pts:
        val = (p.get("unlocked") or 0.0) - (p.get("burned") or 0.0)
        if prec is not None and p["timestamp"] > maintenant:
            d = val - prec
            if d > 0:
                out.append((p["timestamp"], d))
        prec = val
    return out


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def prochaines_echeances(lots, categories, evenements, maintenant, offre_max,
                         horizon_j=370):
    """Le calendrier à venir : les falaises une par une, les filets regroupés.

    ⚠ SANS REGROUPEMENT, LE TABLEAU EST ILLISIBLE. Celestia déverrouille
    183 562 jetons PAR JOUR au titre de « R&D & Ecosystem ». Publiées telles
    quelles, ce sont 382 lignes identiques sur un an — un mur que personne ne lit,
    et dans lequel la seule vraie falaise se noie. Regroupées, c'est une ligne :
    « 183 562 jetons par jour jusqu'au 10 septembre 2027, soit 67,0 M au total ».

    La distinction n'est pas cosmétique : une falaise est un ÉVÉNEMENT — une date
    à laquelle un détenteur nommé peut vendre — tandis qu'un filet est un DÉBIT
    permanent. Les confondre efface ce qui rend le calendrier utile.

    Le seuil qui les sépare est RELATIF au lot : dix fois son débit quotidien
    médian, ou un demi pour cent de l'offre maximale. Un seuil absolu classerait
    tout le filet de Celestia en falaises sur un gros jeton, et aucune falaise en
    falaise sur un petit.
    """
    fin = maintenant + horizon_j * 86400
    # Le bénéficiaire nommé, quand la source le donne pour cette minute-là.
    noms = {}
    for e in evenements or []:
        ts = e.get("timestamp") or 0
        for a in e.get("cliffAllocations") or []:
            noms.setdefault(ts, []).append((a.get("recipient"), a.get("category"),
                                            a.get("amount") or 0.0))

    falaises, filets = [], []
    for lot in lots:
        nom = lot.get("label")
        cat = categorie_du_lot(nom, categories)
        d = [(ts, v) for ts, v in deltas_futurs(lot, maintenant) if ts <= fin]
        if not d:
            continue
        med = _median([v for _, v in d])
        seuil = max(med * 10.0, (offre_max or 0) * 0.005 if offre_max else 0.0)
        # Un lot dont TOUS les points se valent est un filet pur : le multiple de
        # la médiane ne peut alors désigner personne, et c'est le bon résultat.
        courant = None
        for ts, v in d:
            if seuil > 0 and v >= seuil:
                if courant:
                    filets.append(courant)
                    courant = None
                falaises.append({"date": _iso(ts), "type": "falaise", "lot": nom,
                                 "categorie": cat, "jetons": v})
                continue
            if courant and abs(v - courant["_taux"]) <= courant["_taux"] * 0.05:
                courant["fin"] = _iso(ts)
                courant["jetons"] += v
                courant["jours"] += 1
            else:
                if courant:
                    filets.append(courant)
                courant = {"date": _iso(ts), "fin": _iso(ts), "type": "filet",
                           "lot": nom, "categorie": cat, "jetons": v,
                           "jours": 1, "_taux": v}
        if courant:
            filets.append(courant)

    # ⚠ UN GROUPE D'UN SEUL JOUR N'EST PAS UN DÉBIT, C'EST UNE FALAISE.
    # Le seuil relatif est AVEUGLE quand un lot n'a qu'une variation future :
    # sa médiane vaut alors cette variation même, et « dix fois la médiane » ne
    # peut désigner personne. Mesuré : ONDO libère 792 millions de jetons le
    # 18 janvier 2027 en une seule fois, XDC 441 millions le 2 janvier, PYTH
    # 1,13 milliard le 19 mai — les trois étaient rangés en filets, c'est-à-dire
    # présentés comme un écoulement régulier alors que ce sont des dates.
    # La durée tranche ce que le seuil ne sait pas trancher.
    vrais_filets = []
    for f in filets:
        taux = f.pop("_taux")
        if f["jours"] <= 1:
            falaises.append({"date": f["date"], "type": "falaise", "lot": f["lot"],
                             "categorie": f["categorie"], "jetons": f["jetons"]})
            continue
        f["par_jour"] = round(taux, 2)
        vrais_filets.append(f)

    out = falaises + vrais_filets
    for e in out:
        e["jetons"] = round(e["jetons"], 2)
        if e["type"] == "falaise":
            # Le nom du bénéficiaire, quand la source l'attache à cette minute
            # et que le montant concorde à 1 % près. Sans cette concordance, on
            # attribuerait à un détenteur nommé une somme qui n'est pas la sienne.
            ts = int(datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc).timestamp())
            for ts_e, lst in noms.items():
                if abs(ts_e - ts) > 86400:
                    continue
                for rec, cat, montant in lst:
                    if montant and abs(montant - e["jetons"]) <= montant * 0.01:
                        e["beneficiaire"] = rec
                        if cat:
                            e["categorie"] = cat
                        # La série est datée au JOUR ; l'événement porte la
                        # MINUTE. « le 24 novembre à 05 h 48 » et « le 25
                        # novembre » ne décrivent pas la même échéance, et c'est
                        # la première qui est juste.
                        e["date"] = _iso(ts_e)
                        break
    out.sort(key=lambda x: (x["date"], -(x.get("jetons") or 0)))
    return out


def fenetre(lots, maintenant, jours):
    """Ce que la fenêtre déverrouille, en jetons — falaises ET filets.

    On lit la série, pas la liste d'événements : c'est un solde à une date moins
    un solde à une autre, la seule mesure qui ne peut rien oublier. La version
    précédente sommait les événements et rendait ZÉRO sur Ethena, dont un tiers
    de l'offre reste pourtant à sortir.
    """
    fin = maintenant + jours * 86400
    total = 0.0
    for lot in lots:
        for ts, v in deltas_futurs(lot, maintenant):
            if ts <= fin:
                total += v
    return total


def par_categorie(echeances, maintenant, jours):
    """La même fenêtre, répartie par catégorie économique."""
    limite = datetime.fromtimestamp(maintenant + jours * 86400, timezone.utc)
    out = {}
    for e in echeances:
        deb = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if deb > limite:
            continue
        if e["type"] == "filet":
            # Un filet à cheval sur la limite ne compte que pour sa part écoulée.
            f = datetime.strptime(e["fin"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            part = 1.0 if f <= limite else max(0.0, (limite - deb).days) / max(1, (f - deb).days)
            v = (e.get("jetons") or 0.0) * part
        else:
            v = e.get("jetons") or 0.0
        c = e.get("categorie") or "autre"
        out[c] = out.get(c, 0.0) + v
    return out


def construire(gid, slug, doc, jeton, maintenant):
    """Le dossier compact d'un jeton."""
    dd = doc.get("documentedData") or {}
    md = doc.get("metadata") or {}
    lots = dd.get("data") or []
    cats = doc.get("categories") or {}
    alloc = dd.get("tokenAllocation") or {}
    sm = doc.get("supplyMetrics") or {}

    prix = jeton.get("prix")
    mcap = (jeton.get("mcap_b") or 0) * 1e9

    mx = sm.get("maxSupply") or sm.get("adjustedSupply")
    ech = prochaines_echeances(lots, cats, md.get("unlockEvents"), maintenant, mx)
    j90 = fenetre(lots, maintenant, 90)
    m12 = fenetre(lots, maintenant, 365)
    cat90 = par_categorie(ech, maintenant, 90)
    cat12 = par_categorie(ech, maintenant, 365)

    def valoriser(jetons):
        if not (prix and jetons):
            return None, None
        usd = jetons * prix
        part = (usd / mcap * 100.0) if mcap > 0 else None
        return round(usd, 2), (round(part, 2) if part is not None else None)

    usd90, part90 = valoriser(j90)
    usd12, part12 = valoriser(m12)

    # ── Le garde-fou du produit qui n'a pas de sens ──────────────────────
    est_action = any(m in (gid or "") for m in MARQUEURS_ACTION_TOKENISEE)
    statut, motif = "mesurable", None
    if est_action:
        statut = "sans_objet"
        motif = ("Action tokenisée : la source suit les blocages d’introduction "
                 "en bourse comme du vesting, et les quantités sont des NOMBRES "
                 "D’ACTIONS tirés du prospectus. Les multiplier par le prix du "
                 "jeton ne mesure rien.")
    elif part90 is not None and part90 > SEUIL_ABSURDITE_PART_CAPI:
        statut = "non_mesurable"
        motif = ("Le déverrouillage à quatre-vingt-dix jours vaudrait %s fois la "
                 "capitalisation. Aucune dilution réelle n’atteint cet ordre — le "
                 "maximum observé sur cet univers est de 145 %% : le nombre de "
                 "jetons et le prix ne décrivent pas la même chose."
                 % round(part90 / 100.0))

    # ── Les lots, avec leur catégorie et le libellé français ─────────────
    lots_out = []
    for lot in lots:
        nom = lot.get("label")
        cat = categorie_du_lot(nom, cats)
        pts = lot.get("data") or []
        dernier = None
        for p in pts:
            if (p.get("timestamp") or 0) <= maintenant:
                dernier = p
        lots_out.append({
            "nom": nom,
            "categorie": cat,
            "categorie_lib": CATEGORIES_FR.get(cat, cat),
            "initie": cat in CATEGORIES_INITIES,
            "deverrouille": round((dernier.get("unlocked") or 0.0), 2) if dernier else 0.0,
            "brule": round((dernier.get("burned") or 0.0), 2) if dernier else 0.0,
            "points": len(pts),
        })

    # ── VESTING OU ÉMISSION : deux calendriers qui ne se lisent pas pareil ──
    # ⚠ DÉFAUT VU À L'ÉCRAN, PAS DANS LES DONNÉES. La fiche de Bitcoin affichait
    # « déverrouillé au contrat 95,6 % », « à qui revient l'offre : récompenses
    # de liquidité 100 % », et une échéance au nom de « MINING REWARDS ».
    # Les nombres étaient JUSTES — 437 bitcoins par jour, c'est bien l'émission
    # des mineurs — mais le CADRE était faux : Bitcoin n'a ni contrat, ni lot
    # bloqué, ni bénéficiaire à qui l'offre « revient ». Un déverrouillage crée
    # un vendeur qui n'existait pas ; une émission de preuve de travail paie un
    # service déjà rendu. Les confondre ferait lire une menace là où il n'y a
    # qu'un coût connu d'avance.
    # Le discriminant est la présence d'une CONTREPARTIE NOMMÉE dans la
    # répartition finale : équipe, investisseurs privés, vente publique. Sur nos
    # 101 jetons, 88 en ont une et 13 n'en ont pas — Bitcoin, Dogecoin,
    # Litecoin, TAO, CAKE… tous des émissions pures.
    fin_alloc = alloc.get("final") or {}
    nature = ("vesting" if any(fin_alloc.get(k) for k in
                               ("insiders", "privateSale", "publicSale"))
              else "emission")

    # ── L'écart entre le contrat et le flottant ──────────────────────────
    # Ce qui est déverrouillé AU CONTRAT n'est pas ce qui circule : des jetons
    # peuvent être libres et non vendus, gardés par une fondation ou immobilisés.
    # Mesuré sur Hyperliquid : 38,8 % de l'offre maximale déverrouillée contre
    # 23,3 % en circulation, soit 15,5 points d'écart. Sur Celestia, −0,2 point.
    # C'est la seule grandeur qui distingue le contrat du flottant réel.
    # ⚠ QUARANTE ET UN JETONS N'ONT AUCUNE ÉCHÉANCE À VENIR, ET TREIZE D'ENTRE
    # EUX SONT ENCORE VERROUILLÉS — BNB, LINK, HBAR, JUP, CRV… Leur série
    # documentée S'ARRÊTE, elle ne se termine pas. Une case vide ferait lire
    # « plus rien ne sortira » ; la vérité est « la source ne documente plus
    # rien au-delà de cette date ». La fiche doit pouvoir dire laquelle.
    fin_serie = 0
    for lot in lots:
        for p in lot.get("data") or []:
            fin_serie = max(fin_serie, p.get("timestamp") or 0)

    dev_total = sum(l["deverrouille"] - l["brule"] for l in lots_out)
    dev_pct = (dev_total / mx * 100.0) if mx else None
    circ_pct = jeton.get("circ_pct")
    ecart = (round(dev_pct - circ_pct, 1)
             if (dev_pct is not None and isinstance(circ_pct, (int, float))) else None)

    return {
        "symbole": jeton.get("symbole"),
        "slug": slug,
        "page": PAGE % slug,
        "statut": statut,
        "nature": nature,
        "motif": motif,
        "offre_max": mx,
        "calendrier_jusqu_au": _jour(fin_serie) if fin_serie else None,
        "calendrier_epuise": bool(fin_serie and fin_serie <= maintenant),
        "deverrouille_pct": round(dev_pct, 1) if dev_pct is not None else None,
        "circulant_pct": circ_pct,
        "ecart_contrat_flottant_pt": ecart,
        "allocation": {
            # ⚠ LES TROIS N'ONT PAS LA MÊME BASE, ET LES MULTIPLIER EST FAUX.
            # « actuelle » est une part de ce qui est DÉJÀ SORTI ; « finale » une
            # part de l'offre de la fin. J'ai d'abord cru pouvoir dériver la
            # seconde de la première en la multipliant par le taux de
            # déverrouillage : sur Monad le produit tombait au dixième près
            # (75,6 % × 50,1 % = 37,9 %, part finale 37,9 %). C'était une
            # COÏNCIDENCE — cette catégorie-là était déjà libérée à 100 %.
            # Rejoué sur l'univers, le produit dépasse la part finale de 19 à 26
            # points sur PENGU, S et BNB, et BNB encode ses brûlages en parts
            # NÉGATIVES, où la multiplication ne veut plus rien dire.
            # La grandeur cherchée existe déjà et vient de la source :
            # « avancement » dit, catégorie par catégorie, quelle fraction de la
            # part finale a été distribuée. On la publie, on n'en calcule aucune.
            "actuelle": alloc.get("current") or {},
            "finale": alloc.get("final") or {},
            "avancement": alloc.get("progress") or {},
        },
        "categories_lib": {k: CATEGORIES_FR.get(k, k)
                           for k in set(list((alloc.get("final") or {}).keys())
                                        + list(cats.keys()))},
        "lots": lots_out,
        "fenetres": {
            "j90": {"jetons": round(j90, 2), "usd": usd90, "part_capi_pct": part90,
                    "par_categorie": {k: round(v, 2) for k, v in cat90.items()}},
            "m12": {"jetons": round(m12, 2), "usd": usd12, "part_capi_pct": part12,
                    "par_categorie": {k: round(v, 2) for k, v in cat12.items()}},
        },
        # Quarante échéances au plus : au-delà, la fiche ne les lit pas et le
        # cache double de taille pour rien.
        # Les vingt-cinq premières : au-delà, la fiche ne les montre pas et le
        # cache double de taille. Le total, lui, porte sur TOUTES — `n_echeances`
        # dit combien ont été laissées de côté, jamais un silence.
        "prochains": [dict(e, categorie_lib=CATEGORIES_FR.get(e.get("categorie"),
                                                              e.get("categorie")),
                           initie=e.get("categorie") in CATEGORIES_INITIES,
                           usd=(round((e.get("jetons") or 0) * prix, 2) if prix else None),
                           part_capi_pct=(round((e.get("jetons") or 0) * prix / mcap * 100, 3)
                                          if (prix and mcap > 0) else None))
                      for e in ech[:25]],
        "n_echeances": len(ech),
        "emission_mensuelle": emission_mensuelle(lots),
        # Les notes TELLES QUELLES. Elles disent quelle ligne est un fait et
        # quelle ligne est une hypothèse ; les reformuler serait effacer
        # précisément ce qui les rend utiles.
        "notes": md.get("notes") or [],
    }


def raison_sans_calendrier(jeton):
    """Pourquoi ce jeton n'a pas de calendrier — jamais un vide muet.

    Sur les 99 jetons de notre univers qui n'en ont pas, la grande majorité n'en
    a pas PAR NATURE : ether et bitcoin n'ont jamais eu de vesting, stETH et
    WBTC sont des enveloppes, l'or tokenisé suit un actif. Laisser une case vide
    ferait croire à un trou de collecte, et les deux appellent des gestes
    opposés.
    """
    a = jeton.get("archetype")
    if a in ("enveloppe", "staking_liquide"):
        return ("Enveloppe : ce jeton représente un autre actif, dont il suit "
                "l’émission. Son calendrier est celui du sous-jacent.")
    if a in ("matiere_tokenisee", "action_tokenisee"):
        return ("Actif tokenisé : l’émission suit la matière ou le titre "
                "sous-jacent, pas un calendrier de projet.")
    if a == "reserve_valeur":
        return ("Émission connue d’avance et sans attributaire : ce jeton n’a "
                "jamais eu de calendrier de déverrouillage.")
    return ("La source ne publie pas de calendrier pour ce jeton. Ce n’est pas "
            "un défaut de collecte : tous les jetons n’en ont pas.")


def archiver(chemin):
    """Une copie datée du cache COMPACT, à chaque passage.

    L'endpoint n'est pas documenté, deux routes voisines sont déjà passées au
    payant et le dépôt d'adaptateurs qui produisait ces chiffres répond 404. Le
    jour où celle-ci se ferme, on veut pouvoir relire ce qu'on a su.
    On archive le compact, pas les 467 Mo bruts : ce qu'on saurait relire.
    """
    os.makedirs(ARCHIVE, exist_ok=True)
    jour = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cible = os.path.join(ARCHIVE, "crypto_vesting_%s.json" % jour)
    shutil.copy2(chemin, cible)
    anciens = sorted(f for f in os.listdir(ARCHIVE) if f.endswith(".json"))
    for f in anciens[:-ARCHIVES_GARDEES]:
        try:
            os.remove(os.path.join(ARCHIVE, f))
        except Exception:
            pass
    return cible, len(anciens[-ARCHIVES_GARDEES:])


def ecrire(doc, force=False):
    """Publie — sauf si l'univers a fondu d'un quart ou plus.

    ⚠ LA PANNE QUE CE DÉPÔT A DÉJÀ PAYÉE DEUX FOIS. Un passage sur quatre cents
    sociétés avait effacé les paquets de quatre cent trente-cinq autres ; un
    autre remplaçait un cache de deux cents jetons par un de deux. Un refus qui
    ne se dit pas est un cache qu'on croit écrit : il est bruyant.

    Les deux fichiers s'écrivent de la même façon, atomiquement — le contrôle
    lit le `.json` en premier, et un `.json` tronqué à côté d'un `.js` valide
    ferait tomber le garde-fou sur le fichier que le site ne sert pas.
    """
    os.makedirs(CACHE, exist_ok=True)
    js = os.path.join(CACHE, "crypto_vesting_cache.js")
    jsonf = os.path.join(CACHE, "crypto_vesting_cache.json")
    if not force and os.path.exists(jsonf):
        try:
            with open(jsonf, encoding="utf-8") as fh:
                ancien = json.load(fh)
            avant = len(ancien.get("jetons") or {})
            apres = len(doc.get("jetons") or {})
            if avant >= 20 and apres < avant * 0.75:
                print("[refus] calendrier NON écrit : %d jetons contre %d dans le "
                      "cache existant. Une collecte partielle ne remplace pas une "
                      "collecte." % (apres, avant), file=sys.stderr)
                return None
        except Exception as e:
            print("[warn] cache précédent illisible (%s) : on écrit sans pouvoir "
                  "comparer" % e, file=sys.stderr)
    tmp = jsonf + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, jsonf)
    tmpjs = js + ".tmp"
    with open(tmpjs, "w", encoding="utf-8") as fh:
        fh.write("window.__CRYPTO_VESTING__=")
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    os.replace(tmpjs, js)
    return jsonf


def main():
    t0 = time.time()
    force = "--force" in sys.argv
    refaire_carte = "--carte" in sys.argv

    nos = univers_du_site()
    print("[info] univers du site : %d jetons" % len(nos))

    slugs = _get(LISTE)
    if not isinstance(slugs, list) or len(slugs) < 50:
        raise RuntimeError(
            "la liste des protocoles rend %s : la source ne répond pas "
            "normalement, on n'écrit RIEN." % type(slugs).__name__)
    print("[info] protocoles au catalogue : %d" % len(slugs))

    carte, rebatie = carte_slugs(slugs, forcer=refaire_carte)
    if not rebatie:
        print("[info] carte des slugs reprise du cache (%d entrées)" % len(carte))
    inv = {g: s for s, g in carte.items()}

    cibles = [(g, inv[g]) for g in nos if g in inv]
    print("[info] jetons avec un calendrier : %d sur %d" % (len(cibles), len(nos)))

    def charger(t):
        g, slug = t
        try:
            return g, slug, _get(FICHIER % slug)
        except Exception as e:
            print("[warn] %s (%s) : %s" % (nos[g].get("symbole"), slug, e),
                  file=sys.stderr)
            return g, slug, None

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=FILS) as ex:
        lus = list(ex.map(charger, cibles))
    print("[info] %d fichiers lus en %.1f s"
          % (sum(1 for x in lus if x[2]), time.time() - t1))

    echoues = [nos[g].get("symbole") for g, _, d in lus if not d]
    if len(echoues) > max(3, len(cibles) * 0.2):
        raise RuntimeError(
            "%d fichiers sur %d n'ont pas répondu : la source est en panne, on "
            "n'écrit RIEN plutôt que de publier un calendrier amputé."
            % (len(echoues), len(cibles)))

    maintenant = int(time.time())
    jetons = {}
    ecartes = []
    rt_non_vide = 0
    for g, slug, d in lus:
        if not d:
            continue
        if (d.get("metadata") or {}).get("realTimeData"):
            rt_non_vide += 1
        fiche = construire(g, slug, d, nos[g], maintenant)
        if fiche["statut"] != "mesurable":
            ecartes.append((fiche["symbole"], fiche["statut"]))
        jetons[g] = fiche

    sans = {}
    for g, j in nos.items():
        if g in jetons:
            continue
        sans[g] = {"symbole": j.get("symbole"), "raison": raison_sans_calendrier(j)}

    doc = {
        "genere_le": _iso(time.time()),
        "source": "DefiLlama — jeu de données public defillama-datasets.llama.fi/emissions",
        "univers": len(nos),
        "protocoles_catalogue": len(slugs),
        "couverture": {"avec_calendrier": len(jetons), "sans_calendrier": len(sans),
                       "ecartes": len(ecartes)},
        "seuil_absurdite_part_capi": SEUIL_ABSURDITE_PART_CAPI,
        "categories_lib": CATEGORIES_FR,
        "methode": (
            "Les quantités et les dates viennent des tableaux publiés par les "
            "projets, repris par DefiLlama. Un déverrouillage en FALAISE est un "
            "montant instantané ; un déverrouillage LINÉAIRE est un débit "
            "hebdomadaire, et les deux ne s'additionnent pas. La valeur en "
            "dollars et la part de capitalisation sont calculées avec le cours "
            "et la capitalisation du MÊME cache que les fiches — un rapport dont "
            "le numérateur et le dénominateur viennent de deux instants "
            "différents est un rapport faux."),
        "avertissement": (
            "⚠ CE CALENDRIER EST DOCUMENTAIRE, JAMAIS VÉRIFIÉ SUR LA CHAÎNE. Il "
            "vient des prospectus et des tableaux fournis par les équipes. "
            "Mesuré : le champ de vérification en temps réel de la source est "
            "vide sur la totalité des jetons collectés, et son champ de sources "
            "l'est aussi — les notes citent les documents, jamais une adresse. "
            "Une date peut donc être juste sur le papier et fausse dans le "
            "contrat, et rien ici ne permettrait de le voir."),
        "realTimeData_non_vide": rt_non_vide,
        "ecartes": [{"symbole": s, "statut": st} for s, st in ecartes],
        "jetons": jetons,
        "sans_calendrier": sans,
    }

    chemin = ecrire(doc, force=force)
    if not chemin:
        return 1
    poids = os.path.getsize(chemin)
    arch, gardees = archiver(chemin)
    print("[ok] calendrier : %d jetons avec, %d sans, %d écartés — %.1f Ko en %.1f s"
          % (len(jetons), len(sans), len(ecartes), poids / 1024.0, time.time() - t0))
    print("[ok] archive %s (%d gardées)" % (os.path.basename(arch), gardees))
    if rt_non_vide:
        print("[info] %d jeton(s) portent une vérification en temps réel — "
              "l'avertissement du cache doit être revu" % rt_non_vide)

    # Le classement, pour l'œil : ce que la page montrera en tête.
    tri = sorted((j for j in jetons.values()
                  if j["statut"] == "mesurable"
                  and (j["fenetres"]["j90"]["part_capi_pct"] or 0) > 0),
                 key=lambda j: -(j["fenetres"]["j90"]["part_capi_pct"] or 0))
    print("\n[recap] déverrouillage à 90 jours, en part de capitalisation :")
    for j in tri[:10]:
        f = j["fenetres"]["j90"]
        print("  %-8s %8.2f %%   %10.1f M$   %s"
              % (j["symbole"], f["part_capi_pct"], (f["usd"] or 0) / 1e6, j["slug"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
