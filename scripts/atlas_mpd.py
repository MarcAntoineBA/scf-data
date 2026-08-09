#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""atlas_mpd.py — PRODUCTIVITÉ MARGINALE DE LA DETTE. Source unique du calcul.

Importé par fetch_atlas_econ.py, fetch_atlas_detail.py et patch_atlas_mpd.py :
la formule n'existe qu'ici, donc les trois caches ne peuvent pas diverger.

────────────────────────────────────────────────────────────────────────────────
CE QUE ÇA MESURE
    Combien d'unités de PIB supplémentaires une économie obtient pour chaque
    unité de dette supplémentaire qu'elle crée.

        MPD(t) =   PIB(t) − PIB(t−10)
                 ─────────────────────
                  Dette(t) − Dette(t−10)

    0,50 → 1 € de dette nouvelle a produit 0,50 € de PIB nouveau.
    Une valeur qui décroît dans le temps = l'économie a besoin de plus en plus
    de dette pour produire la même croissance.

POURQUOI LA DETTE **TOTALE** ET NON LA SEULE DETTE PUBLIQUE
    Le PIB est nourri par tout le crédit créé dans l'économie, pas seulement
    celui de l'État. L'Espagne de 2007 affichait 36 % de dette publique — et
    l'une des pires bulles de crédit privé de l'histoire européenne. Ne regarder
    que l'État aurait donné un pays parfaitement sain à la veille du krach.
    On additionne donc :
        debt        dette publique brute, % du PIB          (FMI WEO GGXWDG_NGDP)
        credit_gdp  crédit au secteur privé, % du PIB       (BM FS.AST.PRVT.GD.ZS)

POURQUOI LE PIB EN MONNAIE LOCALE (ngdp_lcu, BM NY.GDP.MKTP.CN)
    PIÈGE ÉVITÉ : passer par le PIB en dollars ferait entrer les mouvements de
    change dans le calcul. Numérateur et dénominateur sont convertis au taux de
    l'année, mais ils portent sur des ANNÉES DIFFÉRENTES (t et t−10) : une
    dévaluation contamine alors le ratio. Vérifié sur l'Argentine et la Turquie,
    où la version dollar donne des valeurs aberrantes. En monnaie locale, le
    change disparaît complètement de l'équation.

CE QUE CETTE MESURE N'EST PAS — ET POURQUOI ELLE DIFFÈRE DU CHAPITRE THÈSE
    Le chapitre « Le mur de la dette » calcule le MÊME indicateur sur la série
    BRI (crédit au secteur non financier), plus rigoureuse mais limitée à 44
    économies. Les deux additionnent bien public + privé, mais les périmètres
    comptables ne coïncident pas, donc les VALEURS diffèrent — dans les deux
    sens, ce n'est PAS un biais systématique. Mesuré le 2026-08-06 (dette totale
    en % du PIB, puis MPD) :

        pays   Atlas (FMI+BM)        BRI                  MPD Atlas / BRI
        US     142+201 = 343 %       111+140 = 251 %      0,26  /  0,40
        FR     121+108 = 228 %       108+215 = 324 %      0,33  /  0,26
        DE      74+ 77 = 151 %        59+137 = 196 %      1,00  /  0,61
        JP     193+187 = 380 %       179+175 = 354 %      0,18  /  0,28

    Deux écarts de périmètre expliquent l'essentiel :
      * `debt` (FMI GGXWDG) est la dette publique BRUTE, titres détenus par
        d'autres administrations inclus ; la BRI consolide davantage.
      * `credit_gdp` (BM FS.AST.PRVT.GD.ZS) compte le crédit intérieur au
        secteur privé par TOUTES les sociétés financières — très large aux
        États-Unis (201 % contre 140 % à la BRI) — mais ignore la dette
        contractée à l'étranger, ce qui la sous-estime en France (108 % contre
        215 %, l'endettement des entreprises françaises passant beaucoup par
        les marchés obligataires et l'international).

    Conséquence pratique : la métrique de l'Atlas sert à COMPARER 171 pays entre
    eux et à lire une TENDANCE dans le temps, pas à fournir le niveau comptable
    exact d'un pays. Pour ce niveau, c'est la version BRI du chapitre qui fait
    référence. Les deux doivent donc être présentées comme distinctes au lecteur.
────────────────────────────────────────────────────────────────────────────────
"""

# Fenêtre glissante, en années. 10 ans : assez long pour effacer le bruit du cycle
# économique, assez court pour que la dégradation reste lisible décennie par décennie.
MPD_WINDOW = 10

# Variation minimale de dette sur la fenêtre, en % du PIB de l'année de départ.
# En dessous, le dénominateur est trop petit : le ratio explose et ne mesure plus
# rien (diviser une croissance de 10 ans par un epsilon de dette). Ces pays sont
# marqués `mpd_dlv` — « dette stable ou réduite » — au lieu d'être noyés dans les
# données manquantes : ne pas s'endetter pour croître est une INFORMATION, pas un trou.
MIN_DEBT_DELTA_PCT_GDP = 5.0

# Nombre minimal de points calculables pour publier une série. En dessous, on n'a
# pas une trajectoire mais un point isolé, souvent issu d'un unique millésime
# douteux (vérifié : l'Afghanistan sortait à 16,3 sur UNE seule année valide).
MIN_POINTS = 3

# Séries d'entrée requises, dans l'ordre où on les documente à l'utilisateur.
# ngdp_lcu ne sert QU'AU CALCUL : elle est retirée du cache après coup (66 ans
# × 217 pays de PIB en monnaie locale n'intéressent aucun graphe de l'Atlas).
MPD_INPUTS = ("debt", "credit_gdp", "ngdp_lcu")


def _at(series, year):
    """Valeur d'une série {s, v} pour une année donnée. None si hors bornes/trou."""
    if not series:
        return None
    i = year - series["s"]
    v = series.get("v") or []
    if i < 0 or i >= len(v):
        return None
    return v[i]


def _span(series):
    return series["s"], series["s"] + len(series["v"]) - 1


def compute_mpd(hist, window=MPD_WINDOW):
    """hist = {clé: {s, v}} d'un pays → (serie_mpd, serie_dlv).

    serie_mpd : {s, v} des ratios (None là où non calculable), ou None.
    serie_dlv : {s, v} de 1/None marquant les années « dette stable ou réduite ».
    """
    debt, cred, ngdp = (hist.get(k) for k in MPD_INPUTS)
    if not (debt and cred and ngdp):
        return None, None

    starts, ends = zip(*(_span(s) for s in (debt, cred, ngdp)))
    y0, y1 = max(starts), min(ends)
    if y1 - y0 < window:
        return None, None

    # Niveau de dette totale, en monnaie locale, année par année.
    level = {}
    for y in range(y0, y1 + 1):
        d, c, g = _at(debt, y), _at(cred, y), _at(ngdp, y)
        if d is None or c is None or g is None or g <= 0:
            continue
        level[y] = ((d + c) / 100.0) * g

    vals, dlv, any_v, any_d = [], [], False, False
    for y in range(y0 + window, y1 + 1):
        y_prev = y - window
        g, g_prev = _at(ngdp, y), _at(ngdp, y_prev)
        lv, lv_prev = level.get(y), level.get(y_prev)

        if None in (g, g_prev, lv, lv_prev) or g_prev <= 0:
            vals.append(None)
            dlv.append(None)
            continue

        d_debt = lv - lv_prev
        # Dénominateur trop faible (ou négatif) → pas un ratio, un signal.
        if d_debt < (MIN_DEBT_DELTA_PCT_GDP / 100.0) * g_prev:
            vals.append(None)
            dlv.append(1)
            any_d = True
            continue

        vals.append(round((g - g_prev) / d_debt, 2))
        dlv.append(None)
        any_v = True

    s = y0 + window
    n_pts = sum(1 for x in vals if x is not None)
    return ({"s": s, "v": vals} if (any_v and n_pts >= MIN_POINTS) else None,
            {"s": s, "v": dlv} if any_d else None)


def last_non_null(series):
    """(valeur, année) du dernier point non nul d'une série {s, v}. (None, None) sinon."""
    if not series:
        return None, None
    v = series["v"]
    for i in range(len(v) - 1, -1, -1):
        if v[i] is not None:
            return v[i], series["s"] + i
    return None, None


def inject_mpd(countries, verbose=True, drop_inputs=True):
    """Ajoute hist.mpd / hist.mpd_dlv et latest.mpd / latest.mpd_dlv à chaque pays.

    Idempotent : recalcule intégralement à partir des séries sources.

    RÉSISTANCE AUX PANNES — si les séries sources sont absentes (la Banque
    mondiale ou le FMI n'ont pas répondu ce jour-là), on NE PURGE PAS le MPD
    déjà présent : le fetcher fonctionne en « garde par source », une panne
    d'API ne doit pas vider une métrique de la carte. Le cas est compté et
    journalisé pour rester visible.

    drop_inputs : retire ngdp_lcu du cache après calcul (elle ne sert à rien
    d'autre et pèse 217 pays × 66 ans pour zéro affichage).
    """
    n_ok = n_dlv = n_kept = 0
    missing = {k: 0 for k in MPD_INPUTS}

    for _a3, entry in countries.items():
        hist = entry.get("hist")
        if not isinstance(hist, dict):
            continue
        latest = entry.get("latest")
        has_inputs = all(hist.get(k) for k in MPD_INPUTS)
        for k in MPD_INPUTS:
            if not hist.get(k):
                missing[k] += 1

        if not has_inputs:
            # Sources incomplètes : on garde tel quel (valeur du run précédent).
            if hist.get("mpd"):
                n_kept += 1
            if drop_inputs:
                hist.pop("ngdp_lcu", None)
            continue

        mpd, dlv = compute_mpd(hist)

        hist.pop("mpd", None)
        hist.pop("mpd_dlv", None)
        if isinstance(latest, dict):
            latest.pop("mpd", None)
            latest.pop("mpd_dlv", None)

        if mpd:
            hist["mpd"] = mpd
            n_ok += 1
            val, yr = last_non_null(mpd)
            if val is not None and isinstance(latest, dict):
                latest["mpd"] = [val, yr, 0]
        if dlv:
            hist["mpd_dlv"] = dlv
            val, yr = last_non_null(dlv)
            # On ne signale « dette stable ou réduite » que si aucune valeur
            # chiffrée n'existe : sinon un épisode ancien de désendettement
            # masquerait le ratio actuel, qui est l'information principale.
            if val and isinstance(latest, dict) and "mpd" not in latest:
                latest["mpd_dlv"] = [1, yr, 0]
                n_dlv += 1

        if drop_inputs:
            hist.pop("ngdp_lcu", None)

    if verbose:
        print(f"[MPD] {n_ok} pays avec série · {n_dlv} « dette stable ou réduite »"
              + (f" · {n_kept} conservés (sources incomplètes)" if n_kept else ""))
        print("[MPD] séries sources manquantes : "
              + " · ".join(f"{k}={v}" for k, v in missing.items()))
    return {"n_ok": n_ok, "n_dlv": n_dlv, "n_kept": n_kept, "missing": missing}
