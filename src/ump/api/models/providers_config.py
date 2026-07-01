from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

TransmissionModePolicy = Literal[
    "pass-through",
    "emulate-ref",
    "emulate-ref-only",
    "value-only",
]

# a type alias to give context to an otherwise generic str
ProviderName: TypeAlias = Annotated[
    str,
    Field(description=("The name of the provider. This should be a valid identifier.")),
]


class GraphProperties(BaseModel):
    root_path: str = Field(
        alias="root-path",
        description=(
            "If the results are stored in Geoserver,"
            "you can specify the object path to the "
            "feature collection using root-path."
            "Use dots to separate a path with several "
            "components: root-path: result.some_obj.some_features."
        ),
    )
    x_path: str = Field(
        alias="x-path",
        description=(
            "If the results are stored in Geoserver,"
            "you can specify the object path to the "
            "feature collection using x-path."
            "Use dots to separate a path with several "
            "components: x-path: result.some_obj.some_features."
        ),
    )
    y_path: str = Field(
        alias="y-path",
        description=(
            "If the results are stored in Geoserver,"
            "you can specify the graph properties using "
            "graph-properties."
        ),
    )


class ProcessConfig(BaseModel):
    description: str | None = None
    version: str | None = None
    result_storage: Literal["geoserver", "remote", "ldproxy"] = Field(
        alias="result-storage"
    )
    transmission_mode_policy: TransmissionModePolicy = Field(
        default="pass-through",
        alias="transmission-mode-policy",
        description=(
            "UMP transmission mode policy. Allowed values: pass-through, "
            "emulate-ref, emulate-ref-only, value-only."
        ),
    )
    exclude: bool = False
    result_path: str | None = Field(
        default=None,
        alias="result-path",
        description=(
            "If the results should be stored in Geoserver,"
            "you can specify the object path to the "
            "feature collection using result-path."
            "Use dots to separate a path with several "
            "components: result-path: result.some_obj.some_features."
        ),
    )
    graph_properties: GraphProperties | None = Field(
        default=None,
        alias="graph-properties",
        description=(
            "If the results are stored in Geoserver,"
            "you can specify the graph properties using "
            "graph-properties."
        ),
    )
    anonymous_access: bool = Field(
        alias="anonymous-access",
        default=False,
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
            "cached based in inputs"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_transmission_mode(cls, data):
        if isinstance(data, dict) and "transmissionMode" in data:
            raise ValueError(
                "'transmissionMode' in providers config is no longer supported. "
                "Use 'transmission-mode-policy' and set transmissionMode "
                "in the execute request body."
            )
        return data


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
    name: str
    server_url: HttpUrl = Field(
        alias="url",
        description=(
            "The URL of the model server pointing to an OGC Processes api. "
            "It should be a valid HTTP or HTTPS URL with path to the landing page."
        ),
    )
    timeout: int = Field(
        default=60,
        description=("Timeout in seconds for the model server. Default is 60 seconds."),
    )
    authentication: AuthConfig = Field(default_factory=NoAuthConfig)
    processes: dict[ProviderName, ProcessConfig] = Field(
        description=(
            "Processes are defined as a dictionary with process name as key "
            "and process properties as value."
        )
    )

    @field_validator("server_url", mode="before")
    def ensure_trailing_slash(cls, value: str) -> HttpUrl:
        """Ensure server_url has a trailing slash."""

        if not str(value).endswith("/"):
            value += "/"
        return HttpUrl(value)


# a TypeAlias to give context to an otherwise generic dict
ModelServers: TypeAlias = Annotated[
    dict[str, ProviderConfig],
    Field(
        description=(
            "A dictionary of model servers with their names as keys and "
            "ModelServer objects as values."
        )
    ),
]

# a TypeAdapter allows us to use pydantics model_validate method
# on arbitrary python types
model_servers_adapter: TypeAdapter[ModelServers] = TypeAdapter(ModelServers)

if __name__ == "__main__":
    print(model_servers_adapter.json_schema())
