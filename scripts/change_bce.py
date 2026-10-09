"""change_bce.py — les cours de change de référence de la BCE, en complément.

POURQUOI CE FICHIER EXISTE
Les collecteurs lisent leurs taux dans deux caches, `fx_rates_cache.json` et
`tradfi_fx_cache.json`, qui n'existent que sur le Mac : dans le cloud, chaque
passage part d'une machine neuve et aucun des deux n'y est publié ni rapatrié.
Mesuré le 09/10/2026, deux conséquences silencieuses :

  · `fetch_intl_fundamentals.py` jetait les vingt exercices servis par
    TradingView de 1 536 sociétés (« conversion impossible : le cache de change
    est vide ») — TotalEnergies, Novartis, ABB, Zurich, Equinor, Unilever,
    Prosus… restaient à cinq exercices ;
  · `fetch_univers_actions.py` publiait toutes les capitalisations en dollars à
    `null`. L'élection de la cotation d'origine ne pouvait plus comparer les
    tailles et retombait sur l'ordre des places, où New York passe devant
    Paris, Amsterdam, Francfort et Londres : Sanofi, ASML, SAP, AstraZeneca,
    HSBC, BP, UBS, Santander… perdaient contre leur certificat américain et
    sortaient de la collecte internationale.

La BCE publie le cours de référence quotidien depuis le 04/01/1999, en un seul
fichier, sans clé. Chaque cours y est « unités par euro » et le dollar y figure,
ce qui donne la valeur d'une unité en dollars au même jour :
(dollars par euro) ÷ (unités par euro). Recoupé le 09/10/2026 contre le cache
du Mac : écart médian de 0,01 % (HKD) à 0,45 % (ZAR) sur 4 000 à 7 000 jours
communs — l'écart d'heure entre le fixing de 14 h 15 et la clôture.

⚠ ON COMPLÈTE, ON NE REMPLACE PAS. Une devise que les caches portent déjà
garde leur série : deux fournisseurs mélangés jour par jour feraient des taux
moyens qui changent de source au milieu d'un exercice. Les devises que la BCE
ne publie pas restent absentes — dollar de Taïwan, peso chilien, roupie
pakistanaise… — et l'appelant refuse alors, il n'approxime pas. Seules
exceptions : les monnaies arrimées au dollar (riyal, dirham…), dont le taux est
fixé par leur banque centrale.
"""

import csv
import io
import sys
import urllib.request
import zipfile

BCE_HIST = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"

# Les devises du Golfe et le dinar jordanien, que la BCE ne cote pas, sont
# ARRIMÉES au dollar par leur banque centrale : unités pour UN dollar, et le
# premier jour où l'arrimage vaut à ce taux. Même table que
# `fetch_marche_actions.PARITES_FIXES_USD` ; la date en plus, parce qu'ici on
# convertit des exercices anciens et qu'un arrimage ne remonte pas avant sa
# création. Avant cette date, pas de taux — l'exercice est refusé.
PARITES_FIXES_USD = {"SAR": (3.75, "1986-06-01"), "AED": (3.6725, "1997-11-01"),
                     "QAR": (3.64, "2001-07-01"), "OMR": (0.3845, "1986-01-01"),
                     "BHD": (0.376, "2001-01-01"), "JOD": (0.709, "1995-10-01")}

_memo = [None]


def series_bce(ua="Mozilla/5.0", timeout=30):
    """{DEVISE: {AAAA-MM-JJ: valeur d'une unité en dollars}}, ou {} en cas d'échec.

    Téléchargé une seule fois par processus.
    """
    if _memo[0] is not None:
        return _memo[0]
    try:
        req = urllib.request.Request(BCE_HIST, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            brut = r.read()
    except Exception as e:
        print("[warn] cours de la BCE indisponibles (%s) : les devises absentes "
              "des caches restent sans taux" % e, file=sys.stderr)
        _memo[0] = {}
        return _memo[0]
    _memo[0] = series_depuis_zip(brut)
    return _memo[0]


def series_depuis_zip(brut):
    """Le fichier `eurofxref-hist.zip` → {DEVISE: {jour: dollars par unité}}.

    Séparé du téléchargement pour être vérifiable hors ligne.
    """
    out = {}
    try:
        with zipfile.ZipFile(io.BytesIO(brut)) as z:
            texte = z.read(z.namelist()[0]).decode("utf-8")
    except Exception as e:
        print("[warn] fichier de la BCE illisible (%s)" % e, file=sys.stderr)
        return out
    lignes = csv.reader(io.StringIO(texte))
    entete = [c.strip() for c in next(lignes, [])]
    if "USD" not in entete:
        print("[warn] fichier de la BCE sans colonne USD : format changé ?",
              file=sys.stderr)
        return out
    i_usd = entete.index("USD")
    colonnes = {dev: i for i, dev in enumerate(entete)
                if i and dev and dev != "USD"}
    for row in lignes:
        if len(row) <= i_usd:
            continue
        jour = row[0].strip()
        try:
            usd_par_eur = float(row[i_usd])
        except ValueError:
            continue
        if usd_par_eur <= 0:
            continue
        out.setdefault("EUR", {})[jour] = usd_par_eur
        for dev, i in colonnes.items():
            try:
                par_eur = float(row[i])
            except (ValueError, IndexError):
                continue
            if par_eur > 0:
                out.setdefault(dev, {})[jour] = usd_par_eur / par_eur
    # Les parités fixes, posées sur le calendrier de la BCE : les taux moyens
    # d'exercice exigent assez de relevés dans l'année, un seul point ne
    # suffirait pas.
    jours = sorted(out.get("EUR") or {})
    for dev, (par_usd, depuis) in PARITES_FIXES_USD.items():
        serie = {j: 1.0 / par_usd for j in jours if j >= depuis}
        if serie:
            out[dev] = serie
    return out


def completer(fusion, ua="Mozilla/5.0"):
    """Ajoute à `fusion` les devises qu'il n'a pas, depuis la BCE. En place.

    Rend la liste des devises ajoutées.
    """
    bce = series_bce(ua)
    ajout = sorted(dev for dev in bce if not fusion.get(dev))
    for dev in ajout:
        fusion[dev] = dict(bce[dev])
    if ajout:
        print("[info] change : %d devise(s) complétée(s) par la BCE (%s)"
              % (len(ajout), ", ".join(ajout)))
    return ajout
