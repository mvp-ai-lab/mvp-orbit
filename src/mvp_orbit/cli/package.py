from __future__ import annotations

import gzip
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mvp_orbit.core.canonical import object_id_for_bytes


@dataclass
class FilePackageBuildResult:
    package_id: str
    archive_path: Path
    file_count: int


def build_file_package(source_dir: str | Path, *, tmp_dir: str | Path | None = None) -> FilePackageBuildResult:
    source = Path(source_dir).resolve()
    if not source.exists() or not source.is_dir():
        raise ValueError(f"source_dir is not a directory: {source}")

    files = _collect_source_files(source)
    if not files:
        raise ValueError(f"no files selected for package in {source}")

    workspace_parent = Path(tmp_dir).resolve() if tmp_dir else Path(tempfile.mkdtemp(prefix="orbit-package-"))
    workspace_parent.mkdir(parents=True, exist_ok=True)

    archive_path = workspace_parent / "package.tar.gz"
    if archive_path.exists():
        archive_path.unlink()

    _write_deterministic_archive(source, files, archive_path)
    package_bytes = archive_path.read_bytes()
    package_id = object_id_for_bytes(package_bytes)

    final_archive_path = workspace_parent / f"{package_id}.tar.gz"
    archive_path.replace(final_archive_path)
    return FilePackageBuildResult(package_id=package_id, archive_path=final_archive_path, file_count=len(files))


def _write_deterministic_archive(source: Path, files: list[Path], archive_path: Path) -> None:
    with archive_path.open("wb") as raw_handle:
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gzip_handle:
            with tarfile.open(fileobj=gzip_handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel_path in sorted(files):
                    src_path = source / rel_path
                    tar_info = _tar_info_for_path(src_path, rel_path.as_posix())
                    if src_path.is_symlink():
                        tar.addfile(tar_info)
                        continue
                    with src_path.open("rb") as source_handle:
                        tar.addfile(tar_info, source_handle)


def _tar_info_for_path(path: Path, name: str) -> tarfile.TarInfo:
    st = path.lstat()
    info = tarfile.TarInfo(name=name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = stat.S_IMODE(st.st_mode)
    if path.is_symlink():
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        info.size = 0
        return info
    info.size = st.st_size
    return info


def _collect_source_files(source: Path) -> list[Path]:
    git_files = _collect_git_files(source)
    if git_files is not None:
        return git_files

    result: list[Path] = []
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            result.append((root_path / name).relative_to(source))
    return sorted(result)


def _collect_git_files(source: Path) -> list[Path] | None:
    if shutil.which("git") is None:
        return None

    try:
        repo_root_text = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    repo_root = Path(repo_root_text).resolve()
    try:
        source_relative = source.relative_to(repo_root)
    except ValueError:
        return None

    raw = subprocess.check_output(
        ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        text=False,
    )
    repo_paths = [Path(item.decode("utf-8")) for item in raw.split(b"\x00") if item]

    selected: list[Path] = []
    for repo_path in repo_paths:
        if source_relative == Path("."):
            selected.append(repo_path)
            continue
        if repo_path == source_relative or source_relative in repo_path.parents:
            selected.append(repo_path.relative_to(source_relative))

    return sorted(selected)
