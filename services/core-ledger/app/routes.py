import datetime as _dt
import hashlib
import logging
import os
import secrets
from decimal import Decimal

from flask import Blueprint, request, jsonify, send_from_directory, abort
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from .mailer import send_email
from .models import (
    db, Account, Transaction, LedgerEntry, WebhookEvent, EntryStatus,
    PlatformSetting, WithdrawalRequest, AuditLog, AdminUser, PasswordResetCode, utcnow,
)

bp = Blueprint("ledger", __name__)


def _log_audit(event_type: str, actor: str | None = None, detail: dict | None = None):
    """Best-effort: um problema ao gravar auditoria nunca pode derrubar a
    operação principal (login, transferência, etc.)."""
    try:
        db.session.add(AuditLog(
            event_type=event_type,
            actor=actor,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
            detail_json=detail,
        ))
    except Exception:
        pass


@bp.get("/health")
def health():
    return {"status": "ok"}


@bp.post("/accounts")
def create_account():
    data = request.get_json(force=True)
    kind = data.get("kind", "merchant")
    # contas de sistema podem ficar negativas por padrao (representam dinheiro
    # "a receber" do PSP enquanto uma cobranca esta pendente); pode ser
    # sobrescrito explicitamente via "allow_negative".
    default_allow_negative = kind == "system"
    acc = Account(
        owner_ref=data["owner_ref"],
        kind=kind,
        currency=data.get("currency", "BRL"),
        allow_negative=data.get("allow_negative", default_allow_negative),
    )
    db.session.add(acc)
    db.session.commit()
    return jsonify(acc.to_dict()), 201


@bp.get("/accounts/<account_id>")
def get_account(account_id):
    acc = Account.query.get_or_404(account_id)
    return jsonify(acc.to_dict())


# --- Contas de sistema (pix_pending / card_receivable / crypto_pending) --
#
# pix-service, card-service e crypto-service precisam debitar uma conta de
# "a receber do PSP" antes de creditar o lojista/cliente. Antes, cada
# serviço exigia isso via variável de ambiente (SYSTEM_PIX_PENDING_ACCOUNT
# etc.), configurada manualmente no EasyPanel — se esquecida ou vazia, o
# id da conta virava None/"" e o lançamento quebrava com um 500 cru (erro
# de integridade no banco, sem mensagem clara). Agora o core-ledger cria
# essas contas sozinho (auto_get_or_create_system_account) e cada serviço
# busca o id aqui — sem passo manual, sem curl, sem redeploy.

def _auto_get_or_create_system_account(owner_ref: str) -> str:
    acc = Account.query.filter_by(owner_ref=owner_ref, kind="system").first()
    if acc is None:
        acc = Account(owner_ref=owner_ref, kind="system", allow_negative=True)
        db.session.add(acc)
        db.session.commit()
    return acc.id


@bp.get("/admin/settings/system-accounts")
def get_system_accounts():
    return jsonify({
        "pix_pending_account_id": _auto_get_or_create_system_account("system:pix_pending"),
        "card_receivable_account_id": _auto_get_or_create_system_account("system:card_receivable"),
        "crypto_pending_account_id": _auto_get_or_create_system_account("system:crypto_pending"),
        "payouts_pending_account_id": _auto_get_or_create_system_account("system:payouts_pending"),
    })


@bp.post("/transactions")
def create_transaction():
    """Cria uma transação pendente com N lançamentos que devem somar zero.

    Corpo esperado:
    {
      "idempotency_key": "pix:txid:abc123",
      "rail": "pix",
      "external_ref": "abc123",
      "entries": [{"account_id": "...", "amount_cents": -1000},
                   {"account_id": "...", "amount_cents": 1000}],
      "metadata": {}
    }
    """
    data = request.get_json(force=True)

    existing = Transaction.query.filter_by(
        idempotency_key=data["idempotency_key"]
    ).first()
    if existing:
        # Idempotência: mesma chave = devolve o resultado já processado,
        # nunca duplica o lançamento.
        return jsonify(existing.to_dict()), 200

    entries_in = list(data["entries"])
    total = sum(e["amount_cents"] for e in entries_in)
    if total != 0:
        return jsonify({"error": "entries must sum to zero (double-entry)"}), 400

    # Taxa de plataforma: em cobrancas recebidas via pix/card/crypto (2
    # lancamentos: um debito na conta de sistema do PSP, um credito na
    # carteira que recebe), retira fee_bps do valor creditado e manda pra
    # conta da plataforma. So age se a conta da plataforma estiver
    # configurada (opt-in) e a transacao tiver exatamente esse formato de
    # 2 entradas. Transferencias internas (/transfers) nao passam por aqui.
    if data["rail"] in ("pix", "card", "crypto") and len(entries_in) == 2:
        platform_account_row = PlatformSetting.query.get("platform_account_id")
        fee_bps_row = PlatformSetting.query.get("fee_bps")
        if platform_account_row and platform_account_row.value:
            fee_bps = int(fee_bps_row.value) if fee_bps_row and fee_bps_row.value else 100
            platform_account_id = platform_account_row.value
            credit_entry = next((e for e in entries_in if e["amount_cents"] > 0), None)
            if credit_entry and credit_entry["account_id"] != platform_account_id and fee_bps > 0:
                gross = credit_entry["amount_cents"]
                fee_cents = round(gross * fee_bps / 10000)
                if 0 < fee_cents < gross:
                    credit_entry["amount_cents"] = gross - fee_cents
                    entries_in.append({"account_id": platform_account_id, "amount_cents": fee_cents})

    # Valida todas as contas antes de tocar no banco: evita gerar um erro
    # de integridade sem contexto (500 cru) quando um account_id vier
    # vazio/None ou apontando pra uma conta que não existe (ex: variável de
    # ambiente de um dos serviços de rail não configurada).
    for e in entries_in:
        account_id = e.get("account_id")
        if not account_id or not Account.query.get(account_id):
            return jsonify({
                "error": "account_id inválido ou ausente em um dos lançamentos",
                "account_id": account_id,
            }), 400

    txn = Transaction(
        idempotency_key=data["idempotency_key"],
        rail=data["rail"],
        external_ref=data.get("external_ref"),
        status=EntryStatus.PENDING,
        metadata_json=data.get("metadata"),
    )
    db.session.add(txn)
    db.session.flush()

    for e in entries_in:
        db.session.add(
            LedgerEntry(
                transaction_id=txn.id,
                account_id=e["account_id"],
                amount_cents=e["amount_cents"],
            )
        )

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        # Só existe uma causa legítima pra um IntegrityError aqui: duas
        # requisições concorrentes com a mesma idempotency_key colidindo.
        # Qualquer outra causa (account_id inválido, etc.) já devia ter
        # sido barrada na validação acima — mas se passar, respondemos com
        # um erro claro em vez de deixar a exceção estourar como 500 cru.
        existing = Transaction.query.filter_by(
            idempotency_key=data["idempotency_key"]
        ).first()
        if existing:
            return jsonify(existing.to_dict()), 200
        return jsonify({"error": "falha de integridade ao criar a transação", "detail": str(exc.orig)}), 400

    return jsonify(txn.to_dict()), 201


def _settle_txn(txn):
    """Aplica o efeito de uma transação pendente nos saldos das contas
    (parte pura, sem decidir status HTTP -- usada tanto pela rota de settle
    quanto pela conversão automática pra cripto abaixo). Retorna None se
    aplicou com sucesso, ou um dict de erro (nada é alterado) se algum
    saldo ficaria negativo sem permissão."""
    accounts = {e.account_id: Account.query.get(e.account_id) for e in txn.entries}
    for entry in txn.entries:
        acc = accounts[entry.account_id]
        projected = acc.balance_cents + entry.amount_cents
        if projected < 0 and not acc.allow_negative:
            return {
                "error": "settle recusado: deixaria a conta com saldo negativo",
                "account_id": acc.id,
                "balance_cents": acc.balance_cents,
            }

    for entry in txn.entries:
        accounts[entry.account_id].balance_cents += entry.amount_cents

    txn.status = EntryStatus.CONFIRMED
    db.session.add(WebhookEvent(transaction_id=txn.id, event_type="transaction.confirmed"))
    return None


# Cotação BRL->USDT usada só pela conversão automática (carteira cripto
# interna) abaixo -- mesmo esquema de placeholder fixo do crypto-service
# (ASSET_BRL_RATE em services/crypto-service/app.py). Troque por uma
# cotação em tempo real antes de operar com volume real.
USDT_BRL_RATE = Decimal(os.environ.get("USDT_BRL_RATE", "5.50"))


def _get_or_create_crypto_account(customer) -> str:
    if customer.crypto_account_id:
        return customer.crypto_account_id
    acc = Account(
        owner_ref=f"{customer.document or customer.name}:usdt-wallet",
        kind="customer",
        currency="USDT",
        allow_negative=False,
    )
    db.session.add(acc)
    db.session.flush()
    customer.crypto_account_id = acc.id
    db.session.add(customer)
    return acc.id


def _maybe_auto_convert_to_crypto(source_txn):
    """Depois de liquidar um PIX/cartão recebido, confere se a conta
    creditada é de um cliente com a conversão automática pra cripto ligada
    (Customer.auto_convert_to_crypto) -- se for, converte o valor na hora
    pro saldo interno de USDT dele, em duas transações separadas (uma só em
    BRL, outra só em USDT, cada uma batendo em zero -- mantém a partida
    dobrada por moeda). Isso é uma carteira cripto interna/contábil da
    Divisions Pay, não uma compra real em corretora nem um envio on-chain;
    pra virar USDT de verdade numa wallet externa ainda depende do fluxo
    manual do crypto-service, igual o saque PIX hoje."""
    if source_txn.rail not in ("pix", "card"):
        return

    for entry in source_txn.entries:
        if entry.amount_cents <= 0:
            continue
        customer = Customer.query.filter_by(account_id=entry.account_id).first()
        if not customer or not customer.auto_convert_to_crypto:
            continue

        brl_cents = entry.amount_cents
        idem_brl = f"autoconvert:{source_txn.id}:{customer.id}:brl"
        if Transaction.query.filter_by(idempotency_key=idem_brl).first():
            continue  # já convertido (ex: settle chamado de novo)

        usdt_cents = int((Decimal(brl_cents) / USDT_BRL_RATE).quantize(Decimal("1")))
        if usdt_cents <= 0:
            continue

        crypto_pool_brl_id = _auto_get_or_create_system_account("system:crypto_conversion_pool_brl")
        crypto_pool_usdt_id = _auto_get_or_create_system_account("system:crypto_conversion_pool_usdt")
        crypto_account_id = _get_or_create_crypto_account(customer)
        db.session.commit()

        brl_leg = Transaction(
            idempotency_key=idem_brl,
            rail="internal_transfer",
            status=EntryStatus.PENDING,
            metadata_json={
                "kind": "auto_convert_to_crypto_brl_leg",
                "source_transaction_id": source_txn.id,
                "customer_id": customer.id,
            },
        )
        db.session.add(brl_leg)
        db.session.flush()
        db.session.add(LedgerEntry(transaction_id=brl_leg.id, account_id=customer.account_id, amount_cents=-brl_cents))
        db.session.add(LedgerEntry(transaction_id=brl_leg.id, account_id=crypto_pool_brl_id, amount_cents=brl_cents))
        db.session.commit()
        if _settle_txn(brl_leg):
            db.session.rollback()
            continue
        db.session.commit()

        usdt_leg = Transaction(
            idempotency_key=f"autoconvert:{source_txn.id}:{customer.id}:usdt",
            rail="internal_transfer",
            status=EntryStatus.PENDING,
            metadata_json={
                "kind": "auto_convert_to_crypto_usdt_leg",
                "source_transaction_id": source_txn.id,
                "customer_id": customer.id,
                "rate_used_brl_per_usdt": str(USDT_BRL_RATE),
                "brl_cents": brl_cents,
            },
        )
        db.session.add(usdt_leg)
        db.session.flush()
        db.session.add(LedgerEntry(transaction_id=usdt_leg.id, account_id=crypto_pool_usdt_id, amount_cents=-usdt_cents))
        db.session.add(LedgerEntry(transaction_id=usdt_leg.id, account_id=crypto_account_id, amount_cents=usdt_cents))
        db.session.commit()
        if _settle_txn(usdt_leg):
            db.session.rollback()
            continue
        db.session.commit()


@bp.post("/transactions/<transaction_id>/settle")
def settle_transaction(transaction_id):
    """Confirma uma transação pendente e aplica o efeito nos saldos das contas.
    Chamado pelos adapters (pix/card/crypto-service) quando o PSP confirma o pagamento."""
    txn = Transaction.query.get_or_404(transaction_id)

    if txn.status == EntryStatus.CONFIRMED:
        return jsonify(txn.to_dict()), 200

    if txn.status != EntryStatus.PENDING:
        return jsonify({"error": f"cannot settle transaction in status {txn.status.value}"}), 409

    error = _settle_txn(txn)
    if error:
        db.session.rollback()
        return jsonify(error), 409
    db.session.commit()

    _maybe_auto_convert_to_crypto(txn)

    return jsonify(txn.to_dict())


@bp.post("/transactions/<transaction_id>/fail")
def fail_transaction(transaction_id):
    txn = Transaction.query.get_or_404(transaction_id)
    if txn.status != EntryStatus.PENDING:
        return jsonify({"error": f"cannot fail transaction in status {txn.status.value}"}), 409

    txn.status = EntryStatus.FAILED
    db.session.add(WebhookEvent(transaction_id=txn.id, event_type="transaction.failed"))
    db.session.commit()
    return jsonify(txn.to_dict())


# --- Admin: seleção de provedor por trilho de pagamento -------------------

from .models import ProviderSetting

VALID_PROVIDERS = {
    "pix": {"sandbox", "mercadopago", "pagarme", "direct"},
    "card": {"sandbox", "mercadopago", "pagarme", "direct"},
}


@bp.get("/admin/settings/providers")
def list_provider_settings():
    rows = ProviderSetting.query.all()
    configured = {r.rail: r.to_dict() for r in rows}
    # trilhos ainda não configurados caem no default "mercadopago"
    for rail in VALID_PROVIDERS:
        configured.setdefault(rail, {"rail": rail, "provider": "sandbox"})
    return jsonify(list(configured.values()))


@bp.put("/admin/settings/providers/<rail>")
def set_provider_setting(rail):
    if rail not in VALID_PROVIDERS:
        return jsonify({"error": f"trilho desconhecido: {rail}"}), 404

    data = request.get_json(force=True)
    provider = data.get("provider")
    if provider not in VALID_PROVIDERS[rail]:
        return jsonify({
            "error": f"provedor inválido para {rail}. Opções: {sorted(VALID_PROVIDERS[rail])}"
        }), 400

    row = ProviderSetting.query.get(rail)
    if row is None:
        row = ProviderSetting(rail=rail, provider=provider)
        db.session.add(row)
    else:
        row.provider = provider
    db.session.commit()
    return jsonify(row.to_dict())


@bp.get("/transactions/by-external-ref/<external_ref>")
def get_transaction_by_external_ref(external_ref):
    txn = Transaction.query.filter_by(external_ref=external_ref).first()
    if txn is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(txn.to_dict())


# --- Carteiras de cliente: cadastro, saldo, transferência P2P, extrato ----

from .models import Customer, VerificationDocument, FacialVerificationLink


@bp.get("/customers")
def list_customers():
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    customers = Customer.query.order_by(Customer.created_at.desc()).offset(offset).limit(limit).all()
    return jsonify([c.to_dict() for c in customers])


@bp.get("/customers/lookup")
def lookup_customer():
    """Resolve um destinatário de transferência pelo documento ou e-mail,
    sem expor a lista completa de clientes (usado pelo customer-portal:
    o cliente logado nunca vê quem mais existe na plataforma, só confirma
    que o destinatário que ele digitou existe)."""
    login = (request.args.get("login") or "").strip()
    exclude_customer_id = request.args.get("exclude_customer_id")
    if not login:
        return jsonify({"error": "informe documento ou e-mail"}), 400

    customer = Customer.query.filter(
        (Customer.document == login) | (Customer.email == login)
    ).first()
    if not customer or customer.id == exclude_customer_id:
        return jsonify({"error": "cliente nao encontrado"}), 404

    return jsonify({
        "id": customer.id,
        "name": customer.name,
        "account_id": customer.account_id,
    })


@bp.post("/customers")
def create_customer():
    """Cria um cliente e já abre a carteira (Account) dele junto.
    "password" é opcional — só preencha se esse cliente vai ter acesso ao
    portal próprio (customer-portal)."""
    data = request.get_json(force=True)

    document = (data.get("document") or "").strip() or None
    email = (data.get("email") or "").strip() or None
    dup_conditions = []
    if document:
        dup_conditions.append(Customer.document == document)
    if email:
        dup_conditions.append(Customer.email == email)
    if dup_conditions and Customer.query.filter(or_(*dup_conditions)).first():
        return jsonify({"error": "já existe um cliente com esse documento ou e-mail"}), 409

    acc = Account(
        owner_ref=document or data["name"],
        kind=data.get("kind", "customer"),
        currency=data.get("currency", "BRL"),
        allow_negative=False,
    )
    db.session.add(acc)
    db.session.flush()

    password = data.get("password")
    if password:
        weak = _password_weakness(password)
        if weak:
            return jsonify({"error": weak}), 400
    customer = Customer(
        account_id=acc.id,
        name=data["name"],
        document=document,
        email=email,
        password_hash=generate_password_hash(password) if password else None,
    )
    db.session.add(customer)
    _log_audit("customer.created", actor="admin", detail={"customer_name": customer.name, "has_login": bool(password)})
    db.session.commit()
    return jsonify(customer.to_dict()), 201


def _password_weakness(password: str) -> str | None:
    """Retorna uma mensagem de erro se a senha for fraca, ou None se estiver ok.
    Regra simples e objetiva: 8+ caracteres, pelo menos uma letra e um número
    — suficiente pra afastar senhas óbvias (123456, senha123) sem exigir
    caracteres especiais que ninguém lembra."""
    if len(password) < 8:
        return "senha precisa ter pelo menos 8 caracteres"
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "senha precisa ter letras e números"
    return None


LOGIN_LOCKOUT_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_WINDOW_MINUTES = 15


@bp.post("/customers/authenticate")
def authenticate_customer():
    """Login do portal do cliente: verifica documento/e-mail + senha.
    Retorna o cliente se bater, 401 caso contrario. Nao expoe password_hash.

    Trava por 15 minutos depois de muitas tentativas erradas seguidas pro
    mesmo login (força bruta de senha) — a contagem vem da própria trilha
    de auditoria, sem precisar de tabela/cache separado."""
    import datetime as _dt

    data = request.get_json(force=True)
    login = (data.get("login") or "").strip()
    password = data.get("password", "")

    cutoff = utcnow() - _dt.timedelta(minutes=LOGIN_LOCKOUT_WINDOW_MINUTES)
    recent_failures = AuditLog.query.filter(
        AuditLog.event_type == "customer.login_failed",
        AuditLog.actor == login,
        AuditLog.created_at > cutoff,
    ).count()
    if recent_failures >= LOGIN_LOCKOUT_MAX_ATTEMPTS:
        return jsonify({
            "error": f"muitas tentativas erradas. Tente de novo em {LOGIN_LOCKOUT_WINDOW_MINUTES} minutos."
        }), 429

    customer = Customer.query.filter(
        (Customer.document == login) | (Customer.email == login)
    ).first()

    if not customer or not customer.password_hash or not check_password_hash(customer.password_hash, password):
        _log_audit("customer.login_failed", actor=login)
        db.session.commit()
        return jsonify({"error": "credenciais invalidas"}), 401

    _log_audit("customer.login_success", actor=customer.id)
    db.session.commit()
    return jsonify(customer.to_dict())


@bp.put("/customers/<customer_id>/password")
def set_customer_password(customer_id):
    """Define/troca a senha de acesso ao portal (chamado pelo admin-panel
    quando o admin cria/reseta o acesso de um cliente)."""
    customer = Customer.query.get_or_404(customer_id)
    data = request.get_json(force=True)
    password = data.get("password")
    weak = _password_weakness(password or "")
    if weak:
        return jsonify({"error": weak}), 400
    customer.password_hash = generate_password_hash(password)
    _log_audit("customer.password_changed", actor=customer.id)
    db.session.commit()
    return jsonify(customer.to_dict())


@bp.put("/customers/<customer_id>/auto-convert-crypto")
def set_auto_convert_crypto(customer_id):
    """Liga/desliga a conversão automática de PIX/cartão recebido pro saldo
    interno de USDT (carteira cripto). O cliente escolhe isso na tela
    "Carteira cripto" do customer-portal."""
    customer = Customer.query.get_or_404(customer_id)
    data = request.get_json(force=True)
    customer.auto_convert_to_crypto = bool(data.get("enabled"))
    _log_audit("customer.auto_convert_crypto_changed", actor=customer.id, detail={"enabled": customer.auto_convert_to_crypto})
    db.session.commit()
    return jsonify(customer.to_dict())


# --- Verificação de identidade (KYC): documento + selfie por link --------
#
# Pessoa física manda RG ou CNH (frente e verso); empresa manda o cartão
# CNPJ. Depois, os dois tipos passam por uma selfie -- como ainda não tem
# app mobile, a selfie é tirada pelo NAVEGADOR DO CELULAR, através de um
# link de uso único que o customer-portal gera e mostra pro cliente (ele
# abre esse link no celular dele, tira a foto, pronto). O status do
# cliente (verification_status) anda sozinho conforme os arquivos chegam;
# só a decisão final (approved/rejected) é humana, feita pelo admin no
# admin-panel depois de olhar os documentos e a selfie lado a lado.
#
# ⚠️ Isso NÃO é reconhecimento facial biométrico automático (comparar a
# selfie com a foto do documento por algoritmo) -- é captura da selfie +
# conferência manual por um humano. Comparação biométrica de verdade exige
# um provedor especializado (ver aviso maior na resposta desta conversa);
# construir isso do zero, sem um provedor validado, tende a ser tanto
# inseguro (fácil de enganar com uma foto) quanto impreciso.

UPLOAD_DIR = os.environ.get("VERIFICATION_UPLOAD_DIR", "/app/uploads")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except OSError:
    # Ambiente sem permissão pra criar o diretório default agora (ex: rodando
    # testes fora do container) -- tenta de novo, de forma preguiçosa, na
    # hora do primeiro upload de verdade (_save_upload).
    pass

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB por arquivo
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
DOCUMENT_KINDS_FOR_CPF = {"id_front", "id_back"}
DOCUMENT_KINDS_FOR_CNPJ = {"cnpj_card"}
FACIAL_LINK_TTL_MINUTES = 30


def _required_document_kinds(customer) -> set:
    if customer.account.kind == "merchant":
        return set(DOCUMENT_KINDS_FOR_CNPJ)
    return set(DOCUMENT_KINDS_FOR_CPF)


def _recompute_verification_status(customer):
    """Anda o status sozinho conforme os arquivos chegam -- nunca mexe se
    já for uma decisão humana final (approved/rejected)."""
    if customer.verification_status in ("approved", "rejected"):
        return
    kinds_present = {
        d.kind for d in VerificationDocument.query.filter_by(customer_id=customer.id).all()
    }
    has_required_docs = _required_document_kinds(customer).issubset(kinds_present)
    has_selfie = "selfie" in kinds_present
    if has_required_docs and has_selfie:
        customer.verification_status = "pending_review"
    elif has_required_docs:
        customer.verification_status = "facial_pending"
    else:
        customer.verification_status = "documents_pending"


def _save_upload(file_storage, kind: str, customer_id: str) -> "VerificationDocument":
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    content_type = file_storage.mimetype
    if content_type not in ALLOWED_UPLOAD_TYPES:
        raise ValueError(f"tipo de arquivo não permitido: {content_type} (envie jpg, png, webp ou pdf)")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError("arquivo maior que 8MB")
    if size == 0:
        raise ValueError("arquivo vazio")

    ext = ALLOWED_UPLOAD_TYPES[content_type]
    stored_filename = f"{customer_id}_{kind}_{secrets.token_hex(8)}{ext}"
    stored_filename = secure_filename(stored_filename)
    file_storage.save(os.path.join(UPLOAD_DIR, stored_filename))

    doc = VerificationDocument(
        customer_id=customer_id,
        kind=kind,
        stored_filename=stored_filename,
        content_type=content_type,
    )
    db.session.add(doc)
    return doc


@bp.post("/customers/<customer_id>/documents")
def upload_customer_document(customer_id):
    """Upload de um documento de identidade (id_front/id_back/cnpj_card),
    chamado pelo customer-portal (o cliente ainda logado, no navegador
    onde fez o cadastro)."""
    customer = Customer.query.get_or_404(customer_id)
    kind = request.form.get("kind")
    allowed_kinds = DOCUMENT_KINDS_FOR_CPF | DOCUMENT_KINDS_FOR_CNPJ
    if kind not in allowed_kinds:
        return jsonify({"error": f"kind inválido: {kind}"}), 400
    if "file" not in request.files:
        return jsonify({"error": "nenhum arquivo enviado"}), 400

    if kind in DOCUMENT_KINDS_FOR_CPF:
        doc_type = request.form.get("document_type")
        if doc_type not in ("rg", "cnh"):
            return jsonify({"error": "document_type deve ser 'rg' ou 'cnh'"}), 400
        customer.id_document_type = doc_type

    try:
        doc = _save_upload(request.files["file"], kind, customer.id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    _recompute_verification_status(customer)
    _log_audit("customer.document_uploaded", actor=customer.id, detail={"kind": kind})
    db.session.commit()
    return jsonify({"document": doc.to_dict(), "customer": customer.to_dict()}), 201


@bp.get("/customers/<customer_id>/verification")
def get_customer_verification(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    documents = VerificationDocument.query.filter_by(customer_id=customer.id).all()
    return jsonify({
        "customer": customer.to_dict(),
        "required_kinds": sorted(_required_document_kinds(customer)),
        "documents": [d.to_dict() for d in documents],
    })


@bp.get("/verification-documents/<document_id>/file")
def get_verification_document_file(document_id):
    """Serve o arquivo em si. ⚠️ Sem autenticação própria (mesmo modelo de
    confiança do resto do core-ledger -- ver comentário no topo do
    arquivo/routes.py sobre não ser exposto direto pra internet): quem
    expõe isso pro admin logado é o admin-panel, que faz o proxy dessa
    chamada e é ele quem exige sessão de admin válida."""
    doc = VerificationDocument.query.get_or_404(document_id)
    return send_from_directory(UPLOAD_DIR, doc.stored_filename, mimetype=doc.content_type)


@bp.post("/customers/<customer_id>/facial-verification-link")
def create_facial_verification_link(customer_id):
    """Gera um link de uso único (expira em 30min) pro cliente abrir no
    celular e tirar a selfie -- o customer-portal monta a URL completa
    (com o domínio público dele) a partir do token retornado aqui."""
    customer = Customer.query.get_or_404(customer_id)
    link = FacialVerificationLink(
        customer_id=customer.id,
        token=secrets.token_urlsafe(24),
        expires_at=utcnow() + _dt.timedelta(minutes=FACIAL_LINK_TTL_MINUTES),
    )
    db.session.add(link)
    _log_audit("customer.facial_link_created", actor=customer.id)
    db.session.commit()
    return jsonify({
        "token": link.token,
        "expires_at": link.expires_at.isoformat(),
    }), 201


def _resolve_facial_link(token: str) -> "FacialVerificationLink":
    link = FacialVerificationLink.query.filter_by(token=token).first()
    if not link:
        abort(404)
    if link.used_at is not None:
        abort(410, description="esse link já foi usado")
    if _is_expired(link.expires_at):
        abort(410, description="esse link expirou")
    return link


@bp.get("/facial-verification/<token>")
def check_facial_verification_link(token):
    """Chamado pela página que abre no celular, pra confirmar que o link
    ainda vale antes de mostrar o botão de tirar a foto."""
    link = _resolve_facial_link(token)
    customer = Customer.query.get_or_404(link.customer_id)
    return jsonify({"valid": True, "customer_name": customer.name})


@bp.post("/facial-verification/<token>/selfie")
def submit_facial_verification_selfie(token):
    """Recebe a selfie tirada no celular. Rota pública de propósito (o
    celular normalmente não está logado no portal) -- protegida só pelo
    token, que é de uso único e expira rápido."""
    link = _resolve_facial_link(token)
    customer = Customer.query.get_or_404(link.customer_id)
    if "file" not in request.files:
        return jsonify({"error": "nenhum arquivo enviado"}), 400

    try:
        doc = _save_upload(request.files["file"], "selfie", customer.id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    link.used_at = utcnow()
    _recompute_verification_status(customer)
    _log_audit("customer.facial_selfie_submitted", actor=customer.id)
    db.session.commit()
    return jsonify({"document": doc.to_dict(), "customer": customer.to_dict()}), 201


@bp.get("/customers/verifications")
def list_customer_verifications():
    """Fila de verificação pro admin: por padrão só quem está pronto pra
    revisão (documentos + selfie enviados); ?status=all traz todo mundo."""
    status = request.args.get("status", "pending_review")
    query = Customer.query
    if status != "all":
        query = query.filter(Customer.verification_status == status)
    customers = query.order_by(Customer.created_at.desc()).limit(200).all()
    return jsonify([c.to_dict() for c in customers])


@bp.put("/customers/<customer_id>/verification/review")
def review_customer_verification(customer_id):
    """Decisão humana do admin depois de olhar os documentos + selfie."""
    customer = Customer.query.get_or_404(customer_id)
    data = request.get_json(force=True)
    decision = data.get("decision")
    if decision not in ("approved", "rejected", "documents_pending"):
        return jsonify({"error": "decision deve ser 'approved', 'rejected' ou 'documents_pending' (reabrir)"}), 400
    customer.verification_status = decision
    customer.verification_note = data.get("note") or None
    _log_audit("customer.verification_reviewed", actor="admin", detail={"customer_id": customer.id, "decision": decision})
    db.session.commit()
    return jsonify(customer.to_dict())


# --- Login único (admin ou cliente) e recuperação de senha por e-mail -----
#
# Uma tela de login só, pro dono do sistema e pros clientes: tenta como
# admin primeiro, depois como cliente, e devolve "role" pra quem chamou
# (customer-portal) saber pra onde mandar o navegador. A recuperação de
# senha (código por e-mail) usa a mesma tabela PasswordResetCode pros dois
# tipos de conta -- só muda o "subject_type".

AUTH_LOCKOUT_MAX_ATTEMPTS = 8
AUTH_LOCKOUT_WINDOW_MINUTES = 15
PASSWORD_RESET_CODE_TTL_MINUTES = 10
PASSWORD_RESET_TOKEN_TTL_MINUTES = 10
PASSWORD_RESET_MAX_ATTEMPTS = 5


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_numeric_code(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _is_expired(expires_at) -> bool:
    """Compara um datetime salvo no banco com "agora", sem quebrar se um dos
    dois for timezone-aware e o outro não -- MySQL (e SQLite) costumam
    devolver o datetime sem tzinfo mesmo quando a coluna é
    DateTime(timezone=True), então comparar direto com utcnow() (que É
    aware) derruba com TypeError."""
    if expires_at is None:
        return True
    now = utcnow()
    if expires_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    return expires_at < now


def _resolve_login_subject(login: str):
    """Acha quem é esse login: admin (por e-mail) ou cliente (documento ou
    e-mail). Retorna (subject_type, subject) ou (None, None)."""
    admin = AdminUser.query.filter_by(email=login).first()
    if admin:
        return "admin", admin
    customer = Customer.query.filter(
        (Customer.document == login) | (Customer.email == login)
    ).first()
    if customer:
        return "customer", customer
    return None, None


@bp.post("/auth/login")
def auth_login():
    """Login único: tenta como admin primeiro, depois como cliente do
    portal. A resposta traz "role" ("admin"|"customer") pra quem chamou
    decidir pra onde mandar o usuário -- painel do admin ou painel do
    cliente."""
    data = request.get_json(force=True)
    login = (data.get("login") or "").strip()
    password = data.get("password", "")

    cutoff = utcnow() - _dt.timedelta(minutes=AUTH_LOCKOUT_WINDOW_MINUTES)
    recent_failures = AuditLog.query.filter(
        AuditLog.event_type == "auth.login_failed",
        AuditLog.actor == login,
        AuditLog.created_at > cutoff,
    ).count()
    if recent_failures >= AUTH_LOCKOUT_MAX_ATTEMPTS:
        return jsonify({
            "error": f"muitas tentativas erradas. Tente de novo em {AUTH_LOCKOUT_WINDOW_MINUTES} minutos."
        }), 429

    admin = AdminUser.query.filter_by(email=login).first()
    if admin and check_password_hash(admin.password_hash, password):
        _log_audit("auth.login_success", actor=f"admin:{admin.id}")
        db.session.commit()
        return jsonify({"role": "admin", **admin.to_dict()})

    customer = Customer.query.filter(
        (Customer.document == login) | (Customer.email == login)
    ).first()
    if customer and customer.password_hash and check_password_hash(customer.password_hash, password):
        _log_audit("auth.login_success", actor=f"customer:{customer.id}")
        db.session.commit()
        return jsonify({"role": "customer", **customer.to_dict()})

    _log_audit("auth.login_failed", actor=login)
    db.session.commit()
    return jsonify({"error": "credenciais inválidas"}), 401


@bp.post("/auth/password-reset/request")
def password_reset_request():
    """Passo 1: gera um código de 6 dígitos e manda por e-mail. Sempre
    responde com a mesma mensagem genérica, exista ou não esse login --
    senão dá pra usar essa tela pra descobrir quais e-mails/documentos têm
    conta no sistema (enumeração de usuários)."""
    data = request.get_json(force=True)
    login = (data.get("login") or "").strip()
    generic_message = {"message": "Se esse login existir e tiver e-mail cadastrado, enviamos um código pra ele."}

    if not login:
        return jsonify(generic_message), 200

    subject_type, subject = _resolve_login_subject(login)
    email = getattr(subject, "email", None) if subject else None
    if not subject or not email:
        _log_audit("password_reset.requested_unknown", actor=login)
        db.session.commit()
        return jsonify(generic_message), 200

    code = _generate_numeric_code()
    db.session.add(PasswordResetCode(
        subject_type=subject_type,
        subject_id=subject.id,
        code_hash=_hash_secret(code),
        code_expires_at=utcnow() + _dt.timedelta(minutes=PASSWORD_RESET_CODE_TTL_MINUTES),
    ))
    _log_audit("password_reset.requested", actor=f"{subject_type}:{subject.id}")

    try:
        send_email(
            email,
            "Seu código de recuperação — Divisions Pay",
            f"Seu código para redefinir a senha é: {code}\n\n"
            f"Ele vale por {PASSWORD_RESET_CODE_TTL_MINUTES} minutos. "
            "Se você não pediu isso, pode ignorar este e-mail.",
        )
    except Exception:
        # Nunca deixa quem chamou saber se o e-mail existe ou nao, e nunca
        # derruba a operacao por causa disso -- so registra no log do
        # servico, pro admin perceber que o SMTP esta com problema.
        logging.getLogger(__name__).exception(
            "falha ao enviar e-mail de recuperacao de senha (subject_type=%s subject_id=%s)",
            subject_type, subject.id,
        )

    db.session.commit()
    return jsonify(generic_message), 200


@bp.post("/auth/password-reset/verify")
def password_reset_verify():
    """Passo 2: troca o código de 6 dígitos por um reset_token de uso único
    (curto, ~10min), sem o qual não dá pra trocar a senha."""
    data = request.get_json(force=True)
    login = (data.get("login") or "").strip()
    code = (data.get("code") or "").strip()

    subject_type, subject = _resolve_login_subject(login)
    if not subject:
        return jsonify({"error": "código inválido ou expirado"}), 400

    row = (
        PasswordResetCode.query
        .filter_by(subject_type=subject_type, subject_id=subject.id, used_at=None)
        .order_by(PasswordResetCode.created_at.desc())
        .first()
    )
    if not row or _is_expired(row.code_expires_at):
        return jsonify({"error": "código inválido ou expirado"}), 400
    if row.attempts >= PASSWORD_RESET_MAX_ATTEMPTS:
        return jsonify({"error": "muitas tentativas -- peça um código novo"}), 429

    if row.code_hash != _hash_secret(code):
        row.attempts += 1
        db.session.commit()
        return jsonify({"error": "código incorreto"}), 400

    reset_token = secrets.token_urlsafe(32)
    row.verified_at = utcnow()
    row.reset_token_hash = _hash_secret(reset_token)
    row.reset_token_expires_at = utcnow() + _dt.timedelta(minutes=PASSWORD_RESET_TOKEN_TTL_MINUTES)
    _log_audit("password_reset.verified", actor=f"{subject_type}:{subject.id}")
    db.session.commit()

    return jsonify({"reset_token": reset_token, "role": subject_type})


@bp.post("/auth/password-reset/confirm")
def password_reset_confirm():
    """Passo 3: troca o reset_token (do passo 2) pela senha nova."""
    data = request.get_json(force=True)
    reset_token = (data.get("reset_token") or "").strip()
    new_password = data.get("new_password") or ""

    if not reset_token:
        return jsonify({"error": "token inválido"}), 400

    weak = _password_weakness(new_password)
    if weak:
        return jsonify({"error": weak}), 400

    row = (
        PasswordResetCode.query
        .filter_by(reset_token_hash=_hash_secret(reset_token), used_at=None)
        .first()
    )
    if not row or _is_expired(row.reset_token_expires_at):
        return jsonify({"error": "token inválido ou expirado -- peça a recuperação de novo"}), 400

    if row.subject_type == "admin":
        subject = AdminUser.query.get(row.subject_id)
    else:
        subject = Customer.query.get(row.subject_id)
    if not subject:
        return jsonify({"error": "conta não encontrada"}), 404

    subject.password_hash = generate_password_hash(new_password)
    row.used_at = utcnow()
    _log_audit("password_reset.completed", actor=f"{row.subject_type}:{row.subject_id}")
    db.session.commit()

    return jsonify({"status": "ok", "role": row.subject_type})


# --- Configuracoes da plataforma: conta e percentual da taxa -------------

@bp.get("/admin/settings/platform")
def get_platform_settings():
    rows = {r.key: r.value for r in PlatformSetting.query.all()}
    return jsonify({
        "platform_account_id": rows.get("platform_account_id"),
        "fee_bps": int(rows.get("fee_bps", 100)),
    })


@bp.put("/admin/settings/platform")
def set_platform_settings():
    data = request.get_json(force=True)
    for key in ("platform_account_id", "fee_bps"):
        if key not in data:
            continue
        row = PlatformSetting.query.get(key)
        value = str(data[key]) if data[key] is not None else None
        if row is None:
            row = PlatformSetting(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value
    db.session.commit()
    rows = {r.key: r.value for r in PlatformSetting.query.all()}
    return jsonify({
        "platform_account_id": rows.get("platform_account_id"),
        "fee_bps": int(rows.get("fee_bps", 100)),
    })


@bp.get("/customers/<customer_id>")
def get_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    return jsonify(customer.to_dict())


@bp.get("/customers/<customer_id>/statement")
def customer_statement(customer_id):
    """Extrato: todos os lançamentos que afetaram a carteira do cliente,
    mais recentes primeiro. Suporta paginação simples (?limit=&offset=)."""
    customer = Customer.query.get_or_404(customer_id)
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    entries = (
        LedgerEntry.query.filter_by(account_id=customer.account_id)
        .order_by(LedgerEntry.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    out = []
    for e in entries:
        txn = Transaction.query.get(e.transaction_id)
        out.append({
            "amount_cents": e.amount_cents,
            "created_at": e.created_at.isoformat(),
            "rail": txn.rail,
            "transaction_id": txn.id,
            "transaction_status": txn.status.value,
            "metadata": txn.metadata_json,
        })
    return jsonify({
        "account_id": customer.account_id,
        "balance_cents": customer.account.balance_cents,
        "entries": out,
    })


SYSTEM_ACCOUNT_LABELS = {
    "system:pix_pending": "Mercado Pago / Pagar.me (PIX)",
    "system:card_receivable": "Adquirente de cartão",
    "system:crypto_pending": "Rede cripto (on-chain)",
    "system:payouts_pending": "Saque PIX (banco de destino)",
    "system:fees": "Divisions Pay (taxas)",
    "system:crypto_conversion_pool_brl": "Conversão automática para cripto",
    "system:crypto_conversion_pool_usdt": "Conversão automática para cripto",
}


def _party_for_account(account_id):
    """Descobre quem é o dono de uma conta contábil pra montar o
    comprovante: um Customer/lojista de verdade (nome + documento) ou uma
    conta de sistema (rotulada de forma amigável, sem expor o owner_ref
    cru pro cliente final)."""
    customer = Customer.query.filter_by(account_id=account_id).first()
    if customer:
        return {
            "kind": "merchant" if customer.document and len(customer.document) > 11 else "customer",
            "name": customer.name,
            "document": customer.document,
        }
    account = Account.query.get(account_id)
    if account and account.kind == "system":
        label = SYSTEM_ACCOUNT_LABELS.get(account.owner_ref, "Divisions Pay (sistema)")
        return {"kind": "system", "name": label, "document": None}
    return {"kind": "unknown", "name": "—", "document": None}


@bp.get("/transactions/<transaction_id>")
def get_transaction(transaction_id):
    """Detalhe completo de uma transação -- usado tanto pelo pix-service
    (reconciliação/checagem de status) quanto pelo comprovante do
    customer-portal. Cada lançamento vem com "party": quem enviou/recebeu
    (Customer/lojista de verdade, com nome + documento) ou uma conta de
    sistema (rotulada de forma amigável, sem expor o owner_ref cru). Não faz
    controle de acesso aqui -- quem chama é responsável por confirmar que o
    cliente logado participa dessa transação antes de mostrar o resultado."""
    txn = Transaction.query.get_or_404(transaction_id)
    entries = []
    for e in txn.entries:
        entries.append({
            "id": e.id,
            "account_id": e.account_id,
            "amount_cents": e.amount_cents,
            "created_at": e.created_at.isoformat(),
            "party": _party_for_account(e.account_id),
        })
    return jsonify({
        "id": txn.id,
        "rail": txn.rail,
        "status": txn.status.value,
        "external_ref": txn.external_ref,
        "metadata": txn.metadata_json,
        "created_at": txn.created_at.isoformat(),
        "entries": entries,
    })


@bp.patch("/transactions/<transaction_id>/metadata")
def patch_transaction_metadata(transaction_id):
    """Mescla campos extras no metadata_json de uma transação (nunca apaga
    o que já existe). Existe pra permitir enriquecer uma cobrança depois
    que ela já foi criada -- por exemplo, o pix-service adicionando os
    dados bancários do pagador assim que o provedor os disponibiliza,
    tipicamente só depois que o pagamento é confirmado."""
    txn = Transaction.query.get_or_404(transaction_id)
    patch = request.get_json(force=True) or {}
    merged = dict(txn.metadata_json or {})
    merged.update(patch)
    txn.metadata_json = merged
    db.session.commit()
    return jsonify(txn.to_dict())


@bp.post("/transfers")
def transfer():
    """Transferência interna instantânea entre duas carteiras da plataforma
    (cliente->cliente, cliente->lojista, etc). Não sai da plataforma — não é
    PIX, TED nem saque; é só um lançamento em partida dobrada, liquidado na
    hora. Bloqueia se o saldo de origem não for suficiente (a menos que a
    conta tenha allow_negative=True, o que normalmente só vale pra contas de
    sistema, não pra carteiras de cliente/lojista)."""
    data = request.get_json(force=True)
    idempotency_key = data["idempotency_key"]
    from_account_id = data["from_account_id"]
    to_account_id = data["to_account_id"]
    amount_cents = data["amount_cents"]

    if amount_cents <= 0:
        return jsonify({"error": "amount_cents deve ser positivo"}), 400

    existing = Transaction.query.filter_by(idempotency_key=idempotency_key).first()
    if existing:
        return jsonify(existing.to_dict()), 200

    from_acc = Account.query.get_or_404(from_account_id)
    to_acc = Account.query.get_or_404(to_account_id)

    if not from_acc.allow_negative and from_acc.balance_cents < amount_cents:
        return jsonify({
            "error": "saldo insuficiente",
            "balance_cents": from_acc.balance_cents,
            "requested_cents": amount_cents,
        }), 402

    txn = Transaction(
        idempotency_key=idempotency_key,
        rail="internal_transfer",
        status=EntryStatus.PENDING,
        metadata_json=data.get("metadata"),
    )
    db.session.add(txn)
    db.session.flush()

    db.session.add(LedgerEntry(transaction_id=txn.id, account_id=from_acc.id, amount_cents=-amount_cents))
    db.session.add(LedgerEntry(transaction_id=txn.id, account_id=to_acc.id, amount_cents=amount_cents))

    from_acc.balance_cents -= amount_cents
    to_acc.balance_cents += amount_cents
    txn.status = EntryStatus.CONFIRMED
    db.session.add(WebhookEvent(transaction_id=txn.id, event_type="transfer.confirmed"))
    _log_audit("transfer.confirmed", actor=from_acc.id, detail={
        "to_account_id": to_acc.id, "amount_cents": amount_cents,
    })

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Transaction.query.filter_by(idempotency_key=idempotency_key).first()
        return jsonify(existing.to_dict()), 200

    return jsonify(txn.to_dict()), 201


# --- Saques (saída de dinheiro real pra outro banco via chave PIX) -------
#
# O valor sai do saldo do cliente na hora que o pedido é criado (evita
# gastar o mesmo saldo duas vezes enquanto o saque está pendente). O envio
# de verdade pra fora da Divisions Pay é feito manualmente pelo admin, pela
# conta real do Mercado Pago da empresa — ver WithdrawalRequest em models.py
# pra entender por quê.

@bp.post("/withdrawals")
def create_withdrawal():
    data = request.get_json(force=True)
    customer_id = data["customer_id"]
    amount_cents = data["amount_cents"]
    pix_key = (data.get("pix_key") or "").strip()
    pix_key_type = data.get("pix_key_type", "cpf")
    idempotency_key = data["idempotency_key"]

    if amount_cents <= 0:
        return jsonify({"error": "amount_cents deve ser positivo"}), 400
    if not pix_key:
        return jsonify({"error": "informe a chave PIX de destino"}), 400

    existing_txn = Transaction.query.filter_by(idempotency_key=idempotency_key).first()
    if existing_txn:
        existing_wr = WithdrawalRequest.query.filter_by(transaction_id=existing_txn.id).first()
        return jsonify(existing_wr.to_dict()), 200

    customer = Customer.query.get_or_404(customer_id)
    from_acc = customer.account
    payouts_pending_id = _auto_get_or_create_system_account("system:payouts_pending")
    payouts_acc = Account.query.get(payouts_pending_id)

    if from_acc.balance_cents < amount_cents:
        return jsonify({
            "error": "saldo insuficiente",
            "balance_cents": from_acc.balance_cents,
            "requested_cents": amount_cents,
        }), 402

    txn = Transaction(
        idempotency_key=idempotency_key,
        rail="withdrawal_pix",
        status=EntryStatus.PENDING,
        metadata_json={"pix_key": pix_key, "pix_key_type": pix_key_type},
    )
    db.session.add(txn)
    db.session.flush()

    db.session.add(LedgerEntry(transaction_id=txn.id, account_id=from_acc.id, amount_cents=-amount_cents))
    db.session.add(LedgerEntry(transaction_id=txn.id, account_id=payouts_acc.id, amount_cents=amount_cents))
    from_acc.balance_cents -= amount_cents
    payouts_acc.balance_cents += amount_cents
    txn.status = EntryStatus.CONFIRMED

    withdrawal = WithdrawalRequest(
        customer_id=customer.id,
        account_id=from_acc.id,
        amount_cents=amount_cents,
        pix_key=pix_key,
        pix_key_type=pix_key_type,
        status="pending",
        transaction_id=txn.id,
    )
    db.session.add(withdrawal)
    _log_audit("withdrawal.requested", actor=customer.id, detail={
        "amount_cents": amount_cents, "pix_key_type": pix_key_type,
    })

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing_txn = Transaction.query.filter_by(idempotency_key=idempotency_key).first()
        existing_wr = WithdrawalRequest.query.filter_by(transaction_id=existing_txn.id).first()
        return jsonify(existing_wr.to_dict()), 200

    return jsonify(withdrawal.to_dict()), 201


@bp.get("/withdrawals")
def list_withdrawals():
    status = request.args.get("status")
    query = WithdrawalRequest.query
    if status:
        query = query.filter_by(status=status)
    withdrawals = query.order_by(WithdrawalRequest.created_at.desc()).limit(200).all()
    out = []
    for w in withdrawals:
        d = w.to_dict()
        customer = Customer.query.get(w.customer_id)
        d["customer_name"] = customer.name if customer else None
        out.append(d)
    return jsonify(out)


@bp.get("/customers/<customer_id>/withdrawals")
def list_customer_withdrawals(customer_id):
    withdrawals = (
        WithdrawalRequest.query.filter_by(customer_id=customer_id)
        .order_by(WithdrawalRequest.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([w.to_dict() for w in withdrawals])


@bp.post("/withdrawals/<withdrawal_id>/mark-paid")
def mark_withdrawal_paid(withdrawal_id):
    """Chamado pelo admin depois de mandar o PIX de verdade (pela conta real
    do Mercado Pago da empresa) pra chave do cliente. Só registra que o
    saque foi cumprido — o dinheiro já tinha saído do saldo do cliente
    quando o pedido foi criado."""
    withdrawal = WithdrawalRequest.query.get_or_404(withdrawal_id)
    if withdrawal.status != "pending":
        return jsonify({"error": f"saque não está pendente (status atual: {withdrawal.status})"}), 409

    data = request.get_json(silent=True) or {}
    withdrawal.status = "paid"
    withdrawal.resolved_at = utcnow()
    withdrawal.admin_note = data.get("note")
    _log_audit("withdrawal.paid", actor="admin", detail={"withdrawal_id": withdrawal.id})
    db.session.commit()
    return jsonify(withdrawal.to_dict())


@bp.post("/withdrawals/<withdrawal_id>/mark-failed")
def mark_withdrawal_failed(withdrawal_id):
    """O admin não conseguiu completar o PIX de saída (chave inválida, etc.)
    — estorna o valor de volta pro saldo do cliente."""
    withdrawal = WithdrawalRequest.query.get_or_404(withdrawal_id)
    if withdrawal.status != "pending":
        return jsonify({"error": f"saque não está pendente (status atual: {withdrawal.status})"}), 409

    data = request.get_json(silent=True) or {}
    from_acc = Account.query.get(withdrawal.account_id)
    payouts_pending_id = _auto_get_or_create_system_account("system:payouts_pending")
    payouts_acc = Account.query.get(payouts_pending_id)

    reversal = Transaction(
        idempotency_key=f"withdrawal-reversal:{withdrawal.id}",
        rail="withdrawal_pix_reversal",
        status=EntryStatus.CONFIRMED,
        metadata_json={"withdrawal_id": withdrawal.id, "reason": data.get("note")},
    )
    db.session.add(reversal)
    db.session.flush()
    db.session.add(LedgerEntry(transaction_id=reversal.id, account_id=payouts_acc.id, amount_cents=-withdrawal.amount_cents))
    db.session.add(LedgerEntry(transaction_id=reversal.id, account_id=from_acc.id, amount_cents=withdrawal.amount_cents))
    payouts_acc.balance_cents -= withdrawal.amount_cents
    from_acc.balance_cents += withdrawal.amount_cents

    withdrawal.status = "failed"
    withdrawal.resolved_at = utcnow()
    withdrawal.admin_note = data.get("note")
    withdrawal.reversal_transaction_id = reversal.id
    _log_audit("withdrawal.failed", actor="admin", detail={"withdrawal_id": withdrawal.id, "note": data.get("note")})
    db.session.commit()
    return jsonify(withdrawal.to_dict())
