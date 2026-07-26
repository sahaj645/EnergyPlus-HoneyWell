# HIVE results

Generated 2026-07-26T22:52:11 · run period `ab_20260726T225105`

## Headline

- **Total site electricity: -9.9%** (5079.5 -> 5580.2 kWh)
- HVAC subsystem (cooling + fans + pumps electricity, labeled): -23.0% (725.9 -> 892.8 kWh)
- Cost saved: -3554.00 INR
- Carbon avoided: -335.84 kgCO2
- Peak demand reduction: +0.49 kW

## All comparisons

| comparison | site kWh Δ% | HVAC kWh Δ% | ₹ saved | kgCO2 avoided | peak kW Δ |
|---|---|---|---|---|---|
| agent vs baseline | -9.9% | -23.0% | -3554.00 | -335.84 | +0.49 |
| constant vs baseline | -9.9% | -23.0% | -3554.00 | -335.84 | +0.49 |
| agent vs constant | +0.0% | +0.0% | +0.00 | +0.00 | +0.00 |

### HVAC subsystem breakdown by meter

| comparison | cooling | fans | pumps |
|---|---|---|---|
| agent vs baseline | -21.2% | -27.7% | +0.0% |
| constant vs baseline | -21.2% | -27.7% | +0.0% |
| agent vs constant | +0.0% | +0.0% | +0.0% |

## Per-arm KPIs

| arm | site kWh | HVAC kWh | peak kW | cost INR | carbon kg |
|---|---|---|---|---|---|
| baseline | 5079.5 | 725.9 | 19.78 | 39495.54 | 2948.09 |
| constant | 5580.2 | 892.8 | 19.30 | 43049.53 | 3283.93 |
| agent | 5580.2 | 892.8 | 19.30 | 43049.53 | 3283.93 |

## Comfort violations (occupied hours, |PMV| > 0.5)

### baseline

| zone | occupied h | violation h | % of occupied | worst excursion (PMV) |
|---|---|---|---|---|
| Core_ZN | 101.00 | 5.67 | 5.6% | 0.026 |
| Perimeter_ZN_1 | 101.00 | 0.00 | 0.0% | 0.000 |
| Perimeter_ZN_2 | 101.00 | 0.00 | 0.0% | 0.000 |
| Perimeter_ZN_3 | 101.00 | 0.00 | 0.0% | 0.000 |
| Perimeter_ZN_4 | 101.00 | 0.00 | 0.0% | 0.000 |
| ALL | 505.00 | 5.67 | 1.1% | 0.026 |

### constant

| zone | occupied h | violation h | % of occupied | worst excursion (PMV) |
|---|---|---|---|---|
| Core_ZN | 101.00 | 17.83 | 17.7% | 0.103 |
| Perimeter_ZN_1 | 101.00 | 8.33 | 8.3% | 0.066 |
| Perimeter_ZN_2 | 101.00 | 1.00 | 1.0% | 0.014 |
| Perimeter_ZN_3 | 101.00 | 6.00 | 5.9% | 0.055 |
| Perimeter_ZN_4 | 101.00 | 2.17 | 2.1% | 0.023 |
| ALL | 505.00 | 35.33 | 7.0% | 0.103 |

### agent

| zone | occupied h | violation h | % of occupied | worst excursion (PMV) |
|---|---|---|---|---|
| Core_ZN | 101.00 | 17.83 | 17.7% | 0.103 |
| Perimeter_ZN_1 | 101.00 | 8.33 | 8.3% | 0.066 |
| Perimeter_ZN_2 | 101.00 | 1.00 | 1.0% | 0.014 |
| Perimeter_ZN_3 | 101.00 | 6.00 | 5.9% | 0.055 |
| Perimeter_ZN_4 | 101.00 | 2.17 | 2.1% | 0.023 |
| ALL | 505.00 | 35.33 | 7.0% | 0.103 |

## Per-day breakdown

### baseline

| date | kWh | cost INR | carbon kg | peak kW |
|---|---|---|---|---|
| 2015-05-10 | 209.99 | 1702.61 | 141.320 | 7.79 |
| 2015-05-11 | 586.17 | 4852.79 | 365.330 | 19.78 |
| 2015-05-12 | 574.79 | 4771.90 | 357.900 | 19.34 |
| 2015-05-13 | 563.63 | 4680.48 | 351.176 | 18.87 |
| 2015-05-14 | 562.91 | 4673.72 | 350.734 | 19.02 |
| 2015-05-15 | 561.06 | 4654.58 | 349.076 | 19.12 |
| 2015-05-16 | 2020.90 | 14159.46 | 1032.555 | 12.43 |

### constant

| date | kWh | cost INR | carbon kg | peak kW |
|---|---|---|---|---|
| 2015-05-10 | 318.43 | 2588.91 | 215.250 | 10.22 |
| 2015-05-11 | 609.35 | 4982.86 | 385.477 | 19.30 |
| 2015-05-12 | 610.97 | 4996.85 | 386.689 | 18.98 |
| 2015-05-13 | 599.42 | 4903.65 | 379.559 | 18.55 |
| 2015-05-14 | 598.90 | 4898.55 | 379.231 | 18.70 |
| 2015-05-15 | 595.21 | 4866.60 | 376.195 | 18.77 |
| 2015-05-16 | 2247.91 | 15812.11 | 1161.532 | 10.89 |

### agent

| date | kWh | cost INR | carbon kg | peak kW |
|---|---|---|---|---|
| 2015-05-10 | 318.43 | 2588.91 | 215.250 | 10.22 |
| 2015-05-11 | 609.35 | 4982.86 | 385.477 | 19.30 |
| 2015-05-12 | 610.97 | 4996.85 | 386.689 | 18.98 |
| 2015-05-13 | 599.42 | 4903.65 | 379.559 | 18.55 |
| 2015-05-14 | 598.90 | 4898.55 | 379.231 | 18.70 |
| 2015-05-15 | 595.21 | 4866.60 | 376.195 | 18.77 |
| 2015-05-16 | 2247.91 | 15812.11 | 1161.532 | 10.89 |

## Cumulative kWh series

Full per-arm, per-timestep cumulative series is in `results.json` (`arms.<label>.cumulative_kwh`) for the dashboard's race chart; omitted here for length.
