#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ÉMETTEURS — une fiche obligataire par grande entreprise (sous-onglet
« Obligations » de l'Analyse fondamentale, obligations.js côté site). Une fois
par jour.

POURQUOI : les cinq fiches « crédit » sont des PANIERS (des indices). Elles
disent le prix moyen du risque, jamais celui d'Oracle ou de Meta — or c'est
entreprise par entreprise que le marché trie, et c'est là que la vague
d'emprunts de l'IA (2025-2026) se lit.

CE QUE L'ON MESURE, pour chaque émetteur :
  - chacune de ses obligations : prix, rendement, ÉCART À L'ÉTAT à la même
    échéance (rendement − taux d'État interpolé à la durée restante) ;
  - sa COURBE : le taux auquel le marché lui prête à 5, 10 et 30 ans, jour
    après jour depuis que ses obligations cotent ;
  - ses émissions des trois dernières années (montant, taux et écart le jour
    de l'émission) et le mur des échéances (ce qu'il rembourse, an par an) ;
  - ses notes (S&P, Moody's, Fitch) et leur histoire (registre ESMA) ;
  - sa capacité à payer (SEC : dette, trésorerie, intérêts, EBITDA,
    investissements) — sociétés américaines hors banques.

SOURCES (publiques et gratuites ; aucune signature anti-robot contournée) :
  - SPDR (State Street) : avoirs quotidiens de 8 ETF obligataires → l'univers
    (~13 000 obligations en dollars et en euros), le prix du jour, le repli ;
  - onvista : fiche (date, montant et prix d'émission) et cours quotidien de
    chaque obligation sur les places allemandes, depuis sa cotation ;
  - Trésor américain / FRED et Bundesbank : les courbes d'État (fonctions de
    oblig_souverains, rien n'est collecté deux fois) ; BCE/FRED : euro-dollar ;
  - ESMA (registre RADAR) : les notes ; SEC (companyfacts) : les comptes.

⚠ Les obligations « 144A » (placements privés américains : CoreWeave, les
  data centers de l'IA…) ne cotent pas en Allemagne. Leur historique commence
  le jour où ce collecteur les a vues : prix SPDR, cumulés de passage en
  passage dans la fiche elle-même (`px_spdr`).
⚠ Écart « G-spread » (rendement − État interpolé), pas OAS. Les titres de
  banque à taux fixe puis variable sont rendus à leur date de rappel.
⚠ Registre ESMA : des notes jamais retirées y dorment (Fitch sur AMD : CCC en
  2016). Une agence sans aucune action depuis 24 mois est écartée.
"""
import bisect
import calendar
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

from oblig_net import get, get_json, log, estnb, fred, variations, il_y_a  # noqa: E402
import oblig_souverains  # noqa: E402
from oblig_pays import ECHELLE_SP, ECHELLE_MOODYS, lecture  # noqa: E402
from fetch_obligations import ecrire, lire_json, nettoyer, compacter_arbre  # noqa: E402

BUDGET_S = float(os.environ.get("SCF_EM_BUDGET_S") or 3000)      # 50 min : le seau quotidien coupe à 70
SEULEMENT = [x for x in (os.environ.get("SCF_EM_SEULEMENT") or "").split(",") if x]

FAMILLES = [("ia", "Tech et IA"), ("dc", "Data centers de l'IA"), ("banques", "Banques"),
            ("telecoms", "Télécoms et médias"), ("sante", "Santé"), ("conso", "Consommation"),
            ("industrie", "Industrie et automobile"), ("energie", "Énergie")]


def E(code, nom, fam, pays, sym, usd, eur, esma, nom_long=None):
    return {"code": code, "nom": nom, "famille": fam, "pays": pays, "sym": sym, "usd": usd, "eur": eur,
            "esma": esma, "nom_long": nom_long or nom}


# Les clés SPDR sont les noms d'émetteur des fichiers d'avoirs, tranche retirée
# (voir `cle_us` / `cle_eu`). Les noms ESMA sont EXACTS : la recherche large
# attrape des homonymes (« Dell » → Banco de Sabadell, « Cisco » → San Francisco).
EMETTEURS = [
    # ── Tech et IA ──
    E("apple", "Apple", "ia", "États-Unis", "AAPL", ["APPLE INC"], ["APPLE INC"], ["Apple Inc."]),
    E("microsoft", "Microsoft", "ia", "États-Unis", "MSFT", ["MICROSOFT CORP"], ["MICROSOFT CORP"], ["Microsoft Corporation", "Microsoft Corp."]),
    E("alphabet", "Alphabet", "ia", "États-Unis", "GOOGL", ["ALPHABET INC"], ["ALPHABET INC"], ["Alphabet Inc."], "Alphabet (Google)"),
    E("amazon", "Amazon", "ia", "États-Unis", "AMZN", ["AMAZON.COM INC"], ["AMAZON.COM INC"], ["Amazon.com, Inc.", "Amazon.com Inc."]),
    E("meta", "Meta", "ia", "États-Unis", "META", ["META PLATFORMS INC"], [], ["Meta Platforms, Inc."], "Meta Platforms"),
    E("oracle", "Oracle", "ia", "États-Unis", "ORCL", ["ORACLE CORP"], [], ["Oracle Corporation", "Oracle Corp."]),
    E("broadcom", "Broadcom", "ia", "États-Unis", "AVGO", ["BROADCOM INC", "BROADCOM CRP /  CAYMN FI", "BROADCOM CRP / CAYMN FI"], [],
      ["Broadcom Inc.", "Broadcom Inc"]),
    E("nvidia", "Nvidia", "ia", "États-Unis", "NVDA", ["NVIDIA CORP"], [], ["NVIDIA Corp.", "NVIDIA Corporation", "Nvidia Corporation"]),
    E("intel", "Intel", "ia", "États-Unis", "INTC", ["INTEL CORP"], [], ["Intel Corporation", "Intel Corp."]),
    E("ibm", "IBM", "ia", "États-Unis", "IBM", ["IBM CORP", "IBM INTERNAT CAPITAL"], ["IBM CORP"],
      ["International Business Machines Corporation", "International Business Machines Corp."]),
    E("cisco", "Cisco", "ia", "États-Unis", "CSCO", ["CISCO SYSTEMS INC"], [], ["Cisco Systems Inc.", "Cisco Systems, Inc."]),
    E("dell", "Dell", "ia", "États-Unis", "DELL", ["DELL INT LLC / EMC CORP", "DELL INC"], [],
      ["Dell International LLC", "Dell International L.L.C."], "Dell Technologies"),
    E("salesforce", "Salesforce", "ia", "États-Unis", "CRM", ["SALESFORCE INC"], [], ["Salesforce, Inc.", "Salesforce Inc."]),
    E("amd", "AMD", "ia", "États-Unis", "AMD", ["ADVANCED MICRO DEVICES"], [], ["Advanced Micro Devices Inc.", "Advanced Micro Devices, Inc."]),
    E("qualcomm", "Qualcomm", "ia", "États-Unis", "QCOM", ["QUALCOMM INC"], [], ["Qualcomm Incorporated", "QUALCOMM Incorporated"]),
    E("texas", "Texas Instruments", "ia", "États-Unis", "TXN", ["TEXAS INSTRUMENTS INC"], [],
      ["Texas Instruments Inc.", "Texas Instruments, Incorporated", "Texas Instruments Incorporated"]),
    E("hpe", "HPE", "ia", "États-Unis", "HPE", ["HP ENTERPRISE CO"], [],
      ["Hewlett Packard Enterprise Company", "Hewlett Packard Enterprise Co."], "Hewlett Packard Enterprise"),
    E("netflix", "Netflix", "ia", "États-Unis", "NFLX", ["NETFLIX INC"], ["NETFLIX INC"], ["Netflix Inc", "Netflix, Inc."]),
    E("equinix", "Equinix", "ia", "États-Unis", "EQIX", ["EQUINIX INC", "EQUINIX EU 2 FINANCING C", "EQUINIX ASIA FIN CORP"],
      ["EQUINIX INC", "EQUINIX EU 2 FINANCING C"], ["Equinix, Inc.", "Equinix Inc."]),
    E("softbank", "SoftBank", "ia", "Japon", "9984.T", ["SOFTBANK GROUP CORP"], ["SOFTBANK GROUP CORP"], ["SoftBank Group Corp."], "SoftBank Group"),
    # ── Data centers de l'IA (haut rendement, placements 144A) ──
    E("coreweave", "CoreWeave", "dc", "États-Unis", "CRWV", ["COREWEAVE INC"], ["COREWEAVE INC"], ["CoreWeave, Inc."]),
    E("terawulf", "TeraWulf", "dc", "États-Unis", "WULF", ["WULF COMPUTE LLC"], [], ["Wulf Compute, LLC", "WULF Compute LLC"], "TeraWulf (Wulf Compute)"),
    E("applied-digital", "Applied Digital", "dc", "États-Unis", "APLD", ["APLD COMPUTECO LLC", "APLD COMPUTECO 2 LLC"], [], ["APLD ComputeCo LLC", "APLD ComputeCo 2 LLC"],
      "Applied Digital (APLD ComputeCo)"),
    E("cipher", "Cipher", "dc", "États-Unis", "CIFR", ["CIPHER COMPUTE LLC"], [], ["Cipher Compute LLC"], "Cipher Mining (Cipher Compute)"),
    E("galaxy", "Galaxy", "dc", "États-Unis", "GLXY", ["GALAXY HELIOS DATA CNTR"], [], [], "Galaxy Digital (Helios)"),
    E("core-scientific", "Core Scientific", "dc", "États-Unis", "CORZ", ["CORE SCIENTIFIC FINANCE"], [], ["Core Scientific Finance I, LLC", "Core Scientific Finance I LLC"]),
    # ── Banques ──
    E("jpmorgan", "JPMorgan", "banques", "États-Unis", "JPM", ["JPMORGAN CHASE + CO"], ["JPMORGAN CHASE & CO"], ["JPMorgan Chase & Co."], "JPMorgan Chase"),
    E("bofa", "Bank of America", "banques", "États-Unis", "BAC", ["BANK OF AMERICA CORP"], ["BANK OF AMERICA CORP"],
      ["Bank of America Corporation", "Bank of America Corp."]),
    E("morgan-stanley", "Morgan Stanley", "banques", "États-Unis", "MS", ["MORGAN STANLEY"], ["MORGAN STANLEY"], ["Morgan Stanley"]),
    E("goldman", "Goldman Sachs", "banques", "États-Unis", "GS", ["GOLDMAN SACHS GROUP INC"], ["GOLDMAN SACHS GROUP INC"],
      ["Goldman Sachs Group, Inc. (The)"]),
    E("wells-fargo", "Wells Fargo", "banques", "États-Unis", "WFC", ["WELLS FARGO + COMPANY"], ["WELLS FARGO & COMPANY"], ["Wells Fargo & Co.", "Wells Fargo & Company"]),
    E("citigroup", "Citigroup", "banques", "États-Unis", "C", ["CITIGROUP INC"], ["CITIGROUP INC"], ["Citigroup Inc."]),
    E("hsbc", "HSBC", "banques", "Royaume-Uni", "HSBA.L", ["HSBC HOLDINGS PLC"], ["HSBC HOLDINGS PLC"], ["HSBC Holdings plc", "HSBC Holdings PLC"]),
    E("barclays", "Barclays", "banques", "Royaume-Uni", "BARC.L", ["BARCLAYS PLC"], ["BARCLAYS PLC"], ["Barclays PLC", "Barclays plc"]),
    E("bnp", "BNP Paribas", "banques", "France", "BNP.PA", [], ["BNP PARIBAS"], ["BNP Paribas", "BNP Paribas S.A."]),
    E("socgen", "Société Générale", "banques", "France", "GLE.PA", [], ["SOCIETE GENERALE"], ["Societe Generale", "Societe Generale S.A."]),
    E("credit-agricole", "Crédit Agricole", "banques", "France", "ACA.PA", [], ["CREDIT AGRICOLE SA"], ["Credit Agricole S.A."]),
    E("deutsche-bank", "Deutsche Bank", "banques", "Allemagne", "DBK.DE", ["DEUTSCHE BANK NY"], ["DEUTSCHE BANK AG"], ["Deutsche Bank AG"]),
    E("santander", "Santander", "banques", "Espagne", "SAN.MC", ["BANCO SANTANDER SA"], ["BANCO SANTANDER SA"], ["Banco Santander, S.A.", "Banco Santander S.A.", "Banco Santander, S.A. (Spain)"]),
    E("ubs", "UBS", "banques", "Suisse", "UBSG.SW", [], ["UBS GROUP AG"], ["UBS Group AG"]),
    E("ing", "ING", "banques", "Pays-Bas", "INGA.AS", ["ING GROEP NV"], ["ING GROEP NV"], ["ING Groep N.V."]),
    # ── Télécoms et médias ──
    E("att", "AT&T", "telecoms", "États-Unis", "T", ["AT+T INC"], ["AT&T INC"], ["AT&T Inc."]),
    E("verizon", "Verizon", "telecoms", "États-Unis", "VZ", ["VERIZON COMMUNICATIONS"], ["VERIZON COMMUNICATIONS"], ["Verizon Communications Inc."]),
    E("t-mobile", "T-Mobile US", "telecoms", "États-Unis", "TMUS", ["T MOBILE USA INC"], ["T-MOBILE USA INC"], ["T-Mobile USA, Inc.", "T-Mobile USA Inc."]),
    E("comcast", "Comcast", "telecoms", "États-Unis", "CMCSA", ["COMCAST CORP"], ["COMCAST CORP"], ["Comcast Corp.", "Comcast Corporation"]),
    E("charter", "Charter", "telecoms", "États-Unis", "CHTR", ["CHARTER COMM OPT LLC/CAP"], [], ["Charter Communications Operating, LLC"], "Charter (Charter Operating)"),
    E("disney", "Disney", "telecoms", "États-Unis", "DIS", ["WALT DISNEY COMPANY/THE"], [], ["Walt Disney Company (The)", "The Walt Disney Company"]),
    E("deutsche-telekom", "Deutsche Telekom", "telecoms", "Allemagne", "DTE.DE", ["DEUTSCHE TELEKOM INT FIN"],
      ["DEUTSCHE TELEKOM AG", "DEUTSCHE TELEKOM INT FIN"], ["Deutsche Telekom AG", "Deutsche Telekom International Finance B.V."]),
    E("orange", "Orange", "telecoms", "France", "ORA.PA", ["ORANGE SA"], ["ORANGE SA"], ["Orange S.A."]),
    E("vodafone", "Vodafone", "telecoms", "Royaume-Uni", "VOD.L", ["VODAFONE GROUP PLC"], ["VODAFONE INTERNAT FINANC", "VODAFONE GROUP PLC"], ["Vodafone Group Plc", "Vodafone Group PLC"]),
    # ── Santé ──
    E("abbvie", "AbbVie", "sante", "États-Unis", "ABBV", ["ABBVIE INC"], ["ABBVIE INC"], ["AbbVie Inc."]),
    E("unitedhealth", "UnitedHealth", "sante", "États-Unis", "UNH", ["UNITEDHEALTH GROUP INC"], [], ["UnitedHealth Group Incorporated"]),
    E("amgen", "Amgen", "sante", "États-Unis", "AMGN", ["AMGEN INC"], [], ["Amgen Inc."]),
    E("cvs", "CVS Health", "sante", "États-Unis", "CVS", ["CVS HEALTH CORP"], [], ["CVS Health Corporation"]),
    E("pfizer", "Pfizer", "sante", "États-Unis", "PFE", ["PFIZER INVESTMENT ENTER", "PFIZER INC"], ["PFIZER NETHERLANDS INTL"], ["Pfizer Inc."]),
    E("merck", "Merck & Co", "sante", "États-Unis", "MRK", ["MERCK + CO INC"], [], ["Merck & Co., Inc.", "Merck & Co. Inc."]),
    E("lilly", "Eli Lilly", "sante", "États-Unis", "LLY", ["ELI LILLY + CO"], ["ELI LILLY & CO"], ["Eli Lilly and Company", "Eli Lilly & Co."]),
    E("bms", "Bristol Myers Squibb", "sante", "États-Unis", "BMY", ["BRISTOL MYERS SQUIBB CO"], ["BRISTOL-MYERS SQUIBB CO"],
      ["Bristol-Myers Squibb Company", "Bristol-Myers Squibb Co."]),
    E("hca", "HCA Healthcare", "sante", "États-Unis", "HCA", ["HCA INC"], [], ["HCA Inc."]),
    E("jnj", "Johnson & Johnson", "sante", "États-Unis", "JNJ", ["JOHNSON + JOHNSON"], ["JOHNSON & JOHNSON"], ["Johnson & Johnson"]),
    E("sanofi", "Sanofi", "sante", "France", "SAN.PA", ["SANOFI SA"], ["SANOFI SA"], ["Sanofi", "Sanofi SA"]),
    # ── Consommation ──
    E("home-depot", "Home Depot", "conso", "États-Unis", "HD", ["HOME DEPOT INC"], [], ["Home Depot, Inc. (The)", "Home Depot Inc."]),
    E("walmart", "Walmart", "conso", "États-Unis", "WMT", ["WALMART INC"], ["WALMART INC"], ["Walmart Inc.", "Walmart, Inc."]),
    E("philip-morris", "Philip Morris", "conso", "États-Unis", "PM", ["PHILIP MORRIS INTL INC"], ["PHILIP MORRIS INTL INC"],
      ["Philip Morris International Inc.", "Philip Morris International, Inc."]),
    E("abinbev", "AB InBev", "conso", "Belgique", "ABI.BR", ["ANHEUSER BUSCH INBEV WOR", "ANHEUSER BUSCH CO/INBEV", "ANHEUSER BUSCH INBEV FIN"],
      ["ANHEUSER-BUSCH INBEV SA/"], ["Anheuser-Busch InBev SA/NV", "Anheuser-Busch InBev S.A./N.V."]),
    E("pepsico", "PepsiCo", "conso", "États-Unis", "PEP", ["PEPSICO INC", "PEPSICO SINGAPORE FIN"], ["PEPSICO INC"], ["PepsiCo, Inc.", "PepsiCo Inc."]),
    E("lowes", "Lowe's", "conso", "États-Unis", "LOW", ["LOWE S COS INC"], [], ["Lowe's Cos. Inc.", "Lowe's Companies, Inc."]),
    E("lvmh", "LVMH", "conso", "France", "MC.PA", [], ["LVMH MOET HENNESSY VUITT"],
      ["LVMH Moet Hennessy Louis Vuitton S.E.", "LVMH Moet Hennessy Louis Vuitton SE"]),
    E("nestle", "Nestlé", "conso", "Suisse", "NESN.SW", [], ["NESTLE FINANCE INTL LTD"],
      ["Nestle Finance International Ltd.", "Nestle Finance International Ltd", "Nestle Holdings Inc.", "Nestle Holdings, Inc."]),
    # ── Industrie et automobile ──
    E("ford", "Ford", "industrie", "États-Unis", "F", ["FORD MOTOR CREDIT CO LLC", "FORD MOTOR COMPANY"], ["FORD MOTOR CREDIT CO LLC"],
      ["Ford Motor Credit Company LLC", "Ford Motor Credit Co. LLC"], "Ford (Ford Credit)"),
    E("gm", "General Motors", "industrie", "États-Unis", "GM", ["GENERAL MOTORS FINL CO", "GENERAL MOTORS CO"], ["GENERAL MOTORS FINL CO"],
      ["General Motors Financial Company, Inc.", "General Motors Financial Co. Inc."], "General Motors (GM Financial)"),
    E("toyota", "Toyota", "industrie", "Japon", "7203.T", ["TOYOTA MOTOR CREDIT CORP"], ["TOYOTA MOTOR CREDIT CORP", "TOYOTA MOTOR FINANCE BV"],
      ["Toyota Motor Credit Corporation", "Toyota Motor Credit Corp."], "Toyota (Toyota Motor Credit)"),
    E("boeing", "Boeing", "industrie", "États-Unis", "BA", ["BOEING CO", "BOEING CO/THE"], [], ["Boeing Co.", "The Boeing Company", "Boeing Company (The)"]),
    E("rtx", "RTX", "industrie", "États-Unis", "RTX", ["RTX CORP"], ["RTX CORP"], ["RTX Corp.", "RTX Corporation"]),
    E("caterpillar", "Caterpillar", "industrie", "États-Unis", "CAT", ["CATERPILLAR FINL SERVICE", "CATERPILLAR INC"], ["CATERPILLAR FINL SERVICE"], ["Caterpillar Financial Services Corporation", "Caterpillar Financial Services Corp.", "Caterpillar Inc."]),
    E("volkswagen", "Volkswagen", "industrie", "Allemagne", "VOW3.DE", [], ["VOLKSWAGEN INTL FIN NV", "VOLKSWAGEN FINANCIAL SER"],
      ["Volkswagen International Finance N.V.", "Volkswagen Financial Services N.V."]),
    E("siemens", "Siemens", "industrie", "Allemagne", "SIE.DE", [], ["SIEMENS FINANCIERINGSMAT"], ["Siemens AG", "Siemens Financieringsmaatschappij N.V."]),
    E("mercedes", "Mercedes-Benz", "industrie", "Allemagne", "MBG.DE", ["MERCEDES BENZ FIN NA"], ["MERCEDES-BENZ INT FINCE", "MERCEDES-BENZ GROUP AG"], ["Mercedes-Benz Group AG", "Mercedes-Benz International Finance B.V.", "Mercedes-Benz Finance North America LLC"]),
    E("bmw", "BMW", "industrie", "Allemagne", "BMW.DE", [], ["BMW FINANCE NV", "BMW INTL INVESTMENT BV"], ["BMW Finance N.V.", "BMW International Investment B.V.", "BMW US Capital, LLC", "BMW U.S. Capital LLC"]),
    E("stellantis", "Stellantis", "industrie", "Pays-Bas", "STLAM.MI", ["STELLANTIS FIN US INC"], ["STELLANTIS NV"], ["Stellantis N.V."]),
    E("airbus", "Airbus", "industrie", "France", "AIR.PA", [], ["AIRBUS SE"], ["Airbus SE"]),
    E("schneider", "Schneider Electric", "industrie", "France", "SU.PA", [], ["SCHNEIDER ELECTRIC SE"], ["Schneider Electric S.E.", "Schneider Electric SE"]),
    # ── Énergie ──
    E("energy-transfer", "Energy Transfer", "energie", "États-Unis", "ET", ["ENERGY TRANSFER LP"], [], ["Energy Transfer LP"]),
    E("totalenergies", "TotalEnergies", "energie", "France", "TTE.PA", ["TOTALENERGIES CAPITAL SA", "TOTALENERGIES CAP INTL"],
      ["TOTALENERGIES CAP INTL", "TOTALENERGIES SE", "TOTALENERGIES CAPITAL"], ["TotalEnergies SE", "TotalEnergies Capital International", "TotalEnergies Capital International SA", "TotalEnergies Capital", "TotalEnergies Capital S.A."]),
    E("bp", "BP", "energie", "Royaume-Uni", "BP.L", ["BP CAP MARKETS AMERICA", "BP CAPITAL MARKETS PLC"], ["BP CAPITAL MARKETS PLC", "BP CAPITAL MARKETS BV"], ["BP plc", "BP PLC", "BP Capital Markets p.l.c.", "BP Capital Markets America Inc."]),
    E("shell", "Shell", "energie", "Royaume-Uni", "SHEL.L", ["SHELL FINANCE US INC", "SHELL INTERNATIONAL FIN"], ["SHELL INTERNATIONAL FIN"], ["Shell plc", "Shell International Finance B.V.", "Shell International Finance BV", "Shell Finance US Inc."]),
    E("nextera", "NextEra Energy", "energie", "États-Unis", "NEE", ["NEXTERA ENERGY CAPITAL"], ["NEXTERA ENERGY CAPITAL"], ["NextEra Energy Capital Holdings, Inc."]),
    E("engie", "Engie", "energie", "France", "ENGI.PA", [], ["ENGIE SA"], ["ENGIE SA", "Engie S.A."]),
    E("iberdrola", "Iberdrola", "energie", "Espagne", "IBE.MC", [], ["IBERDROLA FINANZAS SAU", "IBERDROLA INTL BV"], ["Iberdrola, S.A.", "Iberdrola Finanzas, S.A.U.", "Iberdrola Finanzas S.A.U.", "Iberdrola International B.V.", "Iberdrola International BV"]),
    E("enel", "Enel", "energie", "Italie", "ENEL.MI", [], ["ENEL FINANCE INTL NV", "ENEL SPA"], ["Enel S.p.A.", "Enel SpA", "ENEL S.p.A."]),
]

# ═══════════════════════════════════════════════════════════════════════════
# 1. L'UNIVERS : les avoirs quotidiens des ETF SPDR
# ═══════════════════════════════════════════════════════════════════════════
SPDR = [("us", t) for t in ("spbo", "spsb", "spib", "splb", "jnk", "sphy")] + [("eu", t) for t in ("sybc", "sybj")]
URL_SPDR = {"us": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-%s.xlsx",
            "eu": "https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-%s-gy.xlsx"}
RX_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
# « ORACLE CORP SR UNSECURED 11/32 6.25 » → « ORACLE CORP »
RX_US = re.compile(r"\s+(?:SR\b|SUBORDINATED|SUBORD\b|COMPANY GUAR|JR\b|SECURED|UNSECURED|1ST\b|2ND\b|144A|REGS\b|LOCAL GOVT|GOVT\b|"
                   r"BANK GUAR|COVERED|NOTES\b|\d{2}/\d{2}\b).*$")
# « GOLDMAN SACHS GROUP INC 3.5 01/23/2033 » → « GOLDMAN SACHS GROUP INC »
RX_EU = re.compile(r"\s+-?\d+(?:\.\d+)?\s+\d{2}/\d{2}/\d{4}.*$")
MOIS_EN = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    s = str(x or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", s)
    if m and m.group(2).lower() in MOIS_EN:
        return date(int(m.group(3)), MOIS_EN[m.group(2).lower()], int(m.group(1)))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def cle_us(nom):
    return RX_US.sub("", (nom or "").upper()).strip()


def cle_eu(nom):
    return RX_EU.sub("", (nom or "").upper()).strip()


def univers(journal):
    """{isin: ligne} pour les 8 fonds, et la date des avoirs (« As of »)."""
    import openpyxl
    U, dates = {}, []
    for z, t in SPDR:
        b = get(URL_SPDR[z] % t, timeout=120)
        if not b:
            journal.append("SPDR %s illisible" % t.upper())
            continue
        try:
            rows = list(openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True).worksheets[0].iter_rows(values_only=True))
        except Exception as e:  # noqa: BLE001
            journal.append("SPDR %s : %s" % (t.upper(), str(e)[:80]))
            continue
        tete = next((i for i, r in enumerate(rows) if r and any(str(c or "").strip().lower() in ("isin", "identifier") for c in r)), None)
        au = None
        # ⚠ La date des avoirs est dans l'en-tête (« As of 01-Oct-2026 ») : lire plus bas
        #   prenait l'échéance de la première ligne (12/31/2030) pour la date des prix.
        for r in rows[:tete or 0]:
            for c in r or ():
                m = re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b", str(c or ""))
                if m and _date(m.group(1)):
                    au = _date(m.group(1))
        if tete is None or not au:
            journal.append("SPDR %s : en-tête introuvable" % t.upper())
            continue
        dates.append(au)
        H = [str(c or "").strip().lower() for c in rows[tete]]
        n = 0
        for r in rows[tete + 1:]:
            if not r:
                continue
            if z == "us":
                nom, isin, cpn, par, mv, dev, ech = r[0], r[1], _f(r[4]), _f(r[5]), _f(r[6]), r[7], _date(r[8])
                prix = 100 * mv / par if par and mv and par > 0 else None
                cle = cle_us(nom)
            else:
                g = dict(zip(H, r))
                nom, isin, dev = g.get("security name"), g.get("isin"), g.get("currency local")
                cpn, prix, ech = _f(g.get("interest rate")), _f(g.get("local price")), _date(g.get("maturity date"))
                par, mv = _f(g.get("par value local")), _f(g.get("base market value"))
                cle = cle_eu(nom)
            isin = str(isin or "").strip()
            if not RX_ISIN.match(isin) or not ech or cpn is None or not prix or not (5 < prix < 250):
                continue
            nomU = str(nom or "").upper()
            x = {"isin": isin, "ligne": str(nom).strip(), "cle": cle, "coupon": cpn, "ech": ech.isoformat(), "prix": round(prix, 4),
                 "dev": str(dev or "").strip().upper(), "par": par, "mv": mv, "fonds": t.upper(), "au": au.isoformat(),
                 "var": bool(re.search(r"\bVAR\b", nomU)), "a144": "144A" in nomU, "frn": bool(re.search(r"\b(FRN|FLOAT|FLT)\b", nomU))}
            if isin not in U:
                U[isin] = x
                n += 1
        log("[info] SPDR %s : %d lignes, au %s" % (t.upper(), n, au))
    return U, (max(dates).isoformat() if dates else None)


# ═══════════════════════════════════════════════════════════════════════════
# 2. ONVISTA : la fiche (émission) et le cours quotidien de chaque obligation
# ═══════════════════════════════════════════════════════════════════════════
OV = "https://api.onvista.de/api/v1"
PLACES = ["Frankfurt", "Stuttgart", "München", "Düsseldorf", "Berlin", "Hamburg", "gettex", "Tradegate BSX", "Quotrix"]


def _ov(chemin):
    return get_json(OV + chemin, accept="application/json", timeout=40, debit=0.12, essais=2)


def _mois_de(txt):
    t = (txt or "").lower()
    m = re.search(r"(\d+)\s*monat", t)
    if m:
        return int(m.group(1))
    if "jahr" in t or "jährl" in t:
        return 12
    if "halbj" in t:
        return 6
    if "viertel" in t or "quart" in t:
        return 3
    return None


def fiche_ov(isin, dev_defaut=None):
    """La fiche d'émission d'une obligation, ou {"absent": True} si onvista ne la connaît pas.
    ⚠ Certaines fiches (BNP, Crédit Agricole, Engie… cotées à Paris) n'ont ni données
      d'émission ni devise : on prend alors celle de l'indice, sinon aucune place n'est retenue."""
    s = _ov("/bonds/ISIN:%s/snapshot" % isin)
    if s is None:
        return None
    if not isinstance(s, dict) or "instrument" not in s:
        return {"absent": True}
    b = s.get("bondsBaseData") or {}
    det = s.get("bondsDetails") or {}
    dev = (det.get("isoCurrency") or dev_defaut or "").upper()
    q = [x for x in ((s.get("quoteList") or {}).get("list") or [])
         if (x.get("isoCurrency") or "").upper() == dev and (x.get("market") or {}).get("idNotation")]
    q.sort(key=lambda x: PLACES.index(x["market"]["name"]) if x["market"].get("name") in PLACES else 99)
    j = lambda k: (b.get(k) or "")[:10] or None  # noqa: E731
    return {"iid": s["instrument"].get("entityValue"), "dev": dev, "coupon": det.get("coupon"),
            "type_coupon": det.get("nameTypeCoupon"), "emis": j("datetimeEmission"), "montant": b.get("volumeEmission"),
            "prix_emis": b.get("priceEmission"), "ech": j("datetimeMaturity"), "periode": _mois_de(b.get("nameCouponPeriod")),
            "rappel": j("datetimeCancellationIssuer"), "perpetuelle": bool(b.get("perpetual")),
            "places": [[x["market"].get("name"), x["market"]["idNotation"]] for x in q[:4]],
            "v": 2}


# Disjoncteur : après 25 échecs de suite (adresse refusée, panne), plus aucune
# requête onvista pendant ce passage — les fiches concernées sont reprises telles quelles.
OV_ETAT = {"echecs_suite": 0, "hs": False}


def cours_ov(iid, idn):
    """[(date ISO, cours de clôture en % du nominal)] depuis la cotation."""
    if OV_ETAT["hs"]:
        return None
    h = _ov("/instruments/BOND/%s/eod_history?idNotation=%s&range=MAX&startDate=1990-01-01" % (iid, idn))
    if not isinstance(h, dict) or not h.get("datetimeLast"):
        OV_ETAT["echecs_suite"] += 1
        if OV_ETAT["echecs_suite"] >= 25:
            OV_ETAT["hs"] = True
            log("[warn] onvista : 25 échecs de suite, plus aucune requête pendant ce passage")
        return None
    OV_ETAT["echecs_suite"] = 0
    ts, px = h.get("datetimeLast") or [], h.get("last") or []
    out = {}
    for t, p in zip(ts, px):
        if estnb(p) and 5 < p < 250:
            out[datetime.fromtimestamp(t, timezone.utc).date().isoformat()] = float(p)
    return sorted(out.items())


# ═══════════════════════════════════════════════════════════════════════════
# 3. LE CALCUL : rendement, écart à l'État, courbe de l'émetteur
# ═══════════════════════════════════════════════════════════════════════════
def _ajouter_mois(d, n):
    m = d.month - 1 + n
    a, m = d.year + m // 12, m % 12 + 1
    return date(a, m, min(d.day, calendar.monthrange(a, m)[1]))


class Titre:
    """Une obligation à coupon fixe : l'échéancier des coupons, remonté depuis
    l'échéance, et le rendement actuariel tiré d'un prix PIED DE COUPON (le
    coupon couru est ajouté) — convention des marchés : semestriel en dollars,
    annuel en euros (selon la fiche onvista quand elle existe)."""

    def __init__(self, coupon, ech, freq):
        self.c, self.f = float(coupon), int(freq)
        self.ech = ech
        pas = 12 // self.f
        d, k, L = ech, 0, []
        while d > date(1980, 1, 1):
            L.append(d.toordinal())
            k += 1
            d = _ajouter_mois(ech, -pas * k)
        L.append(d.toordinal())
        self.o = sorted(L)

    def rendement(self, prix, jour):
        """(rendement %, duration modifiée, années restantes) ou None."""
        i = bisect.bisect_right(self.o, jour)
        if i <= 0 or i >= len(self.o) or not (5 < prix < 250):
            return None
        nxt, prv = self.o[i], self.o[i - 1]
        n = len(self.o) - i
        w = (nxt - jour) / (nxt - prv)
        f, c = self.f, self.c / self.f
        sale = prix + c * (1 - w)

        def pv(y):
            v = 1.0 / (1.0 + y / f)
            if abs(1 - v) < 1e-12:
                return c * n + 100.0
            return v ** w * (c * (1 - v ** n) / (1 - v) + 100.0 * v ** (n - 1))
        ans = (self.o[-1] - jour) / 365.25
        y = max(-0.02, (self.c + (100 - prix) / max(ans, 0.25)) / ((100 + prix) / 2))
        ok = False
        for _ in range(40):
            p = pv(y)
            dp = (pv(y + 1e-5) - pv(y - 1e-5)) / 2e-5
            if dp == 0:
                break
            y2 = y - (p - sale) / dp
            if not (-0.5 < y2 < 3):
                break
            if abs(y2 - y) < 1e-10:
                y, ok = y2, True
                break
            y = y2
        if not ok:
            lo, hi = -0.05, 2.0
            if (pv(lo) - sale) * (pv(hi) - sale) > 0:
                return None
            for _ in range(70):
                mi = (lo + hi) / 2
                if pv(mi) > sale:
                    lo = mi
                else:
                    hi = mi
            y = (lo + hi) / 2
        p = pv(y)
        dm = -(pv(y + 1e-4) - pv(y - 1e-4)) / 2e-4 / p
        return 100 * y, dm, ans


class Courbe:
    """Les taux d'État au jour le jour, interpolés à n'importe quelle durée."""

    def __init__(self, series):
        par = defaultdict(dict)
        for m, pts in (series or {}).items():
            for d, v in pts:
                if estnb(v):
                    par[d[:10]][float(m)] = v
        self.jours = sorted(d for d in par if len(par[d]) >= 4)
        self.o = [date.fromisoformat(d).toordinal() for d in self.jours]
        self.p = [sorted(par[d].items()) for d in self.jours]

    def taux(self, jour, mat):
        i = bisect.bisect_right(self.o, jour) - 1
        if i < 0 or jour - self.o[i] > 6:
            return None
        P = self.p[i]
        if mat <= P[0][0]:
            return P[0][1]
        if mat >= P[-1][0]:
            return P[-1][1]
        for k in range(1, len(P)):
            if P[k][0] >= mat:
                (m0, v0), (m1, v1) = P[k - 1], P[k]
                return v0 + (v1 - v0) * (mat - m0) / (m1 - m0)
        return None

    def dernier(self):
        return self.jours[-1] if self.jours else None


def _filtrer(points):
    """Retire les cours FIGÉS (une cotation qui ne bouge plus depuis 6 séances
    est un affichage, pas un prix) et les sauts isolés (écart à la médiane
    locale > max(100 pb, 5 écarts absolus médians))."""
    out, rep = [], 0
    for k, p in enumerate(points):
        rep = rep + 1 if k and p[1] == points[k - 1][1] else 0
        if rep < 6:
            out.append(p)
    if len(out) < 12:
        return out
    s = [p[3] for p in out]
    garde = []
    for k, p in enumerate(out):
        fen = sorted(s[max(0, k - 5):k + 6])
        med = fen[len(fen) // 2]
        mad = sorted(abs(x - med) for x in fen)[len(fen) // 2]
        if abs(p[3] - med) <= max(100.0, 5 * mad):
            garde.append(p)
    return garde


def serie_titre(o, hist, courbe):
    """[(ordinal, prix, rendement %, écart pb, durée restante, duration)] d'une obligation."""
    if not hist or not courbe:
        return []
    T = Titre(o["coupon"], date.fromisoformat(o["ech_calc"]), o["freq"])
    out = []
    for d, px in hist:
        j = date.fromisoformat(d).toordinal()
        r = T.rendement(px, j)
        if not r or not (-1 < r[0] < 40) or r[2] < 0.25:
            continue                       # (sous 3 mois, un centime de prix fait des points de rendement)
        g = courbe.taux(j, r[2])
        if g is None:
            continue
        e = (r[0] - g) * 100
        if not (-150 < e < 4000):
            continue
        out.append((j, px, r[0], e, r[2], r[1]))
    return _filtrer(out)


def ajuster(pts, T, h=0.35):
    """Écart de l'émetteur à la durée T : régression locale (noyau en log-durée)
    sur ses obligations. Rien si aucune n'est proche (±30 %), ou si toutes sont
    du même côté et loin — un chiffre extrapolé vaudrait moins que le vide."""
    lt = math.log(T)
    P = [(math.log(a) - lt, s, w) for a, s, w in pts if 0.4 <= a <= 60]
    W = [(x, s, w * math.exp(-0.5 * (x / h) ** 2)) for x, s, w in P if abs(x) < 1.1]
    if not W:
        return None
    proches = [x for x, _, _ in W if abs(x) < 0.7]
    if not proches:
        return None
    sw = sum(w for _, _, w in W)
    if sw <= 1e-9:
        return None
    mx = sum(w * x for x, _, w in W) / sw
    ms = sum(w * s for _, s, w in W) / sw
    if any(x <= 0 for x in proches) and any(x >= 0 for x in proches) and len(W) >= 3:
        vx = sum(w * (x - mx) ** 2 for x, _, w in W)
        if vx > 1e-6:
            b = sum(w * (x - mx) * (s - ms) for x, s, w in W) / vx
            return ms - b * mx
    return ms if min(abs(x) for x in proches) <= 0.3 else None


def _semaine(o):
    a, s, _ = date.fromordinal(o).isocalendar()
    return a * 100 + s


def series_emetteur(titres, courbe, fin):
    """Les courbes de l'émetteur dans le temps : quotidien sur 13 mois,
    hebdomadaire avant. Chaque obligation compte à la date de son dernier cours
    (au plus 4 jours avant)."""
    if not titres:
        return {}
    lim = fin - 400
    jours = sorted({p[0] for t in titres for p in t["pts"] if p[0] >= lim})
    sem = sorted({_semaine(p[0]) for t in titres for p in t["pts"] if p[0] < lim})
    grille = []                                        # (ordinal du point, test d'appartenance)
    for s in sem:
        grille.append(("s", s))
    for j in jours:
        grille.append(("j", j))
    # dernier cours de chaque titre par jour et par semaine
    par_point = defaultdict(list)
    for t in titres:
        P = t["pts"]
        w = math.sqrt(t.get("montant") or 5e8) / 1e4
        dern_sem = {}
        for p in P:
            if p[0] < lim:
                dern_sem[_semaine(p[0])] = p
        for s, p in dern_sem.items():
            par_point[("s", s)].append((p[4], p[3], w, p[0]))
        os_ = [p[0] for p in P]
        for j in jours:
            i = bisect.bisect_right(os_, j) - 1
            if i >= 0 and j - os_[i] <= 4:
                p = P[i]
                par_point[("j", j)].append((p[4] - (j - p[0]) / 365.25, p[3], w, j))
    out = {k: [] for k in ("e5", "e10", "e30", "emoy", "r5", "r10", "r30", "n")}
    actifs_max = len(titres)
    for g in grille:
        pts = par_point.get(g) or []
        if len(pts) < min(2, actifs_max):
            continue
        o = max(p[3] for p in pts)
        d = date.fromordinal(o).isoformat()
        tri = sorted(p[1] for p in pts if p[0] >= 1)
        if tri:
            out["emoy"].append((d, tri[len(tri) // 2]))
        out["n"].append((d, len(pts)))
        for T in (5, 10, 30):
            e = ajuster([(p[0], p[1], p[2]) for p in pts], T)
            g0 = courbe.taux(o, T)
            if e is not None and g0 is not None:
                out["e%d" % T].append((d, e))
                out["r%d" % T].append((d, g0 + e / 100))
    return {k: v for k, v in out.items() if v}


# ═══════════════════════════════════════════════════════════════════════════
# 4. LES NOTES (registre ESMA) et LES COMPTES (SEC)
# ═══════════════════════════════════════════════════════════════════════════
AGENCES = {"sp": ("Standard & Poor", ECHELLE_SP), "moodys": ("Moody", ECHELLE_MOODYS), "fitch": ("Fitch", ECHELLE_SP)}


def _agence(cra):
    c = cra or ""
    if "Poor" in c or "S&P" in c:
        return "sp"
    if "Moody" in c:
        return "moodys"
    if "Fitch" in c:
        return "fitch"
    return None


def _persp_esma(txt):
    t = (txt or "").lower()
    if "negative" in t:
        return "négative"
    if "positive" in t:
        return "positive"
    if "developing" in t or "evolving" in t:
        return "en évolution"
    if "stable" in t:
        return "stable"
    if "watch" in t or "review" in t:
        return "sous surveillance"
    return None


def notes_esma(noms, journal):
    """Pour chaque agence : la note retenue, sa date, sa perspective, son histoire.
    Règle : la note d'ÉMETTEUR (ISR) si elle n'a pas plus d'un an de retard sur
    les notes de ses titres (INT) ; sinon la note la plus fréquente parmi ses
    titres à leur dernière date d'action (le registre garde des notes d'émetteur
    jamais mises à jour : Moody's sur Netflix, Ba1 en 2023 pour des titres A2
    en 2026). Une agence sans action depuis 24 mois est écartée."""
    if not noms:
        return None
    docs = []
    for objet in ("ISR", "INT"):
        q = {"q": "type_s:parent AND ratedObjectCode:%s AND issuerName:(%s)" % (objet, " OR ".join('"%s"' % n.replace('"', '') for n in noms)),
             "rows": 600, "wt": "json", "sort": "racValidityDatetime desc",
             "fl": "craName,issuerName,ratingValueLabel,racValidityDatetimeStr,lastActionTypeLabel,ratingStatusLabel,timeHorizonType,ratedObjectCode"}
        d = get_json("https://registers.esma.europa.eu/solr/esma_registers_radar/select?" + urllib.parse.urlencode(q),
                     accept="application/json", timeout=90)
        if d is None:
            journal.append("ESMA illisible (%s)" % noms[0])
            return None
        docs += ((d.get("response") or {}).get("docs") or [])
        time.sleep(0.2)
    auj = date.today()
    par = defaultdict(lambda: {"ISR": [], "INT": []})
    for x in docs:
        ag = _agence(x.get("craName"))
        if not ag or x.get("timeHorizonType") != "L" or x.get("issuerName") not in noms:
            continue
        note = (x.get("ratingValueLabel") or "").strip()
        if note not in AGENCES[ag][1]:
            continue
        dd = (x.get("racValidityDatetimeStr") or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}$", dd) or dd > auj.isoformat():
            continue
        par[ag][x.get("ratedObjectCode")].append((dd, note, x.get("ratingStatusLabel"), x.get("lastActionTypeLabel")))
    out = {}
    for ag, D in par.items():
        isr = sorted(D["ISR"], key=lambda z: (z[0], z[1]))
        it = sorted(D["INT"], key=lambda z: (z[0], z[1]))
        d_int = it[-1][0] if it else None
        # la note la plus fréquente de ses titres sur 400 jours (le dernier jour seul peut
        # ne porter que des hybrides : Energy Transfer, Ba1 chez Moody's pour un émetteur Baa2)
        lim_int = il_y_a(d_int, 400) if d_int else None
        mode_int = Counter(n for d0, n, _, _ in it if d0 >= lim_int).most_common(1)[0][0] if it else None
        if isr and (not d_int or isr[-1][1] == mode_int
                    or date.fromisoformat(isr[-1][0]).toordinal() >= date.fromisoformat(d_int).toordinal() - 365):
            note, d_note, src = isr[-1][1], isr[-1][0], "note d'émetteur"
            persp = _persp_esma(isr[-1][2])
        elif it:
            note, d_note, src, persp = mode_int, d_int, "note de ses obligations", None
        else:
            continue
        # L'histoire : la note la plus fréquente de ses titres à chaque date d'action,
        # plus les notes d'émetteur ; un palier de moins de
        # 45 jours qui revient au précédent est un lot d'hybrides, pas un changement.
        # ⚠ Les titres ne racontent l'histoire de l'ÉMETTEUR que si leur note dit la
        #   même chose que lui : chez BNP, la note la plus fréquente de ses titres chez
        #   Moody's (Baa2, la dette « non préférée ») n'est pas celle de la banque (A1) —
        #   les mêler faisait écrire « relevée de Baa2 à A1 », un relèvement qui n'a
        #   jamais eu lieu. Sinon : les seules notes d'émetteur.
        par_d = defaultdict(Counter)
        for d0, n, _, _ in it:
            par_d[d0][n] += 1
        hist_src = [(d0, c.most_common(1)[0][0]) for d0, c in sorted(par_d.items())] if mode_int == note else []
        hist_src += [(d0, n) for d0, n, _, _ in isr]
        hist_src.sort()
        if not hist_src or hist_src[-1][1] != note:
            hist_src.append((d_note, note))
        derniere = max(x for x in (isr[-1][0] if isr else None, d_int) if x)
        if (auj - date.fromisoformat(derniere)).days > 730:
            continue
        hist, prec = [], None
        for d0, n in hist_src:
            if n != prec:
                hist.append([d0, n])
                prec = n
        k = 1
        while k < len(hist) - 1:
            if hist[k + 1][1] == hist[k - 1][1] and (date.fromisoformat(hist[k + 1][0]) - date.fromisoformat(hist[k][0])).days < 45:
                del hist[k:k + 2]
            else:
                k += 1
        cr = AGENCES[ag][1].index(note)
        out[ag] = {"note": note, "cran": cr, "date": d_note, "derniere_action": derniere, "source": src,
                   "perspective": persp, "histoire": hist[-20:]}
    if not out:
        return None
    crans = sorted(x["cran"] for x in out.values())
    med = crans[len(crans) // 2]
    return {"agences": out, "composite": ECHELLE_SP[med], "cran": med, "lecture": lecture(med),
            "negatives": sum(1 for x in out.values() if x.get("perspective") == "négative"),
            "positives": sum(1 for x in out.values() if x.get("perspective") == "positive")}


SEC_TAGS = {
    "ca": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "resultat_op": ["OperatingIncomeLoss"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization"],
    "interets": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestAndDebtExpense"],
    "flux_exploitation": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
}
SEC_BILAN = {
    "dette": ["LongTermDebt", "DebtInstrumentCarryingAmount", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
              "DebtLongtermAndShorttermCombinedAmount"],
    "dette_lt": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "dette_ct": ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "papier": ["CommercialPaper"],
    "tresorerie": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "placements": ["MarketableSecuritiesCurrent", "ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
}


def _annuel(g, tags, instant):
    """{fin d'exercice: valeur} — le premier tag qui a une valeur pour chaque
    exercice, dans l'ordre de priorité ; dépôt annuel le plus récent."""
    out = {}
    for t in tags:
        u = ((g.get(t) or {}).get("units") or {}).get("USD") or []
        par = {}
        for x in u:
            if x.get("form") not in ("10-K", "10-K/A") or not estnb(x.get("val")):
                continue
            if instant:
                if x.get("start"):
                    continue
            else:
                if not x.get("start"):
                    continue
                dur = (_date(x["end"]) - _date(x["start"])).days
                if not 350 <= dur <= 380:
                    continue
            fin = x["end"]
            if fin not in par or (x.get("filed") or "") > par[fin][0]:
                par[fin] = (x.get("filed") or "", x["val"])
        for fin, (_, v) in par.items():
            if fin not in out and v != 0:
                out[fin] = v
    return out


def comptes_sec(cik, encours_au, journal):
    if not cik:
        return None
    j = get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % int(cik), accept="application/json", timeout=90)
    g = ((j or {}).get("facts") or {}).get("us-gaap")
    if not g:
        return None
    A = {k: _annuel(g, v, False) for k, v in SEC_TAGS.items()}
    # Amortissements : Oracle (entre autres) ne publie pas le total, mais les deux
    # parts séparément — corporels (Depreciation) et incorporels. On les additionne.
    dep, amo = _annuel(g, ["Depreciation"], False), _annuel(g, ["AmortizationOfIntangibleAssets"], False)
    for fin, v in dep.items():
        if fin not in A["da"]:
            A["da"][fin] = v + (amo.get(fin) or 0)
    B = {k: _annuel(g, v, True) for k, v in SEC_BILAN.items()}
    fins = sorted(set(A["resultat_op"]) | set(B["dette"]) | set(B["dette_lt"]))
    fins = [f for f in fins if f >= "2009-01-01"][-12:]
    if len(fins) < 2:
        return None
    L = []
    for f in fins:
        dette = B["dette"].get(f)
        if dette is None and f in B["dette_lt"]:
            dette = B["dette_lt"][f] + (B["dette_ct"].get(f) or 0)
        if dette is not None:
            dette += B["papier"].get(f) or 0
        tres = B["tresorerie"].get(f)
        if tres is not None:
            tres += B["placements"].get(f) or 0
        L.append({"fin": f, "ca": A["ca"].get(f), "resultat_op": A["resultat_op"].get(f), "da": A["da"].get(f),
                  "interets": A["interets"].get(f), "flux_exploitation": A["flux_exploitation"].get(f),
                  "capex": A["capex"].get(f), "dette": dette, "tresorerie": tres})
    # Garde : la dette déclarée ne peut pas être inférieure aux obligations recensées
    # (un tag posé sur UN seul emprunt passerait pour la dette totale).
    # (comparée aux obligations recensées EN CIRCULATION À LA MÊME DATE : Meta et
    #  Alphabet ont émis des dizaines de milliards après leur dernier rapport annuel)
    for x in L:
        ob = encours_au(x["fin"]) if encours_au else None
        if ob and x.get("dette") and x["dette"] < 0.85 * ob:
            journal.append("SEC : dette %.0f Md < obligations recensées %.0f Md au %s, écartée" % (x["dette"] / 1e9, ob / 1e9, x["fin"]))
            x["dette"] = None
    out = {"annees": [x["fin"] for x in L]}
    for k in ("ca", "resultat_op", "da", "interets", "flux_exploitation", "capex", "dette", "tresorerie"):
        out[k] = [round(x[k] / 1e9, 3) if estnb(x[k]) else None for x in L]
    out["ebitda"] = [round(a + b, 3) if estnb(a) and estnb(b) else None for a, b in zip(out["resultat_op"], out["da"])]
    out["dette_nette"] = [round(a - b, 3) if estnb(a) and estnb(b) else None for a, b in zip(out["dette"], out["tresorerie"])]
    out["fcf"] = [round(a - b, 3) if estnb(a) and estnb(b) else None for a, b in zip(out["flux_exploitation"], out["capex"])]
    # (Disney déclare ses intérêts en NÉGATIF : on prend la valeur absolue ; hors de
    #  0,5-15 %, le rapport mêle des postes différents — rien plutôt qu'un chiffre faux)
    out["interets"] = [abs(v) if estnb(v) else None for v in out["interets"]]
    cout = [None]
    for i in range(1, len(L)):
        a, b, it = out["dette"][i - 1], out["dette"][i], out["interets"][i]
        c = round(100 * it / ((a + b) / 2), 2) if estnb(a) and estnb(b) and estnb(it) and a + b > 0 else None
        cout.append(c if c is not None and 0.5 <= c <= 15 else None)
    out["cout_dette"] = cout
    out["unite"] = "Md$"
    out["source"] = "SEC EDGAR, comptes annuels déposés (10-K)"
    if not any(estnb(v) for v in out["dette"]) and not any(estnb(v) for v in out["ebitda"]):
        return None
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 5. LA FICHE D'UN ÉMETTEUR
# ═══════════════════════════════════════════════════════════════════════════
def _iso(o):
    return date.fromordinal(o).isoformat()


def _court(x, nom):
    """« Oracle 6,25 % 2032 »."""
    c = ("%.3f" % x["coupon"]).rstrip("0").rstrip(".").replace(".", ",")
    return "%s %s %% %s" % (nom, c, x["ech"][:4])


def hist_compact(pts, fin):
    """Hebdomadaire sur 3 ans, mensuel avant : le détail de chaque obligation
    pour la courbe qui s'ouvre au clic, sans alourdir la fiche."""
    lim = fin - 3 * 365
    vieux, recents = {}, {}
    for p in pts:
        if p[0] < lim:
            vieux[date.fromordinal(p[0]).isoformat()[:7]] = p
        else:
            recents[_semaine(p[0])] = p
    L = sorted(list(vieux.values()) + list(recents.values()))
    return {"d": [_iso(p[0]) for p in L], "v": [round(p[2], 3) for p in L], "e": [round(p[3]) for p in L]}


def serie_pts(L, dec):
    return {"d": [d for d, _ in L], "v": [round(v, dec) for _, v in L]}


def construire_emetteur(em, lignes, reg, courbes, fx, prec, journal, cik_map):
    """Rend (fiche détaillée, ligne de synthèse)."""
    auj = date.today()
    devs = Counter(x["dev"] for x in lignes)
    dev_ref = devs.most_common(1)[0][0]
    courbe = courbes.get(dev_ref)
    px_prec = {}
    for o in (prec or {}).get("obligations") or []:
        if o.get("px_spdr"):
            px_prec[o["isin"]] = [tuple(x) for x in o["px_spdr"]]
    titres, oblig = [], []
    for x in sorted(lignes, key=lambda z: z["ech"]):
        r = reg.get(x["isin"]) or {}
        if x["frn"] or r.get("perpetuelle") or (not x["var"] and r.get("type_coupon") and not re.match(r"(?i)fest|fix", r["type_coupon"] or "")):
            continue                                   # taux variable ou perpétuelle : pas de rendement à maturité
        # (un titre « VAR » est à taux FIXE jusqu'à sa date de rappel : onvista le dit « variable »)
        freq = 12 // r["periode"] if r.get("periode") in (3, 6, 12) else (2 if x["dev"] == "USD" else 1)
        ech_calc = x["ech"]
        rappel = r.get("rappel")
        if rappel and (x["var"] or em["famille"] == "banques"):
            # Titre à taux fixe puis variable : rendu à sa date de rappel (au moins 9 mois avant l'échéance).
            if (date.fromisoformat(x["ech"]) - date.fromisoformat(rappel)).days >= 270 and rappel > auj.isoformat():
                ech_calc = rappel
        elif x["var"]:
            continue                                   # « VAR » sans date de rappel connue : pas de rendement défendable
        o = {"isin": x["isin"], "nom": _court(x, em["nom"]), "ligne": x["ligne"], "coupon": x["coupon"], "ech": x["ech"],
             "ech_calc": ech_calc, "freq": freq, "dev": x["dev"], "montant": r.get("montant"), "emis": r.get("emis"),
             "prix_emis": r.get("prix_emis"), "a144": x["a144"], "rappel": ech_calc if ech_calc != x["ech"] else None}
        cg = courbes.get(x["dev"])
        hist = r.get("_hist")
        source = "onvista (%s)" % r.get("_place") if hist else None
        if not hist:
            # Repli : les prix SPDR cumulés de passage en passage
            P = dict(px_prec.get(x["isin"]) or [])
            P[x["au"]] = x["prix"]
            hist = sorted(P.items())[-420:]
            o["px_spdr"] = [[d, round(v, 4)] for d, v in hist]
            source = "SPDR (State Street), cumulé depuis le %s" % hist[0][0]
        pts = serie_titre(o, hist, cg)
        o["source"] = source
        # le prix du jour officiel (SPDR), pour le contrôle croisé
        o["px_spdr_jour"] = [x["au"], x["prix"]]
        if source and source.startswith("onvista"):
            hd = dict(hist)
            if x["au"] in hd and cg:
                T0 = Titre(o["coupon"], date.fromisoformat(ech_calc), freq)
                ja = date.fromisoformat(x["au"]).toordinal()
                a1, a2 = T0.rendement(hd[x["au"]], ja), T0.rendement(x["prix"], ja)
                if a1 and a2:
                    o["ecart_sources_pb"] = round((a1[0] - a2[0]) * 100, 1)
        if pts:
            p = pts[-1]
            o.update({"date": _iso(p[0]), "prix": round(p[1], 3), "rdt": round(p[2], 3), "ecart": round(p[3]), "ans": round(p[4], 2),
                      "duration": round(p[5], 2)})
            ser = [(_iso(q[0]), q[3]) for q in pts]
            o["var_pb"] = {k: v for k, v in variations(ser, 1).items() if k in ("1m", "3m", "1a")}
            sr = [(_iso(q[0]), q[2]) for q in pts]
            o["var_rdt_pb"] = {k: v for k, v in variations(sr, 100).items() if k in ("1m", "3m", "1a")}
            o["hist"] = hist_compact(pts, pts[-1][0])
            if x["dev"] == dev_ref:
                titres.append({"pts": pts, "montant": o["montant"], "isin": x["isin"]})
        # à l'émission : le rendement au prix d'émission, l'écart à l'État ce jour-là
        if o["emis"] and estnb(o["prix_emis"]) and cg:
            try:
                je = date.fromisoformat(o["emis"]).toordinal()
                rr = Titre(o["coupon"], date.fromisoformat(ech_calc), freq).rendement(o["prix_emis"], je)
                if rr:
                    g0 = cg.taux(je, rr[2])
                    o["rdt_emis"] = round(rr[0], 3)
                    o["ecart_emis"] = round((rr[0] - g0) * 100) if g0 is not None else None
                    o["ans_emis"] = round(rr[2], 1)
            except ValueError:
                pass
        oblig.append(o)
    fin = max((t["pts"][-1][0] for t in titres), default=None)
    S = series_emetteur(titres, courbe, fin) if titres and courbe else {}
    # les points du jour (la courbe de l'émetteur) : chaque obligation à son dernier cours de moins de 7 jours
    pts_jour = []
    if fin:
        for o in oblig:
            if o.get("date") and o["dev"] == dev_ref and fin - date.fromisoformat(o["date"]).toordinal() <= 7:
                pts_jour.append([o["ans"], o["rdt"], o["ecart"], o["isin"]])
    etat = []
    if courbe and fin:
        for m in (0.083, 0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30):
            v = courbe.taux(fin, m)
            if v is not None:
                etat.append([m, round(v, 3)])
    # montants : encours, émissions récentes, mur des échéances (dans la devise de référence)
    taux_fx = fx.get("dernier") or 1.0          # dollars par euro

    def en_ref(montant, dev):
        if not estnb(montant):
            return None
        if dev == dev_ref:
            return montant
        return montant * taux_fx if dev == "EUR" and dev_ref == "USD" else montant / taux_fx if dev == "USD" and dev_ref == "EUR" else None
    encours = sum(en_ref(o["montant"], o["dev"]) or 0 for o in oblig if o["ech"] > auj.isoformat())
    sans_montant = sum(1 for o in oblig if not estnb(o.get("montant")))
    mur = defaultdict(float)
    for o in oblig:
        v = en_ref(o["montant"], o["dev"])
        if v and o["ech"] > auj.isoformat():
            a = int(o["ech"][:4])
            mur[min(a, auj.year + 10)] += v / 1e9
    emis = [o for o in oblig if o.get("emis") and o["emis"] >= il_y_a(auj.isoformat(), 3 * 365 + 30)]
    emis.sort(key=lambda o: o["emis"], reverse=True)
    trim = defaultdict(float)
    for o in emis:
        v = en_ref(o["montant"], o["dev"])
        if v:
            trim["%s-T%d" % (o["emis"][:4], (int(o["emis"][5:7]) - 1) // 3 + 1)] += v / 1e9
    # notes et comptes
    notes = None
    try:
        notes = notes_esma(em["esma"], journal) if em["esma"] else None
    except Exception as e:  # noqa: BLE001
        journal.append("notes %s : %s" % (em["code"], str(e)[:80]))
    if notes is None and (prec or {}).get("notes"):
        notes = dict(prec["notes"], reprise_du=(prec.get("genere_le") or "")[:10])
    comptes = None
    if em["famille"] != "banques" and "." not in em["sym"]:
        try:
            def encours_au(fin_ex):
                return sum(en_ref(o["montant"], o["dev"]) or 0 for o in oblig
                           if o.get("emis") and o["emis"] <= fin_ex < o["ech"]) if dev_ref == "USD" else None
            comptes = comptes_sec(cik_map.get(em["sym"]), encours_au, journal)
        except Exception as e:  # noqa: BLE001
            journal.append("SEC %s : %s" % (em["code"], str(e)[:80]))
        if comptes is None and (prec or {}).get("comptes"):
            comptes = prec["comptes"]
    # le chiffre de tête : le 10 ans, sinon la durée la plus proche disponible
    # (Airbus n'a plus d'obligation assez longue pour un vrai « 10 ans » : sa série
    #  s'arrêtait en 2024 mais restait choisie — la tête doit être à jour)
    der_g = max((v[-1][0] for v in S.values() if v), default=None)
    ref = next((T for T in (10, 5, 30) if S.get("r%d" % T) and der_g and S["r%d" % T][-1][0] >= il_y_a(der_g, 10)), None)
    sy = {"code": em["code"], "nom": em["nom"], "nom_long": em["nom_long"], "famille": em["famille"], "pays": em["pays"],
          "sym": em["sym"], "dev": dev_ref, "devises": sorted(devs), "n": len(oblig), "n_cotees": len(titres),
          "encours_md": round(encours / 1e9, 1) if encours else None, "sans_montant": sans_montant,
          "notation": {k: notes[k] for k in ("composite", "cran", "lecture")} if notes else None,
          "a144": all(o["a144"] for o in oblig) if oblig else False}
    if ref:
        r, e = S["r%d" % ref], S["e%d" % ref]
        sy.update({"ref": ref, "rdt": round(r[-1][1], 3), "ecart": round(e[-1][1]), "date": r[-1][0],
                   "var_rdt_pb": {k: v for k, v in variations(r, 100).items() if k in ("1s", "1m", "3m", "1a", "ytd")},
                   "var_ecart_pb": {k: v for k, v in variations(e, 1).items() if k in ("1s", "1m", "3m", "1a", "ytd")},
                   "depuis": S["e%d" % ref][0][0]})
    elif S.get("emoy"):
        sy.update({"ref": None, "ecart": round(S["emoy"][-1][1]), "date": S["emoy"][-1][0]})
    if S.get("emoy"):
        sy["ecart_moy"] = round(S["emoy"][-1][1])
    sy["mini"] = [[round(p[0], 1), round(p[1], 2)] for p in sorted(pts_jour)]
    det = {"code": em["code"], "synthese": sy, "obligations": oblig,
           "courbe": {"au": _iso(fin) if fin else None, "points": sorted(pts_jour), "etat": etat, "dev": dev_ref},
           "series": {k: serie_pts(v, 0 if k == "n" else (1 if k.startswith("e") else 3)) for k, v in S.items()},
           "etat10": None, "emissions": [{k: o.get(k) for k in ("isin", "nom", "emis", "montant", "dev", "coupon", "ech", "rdt_emis",
                                                                   "ecart_emis", "ans_emis", "rdt", "ecart", "a144")} for o in emis],
           "emissions_trim": dict(sorted(trim.items())), "mur": {"annees": sorted(mur), "md": [round(mur[a], 2) for a in sorted(mur)],
                                                                  "dernier_groupe": auj.year + 10},
           "notes": notes, "comptes": comptes, "fx_eurusd": taux_fx}
    if courbe and S.get("e10"):
        det["etat10"] = serie_pts([(d, courbe.taux(date.fromisoformat(d).toordinal(), 10)) for d, _ in S["e10"]
                                   if courbe.taux(date.fromisoformat(d).toordinal(), 10) is not None], 3)
    return det, sy


# ═══════════════════════════════════════════════════════════════════════════
# 6. LE PASSAGE
# ═══════════════════════════════════════════════════════════════════════════
def joli(cle):
    """« JPMORGAN CHASE + CO » → « Jpmorgan Chase & Co » (classement général)."""
    s = cle.replace(" + ", " & ").title()
    return re.sub(r"\b(Llc|Lp|Sa|Ag|Nv|Se|Bv|Plc)\b", lambda m: m.group(1).upper(), s)


def main():
    t0 = time.time()
    journal = []
    maintenant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    auj = date.today()
    prec_synth = lire_json("oblig_emetteurs.json") or {}

    U, au = univers(journal)
    if len(U) < 3000:
        log("[erreur] univers SPDR trop maigre (%d lignes) : rien n'est réécrit" % len(U))
        return 1
    log("[info] univers : %d obligations au %s (%.0f s)" % (len(U), au, time.time() - t0))

    # courbes d'État (réutilise oblig_souverains)
    courbes = {}
    try:
        courbes["USD"] = Courbe(oblig_souverains.src_us()["series"])
    except Exception as e:  # noqa: BLE001
        journal.append("courbe Trésor : " + str(e)[:100])
    try:
        courbes["EUR"] = Courbe(oblig_souverains.src_de()["series"])
    except Exception as e:  # noqa: BLE001
        journal.append("courbe Bund : " + str(e)[:100])
    fxp = fred("DEXUSEU")
    fx = {"dernier": fxp[-1][1] if fxp else None, "au": fxp[-1][0] if fxp else None}
    if not fx["dernier"]:
        journal.append("euro-dollar FRED vide : conversions au taux du passage précédent")
        fx = prec_synth.get("fx") or {"dernier": 1.15, "au": None, "repli": True}
    log("[info] courbes : %s ; EUR/USD %s (%.0f s)" % ({k: v.dernier() for k, v in courbes.items()}, fx["dernier"], time.time() - t0))

    # CIK des sociétés américaines (SEC)
    cik_map = {}
    ct = get_json("https://www.sec.gov/files/company_tickers.json", accept="application/json", timeout=60)
    for v in (ct or {}).values():
        try:
            cik_map[str(v["ticker"]).upper()] = int(v["cik_str"])
        except (KeyError, TypeError, ValueError):
            pass

    # répartition de l'univers par émetteur
    par_cle = defaultdict(list)
    for x in U.values():
        par_cle[x["cle"]].append(x)
    reg = lire_json("oblig_em_registre.json") or {}
    reg = reg.get("titres", reg) if isinstance(reg, dict) else {}
    a_faire = [em for em in EMETTEURS if not SEULEMENT or em["code"] in SEULEMENT]
    # le plus ancien d'abord : si le budget coupe, ce sont les fiches les plus fraîches qui attendent
    age = {x["code"]: x.get("genere_le") or "" for x in prec_synth.get("emetteurs") or []}
    a_faire.sort(key=lambda em: age.get(em["code"], ""))
    synth_par_code = {x["code"]: x for x in prec_synth.get("emetteurs") or []}
    tailles, faits, repris, n_req = {}, 0, 0, 0
    for em in a_faire:
        lignes = []
        for k in em["usd"]:
            lignes += [x for x in par_cle.get(k, []) if x["dev"] == "USD"]
        for k in em["eur"]:
            lignes += [x for x in par_cle.get(k, []) if x["dev"] == "EUR"]
        vus = set()
        lignes = [x for x in lignes if not (x["isin"] in vus or vus.add(x["isin"]))
                  and date.fromisoformat(x["ech"]) > auj and x["dev"] in ("USD", "EUR")]
        if not lignes:
            journal.append("%s : aucune obligation dans l'univers" % em["code"])
            continue
        if time.time() - t0 > BUDGET_S:
            repris += 1
            continue
        # fiches onvista manquantes (une fois pour toutes ; absente → nouvel essai dans 14 jours)
        for x in lignes:
            r = reg.get(x["isin"])
            if r and (r.get("idn") or r.get("places") or r.get("v") == 2) and (not r.get("absent") or (r.get("essai") or "") > il_y_a(auj.isoformat(), 14)):
                continue
            if OV_ETAT["hs"]:
                break
            f = fiche_ov(x["isin"], x["dev"])
            n_req += 1
            if f is None:
                continue
            f["essai"] = auj.isoformat()
            reg[x["isin"]] = f
        # cours quotidiens (tous les titres de l'émetteur, à chaque passage : la fiche reste cohérente)
        attendus = echecs = 0
        for x in lignes:
            r = reg.get(x["isin"]) or {}
            if r.get("absent") or not (r.get("idn") or r.get("places")):
                continue
            attendus += 1
            if not r.get("idn"):
                # première fois : la place qui a le plus long historique (au plus 3 essais)
                meilleur = None
                for nom_p, idn in r["places"][:3]:
                    h = cours_ov(r["iid"], idn)
                    n_req += 1
                    if h and (meilleur is None or h[0][0] < meilleur[2][0][0] or (h[0][0] == meilleur[2][0][0] and len(h) > len(meilleur[2]))):
                        meilleur = (nom_p, idn, h)
                    if h and r.get("emis") and h[0][0] <= il_y_a(r["emis"], -20):
                        break                              # cote depuis l'émission : inutile de chercher ailleurs
                if meilleur:
                    r["idn"], r["place"] = meilleur[1], meilleur[0]
                    r["_hist"] = meilleur[2]
            else:
                h = cours_ov(r["iid"], r["idn"])
                n_req += 1
                if h:
                    r["_hist"] = h
            if not r.get("_hist"):
                echecs += 1
            if r.get("_hist"):
                r["_place"] = r.get("place")
                # garde : un historique qui s'arrête depuis plus de 10 jours n'est plus coté
                if (auj - date.fromisoformat(r["_hist"][-1][0])).days > 10:
                    r["_hist"] = [p for p in r["_hist"]]   # gardé (le point du jour vient alors de SPDR)
        prec = lire_json("oblig_em_%s.json" % em["code"])
        # JAMAIS À RECULONS : sans les cours d'onvista, la fiche retomberait sur les seuls
        # prix SPDR du jour et perdrait des années d'historique. On garde la précédente.
        if prec and attendus and echecs > 0.3 * attendus:
            journal.append("%s : %d cours onvista sur %d en échec, fiche du %s reprise" % (em["code"], echecs, attendus, (prec.get("genere_le") or "")[:10]))
            for x in lignes:
                (reg.get(x["isin"]) or {}).pop("_hist", None)
            repris += 1
            continue
        try:
            det, sy = construire_emetteur(em, lignes, reg, courbes, fx, prec, journal, cik_map)
        except Exception as e:  # noqa: BLE001
            journal.append("%s : %s" % (em["code"], str(e)[:160]))
            for x in lignes:
                (reg.get(x["isin"]) or {}).pop("_hist", None)
            continue
        for x in lignes:
            (reg.get(x["isin"]) or {}).pop("_hist", None)
            (reg.get(x["isin"]) or {}).pop("_place", None)
        sy["genere_le"] = maintenant
        det["genere_le"] = maintenant
        det["prix_spdr_au"] = au
        tailles["oblig_em_%s.json" % em["code"]] = ecrire("oblig_em_%s.json" % em["code"], nettoyer(compacter_arbre(det)))
        synth_par_code[em["code"]] = sy
        faits += 1
        log("[info] %-18s %3d obligations, %3d cotées, réf %s : %s %% / %s pb (%.0f s, %d requêtes)"
            % (em["code"], sy["n"], sy["n_cotees"], sy.get("ref"), sy.get("rdt"), sy.get("ecart"), time.time() - t0, n_req))

    # ── classement de tout l'univers : qui doit le plus (en valeur de marché, dollars) ──
    taux_fx = fx.get("dernier") or 1.0
    cle_code = {}
    for em in EMETTEURS:
        for k in em["usd"] + em["eur"]:
            cle_code[k] = em["code"]
    poids = defaultdict(lambda: [0.0, 0])
    for x in U.values():
        if not x.get("mv") and not x.get("par"):
            continue
        v = x["mv"] if x["dev"] == "USD" else (x["par"] or 0) * x["prix"] / 100 * taux_fx if x["dev"] == "EUR" else None
        if not v:
            continue
        k = cle_code.get(x["cle"], "~" + x["cle"])
        poids[k][0] += v
        poids[k][1] += 1
    total = sum(v[0] for v in poids.values())
    classement = []
    for k, (v, n) in sorted(poids.items(), key=lambda z: -z[1][0])[:60]:
        em = next((e for e in EMETTEURS if e["code"] == k), None)
        classement.append({"code": k if em else None, "nom": em["nom"] if em else joli(k[1:]), "part": round(100 * v / total, 2), "n": n,
                           "famille": em["famille"] if em else None})

    # ── la vague : émissions par trimestre et par famille (montants en dollars) ──
    vague = defaultdict(lambda: defaultdict(float))
    ecarts_fam = defaultdict(lambda: defaultdict(list))
    for em in EMETTEURS:
        d = lire_json("oblig_em_%s.json" % em["code"])
        if not d:
            continue
        for o in d.get("emissions") or []:
            if not estnb(o.get("montant")) or not o.get("emis"):
                continue
            v = o["montant"] * (taux_fx if o.get("dev") == "EUR" else 1.0)
            vague[em["famille"]]["%s-T%d" % (o["emis"][:4], (int(o["emis"][5:7]) - 1) // 3 + 1)] += v / 1e9
        e10 = ((d.get("series") or {}).get("e10")) or {}
        if e10.get("d0") and e10.get("dj"):
            o0 = date.fromisoformat(e10["d0"]).toordinal()
            o = o0
            for k, (dj, v) in enumerate(zip(e10["dj"], e10["v"])):
                o += dj if k else 0
                if o >= auj.toordinal() - 3 * 365 and estnb(v):
                    ecarts_fam[em["famille"]][_semaine(o)].append(v)
    trims = sorted({t for f in vague.values() for t in f})[-12:]
    familles_ecart = {}
    for f, sem in ecarts_fam.items():
        L = []
        for s in sorted(sem):
            vals = sorted(sem[s])
            if len(vals) >= 3:
                a, w = divmod(s, 100)
                L.append((date.fromisocalendar(a, w, 5).isoformat(), vals[len(vals) // 2]))
        if len(L) > 20:
            familles_ecart[f] = serie_pts(L, 1)

    # registre : sans les historiques (ils sont recalculés à chaque passage)
    for r in reg.values():
        r.pop("_hist", None)
        r.pop("_place", None)
        for k in ("emetteur_ov", "coupon", "ech", "dev"):
            r.pop(k, None)
        if r.get("idn"):
            r.pop("places", None)          # la place est choisie : la liste ne sert plus
        for k in [k for k, v in r.items() if v is None or v is False]:
            r.pop(k)
    ecrire("oblig_em_registre.json", {"genere_le": maintenant, "titres": reg})

    ems = [synth_par_code[em["code"]] for em in EMETTEURS if em["code"] in synth_par_code]
    synth = {"genere_le": maintenant, "prix_spdr_au": au, "fx": fx, "familles": [{"code": c, "nom": n} for c, n in FAMILLES],
             "emetteurs": ems, "classement": classement, "univers": {"n": len(U), "total_md_usd": round(total / 1e9)},
             "vague": {"trimestres": trims, "familles": {f: [round(v.get(t, 0), 2) for t in trims] for f, v in vague.items()},
                       "unite": "Md$", "nature": "obligations encore en circulation, par date d'émission"},
             "ecarts_familles": familles_ecart,
             "sources": [
                 {"nom": "SPDR (State Street Global Advisors)", "quoi": "avoirs quotidiens de 8 ETF obligataires : univers et prix du jour",
                  "url": "https://www.ssga.com/"},
                 {"nom": "onvista", "quoi": "fiche d'émission et cours quotidien de chaque obligation (places allemandes)",
                  "url": "https://www.onvista.de/anleihen/"},
                 {"nom": "Trésor américain, FRED, Bundesbank", "quoi": "courbes d'État (écarts) ; euro-dollar", "url": "https://fred.stlouisfed.org/"},
                 {"nom": "ESMA (registre RADAR)", "quoi": "notes S&P, Moody's, Fitch et leur histoire",
                  "url": "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_radar"},
                 {"nom": "SEC EDGAR", "quoi": "comptes annuels (dette, trésorerie, intérêts, investissements)", "url": "https://www.sec.gov/"}],
             "journal": journal[:60], "duree_s": round(time.time() - t0), "faits": faits, "repris": repris}
    tailles["oblig_emetteurs.json"] = ecrire("oblig_emetteurs.json", nettoyer(compacter_arbre(synth)))
    log("[info] %d fiches refaites, %d reprises, %d requêtes onvista, %.0f s ; plus gros fichier : %s"
        % (faits, repris, n_req, time.time() - t0, max(tailles.items(), key=lambda z: z[1]) if tailles else None))
    log("[info] journal : %s" % journal[:20])
    return 0 if faits or repris else 1


if __name__ == "__main__":
    sys.exit(main())
