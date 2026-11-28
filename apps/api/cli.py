"""flask --app apps.api seed-tenant ... | reindex (--tenant <id> | --all)"""

import click
from flask import Flask
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import hash_key, new_api_key
from .models import Tenant
from .search_index import index_config, make_redis, rebuild
from .state import app_state


def create_tenant(session: Session, name: str, max_file_mb: int, max_batch_files: int) -> tuple[Tenant, str]:
    """Insert a tenant and return it with its plaintext API key (only the hash is stored)."""
    key = new_api_key()
    tenant = Tenant(
        name=name, api_key_hash=hash_key(key), max_file_mb=max_file_mb, max_batch_files=max_batch_files
    )
    session.add(tenant)
    session.flush()
    return tenant, key


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-tenant")
    @click.option("--name", required=True, help="Unique tenant name.")
    @click.option("--max-file-mb", default=25, show_default=True, type=click.IntRange(min=1))
    @click.option("--max-batch-files", default=50, show_default=True, type=click.IntRange(min=1))
    def seed_tenant(name: str, max_file_mb: int, max_batch_files: int) -> None:
        """Create a tenant and print its X-Tenant-Key once."""
        with app_state().sessions() as session:
            try:
                with session.begin():
                    tenant, key = create_tenant(session, name, max_file_mb, max_batch_files)
            except IntegrityError as exc:
                raise click.ClickException(f"tenant {name!r} already exists") from exc
        click.echo(f"tenant_id={tenant.id}")
        click.echo(f"api_key={key}")
        click.echo("Store the key now: only its SHA-256 hash is saved.")

    @app.cli.command("create-ask-key")
    @click.option("--tenant", "tenant_id", type=int, required=True)
    def create_ask_key_cmd(tenant_id: int) -> None:
        """Create an ask-only key (read + POST /ask) for a tenant and print it once."""
        from .auth import hash_key, new_ask_key
        from .models import ApiKey, Tenant

        with app_state().sessions() as session, session.begin():
            if session.get(Tenant, tenant_id) is None:
                raise click.ClickException(f"tenant {tenant_id} does not exist")
            key = new_ask_key()
            session.add(ApiKey(tenant_id=tenant_id, key_hash=hash_key(key), scope="ask"))
        click.echo(f"ask_key={key}")
        click.echo("Ask-only: it can read and POST /ask, nothing else. Only its SHA-256 hash is saved.")

    @app.cli.command("revoke-ask-keys")
    @click.option("--tenant", "tenant_id", type=int, required=True)
    def revoke_ask_keys_cmd(tenant_id: int) -> None:
        """Revoke every active ask-only key of a tenant (they become unknown keys: 401)."""
        from sqlalchemy import func, update

        from .models import ApiKey

        with app_state().sessions() as session, session.begin():
            n = session.execute(
                update(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
                .values(revoked_at=func.now(6))
            ).rowcount
        click.echo(f"revoked={n}")

    @app.cli.command("purge-tenant")
    @click.option("--tenant", "tenant_id", type=int, required=True, help="The tenant whose documents to delete.")
    @click.option("--yes", is_flag=True, help="Confirm: delete this tenant's documents, chunks, index keys and objects.")
    def purge_tenant_cmd(tenant_id: int, yes: bool) -> None:
        """LOCAL/TEST ONLY: delete one tenant's documents from MySQL, Redis and S3 (keeps the tenant)."""
        from .purge import PurgeRefused, purge_tenant, refuse_in_production

        state = app_state()
        try:
            refuse_in_production(state.settings.app_env)  # before any connection is opened
        except PurgeRefused as exc:
            raise click.ClickException(str(exc)) from exc
        if not yes:
            raise click.UsageError("this deletes data: pass --yes to confirm")
        settings = state.settings
        try:
            report = purge_tenant(state.sessions, make_redis(settings.redis_url), index_config(settings),
                                  state.storage, tenant_id)
        except PurgeRefused as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"tenant={report.tenant_id} documents={report.documents} batches={report.batches} "
            f"redis_keys={report.redis_keys} s3_objects={report.s3_objects} "
            f"processed_jobs={report.processed_jobs} outbox_events={report.outbox_events}"
        )

    @app.cli.command("reindex")
    @click.option("--tenant", "tenant_id", type=int, help="Rebuild one tenant's chunks.")
    @click.option("--all", "all_tenants", is_flag=True, help="Rebuild every tenant (e.g. after FLUSHDB).")
    def reindex(tenant_id: int | None, all_tenants: bool) -> None:
        """Rebuild the Redis index from MySQL (the source of truth). No embedding API calls."""
        if (tenant_id is None) == (not all_tenants):
            raise click.UsageError("give exactly one of --tenant <id> or --all")
        settings = app_state().settings
        r = make_redis(settings.redis_url)
        with app_state().sessions() as session:
            written = rebuild(session, r, index_config(settings), tenant_id)
        if tenant_id is not None:
            written.setdefault(tenant_id, 0)
        for tid, count in sorted(written.items()):
            click.echo(f"tenant={tid} chunks={count}")
        click.echo(f"total={sum(written.values())}")
