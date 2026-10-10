#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Le garde-fou de la profondeur des fiches internationales.

Trois trous trouvés le 09/10/2026 sur la fiche de TotalEnergies, puis sur les
plus grosses sociétés européennes — cinq exercices au lieu de vingt, des notes
« non notée » ou « données partielles » :

  [1] dans le cloud, aucun cache de change : les vingt exercices servis par
      TradingView étaient jetés dès qu'il fallait convertir une devise
      (TotalEnergies, Novartis, Unilever, ABB, Equinor… 1 536 sociétés) ;
  [2] les banques, les assureurs et Sanofi ont un chiffre d'affaires de
      définition différente chez les deux fournisseurs : refusés en bloc
      (BNP +205 %, Intesa +106 %, Allianz +25 %, Sanofi −6,6 %) ;
  [3] l'élection de la cotation d'origine donne la victoire au certificat
      américain : Sanofi, ASML, SAP, AstraZeneca, HSBC, BP, UBS… sortaient de
      la collecte ; Shell, Novo Nordisk, Rio Tinto, BBVA, Nokia n'y entraient
      jamais.

Tout est hors ligne : ni la BCE ni TradingView ne sont interrogés.

    python3 test_intl_profondeur.py            les contrôles
    python3 test_intl_profondeur.py --mutants  vérifie que les contrôles MORDENT
"""

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ICI = os.path.dirname(os.path.abspath(__file__))
echecs = []


def v(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, ("" if ok else " — " + detail)))
    if not ok:
        echecs.append(titre)
    return ok


def charger(dossier):
    sys.path.insert(0, dossier)
    for nom in ("change_bce", "fi"):
        sys.modules.pop(nom, None)
    sys.argv = [sys.argv[0]]
    spec = importlib.util.spec_from_file_location(
        "fi", os.path.join(dossier, "fetch_intl_fundamentals.py"))
    fi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fi)
    import change_bce
    return fi, change_bce


# ── Les fixtures ──────────────────────────────────────────────────────────────

def zip_bce():
    """Un `eurofxref-hist.zip` miniature : cinq ans de jours ouvrés, taux fixes."""
    import datetime as dt
    lignes = ["Date,USD,JPY,GBP,CHF,"]
    j = dt.date(2009, 1, 1)
    while j <= dt.date(2013, 12, 31):
        if j.weekday() < 5:
            lignes.append("%s,1.25,125,0.5,N/A," % j.isoformat())
        j += dt.timedelta(days=1)
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        z.writestr("eurofxref-hist.csv", "\n".join(lignes))
    return tampon.getvalue()


def tv_fixture(devise="EUR", ca=(None, None), rn=(100.0, 90.0), actifs=(1000.0, 900.0)):
    """Réponse TradingView : exercices 2011 (commun) et 2010 (à ajouter)."""
    import calendar
    import datetime as dt
    fin = calendar.timegm(dt.date(2011, 12, 31).timetuple())
    return {
        "_symbole_tv": "EURONEXT:XXX", "description": "Xxx",
        "fiscal_period_fy_h": ["2011", "2010"], "fiscal_period_end_fy": fin,
        "currency": devise,
        "total_revenue_fy_h": list(ca), "net_income_fy_h": list(rn),
        "total_assets_fy_h": list(actifs),
    }


def sa_commun(ca, rn, actifs, n=1):
    """Exercices de la source principale, 2011 et au-delà."""
    return [{"annee": 2011 + i, "fin": "%d-12-31" % (2011 + i), "revenue": ca,
             "net_income": rn, "assets": actifs} for i in range(n)]


def controles(dossier):
    fi, bce = charger(dossier)

    print("\n[1] Le change ne dépend plus d'un cache que le cloud n'a pas")
    s = bce.series_depuis_zip(zip_bce())
    v(abs(s.get("EUR", {}).get("2010-06-01", 0) - 1.25) < 1e-9,
      "EUR lu comme dollars par euro", str(s.get("EUR", {}).get("2010-06-01")))
    v(abs(s.get("GBP", {}).get("2010-06-01", 0) - 2.5) < 1e-9,
      "GBP = (dollars par euro) ÷ (livres par euro)", str(s.get("GBP", {}).get("2010-06-01")))
    v("CHF" not in s, "une colonne N/A ne fabrique pas de devise", str(sorted(s)))
    v(abs(s.get("SAR", {}).get("2010-06-01", 0) - 1 / 3.75) < 1e-9,
      "le riyal arrimé au dollar est posé", str(s.get("SAR", {}).get("2010-06-01")))
    bce._memo[0] = s
    fusion = {"GBP": {"2010-06-01": 9.99}}
    bce.completer(fusion)
    v(fusion["GBP"] == {"2010-06-01": 9.99}, "une devise déjà en cache n'est pas écrasée")
    v("EUR" in fusion, "une devise absente du cache est ajoutée")
    # Le chemin réel : charger_fx dans un dossier de cache VIDE (le cloud).
    vide = tempfile.mkdtemp()
    ancien = fi.CACHE_DIR
    try:
        fi.CACHE_DIR = __import__("pathlib").Path(vide)
        fx = fi.charger_fx()
    finally:
        fi.CACHE_DIR = ancien
        shutil.rmtree(vide, ignore_errors=True)
    v(bool(fx.get("EUR")), "charger_fx rend des taux sans aucun cache (cas du cloud)",
      "devises : %s" % sorted(fx))
    lignes, diag = fi.exercices_tradingview(
        tv_fixture("EUR", ca=(500.0, 400.0)), "USD", fx, sa_commun(625.0, 125.0, 1250.0))
    v(len(lignes) == 1 and abs((lignes[0].get("revenue") or 0) - 500.0) < 1e-6,
      "TotalEnergies : un exercice en euros converti en dollars et ajouté",
      "lignes=%s refus=%s" % (lignes, diag.get("refus")))

    print("\n[2] Une banque prouve son identité par le résultat et le bilan")
    # Chiffre d'affaires +200 % (intérêts bruts contre produit net bancaire),
    # résultat net et total du bilan identiques sur trois exercices communs.
    def banque(rn_sa, n=3, actifs_sa=1000.0):
        tv = tv_fixture("EUR", ca=(300.0, 280.0, 260.0, 250.0),
                        rn=(10.0, 10.0, 10.0, 9.0), actifs=(1000.0, 1000.0, 1000.0, 950.0))
        tv["fiscal_period_fy_h"] = ["2013", "2012", "2011", "2010"]
        import calendar
        import datetime as dt
        tv["fiscal_period_end_fy"] = calendar.timegm(dt.date(2013, 12, 31).timetuple())
        sa = [{"annee": a, "fin": "%d-12-31" % a, "revenue": 100.0,
               "net_income": rn_sa, "assets": actifs_sa} for a in (2013, 2012, 2011)][:n]
        return fi.exercices_tradingview(tv, "EUR", {}, sa)
    lignes, diag = banque(10.0)
    v(len(lignes) >= 1 and not diag.get("refus"),
      "BNP : chiffre d'affaires +200 % mais résultat et bilan concordants → accepté",
      str(diag.get("refus")))
    v(all(l.get("revenue") is None for l in lignes),
      "le chiffre d'affaires de définition différente est ÉCARTÉ des lignes ajoutées",
      str([l.get("revenue") for l in lignes]))
    v(all(l.get("net_income") is not None for l in lignes),
      "le résultat net des exercices anciens est gardé")
    lignes, diag = banque(10.7)
    v(not lignes and bool(diag.get("refus")),
      "Santander : résultat net à 7 % → refusé (un seul accord ne suffit pas)")
    lignes, diag = banque(10.0, actifs_sa=1200.0)
    v(not lignes and bool(diag.get("refus")),
      "résultat net concordant mais bilan à 17 % → refusé (les DEUX sont exigés)")
    lignes, diag = banque(10.0, n=2)
    v(not lignes and bool(diag.get("refus")),
      "deux exercices communs seulement → refusé (trois exigés)")

    print("\n[3] La cotation d'origine reprise à un certificat américain")
    reponse = {
        "EURONEXT:SAN": {"isin": "FR0000120578", "type": "stock", "Value.Traded": 1.5e8},
        "XETR:APC": {"isin": "US0378331005", "type": "stock", "Value.Traded": 5e7},
        "TSX:AAPL": {"isin": "CA03785Y1007", "type": "dr", "Value.Traded": 2e7},
        "SIX:NOVN": {"isin": "CH0012005267", "type": "stock", "Value.Traded": 2.5e8},
        "SIX:NOVNEE": {"isin": "CH0038459415", "type": "stock", "Value.Traded": 5.7e7},
        "LSE:WPP": {"isin": "JE00B8KF9B49", "type": "stock", "Value.Traded": 3e7},
    }
    ancien_lot = fi._tv_lot
    fi._tv_lot = lambda tickers, colonnes=None: {t: reponse[t] for t in tickers if t in reponse}
    fi._TV_PLACES[0] = {"PA": "EURONEXT", "DE": "XETR", "TO": "TSX", "SW": "SIX",
                        "L": "LSE", "F": "FWB"}
    try:
        r = fi.cotations_d_origine({"SAN.PA": "SNY", "APC.DE": "AAPL", "AAPL.TO": "AAPL",
                                    "NOVN.SW": "NVS", "NOVNEE.SW": "NVS",
                                    "WPP.L": "WPP", "XYZ.F": "XYZ"})
    finally:
        fi._tv_lot = ancien_lot
        fi._TV_PLACES[0] = None
    v("SAN.PA" in r, "Sanofi (ISIN FR, Paris) reprise", str(r))
    v("APC.DE" not in r, "Apple à Francfort (ISIN US) écartée", str(r))
    v("AAPL.TO" not in r, "certificat canadien d'Apple (type dr) écarté", str(r))
    v("NOVN.SW" in r and "NOVNEE.SW" not in r,
      "Novartis : une seule ligne, la plus échangée", str(r))
    v("WPP.L" in r, "WPP (ISIN Jersey, Londres) reprise", str(r))
    v("XYZ.F" not in r, "une place de reflet n'est jamais une cotation d'origine", str(r))


def controles_ing(dossier):
    sys.path.insert(0, dossier)
    sys.modules.pop("corrections_sources", None)
    import corrections_sources as cs

    print("\n[4] ING : le résultat XBRL faux est remplacé, seulement sur la période prouvée")
    ref = cs.REFERENCE_TV["INGA.AS"]
    officiels = {a: v for a, v, _u in ref["preuves"]}
    ans = list(range(2025, 2013, -1))
    def tv(ecart=0.0, devise="EUR"):
        return {"fiscal_period_fy_h": [str(a) for a in ans], "currency": devise,
                "net_income_fy_h": [officiels.get(a, 4.2e9) * (1 + ecart) for a in ans],
                "earnings_per_share_basic_fy_h": [1.0 for a in ans],
                "earnings_per_share_diluted_fy_h": [1.0 for a in ans]}
    def faux():
        return [{"annee": a, "net_income": 12126e6, "net_income_total": 12426e6,
                 "eps_basic": 3.35, "eps_diluted": 3.35} for a in (2015, 2016, 2022)]
    ex = faux()
    d = cs.corriger("INGA.AS", ex, "EUR", tv())
    e22 = [e for e in ex if e["annee"] == 2022][0]
    v(d.get("applique") and abs(e22["net_income"] - 3674e6) < 1, "2022 : 12 126 M€ remplacé par 3 674 M€ publié",
      str((d, e22["net_income"])))
    v(abs(e22["net_income_total"] - (3674e6 + 300e6)) < 1, "les minoritaires (300 M€) sont conservés",
      str(e22["net_income_total"]))
    v(e22["eps_diluted"] == 1.0, "le BPA suit la référence")
    v(all(e["net_income"] is None for e in ex if e["annee"] < ref["depuis"]),
      "avant %d : vide plutôt que faux" % ref["depuis"], str([(e["annee"], e["net_income"]) for e in ex]))
    ex = faux()
    d = cs.corriger("INGA.AS", ex, "EUR", tv(ecart=0.01))
    v(not d.get("applique") and ex[2]["net_income"] == 12126e6,
      "TradingView à 1 % d'une preuve → rien n'est corrigé", str(d.get("refus")))
    ex = faux()
    d = cs.corriger("INGA.AS", ex, "EUR", tv(devise="USD"))
    v(not d.get("applique"), "devise différente → refus (on ne convertit pas)", str(d))
    v(cs.cle_par_cik("0001039765") == "INGA.AS", "le collecteur SEC retrouve ING par son CIK")
    v(cs.corriger("MC.PA", [], "EUR", tv()) is None, "une société hors registre n'est pas touchée")


def controles_impossibles(dossier):
    sys.path.insert(0, dossier)
    sys.modules.pop("fondamentaux_communs", None)
    import fondamentaux_communs as fc

    print("\n[5] Pas de ratio sur un dénominateur impossible")
    v(fc._marge(-75.1, -62.5) is None, "Altamir : marge sur chiffre d'affaires négatif → vide",
      str(fc._marge(-75.1, -62.5)))
    v(fc._marge(5, 0) is None, "chiffre d'affaires nul → vide")
    v(fc._marge(66.2, 100) == 66.2, "cas ordinaire inchangé", str(fc._marge(66.2, 100)))
    ex = [{"goodwill": 108.2, "assets": 18.4}, {"goodwill": 5.0, "assets": 100.0}]
    fc.effacer_l_impossible(ex)
    v(ex[0]["goodwill"] is None and ex[0]["assets"] is None,
      "MTC : goodwill au-dessus de l'actif → les deux effacés", str(ex[0]))
    v(ex[1]["goodwill"] == 5.0, "goodwill ordinaire conservé")


def controles_secteurs(dossier):
    sys.path.insert(0, dossier)
    for m in ("fetch_secteurs_mondiaux", "fii"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("fii", os.path.join(dossier, "fetch_indices_fiches.py"))
    fii = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fii)

    print("\n[6] Indices : une industrie SIC sans secteur reçoit son secteur")
    v = {"name": "Grupo México", "sector": None, "industry": "Metal Mining"}
    w = fii.avec_secteur(v)
    v_(w.get("sector") == "Materials", "Grupo México (Metal Mining) → Materials", str(w))
    v_(v.get("sector") is None, "la ligne d'origine n'est pas modifiée")
    w = fii.avec_secteur({"sector": None, "industry": "Bottled and Canned Soft Drinks and Carbonated Waters"})
    v_(w.get("sector") == "Consumer Staples", "FEMSA (boissons) → Consumer Staples", str(w))
    w = fii.avec_secteur({"sector": None, "industry": "Other"})
    v_(not w.get("sector"), "« Other » reste non classé : on ne devine pas", str(w))
    w = fii.avec_secteur({"sector": "Energy", "industry": "Metal Mining"})
    v_(w.get("sector") == "Energy", "un secteur déjà donné par la source n'est jamais remplacé")
    src = open(os.path.join(dossier, "fetch_indices_fiches.py"), encoding="utf-8").read()
    v_("avec_secteur(lignes[k])" in src, "le collecteur applique la traduction à chaque membre")


v_ = v


def controles_classes(dossier):
    sys.path.insert(0, dossier)
    sys.modules.pop("fsec", None)
    spec = importlib.util.spec_from_file_location("fsec", os.path.join(dossier, "fetch_sec_fundamentals.py"))
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)

    print("\n[7] Berkshire : la fiche de l'action B est celle de la société, à l'échelle B")
    soeurs = dict(fs.classes_soeurs("1067983", "BRK.A"))
    v(abs(soeurs.get("BRK-B", 0) - 1 / 1500) < 1e-12, "BRK.A → BRK-B au rapport 1/1 500", str(soeurs))
    v("BRK.A" not in soeurs, "une classe n'est pas sa propre sœur")
    v(fs.classes_soeurs("320193", "AAPL") == [], "une société à classe unique n'a pas de sœur")
    ex = fs.convertir_classe([{"eps_diluted": 1500.0, "shares_diluted": 1000.0, "net_income": 9.0}], 1 / 1500)
    v(abs(ex[0]["eps_diluted"] - 1.0) < 1e-9 and abs(ex[0]["shares_diluted"] - 1.5e6) < 1e-3,
      "BPA ÷ 1 500, nombre d'actions × 1 500", str(ex))
    v(ex[0]["net_income"] == 9.0, "les montants de la société ne changent pas")
    r = fs.resume_classe({"cours_natif": 750000.0}, "BRK.A", 1 / 1500)
    v(abs(r["cours_natif"] - 500.0) < 1e-6 and r["classe_de"] == "BRK.A",
      "le cours suit le rapport (P/E inchangé)", str(r))


# ── Les mutants : chaque correction retirée doit faire échouer un contrôle ────

MUTANTS = [
    ("change BCE débranché", "fetch_intl_fundamentals.py",
     "        change_bce.completer(fusion, UA)\n", "        pass\n"),
    ("le change écrase le cache", "change_bce.py",
     "if not fusion.get(dev)", "if True"),
    ("parités fixes oubliées", "change_bce.py",
     "        if serie:\n            out[dev] = serie", "        pass"),
    ("identité par le seul résultat net", "fetch_intl_fundamentals.py",
     "                  and abs(med_ac) <= TV_ECART_RACCORD)", "                  )"),
    ("identité sur un exercice commun", "fetch_intl_fundamentals.py",
     "n_rn >= 3 and n_ac >= 3", "n_rn >= 1 and n_ac >= 1"),
    ("chiffre d'affaires gardé chez les banques", "fetch_intl_fundamentals.py",
     "        retenus = set()\n", "        pass\n"),
    ("certificats acceptés", "fetch_intl_fundamentals.py",
     'if v.get("type") != "stock":', 'if False:'),
    ("ISIN non vérifié", "fetch_intl_fundamentals.py",
     "if isin[:2] not in PAYS_DE_LA_PLACE", "if False and isin[:2] not in PAYS_DE_LA_PLACE"),
    ("deux lignes par société", "fetch_intl_fundamentals.py",
     "        groupe = candidats[sym]\n", "        groupe = sym\n"),
    ("preuves non revérifiées", "corrections_sources.py",
     "abs(v / officiel - 1) > TOLERANCE_PREUVE", "False"),
    ("période prouvée ignorée", "corrections_sources.py",
     'e["annee"] < depuis:', 'e["annee"] < 0:'),
    ("minoritaires perdus", "corrections_sources.py",
     'e["net_income_total"] += rep["net_income"] - rn_avant', 'e["net_income_total"] = rep["net_income"]'),
    ("marge sur chiffre d'affaires négatif", "fondamentaux_communs.py",
     "if not isinstance(ca, (int, float)) or ca <= 0:", "if not isinstance(ca, (int, float)):"),
    ("goodwill au-dessus de l'actif toléré", "fondamentaux_communs.py",
     '("goodwill", "assets", "goodwill au-dessus de l’actif"),', ''),
    ("traduction SIC débranchée", "fetch_indices_fiches.py",
     "        _v = avec_secteur(lignes[k])\n", "        _v = lignes[k]\n"),
    ("table SIC amputée", "fetch_secteurs_mondiaux.py",
     '    "Bottled and Canned Soft Drinks and Carbonated Waters": "Consumer Staples",\n', ""),
    ("nombre d'actions non converti", "fetch_sec_fundamentals.py",
     "                e[k] = e[k] / facteur", "                pass"),
    ("cours de la sœur non converti", "fetch_sec_fundamentals.py",
     '        r["cours_natif"] = r["cours_natif"] * facteur', '        pass'),
    ("devise non contrôlée", "corrections_sources.py",
     'if _devise(tv.get("currency")) != devise:', 'if False:'),
]


def mutants():
    tues = 0
    for nom, fichier, avant, apres in MUTANTS:
        tmp = tempfile.mkdtemp()
        try:
            for f in ("fetch_intl_fundamentals.py", "change_bce.py", "fetch_stock_logos_tv.py",
                      os.path.basename(__file__)):
                if os.path.exists(os.path.join(ICI, f)):
                    shutil.copy(os.path.join(ICI, f), tmp)
            for f in os.listdir(ICI):
                if f.endswith(".py") and not os.path.exists(os.path.join(tmp, f)):
                    shutil.copy(os.path.join(ICI, f), tmp)
            p = os.path.join(tmp, fichier)
            src = open(p, encoding="utf-8").read()
            if src.count(avant) != 1:
                print("  ? %s — motif introuvable (%d occurrence(s))" % (nom, src.count(avant)))
                continue
            open(p, "w", encoding="utf-8").write(src.replace(avant, apres))
            r = subprocess.run([sys.executable, os.path.join(tmp, os.path.basename(__file__))],
                               capture_output=True, text=True, cwd=tmp)
            tue = r.returncode != 0
            tues += tue
            print("  %s mutant « %s » %s" % ("✓" if tue else "✗", nom,
                                              "tué" if tue else "SURVIT"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return tues, len(MUTANTS)


if __name__ == "__main__":
    if "--mutants" in sys.argv:
        t, n = mutants()
        print("\n%d/%d mutants tués" % (t, n))
        sys.exit(0 if t == n else 1)
    controles(ICI)
    controles_ing(ICI)
    controles_impossibles(ICI)
    controles_secteurs(ICI)
    controles_classes(ICI)
    print("\n%s — %d échec(s)" % ("OK" if not echecs else "ÉCHEC", len(echecs)))
    sys.exit(1 if echecs else 0)
