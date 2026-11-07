"""Publicador: roda no GitHub Actions a cada 5 minutos (passo 6).

Abre cada fila/<cliente>.json e publica no Instagram o que já passou do
horário. Antes, lê os cliques de aprovação no Telegram e cuida do prazo de
cada horário: lembretes, aviso 2 h antes e "passou do horário" (aprovacao.py).
Cada item anda assim, salvando no GitHub a cada passo:

  esperando aprovação -> aguardando (clique em Aprovar no grupo do cliente)
  aguardando -> publicando        (salvo antes de falar com a Meta)
             -> caixa preparada   (número da Meta salvo antes de soltar)
             -> publicado         (número do post salvo)

Se uma volta cai no meio, o item fica em "publicando" e a próxima pergunta
à Meta se a caixa já saiu (PUBLISHED). Por isso nunca posta duas vezes.

Carrossel (item 8.4, tipo "carrossel", lista "imagens" na ordem) tem um
passo a mais antes da caixa: uma caixa por slide (is_carousel_item), com os
números salvos na fila ("slides preparados"). Com todos prontos, a caixa do
álbum (media_type CAROUSEL, children e a legenda) é a "caixa" de sempre:
salva antes de soltar, e a trava contra post duplicado é a mesma. Slide sozinho
nunca vai pro ar. Slide que deu erro ou venceu (24 h) é refeito do zero.
Antes de pedir a caixa, confere se a arte está no Pages: com 404 (build
atrasado ou falho) espera a volta seguinte sem gastar tentativa e avisa se
continuar. Um cliente com problema (fila quebrada, horário sem fuso) avisa e
fica sem publicar naquela volta; os outros seguem.
Falha passageira tenta de novo na volta seguinte; na 6ª vira "erro". Mais
de 2 h atrasado vira "atrasado" e não sai sem alguém decidir. Erro, atraso
e qualquer outro problema viram aviso no Telegram (avisos.py), na conversa
privada de quem cuida do robô, sem repetir a cada volta. Quando sai aviso a
execução falha, e o GitHub manda e-mail também.

Token de cada cliente: segredo IG_TOKEN_<CLIENTE> no GitHub
(teste-livro -> IG_TOKEN_TESTE_LIVRO), passado pelo nome no publicar.yml.

Renovação: o token do Instagram vale 60 dias. O tokens.json guarda só a
data da última renovação de cada cliente (nenhum token). Passou de 30 dias,
o robô pede um token novo à Meta e grava por cima do segredo, usando a
chave GH_SEGREDOS (só mexe nos segredos deste repositório). Falhou, tenta
de novo no dia seguinte e avisa. Cliente novo entra no tokens.json sozinho,
com a data do dia.

Vencimentos: o tokens.json lista as chaves que vencem (GH_SEGREDOS, a do
cron-job.org, a GH_DESPERTADOR do Cloudflare, a versão da API do GitHub),
com a data e o que fazer. O robô avisa 30, 7 e 1 dia antes, e a cada 12 h
depois de vencida, até alguém mudar a data.

Robô parado: o cron-job.org acorda o robô a cada 5 min. Se a volta anterior
foi há mais de 30 min, o despertador falhou e esta volta veio do agendamento
do GitHub (que roda quando dá); o robô avisa. A volta anterior é a de número
menor, e a conta usa o relógio desta volta: em 01/11/2026, no fim do horário
de verão dos EUA, o GitHub informou por 1 h o horário das voltas com 1 h a
mais e o robô avisou um buraco que não existiu.
"""
import http.client
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
from aprovacao import ATRASO_MAXIMO, aviso

try:
    import avisos as central
except Exception as e:  # erro nos avisos nunca pode parar a publicação
    central = None
    print(f"Avisos: não carreguei o avisos.py ({type(e).__name__}: {e}), fica só o e-mail do GitHub")

RAIZ = Path(__file__).resolve().parent
API = "https://graph.instagram.com"
BRASILIA = timezone(timedelta(hours=-3))
TENTATIVAS = 6  # com volta a cada 5 min, uns 30 min de insistência (eram 3 voltas de 15 min, uns 45 min)
TOKENS = RAIZ / "tokens.json"
RENOVAR_COM = timedelta(days=30)
PARADO = timedelta(minutes=30)


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
    except (OSError, http.client.HTTPException, ValueError) as e:
        # demora de mais de 60 s, conexão caída no meio ou resposta que não é JSON: o urllib deixa passar
        # cru e, antes, isso derrubava a volta inteira sem contar tentativa nem avisar no Telegram
        raise ErroMeta(f"a Meta não respondeu direito ({type(e).__name__})") from None


def arte_sumiu(url):
    """True só quando o Pages responde 404 pra arte (o build com ela ainda não saiu ou falhou). Qualquer
    outra resposta, ou falha de conexão, segue como antes e quem decide é a Meta"""
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers={"User-Agent": "publicador-wdr"}),
                               timeout=20).close()
    except urllib.error.HTTPError as e:
        return e.code == 404
    except Exception:
        pass
    return False


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


def salvar_json(arq, mudar, mensagem):
    """Mesma ideia do salvar, pra um arquivo inteiro (tokens.json, avisos.json)"""
    for _ in range(3):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        dados = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}
        mudar(dados)
        arq.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git("add", arq.name)
        git("commit", "-q", "-m", mensagem)
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return True
        time.sleep(5)
    return False


def salvar_tokens(mudar, mensagem):
    """tokens.json: só datas, nenhum token"""
    return salvar_json(TOKENS, mudar, mensagem)


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
            avisos.append(aviso(f"renovar|{cliente}|{hoje}",
                                f"{cliente}: não consegui renovar o token ({idade} dias de uso, vence com 60). "
                                f"Tento de novo amanhã. Erro: {e}"))
            continue
        salvar_tokens(lambda d: d["clientes"].update({cliente: {"renovado_em": hoje}}), f"{cliente}: token renovado")
        print(f"{cliente}: token renovado, vale mais 60 dias")

    for nome, v in dados.get("vencimentos", {}).items():
        vence = date.fromisoformat(v["vence"])
        dias = (vence - agora.date()).days
        if dias > 30:
            continue
        etapa = "venceu" if dias <= 0 else "1" if dias <= 1 else "7" if dias <= 7 else "30"
        quando = "já venceu" if dias <= 0 else f"falta {dias} dia" if dias == 1 else f"faltam {dias} dias"
        avisos.append(aviso(f"vence|{nome}|{etapa}",
                            f"{nome} vence em {vence:%d/%m/%Y} ({quando}). {v['o_que_fazer']} Depois, mudar a data "
                            f"em vencimentos no tokens.json.", "problema" if etapa == "venceu" else "evento"))
    return avisos


def ler_voltas():
    """Últimas voltas deste robô, pela API do GitHub (chave da própria volta, só leitura)"""
    chave = os.environ.get("GH_LEITURA")
    pedido = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY')}/actions/workflows/publicar.yml/runs"
        "?per_page=10", headers={"Accept": "application/vnd.github+json", "User-Agent": "publicador-wdr",
                                 **({"Authorization": f"Bearer {chave}"} if chave else {})})
    with urllib.request.urlopen(pedido, timeout=30) as r:
        return json.load(r)["workflow_runs"]


def robo_parado(agora):
    """Avisa se a volta anterior foi há mais de 30 min (despertador do cron-job.org falhou)"""
    try:
        voltas = ler_voltas()
    except Exception as e:
        print(f"Não consegui ver a volta anterior ({type(e).__name__}), sigo sem conferir")
        return []
    atual = int(os.environ.get("GITHUB_RUN_ID") or 0)
    antes = [v for v in voltas if not atual or int(v["id"]) < atual]
    if not antes:
        return []
    # horário errado do GitHub só deixa a anterior mais perto (ou no futuro): pode perder um aviso, nunca inventar
    esta = agora
    anterior = datetime.fromisoformat(max(antes, key=lambda v: int(v["id"]))["created_at"]
                                      .replace("Z", "+00:00")).astimezone(BRASILIA)
    if esta - anterior <= PARADO:
        return []
    minutos = int((esta - anterior).total_seconds() // 60)
    duracao = f"{minutos} min" if minutos < 120 else f"{minutos / 60:.1f} h".replace(".", ",")
    return [aviso(f"parado|{anterior:%Y-%m-%dT%H:%M}",
                  f"O robô ficou {duracao} sem rodar (de {anterior:%d/%m %H:%M} a {esta:%d/%m %H:%M}). O despertador "
                  f"do cron-job.org deve ter parado: conferir o histórico do trabalho em console.cron-job.org e se a "
                  f"chave despertador-social-wdr ainda vale.")]


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
    return esperar_todas([caixa], token)


def esperar_todas(caixas, token):
    """True quando a Meta terminou de processar todas (os slides do carrossel ou uma caixa só)"""
    faltam = list(caixas)
    for _ in range(12):
        for caixa in list(faltam):
            status = situacao_caixa(caixa, token)
            if status == "FINISHED":
                faltam.remove(caixa)
            elif status in ("ERROR", "EXPIRED"):
                raise ErroMeta(f"a Meta não processou a imagem ({status})")
        if not faltam:
            return True
        time.sleep(5)
    return False


def preparar_slides(item, token):
    """Uma caixa por slide, na ordem. Sem legenda: a legenda vai no álbum"""
    return [pedir(f"{API}/me/media", dados={"image_url": imagem, "is_carousel_item": "true",
                                            "access_token": token})["id"] for imagem in item["imagens"]]


def preparar_album(item, slides, token):
    return pedir(f"{API}/me/media", dados={"media_type": "CAROUSEL", "children": ",".join(slides),
                                           "caption": item["legenda"], "access_token": token})["id"]


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
                item["caixas_dos_slides"] = None  # álbum vencido: os slides também, refaz tudo
        if not caixa:
            atraso = agora - datetime.fromisoformat(item["quando"])
            if atraso > ATRASO_MAXIMO:
                marcar({"situacao": "atrasado"}, "atrasado, não publicado")
                return aviso(f"atrasado|{nome}", f"{nome}: {atraso.total_seconds() / 3600:.1f} h atrasado, "
                                                 f"ficou sem publicar pra alguém decidir")
            sumiu = next((u for u in item.get("imagens") or [item.get("imagem")] if u and arte_sumiu(u)), None)
            if sumiu:
                print(f"{nome}: a arte ainda não está no Pages ({sumiu.rsplit('/', 1)[-1]}), tento na próxima volta")
                return aviso(f"pages|{nome}", f"{nome}: a arte {sumiu.rsplit('/', 1)[-1]} não está no Pages, então a "
                                              f"Meta não teria de onde baixar. Tento a cada volta sem gastar tentativa. "
                                              f"Se continuar: GitHub > Actions > pages build and deployment > Re-run "
                                              f"do mais recente", "passageiro")
            if item["tipo"] == "carrossel":
                slides = item.get("caixas_dos_slides")
                if slides and any(situacao_caixa(s, token) in ("ERROR", "EXPIRED") for s in slides):
                    slides = None
                if not slides:
                    slides = preparar_slides(item, token)
                    if not marcar({"caixas_dos_slides": slides}, "slides preparados"):
                        return None  # sem os números salvos não segue; os slides somem sozinhos em 24 h
                if not esperar_todas(slides, token):
                    print(f"{nome}: a Meta ainda está processando os slides, segue na próxima volta")
                    return None
                caixa = preparar_album(item, slides, token)
            else:
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
            return aviso(f"erro|{nome}", f"{nome}: desistiu depois de {TENTATIVAS} tentativas. Último erro: {e}")
    return None


def conferir_fila(itens):
    """Erro claro pra fila mexida à mão: horário sem fuso (sem o -03:00) dava erro de comparação a cada volta"""
    for i in itens:
        if datetime.fromisoformat(i["quando"]).tzinfo is None:
            raise ValueError(f"{i['id']} com horário sem fuso ({i['quando']}): falta o -03:00 no fim")


def cuidar_da_fila(cliente, arq, token, agora):
    """Lembretes, prazo e publicação de um cliente. Devolve avisos"""
    avisos = []
    conferir_fila(json.loads(arq.read_text(encoding="utf-8"))["itens"])
    try:
        avisos += aprovacao.cuidar_dos_horarios(cliente, salvar, agora)
    except Exception as e:  # lembrete quebrado nunca pode parar a publicação
        avisos.append(aviso(f"prazo|{cliente}", f"{cliente}: erro nos lembretes e no prazo de aprovação "
                                                f"({type(e).__name__}: {e})", "problema"))
    itens = json.loads(arq.read_text(encoding="utf-8"))["itens"]  # lido depois: o prazo pode ter mudado a fila
    vencidos = [i for i in itens if i["situacao"] in ("aguardando", "publicando")
                and datetime.fromisoformat(i["quando"]) <= agora]
    contagem = {s: sum(1 for i in itens if i["situacao"] == s) for s in {i["situacao"] for i in itens}}
    print(f"{cliente}: token {'ok' if token else 'FALTANDO'}, {len(vencidos)} pra publicar agora, {contagem}")
    # avisa assim que tiver post na fila, e não só na hora de publicar (cliente novo sem o segredo ou sem a linha
    # no publicar.yml só aparecia quando o primeiro post já devia estar saindo)
    pendentes = [i for i in itens if i["situacao"] in (aprovacao.ESPERANDO, "aguardando", "publicando")]
    if pendentes and not token:
        primeiro = datetime.fromisoformat(min(i["quando"] for i in pendentes))
        avisos.append(aviso(f"segredo|{cliente}", f"{cliente}: falta o segredo {segredo(cliente)} no GitHub (ou a "
                                                   f"linha dele no publicar.yml). Primeiro post na fila: "
                                                   f"{primeiro:%d/%m %H:%M}. Nada desse cliente sai até colocar",
                            "problema"))
        return avisos
    for item in vencidos:
        problema = processar(cliente, item, token, agora)
        if problema:
            avisos.append(problema)
    return avisos


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("O publicador roda só no GitHub, porque mexe na fila direto lá. No PC, usar o fila.py.")
    git("config", "user.name", "publicador")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    agora = datetime.now(BRASILIA)

    avisos, tokens = robo_parado(agora), {}
    # aprovações primeiro, pra um clique de 8h55 já valer na volta das 9h
    try:
        avisos += aprovacao.ler_cliques(salvar, agora)
    except Exception as e:  # problema no Telegram nunca pode parar o que já foi aprovado
        avisos.append(aviso("telegram-erro", f"Telegram: erro lendo os cliques ({type(e).__name__}: {e})", "problema"))
    try:
        avisos += aprovacao.conferir_entrega(agora)
    except Exception as e:
        avisos.append(aviso("entrega-conferir", f"Telegram: erro conferindo a entrega na caixa ({type(e).__name__}: {e})",
                            "problema"))
    if os.environ.get("TESTAR_AVISO") == "true":  # caixa "Mandar um aviso de teste" no Run workflow
        avisos.append(aviso(f"teste-aviso|{os.environ.get('GITHUB_RUN_ID')}",
                            "Aviso de teste pedido no Run workflow. Se chegou aqui, os avisos estão funcionando."))
    for arq in sorted((RAIZ / "fila").glob("*.json")):
        cliente = arq.stem
        token = os.environ.get(segredo(cliente))
        if token:
            tokens[cliente] = token  # antes de tudo: fila com problema não pode atrasar a renovação do token
        try:
            avisos += cuidar_da_fila(cliente, arq, token, agora)
        except Exception as e:  # um cliente com problema não para os outros
            print(f"{cliente}: ERRO ({type(e).__name__}: {e}), sigo com os outros clientes")
            avisos.append(aviso(f"fila|{cliente}", f"{cliente}: erro na fila, nada desse cliente sai enquanto não "
                                                   f"resolver; os outros clientes seguem ({type(e).__name__}: {e})",
                                "problema"))

    avisos += cuidar_dos_tokens(tokens, agora)
    if avisos:
        print("\nPRECISA DE ATENÇÃO:\n" + "\n".join(a["texto"] if isinstance(a, dict) else a for a in avisos))
    if not central:
        vermelho = bool(avisos)
    else:
        try:
            vermelho = central.cuidar(avisos, agora, salvar_json)
        except Exception as e:  # aviso quebrado: fica o e-mail do GitHub
            print(f"Avisos: erro no avisos.py ({type(e).__name__}: {e})")
            vermelho = True
    if vermelho:
        sys.exit(1)


if __name__ == "__main__":
    main()
