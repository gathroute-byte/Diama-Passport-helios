# Data sourcing notes (starting points verified 19 October 2026)

This list identifies source starting points. It is not evidence that every required variable is openly downloadable, at district level, for every year. Rights, resolution, sampling design, temporal coverage, and geography must be checked for each release before modelling.

## Yield: start with Rwanda for a tractable pilot

- **National Institute of Statistics of Rwanda (NISR), Seasonal Agricultural Survey (SAS)**: https://www.statistics.gov.rw/data-sources/surveys/Seasonal-Agricultural-Survey
  - NISR describes SAS as producing crop area, yield, and production statistics by agricultural season. Season A/B/C matter: the crop-year unit should be a district × crop × season, not just a calendar-year aggregate.
- **NISR SAS 2025 catalog record**: https://www.microdatabk.statistics.gov.rw/catalog/124
  - Metadata indicate the 2024/2025 agricultural year and crop production/yield estimates. Confirm the downloadable tables' geographic granularity. Published summaries are not automatically a 400-district-year plot panel.
- **NISR SAS 2024 Season A publication/tables**: https://gis.statistics.gov.rw/statistical-publications/agriculture-environment/seasonal-agricultural-survey-A-2024
  - A useful example of public district/season reporting, but its data granularity and historical extent must be confirmed.
- **FAOSTAT**: https://www.fao.org/faostat/en/#data/QCL
  - Useful national crop-yield series and context. National country-year data alone cannot validate district-year or within-country spatial ranking and will not meet the district-season sample design on their own.

## Weather and environmental inputs

- **CHIRPS precipitation**: https://www.chc.ucsb.edu/data/chirps
  - Rainfall dataset useful for dekadal/daily rainfall and anomaly construction. Document version, period, gauge blend, pixel-to-district aggregation and baseline used for anomalies.
- **Copernicus ERA5-Land**: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land
  - Provides land-surface and meteorological variables. It is hourly; daily Tmax, vapor pressure/VPD and root-zone soil moisture require careful extraction and unit conversion. A variable listed in ERA5-Land should not be mistaken for a directly supplied VPD product without documenting the calculation.
- **NASA MODIS vegetation indices**: https://lpdaac.usgs.gov/products/mod13q1v061/
  - MOD13Q1 provides a 16-day vegetation-index product. Apply scale factors and QA flags, spatially aggregate to the crop area/district, and document how sparse observation dates are handled. MODIS district-mean vegetation is not automatically crop-specific if other land covers are included.
- **Copernicus / AgERA5**: https://agriculture.ec.europa.eu/data-and-analysis/agri4cast/agrometeorological-data_en
  - Consider as a secondary agrometeorological source. Verify the current variables, temporal coverage, spatial resolution and access route at download time.

## Phenology dates: a hard data requirement, not a convenience

The model needs start and end dates for all six scoring windows, including `stress_exposure`. Planting, flowering onset, fill onset and harvest alone do not uniquely identify six non-overlapping windows. Crop calendars can provide broad ranges, but district-season-specific timing should preferably come from survey dates, crop-specific thermal-time, field phenology records, or carefully validated remote-sensing breakpoints. If timing is inferred, save the method, parameters and uncertainty; do not infer dates from yield outcomes.

Maize, common bean and coffee have different phenologies. Do not transfer the same calendar dates mechanically. Coffee is perennial and often has flowering/cherry-development cycles tied to rainfall and location; a single annual six-stage sequence needs explicit justification before it is compared with annual cereal crop stages.

## Proposed source plan

1. Prove the pipeline on a Rwanda maize district-season panel with public NISR yield tables and daily climate for the exact same crop seasons.
2. Establish whether historical SAS data permit district-level continuity and at least several independent years; resolve survey-design breaks and yield-unit differences before back-testing.
3. Add a source-controlled crop area mask / vegetation product so that crop condition is measured over relevant growing areas, not all district land.
4. Add district-season phenology windows and a land index with clear units/source. A repeated constant land index remains useful only as a static covariate; it is not a changing phase observation.
5. Expand to common bean and coffee only where yield series and phenology are sufficiently consistent. Do not pad a sparse coffee panel with imputed yield records to hit a target sample count.
6. Archive raw downloads, source licenses, retrieval date, transformations, and SHA-256 checksums. Save a `source_manifest.json` alongside each run.

## Known limitation

The named sources do not automatically form a finished 400+ district-year joined dataset. The actual back-test cannot truthfully be run until the harmonized panel exists. No synthetic or national-only proxy results should be labelled as East African district-level performance.

---

## Research addendum, 2026-10-19 (desk research only; no data files were downloaded)

The execution environment for this session had no outbound network for file download, so **nothing below was downloaded, hashed or joined**. These are findings from the publishers' own pages, retrieved via web search on 2026-10-19. SHA-256 hashes must be computed when files are actually retrieved.

**NISR Seasonal Agricultural Survey (SAS).** NISR publishes per-season reports (Season A, Season B, annual) with district-level tables, e.g. "Average yield by crop type and district (Kg/Ha)" and "Crop production by crop type and district (MT)". The 2025 Season A page lists a PDF report and an Excel tables workbook; the 2025 Season B and 2023/2024/2025 annual reports are PDFs. SAS 2025 Season A covered all 30 districts (1,200 segments). Season definitions: A ≈ Sep–Feb, B ≈ Mar–Jun, C ≈ Jul–Sep. Implications: district × crop × season yield outcomes appear obtainable; but (a) tables live in report PDFs/xlsx and need verified extraction; (b) the survey is sample-based, so district estimates carry sampling error not captured by the back-test; (c) series continuity and survey-design changes across years are unchecked; (d) the licence/re-use terms of the NISR site were not read in this session and must be confirmed before any redistribution.

**NASA POWER daily.** The API exposes daily `T2M_MAX` and `RH2M` (and other parameters). POWER is built on satellite and assimilation-model inputs at coarse native resolution (order of 0.5°), so many Rwandan districts will share a grid cell; district-level "weather" is therefore largely spatial interpolation of a few cells and cannot be treated as independent district observations. `RH2M` is a daily mean, so a VPD computed from `T2M_MAX` and mean RH is an approximation of the daytime deficit that the hypothesis refers to. The data are not static and may be replaced with improved versions, so retrieval date and request parameters must be recorded. POWER requests an acknowledgement in publications. Availability of a root-zone soil-moisture parameter was not verified here.

**What is still missing for a valid back-test (none of this is solvable by download alone):**
1. District-season phase windows for all six phases, fixed before outcomes are examined. No source found here publishes them; national/agro-ecological crop calendars would give planting/harvest only and would make the `flowering`, `stress_exposure` and `fill` windows assumptions, not observations.
2. A crop-specific vegetation signal (`crop_raw`): needs a crop mask; all-land NDVI is not a maize signal.
3. A time-varying `land_raw` with a documented source (a constant is only a static covariate).
4. A non-forward-looking yield-anomaly baseline, which needs several prior seasons per district.
5. Evidence that the candidate heat/low-VPD event (Tmax above the grid's 31–34 °C range with low VPD) occurs often enough in the relevant districts and windows. This was not checked; if it is rare in the highland districts, the interaction term cannot be tested there.

Until items 1–4 exist, the correct scientific status is **NOT EVALUABLE**.
