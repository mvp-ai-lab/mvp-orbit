from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mvp_orbit.core.canonical import canonical_json_bytes, object_id_for_bytes, object_id_for_json, validate_object_id
from mvp_orbit.core.models import CommandObject, LogObject, ObjectNamespace, ResultObject, SignedTaskObject


@dataclass
class StoredObjectMeta:
    namespace: ObjectNamespace
    object_id: str
    size: int
    storage_ref: str


class ObjectStoreBackend(Protocol):
    def put_bytes(
        self,
        namespace: ObjectNamespace,
        object_id: str,
        payload: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> StoredObjectMeta:
        raise NotImplementedError

    def get_bytes(self, namespace: ObjectNamespace, object_id: str, *, filename: str) -> bytes:
        raise NotImplementedError

    def exists(self, namespace: ObjectNamespace, object_id: str, *, filename: str) -> bool:
        raise NotImplementedError

    def get_meta(self, namespace: ObjectNamespace, object_id: str, *, filename: str) -> StoredObjectMeta:
        raise NotImplementedError


class ObjectStore:
    def __init__(self, backend: ObjectStoreBackend) -> None:
        self.backend = backend

    def put_package(self, payload: bytes) -> str:
        package_id = object_id_for_bytes(payload)
        self.backend.put_bytes(
            ObjectNamespace.PACKAGE,
            package_id,
            payload,
            content_type="application/gzip",
            filename=self._filename(ObjectNamespace.PACKAGE, package_id),
        )
        return package_id

    def get_package(self, package_id: str) -> bytes:
        return self.backend.get_bytes(
            ObjectNamespace.PACKAGE,
            package_id,
            filename=self._filename(ObjectNamespace.PACKAGE, package_id),
        )

    def put_command(self, command: CommandObject) -> str:
        payload = command.model_dump(mode="json", exclude_none=True)
        command_id = object_id_for_json(payload)
        self._put_json(ObjectNamespace.COMMAND, command_id, payload)
        return command_id

    def get_command(self, command_id: str) -> CommandObject:
        return CommandObject.model_validate(self._get_json(ObjectNamespace.COMMAND, command_id))

    def put_signed_task(self, task: SignedTaskObject) -> str:
        payload = task.model_dump(mode="json", exclude_none=True)
        self._put_json(ObjectNamespace.TASK, task.task_id, payload)
        return task.task_id

    def get_signed_task(self, task_id: str) -> SignedTaskObject:
        return SignedTaskObject.model_validate(self._get_json(ObjectNamespace.TASK, task_id))

    def put_log(self, log: LogObject) -> str:
        payload = log.model_dump(mode="json", exclude_none=True)
        log_id = object_id_for_json(payload)
        self._put_json(ObjectNamespace.LOG, log_id, payload)
        return log_id

    def get_log(self, log_id: str) -> LogObject:
        return LogObject.model_validate(self._get_json(ObjectNamespace.LOG, log_id))

    def put_result(self, result: ResultObject) -> str:
        payload = result.model_dump(mode="json", exclude_none=True)
        result_id = object_id_for_json(payload)
        self._put_json(ObjectNamespace.RESULT, result_id, payload)
        return result_id

    def get_result(self, result_id: str) -> ResultObject:
        return ResultObject.model_validate(self._get_json(ObjectNamespace.RESULT, result_id))

    def _put_json(self, namespace: ObjectNamespace, object_id: str, payload: dict) -> None:
        self.backend.put_bytes(
            namespace,
            object_id,
            canonical_json_bytes(payload),
            content_type="application/json",
            filename=self._filename(namespace, object_id),
        )

    def _get_json(self, namespace: ObjectNamespace, object_id: str) -> dict:
        raw = self.backend.get_bytes(namespace, object_id, filename=self._filename(namespace, object_id))
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _filename(namespace: ObjectNamespace, object_id: str) -> str:
        validate_object_id(object_id)
        if namespace is ObjectNamespace.PACKAGE:
            return f"{object_id}.tar.gz"
        return f"{object_id}.json"


class GitHubGhCliBackend:
    def __init__(self, owner: str, repo: str, *, release_prefix: str = "mvp-orbit", gh_bin: str = "gh") -> None:
        self.owner = owner
        self.repo = repo
        self.release_prefix = release_prefix
        self.gh_bin = gh_bin
        self.repo_ref = f"{owner}/{repo}"
        if shutil.which(self.gh_bin) is None:
            raise RuntimeError(f"GitHub CLI not found: {self.gh_bin}")

    def put_bytes(
        self,
        namespace: ObjectNamespace,
        object_id: str,
        payload: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> StoredObjectMeta:
        release_tag = self._ensure_release(namespace)
        existing = self._find_asset(release_tag, filename)
        if existing is not None:
            return self._meta(namespace, object_id, existing)

        tmp_dir = Path(tempfile.mkdtemp(prefix="orbit-gh-upload-"))
        try:
            tmp_file = tmp_dir / filename
            tmp_file.write_bytes(payload)
            self._run_gh(
                [
                    "release",
                    "upload",
                    release_tag,
                    str(tmp_file),
                    "--repo",
                    self.repo_ref,
                ]
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        asset = self._get_asset(namespace, object_id, filename)
        return self._meta(namespace, object_id, asset)

    def get_bytes(self, namespace: ObjectNamespace, object_id: str, *, filename: str) -> bytes:
        release_tag = self._release_tag(namespace)
        tmp_dir = Path(tempfile.mkdtemp(prefix="orbit-gh-download-"))
        try:
            self._run_gh(
                [
                    "release",
                    "download",
                    release_tag,
                    "--repo",
                    self.repo_ref,
                    "--pattern",
                    filename,
                    "--dir",
                    str(tmp_dir),
                ]
            )
            file_path = tmp_dir / filename
            if not file_path.exists():
                raise FileNotFoundError(f"{namespace.value}:{object_id}")
            return file_path.read_bytes()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def exists(self, namespace: ObjectNamespace, object_id: str, *, filename: str) -> bool:
        try:
            self._get_asset(namespace, object_id, filename)
        except FileNotFoundError:
            return False
        return True

    def get_meta(self, namespace: ObjectNamespace, object_id: str, *, filename: str) -> StoredObjectMeta:
        return self._meta(namespace, object_id, self._get_asset(namespace, object_id, filename))

    def purge_managed_releases(self) -> list[str]:
        deleted: list[str] = []
        for namespace in ObjectNamespace:
            release_tag = self._release_tag(namespace)
            result = self._run_gh(
                ["release", "view", release_tag, "--repo", self.repo_ref, "--json", "name"],
                check=False,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                if self._is_release_missing(stderr, stdout):
                    continue
                message = stderr or stdout or f"failed to inspect release {release_tag}"
                raise RuntimeError(f"gh command failed: {message}")
            self._run_gh(
                ["release", "delete", release_tag, "--repo", self.repo_ref, "--yes"],
            )
            deleted.append(release_tag)
        return deleted

    def _meta(self, namespace: ObjectNamespace, object_id: str, asset: dict) -> StoredObjectMeta:
        return StoredObjectMeta(
            namespace=namespace,
            object_id=object_id,
            size=int(asset.get("size", 0)),
            storage_ref=f"gh_release_asset:{asset.get('apiUrl', asset.get('name', 'unknown'))}",
        )

    def _ensure_release(self, namespace: ObjectNamespace) -> str:
        release_tag = self._release_tag(namespace)
        result = self._run_gh(
            ["release", "view", release_tag, "--repo", self.repo_ref, "--json", "name"],
            check=False,
        )
        if result.returncode == 0:
            return release_tag

        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        if not self._is_release_missing(stderr, stdout):
            message = stderr or stdout or f"failed to inspect release {release_tag}"
            raise RuntimeError(f"gh command failed: {message}")

        create_result = self._run_gh(
            [
                "release",
                "create",
                release_tag,
                "--repo",
                self.repo_ref,
                "--title",
                release_tag,
                "--notes",
                "",
            ],
            check=False,
        )
        if create_result.returncode == 0:
            return release_tag

        create_stderr = (create_result.stderr or "").strip()
        create_stdout = (create_result.stdout or "").strip()
        if self._is_release_already_exists(create_stderr, create_stdout):
            return release_tag

        message = create_stderr or create_stdout or f"failed to create release {release_tag}"
        raise RuntimeError(f"gh command failed: {message}")
        return release_tag

    def _get_asset(self, namespace: ObjectNamespace, object_id: str, filename: str) -> dict:
        release_tag = self._ensure_release(namespace)
        assets = self._list_assets(release_tag)
        for asset in assets:
            if asset.get("name") == filename:
                return asset
        raise FileNotFoundError(f"{namespace.value}:{object_id}")

    def _find_asset(self, release_tag: str, filename: str) -> dict | None:
        for asset in self._list_assets(release_tag):
            if asset.get("name") == filename:
                return asset
        return None

    def _list_assets(self, release_tag: str) -> list[dict]:
        result = self._run_gh(
            ["release", "view", release_tag, "--repo", self.repo_ref, "--json", "assets"],
        )
        payload = json.loads(result.stdout or "{}")
        return payload.get("assets", [])

    def _release_tag(self, namespace: ObjectNamespace) -> str:
        return f"{self.release_prefix}-{namespace.value}"

    @staticmethod
    def _is_release_missing(stderr: str, stdout: str) -> bool:
        text = f"{stderr}\n{stdout}".lower()
        return "release not found" in text or "not found" in text

    @staticmethod
    def _is_release_already_exists(stderr: str, stdout: str) -> bool:
        text = f"{stderr}\n{stdout}".lower()
        return "already exists" in text

    def _run_gh(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [self.gh_bin, *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
            raise RuntimeError(f"gh command failed: {stderr}")
        return result
