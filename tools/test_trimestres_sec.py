#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_trimestres_sec.py — Garde-fou : la série trimestrielle doit se refermer sur
l'exercice annuel, et le bilan ne doit JAMAIS être reconstitué par soustraction.

CE QU'IL EMPÊCHE DE REVENIR

Le trimestre de clôture n'est presque jamais balisé. Mesuré à la source le
05/09/2026 : sur JPMorgan, ZÉRO des dix-neuf clôtures annuelles porte un point de
quatre-vingt-dix jours sous `NetIncomeLoss` ; sur Exxon, ZÉRO sur dix-neuf. La
société publie trois trimestres et un exercice, jamais le quatrième. Il faut donc
le RECONSTITUER — exercice complet moins le cumul à neuf mois — et cette
soustraction est le seul endroit du collecteur où l'on fabrique un chiffre.

Deux façons de la rater, et elles ne se voient ni l'une ni l'autre à l'écran :

  · ELLE NE SE REFERME PAS. Les quatre trimestres ne totalisent plus l'exercice
    qu'ils découpent. La fiche affiche alors un graphe annuel et un graphe
    trimestriel qui se contredisent de quelques pour cent, sans qu'aucun des deux
    n'ait l'air faux. C'est arrivé pendant l'écriture, deux fois :
      – Timberland Bancorp, exercice clos le 30/09/2013 : les quatre trimestres
        pris au dernier dépôt totalisaient 6 983 k$ pour un exercice de 4 757,
        soit 47 % de trop, parce que l'annexe « données trimestrielles » du 10-K
        est rebalisée d'une année sur l'autre avec des valeurs incohérentes ;
      – Exxon 2022 : l'exercice valait 413 680 M$ sous `Revenues` pendant que ses
        quatre trimestres totalisaient 400 940 sous l'étiquette 606, que la
        société avait cessé de déposer en annuel sans cesser de la déposer en
        trimestriel. 3,1 % d'écart entre deux lignes de la même fiche.

  · ON SOUSTRAIT UN SOLDE. L'actif, le passif, les capitaux propres et la
    trésorerie sont des grandeurs d'INSTANT : le bilan du 31 décembre n'est pas
    la somme de quatre trimestres, et retrancher celui du 30 septembre ne produit
    pas un quatrième trimestre — ça produit une VARIATION qu'on afficherait comme
    un solde. Un actif de 464 Md$ deviendrait 12 Md$ ; ça reste un nombre
    plausible, et rien à l'écran ne dirait que ce n'en est pas un.

⚠ UN GARDE QUI REFUSE TOUT EST AUSSI INUTILE QU'UN GARDE QUI NE REFUSE RIEN.
D'où les contre-épreuves, qui pèsent autant que les cas réels : une chaîne
semestrielle ne doit produire AUCUN trimestre, un solde doit continuer d'être lu
à chaque fin de trimestre, une société sans chiffre d'affaires doit garder ses
trimestres de résultat, et une étiquette qui change au milieu de la série doit
être recousue plutôt que de couper la série en deux.

Lancement : python3 tools/test_trimestres_sec.py
"""

import os
import sys
import pathlib

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "scripts"))
import fetch_sec_fundamentals as F  # noqa: E402

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-56s %-22s %s"
          % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


# ─────────────────────────────────────────────────────────────────────────
# Des faits SYNTHÉTIQUES, à la forme exacte de `companyfacts`
# ─────────────────────────────────────────────────────────────────────────
# Aucun appel réseau : la SEC est gratuite mais lente, et un garde qui dépend
# d'internet finit par être désarmé le jour où il échoue pour une autre raison.
# Les chiffres reproduisent la forme réelle mesurée sur Exxon 2025 — cumuls à
# 89, 180, 272 et 364 jours, quatrième trimestre jamais balisé.

def duree(debut, fin, val, forme="10-Q", depose="2026-02-01", accn="0-1"):
    # ⚠ `fp` VAUT « FY » SUR TOUT CE QUE PORTE UN 10-K, Y COMPRIS SUR DES POINTS
    # DE QUATRE-VINGT-DIX JOURS. Mesuré sur AAPL : les huit points déposés sous
    # `Revenues` sont tous trimestriels et tous étiquetés FY. Les faits
    # synthétiques reproduisent ce piège, sans quoi le contrôle validerait un
    # code qui ferait confiance à `fp` — ce que la réalité interdit.
    return {"start": debut, "end": fin, "val": val, "form": forme,
            "fp": "FY" if forme != "10-Q" else "Q?",
            "filed": depose, "accn": accn}


def instant(fin, val, forme="10-Q", depose="2026-02-01", accn="0-1"):
    return {"end": fin, "val": val, "form": forme,
            "fp": "FY" if forme != "10-Q" else "Q?",
            "filed": depose, "accn": accn}


def bloc(*concepts):
    """{taxonomie: {concept: {units: {unité: points}}}} — la forme de l'API."""
    out = {"us-gaap": {}}
    for nom, unite, points in concepts:
        out["us-gaap"][nom] = {"units": {unite: points}}
    return out


# Un exercice civil 2025, publié comme le publie un déposant réel : trois cumulés
# en 10-Q, l'exercice en 10-K, AUCUN quatrième trimestre balisé.
CUMULS_2025 = [
    duree("2025-01-01", "2025-03-31", 100.0),
    duree("2025-01-01", "2025-06-30", 210.0),
    duree("2025-01-01", "2025-09-30", 330.0),
    duree("2025-01-01", "2025-12-31", 500.0, forme="10-K"),
]
# Le même exercice pour un poste de BILAN : quatre soldes, rien à soustraire.
SOLDES_2025 = [
    instant("2025-03-31", 1000.0),
    instant("2025-06-30", 1100.0),
    instant("2025-09-30", 1200.0),
    instant("2025-12-31", 1300.0, forme="10-K"),
]


def par_fin(serie):
    return {f: round(v[0], 6) for f, v in serie.items()}


# Un point ABSENT ne doit pas faire exploser le garde : une garde qui s'écroule
# sur une KeyError dit « quelque chose ne va pas » là où on veut lire « le
# trimestre de clôture a disparu ». Éprouvé en mutant le code exprès : sans ce
# tampon, restreindre la fenêtre trimestrielle plantait la garde au lieu de la
# faire parler.
VIDE = (None,) * 7


def point(serie, fin):
    return serie.get(fin) or VIDE


def ligne(lignes, i):
    try:
        return lignes[i]
    except IndexError:
        return {}


def main():
    print("LE QUATRIÈME TRIMESTRE, QUI N'EST PAS BALISÉ")
    flux = F._trimestres(bloc(("NetIncomeLoss", "USD", CUMULS_2025)),
                         ["NetIncomeLoss"], instant=False, devise="USD")
    v = par_fin(flux)
    verifie("les quatre trimestres sont rendus", sorted(v),
            ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
    verifie("le premier est déposé tel quel", v.get("2025-03-31"), 100.0)
    verifie("le deuxième est 210 − 100", v.get("2025-06-30"), 110.0)
    verifie("le troisième est 330 − 210", v.get("2025-09-30"), 120.0)
    verifie("le quatrième est 500 − 330", v.get("2025-12-31"), 170.0)
    verifie("LA SÉRIE SE REFERME SUR L'EXERCICE", round(sum(v.values()), 6), 500.0)
    verifie("le trimestre de clôture porte son calcul",
            bool(point(flux, "2025-12-31")[5]), True)
    verifie("le premier trimestre n'en porte pas — il est déposé",
            point(flux, "2025-03-31")[5], None)
    verifie("le trimestre de clôture s'ouvre le lendemain du cumul",
            point(flux, "2025-12-31")[3], "2025-10-01")

    print()
    print("LE BILAN NE SE SOUSTRAIT PAS")
    soldes = F._trimestres(bloc(("Assets", "USD", SOLDES_2025)),
                           ["Assets"], instant=True, devise="USD")
    s = par_fin(soldes)
    verifie("les quatre soldes sont lus tels quels",
            [s.get(k) for k in sorted(s)], [1000.0, 1100.0, 1200.0, 1300.0])
    verifie("AUCUN solde ne porte de calcul",
            any(x[5] for x in soldes.values()), False)
    verifie("aucune variation ne s'est glissée dans la série",
            100.0 in s.values(), False)

    # La même chose vue de bout en bout, par le chemin que le collecteur emprunte
    # vraiment : un paquet complet, flux ET bilan, passé par `construire`.
    print()
    print("DE BOUT EN BOUT — le paquet complet, comme en production")
    faits = bloc(
        ("Revenues", "USD", CUMULS_2025 + [
            duree("2024-01-01", "2024-12-31", 400.0, forme="10-K"),
        ]),
        ("NetIncomeLoss", "USD", [
            duree("2025-01-01", "2025-03-31", 10.0),
            duree("2025-01-01", "2025-06-30", 21.0),
            duree("2025-01-01", "2025-09-30", 33.0),
            duree("2025-01-01", "2025-12-31", 50.0, forme="10-K"),
            duree("2024-01-01", "2024-12-31", 40.0, forme="10-K"),
        ]),
        ("Assets", "USD", SOLDES_2025 + [instant("2024-12-31", 900.0,
                                                 forme="10-K")]),
        ("StockholdersEquity", "USD", SOLDES_2025 + [instant("2024-12-31", 500.0,
                                                             forme="10-K")]),
        # Le nombre MOYEN d'actions, en cumulé et en HAUSSE : c'est le cas où la
        # soustraction rendrait un nombre positif, plausible et faux. Sans lui
        # dans ce paquet, la garde des grandeurs non additives ne serait jamais
        # sollicitée par le chemin réel — éprouvé en mutant le collecteur.
        ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", [
            duree("2025-01-01", "2025-03-31", 1000.0),
            duree("2025-01-01", "2025-06-30", 1100.0),
            duree("2025-01-01", "2025-09-30", 1200.0),
            duree("2025-01-01", "2025-12-31", 1300.0, forme="10-K"),
        ]),
        ("EarningsPerShareDiluted", "USD/shares", [
            duree("2025-01-01", "2025-03-31", 0.01),
            duree("2025-01-01", "2025-06-30", 0.02),
            duree("2025-01-01", "2025-09-30", 0.03),
            duree("2025-01-01", "2025-12-31", 0.04, forme="10-K"),
        ]),
    )
    bati = F.construire(faits) or {"resume": {}, "trimestres": []}
    trims = bati["trimestres"]
    verifie("quatre trimestres bâtis", len(trims), 4)
    verifie("les produits se referment sur l'exercice",
            round(sum(t["revenue"] for t in trims), 6), 500.0)
    verifie("le résultat net aussi",
            round(sum(t["net_income"] for t in trims), 6), 50.0)
    verifie("le résumé léger accompagne l'index",
            bati["resume"].get("trim"),
            {"n": 4, "premier": "2025-03-31", "dernier": "2025-12-31"})
    verifie("l'exercice ne signale AUCUN écart de bouclage",
            any(t.get("_ecart_exercice") for t in trims), False)

    # LA GARDE QUI COMPTE : aucune grandeur d'instant ne doit apparaître dans les
    # champs reconstitués, d'aucun trimestre, jamais. Ni aucune grandeur NON
    # ADDITIVE — le nombre moyen d'actions et le bénéfice par action — pour la
    # raison expliquée plus bas.
    def reconstitues(lignes, interdits):
        return sorted({champ
                       for t in lignes
                       for champs in (t.get("_reconstitue_champs") or {}).values()
                       for champ in champs
                       if champ in interdits})

    verifie("AUCUNE grandeur d'instant n'est reconstituée",
            reconstitues(trims, F.INSTANTS), [])
    verifie("AUCUNE grandeur non additive n'est reconstituée",
            reconstitues(trims, F.NON_ADDITIFS), [])
    verifie("l'actif du dernier trimestre est le solde, pas la variation",
            ligne(trims, -1).get("assets"), 1300.0)
    verifie("le trimestre de clôture est marqué",
            ligne(trims, -1).get("_reconstitue"), True)
    # `revenue_total` porte la même étiquette `Revenues` que `revenue` : il est
    # donc reconstitué lui aussi, et la liste le dit. On l'écrit en toutes
    # lettres plutôt que de vérifier « au moins ceci » — une marque qui
    # apparaîtrait ou disparaîtrait ailleurs doit faire réagir ce contrôle.
    verifie("et il nomme les champs reconstitués",
            sorted(c
                   for l in (ligne(trims, -1).get("_reconstitue_champs")
                             or {}).values()
                   for c in l),
            ["net_income", "revenue", "revenue_total"])

    print()
    print("LE NOMBRE MOYEN D'ACTIONS NE SE SOUSTRAIT PAS NON PLUS")
    # Une moyenne pondérée sur neuf mois moins une moyenne sur trois mois ne fait
    # pas six mois d'actions : ça ne fait rien. Mesuré avant correction sur les
    # huit sociétés d'essai, la soustraction rendait 145 nombres d'actions
    # NÉGATIFS sur 474 trimestres — JPMorgan 46 fois, Apple 36, NVIDIA 25. Ils
    # n'étaient écartés que par la garde du signe, c'est-à-dire par chance : chez
    # une société dont le nombre d'actions monte, le résultat aurait été positif,
    # plausible et faux.
    #
    # Ici les moyennes MONTENT — c'est le cas dangereux, celui qu'aucune garde de
    # signe n'attrape.
    actions = bloc(("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", [
        duree("2025-01-01", "2025-03-31", 1000.0),
        duree("2025-01-01", "2025-06-30", 1100.0),
        duree("2025-01-01", "2025-09-30", 1200.0),
        duree("2025-01-01", "2025-12-31", 1300.0, forme="10-K"),
    ]))
    # `additif` est lu dans la table du collecteur, pas écrit en dur : retirer
    # `shares_diluted` de `NON_ADDITIFS` doit faire tomber ce contrôle.
    v = par_fin(F._trimestres(
        actions, F.CONCEPTS["shares_diluted"], devise="USD",
        additif=("shares_diluted" not in F.NON_ADDITIFS)))
    verifie("seul le trimestre DÉPOSÉ est servi", v, {"2025-03-31": 1000.0})
    verifie("aucune différence de moyennes n'est publiée",
            [100.0 in v.values(), 1300.0 in v.values()], [False, False])

    print()
    print("LE PIÈGE TIMBERLAND — l'annexe trimestrielle du 10-K qui ment")
    # Reproduit le cas réel : le 10-K de 2015 rebalise les trimestres de 2013
    # avec des valeurs qui ne totalisent plus l'exercice. Le dépôt le plus récent
    # gagne partout ailleurs dans ce collecteur ; ici il doit PERDRE contre la
    # chaîne, qui se referme par construction.
    menteur = bloc(("NetIncomeLoss", "USD", CUMULS_2025 + [
        duree("2025-04-01", "2025-06-30", 999.0, forme="10-K",
              depose="2027-12-07"),
        duree("2025-07-01", "2025-09-30", 888.0, forme="10-K",
              depose="2027-12-07"),
    ]))
    v = par_fin(F._trimestres(menteur, ["NetIncomeLoss"], devise="USD"))
    verifie("la chaîne l'emporte sur l'annexe rebalisée",
            [v.get("2025-06-30"), v.get("2025-09-30")], [110.0, 120.0])
    verifie("et la série se referme quand même", round(sum(v.values()), 6), 500.0)

    print()
    print("CONTRE-ÉPREUVES — ce qui ne doit PAS être fabriqué")
    # Un déposant semestriel : la différence de deux cumuls distants de six mois
    # n'est pas un trimestre, et l'écrire doublerait le chiffre du lecteur.
    semestriel = bloc(("Revenues", "USD", [
        duree("2025-01-01", "2025-06-30", 210.0),
        duree("2025-01-01", "2025-12-31", 500.0, forme="10-K"),
    ]))
    verifie("une chaîne semestrielle ne rend AUCUN trimestre",
            par_fin(F._trimestres(semestriel, ["Revenues"], devise="USD")), {})

    # Les trimestres déposés tels quels, sans aucune chaîne : ils passent.
    isoles = bloc(("Revenues", "USD", [
        duree("2025-01-01", "2025-03-31", 100.0),
        duree("2025-04-01", "2025-06-30", 110.0),
    ]))
    verifie("des trimestres isolés sont servis tels quels",
            par_fin(F._trimestres(isoles, ["Revenues"], devise="USD")),
            {"2025-03-31": 100.0, "2025-06-30": 110.0})

    # L'ÉTIQUETTE QUI MIGRE : `SalesRevenueNet` puis, à partir de 2025, la 606.
    # Une série qui choisirait une seule étiquette perdrait la moitié du temps.
    migrante = bloc(
        ("SalesRevenueNet", "USD", [
            duree("2024-01-01", "2024-03-31", 60.0),
            duree("2024-01-01", "2024-06-30", 130.0),
        ]),
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD",
         CUMULS_2025),
    )
    v = par_fin(F._trimestres(
        migrante, F.CONCEPTS["revenue"], devise="USD"))
    verifie("les deux étiquettes sont recousues, pas choisies",
            sorted(v), ["2024-03-31", "2024-06-30",
                        "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])

    # LA BANQUE SANS CHIFFRE D'AFFAIRES : mesuré sur Timberland Bancorp, le
    # concept `Revenues` est absent de son paquet et `NetIncomeLoss` y porte 63
    # fins de trimestre. Un axe bâti sur le seul chiffre d'affaires rendrait zéro.
    banque = bloc(
        ("NetIncomeLoss", "USD", CUMULS_2025 + [
            duree("2024-01-01", "2024-12-31", 400.0, forme="10-K")]),
        ("Assets", "USD", SOLDES_2025 + [instant("2024-12-31", 900.0,
                                                 forme="10-K")]),
        ("StockholdersEquity", "USD", SOLDES_2025 + [
            instant("2024-12-31", 500.0, forme="10-K")]),
    )
    bati = F.construire(banque) or {"trimestres": []}
    verifie("une société sans produits garde ses trimestres de résultat",
            len(bati["trimestres"]), 4)

    print()
    print("L'INTERRUPTEUR")
    coupe = F.construire(faits, avec_trimestres=False) or {"resume": {},
                                                           "trimestres": None}
    verifie("--sans-trimestres coupe la construction", coupe["trimestres"], [])
    verifie("et il ne pose pas de résumé trompeur dans l'index",
            coupe["resume"].get("trim"), None)
    verifie("l'option est lue", F._options(["--sans-trimestres"])["trimestres"],
            False)
    verifie("et elle est active par défaut", F._options([])["trimestres"], True)

    print()
    print("LE COLLECTEUR RÉEL, S'IL A DÉJÀ TOURNÉ ICI")
    # Le contrôle précédent est hermétique — c'est ce qui le rend fiable. Celui-ci
    # regarde la production quand elle existe, parce qu'un garde qui n'a jamais vu
    # de vraies données ne prouve que ce qu'on a bien voulu lui montrer. Il
    # S'ABSTIENT en l'absence de cache, il ne rate pas.
    import glob
    import json
    fichiers = sorted(glob.glob(str(F.CACHE_DIR / "sec_trim_*.json")))
    if not fichiers:
        print("  (aucun sec_trim_*.json dans le cache — contrôle sauté)")
    else:
        fautifs, lignes = [], 0
        for chemin in fichiers[:40]:
            try:
                with open(chemin, encoding="utf-8") as fh:
                    paquet = json.load(fh)
            except Exception:
                continue
            for sym, corps in (paquet.get("societes") or {}).items():
                for t in corps.get("trimestres") or []:
                    lignes += 1
                    for champs in (t.get("_reconstitue_champs") or {}).values():
                        for champ in champs:
                            if champ in F.INSTANTS or champ in F.NON_ADDITIFS:
                                fautifs.append("%s %s %s"
                                               % (sym, t.get("fin"), champ))
        print("  %d trimestres relus dans %d paquet(s)" % (lignes,
                                                           len(fichiers[:40])))
        verifie("aucun solde ni aucune moyenne reconstitués en production",
                fautifs[:3], [])

    # ══════════════════════════════════════════════════════════════════════
    # CE QUE LES TRENTE-CINQ PREMIERS CONTRÔLES LAISSAIENT PASSER
    # ══════════════════════════════════════════════════════════════════════
    # Une relecture adversariale a muté le collecteur de quatre façons sans en
    # faire tomber un seul : la fusion des paquets désarmée (la panne que ce
    # dépôt a DÉJÀ payée — quatre cents sociétés effaçant quatre cent
    # trente-cinq paquets), le seuil de bouclage porté à 1 000 %, l'écriture de
    # l'écart supprimée. Un contrôle qu'on ne peut pas faire échouer ne protège
    # rien. Ces quatre-là exercent donc les fonctions elles-mêmes.
    print()
    print("LA FUSION, ET LES SEUILS — ce qui n'était pas exercé")

    # 1. La fusion doit REPRENDRE une société absente de la passe courante.
    import tempfile, shutil
    bac = tempfile.mkdtemp(prefix="trim_fusion_")
    try:
        ancien_out = F.OUT_DIR
        F.OUT_DIR = pathlib.Path(bac)
        (F.OUT_DIR / ("sec_trim_%s.json" % F._initiale("ZZTEST"))).write_text(
            json.dumps({"societes": {"ZZTEST": {"symbole": "ZZTEST",
                                                "trimestres": [{"fin": "2020-03-31"}]}}}),
            encoding="utf-8")
        paquets = {}
        F._fusionner_trimestres(paquets)
        repris = any("ZZTEST" in v for v in paquets.values())
        verifie("la fusion reprend une société absente de la passe", repris, True)

        # 2. Elle ne doit PAS écraser la version fraîche par l'ancienne.
        paquets2 = {F._initiale("ZZTEST"): {"ZZTEST": {"symbole": "ZZTEST",
                                                       "trimestres": [{"fin": "2026-06-30"}]}}}
        F._fusionner_trimestres(paquets2)
        garde = paquets2[F._initiale("ZZTEST")]["ZZTEST"]["trimestres"][0]["fin"]
        verifie("et elle ne remplace pas la version fraîche par l'ancienne",
                garde, "2026-06-30")
    finally:
        F.OUT_DIR = ancien_out
        shutil.rmtree(bac, ignore_errors=True)

    # 3. Le seuil de bouclage doit rester un seuil, pas une porte ouverte.
    verifie("le seuil de bouclage reste sous 2 %", F.ECART_BOUCLAGE < 0.02, True)
    verifie("et il n'est pas nul", F.ECART_BOUCLAGE > 0, True)

    # 4. Un exercice qui ne boucle pas doit être MARQUÉ, pas corrigé ni tu.
    lignes_ctrl = [
        ({"fin": "2024-03-31", "revenue": 100.0, "exercice": 2024, "t": 1}, "2024-12-31"),
        ({"fin": "2024-06-30", "revenue": 100.0, "exercice": 2024, "t": 2}, "2024-12-31"),
        ({"fin": "2024-09-30", "revenue": 100.0, "exercice": 2024, "t": 3}, "2024-12-31"),
        ({"fin": "2024-12-31", "revenue": 100.0, "exercice": 2024, "t": 4}, "2024-12-31"),
    ]
    sortie = F._controler_bouclage(list(lignes_ctrl), {"2024-12-31": {"revenue": 800.0}})
    marques = [t for t in sortie if t.get("_ecart_exercice")]
    verifie("un exercice qui ne boucle pas est marqué", len(marques) > 0, True)
    valeurs = [t.get("revenue") for t in sortie]
    verifie("et sa valeur n'est PAS corrigée en douce", valeurs, [100.0] * 4)

    # ══════════════════════════════════════════════════════════════════════
    # LE DÉPÔT QUE `companyfacts` A SAUTÉ
    # ══════════════════════════════════════════════════════════════════════
    # Mesuré le 27/09/2026 : quarante-huit sociétés figées alors que leur 10-Q
    # suivant était déposé — seize vérifiées une à une, dont Southern, Duke,
    # Humana (juin absent), Citigroup (mars ET juin absents). EDGAR listait le
    # dépôt, `companyfacts` n'en portait aucun fait, et la série s'arrêtait sans
    # rien dire. Le correctif lit l'instance XBRL du dépôt manquant. Ces contrôles reproduisent la forme réelle de l'instance de
    # Southern : espace de noms par défaut, taxonomie 2026, dépôt COMMUN avec
    # des filiales sous `LegalEntityAxis`, faits répétés à deux précisions.
    # ⚠ L'ORDRE DES FAITS EST ADVERSE, EXPRÈS : la filiale et l'autre CIK
    # précèdent la société, le doublon arrondi encadre le précis. Dans l'ordre
    # naturel, retirer le filtre des segments, celui du CIK ou la règle de
    # précision ne faisait tomber AUCUN contrôle — éprouvé en mutant le code.
    print()
    print("LE DÉPÔT QUE L'API A SAUTÉ — lu dans son instance XBRL")
    instance = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance"
  xmlns:dei="http://xbrl.sec.gov/dei/2026" xmlns:us-gaap="http://fasb.org/us-gaap/2026"
  xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:so="http://example.com/so">
 <context id="h1"><entity><identifier scheme="http://www.sec.gov/CIK">0000092122</identifier></entity>
  <period><startDate>2026-01-01</startDate><endDate>2026-06-30</endDate></period></context>
 <context id="q2"><entity><identifier scheme="http://www.sec.gov/CIK">0000092122</identifier></entity>
  <period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period></context>
 <context id="fin"><entity><identifier scheme="http://www.sec.gov/CIK">0000092122</identifier></entity>
  <period><instant>2026-06-30</instant></period></context>
 <context id="filiale"><entity><identifier scheme="http://www.sec.gov/CIK">0000092122</identifier>
  <segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">so:GeorgiaPowerMember</xbrldi:explicitMember></segment></entity>
  <period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period></context>
 <context id="autre"><entity><identifier scheme="http://www.sec.gov/CIK">0000011111</identifier></entity>
  <period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period></context>
 <unit id="usd"><measure>iso4217:USD</measure></unit>
 <unit id="bpa"><divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>
  <unitDenominator><measure>xbrli:shares</measure></unitDenominator></divide></unit>
 <dei:DocumentFiscalYearFocus contextRef="h1">2026</dei:DocumentFiscalYearFocus>
 <dei:DocumentFiscalPeriodFocus contextRef="h1">Q2</dei:DocumentFiscalPeriodFocus>
 <us-gaap:NetIncomeLoss contextRef="filiale" unitRef="usd" decimals="-6">9000000</us-gaap:NetIncomeLoss>
 <us-gaap:NetIncomeLoss contextRef="autre" unitRef="usd" decimals="-6">7000000</us-gaap:NetIncomeLoss>
 <us-gaap:NetIncomeLoss contextRef="h1" unitRef="usd" decimals="-6">31000000</us-gaap:NetIncomeLoss>
 <us-gaap:NetIncomeLoss contextRef="q2" unitRef="usd" decimals="-6">15000000</us-gaap:NetIncomeLoss>
 <us-gaap:NetIncomeLoss contextRef="q2" unitRef="usd" decimals="-6">15000000</us-gaap:NetIncomeLoss>
 <us-gaap:Assets contextRef="fin" unitRef="usd" decimals="-9">2000000000</us-gaap:Assets>
 <us-gaap:Assets contextRef="fin" unitRef="usd" decimals="-6">2003000000</us-gaap:Assets>
 <us-gaap:Assets contextRef="fin" unitRef="usd" decimals="-9">2000000000</us-gaap:Assets>
 <us-gaap:EarningsPerShareDiluted contextRef="q2" unitRef="bpa" decimals="2">1.03</us-gaap:EarningsPerShareDiluted>
 <us-gaap:Revenues contextRef="q2" unitRef="usd" xsi:nil="true"/>
 <so:ExtensionPropre contextRef="q2" unitRef="usd" decimals="-6">5</so:ExtensionPropre>
</xbrl>"""
    lu = F.faits_instance(instance, "0000092122", "0000092122-26-000054",
                          "10-Q", "2026-07-30")
    ni = sorted((p.get("start"), p["end"], p["val"])
                for p in lu["us-gaap"]["NetIncomeLoss"]["units"]["USD"])
    verifie("seuls les faits NON DIMENSIONNELS de la société", ni,
            [("2026-01-01", "2026-06-30", 31000000),
             ("2026-04-01", "2026-06-30", 15000000)])
    verifie("le plus PRÉCIS de deux doublons l'emporte",
            [p["val"] for p in lu["us-gaap"]["Assets"]["units"]["USD"]],
            [2003000000])
    verifie("l'unité divisée s'écrit comme dans l'API",
            sorted(lu["us-gaap"]["EarningsPerShareDiluted"]["units"]), ["USD/shares"])
    pt = lu["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]
    verifie("chaque point porte le dépôt, la forme et l'exercice",
            (pt["accn"], pt["form"], pt["filed"], pt["fy"], pt["fp"]),
            ("0000092122-26-000054", "10-Q", "2026-07-30", 2026, "Q2"))
    verifie("ni fait nul, ni extension propre, ni taxonomie inconnue",
            (sorted(lu), "Revenues" in lu["us-gaap"]), (["us-gaap"], False))

    # De bout en bout : `companyfacts` porte l'exercice 2025 et le 10-Q de mars ;
    # EDGAR liste en plus le 10-Q de juin, que l'API a sauté.
    api = bloc(("NetIncomeLoss", "USD", [
        duree("2025-01-01", "2025-12-31", 50.0e6, forme="10-K", accn="a-10k",
              depose="2026-02-19"),
        duree("2026-01-01", "2026-03-31", 16.0e6, accn="a-q1", depose="2026-04-30"),
    ]), ("Assets", "USD", [
        # Un exercice sans aucun poste d'ancrage n'en est pas un (voir
        # `ANCRES_EXERCICE`) : le bilan de clôture le fait exister.
        instant("2025-12-31", 1.9e9, forme="10-K", accn="a-10k",
                depose="2026-02-19"),
    ]))
    liste = {"filings": {"recent": {
        "form": ["10-Q", "8-K", "10-Q", "10-K"],
        "accessionNumber": ["0000092122-26-000054", "x-8k", "a-q1", "a-10k"],
        "filingDate": ["2026-07-30", "2026-07-01", "2026-04-30", "2026-02-19"],
        "reportDate": ["2026-06-30", "2026-07-01", "2026-03-31", "2025-12-31"],
        "isXBRL": [1, 0, 1, 1],
        "primaryDocument": ["so-20260630.htm", "x.htm", "q1.htm", "k.htm"]}}}
    demandes = []

    def faux_get(url, accept_404=False, brut=False):
        demandes.append(url.rsplit("/", 1)[-1])
        if "submissions" in url:
            return liste
        if url.endswith("so-20260630_htm.xml"):
            return instance
        return None

    vrai_get = F._get
    F._get = faux_get
    try:
        complement, rapport = F.completer_depots("0000092122", api)
    finally:
        F._get = vrai_get
    verifie("seul le dépôt ABSENT de l'API est téléchargé", demandes,
            ["CIK0000092122.json", "so-20260630_htm.xml"])
    verifie("le rapport nomme le dernier rapport périodique d'EDGAR",
            rapport["dernier_rapport"], "2026-06-30")
    avant = (F.construire(api) or {"resume": {}})["resume"].get("trim") or {}
    apres = (F.construire(F.fusionner_faits(api, complement))
             or {"resume": {}})["resume"].get("trim") or {}
    verifie("SANS le complément, la série s'arrête en mars", avant.get("dernier"),
            "2026-03-31")
    verifie("AVEC lui, le trimestre de juin est rendu", apres.get("dernier"),
            "2026-06-30")

    # Un échec réseau n'empêche rien : la société est bâtie comme avant.
    def get_en_panne(url, accept_404=False, brut=False):
        if "submissions" in url:
            return liste
        raise OSError("réseau coupé")

    F._get = get_en_panne
    try:
        complement, rapport = F.completer_depots("0000092122", api)
    finally:
        F._get = vrai_get
    verifie("une instance illisible ne lève rien et se DIT",
            (complement, len(rapport["echecs"]), rapport["dernier_rapport"]),
            (None, 1, "2026-06-30"))

    print()
    if échecs:
        print("✗ %d contrôle(s) en échec :" % len(échecs))
        for e in échecs:
            print("   · %s" % e)
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
