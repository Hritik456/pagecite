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
