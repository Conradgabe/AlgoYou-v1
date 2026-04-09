"""
XAUUSD 15-Minute Pattern Analysis
Statistically tests which patterns have real edge on gold scalping timeframe.
"""
import yfinance as yf
import pandas as pd
import numpy as np

# Fetch 60 days of 15m gold futures
print("Fetching XAUUSD 15m data...")
gold = yf.download('GC=F', period='60d', interval='15m', progress=False)
gold.columns = gold.columns.get_level_values(0)

print(f"\n=== XAUUSD 15m DATA ===")
print(f"Records: {len(gold)}")
print(f"Date range: {gold.index[0]} to {gold.index[-1]}")
print(f"Price range: ${gold['Low'].min():.2f} to ${gold['High'].max():.2f}")
print()

# Derived columns
gold['Return'] = gold['Close'].pct_change()
gold['Range'] = gold['High'] - gold['Low']
gold['Body'] = abs(gold['Close'] - gold['Open'])
gold['UpperWick'] = gold['High'] - gold[['Open','Close']].max(axis=1)
gold['LowerWick'] = gold[['Open','Close']].min(axis=1) - gold['Low']
gold['Bullish'] = gold['Close'] > gold['Open']
gold['Bearish'] = gold['Close'] < gold['Open']

# Forward returns (1-bar=15m, 2-bar=30m, 4-bar=1h, 8-bar=2h)
for n in [1, 2, 4, 8]:
    gold[f'Fwd_{n}'] = gold['Close'].shift(-n) / gold['Close'] - 1

# ── PATTERN 1: Consecutive candle momentum ──
gold['Bull3'] = gold['Bullish'] & gold['Bullish'].shift(1) & gold['Bullish'].shift(2)
gold['Bear3'] = gold['Bearish'] & gold['Bearish'].shift(1) & gold['Bearish'].shift(2)

print("== PATTERN 1: 3 Consecutive Candles ==")
for label, mask, direction in [
    ("3 Bull -> Long", gold['Bull3'], 1),
    ("3 Bear -> Short", gold['Bear3'], -1),
]:
    for n in [1, 2, 4, 8]:
        rets = gold.loc[mask, f'Fwd_{n}'].dropna() * direction
        if len(rets) > 5:
            print(f"  {label} {n}-bar: avg={rets.mean()*100:+.4f}%, win={((rets>0).mean()*100):.1f}%, n={len(rets)}")
    print()

# ── PATTERN 2: Big candle continuation ──
gold['AvgRange'] = gold['Range'].rolling(20).mean()
gold['BigBull'] = (gold['Range'] > gold['AvgRange'] * 2) & gold['Bullish']
gold['BigBear'] = (gold['Range'] > gold['AvgRange'] * 2) & gold['Bearish']

print("== PATTERN 2: Big Candle (>2x avg range) ==")
for label, mask, direction in [
    ("Big Bull -> Long", gold['BigBull'], 1),
    ("Big Bear -> Short", gold['BigBear'], -1),
]:
    for n in [1, 2, 4, 8]:
        rets = gold.loc[mask, f'Fwd_{n}'].dropna() * direction
        if len(rets) > 5:
            print(f"  {label} {n}-bar: avg={rets.mean()*100:+.4f}%, win={((rets>0).mean()*100):.1f}%, n={len(rets)}")
    print()

# ── PATTERN 3: Mean reversion ──
gold['SMA20'] = gold['Close'].rolling(20).mean()
gold['DistFromSMA'] = (gold['Close'] - gold['SMA20']) / gold['SMA20']
gold['Overbought'] = gold['DistFromSMA'] > 0.01
gold['Oversold'] = gold['DistFromSMA'] < -0.01

print("== PATTERN 3: Mean Reversion (>1% from 20-SMA) ==")
for label, mask, direction in [
    ("Oversold -> Long", gold['Oversold'], 1),
    ("Overbought -> Short", gold['Overbought'], -1),
]:
    for n in [1, 2, 4, 8]:
        rets = gold.loc[mask, f'Fwd_{n}'].dropna() * direction
        if len(rets) > 5:
            print(f"  {label} {n}-bar: avg={rets.mean()*100:+.4f}%, win={((rets>0).mean()*100):.1f}%, n={len(rets)}")
    print()

# ── PATTERN 4: Session analysis ──
gold['Hour'] = gold.index.hour

print("== PATTERN 4: Hourly Bias (UTC) ==")
print(f"  {'Hour':>4} {'AvgRet%':>10} {'AvgRange$':>10} {'Bull%':>7} {'Count':>6}")
for h in range(24):
    subset = gold[gold['Hour'] == h]
    if len(subset) > 20:
        avg_ret = subset['Return'].mean() * 100
        avg_range = subset['Range'].mean()
        bull_pct = subset['Bullish'].mean() * 100
        print(f"  {h:>4} {avg_ret:>+9.4f}% ${avg_range:>8.2f} {bull_pct:>6.1f}% {len(subset):>6}")
print()

# ── PATTERN 5: Reversal candles ──
gold['Hammer'] = (gold['LowerWick'] > gold['Body'] * 2) & gold['Bullish'] & (gold['Body'] > 0)
gold['ShootingStar'] = (gold['UpperWick'] > gold['Body'] * 2) & gold['Bearish'] & (gold['Body'] > 0)

print("== PATTERN 5: Reversal Candles ==")
for label, mask, direction in [
    ("Hammer -> Long", gold['Hammer'], 1),
    ("Shooting Star -> Short", gold['ShootingStar'], -1),
]:
    for n in [1, 2, 4, 8]:
        rets = gold.loc[mask, f'Fwd_{n}'].dropna() * direction
        if len(rets) > 5:
            print(f"  {label} {n}-bar: avg={rets.mean()*100:+.4f}%, win={((rets>0).mean()*100):.1f}%, n={len(rets)}")
    print()

# ── PATTERN 6: EMA crossover ──
gold['EMA9'] = gold['Close'].ewm(span=9).mean()
gold['EMA21'] = gold['Close'].ewm(span=21).mean()
gold['EMA_Bull_X'] = (gold['EMA9'] > gold['EMA21']) & (gold['EMA9'].shift(1) <= gold['EMA21'].shift(1))
gold['EMA_Bear_X'] = (gold['EMA9'] < gold['EMA21']) & (gold['EMA9'].shift(1) >= gold['EMA21'].shift(1))

print("== PATTERN 6: EMA 9/21 Crossover ==")
for label, mask, direction in [
    ("Bull Cross -> Long", gold['EMA_Bull_X'], 1),
    ("Bear Cross -> Short", gold['EMA_Bear_X'], -1),
]:
    for n in [1, 2, 4, 8]:
        rets = gold.loc[mask, f'Fwd_{n}'].dropna() * direction
        if len(rets) > 5:
            print(f"  {label} {n}-bar: avg={rets.mean()*100:+.4f}%, win={((rets>0).mean()*100):.1f}%, n={len(rets)}")
    print()

# ── PATTERN 7: Squeeze breakout ──
gold['LowRange'] = gold['Range'] < gold['AvgRange'] * 0.5
gold['Squeeze'] = gold['LowRange'] & gold['LowRange'].shift(1) & gold['LowRange'].shift(2)

print("== PATTERN 7: Squeeze (3 tight bars) then Breakout ==")
for n in [1, 2, 4, 8]:
    sq = gold.loc[gold['Squeeze'], f'Fwd_{n}'].dropna()
    abs_move = sq.abs().mean() * 100
    up_pct = (sq > 0).mean() * 100
    print(f"  After squeeze {n}-bar: abs_move={abs_move:.4f}%, up%={up_pct:.1f}%, n={len(sq)}")
print()

# ── PATTERN 8: RSI extremes ──
gold['RSI'] = 100 - (100 / (1 + gold['Return'].clip(lower=0).rolling(14).mean() / gold['Return'].clip(upper=0).abs().rolling(14).mean()))
gold['RSI_OB'] = gold['RSI'] > 75
gold['RSI_OS'] = gold['RSI'] < 25

print("== PATTERN 8: RSI Extremes ==")
for label, mask, direction in [
    ("RSI<25 Oversold -> Long", gold['RSI_OS'], 1),
    ("RSI>75 Overbought -> Short", gold['RSI_OB'], -1),
]:
    for n in [1, 2, 4, 8]:
        rets = gold.loc[mask, f'Fwd_{n}'].dropna() * direction
        if len(rets) > 5:
            print(f"  {label} {n}-bar: avg={rets.mean()*100:+.4f}%, win={((rets>0).mean()*100):.1f}%, n={len(rets)}")
    print()

# ══════════════════════════════════════════════════════════════
# RANKED SUMMARY: 4-bar (1 hour) forward return
# ══════════════════════════════════════════════════════════════
print("=" * 65)
print("RANKED SUMMARY — 4-bar (1h) forward expectancy")
print("=" * 65)

patterns = {
    '3 Bull Candles -> Long':      (gold['Bull3'], 1),
    '3 Bear Candles -> Short':     (gold['Bear3'], -1),
    'Big Bull Candle -> Long':     (gold['BigBull'], 1),
    'Big Bear Candle -> Short':    (gold['BigBear'], -1),
    'Oversold (SMA) -> Long':      (gold['Oversold'], 1),
    'Overbought (SMA) -> Short':   (gold['Overbought'], -1),
    'Hammer -> Long':              (gold['Hammer'], 1),
    'Shooting Star -> Short':      (gold['ShootingStar'], -1),
    'EMA Bull Cross -> Long':      (gold['EMA_Bull_X'], 1),
    'EMA Bear Cross -> Short':     (gold['EMA_Bear_X'], -1),
    'RSI<25 -> Long':              (gold['RSI_OS'], 1),
    'RSI>75 -> Short':             (gold['RSI_OB'], -1),
}

results = []
for name, (mask, direction) in patterns.items():
    rets = gold.loc[mask, 'Fwd_4'].dropna() * direction
    if len(rets) > 10:
        avg = rets.mean() * 100
        wr = (rets > 0).mean() * 100
        results.append((avg, wr, len(rets), name))

results.sort(key=lambda x: x[0], reverse=True)

print(f"{'Pattern':<35} {'AvgRet':>8} {'WinRate':>8} {'Count':>6}")
print("-" * 65)
for avg, wr, n, name in results:
    flag = " <<< EDGE" if avg > 0.01 and wr > 52 else ""
    print(f"{name:<35} {avg:>+7.4f}% {wr:>6.1f}% {n:>6}{flag}")

print()
print("<<< EDGE = positive avg return AND >52% win rate on 4-bar hold")
print("These are the patterns worth building a strategy around.")
