#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""atlas_prix.py — STRUCTURE ET DYNAMIQUE DES PRIX. Source unique du volet inflation.

Importé par fetch_atlas_detail.py. Comme atlas_mpd.py, la logique n'existe QU'ICI :
le cache détail et le cache carte ne peuvent donc pas diverger sur la définition
d'un indicateur de prix.

────────────────────────────────────────────────────────────────────────────────
POURQUOI CE MODULE EXISTE
    L'Atlas n'avait qu'UN chiffre d'inflation : l'IPC moyen annuel du FMI. C'est
    la moyenne d'un panier moyen sur une année moyenne — assez pour classer les
    pays, incapable de dire POURQUOI les prix montent ni SI ça ralentit.
    Trois questions différentes, trois familles de chiffres :

    1. DYNAMIQUE — à quelle vitesse, et est-ce que ça décélère ?
         infl        IPC, moyenne annuelle          FMI WEO      1980→2031
         infl_eop    IPC, fin de période (déc/déc)  FMI WEO      1980→2031
         infl_vol10  volatilité sur 10 ans          dérivée      —
       ⚠ LE COUPLE MOYENNE / FIN DE PÉRIODE EST LE POINT CLÉ DE CE VOLET.
         La moyenne annuelle porte l'inflation de l'année PRÉCÉDENTE dans son
         calcul (effet de report) ; la fin de période ne regarde que décembre
         contre décembre. Quand la fin de période passe SOUS la moyenne, la
         désinflation est déjà là — la moyenne mettra un an à le dire. C'est
         exactement ce qui s'est passé en zone euro en 2023 : moyenne 5,4 %,
         fin de période 2,9 %. Un seul des deux chiffres racontait la suite.

    2. STRUCTURE — quels prix montent ?
         infl_core   IPC hors alimentation & énergie  OCDE   mensuel, 57 pays
         infl_food   IPC alimentation                 OCDE   mensuel, 57 pays
         infl_nrg    IPC énergie                      OCDE   mensuel, 57 pays
         deflator    déflateur du PIB                 BM     annuel, ~255
       L'IPC mesure le panier du CONSOMMATEUR ; le déflateur du PIB mesure le
       prix de TOUT ce que le pays PRODUIT. L'écart entre les deux n'est pas du
       bruit : il porte les termes de l'échange. Un exportateur de pétrole voit
       son déflateur bondir quand le baril monte alors que son IPC bouge à
       peine — et l'inverse chez l'importateur. Là où l'écart est durablement
       négatif, le pays s'appauvrit en produisant : il vend moins cher ce qu'il
       fabrique qu'il n'achète ce qu'il consomme.

    3. CAUSES MONÉTAIRES ET EXTERNES — d'où ça vient ?
         m2_gr       croissance de la masse monétaire (M2)  BM   ~157 pays
         fx_dep      dépréciation de la monnaie vs USD      dérivée BM ~213
         rinr        taux d'intérêt réel                    BM   ~140 pays
         price_lvl   niveau des prix comparé aux États-Unis dérivée BM ~200
       Pour un pays qui importe son énergie et sa nourriture, une monnaie qui
       perd 30 % contre le dollar EST l'inflation de l'an prochain. C'est le
       seul indicateur de ce volet qui soit un signal AVANCÉ.

CE QUE CE VOLET N'EST PAS
    Ce n'est pas une prévision, et ce n'est pas une mesure du coût de la vie.
    `price_lvl` compare des NIVEAUX de prix entre pays à un instant donné ;
    `infl` mesure leur VARIATION dans le temps. Un pays cher peut avoir une
    inflation nulle, un pays bon marché une hyperinflation. Les deux colonnes
    ne se déduisent jamais l'une de l'autre.

PIÈGE ÉVITÉ — L'ANNUALISATION DES SÉRIES OCDE
    L'OCDE publie le glissement annuel MENSUEL (juillet 2026 contre juillet
    2025). Le reste de l'Atlas est annuel. On ne prend donc PAS le dernier mois
    comme « valeur de l'année » : on fait la MOYENNE des 12 glissements annuels
    de l'année civile, ce qui redonne exactement la définition de l'IPC moyen
    annuel du FMI et rend les deux séries superposables sur un même graphe.
    Une année incomplète (l'année en cours) n'est publiée que si elle compte au
    moins 6 mois, et le dernier point mensuel est conservé À PART (`prix_last`)
    pour que la fiche pays puisse afficher « juillet 2026 : +2,1 % » sans
    prétendre que c'est l'année.

PIÈGE ÉVITÉ — LE SENS DE `fx_dep`
    La Banque mondiale publie PA.NUS.FCRF en UNITÉS DE MONNAIE LOCALE PAR
    DOLLAR. Le chiffre MONTE quand la monnaie s'AFFAIBLIT. On garde ce sens et
    on nomme la série « dépréciation » : positif = la monnaie perd de la valeur.
    L'écrire à l'envers (« appréciation ») donnerait un graphe où la crise
    argentine ressemble à un succès.

PIÈGE ÉVITÉ — LE SENS DE `price_lvl`
    Le ratio est PPA ÷ taux de marché, ramené en base 100 = États-Unis. Sous
    100, le pays est moins cher que les États-Unis. L'inverse du ratio dirait
    la même chose à l'envers ; la convention retenue est celle de la Banque
    mondiale (« price level ratio of PPP conversion factor to market exchange
    rate »), pour que le chiffre de l'Atlas soit recoupable tel quel.
────────────────────────────────────────────────────────────────────────────────
"""

import json
import sys
import urllib.error
import urllib.request

# ── Séries de travail : servent au calcul, ne sont JAMAIS publiées ────────────
# fx_lcu (monnaie locale par dollar) et ppp_lcu (facteur de conversion PPA) ne
# racontent rien à un lecteur ; ce sont leurs DÉRIVÉES qui parlent. Comme
# ngdp_lcu pour le MPD, on les retire du cache après usage.
PRIX_WORK_KEYS = ("fx_lcu", "ppp_lcu")

# Fenêtre de la volatilité. 10 ans : même fenêtre que le MPD, pour qu'un lecteur
# qui compare les deux volets ne change pas d'unité de temps en route.
VOL_WINDOW = 10
# En dessous de 7 observations sur les 10 ans, l'écart-type mesure surtout les
# trous de la série. Vérifié : sans ce seuil, une dizaine de petits États
# sortaient une « volatilité » calculée sur 2 points.
VOL_MIN_POINTS = 7

# ── OCDE — glissement annuel mensuel de l'IPC par poste ──────────────────────
# Clé SDMX : REF_AREA.FREQ.METHODOLOGY.MEASURE.UNIT_MEASURE.EXPENDITURE.ADJUSTMENT.TRANSFORMATION
#   MEASURE=CPI · UNIT_MEASURE=PA (pourcentage) · TRANSFORMATION=GY (glissement annuel)
# EXPENDITURE : _T total · _TXCP01_NRG hors alimentation et énergie (= sous-jacente
# au sens OCDE) · CP01 alimentation & boissons non alcoolisées · CP045 électricité,
# gaz et autres combustibles.
# ⚠ CP045 est l'énergie du LOGEMENT. Les carburants des transports sont en CP0722.
# On publie CP045 sous le nom « énergie » parce que c'est le poste que les mesures
# de soutien des États visent (boucliers tarifaires), et on le DIT dans le libellé
# côté front plutôt que d'agréger deux postes à des pondérations qu'on n'a pas.
OECD_DSD = ("https://sdmx.oecd.org/public/rest/data/"
            "OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/"
            ".M.N.CPI.PA.{exp}.N.GY?startPeriod={start}"
            "&format=jsondata&dimensionAtObservation=AllDimensions")
OECD_EXPENDITURE = {
    "infl_core": "_TXCP01_NRG",
    "infl_food": "CP01",
    "infl_nrg": "CP045",
}
# ⚠ LE TOTAL OCDE (`_T`) A ÉTÉ RETIRÉ DE LA COLLECTE, VOLONTAIREMENT.
# Il ne servait que de témoin de cohérence (« la clé SDMX est-elle la bonne ? »),
# n'était jamais publié, et coûtait un quart du temps de ce bloc — sur un job qui
# s'est fait TUER par son plafond de 50 min le 2026-08-08. L'inflation totale
# vient déjà du FMI (`infl`), sur 228 entités au lieu de 57 : le témoin était un
# doublon payant. Pour re-vérifier la clé un jour, la rejouer à la main plutôt
# que la faire tourner tous les jours en production.
OECD_START = "1990-01"
# Mois minimum pour publier l'année en cours (cf. docstring). 6 = un semestre.
OECD_MIN_MONTHS_PARTIAL = 6


def _r1(x):
    if x is None:
        return None
    r = round(float(x), 1)
    return int(r) if r == int(r) else r


def _r2(x):
    if x is None:
        return None
    r = round(float(x), 2)
    return int(r) if r == int(r) else r


def _pack(year_map, rnd=_r1, min_points=2):
    """{année: val} → {'s': première année, 'v': [...]}. None si trop maigre."""
    if not year_map:
        return None
    y0, y1 = min(year_map), max(year_map)
    v = []
    for y in range(y0, y1 + 1):
        val = year_map.get(y)
        v.append(rnd(val) if val is not None else None)
    if len([x for x in v if x is not None]) < min_points:
        return None
    return {"s": y0, "v": v}


def _unpack(series):
    """{'s','v'} → {année: val} (sans les trous)."""
    if not series or not series.get("v"):
        return {}
    s = series["s"]
    return {s + i: x for i, x in enumerate(series["v"]) if x is not None}


# ══ 1. OCDE : la structure des prix, poste par poste ══════════════════════════

def _oecd_get(url, timeout=180, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/vnd.sdmx.data+json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError) as e:
            last = e
    sys.stderr.write(f"[WARN] OCDE prix : {last}\n")
    return None


def _oecd_parse(doc, a3set):
    """SDMX-JSON « AllDimensions » → {a3: {année: moyenne des glissements},
    a3: {'_last': (période, valeur)}}.

    Le format place les observations dans un dict dont la CLÉ est la liste des
    index de dimension séparés par ':'. On ne peut donc rien lire sans la table
    des dimensions — d'où la reconstruction des index ci-dessous.
    """
    if not doc:
        return {}, {}
    try:
        struct = doc["data"]["structures"][0]
        dims = struct["dimensions"]["observation"]
        obs = doc["data"]["dataSets"][0]["observations"]
    except (KeyError, IndexError, TypeError):
        sys.stderr.write("[WARN] OCDE prix : structure SDMX inattendue\n")
        return {}, {}
    pos = {d["id"]: (i, [v["id"] for v in d["values"]]) for i, d in enumerate(dims)}
    if "REF_AREA" not in pos or "TIME_PERIOD" not in pos:
        sys.stderr.write("[WARN] OCDE prix : REF_AREA/TIME_PERIOD absentes\n")
        return {}, {}
    i_area, areas = pos["REF_AREA"]
    i_time, times = pos["TIME_PERIOD"]

    # {a3: {année: [glissements mensuels]}} puis moyenne ; et le dernier mois.
    by_year, last = {}, {}
    for key, val in obs.items():
        try:
            idx = [int(x) for x in key.split(":")]
            a3 = areas[idx[i_area]]
            period = times[idx[i_time]]           # 'YYYY-MM'
            x = val[0]
        except (ValueError, IndexError, TypeError):
            continue
        if x is None or a3 not in a3set:
            continue
        try:
            year = int(period[:4])
            x = float(x)
        except (ValueError, TypeError):
            continue
        by_year.setdefault(a3, {}).setdefault(year, []).append(x)
        if a3 not in last or period > last[a3][0]:
            last[a3] = (period, x)

    out = {}
    for a3, years in by_year.items():
        ymax = max(years)
        acc = {}
        for y, vals in years.items():
            # L'année en cours n'est publiée qu'à partir d'un semestre complet :
            # une moyenne sur 2 mois n'est pas une inflation annuelle.
            if y == ymax and len(vals) < OECD_MIN_MONTHS_PARTIAL and len(years) > 1:
                continue
            acc[y] = sum(vals) / len(vals)
        if acc:
            out[a3] = acc
    return out, last


def fetch_oecd_prices(a3set, verbose=True):
    """({a3: {clé: {s,v}}}, {a3: {clé: {'ym','v'}}}, ok).

    ok = au moins la sous-jacente ET l'alimentation ont répondu. En dessous,
    l'appelant considère la source en échec et garde le run précédent (garde
    par source), plutôt que d'effacer un volet entier sur un 503 passager.
    """
    hist, last_pt, got = {}, {}, 0
    for key, exp in OECD_EXPENDITURE.items():
        doc = _oecd_get(OECD_DSD.format(exp=exp, start=OECD_START))
        years, last = _oecd_parse(doc, a3set)
        if not years:
            if verbose:
                print(f"[OCDE.prix] {key} ({exp}) : VIDE")
            continue
        got += 1
        n = 0
        for a3, ymap in years.items():
            s = _pack(ymap, rnd=_r1)
            if s:
                hist.setdefault(a3, {})[key] = s
                n += 1
        for a3, (period, x) in last.items():
            last_pt.setdefault(a3, {})[key] = {"ym": period, "v": _r1(x)}
        if verbose:
            spans = [v for v in last.values()]
            newest = max((p for p, _ in spans), default="?")
            print(f"[OCDE.prix] {key} ({exp}) : {n} pays · dernier mois {newest}")
    # `infl_oecd` n'est qu'un témoin de cohérence : on le retire avant publication.
    # Il sert à vérifier que le total OCDE colle à l'IPC FMI du même pays ; si les
    # deux divergeaient fortement, c'est la CLÉ SDMX qui serait fausse, pas le pays.
    for a3 in hist:
        hist[a3].pop("infl_oecd", None)
    ok = got >= 2
    return hist, last_pt, ok


# ══ 2. Dérivées sans réseau ═══════════════════════════════════════════════════

def compute_fx_dep(hist):
    """Dépréciation annuelle de la monnaie contre le dollar, en %.

    PA.NUS.FCRF = unités de monnaie locale par dollar → une HAUSSE du taux est
    une PERTE de valeur de la monnaie. On publie donc directement la variation
    du taux : positive = dépréciation.
    """
    fx = _unpack(hist.get("fx_lcu"))
    if len(fx) < 3:
        return None
    out = {}
    for y, v in fx.items():
        prev = fx.get(y - 1)
        # Un taux nul ou négatif n'existe pas : c'est une valeur corrompue.
        if prev is None or prev <= 0 or v is None or v <= 0:
            continue
        out[y] = (v / prev - 1.0) * 100.0
    return _pack(out, rnd=_r1)


def compute_price_level(hist):
    """Niveau des prix comparé aux États-Unis, base 100.

    ratio = facteur de conversion PPA ÷ taux de change de marché, ×100.
    Sous 100 : le pays est moins cher que les États-Unis à revenu converti.
    """
    ppp = _unpack(hist.get("ppp_lcu"))
    fx = _unpack(hist.get("fx_lcu"))
    if not ppp or not fx:
        return None
    out = {}
    for y, p in ppp.items():
        f = fx.get(y)
        if f is None or f <= 0 or p is None or p <= 0:
            continue
        out[y] = p / f * 100.0
    return _pack(out, rnd=_r1)


def compute_infl_vol(hist, window=VOL_WINDOW):
    """Écart-type glissant de l'inflation sur `window` années.

    Ce que ça mesure : la PRÉVISIBILITÉ des prix. Deux pays à 4 % de moyenne
    n'offrent pas le même environnement si l'un oscille entre 3 et 5 et l'autre
    entre −6 et 18 — c'est la volatilité, pas le niveau, qui rend un contrat de
    long terme impossible à écrire.

    ⚠ On calcule sur la série RÉALISÉE seulement si elle porte ses prévisions :
    le FMI publie jusqu'en 2031, et une prévision est lissée par construction.
    L'inclure ferait TOMBER artificiellement la volatilité des dernières années.
    L'appelant passe donc `forecast_from` pour couper la série.
    """
    infl = _unpack(hist.get("infl"))
    if len(infl) < VOL_MIN_POINTS:
        return None
    out = {}
    for y in infl:
        win = [infl[k] for k in range(y - window + 1, y + 1) if k in infl]
        if len(win) < VOL_MIN_POINTS:
            continue
        m = sum(win) / len(win)
        var = sum((x - m) ** 2 for x in win) / (len(win) - 1)
        out[y] = var ** 0.5
    return _pack(out, rnd=_r2)


def _truncate(series, last_year):
    """Coupe une série packée après `last_year` (retire les prévisions)."""
    if not series or last_year is None:
        return series
    s, v = series["s"], series["v"]
    keep = last_year - s + 1
    if keep <= 0:
        return None
    return {"s": s, "v": v[:keep]}


# ══ 3. Injection ══════════════════════════════════════════════════════════════

def inject_prix(countries, oecd_hist=None, oecd_last=None, oecd_ok=False,
                forecast_from=None, verbose=True, drop_inputs=True):
    """Ajoute au cache détail le volet « Prix & inflation ».

    Écrit dans chaque pays :
        hist.infl_core / infl_food / infl_nrg   séries annuelles OCDE
        hist.fx_dep / price_lvl / infl_vol10    dérivées des séries BM/FMI
        prix.last                               dernier point MENSUEL OCDE
    et retire les séries de travail (fx_lcu, ppp_lcu).

    RÉSISTANCE AUX PANNES — identique à inject_mpd : si une entrée manque, on
    NE PURGE PAS ce qui existe déjà. Un 503 de l'OCDE ne doit pas vider un volet.
    """
    oecd_hist = oecd_hist or {}
    oecd_last = oecd_last or {}
    n = {"infl_core": 0, "infl_food": 0, "infl_nrg": 0,
         "fx_dep": 0, "price_lvl": 0, "infl_vol10": 0, "kept": 0}

    for a3, entry in countries.items():
        hist = entry.get("hist")
        if not isinstance(hist, dict):
            continue

        # ── OCDE : garde par source. Source OK → on remplace (y compris par
        # l'absence, si le pays est sorti du périmètre) ; source KO → on garde.
        if oecd_ok:
            fresh = oecd_hist.get(a3, {})
            for key in ("infl_core", "infl_food", "infl_nrg"):
                if key in fresh:
                    hist[key] = fresh[key]
                    n[key] += 1
                else:
                    hist.pop(key, None)
            lp = {k: v for k, v in (oecd_last.get(a3) or {}).items() if k != "infl_oecd"}
            if lp:
                entry["prix"] = {"last": lp}
            else:
                entry.pop("prix", None)
        elif any(hist.get(k) for k in ("infl_core", "infl_food", "infl_nrg")):
            n["kept"] += 1

        # ── Dérivées : recalculées intégralement quand leurs entrées sont là.
        # Entrée absente → on ne touche à rien : la dérivée du run précédent
        # reste en place (garde par source, comme inject_mpd).
        fxd = compute_fx_dep(hist)
        if fxd:
            hist["fx_dep"] = fxd
            n["fx_dep"] += 1

        lvl = compute_price_level(hist)
        if lvl:
            hist["price_lvl"] = lvl
            n["price_lvl"] += 1

        # Volatilité : sur le RÉALISÉ seulement. On calcule sur une VUE tronquée
        # de l'historique — jamais en modifiant `hist`, qui doit garder les
        # prévisions du FMI pour tous les autres usages.
        if hist.get("infl"):
            realise = _truncate(hist["infl"],
                                forecast_from - 1 if forecast_from else None) or hist["infl"]
            vol = compute_infl_vol({"infl": realise})
            if vol:
                hist["infl_vol10"] = vol
                n["infl_vol10"] += 1

        if drop_inputs:
            for k in PRIX_WORK_KEYS:
                hist.pop(k, None)

    if verbose:
        print(f"[prix] sous-jacente {n['infl_core']} · alimentation {n['infl_food']} "
              f"· énergie {n['infl_nrg']} · dépréciation {n['fx_dep']} "
              f"· niveau des prix {n['price_lvl']} · volatilité {n['infl_vol10']}"
              + (f" · conservés d'un run précédent {n['kept']}" if n["kept"] else ""))
    return countries
