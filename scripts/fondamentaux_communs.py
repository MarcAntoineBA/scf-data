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
import math

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
    ("Dividende", "annees_hausse_dividende", "Années de hausse consécutive", 8, 4, "haut"),

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
        "lecture": _lire(note_ramenee if note_ramenee is not None else total),
        "lecture_bareme_concurrent": _lire(round(total, 1)),
        "profil": profil,
        "par_categorie": {k: {"obtenu": round(v["obtenu"], 1), "possible": v["possible"],
                              "notables": v["notables"]} for k, v in par_categorie.items()},
        "details": details,
    }


# ─────────────────────────────────────────────────────────────────────────
# Croissances, prédictibilité, scores
# ─────────────────────────────────────────────────────────────────────────
def _croissance_annuelle(series):
    """[(annee, valeur)] triés → liste des variations en % d'une année sur l'autre.

    Une variation calculée sur une base NÉGATIVE n'a pas de sens : passer de
    −10 à −5 n'est pas « +50 % » et passer de −5 à +5 n'est pas « +200 % ».
    Ces cas rendent None plutôt qu'un nombre spectaculaire et faux.
    """
    out = []
    for i in range(1, len(series)):
        prev, cur = series[i - 1][1], series[i][1]
        if prev is None or cur is None or prev <= 0:
            out.append(None)
        else:
            out.append(round(100 * (cur - prev) / prev, 2))
    return out


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
    g = _croissance_annuelle(series)
    return {
        "1a": g[-1] if g else None,
        "5a": _mediane(g[-5:]) if len(g) >= 3 else None,
        "10a": _mediane(g[-10:]) if len(g) >= 5 else None,
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
    pts = [(i, v) for i, (_, v) in enumerate(series) if v is not None and v > 0]
    if len(pts) < 5:
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
_FACTEURS_USUELS = [1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 50]


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


def _corriger_divisions(exercices):
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
    for i in range(n - 1, 0, -1):
        a, b = exercices[i - 1], exercices[i]
        sa, sb = a.get("shares_diluted"), b.get("shares_diluted")
        if sa and sb and sa > 0:
            f = _facteur_division(sb / sa)
            if f is not None:
                na, nb = a.get("net_income"), b.get("net_income")
                confirme = True
                if na and nb and na > 0:
                    # Si le résultat net a été multiplié par le même facteur,
                    # c'est une vraie croissance et non une division.
                    if abs((nb / na) - (sb / sa)) / max(sb / sa, 1e-9) < 0.20:
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
    lev_c = _div(cur.get("lt_debt"), cur.get("assets"))
    lev_p = _div(prev.get("lt_debt"), prev.get("assets"))
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
    return sum(d.values()), d


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
    presentes = [v for v in parts.values() if v is not None]
    if len(presentes) < 4:
        return None, parts
    return _r(sum(presentes), 2), parts


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
