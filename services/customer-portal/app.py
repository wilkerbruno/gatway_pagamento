"""customer-portal — Divisions Pay: painel do cliente, estilo banco digital.

Cada cliente loga com o próprio documento/e-mail + senha (definida pelo
admin ao criar a conta, ou trocada depois via /customers/<id>/password no
core-ledger) e só vê os dados da própria carteira — sessão isolada por
cliente, nunca a lista de outros clientes. O cliente também pode transferir
para outro cliente da plataforma diretamente por aqui (tipo PicPay), sem
precisar passar pelo admin.
"""
import os
import uuid

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.environ.get("CUSTOMER_PORTAL_SECRET", "troque-isto-em-producao")

LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")

RAIL_LABELS = {
    "pix": "Pix",
    "card": "Cartão",
    "crypto": "Cripto",
    "internal_transfer": "Transferência",
}


def api_get(path, **kwargs):
    r = requests.get(f"{LEDGER_URL}{path}", timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()


def current_customer():
    customer_id = session.get("customer_id")
    if not customer_id:
        return None
    try:
        return api_get(f"/customers/{customer_id}")
    except requests.RequestException:
        return None


def require_login():
    customer = current_customer()
    if not customer:
        return None
    return customer


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
    r = requests.post(f"{LEDGER_URL}/customers/authenticate", json={
        "login": request.form["login"],
        "password": request.form["password"],
    }, timeout=10)
    if r.status_code != 200:
        flash("Documento/e-mail ou senha incorretos.", "error")
        return redirect(url_for("login_form"))
    customer = r.json()
    session["customer_id"] = customer["id"]
    return redirect(url_for("dashboard"))


@app.post("/logout")
def logout():
    session.clear()
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


@app.get("/perfil")
def profile():
    customer = require_login()
    if not customer:
        return redirect(url_for("login_form"))
    return render_template("profile.html", customer=customer, balance_display=format_currency(customer["account"]["balance_cents"]))
