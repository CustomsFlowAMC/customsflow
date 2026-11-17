# -*- coding: utf-8 -*-
"""
CustomsFlow - robo de e-mail
-----------------------------
Roda dentro do GitHub Actions (agendado). A cada execucao:

  1) Conecta no Gmail via IMAP e procura e-mails NAO LIDOS vindos dos
     remetentes autorizados (variavel de ambiente ALLOWED_SENDERS).
  2) Para cada e-mail encontrado, baixa os PDFs anexados (direto ou dentro
     de um .zip) e roda ajustar_faturas.process() em cada um.
  3) Responde o e-mail original (mesma thread) com os PDFs ja ajustados
     anexados.
  4) Marca o e-mail como lido/processado.
  5) Atualiza docs/dados.json com o historico, para o painel (GitHub Pages).
  6) Se alguma fatura der erro, envia um ALERTA por e-mail (ALERT_EMAIL).

Variaveis de ambiente esperadas (configuradas como Secrets no GitHub):
  EMAIL_ADDRESS        - ex: customsflow@gmail.com
  EMAIL_APP_PASSWORD   - senha de app do Gmail (16 caracteres)
  ALLOWED_SENDERS       - lista separada por virgula de remetentes autorizados
  ALERT_EMAIL          - (opcional) quem recebe o alerta de erro.
                         Se nao existir, o alerta vai para o EMAIL_ADDRESS.
"""

import os
import json
import ssl
import zipfile
import imaplib
import smtplib
import datetime
import traceback
from io import BytesIO
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parseaddr

from ajustar_faturas import process

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465

DADOS_PATH = os.path.join("docs", "dados.json")


def mascarar_email(endereco):
    """Mascara um e-mail para exibicao publica no painel, ex:
    joelma.oliveira@consultant.volvo.com -> j***@consultant.volvo.com"""
    if "@" not in endereco:
        return "***"
    usuario, dominio = endereco.split("@", 1)
    inicial = usuario[0] if usuario else "*"
    return f"{inicial}***@{dominio}"


def carregar_dados():
    if os.path.exists(DADOS_PATH):
        with open(DADOS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"total_processadas": 0, "total_erros": 0, "execucoes": []}


def salvar_dados(dados):
    os.makedirs(os.path.dirname(DADOS_PATH), exist_ok=True)
    with open(DADOS_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def conectar_imap(email_addr, senha):
    imap = imaplib.IMAP4_SSL(IMAP_SERVER)
    imap.login(email_addr, senha)
    imap.select("INBOX")
    return imap


def extrair_pdfs_do_anexo(nome_arquivo, conteudo):
    """Recebe um anexo (nome + bytes) e devolve uma lista de (nome_pdf, bytes_pdf).
    Se o anexo ja for um .pdf, devolve ele mesmo. Se for um .zip, extrai todos
    os .pdf de dentro. Qualquer outro tipo de arquivo e ignorado."""
    if not nome_arquivo:
        return []

    nome_lower = nome_arquivo.lower()

    if nome_lower.endswith(".pdf"):
        return [(nome_arquivo, conteudo)]

    if nome_lower.endswith(".zip"):
        pdfs = []
        try:
            with zipfile.ZipFile(BytesIO(conteudo)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    if info.filename.lower().endswith(".pdf"):
                        nome_dentro_zip = os.path.basename(info.filename)
                        pdfs.append((nome_dentro_zip, z.read(info)))
        except zipfile.BadZipFile:
            pass
        return pdfs

    return []


def enviar_resposta(email_addr, senha, msg_original, arquivos_processados, assunto_resp, corpo_resp):
    resposta = EmailMessage()
    remetente_original = parseaddr(msg_original.get("From"))[1]
    resposta["From"] = email_addr
    resposta["To"] = remetente_original
    assunto_orig = msg_original.get("Subject", "")
    resposta["Subject"] = f"Re: {assunto_orig}" if assunto_orig else assunto_resp

    message_id = msg_original.get("Message-ID")
    if message_id:
        message_id_limpo = " ".join(message_id.split())
        resposta["In-Reply-To"] = message_id_limpo
        resposta["References"] = message_id_limpo

    resposta.set_content(corpo_resp)

    for nome_arquivo, conteudo in arquivos_processados:
        resposta.add_attachment(
            conteudo, maintype="application", subtype="pdf", filename=nome_arquivo
        )

    contexto = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=contexto) as smtp:
        smtp.login(email_addr, senha)
        smtp.send_message(resposta)


def enviar_alerta(email_addr, senha, destino, erros):
    """Envia um e-mail de alerta listando as faturas que deram erro.
    Nunca derruba o robo: se o alerta falhar, so registra no log."""
    try:
        alerta = EmailMessage()
        alerta["From"] = email_addr
        alerta["To"] = destino
        alerta["Subject"] = f"[CustomsFlow] ALERTA: {len(erros)} fatura(s) com erro"

        linhas = [
            "O robo do CustomsFlow encontrou erro ao processar a(s) fatura(s) abaixo.",
            "O remetente NAO recebeu o retorno dessas faturas.",
            "",
        ]
        for e in erros:
            linhas += [
                f"Arquivo:   {e['arquivo']}",
                f"Remetente: {e['remetente']}",
                f"Assunto:   {e['assunto']}",
                f"Erro:      {e['erro']}",
                "",
            ]
        linhas += [
            "Depois de corrigir, marque o e-mail original como NAO LIDO",
            "(ou peca o reenvio) e rode o workflow manualmente.",
            "",
            "Detalhe tecnico (traceback do primeiro erro):",
            erros[0]["traceback"],
        ]
        alerta.set_content("\n".join(linhas))

        contexto = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=contexto) as smtp:
            smtp.login(email_addr, senha)
            smtp.send_message(alerta)
        print(f"Alerta de erro enviado para {destino}.")
    except Exception as ex:
        print(f"!! Falha ao enviar alerta de erro: {ex}")


def main():
    email_addr = os.environ["EMAIL_ADDRESS"]
    senha = os.environ["EMAIL_APP_PASSWORD"]
    remetentes_permitidos = [
        r.strip().lower() for r in os.environ.get("ALLOWED_SENDERS", "").split(",") if r.strip()
    ]
    alerta_destino = os.environ.get("ALERT_EMAIL", "").strip() or email_addr
    assunto_resp = os.environ.get("ASSUNTO_RESPOSTA", "Faturas ajustadas - retorno automatico")
    corpo_resp = os.environ.get(
        "CORPO_RESPOSTA",
        "Segue(m) em anexo a(s) fatura(s) ja ajustada(s) conforme o destino de cada uma.\n\n"
        "Este e-mail foi enviado automaticamente pelo CustomsFlow.",
    )

    dados = carregar_dados()
    execucao = {
        "data_hora": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "processadas": [],
    }
    erros_para_alerta = []

    if not remetentes_permitidos:
        print("Nenhum remetente autorizado configurado (ALLOWED_SENDERS). Encerrando.")
        return

    imap = conectar_imap(email_addr, senha)

    for remetente in remetentes_permitidos:
        status, dados_busca = imap.search(None, f'(UNSEEN FROM "{remetente}")')
        if status != "OK":
            continue
        ids = dados_busca[0].split()

        for msg_id in ids:
            status, dados_msg = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = message_from_bytes(dados_msg[0][1])

            anexos_processados = []
            houve_pdf = False

            for parte in msg.walk():
                nome_anexo = parte.get_filename()
                if not nome_anexo:
                    continue
                conteudo_bruto = parte.get_payload(decode=True)
                if not conteudo_bruto:
                    continue

                for nome_arquivo, conteudo_original in extrair_pdfs_do_anexo(nome_anexo, conteudo_bruto):
                    houve_pdf = True

                    caminho_entrada = os.path.join("/tmp", nome_arquivo)
                    with open(caminho_entrada, "wb") as f:
                        f.write(conteudo_original)

                    nome_saida = nome_arquivo
                    caminho_saida = os.path.join("/tmp", "ajustada_" + nome_arquivo)

                    try:
                        resumo = process(caminho_entrada, caminho_saida)
                        with open(caminho_saida, "rb") as f:
                            conteudo_ajustado = f.read()
                        anexos_processados.append((nome_saida, conteudo_ajustado))
                        execucao["processadas"].append(
                            {"arquivo": nome_arquivo, "remetente": mascarar_email(remetente), "status": "ok", "resumo": resumo}
                        )
                        dados["total_processadas"] += 1
                    except Exception as e:
                        execucao["processadas"].append(
                            {"arquivo": nome_arquivo, "remetente": mascarar_email(remetente), "status": "erro", "resumo": str(e)}
                        )
                        dados["total_erros"] += 1
                        erros_para_alerta.append({
                            "arquivo": nome_arquivo,
                            "remetente": remetente,
                            "assunto": msg.get("Subject", "(sem assunto)"),
                            "erro": str(e),
                            "traceback": traceback.format_exc(),
                        })

            if houve_pdf and anexos_processados:
                enviar_resposta(email_addr, senha, msg, anexos_processados, assunto_resp, corpo_resp)

            imap.store(msg_id, "+FLAGS", "\\Seen")

    imap.logout()

    if execucao["processadas"]:
        dados["execucoes"].insert(0, execucao)
        dados["execucoes"] = dados["execucoes"][:200]
        salvar_dados(dados)
        print(f"Processado(s): {len(execucao['processadas'])} arquivo(s).")
    else:
        print("Nenhum e-mail novo encontrado.")

    if erros_para_alerta:
        enviar_alerta(email_addr, senha, alerta_destino, erros_para_alerta)


if __name__ == "__main__":
    main()
      
