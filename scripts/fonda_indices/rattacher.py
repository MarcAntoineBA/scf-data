#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rattacher.py — d'un membre historique d'indice (ISIN, code de place, nom) au
symbole Yahoo de sa cotation, pour lire son cours et ses états.

Ordre : ISIN (cotation de la place de l'indice de préférence), code de place +
suffixe, puis NOM normalisé (les vieux rapports SEC ne donnent que « EXXON MOBIL
CORP ») — nom exact après normalisation, sinon ressemblance ≥ 0,92 ET nettement
devant la suivante. Un rapprochement ambigu n'est pas fait : le membre reste
« non chiffré » et la couverture le dit.
"""
import difflib
import json
import os
import re
import unicodedata

ICI = os.path.dirname(os.path.abspath(__file__))
SUFFIXE = {"epa": ".PA", "ams": ".AS", "ebr": ".BR", "etr": ".DE", "fra": ".F", "lon": ".L", "tyo": ".T",
           "hkg": ".HK", "sha": ".SS", "she": ".SZ", "nse": ".NS", "bom": ".BO", "krx": ".KS", "tpe": ".TW",
           "bvmf": ".SA", "tsx": ".TO", "asx": ".AX", "bit": ".MI", "bme": ".MC", "swx": ".SW", "bmv": ".MX",
           "eli": ".LS", "us": ""}
_FORMES = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|companies|plc|ag|se|sa|nv|spa|ltd|limited|"
                     r"holdings?|group|the|class [a-z]|cl [a-z]|com|common|stock|shs|new|del|de|sab de cv|n v|s a|"
                     r"trust|reit|lp|llc|international|intl)\b")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and "))
    s = _FORMES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


class Rattacheur:
    def __init__(self):
        self.isin = json.load(open(os.path.join(ICI, "isin_de.json")))
        self.nom = json.load(open(os.path.join(ICI, "nom_de.json")))
        self.par_isin = {}
        for k, v in self.isin.items():
            self.par_isin.setdefault(v, []).append(k)
        self.par_nom_norm = {}
        for k, v in self.nom.items():
            self.par_nom_norm.setdefault(norm(v), []).append(k)

    def _pref(self, syms, suffixe):
        """La cotation de la place voulue d'abord ; les cotations américaines
        « sans suffixe » quand le suffixe est vide."""
        def rang(s):
            if suffixe == "":
                return (0 if "." not in s or re.match(r"^[A-Z]+\.[A-Z]$", s) else 1, len(s))
            return (0 if s.endswith(suffixe) else 1, len(s))
        return sorted(syms, key=rang)[0] if syms else None

    def symbole(self, m, place_defaut=None):
        """m : {isin, ticker|code_place, place, nom}. Rend (symbole, méthode) ou (None, None)."""
        place = (m.get("place") or place_defaut or "").lower()
        suf = SUFFIXE.get(place, None)
        if m.get("isin") and m["isin"] in self.par_isin:
            return self._pref(self.par_isin[m["isin"]], suf if suf is not None else ""), "isin"
        code = m.get("code_place") or m.get("ticker")
        if code and suf is not None:
            s = str(code).strip().replace(" ", "")
            for v in (s, s.replace(".", "-"), s.replace("/", "-")):
                sym = v + suf
                if sym in self.nom:
                    return sym, "code"
        if m.get("nom"):
            n = norm(m["nom"])
            if n in self.par_nom_norm:
                return self._pref(self.par_nom_norm[n], suf if suf is not None else ""), "nom"
            # ressemblance, restreinte à la place quand on la connaît
            pool = [k for k in self.par_nom_norm if k]
            best = difflib.get_close_matches(n, pool, n=2, cutoff=0.92)
            if best and (len(best) == 1 or difflib.SequenceMatcher(None, n, best[0]).ratio()
                         - difflib.SequenceMatcher(None, n, best[1]).ratio() >= 0.03):
                syms = self.par_nom_norm[best[0]]
                if suf is not None:
                    syms = [s for s in syms if (s.endswith(suf) if suf else "." not in s)] or []
                if syms:
                    return self._pref(syms, suf if suf is not None else ""), "nom~"
        return None, None
