"""Publicador: roda no GitHub Actions a cada 15 minutos (passo 6).

Abre cada fila/<cliente>.json e publica no Instagram o que já passou do
horário. Antes, lê os cliques de aprovação no Telegram (aprovacao.py).
Cada item anda assim, salvando no GitHub a cada passo:

  esperando aprovação -> aguardando (clique em Aprovar no grupo do cliente)
  aguardando -> publicando        (salvo antes de falar com a Meta)
             -> caixa preparada   (número da Meta salvo antes de soltar)
             -> publicado         (número do post salvo)

Se uma volta cai no meio, o item fica em "publicando" e a próxima pergunta
à Meta se a caixa já saiu (PUBLISHED). Por isso nunca posta duas vezes.
Falha passageira tenta de novo na volta seguinte; na 3ª vira "erro". Mais
de 3 h atrasado vira "atrasado" e não sai sem alguém decidir. Erro e
atraso fazem a execução falhar, e o GitHub manda e-mail.

Token de cada cliente: segredo IG_TOKEN_<CLIENTE> no GitHub
(teste-livro -> IG_TOKEN_TESTE_LIVRO), passado pelo nome no publicar.yml.

Renovação: o token do Instagram vale 60 dias. O tokens.json guarda só a
data da última renovação de cada cliente (nenhum token). Passou de 30 dias,
o robô pede um token novo à Meta e grava por cima do segredo, usando a
chave GH_SEGREDOS (só mexe nos segredos deste repositório). Falhou, tenta
de novo no dia seguinte e avisa por e-mail. Cliente novo entra no
tokens.json sozinho, com a data do dia. Um mês antes da GH_SEGREDOS vencer
(data em chave_github_vence), avisa uma vez por dia.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aprovacao

RAIZ = Path(__file__).resolve().parent
API = "https://graph.instagram.com"
BRASILIA = timezone(timedelta(hours=-3))
ATRASO_MAXIMO = timedelta(hours=3)
TENTATIVAS = 3
TOKENS = RAIZ / "tokens.json"
RENOVAR_COM = timedelta(days=30)


class ErroMeta(Exception):
    pass


def pedir(url, dados=None, **params):
    """GET na API (POST quando vem dados). Erro vira ErroMeta, sem o token"""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    corpo = urllib.parse.urlencode(dados).encode() if dados else None
    try:
        with urllib.request.urlopen(url, data=corpo, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            erro = json.load(e).get("error", {})
        except ValueError:
            erro = {}
        raise ErroMeta(f"{e.code}: {erro.get('message', e.reason)}") from None
    except urllib.error.URLError as e:
        raise ErroMeta(f"sem conexão com a Meta: {e.reason}") from None


def git(*args):
    return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True, text=True)


def salvar(cliente, id_item, mudancas, mensagem, esperado=None):
    """Grava a mudança de um item no GitHub. Pega a fila mais nova antes de
    cada tentativa, pra não apagar o que chegou do PC nesse meio tempo.
    Com esperado, só grava se o item ainda estiver nessa situação."""
    arq = RAIZ / "fila" / f"{cliente}.json"
    for _ in range(3):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        fila = json.loads(arq.read_text(encoding="utf-8"))
        item = next((i for i in fila["itens"] if i["id"] == id_item), None)
        if not item or (esperado and item["situacao"] != esperado):
            return False
        item.update(mudancas)
        arq.write_text(json.dumps(fila, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git("commit", "-q", "-am", f"{cliente} {id_item}: {mensagem}")
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return True
        time.sleep(5)
    return False


def segredo(cliente):
    return "IG_TOKEN_" + cliente.upper().replace("-", "_")


def salvar_tokens(mudar, mensagem):
    """Mesma ideia do salvar, pro tokens.json (só datas, nenhum token)"""
    for _ in range(3):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        dados = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
        mudar(dados)
        TOKENS.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git("add", "tokens.json")
        git("commit", "-q", "-m", mensagem)
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return True
        time.sleep(5)
    return False


def renovar(cliente, token):
    """Pede token novo à Meta e grava no segredo do cliente. O antigo segue
    valendo até vencer, então uma falha aqui não derruba a publicação"""
    chave = os.environ.get("GH_SEGREDOS")
    if not chave:
        raise ErroMeta("falta o segredo GH_SEGREDOS no GitHub")
    novo = pedir(f"{API}/refresh_access_token", grant_type="ig_refresh_token", access_token=token)["access_token"]
    print(f"::add-mask::{novo}")  # o GitHub esconde esse valor em todo o registro
    r = subprocess.run(["gh", "secret", "set", segredo(cliente), "--repo", os.environ["GITHUB_REPOSITORY"]],
                       input=novo, text=True, capture_output=True, env={**os.environ, "GH_TOKEN": chave})
    if r.returncode != 0:
        raise ErroMeta(f"o GitHub não aceitou gravar {segredo(cliente)}: {r.stderr.strip()[:200]}")


def cuidar_dos_tokens(tokens, agora):
    """Renova o que passou de 30 dias. Devolve avisos pra quem cuida"""
    dados = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
    hoje = agora.date().isoformat()
    forcar = os.environ.get("RENOVAR_AGORA") == "true"
    avisos = []

    for cliente, token in tokens.items():
        info = dados.get("clientes", {}).get(cliente)
        if not info:
            salvar_tokens(lambda d: d.setdefault("clientes", {}).update({cliente: {"renovado_em": hoje}}),
                          f"{cliente}: token novo, 60 dias contando de hoje")
            print(f"{cliente}: token novo anotado com a data de hoje")
            continue
        idade = (agora.date() - date.fromisoformat(info["renovado_em"])).days
        if not forcar and (idade < RENOVAR_COM.days or info.get("tentou_em") == hoje):
            print(f"{cliente}: token com {idade} dias, renova com {RENOVAR_COM.days}")
            continue
        try:
            renovar(cliente, token)
        except ErroMeta as e:
            salvar_tokens(lambda d: d["clientes"][cliente].update(tentou_em=hoje), f"{cliente}: renovação do token falhou")
            avisos.append(f"{cliente}: não consegui renovar o token ({idade} dias de uso, vence com 60). "
                          f"Tento de novo amanhã. Erro: {e}")
            continue
        salvar_tokens(lambda d: d["clientes"].update({cliente: {"renovado_em": hoje}}), f"{cliente}: token renovado")
        print(f"{cliente}: token renovado, vale mais 60 dias")

    vence = dados.get("chave_github_vence")
    if vence and agora.date() >= date.fromisoformat(vence) - timedelta(days=30) and dados.get("avisou_chave_em") != hoje:
        salvar_tokens(lambda d: d.update(avisou_chave_em=hoje), "aviso: GH_SEGREDOS perto de vencer")
        avisos.append(f"a chave GH_SEGREDOS vence em {vence}. Gerar outra igual no GitHub, "
                      f"trocar o segredo e mudar a data em tokens.json")
    return avisos


def situacao_caixa(caixa, token):
    return pedir(f"{API}/{caixa}", fields="status_code", access_token=token).get("status_code")


def preparar(item, token):
    dados = {"image_url": item["imagem"], "access_token": token}
    if item["tipo"] == "story":
        dados["media_type"] = "STORIES"
    else:
        dados["caption"] = item["legenda"]
    return pedir(f"{API}/me/media", dados=dados)["id"]


def esperar(caixa, token):
    """True quando a Meta terminou de processar a imagem"""
    for _ in range(12):
        status = situacao_caixa(caixa, token)
        if status == "FINISHED":
            return True
        if status in ("ERROR", "EXPIRED"):
            raise ErroMeta(f"a Meta não processou a imagem ({status})")
        time.sleep(5)
    return False


def processar(cliente, item, token, agora):
    """Leva um item até publicado. Devolve um aviso quando precisa de alguém"""
    def marcar(mudancas, mensagem, esperado=None):
        ok = salvar(cliente, item["id"], mudancas, mensagem, esperado)
        if ok:
            item.update(mudancas)
        return ok

    nome = f"{cliente} {item['id']}"
    if item["situacao"] == "aguardando" and not marcar({"situacao": "publicando"}, "publicando", "aguardando"):
        return None  # não travou o item, fica pra próxima volta

    try:
        caixa = item.get("caixa")
        if caixa:
            status = situacao_caixa(caixa, token)
            if status == "PUBLISHED":
                marcar({"situacao": "publicado", "publicado_em": agora.isoformat()}, "já tinha saído")
                print(f"{nome}: já tinha saído na volta anterior")
                return None
            if status in ("ERROR", "EXPIRED"):
                caixa = None
        if not caixa:
            atraso = agora - datetime.fromisoformat(item["quando"])
            if atraso > ATRASO_MAXIMO:
                marcar({"situacao": "atrasado"}, "atrasado, não publicado")
                return f"{nome}: {atraso.total_seconds() / 3600:.1f} h atrasado, ficou sem publicar pra alguém decidir"
            caixa = preparar(item, token)
            if not marcar({"caixa": caixa}, "caixa preparada"):
                return None  # sem o número salvo não solta; a caixa some sozinha em 24 h
        if not esperar(caixa, token):
            print(f"{nome}: a Meta ainda está processando, segue na próxima volta")
            return None
        post = pedir(f"{API}/me/media_publish", dados={"creation_id": caixa, "access_token": token})
        marcar({"situacao": "publicado", "instagram_id": post["id"],
                "publicado_em": datetime.now(BRASILIA).isoformat()}, "publicado")
        print(f"{nome}: publicado")
    except ErroMeta as e:
        tentativas = item.get("tentativas", 0) + 1
        final = tentativas >= TENTATIVAS
        marcar({"situacao": "erro" if final else "publicando", "tentativas": tentativas,
                "ultimo_erro": str(e)}, f"falhou ({tentativas}ª vez)")
        print(f"{nome}: falhou ({tentativas}ª vez): {e}")
        if final:
            return f"{nome}: desistiu depois de {TENTATIVAS} tentativas. Último erro: {e}"
    return None


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("O publicador roda só no GitHub, porque mexe na fila direto lá. No PC, usar o fila.py.")
    git("config", "user.name", "publicador")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    agora = datetime.now(BRASILIA)

    avisos, tokens = [], {}
    # aprovações primeiro, pra um clique de 8h55 já valer na volta das 9h
    try:
        avisos += aprovacao.ler_cliques(salvar, agora)
    except Exception as e:  # problema no Telegram nunca pode parar o que já foi aprovado
        avisos.append(f"Telegram: erro lendo os cliques ({type(e).__name__}: {e})")
    for arq in sorted((RAIZ / "fila").glob("*.json")):
        cliente = arq.stem
        itens = json.loads(arq.read_text(encoding="utf-8"))["itens"]
        token = os.environ.get(segredo(cliente))
        if token:
            tokens[cliente] = token
        vencidos = [i for i in itens if i["situacao"] in ("aguardando", "publicando")
                    and datetime.fromisoformat(i["quando"]) <= agora]
        contagem = {s: sum(1 for i in itens if i["situacao"] == s) for s in {i["situacao"] for i in itens}}
        print(f"{cliente}: token {'ok' if token else 'FALTANDO'}, {len(vencidos)} pra publicar agora, {contagem}")
        if vencidos and not token:
            avisos.append(f"{cliente}: falta o segredo {segredo(cliente)} no GitHub")
            continue
        for item in vencidos:
            aviso = processar(cliente, item, token, agora)
            if aviso:
                avisos.append(aviso)

    avisos += cuidar_dos_tokens(tokens, agora)
    if avisos:
        print("\nPRECISA DE ATENÇÃO:\n" + "\n".join(avisos))
        sys.exit(1)


if __name__ == "__main__":
    main()
