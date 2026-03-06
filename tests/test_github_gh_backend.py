from __future__ import annotations

import subprocess

import pytest

from mvp_orbit.core.models import ObjectNamespace
from mvp_orbit.integrations.object_store import GitHubGhCliBackend


def _cp(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_ensure_release_treats_already_exists_as_success(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    backend = GitHubGhCliBackend("owner", "repo")

    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        if args[:3] == ["release", "view", "mvp-orbit-package"]:
            return _cp(args, 1, stderr="release not found")
        if args[:3] == ["release", "create", "mvp-orbit-package"]:
            return _cp(args, 1, stderr="Release.tag_name already exists")
        raise AssertionError(args)

    monkeypatch.setattr(backend, "_run_gh", fake_run)

    assert backend._ensure_release(ObjectNamespace.PACKAGE) == "mvp-orbit-package"
    assert [call[:2] for call in calls] == [["release", "view"], ["release", "create"]]


def test_ensure_release_does_not_create_on_unrelated_view_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    backend = GitHubGhCliBackend("owner", "repo")

    def fake_run(args: list[str], *, check: bool = True):
        if args[:3] == ["release", "view", "mvp-orbit-package"]:
            return _cp(args, 1, stderr="Post https://api.github.com/... Proxy Authentication Required")
        raise AssertionError(args)

    monkeypatch.setattr(backend, "_run_gh", fake_run)

    with pytest.raises(RuntimeError, match="Proxy Authentication Required"):
        backend._ensure_release(ObjectNamespace.PACKAGE)
