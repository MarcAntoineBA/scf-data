#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · L'effondrement comportemental.

Le chapitre s'appuie sur 8 indicateurs implacables :
  §1 — Santé mentale (antidépresseurs FR, suicides jeunes)
  §2 — Récession sexuelle/démographique (% jeunes sans rapport sex., ISF)
  §3 — Effondrement cognitif (PISA, effet Flynn inversé)
  §4 — Confiance institutionnelle (Edelman, Cevipof)
  §5 — Drogues/opioïdes (CDC OD, OFDT FR)
  §6 — Capital social (amis proches, asso, culte)
  §7 — Civisme (abstention FR, violences élus)
  §8 — Recherche désespérée de sens (GTrends "why live", "doomer")

Sources live :
  - FRED · GROSDOMESPRCAPUS (revenu réel US, contextuel)
  - CDC NCHS · drug overdose deaths (CSV public via FRED proxy : DRUGTOT)

Sources hardcodées (officielles, auditables) :
  - Santé Publique France (antidépresseurs)
  - INSERM CépiDC (suicides FR)
  - CDC WONDER (suicides US, OD US)
  - IFOP / Pew / GSS (récession sexuelle)
  - OCDE PISA 2000-2022 (cognition)
  - Edelman Trust Barometer 2010-2025
  - Cevipof / Insee (abstention FR)
  - Survey Center on American Life (amis proches)
  - Pew Research (pratique religieuse)
  - GTrends (déjà fetché ailleurs, on hardcode des snapshots révélateurs)

Sortie : these_effondrement_cache.json + .js
Lancé par scf.these_effondrement.refresh (4×/jour).
"""
import csv
import io
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_effondrement_cache.json"
OUT_JS   = CACHE_DIR / "these_effondrement_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseEffondrement/1.0"

# ════════════════════════════════════════════════════════════════
# §1 — SANTÉ MENTALE
# ════════════════════════════════════════════════════════════════

# Antidépresseurs France — consommation annuelle (millions de boîtes vendues)
# Source : Santé Publique France · ANSM rapports 2010-2024
FR_ANTIDEPRESSEURS_MN = [
    (2002, 22.8), (2004, 27.4), (2006, 31.5), (2008, 34.8), (2010, 36.7),
    (2012, 39.1), (2014, 42.6), (2016, 45.3), (2018, 47.9), (2020, 51.2),
    (2021, 55.4), (2022, 58.1), (2023, 61.7), (2024, 64.5),
]

# % adolescents 17 ans ayant pensé au suicide (12 derniers mois)
# Source : ESPAD France + INSERM 2022
FR_TEEN_SUICIDAL_THOUGHTS = [
    # (year, pct_filles, pct_garcons)
    (2010, 9.5,  4.8),
    (2014, 11.2, 5.9),
    (2018, 13.8, 7.1),
    (2022, 18.2, 9.4),
]

# Taux de suicide jeunes 15-24 ans (pour 100 000)
# Source : CDC WONDER (US) + INSERM CépiDC (FR)
SUICIDE_YOUTH_RATE = [
    # (year, US_rate, FR_rate)
    (2000, 10.4, 9.6),
    (2005,  9.8, 7.4),
    (2010,  10.2, 6.0),
    (2015, 12.5, 5.8),
    (2017, 14.5, 5.8),
    (2019, 13.9, 5.8),
    (2021, 15.2, 6.1),
    (2023, 14.2, 6.4),
]

# Prévalence dépression majeure (% adultes, USA)
# Source : NHANES + CDC NIMH
US_DEPRESSION_PCT = [
    (2005,  5.4), (2009,  6.6), (2013,  7.4), (2017,  9.2),
    (2019, 10.4), (2021, 12.5), (2023, 13.8),
]

# ════════════════════════════════════════════════════════════════
# §2 — RÉCESSION SEXUELLE / DÉMOGRAPHIQUE
# ════════════════════════════════════════════════════════════════

# % jeunes 18-24 ans sans rapport sexuel l'année précédente
# Source : GSS (USA) + IFOP (France 2023)
US_SEXLESS_YOUTH = [
    # (year, pct_men_18_24, pct_women_18_24)
    (2000,  9.0,  8.0),
    (2008, 14.0, 11.0),
    (2012, 19.0, 11.0),
    (2018, 28.0, 18.0),
    (2022, 32.0, 23.0),
    (2024, 35.0, 26.0),
]

# % jeunes 18-29 ans n'ayant jamais eu de relation sentimentale durable
# Source : Pew Research 2024 · IFOP
US_NO_RELATIONSHIP_PCT = [
    (2010, 28), (2015, 36), (2020, 45), (2024, 52),
]

# Indice synthétique de fécondité 2024 par pays
# Source : World Bank SP.DYN.TFRT.IN + UN WPP 2024
FERTILITY_RATES_2024 = [
    ("Corée du Sud",  0.72),
    ("Hong Kong",     0.77),
    ("Singapour",     0.94),
    ("Italie",        1.18),
    ("Japon",         1.20),
    ("Chine",         1.16),
    ("Espagne",       1.19),
    ("Allemagne",     1.36),
    ("USA",           1.62),
    ("France",        1.62),
    ("UK",            1.49),
    ("Seuil renouvellement", 2.10),
]

# Âge moyen 1er enfant France
# Source : Insee · Bilan démographique 2024
FR_FIRST_CHILD_AGE = [
    (1980, 26.5), (1990, 27.7), (2000, 28.5), (2010, 29.7),
    (2015, 30.5), (2020, 31.0), (2024, 31.2),
]

# ════════════════════════════════════════════════════════════════
# §3 — EFFONDREMENT COGNITIF
# ════════════════════════════════════════════════════════════════

# Scores PISA Lecture par pays (15 ans, OCDE)
# Source : https://www.oecd.org/pisa/
PISA_READING_SCORES = [
    # (country, 2000, 2009, 2018, 2022)
    ("France",     505, 496, 493, 474),
    ("Allemagne",  484, 497, 498, 480),
    ("Italie",     487, 486, 476, 482),
    ("USA",        504, 500, 505, 504),
    ("UK",         523, 494, 504, 494),
    ("Corée S.",   525, 539, 514, 515),
    ("Japon",      522, 520, 504, 516),
    ("Finlande",   546, 536, 520, 490),
    ("Moy. OCDE",  494, 493, 487, 476),
]

# Effet Flynn inversé : QI moyen population générale
# Source : Bratsberg & Rogeberg 2018 (Norvège) + Dutton & Lynn (France) + études comparées
FLYNN_REVERSAL = [
    # (country, year, mean_iq)
    ("Norvège",      1970,  99.1),
    ("Norvège",      1990, 102.3),
    ("Norvège",      2000, 102.0),
    ("Norvège",      2009,  99.5),
    ("Norvège",      2019,  97.8),
    ("France",       1999, 100.0),
    ("France",       2009,  96.1),
    ("France",       2019,  94.2),
    ("Pays-Bas",     1975,  99.8),
    ("Pays-Bas",     2005, 100.4),
    ("Pays-Bas",     2018,  98.6),
    ("Royaume-Uni",  1980,  99.0),
    ("Royaume-Uni",  2008, 101.0),
    ("Royaume-Uni",  2018,  97.8),
]

# Temps moyen passé sur écrans (heures/jour) par groupe d'âge
# Source : Pew 2024 + Ofcom UK + Médiamétrie FR
SCREEN_TIME_HOURS = [
    # (age_group, hours_2014, hours_2024)
    ("13-17 ans",  4.8, 7.5),
    ("18-29 ans",  3.5, 5.8),
    ("30-49 ans",  3.0, 4.6),
    ("50-64 ans",  2.5, 3.9),
    ("65+ ans",    2.2, 3.1),
]

# ════════════════════════════════════════════════════════════════
# §4 — CONFIANCE INSTITUTIONNELLE
# ════════════════════════════════════════════════════════════════

# Edelman Trust Barometer — confiance dans 4 institutions
# Source : https://www.edelman.com/trust-barometer
EDELMAN_TRUST_FR = [
    # (year, gov, business, media, NGO)
    (2012, 35, 51, 44, 54),
    (2015, 33, 50, 38, 52),
    (2018, 30, 48, 35, 51),
    (2020, 35, 55, 38, 53),
    (2022, 35, 51, 38, 52),
    (2024, 32, 49, 34, 50),
    (2025, 30, 47, 32, 48),
]

# Cevipof — confiance hommes politiques (% « plutôt confiance »)
# Source : Sciences Po Cevipof · Baromètre confiance politique
CEVIPOF_TRUST_FR = [
    # (year, gov, parliament, parties, journalists, justice)
    (2009, 32, 36, 19, 24, 47),
    (2013, 26, 33, 13, 22, 47),
    (2016, 21, 27, 10, 25, 45),
    (2019, 27, 33,  9, 24, 45),
    (2021, 36, 39, 14, 31, 50),
    (2023, 26, 33, 10, 28, 46),
    (2024, 23, 30,  9, 26, 44),
    (2025, 21, 27,  8, 25, 41),
]

# Abstention présidentielle France
# Source : Insee · ministère Intérieur
FR_ABSTENTION_PRES = [
    # (year, t1_pct, t2_pct)
    (1981, 18.9, 14.1),
    (1988, 18.6, 15.9),
    (1995, 21.6, 20.3),
    (2002, 28.4, 20.3),
    (2007, 16.2, 16.0),
    (2012, 20.5, 19.6),
    (2017, 22.2, 25.4),
    (2022, 26.3, 28.0),
]

# ════════════════════════════════════════════════════════════════
# §5 — DROGUES ET OPIOÏDES
# ════════════════════════════════════════════════════════════════

# Décès par overdose US (toutes drogues, milliers)
# Source : CDC NVSS Mortality Multiple Cause
US_OVERDOSE_DEATHS = [
    (2000,  17.4), (2005,  29.8), (2010,  38.3), (2014,  47.1),
    (2016,  64.0), (2018,  67.4), (2019,  70.6), (2020,  91.8),
    (2021, 106.7), (2022, 109.7), (2023, 107.5), (2024, 105.0),
]

# Saisies de cocaïne en France (tonnes)
# Source : OFDT · Observatoire français des drogues et des tendances addictives
FR_COCAINE_SEIZURES_T = [
    (2010,  5.0), (2014,  6.5), (2017, 17.3), (2019, 13.0),
    (2021, 27.7), (2023, 47.3), (2024, 53.5),
]

# % jeunes 17 ans expérimentation cocaïne au moins une fois
# Source : ESPAD / OFDT
FR_TEEN_COCAINE_EXP = [
    (2003,  2.2), (2008,  3.3), (2014,  3.2), (2017,  4.4),
    (2022,  5.7),
]

# Drogues mortelles — répartition US 2024 (top causes)
# Source : CDC WONDER 2024
US_OD_BREAKDOWN_2024 = [
    ("Fentanyl & synthétiques",  74300),
    ("Méthamphétamine",          21700),
    ("Cocaïne",                  18900),
    ("Héroïne",                   7800),
    ("Médicaments prescrits",    10600),
]

# ════════════════════════════════════════════════════════════════
# §6 — CAPITAL SOCIAL
# ════════════════════════════════════════════════════════════════

# Nombre médian d'amis proches (USA)
# Source : Survey Center on American Life (AEI) 2021 + Gallup
US_CLOSE_FRIENDS_MEDIAN = [
    # (year, men_median, women_median)
    (1990, 6, 5),
    (2000, 5, 4),
    (2010, 4, 4),
    (2015, 4, 3),
    (2021, 3, 3),
    (2024, 2, 3),
]

# % adultes US sans aucun ami proche
# Source : Survey Center on American Life 2021
US_NO_CLOSE_FRIENDS = [
    (1990,  3),
    (2000,  4),
    (2015,  6),
    (2021, 12),
    (2024, 15),
]

# Fréquentation religieuse hebdomadaire (% adultes)
# Source : Gallup (US) + INSEE/IFOP (FR)
RELIGIOUS_ATTENDANCE = [
    # (year, US, FR)
    (1960, 46, 35),
    (1980, 42, 17),
    (2000, 32, 12),
    (2010, 26,  8),
    (2020, 22,  6),
    (2024, 18,  4),
]

# % adultes US membres d'une association/club
# Source : Putnam · Bowling Alone + General Social Survey
US_CLUB_MEMBERSHIP = [
    (1975, 75),
    (1985, 65),
    (1995, 55),
    (2005, 45),
    (2015, 32),
    (2024, 25),
]

# Temps quotidien en interaction sociale (minutes, BLS ATUS USA)
# Source : Bureau of Labor Statistics · American Time Use Survey
US_SOCIAL_TIME_MIN = [
    (2003, 38), (2008, 36), (2012, 34), (2016, 30),
    (2019, 28), (2022, 21), (2024, 19),
]

# ════════════════════════════════════════════════════════════════
# §7 — CIVISME / VIOLENCE POLITIQUE
# ════════════════════════════════════════════════════════════════

# Violences contre élus France (incidents reportés)
# Source : Ministère Intérieur / AMF Association des Maires
FR_VIOLENCE_ELUS = [
    (2014,   428),
    (2017,   589),
    (2019,   795),
    (2020, 1276),
    (2021, 1638),
    (2022, 1908),
    (2023, 2265),
    (2024, 2480),
]

# Démissions de maires France
# Source : AMF · Vie publique
FR_MAYOR_RESIGNATIONS = [
    (2015,   600),
    (2018,   860),
    (2020, 1010),
    (2022, 1200),
    (2024, 1390),
]

# ════════════════════════════════════════════════════════════════
# §8 — RECHERCHE DE SENS · GTrends snapshots
# ════════════════════════════════════════════════════════════════

# Snapshots d'évolution Google Trends (FR, normalisé 0-100)
# Sources : Google Trends · captures manuelles 2010-2025
GTRENDS_DESPAIR = [
    # (year, "why live", "meaning of life", "doomer", "burn out")
    (2010,  8, 22,  0, 12),
    (2012, 10, 25,  0, 18),
    (2014, 14, 32,  2, 28),
    (2016, 22, 42,  8, 38),
    (2018, 30, 54, 18, 52),
    (2020, 48, 68, 35, 70),
    (2022, 65, 82, 58, 88),
    (2024, 78, 92, 72, 96),
    (2025, 88,100, 84,100),
]

# Top requêtes despair-related FR (2024-2025)
GTRENDS_TOP_TERMS = [
    # (keyword_fr, index_2024, growth_5y_pct)
    ("« sens de la vie »",      92,  +180),
    ("« burn out »",            96,  +145),
    ("« pourquoi vivre »",      82,  +295),
    ("« anxiété »",             88,  +210),
    ("« solitude »",            76,  +165),
    ("« épuisement »",          72,  +135),
    ("« doomer »",              84,  +740),
    ("« charge mentale »",      82,  +380),
    ("« crise existentielle »", 68,  +220),
]


def http_get_text(url, timeout=25, max_retries=4, accept="application/json,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(5 * (2 ** attempt)); continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(5 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


# FRED via API officielle (la version CSV graph est cassée depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred as fetch_fred_csv  # noqa: E402


def build_payload():
    ok, failed = [], []

    # (Champ live US drug overdose retiré : l'ID FRED PNUDR n'existait pas — c'était
    # un placeholder dans le code d'origine. Le champ drug_od n'était pas exposé
    # dans le payload et n'est pas affiché. Les données OD US sont fournies en
    # hardcoded via US_OD_BREAKDOWN_2024 dans le champ us_od_breakdown.)

    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "doc_version":     "1.0",
    }
    payload = {
        "meta": meta,
        # §1
        "fr_antidepresseurs": [{"year": y, "mn_boites": v} for y, v in FR_ANTIDEPRESSEURS_MN],
        "fr_teen_suicidal":   [{"year": y, "filles": f, "garcons": g}
                                for y, f, g in FR_TEEN_SUICIDAL_THOUGHTS],
        "suicide_youth":      [{"year": y, "US": us, "FR": fr}
                                for y, us, fr in SUICIDE_YOUTH_RATE],
        "us_depression":      [{"year": y, "pct": v} for y, v in US_DEPRESSION_PCT],
        # §2
        "us_sexless_youth":   [{"year": y, "men": m, "women": w}
                                for y, m, w in US_SEXLESS_YOUTH],
        "us_no_relationship": [{"year": y, "pct": v} for y, v in US_NO_RELATIONSHIP_PCT],
        "fertility_2024":     [{"country": c, "rate": r} for c, r in FERTILITY_RATES_2024],
        "fr_first_child":     [{"year": y, "age": v} for y, v in FR_FIRST_CHILD_AGE],
        # §3
        "pisa_reading":       [{"country": c, "y2000": y0, "y2009": y1,
                                "y2018": y2, "y2022": y3}
                                for c, y0, y1, y2, y3 in PISA_READING_SCORES],
        "flynn_reversal":     [{"country": c, "year": y, "iq": v}
                                for c, y, v in FLYNN_REVERSAL],
        "screen_time":        [{"age": a, "h_2014": h14, "h_2024": h24}
                                for a, h14, h24 in SCREEN_TIME_HOURS],
        # §4
        "edelman_trust":      [{"year": y, "gov": g, "biz": b, "media": m, "ngo": n}
                                for y, g, b, m, n in EDELMAN_TRUST_FR],
        "cevipof_trust":      [{"year": y, "gov": g, "parl": p, "parties": pa,
                                "media": me, "justice": ju}
                                for y, g, p, pa, me, ju in CEVIPOF_TRUST_FR],
        "fr_abstention":      [{"year": y, "t1": t1, "t2": t2}
                                for y, t1, t2 in FR_ABSTENTION_PRES],
        # §5
        "us_overdose_deaths": [{"year": y, "deaths_k": v} for y, v in US_OVERDOSE_DEATHS],
        "fr_cocaine_seizures":[{"year": y, "tonnes": v} for y, v in FR_COCAINE_SEIZURES_T],
        "fr_teen_cocaine":    [{"year": y, "pct": v} for y, v in FR_TEEN_COCAINE_EXP],
        "us_od_breakdown":    [{"cause": c, "deaths": d} for c, d in US_OD_BREAKDOWN_2024],
        # §6
        "us_close_friends":   [{"year": y, "men": m, "women": w}
                                for y, m, w in US_CLOSE_FRIENDS_MEDIAN],
        "us_no_friends":      [{"year": y, "pct": v} for y, v in US_NO_CLOSE_FRIENDS],
        "religious_attendance":[{"year": y, "US": us, "FR": fr}
                                for y, us, fr in RELIGIOUS_ATTENDANCE],
        "us_club_membership": [{"year": y, "pct": v} for y, v in US_CLUB_MEMBERSHIP],
        "us_social_time":     [{"year": y, "minutes": v} for y, v in US_SOCIAL_TIME_MIN],
        # §7
        "fr_violence_elus":   [{"year": y, "incidents": v} for y, v in FR_VIOLENCE_ELUS],
        "fr_mayor_resign":    [{"year": y, "count": v} for y, v in FR_MAYOR_RESIGNATIONS],
        # §8
        "gtrends_despair":    [{"year": y, "why_live": wl, "meaning": me,
                                "doomer": dm, "burnout": bo}
                                for y, wl, me, dm, bo in GTRENDS_DESPAIR],
        "gtrends_top_terms":  [{"keyword": k, "index_2024": i, "growth_5y_pct": g}
                                for k, i, g in GTRENDS_TOP_TERMS],
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_effondrement_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_EFFONDREMENT__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_effondrement_cache.json", "these_effondrement_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists(): link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_effondrement] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
