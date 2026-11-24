"""PageCite API: Flask app factory (`flask --app apps.api ...`, gunicorn `apps.api:create_app()`)."""

from flask import Flask, jsonify
from sqlalchemy.orm import sessionmaker

from .auth import authenticate
from .cli import register_cli
from .config import Settings
from .db import make_engine
from .errors import register_error_handlers
from .request_log import configure_logging, register_request_hooks
from .routes.batches import bp as batches_bp
from .routes.documents import bp as documents_bp
from .state import EXTENSION_KEY, AppState, close_session
from .storage import S3Storage, Storage


def create_app(settings: Settings | None = None, storage: Storage | None = None) -> Flask:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = Flask(__name__, static_folder=None)  # the web console is served by Caddy
    engine = make_engine(settings.database_url)
    app.extensions[EXTENSION_KEY] = AppState(
        settings=settings,
        engine=engine,
        sessions=sessionmaker(engine, expire_on_commit=False),
        storage=storage or S3Storage(settings),
    )

    register_request_hooks(app)  # first: sets request_id for everything after it
    app.before_request(authenticate)
    app.teardown_appcontext(close_session)
    register_error_handlers(app)

    @app.get("/health", endpoint="health")
    def health():
        return jsonify({"status": "ok"})

    app.register_blueprint(batches_bp)
    app.register_blueprint(documents_bp)
    register_cli(app)
    return app
