# HelioS / SEL-CCI model-review addendum

Date: 2026-10-19
Model version: `helios-v0.9-int-flower-fill`

## Status of claimed empirical results

No real district-season yield/weather/phenology panel or run-output snapshot is present in this package. Therefore no statement such as “450 district-years,” “mean Spearman lift +0.136,” or “AUC lift +0.142” can be verified from this package. The code and unit tests demonstrate pipeline mechanics only; synthetic tests are not empirical evidence. A subjective probability such as “30–45% chance the results are real” is not a reproducible statistical conclusion unless its reference class and method are specified.

Do not issue a `SUPPORTED` performance verdict until the exact panel, daily inputs, source manifest, runtime versions, complete held-out-year table, nested calibration table, sensitivity outputs, and hashes for the actual inputs are archived and independently rerun.

## Arithmetic correction to the ten-day example

The stated rule is `Tmax > 32 °C AND VPD < 1.0 kPa`.

The qualifying rows are days 3, 4, 7, 8, and 10. That is **5 days**, not 4. With base score 68.4 and `lambda = 0.4`:

`adjusted_score = 68.4 - (0.4 × 5) = 66.4`.

The pasted output saying “4 days” and “66.8” is inconsistent with its own daily flags. The row listing in that example actually marks five days as active.

The separate six-phase arithmetic example using `S_int_flow = 8` and `S_int_fill = 5` is internally consistent:

- No-interaction phase-score sum = 413.8; average = 68.9667; rounded score = 69.
- Interaction-adjusted sum = 408.6; average = 68.1; rounded score = 68.

Those are illustrative calculations, not observed crop results.

## Score direction must be explicit

The current implementation treats a high SEL-CCI score as **more favorable crop conditions** and a low score as greater loss risk. This makes subtracting an adverse interaction coherent, and ROC-AUC uses the negative of the favorability score to rank loss events.

Do not describe the output interchangeably as a “risk score” without stating the direction. If users require a score where larger means greater risk, define and document a separate transformation such as `risk_index = 100 - favorability_score`, after confirming the score range and clipping rule.

## Phase-specific input requirement

M1 is only a phenological model if at least some of its four inputs differ meaningfully by phase and those differences are built using each season's actual stage dates. If the same annual land/crop/climate/moisture values are copied into every phase, M1 reduces to a static alternative set of average weights; it does not test stage-timed risk allocation. Static land suitability can legitimately repeat, but crop condition, phase-relevant climate stress, and moisture exposure should be constructed from the corresponding phase windows where the data permit.

## Climate-feature construction warning

Do not feed raw `Tmax` into a monotonic “lower is better” transform and call the result a general climate-favorability score without justification. That transform could reward cooler days even where cold stress is damaging. Prefer a documented crop- and phase-specific temperature suitability/stress measure that can account for both lower and upper harmful extremes, or explicitly constrain the study to heat stress and label the feature accordingly. Any transformations and direction assumptions belong in the source manifest and should be fixed before the final evaluation.

## Biological status of the low-VPD interaction

The low-VPD/high-temperature term is a **candidate mechanism**, not an established, universally damaging condition for East African maize, beans, or coffee. Published maize studies report context-dependent relationships between temperature, relative humidity, VPD, pollen viability and seed set. One field study found that higher relative humidity could alleviate the heat impact on maize seed set under its experimental conditions; another reported that both high temperature and high VPD can damage maize tassel performance. These are not East African validation studies and cannot establish local yield coefficients.

Relevant references:

- Dong, X. et al. (2024). “Impacts of high temperature, relative air humidity, and vapor pressure deficit on the seed set of contrasting maize genotypes during flowering.” *Journal of Integrative Agriculture*, 23(9), 2955–2969. https://doi.org/10.1016/j.jia.2023.09.007
- Guan, L., Chen, Y. & Dong, X. (2024). “Impacts of High Temperature and Vapor Pressure Deficit on the Maize Opened Spikelet Ratio and Pollen Viability.” *Agronomy*, 14(11), 2510. https://doi.org/10.3390/agronomy14112510
- Yang, Z. et al. (2012). “Temperature effect on transpiration response of maize plants to vapour pressure deficit.” *Environmental and Experimental Botany*, 78, 157–162. https://doi.org/10.1016/j.envexpbot.2011.12.034

Keep the specified low-VPD interaction as the pre-registered M2 hypothesis. If a high-VPD or two-sided-VPD alternative is added, pre-register it as a separate candidate model and report its results separately; do not select a preferred formulation after inspecting the outer test labels.

## Outcome and validation discipline

- Use yield anomaly as the primary outcome when a properly constructed, source-documented anomaly is available. Raw yield level can make cross-district land suitability and persistent productivity differences look like temporal risk skill.
- Report `M1 − M0` to test whether phase-weighting adds value and `M2 − M1` to test whether the interaction adds value. `M2 − M0` alone conflates both effects.
- Keep the outer leave-one-year-out test untouched. Estimate scaling, interaction thresholds, lambda values, and loss-event cutoffs from training data only.
- Treat small numbers of held-out years as weak evidence even when point estimates are large. Display every held-out year, the years with negative lift, the confidence interval method, spatial-holdout results, and threshold sensitivity.
- Any “minimum district-years” or lift threshold is a design choice unless supported by a power analysis using the actual outcome distribution and clustering structure.

## Current verdict

**NOT EVALUABLE on real crop performance yet.** The package is a reproducible implementation scaffold. A real-model verdict requires the real source data and complete run artefacts. The interaction's physiological rationale is plausible enough to test, but its specific low-VPD direction and value for East African yields remain empirical questions.
