import logging
import os
import threading
from threading import Timer
from typing import List, Optional

import yaml
from pydantic import ValidationError
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.providers_config import (
    ProcessConfig,
    ProviderConfig,
    ProvidersConfig,
)

logger = logging.getLogger(__name__)


class _ConfigFileHandler(FileSystemEventHandler):
    DEBOUNCE_DELAY = 0.5  # 500ms

    def __init__(self, adapter: ProvidersPort, config_path: str, config_filename: str):
        self.adapter = adapter
        self.config_path = os.path.abspath(config_path)
        self.config_filename = config_filename
        self._reload_timer = None

    def on_any_event(self, event):
        logger.info("File event: %s on %s", event.event_type, event.src_path)

        if event.is_directory:
            return
        src_path = str(event.src_path)
        # Robust checks for configmap updates (Kubernetes)
        if (
            src_path == self.config_path
            or src_path.endswith(self.config_filename)
            or "..data" in src_path
        ):
            self._debounced_reload()

    def _debounced_reload(self):
        if self._reload_timer:
            self._reload_timer.cancel()
        self._reload_timer = Timer(self.DEBOUNCE_DELAY, self.adapter.load_providers)
        self._reload_timer.start()


class ProviderConfigFileAdapter(ProvidersPort):
    def __init__(self, config_path: str):
        self._config_path = config_path
        self._lock = threading.Lock()
        self._providers: ProvidersConfig = ProvidersConfig(providers=[])

        self.load_providers()

    def start_file_watcher(self):
        observer = PollingObserver()
        config_dir = os.path.dirname(self._config_path)
        config_filename = os.path.basename(self._config_path)

        handler = _ConfigFileHandler(self, self._config_path, config_filename)

        observer.schedule(handler, path=config_dir, recursive=False)
        observer.start()

        self._observer = observer

    def stop_file_watcher(self):
        if hasattr(self, "_observer"):
            self._observer.stop()
            self._observer.join()

    def _atomic_update(self, new_providers: ProvidersConfig):
        """
        Atomar und thread-safe die Provider-Konfiguration aktualisieren, bei Fehlern Rollback.
        """
        with self._lock:
            old_providers = self._providers
            try:
                # Deep copy the new provider configuration
                self._providers = ProvidersConfig(
                    providers=[
                        provider.model_copy(deep=True)
                        for provider in new_providers.providers
                    ]
                )
            except Exception as update_error:
                logger.error("Error updating providers: %s", update_error)
                self._providers = old_providers
                raise update_error

    def load_providers(self):
        logger.info(f"(Re)Loading providers from {self._config_path}")

        try:
            with open(self._config_path, encoding="UTF-8") as file:
                content = yaml.safe_load(file)
                if content:
                    validated = ProvidersConfig(**content)
                    self._atomic_update(validated)
                    count = len(validated.providers)
                    names = [p.name for p in validated.providers]
                    logger.info(
                        "Providers (re)loaded: %d provider(s): %s",
                        count,
                        names,
                    )
                    # Log soft policy warnings for each configured process so
                    # operators catch unusual combinations early without having
                    # to wait for a job to expose the problem at runtime.
                    for provider in validated.providers:
                        for proc in provider.processes:
                            for warning in proc.policy_warnings():
                                logger.warning("[config] %s", warning)
                else:
                    # yaml.safe_load returns None for an empty file.
                    # This is a valid but likely unintended state — warn loudly
                    # so operators notice that no providers are available.
                    logger.warning(
                        "Providers file is empty (%s) — no providers configured, process routes will 404",
                        self._config_path,
                    )
        except FileNotFoundError:
            logger.error("Providers file not found: %s", self._config_path)
        except yaml.YAMLError as parse_error:
            logger.error("Failed to parse providers file: %s", parse_error)
        except ValidationError as validation_error:
            logger.error("Validation error in providers file: %s", validation_error)
        except Exception as unexpected_error:
            logger.error("Unexpected error loading providers: %s", unexpected_error)

    def get_providers(self) -> List[ProviderConfig]:
        with self._lock:
            return list(self._providers.providers)

    def get_provider(self, provider_name: str) -> Optional[ProviderConfig]:
        with self._lock:
            for provider in self._providers.providers:
                if provider.name == provider_name:
                    return provider
            return None

    def get_process_config(
        self, provider_name: str, process_id: str
    ) -> Optional[ProcessConfig]:
        with self._lock:
            provider = self.get_provider(provider_name)
            if provider:
                for process in provider.processes:
                    if process.id == process_id:
                        return process
            return None

    def list_providers(self) -> List[str]:
        with self._lock:
            return [provider.name for provider in self._providers.providers]

    def get_processes(self, provider_name: str) -> List[str]:
        with self._lock:
            provider = self.get_provider(provider_name)
            if provider:
                return [process.id for process in provider.processes]
            return []

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        with self._lock:
            process = self.get_process_config(provider_name, process_id)
            if process:
                return not process.exclude
            return False
