# ump/core/managers/process_manager.py
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional, cast

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.processes import ProcessesPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.managers.job_manager import JobManager
from ump.core.managers.process_cache import ProcessCache, ProcessListCache
from ump.core.models.link import Link
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.process import Process, ProcessList, ProcessSummary
from ump.core.models.providers_config import ProviderConfig
from ump.core.settings import app_settings, logger
from ump.core.utils.link_rewriter import rewrite_links_to_local


class ProcessManager(ProcessesPort):
    def __init__(
        self,
        provider_config_service: ProvidersPort,
        http_client: HttpClientPort,
        process_id_validator: ProcessIdValidatorPort,
        remote_auth: RemoteAuthPort | None = None,
        cache_expiry_seconds: int = 300,
    ) -> None:
        self.provider_config_service = provider_config_service
        self.http_client = http_client
        self.process_id_validator = process_id_validator
        self._remote_auth = remote_auth
        self._process_cache = ProcessListCache[ProcessSummary](
            expiry_seconds=cache_expiry_seconds
        )
        # cache individual process descriptions by id
        self._process_cache_by_id = ProcessCache[Process](
            expiry_seconds=cache_expiry_seconds
        )
        # Will be set by composition root after instantiation.
        self.job_manager: Optional[JobManager] = None

        # A pipeline of handlers (functions) that transform a fetched process dict.
        # Each handler has signature: handler(provider_name: str, proc: Dict[str, Any]) -> Dict[str, Any]
        # Handlers can raise ValueError to indicate the process should be skipped.
        self._process_handlers: List[
            Callable[[str, Dict[str, Any]], Dict[str, Any]]
        ] = [
            self._handle_process_id,
            # Fill in spec-required defaults if upstream omits optional-but-expected
            # sections we rely on (lenient mode). This must run early so later
            # handlers can assume presence.
            self._handle_fill_defaults,
            # Remove / ignore malformed metadata entries so validation doesn't fail.
            self._handle_sanitize_metadata,
            self._handle_rewrite_links,
        ]

    def add_process_handler(
        self, handler: Callable[[str, Dict[str, Any]], Dict[str, Any]]
    ):
        """Register an additional process handler at runtime."""
        self._process_handlers.append(handler)

    # --- Default handlers -------------------------------------------------
    def _handle_process_id(
        self, provider_name: str, proc: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Enforce and create the canonical process id using the injected validator.

        Raises ValueError if the id cannot be created/validated.
        """
        raw_proc_id = proc.get("id")
        if raw_proc_id is None:
            raise ValueError("missing process id")
        # Avoid double-prefixing: if the remote returned an ID that already carries
        # our provider prefix (e.g. another UMP instance), keep it as-is.
        full_proc_id = self._to_canonical_id(provider_name, raw_proc_id)
        proc["id"] = full_proc_id
        return proc

    def _handle_rewrite_links(
        self, provider_name: str, proc: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Rewrite remote links to local links if enabled in settings.
        This handler is a no-op if links are missing or rewriting is disabled.
        """

        if not app_settings.UMP_REWRITE_REMOTE_LINKS:
            return proc

        if "links" not in proc or not isinstance(proc.get("links"), list):
            return proc
        try:
            from ump.core.models.link import Link
            from ump.core.utils.link_rewriter import rewrite_links_to_local

            proc_links = [Link(**l) for l in proc.get("links", [])]
            proc["links"] = [
                l.model_dump()
                for l in rewrite_links_to_local(str(proc.get("id")), proc_links)
            ]
        except Exception:
            logger.warning(f"Failed to rewrite links for process {proc.get('id')}")
            # leave them empty
            proc["links"] = []

        return proc

    def _handle_fill_defaults(
        self, provider_name: str, proc: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Leniently accept partially non-spec processes by synthesizing
        reasonable defaults. We NEVER drop a process solely because fields like
        links, version or jobControlOptions are missing – upstream catalogs are
        often sparse.

        Rules:
        - version: default to "0.0.0" if missing
        - jobControlOptions: default to ["sync-execute", "async-execute"] (common superset)
        - outputTransmission: default to ["value"]
        - links: if missing/empty insert a minimal 'self' placeholder; will later be
          rewritten if link rewriting enabled. We insert the canonical id after
          the id handler has run (hence this handler is placed after _handle_process_id).
        - keywords: ensure list type
        - metadata: ensure list type
        """
        # version
        proc.setdefault("version", "0.0.0")

        # jobControlOptions
        jco = proc.get("jobControlOptions")
        if not isinstance(jco, list) or not jco:
            proc["jobControlOptions"] = ["sync-execute", "async-execute"]

        # outputTransmission
        ot = proc.get("outputTransmission")
        if not isinstance(ot, list) or not ot:
            proc["outputTransmission"] = ["value"]

        # links
        links = proc.get("links")
        if not isinstance(links, list) or not links:
            # minimal placeholder; may be rewritten later; rel 'self' conventional
            proc["links"] = [
                {
                    "href": f"/processes/{proc.get('id', '')}",
                    "rel": "self",
                    "type": "application/json",
                    "title": proc.get("title") or proc.get("id", "process"),
                }
            ]

        # keywords / metadata normalization to lists
        if "keywords" in proc and not isinstance(proc["keywords"], list):
            proc["keywords"] = [proc["keywords"]]
        if "metadata" in proc and not isinstance(proc["metadata"], list):
            proc["metadata"] = [proc["metadata"]]

        return proc

    def _handle_sanitize_metadata(
        self, provider_name: str, proc: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Drop malformed metadata entries (missing required keys) instead of failing.

        Upstream catalogs sometimes place arbitrary provenance dicts into `metadata`.
        Our `Metadata` model expects: title, role, href. If an entry lacks any of
        these keys we silently drop it and log once per process (debug level) with
        a count of removed entries. If after filtering nothing remains we remove
        the metadata key entirely so Pydantic treats it as absent.

        We apply the same logic to output metadata (each item in `outputs`).
        """
        removed_count = 0
        # Process-level metadata
        if isinstance(proc.get("metadata"), list):
            valid_meta: List[Dict[str, Any]] = []
            for m in proc["metadata"]:
                if not isinstance(m, dict):
                    removed_count += 1
                    continue
                if not all(k in m for k in ("title", "role", "href")):
                    removed_count += 1
                    continue
                valid_meta.append(m)
            if valid_meta:
                proc["metadata"] = valid_meta
            else:
                proc.pop("metadata", None)
        # Outputs metadata (if detailed description already fetched)
        if isinstance(proc.get("outputs"), dict):
            for out_id, out_def in list(proc["outputs"].items()):
                meta_list = out_def.get("metadata")
                if isinstance(meta_list, list):
                    valid_out_meta: List[Dict[str, Any]] = []
                    for m in meta_list:
                        if not isinstance(m, dict):
                            removed_count += 1
                            continue
                        if not all(k in m for k in ("title", "role", "href")):
                            removed_count += 1
                            continue
                        valid_out_meta.append(m)
                    if valid_out_meta:
                        out_def["metadata"] = valid_out_meta
                    else:
                        # Delete malformed metadata entirely
                        out_def.pop("metadata", None)
        if removed_count:
            logger.debug(
                f"Sanitized metadata for process '{proc.get('id')}' from provider '{provider_name}': removed {removed_count} malformed entries"
            )
        return proc

    def _to_canonical_id(self, provider_name: str, configured_id: str) -> str:
        """Compute the UMP-local canonical process ID.

        If the configured ID already starts with ``{provider_name}:`` (e.g.
        because the remote is another UMP instance), it is used as-is to avoid
        double-prefixing.  Otherwise the provider prefix is prepended.
        """
        if configured_id.startswith(f"{provider_name}:"):
            return configured_id
        return self.process_id_validator.create(provider_name, configured_id)

    def _find_remote_id(self, provider_name: str, canonical_id: str) -> str:
        """Look up the remote server's process ID for a given UMP canonical ID.

        The configured ``id`` in providers.yaml is the verbatim remote ID.
        Fallback: strip the provider prefix (old behaviour, for servers that use
        bare IDs).
        """
        provider = self.provider_config_service.get_provider(provider_name)
        if provider:
            for proc_cfg in provider.processes:
                if self._to_canonical_id(provider_name, proc_cfg.id) == canonical_id:
                    return proc_cfg.id  # verbatim configured remote ID
        # Fallback: bare id without provider prefix
        try:
            _, bare = self.process_id_validator.extract(canonical_id)
            return bare
        except ValueError:
            return canonical_id

    def _configured_process_ids(self, provider_name: str) -> List[tuple[str, str]]:
        """Return (remote_id, canonical_id) pairs for all non-excluded processes."""
        provider = self.provider_config_service.get_provider(provider_name)
        result: List[tuple[str, str]] = []
        for proc_cfg in provider.processes:
            if getattr(proc_cfg, "exclude", False):
                continue
            remote_id = proc_cfg.id  # verbatim; used in the remote URL
            canonical_id = self._to_canonical_id(provider_name, remote_id)
            result.append((remote_id, canonical_id))
        return result

    def _cache_process(self, model: Process) -> None:
        self._process_cache_by_id.set(model.pid, model)
        logger.debug(f"Cached process '{model.pid}' in per-process cache")
        if ":" in model.pid:
            bare = model.pid.split(":", 1)[1]
            self._process_cache_by_id.set(bare, model)
            logger.debug(f"Also cached process under bare id '{bare}'")

    async def _fetch_process(self, provider_name: str, raw_id: str) -> Process:
        provider = self.provider_config_service.get_provider(provider_name)
        url = str(provider.url).rstrip("/") + f"/processes/{raw_id}"
        auth_headers = (
            self._remote_auth.resolve(provider.authentication).headers
            if self._remote_auth
            else {}
        )
        try:
            data = await self.http_client.get(url, headers=auth_headers or None)
            proc = dict(data)
            for handler in self._process_handlers:
                proc = handler(provider_name, proc)
            model = Process(**proc)
            self._cache_process(model)
            return model
        except OGCProcessException:
            raise
        except Exception as fetch_error:
            logger.error(
                f"Failed to fetch process '{raw_id}' from provider '{provider_name}': {fetch_error}"
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Error",
                    status=502,
                    detail=(
                        f"Could not retrieve process '{raw_id}' from provider '{provider_name}'"
                    ),
                    instance=None,
                )
            )

    def _resolve_provider_for_bare_id(self, process_id: str) -> tuple[str, str]:
        for provider_name in self.provider_config_service.list_providers():
            provider = self.provider_config_service.get_provider(provider_name)
            for proc_cfg in provider.processes:
                if getattr(proc_cfg, "exclude", False):
                    continue
                # Match against the remote ID or the canonical ID
                if (
                    proc_cfg.id == process_id
                    or self._to_canonical_id(provider_name, proc_cfg.id) == process_id
                ):
                    return provider_name, proc_cfg.id
        raise OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Not Found",
                status=404,
                detail=f"Process '{process_id}' not found",
                instance=None,
            )
        )

    async def get_all_processes(self) -> ProcessList:
        """
        Fetches processes for all providers concurrently using asyncio.gather.
        Aggregates all results into a single ProcessList.
        """
        provider_names = self.provider_config_service.list_providers()

        # Evict cache entries for providers that are no longer configured
        # (handles providers.yaml reload where a provider was removed or renamed)
        self._process_cache.invalidate_stale_keys(set(provider_names))

        provider_summaries: Dict[str, List[ProcessSummary]] = {}
        fetch_plan: List[
            tuple[str, str, str]
        ] = []  # (provider_name, remote_id, canonical_id)

        for provider_name in provider_names:
            cached = self._process_cache.get(provider_name)
            if cached is not None:
                provider_summaries[provider_name] = list(cached)
                continue

            configured = self._configured_process_ids(provider_name)
            provider_summaries[provider_name] = []

            for remote_id, canonical_id in configured:
                fetch_plan.append((provider_name, remote_id, canonical_id))

        # Fetch all processes directly (bypasses per-process cache to use remote_id)
        tasks = [
            self._fetch_process(provider_name, remote_id)
            for provider_name, remote_id, _ in fetch_plan
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (provider_name, remote_id, canonical_id), result in zip(
            fetch_plan, results
        ):
            if isinstance(result, Exception):
                logger.warning(
                    f"Failed to fetch process '{canonical_id}' from provider '{provider_name}': {result}"
                )
                continue
            process = cast(Process, result)
            summary = ProcessSummary(**process.model_dump())
            provider_summaries.setdefault(provider_name, []).append(summary)

        for provider_name, summaries in provider_summaries.items():
            # Bug fix: never cache an empty list — always retry on the next request
            # so that a providers.yaml fix takes effect without waiting for TTL expiry.
            if summaries:
                self._process_cache.set(provider_name, summaries)

        all_processes = [
            proc for summaries in provider_summaries.values() for proc in summaries
        ]
        return ProcessList(processes=all_processes)

    async def get_process(self, process_id: str) -> Process:
        """
        Retrieve a single process by id. The id may include a provider prefix
        (e.g. 'provider:proc'). If a provider prefix is present we look up the
        configured remote ID for that provider and fetch directly. Otherwise we
        search across configured providers for a matching process.
        """
        cached = self._process_cache_by_id.get(process_id)
        if cached is not None:
            logger.info(f"Process cache hit for id '{process_id}'")
            return cached

        logger.info(f"Process cache miss for id '{process_id}'")

        try:
            provider_name, _ = self.process_id_validator.extract(process_id)
        except ValueError:
            provider_name, _ = self._resolve_provider_for_bare_id(process_id)

        # Use the verbatim configured remote ID, not the bare stripped ID
        remote_id = self._find_remote_id(provider_name, process_id)
        return await self._fetch_process(provider_name, remote_id)

    async def cleanup(self) -> None:
        """Cleanup resources"""
        await self.http_client.close()

    async def execute_process(
        self, process_id: str, payload: dict, headers: dict
    ) -> dict:
        """Delegate execution to JobManager (always async semantics Step 1).

        `payload` is the full normalized ExecuteRequest provider payload (includes
        inputs, outputs, response preferences, subscriber callbacks). JobManager
        will decide which parts to persist locally (e.g., inputs metadata) while
        forwarding the entire payload downstream.
        """
        if self.job_manager is None:
            logger.error("JobManager not attached to ProcessManager")
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Server Error",
                    status=500,
                    detail="Contact admin",
                    instance=None,
                )
            )
        return await self.job_manager.run_execution_pipeline(
            process_id, payload or {}, headers
        )

    def attach_job_manager(self, job_manager: JobManager) -> None:
        """Attach the JobManager once; explicit wiring keeps type hints simple."""
        self.job_manager = job_manager
