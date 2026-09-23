"""admin-panel — painel web simples pra operar o gateway sem precisar de curl.

Protegido por HTTP Basic Auth (ADMIN_USER / ADMIN_PASSWORD via env). Fala com
core-ledger e pix-service pelos endereços internos — nunca expõe as APIs de
pagamento diretamente pro navegador, só este painel.
"""
import os
from functools import wraps

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, Response

app = Flask(__name__)
app.secret_key = os.environ.get("ADMIN_PANEL_SECRET", "troque-isto-em-producao")

LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")
PIX_URL = os.environ.get("PIX_URL", "http://pix-service:8002")
CARD_URL = os.environ.get("CARD_URL", "http://card-service:8003")
CRYPTO_URL = os.environ.get("CRYPTO_URL", "http://crypto-service:8004")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASSWORD:
            return Response(
                "Autenticação necessária", 401,
                {"WWW-Authenticate": 'Basic realm="Admin do Gateway"'},
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


@app.get("/health")
def health():
    # sem auth — pra checagem de infra (EasyPanel, load balancer, etc.)
    return {"status": "ok"}


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
    return render_template("dashboard.html", customers=customers, providers=providers)


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
    }
    for key, label in labels.items():
        account_id = ids.get(key)
        try:
            account = api_get(LEDGER_URL, f"/accounts/{account_id}") if account_id else None
        except requests.RequestException:
            account = None
        accounts.append({"label": label, "id": account_id, "account": account})

    return render_template("system_accounts.html", accounts=accounts)
