### SPY unit
SPY T1 — unit 1:1 (as traded) through the live filters on 2026-09-03
  reverted episodes considered : 4489
     rejected, theory gate                  3579
     rejected, cheapest leg                 95
     rejected, max loss > 2.5% equity       76
     rejected, coverage ratio               67
  survived every filter        : 672

  total       8,497.80   mean      12.65   median       2.00   win 66%
  worst        -150.00   best     696.00
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

  total       6,048.80   mean      15.67   median       1.00   win 57%
  worst        -422.00   best     696.00
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

  total      35,293.40   mean      52.52   median       4.00   win 66%
  worst        -366.00   best   2,510.00
  short contracts per 1 long: median 1, max 1
  size multiple applied     : median 10x, max 10x
  max loss per trade        : median $20, cap $2,500

