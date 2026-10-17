"""Report builder must refuse incomplete/stale audit evidence.

The builder may only publish a page from a *validated* completed run: the
``COMPLETED`` marker must exist, parse, declare completion, and link to the
other outputs. Absent, truncated or mismatched markers must not overwrite an
existing report.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from livecell_audit.audit import COMPLETION_MARKER, AuditConfig, run_audit

_ROOT = Path(__file__).resolve().parents[1]


def _load_build_report():
    spec = importlib.util.spec_from_file_location(
        "build_report", _ROOT / "scripts" / "build_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed_run(mini_dataset, tmp_path, name="audit"):
    out = str(tmp_path / name)
    run_audit(AuditConfig(data_root=mini_dataset["data_root"], out_dir=out))
    assert os.path.isfile(os.path.join(out, COMPLETION_MARKER))
    return out


def _sentinel_report_dir(tmp_path):
    report_dir = tmp_path / "report"
    data_dir = report_dir / "data"
    data_dir.mkdir(parents=True)
    sentinel = data_dir / "report.js"
    sentinel.write_text("SENTINEL-DO-NOT-OVERWRITE", encoding="utf-8")
    return str(report_dir), sentinel


def test_build_report_accepts_a_completed_run(mini_dataset, tmp_path):
    build_report = _load_build_report()
    out = _completed_run(mini_dataset, tmp_path)
    report_dir = str(tmp_path / "report")
    code = build_report.main(
        [
            "--audit-dir",
            out,
            "--data-root",
            mini_dataset["data_root"],
            "--report-dir",
            report_dir,
        ]
    )
    assert code == 0
    assert os.path.isfile(os.path.join(report_dir, "data", "report.js"))
    assert os.path.isfile(os.path.join(report_dir, "data", "findings.js"))


def test_build_report_rejects_absent_marker(mini_dataset, tmp_path, capsys):
    build_report = _load_build_report()
    out = _completed_run(mini_dataset, tmp_path)
    os.remove(os.path.join(out, COMPLETION_MARKER))
    report_dir, sentinel = _sentinel_report_dir(tmp_path)
    code = build_report.main(
        [
            "--audit-dir",
            out,
            "--data-root",
            mini_dataset["data_root"],
            "--report-dir",
            report_dir,
        ]
    )
    assert code == 2
    assert "completion marker" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE"
    assert not os.path.exists(os.path.join(report_dir, "data", "findings.js"))


def test_build_report_rejects_truncated_marker(mini_dataset, tmp_path, capsys):
    build_report = _load_build_report()
    out = _completed_run(mini_dataset, tmp_path)
    with open(os.path.join(out, COMPLETION_MARKER), "w", encoding="utf-8") as handle:
        handle.write("{")  # truncated JSON
    report_dir, sentinel = _sentinel_report_dir(tmp_path)
    code = build_report.main(
        [
            "--audit-dir",
            out,
            "--data-root",
            mini_dataset["data_root"],
            "--report-dir",
            report_dir,
        ]
    )
    assert code == 2
    assert "completion marker" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE"


def test_build_report_rejects_mismatched_marker(mini_dataset, tmp_path, capsys):
    build_report = _load_build_report()
    out = _completed_run(mini_dataset, tmp_path)
    marker_path = os.path.join(out, COMPLETION_MARKER)
    with open(marker_path, encoding="utf-8") as handle:
        marker = json.load(handle)
    marker["total_annotations"] = marker["total_annotations"] + 1  # stale/mixed
    with open(marker_path, "w", encoding="utf-8") as handle:
        json.dump(marker, handle)
    report_dir, sentinel = _sentinel_report_dir(tmp_path)
    code = build_report.main(
        [
            "--audit-dir",
            out,
            "--data-root",
            mini_dataset["data_root"],
            "--report-dir",
            report_dir,
        ]
    )
    assert code == 2
    assert "does not match" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE"


def test_build_report_rejects_mismatched_source_checksums(mini_dataset, tmp_path, capsys):
    build_report = _load_build_report()
    out = _completed_run(mini_dataset, tmp_path)
    marker_path = os.path.join(out, COMPLETION_MARKER)
    with open(marker_path, encoding="utf-8") as handle:
        marker = json.load(handle)
    marker["source_sha256"]["train"] = "0" * 64
    with open(marker_path, "w", encoding="utf-8") as handle:
        json.dump(marker, handle)
    report_dir, sentinel = _sentinel_report_dir(tmp_path)
    code = build_report.main(
        [
            "--audit-dir",
            out,
            "--data-root",
            mini_dataset["data_root"],
            "--report-dir",
            report_dir,
        ]
    )
    assert code == 2
    assert "source" in capsys.readouterr().err.lower()
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE"


def test_build_report_rejects_promoted_parent_without_marker(tmp_path, capsys):
    """A dir that only *looks* populated (no marker) cannot publish."""
    build_report = _load_build_report()
    audit_dir = tmp_path / "empty-audit"
    audit_dir.mkdir()
    report_dir, sentinel = _sentinel_report_dir(tmp_path)
    code = build_report.main(
        [
            "--audit-dir",
            str(audit_dir),
            "--data-root",
            str(tmp_path / "data"),
            "--report-dir",
            report_dir,
        ]
    )
    assert code == 2
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE"
    capsys.readouterr()
