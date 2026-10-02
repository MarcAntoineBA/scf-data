#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Le marché contre les agences : l'écart de taux face à l'Allemagne et la note de
chaque État de la zone euro (module de fetch_obligations.py, 02/10/2026).

QUESTION (MA) : une note d'agence dit le risque qu'un État ne rembourse pas ;
l'écart de taux avec l'Allemagne dit ce que le MARCHÉ fait payer pour ce risque.
Même monnaie, même banque centrale : dans la zone euro, l'écart ne mesure que le
risque prêté à l'État. Placer les deux face à face montre quels pays le marché
juge plus sévèrement (ou plus gentiment) que les agences.

⚠ ZONE EURO SEULEMENT. Hors zone euro, l'écart avec le Bund mêle le risque de
  change et l'inflation de chaque monnaie : il ne mesure plus le risque de l'État.
⚠ DEUX VUES, JAMAIS MÉLANGÉES SUR UN MÊME GRAPHE :
  · « aujourd'hui » : cotations de marché TradingView (rendement de l'obligation
    d'État de référence à 10 ans), les 13 pays cotés. L'écart est calculé DANS la
    même source (le 10 ans allemand TradingView), jamais entre deux sources : le
    Bund de la Bundesbank (courbe lissée) n'est pas l'obligation de référence ;
  · « moyenne du mois » : taux longs de convergence de la BCE (IRS), les 21 pays,
    même mois pour tous. Plus complet, mais 4 à 6 semaines de retard.
⚠ Les notes viennent du registre réglementaire de l'ESMA (et de la table vérifiée
  d'oblig_pays) : jamais d'une note recopiée sans date.
"""
import datetime as dt
import json
import time
import urllib.request

from oblig_net import get_txt

ZONE = {"de": "Allemagne", "nl": "Pays-Bas", "lu": "Luxembourg", "fi": "Finlande", "at": "Autriche", "ie": "Irlande",
        "fr": "France", "be": "Belgique", "ee": "Estonie", "si": "Slovénie", "lt": "Lituanie", "mt": "Malte",
        "es": "Espagne", "pt": "Portugal", "sk": "Slovaquie", "lv": "Lettonie", "cy": "Chypre", "hr": "Croatie",
        "bg": "Bulgarie", "it": "Italie", "gr": "Grèce"}
URL_TV = "https://scanner.tradingview.com/global/scan"
URL_IRS = "https://data-api.ecb.europa.eu/service/data/IRS/M.%s.L.L40.CI.0000.EUR.N.Z?format=csvdata&lastNObservations=6"
SOURCE_TV = "https://www.tradingview.com/markets/bonds/prices-eu/"
SOURCE_IRS = "https://data.ecb.europa.eu/data/datasets/IRS"


def _tradingview(journal):
    """{code: rendement en %} des obligations d'État de référence à 10 ans."""
    corps = json.dumps({"symbols": {"tickers": ["TVC:%s10Y" % c.upper() for c in ZONE]},
                        "columns": ["close", "description"]}).encode()
    for essai in range(3):
        try:
            req = urllib.request.Request(URL_TV, data=corps, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            out = {}
            for x in d.get("data") or []:
                c = x["s"].split(":")[1][:2].lower()
                v = (x.get("d") or [None])[0]
                if c in ZONE and isinstance(v, (int, float)) and -2 < v < 30:
                    out[c] = float(v)
            return out
        except Exception as e:  # noqa: BLE001
            if essai == 2:
                journal.append("TradingView (taux 10 ans) : " + str(e)[:100])
            time.sleep(3 * (essai + 1))
    return {}


def _bce(journal):
    """(mois, {code: rendement en %}, {code: motif d'exclusion}) : le dernier mois où
    l'Allemagne est publiée.
    ⚠ Une série FIGÉE n'est pas un prix de marché : la BCE publie pour la Lituanie
      2,88 % douze mois de suite (−30 pb sous l'Allemagne, quand le marché la cote
      +116 pb), pour la Bulgarie le taux d'ÉMISSION d'une seule obligation, inchangé
      quatre mois. Trois mois identiques → pays écarté, et dit."""
    import csv
    import io
    t = get_txt(URL_IRS % "+".join(c.upper() for c in ZONE), entetes={"Accept": "text/csv"}, timeout=120)
    if not t:
        journal.append("BCE IRS (zone euro) : vide")
        return None, {}, {}
    par, series = {}, {}
    for r in csv.DictReader(io.StringIO(t)):
        try:
            c, m, v = r["REF_AREA"].lower(), r["TIME_PERIOD"], float(r["OBS_VALUE"])
        except (KeyError, TypeError, ValueError):
            continue
        par.setdefault(m, {})[c] = v
        series.setdefault(c, []).append((m, v))
    mois = [m for m in sorted(par) if "de" in par[m]]
    if not mois:
        return None, {}, {}
    m0 = mois[-1]
    exclus = {}
    for c, sv in series.items():
        der = [v for _, v in sorted(sv)][-3:]
        if len(der) == 3 and len(set(der)) == 1:
            exclus[c] = "taux identique trois mois de suite (%s %%) : pas un prix de marché" % ("%g" % der[0]).replace(".", ",")
    vals = {c: v for c, v in par[m0].items() if c not in exclus}
    return m0, vals, exclus


def _ecarts(taux):
    de = taux.get("de")
    if de is None:
        return {}
    return {c: {"taux": round(v, 3), "ecart_pb": round(100 * (v - de), 1)} for c, v in taux.items()}


def construire(journal, notations, precedent=None):
    """`notations` : {code: {"agences": {sp|moodys|fitch: {note, perspective, action, url, type}}}}
    (oblig_pays.notations, mis à jour par l'ESMA). `precedent` : le bloc du passage
    précédent (repris si une source tombe ; son historique est prolongé)."""
    precedent = precedent or {}
    maintenant = dt.datetime.now(dt.timezone.utc)
    out = {"noms": ZONE, "genere_le": maintenant.isoformat(timespec="seconds")}

    tv = _ecarts(_tradingview(journal))
    if len(tv) >= 8 and "fr" in tv:
        out["quotidien"] = {"date": maintenant.date().isoformat(), "heure": maintenant.isoformat(timespec="minutes"),
                            "source": "TradingView, rendement de l'obligation d'État de référence à 10 ans", "source_url": SOURCE_TV,
                            "pays": tv}
    elif precedent.get("quotidien"):
        out["quotidien"] = dict(precedent["quotidien"], reprise=True)
        journal.append("notes et écarts : cotations du jour absentes, reprise du passage précédent")

    mois, irs, exclus = _bce(journal)
    m = _ecarts(irs)
    if mois and len(m) >= 15:
        out["mensuel"] = {"mois": mois, "source": "BCE, taux longs de convergence (moyenne du mois)", "source_url": SOURCE_IRS, "pays": m,
                          "exclus": {c: x for c, x in exclus.items() if c in ZONE}}
    elif precedent.get("mensuel"):
        out["mensuel"] = dict(precedent["mensuel"], reprise=True)

    notes = {}
    for c in ZONE:
        a = ((notations or {}).get(c) or {}).get("agences") or {}
        notes[c] = {ag: {"note": x.get("note"), "perspective": x.get("perspective"), "action": (x.get("action") or "")[:60],
                         "source": "ESMA" if x.get("type") == "ESMA" else x.get("type") or "table vérifiée"}
                    for ag, x in a.items() if x.get("note")}
    out["notes"] = notes
    sans = [c for c in ZONE if not (notes.get(c) or {}).get("moodys")]
    if sans:
        journal.append("notes et écarts : pas de note Moody's pour " + ",".join(sans))

    # Historique des écarts du jour : un point par jour et par pays, un an et demi.
    h = precedent.get("historique") or {"d": [], "s": {}}
    if out.get("quotidien") and not out["quotidien"].get("reprise"):
        d0 = out["quotidien"]["date"]
        if h["d"] and h["d"][-1] == d0:
            i = len(h["d"]) - 1
        else:
            h["d"].append(d0)
            i = len(h["d"]) - 1
            for c in h["s"]:
                h["s"][c].append(None)
        for c, x in out["quotidien"]["pays"].items():
            h["s"].setdefault(c, [None] * len(h["d"]))
            h["s"][c][i] = x["ecart_pb"]
        if len(h["d"]) > 550:
            k = len(h["d"]) - 550
            h["d"] = h["d"][k:]
            h["s"] = {c: v[k:] for c, v in h["s"].items()}
    out["historique"] = h
    return out


if __name__ == "__main__":
    import oblig_pays
    j = []
    r = construire(j, oblig_pays.notations(j))
    print(json.dumps({k: v for k, v in r.items() if k != "historique"}, ensure_ascii=False, indent=1)[:5000])
    print("journal", j)
