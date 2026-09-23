# Divisions Pay — Gateway de Pagamentos

> **Leia `docs/COMPLIANCE.md` antes de escrever qualquer linha de código de produção.**
> Este repositório é o esqueleto de um **motor de pagamentos**, não um produto pronto para
> movimentar dinheiro real. Processar PIX, cartão e cripto legalmente no Brasil exige
> registro/licenciamento e certificações que estão fora do escopo de código — veja o roadmap.

## O que é isto

Uma arquitetura de microsserviços em Flask, pensada para rodar no seu stack atual
(Docker + EasyPanel + GitHub Actions), organizada em:

- **core-ledger** — o "cérebro": contas, saldos, lançamentos contábeis (double-entry),
  idempotência, orquestração de transações e disparo de webhooks. É o único serviço que
  fala com o seu banco de dados de verdade.
- **pix-service** — adaptador para PIX (BACEN SPI/DICT). Em fase de sandbox, fala com um
  Participante do PIX (PSP parceiro) via API; só fala direto com o BACEN se/quando você
  virar Participante Direto ou Indireto.
- **card-service** — tokenização e roteamento de cartão. **Nunca guarda PAN em texto puro
  fora de um ambiente PCI-DSS certificado.** Em fase de sandbox, delega a captura/
  processamento a uma adquirente/subadquirente (ex: via API), guardando apenas tokens.
- **crypto-service** — geração de endereços/invoices, monitoramento de confirmações on-chain
  e (opcional) liquidação via custodiante licenciado ou exchange parceira.

## Por que não "cartão de verdade" direto no seu banco

Guardar número de cartão (PAN), CVV ou trilha magnética em qualquer sistema seu exige
certificação **PCI-DSS** (nível 1 se o volume for alto) — auditoria anual, testes de
invasão trimestrais, segregação de rede, HSM, etc. É um projeto à parte, caro, e sem ele
você está descumprindo os termos das bandeiras (Visa/Mastercard/Elo) e pode responder
civil e criminalmente por vazamento de dados. Por isso o `card-service` aqui é desenhado
para **tokenizar via uma adquirente/gateway já certificado** — é assim que praticamente
todo gateway do mercado (Stripe, Pagar.me, Cielo, etc.) opera por baixo dos panos.

## Como rodar (ambiente de desenvolvimento, sem dinheiro real)

```bash
cp .env.example .env
docker compose up --build
```

Local usa `docker-compose.yml` + `docker-compose.override.yml` juntos (o Compose lê os dois automaticamente) — é o override que publica as portas no seu `localhost`. Em produção (EasyPanel), só o `docker-compose.yml` é usado; o acesso externo é configurado pela aba "Domínios" de cada serviço, apontando pro nome do serviço + porta interna (ex: `admin-panel:8000`), sem precisar publicar porta nenhuma.

Isso sobe os 4 serviços + MySQL + Redis, todos em modo "sandbox" (sem credenciais
reais de nenhuma rede de pagamento).

## Próximos passos

Veja `docs/ROADMAP.md` para a ordem recomendada: sandbox → parceiro licenciado → produção.

## Provedor plugável (PIX e cartão) — trocável pelo admin, sem redeploy

Até você ter licença/parceria própria, `pix-service` e `card-service` roteiam
para **Mercado Pago** ou **Pagar.me** por trás de uma interface comum
(`providers.py` em cada serviço). O admin escolhe qual está ativo via API do
`core-ledger`:

```bash
# ver o que está ativo em cada trilho
curl http://localhost:8001/admin/settings/providers

# trocar o PIX para Mercado Pago
curl -X PUT http://localhost:8001/admin/settings/providers/pix \
  -H "Content-Type: application/json" -d '{"provider": "mercadopago"}'

# trocar o cartão para Pagar.me
curl -X PUT http://localhost:8001/admin/settings/providers/card \
  -H "Content-Type: application/json" -d '{"provider": "pagarme"}'
```

A troca vale em até 10s (cache curto em cada serviço) — sem redeploy. Quando
você conseguir a licença/participação direta no PIX, implemente
`DirectPixProvider.create_charge()` em `services/pix-service/providers.py` e
troque para `provider: "direct"` — todo o resto (ledger, idempotência,
webhooks) continua igual, porque o contrato da interface não muda.

## Cripto sem provedor terceiro

`crypto-service` **não** usa Coinbase Commerce, BitPay ou similar. Cada
cobrança gera um endereço próprio (derivação HD/BIP-44 a partir de uma
mnemônica sua — `wallet.py`), e o próprio serviço consulta a blockchain via
RPC (Polygon por padrão) para detectar o pagamento e liquidar no ledger.
Isso não depende de nenhum processador de pagamento — só de um nó/RPC
público, que é apenas uma fonte de leitura da blockchain.

⚠️ Isso não elimina a análise regulatória sobre custódia de ativos virtuais
(ver `docs/COMPLIANCE.md`), nem o cuidado de segurança com a mnemônica em
produção (ver aviso em `services/crypto-service/wallet.py`).

## Carteira de cliente ("banco" interno da plataforma)

O `core-ledger` já expõe clientes com carteira própria, transferência interna
instantânea (P2P) e extrato:

```bash
# criar dois clientes (cada um já ganha uma carteira/Account junto)
curl -X POST http://localhost:8001/customers -d '{"name":"Alice","document":"11111111111"}'
curl -X POST http://localhost:8001/customers -d '{"name":"Bob","document":"22222222222"}'

# transferir entre eles (instantâneo, dentro da plataforma)
curl -X POST http://localhost:8001/transfers -d '{
  "idempotency_key": "transfer:unico-por-operacao",
  "from_account_id": "<id da conta da Alice>",
  "to_account_id": "<id da conta do Bob>",
  "amount_cents": 2000
}'

# extrato
curl http://localhost:8001/customers/<id>/statement
```

Regras já embutidas:
- **Sem saldo negativo para cliente/lojista.** Só contas `kind: "system"`
  (as que representam "dinheiro a caminho" do PSP) podem ficar negativas —
  é o que permite reconhecer o crédito na carteira do cliente antes mesmo do
  dinheiro ter, de fato, entrado na sua conta bancária real.
- **Idempotência** também nas transferências: reenviar a mesma
  `idempotency_key` nunca duplica.

### O limite entre "carteira interna" e "virar um banco de verdade"

Isso que está aqui — saldo, transferência entre usuários da sua própria
plataforma, extrato — não exige licença: é o mesmo modelo de "saldo em
conta" que Mercado Pago, PicPay etc. oferecem. O que puxa para o
licenciamento como Instituição de Pagamento (emissora de moeda eletrônica)
é deixar esse saldo **sair** da plataforma: PIX para terceiros fora do seu
sistema, TED, saque para conta bancária externa, cartão pré-pago vinculado
à carteira. Se/quando for construir isso, volte no `docs/COMPLIANCE.md`.

## Painel admin (web)

Serviço `admin-panel`, na porta 8000, protegido por login (HTTP Basic Auth —
`ADMIN_USER`/`ADMIN_PASSWORD` no `.env`, troque os valores padrão antes de
expor publicamente). Dá pra:

- ver clientes e saldo
- criar cliente
- fazer transferência interna
- gerar uma cobrança PIX/cartão/cripto de teste (mostra o QR Code/link e tem
  botão "simular pagamento" pro fluxo sandbox)
- trocar o provedor ativo de PIX/cartão
- conferir as "Contas do sistema" (as contas internas de "a receber do
  provedor" que pix/card/crypto-service usam antes de creditar o
  lojista/cliente) — são criadas sozinhas na primeira cobrança de cada
  tipo, não precisa configurar nada à mão

Acesse em `http://localhost:8000` (local) ou pelo domínio que você habilitar
pra esse serviço no EasyPanel.


## Portal do cliente (customer-portal)

Serviço separado do admin-panel, na porta 8006, com visual de banco digital
(estilo PicPay) já pensado pra virar app mobile depois: cartão de saldo com
o valor escondível, ações rápidas, navegação inferior fixa (Início /
Transferir / Extrato / Perfil) e tema escuro.

Cada cliente loga com o próprio CPF/CNPJ ou e-mail + a senha que o admin
definiu pra ele (campo opcional no formulário "Novo cliente" do admin-panel)
e só vê os **próprios** dados — sessão isolada por cliente, sem acesso a
nenhum dado de outros clientes ou às telas administrativas.

Telas do portal:
- **Início** — saldo (com botão de mostrar/esconder), atalhos e as últimas
  movimentações
- **Transferir** — o próprio cliente manda dinheiro pra outro cliente da
  Divisions Pay direto por aqui (busca o destinatário por CPF/CNPJ ou
  e-mail via `GET /customers/lookup`, que só confirma se existe — nunca
  expõe a lista de clientes pra quem está logado); cai na hora, sem taxa,
  é só um lançamento em partida dobrada via `POST /transfers`
- **Extrato** — histórico completo, paginado
- **Perfil** — dados da conta e sair

Pra dar acesso a um cliente que já existe sem senha, chame direto no
core-ledger: `PUT /customers/<id>/password` com `{"password": "..."}`.

## Taxa da plataforma (1% por padrão)

Configurável em **Taxa** no admin-panel: escolhe qual conta recebe e o
percentual (padrão 1%). A partir daí, toda cobrança recebida via PIX, cartão
ou cripto tem esse percentual descontado automaticamente do valor creditado
ao lojista/cliente e transferido pra essa conta — **além** de qualquer taxa
que o Mercado Pago/Pagar.me já cobrem por fora (essa taxa deles não passa
pelo seu sistema, é descontada antes de você receber o valor líquido deles).

Não incide em transferências internas (`/transfers`) — só nas cobranças que
entram de fora pela primeira vez. Enquanto nenhuma conta estiver configurada,
nenhuma taxa é cobrada (comportamento padrão, opt-in).
