"""Model capabilities registry: per-model limits enforced before spend."""
import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class UnknownModelError(Exception):
    """Raised when a model is not found in the registry."""

    pass


class Source(BaseModel):
    """A documentation source with URL and check date."""

    url: str
    publisher: str
    checked: str  # YYYY-MM-DD


class Limit(BaseModel):
    """A limit value with its basis and sources."""

    value: Any
    basis: str
    sources: list[str] = Field(default_factory=list)
    higgsfield: Any = None
    upstream: Any = None
    note: str | None = None

    @property
    def unknown(self) -> bool:
        """True if this limit's basis is 'unknown'."""
        return self.basis == "unknown"

    def source_urls(self, registry: "ModelCapsRegistry") -> list[tuple[str, str]]:
        """Return list of (url, checked_date) for this limit's sources."""
        result = []
        for source_id in self.sources:
            if source_id in registry.sources:
                s = registry.sources[source_id]
                result.append((s.url, s.checked))
        return result


class ModelCaps(BaseModel):
    """Capabilities and limits for one model."""

    kind: str
    provider: str
    status: str
    aliases: list[str] = Field(default_factory=list)
    upstream: dict[str, Any] = Field(default_factory=dict)
    request_fields: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Limit] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    moderation: list[dict[str, Any]] = Field(default_factory=list)
    harness_policy: dict[str, Any] = Field(default_factory=dict)
    harness_note: str | None = None

    def limit(self, name: str) -> Limit | None:
        """Return the named limit, or None if not present."""
        return self.limits.get(name)


class SharedHiggsfield(BaseModel):
    """Shared Higgsfield facts."""

    upload_content_types: list[str]
    upload_url_ttl_s: int
    output_retention_days_min: int
    failed_or_nsfw_charged: bool
    concurrency_error: str
    sources: list[str]


class Shared(BaseModel):
    """Shared configuration across models."""

    higgsfield: SharedHiggsfield


class ModelCapsRegistry(BaseModel):
    """The complete model capabilities registry."""

    schema_version: int
    checked: str
    recheck_interval_days: int
    precedence: str
    shared: Shared
    models: dict[str, ModelCaps]
    sources: dict[str, Source]

    def resolve(self, model_or_alias: str) -> ModelCaps:
        """
        Resolve a model by id or alias.
        Raises UnknownModelError if not found.
        """
        # Direct lookup
        if model_or_alias in self.models:
            return self.models[model_or_alias]

        # Alias lookup
        for model_id, caps in self.models.items():
            if model_or_alias in caps.aliases:
                return caps

        raise UnknownModelError(
            f"Model '{model_or_alias}' not found in registry. "
            f"Known models: {', '.join(sorted(self.models.keys()))}"
        )

    def stale(self, now: datetime | None = None) -> bool:
        """Return True if the registry is stale (>recheck_interval_days old)."""
        if now is None:
            now = datetime.now(timezone.utc)
        checked_date = datetime.strptime(self.checked, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (now - checked_date).days
        return age_days > self.recheck_interval_days


# Global registry instance
_registry: ModelCapsRegistry | None = None


def load(path: str | None = None) -> ModelCapsRegistry:
    """
    Load the model capabilities registry from JSON.
    Honors MODEL_CAPS_PATH environment variable if path is None.
    """
    global _registry

    if path is None:
        path = os.environ.get("MODEL_CAPS_PATH")

    if path is None:
        # Default to package data
        data_dir = Path(__file__).parent / "data"
        path = str(data_dir / "model_caps.json")

    with open(path, "r") as f:
        data = json.load(f)

    _registry = ModelCapsRegistry(**data)

    # Validate: every limit's sources must exist
    for model_id, caps in _registry.models.items():
        for limit_name, limit in caps.limits.items():
            for source_id in limit.sources:
                if source_id not in _registry.sources:
                    raise ValueError(
                        f"Model {model_id} limit {limit_name} references "
                        f"unknown source '{source_id}'"
                    )

    # Validate: every source must have https:// url (or mcp:/repo-path for obs_*)
    for source_id, source in _registry.sources.items():
        if not (
            source.url.startswith("https://")
            or source.url.startswith("mcp:")
            or source.url.startswith("/")
        ):
            raise ValueError(
                f"Source {source_id} has invalid URL: {source.url} "
                f"(must start with https://, mcp:, or /)"
            )

    # Validate: every non-unknown value must have ≥1 source
    for model_id, caps in _registry.models.items():
        for limit_name, limit in caps.limits.items():
            if not limit.unknown and len(limit.sources) == 0:
                raise ValueError(
                    f"Model {model_id} limit {limit_name} has a value but no sources"
                )

    # Check freshness and warn if stale
    if _registry.stale():
        age_days = (
            datetime.now(timezone.utc)
            - datetime.strptime(_registry.checked, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        ).days
        warnings.warn(
            f"Model capabilities registry is {age_days} days old "
            f"(last checked {_registry.checked}). Consider updating it.",
            UserWarning,
            stacklevel=2,
        )

    return _registry


def get_registry() -> ModelCapsRegistry:
    """Get the loaded registry, loading it if necessary."""
    if _registry is None:
        return load()
    return _registry


def creator_view(model_or_alias: str) -> dict[str, Any]:
    """
    Return a creator-friendly view of a model's capabilities.
    Includes limits with 'not published' for unknowns, sources, and stale flag.
    """
    registry = get_registry()
    caps = registry.resolve(model_or_alias)

    limits_view = {}
    for name, limit in caps.limits.items():
        if limit.unknown:
            limits_view[name] = {
                "value": "not published",
                "note": limit.note,
            }
        else:
            source_info = limit.source_urls(registry)
            limits_view[name] = {
                "value": limit.value,
                "basis": limit.basis,
                "sources": [{"url": url, "checked": checked} for url, checked in source_info],
                "note": limit.note,
            }

    return {
        "model_id": model_or_alias,
        "kind": caps.kind,
        "status": caps.status,
        "limits": limits_view,
        "stale": registry.stale(),
    }
