# Roadmap sugerido

## Fase 0 — Sandbox (o que este repo entrega)
- core-ledger funcional (contas, lançamentos, idempotência, webhooks) com testes.
- pix-service, card-service, crypto-service como adaptadores com **mocks** (simulam
  aprovação/recusa) para você validar o fluxo de ponta a ponta sem nenhuma credencial real.
- Deploy no seu EasyPanel, mesma esteira que os outros projetos (Divisions ERP, EasyFood etc.).

## Fase 1 — Integrações reais via parceiros já licenciados
- PIX: contratar um PSP que ofereça "PIX via API" (várias fintechs brasileiras oferecem
  isso como produto, sem você precisar virar Participante do BACEN). Trocar o mock do
  `pix-service` pela integração real.
- Cartão: contratar uma adquirente/subadquirente com tokenização (campo hospedado ou SDK).
  Trocar o mock do `card-service` pela integração real. Isso já te dá "seu próprio gateway"
  do ponto de vista do lojista/cliente final — sua marca, seu checkout, sua API — mesmo
  processando por trás através de parceiros licenciados.
- Cripto: integrar com uma exchange/custodiante licenciada (ou node próprio + custódia
  própria, se você assumir a responsabilidade regulatória de PSAV).
- Consultoria jurídica para confirmar seu enquadramento (abaixo ou acima do limite que
  exige autorização do BACEN) e ajustar termos de uso, política de privacidade (LGPD).

## Fase 2 — Virar o próprio Participante/Instituição de Pagamento (opcional, alto investimento)
- Só faz sentido em escala: quando o custo das tarifas dos parceiros da Fase 1 supera o
  custo de licenciamento próprio.
- Requer: pedido de autorização ao BACEN, capital mínimo, estrutura de governança e
  compliance PLD/AML dedicada, auditoria externa, e (se for guardar PAN) certificação
  PCI-DSS completa.

## Regra prática
Não pule para a Fase 2 sem ter validado o produto na Fase 1. A maioria dos "gateways de
pagamento" brasileiros de sucesso começou exatamente assim: orquestrando parceiros
licenciados por trás de uma marca e experiência própria.
