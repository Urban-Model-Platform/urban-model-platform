"""OGC API Processes Execute request models.

Derived from execute.yaml and related schema fragments.

Role of this module
-------------------
``ExecuteRequest.from_raw()`` is a **structural validator only**.  It rejects
payloads that are obviously malformed (missing ``value``/``href`` on an input,
an ``href`` that is not a URL, etc.) before the request reaches any remote
server.  It does **not** validate inputs against a specific process description
— that is the job of an optional ``ValidateInputsStep`` (planned, see
Feature X in the refactoring guide).

UMP does **not** transform the execute request payload.  After passing
structural validation the original raw dict is forwarded to the remote server
unchanged.  ``as_provider_payload()`` intentionally does not exist; any future
rewriting (``transmission-mode-policy``, ``response-mode-policy``) belongs in
explicit pipeline steps introduced by Feature VIII.

Models
------
- ``InlineOrRef``       — one input value: either inline data or an OGC link
- ``OutputSpec``        — per-output format + transmissionMode request
- ``ExecuteRequest``    — top-level request body with structural validation
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
    model_validator,
)


class ResponseMode(str, Enum):
    raw = "raw"
    document = "document"


class TransmissionMode(str, Enum):
    inline = "value"  # inline result value
    reference = "reference"  # remote URL reference


class InlineOrRef(BaseModel):
    """Represents either inline data or a reference link.

    We collapse multiple OGC schema oneOf choices into a single flexible model.

    OGC distinguishes two patterns:
    - ``qualifiedInputValue``: ``{ value, mediaType, encoding, schema }``
    - ``link``:               ``{ href, type, rel, title, hreflang }``

    Known named fields (``value``, ``href``, ``format``) are declared
    explicitly for validation.  Any additional OGC link fields (e.g. ``type``,
    ``rel``, ``hreflang``) are preserved transparently via ``extra = "allow"``
    so the forwarded payload is identical to what the client sent.
    """

    model_config = {"extra": "allow"}

    value: Any | None = Field(None, description="Inline value (scalar/object/array)")
    href: Optional[HttpUrl] = Field(None, description="External reference URL")
    format: Optional[str] = Field(None, description="Format identifier")

    @computed_field
    @property
    def is_reference(self) -> bool:
        return self.href is not None and self.value is None

    @computed_field
    @property
    def is_inline(self) -> bool:
        return self.value is not None

    @model_validator(mode="after")
    def ensure_value_or_href(self):
        if self.value is None and self.href is None:
            raise ValueError("InlineOrRef requires either 'value' or 'href'.")
        return self


class OutputSpec(BaseModel):
    format: Optional[dict] = Field(
        None, description="Requested output format / media type"
    )
    transmissionMode: Optional[TransmissionMode] = Field(
        None, description="inline (value) or reference"
    )


class SubscriberCallbacks(BaseModel):
    successUri: HttpUrl
    inProgressUri: Optional[HttpUrl] = None
    failedUri: Optional[HttpUrl] = None


class ExecuteRequest(BaseModel):
    inputs: Dict[str, Union[InlineOrRef, List[InlineOrRef]]] = Field(
        default_factory=dict,
        description="Map of input identifier to one or more inline/reference values",
    )
    outputs: Optional[Dict[str, OutputSpec]] = None
    response: ResponseMode = ResponseMode.raw
    subscriber: Optional[SubscriberCallbacks] = None

    @field_validator("inputs")
    def validate_inputs(cls, v):
        """Structural pre-validation: each input must be an InlineOrRef or a
        non-empty list thereof.  This check is process-agnostic — it catches
        obviously malformed payloads before they reach a remote server.  It
        does NOT validate against a process description schema.
        """
        for key, val in v.items():
            if isinstance(val, list):
                if not val:
                    raise ValueError(f"Input '{key}' list must not be empty")
                for item in val:
                    if not isinstance(item, InlineOrRef):
                        raise ValueError(
                            f"Input '{key}' list contains non InlineOrRef item"
                        )
            elif not isinstance(val, InlineOrRef):
                raise ValueError(
                    f"Input '{key}' must be InlineOrRef or list[InlineOrRef]"
                )
        return v

    # -------- Factory / normalization --------
    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "ExecuteRequest":
        """Construct an ExecuteRequest from a loosely structured raw dict.

        Coercion rules:
        - inputs primitive -> InlineOrRef(value=primitive)
        - inputs dict with keys 'value' or 'href' -> InlineOrRef(**fields)
        - inputs dict without those keys -> InlineOrRef(value=dict)
        - list values -> list[InlineOrRef] with same coercion

        The original ``raw`` dict is never mutated so it can safely be
        forwarded to a remote server after this call returns.
        """
        if not isinstance(raw, dict):
            raw = {}
        # Work on a shallow copy so we can replace raw["inputs"] without
        # mutating the caller's dict (which is forwarded verbatim later).
        working = dict(raw)
        inputs = working.get("inputs", {})
        if isinstance(inputs, dict):
            coerced: Dict[str, Any] = {}
            for k, v in inputs.items():
                if isinstance(v, list):
                    coerced[k] = [cls._coerce_inline(item) for item in v]
                else:
                    coerced[k] = cls._coerce_inline(v)
            working["inputs"] = coerced
        return cls(**working)

    @staticmethod
    def _coerce_inline(value: Any) -> InlineOrRef:
        if isinstance(value, InlineOrRef):
            return value
        if isinstance(value, dict) and ("value" in value or "href" in value):
            # Forward the full dict so extra OGC fields (type, rel, …) are preserved.
            return InlineOrRef(**value)
        if not isinstance(value, (list, tuple, dict)):
            return InlineOrRef(value=value, href=None, format=None)
        if isinstance(value, dict):
            return InlineOrRef(value=value, href=None, format=None)
        # Fallback: treat as raw value (e.g. tuple)
        return InlineOrRef(value=value, href=None, format=None)
