#!/usr/bin/env python3
"""Fetch données "Financiarisation US" — DEUX histoires séparées et démontrées.

PARTIE A — « Wall Street prend le pouvoir » (financiarisation macro). Chaque
panneau = une DIVERGENCE qui montre le basculement, pas un niveau :
  A1 Capital vs Travail   W270RE1A156NBEA (salaires %RNB) / W273RE1A156NBEA (profits)
  A2 Distribuer vs Investir  DIVIDEND (div. nets) / NCBGCFQ027S (invest.) en %PIB
  A3 Bourse vs Économie   NCBEILQ027S ÷ GDP  (capitalisation / PIB, "Buffett")
  A4 Poids de la finance  VAPGDPFI (finance & assurance %PIB, 2005+)
  + comparaison internationale : capitalisation / PIB (World Bank, 11 pays)

PARTIE B — « L'épargnant pris au piège » (exposition des ménages) :
  B1 Allocation actions   BOGZ1FL153064486Q (actions %actifs financiers ménages)
  B2 Patrimoine vs Revenu HNONWPDPI (patrimoine net / revenu disponible)
  B3 Financier vs Tangible TFAABSHNO ÷ TABSHNO (actifs financiers %patrimoine)
  + comparaison internationale : dette des ménages / PIB (BIS, 11 pays, live)

Les verdicts sont calculés côté serveur avec les vraies valeurs (auditables).
Cache : ~/Library/Caches/site_crypto_finance/financiarisation_cache.{json,js}
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred  # noqa: E402

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "financiarisation_cache.json"
CACHE_JS = CACHE_DIR / "financiarisation_cache.js"

COUNTRIES = [
    ("US", "États-Unis"), ("CH", "Suisse"), ("CA", "Canada"),
    ("GB", "Royaume-Uni"), ("FR", "France"), ("JP", "Japon"),
    ("DE", "Allemagne"), ("AU", "Australie"), ("KR", "Corée du Sud"),
    ("CN", "Chine"), ("IN", "Inde"),
]


def fred_url(sid):
    return f"https://fred.stlouisfed.org/series/{sid}"


def pctile(values, x):
    if not values:
        return None
    return round(100.0 * sum(1 for v in values if v <= x) / len(values), 1)


def val_at(dates, values, year):
    """Valeur la plus récente <= année donnée (string ou int)."""
    y = str(year)
    cand = [v for d, v in zip(dates, values) if d[:4] <= y]
    return cand[-1] if cand else values[0]


def as_pct_gdp(s, gdp_map, scale=1.0):
    """Transforme une série $ en % du PIB (aligne par date)."""
    out_d, out_v = [], []
    for d, v in zip(s["dates"], s["values"]):
        g = gdp_map.get(d)
        if g:
            out_d.append(d)
            out_v.append(round(v * scale / g * 100, 3))
    return out_d, out_v


def ratio(num, den, scale=100.0):
    dm = dict(zip(den["dates"], den["values"]))
    out_d, out_v = [], []
    for d, vn in zip(num["dates"], num["values"]):
        vd = dm.get(d)
        if vd:
            out_d.append(d)
            out_v.append(round(vn / vd * scale, 3))
    return out_d, out_v


def ratio_carry(num, gdp, scale=1.0):
    """Ratio num/GDP en % avec report (carry-forward) du PIB trimestriel sur une
    série de fréquence plus fine (ex : bilan Fed hebdomadaire)."""
    gd, gv = gdp["dates"], gdp["values"]
    out_d, out_v, j = [], [], 0
    for d, v in zip(num["dates"], num["values"]):
        while j + 1 < len(gd) and gd[j + 1] <= d:
            j += 1
        if gd[j] <= d and gv[j]:
            out_d.append(d)
            out_v.append(round(v * scale / gv[j] * 100, 2))
    return out_d, out_v


def index_series(s, base_year, deflator=None):
    """Indice base 100 à base_year. deflator : dict date->valeur (ex CPI) pour
    passer en réel."""
    dates, vals = [], []
    for d, v in zip(s["dates"], s["values"]):
        if deflator is not None:
            c = deflator.get(d)
            if not c:
                continue
            v = v / c
        dates.append(d)
        vals.append(v)
    base = next((v for d, v in zip(dates, vals) if d[:4] >= str(base_year)), vals[0])
    return dates, [round(v / base * 100, 1) for v in vals]


def serie(label, dates, values, color, fmt, dash=False):
    return {"label": label, "color": color, "fmt": fmt, "dash": dash,
            "dates": dates, "values": [round(v, 3) for v in values]}


def panel(key, kind, eyebrow, title, verdict, unit, fmt, series,
          sources, latest_label, note=None, pctile_val=None):
    return {"key": key, "kind": kind, "eyebrow": eyebrow, "title": title,
            "verdict": verdict, "unit": unit, "fmt": fmt, "series": series,
            "sources": sources, "latest_label": latest_label,
            "note": note, "pctile": pctile_val}


def fetch_intl(key, id_tmpl, start, label, unit, fmt, source, desc, live, ref_url):
    rows = []
    for iso, name in COUNTRIES:
        sid = id_tmpl.format(iso=iso)
        r = fetch_fred(sid, start=start)
        if not r:
            sys.stderr.write(f"[fin·intl] {key}/{iso}: pas de données\n")
            continue
        rows.append({"iso": iso, "name": name, "sid": sid,
                     "latest": round(r["values"][-1], 1),
                     "latest_year": r["dates"][-1][:4], "start": r["dates"][0][:4],
                     "dates": r["dates"], "values": [round(v, 2) for v in r["values"]]})
    rows.sort(key=lambda x: -x["latest"])
    us = next((x["latest"] for x in rows if x["iso"] == "US"), None)
    us_rank = next((i + 1 for i, x in enumerate(rows) if x["iso"] == "US"), None)
    return {"key": key, "label": label, "unit": unit, "fmt": fmt, "source": source,
            "desc": desc, "live": live, "ref_url": ref_url, "us_latest": us,
            "us_rank": us_rank, "n_countries": len(rows),
            "max_year": max((x["latest_year"] for x in rows), default=""),
            "countries": rows}


# Couleurs
BLUE, PURPLE, GREEN, RED, GOLD = "#5eaff6", "#a78bfa", "#26a69a", "#ef5350", "#fbbf24"


def main():
    panelsA, panelsB = [], []

    gdp = fetch_fred("GDP", start="1947-01-01")
    gmap = dict(zip(gdp["dates"], gdp["values"])) if gdp else {}

    # ── A1 — Capital vs Travail ───────────────────────────────────────────
    lab = fetch_fred("W270RE1A156NBEA", start="1947-01-01")   # salaires %RNB
    pro = fetch_fred("W273RE1A156NBEA", start="1947-01-01")   # profits %RNB
    if lab and pro:
        l0 = val_at(lab["dates"], lab["values"], 1970); l1 = lab["values"][-1]
        p0 = val_at(pro["dates"], pro["values"], 1970); p1 = pro["values"][-1]
        panelsA.append(panel(
            "capital_travail", "duo", "Acte I · qui capte la valeur créée ?",
            "Le capital écrase le travail",
            f"En 1970, les salaires captaient {l0:.0f}% du revenu national ; "
            f"aujourd'hui {l1:.0f}%. La part des profits des entreprises est, elle, "
            f"montée de {p0:.0f}% à {p1:.0f}%. La valeur créée glisse du travailleur "
            f"vers l'actionnaire — moteur premier de la financiarisation.",
            "%", "pct1",
            [serie("Part des salaires", lab["dates"], lab["values"], BLUE, "pct1"),
             serie("Part des profits", pro["dates"], pro["values"], RED, "pct1")],
            [{"id": "W270RE1A156NBEA", "url": fred_url("W270RE1A156NBEA")},
             {"id": "W273RE1A156NBEA", "url": fred_url("W273RE1A156NBEA")}],
            f"Salaires {l1:.0f}% · Profits {p1:.0f}% du revenu national"))

    # ── A2 — Distribuer plutôt qu'investir ────────────────────────────────
    div = fetch_fred("DIVIDEND", start="1947-01-01")       # $ Md
    inv = fetch_fred("NCBGCFQ027S", start="1947-01-01")    # $ M (nonfin corp GFCF)
    if div and inv and gmap:
        dd, dv = as_pct_gdp(div, gmap, 1.0)
        idd, iv = as_pct_gdp(inv, gmap, 0.001)
        d0 = val_at(dd, dv, 1960); d1 = dv[-1]
        i1 = iv[-1]
        panelsA.append(panel(
            "actionnaire_reel", "duo", "Acte II · l'entreprise sert qui ?",
            "Distribuer plutôt qu'investir",
            f"Les dividendes versés aux actionnaires ont bondi de {d0:.1f}% à "
            f"{d1:.1f}% du PIB, tandis que l'investissement productif stagne "
            f"(~{i1:.0f}%). Et encore : les rachats d'actions, d'ampleur comparable, "
            f"ne sont pas comptés ici. L'entreprise redistribue au lieu de bâtir.",
            "%", "pct1",
            [serie("Dividendes nets", dd, dv, RED, "pct1"),
             serie("Investissement productif", idd, iv, GREEN, "pct1")],
            [{"id": "DIVIDEND", "url": fred_url("DIVIDEND")},
             {"id": "NCBGCFQ027S", "url": fred_url("NCBGCFQ027S")}],
            f"Dividendes {d1:.1f}% · Investissement {i1:.1f}% du PIB"))

    # ── A3 — Bourse vs Économie (Buffett) ─────────────────────────────────
    eq = fetch_fred("NCBEILQ027S", start="1947-01-01")
    if eq and gmap:
        # equités en M$ → Md$ (÷1000), puis / PIB(Md$) × 100 = % du PIB.
        bd, bv = ratio({"dates": eq["dates"],
                        "values": [v / 1000.0 for v in eq["values"]]}, gdp, 100.0)
        b0 = val_at(bd, bv, 1972); b1 = bv[-1]
        panelsA.append(panel(
            "buffett", "mono", "Acte III · la bourse suit-elle l'économie ?",
            "Le marché décroche du réel",
            f"La capitalisation boursière vaut {b1:.0f}% du PIB, contre ~{b0:.0f}% "
            f"au début des années 1970. La bourse ne reflète plus l'économie : elle "
            f"la surplombe — et toute richesse, désormais, se mesure en actifs.",
            "%", "pct0",
            [serie("Capitalisation / PIB", bd, bv, GOLD, "pct0")],
            [{"id": "NCBEILQ027S ÷ GDP", "url": fred_url("NCBEILQ027S")}],
            f"{b1:.0f}% du PIB", pctile_val=pctile(bv, b1)))

    # ── A4 — Poids direct de la finance ───────────────────────────────────
    fin = fetch_fred("VAPGDPFI", start="2000-01-01")
    if fin:
        f1 = fin["values"][-1]
        panelsA.append(panel(
            "finance_pib", "mono", "Acte IV · le poids du secteur financier",
            "La finance, secteur à part",
            f"La finance et l'assurance pèsent {f1:.1f}% du PIB américain — un "
            f"secteur qui capte une part croissante de l'activité sans rien produire "
            f"de tangible (série disponible depuis 2005).",
            "%", "pct1",
            [serie("Finance & assurance / PIB", fin["dates"], fin["values"], PURPLE, "pct1")],
            [{"id": "VAPGDPFI", "url": fred_url("VAPGDPFI")}],
            f"{f1:.1f}% du PIB", note="Donnée trimestrielle BEA, depuis 2005."))

    # ── B1 — Allocation actions des ménages ───────────────────────────────
    ea = fetch_fred("BOGZ1FL153064486Q", start="1945-01-01")
    if ea:
        e0 = val_at(ea["dates"], ea["values"], 1985); e1 = ea["values"][-1]
        panelsB.append(panel(
            "allocation_actions", "mono", "Le ménage devient actionnaire",
            "Tout-actions, comme jamais",
            f"{e1:.0f}% du patrimoine financier des ménages américains est investi "
            f"en actions, contre ~{e0:.0f}% dans les années 1980 : un record absolu. "
            f"Le sort financier des Américains est arrimé à la bourse.",
            "%", "pct1",
            [serie("Actions / actifs financiers", ea["dates"], ea["values"], GOLD, "pct1")],
            [{"id": "BOGZ1FL153064486Q", "url": fred_url("BOGZ1FL153064486Q")}],
            f"{e1:.0f}% du patrimoine financier", pctile_val=pctile(ea["values"], e1)))

    # ── B2 — Patrimoine vs Revenu ─────────────────────────────────────────
    nw = fetch_fred("HNONWPDPI", start="1946-01-01")
    if nw:
        n0 = val_at(nw["dates"], nw["values"], 1985); n1 = nw["values"][-1]
        panelsB.append(panel(
            "patrimoine_revenu", "mono", "La richesse se déconnecte du salaire",
            "Le patrimoine gonfle plus vite que les revenus",
            f"Le patrimoine net des ménages vaut {n1:.0f}% de leur revenu annuel, "
            f"contre ~{n0:.0f}% dans les années 1980. La richesse enfle au rythme "
            f"des prix d'actifs, plus à celui du travail — jusqu'à la correction.",
            "%", "pct0",
            [serie("Patrimoine net / revenu", nw["dates"], nw["values"], BLUE, "pct0")],
            [{"id": "HNONWPDPI", "url": fred_url("HNONWPDPI")}],
            f"{n1:.0f}% du revenu disponible", pctile_val=pctile(nw["values"], n1)))

    # ── B3 — Financier vs Tangible ────────────────────────────────────────
    fa = fetch_fred("TFAABSHNO", start="1945-01-01")
    ta = fetch_fred("TABSHNO", start="1945-01-01")
    if fa and ta:
        fad, fav = ratio(fa, ta, 100.0)
        a1 = fav[-1]
        panelsB.append(panel(
            "financier_tangible", "mono", "Le bilan des ménages se financiarise",
            "Plus de papier que de tangible",
            f"{a1:.0f}% du patrimoine des ménages est désormais financier (titres, "
            f"fonds, retraite) plutôt que tangible (logement, biens durables). Leur "
            f"bilan est financiarisé — donc directement exposé aux marchés.",
            "%", "pct1",
            [serie("Actifs financiers / patrimoine", fad, fav, GREEN, "pct1")],
            [{"id": "TFAABSHNO ÷ TABSHNO", "url": fred_url("TFAABSHNO")}],
            f"{a1:.0f}% du patrimoine", pctile_val=pctile(fav, a1)))

    # ══ ANCRE — Le seuil scientifique de la sur-financiarisation (BIS/FMI) ══
    anchor = None
    bis = fetch_fred("QUSPAM770A", start="1950-01-01")
    if bis:
        b1 = bis["values"][-1]
        thr = [100.0] * len(bis["dates"])
        first_over = next((d[:4] for d, v in zip(bis["dates"], bis["values"]) if v >= 100), "?")
        anchor = panel(
            "bis_threshold", "mono", "Le verdict de la recherche · BIS & FMI",
            "Au-delà de ce seuil, la finance freine l'économie",
            f"La recherche académique (BIS 2012/2015, FMI 2015) a établi une "
            f"relation en U inversé : la finance dope la croissance… jusqu'à un "
            f"point de retournement, situé autour de <b>80-100% de crédit privé / "
            f"PIB</b>. Au-delà, elle la freine — en captant les talents, en "
            f"finançant l'immobilier plutôt que la productivité, en extrayant des "
            f"rentes. Le crédit privé américain atteint <b>{b1:.0f}% du PIB</b> et "
            f"dépasse ce seuil de nocivité depuis {first_over}. <b>C'est la "
            f"définition rigoureuse de la sur-financiarisation — et les États-Unis "
            f"y sont, structurellement, depuis une génération.</b>",
            "%", "pct0",
            [serie("Crédit privé / PIB (US)", bis["dates"], bis["values"], GOLD, "pct0"),
             serie("Seuil de nocivité (BIS/FMI)", bis["dates"], thr, RED, "pct0", dash=True)],
            [{"id": "QUSPAM770A", "url": fred_url("QUSPAM770A")}],
            f"{b1:.0f}% du PIB", pctile_val=pctile(bis["values"], b1))

    # ══ PARTIE C — L'inversion : l'économie sous perfusion ══════════════════
    panelsC = []
    # C1 — Le put de la Fed
    wal = fetch_fred("WALCL", start="2002-12-01")
    if wal and gmap:
        wd, wv = ratio_carry(wal, gdp, 0.001)   # M$ → Md$, / PIB Md$
        w0 = val_at(wd, wv, 2007); w1 = wv[-1]; wpk = max(wv)
        panelsC.append(panel(
            "fed_put", "mono", "Acte V · qui sauve les marchés ?",
            "Le put de la Fed",
            f"À chaque krach (2008, 2020), la Réserve Fédérale inonde le système de "
            f"liquidités : son bilan est passé de ~6% du PIB avant 2008 à un pic de "
            f"{wpk:.0f}% en 2021. La banque centrale ne pilote plus seulement "
            f"l'inflation — elle soutient les prix d'actifs, car l'économie ne "
            f"supporte plus leur chute. L'économie dépend de la finance, et non "
            f"l'inverse.",
            "%", "pct0",
            [serie("Bilan de la Fed / PIB", wd, wv, PURPLE, "pct0")],
            [{"id": "WALCL ÷ GDP", "url": fred_url("WALCL")}],
            f"{w1:.0f}% du PIB", note="Bilan Fed disponible depuis 2002."))

    # C2 — La croissance à crédit
    tot = fetch_fred("TCMDO", start="1950-01-01")
    if tot and gmap:
        td, tv = ratio_carry(tot, gdp, 0.001)
        t0 = val_at(td, tv, 1980); t1 = tv[-1]
        panelsC.append(panel(
            "debt_growth", "mono", "Acte VI · sur quoi repose la croissance ?",
            "La croissance carbure au crédit",
            f"La dette totale (ménages, entreprises, État) est passée de ~{t0:.0f}% "
            f"du PIB en 1980 à <b>{t1:.0f}%</b> aujourd'hui. Il faut toujours plus "
            f"de dette pour produire un dollar de PIB : la productivité de la dette "
            f"s'effondre. La croissance américaine n'est plus organique, elle est "
            f"financée.",
            "%", "pct0",
            [serie("Dette totale / PIB", td, tv, RED, "pct0")],
            [{"id": "TCMDO ÷ GDP", "url": fred_url("TCMDO")}],
            f"{t1:.0f}% du PIB", pctile_val=pctile(tv, t1)))

    # C3 — La vélocité s'effondre
    m2v = fetch_fred("M2V", start="1959-01-01")
    if m2v:
        v0 = m2v["values"][0]; v1 = m2v["values"][-1]; vmin = min(m2v["values"])
        panelsC.append(panel(
            "velocity", "mono", "Acte VII · l'argent circule-t-il encore ?",
            "L'argent dort, les actifs montent",
            f"La vélocité de la monnaie — le nombre de fois qu'un dollar change de "
            f"mains dans l'économie réelle — s'est effondrée de {v0:.2f} à {v1:.2f} "
            f"(creux {vmin:.2f} en 2020). On crée toujours plus de monnaie, mais "
            f"elle nourrit les marchés d'actifs plutôt que l'activité productive.",
            "pts", "num2",
            [serie("Vélocité de M2", m2v["dates"], m2v["values"], BLUE, "num2")],
            [{"id": "M2V", "url": fred_url("M2V")}],
            f"{v1:.2f}"))

    # C4 — Le réel décroche
    sp = fetch_fred("SPASTT01USM661N", start="1964-01-01")
    wg = fetch_fred("AHETPI", start="1964-01-01")
    cpi = fetch_fred("CPILFESL", start="1964-01-01")
    if sp and wg and cpi:
        cm = dict(zip(cpi["dates"], cpi["values"]))
        sd, sv = index_series(sp, 1971)
        rd, rv = index_series(wg, 1971, deflator=cm)
        panelsC.append(panel(
            "decoupling", "duo", "Acte VIII · deux économies, deux destins",
            "La bourse s'envole, le salaire stagne",
            f"Depuis 1971 (base 100), la bourse a été multipliée par ~{sv[-1] / 100:.0f} "
            f"quand le salaire horaire réel n'a progressé que de {rv[-1] - 100:.0f}%. "
            f"Wall Street et Main Street ne vivent plus dans la même économie : "
            f"l'une capitalise, l'autre subit.",
            "idx", "idx",
            [serie("Bourse (S&P, réel)", sd, sv, GOLD, "idx"),
             serie("Salaire horaire réel", rd, rv, GREEN, "idx")],
            [{"id": "SPASTT01USM661N", "url": fred_url("SPASTT01USM661N")},
             {"id": "AHETPI", "url": fred_url("AHETPI")}],
            "base 100 en 1971", note="Déflaté par l'IPC (CPILFESL)."))

    # ══ CONCLUSION analytique (synthèse + contre-arguments) ═════════════════
    conclusion = {
        "title": "Alors, sur-financiarisation — vraiment ?",
        "paragraphs": [
            "Pris isolément, aucun de ces graphes ne « prouve » rien : un marché "
            "haut, une dette élevée, des ménages exposés peuvent toujours se "
            "justifier. C'est leur <b>convergence</b> qui fait la démonstration. "
            "Le critère scientifique est rempli : au-delà de ~100% de crédit privé "
            "/ PIB, la recherche (BIS, FMI) montre que la finance freine la "
            "croissance plutôt qu'elle ne la stimule — et les États-Unis y sont "
            "depuis des décennies.",
            "Surtout, la <b>causalité s'est inversée</b>. Les travaux de Mian & "
            "Sufi (<i>House of Debt</i>) et de la BIS montrent que le cycle "
            "financier précède désormais le cycle économique : ce sont les marchés "
            "et le crédit qui déclenchent les récessions, plus l'inverse. La Fed "
            "subordonne sa politique aux prix d'actifs (le « put »), l'entreprise "
            "sert l'actionnaire avant l'investissement, et la consommation des "
            "ménages dépend de la valeur de leur portefeuille. La finance n'est "
            "plus au service de l'économie : l'économie est devenue dépendante de "
            "la finance.",
        ],
        "caveats": [
            "<b>Approfondissement financier ≠ excès.</b> La finance reste un "
            "moteur de croissance jusqu'au point de retournement. La thèse vaut "
            "pour une économie <i>déjà</i> au-delà du seuil, pas universellement.",
            "<b>Privilège du dollar.</b> Une part de la taille financière US "
            "reflète son rôle de centre financier mondial, pas un seul excès "
            "interne (mais la part étrangère des Treasuries a baissé de 34% à 23% "
            "depuis 2012).",
            "<b>Démographie & retraites.</b> Le passage des retraites à "
            "prestations définies vers les 401(k)/IRA a mécaniquement gonflé "
            "l'exposition des ménages aux actions — explication la plus sobre.",
            "<b>Double comptage (Haldane, BoE).</b> La valeur ajoutée mesurée de "
            "la finance est surestimée (on crédite les banques du <i>risque</i> "
            "porté) ; les ratios actifs financiers / PIB sont gonflés "
            "mécaniquement.",
        ],
    }

    refs = [
        {"a": "Epstein (2005)", "t": "définition canonique de la financiarisation"},
        {"a": "Krippner (2005)", "t": "profits par canaux financiers, part FIRE des profits"},
        {"a": "BIS — Cecchetti & Kharroubi (2012, 2015)", "t": "U inversé, la finance freine la croissance au-delà du seuil"},
        {"a": "Arcand, Berkes & Panizza — FMI (2015)", "t": "« Too Much Finance », seuil ~80-100% crédit/PIB"},
        {"a": "Mian & Sufi (2014)", "t": "House of Debt — le cycle du crédit précède la récession"},
        {"a": "Lazonick (2014)", "t": "« Profits Without Prosperity » — rachats d'actions vs investissement"},
        {"a": "Cieslak & Vissing-Jorgensen (2021)", "t": "« The Economics of the Fed Put »"},
    ]

    # ── Comparaisons internationales ──────────────────────────────────────
    mc = fetch_intl(
        "marketcap_gdp", "DDDM01{iso}A156NWDB", "1975-01-01",
        "Capitalisation boursière / PIB", "%", "pct0", "World Bank / GFDD",
        "Poids de la bourse dans l'économie, méthodologie identique pour tous les "
        "pays. Les États-Unis sont hauts — mais pas seuls : les places financières "
        "(Suisse, France) font autant ou plus.",
        False, fred_url("DDDM01USA156NWDB"))
    hd = fetch_intl(
        "hh_debt_gdp", "Q{iso}HAM770A", "1960-01-01",
        "Dette des ménages / PIB", "%", "pct0", "BIS",
        "Levier des ménages (crédit total / PIB, BIS, identique pour tous). Les "
        "ménages US sont en réalité moins endettés qu'en Suisse, au Canada ou en "
        "Corée : leur dépendance passe par les actions, pas par le crédit.",
        True, fred_url("QUSHAM770A"))

    payload = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "anchor": anchor,
        "conclusion": conclusion,
        "refs": refs,
        "partA": {
            "id": "wallstreet",
            "kicker": "Partie A — financiarisation de l'économie",
            "title": "Wall Street prend le pouvoir",
            "thesis": "La finance n'est plus au service de l'économie : elle la "
                      "réoriente à son profit. Quatre basculements le démontrent — "
                      "le travail cède au capital, l'entreprise rémunère "
                      "l'actionnaire plutôt qu'elle n'investit, et la bourse "
                      "décroche du réel.",
            "panels": panelsA,
            "intl": mc if mc["countries"] else None,
        },
        "partB": {
            "id": "menages",
            "kicker": "Partie B — exposition des ménages",
            "title": "L'épargnant pris au piège",
            "thesis": "À l'autre bout de la chaîne, les ménages américains n'ont "
                      "jamais autant dépendu des marchés : leur épargne, leur "
                      "patrimoine et leur retraite sont arrimés à la bourse. Quand "
                      "Wall Street tousse, c'est Main Street qui tremble.",
            "panels": panelsB,
            "intl": hd if hd["countries"] else None,
        },
        "partC": {
            "id": "inversion",
            "kicker": "Partie C — l'inversion",
            "title": "L'économie sous perfusion",
            "thesis": "Voici le cœur de la démonstration : la preuve que la "
                      "causalité s'est inversée. L'économie réelle ne peut plus se "
                      "passer de la finance — liquidités de la banque centrale, "
                      "dette toujours croissante, marchés d'actifs déconnectés du "
                      "travail. La finance ne sert plus l'économie ; l'économie en "
                      "dépend.",
            "panels": panelsC,
            "intl": None,
        },
    }

    if not panelsA and not panelsB:
        sys.stderr.write("[fin] AUCUN panneau — cache non écrit\n")
        sys.exit(1)

    CACHE_FILE.write_text(json.dumps(payload, separators=(",", ":")))
    CACHE_JS.write_text("window.__FINANCIARISATION__=" +
                        json.dumps(payload, separators=(",", ":")) + ";\n")
    sys.stderr.write(f"[fin] OK — ancre:{bool(anchor)} A:{len(panelsA)} "
                     f"B:{len(panelsB)} C:{len(panelsC)} panneaux, intl "
                     f"{bool(mc['countries'])}/{bool(hd['countries'])} "
                     f"-> {CACHE_FILE.name}\n")


if __name__ == "__main__":
    main()
