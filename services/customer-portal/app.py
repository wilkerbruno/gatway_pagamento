"""customer-portal — Divisions Pay: portal do cliente.

Cada cliente loga com o próprio documento/e-mail + senha (definida pelo
admin ao criar a conta, ou trocada depois via /customers/<id>/password no
core-ledger) e só vê os dados da própria carteira — sessão isolada por
cliente, nunca a lista de outros clientes.
"""
import os

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.environ.get("CUSTOMER_PORTAL_SECRET", "troque-isto-em-producao")

LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")


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
    customer = current_customer()
    if not customer:
        return redirect(url_for("login_form"))
    statement = api_get(f"/customers/{customer['id']}/statement")
    return render_template("dashboard.html", customer=customer, statement=statement)
