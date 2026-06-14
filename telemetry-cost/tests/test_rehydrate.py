"""Tests for scripts/rehydrate.py: ISO parsing, receive-hour partition
selection (hour-granular by design — raw lines carry no uniform timestamp),
verbatim raw-line output, --grep filtering, gzip partitions spanning hours,
and the empty-result case."""

from __future__ import annotations

import gzip
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rehydrate  # noqa: E402

NCSA = (
    '203.0.113.7 - alice [10/Jun/2026:14:04:12 +0000] '
    '"POST /login HTTP/1.1" 200 512 "-" "Mozilla/5.0"'
)
CRI = (
    "2026-06-10T14:04:12.123456789Z stderr F "
    '{"level":"error","caller":"pay/charge.go:88","msg":"card declined"}'
)
APP = '{"timestamp":"2026-06-10T14:04:12.123Z","level":"info","service":"payments","msg":"ok"}'


def write_partition(cold_dir: Path, hour_ts: str, lines) -> Path:
    """Append raw lines into the receive-hour partition dir:
    cold-storage/YYYY/MM/DD/HH/events-<HH>.jsonl.gz."""
    dt = rehydrate.parse_iso(hour_ts)
    part = (
        cold_dir
        / f"{dt.year:04d}"
        / f"{dt.month:02d}"
        / f"{dt.day:02d}"
        / f"{dt.hour:02d}"
    )
    part.mkdir(parents=True, exist_ok=True)
    path = part / f"events-{dt.hour:02d}.jsonl.gz"
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    return path


@pytest.fixture
def cold_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cold-storage"
    root.mkdir()
    return root


def pull(cold_dir, frm, to, grep=None):
    import re

    return rehydrate.rehydrate(
        cold_dir,
        rehydrate.parse_iso(frm),
        rehydrate.parse_iso(to),
        re.compile(grep) if grep else None,
    )


class TestParseIso:
    def test_z_suffix(self):
        dt = rehydrate.parse_iso("2026-06-10T14:00:00Z")
        assert dt == datetime(2026, 6, 10, 14, tzinfo=timezone.utc)

    def test_naive_assumed_utc(self):
        dt = rehydrate.parse_iso("2026-06-10T14:00:00")
        assert dt.tzinfo == timezone.utc

    def test_offset_normalized_to_utc(self):
        dt = rehydrate.parse_iso("2026-06-10T16:00:00+02:00")
        assert dt == datetime(2026, 6, 10, 14, tzinfo=timezone.utc)

    def test_fractional_seconds(self):
        dt = rehydrate.parse_iso("2026-06-10T14:00:00.123Z")
        assert dt.microsecond == 123000

    def test_bad_timestamp_raises(self):
        with pytest.raises(ValueError):
            rehydrate.parse_iso("not-a-timestamp")


class TestPartitionSelection:
    """Filtering is by partition-hour directory only: an hour is selected
    when it overlaps the half-open window [--from, --to); every line in a
    selected partition comes back (raw lines have no per-event ts)."""

    def test_exact_hour_window_selects_one_partition(self, cold_dir):
        write_partition(cold_dir, "2026-06-10T13:00:00Z", ["line-13"])
        write_partition(cold_dir, "2026-06-10T14:00:00Z", ["line-14a", "line-14b"])
        write_partition(cold_dir, "2026-06-10T15:00:00Z", ["line-15"])
        lines, _, partitions = pull(
            cold_dir, "2026-06-10T14:00:00Z", "2026-06-10T15:00:00Z"
        )
        assert lines == ["line-14a", "line-14b"]
        assert partitions == 1  # 13h and 15h pruned by path, never opened

    def test_mid_hour_window_selects_overlapping_hours(self, cold_dir):
        for hour in (13, 14, 15, 16, 17):
            write_partition(
                cold_dir, f"2026-06-10T{hour:02d}:00:00Z", [f"line-{hour}"]
            )
        # 14:05 .. 16:05 overlaps hour partitions 14, 15 and 16 — all of
        # each selected partition is returned (hour granularity by design).
        lines, matched_bytes, partitions = pull(
            cold_dir, "2026-06-10T14:05:00Z", "2026-06-10T16:05:00Z"
        )
        assert lines == ["line-14", "line-15", "line-16"]
        assert partitions == 3
        assert matched_bytes == sum(len(line.encode("utf-8")) for line in lines)

    def test_prunes_partitions_outside_window(self, cold_dir):
        write_partition(cold_dir, "2026-06-09T10:00:00Z", ["old"])
        write_partition(cold_dir, "2026-06-10T14:00:00Z", ["new"])
        partitions = rehydrate.find_partitions(
            cold_dir,
            rehydrate.parse_iso("2026-06-10T14:00:00Z"),
            rehydrate.parse_iso("2026-06-10T15:00:00Z"),
        )
        assert len(partitions) == 1
        assert "14" in str(partitions[0])

    def test_unrecognized_layout_still_scanned(self, cold_dir):
        # A stray gz outside YYYY/MM/DD/HH must not be silently dropped.
        stray = cold_dir / "misc.jsonl.gz"
        with gzip.open(stray, "wt", encoding="utf-8") as fh:
            fh.write("stray-line\n")
        lines, _, partitions = pull(
            cold_dir, "2026-06-10T14:00:00Z", "2026-06-10T15:00:00Z"
        )
        assert lines == ["stray-line"]
        assert partitions == 1


class TestRawLines:
    def test_lines_come_back_verbatim(self, cold_dir):
        """Cold storage holds raw originals (NCSA, CRI, JSON): rehydrate
        emits them byte-for-byte, no parsing, no reserialization."""
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA, CRI, APP])
        lines, matched_bytes, _ = pull(
            cold_dir, "2026-06-10T14:00:00Z", "2026-06-10T15:00:00Z"
        )
        assert lines == [NCSA, CRI, APP]
        assert matched_bytes == sum(len(line.encode("utf-8")) for line in lines)

    def test_blank_lines_skipped(self, cold_dir):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", ["a", "", "  ", "b"])
        lines, _, _ = pull(cold_dir, "2026-06-10T14:00:00Z", "2026-06-10T15:00:00Z")
        assert lines == ["a", "b"]


class TestGrep:
    def test_grep_filters_lines(self, cold_dir):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA, CRI, APP])
        lines, _, _ = pull(
            cold_dir,
            "2026-06-10T14:00:00Z",
            "2026-06-10T15:00:00Z",
            grep=r"POST /login",
        )
        assert lines == [NCSA]

    def test_grep_is_regex(self, cold_dir):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA, CRI, APP])
        lines, _, _ = pull(
            cold_dir,
            "2026-06-10T14:00:00Z",
            "2026-06-10T15:00:00Z",
            grep=r'"level":"(error|warn)"',
        )
        assert lines == [CRI]

    def test_no_grep_returns_everything(self, cold_dir):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA, CRI])
        lines, _, _ = pull(cold_dir, "2026-06-10T14:00:00Z", "2026-06-10T15:00:00Z")
        assert len(lines) == 2


class TestEmptyResult:
    def test_window_with_no_partitions(self, cold_dir):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", ["line"])
        lines, matched_bytes, _ = pull(
            cold_dir, "2026-06-11T00:00:00Z", "2026-06-11T01:00:00Z"
        )
        assert lines == []
        assert matched_bytes == 0

    def test_empty_cold_dir(self, cold_dir):
        lines, matched_bytes, partitions = pull(
            cold_dir, "2026-06-10T14:00:00Z", "2026-06-10T15:00:00Z"
        )
        assert (lines, matched_bytes, partitions) == ([], 0, 0)


class TestCli:
    def test_emits_raw_lines_and_stats(self, cold_dir, capsys):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA, CRI])
        rc = rehydrate.main(
            [
                "--from",
                "2026-06-10T14:00:00Z",
                "--to",
                "2026-06-10T15:00:00Z",
                "--cold-dir",
                str(cold_dir),
                "--stats",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == NCSA + "\n" + CRI + "\n"  # stdout: verbatim lines
        assert "matched 2 line(s)" in captured.err
        assert "partition file(s)" in captured.err

    def test_grep_flag(self, cold_dir, capsys):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA, CRI, APP])
        rc = rehydrate.main(
            [
                "--from",
                "2026-06-10T14:00:00Z",
                "--to",
                "2026-06-10T15:00:00Z",
                "--cold-dir",
                str(cold_dir),
                "--grep",
                "card declined",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == CRI + "\n"

    def test_stats_silent_without_flag(self, cold_dir, capsys):
        write_partition(cold_dir, "2026-06-10T14:00:00Z", [NCSA])
        rc = rehydrate.main(
            [
                "--from",
                "2026-06-10T14:00:00Z",
                "--to",
                "2026-06-10T15:00:00Z",
                "--cold-dir",
                str(cold_dir),
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""

    def test_bad_grep_regex_errors(self, cold_dir, capsys):
        rc = rehydrate.main(
            [
                "--from",
                "2026-06-10T14:00:00Z",
                "--to",
                "2026-06-10T15:00:00Z",
                "--cold-dir",
                str(cold_dir),
                "--grep",
                "[unclosed",
            ]
        )
        assert rc == 2
        assert "bad --grep regex" in capsys.readouterr().err

    def test_missing_cold_dir_errors(self, tmp_path, capsys):
        rc = rehydrate.main(
            [
                "--from",
                "2026-06-10T14:00:00Z",
                "--to",
                "2026-06-10T15:00:00Z",
                "--cold-dir",
                str(tmp_path / "nope"),
            ]
        )
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_inverted_window_errors(self, cold_dir, capsys):
        rc = rehydrate.main(
            [
                "--from",
                "2026-06-10T15:00:00Z",
                "--to",
                "2026-06-10T14:00:00Z",
                "--cold-dir",
                str(cold_dir),
            ]
        )
        assert rc == 2
        assert "earlier than" in capsys.readouterr().err

    def test_bad_timestamp_errors(self, cold_dir, capsys):
        rc = rehydrate.main(
            [
                "--from",
                "garbage",
                "--to",
                "2026-06-10T15:00:00Z",
                "--cold-dir",
                str(cold_dir),
            ]
        )
        assert rc == 2
        assert "bad timestamp" in capsys.readouterr().err
