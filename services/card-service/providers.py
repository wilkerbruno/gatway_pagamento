"""Provedores de cartão, plugáveis (mesmo padrão do pix-service). O admin
escolhe via core-ledger: PUT /admin/settings/providers/card.

Em qualquer provedor, este serviço só manipula TOKEN de cartão — nunca PAN.
A tokenização acontece no front-end, com o SDK/campo hospedado do provedor
escolhido (Mercado Pago ou Pagar.me têm SDK de front-end para isso).
"""
import os
from abc import ABC, abstractmethod

import requests


class CardProvider(ABC):
    @abstractmethod
    def charge(self, card_token: str, amount_cents: int, installments: int, external_reference: str) -> dict:
        """Deve retornar {"approved": bool, "provider_ref": str, "raw_status": str}"""


class MercadoPagoCardProvider(CardProvider):
    BASE_URL = "https://api.mercadopago.com/v1/payments"

    def __init__(self):
        self.access_token = os.environ["MERCADOPAGO_ACCESS_TOKEN"]

    def charge(self, card_token, amount_cents, installments, external_reference):
        resp = requests.post(
            self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": external_reference,
            },
            json={
                "transaction_amount": round(amount_cents / 100, 2),
                "token": card_token,
                "installments": installments,
                "payment_method_id": "master",  # a bandeira normalmente vem do próprio token
                "payer": {"email": "sandbox@example.com"},
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "approved": data.get("status") == "approved",
            "provider_ref": str(data.get("id")),
            "raw_status": data.get("status"),
        }


class PagarmeCardProvider(CardProvider):
    BASE_URL = "https://api.pagar.me/core/v5/orders"

    def __init__(self):
        self.secret_key = os.environ["PAGARME_SECRET_KEY"]

    def charge(self, card_token, amount_cents, installments, external_reference):
        resp = requests.post(
            self.BASE_URL,
            auth=(self.secret_key, ""),
            json={
                "code": external_reference,
                "items": [{"amount": amount_cents, "description": "Cobrança cartão", "quantity": 1}],
                "customer": {
                    "name": "Cliente Sandbox",
                    "email": "sandbox@example.com",
                    "type": "individual",
                    "document": "00000000000",
                },
                "payments": [{
                    "payment_method": "credit_card",
                    "credit_card": {"installments": installments, "card_token": card_token},
                }],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        charge = data["charges"][0]
        status = charge.get("status")
        return {
            "approved": status in ("paid", "processing"),
            "provider_ref": charge["id"],
            "raw_status": status,
        }


class DirectCardProvider(CardProvider):
    """Processar cartão sem nenhuma adquirente/subadquirente por trás exige
    você mesmo virar uma credenciadora licenciada pelas bandeiras — um
    patamar regulatório ainda maior que virar Instituição de Pagamento para
    PIX. Na prática, praticamente ninguém opera cartão 100% "direto"; sempre
    existe uma credenciadora no fundo da pilha, mesmo para os grandes
    gateways. Este provider fica aqui só por simetria de interface."""

    def charge(self, card_token, amount_cents, installments, external_reference):
        raise NotImplementedError(
            "Processamento de cartão sem adquirente/credenciadora não é "
            "operacionalmente viável — veja docs/COMPLIANCE.md. Use "
            "provider=mercadopago ou provider=pagarme."
        )


def get_provider(name: str) -> CardProvider:
    providers = {
        "mercadopago": MercadoPagoCardProvider,
        "pagarme": PagarmeCardProvider,
        "direct": DirectCardProvider,
    }
    if name not in providers:
        raise ValueError(f"provedor de cartão desconhecido: {name}")
    return providers[name]()
