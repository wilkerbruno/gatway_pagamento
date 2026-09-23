# Compliance — o que é exigido de verdade para operar um gateway de pagamentos no Brasil

Isto não é aconselhamento jurídico. É um mapa do terreno regulatório para você planejar
o projeto com os olhos abertos. Fale com um advogado especializado em meios de pagamento
e com uma consultoria PCI-DSS antes de processar qualquer transação real.

## 1. Banco Central do Brasil (BACEN)

- Uma empresa que **inicia, processa ou liquida pagamentos por conta de terceiros** se
  enquadra como **Instituição de Pagamento (IP)** sob a Lei 12.865/2013 e a Resolução
  BCB nº 80/2021 (e correlatas). Dependendo do modelo (emissor de moeda eletrônica,
  credenciadora, iniciador de pagamento, etc.), há categorias diferentes de IP.
- Abaixo de certo volume/faturamento, algumas atividades **podem operar sem autorização
  prévia do BACEN**, mas ainda assim seguem regras de registro, reporte e segurança.
  Acima do limite (definido em regulação, e revisado periodicamente), a autorização do
  BACEN é obrigatória antes de operar.
- **PIX especificamente**: para falar diretamente com o SPI (Sistema de Pagamentos
  Instantâneos) e o DICT, a empresa precisa ser **Participante do PIX** (direto ou
  indireto), o que exige ser instituição autorizada a funcionar pelo BACEN. Sem isso,
  o caminho realista é **integrar via um Participante já autorizado** (um PSP/banco
  parceiro) que oferece PIX como serviço — é o que a esmagadora maioria dos "gateways"
  brasileiros faz.
- Processo de autorização como IP: envolve capital mínimo, PLD/AML, governança,
  auditoria, plano de continuidade de negócio, e costuma levar **meses a mais de um ano**.

## 2. PCI-DSS (dados de cartão)

- Qualquer sistema que **armazena, processa ou transmite** PAN (o número do cartão) precisa
  seguir o PCI-DSS. O nível de auditoria (Self-Assessment Questionnaire vs. auditoria
  externa por um QSA) depende do volume anual de transações.
- Construir isso do zero significa: rede segmentada e monitorada, criptografia de dados
  em repouso e trânsito, HSM para chaves, logging e retenção de 1 ano+, testes de invasão
  trimestrais/anuais, política formal de segurança, treinamento de equipe — e uma
  auditoria anual paga (QSA). Custo típico: dezenas a centenas de milhares de reais/ano,
  fora o esforço de engenharia.
- **Alternativa realista e é o padrão de mercado**: nunca tocar no PAN. O front-end coleta
  o cartão direto num campo hospedado (iframe/SDK) da sua adquirente/gateway certificado,
  que devolve um **token**. Seu sistema só manipula o token. Isso reduz drasticamente o
  escopo de PCI-DSS que recai sobre você (SAQ A, o mais simples).

## 3. Bandeiras (Visa, Mastercard, Elo, etc.)

- Para processar cartão você precisa de um contrato com uma **adquirente ou
  subadquirente** credenciada pelas bandeiras (ex: Cielo, Rede, Stone, Adyen, etc.), a
  menos que você mesmo se torne uma adquirente licenciada — isso é um patamar acima de
  IP, com exigências ainda maiores.

## 4. Criptomoedas

- Desde a Lei 14.478/2022, prestadoras de serviços de ativos virtuais (exchanges,
  custodiantes) precisam de autorização do BACEN (regulamentação em consolidação desde
  2023). Se o seu gateway vai **custodiar** cripto de terceiros ou fazer câmbio
  cripto↔fiat por conta de terceiros, você provavelmente se enquadra como Prestadora de
  Serviços de Ativos Virtuais (PSAV) e precisa de autorização.
- Alternativa mais simples: integrar com uma exchange/custodiante já licenciada via API,
  e seu sistema apenas orquestra (gera invoice, monitora confirmação on-chain, dispara
  webhook) sem custodiar fundos de terceiros diretamente.

## 5. LGPD

- Dados de pagamento são dados pessoais sensíveis na prática. Você precisa de base legal
  para tratamento, política de privacidade, DPO (encarregado), resposta a incidentes,
  direito de exclusão/portabilidade, etc. — Lei 13.709/2018.

## 6. AML / PLD e KYC

- Instituições de pagamento precisam de programa de Prevenção à Lavagem de Dinheiro
  (PLD/FT): identificação e verificação de clientes (KYC), monitoramento de transações
  suspeitas, comunicação ao COAF, retenção de registros por 5+ anos.

## Resumo prático

| Você quer... | Precisa de |
|---|---|
| Testar a arquitetura, sem dinheiro real | Nada além do que está neste repo (modo sandbox) |
| Processar PIX de verdade | Parceria com um Participante PIX autorizado, ou virar um |
| Processar cartão de verdade | Contrato com adquirente/subadquirente + tokenização (SAQ A) |
| Guardar PAN você mesmo | Certificação PCI-DSS completa (evite se possível) |
| Custodiar cripto de terceiros | Autorização como PSAV (BACEN) |
| Operar como gateway completo e independente | Autorização como Instituição de Pagamento (BACEN) — projeto de 1+ ano |
