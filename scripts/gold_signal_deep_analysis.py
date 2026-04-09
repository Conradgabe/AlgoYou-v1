"""
Deep analysis of XAUUSD signal quality.
Simulates the liquidity sweep indicator logic on real data,
then analyzes what differentiates WINNING signals from LOSING signals.
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

print("Fetching XAUUSD 15m data (60 days)...")
gold = yf.download('GC=F', period='60d', interval='15m', progress=False)
gold.columns = gold.columns.get_level_values(0)
print(f"Records: {len(gold)}, Range: {gold.index[0]} to {gold.index[-1]}")
print(f"Price: ${gold['Close'].min():.2f} to ${gold['Close'].max():.2f}")
print()

# ============================================================
# SIMULATE THE INDICATOR LOGIC ON REAL DATA
# ============================================================

gold['Hour'] = gold.index.hour
gold['Body'] = abs(gold['Close'] - gold['Open'])
gold['Range'] = gold['High'] - gold['Low']
gold['Bullish'] = gold['Close'] > gold['Open']
gold['Bearish'] = gold['Close'] < gold['Open']
gold['ATR'] = gold['Range'].rolling(14).mean()
gold['AvgBody'] = gold['Body'].rolling(20).mean()
gold['Date'] = gold.index.date

# Forward returns for measuring signal quality
for n in [1, 2, 4, 8, 12, 16]:
    gold[f'Fwd_{n}'] = gold['Close'].shift(-n) / gold['Close'] - 1
    gold[f'FwdHigh_{n}'] = gold['High'].rolling(n).max().shift(-n)
    gold[f'FwdLow_{n}'] = gold['Low'].rolling(n).min().shift(-n)

# Max favorable excursion (MFE) and max adverse excursion (MAE) over next N bars
for n in [4, 8, 12]:
    gold[f'MFE_up_{n}'] = (gold[f'FwdHigh_{n}'] - gold['Close']) / gold['Close'] * 100
    gold[f'MFE_down_{n}'] = (gold['Close'] - gold[f'FwdLow_{n}']) / gold['Close'] * 100

# ============================================================
# BUILD QUIET RANGES (simulating 22:00-02:00 UTC = 18:00-22:00 EDT)
# Since data is in UTC, quiet hours are approximately 22:00-02:00
# But we need to handle the exchange timezone offset
# ============================================================

# Group by date and find the quiet period range
# We'll use 21:00-02:00 UTC as a proxy for the quiet period
gold['IsQuiet'] = gold['Hour'].isin([21, 22, 23, 0, 1])

# Build daily ranges
daily_ranges = []
dates = sorted(gold['Date'].unique())

for i, date in enumerate(dates):
    # Quiet period: evening of previous day + early morning of this day
    if i == 0:
        continue
    prev_date = dates[i-1]

    # Evening of previous day (21:00-23:59)
    evening = gold[(gold['Date'] == prev_date) & (gold['Hour'] >= 21)]
    # Early morning of this day (00:00-01:59)
    morning = gold[(gold['Date'] == date) & (gold['Hour'] <= 1)]

    quiet = pd.concat([evening, morning])
    if len(quiet) < 3:
        continue

    range_high = quiet['High'].max()
    range_low = quiet['Low'].min()
    range_size = range_high - range_low

    daily_ranges.append({
        'date': date,
        'range_high': range_high,
        'range_low': range_low,
        'range_size': range_size,
        'range_mid': (range_high + range_low) / 2
    })

ranges_df = pd.DataFrame(daily_ranges)
print(f"Daily quiet ranges computed: {len(ranges_df)}")
print(f"Average range size: ${ranges_df['range_size'].mean():.2f}")
print(f"Median range size: ${ranges_df['range_size'].median():.2f}")
print()

# ============================================================
# DETECT ALL SWEEP + DISPLACEMENT EVENTS
# ============================================================

signals = []

for _, rng in ranges_df.iterrows():
    date = rng['date']
    rh = rng['range_high']
    rl = rng['range_low']
    rs = rng['range_size']
    rm = rng['range_mid']

    # Get the day's bars (after quiet period: 02:00 UTC onwards)
    day_bars = gold[(gold['Date'] == date) & (gold['Hour'] >= 2)]
    if len(day_bars) < 10:
        continue

    atr = day_bars['ATR'].mean()
    if pd.isna(atr) or atr == 0:
        continue

    sweep_threshold = atr * 0.3

    # Track sweep events
    went_above = False
    went_below = False
    high_sweep_bar = None
    low_sweep_bar = None

    for idx, bar in day_bars.iterrows():
        # Detect price going beyond range
        if bar['High'] > rh + sweep_threshold:
            went_above = True
        if bar['Low'] < rl - sweep_threshold:
            went_below = True

        # High sweep confirmed: went above AND now closes below
        if went_above and bar['Close'] < rh and high_sweep_bar is None:
            high_sweep_bar = idx

        # Low sweep confirmed: went below AND now closes above
        if went_below and bar['Close'] > rl and low_sweep_bar is None:
            low_sweep_bar = idx

    # Now find displacement candles after each sweep
    if high_sweep_bar is not None:
        # Look for bearish displacement within 8 bars after high sweep
        sweep_pos = day_bars.index.get_loc(high_sweep_bar)
        for j in range(sweep_pos, min(sweep_pos + 9, len(day_bars))):
            bar = day_bars.iloc[j]
            if bar['Bearish'] and bar['Body'] >= bar['AvgBody'] * 1.0:
                # BUY signal (inverted: bearish displacement after high sweep = buy)
                # Calculate how far price went above range
                bars_above = day_bars.iloc[:sweep_pos+1]
                max_above = bars_above['High'].max()
                sweep_depth = max_above - rh

                # Time features
                hour = bar.name.hour

                # Get forward returns
                bar_loc = gold.index.get_loc(bar.name)

                sig = {
                    'date': date,
                    'type': 'BUY',
                    'price': bar['Close'],
                    'hour': hour,
                    'range_size': rs,
                    'range_high': rh,
                    'range_low': rl,
                    'sweep_depth': sweep_depth,
                    'sweep_depth_pct': sweep_depth / rh * 100,
                    'body_ratio': bar['Body'] / bar['AvgBody'] if bar['AvgBody'] > 0 else 0,
                    'dist_from_range_high': abs(bar['Close'] - rh),
                    'dist_from_range_high_pct': abs(bar['Close'] - rh) / rh * 100,
                    'atr': atr,
                    'bars_since_sweep': j - sweep_pos,
                }

                # Forward returns
                for n in [1, 2, 4, 8, 12, 16]:
                    col = f'Fwd_{n}'
                    if col in gold.columns and bar_loc + n < len(gold):
                        sig[f'fwd_{n}'] = gold.iloc[bar_loc][col]
                    else:
                        sig[f'fwd_{n}'] = np.nan

                # MFE/MAE
                for n in [4, 8, 12]:
                    for metric in ['MFE_up', 'MFE_down']:
                        col = f'{metric}_{n}'
                        if col in gold.columns:
                            sig[f'{metric}_{n}'] = gold.iloc[bar_loc][col]
                        else:
                            sig[f'{metric}_{n}'] = np.nan

                signals.append(sig)
                break  # one signal per sweep

    if low_sweep_bar is not None:
        # Look for bullish displacement within 8 bars after low sweep
        sweep_pos = day_bars.index.get_loc(low_sweep_bar)
        for j in range(sweep_pos, min(sweep_pos + 9, len(day_bars))):
            bar = day_bars.iloc[j]
            if bar['Bullish'] and bar['Body'] >= bar['AvgBody'] * 1.0:
                # SELL signal (inverted: bullish displacement after low sweep = sell)
                bars_below = day_bars.iloc[:sweep_pos+1]
                max_below = rl - bars_below['Low'].min()
                sweep_depth = max_below

                hour = bar.name.hour
                bar_loc = gold.index.get_loc(bar.name)

                sig = {
                    'date': date,
                    'type': 'SELL',
                    'price': bar['Close'],
                    'hour': hour,
                    'range_size': rs,
                    'range_high': rh,
                    'range_low': rl,
                    'sweep_depth': sweep_depth,
                    'sweep_depth_pct': sweep_depth / rl * 100 if rl > 0 else 0,
                    'body_ratio': bar['Body'] / bar['AvgBody'] if bar['AvgBody'] > 0 else 0,
                    'dist_from_range_low': abs(bar['Close'] - rl),
                    'dist_from_range_low_pct': abs(bar['Close'] - rl) / rl * 100,
                    'atr': atr,
                    'bars_since_sweep': j - sweep_pos,
                }

                for n in [1, 2, 4, 8, 12, 16]:
                    col = f'Fwd_{n}'
                    if col in gold.columns and bar_loc + n < len(gold):
                        sig[f'fwd_{n}'] = gold.iloc[bar_loc][col] * -1  # invert for sells
                    else:
                        sig[f'fwd_{n}'] = np.nan

                for n in [4, 8, 12]:
                    sig[f'MFE_up_{n}'] = gold.iloc[bar_loc].get(f'MFE_down_{n}', np.nan)  # for sells, down is favorable
                    sig[f'MFE_down_{n}'] = gold.iloc[bar_loc].get(f'MFE_up_{n}', np.nan)

                signals.append(sig)
                break

sdf = pd.DataFrame(signals)
print(f"Total signals detected: {len(sdf)}")
print(f"  BUY signals: {len(sdf[sdf['type']=='BUY'])}")
print(f"  SELL signals: {len(sdf[sdf['type']=='SELL'])}")
print()

if len(sdf) == 0:
    print("No signals found. Exiting.")
    exit()

# ============================================================
# CLASSIFY WINNERS VS LOSERS
# Using 8-bar forward return and 1.5:1 RR
# ============================================================

# A signal is a WINNER if MFE (max favorable) >= 1.5x MAE (max adverse) within 8 bars
# AND the forward return at 8 bars is positive (in the signal direction)

sdf['winner_8'] = sdf['fwd_8'] > 0
sdf['winner_4'] = sdf['fwd_4'] > 0

print("=" * 70)
print("OVERALL SIGNAL QUALITY")
print("=" * 70)

for n in [4, 8, 12]:
    col = f'fwd_{n}'
    valid = sdf[col].dropna()
    if len(valid) > 0:
        wr = (valid > 0).mean() * 100
        avg = valid.mean() * 100
        print(f"  {n}-bar hold: avg={avg:+.4f}%, win_rate={wr:.1f}%, n={len(valid)}")
print()

# ============================================================
# WHAT DIFFERENTIATES WINNERS FROM LOSERS?
# ============================================================

print("=" * 70)
print("FACTOR ANALYSIS: What predicts winning signals?")
print("=" * 70)

# Factor 1: Range size
print("\n-- FACTOR 1: Quiet Range Size --")
for threshold in [15, 25, 35, 50, 75]:
    small = sdf[sdf['range_size'] <= threshold]
    large = sdf[sdf['range_size'] > threshold]
    if len(small) > 3 and len(large) > 3:
        small_wr = (small['fwd_8'].dropna() > 0).mean() * 100
        large_wr = (large['fwd_8'].dropna() > 0).mean() * 100
        small_avg = small['fwd_8'].dropna().mean() * 100
        large_avg = large['fwd_8'].dropna().mean() * 100
        print(f"  Range <= ${threshold}: wr={small_wr:.1f}%, avg={small_avg:+.4f}%, n={len(small)}")
        print(f"  Range >  ${threshold}: wr={large_wr:.1f}%, avg={large_avg:+.4f}%, n={len(large)}")
        print()

# Factor 2: Sweep depth (how far price went beyond the range)
print("-- FACTOR 2: Sweep Depth --")
sdf_valid = sdf[sdf['sweep_depth'].notna()]
if len(sdf_valid) > 5:
    median_depth = sdf_valid['sweep_depth'].median()
    shallow = sdf_valid[sdf_valid['sweep_depth'] <= median_depth]
    deep = sdf_valid[sdf_valid['sweep_depth'] > median_depth]
    if len(shallow) > 2 and len(deep) > 2:
        print(f"  Shallow sweep (<= ${median_depth:.1f}): wr={(shallow['fwd_8'].dropna()>0).mean()*100:.1f}%, avg={shallow['fwd_8'].dropna().mean()*100:+.4f}%, n={len(shallow)}")
        print(f"  Deep sweep (> ${median_depth:.1f}): wr={(deep['fwd_8'].dropna()>0).mean()*100:.1f}%, avg={deep['fwd_8'].dropna().mean()*100:+.4f}%, n={len(deep)}")
print()

# Factor 3: Hour of day
print("-- FACTOR 3: Hour of Day (UTC) --")
for h in sorted(sdf['hour'].unique()):
    subset = sdf[sdf['hour'] == h]
    if len(subset) >= 2:
        wr = (subset['fwd_8'].dropna() > 0).mean() * 100
        avg = subset['fwd_8'].dropna().mean() * 100
        print(f"  Hour {h:02d}: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(subset)}")
print()

# Factor 4: Signal type (BUY vs SELL)
print("-- FACTOR 4: Signal Type --")
for sig_type in ['BUY', 'SELL']:
    subset = sdf[sdf['type'] == sig_type]
    if len(subset) > 2:
        wr = (subset['fwd_8'].dropna() > 0).mean() * 100
        avg = subset['fwd_8'].dropna().mean() * 100
        print(f"  {sig_type}: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(subset)}")
print()

# Factor 5: Body ratio (how big the displacement candle was)
print("-- FACTOR 5: Displacement Candle Body Size --")
sdf_body = sdf[sdf['body_ratio'].notna() & (sdf['body_ratio'] > 0)]
if len(sdf_body) > 5:
    median_body = sdf_body['body_ratio'].median()
    small_body = sdf_body[sdf_body['body_ratio'] <= median_body]
    big_body = sdf_body[sdf_body['body_ratio'] > median_body]
    if len(small_body) > 2 and len(big_body) > 2:
        print(f"  Small body (<= {median_body:.2f}x avg): wr={(small_body['fwd_8'].dropna()>0).mean()*100:.1f}%, avg={small_body['fwd_8'].dropna().mean()*100:+.4f}%, n={len(small_body)}")
        print(f"  Big body (> {median_body:.2f}x avg): wr={(big_body['fwd_8'].dropna()>0).mean()*100:.1f}%, avg={big_body['fwd_8'].dropna().mean()*100:+.4f}%, n={len(big_body)}")
print()

# Factor 6: Bars since sweep (immediate vs delayed displacement)
print("-- FACTOR 6: Bars Between Sweep and Signal --")
for b in sorted(sdf['bars_since_sweep'].unique()):
    subset = sdf[sdf['bars_since_sweep'] == b]
    if len(subset) >= 2:
        wr = (subset['fwd_8'].dropna() > 0).mean() * 100
        avg = subset['fwd_8'].dropna().mean() * 100
        print(f"  {b} bars after sweep: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(subset)}")
print()

# Factor 7: Distance from range boundary at signal time
print("-- FACTOR 7: Distance from Breakout Level at Entry --")
# For BUY signals: how far below range high is the entry?
buys = sdf[sdf['type'] == 'BUY']
if 'dist_from_range_high' in buys.columns and len(buys) > 3:
    median_dist = buys['dist_from_range_high'].median()
    close = buys[buys['dist_from_range_high'] <= median_dist]
    far = buys[buys['dist_from_range_high'] > median_dist]
    if len(close) > 1 and len(far) > 1:
        print(f"  BUY close to range high (<= ${median_dist:.1f}): wr={(close['fwd_8'].dropna()>0).mean()*100:.1f}%, n={len(close)}")
        print(f"  BUY far from range high (> ${median_dist:.1f}): wr={(far['fwd_8'].dropna()>0).mean()*100:.1f}%, n={len(far)}")
print()

# ============================================================
# MFE / MAE ANALYSIS (trade quality)
# ============================================================

print("=" * 70)
print("MFE / MAE ANALYSIS (how far trades go in each direction)")
print("=" * 70)

for n in [4, 8, 12]:
    mfe_col = f'MFE_up_{n}'
    mae_col = f'MFE_down_{n}'
    if mfe_col in sdf.columns:
        mfe = sdf[mfe_col].dropna()
        mae = sdf[mae_col].dropna()
        if len(mfe) > 0:
            print(f"  {n}-bar window:")
            print(f"    Avg MFE (favorable): {mfe.mean():.4f}%")
            print(f"    Avg MAE (adverse):   {mae.mean():.4f}%")
            print(f"    MFE/MAE ratio:       {mfe.mean()/mae.mean():.2f}" if mae.mean() > 0 else "    MAE is 0")
            print()

# ============================================================
# BEST FILTER COMBINATION
# ============================================================

print("=" * 70)
print("TESTING FILTER COMBINATIONS")
print("=" * 70)

# Test combinations of the best factors
conditions = {
    'range_small': sdf['range_size'] <= 35,
    'range_medium': sdf['range_size'] <= 50,
    'hour_london': sdf['hour'].isin([7, 8, 9, 10, 11, 12]),
    'hour_ny': sdf['hour'].isin([13, 14, 15, 16, 17]),
    'hour_active': sdf['hour'].isin([7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]),
    'immediate': sdf['bars_since_sweep'] <= 2,
    'delayed': sdf['bars_since_sweep'] >= 3,
    'buy_only': sdf['type'] == 'BUY',
    'sell_only': sdf['type'] == 'SELL',
}

# Test individual conditions
print("\nSingle filters:")
for name, mask in conditions.items():
    subset = sdf[mask]
    if len(subset) >= 3:
        fwd = subset['fwd_8'].dropna()
        if len(fwd) > 0:
            wr = (fwd > 0).mean() * 100
            avg = fwd.mean() * 100
            print(f"  {name:20s}: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(subset)}")

# Test pairs
print("\nBest pairs:")
import itertools
pairs = list(itertools.combinations(conditions.keys(), 2))
results = []
for c1, c2 in pairs:
    mask = conditions[c1] & conditions[c2]
    subset = sdf[mask]
    if len(subset) >= 3:
        fwd = subset['fwd_8'].dropna()
        if len(fwd) > 0:
            wr = (fwd > 0).mean() * 100
            avg = fwd.mean() * 100
            results.append((avg, wr, len(subset), f"{c1} + {c2}"))

results.sort(key=lambda x: x[0], reverse=True)
for avg, wr, n, name in results[:15]:
    flag = " <<< EDGE" if avg > 0.01 and wr > 52 else ""
    print(f"  {name:45s}: wr={wr:.1f}%, avg={avg:+.4f}%, n={n}{flag}")

# Test triples
print("\nBest triples:")
triples = list(itertools.combinations(conditions.keys(), 3))
results3 = []
for c1, c2, c3 in triples:
    mask = conditions[c1] & conditions[c2] & conditions[c3]
    subset = sdf[mask]
    if len(subset) >= 3:
        fwd = subset['fwd_8'].dropna()
        if len(fwd) > 0:
            wr = (fwd > 0).mean() * 100
            avg = fwd.mean() * 100
            results3.append((avg, wr, len(subset), f"{c1} + {c2} + {c3}"))

results3.sort(key=lambda x: x[0], reverse=True)
for avg, wr, n, name in results3[:10]:
    flag = " <<< EDGE" if avg > 0.01 and wr > 52 else ""
    print(f"  {name:60s}: wr={wr:.1f}%, avg={avg:+.4f}%, n={n}{flag}")

print()
print("=" * 70)
print("ANALYSIS COMPLETE")
print("=" * 70)
print()
print("Look for combinations with:")
print("  1. Win rate > 52%")
print("  2. Positive average return")
print("  3. Sample size >= 5 (ideally 10+)")
print("  4. These are the filters to implement in the indicator")
