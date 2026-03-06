from __future__ import annotations

import subprocess

import pytest

from mvp_orbit.core.models import ObjectNamespace
from mvp_orbit.integrations.object_store import HuggingFaceCliBackend


def _cp(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_put_bytes_uses_hf_upload(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/hf")
    backend = HuggingFaceCliBackend(
        "org/orbit-relay",
        repo_type="dataset",
        path_prefix="objects",
        private=False,
    )

    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return _cp(args, 0)

    monkeypatch.setattr(backend, "_run_hf", fake_run)

    meta = backend.put_bytes(
        ObjectNamespace.PACKAGE,
        "pkg-1",
        b"payload",
        content_type="application/gzip",
        filename="pkg-1.tar.gz",
    )

    assert calls
    assert calls[0][:3] == ["upload", "--repo-type", "dataset"]
    assert calls[0][-3] == "org/orbit-relay"
    assert calls[0][-1] == "objects/package/pkg-1.tar.gz"
    assert meta.storage_ref == "hf:org/orbit-relay/objects/package/pkg-1.tar.gz"
    assert meta.size == 7


def test_get_bytes_raises_file_not_found_on_missing_entry(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/hf")
    backend = HuggingFaceCliBackend("org/orbit-relay")

    def fake_run(args: list[str], *, check: bool = True):
        return _cp(args, 1, stderr="404 Client Error: Entry Not Found")

    monkeypatch.setattr(backend, "_run_hf", fake_run)

    with pytest.raises(FileNotFoundError):
        backend.get_bytes(ObjectNamespace.RESULT, "res-1", filename="res-1.json")


def test_purge_managed_paths_deletes_each_namespace(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/hf")
    backend = HuggingFaceCliBackend("org/orbit-relay", path_prefix="objects")

    patterns: list[str] = []

    def fake_run(args: list[str], *, check: bool = True):
        if args[:2] != ["repo-files", "delete"]:
            raise AssertionError(args)
        patterns.append(args[-1])
        return _cp(args, 0)

    monkeypatch.setattr(backend, "_run_hf", fake_run)

    deleted = backend.purge_managed_paths()

    assert deleted == patterns
    assert "objects/package/*" in patterns
    assert "objects/log/*" in patterns
