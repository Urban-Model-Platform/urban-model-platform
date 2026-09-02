"""Filesystem implementation of ``EntityConfigBackendPort`` (dev / Docker).

Writes ldproxy entity YAML as ordinary files under the injected entities
directory (by default the store's ``entities/instances``)::

    {entities}/providers/{provider_id}.yml
    {entities}/services/{service_id}.yml

Every write goes through ``atomic_write_text`` (temp file + ``os.replace``) so
ldproxy — which watches the store — never observes a half-written file.

This backend is only ever used in single-instance deployments (local, Docker);
the multi-pod case uses the Kubernetes backend.  The version token is therefore
a lightweight optimistic guard against concurrent writers *within one process*
(the service registry runs its read-modify-write under an in-process lock), not
a cross-host coordination mechanism.  We derive it from a hash of the file
content: it changes iff the content changes, which is deterministic (unlike a
filesystem mtime, whose resolution can be too coarse to distinguish rapid
successive writes) and semantically exact — two writers producing identical
content do not conflict, because neither loses anything.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ump.adapters.result_storage.atomic_fs import atomic_write_text
from ump.adapters.result_storage.entity_config_backend import (
    ConfigConflict,
    EntityConfigBackendPort,
)


class FilesystemEntityConfigBackend(EntityConfigBackendPort):
    """Persist entity YAML as files under ``entities_path``."""

    def __init__(self, entities_path: str | Path) -> None:
        # Directory that directly contains `providers/` and `services/`. It is
        # the ldproxy store's `entities/instances` by default, but arrives here
        # as a finished path so this backend never derives store layout itself.
        self._entities_dir = Path(entities_path)

    # -- Provider entity ----------------------------------------------------

    def write_provider_entity(self, provider_id: str, yaml_text: str) -> None:
        path = self._provider_path(provider_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, yaml_text)

    def delete_provider_entity(self, provider_id: str) -> None:
        # missing_ok makes this idempotent — cleanup can run unconditionally.
        self._provider_path(provider_id).unlink(missing_ok=True)

    # -- Shared service entity ----------------------------------------------

    def read_service_entity(self, service_id: str) -> tuple[str, str] | None:
        path = self._service_path(service_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return text, self._version_of(text)

    def write_service_entity(
        self, service_id: str, yaml_text: str, expected_version: str | None
    ) -> None:
        path = self._service_path(service_id)
        self._check_version(path, expected_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, yaml_text)

    # -- Internal helpers ---------------------------------------------------

    def _provider_path(self, provider_id: str) -> Path:
        return self._entities_dir / "providers" / f"{provider_id}.yml"

    def _service_path(self, service_id: str) -> Path:
        return self._entities_dir / "services" / f"{service_id}.yml"

    def _check_version(self, path: Path, expected_version: str | None) -> None:
        """Raise ConfigConflict if the on-disk version differs from expected.

        ``expected_version=None`` means "I expect this file not to exist yet";
        a token means "I expect the file to still hold this exact content".
        """
        try:
            current: str | None = self._version_of(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = None
        if current != expected_version:
            raise ConfigConflict(
                f"Service entity {path.name} changed "
                f"(expected version {expected_version!r}, found {current!r})"
            )

    @staticmethod
    def _version_of(text: str) -> str:
        """Return an opaque version token: a hash of the file content."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
