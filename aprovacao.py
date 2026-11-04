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
mensagem do grupo perde os botões e mostra quem decidiu (os lembretes daquele
horário também perdem). Clique em horário já decidido não muda nada e mostra
a situação atual na mensagem. Cada vez que um horário é refeito
(sistema/refazer.py) a versão sobe e o botão leva o número dela: botão de
versão antiga não vale, pra ninguém aprovar sem ver a peça nova. O clique só
é dado como lido depois de gravado na fila; se a volta cair no meio, a
próxima lê de novo sem estragar nada.

Prazo (cuidar_dos_horarios, a cada volta, passo 7.5 parte 3):
  24 h e 2 h antes, sem aprovação: lembrete no grupo, respondendo à mensagem
  original, com os botões. Não sai se o post chegou no grupo menos de 3 h
  antes da hora do lembrete.
  2 h antes, sem aprovação ou em ajuste: aviso técnico (avisos.py).
  Aprovação depois do horário vale até 2 h (ATRASO_MAXIMO) e sai na hora.
  Passou disso sem aprovação: o post vira "passou do horário", os botões
  somem, o grupo é avisado e o aviso técnico leva o comando pra remarcar
  (refazer.py --para).

Caixa do Cloudflare (cloudflare/caixa.js, endereço em "_caixa" no
aprovacao.json, chave no segredo TELEGRAM_CAIXA_CHAVE): o Telegram só
entrega clique pra quem está lendo naquele momento, então com o robô lendo a
cada 5 min quase todo clique se perdia. Com a caixa, o Telegram entrega na
hora pro Worker, que guarda, responde "Recebido" e acorda o robô; o robô lê
a caixa no lugar do getUpdates. Sem "_caixa", volta ao jeito antigo (VPS).
"""
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ESPERANDO = "esperando aprovação"
AJUSTE = "ajuste"
PASSOU = "passou do horário"
ATRASO_MAXIMO = timedelta(hours=2)  # aprovação vale até 2 h depois do horário; o publicar.py usa o mesmo
CHEGOU_HA_POUCO = timedelta(hours=3)  # post que chegou no grupo há menos que isso não ganha lembrete
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


class ErroTelegram(Exception):
    pass


def aviso(codigo, texto, tipo="evento"):
    """Aviso pra quem cuida do robô. avisos.py decide quando mandar pelo tipo:
    evento (uma vez), problema (repete até resolver) ou passageiro (conexão)"""
    return {"codigo": codigo, "texto": texto, "tipo": tipo}


def config(cliente):
    tudo = json.loads((RAIZ / "aprovacao.json").read_text(encoding="utf-8"))
    if cliente not in tudo:
        raise ErroTelegram(f"{cliente} sem grupo de aprovação no aprovacao.json")
    return tudo[cliente]


def caixa():
    """Endereço da caixa do Cloudflare, ou None se o robô lê direto do Telegram"""
    tudo = json.loads((RAIZ / "aprovacao.json").read_text(encoding="utf-8"))
    return tudo.get("_caixa", "").rstrip("/") or None


def pedir_caixa(caminho, corpo=None):
    """GET /caixa (novidades não lidas; ?tudo=1 inclui as lidas nas últimas 24 h)
    ou POST /lido com {"ids": [...]}. Erro vira ErroTelegram, sem a chave"""
    chave = os.environ.get("TELEGRAM_CAIXA_CHAVE", "")
    if not chave:
        raise ErroTelegram("falta o TELEGRAM_CAIXA_CHAVE")
    pedido = urllib.request.Request(
        caixa() + caminho, data=None if corpo is None else json.dumps(corpo).encode(),
        headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json",
                 "User-Agent": "publicador-wdr"})
    try:
        with urllib.request.urlopen(pedido, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ErroTelegram(f"a caixa do Cloudflare recusou ({e.code})") from None
    except Exception as e:
        raise ErroTelegram("sem conexão com a caixa do Cloudflare: " + str(e).replace(chave, "***")) from None


def conferir_entrega(agora):
    """Com a caixa ligada, pergunta ao Telegram se ele está conseguindo entregar nela
    (o mesmo que o telegram.py caixa mostra). Devolve avisos"""
    na_caixa = caixa()
    if not na_caixa:
        return []
    try:
        info = pedir("getWebhookInfo")
    except ErroTelegram as e:
        return [aviso("telegram-info", f"Telegram: não consegui perguntar se a entrega na caixa está ok ({e})",
                      "passageiro")]
    if info.get("url") != na_caixa + "/telegram":
        return [aviso("entrega-desligada", "Telegram: a entrega na caixa do Cloudflare está desligada, então os "
                                           "cliques de aprovação não chegam no robô. Ligar de novo no PC: "
                                           "python sistema/telegram.py caixa ligar", "problema")]
    pendentes, erro = info.get("pending_update_count", 0), info.get("last_error_date", 0)
    print(f"Telegram: entrega na caixa ligada, {pendentes} esperando entrega")
    if pendentes and erro and agora.timestamp() - erro < 30 * 60:
        return [aviso("entrega-erro", f"Telegram: não está conseguindo entregar na caixa do Cloudflare "
                                      f"({info.get('last_error_message')}), {pendentes} clique(s) esperando. Conferir "
                                      f"no PC com python sistema/telegram.py caixa e olhar o Worker caixa-aprovacao no "
                                      f"painel do Cloudflare", "passageiro")]
    return []


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


def hora(quando):
    return f"{quando:%H}h" + (f"{quando:%M}" if quando.minute else "")


def nome_do_horario(itens):
    """Ex.: feed e story de terça, 10/11, às 19h"""
    quando = datetime.fromisoformat(itens[0]["quando"])
    tipos = " e ".join(i["tipo"] for i in itens)
    return f"{tipos} de {DIAS[quando.weekday()]}, {quando:%d/%m}, às {hora(quando)}"


def limite(itens):
    """Até quando a aprovação vale, ex.: 21h de 10/11"""
    fim = datetime.fromisoformat(itens[0]["quando"]) + ATRASO_MAXIMO
    return f"{hora(fim)} de {fim:%d/%m}"


def o_que_mudou(itens):
    """Por que o horário tem versão nova, ex.: refeita depois do ajuste e remarcada. O refazer.py anota em
    "motivo_da_versao" (ajuste, peça nova, remarcado). Ficha de antes disso: com ajuste pedido foi refeita
    depois do ajuste; sem, só pode ter sido remarcada (sem --para o refazer.py só aceita post em ajuste)"""
    motivo = next((i["motivo_da_versao"] for i in itens if "motivo_da_versao" in i), None)
    if motivo is None:
        motivo = ["ajuste", "peça nova"] if any(i.get("ajuste_pedido_por") for i in itens) else ["remarcado"]
    partes = []
    if "peça nova" in motivo:
        partes.append("refeita depois do ajuste" if "ajuste" in motivo else "refeita")
    if "remarcado" in motivo:
        partes.append("remarcada")
    return " e ".join(partes) or "refeita"


def texto_do_dia(itens):
    refeito = f" (versão {versao(itens)}, {o_que_mudou(itens)})" if versao(itens) > 1 else ""
    feed = next((i for i in itens if i["tipo"] == "feed"), None)
    legenda = f"Legenda do feed:\n\n{feed['legenda']}" if feed else "(story sai sem legenda)"
    return (f"{nome_do_horario(itens).capitalize()}{refeito}\n\n{legenda}\n\n"
            "Aprovar: sai no horário. Ajustar: segura o post e pede a mudança.\n"
            "Depois do clique, a confirmação aparece aqui em até 1 min.")


def botoes(cliente, itens):
    horario = itens[0]["quando"][:16]  # dia e hora: dois horários no mesmo dia aprovam separado
    v = versao(itens)
    return {"inline_keyboard": [[{"text": "Aprovar", "callback_data": f"a|{cliente}|{horario}|{v}"},
                                 {"text": "Ajustar", "callback_data": f"j|{cliente}|{horario}|{v}"}]]}


def enviar_dia(cliente, itens, artes):
    """Manda as artes de um dia e a mensagem com os botões. artes: id do item -> arquivo no PC.
    Devolve o número da mensagem dos botões"""
    grupo = config(cliente)["grupo"]
    if len(itens) == 1:
        pedir("sendPhoto", {"foto": artes[itens[0]["id"]]}, chat_id=grupo, photo="attach://foto")
    else:
        pedir("sendMediaGroup", {f"foto{n}": artes[i["id"]] for n, i in enumerate(itens)}, chat_id=grupo,
              media=[{"type": "photo", "media": f"attach://foto{n}"} for n in range(len(itens))])
    return pedir("sendMessage", chat_id=grupo, text=texto_do_dia(itens), reply_markup=botoes(cliente, itens))["message_id"]


def mensagem_original(itens):
    """Número da mensagem com os botões no grupo (o PC anota depois de mandar)"""
    return next((i["mensagem"] for i in itens if i.get("mensagem")), None)


def editar(grupo, numero, texto):
    """Troca o texto de uma mensagem do grupo e tira os botões. Devolve o erro, ou None"""
    try:
        pedir("editMessageText", chat_id=grupo, message_id=numero, text=texto, reply_markup={"inline_keyboard": []})
    except ErroTelegram as e:
        return e
    return None


def fechar(grupo, itens, situacao, exceto=None):
    """Tira os botões da mensagem original e dos lembretes de um horário, menos a que já foi editada"""
    original = mensagem_original(itens)
    if original and original != exceto:
        editar(grupo, original, texto_do_dia(itens).rsplit("\n\n", 1)[0] + f"\n\n{situacao}")
    for numero in {n for i in itens for n in i.get("lembretes", {}).values() if n} - {exceto, original}:
        try:
            pedir("editMessageReplyMarkup", chat_id=grupo, message_id=numero, reply_markup={"inline_keyboard": []})
        except ErroTelegram:
            pass  # lembrete apagado ou já sem botão


def situacao_atual(itens, agora):
    """O que mostrar quando clicam num horário que já foi decidido"""
    s = {i["situacao"] for i in itens}
    quando = datetime.fromisoformat(itens[0]["quando"])
    quem = lambda campo: next((i[campo] for i in itens if i.get(campo)), "alguém")  # noqa: E731
    if PASSOU in s or (ESPERANDO in s and agora > quando + ATRASO_MAXIMO):
        return "PASSOU DO HORÁRIO, não saiu. O William pode remarcar pra outro dia."
    if AJUSTE in s:
        return f"AJUSTE já pedido por {quem('ajuste_pedido_por')}. O William refaz e manda de novo pra aprovar."
    if s & {"atrasado", "erro"}:
        return "NÃO SAIU por um problema na publicação. O William já foi avisado."
    if s == {"publicado"}:
        return "JÁ PUBLICADO."
    if s & {"publicando", "publicado"}:
        return "SAINDO AGORA."
    if "aguardando" in s:
        return f"Já APROVADO por {quem('aprovado_por')}. " + ("Sai no horário." if agora < quando else "Saindo agora.")
    return None


def ler_cliques(salvar, agora):
    """Roda no publicador, antes de publicar. salvar é o do publicar.py
    (grava um item no GitHub). Devolve avisos pra quem cuida do robô"""
    na_caixa = caixa()
    try:
        if na_caixa:
            atualizacoes = pedir_caixa("/caixa")
        else:
            atualizacoes = pedir("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    except ErroTelegram as e:
        return [aviso("telegram-ler", f"Telegram: não consegui ler os cliques ({e}). Enquanto isso, "
                                      f"nenhum clique de aprovação é gravado.", "passageiro")]
    print(f"Telegram: {len(atualizacoes)} novidade(s){' na caixa do Cloudflare' if na_caixa else ''}")
    avisos, lidos = [], []
    for u in atualizacoes:
        # número e tipo de cada novidade, pra achar clique perdido (número em sequência)
        print(f"Telegram: novidade {u['update_id']} "
              f"({'clique' if 'callback_query' in u else ', '.join(k for k in u if k != 'update_id')})")
        if not tratar(u, salvar, agora, avisos):
            # não gravou: fica sem marcar como lido e a próxima volta tenta de novo
            avisos.append(aviso("clique-nao-gravado", "Telegram: um clique não foi gravado na fila. Tento de "
                                "novo a cada volta (o clique fica guardado por 24 h)", "passageiro"))
            break
        lidos.append(u["update_id"])
    if lidos:
        try:
            if na_caixa:
                pedir_caixa("/lido", {"ids": lidos})
            else:
                pedir("getUpdates", offset=lidos[-1] + 1, limit=1, timeout=0)  # marca como lido até aqui
        except ErroTelegram:
            pass  # a próxima volta lê de novo, e clique repetido não muda nada
    return avisos


def tratar(u, salvar, agora, avisos):
    """Um clique ou aviso do Telegram. Devolve False se não conseguiu gravar na fila"""
    msg = u.get("message")
    if msg:
        if msg.get("migrate_to_chat_id"):
            avisos.append(aviso(f"supergrupo|{msg['chat']['id']}",
                                f"O grupo {msg['chat'].get('title')} virou supergrupo: no aprovacao.json, trocar "
                                f"{msg['chat']['id']} por {msg['migrate_to_chat_id']}"))
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
        de, para = (ESPERANDO, "aguardando"), {"situacao": AJUSTE, "ajuste_pedido_por": nome,
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
    quando = datetime.fromisoformat(do_horario[0]["quando"]) if do_horario else None
    passou = quando and agora > quando + ATRASO_MAXIMO  # depois de 2 h o clique não vale mais
    mudou = False
    for item in do_horario:
        if item["situacao"] in de and not passou:
            if not salvar(cliente, item["id"], para, "aprovado" if acao == "a" else "ajuste pedido",
                          esperado=item["situacao"]):
                return False
            mudou = True
    situacoes = sorted({i["situacao"] for i in do_horario})
    print(f"Telegram: {botao} em {cliente} {horario}: "
          f"{'gravado' if mudou else 'passou do horário' if passou else 'nada mudou'} "
          f"(antes: {', '.join(situacoes) or 'post não achado'})")
    responder("Recebido")
    base = msg.get("text", "").rsplit("\n\n", 1)[0]
    if mudou:
        if acao == "a":
            situacao = f"APROVADO por {nome}. " + ("Sai no horário." if agora < quando else
                                                   "Saindo agora, depois do horário.")
        else:
            situacao = (f"AJUSTE pedido por {nome}. Escreva aqui no grupo o que mudar; o William refaz "
                        f"e manda de novo pra aprovar.")
        erro = editar(cfg["grupo"], msg["message_id"], f"{base}\n\n{situacao}")
        if erro:
            avisos.append(aviso(f"editar|{cliente}|{horario}",
                                f"{cliente} {horario}: gravado na fila, mas não consegui mudar a mensagem do grupo ({erro})"))
        fechar(cfg["grupo"], do_horario, situacao, exceto=msg["message_id"])  # mensagem original e lembretes
    elif do_horario:
        atual = situacao_atual(do_horario, agora)
        if atual:  # clique em horário já decidido (lembrete e original têm botões iguais)
            editar(cfg["grupo"], msg["message_id"], f"{base}\n\n{atual}")
    return True


def cuidar_dos_horarios(cliente, salvar, agora):
    """Roda no publicador depois de ler os cliques: lembretes no grupo, aviso técnico 2 h antes e
    "passou do horário" depois de 2 h sem aprovação. Devolve avisos pra quem cuida do robô"""
    try:
        cfg = config(cliente)
    except ErroTelegram:
        return []  # cliente sem grupo de aprovação
    arq = RAIZ / "fila" / f"{cliente}.json"
    if not arq.exists():
        return []
    por_horario = {}
    for i in json.loads(arq.read_text(encoding="utf-8"))["itens"]:
        if i["situacao"] in (ESPERANDO, AJUSTE):
            por_horario.setdefault(i["quando"][:16], []).append(i)
    avisos = []
    for horario, itens in sorted(por_horario.items()):
        quando = datetime.fromisoformat(itens[0]["quando"])
        if agora > quando + ATRASO_MAXIMO:
            avisos += expirar(cliente, cfg["grupo"], horario, itens, salvar, agora)
        elif agora >= quando - timedelta(hours=24):
            avisos += lembrar(cliente, cfg["grupo"], horario, itens, salvar, agora)
    return avisos


def lembrar(cliente, grupo, horario, itens, salvar, agora):
    quando = datetime.fromisoformat(itens[0]["quando"])
    qual = "2h" if agora >= quando - timedelta(hours=2) else "24h"
    marco = quando - timedelta(hours=int(qual[:-1]))
    chegou = max((datetime.fromisoformat(i["enviado_em"]) for i in itens if i.get("enviado_em")), default=None)
    if chegou and chegou > marco - CHEGOU_HA_POUCO:
        return []  # chegou no grupo há pouco: sem lembrete nem aviso
    esperando = [i for i in itens if i["situacao"] == ESPERANDO]
    avisos = []
    if qual == "2h":
        if esperando:
            falta = f"está sem aprovação. Chamar o cliente no grupo. Sem aprovação até as {limite(itens)}, não sai."
        else:
            falta = (f"teve ajuste pedido e ainda não foi refeito (python sistema/refazer.py {cliente} {horario}). "
                     f"Passando das {limite(itens)}, não sai.")
        avisos.append(aviso(f"sem-aprovacao|{cliente}|{horario}|v{versao(itens)}",
                            f"{cliente}: o {nome_do_horario(itens)} {falta}"))
    feitos = {k: v for i in itens for k, v in i.get("lembretes", {}).items()}
    if not esperando or qual in feitos:
        return avisos  # em ajuste o grupo não tem o que clicar
    original = mensagem_original(itens)
    extra = {"reply_parameters": {"message_id": original, "allow_sending_without_reply": True}} if original else {}
    try:
        numero = pedir("sendMessage", chat_id=grupo, reply_markup=botoes(cliente, itens), **extra,
                       text=f"Lembrete: falta aprovar o {nome_do_horario(itens)}. Sem aprovação até as "
                            f"{limite(itens)}, não sai.\n\nAprovar: sai no horário. Ajustar: segura o post e "
                            f"pede a mudança.")["message_id"]
    except ErroTelegram as e:
        return avisos + [aviso(f"lembrete|{cliente}|{horario}",
                               f"{cliente} {horario}: não consegui mandar o lembrete no grupo ({e})", "passageiro")]
    feitos = {"24h": 0, **feitos, qual: numero}  # 0: o de 24 h não saiu, já é hora do de 2 h
    for i in esperando:
        salvar(cliente, i["id"], {"lembretes": feitos}, f"lembrete de {qual} no grupo", esperado=ESPERANDO)
    print(f"Telegram: lembrete de {qual} de {cliente} {horario} no grupo")
    return avisos


def expirar(cliente, grupo, horario, itens, salvar, agora):
    em_ajuste = any(i["situacao"] == AJUSTE for i in itens)
    mudou = False
    for i in itens:
        if salvar(cliente, i["id"], {"situacao": PASSOU, "passou_em": agora.isoformat()},
                  "passou do horário sem aprovação", esperado=i["situacao"]):
            mudou = True
    if not mudou:
        return []  # alguém clicou no meio ou o GitHub recusou: a próxima volta vê de novo
    motivo = "com o ajuste sem refazer" if em_ajuste else "sem aprovação"
    print(f"Telegram: {cliente} {horario} passou do horário {motivo}")
    if not em_ajuste:  # em ajuste a mensagem já está sem botão e mostra quem pediu
        fechar(grupo, itens, f"PASSOU DO HORÁRIO {motivo.upper()}, não saiu. O William pode remarcar pra outro dia.")
    avisos = []
    avisar(grupo, f"O {nome_do_horario(itens)} não saiu: passaram 2 h do horário {motivo}. O William combina "
                  f"aqui um novo dia e hora.", mensagem_original(itens), avisos)
    avisos.append(aviso(f"passou|{cliente}|{horario}|v{versao(itens)}",
                        f"{cliente}: o {nome_do_horario(itens)} não saiu, passaram 2 h do horário {motivo}. "
                        f"Pra remarcar: python sistema/refazer.py {cliente} {horario} --para AAAA-MM-DDTHH:MM"))
    return avisos


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
        avisos.append(aviso(f"teste|{cliente}",
                            f"Telegram: recebi o clique de teste de {cliente}, mas não consegui mudar a mensagem ({e})"))
    return True


def avisar(grupo, texto, responder_a=None, avisos=None):
    """Manda uma mensagem no grupo, de preferência como resposta à mensagem do post"""
    extra = {"reply_parameters": {"message_id": responder_a, "allow_sending_without_reply": True}} if responder_a else {}
    try:
        pedir("sendMessage", chat_id=grupo, text=texto, **extra)
    except ErroTelegram as e:
        if avisos is not None:
            avisos.append(aviso(f"avisar-grupo|{grupo}", f"Telegram: não consegui avisar no grupo {grupo} ({e})"))
