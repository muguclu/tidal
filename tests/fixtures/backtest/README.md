# Backtest golden fixtures (T-302)

Every `expected` block was derived BY HAND from SPEC-05 and the engine docstring, never by running
the engine. `result_sha256` is the only engine-produced value: it was pinned AFTER the engine matched
the hand numbers, and CI (Linux) plus local (Windows) must both reproduce it.

Rules used: signal[t] decided at close t, held from fill t+1; fill = close (1d) or open (1h);
q = 1e6 / (notional at first usable fill); gross[t] = q * pos[t-1] * (fill[t]-fill[t-1]);
cost = (fee+slip)/1e4 * |dq| * notional; equity = 1e6 + cumsum(gross - cost).
Drawdown = equity / running peak - 1. Metrics beyond n_trades / hit_rate / max_drawdown (cagr, vol,
sharpe, turnover) are computed in the test from the hand-derived equity with plain `math`.

## ma_crossover_1d (fast=1, slow=2, no costs)
close 100,100,110,120,110,100,90,100,110,120. SMA2: -,100,105,115,115,105,95,95,105,115.
sign(close-SMA2) = 0,0,+,+,-,-,-,+,+,+ (idx1 tie=0). Held pos = shifted: 0,0,0,1,1,-1,-1,-1,1,1.
q = 1e6/100 = 10000. Price steps: 0,0,+10,+10,-10,-10,-10,+10,+10,+10.
P&L per bar (q*pos_prev*step): idx4 -100k, idx5 -100k, idx6 +100k, idx7 -100k, idx8 -100k, idx9 +100k
=> equity 1.0M x4, then 0.9, 0.8, 0.9, 0.8, 0.7, 0.8 M. Trades at idx3 (+q @120), idx5 (-2q @100),
idx8 (+2q @110). Segments: long -200k, short -100k (both losers) => hit_rate 0; the third (long)
is still open => n_trades 3. Max drawdown 0.7/1.0-1 = -0.3. Turnover = (1+2+2)/(9/252).

## ma_crossover_1h_open_fill (same signal, fills at OPEN, ppy = 252*24 = 6048)
opens 100,99,108,121,112,98,92,99,111,119; steps 0,-1,+9,+13,-9,-14,-6,+7,+12,+8; q=1e4.
P&L: idx4 -90k, idx5 -140k, idx6 +60k (short, -6), idx7 -70k, idx8 -120k, idx9 +80k
=> equity 1M x4, 910k, 770k, 830k, 760k, 640k, 720k. Trades @ opens 121, 98, 111.
Short segment +60-70-120 = -130k, long -230k => hit 0.

## mean_reversion_negative_1d (lookback 3, z_entry 1, z_exit 0.2, fee 10bp, slip 5bp)
close 10,10,5,7.5,7.5,-5,-5,-2,-2,-2. z (ddof 1): idx2 -1.155 long; idx3 (10,5,7.5) sd 2.5 z=0 flat;
idx4 z=+0.577 hold flat; idx5 (7.5,7.5,-5) z=-1.155 long; idx6 z=-0.577 hold; idx7 (-5,-5,-2)
z=+1.155 short; idx8 z=+0.577 hold; idx9 zero variance => hold short.
signal 0,0,1,0,0,1,1,-1,-1,-1; held 0,0,0,1,0,0,1,1,-1,-1. q = 1e6/10 = 1e5.
Costs (15bp total): idx3 7.5e5 notional -> 750+375; idx4 same; idx6 |-5|*1e5 -> 500+250;
idx8 |-2|*2e5 -> 400+200. Gross: only idx7 (long from -5 to -2) = 1e5*3 = +300k.
Equity: 998875, 997750, 997750, 997000, 1297000, 1296400, 1296400. Negative price never clamped.
Segments: long1 = -2250 (loss), long2 = 300000-750-300 = +298950 (win), short open
=> hit 0.5, n_trades 3. Max drawdown = 997000/1e6-1 = -0.003.

## calendar_spread_1d (leg A - leg B, lookback 3, z 1/0.2, fee 20bp)
B = 20 flat, A = spread + 20, so the spread is the previous fixture's series and positions match.
q = 1e6 / (|30|+|20|) = 20000 per leg. Rows per leg per fill (+q A, -q B for a long spread).
Costs 0.002*|dq|*|price| per leg: idx3 A 1100 B 800; idx4 A 1100 B 800; idx6 A 600 B 800;
idx8 (dq=2q) A 1440 B 1600. Gross idx7 = q*3 = 60000.
Equity: 998100, 996200, 996200, 994800, 1054800, 1051760, 1051760. Max drawdown = 994800/1e6-1 = -0.0052.
