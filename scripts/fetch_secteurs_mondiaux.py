#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SECTEURS MONDIAUX — l'agregat sectoriel des trois mille premieres societes.

Ce collecteur remplace l'ancien couple « Thematiques / Secteurs & industries »
par un seul axe : la classification, appliquee aux 3 000 plus grosses
capitalisations mondiales.

CE QUI DISTINGUE CE FICHIER DE SON PREDECESSEUR
------------------------------------------------
1. IL DEDOUBLONNE. Les fragments `marche_NN.json` ne contiennent pas des
   societes mais des COTATIONS : Alphabet y figure vingt-cinq fois, NVIDIA
   quinze, Apple cinq. Un classement brut par capitalisation donne un « top
   3 000 » qui ne contient que quelques centaines d'entreprises, chacune
   repetee. On regroupe donc par NOM — jamais par symbole, qui change (Toyota
   est passe de 7203.T a TM en deux jours).

2. LE DEDOUBLONNAGE REPARE LA CLASSIFICATION, ce qui n'etait pas prevu. Le
   champ `sector` n'est rempli qu'a 50 % sur les cotations brutes, mais a
   98 % apres regroupement : les donnees melent deux vocabulaires — le
   moderne (« Semiconductors ») sur les places principales, l'ancien SIC
   (« Semiconductors and Related Devices ») sur Francfort, Vienne ou Bombay —
   et 19 218 societes sur 20 312 ont AU MOINS une cotation correctement
   etiquetee. On choisit cette cotation-la.

3. LE SEUIL EST EN NOMBRE DE SOCIETES, PAS EN CAPITALISATION. Trois mille,
   soit un plancher mesure a 6,7 Md$. Au-dela de cinq mille le PER se degrade
   (87 %) sans rien apporter.

CE QU'ON N'A PAS FAIT, ET POURQUOI
-----------------------------------
On ne deduit PAS le secteur depuis l'industrie pour les societes qui n'en ont
aucune. Essaye, mesure, abandonne : les deux vocabulaires ne partagent que six
libelles sur six cent soixante-trois, si bien que la table apprise ne rattrape
que deux des trente-trois cas du top 2 000. Ces societes vont dans « Non
classe », qui est une reponse honnete, plutot que dans un secteur devine.
"""

import math
import json
import glob
import os
import sys
import collections
import statistics
from datetime import datetime, timezone

RACINE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("SCF_CACHE", RACINE)

# Combien de societes retenues. Le chiffre est un choix editorial, pas une
# limite technique : la couverture reste bonne jusqu'a cinq mille.
TOP_N = int(os.environ.get("SCF_TOP_N", "3000"))

# En dessous, un groupe n'a pas de sens statistique : une mediane sur trois
# societes n'est pas une mediane, c'est un exemple.
MIN_TITRES = 8

# Les industries, elles, sont naturellement etroites : a huit titres le seuil
# ecartait quatre-vingt-dix-neuf industries et 4,2 % de la capitalisation —
# dont « Home Improvement Retail », qui n'a que quatre societes mais pese
# 518 Md$ (Home Depot, Lowe's). Un metier reel a peu d'acteurs ; c'est
# precisement ce qui le rend interessant.
MIN_TITRES_INDUSTRIE = 4

# Ce que la source appelle une societe mais qui n'en est pas une.
INDUSTRIES_EXCLUES = {"Shell Companies", "Blank Check"}

# Vocabulaire Refinitiv, vingt-trois lignes sur des places philippines. Publie
# tel quel, il fabriquait un douzieme secteur a vingt et un titres qui n'existe
# nulle part ailleurs.
SECTEURS_PARASITES = {
    "Consumer Non-Cyclicals", "Academic & Educational Services",
    "Consumer Goods", "Industrial Goods", "Services",
}

# ── LA NOMENCLATURE SIC, TRADUITE VERS GICS ────────────────────────────────
# Voir l'en-tete du patch : certaines places (Emirats, Vietnam, Malaisie...)
# livrent l'industrie en SIC et AUCUN secteur. Sans cette table, ces societes
# tombent dans « Non classe », qui n'est pas un secteur et n'est comparable a
# rien. Les libelles sont explicites ; on transcrit, on ne devine pas.
SIC_VERS_GICS = {
    # Finance
    "Commercial Banks": "Financials",
    "State Commercial Banks": "Financials",
    "National Commercial Banks": "Financials",
    "Savings Institutions": "Financials",
    "Life Insurance": "Financials",
    "Fire, Marine, and Casualty Insurance": "Financials",
    "Insurance Agents, Brokers, and Service": "Financials",
    "Security Brokers, Dealers, and Flotation Companies": "Financials",
    "Personal Credit Institutions": "Financials",
    "Federal and Federally-Sponsored Credit Agencies": "Financials",
    "Short-Term Business Credit Institutions": "Financials",
    "Finance Services": "Financials",
    "Unit Investment Trusts, Face-Amount Certificate Offices, and Closed-End Management Investment Offices": "Financials",
    # Immobilier
    "Real Estate": "Real Estate",
    "Real Estate Investment Trusts": "Real Estate",
    "Real Estate Agents and Managers": "Real Estate",
    "Land Subdividers and Developers, Except Cemeteries": "Real Estate",
    "Operators of Apartment Buildings": "Real Estate",
    "Operative Builders": "Real Estate",
    # Energie
    "Crude Petroleum and Natural Gas": "Energy",
    "Petroleum Refining": "Energy",
    "Drilling Oil and Gas Wells": "Energy",
    "Oil and Gas Field Services": "Energy",
    "Petroleum and Petroleum Products Wholesalers, Except Bulk Stations and Terminals": "Energy",
    "Bituminous Coal and Lignite Surface Mining": "Energy",
    "Natural Gas Transmission": "Energy",
    # Services aux collectivites
    "Electric Services": "Utilities",
    "Electric and Other Services Combined": "Utilities",
    "Water Supply": "Utilities",
    "Gas and Other Services Combined": "Utilities",
    "Cogeneration Services and Small Power Producers": "Utilities",
    # Materiaux
    "Steel Works, Blast Furnaces, And Rolling And Finishing Mills": "Materials",
    "Industrial Organic Chemicals": "Materials",
    "Industrial Inorganic Chemicals": "Materials",
    "Plastics Materials And Synthetic Resins, Synthetic Rubber, Cellulosic And Other Manmade Fibers, Except Glass": "Materials",
    "Primary Production of Aluminum": "Materials",
    "Metal Mining": "Materials",
    "Gold Mining": "Materials",
    "Concrete, Gypsum, And Plaster Products": "Materials",
    "Cement, Hydraulic": "Materials",
    "Paperboard Mills": "Materials",
    "Paper Mills": "Materials",
    "Miscellaneous Plastics Products": "Materials",
    "Fabricated Rubber Products, Not Elsewhere Classified": "Materials",
    "Agricultural Chemicals": "Materials",
    "Nitrogenous Fertilizers": "Materials",
    # Technologie
    "Semiconductors and Related Devices": "Technology",
    "Computer Programming, Data Processing, And Other Computer Related Services": "Technology",
    "Prepackaged Software": "Technology",
    "Services-Computer Programming, Data Processing, Etc.": "Technology",
    "Electronic Computers": "Technology",
    "Computer Peripheral Equipment, Not Elsewhere Classified": "Technology",
    "Printed Circuit Boards": "Technology",
    "Electronic Components and Accessories": "Technology",
    "Instruments for Measuring and Testing of Electricity and Electrical Signals": "Technology",
    # Communication
    "Telephone Communications": "Communication Services",
    "Radiotelephone Communications": "Communication Services",
    "Radio and Television Broadcasting Stations": "Communication Services",
    "Cable and Other Pay Television Services": "Communication Services",
    "Communications Services, Not Elsewhere Classified": "Communication Services",
    # Sante
    "Pharmaceutical Preparations": "Healthcare",
    "Biological Products, Except Diagnostic Substances": "Healthcare",
    "General Medical and Surgical Hospitals": "Healthcare",
    "Medical, Dental, and Hospital Equipment and Supplies": "Healthcare",
    "Services-Health Services": "Healthcare",
    # Industrie
    "Heavy Construction Other Than Building Construction Contractors": "Industrials",
    "Special Industry Machinery, not elsewhere classified": "Industrials",
    "Deep Sea Foreign Transportation of Freight": "Industrials",
    "Water Transportation": "Industrials",
    "Air Transportation, Scheduled": "Industrials",
    "Trucking, Except Local": "Industrials",
    "Construction - Special Trade Contractors": "Industrials",
    "General Building Contractors": "Industrials",
    "Motorcycles, Bicycles, and Parts": "Consumer Discretionary",
    "Motor Vehicles and Passenger Car Bodies": "Consumer Discretionary",
    "Retail Stores, Not Elsewhere Classified": "Consumer Discretionary",
    "Hotels and Motels": "Consumer Discretionary",
    # Consommation de base
    "Agricultural Production Crops": "Consumer Staples",
    "Food and Kindred Products": "Consumer Staples",
    "Beverages": "Consumer Staples",
    "Grocery Stores": "Consumer Staples",
    "Cigarettes": "Consumer Staples",
}


SECTEURS_FR = {
    "Technology": "Technologie",
    "Financials": "Finance",
    "Industrials": "Industrie",
    "Consumer Discretionary": "Consommation discrétionnaire",
    "Consumer Staples": "Consommation de base",
    "Healthcare": "Santé",
    "Communication Services": "Communication",
    "Energy": "Énergie",
    "Materials": "Matériaux",
    "Utilities": "Services aux collectivités",
    "Real Estate": "Immobilier",
    # La CLE reste sans accent : c'est un identifiant interne, compare tel quel
    # ligne 312 et 332. Seul le LIBELLE affiche en porte.
    "Non classe": "Non classé",
}

# Grandeurs ou une valeur negative ou nulle n'a pas de sens : un PER negatif
# n'est pas « bon marche », il dit que la societe perd de l'argent.
POSITIVES = {"peRatio", "peForward", "psRatio", "pbRatio", "evEbitda",
             "evSales", "interestCoverage"}

PLAFONDS = {"peRatio": 200.0, "peForward": 300.0, "pbRatio": 100.0,
            "psRatio": 50.0, "evEbitda": 150.0, "evSales": 60.0,
            "beta": 4.0, "interestCoverage": 500.0}

# ── LES VALEURS QU'AUCUNE SOCIETE NE PEUT PORTER ───────────────────────────
# `PLAFONDS` ci-dessus ne couvre que la VALORISATION. Les rendements, marges et
# taux de distribution n'avaient aucune borne — et ce sont eux que faussent les
# BDR bresiliens (suffixe `.SA`), certificats de societes americaines cotes a
# Sao Paulo dont le dividende est libelle dans une devise et le cours dans une
# autre.
#
# Audit du 08/09/2026 sur les 3 000 societes : 79 valeurs impossibles, dont 13
# sur `.SA`.
#     B1DX34.SA  Becton, Dickinson   rendement du dividende      812,34 %
#     HONB34.SA  Honeywell           taux de distribution      1 914,58 %
#     N2TN34.SA  Nutanix             rendement des capitaux propres 37 460 %
#
# L'EFFET N'EST PAS MARGINAL : la seule ligne Becton Dickinson faisait passer
# le rendement du dividende de la SANTE de 1,48 % a 6,20 %. Quatre fois trop,
# sur une grandeur que le lecteur compare d'un secteur a l'autre.
#
# ⚠ ON NE BORNE PAS AU PLUS SERRE. Ces bornes laissent passer toutes les
# valeurs economiquement possibles, meme extremes : une marge d'exploitation de
# -900 % existe (une biotech sans revenus), un rendement de 20 % aussi (une
# societe en difficulte). On n'ecarte QUE ce qui ne peut pas etre vrai —
# ecarter une valeur extreme mais reelle serait pire que le defaut corrige.
BORNES_PLAUSIBLES = {
    "dividendYield":   (0.0, 25.0),
    "payoutRatio":     (-500.0, 1000.0),
    "buybackYield":    (-100.0, 100.0),
    "fcfYield":        (-200.0, 200.0),
    "roe":             (-500.0, 500.0),
    "roa":             (-200.0, 200.0),
    "roic":            (-500.0, 500.0),
    "roce":            (-500.0, 500.0),
    "profitMargin":    (-1000.0, 100.0),
    "grossMargin":     (-500.0, 100.0),
    "operatingMargin": (-1000.0, 100.0),
    "ebitdaMargin":    (-1000.0, 100.0),
    "currentRatio":    (0.0, 100.0),
    "debtEquity":      (-50.0, 100.0),
    "debtEbitda":      (-100.0, 200.0),
}

GRANDEURS = [
    "peRatio", "peForward", "psRatio", "pbRatio", "evEbitda", "evSales",
    "grossMargin", "operatingMargin", "profitMargin", "ebitdaMargin",
    "roe", "roa", "roic", "roce", "debtEbitda", "debtEquity", "currentRatio",
    "interestCoverage", "dividendYield", "payoutRatio", "buybackYield",
    "fcfYield", "croissance_ca_pct", "croissance_bpa_pct",
    "croissance_ca_3a_pct", "beta",
    "ch1m", "ch3m", "ch6m", "ch1y", "ch3y", "ch5y",
]


def charger_univers():
    """Lit les fragments et rend (societes, index des champs, nb fragments)."""
    fragments = sorted(glob.glob(os.path.join(CACHE, "marche_[0-9][0-9].json")))
    if not fragments:
        raise SystemExit(
            "[fatal] aucun fragment marche_NN.json dans %s. Ce collecteur "
            "derive d'une collecte existante, il ne la remplace pas." % CACHE)
    champs, societes = None, {}
    for f in fragments:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        if champs is None:
            champs = d["champs"]
        elif d["champs"] != champs:
            raise SystemExit(
                "[fatal] %s ne declare pas les memes champs que le premier "
                "fragment : les colonnes seraient decalees en silence." % f)
        societes.update(d["societes"])
    return societes, {c: i for i, c in enumerate(champs)}, len(fragments)


def dedoublonner(societes, ix):
    """Une ligne par societe reelle, en gardant la cotation la mieux etiquetee.

    Le NOM tranche. Le symbole ne le peut pas : il change, et un ticker nu est
    ambigu (le 7203 de Toyota existe a Tokyo ET a Riyad).
    """
    def g(r, c):
        i = ix[c]
        return r[i] if i < len(r) else None

    cotees = [(k, v) for k, v in societes.items()
              if isinstance(g(v, "marketCapUsd"), (int, float))
              and g(v, "marketCapUsd") > 0]

    par_nom = collections.defaultdict(list)
    for k, v in cotees:
        nom = (g(v, "name") or "").strip()
        if nom:
            par_nom[nom].append((k, v))

    # On prefere la cotation qui porte un secteur — c'est elle qui parle le
    # vocabulaire moderne — puis, a egalite, la plus complete.
    def qualite(kv):
        _, v = kv
        return (1 if g(v, "sector") else 0,
                sum(1 for c in ix if g(v, c) not in (None, "")))

    retenues = []
    for nom, groupe in par_nom.items():
        k, v = max(groupe, key=qualite)
        # La capitalisation, elle, se prend en MEDIANE des cotations : une
        # place isolee affiche parfois une valeur aberrante (Toho, x28 entre
        # sa cotation de Tokyo et une ligne etrangere). La mediane l'ignore
        # la ou le maximum la consacrerait.
        caps = sorted(g(x[1], "marketCapUsd") for x in groupe)
        retenues.append((k, v, nom, statistics.median(caps), len(groupe)))

    retenues.sort(key=lambda t: -t[3])
    return retenues, len(cotees), len(par_nom)


def winsoriser(valeurs):
    """Ramene les 5 % extremes aux bornes, pour qu'un point ne fasse pas la moyenne."""
    if len(valeurs) < 20:
        return valeurs
    tri = sorted(valeurs)
    bas = tri[int(0.05 * len(tri))]
    haut = tri[int(0.95 * len(tri)) - 1]
    return [min(max(v, bas), haut) for v in valeurs]


# Ce qui a ete ecarte comme impossible, par grandeur. Publie dans la sortie :
# un filtre muet est un filtre qu'on oublie, et le jour ou il ecarte trop, rien
# ne le dit.
n_aberrantes = collections.Counter()


def agreger(membres, ix, cle_nom):
    """Moyenne ponderee par la capitalisation, winsorisee, avec garde de couverture."""
    def g(r, c):
        i = ix[c]
        return r[i] if i < len(r) else None

    n = len(membres)
    total_capi = sum(m[3] for m in membres)
    sortie = {"nom": cle_nom, "n_titres": n,
              "capitalisation_usd": int(round(total_capi))}

    for grandeur in GRANDEURS:
        paires = []
        for k, v, nom, capi, _ in membres:
            x = g(v, grandeur)
            if not isinstance(x, (int, float)) or x != x:
                continue
            if grandeur in POSITIVES and x <= 0:
                continue
            pl = PLAFONDS.get(grandeur)
            if pl is not None and abs(x) > pl:
                continue
            # Les bornes de plausibilite : voir BORNES_PLAUSIBLES ci-dessus.
            bp = BORNES_PLAUSIBLES.get(grandeur)
            if bp is not None and not (bp[0] <= x <= bp[1]):
                n_aberrantes[grandeur] += 1
                continue
            paires.append((x, capi))

        # Une cellule calculee sur moins de 40 % du groupe ment sur le groupe :
        # on prefere un tiret. Le seuil est celui de l'ancien agregat.
        if not paires or len(paires) < max(3, int(n * 0.40)):
            sortie[grandeur] = None
            sortie[grandeur + "_n"] = len(paires)
            continue

        vals = winsoriser([p[0] for p in paires])
        poids = [p[1] for p in paires]
        den = sum(poids)
        sortie[grandeur] = (round(sum(v * w for v, w in zip(vals, poids)) / den, 4)
                            if den else None)
        sortie[grandeur + "_n"] = len(paires)
        sortie[grandeur + "_median"] = round(statistics.median([p[0] for p in paires]), 4)

    # Largeur : la part du groupe au-dessus de sa moyenne mobile. C'est une
    # mesure de participation — un indice peut monter avec trois titres.
    for ma, champ in (("ma50", "largeur_ma50"), ("ma200", "largeur_ma200")):
        au_dessus, comptes = 0, 0
        for k, v, nom, capi, _ in membres:
            prix, moyenne = g(v, "price"), g(v, ma)
            if (isinstance(prix, (int, float)) and isinstance(moyenne, (int, float))
                    and moyenne > 0):
                comptes += 1
                if prix > moyenne:
                    au_dessus += 1
        sortie[champ] = round(100.0 * au_dessus / comptes, 1) if comptes else None
        sortie[champ + "_n"] = comptes

    tete = sorted(membres, key=lambda m: -m[3])[:5]
    sortie["principales"] = [{"s": k, "n": nom, "capi": int(round(capi))}
                             for k, v, nom, capi, _ in tete]
    sortie["concentration_tete"] = (
        round(100.0 * sum(m[3] for m in tete) / total_capi, 1) if total_capi else None)
    return sortie


def poser_scores(groupes):
    """Score ORDINAL : cent fois le rang. Il n'a aucun sens absolu."""
    if len(groupes) < 2:
        for x in groupes.values():
            x["score"] = None
            x["rang"] = 1
            x["regime"] = "—"
        return

    med = statistics.median([x["ch3m"] for x in groupes.values()
                             if isinstance(x.get("ch3m"), (int, float))] or [0])
    for x in groupes.values():
        c3 = x.get("ch3m")
        x["momentum_relatif"] = round(c3 - med, 2) if isinstance(c3, (int, float)) else None

    def rangs(champ):
        avec = [(n, x[champ]) for n, x in groupes.items()
                if isinstance(x.get(champ), (int, float))]
        avec.sort(key=lambda t: t[1])
        out = {}
        for i, (n, _) in enumerate(avec):
            out[n] = round(100.0 * i / (len(avec) - 1), 2) if len(avec) > 1 else 50.0
        return out

    r_mom, r_larg, r_prix = rangs("momentum_relatif"), rangs("largeur_ma50"), rangs("ch1m")
    for n, x in groupes.items():
        x["score_momentum"] = r_mom.get(n)
        x["score_largeur"] = r_larg.get(n)
        x["score_prix"] = r_prix.get(n)
        parts = [(0.55, r_mom.get(n)), (0.225, r_larg.get(n)), (0.225, r_prix.get(n))]
        dispo = [(p, v) for p, v in parts if v is not None]
        x["score"] = (round(sum(p * v for p, v in dispo) / sum(p for p, _ in dispo), 2)
                      if dispo else None)
        s = x["score"]
        x["regime"] = ("—" if s is None else
                       "porteur" if s >= 66 else "fragile" if s <= 33 else "neutre")

    ordonnes = sorted([n for n in groupes if groupes[n]["score"] is not None],
                      key=lambda n: -groupes[n]["score"])
    for i, n in enumerate(ordonnes):
        groupes[n]["rang"] = i + 1



# ══════════════════════════════════════════════════════════════════════════
# LA NOTE FONDAMENTALE
# ══════════════════════════════════════════════════════════════════════════
# Le `score` ci-dessus est un CLASSEMENT de momentum : il dit qui monte, et son
# propre avertissement le reconnait — « la normalisation est ORDINALE, elle n'a
# aucun sens absolu ». Affiche en gros sur une carte, il se lisait pourtant
# comme une note de qualite : « Materiaux 88 » voulait dire « premier sur douze
# en momentum », pas « bon secteur ».
#
# Cette note-ci repond a l'autre question, sur le modele de la note des actions
# (`note_q` dans sec_fundamentals_index.js) : vingt criteres, six categories, un
# point au seuil favorable, un demi a l'intermediaire, et la note RAMENEE aux
# seuls criteres mesurables.
#
# Trois choix qui la separent du momentum :
#   1. les seuils sont ABSOLUS. Un ROIC de quinze pour cent vaut un point parce
#      que quinze pour cent est bon, pas parce qu'il est le meilleur des douze.
#      Si tous les secteurs se degradent ensemble, la note baisse pour tous.
#   2. on note la MEDIANE, jamais la moyenne ponderee : celle-ci est tiree par
#      les geants (Technologie, 43,6 % de ROE pondere pour 15,6 % en mediane).
#   3. un critere sans donnee ne penalise pas.

BAREME_NOTE = {
    "Valorisation": [
        ("peRatio_median", "Prix / bénéfices", "bas", 18, 28,
         "La médiane mondiale tourne autour de vingt fois. Au-delà de "
         "vingt-huit, le secteur escompte une croissance qu'il lui reste à produire."),
        ("peForward_median", "Prix / bénéfices attendus", "bas", 16, 24,
         "Le même, sur les bénéfices anticipés. Nettement sous le P/E courant, "
         "il dit que le marché attend une amélioration."),
        ("evEbitda_median", "VE / EBITDA", "bas", 11, 16,
         "Valeur d'entreprise rapportée au résultat d'exploitation avant "
         "amortissements : le multiple qui ignore la structure de dette."),
        ("psRatio_median", "Prix / ventes", "bas", 2.0, 4.0,
         "Utile quand les bénéfices sont volatils ou négatifs : le chiffre "
         "d'affaires, lui, ne se maquille guère."),
    ],
    "Rentabilité": [
        ("roic_median", "Rendement du capital investi", "haut", 15, 8,
         "Le critère central de la qualité : ce que le secteur tire de chaque "
         "euro immobilisé. Au-dessus de quinze pour cent, il crée de la valeur "
         "au-delà de son coût du capital."),
        ("roe_median", "Rendement des capitaux propres", "haut", 15, 8,
         "À croiser avec le ROIC : un ROE très supérieur signale du levier, ou "
         "des rachats d'actions qui écrasent les capitaux propres."),
        ("operatingMargin_median", "Marge d'exploitation", "haut", 15, 8,
         "Ce qui reste du chiffre d'affaires après les coûts d'exploitation."),
        ("profitMargin_median", "Marge nette", "haut", 10, 5,
         "Ce qui reste vraiment, impôts et frais financiers déduits."),
    ],
    "Solidité": [
        ("debtEbitda_median", "Dette / EBITDA", "bas", 2.0, 3.5,
         "Combien d'années de résultat d'exploitation il faudrait pour "
         "rembourser la dette. Au-delà de trois fois et demie, la marge de "
         "manoeuvre se referme."),
        ("interestCoverage_median", "Couverture des intérêts", "haut", 8, 3,
         "Combien de fois le résultat couvre les intérêts. Sous trois, une "
         "remontée des taux fait mal."),
        ("currentRatio_median", "Liquidité générale", "haut", 1.5, 1.0,
         "Actif circulant sur passif circulant : la capacité à honorer un an "
         "d'engagements."),
    ],
    "Croissance": [
        ("croissance_ca_3a_pct_median", "Croissance des ventes sur 3 ans", "haut", 10, 4,
         "Sur trois ans, pas un seul : une bonne année ne fait pas une trajectoire."),
        ("croissance_ca_pct_median", "Croissance des ventes sur 1 an", "haut", 8, 3,
         "L'année écoulée, pour voir si la trajectoire tient encore."),
        ("croissance_bpa_pct_median", "Croissance du bénéfice par action", "haut", 10, 3,
         "Par ACTION : une croissance du bénéfice annulée par des émissions "
         "nouvelles ne profite pas au détenteur."),
    ],
    "Retour à l'actionnaire": [
        ("fcfYield_median", "Rendement des flux libres", "haut", 5, 2,
         "Flux de trésorerie disponible rapporté au cours. C'est le rendement "
         "réel, avant toute décision de distribution."),
        ("dividendYield_median", "Rendement du dividende", "haut", 2.5, 1.0,
         "Ce qui est effectivement versé."),
        ("payoutRatio_median", "Taux de distribution", "bas", 60, 80,
         "Part du bénéfice distribuée. Au-delà de quatre-vingts pour cent, le "
         "dividende se finance au détriment de l'investissement."),
        ("buybackYield_median", "Rendement des rachats d'actions", "haut", 1.0, 0.0,
         "Positif : le secteur réduit son nombre d'actions. Négatif : il dilue "
         "ses actionnaires."),
    ],
    "Risque": [
        ("beta_median", "Bêta", "bas", 1.0, 1.3,
         "Amplitude des variations face au marché. Sous un, le secteur amortit "
         "les chocs."),
        ("pbRatio_median", "Prix / actif net", "bas", 3.0, 6.0,
         "Le prix payé pour un euro de capitaux propres comptables. Très élevé "
         "sur les secteurs riches en incorporels — à lire avec le ROIC."),
    ],
}

NOTE_TOTAL = sum(len(v) for v in BAREME_NOTE.values())


def _noter_critere(valeur, favorable, intermediaire, sens):
    """Un point au seuil favorable, un demi a l'intermediaire, zero sinon."""
    if valeur is None or not isinstance(valeur, (int, float)):
        return None
    if isinstance(valeur, float) and not math.isfinite(valeur):
        return None
    if sens == "haut":
        return 1.0 if valeur >= favorable else (0.5 if valeur >= intermediaire else 0.0)
    return 1.0 if valeur <= favorable else (0.5 if valeur <= intermediaire else 0.0)


def _lecture(note):
    if note >= 15.0:
        return "excellente"
    if note >= 11.0:
        return "de qualité"
    if note >= 7.0:
        return "moyenne"
    return "médiocre"


def poser_note_fondamentale(groupe):
    """Ajoute `note_fondamentale` a un secteur (ou une industrie)."""
    criteres, par_cat = [], {}
    obtenu_total = notables_total = 0.0

    for cat, liste in BAREME_NOTE.items():
        obt, notables = 0.0, 0
        for champ, lib, sens, fav, inter, explication in liste:
            v = groupe.get(champ)
            pt = _noter_critere(v, fav, inter, sens)
            n = groupe.get(champ.replace("_median", "_n"))
            criteres.append({
                "cle": champ, "libelle": lib, "categorie": cat,
                "valeur": round(v, 2) if isinstance(v, (int, float)) else None,
                "point": pt, "statut": "note" if pt is not None else "muet",
                "sens": sens, "seuil_favorable": fav, "seuil_intermediaire": inter,
                "titres": n if isinstance(n, (int, float)) else None,
                "note": explication,
            })
            if pt is not None:
                obt += pt
                notables += 1
        par_cat[cat] = {"obtenu": round(obt, 2), "possible": len(liste),
                        "notables": notables}
        obtenu_total += obt
        notables_total += notables

    # Refusee sous douze criteres sur vingt : ramener huit points mesures
    # pretendrait a une precision qu'on n'a pas.
    assez = notables_total >= 12
    ramenee = (round(20.0 * obtenu_total / notables_total, 1)
               if (notables_total and assez) else None)

    groupe["note_fondamentale"] = {
        "note": ramenee, "sur": 20,
        "brute": round(obtenu_total, 2),
        "criteres_notables": int(notables_total),
        "criteres_total": NOTE_TOTAL,
        "lecture": _lecture(ramenee) if ramenee is not None else None,
        "par_categorie": par_cat,
        "criteres": criteres,
    }

def main():
    societes, ix, n_frag = charger_univers()
    retenues, n_cotations, n_noms = dedoublonner(societes, ix)

    def g(r, c):
        i = ix[c]
        return r[i] if i < len(r) else None

    propres = [t for t in retenues
               if (g(t[1], "industry") or "") not in INDUSTRIES_EXCLUES
               and (g(t[1], "sector") or "") not in SECTEURS_PARASITES]
    top = propres[:TOP_N]

    if len(top) < TOP_N * 0.9:
        raise SystemExit(
            "[fatal] %d societes retenues pour un objectif de %d. L'univers "
            "amont est ampute : on ne reecrit pas l'agregat." % (len(top), TOP_N))

    par_secteur = collections.defaultdict(list)
    par_industrie = collections.defaultdict(list)
    # De quel secteur releve chaque industrie. Le lien EXISTE ici — chaque
    # societe porte ses deux etiquettes — mais il n'etait pas ecrit dans le
    # cache. La fiche d'un secteur ne pouvait donc PAS lister ses industries,
    # et le commentaire du front l'admettait : « le cache ne porte pas le lien
    # industrie -> secteur, on ne peut donc PAS les lister ici sans risquer de
    # se tromper ». On le COMPTE plutot que de le deviner : une industrie peut
    # se retrouver a cheval sur deux secteurs selon la source, on retient le
    # majoritaire et on publie sa part pour que le doute reste visible.
    secteur_des_industries = collections.defaultdict(collections.Counter)

    # Le secteur GICS deja connu pour chaque NOM de societe : une meme societe
    # cotee sur plusieurs places n'a besoin que d'UNE cotation bien etiquetee.
    gics_du_nom = {}
    for t in top:
        s = g(t[1], "sector")
        if s:
            nom_s = g(t[1], "name")
            if nom_s:
                gics_du_nom.setdefault(nom_s, s)

    n_sans_secteur = 0
    n_resolus_nom = 0
    n_resolus_sic = 0
    for t in top:
        sec = g(t[1], "sector")
        ind = g(t[1], "industry")
        if not sec:
            # 1. Un jumeau cote ailleurs porte-t-il deja le secteur ?
            sec = gics_du_nom.get(g(t[1], "name"))
            if sec:
                n_resolus_nom += 1
            else:
                # 2. L'industrie est en SIC : on la traduit.
                sec = SIC_VERS_GICS.get((ind or "").strip())
                if sec:
                    n_resolus_sic += 1
        if not sec:
            n_sans_secteur += 1
            sec = "Non classe"
        par_secteur[sec].append(t)
        if ind:
            par_industrie[ind].append(t)
            secteur_des_industries[ind][sec] += 1

    if n_resolus_nom or n_resolus_sic:
        print("[nomenclature] %d societes ramenees a GICS "
              "(%d par jumeau cote ailleurs, %d par traduction SIC) ; "
              "%d restent non classees"
              % (n_resolus_nom + n_resolus_sic, n_resolus_nom, n_resolus_sic,
                 n_sans_secteur))

    secteurs = {n: agreger(m, ix, n) for n, m in par_secteur.items()
                if len(m) >= MIN_TITRES}
    industries = {n: agreger(m, ix, n) for n, m in par_industrie.items()
                  if len(m) >= MIN_TITRES_INDUSTRIE}

    # « Non classe » n'est pas un secteur : c'est l'aveu que la source n'a pas
    # etiquete ces societes. Il est classe et score comme les autres — sans
    # quoi les rangs mentiraient — mais il porte une marque qui permet au front
    # de le poser a part, en fin de liste, plutot qu'au milieu des vrais.
    poser_scores(secteurs)
    poser_scores(industries)

    for n, s in secteurs.items():
        s["nom_fr"] = SECTEURS_FR.get(n, n)
        s["hors_classification"] = (n == "Non classe")

    # Le rattachement de chaque industrie a son secteur, ecrit noir sur blanc.
    # `secteur_part` dit sur quelle proportion de ses societes ce rattachement
    # repose : a 100 % l'industrie est entierement dans ce secteur, en dessous
    # elle est a cheval et la fiche peut le signaler.
    for n, o in industries.items():
        compte = secteur_des_industries.get(n)
        if not compte:
            continue
        gagnant, combien = compte.most_common(1)[0]
        total = sum(compte.values())
        o["secteur"] = gagnant
        o["secteur_part"] = round(100.0 * combien / total, 1) if total else None

    plancher = top[-1][3]
    sortie = {
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "collecte de marche (stockanalysis), agregee hors ligne",
        "univers": len(top),
        "univers_societes_totales": n_noms,
        "univers_cotations": n_cotations,
        "plancher_capitalisation_usd": int(round(plancher)),
        "part_capitalisation_mondiale_pct": round(
            100.0 * sum(t[3] for t in top) / sum(t[3] for t in retenues), 1),
        "sans_secteur": n_sans_secteur,
        "minimum_titres": MIN_TITRES,
        "minimum_titres_industrie": MIN_TITRES_INDUSTRIE,
        "dedoublonnage": (
            "Les fragments contiennent des COTATIONS, pas des societes : %d "
            "lignes pour %d societes reelles. On regroupe par nom — le symbole "
            "change — et l'on garde la cotation qui porte un secteur, car le "
            "champ n'est rempli qu'a 50 %% sur les lignes brutes contre 98 %% "
            "apres regroupement. La capitalisation est la MEDIANE des "
            "cotations, non le maximum : une place isolee affiche parfois une "
            "valeur aberrante." % (n_cotations, n_noms)),
        "winsorisation": "5-95 centiles avant moyenne ponderee",
        # Ce que les bornes de plausibilite ont ecarte. Un filtre muet est un
        # filtre qu'on oublie : le jour ou il coupe trop, rien ne le dirait.
        "valeurs_impossibles": (sum(n_aberrantes.values()) or 0),
        "valeurs_impossibles_detail": dict(n_aberrantes),
        "bornes_plausibilite": ("les valeurs qu'aucune societe ne peut porter "
                                "sont ecartees — un BDR bresilien affichait "
                                "812 % de rendement du dividende, ce qui "
                                "quadruplait la moyenne de son secteur"),
        "couverture_minimale_cellule": "40 % du groupe, sinon la cellule est vide",
        "formule_score": ("0,55 x rang(momentum relatif a 3 mois) + 0,225 x "
                          "rang(largeur au-dessus de la MA50) + 0,225 x rang(momentum de prix)"),
        "avertissement_score": (
            "Chaque COMPOSANTE est normalisée de façon ordinale — cent fois le rang divisé par le nombre de groupes moins un — mais leur MÉLANGE ne l'est plus : mesuré le 05/09/2026, onze des douze secteurs s'écartent de plus d'un point de la formule du rang. Le score n'a aucun sens absolu et il change si l'on ajoute ou retire un groupe ; le rang est publié à part, dans le champ `rang`."
            "divise par le nombre de groupes moins un. Il n'a aucun sens "
            "absolu et il change si l'on ajoute ou retire un groupe."),
        "secteurs": secteurs,
        "industries": industries,
        "note_fondamentale_bareme": {
            "total": NOTE_TOTAL,
            "categories": {c: len(v) for c, v in BAREME_NOTE.items()},
            "methode": (
                "Vingt criteres, six categories, sur le principe de la note des "
                "actions : un point au seuil favorable, un demi au seuil "
                "intermediaire, et la note RAMENEE aux seuls criteres mesurables. "
                "Refusee sous douze criteres sur vingt."),
            "difference_avec_le_score": (
                "Le `score` est un CLASSEMENT de momentum : il dit qui monte, et "
                "le premier vaut toujours cent. La note fondamentale mesure sur "
                "des seuils ABSOLUS : si les douze secteurs se degradent "
                "ensemble, elle baisse pour tous."),
            "mediane_et_non_moyenne": (
                "Chaque critere lit la MEDIANE du groupe, jamais la moyenne "
                "ponderee, tiree par les geants."),
        },
    }

    # La note fondamentale, sur les secteurs ET les industries.
    for _g in list(secteurs.values()) + list(industries.values()):
        poser_note_fondamentale(_g)

    js = os.path.join(CACHE, "secteurs_mondiaux.js")
    js_tmp = js + ".tmp"
    with open(js_tmp, "w", encoding="utf-8") as fh:
        fh.write("window.__SECTEURS_MONDIAUX__=")
        json.dump(sortie, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    os.replace(js_tmp, js)

    print("[ok] %d societes (%d cotations, %d societes au total)"
          % (len(top), n_cotations, n_noms))
    print("     plancher %.1f Md$ - %.1f %% de la capitalisation mondiale"
          % (plancher / 1e9, sortie["part_capitalisation_mondiale_pct"]))
    print("     %d secteurs - %d industries - %d sans secteur"
          % (len(secteurs), len(industries), n_sans_secteur))
    print("     ecrit %s (%.0f Ko)" % (js, os.path.getsize(js) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
