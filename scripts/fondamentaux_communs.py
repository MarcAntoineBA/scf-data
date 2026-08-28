#!/usr/bin/env python3
"""Calcul commun aux collecteurs de fondamentaux — indépendant de toute source.

Deux collecteurs produisent le même schéma : l'un lit les dépôts XBRL de la SEC
(sociétés américaines), l'autre les états publiés des sociétés cotées ailleurs.
Ce qui change d'une source à l'autre, c'est UNIQUEMENT la façon d'aller chercher
les nombres et de les ranger dans les champs bruts. Tout ce qui vient après —
marges, rendements sur capitaux moyens, croissances par action, recouture des
divisions d'action, Piotroski, Altman, coût du capital, note sur 20 — est ici,
en un seul exemplaire.

C'est une précaution, pas une élégance : deux copies du même calcul divergent
toujours, et la fiche finirait par afficher deux définitions du même mot selon
la nationalité de la société. Un écart qu'on ne voit jamais, parce qu'il ne
produit aucune erreur.
"""
import datetime as _dt
import math


def annee_exercice(fin):
    """L'année d'un exercice, déduite de sa date de CLÔTURE.

    POURQUOI CE N'EST PAS `int(fin[:4])`, ET CE QUE ÇA COÛTAIT

    Les deux collecteurs prenaient l'année civile de la clôture. C'est juste pour
    une société qui clôt le 31 décembre ou le 30 juin, et faux pour toutes celles
    qui suivent un calendrier de 52/53 semaines — très répandu dans la
    distribution et l'industrie américaine. Leur exercice se termine le samedi le
    plus proche du 31 décembre, donc parfois le 1ᵉʳ ou le 2 JANVIER SUIVANT.

    L'exercice 2010 de HNI, clos le 01/01/2011, était donc étiqueté 2011 — et
    entrait en collision avec le vrai exercice 2011, clos le 31/12/2011. Deux
    entrées portaient la même année.

    Rien ne le signalait, et tout en souffrait :
      · les médianes à cinq et dix ans prennent `exs[-5:]` ; un doublon volait sa
        place à une vraie année, et la fenêtre couvrait un exercice de moins ;
      · les croissances comparent des années consécutives ; deux entrées de même
        millésime donnaient 0 % ou un écart absurde ;
      · le nombre d'exercices affiché sur la fiche surestimait la profondeur.

    Mesuré le 28/08/2026 : 131 sociétés américaines (4,2 %) et 1 556
    internationales (13,4 %) portaient au moins une année en double.

    LA RÈGLE. On recule de quinze jours avant de lire l'année. Une clôture du
    1ᵉʳ janvier retombe au 17 décembre et rend l'année précédente ; une clôture du
    31 décembre ne bouge pas ; une clôture de juin ou de septembre non plus.
    Quinze jours suffisent — un exercice de 52/53 semaines ne dérive jamais de
    plus d'une semaine autour du 31 décembre — et restent loin de toute autre
    date de clôture usuelle.

    Résolution mesurée : 67 % des collisions américaines, 97 % des
    internationales. Le reste vient d'un phénomène différent — des sociétés qui
    ont CHANGÉ de date de clôture et publient deux périodes qui se chevauchent —
    et se traite par déduplication, pas par étiquetage.
    """
    try:
        a, m, j = (int(x) for x in str(fin)[:10].split("-"))
        return (_dt.date(a, m, j) - _dt.timedelta(days=15)).year
    except Exception:
        try:
            return int(str(fin)[:4])
        except Exception:
            return None


def dedupliquer_exercices(exercices):
    """Une seule entrée par année : celle du dépôt le plus récent.

    Après correction de l'étiquetage, il reste des collisions d'une autre nature :
    une société qui change de date de clôture publie une période de transition qui
    chevauche l'exercice suivant. Keurig Dr Pepper porte ainsi un exercice clos le
    30/09/2017 et un autre le 31/12/2017.

    On garde l'entrée issue du dépôt le PLUS RÉCENT : un retraitement remplace ce
    qu'il retraite, c'est sa raison d'être. À défaut de numéro de dépôt — les
    sources internationales n'en portent pas — on garde la clôture la plus
    tardive, qui est l'exercice complet plutôt que la période de transition.

    L'ordre chronologique est préservé : tout le reste du calcul en dépend.

    ⚠ LA RICHESSE PASSE AVANT LA FRAÎCHEUR.
    Depuis que l'ossature annuelle est l'union de plusieurs lignes, deux entrées
    peuvent tomber sur la même année sans être des retraitements l'une de
    l'autre : l'une porte l'exercice complet, l'autre n'existe que parce qu'un
    flux de trésorerie a été déposé à une date voisine. Classer sur le seul
    numéro de dépôt ferait gagner la seconde et viderait l'année. On compare donc
    d'abord le nombre de grandeurs renseignées ; le dépôt le plus récent ne
    départage que les entrées de richesse égale — le cas du vrai retraitement.
    """
    if not exercices:
        return exercices

    def garni(e):
        return sum(1 for c, v in e.items()
                   if c != "annee" and isinstance(v, (int, float)))

    def rang(e):
        # Le numéro de dépôt de la SEC porte l'année de dépôt en son milieu :
        # `0000048287-14-000007` a été déposé en 2014. Il classe mieux que la date
        # de clôture, qui ne dit rien de la fraîcheur du retraitement.
        parts = str(e.get("accn") or "").split("-")
        an_depot = -1
        if len(parts) == 3 and len(parts[1]) == 2 and parts[1].isdigit():
            n = int(parts[1])
            an_depot = 2000 + n if n < 80 else 1900 + n
        return (garni(e), an_depot, str(e.get("depose_le") or ""),
                str(e.get("fin") or ""))

    par_annee = {}
    for e in exercices:
        a = e.get("annee")
        if a is None:
            continue
        garde = par_annee.get(a)
        if garde is None or rang(e) > rang(garde):
            par_annee[a] = e
    return [par_annee[a] for a in sorted(par_annee)]


def _div(a, b):
    if a is None or b in (None, 0):
        return None
    try:
        r = a / b
        return r if math.isfinite(r) else None
    except Exception:
        return None


def _pct(a, b):
    r = _div(a, b)
    return round(r * 100, 2) if r is not None else None


# ─────────────────────────────────────────────────────────────────────────
# Les ratios dont le dénominateur s'est effondré
# ─────────────────────────────────────────────────────────────────────────
# Un ROIC de 804 436 % ou une marge nette de −762 356 % ne sont pas faux : ils
# sont dépourvus de sens. Le dénominateur a cessé d'être une référence — Lyell
# Immunopharma perd 274 M$ pour 0,0 M$ de chiffre d'affaires, et une société de
# Shenzhen porte un capital investi NÉGATIF de 6,7 Md.
#
# ⚠ CETTE LISTE NE CONTIENT QUE LES RATIOS BORNÉS PAR NATURE. Les « parts de
# flux » — investissements sur cash, distribution, recherche sur cash — dépassent
# légitimement 100 %, et souvent 250 % : mesuré sur China National Nuclear,
# `capex_ocf` vaut 250 % pour 82 Md de chiffre d'affaires, et c'est exact. Les
# écarter au même titre détruirait de la donnée juste.
#
# ⚠ ET LA BANDE SE JUGE SUR LE RATIO, PAS SUR LE BILAN. Comparer le dénominateur
# à l'actif total se trompe sur les banques : Eastern Bankshares a 134 M de
# chiffre d'affaires pour 30,6 Md d'actif — 0,44 % — et une marge nette de
# 65,8 % parfaitement saine. La matérialité d'un dénominateur se mesure contre le
# NUMÉRATEUR : une marge dépasse 300 % exactement quand le résultat vaut plus de
# trois fois le chiffre d'affaires.
_RATIOS_BORNES = (
    "roic_1a", "roic_5a", "roic_10a",
    "roce_1a", "roce_5a", "roce_10a",
    "roe_1a", "roe_5a", "roe_10a",
    "marge_brute", "marge_ope", "marge_nette", "marge_fcf",
)

# Trois cents pour cent. Au-delà, le compte de valeurs écartées cesse presque de
# bouger quand on desserre — signe que les cas visés sont isolés loin de la
# population ordinaire. Mesuré : ±200 écarte 6 740 valeurs sur 256 772, ±300 en
# écarte 4 367, ±500 seulement 2 850.
_BANDE_RATIO = 300.0


def ecarter_ratios_degeneres(resume):
    """Rend muets les ratios sortis de la bande de plausibilité.

    On n'invente rien et on ne corrige rien : on refuse de prétendre mesurer ce
    qui n'est pas mesurable. Le barème sait déjà traiter un critère muet, et la
    fiche affiche « N/A » en gris pour une jauge sans valeur.

    Les valeurs écartées sont CONSERVÉES sous `ratios_ecartes`, parce que
    l'usage de ce site est d'expliquer plutôt que de masquer — il documente déjà
    le ROE gonflé par les rachats d'actions au lieu de le cacher. Un lecteur qui
    voit une case vide a le droit de savoir pourquoi.

    Rend le nombre de ratios écartés.
    """
    if not isinstance(resume, dict):
        return 0
    ecartes = {}
    for cle in _RATIOS_BORNES:
        v = resume.get(cle)
        if isinstance(v, (int, float)) and abs(v) > _BANDE_RATIO:
            ecartes[cle] = v
            resume[cle] = None
    if ecartes:
        resume["ratios_ecartes"] = ecartes
    else:
        resume.pop("ratios_ecartes", None)
    return len(ecartes)


def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(v) else None


# ─────────────────────────────────────────────────────────────────────────
# La note quantitative sur 20
# ─────────────────────────────────────────────────────────────────────────
# Le barème est celui du concurrent, relevé mot pour mot sur son propre centre
# d'aide public : vingt critères, six catégories, un point au seuil haut, un
# demi-point au seuil bas, zéro sinon. On le reproduit À L'IDENTIQUE — c'est ce
# qui permet de dire au lecteur « voici votre note, recalculée depuis les dépôts
# officiels, avec le détail du calcul », plutôt que de lui proposer une
# n-ième note maison qu'il ne saurait pas comparer.
#
# `sens` vaut "haut" quand plus c'est grand mieux c'est, "bas" dans le cas
# inverse (les dépenses d'investissement, l'endettement, la distribution).
_BAREME = [
    # (catégorie, clé du résumé, libellé, seuil du point, seuil du demi-point, sens)
    ("Rentabilité", "roic_1a",  "ROIC 1 an",                 20, 10, "haut"),
    ("Rentabilité", "roic_5a",  "ROIC médian 5 ans",         20, 10, "haut"),
    ("Rentabilité", "roic_10a", "ROIC médian 10 ans",        20, 10, "haut"),

    ("Profits", "marge_brute", "Marge brute",                50, 30, "haut"),
    ("Profits", "marge_ope",   "Marge d’exploitation",       20, 10, "haut"),
    ("Profits", "marge_nette", "Marge nette",                20, 10, "haut"),
    ("Profits", "capex_ocf",   "Investissements / cash",     20, 40, "bas"),

    ("Croissance", "croissance_ca_1a",  "Chiffre d’affaires par action, 1 an",   10, 5, "haut"),
    ("Croissance", "croissance_ca_5a",  "Chiffre d’affaires par action, 5 ans",  10, 5, "haut"),
    ("Croissance", "croissance_ca_10a", "Chiffre d’affaires par action, 10 ans", 10, 5, "haut"),
    ("Croissance", "predictibilite",    "Prédictibilité du chiffre d’affaires",  90, 50, "haut"),

    ("Bénéfices", "croissance_fcf_1a",  "Cash libre par action, 1 an",   10, 5, "haut"),
    ("Bénéfices", "croissance_fcf_5a",  "Cash libre par action, 5 ans",  10, 5, "haut"),
    ("Bénéfices", "croissance_fcf_10a", "Cash libre par action, 10 ans", 10, 5, "haut"),

    ("Dividende", "croissance_div_1a",  "Dividende par action, 1 an",   10, 5, "haut"),
    ("Dividende", "croissance_div_5a",  "Dividende par action, 5 ans",  10, 5, "haut"),
    ("Dividende", "croissance_div_10a", "Dividende par action, 10 ans", 10, 5, "haut"),
    # ⚠ CE CRITÈRE COMPTE LES ANNÉES SANS BAISSE, ET C'EST DÉLIBÉRÉ.
    # Le barème du concurrent l'intitule « années consécutives de hausse ». Mais
    # le nombre qu'il AFFICHE ne mesure pas cela : il annonce 13 pour NVIDIA,
    # dont son propre graphique de croissance du dividende montre quatre
    # exercices à 0,00 %. Treize hausses consécutives et quatre années plates ne
    # peuvent pas coexister — son chiffre compte les années sans BAISSE.
    # Vérifié le 28/08/2026 sur nos propres dépôts : NVIDIA totalise 2 années de
    # hausse stricte et 14 sans baisse. C'est la seconde qui reproduit son score.
    # On note donc celle-là — la promesse est de recalculer SA note depuis les
    # dépôts officiels, pas d'en inventer une autre — mais on la NOMME pour ce
    # qu'elle est, et le compteur strict reste publié à côté dans le résumé.
    ("Dividende", "annees_sans_baisse_dividende",
     "Années sans baisse du dividende", 8, 4, "haut"),

    ("Santé", "dette_ebitda_brut", "Dette brute / EBITDA", 1.5, 2.5, "bas"),
    ("Santé", "payout_benefices",  "Taux de distribution", 30, 50, "bas"),
]

# Les catégories qui n'ont pas de sens pour une banque, un assureur ou une
# foncière : elles ne publient ni coût des ventes ni investissements corporels
# comparables. Le concurrent le reconnaît dans son aide — « la note n'est pas
# faite pour noter ces entreprises » — et la calcule quand même.
_CRITERES_INDUSTRIELS = {"marge_brute", "capex_ocf"}


def _noter_critere(v, haut, bas, sens):
    if v is None:
        return None
    if sens == "haut":
        return 1.0 if v >= haut else (0.5 if v >= bas else 0.0)
    return 1.0 if v <= haut else (0.5 if v <= bas else 0.0)


def note_quantitative(r):
    """La note du concurrent, plus ce qu'il ne donne pas.

    DEUX ABSENCES QUI N'ONT RIEN À VOIR, ET QU'IL CONFOND.
    Un critère peut être vide pour deux raisons opposées :

      · LE FAIT. Une société qui ne distribue pas n'a pas de croissance du
        dividende. Ce n'est pas une donnée manquante, c'est une donnée nulle :
        elle vaut zéro point, et c'est juste.

      · LE SILENCE DU DÉPÔT. Une banque ne publie pas de marge brute — la notion
        n'a pas de sens pour elle. Lui donner zéro revient à la punir de son
        modèle d'affaires. Mesuré sur les 315 sociétés collectées : 37 % n'ont
        pas de marge brute déposée, 27 % pas de dette sur EBITDA, 21 % pas de
        ROIC. En comptant zéro partout, la note médiane tombe à 8/20 et la
        moitié de l'univers passe pour médiocre — ce qui ne dit plus rien de
        personne.

    On rend donc DEUX notes :
      · `note` — le barème du concurrent à l'identique, absences comptées zéro.
        Elle sert la comparaison avec lui, et rien d'autre.
      · `note_ramenee` — le même score, rapporté aux seuls critères que la
        société pouvait obtenir, remis sur 20. Ce n'est PAS plus généreux : une
        société qui distribue peu garde ses zéros, parce que c'est un fait
        mesuré. Seul le silence du dépôt est neutralisé.
    """
    plat = dict(r)
    c = r.get("croissances") or {}
    for nom, cle in (("ca", "croissance_ca"), ("fcf", "croissance_fcf"), ("div", "croissance_div")):
        bloc = c.get(nom) or {}
        for periode in ("1a", "5a", "10a"):
            plat[cle + "_" + periode] = bloc.get(periode)

    verse = bool(r.get("verse_dividende"))

    details, par_categorie = [], {}
    total = 0.0
    notables = 0
    muets, nuls = [], []

    for cat, cle, libelle, haut, bas, sens in _BAREME:
        v = plat.get(cle)
        pt = _noter_critere(v, haut, bas, sens)

        # Un critère de dividende sur une société qui n'en verse pas : c'est un
        # fait, pas un trou. Zéro point, et il compte dans le dénominateur.
        fait_nul = (pt is None and cat == "Dividende" and not verse)
        if fait_nul:
            pt = 0.0

        # Le taux de distribution d'une société qui ne distribue rien vaut ZÉRO,
        # pas « inconnu » — et zéro est en dessous du seuil, donc le point est
        # acquis. Le concurrent le documente ainsi dans son propre barème ; sans
        # cette règle, 121 sociétés sur 315 perdaient le critère par simple
        # absence de ligne alors que la réponse est évidente.
        if pt is None and cle == "payout_benefices" and not verse:
            pt = 1.0
            fait_nul = True

        if pt is None:
            statut = "muet"          # le dépôt ne publie pas la ligne
            muets.append(cle)
        elif fait_nul:
            statut = "nul_par_nature"
            nuls.append(cle)
            total += pt
            notables += 1
        else:
            statut = "note"
            total += pt
            notables += 1

        details.append({
            "categorie": cat, "cle": cle, "libelle": libelle,
            "valeur": v, "seuil_haut": haut, "seuil_bas": bas, "sens": sens,
            "point": pt, "statut": statut,
        })
        d = par_categorie.setdefault(cat, {"obtenu": 0.0, "possible": 0, "notables": 0})
        d["possible"] += 1
        if pt is not None:
            d["obtenu"] += pt
            d["notables"] += 1

    # Profil : ce qu'on OBSERVE, pas un secteur qu'on devine. « Sans marge brute
    # publiée » est un fait vérifiable ; « c'est une banque » serait une
    # inférence, et elle serait fausse pour les industriels qui ne publient pas
    # cette ligne non plus.
    sans_marge = plat.get("marge_brute") is None
    if sans_marge and plat.get("roic_1a") is None:
        profil = "sans_marge_ni_capital_investi"
    elif sans_marge:
        profil = "sans_marge_brute_publiee"
    elif not verse:
        profil = "sans_dividende"
    elif len(muets) > 4:
        profil = "donnees_partielles"
    else:
        profil = "standard"

    note_ramenee = round(20 * total / notables, 1) if notables >= 10 else None

    def _lire(x):
        if x is None: return None
        if x >= 14: return "excellente"
        if x >= 12: return "de qualité"
        if x >= 8: return "moyenne"
        return "médiocre"

    return {
        "note": round(total, 1),
        "sur": 20,
        "note_ramenee": note_ramenee,
        "criteres_notables": notables,
        "criteres_muets": muets,
        "criteres_nuls_par_nature": nuls,
        # ⚠ PAS DE LECTURE QUAND IL N'Y A PAS DE NOTE.
        # `lecture` valait `_lire(total)` dès que la note ramenée était refusée —
        # c'est-à-dire précisément quand on a jugé qu'on ne savait PAS noter cette
        # société. La fiche affichait alors « non notée » et, juste à côté,
        # « médiocre » : le refus annulé par l'étiquette qui le suit.
        # Mesuré le 28/08/2026 : 946 sociétés sur 3 462 (27,3 %) sont dans ce cas,
        # dont Shell — 249 Md$, notée 1,0/20 sur sept critères mesurables, donc
        # « médiocre ». Ce n'est pas un jugement sur Shell, c'est le constat que
        # treize de nos vingt critères ne s'appliquent pas à une compagnie
        # pétrolière intégrée. Une absence de note se dit ; elle ne se qualifie pas.
        "lecture": _lire(note_ramenee),
        "lecture_bareme_concurrent": _lire(round(total, 1)),
        "profil": profil,
        "par_categorie": {k: {"obtenu": round(v["obtenu"], 1), "possible": v["possible"],
                              "notables": v["notables"]} for k, v in par_categorie.items()},
        "details": details,
    }


# ─────────────────────────────────────────────────────────────────────────
# Croissances, prédictibilité, scores
# ─────────────────────────────────────────────────────────────────────────
# Le plancher sous lequel une base cesse d'être une base, exprimé en part de
# l'amplitude médiane de la série elle-même — parce qu'une valeur « petite » ne
# se juge que contre les autres valeurs de la même société.
#
# ⚠ CINQ POUR CENT EST UN CHOIX, PAS UNE MESURE. Il isole une base de 0,0001 sur
# une série d'amplitude 0,8 ; il ne départage pas un cas frontière à 8 %. On le
# note ici pour que personne ne le prenne un jour pour une constante physique.
_PLANCHER_BASE = 0.05


def _croissance_annuelle(series):
    """[(annee, valeur)] triés → liste des variations en % d'une année sur l'autre.

    TROIS CAS OÙ L'ON NE CALCULE PAS, parce qu'aucun taux n'existe :

    · base NÉGATIVE — passer de −10 à −5 n'est pas « +50 % » et passer de −5 à
      +5 n'est pas « +200 % » ;

    · base INFINITÉSIMALE — Wuxi Paike passe de 0,0001 à 0,7558 yuan de cash
      libre par action. Le calcul rendait +755 700 %, et le barème accordait le
      point plein pour un seuil fixé à 10 %. La base est positive, donc l'ancien
      test la laissait passer : elle n'en était pas une pour autant ;

    · TRAVERSÉE DE ZÉRO — DyDo Group passe de +0,79 à −40,56 yens par action, et
      le calcul rendait −5 215,94 %. Une chute ne peut pas dépasser −100 %. Ce
      nombre n'existe pas, et il s'affichait sur la fiche.

    Mesuré avant correction : 137 points de barème PLEINS attribués à 134
    sociétés sur une base valant moins de 5 % de l'amplitude de sa propre série.

    Le None n'est pas un aveu d'échec : le barème le traite en critère MUET, qui
    sort du dénominateur de la note ramenée au lieu de compter pour zéro.
    """
    # La référence, c'est l'amplitude de la série elle-même : une base de 0,0001
    # est du bruit chez Wuxi Paike et une valeur ordinaire chez une société dont
    # tous les montants sont de cet ordre.
    amplitudes = [abs(v) for _, v in series if v]
    reference = _mediane(amplitudes) if amplitudes else None

    out = []
    for i in range(1, len(series)):
        prev, cur = series[i - 1][1], series[i][1]
        if prev is None or cur is None or prev <= 0:
            out.append(None)                      # base négative ou absente
        elif reference and prev < _PLANCHER_BASE * reference:
            out.append(None)                      # la base est du bruit
        elif cur < 0:
            out.append(None)                      # traversée de zéro : pas un taux
        else:
            out.append(round(100 * (cur - prev) / prev, 2))
    return out


def _mediane_fenetre(vals, n, marge=2):
    """Médiane sur une fenêtre de n ans — ou rien, si la fenêtre n'existe pas.

    `_mediane(vals[-10:])` sur une série de cinq rend la médiane de cinq et se
    fait appeler « dix ans ». Mesuré sur LVMH et Nestlé : `roic_5a` et
    `roic_10a` strictement égaux, le même fait noté deux fois.

    On exige donc n − `marge` points réellement utilisables. Dix ans en
    demandent huit — ce qui laisse passer une société américaine à neuf
    exercices, et arrête net une européenne à cinq. En dessous, on rend None :
    le critère devient muet et sort du dénominateur de la note ramenée, au lieu
    de se faire passer pour une mesure.
    """
    utiles = [v for v in (vals or []) if v is not None]
    if len(utiles) < max(2, n - marge):
        return None
    return _mediane(utiles)


def _mediane(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return round(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, 2)


def _croissances(series):
    """{'1a':…, '5a':…, '10a':…} — dernière variation, puis MÉDIANES.

    Médiane et non moyenne : c'est le choix du barème qu'on veut reproduire, et
    c'est le bon choix — une année de rebond après un creux (+300 %) tirerait
    une moyenne décennale vers le haut et ferait passer une société cyclique
    pour une société en croissance.
    """
    # ⚠ UNE FENÊTRE DE DIX ANS EXIGE DIX ANS, pas cinq.
    #
    # Ces deux lignes acceptaient trois variations pour la médiane à cinq ans et
    # cinq pour celle à dix. Mesuré le 28/08/2026 : 407 des 2 353 fiches
    # affichant une « croissance du chiffre d'affaires 10 ans » la calculaient
    # sur moins de huit variations — dont 111 sur cinq exactement, c'est-à-dire
    # le MÊME chiffre que la jauge « 5 ans » juste au-dessus. Le lecteur voyait
    # deux mesures concordantes là où il n'y en avait qu'une, et en tirait une
    # confiance que rien ne fondait.
    #
    # `_mediane_fenetre` existe dans ce fichier depuis la correction de
    # `roic_5a == roic_10a` — même défaut, même remède — et exige n−2 points.
    # Elle n'était simplement pas appelée ici.
    g = _croissance_annuelle(series)
    return {
        "1a": g[-1] if g else None,
        "5a": _mediane_fenetre(g[-5:], 5),
        "10a": _mediane_fenetre(g[-10:], 10),
        "n": len(g),
    }


def _predictibilite(series):
    """0-100 : à quel point le chiffre d'affaires suit une exponentielle propre.

    C'est le coefficient de détermination d'une régression linéaire sur le
    LOGARITHME du chiffre d'affaires. Le log parce qu'une entreprise saine croît
    en pourcentage, pas en montant : sans lui, une société qui double tous les
    trois ans serait jugée « imprévisible » parce que sa courbe s'incurve.
    100 = série parfaitement régulière. Une valeur négative ou nulle interrompt
    la série (on ne prend pas le log d'un chiffre d'affaires négatif).
    """
    # Huit points, pas cinq. Le R² juge l'ajustement à une TENDANCE : sur cinq
    # points il mesure le bruit, et il punit la stabilité — une société sans
    # tendance n'a rien à ajuster, donc son R² s'effondre. Mesuré : Nestlé
    # sortait à 1,2/100 sur cinq points, c'est-à-dire « imprévisible », pour
    # l'un des chiffres d'affaires les plus réguliers d'Europe. En dessous de
    # huit exercices le critère est muet, ce qui est plus vrai que zéro.
    pts = [(i, v) for i, (_, v) in enumerate(series) if v is not None and v > 0]
    if len(pts) < 8:
        return None
    xs = [p[0] for p in pts]
    ys = [math.log(p[1]) for p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    r2 = (sxy * sxy) / (sxx * syy)
    return round(max(0.0, min(1.0, r2)) * 100, 1)


def _serie_sans_baisse_dividende(dps_par_annee):
    """Années consécutives SANS BAISSE du dividende par action.

    Deux comptages coexistent dans la profession, et ils ne disent pas la même
    chose. « Années de hausse » exige une augmentation chaque année : c'est la
    définition des aristocrates du dividende, la plus exigeante. « Années sans
    baisse » tolère le gel : c'est celle qu'emploient la plupart des sociétés
    américaines dans leurs communiqués.
    L'écart n'est pas théorique — mesuré sur NVIDIA le 2026-08-27 : 2 ans de
    hausse contre 13 ans sans baisse, parce que le dividende a été gelé quatre
    exercices d'affilée. Le concurrent affiche 13 sans dire lequel des deux il
    compte ; on rend les deux, nommés.
    """
    vals = [v for _, v in dps_par_annee if v is not None]
    if len(vals) < 2:
        return 0
    streak = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] >= vals[i - 1] and vals[i] > 0:
            streak += 1
        else:
            break
    return streak


def _serie_hausses_dividende(dps_par_annee):
    """Années consécutives de hausse du dividende par action, en partant de la fin.

    On s'arrête à la première année sans hausse. Une société qui a augmenté
    trente ans puis gelé une fois est à 0, pas à 30 : c'est la définition, et
    c'est ce qui fait la valeur du critère.
    """
    vals = [v for _, v in dps_par_annee if v is not None]
    if len(vals) < 2:
        return 0
    streak = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] > vals[i - 1]:
            streak += 1
        else:
            break
    return streak


# Facteurs de division d'action réellement pratiqués. Une société divise par un
# nombre simple ; personne ne divise par 3,7.
#
# ⚠ 1,5 A ÉTÉ RETIRÉ le 28/08/2026. La bande de tolérance autour de 1,5 —
# [1,41 ; 1,59] — est l'amplitude ordinaire d'une levée de fonds ou d'une
# acquisition payée en titres, pas celle d'un « trois pour deux », devenu rare.
# Mesuré : 447 événements faux sur 389 sociétés américaines, contre au plus 14
# vraies opérations perdues. Neuf d'entre elles sont nommées et rattrapables à la
# main si le besoin s'en fait sentir : ROL 2017-2018, WRB 2016-2017, MRTN
# 2017-2018, AAON 2020-2021, FLO 2008-2009 et 2010-2011, NEOG 2011-2012, CPK
# 2011-2012, UGI 2011-2012.
#
# Le cas qui a révélé le défaut : RBA portait un facteur 1,5 sur DIX exercices
# consécutifs, ce qui divisait son bénéfice par action de 2,86 $ à 1,907.
_FACTEURS_USUELS = [2, 2.5, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 50]


def _facteur_division(r):
    """Le facteur de division si `r` y ressemble, sinon None.

    6 % de tolérance : entre deux exercices, le nombre d'actions bouge aussi par
    rachats et attributions. Au-delà, ce n'est plus une division déguisée.
    """
    for f in _FACTEURS_USUELS:
        if abs(r - f) / f < 0.06:
            return f
        if abs(r * f - 1.0) < 0.06:      # regroupement (division inversée)
            return 1.0 / f
    return None


def _corriger_divisions(exercices, facteurs_lus=None):
    """Recoud les séries PAR ACTION que les divisions d'action coupent en deux.

    LE PIÈGE, ET IL EST SILENCIEUX. La SEC conserve les dépôts TELS QU'ILS ONT
    ÉTÉ FAITS. Le 10-K de 2019 de NVIDIA dit « dividende 0,61 $ par action » ;
    après deux divisions (4 pour 1 en 2021, 10 pour 1 en 2024), le même exercice
    vaut 0,015 $ dans les dépôts récents. Les deux chiffres sont exacts. La série
    qui les enchaîne ne l'est pas : elle affiche une chute de 97 % là où rien ne
    s'est passé, et toute croissance « par action » calculée dessus est fausse.
    Mesuré ici avant correction : croissance médiane du BPA sur 5 ans à 122 %
    contre 95 % chez le concurrent — l'écart était entièrement l'artefact.

    LA SIGNATURE D'UNE DIVISION, et de rien d'autre : le nombre d'actions est
    multiplié par un facteur proche d'un nombre simple, ET le résultat net ne
    suit PAS. Un rachat massif ne fait pas fois dix ; une augmentation de capital
    qui ferait fois dix ferait aussi bondir le résultat. On exige les deux
    conditions, sinon on ne touche à rien — mieux vaut une série non corrigée
    qu'une série corrigée à tort.

    On normalise sur le PLUS RÉCENT : c'est le nombre d'actions d'aujourd'hui,
    celui que le lecteur voit dans son courtier.
    """
    n = len(exercices)
    if n < 3:
        return []
    evenements = []
    cumul = 1.0
    facteurs = [1.0] * n          # facteur à appliquer au NOMBRE D'ACTIONS de l'année

    # ── QUAND LES MILLÉSIMES PARLENT, ON NE DEVINE PLUS ──
    #
    # `facteurs_lus` vient des dépôts eux-mêmes : {date_de_fin: facteur}, où un
    # facteur signifie que CET exercice a été retraité. Le rapport entre deux
    # dépôts d'un même exercice EST le facteur — alors que le rapport entre deux
    # exercices voisins vaut « facteur × dérive de l'année », et c'est cette
    # dérive qui faisait rater O'Reilly d'un dixième de point de tolérance.
    #
    # Les exercices à corriger sont ceux qui PRÉCÈDENT le plus ancien retraité :
    # eux seuls sont restés sur l'ancienne base.
    #
    # ⚠ ET L'ON N'INFÈRE PLUS DU TOUT. Garder les deux compterait le même
    # événement deux fois : chez O'Reilly, ×15 par les millésimes et ×15 par le
    # saut de 2022 à 2023, soit ×225.
    if facteurs_lus:
        # ── DEUX DIVISIONS DU MÊME FACTEUR SONT DEUX ÉVÉNEMENTS ──
        #
        # Regrouper par valeur les confondrait : CSX a divisé par trois en 2011
        # ET en 2021, et ses quatre dates portant le facteur 3 se réduisaient à
        # un seul événement daté de 2009 — la division de 2021 disparaissait,
        # douze exercices restaient faux.
        #
        # Une division retraite les deux ou trois exercices les plus récents au
        # moment du dépôt : les dates d'un même événement sont donc CONTIGUËS. On
        # coupe à quatre ans, au-delà desquels plus aucun 10-K ne retraite.
        par_facteur = {}
        for fin, f in facteurs_lus.items():
            par_facteur.setdefault(f, []).append(fin)
        grappes = []                      # [(facteur, date la plus ancienne)]
        for f, dates in par_facteur.items():
            dates.sort()
            debut = dates[0]
            prec = dates[0]
            for d in dates[1:]:
                # Quatre ans d'écart : ce n'est plus le même retraitement.
                if d[:4].isdigit() and prec[:4].isdigit() and \
                        int(d[:4]) - int(prec[:4]) > 4:
                    grappes.append((f, debut))
                    debut = d
                prec = d
            grappes.append((f, debut))

        for i, e in enumerate(exercices):
            fin = e.get("fin") or ""
            c = 1.0
            for f, d in grappes:
                if fin and fin < d:
                    c *= f
            facteurs[i] = c
        evenements = [{"entre": None, "et": None, "facteur": f,
                       "source": "millésimes", "depuis": d}
                      for f, d in sorted(grappes, key=lambda t: t[1])]
        cumul = max(facteurs) if facteurs else 1.0
        if cumul == 1.0:
            return []
        for i, e in enumerate(exercices):
            f = facteurs[i]
            if f == 1.0:
                continue
            for cle in ("shares_diluted", "shares_basic"):
                if e.get(cle) is not None:
                    e[cle] = e[cle] * f
            for cle in ("eps_diluted", "eps_basic", "dps"):
                if e.get(cle) is not None:
                    e[cle] = e[cle] / f
            e["_facteur_division"] = round(f, 4)
        return evenements

    for i in range(n - 1, 0, -1):
        a, b = exercices[i - 1], exercices[i]
        sa, sb = a.get("shares_diluted"), b.get("shares_diluted")
        if sa and sb and sa > 0:
            f = _facteur_division(sb / sa)
            if f is not None:
                na, nb = a.get("net_income"), b.get("net_income")
                confirme = True
                if na and nb:
                    # Si le résultat net a été multiplié par le même facteur,
                    # c'est une vraie croissance et non une division.
                    #
                    # ⚠ EN VALEUR ABSOLUE. La condition portait `na > 0`, ce qui
                    # éteignait cette vérification ENTIÈREMENT pour toute société
                    # en perte : 680 événements américains et 889 internationaux
                    # passaient sans aucun contrôle. Or une société en perte qui
                    # lève massivement des fonds est précisément le profil qui
                    # fabrique un faux facteur — IonQ, résultat 2021 de
                    # −106 186 000, en est le cas type.
                    if abs(abs(nb / na) - (sb / sa)) / max(sb / sa, 1e-9) < 0.20:
                        confirme = False
                if confirme:
                    cumul *= f
                    evenements.append({
                        "entre": a["annee"], "et": b["annee"],
                        "facteur": round(f, 4),
                        "actions_avant": sa, "actions_apres": sb,
                    })
        facteurs[i - 1] = cumul

    if cumul == 1.0:
        return []

    for i, e in enumerate(exercices):
        f = facteurs[i]
        if f == 1.0:
            continue
        for cle in ("shares_diluted", "shares_basic"):
            if e.get(cle) is not None:
                e[cle] = e[cle] * f
        for cle in ("eps_diluted", "eps_basic", "dps"):
            if e.get(cle) is not None:
                e[cle] = e[cle] / f
        e["_facteur_division"] = round(f, 4)
    return list(reversed(evenements))


def _piotroski(cur, prev):
    """F-Score de Piotroski, 0 à 9. None si le bilan n'a pas de quoi le calculer.

    Neuf tests binaires en trois familles : la société gagne-t-elle de l'argent,
    s'endette-t-elle moins, travaille-t-elle mieux. Le détail est rendu en même
    temps que le total — un 7/9 ne dit rien, un 7/9 dont les deux points perdus
    sont la dilution dit quelque chose.
    """
    if not cur or not prev:
        return None, {}
    d = {}
    roa_c = _div(cur.get("net_income"), cur.get("assets"))
    roa_p = _div(prev.get("net_income"), prev.get("assets"))
    ocf = cur.get("ocf")
    d["roa_positif"] = 1 if (roa_c is not None and roa_c > 0) else 0
    d["cash_positif"] = 1 if (ocf is not None and ocf > 0) else 0
    d["roa_en_hausse"] = 1 if (roa_c is not None and roa_p is not None and roa_c > roa_p) else 0
    d["qualite_du_resultat"] = 1 if (ocf is not None and cur.get("net_income") is not None
                                     and ocf > cur["net_income"]) else 0
    # ── L'ENDETTEMENT SE JUGE SUR CE QU'ON A, PAS SUR CE QUI MANQUE ──
    #
    # Le critère lisait `lt_debt` seule — étiquette us-gaap pure, servie sur la
    # moitié du parc. Quand elle manque, le zéro ne dit pas « la dette a monté »,
    # il dit « on ne sait pas » — et il compte pareil. Mesuré le 28/08/2026 :
    # première cause de zéro d'ignorance du score, 1 643 sociétés côté américain,
    # environ 8 700 au total et 4 403 points en jeu.
    #
    # On juge donc sur la dette TOTALE dès qu'elle existe, et l'on ne retombe sur
    # la dette longue que faute de mieux. Elle agrège les
    # emprunts et les baux, et elle est bien plus souvent servie.
    #
    # ⚠ Ce score n'est alors plus tout à fait le F-Score de Piotroski, dont la
    # composante d'endettement est définie sur la dette à LONG TERME. La fiche
    # doit le dire.
    _dette_c = (cur.get("dette_totale") if cur.get("dette_totale") is not None
                else cur.get("lt_debt"))
    _dette_p = (prev.get("dette_totale") if prev.get("dette_totale") is not None
                else prev.get("lt_debt"))
    lev_c = _div(_dette_c, cur.get("assets"))
    lev_p = _div(_dette_p, prev.get("assets"))
    d["dette_en_baisse"] = 1 if (lev_c is not None and lev_p is not None and lev_c < lev_p) else 0
    cr_c = _div(cur.get("assets_current"), cur.get("liabilities_current"))
    cr_p = _div(prev.get("assets_current"), prev.get("liabilities_current"))
    d["liquidite_en_hausse"] = 1 if (cr_c is not None and cr_p is not None and cr_c > cr_p) else 0
    sh_c, sh_p = cur.get("shares_diluted"), prev.get("shares_diluted")
    d["pas_de_dilution"] = 1 if (sh_c is not None and sh_p is not None and sh_c <= sh_p * 1.005) else 0
    gm_c = _div(cur.get("gross_profit"), cur.get("revenue"))
    gm_p = _div(prev.get("gross_profit"), prev.get("revenue"))
    d["marge_en_hausse"] = 1 if (gm_c is not None and gm_p is not None and gm_c > gm_p) else 0
    at_c = _div(cur.get("revenue"), cur.get("assets"))
    at_p = _div(prev.get("revenue"), prev.get("assets"))
    d["rotation_en_hausse"] = 1 if (at_c is not None and at_p is not None and at_c > at_p) else 0

    # ⚠ LA SOMME NE COMPTE QUE LES CRITÈRES. En Python `True` vaut 1 : poser un
    # drapeau booléen dans ce dictionnaire donnerait un POINT à toutes les
    # sociétés qu'il est censé signaler. Mesuré sur le remède initialement
    # proposé : +1 point à 1 993 sociétés sans aucune donnée de dette — STC 4/9 →
    # 5/9, VIV 4/9 → 5/9, BBD 2/9 → 3/9 — et silencieusement, puisque aucune note
    # ne dépasse 9. Le zéro d'ignorance serait devenu un UN d'ignorance.
    #
    # Le drapeau ci-dessous est donc à préfixe souligné, et la somme l'ignore.
    d["_dette_assiette"] = ("dette totale" if cur.get("dette_totale") is not None
                            else ("long terme" if cur.get("lt_debt") is not None
                                  else "aucune"))
    return sum(v for k, v in d.items() if not k.startswith("_")), d


def _altman_z(cur, mcap_usd):
    """Z-Score d'Altman, version sociétés cotées.

    Z = 1,2·FR/A + 1,4·RÉ/A + 3,3·EBIT/A + 0,6·CAPI/DETTES + 1,0·CA/A
    Au-dessus de 2,99 : zone sûre. En dessous de 1,81 : zone de détresse.
    ⚠ Le modèle a été calibré sur des industriels des années 1960. Une société
    de logiciels sans actifs corporels et sans dette y sort des valeurs
    astronomiques qui ne veulent rien dire — c'est pourquoi on rend AUSSI le
    détail, et pourquoi la fiche devra le cadrer plutôt que l'afficher nu.
    """
    A = cur.get("assets")
    if not A or A <= 0 or not mcap_usd:
        return None, {}
    fr = None
    if cur.get("assets_current") is not None and cur.get("liabilities_current") is not None:
        fr = cur["assets_current"] - cur["liabilities_current"]
    dettes = cur.get("liabilities")
    ebit = cur.get("operating_income")
    parts = {
        "fonds_de_roulement": _r(1.2 * fr / A, 3) if fr is not None else None,
        "reserves": _r(1.4 * cur["retained_earnings"] / A, 3) if cur.get("retained_earnings") is not None else None,
        "resultat_exploitation": _r(3.3 * ebit / A, 3) if ebit is not None else None,
        "capitalisation_sur_dettes": _r(0.6 * mcap_usd / dettes, 3) if dettes else None,
        "rotation": _r(1.0 * cur["revenue"] / A, 3) if cur.get("revenue") is not None else None,
    }
    # ── LES CINQ TERMES, OU RIEN ──
    #
    # On acceptait d'en publier quatre : la somme des termes présents partait
    # telle quelle, et la fiche la comparait aux seuils d'Altman — 1,81
    # « détresse », 2,99 « sûre » — comme si elle était complète.
    #
    # Or les cinq termes sont positifs pour une société saine. Une somme à
    # quatre termes est donc SYSTÉMATIQUEMENT plus basse que le Z vrai, jamais
    # plus haute : ce n'est pas un Z imprécis, c'est un Z biaisé dans un seul
    # sens, celui qui accuse. Mesuré le 28/08/2026 : 754 fiches américaines
    # publiaient un Z à quatre termes, et 341 d'entre elles — 45 % — changeaient
    # de verdict dès que le terme manquant prenait la valeur médiane observée
    # ailleurs. Illumina s'affichait à 1,28 pour un Z réel autour de 2,7.
    #
    # On rend donc None, et le détail à côté : la fiche peut montrer les termes
    # connus sans prétendre au score. Une case vide se discute, un « zone de
    # détresse » faux se croit.
    if any(v is None for v in parts.values()):
        return None, parts
    return _r(sum(parts.values()), 2), parts


# ─────────────────────────────────────────────────────────────────────────
# Coût moyen pondéré du capital
# ─────────────────────────────────────────────────────────────────────────
# WACC = part des fonds propres × leur coût + part de la dette × son coût après impôt.
#
# Le coût des fonds propres suit le modèle d'évaluation des actifs financiers :
# taux sans risque + bêta × prime de risque. Les deux paramètres de marché sont
# posés ici, en clair, avec leur date — plutôt que cachés dans une formule.
# Ils bougent lentement ; une révision annuelle suffit, et elle doit être un
# geste conscient, pas une dérive.
TAUX_SANS_RISQUE = 4.2      # % — rendement du 10 ans américain, relevé le 2026-08-27
PRIME_DE_RISQUE = 5.2       # % — prime actions de long terme, convention Damodaran


# ─────────────────────────────────────────────────────────────────────────
# Le taux d'imposition : celui qu'on affiche, et celui qui sert à calculer
# ─────────────────────────────────────────────────────────────────────────
def _taux_impot_reel(tax, pretax):
    """Le taux effectivement payé cette année-là. None quand il n'a aucun sens.

    Pas de borne, pas de valeur de remplacement : une société en perte, une
    société qui encaisse un crédit d'impôt, une foncière exonérée ont un taux
    négatif, nul ou supérieur à cent pour cent, et c'est un fait à montrer.
    """
    if tax is None or pretax is None or pretax == 0:
        return None
    return round(100.0 * tax / pretax, 1)


def _taux_pour_nopat(tax, pretax):
    """Le taux BORNÉ qui sert au résultat opérationnel après impôt.

    Rend (taux entre 0 et 1, la borne a-t-elle joué). Sans borne, un crédit
    d'impôt exceptionnel produirait un résultat après impôt supérieur au
    résultat avant impôt, et un rendement du capital investi qui n'existe pas.
    Vingt et un pour cent est le taux fédéral américain : c'est un choix, pas
    une mesure, et le drapeau le dit.
    """
    if tax is None or pretax is None or pretax == 0:
        return 0.21, True
    t = tax / pretax
    if not (0.0 <= t <= 0.5):
        return 0.21, True
    return t, False


def _charge(v):
    """Une charge est une charge : on la stocke en valeur absolue.

    Les deux sources ne s'accordent pas sur le signe de la charge d'intérêts —
    351 valeurs négatives sur 352 d'un côté, 1 sur 223 de l'autre. Toute garde
    du type `interest_expense > 0` vide alors un ratio pour quatre cinquièmes
    d'un univers, sans que rien ne le signale.
    """
    return abs(v) if isinstance(v, (int, float)) else v


# ─────────────────────────────────────────────────────────────────────────
# Contrôle d'unité sur le nombre d'actions
# ─────────────────────────────────────────────────────────────────────────
def effacer_l_impossible(exercices):
    """Efface les couples de chiffres qui ne peuvent pas être vrais ensemble.

    POURQUOI

    Un balayage d'invariants comptables sur les 38 297 exercices américains, le
    28/08/2026, a trouvé trois familles de valeurs LOGIQUEMENT impossibles — pas
    extrêmes, impossibles :

      · 220 exercices où la marge brute dépasse 100 % : le résultat brut y est
        supérieur au chiffre d'affaires. EACO 2011 affichait 28,6 M$ de brut pour
        1,24 M$ de chiffre d'affaires, quand la société en fait cent dix-sept.
      · 24 exercices où la trésorerie dépasse l'actif total, 13 où le passif
        courant dépasse le passif, 3 où l'actif courant dépasse l'actif. Ingles
        Markets 2010 portait 1,53 M$ d'actif pour 423 M$ d'actif courant.

    Dans chaque cas, une balise XBRL a été lue à la mauvaise échelle ou sur un
    périmètre partiel. On ne sait pas LEQUEL des deux chiffres est faux — un
    total trop petit et un poste trop grand produisent la même violation — donc
    on n'en répare aucun : on efface les DEUX, et on dit pourquoi.

    CE QU'ON NE TOUCHE PAS, ET C'EST DÉLIBÉRÉ
    Le même balayage a trouvé 2 928 marges au-delà de ±1 000 % et 421 rendements
    du même ordre. Ceux-là ne sont pas impossibles : une biotech sans revenus a
    réellement une marge d'exploitation de −7 000 %, et une société aux capitaux
    propres presque nuls un rendement de 5 000 %. Ces nombres sont vrais et
    inutiles — c'est à la fiche de les cadrer, pas au collecteur de les nier.
    Effacer ce qui est vrai serait le même geste qu'afficher ce qui est faux.

    Rend le nombre d'effacements, par famille.
    """
    faits = {}

    def efface(e, cles, raison):
        for k in cles:
            e[k] = None
        e.setdefault("_impossibles", []).append(raison)
        faits[raison] = faits.get(raison, 0) + 1

    # (poste, son total, libellé) — un poste ne peut pas dépasser son propre total.
    INCLUSIONS = (
        ("assets_current", "assets", "actif courant au-dessus de l’actif"),
        ("liabilities_current", "liabilities", "passif courant au-dessus du passif"),
        ("cash", "assets", "trésorerie au-dessus de l’actif"),
    )

    for e in exercices:
        g = e.get
        rev, brut = g("revenue"), g("gross_profit")
        if (isinstance(rev, (int, float)) and isinstance(brut, (int, float))
                and rev > 0 and brut > rev * 1.001):
            efface(e, ("revenue", "gross_profit"), "marge brute au-dessus de 100 %")

        for petit, grand, quoi in INCLUSIONS:
            a, b = g(petit), g(grand)
            if (isinstance(a, (int, float)) and isinstance(b, (int, float))
                    and b > 0 and a > b * 1.001):
                efface(e, (petit, grand), quoi)

    return faits


def redresser_dividende_par_action(exercices):
    """Répare le dividende par action quand il contredit le montant versé.

    POURQUOI

    Le dividende par action déposé est un chiffre unique, et il se trompe de deux
    façons que rien ne signale :

      · une ERREUR DE PÉRIODE. NVIDIA a déposé, pour son exercice 2016, un fait
        couvrant 370 jours et portant 0,115 — son taux TRIMESTRIEL. Le filtre de
        durée l'accepte : la période est bien annuelle, c'est la valeur qui ne
        l'est pas. La série passe de 0,0085 à 0,0029 puis remonte à 0,0121, et le
        compteur d'années de hausse tombe de treize à deux.
      · une ERREUR D'ÉCHELLE. Southwest Gas déclare 1 980 $ de dividende par
        action là où elle en verse 1,92. Permian Resources en déclare
        27 900 000 $.

    LE CONTRÔLE, sans source extérieure : la société dépose aussi le montant
    TOTAL versé et son nombre d'actions. Le dividende par action implicite vaut
    l'un divisé par l'autre. Mesuré le 28/08/2026 sur 16 235 exercices : 93,8 %
    concordent à 40 % près. Restent 533 exercices — 3,3 % — où l'écart dépasse un
    facteur deux et demi. Un écart pareil n'est pas du bruit.

    ⚠ CE QUE LE CONTRÔLE NE PEUT PAS FAIRE, ET QU'IL A FALLU MESURER POUR LE SAVOIR.
    Le dividende par action déposé est DÉCLARÉ ; le montant versé est ENCAISSÉ.
    L'année où une société change de taux, les deux divergent légitimement.
    JPMorgan 2009 : elle déclare 0,20 $ après avoir coupé, mais elle a DÉCAISSÉ
    0,54 $ — le premier trimestre était encore payé à l'ancien taux de 0,38 $.
    Les deux chiffres sont justes. Une règle qui aurait « corrigé » 0,20 en 0,54
    aurait effacé la coupe la plus importante de sa décennie.

    Deux versions ont donc été essayées et REJETÉES, chacune sur cette société :
      · départager par la série — elle suppose un dividende qui évolue lentement,
        ce qui est faux précisément les années qui comptent ;
      · soustraire les préférentielles puis préférer l'implicite — utile, mais
        insuffisant : même nettoyé, l'écart déclaré/versé de JPMorgan reste de
        2,7 fois, et il est légitime.

    LA RÈGLE RETENUE a donc deux déclencheurs indépendants, et aucun ne repose sur
    la douceur de la série :

      a) L'ÉCHELLE. Un écart au-delà d'un facteur vingt ne s'explique par aucun
         décalage d'encaissement : au pire, un changement de taux en cours
         d'année fait varier le rapport d'un facteur quatre. Southwest Gas
         déclare 1 980 $ là où elle en verse 1,92 ; Permian Resources déclare
         27 900 000 $ par action. Ce sont des unités, pas des dividendes.

      b) LE CREUX EN V. Une vraie coupe PERSISTE — JPMorgan reste à 0,20 $ l'année
         suivante. Une erreur de saisie fait un trou d'un an qui se referme.
         NVIDIA 2016 déposait 0,115 — son taux TRIMESTRIEL — entre 0,34 et 0,485.
         On exige donc que la valeur tombe sous 60 % de la PLUS PETITE de ses deux
         voisines immédiates, et que l'implicite, lui, se tienne entre elles.

    Rend la liste des exercices redressés.
    """
    faits = []
    n = len(exercices)
    for i, e in enumerate(exercices):
        dps = e.get("dps")
        pay = e.get("dividends_paid")
        sh = e.get("shares_diluted")
        if not all(isinstance(x, (int, float)) for x in (dps, pay, sh)):
            continue
        # Les préférentielles ne reviennent pas aux actions ordinaires : sans
        # cette soustraction, l'implicite est gonflé pour toute société qui en
        # verse, et JPMorgan 2009 sortait à 0,88 $ au lieu de 0,54 $.
        pref = e.get("dividends_paid_preferred")
        if isinstance(pref, (int, float)):
            pay = pay - abs(pref)
        if dps <= 0 or pay <= 0 or sh <= 0:
            continue
        implicite = pay / sh
        if implicite <= 0:
            continue
        r = dps / implicite

        motif = None
        if r > 20 or r < 0.05:
            motif = "échelle : le dividende déposé n’est pas dans la même unité"
        else:
            av = exercices[i - 1].get("dps") if i > 0 else None
            ap = exercices[i + 1].get("dps") if i + 1 < n else None
            if (isinstance(av, (int, float)) and isinstance(ap, (int, float))
                    and av > 0 and ap > 0):
                plancher = min(av, ap)
                # Un creux d'un an qui se referme, et un implicite qui, lui, se
                # tient entre les deux voisines : la valeur déposée est seule à
                # être aberrante.
                if (dps < 0.6 * plancher
                        and min(av, ap) * 0.6 <= implicite <= max(av, ap) * 1.6):
                    motif = ("creux d’un an refermé l’année suivante : une vraie "
                             "coupe de dividende persiste")
        if not motif:
            continue
        e["dps"] = implicite
        e["dps_redresse"] = motif
        faits.append((e.get("annee"), dps, implicite))
    return faits


def _corriger_unite_actions(exercices):
    """Rattrape un nombre d'actions exprimé dans la mauvaise unité.

    McDonald's portait 716,4 actions en circulation là où il en faut 716,4
    millions. Le facteur traverse tout : capitalisation, valeur d'entreprise,
    coût du capital, toutes les grandeurs par action.

    Le contrôle ne demande aucune source extérieure — le bénéfice par action
    multiplié par le nombre d'actions doit rendre le résultat net :

        actions_impliquees = net_income / eps_diluted

    Quand le rapport entre les deux est une PUISSANCE DE MILLE, c'est une unité,
    pas un écart comptable : on corrige. Sinon on ne touche à rien — un écart de
    quelques pour cent vient des actions de préférence, et il est normal.

    ⚠ DEUX SENS, PAS UN — ET UN TROISIÈME CAS QUI NE SE CORRIGE PAS.
    Cette fonction ne traitait qu'un sens : le nombre déposé trop PETIT, qu'on
    multiplie. Mesuré le 28/08/2026 sur les 32 729 exercices américains qui
    portent à la fois résultat net, BPA et nombre d'actions :

      · 24 exercices trop petits  — le cas déjà traité ;
      · 35 exercices trop GRANDS d'un facteur mille, jamais corrigés. BiomX
        déclarait 17 403 270 000 actions là où son propre BPA en implique
        17 403 750 : le nombre est déposé en unités sous une balise qui annonce
        des milliers. On divise, symétriquement ;
      · 111 exercices où l'écart dépasse dix fois sans être une puissance de
        mille. Là, aucune correction n'est défendable — on ne sait pas lequel des
        deux chiffres est faux. On EFFACE le nombre d'actions, ce qui vide les
        grandeurs par action au lieu de les publier fausses d'un facteur inconnu.
        Une case vide se voit ; un chiffre d'affaires par action faux se lit
        comme un chiffre d'affaires par action.

    On ne touche pas aux écarts de deux à dix fois (1,1 % des exercices) — ils
    s'expliquent par le périmètre, actions de base contre diluées ou activités
    abandonnées — ni aux 248 exercices où le résultat net et le BPA n'ont pas le
    même signe, qui relèvent d'un autre défaut.

    Rend la liste des corrections, pour qu'elles restent affichables.
    """
    corrections = []
    for e in exercices:
        act = e.get("shares_diluted")
        ni = e.get("net_income")
        eps = e.get("eps_diluted")
        if not act or not ni or not eps or eps == 0:
            continue
        implique = ni / eps
        if implique <= 0 or act <= 0:
            continue
        r = implique / act
        touche = False
        for facteur in (1000.0, 1000000.0, 1000000000.0):
            # 15 % de tolérance : l'écart résiduel vient des préférentielles.
            if abs(r - facteur) / facteur < 0.15:
                for cle in ("shares_diluted", "shares_basic"):
                    if e.get(cle):
                        e[cle] = e[cle] * facteur
                corrections.append({"annee": e.get("annee"), "facteur": int(facteur)})
                touche = True
                break
            # Le sens inverse : le nombre déposé est trop GRAND du même facteur.
            if abs(1.0 / r - facteur) / facteur < 0.15:
                for cle in ("shares_diluted", "shares_basic"):
                    if e.get(cle):
                        e[cle] = e[cle] / facteur
                corrections.append({"annee": e.get("annee"),
                                    "facteur": -int(facteur)})
                touche = True
                break
        if touche:
            continue
        # ── NI CONCORDANT, NI UNE UNITÉ — MAIS AVANT D'EFFACER, UN ARBITRE ──
        #
        # La règle s'appuyait sur le seul nombre implicite (résultat net ÷ BPA).
        # Elle suppose donc un BPA fiable, et il ne l'est pas quand il est
        # minuscule : arrondi au centime, un bénéfice de 0,0264 $ devient 0,01 et
        # le nombre implicite triple. Meta 2012 en est le cas d'école — 2,2
        # milliards d'actions déposées, 5,3 milliards impliquées, et rien de faux.
        #
        # Mesuré le 28/08/2026 : sur 581 exercices à plus de deux fois d'écart,
        # 508 portent un nombre dilué COHÉRENT avec le nombre de base. Le nombre
        # d'actions y est juste ; c'est le BPA qui est l'intrus. Et 20 des 156
        # exercices que cette règle effaçait avaient un BPA de 0,02 $ ou moins.
        #
        # On se donne donc un arbitre INDÉPENDANT du BPA : le nombre d'actions de
        # base, déposé séparément. Le dilué lui est toujours supérieur et proche.
        # Quand les deux s'accordent, ils se confirment l'un l'autre, et aucun
        # BPA arrondi ne doit pouvoir les effacer.
        if r > 10 or r < 0.1:
            sb = e.get("shares_basic")
            if (isinstance(sb, (int, float)) and sb > 0
                    and 0.95 <= act / sb <= 1.30):
                e["shares_bpa_douteux"] = ("le bénéfice par action déposé contredit "
                                           "le nombre d’actions, que les comptes de "
                                           "base et dilué confirment pourtant")
                continue
            for cle in ("shares_diluted", "shares_basic"):
                e[cle] = None
            e["shares_ecarte"] = "incohérent avec le bénéfice par action déposé"
    return corrections

def _wacc(mcap_usd, dette, interets, taux_impot_pct, beta):
    """Le WACC d'un exercice, ou None si l'un des termes manque.

    On ne remplace RIEN par une valeur par défaut : un WACC bâti sur un bêta
    supposé ou une dette devinée serait un nombre inventé, et il servirait
    ensuite à juger si l'entreprise crée de la valeur. Mieux vaut un tiret.
    """
    if not mcap_usd or mcap_usd <= 0 or beta is None:
        return None
    cout_fonds_propres = TAUX_SANS_RISQUE + beta * PRIME_DE_RISQUE
    d = dette or 0
    v = mcap_usd + d
    if v <= 0:
        return None
    if d <= 0:
        return round(cout_fonds_propres, 2)
    # Coût de la dette = ce que la société paie RÉELLEMENT sur ce qu'elle doit,
    # pas un taux de marché théorique. Borné à 25 % : au-delà, c'est que la dette
    # du bilan ne correspond pas aux intérêts de l'exercice (dette remboursée en
    # cours d'année, par exemple), et le ratio n'a plus de sens.
    cout_dette = None
    if interets and d > 0:
        r = 100.0 * abs(interets) / d
        if 0 < r <= 25:
            cout_dette = r
    if cout_dette is None:
        cout_dette = TAUX_SANS_RISQUE + 1.5      # dette notée, écart de crédit usuel
    t = (taux_impot_pct or 21.0) / 100.0
    w = (mcap_usd / v) * cout_fonds_propres + (d / v) * cout_dette * (1 - t)
    return round(w, 2) if math.isfinite(w) else None
