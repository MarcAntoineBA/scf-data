#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_market_concentration.py — « Concentration du marché actions » (US, 1926+)

QUESTION POSÉE
--------------
Le marché se concentre-t-il entre les mains de quelques géants, ou se
ré-atomise-t-il ? Et où en est-on par rapport à un siècle d'histoire — 1929
compris ?

LA MESURE
---------
Part de la capitalisation boursière américaine détenue par les GRANDES
CAPITALISATIONS. Une seule grandeur, une seule définition, tenue à l'identique
de juillet 1926 à aujourd'hui.

« Grande capitalisation » = société dont la taille dépasse le seuil des 10 % les
plus grosses DU NYSE. C'est la définition académique standard (celle qui sert à
construire le facteur taille de Fama-French), et c'est un SEUIL EN DOLLARS, pas
un quota : le décile supérieur ne contient donc pas 10 % des sociétés cotées.
Il en contenait 10,1 % en 1932, quand l'univers était pour l'essentiel le NYSE ;
il en contient 5,5 % en 2026 (176 sociétés sur 3 192), parce que des milliers de
petites valeurs NASDAQ sont venues grossir le bas du classement sans déplacer le
seuil. Le collecteur publie donc le COMPTE RÉEL de sociétés (`nt`, `nb`) : sans
lui, écrire « les 10 % les plus grosses » serait faux hors période ancienne.

SOURCE DU SOCLE HISTORIQUE — mensuel, 1926-07 → dernier mois publié
    Kenneth R. French Data Library, « Portfolios Formed on Size (ME) »,
    construit sur la base CRSP (univers NYSE + AMEX + NASDAQ).
    Le fichier livre, pour chacun des 10 déciles de taille et chaque mois :
      · le NOMBRE de sociétés du décile      (section « Number of Firms »)
      · la CAPITALISATION MOYENNE du décile  (section « Average Firm Size »)
    Leur produit donne la capitalisation totale du décile. D'où :
        part du top 10 % = capi(décile 10) / somme(capi des 10 déciles)
    Ce n'est pas une estimation : c'est la somme exacte des capitalisations
    telles que CRSP les enregistre.

RUPTURES D'UNIVERS — VÉRIFIÉES, PAS SUPPOSÉES
    L'AMEX entre dans CRSP en juillet 1962, le NASDAQ en décembre 1972. Ajouter
    des milliers de micro-capitalisations pourrait mécaniquement gonfler la part
    du décile supérieur. Mesuré sur les mois de part et d'autre :
        juin 1962  66,52 %  →  juillet 1962  66,32 %   (nombre de sociétés 1068 → 1120)
        nov. 1972  62,32 %  →  déc.    1972  61,73 %   (nombre de sociétés 2413 → 2408)
    Aucune marche d'escalier. DEUX raisons, la première étant la principale :
      1. le seuil qui définit le décile supérieur est calculé sur le NYSE SEUL —
         faire entrer des sociétés AMEX ou NASDAQ ne le déplace donc pas ;
      2. les micro-capitalisations ne pèsent de toute façon quasiment rien en
         capitalisation, même quand elles sont des milliers.
    La série est comparable sur toute sa longueur.

LE BOUT LIVE — quotidien
    La bibliothèque French est publiée une fois par mois, avec un à deux mois de
    décalage. Pour que l'indicateur vive au jour le jour, on prolonge la dernière
    valeur officielle par la performance RELATIVE de deux séries quotidiennes
    gratuites :
        ^GSPC (S&P 500)  ≈ le décile supérieur  (le S&P 500 pèse ~80 % de la
                             capitalisation US, le décile supérieur ~78 %)
        VTI              ≈ le marché total — l'ETF suit l'indice CRSP US Total
                             Market, c'est-à-dire LE MÊME UNIVERS que French
        part(t) = part(ancre) x (GSPC_t / GSPC_ancre) / (VTI_t / VTI_ancre)
    Seule la VARIATION vient du proxy ; le NIVEAU reste ancré sur la dernière
    valeur officielle, et se recale à chaque publication de French.

    FIDÉLITÉ MESURÉE (275 mois testés, août 2003 → juin 2026) : on part de la
    valeur officielle du mois M, on extrapole d'un mois, on compare au mois M+1.
        erreur moyenne     -0,05 pt   (aucun biais)
        erreur absolue moy. 0,48 pt   sur une série qui parcourt 31 points
        erreur absolue max  2,23 pt
    Les deux séries sont prises hors dividendes (cours de clôture bruts des deux
    côtés) : mélanger un cours ajusté et un cours brut introduirait un écart de
    rendement, pas de concentration.

SORTIES
    ~/Library/Caches/site_crypto_finance/market_concentration_cache.{json,js}
    window.__MARKET_CONCENTRATION__
"""

import io
import json
import os
import pathlib
import re
import statistics
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

FRENCH_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "Portfolios_Formed_on_ME_CSV.zip"
)
FRENCH_MEMBER = "Portfolios_Formed_on_ME.csv"

CACHE_DIR = pathlib.Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "market_concentration_cache.json"
OUT_JS = CACHE_DIR / "market_concentration_cache.js"
RAW_FRENCH = CACHE_DIR / "market_concentration_french_raw.csv"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Un mois de recul suffit largement pour l'extrapolation (French a 1 à 2 mois de
# retard) ; on en prend 24 pour que le graphe montre la jointure officiel/live.
LIVE_LOOKBACK_DAYS = 900

# Écart maximal toléré entre la valeur extrapolée et l'ancre officielle. La plus
# forte variation sur 12 mois de toute l'histoire vaut 8 points ; au-delà de 6
# points sur quelques semaines, c'est le proxy qui déraille, pas le marché.
MAX_LIVE_DRIFT_PT = 6.0


def log(*a):
    print(f"[concentration {datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def _err(e):
    """Message d'erreur sans chemin personnel (le bilan de collecte est publié)."""
    s = f"{type(e).__name__}: {e}"
    return re.sub(r"/Users/[^/\s]+", "~", s)[:300]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Socle historique : Kenneth French / CRSP
# ─────────────────────────────────────────────────────────────────────────────
def download_french():
    """Renvoie le texte du CSV French, ou None. Conserve une copie brute."""
    try:
        req = urllib.request.Request(FRENCH_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            blob = r.read()
        txt = zipfile.ZipFile(io.BytesIO(blob)).read(FRENCH_MEMBER).decode("latin-1")
        if "Number of Firms in Portfolios" not in txt or "Average Firm Size" not in txt:
            raise ValueError("sections attendues absentes du CSV")
        tmp = RAW_FRENCH.with_suffix(".tmp")
        tmp.write_text(txt, encoding="latin-1")
        os.replace(tmp, RAW_FRENCH)
        log(f"French téléchargé ({len(blob)/1024:.0f} Ko compressés)")
        return txt
    except Exception as e:
        log("échec du téléchargement French —", _err(e))
        if RAW_FRENCH.exists():
            log("repli sur la copie brute conservée")
            return RAW_FRENCH.read_text(encoding="latin-1")
        return None


def parse_french(txt):
    """
    Extrait les deux sections mensuelles et renvoie la série de concentration.

    Le fichier enchaîne plusieurs tableaux séparés par une ligne de titre en
    clair. On repère les titres, puis on lit les lignes « AAAAMM, v1, v2, … »
    jusqu'à la ligne vide qui clôt le tableau. Les 10 dernières colonnes sont
    les déciles (le fichier donne d'abord 30/40/30, puis 5 quintiles, puis 10
    déciles) ; la colonne « <= 0 » est ignorée, elle n'est pas utilisée.
    """
    lines = txt.split("\n")

    def section_at(title):
        start = next(i for i, l in enumerate(lines) if l.strip() == title)
        out, seen_header = {}, False
        for l in lines[start + 1:]:
            s = l.strip()
            if not s:
                if out:
                    break
                continue
            if not re.match(r"^\d{6},", s):
                if not seen_header:
                    seen_header = True
                    continue
                break
            p = [c.strip() for c in s.split(",")]
            try:
                out[p[0]] = [float(x) for x in p[1:]]
            except ValueError:
                continue
        return out

    nfirms = section_at("Number of Firms in Portfolios")
    avgsize = section_at("Average Firm Size")

    series = []
    for d in sorted(set(nfirms) & set(avgsize)):
        n, sz = nfirms[d][-10:], avgsize[d][-10:]
        if len(n) < 10 or len(sz) < 10:
            continue
        me = [a * b for a, b in zip(n, sz)]
        # -99.99 / -999 = donnée manquante chez French
        if any(x < 0 for x in me) or sum(me) <= 0:
            continue
        total = sum(me)
        series.append({
            "d": d,                                     # AAAAMM
            "s": round(me[-1] / total * 100, 3),        # part des grandes capis, %
            "b": round(sum(me[:5]) / total * 100, 3),   # part des 5 déciles du bas, %
            "n": int(round(sum(n))),                    # nombre total de sociétés cotées
            # ATTENTION : les déciles sont découpés sur les seuils NYSE SEULS, puis
            # toutes les sociétés (NYSE+AMEX+NASDAQ) y sont rangées. Le « décile
            # supérieur » ne contient donc PAS 10 % des cotées : 176 sur 3 192
            # (5,5 %) en 2026, contre 10,1 % en 1932 quand l'univers était
            # essentiellement le NYSE. On publie le compte réel — sans lui, toute
            # phrase du type « les 10 % les plus grosses » serait fausse.
            "nt": int(round(n[-1])),                    # sociétés dans le décile supérieur
            "nb": int(round(sum(n[:5]))),               # sociétés dans les 5 déciles du bas
            "m": round(total / 1e6, 3),                 # capitalisation totale, $ mille Md
        })
    return series


# ─────────────────────────────────────────────────────────────────────────────
# 2. Bout live : ^GSPC et VTI (quotidien)
# ─────────────────────────────────────────────────────────────────────────────
def yahoo_daily(symbol, since_ts):
    """{'AAAA-MM-JJ': clôture} — curl_cffi obligatoire (cf. yahoo/curl_cffi)."""
    from curl_cffi import requests as cr
    s = cr.Session(impersonate="chrome120")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&period1={since_ts}&period2={int(time.time())}"
    )
    r = s.get(url, timeout=40)
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    cl = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()] = float(c)
    return out


def build_live_tail(anchor_month, anchor_value):
    """
    Prolonge la dernière valeur officielle jour par jour.

    Renvoie (liste [{d, s}], note). La liste commence au dernier jour de bourse
    du mois d'ancrage (valeur = ancre exacte) pour que la jointure soit visible
    sur le graphe sans discontinuité.
    """
    since = int(time.time()) - LIVE_LOOKBACK_DAYS * 86400
    try:
        g = yahoo_daily("%5EGSPC", since)
        v = yahoo_daily("VTI", since)
    except Exception as e:
        log("bout live indisponible —", _err(e))
        return [], "indisponible"

    common = sorted(set(g) & set(v))
    if len(common) < 30:
        log(f"bout live trop court ({len(common)} jours) — ignoré")
        return [], "indisponible"

    # Jour d'ancrage = dernier jour de bourse du mois officiel
    pref = f"{anchor_month[:4]}-{anchor_month[4:]}"
    in_month = [d for d in common if d.startswith(pref)]
    if not in_month:
        log(f"aucun jour de bourse trouvé pour le mois d'ancrage {anchor_month}")
        return [], "indisponible"
    a = in_month[-1]

    tail = []
    for d in common:
        if d < a:
            continue
        val = anchor_value * (g[d] / g[a]) / (v[d] / v[a])
        if abs(val - anchor_value) > MAX_LIVE_DRIFT_PT:
            log(f"dérive anormale du proxy le {d} ({val:.2f} vs ancre {anchor_value:.2f}) — tronqué")
            break
        tail.append({"d": d, "s": round(val, 3)})
    return tail, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Lecture : percentile, régime, repères
# ─────────────────────────────────────────────────────────────────────────────
# ── Échelle de la jauge — FIXE, jamais recalculée sur la population ──────────
# Un PERCENTILE saturerait : une fois au sommet historique il resterait collé à
# 100 et une concentration qui continue de monter deviendrait invisible. La jauge
# est donc une transformation linéaire de la part brute, sur une échelle figée :
#   100 = 100 % — le décile supérieur possède la TOTALITÉ du marché. Ce n'est pas
#         un choix : c'est le maximum mathématique de la grandeur mesurée.
#     0 =  40 % — sous le marché le plus dispersé jamais observé (47,6 %, mars 1986).
# Marge avant saturation : 21,5 points de part brute au-dessus du record de 2025,
# soit 2,7 fois la plus forte hausse sur 12 mois de tout le siècle (+8,04 pts).
# Le percentile reste calculé et publié, mais comme LECTURE de contexte.
GAUGE_FLOOR = 40.0
GAUGE_CEIL = 100.0


def gauge_score(share):
    """Part brute (%) → jauge 0-100 sur l'échelle fixe."""
    g = (share - GAUGE_FLOOR) / (GAUGE_CEIL - GAUGE_FLOOR) * 100.0
    return round(max(0.0, min(100.0, g)), 1)


def percentile_of(value, population):
    """Part de l'histoire passée à un niveau inférieur ou égal, en %."""
    return round(sum(1 for x in population if x <= value) / len(population) * 100, 1)


def label_for(score):
    """Bandes calées sur la part brute, converties en points de jauge.

    > 76 % de part brute  → 60 sur la jauge (au-dessus de tout précédent)
    70-76 %               → 50-60  (bulle Internet, creux de 1932)
    64-70 %               → 40-50  (64,1 % = médiane du siècle)
    55-64 %               → 25-40
    < 55 %                → 0-25   (années 1970-1980)
    """
    if score >= 60:
        return "Concentration extrême", "neg"
    if score >= 50:
        return "Concentration élevée", "warn"
    if score >= 40:
        return "Concentration ordinaire", "eq"
    if score >= 25:
        return "Marché large", "pos"
    return "Marché très dispersé", "pos"


def regime_for(chg12):
    if chg12 >= 1.0:
        return "concentration", "Le marché se concentre"
    if chg12 <= -1.0:
        return "atomisation", "Le marché s'atomise"
    return "stable", "Régime stable"


def main():
    txt = download_french()
    if not txt:
        log("ABANDON : aucune donnée historique disponible, cache existant conservé")
        return 1
    monthly = parse_french(txt)
    if len(monthly) < 900:
        log(f"ABANDON : série anormalement courte ({len(monthly)} mois), cache conservé")
        return 1

    # Garde-fou anti-régression : une série qui fond d'un tiers = parsing cassé
    if OUT_JSON.exists():
        try:
            prev = json.loads(OUT_JSON.read_text())
            n_prev = len(prev.get("monthly", []))
            if n_prev and len(monthly) < n_prev * 0.67:
                log(f"ABANDON : {len(monthly)} mois contre {n_prev} au passage précédent")
                return 1
        except Exception:
            pass

    levels = [r["s"] for r in monthly]
    last = monthly[-1]
    anchor_month, anchor_value = last["d"], last["s"]

    tail, tail_note = build_live_tail(anchor_month, anchor_value)
    live_value = tail[-1]["s"] if len(tail) > 1 else anchor_value
    live_date = tail[-1]["d"] if len(tail) > 1 else None

    score = gauge_score(live_value)
    label, tone = label_for(score)
    pct_hist = percentile_of(live_value, levels)

    # Variation sur 12 mois : valeur du jour contre la valeur officielle d'il y a un an
    ref12 = monthly[-13]["s"] if len(monthly) > 13 else monthly[0]["s"]
    chg12 = round(live_value - ref12, 2)
    chg12_hist = [monthly[i]["s"] - monthly[i - 12]["s"] for i in range(12, len(monthly))]
    reg_dir, reg_label = regime_for(chg12)

    i_min, i_max = levels.index(min(levels)), levels.index(max(levels))

    payload = {
        "gauge": {
            "score": score,
            "label": label,
            "tone": tone,
            "level_live": round(live_value, 2),
            "level_official": round(anchor_value, 2),
            "asof_official": f"{anchor_month[:4]}-{anchor_month[4:]}",
            "asof_live": live_date,
            "live_status": tail_note,
            # Échelle publiée avec la valeur : la jauge doit rester reconstructible
            # à la main — score = (part − floor) ÷ (ceil − floor) × 100.
            "scale_floor": GAUGE_FLOOR,
            "scale_ceil": GAUGE_CEIL,
            # Percentile historique : plus la jauge, mais toujours la meilleure
            # phrase de contexte (« moins concentré X % du temps depuis 1926 »).
            "percentile": pct_hist,
        },
        "regime": {
            "dir": reg_dir,
            "label": reg_label,
            "chg12m": chg12,
            "pct12m": percentile_of(chg12, chg12_hist),
            "share_time_concentrating": round(
                sum(1 for x in chg12_hist if x > 0) / len(chg12_hist) * 100, 1
            ),
        },
        "stats": {
            "start": monthly[0]["d"],
            "end": anchor_month,
            "n_months": len(monthly),
            "min": round(min(levels), 2), "min_date": monthly[i_min]["d"],
            "max": round(max(levels), 2), "max_date": monthly[i_max]["d"],
            "median": round(statistics.median(levels), 2),
            "bottom50_now": last["b"],
            "bottom50_max": round(max(r["b"] for r in monthly), 2),
            # Comptes réels de sociétés : la statistique la plus parlante du lot
            # (« 176 sociétés sur 3 192 détiennent 78 % du marché »).
            "nfirms_top_now": last["nt"],
            "nfirms_bot_now": last["nb"],
            "pct_firms_top_now": round(last["nt"] / last["n"] * 100, 1) if last["n"] else None,
            "pct_firms_bot_now": round(last["nb"] / last["n"] * 100, 1) if last["n"] else None,
            "nfirms_now": last["n"],
            "nfirms_max": max(r["n"] for r in monthly),
            "nfirms_max_date": max(monthly, key=lambda r: r["n"])["d"],
            "mcap_now": last["m"],
            "chg12m_p10": round(sorted(chg12_hist)[len(chg12_hist) // 10], 2),
            "chg12m_p90": round(sorted(chg12_hist)[9 * len(chg12_hist) // 10], 2),
            "chg12m_min": round(min(chg12_hist), 2),
            "chg12m_max": round(max(chg12_hist), 2),
        },
        "monthly": monthly,
        "live_tail": tail,
        "generated_at": int(time.time()),
        "source": (
            "Kenneth R. French Data Library — Portfolios Formed on Size (ME), "
            "base CRSP (NYSE+AMEX+NASDAQ), mensuel depuis juillet 1926. "
            "Bout quotidien extrapolé via Yahoo Finance ^GSPC et VTI."
        ),
        "methodology": (
            "Part du décile supérieur dans la capitalisation boursière américaine. "
            "Capitalisation d'un décile = nombre de sociétés x capitalisation moyenne, "
            "les deux publiées par French. Jauge = échelle FIXE 40 % (0) → 100 % (100), "
            "le haut étant le maximum mathématique de la grandeur : elle ne sature donc "
            "jamais et continue de monter si la concentration monte. Le bout quotidien "
            "prolonge la dernière valeur officielle par la performance relative du "
            "S&P 500 face au marché total (erreur absolue moyenne : 0,48 point sur 275 mois)."
        ),
    }

    tmp = OUT_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, OUT_JSON)

    js = "/* Auto-generated by fetch_market_concentration.py — do not edit. */\n"
    js += "(function(){var d=" + json.dumps(payload, separators=(",", ":"))
    js += ";window.__MARKET_CONCENTRATION__=d;})();\n"
    tmp_js = OUT_JS.with_suffix(".tmp")
    tmp_js.write_text(js, encoding="utf-8")
    os.replace(tmp_js, OUT_JS)

    log(
        f"OK · {len(monthly)} mois ({monthly[0]['d']}→{anchor_month}) · "
        f"niveau officiel {anchor_value:.2f}% · live {live_value:.2f}% ({live_date}) · "
        f"jauge {score} (part {live_value:.2f}%, pct {pct_hist}) · 12m {chg12:+.2f} pt · {OUT_JS.stat().st_size/1024:.0f} Ko"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
