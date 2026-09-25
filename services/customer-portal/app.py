"""customer-portal — Divisions Pay: painel do cliente, estilo banco digital.

Cada cliente loga com o próprio documento/e-mail + senha (definida pelo
admin ao criar a conta, ou trocada depois via /customers/<id>/password no
core-ledger) e só vê os dados da própria carteira — sessão isolada por
cliente, nunca a lista de outros clientes. O cliente também pode transferir
para outro cliente da plataforma (tipo PicPay) e pedir saque pra uma chave
PIX de outro banco, sem precisar passar pelo admin pra iniciar o pedido.

Segurança: cookies de sessão com HttpOnly/Secure/SameSite, cabeçalhos de
segurança padrão, proteção CSRF em todo POST, e o rate-limit de tentativas
de login mora no core-ledger (compartilhado com o admin-panel se algum dia
precisar), não duplicado aqui.
"""
import hashlib
import hmac
import os
import secrets
import time
import uuid

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort

app = Flask(__name__)
app.secret_key = os.environ.get("CUSTOMER_PORTAL_SECRET", "troque-isto-em-producao")

# URL pública do admin-panel (a que o navegador do admin consegue acessar de
# fora -- não o endereço interno do Docker) e segredo compartilhado com ele
# pra assinar o "bilhete" de handoff: o admin digita a senha aqui, uma vez
# só, e é redirecionado já autenticado pro painel dele (ver login_submit()).
ADMIN_PANEL_PUBLIC_URL = os.environ.get("ADMIN_PANEL_PUBLIC_URL", "").rstrip("/")
SSO_SIGNING_SECRET = os.environ.get("SSO_SIGNING_SECRET", "troque-isto-em-producao")

# URL pública deste próprio serviço -- necessária pro link de verificação
# facial, porque esse link é aberto num dispositivo DIFERENTE (o celular do
# cliente, não o navegador onde ele fez o cadastro), então precisa ser uma
# URL absoluta, nunca relativa.
CUSTOMER_PORTAL_PUBLIC_URL = os.environ.get("CUSTOMER_PORTAL_PUBLIC_URL", "").rstrip("/")
SSO_TOKEN_TTL_SECONDS = 60

# Cookie de sessão o mais travado possível: só HTTPS (EasyPanel termina TLS
# na borda e repassa por HTTP interno — SESSION_COOKIE_SECURE ainda funciona
# porque o navegador só manda o cookie de volta pra origem HTTPS mesmo),
# inacessível a JavaScript (mitiga roubo de sessão via XSS) e nunca enviado
# em navegação cross-site (mitiga CSRF, em conjunto com o token abaixo).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS_COOKIES", "true").lower() != "false",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 2,  # 2h de sessão parada = desloga
)

LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")

RAIL_LABELS = {
    "pix": "Pix recebido",
    "card": "Cartão recebido",
    "crypto": "Cripto recebida",
    "internal_transfer": "Transferência",
    "withdrawal_pix": "Saque",
    "withdrawal_pix_reversal": "Estorno de saque",
}

PIX_KEY_TYPES = {
    "cpf": "CPF",
    "cnpj": "CNPJ",
    "email": "E-mail",
    "phone": "Telefone",
    "random": "Chave aleatória",
}


# --- Segurança: cabeçalhos padrão e CSRF ------------------------------------

@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
        "font-src fonts.gstatic.com; img-src 'self' data:; script-src 'self'"
    )
    return resp


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def _check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, description="Falha de validação do formulário (CSRF). Recarregue a página e tente de novo.")


@app.errorhandler(400)
def bad_request(e):
    flash(str(e.description) if hasattr(e, "description") else "Requisição inválida.", "error")
    return redirect(request.referrer or url_for("login_form")), 400


# --- Helpers -----------------------------------------------------------------

def api_get(path, **kwargs):
    r = requests.get(f"{LEDGER_URL}{path}", timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()


def _build_sso_url(admin_id: str) -> str:
    """Assina um "bilhete" de handoff de curtíssima duração (60s, uso único
    na prática porque o admin-panel confere o prazo) pra logar o admin no
    painel dele sem pedir a senha de novo -- ele já provou quem é aqui."""
    exp = int(time.time()) + SSO_TOKEN_TTL_SECONDS
    manifest = f"{admin_id}:{exp}"
    sig = hmac.new(SSO_SIGNING_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return f"{ADMIN_PANEL_PUBLIC_URL}/sso?admin_id={admin_id}&exp={exp}&sig={sig}"


def current_customer():
    customer_id = session.get("customer_id")
    if not customer_id:
        return None
    try:
        return api_get(f"/customers/{customer_id}")
    except requests.RequestException:
        return None


def require_login():
    return current_customer()


AUTO_CONVERT_LABELS = {
    "auto_convert_to_crypto_brl_leg": "Conversão automática → USDT",
}


def enrich_entry(entry):
    """Anota cada lançamento do extrato com o que o painel precisa pra
    desenhar (rótulo em português, se é entrada ou saída, ícone)."""
    amount = entry["amount_cents"]
    rail = entry.get("rail", "internal_transfer")
    meta = entry.get("metadata") or {}
    kind = meta.get("kind")
    entry["is_credit"] = amount > 0
    entry["rail_label"] = AUTO_CONVERT_LABELS.get(kind) or RAIL_LABELS.get(rail, rail.title())
    entry["amount_display"] = format_currency(abs(amount))
    return entry


def format_currency(cents):
    return f"{cents / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def mask_document(document):
    """Mascara CPF/CNPJ pro comprovante: mantém só os últimos 3
    dígitos visíveis, troca os demais dígitos por "*" (pontuação
    do documento -- pontos, barra, traço -- é preservada como está)."""
    if not document:
        return None
    digits_total = sum(1 for c in document if c.isdigit())
    kept = 0
    out = []
    for ch in reversed(document):
        if ch.isdigit():
            kept += 1
            out.append(ch if (digits_total - kept) >= digits_total - 3 else "*")
        else:
            out.append(ch)
    return "".join(reversed(out))


def _validate_cpf(digits: str) -> bool:
    """Valida CPF pelo algoritmo oficial (dígitos verificadores), não só o
    tamanho -- barra na hora erros de digitação no cadastro, em vez de
    deixar um CPF inválido virar conta e só dar problema depois (login,
    saque, comprovante)."""
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for i in (9, 10):
        total = sum(int(digits[n]) * ((i + 1) - n) for n in range(i))
        check = ((total * 10) % 11) % 10
        if check != int(digits[i]):
            return False
    return True


def _validate_cnpj(digits: str) -> bool:
    """Valida CNPJ pelo algoritmo oficial (dígitos verificadores)."""
    if len(digits) != 14 or digits == digits[0] * 14:
        return False

    def _check_digit(partial: str) -> str:
        weights = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2] if len(partial) == 13 else [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        total = sum(int(d) * w for d, w in zip(partial, weights))
        remainder = total % 11
        return "0" if remainder < 2 else str(11 - remainder)

    d1 = _check_digit(digits[:12])
    d2 = _check_digit(digits[:12] + d1)
    return digits[-2:] == d1 + d2


STATUS_LABELS = {
    "pending": "Pendente",
    "confirmed": "Confirmada",
    "failed": "Falhou",
    "reversed": "Estornada",
}

app.jinja_env.filters["brl"] = format_currency
app.jinja_env.filters["mask_document"] = mask_document


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login")
def login_form():
    if session.get("customer_id"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.post("/login")
def login_submit():
    """Login único: essa mesma tela serve o admin e os clientes. O
    core-ledger diz quem é quem (campo "role") -- cliente segue normal
    (sessão aqui mesmo); admin é redirecionado, já autenticado, pro painel
    dele via handoff assinado (ver _build_sso_url)."""
    r = requests.post(f"{LEDGER_URL}/auth/login", json={
        "login": request.form["login"],
        "password": request.form["password"],
    }, timeout=10)
    if r.status_code == 429:
        flash("Muitas tentativas erradas. Aguarde alguns minutos e tente de novo.", "error")
        return redirect(url_for("login_form"))
    if r.status_code != 200:
        flash("Login ou senha incorretos.", "error")
        return redirect(url_for("login_form"))

    account = r.json()
    session.clear()

    if account["role"] == "admin":
        if not ADMIN_PANEL_PUBLIC_URL:
            flash("Login de administrador reconhecido, mas o painel do admin ainda não foi configurado (falta ADMIN_PANEL_PUBLIC_URL). Fale com quem administra o servidor.", "error")
            return redirect(url_for("login_form"))
        return redirect(_build_sso_url(account["id"]))

    session["customer_id"] = account["id"]
    session.permanent = True
    return redirect(url_for("dashboard"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_form"))


# --- Cadastro (auto-atendimento): pessoa física (CPF) ou empresa (CNPJ) ----

@app.get("/cadastro")
def register_form():
    if session.get("customer_id"):
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.post("/cadastro")
def register_submit():
    if session.get("customer_id"):
        return redirect(url_for("dashboard"))

    account_type = request.form.get("account_type", "cpf")
    name = request.form.get("name", "").strip()
    document_digits = "".join(c for c in request.form.get("document", "") if c.isdigit())
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    password_confirm = request.form.get("password_confirm", "")

    if not name:
        flash("Informe seu nome completo (ou razão social, se for empresa).", "error")
        return redirect(url_for("register_form"))

    if account_type == "cnpj":
        if not _validate_cnpj(document_digits):
            flash("CNPJ inválido. Confira os números digitados.", "error")
            return redirect(url_for("register_form"))
    else:
        if not _validate_cpf(document_digits):
            flash("CPF inválido. Confira os números digitados.", "error")
            return redirect(url_for("register_form"))

    if not email or "@" not in email:
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("register_form"))

    if len(password) < 8 or not password_confirm:
        flash("A senha precisa ter pelo menos 8 caracteres, com letras e números.", "error")
        return redirect(url_for("register_form"))
    if password != password_confirm:
        flash("As senhas não conferem.", "error")
        return redirect(url_for("register_form"))

    r = requests.post(f"{LEDGER_URL}/customers", json={
        "name": name,
        "document": document_digits,
        "email": email,
        "password": password,
        "kind": "merchant" if account_type == "cnpj" else "customer",
    }, timeout=10)

    if r.status_code == 409:
        flash("Já existe uma conta com esse CPF/CNPJ ou e-mail. Tente entrar ou recuperar sua senha.", "error")
        return redirect(url_for("register_form"))
    if r.status_code == 400:
        error_msg = (r.json() or {}).get("error") or "Não foi possível concluir o cadastro. Confira os dados."
        flash(error_msg, "error")
        return redirect(url_for("register_form"))
    if r.status_code not in (200, 201):
        flash("Não foi possível concluir o cadastro. Tente novamente em instantes.", "error")
        return redirect(url_for("register_form"))

    customer = r.json()
    session.clear()
    session["customer_id"] = customer["id"]
    session.permanent = True
    flash("Conta criada! Agora precisamos verificar sua identidade antes de liberar saques.", "success")
    return redirect(url_for("verification_form"))


# --- Esqueci minha senha: código por e-mail, em 3 telas ---------------------
#
# Serve admin e cliente igual (o core-ledger resolve quem é o login). O
# reset_token do passo 2 fica só na sessão do servidor (nunca na URL nem
# visível pro usuário) até o passo 3 confirmar a senha nova.

@app.get("/esqueci-senha")
def forgot_password_form():
    return render_template("forgot_password.html")


@app.post("/esqueci-senha")
def forgot_password_submit():
    login = request.form.get("login", "").strip()
    if login:
        requests.post(f"{LEDGER_URL}/auth/password-reset/request", json={"login": login}, timeout=10)
    # Sempre segue pro passo do código, exista ou não esse login -- não dá
    # pra essa tela virar um jeito de descobrir quais contas existem.
    session["pwreset_login"] = login
    flash("Se esse login existir, enviamos um código de 6 dígitos pro e-mail cadastrado.", "success")
    return redirect(url_for("forgot_password_code_form"))


@app.get("/esqueci-senha/codigo")
def forgot_password_code_form():
    login = session.get("pwreset_login")
    if not login:
        return redirect(url_for("forgot_password_form"))
    return render_template("forgot_password_code.html", login=login)


@app.post("/esqueci-senha/codigo")
def forgot_password_code_submit():
    login = session.get("pwreset_login")
    if not login:
        return redirect(url_for("forgot_password_form"))

    code = request.form.get("code", "").strip()
    r = requests.post(f"{LEDGER_URL}/auth/password-reset/verify", json={"login": login, "code": code}, timeout=10)
    if r.status_code != 200:
        flash(r.json().get("error", "Código inválido."), "error")
        return redirect(url_for("forgot_password_code_form"))

    data = r.json()
    # O reset_token fica só na sessão (cookie assinado, HttpOnly) -- nunca
    # aparece numa URL nem em campo de formulário visível/editável.
    session["pwreset_token"] = data["reset_token"]
    session["pwreset_role"] = data["role"]
    session.pop("pwreset_login", None)
    return redirect(url_for("forgot_password_new_form"))


@app.get("/esqueci-senha/nova-senha")
def forgot_password_new_form():
    if not session.get("pwreset_token"):
        return redirect(url_for("forgot_password_form"))
    return render_template("forgot_password_new.html")


@app.post("/esqueci-senha/nova-senha")
def forgot_password_new_submit():
    reset_token = session.get("pwreset_token")
    if not reset_token:
        return redirect(url_for("forgot_password_form"))

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if new_password != confirm_password:
        flash("As senhas não são iguais. Digite a mesma senha nos dois campos.", "error")
        return redirect(url_for("forgot_password_new_form"))

    r = requests.post(f"{LEDGER_URL}/auth/password-reset/confirm", json={
        "reset_token": reset_token,
        "new_password": new_password,
    }, timeout=10)
    if r.status_code != 200:
        flash(r.json().get("error", "Não foi possível trocar a senha."), "error")
        return redirect(url_for("forgot_password_new_form"))

    session.pop("pwreset_token", None)
    session.pop("pwreset_role", None)
    flash("Senha alterada com sucesso! Já pode entrar com ela.", "success")
    return redirect(url_for("login_form"))


@app.get("/")
def dashboard():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    statement = api_get(f"/customers/{customer['id']}/statement", params={"limit": 8})
    statement["entries"] = [enrich_entry(e) for e in statement["entries"]]
    return render_template(
        "dashboard.html",
        customer=customer,
        statement=statement,
        balance_display=format_currency(statement["balance_cents"]),
    )


@app.get("/extrato")
def extrato():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    offset = int(request.args.get("offset", 0))
    limit = 20
    statement = api_get(
        f"/customers/{customer['id']}/statement",
        params={"limit": limit, "offset": offset},
    )
    statement["entries"] = [enrich_entry(e) for e in statement["entries"]]
    return render_template(
        "extrato.html",
        customer=customer,
        statement=statement,
        balance_display=format_currency(statement["balance_cents"]),
        offset=offset,
        limit=limit,
        has_more=len(statement["entries"]) == limit,
    )


@app.get("/extrato/<transaction_id>")
def transaction_receipt(transaction_id):
    """Comprovante de uma movimentação: id da transação, quem enviou
    e quem recebeu (documento mascarado), status e metadados relevantes
    (chave PIX de destino num saque, provedor usado numa cobrança). Confere
    que o cliente logado participou da transação antes de mostrar
    qualquer coisa -- senão um cliente poderia ver o comprovante de
    qualquer id só adivinhando a URL."""
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    try:
        txn = api_get(f"/transactions/{transaction_id}")
    except requests.RequestException:
        abort(404)

    my_account_id = customer["account"]["id"]
    account_ids = {e["account_id"] for e in txn["entries"]}
    if my_account_id not in account_ids:
        abort(404)

    debit = next((e for e in txn["entries"] if e["amount_cents"] < 0), None)
    credit = next((e for e in txn["entries"] if e["amount_cents"] > 0), None)
    amount_cents = credit["amount_cents"] if credit else abs(debit["amount_cents"]) if debit else 0

    destination_note = None
    meta = txn.get("metadata") or {}
    if txn["rail"] == "withdrawal_pix":
        key_type = PIX_KEY_TYPES.get(meta.get("pix_key_type"), meta.get("pix_key_type"))
        if meta.get("pix_key"):
            destination_note = f"Chave PIX ({key_type}): {meta['pix_key']}"
    elif txn["rail"] in ("pix", "card", "crypto"):
        payer_info = meta.get("payer_info")
        if payer_info and debit:
            # O provedor confirmou quem pagou -- troca o rótulo genérico da
            # conta de sistema ("Mercado Pago (PIX)") pelo pagador de
            # verdade, com o banco dele como nota extra (quando disponível).
            debit["party"] = {
                "kind": "external",
                "name": payer_info.get("name") or debit["party"]["name"],
                "document": payer_info.get("document"),
            }
            if payer_info.get("bank"):
                destination_note = f"Banco do pagador: {payer_info['bank']}"
        provider = meta.get("provider")
        if provider and not destination_note:
            destination_note = f"Processado via {provider}"

    return render_template(
        "receipt.html",
        customer=customer,
        txn=txn,
        debit=debit,
        credit=credit,
        amount_display=format_currency(amount_cents),
        rail_label=RAIL_LABELS.get(txn["rail"], txn["rail"].title()),
        status_label=STATUS_LABELS.get(txn["status"], txn["status"].title()),
        destination_note=destination_note,
    )


@app.get("/transferir")
def transfer_form():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    return render_template("transfer.html", customer=customer, balance_display=format_currency(customer["account"]["balance_cents"]))


@app.post("/transferir")
def transfer_submit():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))

    recipient_login = request.form.get("recipient", "").strip()
    amount_reais = request.form.get("amount", "").replace(",", ".").strip()

    try:
        amount_cents = round(float(amount_reais) * 100)
    except ValueError:
        flash("Valor inválido.", "error")
        return redirect(url_for("transfer_form"))

    if amount_cents <= 0:
        flash("Informe um valor maior que zero.", "error")
        return redirect(url_for("transfer_form"))

    lookup = requests.get(f"{LEDGER_URL}/customers/lookup", params={
        "login": recipient_login,
        "exclude_customer_id": customer["id"],
    }, timeout=10)
    if lookup.status_code != 200:
        flash("Não encontramos nenhum cliente com esse documento/e-mail na Divisions Pay.", "error")
        return redirect(url_for("transfer_form"))
    recipient = lookup.json()

    r = requests.post(f"{LEDGER_URL}/transfers", json={
        "idempotency_key": str(uuid.uuid4()),
        "from_account_id": customer["account"]["id"],
        "to_account_id": recipient["account_id"],
        "amount_cents": amount_cents,
        "metadata": {"note": request.form.get("note", ""), "via": "customer-portal"},
    }, timeout=10)

    if r.status_code == 402:
        flash("Saldo insuficiente para essa transferência.", "error")
        return redirect(url_for("transfer_form"))
    if r.status_code not in (200, 201):
        flash("Não foi possível concluir a transferência. Tente novamente.", "error")
        return redirect(url_for("transfer_form"))

    flash(f"Transferência enviada para {recipient['name']}!", "success")
    return redirect(url_for("dashboard"))


@app.get("/sacar")
def withdraw_form():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    if customer.get("verification_status") != "approved":
        flash("Termine a verificação de identidade antes de sacar.", "error")
        return redirect(url_for("verification_form"))
    withdrawals = api_get(f"/customers/{customer['id']}/withdrawals")
    return render_template(
        "withdraw.html",
        customer=customer,
        balance_display=format_currency(customer["account"]["balance_cents"]),
        pix_key_types=PIX_KEY_TYPES,
        withdrawals=withdrawals,
    )


@app.post("/sacar")
def withdraw_submit():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    if customer.get("verification_status") != "approved":
        flash("Termine a verificação de identidade antes de sacar.", "error")
        return redirect(url_for("verification_form"))

    amount_reais = request.form.get("amount", "").replace(",", ".").strip()
    pix_key = request.form.get("pix_key", "").strip()
    pix_key_type = request.form.get("pix_key_type", "cpf")

    try:
        amount_cents = round(float(amount_reais) * 100)
    except ValueError:
        flash("Valor inválido.", "error")
        return redirect(url_for("withdraw_form"))

    if amount_cents <= 0:
        flash("Informe um valor maior que zero.", "error")
        return redirect(url_for("withdraw_form"))
    if not pix_key:
        flash("Informe a chave PIX de destino.", "error")
        return redirect(url_for("withdraw_form"))

    r = requests.post(f"{LEDGER_URL}/withdrawals", json={
        "idempotency_key": str(uuid.uuid4()),
        "customer_id": customer["id"],
        "amount_cents": amount_cents,
        "pix_key": pix_key,
        "pix_key_type": pix_key_type,
    }, timeout=10)

    if r.status_code == 402:
        flash("Saldo insuficiente para esse saque.", "error")
        return redirect(url_for("withdraw_form"))
    if r.status_code not in (200, 201):
        flash("Não foi possível registrar o saque. Tente novamente.", "error")
        return redirect(url_for("withdraw_form"))

    flash("Saque solicitado! O valor já saiu do seu saldo e será enviado pra sua chave PIX em breve.", "success")
    return redirect(url_for("withdraw_form"))


@app.get("/perfil")
def profile():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    return render_template("profile.html", customer=customer, balance_display=format_currency(customer["account"]["balance_cents"]))


# --- Verificação de identidade (KYC) ---------------------------------------
#
# Pessoa física (CPF): manda RG ou CNH, frente e verso. Empresa (CNPJ): manda
# o cartão CNPJ. Depois, os dois tipos precisam de uma selfie -- como ainda
# não existe app mobile, a selfie é tirada pelo NAVEGADOR DO CELULAR, através
# de um link de uso único (/verificacao-facial/<token>, rota pública, sem
# login -- o celular normalmente não está logado no portal). O status
# (verification_status) anda sozinho conforme os arquivos chegam; só a
# aprovação/reprovação final é decisão humana do admin.

VERIFICATION_STATUS_LABELS = {
    "documents_pending": "Envie seus documentos",
    "facial_pending": "Falta a verificação facial",
    "pending_review": "Documentos em análise",
    "approved": "Verificado",
    "rejected": "Reprovado",
}


@app.get("/verificacao")
def verification_form():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    verification = api_get(f"/customers/{customer['id']}/verification")
    doc_kinds_present = {d["kind"] for d in verification["documents"]}
    is_company = customer["account"]["kind"] == "merchant"
    facial_link = session.get("facial_link")
    return render_template(
        "verification.html",
        customer=customer,
        status=customer.get("verification_status", "documents_pending"),
        status_label=VERIFICATION_STATUS_LABELS.get(customer.get("verification_status"), "Envie seus documentos"),
        is_company=is_company,
        doc_kinds_present=doc_kinds_present,
        facial_link=facial_link,
        verification_note=customer.get("verification_note"),
    )


@app.post("/verificacao/documento")
def verification_upload_document():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))

    kind = request.form.get("kind", "")
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Selecione um arquivo antes de enviar.", "error")
        return redirect(url_for("verification_form"))

    data = {"kind": kind}
    if kind in ("id_front", "id_back"):
        data["document_type"] = request.form.get("document_type", "")

    r = requests.post(
        f"{LEDGER_URL}/customers/{customer['id']}/documents",
        data=data,
        files={"file": (upload.filename, upload.stream, upload.mimetype)},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        error_msg = (r.json() or {}).get("error", "Não foi possível enviar o arquivo.") if r.headers.get("content-type", "").startswith("application/json") else "Não foi possível enviar o arquivo."
        flash(error_msg, "error")
    else:
        flash("Documento enviado!", "success")
    return redirect(url_for("verification_form"))


@app.post("/verificacao/link-facial")
def verification_generate_facial_link():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))

    r = requests.post(f"{LEDGER_URL}/customers/{customer['id']}/facial-verification-link", timeout=10)
    if r.status_code != 201:
        flash("Não foi possível gerar o link agora. Tente de novo.", "error")
        return redirect(url_for("verification_form"))

    data = r.json()
    absolute_url = f"{CUSTOMER_PORTAL_PUBLIC_URL}{url_for('facial_verification_capture', token=data['token'])}" if CUSTOMER_PORTAL_PUBLIC_URL else url_for("facial_verification_capture", token=data["token"], _external=True)
    session["facial_link"] = {"url": absolute_url, "expires_at": data["expires_at"]}
    flash("Link gerado! Abra ele no seu celular pra tirar a selfie.", "success")
    return redirect(url_for("verification_form"))


@app.post("/verificacao/reenviar")
def verification_reset():
    """Depois de uma reprovação, o cliente pode mandar os documentos de
    novo -- isso só reabre o status (documents_pending); os arquivos
    antigos continuam salvos pro admin comparar, se quiser."""
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    requests.put(
        f"{LEDGER_URL}/customers/{customer['id']}/verification/review",
        json={"decision": "documents_pending", "note": None},
        timeout=10,
    )
    flash("Pode reenviar seus documentos.", "success")
    return redirect(url_for("verification_form"))


@app.get("/verificacao-facial/<token>")
def facial_verification_capture(token):
    """Página PÚBLICA (sem login) que abre no navegador do CELULAR --
    mobile-first, só um botão de tirar/escolher foto e enviar."""
    try:
        r = requests.get(f"{LEDGER_URL}/facial-verification/{token}", timeout=10)
    except requests.RequestException:
        return render_template("facial_capture.html", valid=False, error="Não conseguimos confirmar esse link agora. Tente de novo em instantes."), 503
    if r.status_code == 404:
        return render_template("facial_capture.html", valid=False, error="Link inválido."), 404
    if r.status_code == 410:
        return render_template("facial_capture.html", valid=False, error="Esse link expirou ou já foi usado. Gere um novo no computador."), 410
    if r.status_code != 200:
        return render_template("facial_capture.html", valid=False, error="Não foi possível abrir esse link agora."), 400

    data = r.json()
    return render_template("facial_capture.html", valid=True, customer_name=data["customer_name"], token=token)


@app.post("/verificacao-facial/<token>")
def facial_verification_submit(token):
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Tire ou escolha uma foto antes de enviar.", "error")
        return redirect(url_for("facial_verification_capture", token=token))

    r = requests.post(
        f"{LEDGER_URL}/facial-verification/{token}/selfie",
        files={"file": (upload.filename, upload.stream, upload.mimetype)},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        return render_template("facial_capture.html", valid=False, error="Não foi possível enviar a foto. Gere um novo link no computador e tente de novo."), 400

    return render_template("facial_capture.html", valid=True, done=True)


# --- Carteira cripto: saldo em USDT + opção de conversão automática -------

def _usdt_display(crypto_account):
    cents = crypto_account["balance_cents"] if crypto_account else 0
    return f"{cents / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


@app.get("/cripto")
def crypto_wallet():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    return render_template(
        "crypto_wallet.html",
        customer=customer,
        balance_display=format_currency(customer["account"]["balance_cents"]),
        usdt_display=_usdt_display(customer.get("crypto_account")),
        auto_convert=bool(customer.get("auto_convert_to_crypto")),
    )


@app.post("/cripto/auto-convert")
def crypto_wallet_toggle():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    enabled = request.form.get("enabled") == "1"
    r = requests.put(
        f"{LEDGER_URL}/customers/{customer['id']}/auto-convert-crypto",
        json={"enabled": enabled}, timeout=10,
    )
    if r.status_code != 200:
        flash("Não foi possível salvar essa preferência agora. Tente de novo.", "error")
    else:
        flash("Conversão automática PIX/cartão → USDT ativada." if enabled else "Conversão automática desativada.", "success")
    return redirect(url_for("crypto_wallet"))
