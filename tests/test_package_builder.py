from __future__ import annotations

import shutil
import subprocess
import tarfile

import pytest

from mvp_orbit.cli.package import build_file_package


def test_build_file_package_respects_gitignore_and_is_stable(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for git-aware package tests")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("*.log\nignored_dir/\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "new.py").write_text("print('new')\n", encoding="utf-8")
    (tmp_path / "ignore.log").write_text("ignore", encoding="utf-8")
    (tmp_path / "ignored_dir").mkdir()
    (tmp_path / "ignored_dir" / "file.txt").write_text("ignored", encoding="utf-8")

    first = build_file_package(tmp_path)
    second = build_file_package(tmp_path)

    with tarfile.open(first.archive_path, "r:gz") as tar:
        names = set(tar.getnames())

    assert first.package_id == second.package_id
    assert "keep.txt" in names
    assert "new.py" in names
    assert ".gitignore" in names
    assert "ignore.log" not in names
    assert "ignored_dir/file.txt" not in names
