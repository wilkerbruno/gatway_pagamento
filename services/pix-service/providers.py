"""Provedores de PIX, plugáveis. O admin escolhe qual está ativo (ver
core-ledger: GET/PUT /admin/settings/providers/pix) sem precisar de redeploy.

- MercadoPagoPixProvider / PagarmePixProvider: integrações reais (sandbox por
  padrão, usando as chaves de teste das respectivas plataformas).
- DirectPixProvider: onde entra, no futuro, a integração direta com o SPI/DICT
  do Banco Central — só é utilizável depois que a empresa virar Participante
  do PIX (ver docs/COMPLIANCE.md). Hoje ela existe só para o código já ter o
  "encaixe" certo; chamar cria um erro claro em vez de fingir que funciona.
"""
import hmac
import hashlib
import json
import os
import uuid
from abc import ABC, abstractmethod

import requests


class PixProvider(ABC):
    @abstractmethod
    def create_charge(self, amount_cents: int, external_reference: str, payer_email: str | None = None) -> dict:
        """Deve retornar {"provider_ref": str, "qr_code_payload": str, "qr_code_base64": str|None}"""

    @abstractmethod
    def parse_webhook(self, payload: dict, headers: dict, query_params: dict | None = None) -> dict:
        """Deve retornar {"provider_ref": str, "status": "confirmed"|"pending"|"failed", "verified": bool}.

        "verified" é o campo que importa pra segurança: diz se a origem do
        webhook foi de fato confirmada (assinatura válida) antes de confiar
        no conteúdo. Qualquer coisa que mexa com dinheiro de verdade deve
        checar esse campo e recusar (nunca liquidar) um webhook não
        verificado — ver app.py."""

    def check_status(self, provider_ref: str) -> dict:
        """Reconsulta ATIVA (pull) do status direto na API do provedor, sem
        depender de nenhum webhook ter chegado. É o que o botão "Verificar
        pagamento agora" do admin-panel chama, e existe porque webhook pode
        atrasar, ser mal configurado, ou simplesmente nunca chegar (URL
        errada, evento não marcado no painel do provedor, etc.) — sem isso,
        um pagamento real que o cliente já fez fica preso pra sempre esperando
        uma notificação que pode não vir. Diferente do webhook, essa chamada
        não precisa de verificação de assinatura: é o próprio servidor indo
        buscar a informação na API oficial do provedor, autenticado com a
        nossa própria credencial — não há nada pra forjar aqui.

        Deve retornar {"provider_ref": str, "status": "confirmed"|"pending"|"failed"}.
        Levanta NotImplementedError se o provedor não suportar (ex: sandbox)."""
        raise NotImplementedError(
            f"{type(self).__name__} não suporta verificação manual de status."
        )


class MercadoPagoPixProvider(PixProvider):
    BASE_URL = "https://api.mercadopago.com/v1/orders"

    def __init__(self):
        self.access_token = os.environ["MERCADOPAGO_ACCESS_TOKEN"]
        # Chave secreta de assinatura do webhook (Mercado Pago > sua aplicação
        # > Webhooks > "Assinatura secreta"). Sem ela, não tem como confirmar
        # que uma notificação veio mesmo do Mercado Pago — qualquer um
        # poderia forjar um POST dizendo "pagamento aprovado" e ganhar
        # crédito de graça. Por isso, sem essa variável configurada, todo
        # webhook é tratado como NÃO verificado (nunca liquida sozinho).
        self.webhook_secret = os.environ.get("MERCADOPAGO_WEBHOOK_SECRET")

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

    def _verify_signature(self, headers, query_params) -> bool:
        """Esquema de assinatura do Mercado Pago: header 'x-signature' no
        formato 'ts=<epoch>,v1=<hmac_sha256_hex>', calculado sobre o
        manifesto 'id:<data.id>;request-id:<x-request-id>;ts:<ts>;' usando a
        assinatura secreta como chave. Confira contra a documentação atual
        do Mercado Pago antes de operar com volume real — esse é o esquema
        deles no momento em que este código foi escrito."""
        if not self.webhook_secret:
            return False

        signature_header = headers.get("x-signature") or headers.get("X-Signature")
        request_id = headers.get("x-request-id") or headers.get("X-Request-Id")
        data_id = (query_params or {}).get("data.id") or (query_params or {}).get("id")
        if not signature_header or not data_id:
            return False

        parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
        ts, v1 = parts.get("ts"), parts.get("v1")
        if not ts or not v1:
            return False

        manifest = f"id:{str(data_id).lower()};request-id:{request_id or ''};ts:{ts};"
        computed = hmac.new(self.webhook_secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, v1)

    def _query_order_status(self, order_id: str) -> dict:
        """Consulta autoritativa direto no Mercado Pago (GET /v1/orders/{id}).
        Usada tanto pelo webhook (depois de verificado) quanto pela
        reconsulta manual (check_status), que não passa por webhook nenhum."""
        resp = requests.get(
            f"{self.BASE_URL}/{order_id}",
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        payments = data.get("transactions", {}).get("payments", [{}])
        raw_status = (payments[0].get("status") if payments else None) or data.get("status", "pending")
        status_map = {"approved": "confirmed", "processed": "confirmed", "rejected": "failed", "cancelled": "failed"}
        return {
            "provider_ref": data.get("id"),
            "status": status_map.get(raw_status, "pending"),
        }

    def check_status(self, provider_ref: str) -> dict:
        return self._query_order_status(provider_ref)

    def parse_webhook(self, payload, headers, query_params=None):
        verified = self._verify_signature(headers, query_params)
        if not verified:
            # Não confiamos numa notificação que não conseguimos autenticar.
            # Devolve "pending": o pagamento só será liquidado quando algo
            # confiável confirmar (ex: você reconciliar manualmente pelo botão
            # "Verificar pagamento agora", ou a assinatura ficar configurada
            # corretamente).
            return {"provider_ref": None, "status": "pending", "verified": False}

        # Nunca confia no campo "status" do corpo do webhook em si — ele é
        # só um aviso de "algo mudou". Reconsulta a API do Mercado Pago pra
        # pegar o status real e autoritativo antes de liquidar qualquer coisa.
        order_id = payload.get("data", {}).get("id") or payload.get("id")
        if not order_id:
            return {"provider_ref": None, "status": "pending", "verified": True}

        result = self._query_order_status(order_id)
        return {**result, "verified": True}


class PagarmePixProvider(PixProvider):
    BASE_URL = "https://api.pagar.me/core/v5/orders"

    def __init__(self):
        self.secret_key = os.environ["PAGARME_SECRET_KEY"]
        # Configurada no painel da Pagar.me, na tela de webhooks. Sem ela,
        # nenhum webhook é confiado (mesmo raciocínio do Mercado Pago acima).
        self.webhook_secret = os.environ.get("PAGARME_WEBHOOK_SECRET")

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

    def check_status(self, provider_ref: str) -> dict:
        """provider_ref aqui é o id da charge (é o que create_charge devolve
        como provider_ref). A Pagar.me tem um endpoint dedicado pra
        consultar uma charge isoladamente."""
        resp = requests.get(
            f"https://api.pagar.me/core/v5/charges/{provider_ref}",
            auth=(self.secret_key, ""),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_status = data.get("status", "pending")
        status_map = {
            "paid": "confirmed",
            "failed": "failed",
            "canceled": "failed",
            "processing": "pending",
            "pending": "pending",
        }
        return {"provider_ref": data.get("id"), "status": status_map.get(raw_status, "pending")}

    def parse_webhook(self, payload, headers, query_params=None):
        # Pagar.me manda {"type": "order.paid" | "charge.paid" | ..., "data": {...}}.
        # A verificação abaixo é um HMAC simples sobre o corpo cru — confira
        # o esquema exato (nome do header, algoritmo) na documentação atual
        # da Pagar.me antes de operar com volume real; até lá, sem a env var
        # configurada, todo webhook fica "não verificado" por padrão.
        if not self.webhook_secret:
            return {"provider_ref": None, "status": "pending", "verified": False}

        signature = headers.get("x-hub-signature") or headers.get("X-Hub-Signature")
        if not signature:
            return {"provider_ref": None, "status": "pending", "verified": False}

        computed = hmac.new(
            self.webhook_secret.encode(),
            json.dumps(payload, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed, signature.replace("sha256=", "")):
            return {"provider_ref": None, "status": "pending", "verified": False}

        event_type = payload.get("type", "")
        status = "confirmed" if event_type.endswith(".paid") else "pending"
        if event_type.endswith((".failed", ".refused")):
            status = "failed"
        charge = payload.get("data", {})
        return {"provider_ref": charge.get("id"), "status": status, "verified": True}


class SandboxPixProvider(PixProvider):
    """Simula um PIX de ponta a ponta sem chamar nenhuma API externa — nao
    precisa de token/chave de ninguem. E o jeito de testar o fluxo completo
    (cobranca -> pagamento -> liquidacao) antes de configurar Mercado Pago
    ou Pagar.me de verdade. O "pagamento" so acontece quando voce chama
    POST /_sandbox/simulate-payment (o botao "Simular pagamento" no painel)."""

    def create_charge(self, amount_cents, external_reference, payer_email=None):
        fake_payload = f"00020126580014br.gov.bcb.pix0136SANDBOX{external_reference}5204000053039865802BR"
        return {
            "provider_ref": external_reference,
            "qr_code_payload": fake_payload,
            "qr_code_base64": None,
        }

    def parse_webhook(self, payload, headers, query_params=None):
        # sandbox nao recebe webhook de ninguem — a confirmacao vem so pelo
        # endpoint /_sandbox/simulate-payment (chamado direto pelo admin-panel,
        # já autenticado, então não há um webhook externo pra verificar aqui)
        return {"provider_ref": payload.get("provider_ref"), "status": "pending", "verified": True}


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

    def parse_webhook(self, payload, headers, query_params=None):
        raise NotImplementedError("Ver create_charge().")


def get_provider(name: str) -> PixProvider:
    providers = {
        "sandbox": SandboxPixProvider,
        "mercadopago": MercadoPagoPixProvider,
        "pagarme": PagarmePixProvider,
        "direct": DirectPixProvider,
    }
    if name not in providers:
        raise ValueError(f"provedor PIX desconhecido: {name}")
    return providers[name]()
