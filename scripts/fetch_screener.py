#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Le jeu de données du screener : filtrer et trier 37 986 sociétés dans le navigateur.

CE QU'ON REMPLACE
Aujourd'hui la page n'offre qu'une carte de chaleur de trente-neuf lignes
sectorielles, sans tri ni filtre. Le concurrent, lui, filtre par titre sur une
quatre-vingtaine de critères. C'est le volet C du plan.

LA CONTRAINTE, ET POURQUOI ELLE EST DURE
Un screener ne se découpe pas. La fiche d'une société peut tirer un paquet sur
cinq cents — filtrer et trier, non : il faut TOUTES les lignes en même temps. Le
découpage par empreinte, qui règle le problème de la fiche, ne règle pas
celui-ci.

CE QUE LA MESURE A DONNÉ (27/08/2026, sur les 37 986 lignes de la collecte)

    97 champs, tout le monde                      14 290 Ko compressé
    31 champs, tout le monde                       4 694 Ko
    31 champs arrondis, tout le monde              3 496 Ko
    31 champs arrondis, les 10 000 plus grosses      594 Ko
    31 champs arrondis, les  5 000 plus grosses      278 Ko

D'où DEUX ÉTAGES. Le premier — les dix mille plus grosses capitalisations —
part avec la page : moins de six cents kilo-octets, et il couvre ce que
quiconque cherche vraiment. Le second attend qu'on demande l'univers entier.

⚠ L'interface DOIT dire lequel des deux est chargé. Un screener qui annonce
« 3 résultats » alors qu'il n'a regardé qu'un quart du monde ne se trompe pas
d'un peu : il donne une réponse fausse à la question posée.

L'ARRONDI NE COÛTE RIEN
Publier un P/E à 69,725064 quand la page en affiche une décimale est du poids
pur. Trois décimales sous dix, une au-dessus de dix, l'entier au-dessus de mille.
Mesuré : un quart du poids en moins, et pas un chiffre affiché qui change.

SORTIES
    screener_index.js    liste des champs, bornes de filtre, pays/secteurs/
                         industries avec leurs effectifs   (~40 Ko)
    screener_1.json      les 10 000 plus grosses capitalisations
    screener_2.json      les autres, à la demande
"""
import glob
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

CACHE = Path.home() / "Library" / "Caches" / "site_crypto_finance"
PREMIER_ETAGE = 10000

# ── L'AMORÇAGE : ce que la page tire en arrivant ──
#
# Mesuré le 28/08/2026 sur un vrai chargement : la page tirait 10,2 Mo AVANT que
# le visiteur n'ait rien demandé, dont 3,4 Mo pour `screener_1.json` à lui seul.
# Or le tableau n'affiche que SOIXANTE lignes au premier écran, triées par
# capitalisation décroissante. Tirer dix mille sociétés pour en montrer soixante,
# c'est trente-quatre fois trop — et sur une connexion mobile à cinq mégabits,
# cela repousse le premier chiffre de plus de cinq secondes.
#
# On écrit donc un troisième fichier, plus court, qui couvre la vue par défaut et
# les premiers défilements. Les deux étages complets ne changent pas : dès que le
# lecteur filtre, trie sur une autre colonne ou dépasse cet amorçage, la page
# tire `screener_1.json` comme avant. Un screener qui ne regarderait qu'une
# partie du monde donnerait une réponse fausse à la question posée — l'amorçage
# sert à AFFICHER vite, jamais à répondre.
AMORCAGE = 1200

# Les grandeurs du screener. Choisies pour couvrir les blocs de filtres du
# concurrent : identité, valorisation, rentabilité, croissance, santé,
# dividende, marché.
CHAMPS = [
    # identité
    "name", "country", "exchange", "sector", "industry", "priceCurrency",
    "isin", "employees",
    # taille et cours
    "marketCapUsd", "price", "ch1y",
    # valorisation
    "peRatio", "peForward", "psRatio", "pbRatio", "evEbitda", "evSales",
    "pegRatio", "fcfYield", "earningsYield",
    # rentabilité
    "grossMargin", "operatingMargin", "profitMargin", "ebitdaMargin",
    "roe", "roa", "roic", "roce",
    # croissance
    "croissance_ca_pct", "croissance_bpa_pct", "croissance_ca_3a_pct",
    # santé
    "debtEbitda", "debtEquity", "currentRatio", "quickRatio",
    "interestCoverage", "taxRate",
    # dividende
    "dividendYield", "payoutRatio", "buybackYield",
    # marché
    "beta", "rsi", "ch1m", "ch3m", "ch5y",
]

# Ce qui est du TEXTE : on ne l'arrondit pas, et on ne lui calcule pas de bornes.
TEXTE = {"name", "country", "exchange", "sector", "industry", "priceCurrency",
         "isin"}

# Ce sur quoi on propose un filtre par liste, avec les effectifs.
CATEGORIES = ["country", "sector", "industry", "exchange", "priceCurrency"]


def _arrondir(v, entier=False):
    """Trois décimales sous dix, une au-dessus, l'entier au-dessus de mille.

    Publier un P/E à 69,725064 quand la page en affiche une décimale est du
    poids pur. Mesuré : un quart du fichier, et pas un chiffre affiché qui
    change.
    """
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return v
    if v != v:                                   # NaN
        return None
    if entier:
        return int(v)
    a = abs(v)
    if a >= 1000:
        return round(v)
    if a >= 10:
        return round(v, 1)
    return round(v, 3)


def _centile(tri, q):
    if not tri:
        return None
    if len(tri) == 1:
        return tri[0]
    k = (len(tri) - 1) * q
    b = int(k)
    h = min(b + 1, len(tri) - 1)
    return tri[b] if b == h else tri[b] + (tri[h] - tri[b]) * (k - b)


def main():
    t0 = time.time()
    frags = sorted(glob.glob(str(CACHE / "marche_[0-9]*.json")))
    if not frags:
        raise SystemExit("[fatal] aucun fragment de marché : la collecte de "
                         "marché a-t-elle tourné ?")

    brut, champs_src = {}, None
    for f in frags:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            print("[!] %s illisible : %s" % (os.path.basename(f), e))
            continue
        champs_src = champs_src or d.get("champs")
        brut.update(d.get("societes") or {})

    if not champs_src:
        raise SystemExit("[fatal] aucun fragment ne porte sa liste de champs")

    # ── Une société, une ligne : les cotations principales seulement ──
    # Sur 37 986 lignes, Apple apparaît cinq fois. Un tableau filtrable qui rend
    # cinq fois la même société n'est pas un screener.
    #
    # Et ce filtre corrige un DÉFAUT DE DONNÉE mesuré : 231 lignes portent une
    # capitalisation impossible — Apple à 6,9 quadrillions de dollars — parce
    # que la source donne `marketCap` dans la monnaie de la PLACE et
    # `priceCurrency` dans celle de la SOCIÉTÉ. 230 des 231 sont des cotations
    # secondaires.
    fu = CACHE / "univers_actions.json"
    if not fu.exists():
        raise SystemExit("[fatal] univers_actions.json manquant : impossible de "
                         "savoir quelle cotation est la principale.")
    with fu.open(encoding="utf-8") as fh:
        uni = json.load(fh)
    principales = set()
    for t in uni.get("titres", []):
        sym = t.get("yahoo") or t.get("sa")
        if sym and t.get("principal"):
            principales.add(sym)
    if not principales:
        raise SystemExit("[fatal] l'univers ne marque aucune cotation principale.")
    avant = len(brut)
    brut = {s: v for s, v in brut.items() if s in principales}
    print("[ok] %d cotations principales retenues sur %d lignes (%d secondaires "
          "écartées)" % (len(brut), avant, avant - len(brut)))
    idx = {c: i for i, c in enumerate(champs_src)}
    absents = [c for c in CHAMPS if c not in idx]
    if absents:
        print("[!] absents de la collecte, ignorés : %s" % ", ".join(absents))
    champs = [c for c in CHAMPS if c in idx]
    ii = [idx[c] for c in champs]
    i_capi = idx["marketCapUsd"]

    print("[ok] %d sociétés, %d champs retenus sur %d disponibles"
          % (len(brut), len(champs), len(champs_src)))

    # ── Les lignes, arrondies ──
    lignes = {}
    for sym, a in brut.items():
        ligne = []
        for c, i in zip(champs, ii):
            v = a[i] if i < len(a) else None
            if c in TEXTE:
                ligne.append(v)
            else:
                ligne.append(_arrondir(v, c == "marketCapUsd"))
        lignes[sym] = ligne

    # ── LE SECTEUR DÉDUIT DE L'INDUSTRIE ──
    #
    # `sector` est rempli à 92,5 % (18 592 sur 20 096) quand `industry` l'est à
    # 100 %. Les 1 504 sociétés sans secteur DISPARAISSENT dès qu'un secteur est
    # choisi — et le filtre par secteur est le plus employé du screener. Un
    # lecteur qui demande « Financials » ne verra jamais CaixaBank, et rien ne
    # l'en avertit : 642 de ces sociétés sont dans le premier étage, dont cinq à
    # plus de quatre-vingts milliards de dollars.
    #
    # Or 1 498 des 1 504 ont une industrie, et la correspondance industrie →
    # secteur est déjà écrite dans les 18 592 renseignées. On la lit chez elles
    # et on la leur applique.
    #
    # ⚠ Une industrie peut apparaître sous DEUX secteurs quand la source hésite.
    # On retient alors le plus fréquent, et on ne déduit rien si l'industrie est
    # ambiguë à moins de deux contre un : une déduction incertaine vaut moins
    # qu'une case vide, qui au moins ne ment pas.
    # ⚠ DEUX TAXONOMIES, ET AUCUNE INDUSTRIE EN COMMUN.
    # Une première tentative apprenait la correspondance industrie → secteur chez
    # les sociétés renseignées, pour l'appliquer aux autres. Elle n'a rattaché que
    # 28 sociétés sur 1 504, et la mesure a dit pourquoi : les deux groupes ne
    # parlent pas la même langue. Les renseignées emploient 226 industries de
    # style « Semiconductors », « Banks - Diversified » ; les autres, 288 libellés
    # SIC de la SEC — « Semiconductors and Related Devices », « Commercial Banks ».
    # ZÉRO industrie partagée entre les deux ensembles : il n'y avait rien à
    # apprendre.
    #
    # On écrit donc la table à la main. Le vocabulaire SIC est régulier, et un
    # mot suffit presque toujours. L'ordre va du PLUS SPÉCIFIQUE au plus général :
    # « Real Estate Investment Trusts » contient à la fois « real estate » et
    # « trust », et c'est l'immobilier qui doit gagner.
    SIC_VERS_SECTEUR = [
        # immobilier avant finance — les foncières portent « trust »
        (("real estate", "land subdivider", "operators of apartment",
          "operators of nonresidential", "operators of dwellings",
          "land developer"), "Real Estate"),
        # santé avant matériaux — « biological products » n'est pas de la chimie
        (("pharmaceutical", "biological product", "medicinal", "surgical",
          "medical", "diagnostic substance", "health", "hospital", "dental",
          "in vitro", "orthopedic", "laboratory analytical"), "Healthcare"),
        (("semiconductor", "computer", "software", "data processing",
          "printed circuit",
          "electronic component", "prepackaged", "information retrieval",
          "calculating", "office machines", "magnetic", "optical instrument",
          "laboratory apparatus"), "Technology"),
        (("bank", "savings institution", "credit institution", "insurance",
          "insurance carrier", "security broker", "commodity broker",
          "investor", "unit investment",
          "management investment", "finance service", "personal credit",
          "mortgage banker", "title insurance", "blank check", "asset-backed",
          "investment advice", "federal and federally"), "Financials"),
        (("crude petroleum", "petroleum refining", "oil and gas", "oil & gas",
          "drilling oil", "natural gas", "pipe line", "bituminous coal",
          "petroleum bulk"), "Energy"),
        (("electric services", "gas and other services", "water supply",
          "cogeneration", "electric and other services",
          "natural gas distribution", "electric & other"), "Utilities"),
        (("telephone", "communications service", "radio", "television",
          "broadcast", "cable", "publishing", "advertising", "motion picture",
          "newspaper", "periodical", "book"), "Communication Services"),
        (("food and kindred", "agricultural production", "fats and oils",
          "beverages", "malt beverages", "bottled", "sugar", "dairy",
          "grain mill", "bakery", "canned", "tobacco", "soap", "grocery",
          "meat packing", "poultry", "fishing", "cigarettes",
          "food preparation",
          "groceries"), "Consumer Staples"),
        (("eating place", "hotel", "retail", "apparel", "amusement",
          "recreation", "motor vehicle", "footwear", "jewelry", "toys",
          "sporting", "furniture store", "auto dealer", "department store",
          "textile mill", "textile", "household audio", "automotive dealer",
          "catalog", "leather", "household appliance", "educational service",
          "personal service", "membership sport"), "Consumer Discretionary"),
        (("mining", "ores", "gold and silver", "metal", "chemical", "cement",
          "steel", "paper", "plastics", "glass", "rubber", "lumber",
          "fertilizer", "paint", "abrasive", "concrete", "clay",
          "industrial inorganic", "industrial organic", "adhesive",
          "pulp", "primary production", "rolling drawing"), "Materials"),
        (("construction", "machinery", "engineering", "transportation",
          "trucking", "air transportation", "water transportation",
          "shipping", "railroad", "aircraft", "ship building",
          "ship and boat", "boat building", "power, distribution", "engines",
          "electrical industrial", "electric lighting", "wiring equip",
          "measuring", "instrument", "services-",
          "wholesale", "arrangement of transportation", "courier",
          "refuse system", "sanitary", "heavy construction",
          "special industry machinery", "general industrial",
          "fabricated", "motors and generators", "search detection",
          "public warehousing"), "Industrials"),
    ]

    def secteur_sic(libelle):
        t = (libelle or "").lower()
        if not t:
            return None
        for motifs, secteur in SIC_VERS_SECTEUR:
            for m in motifs:
                if m in t:
                    return secteur
        return None

    i_sec, i_ind = champs.index("sector"), champs.index("industry")

    # ── UN SEUL VOCABULAIRE DE SECTEURS ──
    # La source classe la plupart des titres selon les onze secteurs usuels, mais
    # une poignée arrive sous un vocabulaire Refinitiv. Mesuré le 28/08/2026 :
    # 21 sociétés sur 21 296, des cotations philippines pour l'essentiel. Rien en
    # volume, tout en usage — un lecteur qui filtre « Consumer Staples » ne verra
    # pas les sept distributeurs alimentaires rangés sous « Consumer
    # Non-Cyclicals ». Un filtre qui oublie en silence est pire qu'un filtre absent.
    SECTEURS_ETRANGERS = {
        "Consumer Non-Cyclicals": "Consumer Staples",
        "Academic & Educational Services": "Consumer Discretionary",
        # « Consumer Goods » ne recouvre rien de net : la seule société qui le
        # porte a pour industrie « Electronic Equipment ». On efface plutôt que
        # de deviner — la déduction par libellé d'industrie, juste en dessous,
        # tranchera avec une information que ce secteur n'a pas.
        "Consumer Goods": None,
    }
    ramenes = 0
    for l in lignes.values():
        neuf = SECTEURS_ETRANGERS.get(l[i_sec], "")
        if neuf != "":
            l[i_sec] = neuf
            ramenes += 1
    if ramenes:
        print("[ok] %d secteur(s) ramené(s) au vocabulaire canonique" % ramenes)

    deduits = inconnus = sans_industrie = 0
    non_classes = Counter()
    for l in lignes.values():
        if l[i_sec]:
            continue
        ind = l[i_ind]
        if not ind:
            sans_industrie += 1
            continue
        s = secteur_sic(ind)
        if s:
            l[i_sec] = s
            deduits += 1
        else:
            inconnus += 1
            non_classes[ind] += 1
    print("[ok] secteur déduit du libellé SIC : %d rattachée(s), %d non classée(s), "
          "%d sans industrie" % (deduits, inconnus, sans_industrie))
    if non_classes:
        print("     libellés SIC les plus fréquents non couverts : %s"
              % ", ".join("%s (%d)" % (k[:34], v)
                          for k, v in non_classes.most_common(5)))

    # ── Deux étages, par capitalisation ──
    # Un screener ne se découpe pas par empreinte : il faut toutes les lignes à
    # la fois pour filtrer. On coupe donc par TAILLE, ce qui a un sens pour le
    # lecteur — et l'interface dit lequel des deux étages est chargé.
    ordre = sorted(lignes.items(),
                   key=lambda kv: -(kv[1][champs.index("marketCapUsd")] or 0))
    etage1 = dict(ordre[:PREMIER_ETAGE])
    etage2 = dict(ordre[PREMIER_ETAGE:])
    seuil = ordre[min(PREMIER_ETAGE, len(ordre)) - 1][1][champs.index("marketCapUsd")] \
        if ordre else 0

    # ── Les bornes de filtre, aux centiles ──
    # Le minimum et le maximum d'un ratio sont des aberrations : un P/E à
    # quarante mille rendrait tout curseur inutilisable. On borne aux centiles
    # 1 et 99, et on dit qu'au-delà ça existe encore.
    bornes = {}
    for c in champs:
        if c in TEXTE:
            continue
        j = champs.index(c)
        vals = sorted(v for v in (l[j] for l in lignes.values())
                      if isinstance(v, (int, float)) and not isinstance(v, bool))
        if len(vals) < 50:
            continue
        bornes[c] = {
            "min": _arrondir(_centile(vals, 0.01)),
            "q1": _arrondir(_centile(vals, 0.25)),
            "med": _arrondir(_centile(vals, 0.50)),
            "q3": _arrondir(_centile(vals, 0.75)),
            "max": _arrondir(_centile(vals, 0.99)),
            "n": len(vals),
            "vrai_min": _arrondir(vals[0]),
            "vrai_max": _arrondir(vals[-1]),
        }

    # ── Les listes de catégories, avec leurs effectifs ──
    cats = {}
    for c in CATEGORIES:
        if c not in champs:
            continue
        j = champs.index(c)
        n = Counter(l[j] for l in lignes.values() if l[j])
        cats[c] = sorted(([k, v] for k, v in n.items()), key=lambda x: -x[1])

    # ── Ce qui reste d'invraisemblable, on le NOMME ──
    # Une aberration survit au filtre : Grupo Argos, holding colombien coté à
    # Santiago, marqué cotation principale, publié à 3 850 milliards de dollars
    # quand il en vaut deux. Aucune règle générale ne l'attrape sans en emporter
    # d'autres — j'ai essayé « la devise annoncée doit être celle de la place »,
    # elle attrape zéro aberration et marque 1 728 lignes légitimes. On compte
    # et on nomme, au lieu de laisser passer en silence.
    PLAFOND_PLAUSIBLE = 6e12          # la plus grosse capitalisation réelle ~5 500 Md$
    jc = champs.index("marketCapUsd")
    jn = champs.index("name") if "name" in champs else None
    jx = champs.index("exchange") if "exchange" in champs else None
    douteux = [(v[jc], s, v[jn] if jn is not None else "", v[jx] if jx is not None else "")
               for s, v in lignes.items()
               if isinstance(v[jc], (int, float)) and v[jc] > PLAFOND_PLAUSIBLE]
    douteux.sort(reverse=True)
    if douteux:
        print("[!] %d capitalisation(s) au-dessus de %d Md$, donc invraisemblable(s) :"
              % (len(douteux), int(PLAFOND_PLAUSIBLE / 1e9)))
        for mu, sym, nom, pl in douteux[:10]:
            print("      %9.0f Md$  %-14s %-30s %s"
                  % (mu / 1e9, sym, (nom or "")[:30], (pl or "")[:26]))
        index_douteux = [s for _, s, _, _ in douteux]
    else:
        index_douteux = []
        print("[ok] aucune capitalisation invraisemblable")

    # ── Les médianes des colonnes affichées, par industrie ──
    # Chaque valeur du tableau porte un point disant si elle est au-dessus ou en
    # dessous de la médiane de SON industrie. Les médianes complètes pèsent trois
    # mégaoctets et demi en trente-deux paquets — cinquante-trois grandeurs et
    # vingt et un quantiles chacune. Le screener n'en affiche que six : on les
    # embarque avec l'index, et les points sont là dès la première ligne rendue.
    COLONNES_POINTS = ["peRatio", "pbRatio", "roe", "profitMargin",
                       "dividendYield", "croissance_ca_pct"]
    MINI_INDUSTRIE = 8       # même règle que partout : en dessous, c'est un tirage
    j_ind = champs.index("industry") if "industry" in champs else None
    med_ind = {}
    if j_ind is not None:
        par_ind = {}
        for v in lignes.values():
            nom = v[j_ind] if j_ind < len(v) else None
            if not nom:
                continue
            bloc = par_ind.setdefault(nom, {})
            for c in COLONNES_POINTS:
                if c not in champs:
                    continue
                x = v[champs.index(c)]
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    bloc.setdefault(c, []).append(x)
        for nom, bloc in par_ind.items():
            sortie_ind = {}
            for c, vals in bloc.items():
                if len(vals) < MINI_INDUSTRIE:
                    continue
                vals.sort()
                sortie_ind[c] = _arrondir(_centile(vals, 0.50))
            if sortie_ind:
                med_ind[nom] = sortie_ind
        print("[ok] médianes du screener : %d industries, %d couples"
              % (len(med_ind), sum(len(x) for x in med_ind.values())))

    index = {
        "genere_le": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "collecte de marché (stockanalysis), assemblée localement",
        "champs": champs,
        "texte": sorted(TEXTE & set(champs)),
        "total": len(lignes),
        # Combien de sociétés voyagent dans l'amorçage : l'interface en a
        # besoin pour savoir quand elle doit tirer l'étage complet.
        "amorcage": len(ordre[:AMORCAGE]),
        "etage1": len(etage1),
        "etage2": len(etage2),
        "seuil_etage1_usd": int(seuil or 0),
        "bornes": bornes,
        "categories": cats,
        "douteux": index_douteux,
        "medianes_industrie_screener": med_ind,
        "note": ("Deux étages : les %d plus grosses capitalisations partent avec "
                 "la page, le reste attend qu'on demande l'univers entier. "
                 "L'interface DOIT dire lequel est chargé — un screener qui "
                 "annonce trois résultats en n'ayant regardé qu'un quart du monde "
                 "donne une réponse fausse à la question posée."
                 % PREMIER_ETAGE),
    }

    (CACHE / "screener_index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    (CACHE / "screener_index.js").write_text(
        "window.__SCREENER__=" + json.dumps(index, ensure_ascii=False,
                                            separators=(",", ":")) + ";\n",
        encoding="utf-8")
    amorce = dict(ordre[:AMORCAGE])
    for nom, part in (("screener_0.json", amorce),
                      ("screener_1.json", etage1), ("screener_2.json", etage2)):
        (CACHE / nom).write_text(
            json.dumps({"societes": part}, ensure_ascii=False,
                       separators=(",", ":")), encoding="utf-8")

    ko = lambda f: (CACHE / f).stat().st_size / 1024.0
    print("[ok] étage 1 : %d sociétés (au-dessus de %.0f M$) — %.0f Ko"
          % (len(etage1), (seuil or 0) / 1e6, ko("screener_1.json")))
    print("[ok] amorçage : %d sociétés — %.0f Ko (ce que la page tire en arrivant)"
          % (len(amorce), ko("screener_0.json")))
    print("[ok] étage 2 : %d sociétés — %.0f Ko" % (len(etage2), ko("screener_2.json")))
    print("[ok] index   : %.0f Ko · %d champs bornés · %s"
          % (ko("screener_index.json"), len(bornes),
             ", ".join("%s %d" % (c, len(v)) for c, v in cats.items())))
    print("[ok] %.1f s" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
