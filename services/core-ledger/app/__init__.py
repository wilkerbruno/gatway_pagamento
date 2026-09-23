import os
from flask import Flask
from .models import db


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/ledger"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from .routes import bp as ledger_bp
    app.register_blueprint(ledger_bp)

    with app.app_context():
        db.create_all()

    return app
