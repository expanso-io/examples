"""Tests for scripts/audit.py: envelope ingestion, pattern grouping
(src + service-or-stream + level + queried_class), four-bucket
classification, garbage ratio, table rendering, the --json output, and
the --raw mode that classifies raw log lines via scripts/classify.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import audit  # noqa: E402


def make_envelope(
    src="app",
    service="payments",
    level="info",
    queried_class="rare",
    retain=False,
    raw_bytes=120,
    **extra,
):
    """A parsed envelope as the 02-audit job tees it off: classification
    fields + the original line."""
    env = {
        "src": src,
        "service": service,
        "level": level,
        "is_health": False,
        "is_debug": False,
        "slow": False,
        "retain": retain,
        "queried_class": queried_class,
        "raw_bytes": raw_bytes,
        "_raw": "x" * raw_bytes,
    }
    env.update(extra)
    return env


def write_jsonl(path: Path, envelopes) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for env in envelopes:
            fh.write(json.dumps(env) + "\n")


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    """Four patterns, one per bucket.

    - web/-/info/never (health checks, no hold)  -> STOP SHIPPING (20 x 150B)
    - cloudtrail/-/info/never (retain)           -> ROUTE COLD    (10 x 400B)
    - app/payments/info/rare (slow traces)       -> SAMPLE        (50 x 120B)
    - app/payments/error/regular                 -> KEEP HOT      ( 5 x 200B)
    """
    envs = []
    envs += [
        make_envelope("web", None, "info", "never", raw_bytes=150, is_health=True)
        for _ in range(20)
    ]
    envs += [
        make_envelope("cloudtrail", None, "info", "never", retain=True, raw_bytes=400)
        for _ in range(10)
    ]
    envs += [
        make_envelope("app", "payments", "info", "rare", raw_bytes=120, slow=True)
        for _ in range(50)
    ]
    envs += [
        make_envelope("app", "payments", "error", "regular", raw_bytes=200)
        for _ in range(5)
    ]
    path = tmp_path / "audit-sample.jsonl"
    write_jsonl(path, envs)
    return path


def analyze_file(path: Path):
    records, skipped = audit.read_envelopes([str(path)])
    assert skipped == 0
    return audit.analyze(records)


class TestBucketFor:
    def test_never_queried_no_retain_is_stop_shipping(self):
        assert audit.bucket_for("never", retain_events=0) == audit.BUCKET_STOP

    def test_never_queried_with_retain_is_route_cold(self):
        assert audit.bucket_for("never", retain_events=3) == audit.BUCKET_COLD

    def test_rarely_queried_is_sample(self):
        assert audit.bucket_for("rare", retain_events=0) == audit.BUCKET_SAMPLE

    def test_regularly_queried_is_keep_hot(self):
        assert audit.bucket_for("regular", retain_events=0) == audit.BUCKET_HOT


class TestAnalyze:
    def test_groups_by_src_service_level_class(self, sample_file):
        summary = analyze_file(sample_file)
        names = {row["pattern"] for row in summary["patterns"]}
        assert names == {
            "web/-/info/never",
            "cloudtrail/-/info/never",
            "app/payments/info/rare",
            "app/payments/error/regular",
        }

    def test_bucket_assignment(self, sample_file):
        summary = analyze_file(sample_file)
        buckets = {r["pattern"]: r["bucket"] for r in summary["patterns"]}
        assert buckets["web/-/info/never"] == audit.BUCKET_STOP
        assert buckets["cloudtrail/-/info/never"] == audit.BUCKET_COLD
        assert buckets["app/payments/info/rare"] == audit.BUCKET_SAMPLE
        assert buckets["app/payments/error/regular"] == audit.BUCKET_HOT

    def test_table_sorted_by_bytes_descending(self, sample_file):
        summary = analyze_file(sample_file)
        sizes = [r["bytes"] for r in summary["patterns"]]
        assert sizes == sorted(sizes, reverse=True)

    def test_totals_use_raw_bytes_field(self, sample_file):
        summary = analyze_file(sample_file)
        assert summary["total_events"] == 85
        assert summary["total_bytes"] == 20 * 150 + 10 * 400 + 50 * 120 + 5 * 200

    def test_garbage_ratio_is_never_class_bytes(self, sample_file):
        """Garbage ratio = bytes % in the 'never' queried class (both
        STOP SHIPPING and ROUTE COLD — nobody queries either)."""
        summary = analyze_file(sample_file)
        never_bytes = 20 * 150 + 10 * 400
        expected = 100.0 * never_bytes / summary["total_bytes"]
        assert summary["garbage_bytes"] == never_bytes
        assert summary["garbage_ratio_pct"] == pytest.approx(expected, abs=0.01)

    def test_stream_used_when_no_service(self, tmp_path):
        path = tmp_path / "k8s.jsonl"
        write_jsonl(
            path,
            [make_envelope("k8s", None, "warn", "regular", stream="stderr")],
        )
        summary = analyze_file(path)
        assert summary["patterns"][0]["pattern"] == "k8s/stderr/warn/regular"

    def test_unknown_queried_class_defaults_to_rare(self, tmp_path):
        path = tmp_path / "odd.jsonl"
        write_jsonl(path, [make_envelope(queried_class="whatever")])
        summary = analyze_file(path)
        assert summary["patterns"][0]["queried_class"] == "rare"
        assert summary["patterns"][0]["bucket"] == audit.BUCKET_SAMPLE

    def test_empty_input_analyzes_to_zero(self):
        summary = audit.analyze([])
        assert summary["total_events"] == 0
        assert summary["garbage_ratio_pct"] == 0.0


class TestReadEnvelopes:
    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "messy.jsonl"
        path.write_text(
            json.dumps(make_envelope()) + "\nnot json\n\n[1,2,3]\n",
            encoding="utf-8",
        )
        records, skipped = audit.read_envelopes([str(path)])
        assert len(records) == 1
        assert skipped == 2  # bad line + non-dict; blank line ignored

    def test_bytes_prefer_raw_bytes_field(self, tmp_path):
        path = tmp_path / "one.jsonl"
        write_jsonl(path, [make_envelope(raw_bytes=999)])
        records, _ = audit.read_envelopes([str(path)])
        assert records[0]["bytes"] == 999

    def test_bytes_fall_back_to_raw_then_line(self, tmp_path):
        path = tmp_path / "two.jsonl"
        no_count = make_envelope()
        del no_count["raw_bytes"]
        no_count["_raw"] = "abcd"
        bare = make_envelope()
        del bare["raw_bytes"]
        del bare["_raw"]
        write_jsonl(path, [no_count, bare])
        records, _ = audit.read_envelopes([str(path)])
        assert records[0]["bytes"] == 4  # len(_raw)
        assert records[1]["bytes"] == len(json.dumps(bare).encode())  # the line


class TestCli:
    def test_table_output_with_honest_header(self, sample_file, capsys):
        rc = audit.main([str(sample_file)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "heuristic stand-in" in out  # the honesty header
        assert "Pillar 2" in out
        assert "PATTERN" in out
        assert "GARBAGE RATIO" in out
        for bucket in audit.BUCKET_ORDER:
            assert bucket in out

    def test_json_output(self, sample_file, capsys):
        rc = audit.main([str(sample_file), "--json"])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["total_events"] == 85
        assert set(summary["buckets"]) == set(audit.BUCKET_ORDER)
        assert summary["buckets"][audit.BUCKET_HOT]["events"] == 5
        assert "heuristic stand-in" in summary["method_note"]

    def test_missing_file_returns_error(self, tmp_path, capsys):
        rc = audit.main([str(tmp_path / "nope.jsonl")])
        assert rc == 2
        assert "cannot read" in capsys.readouterr().err

    def test_empty_file_json_is_valid(self, tmp_path, capsys):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        rc = audit.main([str(path), "--json"])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["total_events"] == 0


class TestRawMode:
    """--raw classifies raw fixture lines via scripts/classify.py.

    Skipped until scripts/classify.py lands; the lines below exercise only
    contract-mandated behavior (error -> regular, /healthz -> never)."""

    APP_ERROR = json.dumps(
        {
            "timestamp": "2026-06-10T16:04:12.123Z",
            "level": "error",
            "service": "checkout",
            "msg": "payment failed",
            "duration_ms": 532,
        }
    )
    WEB_HEALTH = (
        '203.0.113.7 - - [10/Jun/2026:16:04:12 +0000] '
        '"GET /healthz HTTP/1.1" 200 512 "-" "kube-probe/1.29"'
    )

    @pytest.fixture(autouse=True)
    def _need_classify(self):
        pytest.importorskip("classify")

    @pytest.fixture
    def raw_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "mixed.log"
        path.write_text(
            self.APP_ERROR + "\n" + self.WEB_HEALTH + "\n", encoding="utf-8"
        )
        return path

    def test_raw_mode_counts_and_buckets(self, raw_file, capsys):
        rc = audit.main([str(raw_file), "--raw", "--json"])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["total_events"] == 2
        # error lines get queried regularly -> KEEP HOT (classify contract)
        assert summary["buckets"][audit.BUCKET_HOT]["events"] >= 1
        # health checks are never queried -> they ARE the garbage
        assert summary["garbage_bytes"] > 0
        assert 0 < summary["garbage_ratio_pct"] < 100

    def test_raw_mode_bytes_are_line_bytes(self, raw_file, capsys):
        rc = audit.main([str(raw_file), "--raw", "--json"])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        expected = len(self.APP_ERROR.encode()) + len(self.WEB_HEALTH.encode())
        assert summary["total_bytes"] == expected
