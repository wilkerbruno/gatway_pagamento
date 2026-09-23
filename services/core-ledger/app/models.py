import uuid
import enum
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def gen_uuid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class EntryStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REVERSED = "reversed"


class Account(db.Model):
    """Uma conta contábil interna: pode representar um lojista, um cliente,
    ou uma conta de sistema (ex: 'pix_pending', 'card_receivable', 'fees')."""

    __tablename__ = "accounts"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    owner_ref = db.Column(db.String, nullable=False, index=True)  # id externo do dono
    kind = db.Column(db.String, nullable=False)  # merchant | customer | system
    currency = db.Column(db.String(8), nullable=False, default="BRL")
    balance_cents = db.Column(db.BigInteger, nullable=False, default=0)
    # contas de sistema podem ficar negativas (representam dinheiro "a receber"
    # do PSP enquanto uma cobranca esta pendente); carteiras de cliente/lojista
    # nao podem, por padrao — isso impede transferencia/saque maior que o saldo.
    allow_negative = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "owner_ref": self.owner_ref,
            "kind": self.kind,
            "currency": self.currency,
            "balance_cents": self.balance_cents,
            "allow_negative": self.allow_negative,
        }


class Transaction(db.Model):
    """Um evento de negócio (ex: 'cobrança PIX #123'). Contém 2+ lançamentos
    (LedgerEntry) que juntos somam zero — partida dobrada."""

    __tablename__ = "transactions"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    idempotency_key = db.Column(db.String, unique=True, nullable=False, index=True)
    rail = db.Column(db.String, nullable=False)  # pix | card | crypto
    external_ref = db.Column(db.String, nullable=True)  # txid do PSP, etc.
    status = db.Column(db.Enum(EntryStatus), default=EntryStatus.PENDING, nullable=False)
    metadata_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    entries = db.relationship("LedgerEntry", backref="transaction", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "rail": self.rail,
            "external_ref": self.external_ref,
            "status": self.status.value,
            "metadata": self.metadata_json,
            "entries": [e.to_dict() for e in self.entries],
        }


class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    transaction_id = db.Column(db.String, db.ForeignKey("transactions.id"), nullable=False)
    account_id = db.Column(db.String, db.ForeignKey("accounts.id"), nullable=False)
    amount_cents = db.Column(db.BigInteger, nullable=False)  # positivo=crédito, negativo=débito
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "amount_cents": self.amount_cents,
        }


class WebhookEvent(db.Model):
    """Fila simples de webhooks de saída para o sistema do lojista."""

    __tablename__ = "webhook_events"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    transaction_id = db.Column(db.String, db.ForeignKey("transactions.id"), nullable=False)
    event_type = db.Column(db.String, nullable=False)
    delivered = db.Column(db.Boolean, default=False)
    attempts = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class ProviderSetting(db.Model):
    """Configuração viva de qual provedor está ativo para cada trilho de
    pagamento (pix, card). O admin troca isso em runtime, sem redeploy."""

    __tablename__ = "provider_settings"

    rail = db.Column(db.String, primary_key=True)  # "pix" | "card"
    provider = db.Column(db.String, nullable=False)  # "mercadopago" | "pagarme" | "direct"
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {"rail": self.rail, "provider": self.provider}


class Customer(db.Model):
    """Titular de uma carteira (pode ser pessoa física ou o próprio lojista).
    Cada Customer tem uma Account 1:1 (kind='customer' ou 'merchant')."""

    __tablename__ = "customers"

    id = db.Column(db.String, primary_key=True, default=gen_uuid)
    account_id = db.Column(db.String, db.ForeignKey("accounts.id"), nullable=False, unique=True)
    name = db.Column(db.String, nullable=False)
    document = db.Column(db.String, nullable=True)  # CPF/CNPJ
    email = db.Column(db.String, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    account = db.relationship("Account")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "document": self.document,
            "email": self.email,
            "account": self.account.to_dict(),
        }
