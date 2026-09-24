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


def enrich_entry(entry):
    """Anota cada lançamento do extrato com o que o painel precisa pra
    desenhar (rótulo em português, se é entrada ou saída, ícone)."""
    amount = entry["amount_cents"]
    rail = entry.get("rail", "internal_transfer")
    entry["is_credit"] = amount > 0
    entry["rail_label"] = RAIL_LABELS.get(rail, rail.title())
    entry["amount_display"] = format_currency(abs(amount))
    return entry


def format_currency(cents):
    return f"{cents / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


app.jinja_env.filters["brl"] = format_currency


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
