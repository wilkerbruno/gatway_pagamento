"""card-service — adaptador de cartão (crédito/débito), com provedor plugável.

Nunca aceita PAN/CVV em claro — só token, gerado no front-end pelo SDK do
provedor ativo. O admin escolhe o provedor via core-ledger:
PUT /admin/settings/providers/card (mercadopago | pagarme | direct).
"""
import os
import time
import uuid

import requests
from flask import Flask, request, jsonify

from providers import get_provider

app = Flask(__name__)
LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")
SYSTEM_CARD_RECEIVABLE_ACCOUNT = os.environ.get("SYSTEM_CARD_RECEIVABLE_ACCOUNT")
MERCHANT_ACCOUNT = os.environ.get("DEFAULT_MERCHANT_ACCOUNT")

_settings_cache = {"provider": None, "fetched_at": 0}
SETTINGS_TTL_SECONDS = 10


def active_provider_name() -> str:
    now = time.time()
    if _settings_cache["provider"] is None or now - _settings_cache["fetched_at"] > SETTINGS_TTL_SECONDS:
        resp = requests.get(f"{LEDGER_URL}/admin/settings/providers", timeout=5)
        resp.raise_for_status()
        settings = {row["rail"]: row["provider"] for row in resp.json()}
        _settings_cache["provider"] = settings.get("card", "mercadopago")
        _settings_cache["fetched_at"] = now
    return _settings_cache["provider"]


@app.get("/health")
def health():
    return {"status": "ok", "active_provider": active_provider_name()}


@app.post("/charges")
def create_charge():
    data = request.get_json(force=True)
    if "card_number" in data or "pan" in data or "cvv" in data:
        return jsonify({
            "error": "este serviço não aceita dados de cartão em claro. "
                     "Tokenize no front-end via SDK do provedor ativo."
        }), 400

    token = data["card_token"]
    amount_cents = data["amount_cents"]
    installments = data.get("installments", 1)
    merchant_account = data.get("merchant_account", MERCHANT_ACCOUNT)
    external_reference = f"CARD{uuid.uuid4().hex[:20].upper()}"

    provider_name = data.get("provider_override") or active_provider_name()
    provider = get_provider(provider_name)
    result = provider.charge(token, amount_cents, installments, external_reference)

    if not result["approved"]:
        return jsonify({"status": "declined", "provider": provider_name, "raw_status": result["raw_status"]}), 402

    idem_key = f"card:{provider_name}:{result['provider_ref']}"
    resp = requests.post(
        f"{LEDGER_URL}/transactions",
        json={
            "idempotency_key": idem_key,
            "rail": "card",
            "external_ref": result["provider_ref"],
            "entries": [
                {"account_id": SYSTEM_CARD_RECEIVABLE_ACCOUNT, "amount_cents": -amount_cents},
                {"account_id": merchant_account, "amount_cents": amount_cents},
            ],
            "metadata": {"provider": provider_name, "installments": installments},
        },
        timeout=10,
    )
    resp.raise_for_status()
    txn = resp.json()

    settle = requests.post(f"{LEDGER_URL}/transactions/{txn['id']}/settle", timeout=10)
    settle.raise_for_status()

    return jsonify({
        "provider": provider_name,
        "provider_ref": result["provider_ref"],
        "transaction_id": txn["id"],
        "status": "approved",
    }), 201
