"""pix-service — adaptador PIX, com provedor plugável.

O admin escolhe o provedor ativo (mercadopago | pagarme | direct) via
core-ledger: PUT /admin/settings/providers/pix — sem precisar de redeploy.
Este serviço consulta essa configuração (com um cache curto) antes de cada
operação, então a troca feita pelo admin vale em segundos.
"""
import os
import time
import uuid

import requests
from flask import Flask, request, jsonify

from providers import get_provider

app = Flask(__name__)
LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")
SYSTEM_PIX_PENDING_ACCOUNT = os.environ.get("SYSTEM_PIX_PENDING_ACCOUNT")
MERCHANT_ACCOUNT = os.environ.get("DEFAULT_MERCHANT_ACCOUNT")

_settings_cache = {"provider": None, "fetched_at": 0}
SETTINGS_TTL_SECONDS = 10


def active_provider_name() -> str:
    now = time.time()
    if _settings_cache["provider"] is None or now - _settings_cache["fetched_at"] > SETTINGS_TTL_SECONDS:
        resp = requests.get(f"{LEDGER_URL}/admin/settings/providers", timeout=5)
        resp.raise_for_status()
        settings = {row["rail"]: row["provider"] for row in resp.json()}
        _settings_cache["provider"] = settings.get("pix", "sandbox")
        _settings_cache["fetched_at"] = now
    return _settings_cache["provider"]


@app.get("/health")
def health():
    return {"status": "ok", "active_provider": active_provider_name()}


@app.post("/charges")
def create_charge():
    data = request.get_json(force=True)
    amount_cents = data["amount_cents"]
    merchant_account = data.get("merchant_account", MERCHANT_ACCOUNT)
    external_reference = f"PIX{uuid.uuid4().hex[:20].upper()}"

    provider_name = data.get("provider_override") or active_provider_name()
    provider = get_provider(provider_name)
    charge = provider.create_charge(amount_cents, external_reference, data.get("payer_email"))

    resp = requests.post(
        f"{LEDGER_URL}/transactions",
        json={
            "idempotency_key": f"pix:{provider_name}:{charge['provider_ref']}",
            "rail": "pix",
            "external_ref": charge["provider_ref"],
            "entries": [
                {"account_id": SYSTEM_PIX_PENDING_ACCOUNT, "amount_cents": -amount_cents},
                {"account_id": merchant_account, "amount_cents": amount_cents},
            ],
            "metadata": {"provider": provider_name, "amount_cents": amount_cents},
        },
        timeout=10,
    )
    resp.raise_for_status()
    txn = resp.json()

    return jsonify({
        "provider": provider_name,
        "provider_ref": charge["provider_ref"],
        "transaction_id": txn["id"],
        "qr_code_payload": charge["qr_code_payload"],
        "qr_code_base64": charge.get("qr_code_base64"),
        "status": "pending",
    }), 201


@app.post("/webhook/<provider_name>")
def provider_webhook(provider_name):
    """Cada provedor tem sua própria URL de webhook (ex: /webhook/mercadopago,
    /webhook/pagarme), configurada no painel de cada um deles."""
    provider = get_provider(provider_name)
    payload = request.get_json(force=True)
    result = provider.parse_webhook(payload, dict(request.headers))

    if result["status"] != "confirmed":
        return jsonify({"ignored": True, "status": result["status"]}), 200

    # busca a transação pelo external_ref para achar o id interno
    # (endpoint auxiliar simples; em produção, indexe por external_ref no ledger)
    txn_resp = requests.get(f"{LEDGER_URL}/transactions/by-external-ref/{result['provider_ref']}", timeout=10)
    if txn_resp.status_code != 200:
        return jsonify({"error": "transação não encontrada para este provider_ref"}), 404

    txn = txn_resp.json()
    settle = requests.post(f"{LEDGER_URL}/transactions/{txn['id']}/settle", timeout=10)
    settle.raise_for_status()
    return jsonify(settle.json())


@app.post("/_sandbox/simulate-payment")
def simulate_payment():
    """Atalho só para dev: simula o provedor confirmando o pagamento imediatamente."""
    data = request.get_json(force=True)
    resp = requests.post(
        f"{LEDGER_URL}/transactions/{data['transaction_id']}/settle", timeout=10
    )
    resp.raise_for_status()
    return jsonify(resp.json())
