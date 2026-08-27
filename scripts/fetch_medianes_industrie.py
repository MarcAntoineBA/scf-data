#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Médianes par industrie, pour que chaque ligne de la fiche ait sa comparaison.

LE DÉFAUT QU'ON CORRIGE
Sur la fiche société, les quatre panneaux affichent une trentaine de grandeurs
mais la barre de position n'apparaît que sur onze d'entre elles. Marge brute,
marge nette, marge d'EBITDA, rendement de l'actif, ROIC, ROCE, dette nette sur
EBITDA, liquidité générale, liquidité réduite, couverture des intérêts, taux de
distribution, taux d'imposition : aucune barre. Visuellement, la moitié de
chaque panneau a l'air inachevé.

La raison n'était pas un oubli d'affichage : le cache sectoriel ne publie de
médiane QUE pour onze ratios, sur trente-neuf « narratifs » couvrant sept cent
quatre-vingt-trois actions. Tout le reste n'avait littéralement rien à quoi se
comparer — et les trente-sept mille sociétés de l'univers étendu, elles,
n'avaient AUCUNE comparaison, pour aucun ratio.

CE QUI A CHANGÉ
La collecte de marché sert désormais quatre-vingt-dix-sept champs pour 37 986
sociétés, dont `industry`, rempli à **99,9 %** (mesuré : US 100,0 / France 100,0
/ Japon 100,0 / Inde 99,8). On tient donc de quoi calculer la médiane de
n'importe quelle grandeur, par industrie, sur l'univers entier — sans une seule
requête réseau, en relisant un cache déjà écrit.

TROIS PRÉCAUTIONS, CHACUNE PAYÉE PAR UN DÉFAUT CONNU DE CE DÉPÔT

1. **Un effectif minimum.** Une médiane sur trois sociétés n'est pas une
   médiane, c'est un tirage. En dessous de HUIT valeurs renseignées, la grandeur
   n'est pas publiée pour cette industrie — mieux vaut pas de barre qu'une barre
   qui ment. C'est la même règle que celle qui vient de rendre muettes les
   fenêtres « dix ans » calculées sur cinq exercices.

2. **Des bornes robustes, pas des extrêmes.** Les bornes de la barre sont les
   cinquième et quatre-vingt-quinzième centiles, pas le minimum et le maximum :
   un seul P/E à quarante mille écraserait toute l'échelle et collerait les
   quatre cents autres sociétés sur le même pixel.

3. **Aucun montant.** On n'agrège QUE des grandeurs sans dimension — ratios,
   pourcentages, multiples. Un chiffre d'affaires médian par industrie mêlerait
   des yens, des roupies et des dollars. La règle est déjà écrite ailleurs dans
   ce dépôt, et elle a déjà été payée : le cache des cours est en dollars quand
   les états sont en devise locale, ce qui avait affiché Toyota à un P/E de 0,1.

SORTIE
    medianes_industrie.json   { industries: { <nom>: { n, <grandeur>: [p05, q1,
                                med, q3, p95, effectif] } }, global: {...} }
    medianes_industrie.js     window.__MEDIANES_INDUSTRIE__ = {...}
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

CACHE = Path.home() / "Library" / "Caches" / "site_crypto_finance"
SORTIE_JSON = CACHE / "medianes_industrie.json"
SORTIE_JS = CACHE / "medianes_industrie.js"

# Effectif minimum pour qu'une médiane soit publiée. Huit : en dessous, la
# médiane suit le tirage plus que l'industrie.
MINIMUM = 8

# La fiche d'une société n'a besoin que de SON industrie. On découpe donc, et
# l'empreinte est celle que ce dépôt utilise déjà (h = h*31 + code du
# caractère), pour que Python et JavaScript tombent sur le même paquet. Un
# découpage par première lettre suivrait la LANGUE et non la donnée : la règle a
# déjà été payée ailleurs par un paquet de 3,9 Mo à côté de paquets vides.
PAQUETS = 32


def _paquet(nom):
    h = 0
    for c in str(nom):
        h = (h * 31 + ord(c)) % 4294967296
    return h % PAQUETS

# ── LES GRANDEURS AGRÉGÉES ────────────────────────────────────────────
# Uniquement ce qui est SANS DIMENSION. Pas un seul montant : un chiffre
# d'affaires médian par industrie mêlerait des yens, des roupies et des dollars.
GRANDEURS = [
    # valorisation
    "peRatio", "peForward", "psRatio", "pbRatio", "pFcfRatio", "pOcfRatio",
    "pegRatio", "evEbitda", "evEbit", "evSales", "evFcf",
    # marges
    "grossMargin", "operatingMargin", "profitMargin", "ebitdaMargin",
    "ebitMargin", "fcfMargin",
    # rendements du capital
    "roe", "roa", "roic", "roce",
    # santé et structure
    "currentRatio", "quickRatio", "debtEquity", "debtEbitda",
    "interestCoverage", "assetTurnover", "inventoryTurnover", "taxRate",
    # rendus à l'actionnaire
    "dividendYield", "buybackYield", "earningsYield", "fcfYield", "payoutRatio",
    # croissance (déjà plafonnée à 1000 % par la collecte de marché)
    "croissance_ca_pct", "croissance_ca_trim_pct", "croissance_bpa_pct",
    "croissance_bpa_trim_pct", "croissance_dividende_pct",
    "croissance_ca_exercice_pct", "croissance_ca_suivant_pct",
    "croissance_ca_3a_pct", "croissance_tresorerie_nette_pct",
    # marché
    "beta", "rsi", "ch1m", "ch3m", "ch6m", "chYTD", "ch1y", "ch3y", "ch5y",
    "ecart_objectif_pct",
]


def _centile(tri, q):
    """Le centile q d'une liste DÉJÀ triée, par interpolation linéaire."""
    if not tri:
        return None
    if len(tri) == 1:
        return tri[0]
    k = (len(tri) - 1) * q
    bas = int(k)
    haut = min(bas + 1, len(tri) - 1)
    if bas == haut:
        return tri[bas]
    return tri[bas] + (tri[haut] - tri[bas]) * (k - bas)


# Vingt et un quantiles suffisent à situer une valeur par interpolation, sans
# transporter les 37 986 valeurs. En dessous de vingt valeurs, on ne publie rien :
# le percentile sauterait de cinq points d'un cran à l'autre et prétendrait à une
# précision qu'il n'a pas.
MINIMUM_QUANTILES = 20
PAS = 21


def _quantiles(vals):
    t = sorted(v for v in vals
               if isinstance(v, (int, float)) and v == v and abs(v) != float("inf"))
    if len(t) < MINIMUM_QUANTILES:
        return None
    return [round(_centile(t, i / (PAS - 1.0)), 4) for i in range(PAS)]


def _stats(vals):
    """[p05, q1, médiane, q3, p95, effectif] — ou None si l'effectif ne suffit pas.

    Les bornes sont des CENTILES, pas le minimum et le maximum : un seul P/E à
    quarante mille écraserait l'échelle et collerait toutes les autres sociétés
    sur le même pixel.
    """
    t = sorted(v for v in vals
               if isinstance(v, (int, float)) and v == v and abs(v) != float("inf"))
    if len(t) < MINIMUM:
        return None
    return [round(_centile(t, 0.05), 4), round(_centile(t, 0.25), 4),
            round(_centile(t, 0.50), 4), round(_centile(t, 0.75), 4),
            round(_centile(t, 0.95), 4), len(t)]


def main():
    t0 = time.time()
    frags = sorted(glob.glob(str(CACHE / "marche_[0-9]*.json")))
    if not frags:
        raise SystemExit("[fatal] aucun fragment de marché dans %s — la collecte "
                         "de marché a-t-elle tourné ?" % CACHE)

    par_industrie = {}
    par_secteur = {}
    tout = {g: [] for g in GRANDEURS}
    n_soc = 0
    sans_industrie = 0

    for f in frags:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            print("[!] %s illisible : %s" % (os.path.basename(f), e))
            continue
        ch = d.get("champs") or []
        idx = {c: i for i, c in enumerate(ch)}
        i_ind = idx.get("industry")
        i_sec = idx.get("sector")
        pres = [(g, idx[g]) for g in GRANDEURS if g in idx]
        if i_ind is None or not pres:
            continue

        for sym, arr in (d.get("societes") or {}).items():
            n_soc += 1
            ind = arr[i_ind] if i_ind < len(arr) else None
            sec = arr[i_sec] if (i_sec is not None and i_sec < len(arr)) else None
            if not ind:
                sans_industrie += 1
            for g, i in pres:
                v = arr[i] if i < len(arr) else None
                if not isinstance(v, (int, float)):
                    continue
                tout[g].append(v)
                if ind:
                    par_industrie.setdefault(ind, {}).setdefault(g, []).append(v)
                if sec:
                    par_secteur.setdefault(sec, {}).setdefault(g, []).append(v)

    print("[ok] %d sociétés lues dans %d fragments (%d sans industrie, %.1f %%)"
          % (n_soc, len(frags), sans_industrie, 100.0 * sans_industrie / max(1, n_soc)))
    print("[ok] %d industries, %d secteurs" % (len(par_industrie), len(par_secteur)))

    def replier(brut, avec_quantiles=False):
        out, publiees, muettes = {}, 0, 0
        for nom, gs in brut.items():
            bloc = {}
            qs = {}
            for g, vals in gs.items():
                st = _stats(vals)
                if st:
                    bloc[g] = st
                    publiees += 1
                    if avec_quantiles:
                        q = _quantiles(vals)
                        if q:
                            qs[g] = q
                else:
                    muettes += 1
            if qs:
                bloc["_q"] = qs
            if bloc:
                # L'effectif de l'industrie : le plus grand effectif observé,
                # celui du champ le mieux rempli.
                bloc["n"] = max(v[5] for k, v in bloc.items()
                                if k not in ("n", "_q"))
                out[nom] = bloc
        return out, publiees, muettes

    industries, pub_i, mut_i = replier(par_industrie, avec_quantiles=True)
    secteurs, pub_s, mut_s = replier(par_secteur, avec_quantiles=True)
    glob_stats = {g: s for g in GRANDEURS
                  for s in [_stats(tout[g])] if s}
    # Les quantiles mondiaux servent de repli quand une industrie est trop
    # petite pour avoir les siens — et pour le profil « percentile de l'univers »,
    # qui se calculait jusqu'ici sur sept cent quatre-vingt-trois actions.
    glob_q = {g: q for g in GRANDEURS for q in [_quantiles(tout[g])] if q}
    print("[ok] quantiles  : %d grandeurs au niveau mondial, %d industries en "
          "portent" % (len(glob_q),
                       sum(1 for b in industries.values() if b.get("_q"))))

    print("[ok] industries : %d médianes publiées, %d écartées faute d'effectif "
          "(minimum %d)" % (pub_i, mut_i, MINIMUM))
    print("[ok] secteurs   : %d médianes publiées, %d écartées" % (pub_s, mut_s))
    print("[ok] global     : %d grandeurs" % len(glob_stats))

    # ── L'index, léger, chargé avec la page ──
    # Il porte ce qui sert TOUT DE SUITE : quelle industrie existe, avec quel
    # effectif et dans quel paquet la trouver ; les médianes par grand secteur,
    # peu nombreuses ; et les médianes mondiales, qui servent de repli quand une
    # industrie est trop petite pour avoir les siennes.
    index = {
        "genere_le": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "collecte de marché (stockanalysis), agrégée localement",
        "minimum_effectif": MINIMUM,
        "bornes": "p05, q1, mediane, q3, p95, effectif",
        "regle": ("uniquement des grandeurs SANS DIMENSION : agréger un montant "
                  "mêlerait des yens, des roupies et des dollars"),
        "paquets": PAQUETS,
        "ou": {nom: _paquet(nom) for nom in industries},
        "effectifs": {nom: b["n"] for nom, b in industries.items()},
        "secteurs": secteurs,
        "global": glob_stats,
        "global_q": glob_q,
        "pas_quantiles": PAS,
        "minimum_quantiles": MINIMUM_QUANTILES,
    }
    SORTIE_JSON.write_text(json.dumps(index, ensure_ascii=False,
                                      separators=(",", ":")), encoding="utf-8")
    SORTIE_JS.write_text("window.__MEDIANES_INDUSTRIE__=" +
                         json.dumps(index, ensure_ascii=False,
                                    separators=(",", ":")) + ";\n",
                         encoding="utf-8")

    # ── Les paquets, tirés à la demande ──
    paniers = {}
    for nom, bloc in industries.items():
        paniers.setdefault(_paquet(nom), {})[nom] = bloc
    for i in range(PAQUETS):
        f = CACHE / ("medianes_ind_%02d.json" % i)
        f.write_text(json.dumps({"industries": paniers.get(i, {})},
                                ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
    tailles = sorted((CACHE / ("medianes_ind_%02d.json" % i)).stat().st_size
                     for i in range(PAQUETS))
    ko = SORTIE_JSON.stat().st_size / 1024.0
    print("[ok] index %.0f Ko · %d paquets, plus gros %.0f Ko, total %.0f Ko — %.1f s"
          % (ko, PAQUETS, tailles[-1] / 1024.0,
             (sum(tailles) / 1024.0) + ko, time.time() - t0))

    # ── Ce qu'on vient de débloquer, dit en clair ──
    couvert = sum(1 for b in industries.values() for k in b if k != "n")
    print("[ok] %d couples (industrie x grandeur) désormais comparables"
          % couvert)
    grosses = sorted(industries.items(), key=lambda x: -x[1]["n"])[:8]
    print("\n   %-42s %6s %s" % ("industrie", "n", "grandeurs"))
    for nom, b in grosses:
        print("   %-42s %6d %d" % (nom[:42], b["n"], len(b) - 1))


if __name__ == "__main__":
    sys.exit(main())
