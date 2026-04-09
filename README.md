# AlgoYou v1

Trading tools and indicators for forex and gold scalping.

## Structure

```
scripts/          Pine Script indicators + Python analysis tools
new/scalper/      MT5 scalper engine with Python backend
```

## Pine Script Indicators

| Indicator | File | Timeframe | Description |
|-----------|------|-----------|-------------|
| **BOS+FVG Scalper v2** | `scripts/xauusd_bos_fvg_scalper.pine` | 5m | Break of Structure + Fair Value Gap retest entries with HTF alignment, premium/discount zones, displacement quality filter |
| **BOS+FVG Strategy** | `scripts/bos_fvg_strategy.pine` | 5m | Backtestable strategy version of the BOS+FVG Scalper |
| **Liquidity Sweep** | `scripts/xauusd_liquidity_sweep.pine` | 15m | Session range liquidity sweep detection with displacement signals |
| **Micro Structure** | `scripts/xauusd_micro_structure.pine` | 1m/3m | Order blocks + Break of Structure for micro-timeframe entries |

## BOS+FVG Scalper v2 — Main Indicator

The primary indicator. Built from statistical analysis of 13,000+ bars across 16 forex pairs.

### How It Works

1. Detects swing highs/lows (market structure)
2. Detects Break of Structure (BOS) when price breaks a swing level
3. Identifies Fair Value Gaps (FVG) created by the BOS move
4. Signals entry when price retests the FVG
5. Filters by: HTF trend, premium/discount zone, displacement quality, time of day, rejection candle

### Filters

| Filter | Purpose |
|--------|---------|
| HTF Alignment | 1H EMA trend — only BUY when 1H bullish, SELL when 1H bearish |
| Premium/Discount | Only BUY in discount (below range midpoint), SELL in premium |
| Displacement Quality | FVGs only form from strong BOS candles (body > 1.2x average) |
| FVG Min Size | Gaps smaller than 0.3x ATR are ignored |
| Stack Limit | Only the 2 most recent FVGs per direction can generate signals |
| Rejection Candle | Entry candle must show wick rejection at the FVG level |
| Time Filter | Blocks statistically worst hours (02, 11, 17, 22 UTC) |
| Silver Bullet | Highlights best hours (07, 14, 18 UTC) |
| Cooldown | 6-bar minimum between any signals |

### Best Pairs (from multi-pair backtest)

| Pair | Win Rate | Avg R |
|------|----------|-------|
| NZD/USD | 53.6% | +0.263 |
| GBP/USD | 55.7% | +0.169 |
| USD/JPY | 53.4% | +0.101 |
| CAD/JPY | 55.2% | +0.100 |
| XAUUSD | 50.9% | +0.201 |

### Setup

1. Open TradingView
2. Select a pair (NZD/USD, GBP/USD, or XAUUSD recommended)
3. Switch to **5-minute** timeframe
4. Pine Editor > paste `scripts/xauusd_bos_fvg_scalper.pine` > Add to Chart
5. Turn off Swing Points and BOS Labels in Settings > Display for a cleaner chart

### Signals

| Signal | Color | Meaning |
|--------|-------|---------|
| **BUY** | Green | FVG retest entry, HTF aligned |
| **SELL** | Red | FVG retest entry, HTF aligned |
| **BUY *** | Gold | FVG retest during Silver Bullet window (highest probability) |
| **SELL *** | Gold | FVG retest during Silver Bullet window |

## Python Analysis Tools

| Script | Purpose |
|--------|---------|
| `gold_pattern_analysis.py` | Statistical pattern analysis on 15m gold data |
| `gold_signal_deep_analysis.py` | Factor analysis: what predicts winning vs losing signals |
| `gold_time_window_analysis.py` | BOS+FVG + Silver Bullet time window analysis on 5m data |
| `bos_fvg_multi_pair_test.py` | Multi-pair backtest across 16 forex pairs |

Run with: `python scripts/<script>.py` (requires `yfinance`, `pandas`, `numpy`)

## MT5 Scalper Engine

Located in `new/scalper/`. A Python + MetaTrader 5 scalping engine.

- `engine/` — Python backend (signals, risk management, session handling)
- `mt5/` — MQL5 Expert Advisor for MetaTrader 5
- `config/` — Settings
- `tests/` — Unit tests

## Risk Management

- Risk 0.5% per trade
- Max 6 signals per day
- 2:1 reward-to-risk ratio default
- Do not trade during blocked hours
- Do not hold through major news events (CPI, NFP, FOMC)

## Disclaimer

This software is for educational and research purposes only. It does not constitute financial advice. Trading involves substantial risk of loss. Past performance does not guarantee future results.
