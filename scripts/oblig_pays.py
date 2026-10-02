#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fondamentaux des États emprunteurs (module de fetch_obligations.py).

Ce qu'une fiche d'emprunt d'État doit dire en plus du taux : l'État peut-il
payer ? Dette et déficit (FMI, Eurostat), charge d'intérêts, l'écart entre le
taux qu'il paie et la croissance de son économie (r − g), qui détient sa dette,
à quelle échéance il doit la refinancer, ce que coûtent ses adjudications, ce
que fait sa banque centrale, et comment les agences le notent.

⚠ FMI (imf.org) : l'agent de Python PASSE, un agent personnalisé prend 403 —
  l'inverse de l'OCDE et de la Bank of England. On ne déguise rien.
⚠ La charge d'intérêts du FMI est NETTE (solde primaire − solde) : très basse
  pour un État qui détient beaucoup d'actifs (Canada 0,24 % du PIB contre
  1,66 % à l'OCDE). Pour l'UE, on lui préfère les intérêts BRUTS d'Eurostat.
⚠ Détenteurs (Eurostat) : la BCE elle-même compte comme « non-résident » ;
  en Grèce, les « non-résidents » sont surtout les prêts européens.
⚠ NOTATIONS : aucune source libre et fiable. Table vérifiée à la main le
  01/10/2026, ligne par ligne (Wikipédia avait 9 lignes fausses sur 57) ; la
  fiche affiche la date de vérification et la source de chaque note.
"""
import csv
import io
import json
import re
import urllib.parse
from collections import defaultdict
from datetime import date

from oblig_net import get, get_txt, get_json, fred, log, estnb

ISO3 = {"us": "USA", "de": "DEU", "fr": "FRA", "it": "ITA", "es": "ESP", "gb": "GBR", "jp": "JPN", "ca": "CAN",
        "au": "AUS", "ch": "CHE", "nl": "NLD", "be": "BEL", "at": "AUT", "ie": "IRL", "pt": "PRT", "gr": "GRC",
        "cn": "CHN", "in": "IND"}
EUROSTAT = {"de": "DE", "fr": "FR", "it": "IT", "es": "ES", "nl": "NL", "be": "BE", "at": "AT", "ie": "IE", "pt": "PT", "gr": "EL"}
BCE = {"de": "DE", "fr": "FR", "it": "IT", "es": "ES", "nl": "NL", "be": "BE", "at": "AT", "ie": "IE", "pt": "PT", "gr": "GR"}

# ── Notations : (pays, agence, note, perspective, dernière action, source, type de source) ──
VERIFIE_LE = "2026-10-01"
NOTATIONS = [
    ("de", "sp", "AAA", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("de", "moodys", "Aaa", "Stable", "non verifiee (AGI : confirmee mai 2025 ; revue prevue 13/03/2026, issue non verifiee)", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("de", "fitch", "AAA", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("au", "sp", "AAA", "Stable", "non verifiee", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("au", "moodys", "Aaa", "Stable", "non verifiee", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("au", "fitch", "AAA", "Stable", "non verifiee", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("at", "sp", "AA+", "Stable", "non verifiee", "https://www.oebfa.at/dam/jcr:0a8b96af-2bea-4ba5-a717-668ba25e41bd/Republic%20of%20Austria%20Investor%20Information_Jul26.pdf", "dmo"),
    ("at", "moodys", "Aa1", "Negative", "non verifiee", "https://www.oebfa.at/dam/jcr:0a8b96af-2bea-4ba5-a717-668ba25e41bd/Republic%20of%20Austria%20Investor%20Information_Jul26.pdf", "dmo"),
    ("at", "fitch", "AA", "Stable", "2025-06-10 (abaissement, titre communique OeBFA)", "https://www.oebfa.at/dam/jcr:0a8b96af-2bea-4ba5-a717-668ba25e41bd/Republic%20of%20Austria%20Investor%20Information_Jul26.pdf", "dmo"),
    ("be", "sp", "AA-", "Stable", "2026-04-24 ; prochaine revue 2026-10-23", "https://www.debtagency.be/en/datafederalstaterating", "dmo"),
    ("be", "moodys", "A1", "Stable", "2026-04-17 ; prochaine revue 2026-10-09", "https://www.debtagency.be/en/datafederalstaterating", "dmo"),
    ("be", "fitch", "A+", "Stable", "2026-05-22 ; prochaine revue 2026-11-20", "https://www.debtagency.be/en/datafederalstaterating", "dmo"),
    ("br", "sp", "BB", "Stable", "2023", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("br", "moodys", "Ba1", "Positive", "2024-10-07", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("br", "fitch", "BB", "Stable", "2025-06-25", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("ca", "sp", "AAA", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("ca", "moodys", "Aaa", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("ca", "fitch", "AA+", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("cn", "sp", "A+", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("cn", "moodys", "A1", "Negative au 03/04/2026", "non verifiee (Wikipedia : Stable 27/04/2026)", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("cn", "fitch", "A", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("es", "sp", "A+", "Stable", "2025-09-12", "https://www.tesoro.es/en/deuda-publica/calificacion-crediticia", "dmo"),
    ("es", "moodys", "A3", "Stable", "2025-09-26", "https://www.tesoro.es/en/deuda-publica/calificacion-crediticia", "dmo"),
    ("es", "fitch", "A", "Stable", "2025-09-26 (AGI : revue 13/03/2026, issue non verifiee)", "https://www.tesoro.es/en/deuda-publica/calificacion-crediticia", "dmo"),
    ("us", "sp", "AA+", "Stable", "juin 2026 (maintien, jour exact non verifie)", "https://english.aawsat.com/business/5306818-fitch-keeps-us-aa-cites-economic-resilience-amid-fiscal-risks", "presse"),
    ("us", "moodys", "Aa1", "Stable", "2025-05-16 (abaissement) ; rien trouve en 2026", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("us", "fitch", "AA+", "Stable", "2026-08-13 (confirmee)", "https://english.aawsat.com/business/5306818-fitch-keeps-us-aa-cites-economic-resilience-amid-fiscal-risks", "presse"),
    ("fr", "sp", "A+", "Stable", "2025-10-17 (abaissement) ; confirmee 2026-05-29", "https://www.economiematin.fr/dette-francaise-note", "presse"),
    ("fr", "moodys", "Aa3", "Negative", "fin oct. 2025 (perspective -> negative, 24/10/2025 selon MA, non ouvert) ; confirmee 2026-04-10", "https://www.moneyvox.fr/votre-argent/actualites/108378/moodys-maintient-la-note-souveraine-de-la-france-soulignant-des-progres", "presse"),
    ("fr", "fitch", "A+", "Stable", "2025-09-12 (abaissement) ; confirmee 2026-08-28", "https://www.economiematin.fr/dette-francaise-fitch-maintient-note-malgre-signaux-alarmants", "presse"),
    ("gr", "sp", "BBB", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("gr", "moodys", "Baa3", "Positive", "2026-09-18 (perspective -> positive)", "https://cyprus-mail.com/2026/09/19/moodys-lifts-greece-outlook-on-stronger-economic-resilience", "presse"),
    ("gr", "fitch", "BBB", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("in", "sp", "BBB", "Stable", "2025-08-14", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("in", "moodys", "Baa3", "Stable", "2023", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("in", "fitch", "BBB-", "Stable", "2025-08-25", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("ie", "sp", "AA+", "Stable", "2026-03-20 (relevement)", "https://www.ntma.ie/business-areas/funding-and-debt-management/investor-relations/credit-ratings", "dmo"),
    ("ie", "moodys", "Aa2", "Positive", "2026-08-21 (relevement Aa3->Aa2, perspective positive maintenue)", "https://www.ntma.ie/news/ntma-welcomes-moodys-upgrade-of-irelands-sovereign-debt-rating-to-aa2", "dmo"),
    ("ie", "fitch", "AA", "Stable", "non verifiee (Wikipedia 07/11/2025)", "https://www.ntma.ie/business-areas/funding-and-debt-management/investor-relations/credit-ratings", "dmo"),
    ("it", "sp", "BBB+", "Positive", "2026-01-30 (perspective -> positive) ; confirmee 2026-05-15 ; prochaine 2026-11-13", "https://www.orafinanza.it/it/rating-italia-s&p-fitch-moody-dbrs", "presse"),
    ("it", "moodys", "Baa2", "Stable", "2025-11-21 (relevement) ; revue periodique 2026-09-25 sans action", "https://www.orafinanza.it/it/rating-italia-s&p-fitch-moody-dbrs", "presse"),
    ("it", "fitch", "BBB+", "Stable", "2025-09-19 (relevement) ; confirmee 2026-03-13 et 2026-09-11", "https://www.orafinanza.it/it/rating-italia-s&p-fitch-moody-dbrs", "presse"),
    ("jp", "sp", "A+", "Stable", "note depuis 2015-09-16 ; perspective stable depuis 2020-06-09", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("jp", "moodys", "A1", "Stable", "note et perspective depuis 2014-12-01", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("jp", "fitch", "A", "Stable", "note depuis 2015-04-27 ; perspective stable depuis 2022-03-25", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("nl", "sp", "AAA", "non verifiee", "non verifiee", "https://english.dsta.nl/subjects/c/capital-markets", "dmo"),
    ("nl", "moodys", "Aaa", "non verifiee", "non verifiee", "https://english.dsta.nl/subjects/c/capital-markets", "dmo"),
    ("nl", "fitch", "AAA", "non verifiee", "non verifiee", "https://english.dsta.nl/subjects/c/capital-markets", "dmo"),
    ("pt", "sp", "A+", "Positive", "relevement 2025-08 ; perspective positive (date non verifiee, avant 03/04/2026)", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("pt", "moodys", "A3", "Stable", "2023-11-17 probable (aucun relevement Moody's cite par le ministere en 2024-2026)", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("pt", "fitch", "A+", "Stable", "2026-09-04 (relevement A->A+)", "https://econews.pt/2026/09/07/fitch-upgrades-portugal-to-a-with-stable-outlook/", "presse"),
    ("gb", "sp", "AA", "Stable", "revue avril 2026 (jour non verifie)", "https://www.stockopedia.com/share-prices/morningstar-NSQ:MORN/news/rating-agencies-weigh-up-uk-apos-s-burnham-factor-updated-019ed077-b420-7e37-831c-79eba5333a8d/", "presse"),
    ("gb", "moodys", "Aa3", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("gb", "fitch", "AA-", "Stable", "non verifiee", "https://www.mof.go.jp/english/policy/jgbs/publication/debt_management_report/2026/esaimu2026.pdf", "dmo"),
    ("ch", "sp", "AAA", "Stable", "non verifiee", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("ch", "moodys", "Aaa", "Stable", "non verifiee", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    ("ch", "fitch", "AAA", "Stable", "non verifiee", "https://en.wikipedia.org/wiki/List_of_countries_by_credit_rating", "wikipedia"),
    # 02/10/2026 — les 11 autres pays de la zone euro, pour « le marché contre les agences »
    # (oblig_notes.py) : relevés au registre réglementaire de l'ESMA, tenus à jour par
    # notations_auto. Estonie : S&P a RETIRÉ sa note (ESMA, 31/12/2024) → pas de ligne.
    ("fi", "moodys", "Aa1", "Stable", "2024-12-13 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("fi", "sp", "AA+", "Negative", "2026-04-24 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("fi", "fitch", "AA", "Stable", "2026-07-17 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("sk", "moodys", "A3", "Stable", "2024-12-13 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("sk", "sp", "A", "Stable", "2026-04-24 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("sk", "fitch", "A-", "Stable", "2026-05-08 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lt", "moodys", "A2", "Stable", "2026-04-17 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lt", "sp", "A", "Stable", "2026-05-29 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lt", "fitch", "A+", "Stable", "2026-04-24 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("si", "moodys", "A2", "Stable", "2026-02-27 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("si", "sp", "AA", "Stable", "2026-03-27 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("si", "fitch", "A+", "Stable", "2026-09-11 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lv", "moodys", "A3", "Stable", "2026-01-16 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lv", "sp", "A", "Stable", "2026-05-29 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lv", "fitch", "A-", "Stable", "2026-04-24 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("ee", "moodys", "A1", "Stable", "2026-02-13 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("ee", "fitch", "A+", "Stable", "2026-06-05 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lu", "moodys", "Aaa", "Stable", "2025-02-07 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lu", "sp", "AAA", "Stable", "2026-07-31 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("lu", "fitch", "AAA", "Stable", "2026-04-24 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("mt", "moodys", "A2", "Stable", "2024-11-22 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("mt", "sp", "A-", "Stable", "2025-12-05 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("mt", "fitch", "A+", "Stable", "2026-08-21 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("cy", "moodys", "A3", "Stable", "2024-11-22 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("cy", "sp", "A", "Positive", "2026-09-18 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("cy", "fitch", "A-", "Positive", "2026-05-08 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("hr", "moodys", "A3", "Stable", "2024-11-08 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("hr", "sp", "A", "Stable", "2026-03-13 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("hr", "fitch", "A-", "Stable", "2026-07-31 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("bg", "moodys", "Baa1", "Stable", "2025-01-24 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("bg", "sp", "BBB+", "Positive", "2026-05-15 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
    ("bg", "fitch", "BBB+", "Positive", "2026-09-25 (ESMA)", "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar", "ESMA"),
]
ECHELLE_SP = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
              "CCC+", "CCC", "CCC-", "CC", "C", "D"]
ECHELLE_MOODYS = ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3", "B1", "B2", "B3",
                  "Caa1", "Caa2", "Caa3", "Ca", "C", "D"]
PERSP = {"stable": "stable", "negative": "négative", "positive": "positive", "developing": "en évolution"}


def cran(note, agence):
    n = (note or "").replace("−", "-").strip()
    ech = ECHELLE_MOODYS if agence == "moodys" else ECHELLE_SP
    return ech.index(n) if n in ech else None


def lecture(c):
    if c is None:
        return None
    if c == 0:
        return "qualité maximale"
    if c <= 3:
        return "très haute qualité"
    if c <= 6:
        return "haute qualité"
    if c <= 9:
        return "qualité moyenne"
    return "spéculative"


def _persp_table(persp):
    t = (persp or "").lower()
    for k, v in PERSP.items():
        if t.startswith(k):
            return v
    return None


def notations(journal=None, changes=None):
    out = defaultdict(dict)
    for p, ag, note, persp, der, url, typ in NOTATIONS:
        out[p][ag] = {"note": note, "perspective": _persp_table(persp), "action": der, "url": url, "type": typ}
    # Mise à jour AUTOMATIQUE (ESMA, puis Wikipédia en détecteur daté) : la table
    # vérifiée à la main n'est qu'un point de départ, jamais une valeur figée.
    try:
        from oblig_frais import notations_auto
        out, ch = notations_auto(journal if journal is not None else [], out)
        if changes is not None:
            changes.extend(ch)
    except Exception as e:  # noqa: BLE001
        if journal is not None:
            journal.append("notations automatiques : " + str(e)[:100])
    for p in out:
        for ag in out[p]:
            out[p][ag]["cran"] = cran(out[p][ag]["note"], ag)
    res = {}
    for p, a in out.items():
        crans = sorted(x["cran"] for x in a.values() if x["cran"] is not None)
        med = crans[len(crans) // 2] if crans else None
        res[p] = {"agences": a, "composite": ECHELLE_SP[med] if med is not None else None, "cran": med,
                  "lecture": lecture(med), "verifie_le": VERIFIE_LE,
                  "negatives": sum(1 for x in a.values() if x["perspective"] == "négative"),
                  "positives": sum(1 for x in a.values() if x["perspective"] == "positive"),
                  "dernier_changement": max((ag2.get("action") or "")[:10] for ag2 in a.values())}
    return res


# ── FMI ─────────────────────────────────────────────────────────────────────
IMF_IND = {"dette_pib": "GGXWDG_NGDP", "solde_pib": "GGXCNL_G01_GDP_PT", "primaire_pib": "GGXONLB_G01_GDP_PT",
           "recettes_pib": "GGR_G01_GDP_PT", "croissance_reelle": "NGDP_RPCH", "inflation": "PCPIPCH"}


def fmi(journal):
    out = defaultdict(dict)
    meta = get_json("https://www.imf.org/external/datamapper/api/v1/indicators", ua=None, accept="application/json") or {}
    for cle, ind in IMF_IND.items():
        j = get_json("https://www.imf.org/external/datamapper/api/v1/" + ind, ua=None, accept="application/json", timeout=120)
        vals = ((j or {}).get("values") or {}).get(ind) or {}
        if not vals:
            journal.append("FMI %s vide" % ind)
        for c, i3 in ISO3.items():
            s = vals.get(i3) or {}
            out[c][cle] = {int(a): round(v, 2) for a, v in s.items() if estnb(v) and 1990 <= int(a) <= 2031}
    # croissance NOMINALE (pour r − g) : PIB nominal du WEO, portail SDMX
    t = get_txt("https://api.imf.org/external/sdmx/2.1/data/IMF.RES,WEO/%s.NGDP.A?startPeriod=2000&endPeriod=2031"
                % "+".join(ISO3.values()), accept="application/vnd.sdmx.data+csv;version=1.0.0", ua=None, timeout=150)
    der_obs = {}
    if t:
        niv = defaultdict(dict)
        for r in csv.DictReader(io.StringIO(t)):
            try:
                niv[r["COUNTRY"]][int(r["TIME_PERIOD"])] = float(r["OBS_VALUE"])
            except (KeyError, ValueError):
                continue
            if r.get("LATEST_ACTUAL_ANNUAL_DATA"):
                der_obs[r["COUNTRY"]] = r["LATEST_ACTUAL_ANNUAL_DATA"]
        for c, i3 in ISO3.items():
            n = niv.get(i3, {})
            out[c]["croissance_nominale"] = {a: round(100 * (n[a] / n[a - 1] - 1), 2) for a in n if a - 1 in n}
            if i3 in der_obs:
                try:
                    out[c]["dernier_realise"] = int(str(der_obs[i3])[:4])
                except ValueError:
                    pass
    else:
        journal.append("FMI SDMX (PIB nominal) vide")
    for c in out:
        pb, sb, rv = out[c].get("primaire_pib", {}), out[c].get("solde_pib", {}), out[c].get("recettes_pib", {})
        out[c]["interets_nets_pib"] = {a: round(pb[a] - sb[a], 2) for a in pb if a in sb}
        out[c]["interets_nets_recettes"] = {a: round(100 * (pb[a] - sb[a]) / rv[a], 1) for a in pb if a in sb and rv.get(a)}
    edition = ((meta.get("indicators") or {}).get("GGXWDG_NGDP") or {}).get("source")
    return dict(out), edition


# ── Eurostat ────────────────────────────────────────────────────────────────
def _cube(ds, **flt):
    q = [("format", "JSON"), ("lang", "en")]
    for k, v in flt.items():
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            q.append((k, x))
    d = get_json("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/" + ds + "?" + urllib.parse.urlencode(q),
                 accept="application/json", timeout=120)
    if not d or "id" not in d:
        return None, None, None
    dims, taille = d["id"], d["size"]
    cat = {k: list(d["dimension"][k]["category"]["index"]) for k in dims}

    def val(**coord):
        pos = 0
        for k, s in zip(dims, taille):
            i = cat[k].index(coord[k]) if k in coord and coord[k] in cat[k] else 0
            pos = pos * s + i
        return d["value"].get(str(pos))
    return d, cat, val


def eurostat(journal):
    geos = list(EUROSTAT.values())
    inv = {v: k for k, v in EUROSTAT.items()}
    out = defaultdict(dict)
    try:
        d, cat, val = _cube("gov_10dd_edpt1", unit="PC_GDP", sector="S13", na_item=["GD", "B9", "D41PAY"], geo=geos, sinceTimePeriod="2000")
        if d:
            for g in geos:
                for k, nom in (("GD", "dette"), ("B9", "solde"), ("D41PAY", "interets_bruts")):
                    out[inv[g]][nom] = {int(t): round(val(na_item=k, geo=g, time=t), 2) for t in cat["time"] if val(na_item=k, geo=g, time=t) is not None}
            out["_maj_edp"] = d.get("updated")
        d, cat, val = _cube("gov_10a_main", unit="PC_GDP", sector="S13", na_item="TR", geo=geos, sinceTimePeriod="2000")
        if d:
            for g in geos:
                out[inv[g]]["recettes"] = {int(t): round(val(geo=g, time=t), 2) for t in cat["time"] if val(geo=g, time=t) is not None}
                ib, rv = out[inv[g]].get("interets_bruts", {}), out[inv[g]]["recettes"]
                out[inv[g]]["interets_bruts_recettes"] = {a: round(100 * ib[a] / rv[a], 1) for a in ib if rv.get(a)}
        d, cat, val = _cube("gov_10q_ggdebt", unit="PC_GDP", sector="S13", na_item="GD", geo=geos, sinceTimePeriod="2019-Q1")
        if d:
            for g in geos:
                s = [(t, val(geo=g, time=t)) for t in cat["time"]]
                s = [(t, round(v, 1)) for t, v in s if v is not None]
                if s:
                    out[inv[g]]["dette_trim"] = {"t": [x[0] for x in s], "v": [x[1] for x in s]}
            out["_maj_trim"] = d.get("updated")
        d, cat, val = _cube("gov_10dd_ggd", unit="MIO_EUR", sector="S13", na_item="GD", maturity="TOTAL", geo=geos, sinceTimePeriod="2015")
        if d:
            lib = {"S2": "non_residents", "S121": "banque_centrale", "S122_S123": "banques",
                   "S124-S129": "assureurs_fonds", "S14_S15": "menages"}
            for g in geos:
                series = {}
                for t in cat["time"]:
                    tot = val(sector2="S1_S2", geo=g, time=t)
                    if not tot:
                        continue
                    for k, v in lib.items():
                        x = val(sector2=k, geo=g, time=t)
                        if x is not None:
                            series.setdefault(v, {})[int(t)] = round(100 * x / tot, 1)
                if series:
                    an = max(max(s) for s in series.values())
                    out[inv[g]]["detenteurs"] = {"annee": an, "parts": {k: s.get(an) for k, s in series.items()},
                                                 "series": {k: s for k, s in series.items()}}
    except Exception as e:  # noqa: BLE001
        journal.append("Eurostat : " + str(e)[:140])
    return dict(out)


# ── BCE : structure de la dette (mensuel) ───────────────────────────────────
def bce_structure(journal):
    ref = "+".join(BCE.values())
    inv = {v: k for k, v in BCE.items()}
    out = defaultdict(dict)

    def lignes(cle, n=1):
        t = get_txt("https://data-api.ecb.europa.eu/service/data/GFS/%s?format=csvdata&lastNObservations=%d" % (cle, n),
                    entetes={"Accept": "text/csv"}, timeout=120)
        return list(csv.DictReader(io.StringIO(t))) if t else []
    try:
        for r in lignes("M.N.%s.W0.S13.S1.N.L.LE.F3+F3B.T+TT._Z.YR+RT+EUR._T.F.V.A1+N._T" % ref):
            k = (r["INSTR_ASSET"], r["MATURITY"], r["UNIT_MEASURE"])
            nom = {("F3", "TT", "YR"): "maturite_residuelle", ("F3", "T", "YR"): "maturite_initiale",
                   ("F3", "T", "RT"): "cout_moyen", ("F3", "T", "EUR"): "encours_meur",
                   ("F3B", "T", "EUR"): "variable_indexe_meur"}.get(k)
            if nom and r["REF_AREA"] in inv:
                out[inv[r["REF_AREA"]]][nom] = round(float(r["OBS_VALUE"]), 3)
                out[inv[r["REF_AREA"]]]["mois"] = r["TIME_PERIOD"]
        for r in lignes("M.N.%s.W0.S13.S1.N.L.LE.F3.TM13+TM3C+TY12+TY25+TY5_._Z.EUR._T.F.V.N._T" % ref):
            if r["REF_AREA"] in inv:
                out[inv[r["REF_AREA"]]].setdefault("echeancier_meur", {})[r["MATURITY"]] = round(float(r["OBS_VALUE"]), 0)
        # le coût moyen dans le temps (rendement moyen du stock)
        for r in lignes("M.N.%s.W0.S13.S1.N.L.LE.F3.T._Z.RT._T.F.V.A1._T" % ref, 120):
            if r["REF_AREA"] in inv:
                out[inv[r["REF_AREA"]]].setdefault("cout_serie", []).append((r["TIME_PERIOD"], round(float(r["OBS_VALUE"]), 3)))
        for c, x in out.items():
            if x.get("encours_meur") and x.get("variable_indexe_meur") is not None:
                x["part_variable_indexe"] = round(100 * x["variable_indexe_meur"] / x["encours_meur"], 1)
            if x.get("cout_serie"):
                s = sorted(x["cout_serie"])
                x["cout_serie"] = {"m": [a for a, _ in s], "v": [b for _, b in s]}
    except Exception as e:  # noqa: BLE001
        journal.append("BCE structure : " + str(e)[:140])
    return dict(out)


# ── États-Unis : Trésor (Fiscal Data) ───────────────────────────────────────
FD = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"


def _fd(chemin, **q):
    j = get_json(FD + chemin + "?" + urllib.parse.urlencode(q), accept="application/json", timeout=120)
    return (j or {}).get("data") or []


def etats_unis(journal):
    out = {}
    try:
        p = _fd("/v2/accounting/od/debt_to_penny", sort="-record_date", **{"page[size]": 1})
        if p:
            out["dette_du_jour"] = {"date": p[0]["record_date"], "totale_md": round(float(p[0]["tot_pub_debt_out_amt"]) / 1e9, 1),
                                    "public_md": round(float(p[0]["debt_held_public_amt"]) / 1e9, 1)}
        r = _fd("/v2/accounting/od/avg_interest_rates", sort="-record_date", **{"page[size]": 40})
        if r:
            d0 = r[0]["record_date"]
            m = {x["security_desc"]: float(x["avg_interest_rate_amt"]) for x in r if x["record_date"] == d0 and x["security_type_desc"] == "Marketable"}
            out["cout_moyen"] = {"date": d0, "negociable": m.get("Total Marketable"), "detail": m}
        a = _fd("/v1/accounting/od/auctions_query", sort="-auction_date",
                filter="original_security_term:eq:10-Year,inflation_index_security:eq:No,security_type:eq:Note", **{"page[size]": 12})
        out["adjudications_10a"] = [{"date": x["auction_date"], "taux": float(x["high_yield"]) if x.get("high_yield") not in (None, "null") else None,
                                     "couverture": float(x["bid_to_cover_ratio"]) if x.get("bid_to_cover_ratio") not in (None, "null") else None,
                                     "montant_md": round(float(x["offering_amt"]) / 1e9, 1) if x.get("offering_amt") not in (None, "null") else None,
                                     "etrangers_pct": round(100 * float(x["indirect_bidder_accepted"]) / float(x["total_accepted"]), 1)
                                     if x.get("indirect_bidder_accepted") not in (None, "null") and x.get("total_accepted") not in (None, "null", "0") else None,
                                     "reouverture": x.get("reopening") == "Yes"} for x in a]
        der = _fd("/v1/debt/mspd/mspd_table_3_market", sort="-record_date", **{"page[size]": 1})
        if der:
            last = der[0]["record_date"]
            rows = _fd("/v1/debt/mspd/mspd_table_3_market", filter="record_date:eq:" + last, **{"page[size]": 10000})
            asof = date.fromisoformat(last)
            tot = w = 0.0
            prof, typ = defaultdict(float), defaultdict(float)
            for x in rows:
                o, mm = x.get("outstanding_amt"), x.get("maturity_date")
                if o in (None, "null", "*") or mm in (None, "null") or "Total" in (x.get("security_class2_desc") or ""):
                    continue
                try:
                    md = date.fromisoformat(mm)
                    v = float(o)
                except ValueError:
                    continue
                if md <= asof:
                    continue
                tot += v
                w += v * (md - asof).days / 365.25
                prof[md.year] += v
                typ[x.get("security_class1_desc") or "?"] += v
            if tot:
                out["structure"] = {"date": last, "negociable_md": round(tot / 1e3, 0), "maturite_moy_ans": round(w / tot, 2),
                                    "parts": {k: round(100 * v / tot, 1) for k, v in typ.items()},
                                    "echeancier_md": {str(y): round(v / 1e3, 0) for y, v in sorted(prof.items())[:12]}}
        ofs = _fd("/v1/accounting/tb/ofs2_estimated_ownership_treasury_securities", sort="-record_date", **{"page[size]": 80})
        par = defaultdict(dict)
        for x in ofs:
            if x.get("securities_bil_amt") not in (None, "null"):
                par[x["end_of_month"]][x["securities_owner"]] = float(x["securities_bil_amt"])
        compl = [d for d, v in par.items() if "Foreign And International" in v and "Total Public Debt" in v]
        if compl:
            d = max(compl)
            v = par[d]
            tot = v["Total Public Debt"]
            out["detenteurs"] = {"date": d, "total_md": tot, "parts": {k: round(100 * x / tot, 1) for k, x in v.items() if k != "Total Public Debt"}}
        fed = fred("TREAST")
        if fed and out.get("structure"):
            out["fed_detient"] = {"date": fed[-1][0], "md": round(fed[-1][1] / 1e3, 0),
                                  "part_negociable": round(100 * fed[-1][1] / 1e3 / out["structure"]["negociable_md"], 1)}
    except Exception as e:  # noqa: BLE001
        journal.append("États-Unis (Fiscal Data) : " + str(e)[:140])
    return out


def japon(journal):
    codes = {"FOF_FFAS700L311": "total", "FOF_FFAS110A311": "banque_du_japon", "FOF_FFAS120A311": "banques",
             "FOF_FFAS131A311": "assureurs", "FOF_FFAS140A311": "fonds_de_pension", "FOF_FFAS500A311": "non_residents",
             "FOF_FFAS430A311": "menages"}
    try:
        d = get_json("https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en&db=FF&startDate=%d01&code=%s"
                     % (date.today().year - 6, ",".join(codes)), accept="application/json", timeout=120)
        niv = {}
        for r in (d or {}).get("RESULTSET", []):
            v = r.get("VALUES") or {}
            niv[codes[r["SERIES_CODE"]]] = list(zip(v.get("SURVEY_DATES", []), v.get("VALUES", [])))
        if "total" not in niv:
            return {}
        tot = dict(niv["total"])
        der = niv["total"][-1][0]
        parts = {k: round(100 * dict(s)[der] / tot[der], 1) for k, s in niv.items() if k != "total" and der in dict(s) and dict(s)[der] is not None}
        serie_bj = [(t, round(100 * v / tot[t], 1)) for t, v in niv.get("banque_du_japon", []) if tot.get(t) and v is not None]
        return {"detenteurs": {"periode": str(der), "parts": parts, "total_billions_jpy": round(tot[der] / 1e4, 0)},
                "part_boj": {"t": [str(a) for a, _ in serie_bj], "v": [b for _, b in serie_bj]}}
    except Exception as e:  # noqa: BLE001
        journal.append("Japon (BoJ) : " + str(e)[:140])
        return {}


# ── Taux directeurs ─────────────────────────────────────────────────────────
def taux_directeurs(journal):
    out = {}
    try:
        t = get_txt("https://stats.bis.org/api/v1/data/WS_CBPOL/D.US+XM+GB+JP+CH+CA+AU+CN+IN/all?startPeriod=%d-01-01" % (date.today().year - 3),
                    accept="application/vnd.sdmx.data+csv;version=1.0.0", timeout=120)
        par = defaultdict(list)
        for r in csv.DictReader(io.StringIO(t or "")):
            v = r.get("OBS_VALUE")
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if x == x:                      # ⚠ la BRI publie des NaN (week-ends au Canada, trous)
                par[r["REF_AREA"]].append((r["TIME_PERIOD"], x))
        cle = {"US": "us", "XM": "ez", "GB": "gb", "JP": "jp", "CH": "ch", "CA": "ca", "AU": "au", "CN": "cn", "IN": "in"}
        for a, s in par.items():
            s.sort()
            der = s[-1]
            # date du dernier CHANGEMENT
            chg = None
            for i in range(len(s) - 1, 0, -1):
                if s[i][1] != s[i - 1][1]:
                    chg = (s[i][0], round(s[i][1] - s[i - 1][1], 3))
                    break
            out[cle[a]] = {"taux": der[1], "date": der[0], "dernier_mouvement": chg, "source": "BRI (taux directeurs)"}
        # Sources nationales, plus fraîches que la BRI
        b = get_txt("https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV?format=csvdata&lastNObservations=1",
                    entetes={"Accept": "text/csv"})
        for r in csv.DictReader(io.StringIO(b or "")):
            out.setdefault("ez", {}).update({"taux": float(r["OBS_VALUE"]), "date": r["TIME_PERIOD"], "source": "BCE (taux de dépôt)", "nom": "taux de dépôt"})
        lo, hi = fred("DFEDTARL", "2024-01-01"), fred("DFEDTARU", "2024-01-01")
        if lo and hi:
            out.setdefault("us", {}).update({"taux": hi[-1][1], "bas": lo[-1][1], "date": hi[-1][0], "source": "Réserve fédérale (fourchette cible)", "nom": "fonds fédéraux"})
            for i in range(len(hi) - 1, 0, -1):
                if hi[i][1] != hi[i - 1][1]:
                    out["us"]["dernier_mouvement"] = (hi[i][0], round(hi[i][1] - hi[i - 1][1], 3))
                    break
    except Exception as e:  # noqa: BLE001
        journal.append("taux directeurs : " + str(e)[:140])
    for c, n in (("gb", "Bank Rate"), ("jp", "taux directeur"), ("ch", "taux directeur"), ("ca", "taux cible"), ("au", "cash rate"), ("cn", "LPR 1 an"), ("in", "repo")):
        if c in out:
            out[c].setdefault("nom", n)
    return out


BANQUE_CENTRALE = {"us": "us", "de": "ez", "fr": "ez", "it": "ez", "es": "ez", "nl": "ez", "be": "ez", "at": "ez", "ie": "ez",
                   "pt": "ez", "gr": "ez", "ez": "ez", "gb": "gb", "jp": "jp", "ca": "ca", "au": "au", "ch": "ch", "cn": "cn", "in": "in"}


def construire(journal):
    imf, edition = {}, None
    try:
        imf, edition = fmi(journal)
    except Exception as e:  # noqa: BLE001
        journal.append("FMI : " + str(e)[:140])
    es = eurostat(journal)
    bce = bce_structure(journal)
    us = etats_unis(journal)
    jp = japon(journal)
    td = taux_directeurs(journal)
    changements_notes = []
    nt = notations(journal, changements_notes)
    an = date.today().year
    out = {}
    for c in ISO3:
        f = imf.get(c, {})
        x = {"fmi": f, "fmi_edition": edition}

        def v(cle, a):
            return (f.get(cle) or {}).get(a)
        x["resume"] = {
            "dette_pib": v("dette_pib", an - 1), "dette_pib_prev": v("dette_pib", an), "dette_pib_5a": v("dette_pib", an + 4),
            "solde_pib": v("solde_pib", an), "primaire_pib": v("primaire_pib", an),
            "interets_pib": (f.get("interets_nets_pib") or {}).get(an), "interets_recettes": (f.get("interets_nets_recettes") or {}).get(an),
            "croissance_nominale": (f.get("croissance_nominale") or {}).get(an), "croissance_reelle": v("croissance_reelle", an),
            "inflation": v("inflation", an), "annee": an, "dernier_realise": f.get("dernier_realise")}
        if c in es:
            e = es[c]
            x["eurostat"] = e
            ib = e.get("interets_bruts") or {}
            if ib:
                a = max(ib)
                x["resume"]["interets_bruts_pib"] = ib[a]
                x["resume"]["interets_bruts_recettes"] = (e.get("interets_bruts_recettes") or {}).get(a)
                x["resume"]["interets_bruts_annee"] = a
            if e.get("dette_trim"):
                x["resume"]["dette_trim"] = e["dette_trim"]["v"][-1]
                x["resume"]["dette_trim_t"] = e["dette_trim"]["t"][-1]
            if e.get("detenteurs"):
                x["detenteurs"] = dict(e["detenteurs"], source="Eurostat (dette des administrations publiques)")
        if c in bce:
            x["structure"] = dict(bce[c], source="BCE, statistiques de finances publiques (titres des administrations publiques)")
        if c == "us" and us:
            x["us"] = us
            if us.get("detenteurs"):
                x["detenteurs"] = dict(us["detenteurs"], source="Trésor américain, Treasury Bulletin (OFS-2)")
            if us.get("structure"):
                x["structure"] = {"maturite_residuelle": us["structure"]["maturite_moy_ans"], "mois": us["structure"]["date"],
                                  "cout_moyen": (us.get("cout_moyen") or {}).get("negociable"),
                                  "echeancier_md": us["structure"]["echeancier_md"], "parts": us["structure"]["parts"],
                                  "source": "Trésor américain (situation mensuelle de la dette, taux moyens)"}
        if c == "jp" and jp:
            x["detenteurs"] = dict(jp["detenteurs"], source="Banque du Japon (comptes financiers)")
            x["part_bc_serie"] = jp.get("part_boj")
        # r − g : le taux APPARENT de la dette (coût moyen du stock) contre la croissance nominale
        r = (x.get("structure") or {}).get("cout_moyen")
        g = x["resume"]["croissance_nominale"]
        if estnb(r) and estnb(g):
            x["resume"]["r_moins_g"] = round(r - g, 2)
            x["resume"]["r_source"] = "coût moyen de la dette en circulation"
        bc = td.get(BANQUE_CENTRALE.get(c))
        if bc:
            x["banque_centrale"] = bc
        if c in nt:
            x["notation"] = nt[c]
        out[c] = x
    out["ez"] = {"banque_centrale": td.get("ez")}
    out["_changements_notes"] = changements_notes
    out["_notations"] = nt          # tous les pays notés, zone euro entière comprise (oblig_notes)
    return out
