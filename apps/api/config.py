"""Settings from env vars (and .env for local runs), via pydantic-settings."""

import os

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FAULT_ENV_PREFIX = "FAULT_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: str = "development"  # development | chaos | production
    log_level: str = "INFO"

    database_url: str

    # Internal endpoint (HEAD/DELETE from the API) and the public one the browser uses.
    # The host is part of a SigV4 signature, so presigned URLs must be signed for the public host.
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_bucket: str
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    presign_expires_seconds: int = 900
    # path for MinIO (dev); virtual for AWS, with the regional endpoint for aws_region (storage.py).
    s3_addressing_style: str = "path"

    # Redis: DB 0 holds the search index, DB 1 is the Celery broker.
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    # Embeddings. SecretStr keeps the key out of reprs and logs.
    openai_api_key: SecretStr | None = None
    openai_embed_model: str = "text-embedding-3-small"
    embed_dim: int = 1536
    embed_batch_size: int = 100

    # Redis index (derived from MySQL; `reindex` rebuilds it).
    index_name: str = "idx:chunks"
    index_prefix: str = "chunk:"

    # Chunking.
    tokenizer: str = "cl100k_base"
    chunk_tokens: int = 500
    chunk_overlap: int = 50

    # Parse limits (spec §8): pages per document, scanned pages to OCR, OCR time.
    max_pages: int = 500
    ocr_max_pages: int = 50
    ocr_page_timeout_s: int = 60
    ocr_budget_s: int = 900

    # Delivery and retries (docs/adr/0003). Must satisfy:
    #   ocr_budget_s < stage_soft_time_limit_s < stage_time_limit_s < broker_visibility_timeout_s
    #   retry_backoff_max_s < broker_visibility_timeout_s   (ETA retries are held unacked)
    broker_visibility_timeout_s: int = 1800
    stage_soft_time_limit_s: int = 1200
    stage_time_limit_s: int = 1500
    max_retries: int = 5
    retry_backoff_max_s: int = 300
    # Attempts per (document, stage, version), counting retries AND redeliveries after a crash.
    # 8 = 1 + 5 retries + 2 redeliveries; a poison document then lands in the DLQ instead of looping.
    stage_attempt_limit: int = 8

    # Fault injection for the chaos/DLQ demo only (docs/adr/0003). Refused in production.
    fault_embed_429_prefix: str | None = None

    # Retrieval + answers (P4). The chat model writes the answer; the fast model decomposes and reranks.
    openai_chat_model: str = "gpt-5.4-mini"
    openai_fast_model: str = "gpt-5.4-nano"
    chat_reasoning_effort: str | None = None  # sent only when set (e.g. "low")
    fast_reasoning_effort: str | None = None
    retrieval_strategy: str = "hybrid_rerank"  # vector_only | hybrid | hybrid_rerank
    retrieval_decompose: bool = True
    retrieval_k: int = 20  # candidates per sub-query
    rerank_candidates: int = 20
    rerank_passage_tokens: int = 200
    context_blocks: int = 6
    answer_max_tokens: int = 900
    # Upstream timeouts: a hung OpenAI call must not hold a gunicorn thread.
    answer_timeout_s: float = 60.0
    json_timeout_s: float = 20.0
    embed_timeout_s: float = 20.0

    # Eval harness (P5): the LLM judge for faithfulness (`python -m eval.run`). Pinned to the same dated
    # snapshot the answers use (OPENAI_CHAT_MODEL in .env), so re-runs judge with the same model.
    eval_judge_model: str = "gpt-5.4-mini-2026-03-17"

    # Public demo limits (P5B, apps/api/limits.py). Off unless set, so dev and tests are unchanged.
    # They live in their own Redis DB, apart from the index (DB 0) and the broker (DB 1).
    limits_redis_url: str = "redis://localhost:6379/2"
    ask_rate_per_minute: int | None = None  # per client IP; prod 10
    ask_rate_per_day: int | None = None  # per client IP, UTC day; prod 20
    daily_spend_cap_usd: float | None = None  # global OpenAI spend of /ask per UTC day; prod 0.50
    spend_reserve_usd: float = 0.01  # reserved before each /ask (worst case), settled to the actual cost
    ask_max_chars: int = 500

    # Build identity: baked into the image (Dockerfile ARG), shown by /health.
    git_sha: str = "dev"

    @model_validator(mode="after")
    def _check_timing(self) -> "Settings":
        chain = [
            ("OCR_BUDGET_S", self.ocr_budget_s),
            ("STAGE_SOFT_TIME_LIMIT_S", self.stage_soft_time_limit_s),
            ("STAGE_TIME_LIMIT_S", self.stage_time_limit_s),
            ("BROKER_VISIBILITY_TIMEOUT_S", self.broker_visibility_timeout_s),
        ]
        for (a, va), (b, vb) in zip(chain, chain[1:]):
            if not va < vb:
                raise ValueError(f"{a} ({va}) must be < {b} ({vb}): a task must finish inside the visibility timeout")
        if not self.retry_backoff_max_s < self.broker_visibility_timeout_s:
            raise ValueError(
                f"RETRY_BACKOFF_MAX_S ({self.retry_backoff_max_s}) must be < BROKER_VISIBILITY_TIMEOUT_S "
                f"({self.broker_visibility_timeout_s}): a longer retry countdown is redelivered while still held"
            )
        return self

    @model_validator(mode="after")
    def _priced_models_when_capped(self) -> "Settings":
        """A spend cap can only be enforced for models with a known price: refuse to start otherwise."""
        if self.daily_spend_cap_usd is not None:
            from .retrieval.pricing import UnknownModelPrice, price_for

            for model in (self.openai_chat_model, self.openai_fast_model, self.openai_embed_model):
                try:
                    price_for(model)
                except UnknownModelPrice as exc:
                    raise ValueError(f"DAILY_SPEND_CAP_USD is set but {exc}") from None
            if self.spend_reserve_usd <= 0 or self.spend_reserve_usd > self.daily_spend_cap_usd:
                raise ValueError("SPEND_RESERVE_USD must be > 0 and <= DAILY_SPEND_CAP_USD")
        if self.s3_addressing_style not in ("path", "virtual"):
            raise ValueError("S3_ADDRESSING_STYLE must be path or virtual")
        return self

    @model_validator(mode="after")
    def _no_faults_in_production(self) -> "Settings":
        if self.app_env.lower() == "production":
            fault_vars = sorted(k for k in os.environ if k.upper().startswith(FAULT_ENV_PREFIX))
            if self.fault_embed_429_prefix or fault_vars:
                raise ValueError(
                    "fault injection is not allowed with APP_ENV=production "
                    f"(set: {', '.join(fault_vars) or 'FAULT_EMBED_429_PREFIX'})"
                )
        return self
