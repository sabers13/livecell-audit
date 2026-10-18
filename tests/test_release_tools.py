"""Release regressions: immutable evidence, full ZIP inventories and launcher routing."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_tools", ROOT / "tools/release_tools.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def archive_tree(tmp_path):
    root = tmp_path / "package"
    (root / "site/data").mkdir(parents=True)
    (root / "site/index.html").write_text('<script src="data/site-data.js"></script>')
    (root / "site/data/site-data.js").write_text("window.LIVECELL_REVIEW = {};\n")
    (root / "site/data/rankings.js").write_text("window.LIVECELL_RANKINGS = {};\n")
    release.write_checksums(str(root))
    ctx = SimpleNamespace(repo="/private/repo", run_dir="/private/run",
                          out_root="/private/outputs", data_root="/private/data")
    return root, tmp_path / "release.zip", ctx


def _archive(tree):
    root, archive, ctx = tree
    release.zip_deterministic(str(root), str(archive), "release")
    return release.verify_zip(str(archive), ctx)


def test_valid_portable_archive(archive_tree):
    assert _archive(archive_tree) == []


def test_added_file_must_be_in_checksums(archive_tree):
    root, _, _ = archive_tree
    (root / "unverified.txt").write_text("added after checksum generation")
    assert any("missing from checksums: unverified.txt" in p for p in _archive(archive_tree))


@pytest.mark.parametrize("contents", ["", "not-a-checksum\n", "0" * 64 + "  ../outside.txt\n"])
def test_invalid_checksum_inventory_is_rejected(archive_tree, contents):
    root, _, _ = archive_tree
    (root / "checksums.sha256").write_text(contents)
    assert _archive(archive_tree)


def test_duplicate_checksum_entry_is_rejected(archive_tree):
    root, _, _ = archive_tree
    checksums = root / "checksums.sha256"
    checksums.write_text(checksums.read_text() + checksums.read_text().splitlines()[0] + "\n")
    assert any("duplicate checksum" in p for p in _archive(archive_tree))


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "release/../../outside.txt",
                                 "C:/outside.txt", "release\\outside.txt"])
def test_unsafe_archive_is_rejected_before_extraction(archive_tree, name, monkeypatch):
    _, archive, ctx = archive_tree
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(name, "unsafe")
    def unexpected_extract(*args, **kwargs):
        pytest.fail("invalid archives must be rejected before extraction")
    monkeypatch.setattr(zipfile.ZipFile, "extractall", unexpected_extract)
    assert any("unsafe archive path" in p for p in release.verify_zip(str(archive), ctx))


def test_top_level_files_are_not_ignored(archive_tree):
    root, archive, ctx = archive_tree
    release.zip_deterministic(str(root), str(archive), "release")
    with zipfile.ZipFile(archive, "a") as handle:
        handle.writestr("unverified.txt", "outside package root")
    assert any("top-level directory" in p for p in release.verify_zip(str(archive), ctx))


def test_corrupt_zip_returns_actionable_problem(archive_tree):
    _, archive, ctx = archive_tree
    archive.write_bytes(b"not a zip")
    assert any("cannot read release archive" in p for p in release.verify_zip(str(archive), ctx))


def test_link_cannot_resolve_outside_package(archive_tree):
    root, _, _ = archive_tree
    (root.parent / "outside.txt").write_text("exists on this machine only")
    (root / "site/index.html").write_text('<a href="../../outside.txt">escape</a>')
    assert release.resolve_package_links(str(root))


def test_link_with_fragment_resolves(archive_tree):
    root, _, _ = archive_tree
    (root / "site/index.html").write_text('<a href="data/site-data.js#example">evidence</a>')
    assert release.resolve_package_links(str(root)) == []


@pytest.fixture
def completed_run(tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "site").mkdir(parents=True)
    (run / "site/index.html").write_text("verified site")
    release.write_text(str(run / "logs/site.log"), "completed\n")
    identity = {"input": "frozen"}
    record = {
        "status": "completed",
        "log": {"sha256": release.sha256_file(str(run / "logs/site.log"))},
        "outputs": {"tree_sha256": release.hash_tree(str(run / "site"))["tree_sha256"]},
    }
    release.write_json(str(run / "records/stage-site.json"), record)
    release.write_json(str(run / "records/chain-verification.json"), {"ok": True})
    receipt = {"identity": identity, "stages": [{
        "stage": "site", "record_sha256": release.record_sha256(str(run), "site"),
        "output_tree_sha256": record["outputs"]["tree_sha256"],
    }]}
    release.write_json(str(run / "records/release-receipt.json"), receipt)
    release.write_json(str(run / "COMPLETED"), {
        "status": "completed", "stage": "release-run", "run_id": "run", "stages": ["site"],
        "chain_verification_sha256": release.sha256_file(str(run / "records/chain-verification.json")),
        "release_receipt": {"sha256": release.sha256_file(str(run / "records/release-receipt.json"))},
    })
    monkeypatch.setattr(release, "active_stages", lambda previews: ("site",))
    ctx = SimpleNamespace(run_dir=str(run), with_previews=False, identity_block=lambda: identity)
    return run, ctx, receipt


def test_completed_run_recheck_is_read_only(completed_run):
    run, ctx, receipt = completed_run
    before = release.hash_tree(str(run))
    assert release.verify_completed_run(ctx) == receipt
    assert release.hash_tree(str(run)) == before


@pytest.mark.parametrize("path", ["site/index.html", "records/stage-site.json", "logs/site.log",
                                 "records/chain-verification.json", "records/release-receipt.json"])
def test_repackaging_rejects_changed_completion_evidence(completed_run, path):
    run, ctx, _ = completed_run
    with (run / path).open("a") as handle:
        handle.write("\n ")
    with pytest.raises(release.ReleaseError, match="(hash matches|outputs still match)"):
        release.verify_completed_run(ctx)


def test_stage_cannot_mutate_completed_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "COMPLETED").write_text('{"status":"completed"}')
    args = release.build_parser().parse_args(["stage", "--run", str(run), "--stage", "site"])
    before = release.hash_tree(str(run))
    with pytest.raises(release.ReleaseError, match="immutable"):
        release.cmd_stage(args)
    assert release.hash_tree(str(run)) == before


def test_verify_completed_chain_preserves_records(completed_run, monkeypatch):
    run, _, _ = completed_run
    monkeypatch.setitem(release.STAGE_INPUT_CHECK, "site", lambda ctx, checker: None)
    monkeypatch.setitem(release.STAGE_OUTPUT_CHECK, "site", lambda ctx, checker: None)
    args = release.build_parser().parse_args(["verify-chain", "--run", str(run)])
    before = release.hash_tree(str(run))
    assert release.cmd_verify_chain(args) == 0
    assert release.hash_tree(str(run)) == before


def test_completed_site_check_does_not_repair_links(completed_run, monkeypatch):
    run, ctx, _ = completed_run
    monkeypatch.setattr(release, "_import_package", lambda: (None, None, None, None))
    before = release.hash_tree(str(run))
    with pytest.raises(release.ReleaseError, match="site input ranking-fixed-mem"):
        release.check_site_inputs(ctx, release.Checker("site inputs"))
    assert not (run / "site-inputs").exists()
    assert release.hash_tree(str(run)) == before


def test_no_tiff_identity_option_reaches_preflight(mini_dataset, tmp_path):
    result = subprocess.run([
        "bash", str(ROOT / "tools/run_release.sh"), "validate",
        "--data-root", mini_dataset["data_root"], "--out-root", str(tmp_path / "out"),
        "--allow-external-out-root", "--min-free-gb", "0", "--no-tiff-identity",
        "--python", "/bin/true", "--run-job", "/bin/true",
    ], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "INVOCATION_ID"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "validation passed" in result.stdout + result.stderr


@pytest.mark.parametrize("command", [["list"], ["verify-package", "--zip", "fixture.zip"]])
def test_read_commands_use_resource_launcher(tmp_path, command):
    marker = tmp_path / "launcher-used"
    launcher = tmp_path / "launcher"
    launcher.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexec "$@"\n')
    launcher.chmod(0o755)
    result = subprocess.run([
        "bash", str(ROOT / "tools/run_release.sh"), *command,
        "--python", "/bin/true", "--run-job", str(launcher),
    ], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "INVOCATION_ID"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists(), "the command bypassed the resource launcher"


def test_single_job_does_not_nest_launchers(tmp_path):
    # An already-capped caller must run the no-op interpreter directly. The
    # fake launcher fails if called; no real computation or launcher is used.
    result = subprocess.run([
        "bash", str(ROOT / "tools/run_release.sh"), "list", "--single-job",
        "--python", "/bin/true", "--run-job", "/bin/false",
    ], capture_output=True, text=True, env={**os.environ, "INVOCATION_ID": "already-capped"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_site_check_explains_missing_source_outputs(tmp_path, capsys):
    checker_spec = importlib.util.spec_from_file_location("check_review_site", ROOT / "tools/check_review_site.py")
    checker = importlib.util.module_from_spec(checker_spec)
    checker_spec.loader.exec_module(checker)
    assert checker.main(["--site", str(tmp_path), "--inputs", str(tmp_path),
                         "--audit-dir", str(tmp_path)]) == 1
    assert "matching completed server run" in capsys.readouterr().out
