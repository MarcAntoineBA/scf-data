#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L'axe MÉTIER, dérivé des vingt mille cotations principales.

POURQUOI CE COLLECTEUR EXISTE

Le TradFi Tracker suit trente-neuf « narratifs » bâtis sur huit cent douze
actions écrites à la main dans deux dictionnaires Python. Ces trente-neuf
mélangent DEUX natures que rien ne rapproche :

  · des SECTEURS — Banques, Pharma mondial, Télécoms, Luxe, Utilities, REITs.
    Ils décrivent un MÉTIER, et la nomenclature les connaît déjà ;
  · des THÈMES — Space Economy, Quantum Computing, Cybersécurité, Nucléaire &
    SMR. Ils décrivent une THÈSE, et aucune nomenclature ne les contient.

Ce mélange a un coût mesuré. On a cherché à peupler les trente-neuf depuis le
champ `industry` : **dix seulement** ont une industrie dominante non revendiquée
par un autre narratif, et même parmi eux « Quantum Computing » attirerait cent
soixante fabricants de matériel informatique, « Biotech » passerait de vingt et
un titres à cinq cent quatre-vingt-seize. « Cybersécurité » est à CENT pour cent
« Software - Infrastructure », industrie que cinq autres narratifs revendiquent.

L'industrie décrit ce que la société VEND. Le narratif décrit une THÈSE. Les
confondre casse les deux.

L'ALTERNATIVE, ET ELLE EST DANS LE PLAN DE BATAILLE

Deux axes orthogonaux, portés tous deux par chaque société :

  · l'axe MÉTIER se DÉRIVE — ce fichier. Vingt mille sociétés, deux cent
    vingt-deux industries d'au moins huit titres, onze secteurs. Aucune liste
    écrite à la main, donc exhaustif, à jour chaque nuit, et AUDITABLE : on peut
    demander « pourquoi cette société est-elle là » et la réponse est un champ.
  · l'axe THÈME reste éditorial, petit et assumé. C'est sa valeur, et c'est ce
    qu'aucun découpage automatique ne remplacera.

Le concurrent, lui, cure ses « Sélections » à la main. Les nôtres se calculent.

CE QUE CE COLLECTEUR NE COÛTE PAS

Aucune requête réseau. Il relit `marche_NN.json`, déjà écrit, et tout ce dont il
a besoin y est — mesuré : cours 100 %, moyenne 50 jours 98,8 %, performance un
mois 98,5 %, capitalisation 100 %, industrie 100 %.

LA MÊME FORMULE QUE LE TRACKER, POUR QUE LES DEUX AXES SE COMPARENT

    score = 0,55 × rang(momentum relatif)
          + 0,225 × rang(largeur)
          + 0,225 × rang(momentum de prix)

⚠ La normalisation est ORDINALE : un score vaut cent fois le rang divisé par le
nombre de groupes moins un. Il n'a donc AUCUN sens absolu et il change si l'on
ajoute ou retire un groupe. C'est écrit dans la sortie, en clair.
"""
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

CACHE = Path.home() / "Library" / "Caches" / "site_crypto_finance"

# Un groupe de moins de huit titres n'a pas d'agrégat qui veuille dire quelque
# chose : c'est la même règle que pour les médianes par industrie.
MINIMUM = 8

# Combien de sociétés nommées par groupe. Cinq suffisent à dire qui pèse ; dix
# doublaient le poids du fichier pour un cinquième d'information en plus.
PRINCIPALES_N = 5

# Les grandeurs agrégées en moyenne pondérée par la capitalisation. Uniquement
# du SANS DIMENSION — agréger un montant mêlerait des devises.
PONDEREES = [
    "peRatio", "peForward", "psRatio", "pbRatio", "evEbitda", "evSales",
    "grossMargin", "operatingMargin", "profitMargin", "ebitdaMargin",
    "roe", "roa", "roic", "roce",
    "debtEbitda", "debtEquity", "currentRatio", "interestCoverage",
    "dividendYield", "payoutRatio", "buybackYield", "fcfYield",
    "croissance_ca_pct", "croissance_bpa_pct", "croissance_ca_3a_pct",
    "beta", "ch1m", "ch3m", "ch6m", "ch1y", "ch3y", "ch5y",
]

# Les centiles de winsorisation, pour que quelques aberrations n'emportent pas
# une moyenne. Le dépôt applique déjà cette règle à ses agrégats sectoriels.
WINSOR = (5, 95)


def _centile(tri, p):
    if not tri:
        return None
    k = (len(tri) - 1) * (p / 100.0)
    b = int(k)
    h = min(b + 1, len(tri) - 1)
    return tri[b] if b == h else tri[b] + (tri[h] - tri[b]) * (k - b)


def _moyenne_ponderee(paires):
    """Moyenne pondérée par la capitalisation, après winsorisation.

    Sans winsorisation, un P/E à quarante mille tire la moyenne d'un secteur
    entier. Le dépôt s'est déjà fait mordre là-dessus.
    """
    paires = [(v, w) for v, w in paires
              if isinstance(v, (int, float)) and not isinstance(v, bool)
              and v == v and abs(v) != float("inf") and w and w > 0]
    if len(paires) < 3:
        return None, 0, None, None
    vals = sorted(v for v, _ in paires)
    lo, hi = _centile(vals, WINSOR[0]), _centile(vals, WINSOR[1])
    num = den = 0.0
    for v, w in paires:
        v = max(lo, min(hi, v))
        num += v * w
        den += w
    med = _centile(vals, 50)
    return (round(num / den, 4) if den else None), len(paires), \
        round(med, 4), (round(_centile(vals, 25), 4), round(_centile(vals, 75), 4))


def rang_normalise(valeurs):
    """Cent fois le rang, divisé par le nombre de groupes moins un.

    Purement ORDINAL et relatif aux autres groupes : un score de soixante-quinze
    ne veut pas dire « bon », il veut dire « meilleur que les trois quarts ».
    Une valeur absente vaut cinquante — le milieu, faute de mieux.
    """
    connus = [(v, k) for k, v in valeurs.items() if isinstance(v, (int, float))]
    connus.sort()
    n = len(connus)
    out = {k: 50.0 for k in valeurs}
    for rang, (_, k) in enumerate(connus):
        out[k] = round(100.0 * rang / (n - 1), 2) if n > 1 else 50.0
    return out


def main():
    t0 = time.time()

    # ── L'univers : les cotations principales ──
    f_uni = CACHE / "univers_actions.json"
    if not f_uni.exists():
        raise SystemExit("[fatal] univers_actions.json absent : impossible de "
                         "savoir quelle cotation est la principale.")
    with f_uni.open(encoding="utf-8") as fh:
        u = json.load(fh)
    principales = {t.get("yahoo") or t.get("sa")
                   for t in u.get("titres", [])
                   if (t.get("yahoo") or t.get("sa")) and t.get("principal")}
    if not principales:
        raise SystemExit("[fatal] l'univers ne marque aucune cotation principale.")

    frags = sorted(glob.glob(str(CACHE / "marche_[0-9]*.json")))
    if not frags:
        raise SystemExit("[fatal] aucun fragment de marché.")
    lignes, champs = {}, None
    for f in frags:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        champs = champs or d.get("champs")
        for sym, a in (d.get("societes") or {}).items():
            if sym in principales:
                lignes[sym] = a
    if not champs:
        raise SystemExit("[fatal] aucun fragment ne porte sa liste de champs.")
    i = {c: k for k, c in enumerate(champs)}
    print("[ok] %d cotations principales lues dans %d fragments"
          % (len(lignes), len(frags)))

    def val(a, c):
        k = i.get(c)
        if k is None or k >= len(a):
            return None
        v = a[k]
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def txt(a, c):
        k = i.get(c)
        return a[k] if (k is not None and k < len(a) and a[k]) else None

    # ── Le secteur manquant se déduit de l'industrie ──
    # 7,5 % des sociétés n'ont pas de secteur mais toutes ont une industrie. Le
    # lien entre les deux est connu pour les 92,5 % qui portent les deux : on
    # l'applique aux autres, quand une industrie ne pointe que vers UN secteur.
    lien = defaultdict(Counter)
    for a in lignes.values():
        s, ind = txt(a, "sector"), txt(a, "industry")
        if s and ind:
            lien[ind][s] += 1
    deduit = {}
    for ind, c in lien.items():
        if len(c) == 1 or c.most_common(1)[0][1] >= 0.9 * sum(c.values()):
            deduit[ind] = c.most_common(1)[0][0]

    secteur_de = {}
    n_deduit = n_sans = 0
    for sym, a in lignes.items():
        s = txt(a, "sector")
        if not s:
            s = deduit.get(txt(a, "industry"))
            if s:
                n_deduit += 1
        if s:
            secteur_de[sym] = s
        else:
            n_sans += 1
    print("[ok] secteur : %d déduits de l'industrie, %d restent sans"
          % (n_deduit, n_sans))

    # ── Les deux niveaux ──
    def agreger(cle_de, nom_niveau):
        groupes = defaultdict(list)
        for sym, a in lignes.items():
            g = cle_de(sym, a)
            if g:
                groupes[g].append((sym, a))
        sortie = {}
        for g, membres in groupes.items():
            if len(membres) < MINIMUM:
                continue
            capi = sum((val(a, "marketCapUsd") or 0) for _, a in membres)
            bloc = {
                "nom": g,
                "n_titres": len(membres),
                "capitalisation_usd": int(capi),
            }
            for c in PONDEREES:
                if c not in i:
                    continue
                paires = [(val(a, c), val(a, "marketCapUsd") or 0)
                          for _, a in membres]
                moy, n, med, quart = _moyenne_ponderee(paires)
                if moy is None:
                    continue
                # Seulement la moyenne pondérée : la médiane, les quartiles et
                # l'effectif de chaque grandeur sont DÉJÀ publiés par le
                # collecteur de médianes par industrie. Les republier ferait
                # passer le fichier de 342 à 948 Ko — et créerait deux endroits
                # où le même nombre peut diverger.
                bloc[c] = moy

            # ── La largeur : la part des titres au-dessus de leur moyenne ──
            # Comptée sur TOUS les membres, pas sur trois. Le Tracker comptait
            # sur trois, et son « pourcentage » ne prenait que quatre valeurs.
            for lib, test in (
                ("largeur_ma50", lambda a: (val(a, "price"), val(a, "ma50"))),
                ("largeur_ma200", lambda a: (val(a, "price"), val(a, "ma200"))),
            ):
                n = d_ = 0
                for _, a in membres:
                    p, m = test(a)
                    if p is None or m is None or m <= 0:
                        continue
                    d_ += 1
                    n += 1 if p > m else 0
                if d_ >= MINIMUM:
                    bloc[lib] = round(100.0 * n / d_, 1)
                    bloc[lib + "_n"] = d_

            # Les dix plus grosses, pour que le lecteur sache qui pèse.
            gros = sorted(((val(a, "marketCapUsd") or 0, s, txt(a, "name"))
                           for s, a in membres), reverse=True)[:PRINCIPALES_N]
            bloc["principales"] = [{"s": s, "n": n or s,
                                    "capi": int(c)} for c, s, n in gros]
            bloc["concentration_tete"] = (
                round(100.0 * sum(c for c, _, _ in gros) / capi, 1) if capi else None)
            sortie[g] = bloc
        print("[ok] %-10s : %d groupes d'au moins %d titres, sur %d groupes"
              % (nom_niveau, len(sortie), MINIMUM, len(groupes)))
        return sortie

    secteurs = agreger(lambda s, a: secteur_de.get(s), "secteurs")
    industries = agreger(lambda s, a: txt(a, "industry"), "industries")

    # ── Le score composite, sur chaque niveau ──
    # Même formule que le Tracker, pour que les deux axes se comparent.
    def scorer(blocs, nom_niveau):
        if len(blocs) < 3:
            return
        # Le momentum RELATIF : la performance du groupe moins celle du monde,
        # pondérée par capitalisation sur tout l'univers.
        num = den = 0.0
        for a in lignes.values():
            p, w = val(a, "ch3m"), val(a, "marketCapUsd") or 0
            if p is not None and w > 0:
                num += p * w
                den += w
        monde_3m = (num / den) if den else 0.0

        rel = {g: (b.get("ch3m") - monde_3m) if b.get("ch3m") is not None else None
               for g, b in blocs.items()}
        larg = {g: b.get("largeur_ma50") for g, b in blocs.items()}
        prix = {g: (0.5 * (b.get("ch1m") or 0) + 0.5 * (b.get("ch3m") or 0))
                if (b.get("ch1m") is not None or b.get("ch3m") is not None) else None
                for g, b in blocs.items()}

        r_rel, r_larg, r_prix = (rang_normalise(rel), rang_normalise(larg),
                                 rang_normalise(prix))
        classement = []
        for g, b in blocs.items():
            b["momentum_relatif"] = round(rel[g], 2) if rel[g] is not None else None
            b["score_momentum"] = r_rel[g]
            b["score_largeur"] = r_larg[g]
            b["score_prix"] = r_prix[g]
            b["score"] = round(0.55 * r_rel[g] + 0.225 * r_larg[g]
                               + 0.225 * r_prix[g], 2)
            # Le régime : trois conditions, comme le Tracker.
            b["regime"] = ("porteur"
                           if (b.get("largeur_ma50") or 0) > 50
                           and (rel[g] or 0) > 0 else "neutre")
            classement.append((b["score"], g))
        classement.sort(reverse=True)
        for rang, (_, g) in enumerate(classement, 1):
            blocs[g]["rang"] = rang
        print("[ok] %-10s : score calculé, monde à %+.2f %% sur trois mois"
              % (nom_niveau, monde_3m))

    scorer(secteurs, "secteurs")
    scorer(industries, "industries")

    # ── Écriture ──
    charge = {
        "genere_le": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "collecte de marché, agrégée localement — aucune requête réseau",
        "univers": len(lignes),
        "minimum_titres": MINIMUM,
        "winsorisation": "%d–%d centiles avant moyenne pondérée" % WINSOR,
        "formule_score": ("0,55 × rang(momentum relatif à 3 mois) + 0,225 × "
                          "rang(largeur au-dessus de la MA50) + 0,225 × "
                          "rang(momentum de prix)"),
        "avertissement_score": (
            "La normalisation est ORDINALE : le score vaut cent fois le rang "
            "divisé par le nombre de groupes moins un. Il n'a aucun sens absolu "
            "et il change si l'on ajoute ou retire un groupe. Un score de "
            "soixante-quinze ne dit pas « bon », il dit « meilleur que les trois "
            "quarts »."),
        "secteurs": secteurs,
        "industries": industries,
    }
    (CACHE / "secteurs_monde.json").write_text(
        json.dumps(charge, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    (CACHE / "secteurs_monde.js").write_text(
        "window.__SECTEURS_MONDE__=" +
        json.dumps(charge, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")

    ko = (CACHE / "secteurs_monde.json").stat().st_size / 1024.0
    print("[ok] secteurs_monde — %.0f Ko, %.1f s" % (ko, time.time() - t0))

    # ── Ce que ça donne, pour qu'on puisse le lire sans ouvrir le fichier ──
    print("\n   %-32s %6s %10s %7s %7s %6s"
          % ("secteur", "titres", "capi Md$", "P/E", "largeur", "score"))
    for g, b in sorted(secteurs.items(), key=lambda x: -(x[1].get("score") or 0)):
        print("   %-32s %6d %10.0f %7s %6s %% %6.1f"
              % (g[:32], b["n_titres"], b["capitalisation_usd"] / 1e9,
                 ("%.1f" % b["peRatio"]) if b.get("peRatio") else "—",
                 ("%.0f" % b["largeur_ma50"]) if b.get("largeur_ma50") else "—",
                 b.get("score") or 0))
    print("\n   les dix industries les mieux classées :")
    for g, b in sorted(industries.items(),
                       key=lambda x: -(x[1].get("score") or 0))[:10]:
        print("   %-38s %5d titres  largeur %3s %%  score %5.1f"
              % (g[:38], b["n_titres"],
                 ("%.0f" % b["largeur_ma50"]) if b.get("largeur_ma50") else "—",
                 b.get("score") or 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
