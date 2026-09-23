from flask import Blueprint, request, jsonify
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from .models import db, Account, Transaction, LedgerEntry, WebhookEvent, EntryStatus, PlatformSetting

bp = Blueprint("ledger", __name__)


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
    except IntegrityError:
        db.session.rollback()
        existing = Transaction.query.filter_by(
            idempotency_key=data["idempotency_key"]
        ).first()
        return jsonify(existing.to_dict()), 200

    return jsonify(txn.to_dict()), 201


@bp.post("/transactions/<transaction_id>/settle")
def settle_transaction(transaction_id):
    """Confirma uma transação pendente e aplica o efeito nos saldos das contas.
    Chamado pelos adapters (pix/card/crypto-service) quando o PSP confirma o pagamento."""
    txn = Transaction.query.get_or_404(transaction_id)

    if txn.status == EntryStatus.CONFIRMED:
        return jsonify(txn.to_dict()), 200

    if txn.status != EntryStatus.PENDING:
        return jsonify({"error": f"cannot settle transaction in status {txn.status.value}"}), 409

    accounts = {e.account_id: Account.query.get(e.account_id) for e in txn.entries}
    for entry in txn.entries:
        acc = accounts[entry.account_id]
        projected = acc.balance_cents + entry.amount_cents
        if projected < 0 and not acc.allow_negative:
            return jsonify({
                "error": "settle recusado: deixaria a conta com saldo negativo",
                "account_id": acc.id,
                "balance_cents": acc.balance_cents,
            }), 409

    for entry in txn.entries:
        accounts[entry.account_id].balance_cents += entry.amount_cents

    txn.status = EntryStatus.CONFIRMED
    db.session.add(WebhookEvent(transaction_id=txn.id, event_type="transaction.confirmed"))
    db.session.commit()

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


@bp.get("/transactions/<transaction_id>")
def get_transaction(transaction_id):
    txn = Transaction.query.get_or_404(transaction_id)
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

from .models import Customer


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
    acc = Account(
        owner_ref=data.get("document") or data["name"],
        kind=data.get("kind", "customer"),
        currency=data.get("currency", "BRL"),
        allow_negative=False,
    )
    db.session.add(acc)
    db.session.flush()

    password = data.get("password")
    customer = Customer(
        account_id=acc.id,
        name=data["name"],
        document=data.get("document"),
        email=data.get("email"),
        password_hash=generate_password_hash(password) if password else None,
    )
    db.session.add(customer)
    db.session.commit()
    return jsonify(customer.to_dict()), 201


@bp.post("/customers/authenticate")
def authenticate_customer():
    """Login do portal do cliente: verifica documento/e-mail + senha.
    Retorna o cliente se bater, 401 caso contrario. Nao expoe password_hash."""
    data = request.get_json(force=True)
    login = data.get("login", "")
    password = data.get("password", "")

    customer = Customer.query.filter(
        (Customer.document == login) | (Customer.email == login)
    ).first()

    if not customer or not customer.password_hash or not check_password_hash(customer.password_hash, password):
        return jsonify({"error": "credenciais invalidas"}), 401

    return jsonify(customer.to_dict())


@bp.put("/customers/<customer_id>/password")
def set_customer_password(customer_id):
    """Define/troca a senha de acesso ao portal (chamado pelo admin-panel
    quando o admin cria/reseta o acesso de um cliente)."""
    customer = Customer.query.get_or_404(customer_id)
    data = request.get_json(force=True)
    password = data.get("password")
    if not password or len(password) < 6:
        return jsonify({"error": "senha precisa ter pelo menos 6 caracteres"}), 400
    customer.password_hash = generate_password_hash(password)
    db.session.commit()
    return jsonify(customer.to_dict())


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

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Transaction.query.filter_by(idempotency_key=idempotency_key).first()
        return jsonify(existing.to_dict()), 200

    return jsonify(txn.to_dict()), 201
