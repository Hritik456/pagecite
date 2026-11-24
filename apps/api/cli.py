"""flask --app apps.api seed-tenant ..."""

import click
from flask import Flask
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import hash_key, new_api_key
from .models import Tenant
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
