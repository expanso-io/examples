# DESIGN CONTRACTS — demo-telemetry-cost
## Internal build contracts. All components MUST conform. (This file ships in the repo; keep it clean.)

**Repo identity:** published as `expanso-io/demo-telemetry-cost` (Apache 2.0, public).
Working dir: `~/code/noisemaker`. The four demos map to the four pillars of the
telemetry cost series: Tax, Audit, Filter, Tiers.

**What it demos:** Expanso Edge pipelines (run locally or via Docker) managed the same
way you'd manage a fleet through Expanso Cloud (cloud.expanso.io). Every pipeline is a
standard Expanso job YAML: deploy locally with `expanso-cli job deploy --endpoint
http://127.0.0.1:19010`, or to a real fleet through Expanso Cloud. Always describe
the engine as Expanso Edge in code, configs, comments, and docs.

---

## Verified facts (do not re-derive; verified against expanso-edge v2.1.17 on 2026-06-10)

- Local run: `expanso-edge run --local --api-listen 127.0.0.1:19010 --data-dir ./.edge-data --no-watch`
  (NOT `expanso-edge run --config pipeline.yaml` — on v2.1.17 `--config` is the NODE config.)
- Deploy: `expanso-cli job deploy jobs/01-tax.yaml --endpoint http://127.0.0.1:19010 --force`
- Validate: `expanso-cli job validate <file> --offline` → exits 0 on pass.
- Job format (required wrapper):
  ```yaml
  name: my-job
  description: ...
  type: pipeline
  config:
    input: {...}
    pipeline: {processors: [...]}
    output: {...}
  ```
- Bloblang gotcha: a `mapping` processor that only assigns `root.x` REPLACES the
  document with `{x: ...}`. Every enriching mapping must begin `root = this`.
- Docker image: `ghcr.io/expanso-io/expanso-edge:latest` (also `:nightly`).
- Components confirmed available: inputs `generate`, `file`, `http_server`; outputs
  `stdout`, `file`, `http_client`, `aws_s3`, `broker` (pattern: fan_out), `switch`;
  processors `mapping`; functions `random_int`, `counter()`, `now()`, `deleted()`,
  `parse_json()`, metadata `@message_number`, `meta()`.
- ALWAYS verify any other component/field syntax against `~/.expanso-docs/llm.txt`
  (grep it) before use. Port 9010 and 9011 are TAKEN on the dev machine; we use
  19010 (edge API) and 8090/8081 below.

## Ports & endpoints

| Component | Port | Endpoints |
|---|---|---|
| costboard (Python, stdlib only) | 8090 | `POST /ingest/raw`, `POST /ingest/hot`, `POST /ingest/cold`, `GET /stats`, `POST /reset`, `GET /` (dashboard) |
| scenario pipeline http_server input | 8081 | `POST /ingest` |
| expanso-edge local API | 19010 | (managed by edge) |

Ingest endpoints accept either a single JSON event or newline-delimited JSON batches;
costboard counts **bytes of the request body** and event count per lane. `/stats` returns:

```json
{
  "raw":  {"bytes": 0, "events": 0},
  "hot":  {"bytes": 0, "events": 0, "events_by_level": {"ERROR": 0}, "cost_usd": 0.0},
  "cold": {"bytes": 0, "events": 0, "cost_usd_month": 0.0},
  "reduction_pct": 0.0,           // 1 - hot.bytes/raw.bytes, 0 if raw==0
  "presets": {...active preset...},
  "started_at": "ISO8601"
}
```

`POST /ingest/cold` ALSO appends gzip JSONL to `cold-storage/YYYY/MM/DD/HH/events-<HH>.jsonl.gz`
(partitioned by event `ts`, falling back to wall clock).

## Pricing presets — `presets.json`

Clearly labeled "editable estimates, list prices as of June 2026". Keys:
`hot_per_gb_ingest` (default 0.10, Datadog-style ingest), `hot_per_million_events`
(default 1.70, 15-day indexing), `cold_per_gb_month` (default 0.023, S3 standard).
Hot cost = bytes/GB * hot_per_gb_ingest + events/1M * hot_per_million_events.
Cold cost = bytes/GB * cold_per_gb_month (labeled "per month"). Costboard accepts
`--presets path` and `PRESETS_FILE` env.

## Event schema (generator → pipelines)

```json
{
  "ts": "2026-06-10T19:00:00.123Z",
  "site": "site-03",                  // 8 sites
  "service": "payments-api",          // 6 services
  "level": "DEBUG|INFO|WARN|ERROR",
  "kind": "app|health|debug|heartbeat|error|crashloop",
  "message": "human-ish log line",
  "http_target": "/health" ,          // present on health/app kinds
  "duration_ms": 12,                  // app/error kinds
  "dup_key": "svc:err:hash",          // crashloop only, identical within a burst
  "ever_queried": false,              // synthetic audit ground truth
  "compliance": "retain|none"         // retain = must keep for audit, not in SIEM
}
```

Fixture mix (scripts/make_fixtures.py, seeded `--seed 42`, default 20,000 events
spread over a synthetic 2-hour window; committed at fixtures/events.jsonl):
~25% health checks, ~20% DEBUG noise, ~8% heartbeats, ~2% ERROR, ~1.5% crashloop
duplicates in bursts (same dup_key), rest INFO/WARN app logs. `ever_queried` true for
ERROR/WARN + a small slice of INFO patterns (~12% of total). `compliance: retain` for
~30% (auth/payment/access patterns). These ratios are the eval ground truth.

## The four demos (jobs/ directory, all job-wrapped)

- `jobs/00-generator.yaml` — live generator: `generate` input (interval 20ms) producing
  the schema above via Bloblang; output `broker` fan_out → http_client POST
  `http://localhost:8090/ingest/raw` AND http_client POST `http://localhost:8081/ingest`.
- `jobs/01-tax.yaml` — input http_server :8081 `/ingest` → mapping `root = this` →
  output http_client POST costboard `/ingest/hot`. Everything ships. Meter runs.
- `jobs/02-audit.yaml` — same as 01 PLUS broker fan_out second lane: file output
  append `data/audit-sample.jsonl`. `scripts/audit.py` reads it, prints the
  volume-vs-queried table + garbage ratio (methodology of Pillar 2).
- `jobs/03-filter-step1.yaml` — drop health checks + heartbeats (`kind` in
  [health, heartbeat] → deleted()).
- `jobs/03-filter-step2.yaml` — step1 + drop DEBUG.
- `jobs/03-filter-step3.yaml` — step2 + crash-loop dedupe (keep first N of a dup_key;
  use the documented dedupe/cache mechanism if available in llm.txt, else a
  mapping+count approach; verify syntax).
- `jobs/03-filter-step4.yaml` — step3 + keep 100% ERROR/WARN and slow requests
  (duration_ms > 2000), sample 10% of remaining INFO via random_int. THE demo.
- `jobs/04-tiers.yaml` — switch output: `compliance == "retain"` AND NOT ever-needed-hot
  → costboard `/ingest/cold`; signal (ERROR/WARN/slow + queried patterns) →
  `/ingest/hot`; junk (health/heartbeat/DEBUG) → deleted in processors first.
  `scripts/rehydrate.py --from <ISO> --to <ISO> [--service X]` reads cold-storage/
  partitions and emits matching events (the auditor-calls story).

Test variants: tests programmatically rewrite the job's `input` block to
`file: {paths: [fixtures/events.jsonl], codec: lines}` + add a `parse_json()` first
processor if needed (fixtures are JSON lines; http path receives JSON too — pipelines
should tolerate both by attempting parse_json when payload is a string. Keep ONE
canonical first processor: `root = if this.type() == "string" { this.parse_json() } else { this }`
— verify `.type()` exists in llm.txt; otherwise handle via codec/content_type).

## Determinism & evals

`evals/run_evals.py` (stdlib only): starts costboard (fresh), starts local edge
(`--data-dir .edge-data-eval`), for each scenario: POST /reset, deploy file-input
variant, wait for job completed + outputs flushed (poll /stats until stable 3s),
record stats, assert:

- S1 Tax: hot.events == fixture count; reduction_pct == 0.
- S2 Audit: audit.py garbage ratio within ±3pts of fixture ground truth; table renders.
- S3 Filter step4: reduction_pct ≥ 30; hot ERROR count == fixture ERROR count (100%
  error retention); slow requests retained.
- S4 Tiers: hot contains zero `compliance=="retain" && !signal` events; cold+hot+dropped
  == total; rehydrate.py over a 30-min window returns exactly the cold events in window
  (byte-for-byte field equality after sort).

Writes `evals/REPORT.md` with the real numbers table (feeds the content posts).

## Repo layout

```
README.md LICENSE DEMO.md RESULTS.md DESIGN.md Makefile docker-compose.yml presets.json .gitignore
jobs/                  # the Expanso job YAMLs (the demos)
fixtures/events.jsonl  # committed, seeded
scripts/make_fixtures.py audit.py rehydrate.py
costboard/server.py costboard/static/index.html
tests/                 # pytest: test_validate_jobs.py test_fixtures.py test_costboard.py test_audit.py test_rehydrate.py test_integration.py
evals/run_evals.py evals/REPORT.md
.github/workflows/ci.yml
```

## Makefile targets

`make edge` (start local edge), `make board` (start costboard), `make generator`,
`make demo1..demo4`, `make demo3-step1..4`, `make audit`, `make rehydrate FROM= TO=`,
`make test`, `make eval`, `make reset`, `make clean` (kills edge+board, removes
.edge-data*, data/, cold-storage/). Demos deploy via expanso-cli against 19010.
Every target idempotent; `make clean` MUST leave no processes (pkill by exact
`api-listen 127.0.0.1:19010` and `costboard/server.py` match).

## docker-compose.yml

Services: `costboard` (python:3.12-slim, mounts costboard/ + presets.json),
`edge` (ghcr.io/expanso-io/expanso-edge:latest, `run --local --api-listen 0.0.0.0:19010`),
plus a one-shot `deploy` service (expanso-edge image has expanso-cli? if not, use
curl against edge API or ship a tiny deploy container with the CLI installer
`~/.expanso-docs/install-edge.sh` pattern — verify; simplest reliable path wins).
Inside compose, hostnames replace localhost: pipelines templated with env override
`COSTBOARD_URL` / `SCENARIO_URL` if env interpolation is supported by config
(verify `${VAR}` interpolation in llm.txt; if unsupported, ship jobs/compose/*.yaml
variants with service hostnames). Profiles: demo1..demo4.

## CI (.github/workflows/ci.yml)

ubuntu-latest: install expanso-edge + expanso-cli via official installer
(https://get.expanso.io style — verify exact URL in llm.txt or install-edge.sh),
python 3.12. Steps: validate all jobs offline → pytest → run_evals.py. If installer
needs creds/unavailable on runners, gracefully skip edge-dependent steps with a
warning (command -v guard) but ALWAYS run validation-free tests. No secrets required.

## Style rules

- Python: stdlib only (no pip installs for users), python3.10+ compatible, ruff-clean.
- Costboard dashboard: dark atmospheric design, big dollar meter, live volume lanes
  (raw/hot/cold), reduction %. NO Inter/Roboto/Arial/system fonts (use a self-hosted/
  Google font like "IBM Plex Mono"/"Space Grotesk"), no purple-gradient-on-white,
  no scrollbars. 1s polling of /stats is fine. Single HTML file, no build step.
- Comments explain the WHY of each pipeline stage (they double as demo talk track).
- Apache 2.0 headers not required per-file; LICENSE at root.
- "From the team at Expanso" in README; product pitch only in the closing section.

---

# DESIGN v2 — log-simulators integration (supersedes conflicting v1 sections)

Repo moved: this example now lives at `expanso-examples/telemetry-cost/` and is
published as part of `expanso-io/examples`. The old synthetic generator
(jobs/00-generator.yaml, scripts/make_fixtures.py) is REPLACED by the public
[expanso-io/log-simulators](https://github.com/expanso-io/log-simulators) suite.

## Event sources (all uvx, no install; seeded = byte-deterministic)

| Stream | Command core | Formats |
|---|---|---|
| app  | `logsim-app`  | JSON lines: timestamp, level (lowercase debug/info/warn/error), service, trace_id, msg, duration_ms, http{method,path,status}, user{id,email}, host, optional error{} |
| k8s  | `logsim-k8s --scenario crash-loop` | CRI lines: `<RFC3339Nano> stdout|stderr F <payload>`; payload = embedded zap JSON {level,ts,caller,msg,...} OR klog (`W0611 ...`) OR nginx-ingress access lines |
| web  | `logsim-web`  | NCSA combined: `IP - user [ts] "METHOD path HTTP/1.1" status bytes "ref" "ua"` |
| cloud| `logsim-cloud`| CloudTrail JSON lines (eventVersion, eventSource, eventName, readOnly, ...) |

Live mode: each simulator runs with `--output tcp://localhost:5601`.
Fixtures: `make fixtures` regenerates `fixtures/{app.ndjson,k8s.log,web.log,cloudtrail.ndjson}`
via uvx with `--seed 42 --count N --backfill 2h --start-time 2026-06-10T16:00:00+00:00`
(app 6000, k8s 5000 crash-loop, web 6000, cloud 1200). Committed.

## Topology (raw-byte-accurate metering)

```
logsim-* --output tcp://:5601
        v
jobs/00-intake.yaml   socket_server :5601 (codec lines) -> PURE TEE, no parsing:
                      broker fan_out -> http_client costboard /ingest/raw
                                     -> http_client localhost:8081/ingest
        v
jobs/01..04           http_server :8081 (unchanged demos): parse+classify+filter,
                      then OUTPUT THE ORIGINAL RAW LINE (byte-honest billing)
```

Scenario pipelines stash the original line first (`root._raw = content().string()`
or equivalent verified idiom), parse/classify into working fields, filter, and
final-map `root = this._raw` so hot/cold lanes carry the original bytes.
Costboard change: parse_events treats non-JSON lines as opaque events
({bytes counted, level "RAW"}). events_by_level keys are now the source values
(lowercase "error" for app logs).

## The classification mapping (one Bloblang block, shared by scenarios)

Source detection order:
1. parse_json() succeeds + has `eventVersion` -> cloudtrail (level=info, retain=true)
2. parse_json() succeeds + has `level` & `service` -> app
3. CRI prefix match (`^\d{4}-\d{2}-\d{2}T\S+ (stdout|stderr) [FP] `) -> k8s; payload
   re-parsed: embedded JSON zap -> level/caller/msg; klog W/E/I prefix -> level;
   nginx access line -> treat as web access semantics
4. NCSA regex match -> web (status>=500 error, >=400 warn, else info)
5. anything else -> unknown (keep, info)

Classified working fields: src, level, is_health (path in /health /healthz /ready
/livez /metrics or k8s liveness UA), is_debug, dup_key (k8s zap: caller+msg),
slow (app duration_ms > 2000), retain (cloudtrail OR app service=="auth" OR web
"POST /login"), queried_class for audit (error/warn/5xx/auth -> "regular",
slow -> "rare", health/debug/2xx-static -> "never", else "rare").

scripts/classify.py = the SAME rules in Python (reference implementation);
evals compare pipeline output vs reference verdicts. audit.py uses classify.py
for the pattern table + garbage ratio (no synthetic ever_queried anymore;
heuristics documented honestly as "what your query audit would tell you").

## Demos (semantic deltas only)

- 01-tax: unchanged shape; raw lines -> hot.
- 02-audit: tee parsed-envelope sample (JSON with classification fields) to
  data/audit-sample.jsonl for audit.py; hot lane still raw lines.
- 03-filter steps (names all `03-filter`): 1 drop is_health; 2 +drop is_debug;
  3 +dedupe dup_key (bare cache_resources memory form per v1 NOTE);
  4 +keep error/warn/slow, sample 10% of the rest.
- 04-tiers: retain & !signal -> cold; signal (error/warn/slow/5xx) -> hot;
  is_health/is_debug -> deleted; everything else (2xx web browsing, k8s chatter)
  -> deleted (the stop-shipping bucket).

## Eval claims v2 (run_evals.py)

- S1: hot bytes == raw bytes; reduction 0.
- S2: audit.py garbage ratio within ±3pts of classify.py reference on fixtures.
- S3 step4: reduction >= 30%; every reference-classified error/warn line present
  in hot (count match by reference; crash-loop dedupe expectation = unique
  dup_keys, not raw dup count); slow app lines retained.
- S4: zero retain&!signal lines in hot; rehydrate window roundtrip matches the
  cold partition contents (cold lane lines are raw originals; rehydrate greps
  by costboard receive-partition; --from/--to filter on partition hour).
  NOTE: rehydrate.py now matches raw lines (no per-event ts field); it filters
  by partition directory hour only, documented as such.

## Cloud-first orchestration (docs + Makefile)

README/DEMO lead with Expanso Cloud: create network at cloud.expanso.io,
`expanso-edge bootstrap --token ...`, `expanso-cli profile save demo --endpoint
<network> --api-key ... --select`, then `make demo1 EDGE_FLAGS="--profile demo"`.
Makefile: DEPLOY uses `$(DEPLOY_FLAGS)` (default `--endpoint $(EDGE_ENDPOINT)`,
overridable with `--profile <name>`). Local mode stays as the offline/CI path.
make sims / sims-stop: launch/stop the logsim tcp streams (pidfiles in .run/).

## Monorepo layout

Root: README.md (examples index), LICENSE, .github/workflows/telemetry-cost-ci.yml
(working-directory + paths filter on telemetry-cost/**). Example self-contained
in telemetry-cost/. CI installs uv (for uvx logsim fixtures) + expanso CLIs.

---

# DESIGN v3 — Cloud-first demo runner (THE framework; supersedes "cloud as override")

Expanso Cloud is the orchestrator, not an option. The headline experience deploys
pipelines THROUGH cloud.expanso.io onto a registered edge node. Local `--local`
mode survives only as the offline/CI escape hatch. Every doc, every default, leads
with Cloud.

## The mental model the demo must teach (say this out loud while recording)

- Control plane = Expanso Cloud. You deploy a job once; the cloud schedules it onto
  matching edge nodes and manages its lifecycle. You never SSH a node.
- Data plane = the edge node. Here it runs on your laptop (a real registered node,
  just nearby), so the log streams and the dashboard are local while the
  orchestration is genuinely remote. The same job YAML lands on a fleet of 400 the
  same way it lands on this one.
- The node is dedicated to the demo (label `demo=telemetry-cost`) and lives in its
  own data dir, so it never collides with any other Expanso node on the machine or
  any shared/customer network.

## Verified Cloud CLI (expanso v2.1.17 — do not re-derive)

```
expanso-edge bootstrap --token "$TOKEN" --data-dir "$PWD/.edge-cloud"
expanso-edge run --data-dir "$PWD/.edge-cloud"            # connects out to nats://<net>.cloud.expanso.io:4222
expanso-cli profile save "$PROFILE" --endpoint "$NET_ENDPOINT" --api-key "$API_KEY" --select
expanso-cli node list --profile "$PROFILE"               # READ-ONLY; shows STATE connected/connecting
expanso-cli job deploy FILE --profile "$PROFILE" --force
expanso-cli execution list --job NAME --profile "$PROFILE" --watch    # the "it landed on the node" beat
expanso-cli job logs JOB_ID --profile "$PROFILE" --follow
expanso-cli job stop NAME --profile "$PROFILE" --force
```

Node labels: written as a config.d file in the node's data dir. Exact shape
(verified against a live node):
```
# .edge-cloud/config.d/30-demo-labels.yaml
labels:
    demo: telemetry-cost
    os: macos
```

Job placement: the control plane schedules a job onto nodes matching
`spec.selector.match_labels`. Demo jobs MUST carry `selector: {match_labels:
{demo: telemetry-cost}}` so they land ONLY on the dedicated demo node, never on a
neighbor in a shared network. Base jobs in jobs/ stay selector-free (portable for
--local); the selector lives in generated jobs/cloud/ variants.

## SAFETY (hard constraints for the runner and for any agent)

- NEVER deploy to, stop jobs on, or otherwise mutate any pre-existing profile the
  user already had. Assume every saved profile other than the demo profile points
  at a real production or customer fleet. The runner operates ONLY on a profile the
  user explicitly created for this demo (recorded in .demo-cloud.env or passed as
  --profile).
- The runner must REFUSE to guess a profile. No profile + no .demo-cloud.env -> print
  the cloud-setup instructions and exit nonzero. It must never fall back to a
  customer profile or silently to local.
- Read-only cloud calls (profile list, node list) are fine anywhere.

## New files

```
scripts/demo.sh          # THE runner (cloud-first; --local escape hatch)
scripts/cloud_setup.sh   # one-time interactive Cloud onboarding
scripts/preflight.sh     # doctor: tools + cloud connectivity, green/red with fixes
jobs/cloud/*.yaml        # selector-injected variants (generated by `make cloud-jobs`)
QUICKSTART.md            # the 3-step cloud-first front door
.demo-cloud.env          # written by cloud_setup (DEMO_PROFILE=...); gitignored
```

## scripts/cloud_setup.sh (one-time, interactive, idempotent)

1. preflight.sh --tools (uv, expanso-edge, expanso-cli, python3, curl).
2. Print, then wait: "Open https://cloud.expanso.io -> create a network (e.g.
   'telemetry-demo') -> Add Node -> copy the bootstrap token." Read $TOKEN.
3. `expanso-edge bootstrap --token "$TOKEN" --data-dir "$PWD/.edge-cloud" --force`
4. Write .edge-cloud/config.d/30-demo-labels.yaml (labels demo=telemetry-cost,
   os=<uname>). Start the node: nohup expanso-edge run --data-dir "$PWD/.edge-cloud"
   (pidfile .run/cloud-edge.pid, log .run/cloud-edge.log).
5. Print: "In Expanso Cloud, open the network's API access / Settings -> copy the
   control-plane endpoint and an API key (exp_ak_...)." Read $NET_ENDPOINT, $API_KEY.
6. `expanso-cli profile save "$PROFILE" --endpoint "$NET_ENDPOINT" --api-key
   "$API_KEY"` (PROFILE defaults telemetry-demo; --select optional).
7. Poll `expanso-cli node list --profile "$PROFILE"` up to ~60s until the demo node
   shows connected. Print the node row.
8. Write .demo-cloud.env: DEMO_PROFILE="$PROFILE". Print "Setup done -> run: make demo".
Re-running detects an existing connected demo node + profile and skips to verify.

## scripts/demo.sh (the star; cloud-first)

Usage: `./scripts/demo.sh [--profile NAME] [--scenario tax|audit|filter|tiers|all]
[--local] [--no-pause] [--rate N] [--keep]`

Resolution: --profile > $DEMO_PROFILE > .demo-cloud.env. If none and not --local:
print cloud-setup guidance, exit 1 (NEVER guess).

Flow (cloud):
1. preflight.sh (must pass: tools + node connected on $PROFILE). On fail, exact fix line.
2. `make cloud-jobs` (regenerate jobs/cloud/ from base + selector). board up (local costboard).
3. Banner explaining control-plane vs data-plane (the mental model above).
4. Per scenario (guided; pause for Enter unless --no-pause):
   - reset the board, deploy jobs/cloud/<scenario>.yaml + jobs/cloud/00-intake.yaml
     via --profile, print the deploy returns.
   - show `expanso-cli execution list --job <name> --profile $PROFILE` so the
     audience sees the cloud scheduling it onto the node.
   - ensure sims running (start if not). Open $BOARD_URL (macOS `open`).
   - print the talk-track beat (the claim) + what to watch on the meter.
   - filter scenario: walk step1->4 as in-place redeploys (same job name '03-filter'),
     each one printed as a single command + "watch the line bend".
   - tiers scenario: after collecting, run `make rehydrate FROM=.. TO=..` for the
     auditor beat.
5. End: print the cleanup line. Unless --keep, leave node+profile (they are reusable);
   --keep also leaves board+sims for continued play. Default tears down board+sims+
   scenario jobs (stop via --profile) but LEAVES the demo node connected (it is cheap
   and reused next run). `--local` swaps cloud deploy for the local-edge path and skips
   all profile logic.

`--no-pause --scenario filter` is the B-roll mode for clean screen recordings.

## Makefile (cloud-first)

- `make demo` -> scripts/demo.sh (cloud). `make demo SCENARIO=filter` passes through.
- `make cloud-setup` -> scripts/cloud_setup.sh. `make doctor` -> scripts/preflight.sh.
- `make demo-local` -> scripts/demo.sh --local (the offline path; what old `make demo1..4` were).
- `make cloud-jobs` -> generate jobs/cloud/*.yaml: copy base job, inject
  `selector:\n  match_labels:\n    demo: telemetry-cost` at top level of the spec
  (sibling of name/type/config). Validate each offline.
- Keep all existing targets (edge, board, sims, demo1..4, eval, clean...). `clean`
  also stops the cloud demo node (.run/cloud-edge.pid) and removes .edge-cloud,
  but does NOT delete the saved profile or .demo-cloud.env (cheap to keep).

## Acceptance

- jobs/cloud/*.yaml all pass `expanso-cli job validate --offline`, and carry the
  selector at spec top level (NOT inside config).
- `make doctor` runs and correctly reports red when no demo profile exists.
- `./scripts/demo.sh --local --no-pause --scenario tax` runs green end-to-end
  locally (the offline proxy for the cloud path; the cloud path is identical except
  --profile replaces --endpoint and is validated offline).
- No customer profile is ever written to or deployed against by any script or agent.
