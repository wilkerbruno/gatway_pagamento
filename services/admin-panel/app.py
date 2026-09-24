"""admin-panel — painel web simples pra operar o gateway sem precisar de curl.

Login: não existe mais Basic Auth (aquele popup do navegador) nem senha
fixa em variável de ambiente. Quem entra é uma AdminUser de verdade (com
e-mail, cadastrada no core-ledger), autenticada numa tela de login única
compartilhada com os clientes -- ela mora no customer-portal. Quando o
core-ledger confirma que é um admin, o customer-portal gera um "bilhete" de
handoff assinado (HMAC, válido por 60s) e redireciona o navegador pra cá,
em /sso, que troca esse bilhete por uma sessão aqui. Sem sessão válida,
qualquer rota protegida manda o navegador de volta pro login único.

Fala com core-ledger e pix-service pelos endereços internos — nunca expõe
as APIs de pagamento diretamente pro navegador, só este painel.
"""
import hashlib
import hmac
import os
import secrets
import time
from functools import wraps

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, abort

app = Flask(__name__)
app.secret_key = os.environ.get("ADMIN_PANEL_SECRET", "troque-isto-em-producao")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS_COOKIES", "true").lower() != "false",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 2,  # 2h de sessão parada = desloga
)

LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")
PIX_URL = os.environ.get("PIX_URL", "http://pix-service:8002")
CARD_URL = os.environ.get("CARD_URL", "http://card-service:8003")
CRYPTO_URL = os.environ.get("CRYPTO_URL", "http://crypto-service:8004")

# URL pública do customer-portal (onde mora a tela de login única) e o
# segredo compartilhado com ele pra validar o bilhete de handoff em /sso.
CUSTOMER_PORTAL_PUBLIC_URL = os.environ.get("CUSTOMER_PORTAL_PUBLIC_URL", "").rstrip("/")
SSO_SIGNING_SECRET = os.environ.get("SSO_SIGNING_SECRET", "troque-isto-em-producao")


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            if CUSTOMER_PORTAL_PUBLIC_URL:
                return redirect(f"{CUSTOMER_PORTAL_PUBLIC_URL}/login")
            return Response(
                "Não autenticado, e o login único (env CUSTOMER_PORTAL_PUBLIC_URL) "
                "ainda não foi configurado nesse servidor -- fale com quem administra "
                "a infraestrutura.",
                401,
            )
        return f(*args, **kwargs)
    return wrapped


def api_get(base, path, **kwargs):
    r = requests.get(f"{base}{path}", timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()


def api_post(base, path, json=None):
    r = requests.post(f"{base}{path}", json=json, timeout=10)
    return r


def api_put(base, path, json=None):
    r = requests.put(f"{base}{path}", json=json, timeout=10)
    return r


def error_message(resp):
    """Extrai uma mensagem curta e legível de uma resposta de erro — em vez
    de despejar o corpo cru (que pode ser uma página HTML de erro 500) na
    tela do admin."""
    try:
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            return str(data["error"])
    except ValueError:
        pass
    text = resp.text.strip()
    if text.startswith("<"):
        return f"o servidor respondeu com um erro inesperado (HTTP {resp.status_code})."
    return text[:300]


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
    return redirect(request.referrer or url_for("dashboard")), 400


@app.get("/health")
def health():
    # sem auth — pra checagem de infra (EasyPanel, load balancer, etc.)
    return {"status": "ok"}


@app.get("/sso")
def sso_login():
    """Troca o bilhete de handoff assinado pelo customer-portal (depois que
    o admin já provou a senha por lá, na tela de login única) por uma
    sessão aqui. O token vale 60s -- é só o tempo do redirect acontecer,
    não dá pra reutilizar depois disso, e a assinatura HMAC garante que só
    quem tem o mesmo SSO_SIGNING_SECRET (ou seja, o customer-portal) pôde
    ter gerado essa URL."""
    admin_id = request.args.get("admin_id", "")
    exp = request.args.get("exp", "")
    sig = request.args.get("sig", "")

    if not admin_id or not exp or not sig:
        abort(400, description="link de acesso inválido.")

    manifest = f"{admin_id}:{exp}"
    expected_sig = hmac.new(SSO_SIGNING_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        abort(400, description="link de acesso inválido ou adulterado.")

    try:
        if int(exp) < time.time():
            abort(400, description="link de acesso expirado -- faça login de novo.")
    except ValueError:
        abort(400, description="link de acesso inválido.")

    session.clear()
    session["admin_id"] = admin_id
    session.permanent = True
    return redirect(url_for("dashboard"))


@app.post("/logout")
def logout():
    session.clear()
    if CUSTOMER_PORTAL_PUBLIC_URL:
        return redirect(f"{CUSTOMER_PORTAL_PUBLIC_URL}/login")
    return redirect(url_for("dashboard"))


@app.get("/")
@require_auth
def dashboard():
    try:
        customers = api_get(LEDGER_URL, "/customers?limit=100")
    except requests.RequestException as e:
        flash(f"Não consegui falar com o core-ledger: {e}", "error")
        customers = []
    try:
        providers = {p["rail"]: p["provider"] for p in api_get(LEDGER_URL, "/admin/settings/providers")}
    except requests.RequestException:
        providers = {}
    try:
        pending_withdrawals = len(api_get(LEDGER_URL, "/withdrawals?status=pending"))
    except requests.RequestException:
        pending_withdrawals = 0
    return render_template("dashboard.html", customers=customers, providers=providers, pending_withdrawals=pending_withdrawals)


@app.get("/customers/new")
@require_auth
def new_customer_form():
    return render_template("new_customer.html")


@app.post("/customers/new")
@require_auth
def new_customer_submit():
    resp = api_post(LEDGER_URL, "/customers", json={
        "name": request.form["name"],
        "document": request.form.get("document") or None,
        "email": request.form.get("email") or None,
        "kind": request.form.get("kind", "customer"),
        "password": request.form.get("password") or None,
    })
    if resp.status_code >= 400:
        flash(f"Erro ao criar cliente: {error_message(resp)}", "error")
        return redirect(url_for("new_customer_form"))
    flash("Cliente criado com sucesso.", "success")
    return redirect(url_for("dashboard"))


@app.get("/customers/<customer_id>")
@require_auth
def customer_detail(customer_id):
    customer = api_get(LEDGER_URL, f"/customers/{customer_id}")
    statement = api_get(LEDGER_URL, f"/customers/{customer_id}/statement")
    return render_template("customer_detail.html", customer=customer, statement=statement)


@app.get("/transfer")
@require_auth
def transfer_form():
    customers = api_get(LEDGER_URL, "/customers?limit=200")
    return render_template("transfer.html", customers=customers)


@app.post("/transfer")
@require_auth
def transfer_submit():
    import uuid
    resp = api_post(LEDGER_URL, "/transfers", json={
        "idempotency_key": f"admin-panel:{uuid.uuid4()}",
        "from_account_id": request.form["from_account_id"],
        "to_account_id": request.form["to_account_id"],
        "amount_cents": int(round(float(request.form["amount_brl"].replace(",", ".")) * 100)),
    })
    if resp.status_code >= 400:
        flash(f"Transferência recusada: {error_message(resp)}", "error")
    else:
        flash("Transferência concluída.", "success")
    return redirect(url_for("dashboard"))


@app.get("/settings/platform")
@require_auth
def platform_settings_form():
    customers = api_get(LEDGER_URL, "/customers?limit=200")
    settings = api_get(LEDGER_URL, "/admin/settings/platform")
    return render_template("platform_settings.html", customers=customers, settings=settings)


@app.post("/settings/platform")
@require_auth
def platform_settings_submit():
    fee_bps = int(round(float(request.form["fee_percent"].replace(",", ".")) * 100))
    resp = api_put(LEDGER_URL, "/admin/settings/platform", json={
        "platform_account_id": request.form["platform_account_id"],
        "fee_bps": fee_bps,
    })
    if resp.status_code >= 400:
        flash(f"Erro ao salvar: {error_message(resp)}", "error")
    else:
        flash("Taxa da plataforma atualizada.", "success")
    return redirect(url_for("platform_settings_form"))


@app.get("/crypto/new")
@require_auth
def crypto_new_form():
    customers = api_get(LEDGER_URL, "/customers?limit=200")
    return render_template("crypto_new.html", customers=customers)


@app.post("/crypto/new")
@require_auth
def crypto_new_submit():
    amount_cents = int(round(float(request.form["amount_brl"].replace(",", ".")) * 100))
    resp = api_post(CRYPTO_URL, "/invoices", json={
        "amount_cents": amount_cents,
        "asset": request.form.get("asset", "USDT"),
        "merchant_account": request.form["merchant_account_id"],
    })
    if resp.status_code >= 400:
        flash(f"Erro ao gerar cobrança cripto: {error_message(resp)}", "error")
        return redirect(url_for("crypto_new_form"))
    invoice = resp.json()
    return render_template("crypto_invoice.html", invoice=invoice)


@app.post("/crypto/<transaction_id>/simulate")
@require_auth
def crypto_simulate(transaction_id):
    resp = api_post(CRYPTO_URL, "/_sandbox/simulate-confirmation", json={"transaction_id": transaction_id})
    if resp.status_code >= 400:
        flash(f"Erro ao simular confirmação: {error_message(resp)}", "error")
    else:
        flash("Confirmação cripto simulada com sucesso (modo sandbox).", "success")
    return redirect(url_for("dashboard"))


@app.get("/settings/providers")
@require_auth
def providers_form():
    providers = api_get(LEDGER_URL, "/admin/settings/providers")
    return render_template("providers.html", providers=providers)


@app.post("/settings/providers/<rail>")
@require_auth
def providers_submit(rail):
    resp = api_put(LEDGER_URL, f"/admin/settings/providers/{rail}", json={
        "provider": request.form["provider"],
    })
    if resp.status_code >= 400:
        flash(f"Erro ao trocar provedor: {error_message(resp)}", "error")
    else:
        flash(f"Provedor de {rail} atualizado.", "success")
    return redirect(url_for("providers_form"))


@app.get("/pix/new")
@require_auth
def pix_new_form():
    customers = api_get(LEDGER_URL, "/customers?limit=200")
    return render_template("pix_new.html", customers=customers)


@app.post("/pix/new")
@require_auth
def pix_new_submit():
    amount_cents = int(round(float(request.form["amount_brl"].replace(",", ".")) * 100))
    resp = api_post(PIX_URL, "/charges", json={
        "amount_cents": amount_cents,
        "merchant_account": request.form["merchant_account_id"],
    })
    if resp.status_code >= 400:
        flash(f"Erro ao gerar cobrança PIX: {error_message(resp)}", "error")
        return redirect(url_for("pix_new_form"))
    charge = resp.json()
    return render_template("pix_charge.html", charge=charge)


@app.post("/pix/<transaction_id>/simulate")
@require_auth
def pix_simulate(transaction_id):
    resp = api_post(PIX_URL, "/_sandbox/simulate-payment", json={"transaction_id": transaction_id})
    if resp.status_code >= 400:
        flash(f"Erro ao simular pagamento: {error_message(resp)}", "error")
    else:
        flash("Pagamento PIX simulado com sucesso (modo sandbox).", "success")
    return redirect(url_for("dashboard"))


@app.post("/pix/<transaction_id>/check-status")
@require_auth
def pix_check_status(transaction_id):
    """Reconsulta o status direto na API do provedor (Mercado Pago/Pagar.me),
    sem depender de webhook -- útil quando o cliente já pagou de verdade mas
    o saldo não subiu (webhook atrasado, mal configurado, ou que nunca
    chegou)."""
    resp = api_post(PIX_URL, f"/charges/{transaction_id}/check-status")
    if resp.status_code >= 400:
        flash(f"Erro ao verificar pagamento: {error_message(resp)}", "error")
        return redirect(url_for("dashboard"))

    result = resp.json()
    status = result.get("status")
    if status == "confirmed":
        flash("Pagamento confirmado! O saldo já foi atualizado.", "success")
    elif status == "failed":
        flash("O provedor informou que esse pagamento falhou/foi cancelado.", "error")
    else:
        flash("Ainda não consta como pago no provedor. Se você já pagou, aguarde um pouco -- o PIX pode levar alguns segundos para compensar.", "info")
    return redirect(url_for("dashboard"))


@app.get("/pix/check")
@require_auth
def pix_check_form():
    """Atalho pra colar um transaction_id de uma cobrança PIX antiga (gerada
    numa sessão anterior, sem precisar navegar até ela) e verificar o status
    direto na API do provedor."""
    return render_template("pix_check.html")


@app.get("/settings/system-accounts")
@require_auth
def system_accounts():
    """Contas internas de 'a receber do PSP' que pix/card/crypto-service
    usam antes de creditar o lojista/cliente. São criadas sozinhas pelo
    core-ledger na primeira vez que qualquer serviço precisa delas — essa
    tela é só pra conferir que existem e ver o saldo pendente de cada uma,
    sem precisar de curl."""
    try:
        ids = api_get(LEDGER_URL, "/admin/settings/system-accounts")
    except requests.RequestException as e:
        flash(f"Não consegui falar com o core-ledger: {e}", "error")
        return render_template("system_accounts.html", accounts=[])

    accounts = []
    labels = {
        "pix_pending_account_id": "Pix (a receber do provedor)",
        "card_receivable_account_id": "Cartão (a receber do provedor)",
        "crypto_pending_account_id": "Cripto (a receber on-chain)",
        "payouts_pending_account_id": "Saques (reservado pra sair)",
    }
    for key, label in labels.items():
        account_id = ids.get(key)
        try:
            account = api_get(LEDGER_URL, f"/accounts/{account_id}") if account_id else None
        except requests.RequestException:
            account = None
        accounts.append({"label": label, "id": account_id, "account": account})

    return render_template("system_accounts.html", accounts=accounts)


@app.get("/saques")
@require_auth
def withdrawals_list():
    """Fila de saques pedidos pelos clientes no portal deles. O admin manda
    o PIX de verdade pela conta real da empresa (Mercado Pago produção) e
    depois marca aqui como pago -- ou como falhou, se a chave estiver errada
    (o valor volta pro cliente automaticamente)."""
    status = request.args.get("status", "pending")
    try:
        params = {} if status == "all" else {"status": status}
        withdrawals = api_get(LEDGER_URL, "/withdrawals", params=params)
    except requests.RequestException as e:
        flash(f"Não consegui falar com o core-ledger: {e}", "error")
        withdrawals = []
    return render_template("withdrawals.html", withdrawals=withdrawals, status=status)


@app.post("/saques/<withdrawal_id>/pagar")
@require_auth
def withdrawal_mark_paid(withdrawal_id):
    note = request.form.get("note", "")
    resp = api_post(LEDGER_URL, f"/withdrawals/{withdrawal_id}/mark-paid", json={"note": note})
    if resp.status_code >= 400:
        flash(f"Erro ao marcar saque como pago: {error_message(resp)}", "error")
    else:
        flash("Saque marcado como pago.", "success")
    return redirect(url_for("withdrawals_list"))


@app.post("/saques/<withdrawal_id>/falhar")
@require_auth
def withdrawal_mark_failed(withdrawal_id):
    note = request.form.get("note", "")
    resp = api_post(LEDGER_URL, f"/withdrawals/{withdrawal_id}/mark-failed", json={"note": note})
    if resp.status_code >= 400:
        flash(f"Erro ao marcar saque como falho: {error_message(resp)}", "error")
    else:
        flash("Saque marcado como falho -- o valor voltou pro saldo do cliente.", "success")
    return redirect(url_for("withdrawals_list"))
