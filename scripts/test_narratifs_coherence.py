#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contrôle de cohérence du cache des fondamentaux par narratif.

Un garde-fou par défaut corrigé le 05/09/2026 — on relit le cache PUBLIÉ, pas
le code, parce que c'est le cache qui part à l'écran et que les quatre défauts
d'origine étaient tous invisibles depuis le code seul :

  1. le revenu publié n'était pas le dénominateur du rapport publié
     (mesuré : 5 narratifs sur 25 divergeaient, « Mineurs de bitcoin »
     affichait « — » à côté d'un dénominateur réel de 4 590,8 M$) ;
  2. un rapport au dénominateur effondré était publié comme s'il voulait dire
     quelque chose (« Jetons de paiement » à 126 256×, « Bitcoin
     institutionnel » à 369× en capitalisation/TVL) ;
  3. le multiple s'appelait « P/S » alors que son dénominateur est des frais ;
  4. 22 jetons publiaient une capitalisation supérieure à leur valeur
     pleinement diluée (Monero à 1,0184, ce qui est impossible) et 2 jetons
     dépassaient 100 % d'offre en circulation (USTB 100,6 %, KAG 101,3 %).

Usage :
    python3 test_narratifs_coherence.py                 # cache de production
    python3 test_narratifs_coherence.py <chemin.json>   # un autre cache
Sortie : 0 si tout passe, 1 sinon. Chaque échec nomme le narratif ou le jeton.
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE_DEFAUT = (Path.home() / "Library" / "Caches" / "site_crypto_finance"
                / "narratives_fundamentals_cache.json")

# Repli si le cache ne publie pas ses propres seuils (cache écrit par une
# version antérieure du collecteur). Ce ne sont pas des valeurs inventées :
# ce sont celles du collecteur, justifiées dans son en-tête par la
# distribution mesurée des 25 narratifs.
SEUILS_PAR_DEFAUT = {"ps_ttm": 10000.0, "mc_tvl": 100.0}

STATUTS_VALIDES = {"mesurable", "non_mesurable", "sans_objet"}
DENOMINATEURS_VALIDES = {"frais", "chiffre d'affaires", "mixte", None}

# Tolérance des comparaisons de sommes : le cache arrondit chaque revenu à
# 0,01 M$, et un panier compte jusqu'à ~45 constituants. 45 × 0,01 = 0,45 ;
# on prend 0,5 M$, soit l'erreur d'arrondi maximale possible, pas un
# amortisseur choisi pour faire passer le contrôle.
TOLERANCE_M = 0.5


class Echecs(list):
    def ajouter(self, categorie, ou, message):
        self.append((categorie, ou, message))


def cache_anterieur_au_correctif(cache, echecs):
    """Un cache écrit par une version antérieure du collecteur n'a simplement pas
    les champs qu'on contrôle. Le dire UNE fois, en nommant la cause, vaut mieux
    que dérouler cent-trente symptômes qui font croire à cent-trente défauts —
    et évite qu'on aille chercher un bug de calcul là où il n'y a qu'un miroir
    qui n'a pas été mis à jour."""
    narratives = cache.get("narratives", [])
    manquants = [n["narrative"] for n in narratives if "ps_ttm_statut" not in n]
    if not manquants:
        return False
    echecs.ajouter(
        "version", f"{len(manquants)}/{len(narratives)} narratifs",
        "cache antérieur au correctif du 05/09/2026 : ps_ttm_statut, "
        "rev_m_1y_crypto et rev_m_1y_actions absents. Les contrôles de "
        "dénominateur, de statut et de nature sont sautés — republier ce cache "
        "avec le collecteur corrigé avant de conclure quoi que ce soit.")
    return True


def charger(chemin):
    with open(chemin, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 1. Offre : capitalisation ≤ valeur pleinement diluée, circulation ≤ 100 % ──
def controler_offre(cache, echecs):
    """Le défaut mesuré le 05/09/2026 tenait à ce que la capitalisation vient du
    tracker (instant T) et la valeur pleinement diluée de CoinGecko (instant T').
    Aucune tolérance ici : une capitalisation au-dessus de la valeur pleinement
    diluée n'est pas un arrondi, c'est une impossibilité de définition."""
    vus = set()
    for n in cache.get("narratives", []):
        for t in n.get("tokens", []):
            cle = t.get("id") or t.get("symbol")
            if cle in vus:
                continue
            vus.add(cle)
            mcap = t.get("mcap_b") or 0
            fdv = t.get("fdv_b") or 0
            if mcap > 0 and 0 < fdv < mcap:
                echecs.ajouter(
                    "offre", f"{t.get('symbol')} ({n['narrative']})",
                    f"capitalisation {mcap} Md$ > valeur pleinement diluée {fdv} Md$ "
                    f"(rapport {mcap / fdv:.4f})")
            circ = t.get("circ_pct")
            if circ is not None and circ > 100.0:
                echecs.ajouter(
                    "offre", f"{t.get('symbol')} ({n['narrative']})",
                    f"offre en circulation {circ} % > 100 %")
    return len(vus)


# ── 2. Le revenu publié EST le dénominateur du rapport publié ──────────────
def controler_denominateur(cache, echecs):
    """On recalcule le dénominateur depuis les constituants et on le compare au
    champ publié. C'est exactement l'incohérence d'origine : rev_m_1y_total ne
    sommait que les jetons crypto tandis que ps_ttm divisait par le panier
    complet, actions comprises."""
    for n in cache.get("narratives", []):
        toks = n.get("tokens", [])
        crypto_m = 0.0
        actions_m = 0.0
        mcap_avec = 0.0
        for t in toks:
            m = t.get("mcap_b") or 0
            if m <= 0:
                continue
            if t.get("is_stock"):
                r = t.get("_stock_revenue_m")
                if r and r > 3 * m * 1000:   # même garde anti reventes brutes
                    r = None
                if r and r > 0:
                    actions_m += r
                    mcap_avec += m
            else:
                r = t.get("rev_m_1y")
                if r and r > 0:
                    crypto_m += r
                    mcap_avec += m
        total_m = crypto_m + actions_m
        publie = n.get("rev_m_1y_total")

        if total_m > 0 and publie is None:
            echecs.ajouter("denominateur", n["narrative"],
                           f"revenu publié absent alors que le dénominateur du rapport "
                           f"vaut {total_m:.1f} M$")
        elif total_m <= 0 and publie:
            echecs.ajouter("denominateur", n["narrative"],
                           f"revenu publié {publie} M$ alors qu'aucun constituant "
                           f"n'apporte de dénominateur")
        elif publie is not None and abs(publie - total_m) > TOLERANCE_M:
            echecs.ajouter("denominateur", n["narrative"],
                           f"revenu publié {publie} M$ ≠ dénominateur du rapport "
                           f"{total_m:.1f} M$ (écart {publie - total_m:+.1f})")

        # Les deux moitiés doivent redonner le total : c'est ce qui garantit
        # qu'on n'a pas confondu frais on-chain et chiffre d'affaires coté.
        c_pub = n.get("rev_m_1y_crypto") or 0
        a_pub = n.get("rev_m_1y_actions") or 0
        if publie is not None and abs((c_pub + a_pub) - publie) > TOLERANCE_M:
            echecs.ajouter("denominateur", n["narrative"],
                           f"part crypto {c_pub} + part actions {a_pub} ≠ total {publie}")
        if abs(c_pub - crypto_m) > TOLERANCE_M:
            echecs.ajouter("denominateur", n["narrative"],
                           f"part crypto publiée {c_pub} ≠ recalculée {crypto_m:.1f} M$")
        if abs(a_pub - actions_m) > TOLERANCE_M:
            echecs.ajouter("denominateur", n["narrative"],
                           f"part actions publiée {a_pub} ≠ recalculée {actions_m:.1f} M$")

        # Le rapport lui-même doit se rejouer : numérateur / dénominateur.
        ps = n.get("ps_ttm")
        if ps and total_m > 0:
            attendu = mcap_avec * 1000 / total_m
            if abs(attendu - ps) > max(0.2, ps * 0.005):
                echecs.ajouter("denominateur", n["narrative"],
                               f"prix/frais publié {ps}× ≠ recalculé {attendu:.1f}×")


# ── 3. Un rapport « mesurable » ne dépasse pas son seuil d'absurdité ───────
def controler_statuts(cache, echecs):
    seuils = cache.get("seuils_absurdite") or SEUILS_PAR_DEFAUT
    for n in cache.get("narratives", []):
        for champ, cle_seuil in (("ps_ttm", "ps_ttm"), ("mc_tvl", "mc_tvl")):
            statut = n.get(f"{champ}_statut")
            motif = n.get(f"{champ}_motif")
            valeur = n.get(champ)
            seuil = seuils.get(cle_seuil) or SEUILS_PAR_DEFAUT[cle_seuil]
            if statut not in STATUTS_VALIDES:
                echecs.ajouter("statut", n["narrative"],
                               f"{champ}_statut = {statut!r}, attendu l'un de "
                               f"{sorted(STATUTS_VALIDES)}")
                continue
            if not motif:
                echecs.ajouter("statut", n["narrative"],
                               f"{champ}_motif absent : un statut sans motif ne "
                               f"s'audite pas")
            if statut == "mesurable":
                if valeur is None:
                    echecs.ajouter("statut", n["narrative"],
                                   f"{champ} déclaré mesurable mais aucune valeur")
                elif valeur > seuil:
                    echecs.ajouter("statut", n["narrative"],
                                   f"{champ} = {valeur}× déclaré « mesurable » alors "
                                   f"que le seuil d'absurdité est {seuil:.0f}×")
            # La valeur brute n'est JAMAIS supprimée : c'est la doctrine.
            if statut == "non_mesurable" and valeur is None and n.get(f"{champ}_n_tokens"):
                pass  # dénominateur inexistant : valeur légitimement absente


# ── 4. La nature du dénominateur correspond à sa composition ──────────────
def controler_nature_denominateur(cache, echecs):
    """Le multiple s'appelait « P/S » alors que sa part crypto est constituée de
    FRAIS. Le champ ps_ttm_denominateur doit dire lequel domine, et le dire
    juste, sinon on a seulement déplacé le mensonge d'un champ à l'autre."""
    for n in cache.get("narratives", []):
        nature = n.get("ps_ttm_denominateur")
        if nature not in DENOMINATEURS_VALIDES:
            echecs.ajouter("nature", n["narrative"],
                           f"ps_ttm_denominateur = {nature!r}, attendu l'un de "
                           f"{sorted(x for x in DENOMINATEURS_VALIDES if x)}")
            continue
        total = n.get("rev_m_1y_total") or 0
        crypto = n.get("rev_m_1y_crypto") or 0
        actions = n.get("rev_m_1y_actions") or 0
        if total <= 0:
            if nature is not None:
                echecs.ajouter("nature", n["narrative"],
                               f"dénominateur nul mais déclaré {nature!r}")
            continue
        if actions >= 0.95 * total:
            attendu = "chiffre d'affaires"
        elif crypto >= 0.95 * total:
            attendu = "frais"
        else:
            attendu = "mixte"
        if nature != attendu:
            echecs.ajouter("nature", n["narrative"],
                           f"ps_ttm_denominateur = {nature!r} alors que la composition "
                           f"(crypto {crypto} / actions {actions} sur {total}) dit "
                           f"{attendu!r}")


# ── 5. Le garde-fou d'offre, rejoué sur les trois cas réellement mesurés ───
def controler_garde_offre_unitaire(echecs):
    """Bug → test. Les trois cas ci-dessous ont été relevés le 05/09/2026 sur le
    cache servi en ligne. Ils passent par la fonction du collecteur elle-même :
    si quelqu'un l'affaiblit, ce contrôle tombe avant la publication."""
    try:
        from fetch_narratives_fundamentals import garantir_offre_coherente
    except Exception as e:
        echecs.ajouter("garde", "import",
                       f"impossible d'importer garantir_offre_coherente : {e}")
        return
    cas = [
        # (libellé, jeton tel que mesuré, ce qu'on exige en sortie)
        ("XMR mcap > fdv (1,0184)",
         {"symbol": "XMR", "id": "monero", "mcap_b": 9.942, "fdv_b": 9.762, "circ_pct": 100.0}),
        ("USTB offre 100,6 %",
         {"symbol": "USTB", "id": "ustb", "mcap_b": 0.837, "fdv_b": 0.825, "circ_pct": 100.6}),
        ("KAG offre 101,3 %",
         {"symbol": "KAG", "id": "kag", "mcap_b": 0.242, "fdv_b": 0.242, "circ_pct": 101.3}),
    ]
    for libelle, jeton in cas:
        journal = {"jetons_corriges": 0, "fdv_relevee": 0, "offre_ramenee": 0, "detail": []}
        sortie = garantir_offre_coherente(dict(jeton), journal)
        if (sortie.get("mcap_b") or 0) > (sortie.get("fdv_b") or 0):
            echecs.ajouter("garde", libelle,
                           "le garde-fou laisse passer capitalisation > valeur "
                           "pleinement diluée")
        if (sortie.get("circ_pct") or 0) > 100.0:
            echecs.ajouter("garde", libelle,
                           f"le garde-fou laisse passer une offre de {sortie['circ_pct']} %")
        if journal["jetons_corriges"] != 1:
            echecs.ajouter("garde", libelle,
                           f"correction non journalisée (jetons_corriges="
                           f"{journal['jetons_corriges']}, attendu 1)")
        if not sortie.get("_offre_corrigee"):
            echecs.ajouter("garde", libelle, "correction appliquée sans raison écrite")
    # Un jeton sain ne doit RIEN déclencher : un garde-fou qui corrige tout le
    # monde ne corrige plus personne.
    journal = {"jetons_corriges": 0, "fdv_relevee": 0, "offre_ramenee": 0, "detail": []}
    garantir_offre_coherente({"symbol": "XRP", "id": "ripple", "mcap_b": 88.423,
                              "fdv_b": 140.913, "circ_pct": 62.8}, journal)
    if journal["jetons_corriges"]:
        echecs.ajouter("garde", "XRP sain", "corrigé alors qu'il est cohérent")


def main():
    chemin = Path(sys.argv[1]) if len(sys.argv) > 1 else CACHE_DEFAUT
    if not chemin.exists():
        print(f"[échec] cache introuvable : {chemin}")
        return 1
    cache = charger(chemin)
    echecs = Echecs()

    print(f"cache      : {chemin}")
    print(f"écrit le   : {cache.get('updated')}")
    print(f"narratifs  : {cache.get('n_narratives')}"
          + ("   [BANC D'ESSAI]" if cache.get("banc_essai") else ""))
    seuils = cache.get("seuils_absurdite") or SEUILS_PAR_DEFAUT
    print(f"seuils     : prix/frais {seuils.get('ps_ttm')}× · "
          f"capitalisation/TVL {seuils.get('mc_tvl')}×")

    # Le contrôle d'offre s'applique à TOUS les caches, ancien ou neuf : la
    # capitalisation au-dessus de la valeur pleinement diluée est une
    # impossibilité, pas une question de version.
    n_jetons = controler_offre(cache, echecs)
    if not cache_anterieur_au_correctif(cache, echecs):
        controler_denominateur(cache, echecs)
        controler_statuts(cache, echecs)
        controler_nature_denominateur(cache, echecs)
    controler_garde_offre_unitaire(echecs)

    print(f"jetons vus : {n_jetons}")
    journal = cache.get("garde_offre") or {}
    if journal:
        print(f"garde offre: {journal.get('jetons_corriges', 0)} corrigés "
              f"({journal.get('fdv_relevee', 0)} valeurs pleinement diluées relevées, "
              f"{journal.get('offre_ramenee_a_100', 0)} offres ramenées à 100 %)")

    if not echecs:
        print("\n[OK] cohérence vérifiée : offre, dénominateur, statuts, nature "
              "du dénominateur, garde-fou.")
        return 0
    print(f"\n[ÉCHEC] {len(echecs)} incohérences :")
    for categorie, ou, message in echecs:
        print(f"  · [{categorie}] {ou} : {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
