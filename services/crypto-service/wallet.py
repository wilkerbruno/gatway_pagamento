"""Derivação de endereços HD (BIP-44) — a parte que permite receber cripto
SEM nenhum provedor terceiro (sem Coinbase Commerce, BitPay, etc.): cada
invoice ganha seu próprio endereço, derivado deterministicamente de uma
seed/mnemônica, e o próprio serviço observa a blockchain diretamente via RPC.

⚠️ SEGURANÇA: aqui a mnemônica fica no mesmo processo que gera o endereço,
o que é aceitável para dev/sandbox mas NÃO é o ideal em produção — nessa
implementação a chave privada correspondente também pode ser derivada nesse
processo, e se o servidor for comprometido, os fundos recebidos correm risco.
Para produção, o caminho correto é: gerar endereços a partir de um XPUB
watch-only (sem chave privada nenhuma no servidor que fica exposto à
internet) e manter as chaves privadas em uma wallet fria/HSM, movendo os
fundos recebidos periodicamente para lá. Trocar por essa abordagem antes de
qualquer volume real.
"""
import os

from eth_account import Account
from eth_account.hdaccount import ETHEREUM_DEFAULT_PATH

Account.enable_unaudited_hdwallet_features()

MNEMONIC = os.environ.get("HD_WALLET_MNEMONIC")


def derive_address(index: int) -> str:
    if not MNEMONIC:
        raise RuntimeError("HD_WALLET_MNEMONIC não configurada (ver .env.example)")
    path = f"m/44'/60'/0'/0/{index}"
    acct = Account.from_mnemonic(MNEMONIC, account_path=path)
    return acct.address
