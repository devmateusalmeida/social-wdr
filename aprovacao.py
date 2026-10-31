"""Aprovação dos posts pelo Telegram (passo 7).

Um bot só pra WDR (token no segredo TELEGRAM_TOKEN), um grupo por cliente.
aprovacao.json diz o grupo de cada cliente e quem pode aprovar (número da
pessoa no Telegram: nome). Os números não são segredo; sem o token ninguém
faz nada com eles.

Cada horário vai pro grupo como as artes (feed e story juntos) e uma mensagem
com a legenda e os botões Aprovar e Ajustar. O post fica "esperando
aprovação" na fila e o publicador não mexe nele até alguém aprovar.
"""
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ESPERANDO = "esperando aprovação"
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


class ErroTelegram(Exception):
    pass


def config(cliente):
    tudo = json.loads((RAIZ / "aprovacao.json").read_text(encoding="utf-8"))
    if cliente not in tudo:
        raise ErroTelegram(f"{cliente} sem grupo de aprovação no aprovacao.json")
    return tudo[cliente]


def pedir(metodo, arquivos=None, **params):
    """Chama a API do bot. Com arquivos, manda as imagens do PC junto (não
    depende do Pages já estar no ar). Erro vira ErroTelegram, sem o token"""
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise ErroTelegram("falta o TELEGRAM_TOKEN")
    campos = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
              for k, v in params.items()}
    limite = uuid.uuid4().hex
    corpo = b""
    for k, v in campos.items():
        corpo += f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, caminho in (arquivos or {}).items():
        corpo += (f'--{limite}\r\nContent-Disposition: form-data; name="{k}"; filename="{Path(caminho).name}"\r\n'
                  f"Content-Type: image/jpeg\r\n\r\n").encode() + Path(caminho).read_bytes() + b"\r\n"
    corpo += f"--{limite}--\r\n".encode()
    pedido = urllib.request.Request(f"https://api.telegram.org/bot{token}/{metodo}", data=corpo,
                                    headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
    try:
        with urllib.request.urlopen(pedido, timeout=60) as r:
            resposta = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            resposta = json.load(e)
        except ValueError:
            raise ErroTelegram(f"Telegram recusou ({e.code})") from None
    except Exception as e:
        raise ErroTelegram("sem conexão com o Telegram: " + str(e).replace(token, "***")) from None
    if not resposta.get("ok"):
        raise ErroTelegram(f"Telegram recusou ({resposta.get('error_code')}): {resposta.get('description')}")
    return resposta["result"]


def texto_do_dia(itens):
    quando = datetime.fromisoformat(itens[0]["quando"])
    tipos = " e ".join(i["tipo"] for i in itens).capitalize()
    feed = next((i for i in itens if i["tipo"] == "feed"), None)
    legenda = f"Legenda do feed:\n\n{feed['legenda']}" if feed else "(story sai sem legenda)"
    return (f"{tipos} de {DIAS[quando.weekday()]}, {quando:%d/%m}, às {quando:%H}h\n\n{legenda}\n\n"
            "Aprovar: sai no horário. Ajustar: segura o post e pede a mudança.\n"
            "Depois do clique, a confirmação aparece aqui em até 15 min.")


def enviar_dia(cliente, itens, artes):
    """Manda as artes de um dia e a mensagem com os botões. artes: id do item -> arquivo no PC.
    Devolve o número da mensagem dos botões"""
    grupo = config(cliente)["grupo"]
    horario = itens[0]["quando"][:16]  # dia e hora: dois horários no mesmo dia aprovam separado
    if len(itens) == 1:
        pedir("sendPhoto", {"foto": artes[itens[0]["id"]]}, chat_id=grupo, photo="attach://foto")
    else:
        pedir("sendMediaGroup", {f"foto{n}": artes[i["id"]] for n, i in enumerate(itens)}, chat_id=grupo,
              media=[{"type": "photo", "media": f"attach://foto{n}"} for n in range(len(itens))])
    botoes = {"inline_keyboard": [[{"text": "Aprovar", "callback_data": f"a|{cliente}|{horario}"},
                                   {"text": "Ajustar", "callback_data": f"j|{cliente}|{horario}"}]]}
    return pedir("sendMessage", chat_id=grupo, text=texto_do_dia(itens), reply_markup=botoes)["message_id"]
