"""RemoteAuthAdapter — converts provider AuthConfig to HTTP headers.

This adapter owns all credential-encoding knowledge so the core stays
infrastructure-free.  Every auth type is reduced to a plain headers dict;
``HttpClientPort`` never sees aiohttp or base64 details.
"""

from __future__ import annotations

import base64

from ump.core.interfaces.remote_auth import ProviderCredentials, RemoteAuthPort
from ump.core.models.providers_config import AuthConfig


class RemoteAuthAdapter(RemoteAuthPort):
    """Stateless adapter — safe to share as a singleton.

    Supported auth types:
      NoAuth            → empty headers (pass-through)
      BasicAuth         → Authorization: Basic base64(user:pass)
      BearerToken       → Authorization: Bearer <token>
      ApiKey            → {key_name}: <key_value>
    """

    def resolve(self, auth_config: AuthConfig) -> ProviderCredentials:
        auth_type = getattr(auth_config, "type", "NoAuth")

        if auth_type == "BasicAuth":
            raw = f"{auth_config.user}:{auth_config.password.get_secret_value()}"
            encoded = base64.b64encode(raw.encode()).decode()
            return ProviderCredentials(headers={"Authorization": f"Basic {encoded}"})

        if auth_type == "BearerToken":
            token = auth_config.token.get_secret_value()
            return ProviderCredentials(headers={"Authorization": f"Bearer {token}"})

        if auth_type == "ApiKey":
            return ProviderCredentials(
                headers={auth_config.key_name: auth_config.key_value.get_secret_value()}
            )

        # NoAuth or any unknown type → no additional headers
        return ProviderCredentials()
