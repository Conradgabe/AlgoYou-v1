"""
BOS + FVG Retest Strategy — Multi-Pair Test
Tests the exact same logic across 15 forex pairs + gold.
No overfitting. No narrative. Just data.
"""
import yfinance as yf
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 15 FOREX PAIRS + GOLD
# ============================================================

pairs = {
    # Major pairs
    'EURUSD=X': 'EUR/USD',
    'GBPUSD=X': 'GBP/USD',
    'USDJPY=X': 'USD/JPY',
    'USDCHF=X': 'USD/CHF',
    'AUDUSD=X': 'AUD/USD',
    'NZDUSD=X': 'NZD/USD',
    'USDCAD=X': 'USD/CAD',
    # Cross pairs
    'EURJPY=X': 'EUR/JPY',
    'GBPJPY=X': 'GBP/JPY',
    'EURGBP=X': 'EUR/GBP',
    'AUDJPY=X': 'AUD/JPY',
    'EURAUD=X': 'EUR/AUD',
    'GBPAUD=X': 'GBP/AUD',
    'CADJPY=X': 'CAD/JPY',
    'NZDJPY=X': 'NZD/JPY',
    # Gold
    'GC=F': 'XAUUSD',
}

def detect_swings(df, lookback=5):
    """Detect swing highs and lows."""
    highs = df['High'].values
    lows = df['Low'].values
    n = len(df)
    swing_high = [False] * n
    swing_low = [False] * n
    swing_high_val = [np.nan] * n
    swing_low_val = [np.nan] * n

    for i in range(lookback, n - lookback):
        is_high = True
        is_low = True
        for j in range(1, lookback + 1):
            if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                is_high = False
            if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                is_low = False
        swing_high[i] = is_high
        swing_low[i] = is_low
        if is_high:
            swing_high_val[i] = highs[i]
        if is_low:
            swing_low_val[i] = lows[i]

    return swing_high, swing_low, swing_high_val, swing_low_val


def detect_bos(df, swing_high_val, swing_low_val):
    """Detect Break of Structure."""
    closes = df['Close'].values
    n = len(df)
    bos_bull = [False] * n
    bos_bear = [False] * n
    last_sh = np.nan
    last_sl = np.nan

    for i in range(n):
        if not np.isnan(swing_high_val[i]):
            last_sh = swing_high_val[i]
        if not np.isnan(swing_low_val[i]):
            last_sl = swing_low_val[i]

        if i > 0 and not np.isnan(last_sh):
            if closes[i] > last_sh and closes[i-1] <= last_sh:
                bos_bull[i] = True
        if i > 0 and not np.isnan(last_sl):
            if closes[i] < last_sl and closes[i-1] >= last_sl:
                bos_bear[i] = True

    return bos_bull, bos_bear


def detect_fvg(df):
    """Detect Fair Value Gaps."""
    n = len(df)
    bull_fvg = [False] * n
    bear_fvg = [False] * n

    for i in range(2, n):
        # Bullish FVG: candle[i].low > candle[i-2].high
        if df['Low'].iloc[i] > df['High'].iloc[i-2] and df['Close'].iloc[i-1] > df['Open'].iloc[i-1]:
            bull_fvg[i] = True
        # Bearish FVG: candle[i].high < candle[i-2].low
        if df['High'].iloc[i] < df['Low'].iloc[i-2] and df['Close'].iloc[i-1] < df['Open'].iloc[i-1]:
            bear_fvg[i] = True

    return bull_fvg, bear_fvg


def run_strategy(df, pair_name, swing_lookback=5, fvg_search=5, max_retest_bars=40, cooldown=6):
    """
    Run BOS + FVG Retest strategy.
    Returns list of signal results.
    """
    if len(df) < 100:
        return []

    df = df.copy()
    df['Hour'] = df.index.hour

    # Detect components
    sh, sl, sh_val, sl_val = detect_swings(df, swing_lookback)
    bos_bull, bos_bear = detect_bos(df, sh_val, sl_val)
    bull_fvg, bear_fvg = detect_fvg(df)

    signals = []
    last_signal_bar = -999

    for i in range(20, len(df) - 20):
        # Cooldown
        if i - last_signal_bar < cooldown:
            continue

        hour = df['Hour'].iloc[i]

        # Find recent BOS (within fvg_search bars)
        recent_bull_bos = False
        recent_bear_bos = False
        bos_bar = None
        for k in range(max(0, i - fvg_search), i + 1):
            if bos_bull[k]:
                recent_bull_bos = True
                bos_bar = k
            if bos_bear[k]:
                recent_bear_bos = True
                bos_bar = k

        if not recent_bull_bos and not recent_bear_bos:
            continue

        # Find FVG near the BOS
        for k in range(max(0, i - fvg_search), i + 1):
            # Bullish BOS + Bullish FVG → look for retest (price dips into FVG)
            if recent_bull_bos and bull_fvg[k] and k >= 2:
                fvg_top = df['Low'].iloc[k]
                fvg_bot = df['High'].iloc[k - 2]
                fvg_mid = (fvg_top + fvg_bot) / 2

                # Check if current bar retests the FVG
                if df['Low'].iloc[i] <= fvg_top and df['Close'].iloc[i] > fvg_bot:
                    entry = df['Close'].iloc[i]
                    stop = fvg_bot - (fvg_top - fvg_bot) * 0.5
                    risk = entry - stop
                    if risk <= 0:
                        continue

                    # Forward returns
                    results = {'pair': pair_name, 'type': 'BUY', 'hour': hour,
                               'entry': entry, 'risk': risk, 'bar_idx': i}

                    for n in [1, 2, 4, 8, 12]:
                        if i + n < len(df):
                            fwd = (df['Close'].iloc[i + n] - entry) / risk
                            results[f'fwd_{n}R'] = fwd

                            # Max favorable excursion
                            mfe = (df['High'].iloc[i+1:i+n+1].max() - entry) / risk
                            mae = (entry - df['Low'].iloc[i+1:i+n+1].min()) / risk
                            results[f'mfe_{n}R'] = mfe
                            results[f'mae_{n}R'] = mae
                        else:
                            results[f'fwd_{n}R'] = np.nan
                            results[f'mfe_{n}R'] = np.nan
                            results[f'mae_{n}R'] = np.nan

                    signals.append(results)
                    last_signal_bar = i
                    break

            # Bearish BOS + Bearish FVG → look for retest (price rallies into FVG)
            if recent_bear_bos and bear_fvg[k] and k >= 2:
                fvg_top = df['Low'].iloc[k - 2]
                fvg_bot = df['High'].iloc[k]
                fvg_mid = (fvg_top + fvg_bot) / 2

                if df['High'].iloc[i] >= fvg_bot and df['Close'].iloc[i] < fvg_top:
                    entry = df['Close'].iloc[i]
                    stop = fvg_top + (fvg_top - fvg_bot) * 0.5
                    risk = stop - entry
                    if risk <= 0:
                        continue

                    results = {'pair': pair_name, 'type': 'SELL', 'hour': hour,
                               'entry': entry, 'risk': risk, 'bar_idx': i}

                    for n in [1, 2, 4, 8, 12]:
                        if i + n < len(df):
                            fwd = (entry - df['Close'].iloc[i + n]) / risk
                            results[f'fwd_{n}R'] = fwd
                            mfe = (entry - df['Low'].iloc[i+1:i+n+1].min()) / risk
                            mae = (df['High'].iloc[i+1:i+n+1].max() - entry) / risk
                            results[f'mfe_{n}R'] = mfe
                            results[f'mae_{n}R'] = mae
                        else:
                            results[f'fwd_{n}R'] = np.nan
                            results[f'mfe_{n}R'] = np.nan
                            results[f'mae_{n}R'] = np.nan

                    signals.append(results)
                    last_signal_bar = i
                    break

    return signals


# ============================================================
# RUN ON ALL PAIRS
# ============================================================

print("=" * 80)
print("BOS + FVG RETEST — MULTI-PAIR TEST (5-MINUTE DATA, 60 DAYS)")
print("=" * 80)
print()

all_signals = []

for ticker, name in pairs.items():
    print(f"Fetching {name} ({ticker})...", end=" ")
    try:
        df = yf.download(ticker, period='60d', interval='5m', progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200:
            print(f"insufficient data ({len(df)} bars)")
            continue
        print(f"{len(df)} bars", end=" >> ")

        sigs = run_strategy(df, name)
        all_signals.extend(sigs)
        print(f"{len(sigs)} signals")
    except Exception as e:
        print(f"ERROR: {e}")

print(f"\nTotal signals across all pairs: {len(all_signals)}")

if len(all_signals) == 0:
    print("No signals found. Exiting.")
    exit()

sdf = pd.DataFrame(all_signals)

# ============================================================
# RESULTS BY PAIR
# ============================================================

print("\n" + "=" * 80)
print("RESULTS BY PAIR — 4-bar hold (measured in R-multiples)")
print("=" * 80)
print(f"{'Pair':<12} {'Signals':>8} {'WinRate':>8} {'AvgR':>8} {'MFE':>8} {'MAE':>8} {'Hit2R%':>8} {'Verdict':>10}")
print("-" * 80)

pair_results = []
for pair_name in sdf['pair'].unique():
    sub = sdf[sdf['pair'] == pair_name]
    fwd = sub['fwd_4R'].dropna()
    if len(fwd) < 5:
        continue

    wr = (fwd > 0).mean() * 100
    avg_r = fwd.mean()
    mfe = sub['mfe_4R'].dropna().mean()
    mae = sub['mae_4R'].dropna().mean()
    hit_2r = (sub['mfe_8R'].dropna() >= 2.0).mean() * 100  # did price reach 2R within 8 bars?

    verdict = "EDGE" if wr > 52 and avg_r > 0 else "WEAK" if wr > 48 else "NO EDGE"

    pair_results.append((avg_r, wr, len(sub), mfe, mae, hit_2r, pair_name, verdict))
    print(f"{pair_name:<12} {len(sub):>8} {wr:>7.1f}% {avg_r:>+7.3f} {mfe:>7.2f} {mae:>7.2f} {hit_2r:>7.1f}% {verdict:>10}")

pair_results.sort(key=lambda x: x[0], reverse=True)

# ============================================================
# RANKED SUMMARY
# ============================================================

print("\n" + "=" * 80)
print("RANKED BY AVERAGE R (best to worst)")
print("=" * 80)
print(f"{'Rank':>4} {'Pair':<12} {'Signals':>8} {'WinRate':>8} {'AvgR':>8} {'Hit2R%':>8} {'Verdict':>10}")
print("-" * 80)
for rank, (avg_r, wr, n, mfe, mae, hit_2r, name, verdict) in enumerate(pair_results, 1):
    flag = " <<<" if verdict == "EDGE" else ""
    print(f"{rank:>4} {name:<12} {n:>8} {wr:>7.1f}% {avg_r:>+7.3f} {hit_2r:>7.1f}% {verdict:>10}{flag}")

# ============================================================
# RESULTS BY SIGNAL TYPE
# ============================================================

print("\n" + "=" * 80)
print("RESULTS BY SIGNAL TYPE (across all pairs)")
print("=" * 80)

for sig_type in ['BUY', 'SELL']:
    sub = sdf[sdf['type'] == sig_type]
    for n in [1, 2, 4, 8]:
        col = f'fwd_{n}R'
        fwd = sub[col].dropna()
        if len(fwd) > 5:
            wr = (fwd > 0).mean() * 100
            avg = fwd.mean()
            print(f"  {sig_type} {n}-bar: wr={wr:.1f}%, avgR={avg:+.3f}, n={len(fwd)}")
    print()

# ============================================================
# RESULTS BY HOUR (across all pairs)
# ============================================================

print("=" * 80)
print("RESULTS BY HOUR (across all pairs, 4-bar hold)")
print("=" * 80)

for h in sorted(sdf['hour'].unique()):
    sub = sdf[sdf['hour'] == h]
    fwd = sub['fwd_4R'].dropna()
    if len(fwd) >= 5:
        wr = (fwd > 0).mean() * 100
        avg = fwd.mean()
        flag = " <<<" if wr > 55 and avg > 0 else ""
        print(f"  Hour {h:02d}: wr={wr:.1f}%, avgR={avg:+.3f}, n={len(fwd)}{flag}")

# ============================================================
# OPTIMAL HOLD TIME
# ============================================================

print("\n" + "=" * 80)
print("OPTIMAL HOLD TIME (across all pairs)")
print("=" * 80)

for n in [1, 2, 4, 8, 12]:
    col = f'fwd_{n}R'
    fwd = sdf[col].dropna()
    if len(fwd) > 0:
        wr = (fwd > 0).mean() * 100
        avg = fwd.mean()
        print(f"  {n}-bar hold: wr={wr:.1f}%, avgR={avg:+.3f}, n={len(fwd)}")

# ============================================================
# MFE ANALYSIS — How far do trades go before reversing?
# ============================================================

print("\n" + "=" * 80)
print("MFE/MAE ANALYSIS (how far trades go)")
print("=" * 80)

for n in [4, 8]:
    mfe = sdf[f'mfe_{n}R'].dropna()
    mae = sdf[f'mae_{n}R'].dropna()
    if len(mfe) > 0:
        print(f"  {n}-bar window:")
        print(f"    Avg MFE: {mfe.mean():.2f}R (trades go this far in your favor)")
        print(f"    Avg MAE: {mae.mean():.2f}R (trades go this far against you)")
        print(f"    MFE >= 1R: {(mfe >= 1.0).mean()*100:.1f}% of trades")
        print(f"    MFE >= 2R: {(mfe >= 2.0).mean()*100:.1f}% of trades")
        print(f"    MAE >= 1R: {(mae >= 1.0).mean()*100:.1f}% of trades (would be stopped out)")
        print()

# ============================================================
# TOP 5 PAIRS — DETAILED BREAKDOWN
# ============================================================

print("=" * 80)
print("TOP 5 PAIRS — DETAILED")
print("=" * 80)

top5 = pair_results[:5]
for avg_r, wr, n, mfe, mae, hit_2r, name, verdict in top5:
    sub = sdf[sdf['pair'] == name]
    print(f"\n  {name} ({n} signals, {verdict}):")

    for hold in [1, 2, 4, 8]:
        col = f'fwd_{hold}R'
        fwd = sub[col].dropna()
        if len(fwd) > 0:
            print(f"    {hold}-bar: wr={( fwd > 0).mean()*100:.1f}%, avgR={fwd.mean():+.3f}")

    # By type
    for t in ['BUY', 'SELL']:
        tsub = sub[sub['type'] == t]
        fwd = tsub['fwd_4R'].dropna()
        if len(fwd) >= 3:
            print(f"    {t}: wr={(fwd>0).mean()*100:.1f}%, avgR={fwd.mean():+.3f}, n={len(fwd)}")

    # Best hours
    print(f"    Best hours: ", end="")
    for h in sorted(sub['hour'].unique()):
        hsub = sub[sub['hour'] == h]
        fwd = hsub['fwd_4R'].dropna()
        if len(fwd) >= 3 and (fwd > 0).mean() > 0.55:
            print(f"H{h:02d}({(fwd>0).mean()*100:.0f}%) ", end="")
    print()

print("\n" + "=" * 80)
print("TEST COMPLETE")
print("=" * 80)
