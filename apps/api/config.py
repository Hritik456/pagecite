"""Settings from env vars (and .env for local runs), via pydantic-settings."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: str = "development"
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

    # OCR caps: a parse task must finish well inside the broker visibility timeout (docs/adr/0003).
    ocr_max_pages: int = 50
    ocr_page_timeout_s: int = 60
    ocr_budget_s: int = 900

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
