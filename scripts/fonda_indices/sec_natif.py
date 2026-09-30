#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sec_natif.py — les exercices d'une société AMÉRICAINE disparue (rachetée, radiée)
ou renommée, lus directement dans l'API XBRL de la SEC par son CIK.

Pourquoi : les états de l'univers (sec_detail) ne couvrent que les sociétés
cotées AUJOURD'HUI. Un S&P 500 de 2012 compte des dizaines de membres disparus
depuis (Celgene, Monsanto, Time Warner, EMC…) : sans eux, la couverture tombe
sous le seuil et l'année n'est pas publiée.

Du nom de l'avoir (« Celgene Corp. ») au CIK : la liste EDGAR de TOUS les noms
déposés (cik-lookup-data.txt, anciens noms compris), nom normalisé exact, sinon
les mêmes mots dans un autre ordre (« DU PONT E I DE NEMOURS »). Plusieurs CIK
pour un nom : celui qui a déposé des 10-K avec un bénéfice sur l'année voulue.

⚠ BASE D'ACTIONS : chaque grandeur par action est prise dans le dépôt qui l'a
PUBLIÉE EN PREMIER (le 10-K de l'exercice), donc sur la base d'actions de la date
de ce dépôt — deux mois environ après la clôture. Le cours de décembre lu dans
l'avoir du fonds est sur la base de décembre. Une division d'actions tombée entre
les deux fausserait le rapport d'un facteur 2 ou 3 : `division_suspecte` la
cherche (nombre d'actions qui saute d'un facteur ≥ 1,4 d'un exercice à l'autre)
et l'appelant écarte le membre plutôt que de deviner.
"""
import json
import os
import re
import time
import urllib.request

import rattacher as RT

ICI = os.path.dirname(os.path.abspath(__file__))
DOS = os.path.join(ICI, "sec_nat")
UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")}
FORMES_ANNUELLES = ("10-K", "10-K/A", "10-K405", "10-KT", "20-F", "40-F")
T_NI = ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss")
T_REV = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
         "RevenuesNetOfInterestExpense", "SalesRevenueGoodsNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
         "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense")
T_DIV = ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends", "PaymentsOfOrdinaryDividends")
T_EQ = ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
T_EPS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")
T_SH = ("WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasic")
_FORMES_EXTRA = re.compile(r"\b(nvs|cl|class|a|b|c|reit|ltd|the)\b")


def cle_nom(s):
    n = _FORMES_EXTRA.sub(" ", RT.norm(s))
    return re.sub(r"\s+", " ", n).strip()


def cle_jetons(s):
    return " ".join(sorted(cle_nom(s).split()))


class Annuaire:
    """Nom (actuel ou ancien) → CIK, d'après la liste EDGAR complète."""
    def __init__(self):
        self.nom, self.jetons = {}, {}
        for ligne in open(os.path.join(DOS, "cik-lookup-data.txt"), encoding="latin-1"):
            p = ligne.rstrip("\n").rsplit(":", 2)
            if len(p) < 3 or not p[0]:
                continue
            cik = int(p[1])
            self.nom.setdefault(cle_nom(p[0]), set()).add(cik)
            self.jetons.setdefault(cle_jetons(p[0]), set()).add(cik)

    def ciks(self, nom):
        k = cle_nom(nom)
        if k and k in self.nom:
            return sorted(self.nom[k])
        j = cle_jetons(nom)
        if j and j in self.jetons:
            return sorted(self.jetons[j])
        return []


def _get(url, essais=4):
    for e in range(essais):
        try:
            time.sleep(0.12)  # ≤ 10 requêtes/s (règle de la SEC)
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as x:
            if x.code == 404:
                return None
            time.sleep(2 * (e + 1))
        except Exception:
            time.sleep(2 * (e + 1))
    return None


def _jours(a, b):
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _premiers(faits, duree):
    """{fin: (valeur, déposé_le)} : la PREMIÈRE publication annuelle de chaque période."""
    out = {}
    for f in faits:
        if f.get("form") not in FORMES_ANNUELLES or "end" not in f:
            continue
        if duree:
            if "start" not in f or not (330 <= _jours(f["start"], f["end"]) <= 380):
                continue
        if f["end"] not in out or f["filed"] < out[f["end"]][1]:
            out[f["end"]] = (f["val"], f["filed"])
    return out


def exercices(cik):
    """[{fin, ni, rev, div, eq, eps, sh, depose}] — valeurs telles que publiées la
    première fois (cache disque : on ne garde que l'extrait)."""
    os.makedirs(os.path.join(DOS, "ex"), exist_ok=True)
    f = os.path.join(DOS, "ex", "%010d.json" % cik)
    if os.path.exists(f):
        return json.load(open(f))
    d = _get("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % cik)
    gaap = ((d or {}).get("facts") or {}).get("us-gaap") or {}

    def serie(tags, duree, unite):
        """Première balise qui a des valeurs, période par période (une société
        change de balise avec les normes : SalesRevenueNet → RevenueFromContract…)."""
        out = {}
        for t in tags:
            u = (gaap.get(t) or {}).get("units") or {}
            vals = u.get(unite) or (next(iter(u.values())) if u and unite is None else None)
            if not vals:
                continue
            for fin, v in _premiers(vals, duree).items():
                out.setdefault(fin, v)
        return out
    ni = serie(T_NI, True, "USD")
    rev = serie(T_REV, True, "USD")
    if not rev:
        nii, nonii = serie(("InterestIncomeExpenseNet",), True, "USD"), serie(("NoninterestIncome",), True, "USD")
        rev = {k: (nii[k][0] + nonii[k][0], nii[k][1]) for k in nii if k in nonii}
    div = serie(T_DIV, True, "USD")
    eq = serie(T_EQ, False, "USD")
    eps = serie(T_EPS, True, "USD/shares")
    sh = serie(T_SH, True, "shares")
    out = []
    for fin in sorted(ni):
        out.append({"fin": fin, "ni": ni[fin][0], "depose": ni[fin][1],
                    "rev": rev.get(fin, (None,))[0], "div": abs(div[fin][0]) if fin in div else None,
                    "eq": eq.get(fin, (None,))[0], "eps": eps.get(fin, (None,))[0], "sh": sh.get(fin, (None,))[0],
                    "sh_depose": sh.get(fin, (None, None))[1], "eps_depose": eps.get(fin, (None, None))[1]})
    json.dump({"nom": (d or {}).get("entityName"), "ex": out}, open(f, "w"))
    return {"nom": (d or {}).get("entityName"), "ex": out}


def exercice_de(ex, y):
    """L'exercice de l'année civile Y : clos de juin Y à mai Y+1 (règle du moteur)."""
    c = [e for e in ex if "%d-06-01" % y <= e["fin"] <= "%d-05-31" % (y + 1)]
    return c[-1] if c else None


def division_suspecte(ex, x):
    """Le nombre d'actions saute d'un facteur ≥ 1,4 (ou ≤ 1/1,4) par rapport à
    l'exercice précédent : une division (ou un regroupement) a pu tomber entre la
    clôture et le dépôt. On ne devine pas le côté."""
    i = ex.index(x)
    if i == 0 or not x.get("sh") or not ex[i - 1].get("sh"):
        return False
    r = x["sh"] / ex[i - 1]["sh"]
    return r >= 1.4 or r <= 1 / 1.4
