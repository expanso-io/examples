# Quickstart

The telemetry cost demo, deployed the way you would run a real fleet: through
[Expanso Cloud](https://cloud.expanso.io). You deploy a job once to the cloud
control plane; the cloud schedules it onto your edge node and manages its
lifecycle. Here the node is your laptop, so the log streams and the dollar meter
are local while the orchestration is genuinely remote. The same job YAML lands
on a fleet of 400 the same way it lands on this one.

Three steps: install, connect to the cloud, run the demo.

## Step 0: Install

One-liners, no build step. Linux or macOS, Python 3.10+, and `make`.

```bash
# uv (provides uvx, which runs the log simulators with no install)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Expanso Edge (the data-plane agent that runs on your node)
curl -fsSL https://get.expanso.io/edge/install.sh | bash

# Expanso CLI (the control-plane client that talks to the cloud)
curl -fsSL https://get.expanso.io/cli/install.sh | sh
```

Then clone the repo:

```bash
git clone https://github.com/expanso-io/examples.git
cd examples/telemetry-cost
```

## Step 1: Connect to Expanso Cloud

```bash
make cloud-setup
```

This is a one-time, interactive onboarding. It walks you through
[cloud.expanso.io](https://cloud.expanso.io) and asks for three things:

1. A **network** you create in the console (name it `telemetry-demo`).
2. A **node token** from that network's Add Node screen, used to bootstrap your
   laptop as a registered edge node dedicated to this demo (labeled
   `demo=telemetry-cost`, in its own data dir, so it never collides with any
   other Expanso node on the machine).
3. An **API key** (`exp_ak_...`) and the network endpoint, saved as a CLI
   profile so the cloud can schedule jobs onto your node.

**What you are doing and why.** Expanso splits into a control plane and a data
plane. The control plane is Expanso Cloud: it holds your jobs and decides which
nodes run them. The data plane is the edge node: it runs the pipeline next to
where the data is. `make cloud-setup` registers your laptop as one node in that
data plane and points the CLI at the control plane. After this, you deploy to
the cloud and the cloud places the work on your node. You never SSH the node.

When it finishes, the demo profile is recorded in `.demo-cloud.env`
(gitignored). Setup will not touch or guess any other profile you already have.

## Step 2: Run the demo

```bash
make demo
```

What you see:

- Jobs scheduled **by Expanso Cloud** onto your node. The runner prints the
  deploy returns and the `execution list` rows so you watch the control plane
  place each pipeline on the data plane.
- The four log simulators streaming real formats (app JSON, Kubernetes CRI,
  NCSA web, CloudTrail) over TCP into the edge intake.
- The **costboard** dashboard opening at http://localhost:8090: live raw / hot /
  cold volume lanes, a reduction percentage, and a running dollar meter.
- The four scenarios in sequence, each pausing for you: **tax** (ship
  everything, meter at full rate), **audit** (prove the garbage ratio),
  **filter** (cut 30%+ without losing a queried line), **tiers** (route
  compliance data to cheap cold storage, then rehydrate a window).

Run one scenario at a time with `SCENARIO`:

```bash
make demo SCENARIO=tax      # or audit, filter, tiers
```

`make clean` stops everything the demo started and removes generated state. It
leaves your demo node and profile in place (they are cheap and reused next run).

## Recording B-roll

For a clean screen recording with no pauses for Enter:

```bash
make demo SCENARIO=filter NOPAUSE=1
```

This drives the filter arc end to end on its own timing, so you can capture the
hot lane bending below raw without a hand on the keyboard.

## Run the node somewhere else

Your laptop is the smallest possible fleet, but nothing requires the node to be
local. Bootstrap Expanso Edge on a VM, an edge box, or a server with the same
node token, and the cloud schedules the same jobs onto it. Only two things have
to match: the saved **profile** (so the CLI talks to the right network) and the
job **selector** (`demo=telemetry-cost`, so the work lands on your demo node).
The costboard and the simulators can run wherever you want to watch them.

## No cloud account yet, or offline

Skip the cloud entirely. A local Expanso Edge node in `--local` mode plays the
fleet. This is also what CI and the eval harness use.

```bash
make demo-local
```

Same scenarios, same dashboard, same numbers. No account, no network. When you
are ready for the real motion, come back to Step 1.

---

See [DEMO.md](DEMO.md) for the presenter runbook and [README.md](README.md) for
the full architecture and the four-demo claims.
