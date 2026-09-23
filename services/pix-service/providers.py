"""Provedores de PIX, plugáveis. O admin escolhe qual está ativo (ver
core-ledger: GET/PUT /admin/settings/providers/pix) sem precisar de redeploy.

- MercadoPagoPixProvider / PagarmePixProvider: integrações reais (sandbox por
  padrão, usando as chaves de teste das respectivas plataformas).
- DirectPixProvider: onde entra, no futuro, a integração direta com o SPI/DICT
  do Banco Central — só é utilizável depois que a empresa virar Participante
  do PIX (ver docs/COMPLIANCE.md). Hoje ela existe só para o código já ter o
  "encaixe" certo; chamar cria um erro claro em vez de fingir que funciona.
"""
import os
import uuid
from abc import ABC, abstractmethod

import requests


class PixProvider(ABC):
    @abstractmethod
    def create_charge(self, amount_cents: int, external_reference: str, payer_email: str | None = None) -> dict:
        """Deve retornar {"provider_ref": str, "qr_code_payload": str, "qr_code_base64": str|None}"""

    @abstractmethod
    def parse_webhook(self, payload: dict, headers: dict) -> dict:
        """Deve retornar {"provider_ref": str, "status": "confirmed"|"pending"|"failed"}"""


class MercadoPagoPixProvider(PixProvider):
    BASE_URL = "https://api.mercadopago.com/v1/orders"

    def __init__(self):
        self.access_token = os.environ["MERCADOPAGO_ACCESS_TOKEN"]

    def create_charge(self, amount_cents, external_reference, payer_email=None):
        amount = f"{amount_cents / 100:.2f}"
        resp = requests.post(
            self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": external_reference,
            },
            json={
                "type": "online",
                "total_amount": amount,
                "external_reference": external_reference,
                "processing_mode": "automatic",
                "transactions": {
                    "payments": [{
                        "amount": amount,
                        "payment_method": {"id": "pix", "type": "bank_transfer"},
                    }]
                },
                "payer": {"email": payer_email or "sandbox_payer@example.com"},
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        payment = data["transactions"]["payments"][0]
        pm = payment["payment_method"]
        return {
            "provider_ref": data["id"],
            "qr_code_payload": pm.get("qr_code"),
            "qr_code_base64": pm.get("qr_code_base64"),
        }

    def parse_webhook(self, payload, headers):
        # Mercado Pago manda notificações do tipo {"type": "payment", "data": {"id": ...}}.
        # Em produção: com o id, consulte GET /v1/orders/{id} para confirmar o status
        # (nunca confie cegamente no corpo do webhook) e valide a assinatura
        # (header x-signature) antes de processar.
        status_map = {"approved": "confirmed", "rejected": "failed"}
        status = payload.get("status", "pending")
        return {
            "provider_ref": payload.get("id") or payload.get("data", {}).get("id"),
            "status": status_map.get(status, "pending"),
        }


class PagarmePixProvider(PixProvider):
    BASE_URL = "https://api.pagar.me/core/v5/orders"

    def __init__(self):
        self.secret_key = os.environ["PAGARME_SECRET_KEY"]

    def create_charge(self, amount_cents, external_reference, payer_email=None):
        resp = requests.post(
            self.BASE_URL,
            auth=(self.secret_key, ""),
            json={
                "code": external_reference,
                "items": [{"amount": amount_cents, "description": "Cobrança PIX", "quantity": 1}],
                "customer": {
                    "name": "Cliente Sandbox",
                    "email": payer_email or "sandbox@example.com",
                    "type": "individual",
                    "document": "00000000000",
                },
                "payments": [{
                    "payment_method": "pix",
                    "pix": {"expires_in": 3600},
                }],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        charge = data["charges"][0]
        tx = charge["last_transaction"]
        return {
            "provider_ref": charge["id"],
            "qr_code_payload": tx.get("qr_code"),
            "qr_code_base64": tx.get("qr_code_url"),
        }

    def parse_webhook(self, payload, headers):
        # Pagar.me manda {"type": "order.paid" | "charge.paid" | ..., "data": {...}}.
        # Em produção: valide a assinatura (header configurado no painel) antes de processar.
        event_type = payload.get("type", "")
        status = "confirmed" if event_type.endswith(".paid") else "pending"
        if event_type.endswith((".failed", ".refused")):
            status = "failed"
        charge = payload.get("data", {})
        return {"provider_ref": charge.get("id"), "status": status}


class DirectPixProvider(PixProvider):
    """Integração direta com o SPI/DICT do Banco Central. Só funciona depois
    que a empresa virar Participante do PIX autorizado — ver docs/COMPLIANCE.md.
    Deixa o erro explícito em vez de simular, para não mascarar que ainda não
    dá para usar em produção."""

    def create_charge(self, amount_cents, external_reference, payer_email=None):
        raise NotImplementedError(
            "PIX direto via SPI/DICT requer ser Participante do PIX autorizado "
            "pelo BACEN. Veja docs/COMPLIANCE.md e docs/ROADMAP.md (Fase 2). "
            "Até lá, use provider=mercadopago ou provider=pagarme."
        )

    def parse_webhook(self, payload, headers):
        raise NotImplementedError("Ver create_charge().")


def get_provider(name: str) -> PixProvider:
    providers = {
        "mercadopago": MercadoPagoPixProvider,
        "pagarme": PagarmePixProvider,
        "direct": DirectPixProvider,
    }
    if name not in providers:
        raise ValueError(f"provedor PIX desconhecido: {name}")
    return providers[name]()
