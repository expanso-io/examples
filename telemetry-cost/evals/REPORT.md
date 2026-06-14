# Eval Report — telemetry-cost

Generated: 2026-06-14T20:52:36Z by evals/run_evals.py
Fixtures: app.ndjson, k8s.log, web.log, cloudtrail.ndjson (18441 lines, 6,115,205 bytes, 2h seeded window via log-simulators)
Overall: **PASS**

## Numbers

| Scenario | Raw GB | Hot GB | Cold GB | Hot events | Reduction % | Est. annual $ | Result |
|---|---|---|---|---|---|---|---|
| 01-tax | 0.006134 | 0.006115 | 0.000000 | 18441 | 0.3 | 139.99 | PASS |
| 02-audit | 0.006134 | 0.000000 | 0.000000 | 0 | 100.0 | 0.00 | PASS |
| 03-filter-step1 | 0.006134 | 0.006115 | 0.000000 | 18441 | 0.3 | 139.99 | PASS |
| 03-filter-step2 | 0.006134 | 0.005762 | 0.000000 | 17529 | 6.1 | 133.04 | PASS |
| 03-filter-step3 | 0.006134 | 0.005204 | 0.000000 | 14495 | 15.2 | 110.21 | PASS |
| 03-filter-step4 | 0.006134 | 0.001042 | 0.000000 | 2882 | 83.0 | 21.92 | PASS |
| 04-tiers | 0.006134 | 0.000714 | 0.001676 | 2393 | 88.4 | 18.13 | PASS |

Est. annual $ = hot ingest cost extrapolated from the 2h fixture window to 8,760h, plus 12x the cold monthly storage cost. Prices come from presets.json (editable estimates, list prices as of June 2026): `{"hot_per_gb_ingest": 0.1, "hot_per_million_events": 1.7, "cold_per_gb_month": 0.023, "_note": "Editable estimates, list prices as of June 2026. hot_per_gb_ingest: Datadog-style per-GB log ingest. hot_per_million_events: 15-day indexing per million events. cold_per_gb_month: S3 Standard per GB-month."}`

## Reference ground truth (scripts/classify.py)

- lines by source: `{"app": 6000, "cloudtrail": 1200, "k8s": 5241, "web": 6000}`
- lines by level: `{"debug": 912, "error": 559, "info": 15060, "warn": 1910}`
- signal (error/warn/slow): 2470 (1604 after crash-loop dedupe)
- compliance-retain lines: 2585
- tiers routing: `{"hot": 2463, "cold": 2240, "dropped": 13738}`
- garbage ratio (never-queried bytes): 10.87%

## Claims

- **01-tax** — hot.events == fixture line count (18441): PASS (got 18441)
- **01-tax** — hot bytes == raw bytes (± line framing): PASS (raw 6133646, hot 6115205, diff 18441)
- **01-tax** — reduction_pct == 0: PASS (got 0.3006531514860855)
- **02-audit** — audit table renders: PASS
- **02-audit** — garbage ratio within ±3pts of classify.py reference (10.9%): PASS (got 10.7%)
- **03-filter-step4** — reduction_pct >= 30: PASS (got 83.0%)
- **03-filter-step4** — 100% app error retention (316): PASS (got 316)
- **03-filter-step4** — 100% app warn retention (570): PASS (got 570)
- **03-filter-step4** — all reference signal retained incl. slow + dedupe survivors (hot.events >= 1604): PASS (got 2882)
- **03-filter-step4** — INFO sampling is a sample, not a firehose (hot.events <= 4787): PASS (got 2882 (pool 12729))
- **04-tiers** — both tiers active, kept <= total: PASS (hot 2393, cold 2413, dropped 13635)
- **04-tiers** — hot lane within 369 of reference signal (2463): PASS (got 2393)
- **04-tiers** — cold lane within 369 of reference retain-and-quiet (2240): PASS (got 2413)
- **04-tiers** — dropped within 369 of reference (13738): PASS (got 13635)
- **04-tiers** — rehydrate roundtrip returns exactly the cold lane (2413 lines): PASS (got 2413 lines)
- **harness** — raw lane metered the fixture bytes (6,115,205 ± framing): PASS (diff 18441)
