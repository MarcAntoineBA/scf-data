"""corrections_sources.py — les sociétés dont la source principale publie un résultat faux.

POURQUOI CE FICHIER EXISTE
ING Groep balise son résultat consolidé sous une étiquette PROPRE, invisible de
l'API XBRL de la SEC, et met sous les étiquettes standard un autre montant :
12 126 M€ pour 2022 quand son communiqué dit 3 674 M€. stockanalysis recopie ce
montant. Les deux fiches d'ING — INGA.AS et le certificat ING — affichaient un
BPA 2022 de 3,35 € pour 1,02 € réel, et tout ce qui en découle (ROE, taux de
distribution, croissance du bénéfice). Mesuré le 10/10/2026.

Deux sources sur trois disant la même chose fausse, aucune règle automatique ne
peut trancher : la décision est écrite ici, avec ses preuves.

CE QUE LE REGISTRE CONTIENT — ET NE CONTIENT PAS
Il ne porte AUCUN chiffre de la société. Il désigne TradingView comme référence
pour certaines grandeurs, et liste des PREUVES : résultats officiels avec le
lien du communiqué. Les preuves sont REVÉRIFIÉES à chaque passage : si
TradingView s'écarte d'une seule de plus de 0,5 %, rien n'est corrigé, et le
dossier dit pourquoi. Une référence qui dérive cesse d'en être une.

Utilisé par `fetch_intl_fundamentals.py` (qui a déjà la série TradingView) et
par `fetch_sec_fundamentals.py` (qui la demande ici, par le CIK).
"""

import gzip
import json
import urllib.request

TV_SCAN = "https://scanner.tradingview.com/global/scan"
TOLERANCE_PREUVE = 0.005

# Grandeur de notre schéma → colonne annuelle TradingView.
COLONNES = {"net_income": "net_income_fy_h",
            "eps_basic": "earnings_per_share_basic_fy_h",
            "eps_diluted": "earnings_per_share_diluted_fy_h"}

REFERENCE_TV = {
    "INGA.AS": {
        "cik": 1039765,
        "symbole_tv": "EURONEXT:INGA",
        "devise": "EUR",
        "champs": ("net_income", "eps_basic", "eps_diluted"),
        "pourquoi": "résultat XBRL d'ING (et de stockanalysis) faux : "
                    "2022 = 12 126 M€ contre 3 674 M€ publiés",
        # Avant 2017, ni le XBRL ni TradingView ne donnent le résultat publié
        # (2016 : 4 651 M€ publiés, 4 210 chez TradingView — un autre périmètre,
        # retraité de l'assurance). Ces années restent VIDES : `depuis` borne.
        "depuis": 2017,
        "preuves": [
            (2017, 4905e6, "https://www.ing.com/Investors/Financial-performance/Annual-Reports.htm"),
            (2018, 4703e6, "https://www.ing.com/Investors/Financial-performance/Annual-Reports.htm"),
            (2019, 4781e6, "https://ing.com/Investors/Financial-performance/Quarterly-results/ING-posts-2019-net-result-of-4781-million-4Q2019-net-result-of-880-million-1.htm"),
            (2020, 2485e6, "https://www.ing.com/Investor-relations/Financial-performance/Quarterly-results/ING-Press-release-4Q2020.htm"),
            (2021, 4776e6, "https://ing.com/news/2022/02/ing-posts-4q2021-net-result-of-945-million-fy2021-net-result-of-4776-million.html"),
            (2022, 3674e6, "https://ing.com/Investors/Financial-performance/Quarterly-results/ING-posts-FY2022-net-result-of-3674-million-proposed-final-2022-dividend-of-0.389-per-share-1.htm"),
            (2023, 7287e6, "https://ing.com/news/2024/02/4qfy2023-ing-press-release-1.html"),
            (2024, 6392e6, "https://ing.com/news/2025/02/4qfy2024-ing-press-release.html"),
            (2025, 6327e6, "https://ing.com/news/2025/4qfy2025-ing-results.html"),
        ],
    },
}


def cle_par_cik(cik):
    try:
        cik = int(cik)
    except (TypeError, ValueError):
        return None
    for cle, ref in REFERENCE_TV.items():
        if ref.get("cik") == cik:
            return cle
    return None


def _devise(cur):
    cur = (cur or "").upper()
    return {"GBX": "GBP", "GBP": "GBP", "ZAC": "ZAR", "ILA": "ILS"}.get(cur, cur) or None


def series_tv(symbole_tv, timeout=30):
    """La réponse TradingView pour un symbole, ou None."""
    colonnes = ["fiscal_period_fy_h", "currency"] + sorted(set(COLONNES.values()))
    corps = json.dumps({"symbols": {"tickers": [symbole_tv]},
                        "columns": colonnes}).encode("utf-8")
    req = urllib.request.Request(TV_SCAN, data=corps, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
        "Accept-Encoding": "gzip", "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            brut = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                brut = gzip.decompress(brut)
        for x in json.loads(brut).get("data") or []:
            if x.get("s") == symbole_tv:
                return dict(zip(colonnes, x.get("d") or []))
    except Exception:
        return None
    return None


def borne(cle):
    """La première année couverte par les preuves, ou None."""
    ref = REFERENCE_TV.get(cle) or {}
    return ref.get("depuis")


def corriger(cle, exercices, devise, tv=None):
    """Remplace, sur `exercices`, les grandeurs que le registre désigne — après
    avoir revérifié chaque preuve. En place.

    Rend un dossier (dict) à ranger dans le résumé, ou None hors registre.
    """
    ref = REFERENCE_TV.get(cle)
    if not ref:
        return None
    dossier = {"pourquoi": ref["pourquoi"], "reference": "TradingView " + ref["symbole_tv"],
               "applique": False}
    if tv is None:
        tv = series_tv(ref["symbole_tv"])
    if not tv:
        dossier["refus"] = "TradingView n'a rien servi"
        return dossier
    if _devise(tv.get("currency")) != devise:
        # On remplace, on ne convertit pas : une devise différente demanderait
        # un taux, donc une approximation.
        dossier["refus"] = "devise TradingView %s ≠ devise des états %s" % (
            _devise(tv.get("currency")), devise)
        return dossier
    etiquettes = tv.get("fiscal_period_fy_h") or []
    par_an = {}
    for champ in ref["champs"]:
        col = tv.get(COLONNES[champ]) or []
        for i, lab in enumerate(etiquettes):
            try:
                an = int(lab)
            except (TypeError, ValueError):
                continue
            v = col[i] if i < len(col) else None
            if isinstance(v, (int, float)):
                par_an.setdefault(an, {})[champ] = float(v)
    ecarts = []
    for an, officiel, url in ref["preuves"]:
        v = (par_an.get(an) or {}).get("net_income")
        if v is None or abs(v / officiel - 1) > TOLERANCE_PREUVE:
            dossier["refus"] = ("preuve %d non tenue : TradingView %s contre %.0f publié (%s)"
                                % (an, v, officiel, url))
            return dossier
        ecarts.append(round(100 * (v / officiel - 1), 3))
    n = vides = 0
    depuis = ref.get("depuis")
    for e in exercices:
        if depuis and isinstance(e.get("annee"), int) and e["annee"] < depuis:
            # Hors de la période prouvée : la source est fausse et la référence
            # ne la remplace pas. Vide plutôt que faux.
            for champ in ref["champs"]:
                e[champ] = None
            e["net_income_total"] = None
            e["non_verifiable"] = True
            vides += 1
            continue
        rep = par_an.get(e.get("annee"))
        if not rep:
            continue
        rn_avant = e.get("net_income")
        for champ in ref["champs"]:
            if rep.get(champ) is not None:
                e[champ] = rep[champ]
        # Le résultat TOTAL (minoritaires compris) suit du même écart : la part
        # des minoritaires, elle, n'était pas en cause.
        if (isinstance(rn_avant, (int, float))
                and isinstance(e.get("net_income_total"), (int, float))
                and isinstance(rep.get("net_income"), (int, float))):
            e["net_income_total"] += rep["net_income"] - rn_avant
        e["corrige_par"] = "tradingview"
        n += 1
    dossier.update(applique=True, exercices_corriges=n, exercices_vides=vides,
                   depuis=depuis, preuves_ecart_pct=ecarts,
                   preuves=[u for _a, _v, u in ref["preuves"]])
    return dossier
