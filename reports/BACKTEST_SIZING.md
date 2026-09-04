### SPY unit
SPY T1 — unit 1:1 (as traded) through the live filters on 2026-09-03
  reverted episodes considered : 4489
     rejected, theory gate                  3579
     rejected, cheapest leg                 95
     rejected, max loss > 2.5% equity       76
     rejected, coverage ratio               67
  survived every filter        : 672

  total       9,829.00   mean      14.63   median       2.00   win 67%
  worst        -227.00   best     662.00
  short contracts per 1 long: median 1, max 1
  max loss per trade        : median $2, cap $2,500

### SPY weighted
SPY T1 — study weights (integer-rounded) through the live filters on 2026-09-03
  reverted episodes considered : 4489
     rejected, theory gate                  3579
     rejected, max loss > 2.5% equity       362
     rejected, cheapest leg                 95
     rejected, coverage ratio               67
  survived every filter        : 386

  total       8,754.00   mean      22.68   median       1.00   win 65%
  worst        -227.00   best     662.00
  short contracts per 1 long: median 2, max 6
  max loss per trade        : median $700, cap $2,500

### SPY scaled
SPY T1 — 1:1 scaled to the risk cap through the live filters on 2026-09-03
  reverted episodes considered : 4489
     rejected, theory gate                  3579
     rejected, cheapest leg                 95
     rejected, max loss > 2.5% equity       76
     rejected, coverage ratio               67
  survived every filter        : 672

  total      30,878.00   mean      45.95   median       3.00   win 67%
  worst        -227.00   best   2,456.00
  short contracts per 1 long: median 1, max 1
  size multiple applied     : median 10x, max 10x
  max loss per trade        : median $20, cap $2,500

