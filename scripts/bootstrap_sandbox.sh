#!/usr/bin/env bash
# Cria as contas de sistema e uma conta de lojista de teste no core-ledger,
# e imprime as linhas para colar no seu .env. Rode depois de `docker compose up -d`.
set -euo pipefail
LEDGER_URL="${LEDGER_URL:-http://localhost:8001}"

create_account() {
  curl -s -X POST "$LEDGER_URL/accounts" \
    -H "Content-Type: application/json" \
    -d "{\"owner_ref\": \"$1\", \"kind\": \"$2\"}" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])"
}

PIX_PENDING=$(create_account "system:pix_pending" "system")
CARD_RECEIVABLE=$(create_account "system:card_receivable" "system")
CRYPTO_PENDING=$(create_account "system:crypto_pending" "system")
MERCHANT=$(create_account "merchant:demo" "merchant")

echo "Cole isto no seu .env:"
echo "SYSTEM_PIX_PENDING_ACCOUNT=$PIX_PENDING"
echo "SYSTEM_CARD_RECEIVABLE_ACCOUNT=$CARD_RECEIVABLE"
echo "SYSTEM_CRYPTO_PENDING_ACCOUNT=$CRYPTO_PENDING"
echo "DEFAULT_MERCHANT_ACCOUNT=$MERCHANT"
echo ""
echo "Depois rode: docker compose up -d --build (de novo, para os serviços pegarem as envs)"
