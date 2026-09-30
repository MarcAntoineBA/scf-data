#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repli_sec.py — la route de REPLI du S&P 500 : un membre que la cotation actuelle
ne chiffre pas (société rachetée, radiée) est lu à la SEC par son CIK, et son cours
de décembre est celui de l'avoir du fonds (valeur / titres détenus), sans Yahoo.

Rendement bénéficiaire = BPA publié la première fois / cours de l'avoir ; les
autres grandeurs rapportées au nombre d'actions dilué du MÊME dépôt.
⚠ Division entre décembre et le dépôt du 10-K (Walmart 3 pour 1 le 26/02/2024,
10-K du 15/03) : le BPA est déjà divisé, le cours de décembre ne l'est pas. Si le
nombre d'actions saute d'un facteur de division d'un exercice à l'autre, on
regarde les titres détenus par le fonds au trimestre SUIVANT : s'ils ont sauté du
même facteur, la division est postérieure à décembre → on la défait ; s'ils n'ont
pas bougé, elle est antérieure → rien à faire ; sinon le membre est écarté.
"""
import sec_natif as SN

RATIOS = (1.5, 2, 3, 4, 5, 7, 10, 20)


class ReplisSEC:
    def __init__(self, annuaire, suivant):
        self.an = annuaire
        self.suivant = suivant          # {mois: {nom: titres à la date suivante}}
        self.memo_cik = {}
        self.stats = {"repli": 0, "div_ajustee": 0, "div_ecartee": 0, "sans_cik": 0}

    @staticmethod
    def actions(x, m):
        """Nombre d'actions dilué du dépôt, contrôlé. ⚠ Certains dépôts XBRL donnent
        les actions EN MILLIONS (Titanium Metals 2008 : « 182,5 ») — rendement lu à
        100 000 %. On croise avec bénéfice / BPA, puis avec la part détenue par le
        fonds (IVV n'a jamais détenu plus de 5 % d'une société)."""
        sh = x.get("sh")
        impl = x["ni"] / x["eps"] if x.get("eps") and x.get("ni") and (x["ni"] > 0) == (x["eps"] > 0) else None
        if sh and impl and not (0.7 <= sh / impl <= 1.4):
            sh = impl
        sh = sh or impl
        if not sh or sh <= 0:
            return None
        t = m.get("_titres")
        if t:
            for _ in range(2):
                if t / sh > 0.05:
                    sh *= 1000
            if not (1e-6 <= t / sh <= 0.05):
                return None
        return sh

    def berkshire(self, m, mois):
        """Berkshire Hathaway : BPA publié PAR ACTION A ; le fonds détient des B.
        Une A vaut 1 500 B depuis la division des B du 21/01/2010 (30 avant)."""
        ex = SN.exercices(1067983)["ex"]
        x = SN.exercice_de(ex, int(mois[:4]))
        if not x or not x.get("ni"):
            return None
        if not x.get("sh") and mois >= "2010-01":
            # Depuis 2015, Berkshire ne dépose plus son nombre moyen d'actions hors
            # classes : BPA par action B (TradingView, base actuelle = celle des B
            # depuis la division de 2010) rapporté au cours B de l'avoir.
            import tv_hist as TV
            t = TV.charger(["BRK-B"]).get("BRK-B") or {}
            e = {z["an"]: z for z in TV.exercices(t)}.get(int(mois[:4])) if t.get("_tv") else None
            if not e or not e.get("eps"):
                return None
            sh_b = x["ni"] / e["eps"]
            P = m["_prix"]
            out = {"fin": x["fin"], "route": "sec_cik", "ni": e["eps"] / P, "div": 0.0}
            for a in ("rev", "eq"):
                out[a] = x[a] / sh_b / P if x.get(a) is not None else None
            self.stats["repli"] += 1
            return out
        if not x.get("sh"):
            return None
        # Berkshire ne balise pas de BPA annuel ; le nombre moyen d'actions « équivalent
        # A » (≈ 1,6 million) est parfois déposé en millions de fois trop (2010-2011).
        sh_a = x["sh"] / 1e6 if x["sh"] > 1e9 else x["sh"]
        if not (1e6 <= sh_a <= 2e6):
            return None
        pa = m["_prix"] * (1500 if mois >= "2010-01" else 30)
        out = {"fin": x["fin"], "route": "sec_cik", "ni": x["ni"] / sh_a / pa}
        for a in ("rev", "div", "eq"):
            out[a] = x[a] / sh_a / pa if x.get(a) is not None else (0.0 if a == "div" else None)
        self.stats["repli"] += 1
        return out

    def ciks(self, nom):
        if nom not in self.memo_cik:
            self.memo_cik[nom] = self.an.ciks(nom)
        return self.memo_cik[nom]

    def __call__(self, m, mois):
        if not m.get("_prix"):
            return None
        if "berkshire" in (m.get("nom") or "").lower():
            return self.berkshire(m, mois)
        for c in self.ciks(m["nom"]):
            ex = SN.exercices(c)["ex"]
            x = SN.exercice_de(ex, int(mois[:4]))
            if not x or x.get("ni") is None:
                continue
            sh = self.actions(x, m)
            if not sh:
                self.stats["actions_douteuses"] = self.stats.get("actions_douteuses", 0) + 1
                continue
            k = 1.0
            if SN.division_suspecte(ex, x):
                i = ex.index(x)
                r = x["sh"] / ex[i - 1]["sh"]
                rr = next((q for q in RATIOS + tuple(1 / q for q in RATIOS) if abs(r / q - 1) < 0.06), None)
                if rr:
                    t2 = (self.suivant.get(mois) or {}).get(m["nom"])
                    if not t2:
                        self.stats["div_ecartee"] += 1
                        return None
                    q = t2 / m["_titres"]
                    if abs(q / rr - 1) < 0.1:
                        k = rr
                        self.stats["div_ajustee"] += 1
                    elif abs(q - 1) > 0.25:
                        self.stats["div_ecartee"] += 1
                        return None
            P = m["_prix"]
            out = {"fin": x["fin"], "route": "sec_cik",
                   "ni": (x["eps"] * k / P) if x.get("eps") is not None else x["ni"] * k / sh / P}
            for a in ("rev", "div", "eq"):
                out[a] = x[a] * k / sh / P if x.get(a) is not None else (0.0 if a == "div" else None)
            self.stats["repli"] += 1
            return out
        self.stats["sans_cik"] += 1
        return None
