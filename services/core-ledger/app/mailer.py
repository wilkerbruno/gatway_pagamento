"""Envio de e-mail simples via SMTP — usado só pro código de "esqueci minha
senha" (admin e cliente). Nada de fila/retry sofisticado: se o SMTP falhar,
quem chama decide o que fazer (a rota de reset sempre responde algo genérico
pro usuário de qualquer forma, pra não vazar se um login existe ou não —
ver routes.py).

Configuração via env vars:
  SMTP_HOST, SMTP_PORT (padrão 587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM (remetente, ex: "Divisions Pay <no-reply@seudominio.com>"),
  SMTP_USE_TLS (padrão "true").

Duas formas de TLS existem em SMTP e não são intercambiáveis: STARTTLS
(porta 587 tradicionalmente) começa a conexão em texto puro e faz upgrade
pra TLS depois do "hello"; SSL implícito (porta 465, comum em provedores
cPanel como o da CloudWeby) já exige TLS desde o primeiro byte da conexão.
Chamar server.starttls() numa porta 465 trava/falha, porque o servidor já
está esperando um handshake TLS. Por isso a porta 465 é detectada
automaticamente aqui e usa SMTP_SSL em vez de SMTP+starttls.

Sem SMTP_HOST configurado, send_email levanta RuntimeError com uma mensagem
clara — pra isso aparecer no log do serviço em vez de falhar silenciosamente
e o admin nunca descobrir por que ninguém recebe o código."""
import os
import smtplib
from email.message import EmailMessage


def send_email(to_email: str, subject: str, body_text: str) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host:
        raise RuntimeError(
            "SMTP não configurado (faltam as env vars SMTP_HOST/SMTP_USER/"
            "SMTP_PASSWORD/SMTP_FROM) -- não há como enviar e-mail."
        )

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM", user or "no-reply@localhost")
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body_text)

    if port == 465:
        # SSL implícito -- a conexão já nasce criptografada, sem starttls().
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            if user and password:
                server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
