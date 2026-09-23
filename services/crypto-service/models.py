import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def gen_uuid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    hd_index = db.Column(db.Integer, unique=True, nullable=False)
    address = db.Column(db.String, nullable=False)
    asset = db.Column(db.String, nullable=False)  # "MATIC" | "USDT" (ERC-20)
    chain = db.Column(db.String, nullable=False, default="polygon")
    required_amount_base_units = db.Column(db.Numeric, nullable=False)
    amount_cents_brl = db.Column(db.BigInteger, nullable=False)
    transaction_id = db.Column(db.String, nullable=False)  # id no core-ledger
    status = db.Column(db.String, nullable=False, default="awaiting_confirmation")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "address": self.address,
            "asset": self.asset,
            "chain": self.chain,
            "required_amount_base_units": str(self.required_amount_base_units),
            "amount_cents_brl": self.amount_cents_brl,
            "transaction_id": self.transaction_id,
            "status": self.status,
        }


class Counter(db.Model):
    """Contador simples para o próximo índice de derivação HD a usar."""

    __tablename__ = "counters"

    name = db.Column(db.String, primary_key=True)
    value = db.Column(db.Integer, nullable=False, default=0)
