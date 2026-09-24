import uuid
import enum
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# UUIDs (gen_uuid) sempre têm 36 caracteres — usado em todo primary/foreign key
# de string abaixo. MySQL exige tamanho explícito em VARCHAR (Postgres/SQLite
# não exigem, mas aceitam do mesmo jeito), por isso todo db.String() aqui tem
# um tamanho definido.
UUID_LEN = 36


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

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    owner_ref = db.Column(db.String(255), nullable=False, index=True)  # id externo do dono
    kind = db.Column(db.String(20), nullable=False)  # merchant | customer | system
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

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    idempotency_key = db.Column(db.String(255), unique=True, nullable=False, index=True)
    rail = db.Column(db.String(30), nullable=False)  # pix | card | crypto | internal_transfer
    external_ref = db.Column(db.String(255), nullable=True)  # txid do PSP, etc.
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

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    transaction_id = db.Column(db.String(UUID_LEN), db.ForeignKey("transactions.id"), nullable=False)
    account_id = db.Column(db.String(UUID_LEN), db.ForeignKey("accounts.id"), nullable=False)
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

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    transaction_id = db.Column(db.String(UUID_LEN), db.ForeignKey("transactions.id"), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    delivered = db.Column(db.Boolean, default=False)
    attempts = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class ProviderSetting(db.Model):
    """Configuração viva de qual provedor está ativo para cada trilho de
    pagamento (pix, card). O admin troca isso em runtime, sem redeploy."""

    __tablename__ = "provider_settings"

    rail = db.Column(db.String(20), primary_key=True)  # "pix" | "card"
    provider = db.Column(db.String(20), nullable=False)  # "mercadopago" | "pagarme" | "direct"
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {"rail": self.rail, "provider": self.provider}


class Customer(db.Model):
    """Titular de uma carteira (pode ser pessoa física ou o próprio lojista).
    Cada Customer tem uma Account 1:1 (kind='customer' ou 'merchant').
    password_hash é opcional — só é preenchido quando o cliente ganha acesso
    ao portal próprio dele (customer-portal). Contas criadas só pelo admin,
    sem senha, continuam existindo normalmente, só não conseguem logar."""

    __tablename__ = "customers"

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    account_id = db.Column(db.String(UUID_LEN), db.ForeignKey("accounts.id"), nullable=False, unique=True)
    name = db.Column(db.String(255), nullable=False)
    document = db.Column(db.String(32), nullable=True)  # CPF/CNPJ
    email = db.Column(db.String(255), nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    account = db.relationship("Account")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "document": self.document,
            "email": self.email,
            "has_login": self.password_hash is not None,
            "account": self.account.to_dict(),
        }


class WithdrawalRequest(db.Model):
    """Pedido de saque: o cliente pede pra tirar dinheiro da carteira Divisions
    Pay e mandar pra uma chave PIX externa (outro banco). O valor sai do saldo
    do cliente NA HORA que o pedido é criado (fica reservado numa conta de
    sistema 'payouts_pending', pra não gastar duas vezes o mesmo saldo
    enquanto o saque está pendente) — mas o envio de verdade pra fora da
    Divisions Pay é feito manualmente pelo admin, pela conta real do Mercado
    Pago da empresa, até a API de transferência/saque deles ser aprovada pra
    automatizar. Isso é intencional: mover dinheiro pra outro banco por uma
    rede regulada (SPI/PIX) exige ser participante autorizado ou passar por
    quem já é (ver docs/COMPLIANCE.md) — o software não finge que pode pular
    essa etapa."""

    __tablename__ = "withdrawal_requests"

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    customer_id = db.Column(db.String(UUID_LEN), db.ForeignKey("customers.id"), nullable=False)
    account_id = db.Column(db.String(UUID_LEN), db.ForeignKey("accounts.id"), nullable=False)
    amount_cents = db.Column(db.BigInteger, nullable=False)
    pix_key = db.Column(db.String(255), nullable=False)
    pix_key_type = db.Column(db.String(20), nullable=False)  # cpf | cnpj | email | phone | random
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending | paid | failed | canceled
    transaction_id = db.Column(db.String(UUID_LEN), db.ForeignKey("transactions.id"), nullable=True)
    reversal_transaction_id = db.Column(db.String(UUID_LEN), db.ForeignKey("transactions.id"), nullable=True)
    admin_note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "account_id": self.account_id,
            "amount_cents": self.amount_cents,
            "pix_key": self.pix_key,
            "pix_key_type": self.pix_key_type,
            "status": self.status,
            "transaction_id": self.transaction_id,
            "admin_note": self.admin_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class AuditLog(db.Model):
    """Trilha de auditoria de eventos sensíveis (login, criação de cliente,
    troca de senha, transferência, saque). Não é uma feature 'bonita', é
    segurança básica: se algo der errado ou for contestado, dá pra
    reconstruir o que aconteceu, quando e a partir de que IP."""

    __tablename__ = "audit_logs"

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    event_type = db.Column(db.String(50), nullable=False, index=True)
    actor = db.Column(db.String(255), nullable=True)  # customer_id, "admin", etc.
    ip_address = db.Column(db.String(64), nullable=True)
    detail_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "event_type": self.event_type,
            "actor": self.actor,
            "ip_address": self.ip_address,
            "detail": self.detail_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdminUser(db.Model):
    """Conta de administrador de verdade (dono/operador da Divisions Pay).
    Substitui o login fixo por variável de ambiente que o admin-panel usava
    (HTTP Basic Auth) — agora é uma conta no banco, com e-mail, pra dar pra
    fazer login numa tela normal e recuperar senha por e-mail igual o
    cliente. A primeira conta é criada sozinha no boot a partir de
    ADMIN_BOOTSTRAP_EMAIL/ADMIN_BOOTSTRAP_PASSWORD (ver __init__.py)."""

    __tablename__ = "admin_users"

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "email": self.email}


class PasswordResetCode(db.Model):
    """Código de 'esqueci minha senha', enviado por e-mail. Serve tanto pra
    Customer quanto pra AdminUser (subject_type diz qual e subject_id é o id
    dele). Fluxo em 2 etapas pra nunca deixar o código de 6 dígitos valer
    sozinho por muito tempo nem circular mais do que precisa:

    1) /auth/password-reset/request: gera o código (6 dígitos), manda por
       e-mail. Só o hash fica salvo.
    2) /auth/password-reset/verify: troca o código por um reset_token de uso
       único (opaco, ~10min de validade) — só o hash do token fica salvo.
    3) /auth/password-reset/confirm: troca o reset_token pela senha nova.

    "attempts" limita tentativa de força bruta do código de 6 dígitos (10^6
    possibilidades não é nada se não travar depois de algumas erradas)."""

    __tablename__ = "password_reset_codes"

    id = db.Column(db.String(UUID_LEN), primary_key=True, default=gen_uuid)
    subject_type = db.Column(db.String(20), nullable=False)  # "customer" | "admin"
    subject_id = db.Column(db.String(UUID_LEN), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False)
    code_expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reset_token_hash = db.Column(db.String(64), nullable=True)
    reset_token_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class PlatformSetting(db.Model):
    """Configuração chave/valor simples da plataforma: qual conta recebe a
    taxa de 1% (platform_account_id) e o percentual em si (fee_bps, em
    pontos-base — 100 = 1%). Enquanto platform_account_id não estiver
    configurado, nenhuma taxa é cobrada (comportamento opt-in, pra não
    quebrar quem ainda não configurou)."""

    __tablename__ = "platform_settings"

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(255), nullable=True)

    def to_dict(self):
        return {"key": self.key, "value": self.value}
