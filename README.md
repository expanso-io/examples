# Expanso Examples

Runnable, self-contained examples for [Expanso Edge](https://expanso.io) and
[Expanso Cloud](https://cloud.expanso.io). Every example is a complete project:
standard Expanso job YAMLs, a local harness, deterministic tests, and an eval
suite that reproduces every number its docs claim. Nothing here is a slideware
pipeline; if an example says it cuts volume by 30%, `make eval` proves it on
your machine.

Each example lives in its own directory and runs independently. Clone the
repo, `cd` into an example, and follow its README.

## Examples

| Example | What it shows | The claim it proves |
|---|---|---|
| [`telemetry-cost/`](telemetry-cost/) | Filtering, deduplicating, and tiering real log formats (NCSA, Kubernetes CRI, CloudTrail, JSON app logs) at the edge, with a live dollar meter | Edge filtering cuts 30%+ of observability ingest volume while retaining 100% of errors, warnings, and slow requests |

## Companion repos

| Repo | What it provides |
|---|---|
| [expanso-io/log-simulators](https://github.com/expanso-io/log-simulators) | Realistic, seeded log generators (web access, Kubernetes, CloudTrail, app JSON, syslog, firewall, and more). The examples here use them as event sources: one `uvx` command, no install. |

## Contributing

PRs welcome: open one against the example you are improving, keep its tests
and evals green, and follow the conventions in that example's CONTRIBUTING.md.

## License

[Apache 2.0](LICENSE).

From the team at [Expanso](https://expanso.io).
