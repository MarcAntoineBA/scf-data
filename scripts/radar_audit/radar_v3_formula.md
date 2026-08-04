```json
{
  "method": "equal",
  "selected_signals": [
    "funding_btc_avg",
    "funding_btc_daily",
    "stables_growth_30d",
    "stables_growth_7d",
    "oi_btc_usd_close",
    "oi_btc_usd_avg",
    "ls_count_btc",
    "ls_top_count_btc",
    "ls_top_position_btc",
    "btc_eth_ratio",
    "btc_vol"
  ],
  "signs": [
    -1,
    -1,
    1,
    1,
    -1,
    -1,
    -1,
    -1,
    -1,
    1,
    1
  ],
  "weights": [
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091,
    0.09090909090909091
  ],
  "rolling_window": 365,
  "z_cap": 3.0,
  "transformation": "rolling_zscore",
  "score_transform": "cdf_normal_x_100",
  "train_period": [
    "2018-02-01",
    "2023-08-31"
  ],
  "valid_period": [
    "2023-09-01",
    "2024-08-31"
  ],
  "ic_train": 0.3993,
  "ic_valid": 0.3341
}
```
