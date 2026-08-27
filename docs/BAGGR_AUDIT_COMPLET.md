# Baggr — Audit produit complet (reverse engineering fonctionnel)

> **Document de référence destiné à Claude Code** pour la refonte du module *Analyse Fondamentale* de **Capital Antifragile**.
> Cible : construire un produit **plus complet et supérieur** à Baggr, sachant que l'analyse fondamentale n'est qu'**une composante** de Capital Antifragile.
>
> Source : exploration exhaustive de `app.baggr.fr` (compte connecté, plan complet) + site vitrine `baggr.fr`.
> Date de l'audit : **27 août 2026**.
> Méthode : navigation complète de l'UI, extraction DOM des libellés, inspection du bundle JS (routes + endpoints API).

---

## Sommaire

1. [Positionnement, pricing et chiffres clés](#1-positionnement-pricing-et-chiffres-clés)
2. [Stack technique et architecture](#2-stack-technique-et-architecture)
3. [Carte complète des routes](#3-carte-complète-des-routes)
4. [API et modèle de données](#4-api-et-modèle-de-données)
5. [Layout global et navigation](#5-layout-global-et-navigation)
6. [Page d'accueil (Dashboard)](#6-page-daccueil-dashboard)
7. [★ LA FICHE ACTION — le cœur du produit](#7--la-fiche-action--le-cœur-du-produit)
   - 7.1 [Onglet Résumé](#71-onglet-résumé)
   - 7.2 [Onglet Quantitatif](#72-onglet-quantitatif)
   - 7.3 [Onglet Dividende](#73-onglet-dividende)
   - 7.4 [Onglet Résultats](#74-onglet-résultats)
   - 7.5 [Onglet Finances](#75-onglet-finances)
   - 7.6 [Onglet Thèses](#76-onglet-thèses)
   - 7.7 [Onglet Société](#77-onglet-société)
   - 7.8 [Onglet Valorisation](#78-onglet-valorisation)
8. [Screener](#8-screener)
9. [Comparateur](#9-comparateur)
10. [Watchlists](#10-watchlists)
11. [Portefeuille](#11-portefeuille)
12. [Calendriers](#12-calendriers)
13. [Idées](#13-idées)
14. [Communauté](#14-communauté)
15. [Ressources](#15-ressources)
16. [Patterns UI/UX transverses](#16-patterns-uiux-transverses)
17. [★ Dictionnaire exhaustif des métriques](#17--dictionnaire-exhaustif-des-métriques)
18. [Analyse concurrentielle et recommandations pour Capital Antifragile](#18-analyse-concurrentielle-et-recommandations-pour-capital-antifragile)

---

## 1. Positionnement, pricing et chiffres clés

### Proposition de valeur affichée
> « Screener ultra-rapide, suivi de portefeuille, valorisations précises, et communauté d'investisseurs passionnés. »

Baggr est un **outil d'analyse fondamentale + suivi de portefeuille + réseau social d'investisseurs**, en **français**, orienté investisseur particulier « quality / long terme » (PEA, dividendes, ROIC, marge de sécurité).

### Pricing (août 2026)

| Plan | Prix | Note |
|---|---|---|
| Essai gratuit | **14 jours**, sans CB | |
| Mensuel | **17 €/mois** | sans engagement |
| Annuel | **150 €/an** (12,50 €/mois) | −26 % |

Paiement via **Stripe**. Endpoints `/users/startFreeTrial`, `/users/endFreeTrial`, `/stripe/me`.

### Chiffres clés revendiqués

| Élément | Valeur |
|---|---|
| Univers d'actions | **50 000+** (le screener affiche **54 198** lignes, catégorie « Tout » = « 51K+ ») |
| Profondeur d'historique | **40+ ans** (observé : résultats trimestriels depuis **1999**, états financiers sur **11 exercices + TTM** affichés simultanément) |
| Utilisateurs | **6 586+** |
| Note moyenne | 4,9/5 (27 avis) |
| Pays couverts | ~50+ (US 8 202, CN 7 096, IN 5 488, JP 4 225, CA 3 375, KR 2 740, TW 2 253, AU 2 040, HK 1 655, GB 1 446, … FR 707) |
| Secteurs | 11 (GICS-like) |
| Places de marché | ~15 majeures listées (AMEX, Nasdaq GS, NYSE, TSX, TSXV, Euronext Amsterdam/Paris, LSE, Nasdaq Stockholm, Xetra, ASX, BSE, HKEX, JPX, SSE) |

### Faiblesses connues (revue externe + observation)
- **Pas d'application mobile native** (site responsive uniquement).
- Pas de suivi des fonds de PEE.
- **Performance** : temps de chargement long sur plusieurs pages (Macro, Calendriers, cartes de valorisation en lazy-load restent en skeleton plusieurs dizaines de secondes). Point faible exploitable.
- Méthodologie de la **Note Q** non documentée publiquement (boîte noire).

---

## 2. Stack technique et architecture

| Élément | Détail |
|---|---|
| Type | **SPA** (Single Page App), pas de SSR, pas de Next.js |
| Bundler | **Vite / Rolldown** (`assets/index-*.js` + `rolldown-runtime-*.js`) |
| Framework UI | **React** + **MUI** (Material UI v5/v6) — classes `MuiPaper`, `MuiGrid`, `MuiDataGrid`, `MuiSwitch`… |
| Tableaux | **MUI X DataGrid** (`useGridApiRef`, `ColumnManagerDropdown`, `customToolbar`) |
| Graphiques | **Highcharts** (`assets/highcharts-*.js`) — inclut Sankey, donut, radar, colonnes, aires |
| Data fetching | **TanStack Query** (`useQuery`, `useMutation`, `useQueries`, `QueryClientProvider`) |
| Routing | **react-router** (client-side, lazy chunks) |
| Code-splitting | **206 chunks** lazy-loadés |
| Backend | API REST **`https://api.baggr.fr`**, méthode **POST** systématique, CORS avec preflight OPTIONS |
| Auth | session cookie ; `/users/getUser`, `/users/init` |
| Paiement | Stripe |
| Support | **Intercom** (widget bulle bas-droite) |
| Analytics | Google Tag Manager, LinkedIn Insight, Rewardful (`r.wdfl.co` — affiliation) |
| Anti-bot | Google reCAPTCHA (inscription) |
| Contenu IA | **Google Gemini** (badge « Fourni par Gemini » sur les analyses qualitatives) |

**Identifiants** :
- ID interne d'une action = **UUID** (ex. `1c7a9641-8abb-44f8-bd47-7874aa8cbf91`)
- Ticker affiché = **`MIC:SYMBOL`** (ex. `XNGS:NVDA`, `XPAR:EL`, `XLON:REL`, `XTAI:2330`, `XKRX:000660`)
  → utilisation des **codes MIC ISO 10383**, choix intéressant pour un univers mondial.

---

## 3. Carte complète des routes

Extraites du bundle JS (`react-router`) :

```
/                          Dashboard (accueil)
/sign-in  /sign-up  /forgot-password  /verify-email

/screener                  Screener (liste + Catégories/Filtres/Colonnes/Mes vues)
/screener/:stockId         ★ FICHE ACTION (stockId = UUID)
/freeform                  Comparateur (Bêta)

/watchlists                Watchlists
/watchlists/settings       Gestion des watchlists
/portfolio                 Portefeuille
/portfolio/settings        Gestion des comptes / transactions

/calendars                 (redirige vers /calendars/results)
/calendars/results         Calendrier des résultats
/calendars/dividends       Calendrier des dividendes
/calendars/splits          Calendrier des splits
/calendars/ipos            Calendrier des IPOs

/theses                    Thèses (idées communauté)
/thesis/:thesisId          Détail d'une thèse (+ commentaires)
/articles                  Articles
/article/:articleId        Détail article
/transcript/:transcriptId  Transcript de conférence de résultats
/super-investors           Super-investisseurs (13F)
/super-investors/:investorId
/listes                    Sélections Baggr (listes curatées)
/listes/:slug              Détail d'une sélection

/community                 Communauté
/members                   Membres
/members/:userId
/most-followed             Les plus suivies
/public-portfolios         Portefeuilles publics
/public-watchlists         Watchlists publiques
/discord                   Discord

/indexes                   Indices
/macro                     Macro
/compound-interest         Calculatrice d'intérêts composés
/exchanges                 Marchés (horaires)
/classifications           Classifications (pays/secteurs/industries)

/academy                   Académie / formations
/academy/:formationId

/user                      Profil utilisateur
/profile/:slug             Profil public
/sharing                   Ressources partagées
/notifications/:token      Préférences de notification (public)
/upgrade                   Page d'abonnement
```

---

## 4. API et modèle de données

Base : `https://api.baggr.fr` — **tous les appels en POST**.

### Endpoints identifiés

**Utilisateur / compte**
```
/users/getUser
/users/init
/users/updateName
/users/checkSignupEligibility
/users/checkUsernameAvailability
/users/sendVerificationEmail
/users/acceptCGVU
/users/startFreeTrial
/users/endFreeTrial
/users/generateIntercomHash
/stripe/me
/notifications/getUnreadCount
```

**Actions / données fondamentales**
```
/stocks/getStocks                       Recherche / liste d'actions
/stocks/getStock                        Fiche action (métadonnées)
/stocks/getStockGeneralDatas            Bloc "Informations" + ratios agrégés
/stocks/getValuationData                Données de la calculatrice de valorisation
/stocks/getQuotesPrices                 Cotations
/utils/getStockTimeSeries               Séries temporelles (prix, indices, ratios)
```

**Valorisations communautaires**
```
/valuations/getValuations
/valuations/getValuation
/valuations/getCommunityAveragesForStocks
```

**Watchlists**
```
/watchlists/getWatchlists
/watchlists/getStocksGeneralDatasBatch  ← batch : clé de perf du screener/watchlist
```

**Portefeuille**
```
/portfolio/getAssets
/portfolio/getTransactions
/portfolio/getBrokerageAccounts
/portfolio/getAnalyticsData
```

### Enseignements structurants pour Capital Antifragile

1. **Un endpoint "batch"** (`getStocksGeneralDatasBatch`) alimente le screener ET les watchlists → un seul modèle de données « ratios agrégés par action » sert tous les tableaux. À répliquer : une table/materialized view `stock_metrics` dénormalisée, une ligne par action, ~250 colonnes.
2. **Séparation nette** entre :
   - `generalDatas` (snapshot de ratios, rapide, batchable) ;
   - `valuationData` (calculatrice, plus lourd) ;
   - `timeSeries` (séries longues, lazy) ;
   - états financiers complets (chargés à l'ouverture de l'onglet Finances).
3. **Valorisation communautaire** = table à part (`valuations`), agrégée par action (moyenne des estimations utilisateurs). C'est un **actif de rétention** très fort et peu coûteux à implémenter.

---

## 5. Layout global et navigation

### Structure
```
┌─────┬──────────────────────────────────────────────────────────┐
│ LOGO│  [🔍 Rechercher une action...        ]     🔔  ❓         │  ← header sticky
├─────┼──────────────────────────────────────────────────────────┤
│ ▦   │                                                          │
│Accue│                                                          │
│ ★   │                                                          │
│Watch│                     CONTENU DE LA PAGE                   │
│ ▮▮  │                                                          │
│Porte│                                                          │
│ 🔍  │                                                          │
│Scree│                                                          │
│ 📊  │                                                          │
│Compa│                                                          │
│ 📅  │                                                          │
│Calen│                                                          │
│ 💡  │  ← menus flyout au clic (Idées, Calendriers, Communauté, │
│Idées│     Ressources)                                          │
│ 👥  │                                                          │
│Commu│                                                          │
│ 📁  │                                                          │
│Resso│                                                          │
├─────┤                                                          │
│ (m) │                                                          │
│Profi│                                                          │
└─────┴──────────────────────────────────────────────────────────┘
                                                    💬 Intercom ↘
```

### Sidebar (rail icônes + label, ~72 px)

| Item | Comportement |
|---|---|
| **Accueil** | route directe `/` |
| **Watchlists** | route directe |
| **Portefeuille** | route directe |
| **Screener** | route directe |
| **Comparateur** | route directe (`/freeform`) |
| **Calendriers** | flyout → Résultats · Dividendes · Splits · IPOs |
| **Idées** | flyout → Thèses · Super-Investisseurs · Sélections Baggr |
| **Communauté** | flyout → Membres · Portefeuilles · Watchlists · Les plus suivies · Discord |
| **Ressources** | flyout → Indices · Macro · Calculatrice · Marchés · Classifications |
| **Profil** (bas) | avatar coloré, initiale |

### Recherche globale (header)
Autocomplete riche. Chaque résultat affiche : **logo · nom · nb d'abonnés · Note Q (badge coloré) · pays (drapeau) · secteur · capitalisation**.
Dans les sélecteurs internes (comparateur), format alternatif : **logo · nom · `MIC:TICKER · Nom complet de la bourse`**.

> ⚠️ **Détail UX important** : cliquer une ligne du screener **ouvre la fiche action dans un NOUVEL ONGLET** (via `useOpenLink`). Ce n'est pas un lien `<a>` — c'est un handler JS. Conséquence : pas de clic-droit « ouvrir dans un onglet », pas de partage direct depuis la liste, et le référencement interne est nul. **À ne pas reproduire.**

---

## 6. Page d'accueil (Dashboard)

Blocs, dans l'ordre :

1. **Carrousel d'indices** (cartes horizontales scrollables) — S&P 500, NASDAQ 100, CAC 40, DAX, FTSE 100, Nikkei 225, SSE Composite, Taux US 10 ans. Chaque carte : logo/drapeau, dernier cours, variation %, mini-sparkline 2 ans, bouton plein-écran.

2. **« Les plus vues cette semaine »** — tableau : `Nom | Abonnés | Vues (7j)`.
   → **signal social** natif, très bon hook de rétention.

3. **« Principaux indices »** avec badge **« 😊 Cupidité 92/100 »** (indice Fear & Greed maison).
   Colonnes : `Nom | Dernier | ATH (date) | vs ATH % | position dans le range 52 sem. (jauge + %)`.
   Périmètre : S&P 500, Nasdaq-100, Dow Jones, Euro Stoxx 50, DAX, CAC 40, FTSE 100, Nikkei 225, SSE Composite, Or, Bitcoin, Pétrole.

4. **« Sélections Baggr »** — carrousel de listes thématiques (emoji + titre + nb d'actions + nb de vues).

5. **« Marchés »** — horaires d'ouverture groupés par zone (Amérique du Nord / Europe / Asie), avec badge **Ouvert / Fermé** temps réel et plage horaire locale.

6. **« Indicateurs Macro »** (au JJ/MM/AAAA) — 11 tuiles :
   `TAUX US 10 ANS · SPREAD 2A–10A · PIB RÉEL US (ANNUALISÉ) · TAUX D'INFLATION (CPI) · TAUX D'INFLATION (T10YIE) · CHÔMAGE · TAUX DIRECTEUR FED · TAUX HYPOTHÉCAIRE 30 ANS · CONFIANCE CONSO · PROBA. RÉCESSION · PRIME DE RISQUE ACTIONS US`
   \+ **Courbe des taux** (1M, 3M, 6M, 1A, 2A, 5A, 7A, 10A, 20A, 30A).
   → Données FRED / Treasury manifestement.

7. **« Dernières thèses »** — ticker, société, titre, auteur (@pseudo), date, perf depuis publication.

8. **« Derniers résultats »** — société, ticker, date.

9. **« Dernières actualités »** — titre, source (Proactive Investors, Seeking Alpha, 24/7 Wall Street, Investopedia, CNBC, MarketBeat…), date.

---

## 7. ★ LA FICHE ACTION — le cœur du produit

**URL** : `/screener/{uuid}`

### En-tête (persistant)
```
[logo] NVIDIA Corporation      209,66 USD   19,5/20 (?)        [ Suivre ▾ ]
       [XNGS:NVDA ▾]           ↘ -1,59%                        1955 abonnés
────────────────────────────────────────────────────────────────────────────
Résumé | Quantitatif | Dividende | Résultats | Finances | Thèses | Société | Valorisation
```
- Le chip `XNGS:NVDA ▾` est un **sélecteur de cotation** (permet de basculer entre les places où le titre est coté).
- **Note Q** (note quantitative /20) affichée en permanence, couleur selon niveau.
- **« Suivre »** + compteur d'abonnés → dimension sociale sur chaque action.

---

### 7.1 Onglet Résumé

Grille 2 colonnes (gauche ~55 %, droite ~45 %).

#### Colonne gauche

**a) Bandeau résultats**
- Carte « Derniers résultats » (dépliable) : `Résultats Q2 2027 · 26/08/26 · EPS +111,43% · Surprise +6,22%`
- Carte « Prochains résultats » : date `18/11/26`

**b) Carte « Informations »** — **la carte la plus dense du produit**, 9 sous-blocs :

| Bloc | Champs |
|---|---|
| **PROFIL** | ISIN · Pays (drapeau) · Site internet (lien) · Secteur · Industrie · Sous-industrie · **Note quantitative** · **PEA** (Éligible / ⊘ Non éligible) |
| **MARCHÉ** | Catégorie (`Mega Cap (+100Md$)`) · Capitalisation · **Bêta** · **Range 52S** (min-max) · Volume · Volume moyen |
| **VALORISATION** | **Prix juste** · **Prix communauté** · P/E · Forward P/E · P/FCF · P/OCF · P/S · P/B |
| **DIVIDENDE** | Yield TTM · Dividende annuel (TTM) · Payout ratio · Années d'augmentation · Div 1A · Div 5A · Div 10A |
| **REVENUS** | CA 1A · CA 5A · CA 10A · **Futur CA** |
| **EPS** | EPS 1A · EPS 5A · EPS 10A · **Futur EPS** |
| **MARGES** | Marge brute · Marge opé. · Marge nette · Marge FCF |
| **RENTABILITÉ** | ROE 5A · ROIC 5A · ROCE 5A · **ROIIC 5A** · **WACC** |
| **SANTÉ** | Dette nette/EBITDA · Interests Coverage · Goodwill/Assets · Actions en circulation 3A · **Altman Z-Score** · **Piotroski Score** |

> ⚑ **57 métriques dans une seule carte**, toutes colorées (vert/rouge/orange) selon un seuil de qualité. C'est la signature visuelle de Baggr.

**c) Carte « Super investisseurs »** — top détenteurs institutionnels (13F) : photo, nom, fonds, **% du portefeuille du fonds**, valeur de la position.
Ex. : Michael Burry / Scion 13,5 % / 186,6 M$ · Chase Coleman / Tiger Global 9,3 % / 2,2 Md$ · Cathie Wood / ARK 1,8 %…

**d) Carte « Business Model »** — texte long généré par IA, badge **✨ Fourni par Gemini**, dégradé de fondu + expansion.

**e) Carte « Dernières thèses »** — thèses de la communauté sur ce titre + CTA **« ✏️ Écrire une thèse »**.

#### Colonne droite

**f) Graphique de prix**
- Sélecteur **« Affichage »** (Prix, …), bouton plein écran.
- **Ligne de PRU** superposée si le titre est en portefeuille (`PRU : 209,66 $US`) — excellente idée.
- Footer de chips : **`Perf : 826,06%` · `CAGR : 56,12%` · `Linéarité : 0,95`**
  → la **« Linéarité »** (R² de la régression du cours) est une métrique différenciante de Baggr, reprise partout (indices, watchlists).

**g) Carte « Profil quantitatif »** — **radar à 6 axes**, échelle 0→4, badge Note Q :
`Retours sur capitaux · Marges · Croissance · Rentabilité · Dividende · Santé`

**h) Carte « Valorisation »** — barres horizontales comparant le prix actuel à 8 estimations, avec badge d'écart global (`-65,30%`) :
```
Prix juste            ████████████ 346,57 $
Prix de la communauté ████████████ 346,57 $
Prix P/E 10A          ████████████████ 449,73 $
Prix P/FCF 10A        ██████████ 308,48 $
Prix P/OCF 10A        █████████ 274,73 $
Prix P/Sales 10A      ██████ 222,12 $
Prix P/Book 10A       █████ 212,44 $
Prix Div. Yield 10A   ███ 175,00 $        (rouge = sous le cours)
        ┆ ligne verticale = cours actuel
```

**i) Carte « Comptes de résultat »** — **diagramme de Sankey** du P&L, avec sélecteur (Comptes de résultat / …).
Flux : segments de revenus → Chiffre d'affaires → (Coût des marchandises vendues / Résultat brut) → (Charges d'exploitation / Résultat d'exploitation) → … → Résultat net. Très visuel, très partageable.

---

### 7.2 Onglet Quantitatif

**Dashboard de graphiques personnalisable.**

#### Barre de contrôle
`☑ TTM` · `☑ Prévisions` · `[5A | 10A]` · `⚙ Options` · `(?)`

**Menu Options** : `Personnaliser` · `Sauvegarder` · `Restaurer` · `Supprimer` · `Réinitialiser`

**Panneau « Personnaliser le dashboard »** (drawer droite) :
> « Afficher ou masquer les graphiques, les réordonner par glisser-déposer, et ajouter des graphiques sauvegardés. »
Chaque widget : poignée de drag + toggle œil.

#### Les 20 widgets disponibles
| # | Widget | Contenu |
|---|---|---|
| 1 | **Chiffre d'affaires** | barres FY + TTM + prévisions (hachurées) · footer `Perf` / `CAGR` |
| 2 | **Résultat net** | idem |
| 3 | **Free cash flow** | idem |
| 4 | **KPIs** | KPI spécifiques à l'entreprise (ex. *Remaining Performance Obligations*) |
| 5 | **Segments** | donut du CA par segment, plage d'années (ex. 2014-2026) |
| 6 | **Répartition géographique** | donut du CA par pays |
| 7 | **Marges** | 3 séries : Marge brute · Marge opé. · Marge nette |
| 8 | **Rentabilité** | ROE · ROIC · ROCE |
| 9 | **ROIC vs WACC** | ROIC % · WACC % · **ROIC − WACC %** (création de valeur) |
| 10 | **Dépenses** | CAPEX/OCF · R&D/OCF · SBC/FCF |
| 11 | **Trésorerie & dette** | Trésorerie · Dette · Dette nette/EBITDA |
| 12 | **Actions en circulation** | dilution / relution |
| 13 | **Dividendes** | montant total versé |
| 14 | **Employés** | effectif par année |
| 15 | **Retours sur capitaux (Ratios)** | bloc de valeurs |
| 16 | **Rentabilité (Ratios)** | bloc de valeurs |
| 17 | **Croissance (Ratios)** | bloc de valeurs |
| 18 | **Flux de trésorerie (Ratios)** | bloc de valeurs |
| 19 | **Dividendes (Ratios)** | bloc de valeurs |
| 20 | **Santé financière (Ratios)** | bloc de valeurs |

\+ **« Ajouter un graphique sauvegardé »** (graphiques créés ailleurs et épinglés).

#### Détail des 6 blocs « Ratios »

**Retours sur capitaux** : `ROIC 1A / 5A / 10A` · `ROCE 1A / 5A / 10A` · `ROE 1A / 5A / 10A` · `ROIIC 1A / 5A / 10A`

**Rentabilité** : `Marge brute` · `Marge opé.` · `Marge nette` · `Marge FCF` · `%CAPEX/Revenue` · `%CAPEX/OCF` · `WACC` · `WACC 5A` · `WACC 10A`

**Croissance** : `CA 1A / 5A / 10A` · `EPS 1A / 5A / 10A` · `Futur CA 3-5A` · `Futur EPS 3-5A` · **`Prédictibilité du CA`**

**Cash Flows** : `FCF 1A / 5A / 10A` · `OCF 1A / 5A / 10A`

**Dividende** : `Div 1A / 5A / 10A` · `Augmentation` (années consécutives)

**Santé** : `Dette nette/EBITDA` · `Interests Coverage` · `Goodwill/Assets` · `Actions en circulation 3A` · `Payout ratio` · `Payout ratio 10A` · `Altman Z-Score` · `Piotroski Score`

---

### 7.3 Onglet Dividende

#### Carte « Métriques clés »
`YIELD TTM` · `PAYOUT RATIO` · `ANNÉES D'AUGMENTATION` · `CAGR 5A` · `CAGR 10A` · `FRÉQUENCE` (Quarterly / Semi-Annual / Annual / Irregular) · `DERNIER PAIEMENT` (date + montant) · `PROCHAIN PAIEMENT` · **`STATUT`** (badge : Aucun / Aristocrat / King…)

#### Les 8 graphiques
| Graphique | Séries / footer |
|---|---|
| **Dividende / Action** | barres FY + TTM · `Perf` / `CAGR` |
| **Dividend Yield** | courbe historique + ligne médiane · chips `High` / `Médiane` / `Low` |
| **Payout Ratios** | 2 courbes : `Earnings Payout Ratio` · `FCF Payout Ratio` |
| **Croissance Dividende / Action** | barres de croissance YoY |
| **Projection des dividendes** | **simulateur** : `Montant : 1 000,00 €` + `Croissance : 41,98%` → projection 10 ans du dividende annuel ET du **yield on cost** |
| **Shareholder Returns** | `Dividende` · `Rachats d'actions` · `Rendement pour l'actionnaire` (total) |
| **Shareholder Yield** | courbe + `High` / `Médiane` / `Low` |
| **Shareholder Payout Ratios** | `Earnings Shareholder Payout Ratio` · `FCF Shareholder Payout Ratio` |

#### Table « Historique des dividendes »
Table **transposée** (colonnes = trimestres, ex. Q412 → Q226, ~55 colonnes ; lignes = champs), sélecteur de devise.
Lignes : `Date` (ex-date) · `Régularisation` (record date) · `Paiement` · `Déclaration` · `Dividende` · `Dividende ajusté` · `Yield` · `Fréquence`.

---

### 7.4 Onglet Résultats

| Bloc | Contenu |
|---|---|
| **Objectifs de prix** | cours historique + projection des 3 objectifs analystes · chips `High: 138,48%` / `Average: 54,76%` / `Low: 26,4%` (en % d'upside) |
| **Prévisions du CA** | historique + 3 scénarios (haut/moyen/bas) sur ~5 ans · `CAGR High / Avg / Low` |
| **Prévisions des EPS** | idem |
| **Surprises des EPS** | barres estimé vs réel par trimestre · chips `Beat: 100%` / `Miss: 0%` |
| **Notes des analystes** | donut : `Acheter` · `Renforcer` · `Conserver` · `Alléger` (+ Vendre) |
| **Détail des notations** | barres /5 : `Score Global` · `Score de DCF` · `Score de ROE` · `Score de ROA` · `Score de Dette` · `Score de P/E` · `Score de P/B` |
| **Transcripts** | liens vers les transcripts de conf. call (`/transcript/:id`) |
| **Prochains résultats** | date + `EPS estimé` |
| **Historique des publications** | par trimestre : `SURPRISE EPS` · `CROISSANCE EPS` · `SURPRISE CA` · `CROISSANCE CA` |
| **Table « Historique des résultats »** | transposée, **~110 trimestres depuis 1999**. Lignes : `Date` · `EPS Réels` · `EPS Estimés` · `Surprise EPS (%)` · `Croissance EPS (YoY)` · `CA Réel` · `CA Estimé` · `Surprise CA (%)` · `Croissance CA (YoY)` · `Date de la dernière mise à jour` |

---

### 7.5 Onglet Finances

**Le module d'états financiers le plus abouti de Baggr.**

#### Barre d'outils (2 niveaux)

**Niveau 1**
```
[Standardisées | Publiées]   [Annuel | Semestriel | Trimestriel]   [K | M | Md]
[− 0.00 +]  ☑ TTM  ☑ Prévisions  ☑ %Chang.  ☐ Inverser  (?)          ⚙ Options (?)
```
- **Standardisées / Publiées** : plan comptable normalisé vs. tel que publié par l'entreprise. ⚑ Différenciant fort.
- **K / M / Md** : unité d'affichage · **`− 0.00 +`** : nombre de décimales.
- **%Chang.** : affiche la variation YoY sous chaque valeur (colorée).
- **Inverser** : inverse l'ordre chronologique des colonnes.

**Niveau 2 — sous-onglets**
```
Comptes de résultat | Bilans | Flux de trésorerie | Métriques & Ratios | Segments & KPIs | Ajustées | Personnalisées
                                                            Devise: USD   Type: Standard
```

**Table** : 11 exercices (FY16 → FY26) + **TTM**, une **checkbox par ligne** (pour tracer la ligne en graphique), lignes parentes **dépliables**.

#### 📄 Comptes de résultat — lignes
```
Chiffre d'affaires
Coût des marchandises vendues
Résultat brut
Total des charges d'exploitation ▸
    Frais de vente, généraux et administratifs
    Dépenses de R&D
    Amortissements et dépréciations
    Autres charges d'exploitation
Total des coûts et charges
Résultat d'exploitation
Total des produits et charges non opérationnels ▸
    Produits d'intérêts nets
    Intérêts perçus et revenus des placements
    Charges d'intérêts
    Autres produits non opérationnels
Résultat avant impôt
Charge d'impôt
Résultat net ▸
    Résultat des activités poursuivies
Résultat net part du groupe ▸
    Autres ajustements au résultat net
EPS de base
EPS dilué
Moyenne pondérée des actions en circulation
Moyenne pondérée diluée des actions en circulation
EBITDA
```

#### 📄 Bilans — lignes
```
ACTIFS
Total de l'actif circulant ▸
    Total des liquidités et VMP
    Total des créances
    Stocks
    Charges payées d'avance
    Autres actifs à court terme
Total des actifs à long terme ▸
    Immobilisations corporelles nettes
    Immobilisations incorporelles
    Goodwill
    Investissements à long terme
    Actifs d'impôts différés
    Autres actifs à long terme
Total des actifs

PASSIFS
Total du passif circulant ▸
    Total des dettes
    Charges à payer
    Dette à court terme
    Dettes locatives courantes
    Impôts à payer
    Produits constatés d'avance
    Autres passifs à court terme
Total du passif non courant ▸
    Dette à long terme
    Dettes locatives non courantes
    Dettes locatives
    Produits constatés d'avance (non courants)
    Passifs d'impôts différés (non courants)
    Autres passifs non courants
Total du passif

CAPITAUX PROPRES
Total des fonds propres ordinaires ▸
    Actions ordinaires
    Primes d'émission
    Bénéfices non distribués
    Cumul des autres éléments du résultat global
    Actions propres
    Autres capitaux propres
Total des capitaux propres
Total du passif et des capitaux propres
```

#### 📄 Flux de trésorerie — lignes
```
ACTIVITÉS OPÉRATIONNELLES
Résultat net
Amortissements et dépréciations
Rémunération à base d'actions
Impôts différés
Autres ajustements
Variation du BFR ▸
    Variation des créances clients
    Variation des stocks
    Variation des dettes fournisseurs
    Variation des autres éléments d'exploitation
Flux de trésorerie d'exploitation

ACTIVITÉS D'INVESTISSEMENT
Dépenses d'investissement (CAPEX)
Produits de cession d'immobilisations corporelles
Achats de placements
Cessions/échéances de placements
Acquisitions d'entreprises
Autres activités d'investissement
Flux de trésorerie d'investissement

ACTIVITÉS DE FINANCEMENT
Émission nette de dette ▸
    Émission nette de dette à long terme ▸
        Émission de dette à long terme
        Remboursement de dette à long terme
Émission nette d'actions ▸
    Émission nette d'actions ordinaires ▸
        Émission d'actions ordinaires
        Rachat d'actions ordinaires
Total des dividendes versés ▸
    Dividendes ordinaires versés
Autres activités de financement
Flux de trésorerie de financement

FLUX DE TRÉSORERIE DISPONIBLE
Flux de trésorerie disponible ▸
    Flux de trésorerie d'exploitation
    Dépenses d'investissement (CAPEX)
NOPAT
Flux de trésorerie disponible avec levier
Flux de trésorerie disponible sans levier
Variation nette de la trésorerie
Trésorerie au début de la période
Trésorerie à la fin de la période
Impôts payés
Intérêts payés
```

#### 📄 Métriques & Ratios — **131 ratios historisés sur 11 ans + TTM**
Voir [§17 — Dictionnaire exhaustif](#17--dictionnaire-exhaustif-des-métriques). C'est **le morceau le plus impressionnant du produit**.

#### 📄 Segments & KPIs
Décomposition **historisée** avec plages d'années explicites (les segments changent au fil des reclassements) :
```
Revenue (2014-2026)
    Data Center Revenue ▸ Hyperscale Revenue / AI Clouds, Industrial & Enterprise Revenue / Edge Computing Revenue
    Gaming Revenue · Professional Visualization Revenue · Automotive Revenue · OEM & Other Revenue
Revenue (2019-2026)
    Compute & Networking Revenue · Graphics Revenue
EBIT (2019-2026)
    Compute & Networking Operating Income · Graphics Operating Income
Revenue by Geography (2024-2026)
    United States / Taiwan / China / Other Countries
Revenue by Geography (2013-2025)
    ... + Total Other Countries ▸ Singapore / Other
Key Performance Indicators (2019-2026)
    Remaining Performance Obligations
    Remaining Performance Obligations to be Recognized Over NTM
```
⚑ **Très rare sur le marché** : segments réels, sourcés dans les filings, avec gestion des changements de nomenclature.

#### 📄 Ajustées
```
Adjusted Revenue · Adjusted Gross Profit · Adjusted Gross Margin
Adjusted EBIT · Adjusted EBIT Margin · Adjusted EBITDA · Adjusted EBITDA Margin
Adjusted Net Income · Adjusted Net Income Margin · Adjusted EPS
Adjusted Free Cash Flow (FCF) · Adjusted Free Cash Flow Margin · Adjusted Capital Expenditures
```

#### 📄 Personnalisées — **Constructeur de métriques**
Modal « **Ajouter une métrique personnalisée** » :
- Toggle **« Appliquer à toutes les sociétés »**
  > « Activé automatiquement tant que la formule n'utilise que des données présentes chez toutes les sociétés. Une opérande propre à une société (segments, données ajustées) restreint la métrique à celle-ci. »
- **Format** : `Nombre` · `Ratio` · `Pourcentage`
- **Nom de la métrique**
- **Constructeur de formule** : recherche de métriques + blocs opérateurs `[1] [+#] [+] [−] [*] [/] [(] [)]`
- Zone de composition + `Ajouter la métrique` / `Effacer`

⚑ **Killer feature.** Un utilisateur peut créer `SBC / Revenue`, `(FCF − Dividendes) / Dette nette`, etc., et l'appliquer à tout l'univers.

---

### 7.6 Onglet Thèses
Liste des thèses d'investissement publiées par la communauté sur ce titre : ticker, société, titre, auteur (@pseudo + avatar), date, **performance du titre depuis la publication** (colorée), vues, commentaires. CTA `✏️ Écrire une thèse`.

---

### 7.7 Onglet Société

**Sous-onglets : `Informations` · `Actualités` · `Documents` · `Concurrents`**

#### Informations
**a) Carte « Business Model »** avec sélecteur **« Type d'analyse »** — 9 analyses IA (Gemini) :
```
Business Model · Histoire · Directeurs · Capital & Gouvernance
Concurrents · Marché · Analyse SWOT · Avantage Compétitif · Réputation
```
État `✨ ANALYSE EN PRÉPARATION` quand non encore générée.

**b) « Répartition du capital »** — donut des principaux détenteurs (BlackRock, Vanguard, State Street, FMR, dirigeants…, + « Autre »).

**c) « Principaux actionnaires »** — bascule `Actionnaires | Managers`, avec % et valeur.

**d) « Transactions d'insiders par année »** — histogramme achats/ventes.

**e) « Actionnariat »** — badge **`★ Détention initiés : 4,24%`**, bascule `Initiés | Actionnaires | Transactions`.
Colonnes : `Initié | Fonction | Date | Actions | % détenu | Valeur`.

#### Actualités
Flux d'articles : vignette, titre, ticker, `Par : Source` (lien), date+heure.

#### Documents
Sélecteur **Type** : `Annuel (10-K)` / `Trimestriel (10-Q)`. Liste chronologique des rapports avec date de dépôt.

#### Concurrents
**Mini-screener** intégré (même DataGrid) avec boutons `Colonnes`, `Filtres`, `Rechercher` — liste des pairs classée par capitalisation, avec Note Q.

---

### 7.8 Onglet Valorisation

**Le module de valorisation — deuxième pièce maîtresse.**

#### a) « Calculatrice de prix juste »
En-tête : `PRIX JUSTE 346,42 $US` · `RENDEMENT ESTIMÉ 22,21%/an` · `MARGE DE SÉCURITÉ 35,38%`
Bascule **`📈 Graphique` / `▦ Matrice`**.

**Vue Graphique** : historique EPS (ou FCF/OCF/Ventes) + dividende + cours, puis projection à ~5-6 ans du prix cible. Légende : `EPS (CAGR: 63,04%)` · `Dividende/Actions (CAGR: 16,68%)` · `Prix (CAGR: 58,41%)`. Mention `données fournies par baggr`.

**Vue Matrice** : **matrice de sensibilité** `Croissance (lignes) × Multiple final (colonnes)` → **rendement annualisé** attendu, avec heatmap couleur et surlignage de la cellule correspondant aux paramètres courants. ~15 × 15 cellules.

**Panneau « Paramètres »** :
| Champ | Valeurs / Références affichées |
|---|---|
| **Données utilisées** | `Bénéfices/Actions` · `FCF/Actions` · `OCF/Actions` · `Ventes/Actions` |
| **Période historique** | `10 ans` (et autres) |
| **Croissance estimée (%) / an** | champ libre + références : `Prévisions : 32,33%` · `Communauté : 20,70%` |
| **Multiple final estimé** | champ libre + références : `médiane 10A : 56,57` · `Communauté : 29,81` |
| **Rendement cible (%) / an** | champ libre (défaut 12) |
| **☑ Inclure les dividendes** | |
| Boutons | `💾 Enregistrer` · `⤓ Télécharger` · `🗑 Réinitialiser` |

⚑ Le fait d'afficher **côte à côte l'estimation analystes ET la moyenne communautaire** comme ancrages est très malin.

#### b) Carte « Métriques clés »
`P/E` · `Forward P/E` · `PEG RATIO` · `P/FCF` · `PFG RATIO` · `P/OCF` · `P/S` · `P/B` · `DIVIDEND YIELD`
→ chaque valeur avec sa **`médiane 10A`** en regard, et coloration selon l'écart.

#### c) Carte « Communauté »
`NB. D'ÉVALUATIONS : 502` · `PRIX DE LA COMMUNAUTÉ : 346,57 $US`
Puis 4 sous-cartes (`EPS` / `FCF` / `OCF` / `SPS`), chacune avec le nombre d'évaluations et la **moyenne communautaire** de :
`CROISSANCE` · `MULTIPLE` · `PRIX`

⚑ **Le meilleur mécanisme de rétention de Baggr** : chaque utilisateur soumet ses hypothèses, tout le monde voit la moyenne. Effet réseau + contenu propriétaire gratuit.

#### d) Autres cartes
| Carte | Contenu |
|---|---|
| **Bénéfices / Action** | barres FY historiques + prévisions hachurées · `Perf` / `CAGR` |
| **Fair Value Baggr** | cours + **bandes de valorisation dégradées** (rouge = surévalué / vert = sous-évalué) autour d'une fair value. Chips `Prix` · `Fair Value` · `Marge de sécurité : +28 %` |
| **Régression linéaire** | canal de régression log du cours + projection. Chips `Prix` · `Régression: 284,23 $US` · **`Pente: 83.3%/an`** |
| **Forward P/E projeté** | barres : `Médiane` · `Méd. 5A` · `Futur TTM` · `FY27 … FY31`. Chips `P/E TTM` · `Médiane 5A` |
| **Forward P/E Ratio** | courbe historique + médiane · `High` / `Médiane` / `Low` |
| **P/E Ratio** | idem |
| **PEG Ratio** | idem |
| **EPS Yield** | idem |

---

## 8. Screener

**URL** : `/screener` — 4 onglets : `Catégories` · `Filtres` · `Colonnes` · `Mes vues`

### 8.1 Catégories (presets)
Cartes cliquables avec emoji, nom, **compteur d'actions** et description :

| Preset | Volume | Définition affichée |
|---|---|---|
| 🌍 **Tout** | 51K+ | Toutes les actions disponibles dans le monde, la plus large sélection disponible. |
| 🇫🇷 **France** | 621 | Actions françaises, dont le siège social se situe en France. |
| 🇪🇺 **PEA** | 4 673 | Actions éligibles au PEA, dont le siège social se situe dans l'Union Européenne. |
| ✅ **Quality** | 847 | ROE ≥ 20 %, croissance des EPS sur 10 ans ≥ 10 % et dette/EBITDA ≤ 2.5. |
| 📈 **Growth** | 2 813 | Croissance du CA sur 5 ans ≥ 10 % et croissance future CA ≥ 10 %. |
| 💰 **Value** | 9 558 | P/E ≤ 15 et P/Book ≤ 1,5. |
| 🏛️ **Dividend Aristocrats** | 160 | Dividende augmenté annuellement pendant plus de 25 années consécutives. |
| 🔬 **Small Caps** | 40K+ | Capitalisation < 1 milliard de dollars. |

### 8.2 Filtres — 7 catégories, ~90 critères

Chaque critère = **`Min` … `à` … `Max`** (ou un select), avec un bouton **✕** de réinitialisation individuelle.

#### 📊 Informations
| Groupe | Critères |
|---|---|
| **Identification** | `Nom` (texte) · `Ticker` (texte) · `ISIN` (texte) · `Note Q` (min/max) |
| **Localisation** | `Pays` (select) · `Bourse` (select) · `Devise` (select) |
| **Classification** | `Secteur` · `Industrie` · `Sous-Industrie` (selects liés) |
| **Marché** | `Capitalisation (M$)` (min/max) · `Verse un dividende` · `Éligible au PEA` · `Cotation active` |

#### 💵 Retours sur capitaux
```
ROE (Retour sur Capitaux Propres)         : ROE TTM · ROE 5 ans · ROE 10 ans
ROCE (Retour sur Capital Employé)         : ROCE TTM · ROCE 5 ans · ROCE 10 ans
ROIC (Retour sur Capital Investi)         : ROIC TTM · ROIC 5 ans · ROIC 10 ans
ROIIC (Retour sur Capital Investi Incrémental) : ROIIC TTM · ROIIC 5 ans · ROIIC 10 ans
```

#### 💰 Rentabilité
```
Marges     : Marge brute · Marge opé. · Marge nette · Marge FCF
Efficacité : %CAPEX/Revenus · %CAPEX/OCF
WACC       : WACC · WACC 5 ans · WACC 10 ans
```

#### 📈 Croissance
```
Croissance des revenus : Revenus TTM · Revenus 5 ans · Revenus 10 ans · Prédictibilité · Prévision 3-5 ans
Croissance des EPS     : EPS TTM · EPS 5 ans · EPS 10 ans · Prévision 3-5 ans (EPS)
Croissance des FCF     : FCF TTM · FCF 5 ans · FCF 10 ans
Croissance des OCF     : OCF TTM · OCF 5 ans · OCF 10 ans
```

#### ❤️ Santé financière
```
Bilan                  : Dette nette/EBITDA · Intérests Coverage · %Goodwill/Assets
Actions en circulation : Dilution 3 ans
```
> ⚠️ Très pauvre par rapport au reste (4 critères seulement). **Faille exploitable majeure.**

#### 💸 Dividende
```
Croissance du dividende : Dividende TTM · Dividende 5 ans · Dividende 10 ans
Dividend Yield          : Dividend Yield TTM · Dividend Yield 10 ans
Santé du dividende      : Années d'augmentation · Payout ratio · Payout ratio 10 ans
```

#### 💎 Valorisation
```
Bénéfices                        : P/E Ratio · P/E Ratio 10 ans · PEG Ratio · EPS Yield · EPS Yield 10 ans
Flux de trésorerie disponible    : P/FCF Ratio · P/FCF Ratio 10 ans · PFG Ratio · FCF Yield · FCF Yield 10 ans
Flux de trésorerie d'exploitation: P/OCF Ratio · P/OCF Ratio 10 ans · POG Ratio · OCF Yield · OCF Yield 10 ans
Ventes                           : P/Sales Ratio · P/Sales Ratio 10 ans · PSG Ratio · Sales Yield · Sales Yield 10 ans
Valeur comptable                 : P/Book Ratio · P/Book Ratio 10 ans
Valeur d'entreprise              : EV/EBIT Ratio · EV/EBIT Ratio 10 ans · EBIT/EV Yield (Greenblatt) · EBIT/EV Yield 10 ans (Greenblatt)
```
> ⚑ Le **« ratio 10 ans »** systématique (médiane décennale) est le pattern signature de Baggr : on peut filtrer sur *« P/E actuel < médiane 10 ans »*. `PFG`, `POG`, `PSG` = équivalents du PEG appliqués au FCF, OCF et Sales.

### 8.3 Colonnes
Même arborescence à 7 catégories, mais en **toggles** (switch on/off) au lieu de champs min/max.
Un **8ᵉ groupe « Prix »** existe (visible via le gestionnaire de colonnes de la watchlist) :
```
PERFORMANCE & PRIX : Prix · Variation jour. · Moyenne 200 jours · Différence Prix/Moyenne 200 jours
                     Moyenne 90 jours · Différence Prix/Moyenne 90 jours
                     Moyenne 30 jours · Différence Prix/Moyenne 30 jours
                     Moyenne 7 jours  · Différence Prix/Moyenne 7 jours
```
Et le groupe **Valorisation** ajoute un bloc **`ESTIMATION BAGGR`** :
```
Prix juste · Marge de sécurité · Rendement estimé · Prix communauté · Estimation manuelle
```
\+ les écarts au multiple médian : `Dif. PE` · `Dif. FCF` · `Dif. POCF` · `Dif. PS` · `Dif. PB` · `Dif. EV`.

Le gestionnaire de colonnes (drawer) propose une **recherche « Rechercher une métrique… »** + colonne de catégories à gauche.

### 8.4 Mes vues
> « Mes vues sauvegardées — Gérez vos configurations de screener sauvegardées. »
État vide : « Aucune vue sauvegardée · Sauvegardez vos configurations de screener pour les retrouver facilement ».

### 8.5 La grille
- Colonnes par défaut : `Nom` (logo + société + *nb abonnés*) · `Note Q` (badge coloré) · `Pays` · `Secteur` · `Capitalisation ($)` · `Verse un dividende` (✅/❌) · `Éligible au PEA` (✅/❌) · `Actions` (★ ajouter à la watchlist).
- **Pagination : 50 lignes / page**, `1–50 sur 54 198`.
- Tri sur colonne, redimensionnement, sticky de la colonne Nom.
- **Ouverture de la fiche dans un nouvel onglet** au clic sur la ligne.

---

## 9. Comparateur

**URL** : `/freeform` — badge **`Version Bêta`**
> « Créez vos propres graphiques en comparant jusqu'à **10 métriques** différentes et **5 actions** différentes. »

Fonctionnement : on ajoute 1 à 5 sociétés (chips supprimables + bouton `+ Ajouter`), et l'écran affiche **la même table Finances** (Comptes de résultat / Bilans / Flux de trésorerie / Métriques & Ratios / Personnalisées) avec exactement la même barre d'outils (Standardisées/Publiées, Annuel/Semestriel/Trimestriel, K/M/Md, décimales, TTM, Prévisions, %Chang., Inverser, Devise, Type).
Les **checkboxes de lignes** servent à sélectionner jusqu'à 10 métriques à tracer.

> ⚠️ Faiblesse : c'est une simple réutilisation de la table Finances, sans vue de comparaison dédiée (pas de radar comparé, pas de scatter, pas de table de peer-ranking avec percentiles).

---

## 10. Watchlists

**URL** : `/watchlists`
En-tête : sélecteur `Watchlist ▾`, `🔗 Partager`, `⚙ Paramètres`, sous-titre « N watchlist paramétrée ».
Onglets : **`Résumé` · `Tableau` · `Thèses` · `Actualités` · `Résultats` · `Documents`**

### Résumé
Split view :
- **Gauche** : liste des titres, avec sélecteur **« Trier par »** :
  `Nom · Note Q · CAGR Total · Marge de sécurité · Variation jour % · P/E Ratio TTM · PEG Ratio · P/FCF Ratio · PFG Ratio · Dividend Yield · EPS Yield · FCF Yield · Prix de la communauté`
  Chaque ligne : logo, nom, ticker, prix, **valeur du critère de tri** (colorée).
- **Droite** : panneau détail du titre sélectionné → `PRIX JUSTE` / `RENDEMENT ESTIMÉ` / `MARGE DE SÉCURITÉ`, graphique de prix avec **ligne de PRU**, chips `Perf` / `CAGR` / `Linéarité`, puis cartes `Informations`, `Prix`, `Valorisation`.

### Tableau
DataGrid complet avec `Colonnes` · `Filtres` · `💾 Enregistrer` · `↺ Réinitialiser` · `🔍 Rechercher`.
Colonnes par défaut : `Nom` · `Rendement estimé` · `Marge de sécurité` · `Prix juste` · `Prix communauté` · `Variation jour.` · `Prix` · `Note Q` · `Actions`.

### Autres onglets
`Thèses` / `Actualités` / `Résultats` / `Documents` = flux agrégés **restreints aux titres de la watchlist**. ⚑ Très bonne idée, peu coûteuse, forte valeur perçue.

---

## 11. Portefeuille

**URL** : `/portfolio`
En-tête : `Devise : EUR`, sélecteur `Compte : Tous ▾`, `🔗 Partager`, `⚙ Paramètres`.
Onglets : **`Résumé` · `Positions` · `Titres` · `Risque` · `Performances` · `Dividendes` · `Flux` · `Impôts`**

### Résumé
- **3 KPI cards** :
  - `Valeur totale` (+ « sur X € investis ») avec badge `Total Return %`
  - `Total Return` (€) avec badge **`TRI (rendement annualisé)`**
  - `Dividendes` (€ perçus) avec badge `% de rendement`
- **Performance (Time Weighted Return)** : périodes `YTD · 1A · 3A · 5A · MAX`, courbe du portefeuille **vs benchmark S&P 500** avec `Perf` et `CAGR` pour chacun.
- **Répartition des comptes** (donut)
- **Top 5 / Flop 5** positions
- **Carte par compte** : badge de type (`SIMPLE`), nom, type d'enveloppe (`CTO`), courtier (logo Trade Republic), « Depuis le … », + les mêmes KPI.

### Autres onglets
| Onglet | Contenu attendu |
|---|---|
| **Positions** | positions ouvertes / historiques, PRU, +/− value |
| **Titres** | vue agrégée par titre (multi-comptes) |
| **Risque** | **Sharpe, Sortino, volatilité, max drawdown, frontière efficiente de Markowitz, matrice de corrélation** |
| **Performances** | TWR détaillé, contributions |
| **Dividendes** | historique + calendrier + projection |
| **Flux** | dépôts/retraits, cash-flows |
| **Impôts** | fiscalité (PEA/CTO, prélèvements) |

### Imports courtiers
**Interactive Brokers · DEGIRO · Trade Republic · Saxo Banque** (imports automatiques annoncés).
Composants repérés : `PortfolioPerformanceWizard`, `PortfolioInvestments`, `RenderTransactionType`, `benchmarks`, `useFetchExchangeRate`, `useFetchExchangeRateByDate` (→ conversion de devise historisée).

---

## 12. Calendriers

4 calendriers : **Résultats · Dividendes · Splits · IPOs**

### Calendrier des résultats (`/calendars/results`)
> « Publications de résultats des entreprises à venir. »

**Filtres** :
- `Afficher` : `Toutes les actions` · `Mon portefeuille` · `Toutes les watchlists` · `<nom de watchlist>`
- `Pays` : `Tous les pays` · …
- `Tri` : `Abonnés` · `Vues` · `Capitalisation`

**Vue** : semaine `lundi → vendredi` en 5 colonnes, jour courant surligné. Chaque cellule = **grille de logos** (4 par ligne) avec le ticker sous le logo. Navigation `‹ Aujourd'hui ›`.

⚑ Le tri par **Abonnés / Vues** est malin : la pertinence est déterminée par l'intérêt réel de la communauté, pas seulement par la capitalisation.

---

## 13. Idées

### 13.1 Thèses (`/theses`)
> « Analyses d'investissement partagées par la communauté et éligibles au **concours Baggr**. »
Onglets : **`Toutes` · `Gagnants` · `Top 50` · `Flop 50`**
Bandeau : *« 🏅 Participez au concours de la meilleure thèse et tentez de gagner **200 € chaque mois**. »* + `En savoir plus`
Recherche + CTA `✏️ Écrire une thèse`.

Chaque item : ticker, société, **titre**, avatar + `@pseudo`, date, **perf du titre depuis publication**, 👁 vues, 💬 commentaires.
Détail : `/thesis/:id` avec commentaires (`ThesisComments`).

⚑ Le classement **Top 50 / Flop 50 par performance réelle depuis publication** crée une réputation mesurable → gamification puissante.

### 13.2 Super-Investisseurs (`/super-investors`)
> « Découvrez les investisseurs institutionnels les plus influents et les plus performants. »
Onglets : `Super Investisseurs` · `Positions`

**Super Investisseurs** : grille de cartes — photo, nom, fonds, badge du trimestre (`Q2 26`), `N titres`, `+N` achats (vert), `−N` ventes (rouge).
Investisseurs identifiés : Andrew R. Adams (Mairs & Power), Bill Ackman (Pershing Square), Bill Gates (Gates Foundation), Carl Icahn, Cathie Wood (ARK), Chase Coleman (Tiger Global), Chris Hohn (TCI), Chuck Akre, David Rolfe (Wedgewood), David Tepper (Appaloosa), Dev Kantesaria (Valley Forge), Donald Yacktman, François Rochon (Giverny), Greg Abel (Berkshire), Guy Spier (Aquamarine), Howard Marks (Oaktree), Li Lu, Mark Massey, Michael Burry (Scion), Mohnish Pabrai, Ray Dalio (Bridgewater), Stan Moss (Polen)…

**Positions** : *« Top 10 des actions les plus détenues »* avec 3 modes de classement : **`Détenteurs` · `Valeur` · `Conviction`**.
Colonnes : rang, société, ticker, `N détenteurs`, `% moyen`, `valeur totale`.

Détail investisseur : `/super-investors/:id` (+ `useFetchSuperInvestorsPerformance` → performance du fonds).

### 13.3 Sélections Baggr (`/listes`)
> « Des sélections d'actions thématiques préparées par l'équipe Baggr. »
Cartes avec **image de couverture**, emoji, titre, description, `N actions`, `👁 N vues`, bouton `Partager`.

**Les 19 sélections observées** :
| Sélection | Actions | Vues |
|---|---|---|
| 🏰 Les remparts contre l'IA | 34 | 1432 |
| 🍴 Cannibal Companies | 12 | 1248 |
| ⚖️ Les duopoles de marché | 10 | 1009 |
| 👑 Dividend Kings | 53 | 902 |
| ☄️ SaaSpocalypse | 14 | 834 |
| 🔁 Les rois de l'abonnement | 18 | 717 |
| 💻 Semi Conducteurs | 22 | 598 |
| 🥤 Consumer Behavior | 20 | 556 |
| 🛘 Des terres trop rares | 13 | 357 |
| ☢️ Uranium & Nucléaire | 13 | 331 |
| 🧠 La chaîne de valeur de l'IA | 22 | 294 |
| 🛡️ Cybersécurité | 13 | 275 |
| 💊 Big Pharma | 9 | 269 |
| 🍸 Sin Stocks | 22 | 250 |
| 💧 L'hydrogène | 10 | 249 |
| 🧛 Vampire Immobilier | 24 | 247 |
| 🪖 War Economy | 14 | 235 |
| 🛢️ Drill Baby Drill | 24 | 228 |
| 🚀 Conquête spatiale | 16 | 225 |

⚑ Excellent levier SEO/acquisition **et** de découverte. Coût de production faible, valeur perçue élevée. Le compteur de vues public crée une preuve sociale.

---

## 14. Communauté

Menu : **`Membres` · `Portefeuilles` · `Watchlists` · `Les plus suivies` · `Discord`**

- **Membres** (`/members`) : annuaire des utilisateurs, profils publics `/profile/:slug` avec liens sociaux (`ProfileSocialLinks`, `socialPlatforms`).
- **Portefeuilles publics** (`/public-portfolios`) : portefeuilles partagés par les membres.
- **Watchlists publiques** (`/public-watchlists`).
- **Les plus suivies** (`/most-followed`) : actions/membres les plus suivis.
- **Discord** : passerelle vers le serveur communautaire.

\+ `/articles` (articles), `/academy` + `/academy/:formationId` (formations), `/sharing` (ressources partagées).

---

## 15. Ressources

Menu : **`Indices` · `Macro` · `Calculatrice` · `Marchés` · `Classifications`**

### Indices (`/indexes`)
> « Prix des principaux indices boursiers et macro économiques. »
Grille de cartes, chacune avec drapeau/emoji, dernier cours, variation %, courbe 2 ans, et chips **`Perf` / `CAGR` / `Linéarité`**.

Périmètre (21 instruments) :
`S&P 500 · NASDAQ 100 · Dow Jones · Russell 2000 · VIX · CAC 40 · DAX · FTSE 100 · Nikkei 225 · SSE Composite · Or (XAUUSD) · Argent (XAGUSD) · Bitcoin (BTCUSD) · Pétrole (CLUSD) · Blé (KEUSX) · USD/EUR · USD/GBP · USD/JPY · USD/CNY · USD/CHF · USD/SEK`

### Macro (`/macro`)
> « Taux souverains et indicateurs macroéconomiques américains. »
Grille de graphiques (les 11 indicateurs du dashboard + courbe des taux). **Chargement très lent** observé.

### Calculatrice (`/compound-interest`)
Calculatrice d'intérêts composés.

### Marchés (`/exchanges`)
Horaires d'ouverture des places par zone géographique + statut Ouvert/Fermé.

### Classifications (`/classifications`)
> « Tables de référence des pays, secteurs, industries et sous-industries avec le nombre d'actions primaires associées. »
Onglets : **`Pays` · `Secteurs` · `Industries` · `Sous-industries`**
Colonnes : `Nom` · `Code` · `Nb. d'actions` (avec barre de progression) · `% du total`.

**Les 11 secteurs** :
| Secteur | Nb | % |
|---|---|---|
| Industrie | 9 391 | 17,33 % |
| Finance | 8 072 | 14,89 % |
| Technologie | 7 356 | 13,57 % |
| Matériaux | 6 458 | 11,92 % |
| Consommation discrétionnaire | 6 337 | 11,69 % |
| Santé | 5 188 | 9,57 % |
| Consommation courante | 3 351 | 6,18 % |
| Immobilier | 3 255 | 6,01 % |
| Communication | 2 295 | 4,23 % |
| Énergie | 1 451 | 2,68 % |
| Utilitaires | 1 044 | 1,93 % |

---

## 16. Patterns UI/UX transverses

À reprendre (ou à dépasser) dans Capital Antifragile :

| Pattern | Description |
|---|---|
| **Carte à en-tête + `(?)` + ⤢** | Chaque bloc a un titre, une icône d'aide (tooltip explicatif) et un bouton **plein écran**. |
| **Chips de synthèse en footer de graphique** | `Perf : X%` · `CAGR : Y%` · `Linéarité : 0,95` — reprend systématiquement 2-3 KPI sous chaque courbe. |
| **Coloration sémantique** | Vert / orange / rouge sur **toutes** les valeurs numériques, selon des seuils par métrique. |
| **Badge Note Q** | `19,5/20` avec fond coloré (vert foncé → rouge) présent partout : recherche, screener, watchlist, fiche, concurrents. |
| **Référence « médiane 10A »** | Chaque multiple est affiché avec sa médiane décennale à côté. **Signature de Baggr.** |
| **Doubles ancrages « Prévisions » + « Communauté »** | Dans la calculatrice de valorisation, les deux références sont cliquables comme valeurs par défaut. |
| **Tables transposées** | Dividendes et résultats : colonnes = périodes, lignes = champs (scroll horizontal). |
| **Lignes dépliables ▸** | Dans les états financiers, les agrégats se déplient en sous-postes. |
| **Checkbox par ligne** | Pour « envoyer » la ligne vers un graphique. |
| **Drawer de personnalisation** | Colonnes et widgets gérés dans un panneau latéral avec recherche + catégories. |
| **Empty states soignés** | Icône + titre + phrase d'accompagnement + CTA. |
| **Signal social omniprésent** | « N abonnés », « N vues », « N évaluations » sur chaque objet. |
| **Skeletons** | Chargement progressif (mais parfois bloqué — cf. faiblesses). |
| **Emoji comme système d'icônes** | Catégories, sélections, indices, filtres : emoji devant chaque libellé. |

---

## 17. ★ Dictionnaire exhaustif des métriques

### 17.1 Onglet Finances → « Métriques & Ratios » (131 lignes, historisées 11 ans + TTM)

```
━━ MARCHÉ ━━
Prix
Capitalisation
Enterprise Value (EV)

━━ VALORISATION ━━
P/E Ratio
PEG Ratio
EPS Yield
P/FCF Ratio
PFG Ratio
FCF Yield
P/OCF Ratio
POG Ratio
OCF Yield
P/S Ratio
PSG Ratio
Sales Yield
P/B Ratio
EV / EBIT
EBIT/EV Yield (Greenblatt)
EV / Sales
EV / EBITDA
EV / OCF
EV / FCF

━━ VALORISATION ESTIMÉE ━━
Forward P/E
Forward EPS Yield
Forward P/S Ratio
Forward Sales Yield

━━ DIVIDENDE ━━
Dividend / Share
Dividend Payout Ratio
Dividend Yield
Buyback Yield
Debt Paydown Yield
Shareholder Yield

━━ RENTABILITÉ & MARGES ━━
Marge brute
Marge EBIT
Marge EBITDA
Marge opérationnelle
Marge avant impôts
Marge des opérations continues
Marge nette
Marge finale
Marge FCF
Marge FCF-SBC

━━ COÛT DU CAPITAL ━━
WACC (Coût moyen pondéré du capital)

━━ RETOURS SUR CAPITAUX ━━
Working Capital (Fonds de roulement)
Invested Capital (Capital investi)
ROA (Rendement des actifs)
OROA (Rendement des actifs opérationnels)
ROTA (Rendement des actifs tangibles)
ROE (Rendement des capitaux propres)
ROIC (Rendement du capital investi)
ROCE (Rendement du capital employé)
ROIIC
FCF ROC

━━ BILAN & DETTE ━━
Current Ratio
Quick Ratio
Ratio de solvabilité
Ratio de trésorerie
Dette nette / EBITDA
Dette / Actifs
Dette / Capitaux propres
Dette / Capital
Dette LT / Capital
Levier financier
Dette / Market Cap
Debt Service Coverage
Interest Coverage
ST OCF Coverage
OCF Coverage
CAPEX Coverage
Dividend + CAPEX Coverage

━━ EFFICACITÉ OPÉRATIONNELLE ━━
CAPEX / OCF
CAPEX / Depreciation
CAPEX / Revenue
SG&A / Revenue
R&D / Revenue
SBC / Revenue
Intangible Assets / Total Assets
Days of Sales Outstanding
Days of Payables Outstanding
Days of Inventory Outstanding
Operating Cycle
Cash Conversion Cycle
Average Receivables
Average Payables
Average Inventory
Rotation des créances
Rotation des dettes
Rotation des stocks
Rotation des actifs fixes
Rotation des actifs
Rotation du fonds de roulement
OCF Ratio
OCF / Sales
FCF / OCF
R&D / OCF
SBC / FCF
Marketing / OCF
Goodwill / Actifs

━━ MÉTRIQUES PAR ACTION ━━
Revenue / Share
EPS (Earnings / Share)
Interest Debt / Share
Cash / Share
Book Value / Share
Tangible Book Value / Share
Shareholders Equity / Share
OCF / Share
CAPEX / Share
FCF / Share

━━ AUTRES ━━
Income Quality (Qualité du résultat)
Tax Burden (Charge fiscale)
Interest Burden (Charge d'intérêts)
Graham Number
Graham Net-Net
Tangible Asset Value (Valeur des actifs tangibles)
Net Current Asset Value (Valeur nette des actifs courants)
FCF / Equity
FCF / Firm
Net Income / EBT
EBT / EBIT
Effective Tax Rate
```

### 17.2 Métriques présentes ailleurs mais **absentes** de la table Ratios
(À intégrer côté Capital Antifragile pour couvrir 100 % du périmètre Baggr.)
```
Note Q (note quantitative /20)
Profil quantitatif : 6 scores 0-4 (Retours sur capitaux, Marges, Croissance, Rentabilité, Dividende, Santé)
Altman Z-Score
Piotroski Score
Bêta
Range 52 semaines
Volume · Volume moyen
Linéarité (R² de la régression du cours)
Prédictibilité du CA
Prix juste (Baggr) · Marge de sécurité · Rendement estimé
Prix communauté · Estimation manuelle
Prix P/E 10A · Prix P/FCF 10A · Prix P/OCF 10A · Prix P/Sales 10A · Prix P/Book 10A · Prix Div. Yield 10A
Dif. PE · Dif. FCF · Dif. POCF · Dif. PS · Dif. PB · Dif. EV  (écart au multiple médian 10A)
Régression linéaire (valeur + pente %/an)
Fair Value Baggr (+ bandes)
Forward P/E projeté (FY+1 … FY+5)
Moyennes mobiles 7 / 30 / 90 / 200 jours + écart du prix à chacune
CAGR Total (watchlist)
Années d'augmentation du dividende · Statut (Aristocrat / King)
Earnings Payout Ratio · FCF Payout Ratio
Earnings Shareholder Payout Ratio · FCF Shareholder Payout Ratio
Scores analystes : Score Global / DCF / ROE / ROA / Dette / P/E / P/B (sur 5)
Surprise EPS % · Croissance EPS YoY · Surprise CA % · Croissance CA YoY
Beat % / Miss % (historique de surprises)
Objectifs de prix High / Average / Low
Détention initiés %
Employés
Remaining Performance Obligations (KPI sectoriel)
```

### 17.3 Le suffixe temporel — grammaire des métriques Baggr
Toute métrique existe potentiellement en :
- **TTM** (trailing twelve months)
- **1A / 5A / 10A** (CAGR ou moyenne sur la période)
- **Médiane 10 ans** (pour les multiples)
- **Forward / Prévision 3-5 ans** (consensus analystes)
- **Communauté** (moyenne des estimations utilisateurs)

→ Pour Capital Antifragile, modéliser cela comme **(metric_id, horizon, aggregation)** plutôt que 400 colonnes plates.

---

## 18. Analyse concurrentielle et recommandations pour Capital Antifragile

### 18.1 Les 8 vraies forces de Baggr (à égaler au minimum)

1. **Densité d'information par écran.** La carte « Informations » compresse 57 métriques sans être illisible, grâce à la coloration sémantique et au regroupement en 9 blocs de 4-8 lignes.
2. **La médiane 10 ans partout.** Chaque multiple est immédiatement contextualisé. C'est *le* pattern à reprendre et à améliorer (ajouter percentile historique + percentile sectoriel).
3. **Le constructeur de métriques personnalisées** avec formule libre applicable à tout l'univers.
4. **Les segments et KPIs sourcés** avec gestion des changements de nomenclature (plages d'années).
5. **La double table Standardisées / Publiées.**
6. **La valorisation communautaire** (502 évaluations sur NVDA) : contenu propriétaire gratuit + rétention.
7. **Le concours de thèses** avec classement Top 50 / Flop 50 sur performance réelle.
8. **Les Sélections thématiques** : acquisition + découverte à coût quasi nul.

### 18.2 Les 12 faiblesses exploitables

| # | Faiblesse | Opportunité pour Capital Antifragile |
|---|---|---|
| 1 | **Performance** : pages Macro/Calendriers/Valorisation en skeleton pendant 30 s+ | SSR / streaming / cache agressif. Un time-to-interactive < 1 s est un argument commercial. |
| 2 | **Pas d'URL propre par action** (UUID opaque, ouverture en nouvel onglet forcée, pas de `<a>`) | URLs lisibles `/action/nvda-nvidia`, SSR, pages publiques indexables → SEO massif que Baggr n'a pas. |
| 3 | **Filtres « Santé financière » indigents** (4 critères) | Exposer les 17 ratios de dette de la table Ratios comme filtres : Altman Z, Piotroski, Current/Quick Ratio, Interest Coverage, Debt/Equity, échéancier de dette… |
| 4 | **Aucun filtre sur les scores** (Altman Z, Piotroski, scores analystes ne sont pas filtrables) | Les rendre filtrables et triables. |
| 5 | **Comparateur bêta et pauvre** | Vraie vue de comparaison : radar superposé, scatter (ex. ROIC vs valorisation), table de percentiles sectoriels, heatmap. |
| 6 | **Note Q = boîte noire** | **Publier la méthodologie**, la rendre paramétrable par l'utilisateur (pondérations custom des 6 axes), et permettre de screener dessus. C'est un différenciateur de confiance énorme. |
| 7 | **Pas de DCF** | Baggr n'a qu'un modèle « croissance × multiple final ». Ajouter un **vrai DCF** (FCFF/FCFE, WACC paramétrable, valeur terminale Gordon ou multiple, scénarios), un **reverse DCF** (« quelle croissance le marché price-t-il ? ») et une **simulation Monte-Carlo**. |
| 8 | **Pas de backtesting du screener** | « Cette combinaison de filtres aurait fait X % depuis 2010. » Rien de tel chez Baggr. |
| 9 | **Pas de mobile natif, pas d'alertes** | Alertes prix / marge de sécurité / publication de résultats / franchissement de multiple. PWA au minimum. |
| 10 | **Pas d'export / API** | Export CSV/XLSX de n'importe quelle table, API publique, add-in Excel/Google Sheets. Public « power user » très mal servi en France. |
| 11 | **Analyse qualitative = 9 blocs Gemini génériques** | Analyses ancrées dans les filings (RAG sur 10-K/10-Q/transcripts) avec **citations et liens vers le paragraphe source**. C'est là que se joue la crédibilité. |
| 12 | **Univers large mais qualité inégale** (SpaceX noté 4,5/20 alors que non cotée ; « ChinaRevenue » collé) | Qualité et cohérence des données comme argument. Afficher la fraîcheur et la source de chaque donnée. |

### 18.3 Ce qu'il faut construire pour être « au-dessus »

**Parité fonctionnelle (obligatoire)**
- [ ] Les 131 ratios historisés sur 11 ans + TTM, avec %chg YoY
- [ ] Les 3 états financiers standardisés + publiés, annuel/semestriel/trimestriel, lignes dépliables
- [ ] Segments & KPIs + répartition géographique historisés
- [ ] Ratios en versions TTM / 1A / 5A / 10A / médiane 10A / forward
- [ ] Multiples avec médiane décennale et prix implicite associé
- [ ] Constructeur de métriques personnalisées
- [ ] Screener : ~90 filtres + gestionnaire de colonnes + vues sauvegardées
- [ ] Calculatrice de prix juste (EPS / FCF / OCF / Sales) + matrice de sensibilité
- [ ] Dividendes : historique complet, payout earnings & FCF, shareholder yield, projection
- [ ] Résultats : historique de surprises depuis 1999, prévisions haut/moyen/bas, notes analystes
- [ ] Actionnariat : institutionnels, insiders, transactions
- [ ] 13F super-investisseurs
- [ ] Calendriers résultats/dividendes/splits/IPOs
- [ ] Portefeuille TWR + TRI + benchmark + module risque
- [ ] Watchlists avec flux dédiés (thèses/news/résultats/documents)

**Dépassement (le vrai avantage compétitif)**
1. **Transparence des scores** : Note de qualité open-method, pondérations éditables, backtest du score.
2. **Valorisation sérieuse** : DCF + reverse DCF + Monte-Carlo + scénarios nommés sauvegardables et partageables.
3. **Contexte sectoriel systématique** : chaque métrique affichée avec son **percentile dans son industrie** (et pas seulement sa médiane historique). Baggr ne le fait nulle part — c'est le trou le plus béant.
4. **Backtest de screener** intégré.
5. **Qualité des données visible** : date de dernière mise à jour, source, lien vers le filing, signalement d'anomalie.
6. **IA ancrée** : réponses sourcées sur les filings avec citations, pas des paragraphes génériques.
7. **Alertes + digest e-mail** paramétrables sur n'importe quel critère du screener.
8. **Export / API / Sheets**.
9. **Performance et SEO** : SSR, URLs propres, pages publiques par action et par thème → acquisition organique que Baggr n'a pas.
10. **Cohérence « antifragile »** : intégrer explicitement les métriques de robustesse (Altman Z, Piotroski, couverture d'intérêts, échéancier de dette, sensibilité aux taux, concentration client, dépendance fournisseur, résistance en drawdown historique) — c'est votre positionnement, et Baggr ne l'occupe pas.

### 18.4 Architecture de données recommandée (dérivée de Baggr)

```
stocks                 (uuid, isin, mic, ticker, name, country, sector, industry, sub_industry,
                        currency, website, pea_eligible, is_active, logo_url, employees)
stock_quotes           (stock_id, date, open, high, low, close, volume, adj_close)
financial_statements   (stock_id, period_type[FY|H|Q], fiscal_year, fiscal_period, end_date,
                        basis[standardized|as_reported], line_item_code, value, currency, source_filing_id)
segments               (stock_id, segment_type[business|geography|kpi], label, from_year, to_year,
                        fiscal_year, value)
stock_metrics          (stock_id, as_of, ~250 colonnes) ← materialized view pour screener/watchlist (batch)
metric_history         (stock_id, metric_code, period, value)  ← pour les graphiques et médianes N ans
estimates              (stock_id, fiscal_year, metric[revenue|eps], scenario[high|avg|low], value)
analyst_ratings        (stock_id, date, buy, overweight, hold, underweight, sell, target_high/avg/low)
earnings_events        (stock_id, date, fiscal_period, eps_actual, eps_est, rev_actual, rev_est, updated_at)
dividends              (stock_id, ex_date, record_date, pay_date, declare_date, amount, adj_amount, frequency)
holders                (stock_id, holder_id, type[institution|insider], shares, pct, value, date, role)
filings                (stock_id, type[10-K|10-Q|...], date, url)
valuations             (user_id, stock_id, basis[eps|fcf|ocf|sps], growth, exit_multiple, target_return,
                        include_dividends, fair_price)   → agrégat "prix communauté"
custom_metrics         (user_id, name, format, formula_ast, scope)
screener_views         (user_id, name, filters_json, columns_json)
watchlists / portfolios / transactions / theses / curated_lists / ...
```

**Clé de perf** : un endpoint **batch** unique qui renvoie la ligne `stock_metrics` pour N `stock_id` — exactement le `getStocksGeneralDatasBatch` de Baggr. Tout le screener, les watchlists, les concurrents et le comparateur s'appuient dessus.

---

## Annexe A — Récapitulatif de la fiche action (checklist d'implémentation)

| Onglet | Blocs | Métriques uniques |
|---|---|---|
| Résumé | 9 cartes | ~57 (carte Informations) + radar 6 axes + 8 prix implicites |
| Quantitatif | 20 widgets | ~55 ratios en blocs + 14 séries graphiques |
| Dividende | 1 carte KPI + 8 graphiques + 1 table | ~9 KPI + 8 séries + 8 champs d'historique |
| Résultats | 7 blocs + 1 table | ~20 |
| Finances | 7 sous-onglets | ~25 + ~40 + ~44 + **131** + segments + 13 ajustées + custom |
| Thèses | 1 liste | — |
| Société | 4 sous-onglets | 9 analyses IA + actionnariat + documents + concurrents |
| Valorisation | 1 calculatrice + 11 cartes | ~9 multiples + médianes + 4 bases communautaires + 4 historiques de multiples |

**Total distinct estimé : ~260 métriques + ~110 lignes d'états financiers.**

---

## Annexe B — Sources

- Application : https://app.baggr.fr (exploration authentifiée, 27/08/2026)
- Site vitrine et tarifs : https://baggr.fr
- Centre d'aide : https://baggr.fr/aide — https://baggr.fr/aide/screener
- Revue tierce : https://www.cleanyourfinance.com/p/141-avis-baggr-2026-loutil-ultime
