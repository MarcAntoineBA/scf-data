#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fonda_annuel.py — les exercices annuels d'une société en MONTANTS ABSOLUS, et sa
capitalisation à n'importe quel mois, pour rapporter chaque grandeur au prix.

⚠ POURQUOI PAS LE BPA TEL QUEL (mesuré le 30/09/2026) : les états SEC sont sur des
bases d'actions MÊLÉES — chaque chiffre porte la base du rapport d'où il vient.
Apple 2019 est ramené à la division de 2020 (BPA 3,17 $), Apple 2015 ne l'est pas
(9,42 $) : rapporté au cours de l'époque, le P/E serait faux d'un facteur 4. On
prend donc le bénéfice (montant), et une capitalisation à la date :
    capitalisation(m) = actions (base ACTUELLE) × cours ajusté des divisions (m)
Le nombre d'actions de chaque exercice est ramené à la base actuelle par le
produit des divisions postérieures qui le RACCORDE à l'exercice suivant (la base
d'un chiffre est celle de son rapport source, inconnue : on choisit le facteur
qui rend la série continue). Contrôle : capitalisation publiée par le fournisseur,
quand elle existe (depuis 2021).
"""
import glob
import json
import math
import os
from datetime import date

import cours_divisions as CD


def _j(s):
    return date.fromisoformat(str(s)[:10])


class Univers:
    def __init__(self, dossier):
        self.intl, self.sec = {}, {}
        for f in glob.glob(os.path.join(dossier, "intl_detail_*.json")):
            self.intl.update(json.load(open(f))["societes"])
        for f in glob.glob(os.path.join(dossier, "sec_detail_*.json")):
            for k, v in json.load(open(f))["societes"].items():
                self.sec[k] = v
                self.sec[k.replace(".", "-")] = v

    def resoudre(self, sym):
        """Le symbole dont on a les ÉTATS pour une ligne cotée : lui-même, sinon une
        autre cotation de même ISIN (Airbus : Francfort → Paris), sinon une autre
        ligne de même raison sociale (Alphabet C → A)."""
        if not hasattr(self, "_isin"):
            ici = os.path.dirname(os.path.abspath(__file__))
            self._isin = json.load(open(os.path.join(ici, "isin_de.json")))
            self._nom = json.load(open(os.path.join(ici, "nom_de.json")))
            self._par_isin, self._par_nom = {}, {}
            for k, v in self._isin.items():
                self._par_isin.setdefault(v, []).append(k)
            for k, v in self._nom.items():
                self._par_nom.setdefault(v, []).append(k)
        a_etats = lambda k: k in self.intl or k in self.sec
        if a_etats(sym):
            return sym
        for k in self._par_isin.get(self._isin.get(sym), []):
            if a_etats(k):
                return k
        for k in self._par_nom.get(self._nom.get(sym), []):
            if a_etats(k):
                return k
        return None

    def exercices(self, sym):
        """[{fin, ni, rev, div, eq, eps, sh_brut, mcap_publie, devise}] triés, fusion
        intl (fournisseurs qui ajustent) prioritaire, SEC pour les années antérieures."""
        par_fin = {}
        dev = None
        for src, rec in (("sec", self.sec.get(sym) or self.sec.get(sym.replace("-", "."))),
                         ("intl", self.intl.get(sym) or self.intl.get(sym.replace("-", ".")))):
            if not rec:
                continue
            d = ((rec.get("resume") or {}).get("devise")) or ("USD" if src == "sec" else None)
            dev = dev if src == "sec" and dev else d
            for e in rec.get("exercices") or []:
                if not e.get("fin"):
                    continue
                cle = e["fin"][:7]
                # Bénéfice des ACTIONNAIRES ORDINAIRES (après dividendes des préférentielles) :
                # c'est celui du BPA publié. Les banques aidées en 2009 versaient des
                # dividendes préférentiels énormes (Bank of America : 8,5 Md$), et le
                # bénéfice total gonflait le S&P 500 de 2009 de plusieurs pour cent.
                ni = e.get("net_income_common") if e.get("net_income_common") is not None else e.get("net_income")
                x = {"fin": e["fin"][:10], "ni": ni, "rev": e.get("revenue"),
                     # dividendes des actionnaires ORDINAIRES : la ligne « dividendes versés »
                     # des dépôts SEC inclut les préférentielles (JPMorgan 2023 : 13,5 Md$
                     # dont 1,5 aux préférentielles) — rendement du S&P 500 lu +2 à +12 %.
                     "div": (max(0.0, abs(e["dividends_paid"]) - abs(e.get("dividends_paid_preferred") or 0))
                             if e.get("dividends_paid") is not None else None),
                     "dps": e.get("dps"), "eq": e.get("equity_part_groupe") or e.get("equity"),
                     "eps": e.get("eps_diluted"), "sh_brut": e.get("shares_diluted"),
                     "mcap_publie": e.get("mcap_publie"), "src": src, "devise": d}
                # intl écrase sec pour un même exercice (même mois de clôture)
                if src == "intl" or cle not in par_fin:
                    base = par_fin.get(cle, {})
                    base.update({k: v for k, v in x.items() if v is not None})
                    par_fin[cle] = base
        return sorted(par_fin.values(), key=lambda x: x["fin"]), dev

    def normaliser(self, ex, divisions):
        """Pose ex[i]['sh'] : actions sur la base ACTUELLE."""
        # nombre d'actions brut : déclaré, sinon impliqué par bénéfice / BPA
        for x in ex:
            sh = x.get("sh_brut")
            if not sh and x.get("ni") and x.get("eps") and x["eps"] != 0 and (x["ni"] > 0) == (x["eps"] > 0):
                sh = x["ni"] / x["eps"]
            x["sh_raw"] = sh if sh and sh > 0 else None
        prec = None
        for x in reversed(ex):
            r = x["sh_raw"]
            if not r:
                x["sh"] = None
                continue
            # facteurs candidats : produits des divisions POSTÉRIEURES à la clôture,
            # de la plus récente vers la plus ancienne (base du rapport source)
            apres = [f for d, f in divisions if d > x["fin"]]
            cands, p = [1.0], 1.0
            for f in reversed(apres):
                p *= f
                cands.append(p)
            if prec is None:
                # le dernier exercice : un fournisseur qui ajuste est déjà à la base
                # actuelle (1) ; sinon on ne sait pas, on prend 1 et le contrôle dira.
                k = 1.0
            else:
                k = min(cands, key=lambda c: abs(math.log(r * c / prec)))
            x["sh"] = r * k
            x["facteur"] = k
            prec = x["sh"]
        return ex

    def mcap(self, ex, prix, mois):
        """Capitalisation au mois : actions de l'exercice clos au plus tard le mois
        (le plus récent, < 18 mois) × cours ajusté du mois. Devise de COTATION."""
        p = prix.get(mois)
        if not p:
            return None
        # ⚠ « ≤ AAAA-MM-31 » (comparaison de chaînes) : « -28 » écartait les clôtures
        # du 31 décembre, et la première année d'un titre n'avait pas de capitalisation.
        fin_mois = mois + "-31"
        x = next((e for e in reversed(ex) if e["fin"] <= fin_mois and e.get("sh")), None)
        if not x or (_j(mois + "-28") - _j(x["fin"])).days > 548:
            return None
        return x["sh"] * p


def controle(u, sym):
    """Capitalisation calculée à la clôture de chaque exercice contre la publiée."""
    c = CD.cours(sym)
    if not c:
        return None
    ex, dev = u.exercices(sym)
    u.normaliser(ex, c["divisions"])
    out = []
    for x in ex:
        if x.get("mcap_publie") and x.get("sh"):
            m = u.mcap(ex, c["mois"], x["fin"][:7])
            if m:
                out.append((x["fin"][:7], round(m / x["mcap_publie"], 3)))
    return out
