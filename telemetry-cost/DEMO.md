# DEMO.md: presenter runbook (Expanso Cloud)

Four demos, 3 to 5 minutes each, plus a 15 to 20 minute full arc that chains
them. Each demo maps to one post in the telemetry cost series: the tax, the
audit, the filter, the tiers.

The story is the two planes. The **control plane** is Expanso Cloud: you deploy
a job once and the cloud schedules it onto the node. The **data plane** is the
node on your laptop, where the log streams, the pipeline, and the dashboard run.
Say it out loud while you present: you never SSH the node, you deploy to the
cloud and the cloud places the work.

**What is cloud vs local, honestly.** The orchestration is genuinely remote: the
cloud decides placement and manages lifecycle. The data plane is local because
the node is your laptop, so the simulators, the costboard, the audit sample, and
the cold storage all live on this machine. On a real fleet only the node moves;
the control-plane motion is identical. The `--local` path (`make demo-local`)
swaps the cloud for a local edge node and is the offline proxy for everything
below.

**Before any demo:** have the costboard (http://localhost:8090) full screen on
the projector. The dashboard is the demo; the terminal is the prop.

## Pre-flight (60 seconds)

Run this once before you walk on stage. It checks tools plus cloud
connectivity and prints a green/red line with the exact fix for anything red.

```bash
make doctor
```

Green means: `uv`, `expanso-edge`, `expanso-cli`, `python3`, and `curl` are on
PATH, a demo profile exists, and your demo node shows **connected** on it. Red
with no profile means you have not run setup yet:

```bash
make cloud-setup      # one-time; see QUICKSTART.md
```

Then load the profile name into your shell so the manual commands below work:

```bash
source .demo-cloud.env     # sets DEMO_PROFILE
echo "$DEMO_PROFILE"       # confirm it is your demo profile, not a customer one
```

If the node row reads **connecting**, give it a few seconds and rerun
`make doctor`; it flips to connected once the agent's outbound link is up.

## Driving the demo

The simplest drive is one command:

```bash
make demo                       # all four scenarios, pauses for Enter between beats
make demo SCENARIO=filter       # a single pillar (tax | audit | filter | tiers)
make demo SCENARIO=filter NOPAUSE=1   # no pauses, for B-roll capture
```

`make demo` starts the local costboard, regenerates the selector-injected cloud
jobs (`make cloud-jobs`), deploys through your profile, prints the `execution
list` rows so the audience sees the cloud schedule each job onto the node, and
keeps the simulators streaming across scenario changes.

Each section below also gives the **manual command equivalents** so you can
drive a pillar by hand. They assume `DEMO_PROFILE` is set (the `source` line
above) and that the local costboard is up (`make board`).

---

## Demo 1: The Tax (3 min)

**The claim:** with no filtering, you pay for every byte at the door, before
anyone knows if it is worth anything. This is the control group, and for most
teams it is also the current architecture.

### Command

```bash
make demo SCENARIO=tax
```

Manual equivalent:

```bash
make board
make cloud-jobs
expanso-cli job deploy jobs/cloud/01-tax.yaml   --profile "$DEMO_PROFILE" --force
expanso-cli job deploy jobs/cloud/00-intake.yaml --profile "$DEMO_PROFILE" --force
make sims
```

### Cloud beat

Show the audience the control plane placing the job on the node:

```bash
expanso-cli job describe 01-tax --profile "$DEMO_PROFILE"
```

"I deployed to the cloud. The cloud picked the node labeled
`demo=telemetry-cost` and started the pipeline there. That is the same row you
would see for one node or four hundred."

### Talk track

1. **"These are your logs, not a demo schema."** Four real formats stream over
   TCP into one Expanso Edge intake: app JSON, Kubernetes CRI, NCSA web,
   CloudTrail. *Costboard: raw and hot lanes start climbing together.*
2. **"The meter starts at the door."** Ingest pricing charges per GB before
   anyone knows if the data is worth anything. Read the presets line out loud:
   $0.10/GB plus $1.70 per million events. *Costboard: hot cost ticking up.*
3. **"Raw equals hot."** Nothing is filtered, so the two lanes are identical,
   byte for byte. The pipeline ships the original lines, not a re-encoding.
   *Costboard: reduction stuck at 0.0%.*

### Money shot

There is no flip in demo 1, and that is the point: say it explicitly. The
reduction figure is pinned at **0%** while the dollar figure keeps moving. Let
it run in silence for five seconds.

### Reset

```bash
make reset        # zero the costboard counters, keep everything running
```

---

## Demo 2: The Audit (4 min)

**The claim:** you can prove what fraction of your ingest is garbage in about
30 minutes, using nothing but volume-versus-queries data.

### Command

```bash
make demo SCENARIO=audit
```

Manual equivalent (then wait ~60s for a sample, then run the audit):

```bash
make reset
expanso-cli job deploy jobs/cloud/02-audit.yaml  --profile "$DEMO_PROFILE" --force
expanso-cli job deploy jobs/cloud/00-intake.yaml --profile "$DEMO_PROFILE" --force
make sims
make audit
```

### Cloud beat

```bash
expanso-cli job describe 02-audit --profile "$DEMO_PROFILE"
```

The audit job tees a classified sample to `data/audit-sample.jsonl` on the node.
That file is local because the node is local; `make audit` reads it here.

### Talk track

1. **"Before you filter anything, get the number."** The audit answers one
   question: what fraction of what you ship does anyone ever query?
2. **"The pipeline parsed all four formats at the edge."** NCSA regex, CRI
   prefix plus embedded zap and klog payloads, CloudTrail by shape. The tee
   captures each line's classification: source, severity, query-likelihood.
3. **Run `make audit`.** Walk the table: volume share per pattern versus
   queried share, and the four action buckets (stop shipping, route cold,
   sample, keep hot). Be honest about the method: the query-likelihood rules
   are heuristics standing in for your SIEM's query logs. In production you run
   the same join against vendor usage data.

### Money shot

The garbage ratio line at the bottom of the audit table. Pause on it. That
percentage of the meter from demo 1 bought nothing.

### Reset

```bash
make reset
```

---

## Demo 3: The Filter (5 min) - THE demo

**The claim:** filtering at the source cuts 30%+ of volume without touching a
single log anyone queries. Applied one rule at a time so the audience sees what
each filter is worth.

### Command

```bash
make demo SCENARIO=filter
```

This walks step 1 to step 4 as in-place redeploys. Every step keeps the same job
name (`03-filter`), so each deploy replaces the running filter live.

Manual equivalent (deploy the steps in order; watch the line bend between each):

```bash
make reset
make cloud-jobs
expanso-cli job deploy jobs/cloud/00-intake.yaml      --profile "$DEMO_PROFILE" --force
expanso-cli job deploy jobs/cloud/03-filter-step1.yaml --profile "$DEMO_PROFILE" --force   # drop health checks
expanso-cli job deploy jobs/cloud/03-filter-step2.yaml --profile "$DEMO_PROFILE" --force   # + drop debug
expanso-cli job deploy jobs/cloud/03-filter-step3.yaml --profile "$DEMO_PROFILE" --force   # + dedupe crash loop
expanso-cli job deploy jobs/cloud/03-filter-step4.yaml --profile "$DEMO_PROFILE" --force   # + keep error/warn/slow, sample the rest
make sims
```

### Cloud beat

```bash
expanso-cli job describe 03-filter --profile "$DEMO_PROFILE"
```

Same job name across all four steps: the audience watches the execution roll
forward as you tighten the rules, no new job each time.

### Talk track

1. **Step 1. "Drop health checks."** `/healthz` probes in NCSA and ingress
   lines, liveness user agents, the zero-information third of the stream.
   *Costboard: hot lane bends below raw for the first time.*
2. **Step 2. "Debug does not belong in your paid pipeline."** Lowercase `debug`
   in app JSON, klog and zap debug lines inside CRI payloads. *Reduction steps
   up again.*
3. **Step 3. "Crash loops bill you fifty thousand times for one fact."** Dedupe
   on the zap caller+message signature, keep the first copies, drop the rest.
   *Reduction up another notch; event rate visibly drops.*
4. **Step 4. "Keep everything that matters, sample the rest."** 100% of errors
   and warnings in any format, 100% of slow requests, 10% of the routine rest.
   *Reduction crosses 30%.*
5. **"Check what we did NOT lose."** Every error and warning line in the source
   streams is still in the hot lane, verbatim. Volume went down; signal did not.

### Money shot

The moment after step 4 deploys: the reduction percentage flips past **30%**
while errors keep flowing into the hot lane untouched. Call both out loud, in
that order. Give each step 20 to 30 seconds of dashboard time before the next;
the bend is the demo, do not rush it.

### Reset

```bash
make reset
```

---

## Demo 4: The Tiers (4 min)

**The claim:** compliance never said keep it in hot storage. Retention-only logs
belong in object storage at about $0.023/GB-month, and you can still pull a
window back out the day the auditor calls.

### Command

```bash
make demo SCENARIO=tiers
```

Manual equivalent:

```bash
make reset
expanso-cli job deploy jobs/cloud/04-tiers.yaml  --profile "$DEMO_PROFILE" --force
expanso-cli job deploy jobs/cloud/00-intake.yaml --profile "$DEMO_PROFILE" --force
make sims
```

### Cloud beat

```bash
expanso-cli job describe 04-tiers --profile "$DEMO_PROFILE"
```

### Talk track

1. **"Compliance says keep it. It never said keep it hot."** Demo 4 routes every
   line: junk dropped at the edge, signal to the hot sink, retention-only lines
   (CloudTrail, auth-service logs, login attempts) to object storage.
   *Costboard: hot and cold lanes both moving, raw far above both.*
2. **"Read the two prices."** Hot at ingest pricing, cold at $0.023/GB-month.
   *Cold cost reads pennies next to the hot figure.*
3. **"Cold is not a write-only hole."** Lines land verbatim as gzip files
   partitioned by receive hour under `cold-storage/` on the node. Show the
   directory: `ls cold-storage/*/*/*/*/`.
4. **"The auditor calls."** Rehydrate a window (use the current hour, UTC):

   ```bash
   make rehydrate FROM=2026-06-10T19:00:00Z TO=2026-06-10T20:00:00Z GREP='POST /login'
   ```

   Matching raw lines stream back out, original bytes. Honest caveat out loud:
   cold stores raw lines, so the window selects hour partitions and `GREP`
   narrows within them. The cold partitions are on the node's local disk, so
   rehydrate reads them here.

### Money shot

The cold cost figure next to the hot cost figure, then the rehydrated raw lines
scrolling. Cheap AND retrievable is the whole argument.

### Reset

```bash
make reset
```

---

## The 15 to 20 minute full arc

One continuous story for a meetup slot or a customer call. Costboard stays on
screen the entire time. Pre-flight (`make doctor`, `source .demo-cloud.env`) is
done before you start talking.

| Clock | Beat | Commands |
|---|---|---|
| 0:00-1:00 | Frame the two planes: "I deploy once to Expanso Cloud; it schedules onto the node. The node is my laptop, so the streams and the meter are local, but the orchestration is remote. Do not trust me; this runs on your log formats." | |
| 1:00-4:00 | **Demo 1, the tax.** Show `job describe 01-tax` reporting the job placed on the node. Real formats in, meter at full rate, reduction 0%. "This is the default." | `make demo SCENARIO=tax` |
| 4:00-8:00 | **Demo 2, the audit.** Parse at the edge, join volume against query patterns. | `make demo SCENARIO=audit` (wait 60s, reads the local sample) |
| 8:00-9:00 | Bridge: "We have a number. Now we go get it back." | `make reset` |
| 9:00-14:00 | **Demo 3, the filter.** Four in-place redeploys, each bends the line. Money shot at step 4: reduction past 30%, errors intact. | `make demo SCENARIO=filter` |
| 14:00-15:00 | Bridge: "What about the data you are required to keep?" | `make reset` |
| 15:00-18:30 | **Demo 4, the tiers.** Cold lane at pennies, then rehydrate a window live. | `make demo SCENARIO=tiers`, then `make rehydrate FROM=... TO=...` |
| 18:30-20:00 | Close: "Every config you watched is a standard Expanso job YAML in this repo, and the deploy was a real cloud deploy. The same files go to 400 nodes by changing one label. Repo link, series link, calculator link." | |

Pre-stage the `FROM`/`TO` timestamps for the rehydrate command in a scratch
buffer before the talk so you are not doing UTC math on stage. For a single
recorded pillar with no pauses, use `make demo SCENARIO=filter NOPAUSE=1`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make doctor` is red: no demo profile | You have not onboarded. Run `make cloud-setup`, then `source .demo-cloud.env`. |
| Node shows **connecting**, not connected | The agent's outbound link is still coming up. Wait a few seconds and rerun `make doctor`. It connects out to the cloud; nothing inbound to open. |
| Ports busy (8090, 8081, 5601, or a stale process) | `make clean`, then recheck stragglers: `pgrep -fl costboard; pgrep -fl expanso-edge; pgrep -fl logsim`. Kill anything left and rerun. |
| Sims not flowing (dashboard lanes stay at zero) | Either the streams are not running or the intake job is not deployed yet. Run `make sims`; if still flat, redeploy the scenario (it carries `jobs/cloud/00-intake.yaml`). Sim logs are in `.run/sim-*.log`. |
| First `make sims` is slow | `uvx` is pulling log-simulators from GitHub on first use. Pre-warm before the talk: `uvx --from git+https://github.com/expanso-io/log-simulators logsim-app --help`. |
| Want to confirm what the cloud placed | `expanso-cli job describe <name> --profile "$DEMO_PROFILE"` and `expanso-cli node list --profile "$DEMO_PROFILE"` (both read-only). |
| No network at the venue | Present the offline path: `make demo-local`. Same scenarios, no cloud. |
| Everything is weird, 2 minutes to showtime | `make clean && make doctor && make demo SCENARIO=tax`. If the venue network is down, `make clean && make demo-local`. |

After any demo session: `make clean`. It stops the costboard, the simulators,
the scenario jobs, and the local cold-storage state, and leaves your demo node
and profile connected (cheap to keep, reused next run). Verify with
`pgrep -fl costboard; pgrep -fl logsim` (no output means clean). `make clean`
never touches any profile other than the demo one.
