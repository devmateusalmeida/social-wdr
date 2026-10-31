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
não muda nada. Cada vez que um horário é refeito (sistema/refazer.py) a
versão sobe e o botão leva o número dela: botão de versão antiga não vale,
pra ninguém aprovar sem ver a peça nova. O clique só é dado como lido no Telegram depois de gravado
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
    if not campos and not arquivos:  # pedido sem nenhum campo (getMe): formulário vazio o Telegram recusa
        pedido = urllib.request.Request(f"https://api.telegram.org/bot{token}/{metodo}")
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


def versao(itens):
    return max(i.get("versao", 1) for i in itens)


def texto_do_dia(itens):
    quando = datetime.fromisoformat(itens[0]["quando"])
    tipos = " e ".join(i["tipo"] for i in itens).capitalize()
    hora = f"{quando:%H}h" + (f"{quando:%M}" if quando.minute else "")
    refeito = f" (versão {versao(itens)}, refeita depois do ajuste)" if versao(itens) > 1 else ""
    feed = next((i for i in itens if i["tipo"] == "feed"), None)
    legenda = f"Legenda do feed:\n\n{feed['legenda']}" if feed else "(story sai sem legenda)"
    return (f"{tipos} de {DIAS[quando.weekday()]}, {quando:%d/%m}, às {hora}{refeito}\n\n{legenda}\n\n"
            "Aprovar: sai no horário. Ajustar: segura o post e pede a mudança.\n"
            "Depois do clique, a confirmação aparece aqui em até 5 min.")


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
    v = versao(itens)
    botoes = {"inline_keyboard": [[{"text": "Aprovar", "callback_data": f"a|{cliente}|{horario}|{v}"},
                                   {"text": "Ajustar", "callback_data": f"j|{cliente}|{horario}|{v}"}]]}
    return pedir("sendMessage", chat_id=grupo, text=texto_do_dia(itens), reply_markup=botoes)["message_id"]


def ler_cliques(salvar, agora):
    """Roda no publicador, antes de publicar. salvar é o do publicar.py
    (grava um item no GitHub). Devolve avisos pra quem cuida do robô"""
    try:
        atualizacoes = pedir("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    except ErroTelegram as e:
        return [f"Telegram: não consegui ler os cliques ({e}). Nada foi aprovado nesta volta."]
    print(f"Telegram: {len(atualizacoes)} novidade(s)")
    avisos, lido = [], None
    for u in atualizacoes:
        # número e tipo de cada novidade, pra achar clique perdido (número em sequência)
        print(f"Telegram: novidade {u['update_id']} "
              f"({'clique' if 'callback_query' in u else ', '.join(k for k in u if k != 'update_id')})")
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
    partes = clique.get("data", "").split("|")
    if len(partes) == 2 and partes[0] == "t":
        return clique_de_teste(partes[1], msg, agora, responder, avisos)
    try:
        if len(partes) == 3:
            partes.append("1")  # mensagem de antes da versão no botão
        acao, cliente, horario, versao_botao = partes
        versao_botao = int(versao_botao)
        cfg = config(cliente)
    except (ValueError, ErroTelegram):
        print(f"Telegram: clique com dado desconhecido ({clique.get('data')!r}), ignorado")
        responder()
        return True
    if msg.get("chat", {}).get("id") != cfg["grupo"] or acao not in ("a", "j"):
        print(f"Telegram: clique de outro grupo ({msg.get('chat', {}).get('id')}), ignorado")
        responder()
        return True
    botao = "Aprovar" if acao == "a" else "Ajustar"
    nome = cfg["aprovadores"].get(str(clique["from"]["id"]))
    if not nome:
        print(f"Telegram: {botao} em {cliente} {horario}: pessoa fora da lista, não valeu")
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
    do_horario = [i for i in fila["itens"] if i["quando"][:16] == horario]
    if do_horario and versao_botao != versao(do_horario):
        print(f"Telegram: {botao} em {cliente} {horario}: botão da versão {versao_botao}, "
              f"a atual é {versao(do_horario)}, não valeu")
        responder("Esse botão é de uma versão antiga. Use a mensagem mais nova.")
        base = msg.get("text", "").rsplit("\n\n", 1)[0]
        try:
            pedir("editMessageText", chat_id=cfg["grupo"], message_id=msg["message_id"],
                  text=f"{base}\n\nVERSÃO ANTIGA. Vale a mensagem mais nova deste horário.",
                  reply_markup={"inline_keyboard": []})
        except ErroTelegram:
            pass
        return True
    mudou = False
    for item in do_horario:
        if item["situacao"] in de:
            if not salvar(cliente, item["id"], para, "aprovado" if acao == "a" else "ajuste pedido",
                          esperado=item["situacao"]):
                return False
            mudou = True
    situacoes = sorted({i["situacao"] for i in fila["itens"] if i["quando"][:16] == horario})
    print(f"Telegram: {botao} em {cliente} {horario}: "
          f"{'gravado' if mudou else 'nada mudou'} (antes: {', '.join(situacoes) or 'post não achado'})")
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


def clique_de_teste(cliente, msg, agora, responder, avisos):
    """Botão de teste (sistema/telegram.py testar): mostra no grupo que o clique chegou no robô"""
    try:
        grupo = config(cliente)["grupo"]
    except ErroTelegram:
        grupo = None
    if msg.get("chat", {}).get("id") != grupo:
        print(f"Telegram: clique de teste de outro grupo ({msg.get('chat', {}).get('id')}), ignorado")
        responder()
        return True
    print(f"Telegram: clique de teste em {cliente}: recebido")
    responder("Recebido")
    try:
        pedir("editMessageText", chat_id=grupo, message_id=msg["message_id"],
              text=f"Clique de teste recebido pelo robô às {agora:%H:%M}. O grupo está funcionando.",
              reply_markup={"inline_keyboard": []})
    except ErroTelegram as e:
        avisos.append(f"Telegram: recebi o clique de teste de {cliente}, mas não consegui mudar a mensagem ({e})")
    return True


def avisar(grupo, texto, responder_a=None, avisos=None):
    """Manda uma mensagem no grupo, de preferência como resposta à mensagem do post"""
    extra = {"reply_parameters": {"message_id": responder_a, "allow_sending_without_reply": True}} if responder_a else {}
    try:
        pedir("sendMessage", chat_id=grupo, text=texto, **extra)
    except ErroTelegram as e:
        if avisos is not None:
            avisos.append(f"Telegram: não consegui avisar no grupo ({e})")
