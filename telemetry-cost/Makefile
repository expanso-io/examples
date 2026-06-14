# telemetry-cost — build/run harness
#
# Expanso Cloud (cloud.expanso.io) is the orchestrator. You deploy a job once;
# the cloud schedules it onto matching edge nodes and runs its lifecycle. The
# event source is the public log-simulators suite (no install, just uvx).
#
# QUICKSTART (cloud-first — the headline path):
#   make cloud-setup   # one-time: register a demo edge node + save a profile
#   make demo          # run the guided demo THROUGH Expanso Cloud
#                      #   make demo SCENARIO=filter   # one pillar
#                      #   make demo NOPAUSE=1         # B-roll (no Enter prompts)
#   make doctor        # tools + cloud connectivity check (green/red)
#   make cloud-clean   # stop the demo node, keep the saved profile
#
# OFFLINE / LOCAL path (the escape hatch — no cloud account needed):
#   make demo-local SCENARIO=tax   # the same demo against a local edge node
#   make edge          # start a local Expanso Edge node (API on 19010)
#   make board         # start the costboard dashboard (http://localhost:8090)
#   make demo1         # deploy the tax scenario + intake pipeline (local)
#   make sims          # start the log-simulator streams feeding the edge
#   make eval          # reproduce every number in RESULTS.md
#   make clean         # kill everything, remove all generated state
#
# SAFETY: the cloud targets operate ONLY on a profile you create for this demo
# (recorded in .demo-cloud.env). They never touch any other Expanso profile.

SHELL := /bin/bash

EDGE_ENDPOINT ?= http://127.0.0.1:19010
BOARD_URL     ?= http://127.0.0.1:8090
RUN_DIR       := .run

# Where the simulators stream their lines. The intake pipeline binds a
# socket_server on this port; every logsim connects to it as a TCP client.
SINK_PORT     ?= 5601

# Deploy flags. Default targets the local edge over its API. For Expanso Cloud,
# override with a saved profile: make demo1 DEPLOY_FLAGS="--profile demo".
DEPLOY_FLAGS  ?= --endpoint $(EDGE_ENDPOINT)
DEPLOY         = expanso-cli job deploy $(1) $(DEPLOY_FLAGS) --force

# Demos auto-start a LOCAL edge node only when deploying to the default local
# endpoint. With DEPLOY_FLAGS overridden (cloud profile), the fleet is remote:
# only the costboard needs to run here.
ifeq ($(DEPLOY_FLAGS),--endpoint $(EDGE_ENDPOINT))
DEMO_DEPS := edge board
else
DEMO_DEPS := board
endif

# log-simulators source. Defaults to the public repo (uvx pulls it on demand);
# set LOGSIM_SRC to a local checkout to run fully offline.
LOGSIM_GIT    := git+https://github.com/expanso-io/log-simulators
LOGSIM_SRC    ?= $(LOGSIM_GIT)

.PHONY: help cloud-setup demo doctor demo-local cloud-jobs cloud-clean \
        edge board intake sims sims-stop fixtures \
        demo1 demo2 demo3 demo4 \
        demo3-step1 demo3-step2 demo3-step3 demo3-step4 \
        stop-scenarios check-filter-names \
        audit rehydrate test eval reset compose-jobs clean

help:
	@echo "telemetry-cost — Expanso Cloud is the orchestrator."
	@echo ""
	@echo "QUICKSTART (cloud-first):"
	@echo "  cloud-setup   one-time: register a demo edge node + save a profile"
	@echo "  demo          run the guided demo THROUGH Expanso Cloud"
	@echo "                  make demo SCENARIO=tax|audit|filter|tiers|all"
	@echo "                  make demo NOPAUSE=1   # B-roll, no Enter prompts"
	@echo "                  make demo KEEP=1      # leave board+sims+node up"
	@echo "  doctor        tools + cloud connectivity check (green/red)"
	@echo "  cloud-jobs    (re)generate jobs/cloud/*.yaml (selector-injected)"
	@echo "  cloud-clean   stop the demo node + remove .edge-cloud (keep profile)"
	@echo ""
	@echo "OFFLINE / LOCAL path (no cloud account needed):"
	@echo "  demo-local    the same guided demo against a local edge node"
	@echo "                  make demo-local SCENARIO=tax"
	@echo "  edge          start a local Expanso Edge node (API on 19010)"
	@echo "  board         start the costboard dashboard (http://127.0.0.1:8090)"
	@echo "  intake        deploy the socket_server intake/tee pipeline"
	@echo "  sims          start the log-simulator streams (-> tcp://localhost:$(SINK_PORT))"
	@echo "  sims-stop     stop the log-simulator streams"
	@echo "  fixtures      regenerate committed fixtures from log-simulators (seeded)"
	@echo "  demo1..demo4  the four pillar demos, local (tax / audit / filter / tiers)"
	@echo "  demo3-step1..demo3-step4  filter pillar, one rule at a time (local)"
	@echo "  audit         print the volume-vs-queried table from the audit sample"
	@echo "  rehydrate     FROM=<ISO> TO=<ISO> [GREP='pattern'] pull events back from cold"
	@echo "  test          run the pytest suite"
	@echo "  eval          reproduce all published numbers (writes evals/REPORT.md)"
	@echo "  reset         zero the costboard counters"
	@echo "  compose-jobs  generate jobs/compose/ variants (service hostnames)"
	@echo "  clean         kill edge+board+sims+cloud node, remove generated state"

# --- cloud-first runner entrypoints -----------------------------------------
# These four targets are thin wrappers over the cloud-first scripts. They lead
# the help and the docs; the local path below is the offline/CI escape hatch.
#   demo        the guided runner, deploying THROUGH a saved cloud profile
#   cloud-setup one-time interactive onboarding (creates .demo-cloud.env)
#   doctor      preflight: tools + cloud connectivity, green/red with fixes
#   demo-local  the same runner with --local (no profile, local edge node)
# SCENARIO / NOPAUSE / KEEP are passed through as --scenario / --no-pause /
# --keep. Profile resolution + the never-touch-a-customer-profile guard live
# in scripts/demo.sh (it refuses to guess a profile).

demo:
	@./scripts/demo.sh \
		$(if $(SCENARIO),--scenario $(SCENARIO)) \
		$(if $(NOPAUSE),--no-pause) \
		$(if $(KEEP),--keep)

cloud-setup:
	@./scripts/cloud_setup.sh

doctor:
	@./scripts/preflight.sh

demo-local:
	@./scripts/demo.sh --local \
		$(if $(SCENARIO),--scenario $(SCENARIO)) \
		$(if $(NOPAUSE),--no-pause) \
		$(if $(KEEP),--keep)

# --- cloud job variants ------------------------------------------------------
# Copy each base jobs/0*.yaml to jobs/cloud/, injecting a top-level `selector`
# (sibling of name/type/config) so Expanso Cloud schedules the job ONLY onto
# the dedicated demo node (label demo=telemetry-cost). The base jobs stay
# selector-free so they remain portable for --local. The generator preserves
# every comment (it inserts raw text after the `type:` line, no YAML round-trip)
# and validates each variant offline, printing pass/fail per file.

cloud-jobs:
	@python3 scripts/gen_cloud_jobs.py

# Stop the demo edge node and drop its data dir, but KEEP the saved profile and
# .demo-cloud.env (both cheap and reused next run). Pidfile-primary; the pkill
# fallback is pinned to THIS directory's .edge-cloud so it can never match a
# neighbor or customer node running from a different data dir.
cloud-clean:
	-@if [ -f $(RUN_DIR)/cloud-edge.pid ]; then \
		kill $$(cat $(RUN_DIR)/cloud-edge.pid) 2>/dev/null || true; \
		rm -f $(RUN_DIR)/cloud-edge.pid; \
		echo "cloud demo node stopped"; \
	fi
	-@pkill -f "expanso-edge run --data-dir $$PWD/.edge-cloud" 2>/dev/null || true
	@sleep 0.3
	rm -rf .edge-cloud
	@echo "cloud-clean: demo node stopped, .edge-cloud removed (profile + .demo-cloud.env kept)"

$(RUN_DIR):
	@mkdir -p $(RUN_DIR)

# --- long-running processes (nohup + pidfile; idempotent) -------------------

edge: | $(RUN_DIR)
	@if [ -f $(RUN_DIR)/edge.pid ] && kill -0 $$(cat $(RUN_DIR)/edge.pid) 2>/dev/null; then \
		echo "edge already running (pid $$(cat $(RUN_DIR)/edge.pid))"; \
	else \
		nohup expanso-edge run --local --api-listen 127.0.0.1:19010 \
			--data-dir ./.edge-data --no-watch > $(RUN_DIR)/edge.log 2>&1 & \
		echo $$! > $(RUN_DIR)/edge.pid; \
		echo "edge starting (pid $$(cat $(RUN_DIR)/edge.pid))"; \
	fi
	@ok=0; for i in $$(seq 1 100); do \
		curl -fsS $(EDGE_ENDPOINT)/api/v1/health > /dev/null 2>&1 && ok=1 && break; sleep 0.2; \
	done; \
	if [ $$ok -ne 1 ]; then echo "edge failed to start; see $(RUN_DIR)/edge.log"; exit 1; fi
	@echo "edge healthy at $(EDGE_ENDPOINT)"

board: | $(RUN_DIR)
	@if [ -f $(RUN_DIR)/board.pid ] && kill -0 $$(cat $(RUN_DIR)/board.pid) 2>/dev/null; then \
		echo "costboard already running (pid $$(cat $(RUN_DIR)/board.pid))"; \
	else \
		nohup python3 costboard/server.py > $(RUN_DIR)/board.log 2>&1 & \
		echo $$! > $(RUN_DIR)/board.pid; \
		echo "costboard starting (pid $$(cat $(RUN_DIR)/board.pid))"; \
	fi
	@ok=0; for i in $$(seq 1 100); do \
		curl -fsS $(BOARD_URL)/stats > /dev/null 2>&1 && ok=1 && break; sleep 0.2; \
	done; \
	if [ $$ok -ne 1 ]; then echo "costboard failed to start; see $(RUN_DIR)/board.log"; exit 1; fi
	@echo "costboard live at $(BOARD_URL)"

# --- the intake/tee pipeline -------------------------------------------------
# Replaces the old synthetic generator. jobs/00-intake.yaml binds a
# socket_server on $(SINK_PORT), then fans the RAW line out two ways:
#   - to the costboard /ingest/raw lane (the control group)
#   - to the scenario pipeline on localhost:8081/ingest
# No parsing here: the tee measures honest, on-the-wire bytes.

intake: $(DEMO_DEPS)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "intake listening on :$(SINK_PORT) — now run: make sims"

# --- the log-simulator streams ----------------------------------------------
# Four real-world formats stream over TCP into the intake socket_server:
#   logsim-app    structured JSON app logs
#   logsim-k8s    Kubernetes CRI lines (crash-loop scenario)
#   logsim-web    NCSA combined access logs
#   logsim-cloud  AWS CloudTrail JSON
# Modest rates so a demo audience can read the meter climb. Each runs under
# nohup with a pidfile in $(RUN_DIR); sims-stop (and clean) tear them down.

define START_SIM
	@if [ -f $(RUN_DIR)/sim-$(1).pid ] && kill -0 $$(cat $(RUN_DIR)/sim-$(1).pid) 2>/dev/null; then \
		echo "sim $(1) already running (pid $$(cat $(RUN_DIR)/sim-$(1).pid))"; \
	else \
		nohup uvx --from $(LOGSIM_SRC) $(2) $(3) \
			--output tcp://localhost:$(SINK_PORT) \
			> $(RUN_DIR)/sim-$(1).log 2>&1 & \
		echo $$! > $(RUN_DIR)/sim-$(1).pid; \
		echo "sim $(1) started (pid $$(cat $(RUN_DIR)/sim-$(1).pid))"; \
	fi
endef

sims: | $(RUN_DIR)
	$(call START_SIM,app,logsim-app,--rate 8)
	$(call START_SIM,k8s,logsim-k8s,--scenario crash-loop --rate 6)
	$(call START_SIM,web,logsim-web,--rate 10)
	$(call START_SIM,cloud,logsim-cloud,--rate 2)
	@echo "sims streaming to tcp://localhost:$(SINK_PORT) — watch $(BOARD_URL)"

sims-stop:
	-@for s in app k8s web cloud; do \
		if [ -f $(RUN_DIR)/sim-$$s.pid ]; then \
			kill $$(cat $(RUN_DIR)/sim-$$s.pid) 2>/dev/null || true; \
			rm -f $(RUN_DIR)/sim-$$s.pid; \
			echo "sim $$s stopped"; \
		fi; \
	done
	-@pkill -f "logsim-[a-z]* .*--output tcp://" 2>/dev/null || true

# --- fixtures ----------------------------------------------------------------
# Regenerate the committed, seeded fixtures (fixtures/{app.ndjson,k8s.log,
# web.log,cloudtrail.ndjson}). Deterministic: same seed + start-time every run.

fixtures:
	scripts/make_fixtures.sh

# --- the four pillar demos ---------------------------------------------------
# Each demo deploys the scenario pipeline FIRST (so its http_server input on
# 8081 is listening), then the intake/tee pipeline (which feeds 8081 and the
# raw lane). Then start the streams with `make sims`. Open $(BOARD_URL).
#
# Every scenario binds the same ingest port (8081) on purpose: one scenario at
# a time, intake never needs reconfiguring. stop-scenarios clears whichever
# scenario currently holds the port before the next deploy.

SCENARIO_JOBS := 01-tax 02-audit 03-filter 04-tiers

stop-scenarios:
	-@for j in $(SCENARIO_JOBS); do \
		expanso-cli job stop $$j $(DEPLOY_FLAGS) --force >/dev/null 2>&1 || true; \
	done

demo1: $(DEMO_DEPS) stop-scenarios   ## Pillar 1: ship everything — the telemetry tax
	$(call DEPLOY,jobs/01-tax.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "demo1 (tax) deployed — start the streams: make sims  (dashboard: $(BOARD_URL))"
	@echo "  cloud experience (guided, through Expanso Cloud): make demo SCENARIO=tax"

demo2: $(DEMO_DEPS)   ## Pillar 2: tap a sample lane, then 'make audit'
	$(call DEPLOY,jobs/02-audit.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "demo2 (audit) deployed — start the streams: make sims, let it collect, then: make audit"
	@echo "  cloud experience (guided, through Expanso Cloud): make demo SCENARIO=audit"

demo3: $(DEMO_DEPS)   ## Pillar 3: full edge filter (step 4 — THE demo)
	$(call DEPLOY,jobs/03-filter-step4.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "demo3 (filter, step 4) deployed — start the streams: make sims  (dashboard: $(BOARD_URL))"
	@echo "  cloud experience (guided, through Expanso Cloud): make demo SCENARIO=filter"

demo4: $(DEMO_DEPS)   ## Pillar 4: hot/cold tiering + rehydration
	$(call DEPLOY,jobs/04-tiers.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "demo4 (tiers) deployed — start the streams: make sims  (later: make rehydrate FROM=... TO=...)"
	@echo "  cloud experience (guided, through Expanso Cloud): make demo SCENARIO=tiers"

# --- demo3 step-by-step ------------------------------------------------------
# CONTRACT: jobs/03-filter-step*.yaml MUST all carry the SAME `name:` field.
# Expanso identifies jobs by name, so deploying step2 over step1 REPLACES the
# running filter in place — that's the live "tighten the rules" story.
# check-filter-names warns if the contract is broken.

check-filter-names:
	@if ls jobs/03-filter-step*.yaml > /dev/null 2>&1; then \
		n=$$(grep -h '^name:' jobs/03-filter-step*.yaml | sort -u | wc -l | tr -d ' '); \
		if [ "$$n" != "1" ]; then \
			echo "WARNING: jobs/03-filter-step*.yaml carry different 'name:' fields;"; \
			echo "         step deploys will NOT replace each other:"; \
			grep -H '^name:' jobs/03-filter-step*.yaml; \
		fi; \
	fi

demo3-step1: $(DEMO_DEPS) check-filter-names   ## drop health checks
	$(call DEPLOY,jobs/03-filter-step1.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "step 1 deployed — make sims (if not already streaming)"
	@echo "  cloud experience (guided walk of all 4 steps): make demo SCENARIO=filter"

demo3-step2: $(DEMO_DEPS) check-filter-names   ## + drop DEBUG
	$(call DEPLOY,jobs/03-filter-step2.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "step 2 deployed — make sims (if not already streaming)"
	@echo "  cloud experience (guided walk of all 4 steps): make demo SCENARIO=filter"

demo3-step3: $(DEMO_DEPS) check-filter-names   ## + crash-loop dedupe
	$(call DEPLOY,jobs/03-filter-step3.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "step 3 deployed — make sims (if not already streaming)"
	@echo "  cloud experience (guided walk of all 4 steps): make demo SCENARIO=filter"

demo3-step4: $(DEMO_DEPS) check-filter-names   ## + keep error/warn/slow, sample the rest
	$(call DEPLOY,jobs/03-filter-step4.yaml)
	$(call DEPLOY,jobs/00-intake.yaml)
	@echo "step 4 deployed — make sims (if not already streaming)"
	@echo "  cloud experience (guided walk of all 4 steps): make demo SCENARIO=filter"

# --- analysis ----------------------------------------------------------------

audit:
	python3 scripts/audit.py data/audit-sample.jsonl

rehydrate:
	@if [ -z "$(FROM)" ] || [ -z "$(TO)" ]; then \
		echo "usage: make rehydrate FROM=2026-06-10T19:00:00Z TO=2026-06-10T19:30:00Z [GREP='POST /login']"; \
		exit 2; \
	fi
	python3 scripts/rehydrate.py --from $(FROM) --to $(TO) --stats $(if $(GREP),--grep '$(GREP)')

# --- quality gates -----------------------------------------------------------

test:
	python3 -m pytest tests/ -q

eval:
	python3 evals/run_evals.py

reset:
	curl -fsS -X POST $(BOARD_URL)/reset && echo

# --- docker compose helpers --------------------------------------------------
# Generates jobs/compose/*.yaml: same pipelines, with costboard addressed by
# its compose service hostname instead of localhost. (Inside compose, the
# intake and scenario jobs run in the SAME edge container, so localhost:8081
# between them is left untouched.)

compose-jobs:
	@mkdir -p jobs/compose
	@for f in jobs/0*.yaml; do \
		[ -f "$$f" ] || continue; \
		sed -e 's#http://localhost:8090#http://costboard:8090#g' \
		    -e 's#http://127.0.0.1:8090#http://costboard:8090#g' \
		    "$$f" > jobs/compose/$$(basename $$f); \
		echo "wrote jobs/compose/$$(basename $$f)"; \
	done

# --- teardown ----------------------------------------------------------------
# Kills via pidfiles first, then a pkill fallback pinned to OUR exact process
# signatures so unrelated processes are never touched.

clean: sims-stop
	-@if [ -f $(RUN_DIR)/edge.pid ]; then kill $$(cat $(RUN_DIR)/edge.pid) 2>/dev/null || true; fi
	-@if [ -f $(RUN_DIR)/board.pid ]; then kill $$(cat $(RUN_DIR)/board.pid) 2>/dev/null || true; fi
	-@if [ -f $(RUN_DIR)/cloud-edge.pid ]; then kill $$(cat $(RUN_DIR)/cloud-edge.pid) 2>/dev/null || true; fi
	-@pkill -f "api-listen 127.0.0.1:19010" 2>/dev/null || true
	-@pkill -f "costboard/server.py" 2>/dev/null || true
	-@pkill -f "logsim-[a-z]* .*--output tcp://" 2>/dev/null || true
	-@pkill -f "expanso-edge run --data-dir $$PWD/.edge-cloud" 2>/dev/null || true
	@sleep 0.3
	rm -rf $(RUN_DIR) .edge-data* .edge-cloud data cold-storage
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "clean: processes stopped, generated state removed (.demo-cloud.env kept)"
