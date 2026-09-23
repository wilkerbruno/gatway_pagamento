"""crypto-service — recebimento de criptomoedas SEM provedor terceiro
(sem Coinbase Commerce, BitPay, etc.). O serviço:

1. Deriva um endereço novo por invoice (wallet.py, HD/BIP-44).
2. Observa a blockchain diretamente via RPC público (ou seu próprio node)
   para detectar o depósito.
3. Ao confirmar, liquida a transação correspondente no core-ledger.

Isso é "direto" no sentido de não depender de um processador de pagamentos
cripto — mas ainda assim consulta um RPC público (Polygon, por padrão), que é
só uma fonte de dados da blockchain, não um intermediário financeiro.

⚠️ Regulatório: enquanto o serviço só gera endereços e repassa o valor ao
lojista (sem custodiar fundos de terceiros por conta própria além do tempo
de liquidação), o enquadramento como Prestadora de Serviços de Ativos
Virtuais é discutível; se o modelo evoluir para custódia de fato, revise
docs/COMPLIANCE.md. Fale com um advogado antes de operar com volume real.

⚠️ Conversão BRL↔cripto: `ASSET_BRL_RATE_*` abaixo são placeholders fixos.
Em produção, troque por uma cotação em tempo real (ex: API de um exchange)
com uma margem de segurança para variação de preço durante a janela de
pagamento.
"""
import os
import time
import uuid
from decimal import Decimal

import requests
from flask import Flask, request, jsonify
from web3 import Web3

from models import db, Invoice, Counter
from wallet import derive_address

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "CRYPTO_DATABASE_URL", "sqlite:////app/data/crypto.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

LEDGER_URL = os.environ.get("LEDGER_URL", "http://core-ledger:8001")
SYSTEM_CRYPTO_PENDING_ACCOUNT = os.environ.get("SYSTEM_CRYPTO_PENDING_ACCOUNT")
MERCHANT_ACCOUNT = os.environ.get("DEFAULT_MERCHANT_ACCOUNT")

RPC_URL = os.environ.get("WEB3_RPC_URL", "https://polygon-rpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Placeholder de cotação — troque por uma fonte real antes de produção.
ASSET_BRL_RATE = {
    "MATIC": Decimal(os.environ.get("MATIC_BRL_RATE", "2.50")),
    "USDT": Decimal(os.environ.get("USDT_BRL_RATE", "5.50")),
}
USDT_CONTRACT = os.environ.get("USDT_CONTRACT_ADDRESS_POLYGON", "0xc2132D05D31c914a87C6611C10748AEb04B58e8")
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

with app.app_context():
    db.create_all()


def _next_index():
    counter = Counter.query.get("hd_index")
    if counter is None:
        counter = Counter(name="hd_index", value=0)
        db.session.add(counter)
    index = counter.value
    counter.value += 1
    db.session.commit()
    return index


def _required_base_units(asset: str, amount_cents_brl: int) -> Decimal:
    rate = ASSET_BRL_RATE[asset]
    amount_brl = Decimal(amount_cents_brl) / 100
    return (amount_brl / rate).quantize(Decimal("0.000001"))


def _onchain_amount_received(address: str, asset: str) -> Decimal:
    if asset == "MATIC":
        wei = w3.eth.get_balance(Web3.to_checksum_address(address))
        return Decimal(w3.from_wei(wei, "ether"))
    if asset == "USDT":
        contract = w3.eth.contract(address=Web3.to_checksum_address(USDT_CONTRACT), abi=ERC20_ABI)
        decimals = contract.functions.decimals().call()
        raw = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        return Decimal(raw) / (Decimal(10) ** decimals)
    raise ValueError(f"asset não suportado: {asset}")


@app.get("/health")
def health():
    return {"status": "ok", "rpc_connected": w3.is_connected()}


@app.post("/invoices")
def create_invoice():
    data = request.get_json(force=True)
    amount_cents = data["amount_cents"]
    asset = data.get("asset", "USDT")
    merchant_account = data.get("merchant_account", MERCHANT_ACCOUNT)

    if asset not in ASSET_BRL_RATE:
        return jsonify({"error": f"ativo não suportado: {asset}"}), 400

    index = _next_index()
    address = derive_address(index)
    required = _required_base_units(asset, amount_cents)

    resp = requests.post(
        f"{LEDGER_URL}/transactions",
        json={
            "idempotency_key": f"crypto:{address}:{index}",
            "rail": "crypto",
            "external_ref": address,
            "entries": [
                {"account_id": SYSTEM_CRYPTO_PENDING_ACCOUNT, "amount_cents": -amount_cents},
                {"account_id": merchant_account, "amount_cents": amount_cents},
            ],
            "metadata": {"asset": asset, "address": address},
        },
        timeout=10,
    )
    resp.raise_for_status()
    txn = resp.json()

    invoice = Invoice(
        hd_index=index,
        address=address,
        asset=asset,
        chain="polygon",
        required_amount_base_units=required,
        amount_cents_brl=amount_cents,
        transaction_id=txn["id"],
    )
    db.session.add(invoice)
    db.session.commit()

    return jsonify(invoice.to_dict()), 201


@app.post("/invoices/<invoice_id>/check")
def check_invoice(invoice_id):
    """Consulta o saldo do endereço on-chain e liquida se o valor exigido
    chegou. Chame periodicamente (cron/worker) para cada invoice pendente —
    ou use /_watch/poll-all."""
    invoice = Invoice.query.get_or_404(invoice_id)
    if invoice.status != "awaiting_confirmation":
        return jsonify(invoice.to_dict())

    received = _onchain_amount_received(invoice.address, invoice.asset)
    if received >= invoice.required_amount_base_units:
        settle = requests.post(f"{LEDGER_URL}/transactions/{invoice.transaction_id}/settle", timeout=10)
        settle.raise_for_status()
        invoice.status = "confirmed"
        db.session.commit()

    return jsonify(invoice.to_dict())


@app.post("/_watch/poll-all")
def poll_all():
    """Atalho para rodar via cron: varre todas as invoices pendentes."""
    pending = Invoice.query.filter_by(status="awaiting_confirmation").all()
    results = []
    for invoice in pending:
        received = _onchain_amount_received(invoice.address, invoice.asset)
        if received >= invoice.required_amount_base_units:
            settle = requests.post(f"{LEDGER_URL}/transactions/{invoice.transaction_id}/settle", timeout=10)
            settle.raise_for_status()
            invoice.status = "confirmed"
        results.append({"invoice_id": invoice.id, "status": invoice.status})
    db.session.commit()
    return jsonify(results)


@app.post("/_sandbox/simulate-confirmation")
def simulate_confirmation():
    data = request.get_json(force=True)
    resp = requests.post(
        f"{LEDGER_URL}/transactions/{data['transaction_id']}/settle", timeout=10
    )
    resp.raise_for_status()
    return jsonify(resp.json())
