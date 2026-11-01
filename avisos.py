"""Avisos pra quem cuida do robô, no Telegram (passo 7.5).

Vão pra conversa privada de cada pessoa da lista "_avisos" no
aprovacao.json (número da pessoa no Telegram). A pessoa precisa ter mandado
uma mensagem pro bot antes, senão o Telegram não deixa o bot falar com ela.
Aviso técnico nunca vai pro grupo do cliente.

Cada aviso tem um código (o que é e de onde) e um tipo:
  evento      acontece uma vez (post que desistiu, post atrasado): avisa uma vez
  problema    continua até alguém resolver (segredo faltando): avisa, repete a
              cada 12 h e manda "resolvido" quando some
  passageiro  falha de conexão: igual ao problema, mas só avisa se continuar
              8 min depois, pra não avisar soluço

avisos.json guarda, por código, quando o robô viu e quando avisou (sem nome
de ninguém: o repositório é público). Só grava quando algo muda.

A volta fica vermelha (e o GitHub manda e-mail) só quando sai aviso novo ou
repetido, ou quando o Telegram recusou o aviso. Assim o e-mail é a reserva
do Telegram, no mesmo ritmo, e não chega um a cada 5 min.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import aprovacao

RAIZ = Path(__file__).resolve().parent
ARQUIVO = RAIZ / "avisos.json"
REPETIR = timedelta(hours=12)
ESPERAR_PASSAGEIRO = timedelta(minutes=8)
GUARDAR_EVENTO = timedelta(days=7)  # evento igual dentro desse prazo não avisa de novo
LIMITE = 4000  # o Telegram aceita até 4096 letras por mensagem


def normalizar(a):
    """Aviso antigo (só texto) vira evento, com o próprio texto de código"""
    return a if isinstance(a, dict) else aprovacao.aviso(a[:120], a)


def pessoas():
    tudo = json.loads((RAIZ / "aprovacao.json").read_text(encoding="utf-8"))
    return tudo.get("_avisos", [])


def link_da_volta():
    if os.environ.get("GITHUB_RUN_ID"):
        return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    return None


def montar(novos, repetidos, resolvidos):
    partes = []
    if novos:
        partes.append("Robô de publicação, precisa de atenção:\n" + "\n".join(f"- {t}" for t in novos))
    if repetidos:
        partes.append("Continua sem resolver:\n" + "\n".join(f"- {t}" for t in repetidos))
    if resolvidos:
        partes.append("Resolvido:\n" + "\n".join(f"- {t}" for t in resolvidos))
    link = link_da_volta()
    if link:
        partes.append(f"Registro desta volta: {link}")
    texto = "\n\n".join(partes)
    return texto if len(texto) <= LIMITE else texto[:LIMITE - 20] + "\n(... continua no registro)"


def mandar(texto):
    """Manda pra cada pessoa da lista. Devolve quantas recusaram"""
    lista = pessoas()
    if not lista:
        print('Avisos: ninguém em "_avisos" no aprovacao.json, fica só o e-mail do GitHub')
        return 0
    falhas = 0
    for numero in lista:
        try:
            aprovacao.pedir("sendMessage", chat_id=numero, text=texto)
        except aprovacao.ErroTelegram as e:
            falhas += 1
            # sem o número no registro: o registro é público
            print(f"Avisos: o Telegram recusou mandar pra uma pessoa da lista ({e}). Ela já mandou "
                  f"mensagem pro bot no privado?")
    return falhas


def cuidar(lista, agora, salvar_json):
    """lista: avisos desta volta. salvar_json é o do publicar.py.
    Devolve True quando a volta deve ficar vermelha"""
    atuais = {}
    for a in map(normalizar, lista):
        atuais[a["codigo"]] = a
    registro = json.loads(ARQUIVO.read_text(encoding="utf-8")) if ARQUIVO.exists() else {}
    abertos = registro.setdefault("abertos", {})
    antes = json.dumps(registro, sort_keys=True)
    novos, repetidos, resolvidos = [], [], []

    for codigo, a in atuais.items():
        r = abertos.setdefault(codigo, {"tipo": a["tipo"], "visto": agora.isoformat()})
        r["texto"] = a["texto"][:300]
        avisado = r.get("avisado")
        if avisado:
            if a["tipo"] != "evento" and agora - datetime.fromisoformat(avisado) >= REPETIR:
                visto = datetime.fromisoformat(r["visto"])
                repetidos.append(f"{a['texto']} (desde {visto:%d/%m %H:%M})")
                r["avisado"] = agora.isoformat()
            continue
        if a["tipo"] == "passageiro" and agora - datetime.fromisoformat(r["visto"]) < ESPERAR_PASSAGEIRO:
            continue  # espera a próxima volta pra ver se continua
        novos.append(a["texto"])
        r["avisado"] = agora.isoformat()

    for codigo, r in list(abertos.items()):
        if codigo in atuais:
            continue
        if r["tipo"] == "evento":
            if agora - datetime.fromisoformat(r["visto"]) > GUARDAR_EVENTO:
                del abertos[codigo]
            continue
        if r.get("avisado"):
            resolvidos.append(r["texto"])
        del abertos[codigo]

    falhas = 0
    if novos or repetidos or resolvidos:
        falhas = mandar(montar(novos, repetidos, resolvidos))
        print(f"Avisos: {len(novos)} novo(s), {len(repetidos)} repetido(s), {len(resolvidos)} resolvido(s)"
              + (" mandados pro Telegram" if pessoas() else "") + (f", {falhas} recusa(s)" if falhas else ""))
    if json.dumps(registro, sort_keys=True) != antes:
        def mudar(d):
            d.clear()
            d.update(registro)
        salvar_json(ARQUIVO, mudar, f"avisos: {len(novos)} novo(s), {len(repetidos)} repetido(s), "
                                    f"{len(resolvidos)} resolvido(s)")
    return bool(novos or repetidos or falhas)
