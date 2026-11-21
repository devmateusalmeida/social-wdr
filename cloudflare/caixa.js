// Caixa de correio do bot de aprovação (passo 7). Roda grátis no Cloudflare
// (Workers & Pages), colado no editor do painel.
//
// O Telegram entrega aqui cada novidade do bot (clique de botão ou mensagem)
// na hora. O Worker guarda no banco, responde "Recebido" pro clique e acorda
// o robô no GitHub. O robô lê a caixa a cada volta e marca como lido o que
// gravou na fila. Sem isto o clique se perde: o Telegram só entrega clique
// pra quem está lendo a caixa do bot naquele momento.
//
// Ligações (Settings > Bindings):
//   BANCO            banco D1 (a tabela se cria sozinha)
// Segredos (Settings > Variables and Secrets, tipo Secret):
//   SEGREDO_WEBHOOK  o mesmo WEBHOOK_SEGREDO de segredos/telegram.env
//   CHAVE_CAIXA      o mesmo CAIXA_CHAVE de segredos/telegram.env e do
//                    segredo TELEGRAM_CAIXA_CHAVE no GitHub
//   GH_DESPERTADOR   chave fine-grained do GitHub, só Actions do repositório
//   TOKEN_BOT        token do bot (o mesmo TELEGRAM_TOKEN de segredos/telegram.env).
//                    Só pro áudio do mês (18/11): a página manda o áudio pra cá
//                    e daqui ele vai pro privado de quem está em AVISAR.
// O "Recebido" do clique vai dentro da resposta ao Telegram, e o webhook é
// ligado do PC (python sistema/telegram.py caixa ligar).

const REPOSITORIO = "devmateusalmeida/social-wdr"; // trocar quando passar pra conta da WDR
const GUARDAR_LIDAS = 24 * 3600; // segundos: o descobrir ainda vê o que o robô já leu
const AVISAR = [1874038076]; // quem recebe o áudio do mês no privado (ensaio: só o Mateus)
const AUDIO_MAX = 20 * 1024 * 1024; // o Telegram só deixa o bot baixar até 20 MB
// Quem aprova cada cliente (o mesmo aprovacao.json do robô), lido pelo cliqueDoMes (21/11). Só funciona
// com o repositório público: quando ficar privado, os botões mz/mo respondem "tenta de novo" até a lista
// passar pra uma variável do Worker ou pra VPS.
const CONFIG = `https://raw.githubusercontent.com/${REPOSITORIO}/main/aprovacao.json`;
const PC_SEM_SINAL = 10 * 60; // segundos: a vigia do PC bate em /vivo a cada 2 min

export default {
  async fetch(pedido, env, ctx) {
    const url = new URL(pedido.url);
    try {
      if (pedido.method === "OPTIONS" && (url.pathname === "/audio" || url.pathname === "/confirmar")) return new Response(null, { status: 204, headers: CORS });
      if (pedido.method === "POST" && url.pathname === "/confirmar") return await receberConfirmacao(pedido, env);
      if (pedido.method === "POST" && url.pathname === "/audio") return await receberAudio(pedido, env);
      if (pedido.method === "GET" && url.pathname === "/audios") return await listarAudios(pedido, env);
      if (pedido.method === "GET" && url.pathname === "/respostas") return await listarRespostas(pedido, env);
      if (pedido.method === "POST" && url.pathname === "/respostas/lido") return await respostaLida(pedido, env);
      if (pedido.method === "POST" && url.pathname === "/audios/lido") return await audioLido(pedido, env);
      if (pedido.method === "POST" && url.pathname === "/vivo") return await sinalDoPc(pedido, env);
      if (pedido.method === "POST" && url.pathname === "/telegram") return await doTelegram(pedido, env, ctx);
      if (pedido.method === "GET" && url.pathname === "/caixa") return await abrirCaixa(pedido, env, url);
      if (pedido.method === "POST" && url.pathname === "/lido") return await marcarLido(pedido, env);
      if (pedido.method === "GET" && url.pathname === "/") return new Response("Caixa do bot de aprovação no ar.");
    } catch (e) {
      // erro vira 500: o Telegram tenta entregar de novo, o robô tenta na próxima volta
      console.log("erro: " + e);
      return new Response("erro", { status: 500 });
    }
    return new Response("nada aqui", { status: 404 });
  },
};

// Entrega do Telegram. Só vale com o segredo do webhook no cabeçalho.
async function doTelegram(pedido, env, ctx) {
  if (!confere(pedido.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.SEGREDO_WEBHOOK)) {
    return new Response("não", { status: 401 });
  }
  const novidade = await pedido.json();
  if (!Number.isInteger(novidade.update_id)) return new Response("ok");
  // botões da pergunta do áudio do mês (mz| e mo|): resolvidos aqui, o robô nem fica sabendo
  const dadosClique = novidade.callback_query && String(novidade.callback_query.data || "");
  if (/^m[zo]\|/.test(dadosClique)) return await cliqueDoMes(novidade.callback_query, env);
  await preparar(env);
  // a mesma novidade entregue duas vezes fica uma só (o número é a chave)
  await env.BANCO.prepare("INSERT OR IGNORE INTO caixa (id, recebido, dados) VALUES (?, ?, ?)")
    .bind(novidade.update_id, agora(), JSON.stringify(novidade)).run();
  const clique = novidade.callback_query;
  if (!clique) return new Response("ok");
  ctx.waitUntil(acordarRobo(env));
  return Response.json({
    method: "answerCallbackQuery",
    callback_query_id: clique.id,
    // janela com OK (17/11): o aviso pequeno passava despercebido e a pessoa clicava de novo antes da confirmação
    text: "Recebido! Em até 1 minuto esta mensagem muda pra mostrar a decisão. Não precisa clicar de novo.",
    show_alert: true,
  });
}

// Robô e descobrir. ?tudo=1 traz também o que já foi lido nas últimas 24 h.
async function abrirCaixa(pedido, env, url) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  await preparar(env);
  const tudo = url.searchParams.get("tudo") === "1";
  const { results } = await env.BANCO.prepare(
    `SELECT dados FROM caixa ${tudo ? "" : "WHERE lido = 0"} ORDER BY id LIMIT 100`).all();
  return Response.json(results.map((r) => JSON.parse(r.dados)));
}

// O robô manda os números que gravou na fila: {"ids": [..]}
async function marcarLido(pedido, env) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  const { ids } = await pedido.json();
  if (!Array.isArray(ids) || !ids.every(Number.isInteger)) return new Response("ids errados", { status: 400 });
  await preparar(env);
  const passos = ids.map((id) => env.BANCO.prepare("UPDATE caixa SET lido = ? WHERE id = ?").bind(agora(), id));
  passos.push(env.BANCO.prepare("DELETE FROM caixa WHERE lido > 0 AND lido < ?").bind(agora() - GUARDAR_LIDAS));
  await env.BANCO.batch(passos);
  return Response.json({ ok: true });
}

// Mesmo pedido que o cron-job.org faz a cada 5 min. Se falhar, ele cobre.
async function acordarRobo(env) {
  const r = await fetch(`https://api.github.com/repos/${REPOSITORIO}/actions/workflows/publicar.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_DESPERTADOR}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "caixa-aprovacao",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) console.log(`não acordei o robô (${r.status}), o despertador de 5 min cobre`);
}

async function preparar(env) {
  // lido = 0: ainda não lido; senão, a hora em que o robô leu
  await env.BANCO.prepare("CREATE TABLE IF NOT EXISTS caixa (id INTEGER PRIMARY KEY, recebido INTEGER NOT NULL, " +
    "lido INTEGER NOT NULL DEFAULT 0, dados TEXT NOT NULL)").run();
}

function autorizado(pedido, env) {
  return confere((pedido.headers.get("Authorization") || "").replace(/^Bearer /, ""), env.CHAVE_CAIXA);
}

// compara sem dar pista de quantas letras acertou
function confere(recebido, certo) {
  if (!recebido || !certo) return false;
  const a = new TextEncoder().encode(recebido);
  const b = new TextEncoder().encode(certo);
  return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}

function agora() {
  return Math.floor(Date.now() / 1000);
}

// ---------- Áudio do mês (18/11) ----------
// A página do mês (GitHub Pages) manda: cliente, mes, chave, duracao, marcas
// (JSON com qual post estava na tela em cada segundo) e o áudio. A chave é
// HMAC de "cliente|mes" com a CHAVE_CAIXA, gerada pelo montar.py: só a página
// daquele cliente e mês consegue mandar.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function respostaPagina(corpo, status = 200) {
  return new Response(JSON.stringify(corpo), { status, headers: { ...CORS, "Content-Type": "application/json" } });
}

async function chaveDaPagina(env, cliente, mes) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.CHAVE_CAIXA),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const assinatura = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(cliente + "|" + mes));
  return [...new Uint8Array(assinatura)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

const mmss = (s) => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");

async function receberAudio(pedido, env) {
  const form = await pedido.formData();
  const cliente = String(form.get("cliente") || "");
  const mes = String(form.get("mes") || "");
  if (!/^[a-z0-9-]{1,40}$/.test(cliente) || !/^\d{4}-\d{2}$/.test(mes)) return respostaPagina({ erro: "pedido errado" }, 400);
  if (!confere(String(form.get("chave") || ""), await chaveDaPagina(env, cliente, mes))) return respostaPagina({ erro: "não" }, 401);
  const audio = form.get("audio");
  if (!audio || typeof audio === "string" || !audio.size) return respostaPagina({ erro: "sem áudio" }, 400);
  if (audio.size > AUDIO_MAX) return respostaPagina({ erro: "áudio grande demais" }, 413);
  let marcas;
  try { marcas = JSON.parse(String(form.get("marcas") || "[]")); } catch { marcas = null; }
  if (!Array.isArray(marcas) || marcas.length > 1000) return respostaPagina({ erro: "marcas erradas" }, 400);
  const duracao = Number(form.get("duracao")) || 0;

  // linha do tempo legível pra mensagem
  const linhas = [];
  for (let k = 0; k < marcas.length - 1; k++) {
    const a = marcas[k], b = marcas[k + 1];
    if (b.t - a.t < 0.8) continue;
    linhas.push(mmss(a.t) + " a " + mmss(b.t) + "  " + String(a.nome || "").slice(0, 80));
  }
  const tipo = audio.type || "audio/webm";
  const ext = tipo.includes("mp4") ? "m4a" : tipo.includes("ogg") ? "ogg" : "webm";
  const aviso = await avisoDoPc(env).catch(() => "");
  const texto = aviso + `Áudio do mês: ${cliente}, ${mes} (${mmss(duracao)})\n\n` + linhas.join("\n");

  let arquivo = "";
  for (const chat of AVISAR) {
    const envio = new FormData();
    envio.append("chat_id", String(chat));
    envio.append("document", new File([audio], `${cliente}-${mes}.${ext}`, { type: tipo }));
    envio.append("caption", texto.slice(0, 1000));
    const r = await fetch(`https://api.telegram.org/bot${env.TOKEN_BOT}/sendDocument`, { method: "POST", body: envio });
    const j = await r.json();
    if (!j.ok) {
      console.log("Telegram recusou o áudio: " + JSON.stringify(j).slice(0, 300));
      return respostaPagina({ erro: "não consegui entregar" }, 502);
    }
    arquivo = arquivo || j.result.document.file_id;
  }

  await prepararAudios(env);
  await env.BANCO.prepare("INSERT INTO audios (recebido, cliente, mes, duracao, marcas, arquivo, tipo) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(agora(), cliente, mes, duracao, JSON.stringify(marcas), arquivo, tipo).run();
  return respostaPagina({ ok: true });
}

// Cliente confirma na página: etapa "lista" (o Entendi assim está certo) ou
// "final" (as versões novas ficaram certas). Avisa no privado e guarda.
// Fala vaga (19/11): "zero" (faz outro do zero, com o post) e "outros" (os
// outros posts estão ok) vêm dos botões da pergunta no grupo.
const ETAPAS = {
  lista: "confirmou a lista de ajustes", final: "confirmou que ficou tudo certo",
  zero: "pediu outro do zero", outros: "disse que os outros posts estão ok",
};

async function receberConfirmacao(pedido, env) {
  const form = await pedido.formData();
  const cliente = String(form.get("cliente") || "");
  const mes = String(form.get("mes") || "");
  const etapa = String(form.get("etapa") || "");
  if (!/^[a-z0-9-]{1,40}$/.test(cliente) || !/^\d{4}-\d{2}$/.test(mes) || !ETAPAS[etapa]) return respostaPagina({ erro: "pedido errado" }, 400);
  if (!confere(String(form.get("chave") || ""), await chaveDaPagina(env, cliente, mes))) return respostaPagina({ erro: "não" }, 401);
  const post = String(form.get("post") || "");
  if (post && !/^\d{4}-\d{2}-\d{2}$/.test(post)) return respostaPagina({ erro: "pedido errado" }, 400);
  if (etapa === "zero" && !post) return respostaPagina({ erro: "pedido errado" }, 400);
  const hora = new Date().toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", dateStyle: "short", timeStyle: "short" });
  const texto = `${cliente} (${mes}): o cliente ${ETAPAS[etapa]}${post ? " pro post " + post : ""}, em ${hora}.`;
  for (const chat of AVISAR) {
    const r = await fetch(`https://api.telegram.org/bot${env.TOKEN_BOT}/sendMessage`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, text: texto }),
    });
    if (!(await r.json()).ok) return respostaPagina({ erro: "não consegui avisar" }, 502);
  }
  await prepararRespostas(env);
  await env.BANCO.prepare("INSERT INTO respostas (recebido, cliente, mes, etapa, post) VALUES (?, ?, ?, ?, ?)")
    .bind(agora(), cliente, mes, etapa, post || null).run();
  return respostaPagina({ ok: true });
}

// Script do PC (depois o robô): áudios ainda não transcritos.
async function listarAudios(pedido, env) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  await prepararAudios(env);
  const { results } = await env.BANCO.prepare(
    "SELECT id, recebido, cliente, mes, duracao, marcas, arquivo, tipo FROM audios WHERE lido = 0 ORDER BY id LIMIT 20").all();
  return Response.json(results.map((r) => ({ ...r, marcas: JSON.parse(r.marcas) })));
}

async function audioLido(pedido, env) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  const { ids } = await pedido.json();
  if (!Array.isArray(ids) || !ids.every(Number.isInteger)) return new Response("ids errados", { status: 400 });
  await prepararAudios(env);
  await env.BANCO.batch(ids.map((id) => env.BANCO.prepare("UPDATE audios SET lido = ? WHERE id = ?").bind(agora(), id)));
  return Response.json({ ok: true });
}

async function prepararAudios(env) {
  await env.BANCO.prepare("CREATE TABLE IF NOT EXISTS audios (id INTEGER PRIMARY KEY AUTOINCREMENT, recebido INTEGER NOT NULL, " +
    "cliente TEXT NOT NULL, mes TEXT NOT NULL, duracao REAL, marcas TEXT NOT NULL, arquivo TEXT NOT NULL, tipo TEXT, " +
    "lido INTEGER NOT NULL DEFAULT 0)").run();
}

// Respostas da página (confirmações e botões da pergunta) que o script do PC ainda não leu.
async function listarRespostas(pedido, env) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  await prepararRespostas(env);
  const { results } = await env.BANCO.prepare(
    "SELECT id, recebido, cliente, mes, etapa, post FROM respostas WHERE lido = 0 ORDER BY id LIMIT 50").all();
  return Response.json(results);
}

async function respostaLida(pedido, env) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  const { ids } = await pedido.json();
  if (!Array.isArray(ids) || !ids.every(Number.isInteger)) return new Response("ids errados", { status: 400 });
  await prepararRespostas(env);
  await env.BANCO.batch(ids.map((id) => env.BANCO.prepare("UPDATE respostas SET lido = ? WHERE id = ?").bind(agora(), id)));
  return Response.json({ ok: true });
}

// A tabela nasceu em 18/11 sem post e sem lido: acrescenta as colunas uma vez (dá erro se já existem).
async function prepararRespostas(env) {
  await env.BANCO.prepare("CREATE TABLE IF NOT EXISTS respostas (id INTEGER PRIMARY KEY AUTOINCREMENT, recebido INTEGER NOT NULL, " +
    "cliente TEXT NOT NULL, mes TEXT NOT NULL, etapa TEXT NOT NULL)").run();
  for (const coluna of ["post TEXT", "lido INTEGER NOT NULL DEFAULT 0", "origem TEXT"]) {
    try { await env.BANCO.prepare("ALTER TABLE respostas ADD COLUMN " + coluna).run(); } catch (e) { /* já existe */ }
  }
}

// ---------- Pergunta da fala vaga (19/11): cliques direto no Telegram ----------
// mz|cliente|mes|post|fim   Faz outro do zero (fim=1: ele já disse "o restante está tudo certo")
// mo|cliente|mes            Sim, os outros posts estão certos
// Guarda em respostas (o script do PC lê), avisa no privado e muda a mensagem na hora.
async function cliqueDoMes(clique, env) {
  const partes = clique.data.split("|");
  const [tipo, cliente, mes] = partes;
  const post = tipo === "mz" ? partes[3] : "";
  const fim = partes[4] === "1";
  const msg = clique.message || {};
  const responder = (texto) => Response.json({ method: "answerCallbackQuery", callback_query_id: clique.id, text: texto, show_alert: true });
  if (!/^[a-z0-9-]{1,40}$/.test(cliente || "") || !/^\d{4}-\d{2}$/.test(mes || "") ||
      (tipo === "mz" && !/^\d{4}-\d{2}-\d{2}$/.test(post)) || !msg.chat) return responder("Botão antigo, não deu pra usar.");
  const etapa = tipo === "mz" ? "zero" : "outros";
  const origem = msg.chat.id + ":" + msg.message_id;

  // só vale clique de quem aprova esse cliente, no grupo dele (ou no privado de AVISAR, no ensaio),
  // igual ao Aprovar do robô (21/11). Sem conseguir ler a lista, não grava nada e pede pra tentar de novo.
  let cfg;
  try {
    const r = await fetch(CONFIG, { cf: { cacheTtl: 60 } });
    if (!r.ok) throw new Error("status " + r.status);
    cfg = (await r.json())[cliente];
  } catch (e) {
    console.log("não li o aprovacao.json: " + e);
    return responder("Não consegui conferir agora. Tenta de novo em 1 minuto.");
  }
  const quem = String((clique.from || {}).id || "");
  const lugarCerto = cfg && (msg.chat.id === cfg.grupo || AVISAR.includes(msg.chat.id));
  // aprovadores hoje é {número: nome}; o lembrete de 07/11 tira os nomes (lista ou nome vazio): vale qualquer um
  const ap = cfg ? cfg.aprovadores : null;
  const aprova = Array.isArray(ap) ? ap.map(String).includes(quem) : !!ap && Object.prototype.hasOwnProperty.call(ap, quem);
  if (!lugarCerto || !aprova) {
    const nome = (clique.from || {}).first_name || "Alguém";
    const botao = tipo === "mz" ? "Faz outro do zero" : "Sim, estão certos";
    for (const chat of AVISAR) {
      await telegram(env, "sendMessage", { chat_id: chat,
        text: `${cliente} (${mes}): ${nome} (${quem}) clicou em ${botao}, mas não está na lista de aprovadores desse cliente ` +
          `(ou o botão estava fora do grupo dele). Não valeu.` });
    }
    return responder("Só quem está na lista de aprovadores pode clicar aqui.");
  }

  // clique duplo antes de a mensagem mudar: registra uma vez só
  await prepararRespostas(env);
  const ja = await env.BANCO.prepare("SELECT id FROM respostas WHERE origem = ? AND etapa = ? AND IFNULL(post, '') = ?")
    .bind(origem, etapa, post).first();
  if (!ja) {
    await env.BANCO.prepare("INSERT INTO respostas (recebido, cliente, mes, etapa, post, origem) VALUES (?, ?, ?, ?, ?, ?)")
      .bind(agora(), cliente, mes, etapa, post || null, origem).run();
    const hora = new Date().toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", dateStyle: "short", timeStyle: "short" });
    const aviso = await avisoDoPc(env).catch(() => "");
    for (const chat of AVISAR) {
      await telegram(env, "sendMessage", { chat_id: chat,
        text: aviso + `${cliente} (${mes}): o cliente ${ETAPAS[etapa]}${post ? " pro post " + post : ""}, em ${hora} (botão do grupo).` });
    }
  }

  // a mensagem muda: tira os botões desse post; no último, pergunta dos outros ou fecha
  const linhas = ((msg.reply_markup || {}).inline_keyboard || []);
  const urlGravar = (linhas.flat().find((b) => b.url) || {}).url || "";
  const pagina = urlGravar.split("&d=")[0];
  const texto = msg.text || "";
  const FECHO = "Combinado! Te avisamos aqui quando o post novo ficar pronto.";
  let novo, botoes = [];
  if (tipo === "mz") {
    const resto = linhas.filter((l) => !l.some((b) => (b.url || "").includes("post=" + post) || (b.callback_data || "").includes("|" + post + "|")));
    if (resto.length) {
      novo = texto + "\n\nCombinado: o post de " + post.slice(8, 10) + "/" + post.slice(5, 7) + " vai ser refeito do zero.";
      botoes = resto;
    } else if (!fim) {
      novo = texto + "\n\nCombinado, vamos refazer do zero, com outra ideia.\n\nE os outros posts, estão certos?";
      botoes = [[{ text: "Sim, estão certos", callback_data: `mo|${cliente}|${mes}` }]];
      if (pagina) botoes.push([{ text: "Quero mudar outro", url: pagina }]);
    } else {
      novo = texto + "\n\n" + FECHO;
    }
  } else {
    novo = texto + "\n\n" + FECHO;
  }
  if (!ja || linhas.length) {
    await telegram(env, "editMessageText", { chat_id: msg.chat.id, message_id: msg.message_id, text: novo.slice(0, 4000),
      reply_markup: { inline_keyboard: botoes } });
  }
  return responder(tipo === "mz" ? "Combinado! Vamos refazer esse post do zero." : "Combinado! Os outros ficam como estão.");
}

// ---------- Sinal de vida do PC (21/11) ----------
// A vigia do PC (audio_mes.py --vigia) bate em /vivo a cada 2 min. Áudio ou botão que chega com o PC sem
// sinal há mais de 10 min vai pro privado com o aviso na frente: senão o cliente vê "Recebido!" e nada
// acontece, e ninguém fica sabendo.
async function sinalDoPc(pedido, env) {
  if (!autorizado(pedido, env)) return new Response("não", { status: 401 });
  await prepararSinais(env);
  await env.BANCO.prepare("INSERT OR REPLACE INTO sinais (nome, quando) VALUES ('pc', ?)").bind(agora()).run();
  return Response.json({ ok: true });
}

async function prepararSinais(env) {
  await env.BANCO.prepare("CREATE TABLE IF NOT EXISTS sinais (nome TEXT PRIMARY KEY, quando INTEGER NOT NULL)").run();
}

async function avisoDoPc(env) {
  await prepararSinais(env);
  const r = await env.BANCO.prepare("SELECT quando FROM sinais WHERE nome = 'pc'").first();
  if (r && agora() - r.quando <= PC_SEM_SINAL) return "";
  const desde = r ? " desde " + new Date(r.quando * 1000).toLocaleString("pt-BR",
    { timeZone: "America/Sao_Paulo", dateStyle: "short", timeStyle: "short" }) : " (a vigia nunca bateu aqui)";
  return `ATENÇÃO: o PC não dá sinal${desde}. Vigia parada ou PC desligado: isto fica esperando até ela voltar.\n\n`;
}

async function telegram(env, metodo, corpo) {
  const r = await fetch(`https://api.telegram.org/bot${env.TOKEN_BOT}/${metodo}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo),
  });
  const j = await r.json();
  if (!j.ok) console.log(metodo + " falhou: " + JSON.stringify(j).slice(0, 200));
  return j;
}
