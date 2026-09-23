# Arquitetura

```
                         ┌────────────────────┐
                         │   Client (web/app)  │
                         └──────────┬──────────┘
                                    │ HTTPS (API pública / checkout)
                         ┌──────────▼──────────┐
                         │   API Gateway/Nginx  │  (infra/nginx)
                         └──────────┬──────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 │                  │                   │
        ┌────────▼───────┐ ┌────────▼───────┐ ┌─────────▼────────┐
        │  pix-service    │ │  card-service   │ │  crypto-service  │
        │  (adapter PIX)  │ │ (tokenização)   │ │ (invoices/watch) │
        └────────┬───────┘ └────────┬───────┘ └─────────┬────────┘
                 │                  │                   │
                 └──────────────────┼───────────────────┘
                                    │  eventos internos (fila/HTTP interno)
                         ┌──────────▼──────────┐
                         │     core-ledger      │
                         │ contas · lançamentos  │
                         │ idempotência · webhooks│
                         └──────────┬──────────┘
                                    │
                         ┌──────────▼──────────┐
                         │    MySQL + Redis     │
                         └──────────────────────┘
```

## Princípios de design

1. **core-ledger é a fonte da verdade.** Nenhum outro serviço escreve saldo/lançamento
   diretamente no banco — eles chamam o ledger, que aplica o lançamento em partida dobrada
   (toda transação tem um débito e um crédito, soma sempre zero). Isso facilita auditoria
   e reconciliação.
2. **Idempotência em tudo.** Toda chamada que move dinheiro carrega uma `idempotency_key`.
   Reenviar a mesma requisição (retry de rede, timeout do PSP) nunca duplica o lançamento.
3. **Nenhum serviço guarda segredo de rede de pagamento em texto puro.** Credenciais de
   PSP/adquirente ficam em variáveis de ambiente/segredo do orquestrador (EasyPanel
   secrets), nunca no repositório.
4. **card-service nunca vê o PAN completo em claro no seu backend** — a captura acontece
   no front-end via campo hospedado da adquirente; o backend só recebe e repassa tokens.
5. **Webhooks assinados.** Tudo que os serviços de pagamento recebem de fora (PIX, cartão,
   cripto) chega por webhook — valide a assinatura/HMAC antes de processar, e responda
   200 rápido, processando de forma assíncrona.
6. **Cada rede de pagamento é um adaptador substituível.** Trocar de PSP de PIX ou de
   adquirente de cartão deve significar reescrever um serviço, não o sistema inteiro —
   por isso a separação em microsserviços com um contrato de eventos comum com o ledger.

## Fluxo de uma cobrança PIX (exemplo)

1. Cliente pede um QR Code → `pix-service` chama o PSP parceiro, recebe `txid` + payload.
2. `pix-service` registra no `core-ledger` um lançamento **pendente**.
3. PSP parceiro confirma pagamento via webhook → `pix-service` valida assinatura →
   chama `core-ledger` para **liquidar** o lançamento (pendente → confirmado).
4. `core-ledger` dispara webhook de "pagamento confirmado" para o sistema do lojista.

## Fluxo de uma cobrança de cartão (exemplo)

1. Front-end coleta o cartão via campo hospedado da adquirente → recebe um **token**.
2. Cliente manda o token para `card-service`, que chama a adquirente para autorizar.
3. Adquirente aprova/recusa → `card-service` registra o lançamento no `core-ledger`.
4. Captura (se em duas etapas) e eventual estorno seguem o mesmo padrão: `card-service`
   fala com a adquirente, `core-ledger` registra o efeito contábil.
