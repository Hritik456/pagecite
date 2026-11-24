"""Settings from env vars (and .env for local runs), via pydantic-settings."""

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
