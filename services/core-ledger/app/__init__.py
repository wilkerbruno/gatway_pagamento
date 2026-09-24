import os
from flask import Flask
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash
from .models import db, AdminUser


def _run_auto_migrations(app):
    """Lightweight schema-drift fixer.

    db.create_all() only creates tables that don't exist yet -- it never
    alters a table that's already live in the database. When we add a new
    column to an existing model (e.g. Customer.password_hash), a
    previously-deployed database won't have it and every query against
    that model blows up with a 500 ("Unknown column ..."). This walks
    every model's columns, compares them against what's actually in the
    database, and issues a plain ADD COLUMN for anything missing.

    Intentionally conservative: only adds columns, never drops or alters
    existing ones, and skips anything it can't handle safely.
    """
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    with db.engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if table.name not in existing_tables:
                # Brand new table -- db.create_all() already handled it.
                continue

            existing_columns = {
                col["name"] for col in inspector.get_columns(table.name)
            }

            for column in table.columns:
                if column.name in existing_columns:
                    continue
                try:
                    col_type = column.type.compile(dialect=db.engine.dialect)
                except Exception:
                    app.logger.warning(
                        "auto-migration: skipping %s.%s (unsupported type for compile)",
                        table.name, column.name,
                    )
                    continue

                # A new column on a live table can't be NOT NULL without a
                # default (existing rows would violate it), so relax to
                # nullable unless the column itself defines a default.
                nullable_sql = ""
                if not column.nullable and (column.default is not None or column.server_default is not None):
                    nullable_sql = " NOT NULL"

                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}{nullable_sql}"
                app.logger.info("auto-migration: %s", ddl)
                try:
                    conn.execute(text(ddl))
                except Exception as exc:
                    app.logger.error(
                        "auto-migration: failed to add %s.%s: %s",
                        table.name, column.name, exc,
                    )


def _bootstrap_admin_user(app):
    """A primeira conta de admin não vem de um cadastro manual (não existe
    "criar admin" pelo painel, seria um jeito fácil de qualquer um criar
    acesso próprio) -- vem das env vars ADMIN_BOOTSTRAP_EMAIL/
    ADMIN_BOOTSTRAP_PASSWORD. Só age se ainda não existir NENHUM AdminUser
    no banco, então rodar isso de novo depois que já existe conta não
    reseta nada -- pra trocar a senha do admin, é pelo fluxo de "esqueci
    minha senha" (por e-mail), não mexendo direto no .env."""
    if AdminUser.query.first() is not None:
        return

    email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL")
    password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD")
    if not email or not password:
        app.logger.warning(
            "nenhum AdminUser existe ainda e ADMIN_BOOTSTRAP_EMAIL/"
            "ADMIN_BOOTSTRAP_PASSWORD não estão configurados -- ninguém "
            "vai conseguir logar como admin até isso ser resolvido."
        )
        return

    name = os.environ.get("ADMIN_BOOTSTRAP_NAME", "Admin")
    db.session.add(AdminUser(name=name, email=email, password_hash=generate_password_hash(password)))
    db.session.commit()
    app.logger.info("conta de admin inicial criada para %s", email)


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "mysql+pymysql://root:root@mysql:3306/ledger"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from .routes import bp as ledger_bp
    app.register_blueprint(ledger_bp)

    with app.app_context():
        db.create_all()
        try:
            _run_auto_migrations(app)
        except Exception:
            app.logger.exception("auto-migration step failed; continuing boot")
        try:
            _bootstrap_admin_user(app)
        except Exception:
            app.logger.exception("admin bootstrap step failed; continuing boot")

    return app
