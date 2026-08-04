# Radar v3 — Validation walk-forward

Date construction : 2026-08-04

Méthode retenue : **EQUAL**


## Splits temporels

- TRAIN : 2018-02-01 → 2023-08-31 (2038 jours)
- VALID : 2023-09-01 → 2024-08-31 (366 jours)
- TEST  : 2024-09-01 → 2026-08-04 (703 jours) — ⛔ Non utilisé pour construction


## Audit sur TRAIN

| Signal | n | IC train | t | sub1/sub2 | CI 95% | Validé |
|---|---|---|---|---|---|---|
| fng | 0 | - | - | - / - | - | ❌ |
| funding_btc_avg | 1452 | -0.119 | -4.56 | -0.2722 / -0.1176 | [np.float64(-0.1744), np.float64(-0.0634)] | ✅ |
| funding_btc_daily | 1452 | -0.1183 | -4.53 | -0.2709 / -0.1176 | [np.float64(-0.1687), np.float64(-0.0653)] | ✅ |
| funding_eth_avg | 1374 | 0.0624 | 2.32 | -0.2653 / -0.122 | [np.float64(-0.0002), np.float64(0.1114)] | ❌ |
| stables_usd | 2038 | -0.0764 | -3.46 | 0.2594 / -0.379 | [np.float64(-0.1186), np.float64(-0.0316)] | ❌ |
| stables_growth_30d | 2038 | 0.0913 | 4.14 | 0.0595 / 0.0658 | [np.float64(0.0502), np.float64(0.1339)] | ✅ |
| stables_growth_7d | 2038 | 0.097 | 4.4 | 0.1245 / 0.0279 | [np.float64(0.053), np.float64(0.1392)] | ✅ |
| tvl_usd | 2038 | -0.1079 | -4.9 | 0.2265 / -0.4813 | [np.float64(-0.1522), np.float64(-0.0635)] | ❌ |
| tvl_growth_30d | 1997 | 0.0024 | 0.11 | -0.0863 / 0.0469 | [np.float64(-0.0412), np.float64(0.0445)] | ❌ |
| tvl_growth_7d | 2020 | -0.0178 | -0.8 | -0.0951 / 0.0489 | [np.float64(-0.0586), np.float64(0.021)] | ❌ |
| oi_btc_usd_close | 1095 | -0.5577 | -22.21 | -0.5319 / -0.5337 | [np.float64(-0.6052), np.float64(-0.5168)] | ✅ |
| oi_btc_usd_avg | 1095 | -0.5578 | -22.22 | -0.5288 / -0.5347 | [np.float64(-0.603), np.float64(-0.5093)] | ✅ |
| ls_count_btc | 1076 | -0.2054 | -6.88 | -0.3863 / -0.0723 | [np.float64(-0.2587), np.float64(-0.142)] | ✅ |
| ls_top_count_btc | 780 | -0.2926 | -8.54 | -0.3874 / -0.2892 | [np.float64(-0.3561), np.float64(-0.2282)] | ✅ |
| ls_top_position_btc | 780 | -0.1939 | -5.51 | -0.2021 / -0.1434 | [np.float64(-0.2578), np.float64(-0.1243)] | ✅ |
| taker_lsv_btc_perp | 967 | 0.1387 | 4.35 | 0.3587 / 0.0072 | [np.float64(0.0752), np.float64(0.2043)] | ❌ |
| btc_taker_buy_ratio_spot | 2038 | 0.0133 | 0.6 | -0.0288 / 0.0317 | [np.float64(-0.0271), np.float64(0.0583)] | ❌ |
| btc_eth_ratio | 2038 | 0.2386 | 11.08 | 0.0872 / 0.3266 | [np.float64(0.1972), np.float64(0.2799)] | ✅ |
| btc_vol | 2038 | 0.1059 | 4.8 | 0.0564 / 0.1905 | [np.float64(0.0636), np.float64(0.1461)] | ✅ |

## Comparaison méthodes (TRAIN vs VALID)

| Méthode | IC train | IC valid | Ratio (V/T) | Spread valid | Buckets valid |
|---|---|---|---|---|---|
| **equal** 🏆 | 0.3993 | 0.3341 | 0.84 | 14.22 pp | 0-25: +1.2% (33)<br>25-45: +3.1% (118)<br>45-55: +12.8% (64)<br>55-75: +11.0% (134)<br>75-100: +15.5% (17) |
| **ic_weighted**  | 0.4219 | 0.3618 | 0.86 | 14.25 pp | 0-25: +2.9% (123)<br>25-45: +8.4% (118)<br>45-55: +10.0% (57)<br>55-75: +15.2% (62)<br>75-100: +17.1% (6) |
| **ridge**  | 0.7267 | 0.1717 | 0.24 | 5.57 pp | 0-25: -5.6% (29)<br>25-45: +9.9% (302)<br>45-55: +3.2% (32)<br>55-75: +7.6% (3)<br>75-100: +0.0% (0) |
| **ablation**  | 0.495 | 0.4246 | 0.86 | -2.17 pp | 0-25: +2.2% (126)<br>25-45: +9.1% (160)<br>45-55: +12.8% (33)<br>55-75: +17.3% (47)<br>75-100: +0.0% (0) |

## Méthode retenue : equal

Poids attribués (par signal) :

- **funding_btc_avg** : poids = 0.0909, sens = Contrarian
- **funding_btc_daily** : poids = 0.0909, sens = Contrarian
- **stables_growth_30d** : poids = 0.0909, sens = Momentum
- **stables_growth_7d** : poids = 0.0909, sens = Momentum
- **oi_btc_usd_close** : poids = 0.0909, sens = Contrarian
- **oi_btc_usd_avg** : poids = 0.0909, sens = Contrarian
- **ls_count_btc** : poids = 0.0909, sens = Contrarian
- **ls_top_count_btc** : poids = 0.0909, sens = Contrarian
- **ls_top_position_btc** : poids = 0.0909, sens = Contrarian
- **btc_eth_ratio** : poids = 0.0909, sens = Momentum
- **btc_vol** : poids = 0.0909, sens = Momentum

## Critères de validation walk-forward

- IC train = 0.3993
- IC valid = 0.3341
- Ratio valid/train = 0.84 (cible ≥ 0.5)
- Signe stable : ✅

## Test (boîte fermée — Phase 0b.4 à venir)

Le test set (2024-09-01 → 2026-08-04) reste intact pour évaluation finale Phase 0b.4.