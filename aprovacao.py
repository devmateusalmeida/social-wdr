"""Aprovação dos posts pelo Telegram (passo 7).

Um bot só pra WDR (token no segredo TELEGRAM_TOKEN), um grupo por cliente.
aprovacao.json diz o grupo de cada cliente e quem pode aprovar (número da
pessoa no Telegram: nome). Os números não são segredo; sem o token ninguém
faz nada com eles.

Cada horário vai pro grupo como as artes (feed e story juntos) e uma mensagem
com a legenda e os botões Aprovar e Ajustar. O post fica "esperando
aprovação" na fila e o publicador não mexe nele até alguém aprovar.

A cada volta o publicador lê os cliques (o Telegram guarda 24 h) antes de
publicar:
  Aprovar: esperando aprovação -> aguardando (sai no horário)
  Ajustar: esperando aprovação ou aguardando -> ajuste (não sai até o
           William refazer e mandar de novo)
Só vale clique de quem está na lista do cliente, no grupo do cliente. A
mensagem do grupo perde os botões e mostra quem decidiu. Clique repetido
não muda nada. O clique só é dado como lido no Telegram depois de gravado
na fila; se a volta cair no meio, a próxima lê de novo sem estragar nada.
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


def ler_cliques(salvar, agora):
    """Roda no publicador, antes de publicar. salvar é o do publicar.py
    (grava um item no GitHub). Devolve avisos pra quem cuida do robô"""
    try:
        atualizacoes = pedir("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    except ErroTelegram as e:
        return [f"Telegram: não consegui ler os cliques ({e}). Nada foi aprovado nesta volta."]
    avisos, lido = [], None
    for u in atualizacoes:
        if not tratar(u, salvar, agora, avisos):
            # não gravou: fica sem marcar como lido e a próxima volta tenta de novo
            avisos.append("Telegram: um clique não foi gravado na fila, tento de novo na próxima volta "
                          "(o Telegram guarda o clique por 24 h)")
            break
        lido = u["update_id"]
    if lido is not None:
        try:
            pedir("getUpdates", offset=lido + 1, limit=1, timeout=0)  # marca como lido até aqui
        except ErroTelegram:
            pass  # a próxima volta lê de novo, e clique repetido não muda nada
    return avisos


def tratar(u, salvar, agora, avisos):
    """Um clique ou aviso do Telegram. Devolve False se não conseguiu gravar na fila"""
    msg = u.get("message")
    if msg:
        if msg.get("migrate_to_chat_id"):
            avisos.append(f"O grupo {msg['chat'].get('title')} virou supergrupo: no aprovacao.json, trocar "
                          f"{msg['chat']['id']} por {msg['migrate_to_chat_id']}")
        return True
    clique = u.get("callback_query")
    if not clique:
        return True

    def responder(texto=None):
        try:
            pedir("answerCallbackQuery", callback_query_id=clique["id"], **({"text": texto} if texto else {}))
        except ErroTelegram:
            pass  # clique antigo já não aceita resposta; a mensagem do grupo é que avisa

    msg = clique.get("message") or {}
    try:
        acao, cliente, horario = clique.get("data", "").split("|")
        cfg = config(cliente)
    except (ValueError, ErroTelegram):
        responder()
        return True
    if msg.get("chat", {}).get("id") != cfg["grupo"] or acao not in ("a", "j"):
        responder()
        return True
    botao = "Aprovar" if acao == "a" else "Ajustar"
    nome = cfg["aprovadores"].get(str(clique["from"]["id"]))
    if not nome:
        responder("Só quem está na lista de aprovadores pode clicar aqui.")
        avisar(cfg["grupo"], f"{clique['from'].get('first_name', 'Alguém')} clicou em {botao}, mas não está na lista "
                             f"de aprovadores deste cliente, então o clique não valeu. Pra incluir, o William põe "
                             f"o número {clique['from']['id']} no aprovacao.json.", msg.get("message_id"), avisos)
        return True

    if acao == "a":
        de, para = (ESPERANDO,), {"situacao": "aguardando", "aprovado_por": nome, "aprovado_em": agora.isoformat()}
    else:
        de, para = (ESPERANDO, "aguardando"), {"situacao": "ajuste", "ajuste_pedido_por": nome,
                                               "ajuste_pedido_em": agora.isoformat()}
    fila = json.loads((RAIZ / "fila" / f"{cliente}.json").read_text(encoding="utf-8"))
    mudou = False
    for item in fila["itens"]:
        if item["quando"][:16] == horario and item["situacao"] in de:
            if not salvar(cliente, item["id"], para, "aprovado" if acao == "a" else "ajuste pedido",
                          esperado=item["situacao"]):
                return False
            mudou = True
    responder("Recebido")
    if mudou:
        if acao == "a":
            situacao = f"APROVADO por {nome}. Sai no horário."
        else:
            situacao = (f"AJUSTE pedido por {nome}. Escreva aqui no grupo o que mudar; o William refaz "
                        f"e manda de novo pra aprovar.")
        base = msg.get("text", "").rsplit("\n\n", 1)[0]
        try:
            pedir("editMessageText", chat_id=cfg["grupo"], message_id=msg["message_id"],
                  text=f"{base}\n\n{situacao}", reply_markup={"inline_keyboard": []})
        except ErroTelegram as e:
            avisos.append(f"{cliente} {horario}: gravado na fila, mas não consegui mudar a mensagem do grupo ({e})")
    return True


def avisar(grupo, texto, responder_a=None, avisos=None):
    """Manda uma mensagem no grupo, de preferência como resposta à mensagem do post"""
    extra = {"reply_parameters": {"message_id": responder_a, "allow_sending_without_reply": True}} if responder_a else {}
    try:
        pedir("sendMessage", chat_id=grupo, text=texto, **extra)
    except ErroTelegram as e:
        if avisos is not None:
            avisos.append(f"Telegram: não consegui avisar no grupo ({e})")
