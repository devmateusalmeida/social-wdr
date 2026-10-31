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
// O token do bot não fica aqui: o "Recebido" vai dentro da resposta ao
// Telegram, e o webhook é ligado do PC (python sistema/telegram.py caixa ligar).

const REPOSITORIO = "devmateusalmeida/social-wdr"; // trocar quando passar pra conta da WDR
const GUARDAR_LIDAS = 24 * 3600; // segundos: o descobrir ainda vê o que o robô já leu

export default {
  async fetch(pedido, env, ctx) {
    const url = new URL(pedido.url);
    try {
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
    text: "Recebido. A confirmação aparece na mensagem em até 1 min.",
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
