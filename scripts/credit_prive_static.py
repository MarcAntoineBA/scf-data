# -*- coding: utf-8 -*-
"""credit_prive_static.py — données STATIQUES SOURCÉES de l'onglet Crédit Privé.

Ces chiffres n'existent pas en série temporelle publique (FRED). Ils proviennent
d'un dossier de recherche multi-agents avec vérification adversariale (juin 2026).
CHAQUE entrée porte sa source + URL cliquable. Les valeurs réfutées en
vérification ont été corrigées (ex. prêts banques→NDFI = ~1,72 T$, pas 1,32 ;
délinquance étudiante ~10 % au T2 2025 et non T1 ; First Brands ~8,5x et non 20x).
"""

# ── URLs sources (réutilisées) ──────────────────────────────────────────────
U = {
    "fed_lending": "https://www.federalreserve.gov/econres/notes/feds-notes/bank-lending-to-private-credit-size-characteristics-and-financial-stability-implications-20250523.html",
    "fed_risks":   "https://www.federalreserve.gov/econres/notes/feds-notes/private-credit-characteristics-and-risks-20240223.html",
    "fsb":         "https://www.fsb.org/uploads/P060526.pdf",
    "fsb_nbfi":    "https://www.fsb.org/2025/12/fsb-reports-continued-growth-in-nonbank-financial-intermediation-in-2024-to-256-8-trillion/",
    "imf":         "https://www.imf.org/-/media/files/publications/gfsr/2024/april/english/ch2.pdf",
    "bis_sw":      "https://www.bis.org/publ/qtrpdf/r_qt2603v.htm",
    "bis_retail":  "https://www.bis.org/publ/bisbull106.htm",
    "boe":         "https://www.bankofengland.co.uk/financial-stability-report/2025/december-2025",
    "pio_medallia":"https://www.pionline.com/latest-news/pi-apollo-kkr-record-gap-valuing-stressed-private-loan/",
    "afn_auto":    "https://www.autofinancenews.net/allposts/risk-management/60-plus-day-subprime-auto-dqs-hit-32-year-high/",
    "forbes_fitch":"https://www.forbes.com/sites/mayrarodriguezvalladares/2026/05/24/rising-private-credit-defaults-are-testing-banks-and-insurers/",
    "kbra":        "https://finance.yahoo.com/news/kbra-releases-research-private-credit-223200414.html",
    "pitchbook_def":"https://pitchbook.com/news/articles/us-leveraged-loan-default-rates-rise-in-march-as-distress-ratio-hits-3-year-high",
    "lincoln":     "https://www.lincolninternational.com/news/the-lincoln-private-market-index-ends-the-year-with-its-slowest-quarter-of-growth-in-2025/",
    "caia":        "https://caia.org/blog/2026/04/20/private-credit-redemptions-defaults-and-wrappers-oh-my",
    "vaneck":      "https://www.vaneck.com/us/en/blogs/income-investing/what-is-driving-bdc-valuations/",
    "bbg_807":     "https://www.bloomberg.com/news/articles/2026-06-08/us-life-insurers-held-807-billion-of-highly-illiquid-credit",
    "nyfed_hhdc":  "https://www.newyorkfed.org/newsevents/news/research/2026/20260512",
    "nyfed_stu25": "https://libertystreeteconomics.newyorkfed.org/2025/05/student-loan-delinquencies-are-back-and-credit-scores-take-a-tumble/",
    "nyfed_stu26": "https://libertystreeteconomics.newyorkfed.org/2026/05/federal-student-loan-defaults-return-after-pandemic-pause/",
    "fred_nbfi":   "https://fred.stlouisfed.org/series/LNFACBM027SBOG",
    "moodys_300":  "https://www.bloomberg.com/news/articles/2025-10-21/us-banks-back-300-billion-of-private-credit-debt-moody-s-says",
    "ubs15":       "https://www.bloomberg.com/news/articles/2026-02-24/ubs-now-sees-private-credit-defaults-reaching-15-in-worst-case",
    "dimon":       "https://fortune.com/2025/10/15/jamie-dimon-issues-private-credit-warning-when-you-see-one-cockroach-there-are-probably-more/",
    "gundlach":    "https://fortune.com/2025/11/18/jeffrey-gundlach-bond-king-next-financial-crisis-private-credit-subprime-mortgage/",
    "powell":      "https://www.thecrimson.com/article/2026/3/30/powell-private-credit/",
    "capeco":      "https://www.capitaleconomics.com/publications/global-economics-update/private-credit-clos-not-repeat-2008-cdos",
    "pluralsight": "https://www.bloomberg.com/news/articles/2024-08-22/blue-owl-led-private-debt-group-takes-ownership-of-pluralsight",
    "firstbrands": "https://www.cnbc.com/2025/10/10/first-brands-implosion-lenders-scramble-to-contain-the-fallout-.html",
    "tricolor":    "https://www.justice.gov/usao-sdny/pr/ceo-cfo-coo-charged-connection-billion-dollar-collapse-tricolor-auto",
    "tcpc":        "https://pitchbook.com/news/articles/blackrock-bdc-reports-19-nav-decline-in-q4-as-non-accruals-bite",
    "sw_selloff":  "https://pitchbook.com/news/articles/ranks-of-distressed-software-leveraged-loan-issuers-balloon-to-record-25b",
    "elucid":      "https://elucid.media/economie/quelque-chose-de-gros-se-prepare-avec-la-finance-frederic-lordon",
    "diplo":       "https://blog.mondediplo.net/la-crise-financiere-qui-vient",
    "youtube":     "https://www.youtube.com/watch?v=Yu-wqYCylnU",
}

STATIC = {
    "lordon": {
        "video": U["youtube"], "elucid": U["elucid"], "diplo": U["diplo"],
    },

    # KPIs non-live (HY spread, NBFI, cartes, charge-offs, DSR, conso = calculés depuis FRED en JS)
    "kpi": {
        # — Crédit Privé —
        "us_market":     {"val": "1,34 T$", "lab": "Crédit privé · États-Unis", "sub": "≈ ×5 depuis 2009 (T2 2024)", "url": U["fed_lending"], "tone": ""},
        "default_fitch": {"val": "~6,0 %",  "lab": "Défaut du crédit privé US", "sub": "record, avril 2026 (Fitch)", "url": U["forbes_fitch"], "tone": "alert"},
        "software":      {"val": "20–30 %", "lab": "Crédit privé exposé au software", "sub": "secteur cannibalisé par l'IA", "url": U["bis_sw"], "tone": "alert"},
        "life_ins":      {"val": "807 Md$", "lab": "Assureurs-vie · crédit illiquide", "sub": "20 % de leurs obligations (fin 2025)", "url": U["bbg_807"], "tone": "warn"},
        "medallia_gap":  {"val": "14 pts",  "lab": "Écart de valorisation record", "sub": "même prêt : 77¢ / 82¢ / 91¢ (Medallia)", "url": U["pio_medallia"], "tone": "warn"},
        "bdc_discount":  {"val": "0,85×",   "lab": "BDC cotées vs valeur d'actif", "sub": "décote ~15 % · marks contestés", "url": U["vaneck"], "tone": "warn"},
        # — Crédit des Ménages —
        "households":    {"val": "18,79 T$","lab": "Dette des ménages US", "sub": "record absolu (T1 2026)", "url": U["nyfed_hhdc"], "tone": "warn"},
        "deliq_agg":     {"val": "4,8 %",   "lab": "Délinquance agrégée ménages", "sub": "moyenne contenue, mais polarisée", "url": U["nyfed_hhdc"], "tone": ""},
        "subprime_auto": {"val": "6,9 %",   "lab": "Auto subprime · impayés 60+ j", "sub": "record sur 32 ans (vs 0,4 % prime)", "url": U["afn_auto"], "tone": "alert"},
        "student_shock": {"val": "~10 %",   "lab": "Prêts étudiants · impayés 90+ j", "sub": "<1 % avant fin de l'on-ramp (T2 2025)", "url": U["nyfed_stu25"], "tone": "alert"},
    },

    # § Mécanique : le même prêt, trois prix (mark-to-myth)
    "medallia": {
        "title": "Le même prêt, trois prix",
        "bars": [
            {"label": "Apollo", "val": 77, "tone": "red"},
            {"label": "Blackstone", "val": 82, "tone": "gold"},
            {"label": "KKR / Future Standard", "val": 91, "tone": "em"},
        ],
        "unit": "cents par dollar", "annot": "Écart record : 14 points sur un actif identique (prêt à Medallia)",
        "as_of": "13 nov. 2025", "url": U["pio_medallia"],
    },

    # § Mécanique : montée du PIK
    "pik": {
        "title": "L'intérêt payé en dette (PIK)",
        "bars": [{"label": "2021", "val": 7}, {"label": "2025", "val": 11}],
        "unit": "% des prêts au PIK",
        "note": "≈ la moitié sous forme de « PIK toggles », corrélés à la détresse · 8,3 % du revenu d'intérêts",
        "as_of": "T4 2025 (Lincoln)", "url": U["lincoln"],
    },

    # § Mécanique : levier affiché vs réel
    "leverage": {
        "title": "Le levier réel dépasse le levier affiché",
        "bars": [
            {"label": "Leveraged loans (réf.)", "val": 4.0, "tone": "em"},
            {"label": "Crédit privé — affiché", "val": 5.5, "tone": "gold"},
            {"label": "Crédit privé — réel*", "val": 7.0, "tone": "red"},
        ],
        "unit": "× EBITDA", "note": "*après retraitement des « add-backs ». Source : FSB, d'après UBS.",
        "as_of": "nov. 2025", "url": U["fsb"],
    },

    # § Mécanique : flambée des rachats (liquidité asymétrique)
    "redemptions": {
        "title": "Flambée des rachats sur BDC non cotés",
        "bars": [{"label": "T3 2025", "val": 1.6}, {"label": "T4 2025", "val": 4.8}],
        "unit": "% du NAV racheté/trimestre",
        "note": "5 fonds ont honoré des rachats au-dessus de leur plafond (« gate ») au T4 2025",
        "as_of": "T4 2025", "url": U["caia"],
    },

    # § Croissance : explosion des prêts au software
    "software": {
        "title": "L'explosion des prêts directs au logiciel (SaaS)",
        "bars": [{"label": "2015", "val": 8}, {"label": "2025", "val": 500}],
        "unit": "Md$ de prêts directs au software",
        "note": "≈ 19 % du direct lending. Le cœur de l'alerte de Lordon : un secteur cannibalisé par l'IA.",
        "as_of": "fin 2025 (BIS)", "url": U["bis_sw"],
    },

    # § Contagion : circularité assureurs ↔ private equity
    "insurance_pe": {
        "title": "Les passifs d'assurance happés par le private equity",
        "bars": [{"label": "2012", "val": 67}, {"label": "2024", "val": 900}],
        "unit": "Md$ de passifs d'assurance sous contrôle PE",
        "note": "Assureurs adossés au PE (Athene/Apollo, Global Atlantic/KKR) — qualité de crédit inférieure",
        "as_of": "fin 2024 (FSB / BoE)", "url": U["fsb"],
    },

    # § Stress : le défaut dépend de comment on le compte
    "defaults": {
        "title": "Le défaut dépend de la façon de le compter",
        "bars": [
            {"label": "KBRA (par nb d'emprunteurs)", "val": 3.4, "tone": "gold", "url": U["kbra"]},
            {"label": "PitchBook (« dual-track »)", "val": 3.48, "tone": "amber", "url": U["pitchbook_def"]},
            {"label": "Fitch (record)", "val": 6.0, "tone": "red", "url": U["forbes_fitch"]},
        ],
        "unit": "% de défaut", "note": "Le taux « élargi » (incl. restructurations en difficulté) est bien supérieur au défaut de paiement affiché",
        "as_of": "T4 2025 – avr. 2026",
    },

    # § Ménages : auto subprime vs prime
    "auto": {
        "title": "La fracture du crédit auto : subprime vs prime",
        "bars": [{"label": "Prime", "val": 0.4, "tone": "em"}, {"label": "Subprime", "val": 6.9, "tone": "red"}],
        "unit": "% des titrisations en impayé 60+ j", "note": "Écart > 15× — record sur 32 ans pour le subprime (janv. 2026)",
        "as_of": "janv. 2026", "url": U["afn_auto"],
    },

    # § Ménages : choc des prêts étudiants
    "student": {
        "title": "Le choc des prêts étudiants",
        "bars": [{"label": "Avant oct. 2024", "val": 1.0, "tone": "em"}, {"label": "T2 2025", "val": 10.0, "tone": "red"}],
        "unit": "% des soldes en impayé 90+ j",
        "note": "Fin de l'« on-ramp » → réapparition des défauts aux bureaux de crédit · ~5,6 M d'emprunteurs nouvellement délinquants",
        "as_of": "T2 2025 (NY Fed)", "url": U["nyfed_stu25"],
    },

    # § Chronologie des effondrements
    "blowups": [
        {"date": "Août 2024", "name": "Pluralsight", "amount": "~4 Md$ de pertes",
         "desc": "Premier grand « drop-down » du crédit privé : Vista perd ~4 Md$, les prêteurs (Blue Owl, Ares, Golub, Oaktree, BlackRock) prennent 85 % de l'éditeur.",
         "exposed": "Blue Owl · Ares · Golub · Oaktree · BlackRock", "url": U["pluralsight"]},
        {"date": "10 sept. 2025", "name": "Tricolor", "amount": "~800 M$ double-nantis",
         "desc": "Prêteur subprime auto en faillite (Chapter 7) sur fond de fraude : même collatéral nanti auprès de plusieurs banques.",
         "exposed": "JPMorgan (−170 M$) · Fifth Third · Barclays", "url": U["tricolor"]},
        {"date": "Sept.–oct. 2025", "name": "First Brands", "amount": ">10 Md$ de passif",
         "desc": "Équipementier auto : faillite (Chapter 11) avec ~2,3 Md$ d'actifs « disparus » du hors-bilan. Déclencheur du « cafard » de Dimon.",
         "exposed": "Point Bonita/Jefferies (715 M$) · UBS · BlackRock", "url": U["firstbrands"]},
        {"date": "14 oct. 2025", "name": "Alerte Dimon", "amount": "« un cafard »",
         "desc": "« Quand on voit un cafard, il y en a probablement d'autres. » Jamie Dimon (JPMorgan) après les charge-offs Tricolor.",
         "exposed": "Tout le marché", "url": U["dimon"]},
        {"date": "13 nov. 2025", "name": "Medallia", "amount": "écart de 14 pts",
         "desc": "Le même prêt valorisé 77c / 82c / 91c par trois grands gérants — illustration du « mark-to-myth ».",
         "exposed": "Apollo · Blackstone · KKR", "url": U["pio_medallia"]},
        {"date": "Déc. 2025", "name": "BlackRock TCPC", "amount": "NAV −19 %",
         "desc": "Une BDC cotée voit sa valeur d'actif net chuter de 19 %, non-accruals à 9,6 %, PIK à 10,9 % du revenu net.",
         "exposed": "Actionnaires de la BDC", "url": U["tcpc"]},
        {"date": "Fév. 2026", "name": "Sell-off software", "amount": "~25 Md$ < 80c",
         "desc": "La dette d'éditeurs software décote massivement : +17,7 Md$ d'émetteurs en difficulté en 4 semaines — le secteur que vise Lordon.",
         "exposed": "Détenteurs de leveraged loans tech", "url": U["sw_selloff"]},
    ],

    # § Que surveiller
    "watch": [
        {"ico": "fa-magnifying-glass-dollar", "t": "L'écart « défaut élargi » vs défaut de paiement", "d": "Le dual-track inclut les restructurations en difficulté souvent masquées."},
        {"ico": "fa-tags", "t": "La décote des BDC cotées vs NAV", "d": "Sous 0,80–0,85× la NAV : signal que les « marks » sont surévalués."},
        {"ico": "fa-money-bill-transfer", "t": "Part du PIK & taux de non-accrual", "d": "Hausse = tarissement de la trésorerie réelle des emprunteurs."},
        {"ico": "fa-door-closed", "t": "Rachats sur fonds non cotés & « gates »", "d": "L'activation des plafonds de rachat signale un stress de liquidité."},
        {"ico": "fa-building-columns", "t": "Prêts des banques aux non-banques (FRED)", "d": "Le canal de contagion principal vers le système bancaire régulé."},
        {"ico": "fa-microchip", "t": "Exposition software & actions SaaS", "d": "Le cœur de l'alerte Lordon : un secteur cannibalisé par l'IA."},
        {"ico": "fa-car-burst", "t": "Crédit subprime des ménages (auto, étudiant)", "d": "La polarisation par revenu, premier symptôme visible."},
    ],

    # § Que surveiller — version ménages
    "watch_menages": [
        {"ico": "fa-car-burst", "t": "Délinquance auto subprime", "d": "Le canari : record sur 32 ans, écart >15× avec le prime."},
        {"ico": "fa-graduation-cap", "t": "Défauts sur prêts étudiants", "d": "Réapparition aux bureaux de crédit après la fin de l'on-ramp."},
        {"ico": "fa-credit-card", "t": "Délinquance & pertes cartes de crédit", "d": "Le crédit renouvelable, fracturé par niveau de revenu."},
        {"ico": "fa-scale-unbalanced", "t": "Service de la dette / revenu", "d": "La capacité réelle des ménages à rembourser."},
        {"ico": "fa-house-crack", "t": "Délinquance immobilière", "d": "Encore basse — le pilier à surveiller s'il se fissure."},
        {"ico": "fa-mobile-screen", "t": "Dette « fantôme » du BNPL", "d": "Achats fractionnés mal mesurés par les bureaux de crédit."},
    ],
}
