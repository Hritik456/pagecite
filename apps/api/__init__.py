"""PageCite API: Flask app factory (`flask --app apps.api ...`, gunicorn `apps.api:create_app()`)."""

from collections.abc import Callable
from typing import Any

from flask import Flask, jsonify
from sqlalchemy.orm import sessionmaker
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import authenticate
from .cli import register_cli
from .config import Settings
from .db import make_engine
from .errors import register_error_handlers
from .limits import build_limits
from .request_log import configure_logging, register_request_hooks
from .routes.ask import bp as ask_bp
from .routes.batches import bp as batches_bp
from .routes.dead_letters import bp as dead_letters_bp
from .routes.documents import bp as documents_bp
from .state import EXTENSION_KEY, AppState, close_session
from .storage import S3Storage, Storage


def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    retrieval: Callable[[AppState], Any] | None = None,
) -> Flask:
    """`retrieval`: optional factory for the /ask dependencies (tests inject fakes; default: built lazily)."""
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = Flask(__name__, static_folder=None)  # the web console is served by Caddy
    # Only Caddy reaches the API (no published port): trust exactly one X-Forwarded-For hop, the one
    # Caddy appends, so request.remote_addr is the client and a client-sent header can't spoof it.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
    engine = make_engine(settings.database_url)
    app.extensions[EXTENSION_KEY] = AppState(
        settings=settings,
        engine=engine,
        sessions=sessionmaker(engine, expire_on_commit=False),
        storage=storage or S3Storage(settings),
        limits=build_limits(settings),  # no connection yet; None when no demo limit is configured
    )
    if retrieval is not None:
        state = app.extensions[EXTENSION_KEY]
        state.retrieval = retrieval(state)

    register_request_hooks(app)  # first: sets request_id for everything after it
    app.before_request(authenticate)
    app.teardown_appcontext(close_session)
    register_error_handlers(app)

    @app.get("/health", endpoint="health")
    def health():
        return jsonify({"status": "ok", "sha": settings.git_sha})

    app.register_blueprint(batches_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(ask_bp)
    app.register_blueprint(dead_letters_bp)
    register_cli(app)
    return app
