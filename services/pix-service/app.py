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
# Variável de ambiente é só um jeito avançado de sobrescrever; por padrão
# a conta de sistema é criada e resolvida sozinha no core-ledger (veja
# system_pix_pending_account() abaixo), sem precisar configurar nada.
SYSTEM_PIX_PENDING_ACCOUNT_OVERRIDE = os.environ.get("SYSTEM_PIX_PENDING_ACCOUNT")
MERCHANT_ACCOUNT = os.environ.get("DEFAULT_MERCHANT_ACCOUNT")

_settings_cache = {"provider": None, "fetched_at": 0}
_system_account_cache = {"pix_pending_account_id": None, "fetched_at": 0}
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


def system_pix_pending_account() -> str:
    if SYSTEM_PIX_PENDING_ACCOUNT_OVERRIDE:
        return SYSTEM_PIX_PENDING_ACCOUNT_OVERRIDE
    now = time.time()
    if _system_account_cache["pix_pending_account_id"] is None or now - _system_account_cache["fetched_at"] > SETTINGS_TTL_SECONDS:
        resp = requests.get(f"{LEDGER_URL}/admin/settings/system-accounts", timeout=5)
        resp.raise_for_status()
        _system_account_cache["pix_pending_account_id"] = resp.json()["pix_pending_account_id"]
        _system_account_cache["fetched_at"] = now
    return _system_account_cache["pix_pending_account_id"]


def _save_payer_info(transaction_id: str, payer_info: dict | None) -> None:
    """Guarda os dados de quem pagou (nome/documento/banco, quando o
    provedor os devolve) no metadata da transação, pro comprovante do
    cliente conseguir mostrar. Melhor esforço: se isso falhar, não derruba
    a confirmação do pagamento -- o dinheiro já entrou, o comprovante só
    fica sem esse detalhe extra."""
    if not payer_info:
        return
    try:
        requests.patch(
            f"{LEDGER_URL}/transactions/{transaction_id}/metadata",
            json={"payer_info": payer_info},
            timeout=10,
        )
    except requests.RequestException:
        app.logger.warning("não foi possível salvar payer_info da transação %s", transaction_id)


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
                {"account_id": system_pix_pending_account(), "amount_cents": -amount_cents},
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
    result = provider.parse_webhook(payload, dict(request.headers), request.args)

    if not result.get("verified", False) and provider_name != "sandbox":
        # Webhook cuja origem não conseguimos autenticar (assinatura ausente/
        # inválida, ou segredo não configurado). Nunca libera dinheiro com
        # base nisso -- só ignora, silenciosamente do ponto de vista de quem
        # tentou forjar a chamada, mas registrado nos logs do serviço.
        app.logger.warning("webhook %s recebido sem verificação de assinatura -- ignorado", provider_name)
        return jsonify({"ignored": True, "reason": "unverified"}), 200

    if result["status"] != "confirmed":
        return jsonify({"ignored": True, "status": result["status"]}), 200

    # busca a transação pelo external_ref para achar o id interno
    # (endpoint auxiliar simples; em produção, indexe por external_ref no ledger)
    txn_resp = requests.get(f"{LEDGER_URL}/transactions/by-external-ref/{result['provider_ref']}", timeout=10)
    if txn_resp.status_code != 200:
        return jsonify({"error": "transação não encontrada para este provider_ref"}), 404

    txn = txn_resp.json()
    _save_payer_info(txn['id'], result.get("payer_info"))
    settle = requests.post(f"{LEDGER_URL}/transactions/{txn['id']}/settle", timeout=10)
    settle.raise_for_status()
    return jsonify(settle.json())


@app.post("/charges/<transaction_id>/check-status")
def check_charge_status(transaction_id):
    """Reconsulta ativa: vai direto na API do provedor perguntar o status
    real, sem depender de webhook nenhum ter chegado. Existe pra destravar o
    caso "o cliente pagou de verdade mas o saldo não atualizou" (webhook
    atrasado, mal configurado, ou que nunca chegou) -- o botão "Verificar
    pagamento agora" do admin-panel chama isso."""
    txn_resp = requests.get(f"{LEDGER_URL}/transactions/{transaction_id}", timeout=10)
    if txn_resp.status_code != 200:
        return jsonify({"error": "transação não encontrada"}), 404
    txn = txn_resp.json()

    if txn["status"] == "confirmed":
        return jsonify({"status": "confirmed", "already_settled": True, "transaction": txn})
    if txn["status"] != "pending":
        return jsonify({"status": txn["status"], "transaction": txn})

    provider_name = (txn.get("metadata") or {}).get("provider") or active_provider_name()
    provider_ref = txn.get("external_ref")
    if not provider_ref:
        return jsonify({"error": "transação sem referência de provedor (external_ref)"}), 400

    provider = get_provider(provider_name)
    try:
        result = provider.check_status(provider_ref)
    except NotImplementedError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": f"falha ao consultar o provedor {provider_name}: {exc}"}), 502

    if result["status"] == "confirmed":
        _save_payer_info(transaction_id, result.get("payer_info"))
        settle = requests.post(f"{LEDGER_URL}/transactions/{transaction_id}/settle", timeout=10)
        settle.raise_for_status()
        return jsonify({"status": "confirmed", "transaction": settle.json()})
    if result["status"] == "failed":
        fail = requests.post(f"{LEDGER_URL}/transactions/{transaction_id}/fail", timeout=10)
        fail.raise_for_status()
        return jsonify({"status": "failed", "transaction": fail.json()})

    return jsonify({"status": "pending", "transaction": txn})


@app.post("/_sandbox/simulate-payment")
def simulate_payment():
    """Atalho só para dev: simula o provedor confirmando o pagamento imediatamente."""
    data = request.get_json(force=True)
    resp = requests.post(
        f"{LEDGER_URL}/transactions/{data['transaction_id']}/settle", timeout=10
    )
    resp.raise_for_status()
    return jsonify(resp.json())
