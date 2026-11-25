"""configs/*.yaml -> (label, RetrievalConfig). Strict: every key is required and no unknown key is allowed.

RetrievalConfig has defaults (decompose=True among them), so a forgotten key would silently measure a
different strategy than the file name says. Here a missing or misspelt key is an error instead.
"""

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from apps.api.retrieval.pipeline import RetrievalConfig

RETRIEVAL_KEYS = tuple(f.name for f in fields(RetrievalConfig))
REQUIRED_KEYS = ("label", *RETRIEVAL_KEYS)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EvalConfig:
    label: str
    retrieval: RetrievalConfig
    path: str

    def as_json(self) -> dict:
        return {"label": self.label, **{k: getattr(self.retrieval, k) for k in RETRIEVAL_KEYS}}


def parse_config(data: object, source: str = "<config>") -> EvalConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    missing = [k for k in REQUIRED_KEYS if k not in data]
    unknown = sorted(set(data) - set(REQUIRED_KEYS))
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(unknown)}")
        raise ConfigError(f"{source}: {'; '.join(parts)}")
    if not isinstance(data["decompose"], bool):
        raise ConfigError(f"{source}: decompose must be true or false")
    for key in ("k", "rerank_candidates", "top_n", "passage_tokens"):
        if not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] < 1:
            raise ConfigError(f"{source}: {key} must be a positive integer")
    if data["top_n"] < 5:
        raise ConfigError(f"{source}: top_n must be >= 5, or hit@5 cannot be measured")
    try:
        retrieval = RetrievalConfig(**{k: data[k] for k in RETRIEVAL_KEYS})
    except ValueError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return EvalConfig(label=str(data["label"]), retrieval=retrieval, path=source)


def load_config(path: str | Path) -> EvalConfig:
    path = Path(path)
    return parse_config(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
