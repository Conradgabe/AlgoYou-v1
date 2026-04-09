"""
Test whether specific time windows (Silver Bullet, Judas Swing, etc.)
show statistical edge on XAUUSD 5-minute data.
Also test FVG fill rates and order block holding rates by time window.
"""
import yfinance as yf
import pandas as pd
import numpy as np

print("Fetching XAUUSD 5m data (60 days)...")
gold = yf.download('GC=F', period='60d', interval='5m', progress=False)
gold.columns = gold.columns.get_level_values(0)
print(f"Records: {len(gold)}")
print(f"Range: {gold.index[0]} to {gold.index[-1]}")
print()

gold['Hour'] = gold.index.hour
gold['Minute'] = gold.index.minute
gold['HourMin'] = gold['Hour'] * 100 + gold['Minute']
gold['Body'] = abs(gold['Close'] - gold['Open'])
gold['Range'] = gold['High'] - gold['Low']
gold['Bullish'] = gold['Close'] > gold['Open']
gold['Bearish'] = gold['Close'] < gold['Open']
gold['AvgBody'] = gold['Body'].rolling(60).mean()
gold['AvgRange'] = gold['Range'].rolling(60).mean()
gold['Date'] = gold.index.date

# Forward returns
for n in [1, 2, 3, 4, 6, 8, 12]:
    gold[f'Fwd_{n}'] = gold['Close'].shift(-n) / gold['Close'] - 1

# ============================================================
# TEST 1: SILVER BULLET TIME WINDOWS
# Does the DIRECTION of the first big move during these windows predict continuation?
# ============================================================

# Silver Bullet windows in UTC (EST+4 during EDT, EST+5 during EST)
# Using EDT (March-November): EST + 4 = UTC
# London SB: 3-4 AM EST = 7-8 AM UTC  (hours 7)
# NY AM SB:  10-11 AM EST = 14-15 UTC  (hours 14)
# NY PM SB:  2-3 PM EST = 18-19 UTC    (hours 18)

print("=" * 70)
print("TEST 1: SILVER BULLET TIME WINDOWS — Direction Prediction")
print("=" * 70)

windows = {
    'London SB (07-08 UTC)': [7],
    'NY AM SB (14-15 UTC)': [14],
    'NY PM SB (18-19 UTC)': [18],
    'London Full (07-12 UTC)': [7, 8, 9, 10, 11],
    'NY Full (13-17 UTC)': [13, 14, 15, 16],
    'Tokyo (02-06 UTC)': [2, 3, 4, 5],
    'Quiet (22-01 UTC)': [22, 23, 0, 1],
}

for name, hours in windows.items():
    subset = gold[gold['Hour'].isin(hours)]
    if len(subset) < 20:
        continue

    # What's the average absolute move and directional bias?
    avg_ret = subset['Fwd_1'].dropna().mean() * 100
    avg_abs = subset['Fwd_1'].dropna().abs().mean() * 100
    bull_pct = subset['Bullish'].mean() * 100
    avg_range = subset['Range'].mean()

    # For 4-bar and 8-bar holds
    wr_4 = (subset['Fwd_4'].dropna() > 0).mean() * 100
    wr_8 = (subset['Fwd_8'].dropna() > 0).mean() * 100
    avg_4 = subset['Fwd_4'].dropna().mean() * 100
    avg_8 = subset['Fwd_8'].dropna().mean() * 100

    print(f"\n  {name} (n={len(subset)}):")
    print(f"    Avg range/bar: ${avg_range:.2f}")
    print(f"    1-bar: bias={avg_ret:+.4f}%, abs={avg_abs:.4f}%, bull%={bull_pct:.1f}%")
    print(f"    4-bar: avg={avg_4:+.4f}%, wr={wr_4:.1f}%")
    print(f"    8-bar: avg={avg_8:+.4f}%, wr={wr_8:.1f}%")

# ============================================================
# TEST 2: FVG DETECTION AND FILL RATES
# How often do FVGs get filled? Does time window matter?
# ============================================================

print("\n" + "=" * 70)
print("TEST 2: FAIR VALUE GAP FILL RATES BY TIME WINDOW")
print("=" * 70)

# Detect FVGs: bullish FVG = candle[2].high < candle[0].low (gap up)
gold['BullFVG'] = gold['Low'] > gold['High'].shift(2)
gold['BearFVG'] = gold['High'] < gold['Low'].shift(2)

# For each FVG, check if it gets filled within N bars
fvg_results = []
for i in range(2, len(gold) - 20):
    row = gold.iloc[i]

    if row['BullFVG']:
        fvg_top = row['Low']
        fvg_bottom = gold.iloc[i-2]['High']
        fvg_mid = (fvg_top + fvg_bottom) / 2

        # Check if price returns to fill the gap within N bars
        filled_4 = False
        filled_8 = False
        filled_12 = False
        for j in range(1, min(13, len(gold) - i)):
            future = gold.iloc[i + j]
            if future['Low'] <= fvg_mid:
                if j <= 4: filled_4 = True
                if j <= 8: filled_8 = True
                filled_12 = True
                break

        # After filling, does price bounce (meaning FVG acted as support)?
        bounce = False
        if filled_12:
            # Check if price goes UP after touching the FVG
            for j in range(1, min(13, len(gold) - i)):
                future = gold.iloc[i + j]
                if future['Low'] <= fvg_mid:
                    # Check next 4 bars after fill
                    if i + j + 4 < len(gold):
                        post_fill = gold.iloc[i+j:i+j+5]['Close']
                        bounce = post_fill.iloc[-1] > post_fill.iloc[0]
                    break

        fvg_results.append({
            'type': 'BULL_FVG',
            'hour': row.name.hour,
            'size': fvg_top - fvg_bottom,
            'filled_4': filled_4,
            'filled_8': filled_8,
            'filled_12': filled_12,
            'bounce': bounce,
            'body_ratio': row['Body'] / row['AvgBody'] if row['AvgBody'] > 0 else 0
        })

    if row['BearFVG']:
        fvg_top = gold.iloc[i-2]['Low']
        fvg_bottom = row['High']
        fvg_mid = (fvg_top + fvg_bottom) / 2

        filled_4 = False
        filled_8 = False
        filled_12 = False
        for j in range(1, min(13, len(gold) - i)):
            future = gold.iloc[i + j]
            if future['High'] >= fvg_mid:
                if j <= 4: filled_4 = True
                if j <= 8: filled_8 = True
                filled_12 = True
                break

        rejection = False
        if filled_12:
            for j in range(1, min(13, len(gold) - i)):
                future = gold.iloc[i + j]
                if future['High'] >= fvg_mid:
                    if i + j + 4 < len(gold):
                        post_fill = gold.iloc[i+j:i+j+5]['Close']
                        rejection = post_fill.iloc[-1] < post_fill.iloc[0]
                    break

        fvg_results.append({
            'type': 'BEAR_FVG',
            'hour': row.name.hour,
            'size': fvg_top - fvg_bottom,
            'filled_4': filled_4,
            'filled_8': filled_8,
            'filled_12': filled_12,
            'bounce': rejection,
            'body_ratio': row['Body'] / row['AvgBody'] if row['AvgBody'] > 0 else 0
        })

fvg_df = pd.DataFrame(fvg_results)
print(f"\nTotal FVGs detected: {len(fvg_df)}")
print(f"  Bullish: {len(fvg_df[fvg_df['type']=='BULL_FVG'])}")
print(f"  Bearish: {len(fvg_df[fvg_df['type']=='BEAR_FVG'])}")

# Overall fill rates
for ftype in ['BULL_FVG', 'BEAR_FVG']:
    sub = fvg_df[fvg_df['type'] == ftype]
    if len(sub) > 5:
        print(f"\n  {ftype}:")
        print(f"    Fill rate (4 bars): {sub['filled_4'].mean()*100:.1f}%")
        print(f"    Fill rate (8 bars): {sub['filled_8'].mean()*100:.1f}%")
        print(f"    Fill rate (12 bars): {sub['filled_12'].mean()*100:.1f}%")
        print(f"    Bounce/reject after fill: {sub['bounce'].mean()*100:.1f}%")

# FVG fill rates by time window
print("\n  FVG Bounce Rate by Hour (after fill, does price continue in FVG direction?):")
for h in sorted(fvg_df['hour'].unique()):
    sub = fvg_df[fvg_df['hour'] == h]
    if len(sub) >= 5:
        bounce_rate = sub['bounce'].mean() * 100
        fill_rate = sub['filled_8'].mean() * 100
        print(f"    Hour {h:02d}: fill_rate={fill_rate:.1f}%, bounce_rate={bounce_rate:.1f}%, n={len(sub)}")

# ============================================================
# TEST 3: MARKET STRUCTURE SHIFT (BOS) + FVG RETEST
# The Silver Bullet entry: BOS on 5m, then enter on FVG retest
# ============================================================

print("\n" + "=" * 70)
print("TEST 3: BOS + FVG RETEST (Silver Bullet Entry)")
print("=" * 70)

# Detect swing highs/lows with lookback of 3
gold['SwingHigh'] = (gold['High'] > gold['High'].shift(1)) & \
                    (gold['High'] > gold['High'].shift(2)) & \
                    (gold['High'] > gold['High'].shift(-1)) & \
                    (gold['High'] > gold['High'].shift(-2))

gold['SwingLow'] = (gold['Low'] < gold['Low'].shift(1)) & \
                   (gold['Low'] < gold['Low'].shift(2)) & \
                   (gold['Low'] < gold['Low'].shift(-1)) & \
                   (gold['Low'] < gold['Low'].shift(-2))

# BOS: close breaks above recent swing high or below recent swing low
var_swing_high = np.nan
var_swing_low = np.nan
bos_bull = []
bos_bear = []

for i in range(len(gold)):
    if gold.iloc[i]['SwingHigh']:
        var_swing_high = gold.iloc[i]['High']
    if gold.iloc[i]['SwingLow']:
        var_swing_low = gold.iloc[i]['Low']

    is_bull_bos = not np.isnan(var_swing_high) and gold.iloc[i]['Close'] > var_swing_high and \
                  (i == 0 or gold.iloc[i-1]['Close'] <= var_swing_high)
    is_bear_bos = not np.isnan(var_swing_low) and gold.iloc[i]['Close'] < var_swing_low and \
                  (i == 0 or gold.iloc[i-1]['Close'] >= var_swing_low)

    bos_bull.append(is_bull_bos)
    bos_bear.append(is_bear_bos)

gold['BOS_Bull'] = bos_bull
gold['BOS_Bear'] = bos_bear

# For each BOS, check if there's an FVG behind it, then if price retests the FVG
sb_signals = []

for i in range(5, len(gold) - 20):
    row = gold.iloc[i]
    hour = row.name.hour

    if row['BOS_Bull']:
        # Look for bullish FVG in the last 5 bars
        for k in range(max(0, i-5), i):
            if gold.iloc[k]['BullFVG']:
                fvg_top = gold.iloc[k]['Low']
                fvg_bottom = gold.iloc[k-2]['High'] if k >= 2 else gold.iloc[k]['Low'] - 1
                fvg_mid = (fvg_top + fvg_bottom) / 2

                # Check if price retests this FVG within 8 bars
                for j in range(1, min(9, len(gold) - i)):
                    future = gold.iloc[i + j]
                    if future['Low'] <= fvg_top and future['Close'] > fvg_bottom:
                        # Entry on FVG retest! Track forward return
                        entry_price = fvg_mid
                        if i + j + 8 < len(gold):
                            fwd_4 = (gold.iloc[i+j+4]['Close'] - entry_price) / entry_price * 100
                            fwd_8 = (gold.iloc[i+j+8]['Close'] - entry_price) / entry_price * 100
                        else:
                            fwd_4 = np.nan
                            fwd_8 = np.nan

                        sb_signals.append({
                            'type': 'BUY',
                            'hour': hour,
                            'entry_hour': gold.iloc[i+j].name.hour,
                            'fvg_size': fvg_top - fvg_bottom,
                            'fwd_4': fwd_4,
                            'fwd_8': fwd_8,
                            'in_london_sb': hour == 7,
                            'in_ny_am_sb': hour == 14,
                            'in_ny_pm_sb': hour == 18,
                            'in_any_sb': hour in [7, 14, 18],
                            'body_ratio': row['Body'] / row['AvgBody'] if row['AvgBody'] > 0 else 0,
                        })
                        break
                break

    if row['BOS_Bear']:
        for k in range(max(0, i-5), i):
            if gold.iloc[k]['BearFVG']:
                fvg_top = gold.iloc[k-2]['Low'] if k >= 2 else gold.iloc[k]['High'] + 1
                fvg_bottom = gold.iloc[k]['High']
                fvg_mid = (fvg_top + fvg_bottom) / 2

                for j in range(1, min(9, len(gold) - i)):
                    future = gold.iloc[i + j]
                    if future['High'] >= fvg_bottom and future['Close'] < fvg_top:
                        entry_price = fvg_mid
                        if i + j + 8 < len(gold):
                            fwd_4 = (entry_price - gold.iloc[i+j+4]['Close']) / entry_price * 100
                            fwd_8 = (entry_price - gold.iloc[i+j+8]['Close']) / entry_price * 100
                        else:
                            fwd_4 = np.nan
                            fwd_8 = np.nan

                        sb_signals.append({
                            'type': 'SELL',
                            'hour': hour,
                            'entry_hour': gold.iloc[i+j].name.hour,
                            'fvg_size': fvg_top - fvg_bottom,
                            'fwd_4': fwd_4,
                            'fwd_8': fwd_8,
                            'in_london_sb': hour == 7,
                            'in_ny_am_sb': hour == 14,
                            'in_ny_pm_sb': hour == 18,
                            'in_any_sb': hour in [7, 14, 18],
                            'body_ratio': row['Body'] / row['AvgBody'] if row['AvgBody'] > 0 else 0,
                        })
                        break
                break

sb_df = pd.DataFrame(sb_signals)
print(f"\nTotal BOS + FVG Retest signals: {len(sb_df)}")
if len(sb_df) > 0:
    print(f"  BUY: {len(sb_df[sb_df['type']=='BUY'])}")
    print(f"  SELL: {len(sb_df[sb_df['type']=='SELL'])}")

    # Overall performance
    for n in [4, 8]:
        col = f'fwd_{n}'
        valid = sb_df[col].dropna()
        if len(valid) > 0:
            wr = (valid > 0).mean() * 100
            avg = valid.mean() * 100
            print(f"\n  {n}-bar hold: avg={avg:+.4f}%, wr={wr:.1f}%, n={len(valid)}")

    # Silver Bullet windows vs all hours
    print("\n  BY TIME WINDOW:")
    for name, mask in [
        ('Silver Bullet windows only', sb_df['in_any_sb']),
        ('London SB (hour 7)', sb_df['in_london_sb']),
        ('NY AM SB (hour 14)', sb_df['in_ny_am_sb']),
        ('NY PM SB (hour 18)', sb_df['in_ny_pm_sb']),
        ('Outside SB windows', ~sb_df['in_any_sb']),
    ]:
        sub = sb_df[mask]
        if len(sub) >= 3:
            fwd = sub['fwd_4'].dropna()
            if len(fwd) > 0:
                wr = (fwd > 0).mean() * 100
                avg = fwd.mean() * 100
                print(f"    {name:35s}: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(sub)}")

    # By signal type + window
    print("\n  BY TYPE + WINDOW:")
    for sig_type in ['BUY', 'SELL']:
        for name, mask in [
            ('Any SB', sb_df['in_any_sb']),
            ('Outside SB', ~sb_df['in_any_sb']),
        ]:
            sub = sb_df[(sb_df['type'] == sig_type) & mask]
            if len(sub) >= 3:
                fwd = sub['fwd_4'].dropna()
                if len(fwd) > 0:
                    wr = (fwd > 0).mean() * 100
                    avg = fwd.mean() * 100
                    flag = " <<< EDGE" if wr > 55 and avg > 0 else ""
                    print(f"    {sig_type} + {name:15s}: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(sub)}{flag}")

    # By BOS hour (when did the structure break?)
    print("\n  BY BOS HOUR:")
    for h in sorted(sb_df['hour'].unique()):
        sub = sb_df[sb_df['hour'] == h]
        if len(sub) >= 3:
            fwd = sub['fwd_4'].dropna()
            if len(fwd) > 0:
                wr = (fwd > 0).mean() * 100
                avg = fwd.mean() * 100
                flag = " <<<" if wr > 55 and avg > 0 else ""
                print(f"    Hour {h:02d}: wr={wr:.1f}%, avg={avg:+.4f}%, n={len(sub)}{flag}")

    # Signal frequency
    unique_dates = sb_df['hour'].count()
    days = (gold.index[-1] - gold.index[0]).days
    print(f"\n  Signal frequency: {len(sb_df)} signals over {days} days = {len(sb_df)/max(days,1):.1f} per day")

print("\n" + "=" * 70)
print("ANALYSIS COMPLETE")
print("=" * 70)
