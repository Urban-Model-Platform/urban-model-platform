from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)


class GraphProperties(BaseModel):
    """Properties for graph-based result visualization"""

    root_path: str = Field(
        alias="root-path",
        description=(
            "If the results are stored in Geoserver, "
            "you can specify the object path to the "
            "feature collection using root-path. "
            "Use dots to separate a path with several "
            "components: root-path: result.some_obj.some_features."
        ),
    )
    x_path: str = Field(
        alias="x-path",
        description=(
            "Object path to the x-coordinate field. "
            "Use dots to separate path components."
        ),
    )
    y_path: str = Field(
        alias="y-path",
        description=(
            "Object path to the y-coordinate field. "
            "Use dots to separate path components."
        ),
    )


class ProcessConfig(BaseModel):
    """Configuration for an individual process.

    UMP acts as an authoritative execution proxy on two distinct levels:

    **Level 1 — Process description (what UMP advertises to clients)**

    UMP owns and may rewrite the process description it returns to clients.
    A ``transmission_mode_policy`` of ``emulate-ref`` causes UMP to add
    ``transmissionMode: reference`` to the advertised ``outputTransmission``
    even if the remote server does not support it natively.  Clients trust
    the UMP-authored process description, not the remote's.

    **Level 2 — Remote request (how UMP fulfils the client's intent)**

    UMP reads the client's *intent* from the execute request (e.g. the client
    wants ``transmissionMode: reference``) but does **not** forward the client's
    request body to the remote unchanged once policies are active.  Instead,
    UMP constructs its own canonical request to the remote: one that the remote
    can actually service (e.g. ``transmissionMode: value``) and that will allow
    UMP to fulfil the client's original intent (e.g. by storing the value result
    and returning a reference link).  The client's inputs are forwarded
    unchanged; the execution-mode fields (``transmissionMode``, ``response``) in
    the request UMP sends to the remote are determined by UMP's policies, not
    copied from the client.

    ``result_storage``: where results are physically stored when UMP takes
    ownership of them (``remote`` = UMP does not store; ``ldproxy`` /
    ``geoserver`` = UMP stores).

    Hard constraints (validated at config load, prevents startup):
      - ``emulate-ref`` / ``emulate-ref-only`` require a non-remote store —
        UMP cannot fulfil a reference link without somewhere to write the data.
      - ``force-raw`` cannot be combined with ``emulate-ref*`` — UMP needs a
        parseable structured response from the remote to extract and store the
        result; an opaque raw byte stream cannot be parsed.

    Storage is limited to outputs whose mediaType is in the supported whitelist
    (``application/geo+json``, ``application/flatgeobuf``).  Outputs in other
    formats raise ``UnsupportedResultError`` at storage time, not at config load.
    Use ``store-outputs`` to name exactly which output IDs to store; when omitted
    UMP auto-detects by checking every output's mediaType against the whitelist.

    Soft warnings (logged at config load, does not prevent startup):
      - A non-remote store with ``pass-through`` / ``value-only`` policy will
        never be activated.
      - ``store-outputs`` is specified but ``transmission-mode-policy`` does not
        activate storage — the setting will be ignored.
    """

    id: str = Field(description="The unique identifier for this process")
    description: str | None = None
    version: str | None = None
    result_storage: Literal["geoserver", "ldproxy", "remote"] = Field(
        default="remote",
        alias="result-storage",
        description=(
            "Where UMP stores results when it takes ownership of them. "
            "'remote' means UMP does not store (pass-through). "
            "'ldproxy' and 'geoserver' activate UMP-side storage."
        ),
    )
    transmission_mode_policy: Literal[
        "pass-through", "emulate-ref", "emulate-ref-only", "value-only"
    ] = Field(
        default="pass-through",
        alias="transmission-mode-policy",
        description=(
            "Controls (1) what UMP advertises in the process description and "
            "(2) how UMP constructs its own canonical request to the remote. "
            "'pass-through': advertise the remote's native capabilities unchanged; "
            "forward the client's transmissionMode preference to the remote. "
            "'emulate-ref': add transmissionMode=reference to the advertised "
            "outputTransmission even if the remote does not support it natively; "
            "when the client requests reference UMP sends value to the remote, "
            "stores the result, and returns a ref link. "
            "'emulate-ref-only': advertise only reference; always send value to the "
            "remote, always store, always return a ref link; reject client value requests. "
            "'value-only': advertise only value; send value to the remote; "
            "reject client reference requests."
        ),
    )
    response_mode_policy: Literal["pass-through", "force-document", "force-raw"] = (
        Field(
            default="pass-through",
            alias="response-mode-policy",
            description=(
                "Controls the 'response' field in the canonical request UMP sends "
                "to the remote server (independent of what the client requested). "
                "'pass-through': use whatever the client's execute request specified. "
                "'force-document': always request a structured JSON document from the remote, "
                "regardless of the client's preference; required when result storage is active "
                "so UMP can parse the response and extract the result. "
                "'force-raw': always request raw bytes from the remote; "
                "incompatible with result storage."
            ),
        )
    )
    exclude: bool = False
    store_outputs: list[str] | None = Field(
        default=None,
        alias="store-outputs",
        description=(
            "Which output IDs from the remote server's document response to "
            "forward to the result store.  Each entry is the key as it appears "
            "at the top level of the OGC results document.  Dot-notation may be "
            "used to navigate into nested JSON values within an output "
            "(e.g. 'results.voronoi_diagram' for "
            "{'results': {'voronoi_diagram': <FeatureCollection>}}).  "
            "When None (the default) UMP auto-detects which outputs to store by "
            "checking each output's mediaType against the supported format "
            "whitelist (application/geo+json, application/flatgeobuf).  "
            "Only meaningful when transmission-mode-policy is 'emulate-ref' or "
            "'emulate-ref-only'; ignored otherwise."
        ),
    )
    result_path: str | None = Field(
        default=None,
        alias="result-path",
        description=(
            "If the results should be stored in Geoserver, "
            "you can specify the object path to the "
            "feature collection using result-path. "
            "Use dots to separate a path with several "
            "components: result-path: result.some_obj.some_features."
        ),
    )
    graph_properties: GraphProperties | None = Field(
        default=None,
        alias="graph-properties",
        description=(
            "If the results are stored in Geoserver, "
            "you can specify the graph properties using "
            "graph-properties."
        ),
    )
    ttw_job_done: float | None = Field(
        default=None,
        alias="ttw-job-done",
        description=(
            "Time to wait (in seconds) until remote job is finished. "
            "If not set, falls back to provider-level ttw_job_done. "
            "Allows per-process fine-tuning for processes with different "
            "computational footprints on the same provider."
        ),
    )
    poll_interval: float | None = Field(
        default=None,
        alias="poll-interval",
        description=(
            "Interval in seconds between remote status polling requests. "
            "If not set, falls back to JobManagerConfig default. "
            "Allows per-process tuning for different computational characteristics."
        ),
    )
    anonymous_access: bool = Field(
        default=False,
        alias="anonymous-access",
        description=(
            "If set to True, the process can be seen and run "
            "by anonymous users. Jobs and layers created "
            "by anonymous users will be cleaned up after some time."
        ),
    )
    deterministic: bool = Field(
        default=False,
        description=(
            "If set to True, the process is regarded deterministic. "
            "This means that such a process will always produce "
            "the same result for the same input. So, outputs can be "
            "cached based on inputs"
        ),
    )

    @model_validator(mode="after")
    def check_policy_consistency(self) -> "ProcessConfig":
        """Raise ValueError for combinations that can never work correctly.

        Two hard constraints:
        1. Storing results requires an actual store — 'emulate-ref' and
           'emulate-ref-only' cannot function when result_storage is 'remote'.
        2. 'force-raw' tells UMP to accept an opaque byte stream from the
           remote, which means UMP cannot parse the response to extract and
           store results.  Combining it with store-activating policies is
           therefore always broken.
        """
        policy = self.transmission_mode_policy
        response = self.response_mode_policy
        storage = self.result_storage

        store_activating = policy in ("emulate-ref", "emulate-ref-only")

        if store_activating and storage == "remote":
            raise ValueError(
                f"Process '{self.id}': transmission-mode-policy={policy!r} "
                f"requires a result store (result-storage must not be 'remote'), "
                f"but result-storage={storage!r}.  "
                f"Set result-storage to 'ldproxy' (or 'geoserver') to fix this."
            )

        if response == "force-raw" and store_activating:
            raise ValueError(
                f"Process '{self.id}': response-mode-policy='force-raw' is "
                f"incompatible with transmission-mode-policy={policy!r}.  "
                f"Result storage requires a parseable structured response; "
                f"use response-mode-policy='force-document' instead."
            )

        return self

    def policy_warnings(self) -> list[str]:
        """Return human-readable warnings for non-fatal but unusual combinations.

        Unlike the model validator, these do not prevent loading — they are
        logged as warnings to help operators catch misconfigured processes early.
        """
        found: list[str] = []
        policy = self.transmission_mode_policy
        storage = self.result_storage
        store_activating = policy in ("emulate-ref", "emulate-ref-only")

        # A store is configured but the policy will never activate it.
        if storage != "remote" and policy in ("pass-through", "value-only"):
            found.append(
                f"Process '{self.id}': result-storage={storage!r} is configured "
                f"but will never be used because "
                f"transmission-mode-policy={policy!r} does not activate the store."
            )

        # store-outputs is specified but will be silently ignored because the
        # policy never triggers storage.  Warn so operators catch mismatches
        # between store-outputs and transmission-mode-policy at startup.
        if self.store_outputs is not None and not store_activating:
            found.append(
                f"Process '{self.id}': store-outputs={self.store_outputs!r} is "
                f"configured but transmission-mode-policy={policy!r} does not "
                f"activate the result store, so store-outputs will be ignored."
            )

        return found


class BasicAuthConfig(BaseModel):
    type: Literal["BasicAuth"]
    user: str
    password: SecretStr


class ApiKeyAuthConfig(BaseModel):
    type: Literal["ApiKey"]
    key_name: str
    key_value: SecretStr


class BearerTokenAuthConfig(BaseModel):
    type: Literal["BearerToken"]
    token: SecretStr


class NoAuthConfig(BaseModel):
    type: Literal["NoAuth"] = "NoAuth"


AuthConfig = BasicAuthConfig | ApiKeyAuthConfig | BearerTokenAuthConfig | NoAuthConfig


class ProviderConfig(BaseModel):
    """Configuration for a single provider"""

    name: str = Field(description="The name of the provider (e.g., 'infrared')")
    url: HttpUrl = Field(
        description=(
            "The URL of the model server pointing to an OGC Processes API. "
            "It should be a valid HTTP or HTTPS URL with path to the landing page."
        )
    )
    ttw_job_done: float = Field(
        default=1500.0,
        alias="ttw-job-done",
        description=(
            "Time to wait (in seconds) until remote job is finished. "
            "Serves as the provider-wide default when ProcessConfig.ttw_job_done "
            "is not set. Allows per-provider customization of timeout behavior."
        ),
    )
    authentication: AuthConfig = Field(
        default_factory=NoAuthConfig,
        description="Authentication configuration for this provider",
    )
    processes: list[ProcessConfig] = Field(
        default_factory=list,
        description="List of processes available from this provider",
    )

    @field_validator("url", mode="before")
    def ensure_trailing_slash(cls, value: str) -> HttpUrl:
        """Ensure url has a trailing slash."""
        if not str(value).endswith("/"):
            value += "/"
        return HttpUrl(value)


class ProvidersConfig(BaseModel):
    """Root configuration containing all providers"""

    providers: list[ProviderConfig] = Field(
        description="List of provider configurations"
    )
