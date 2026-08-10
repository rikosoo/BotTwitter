#!/usr/bin/env python3
"""
DressLikeMe — bot de oportunidades de resposta na X.

v2: visão (lê a imagem do post), busca por intenção de compra, checagem de
catálogo e score de relevância.

Duas fontes:
  A) CONTAS  — perfis de fandom monitorados (Peaky Blinders, Friends...)
  B) BUSCA   — gente perguntando "onde compro essa jaqueta?" agora mesmo.
               Esta é a fonte que converte.

Fluxo: acha post -> lê texto E imagem -> confere se você tem o produto ->
dá nota de 0 a 10 -> se passar do corte, manda rascunho pro Telegram ->
você aprova -> publica.
"""

import base64
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from anthropic import Anthropic
from requests_oauthlib import OAuth1

# ----------------------------------------------------------------------------
# CONFIGURAÇÃO
# ----------------------------------------------------------------------------

CATALOG_PATH = "catalogo.json"
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Fonte A: contas de fandom. Não são contas de moda — são de série/filme.
ACCOUNTS = [
    "PeakyBlinders",
    "FriendsTV",
    "Stranger_Things",
    "cwtvd",
    "HarryPotterFilm",
    "netflix",
]

# Fonte B: frases de intenção de compra. O ouro está aqui.
INTENT_PHRASES = [
    '"where to buy"', '"where can i get"', '"where did she get"',
    '"what jacket"', '"what dress"', '"outfit id"', '"same jacket"',
    '"need that jacket"', '"obsessed with her outfit"',
]

CHECK_INTERVAL = 10 * 60      # 10 min: resposta tardia nasce enterrada
MAX_POST_AGE_MIN = 60         # ignora post mais velho que isso
MIN_SCORE = 7                 # corte de relevância (0-10)
MAX_ALERTS_PER_CYCLE = 6      # teto anti-enxurrada

# Posts originais para o perfil: uma leva de ideias por dia, neste horário (0-23,
# hora local do servidor). A resposta traz o visitante; o perfil é quem converte.
IDEAS_HOUR = 9
IDEAS_PER_BATCH = 3

X_API_KEY = os.environ["X_API_KEY"]
X_API_SECRET = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_SECRET = os.environ["X_ACCESS_SECRET"]
X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

claude = Anthropic()
oauth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)
TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

with open(CATALOG_PATH, encoding="utf-8") as f:
    CATALOG = json.load(f)

# ----------------------------------------------------------------------------
# BANCO
# ----------------------------------------------------------------------------


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                username TEXT PRIMARY KEY, user_id TEXT, last_id TEXT);
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, tweet_id TEXT, author TEXT,
                opt_a TEXT, opt_b TEXT, product TEXT, score INTEGER,
                message_id INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS seen (tweet_id TEXT PRIMARY KEY, at TEXT);
            CREATE TABLE IF NOT EXISTS ideas (
                id TEXT PRIMARY KEY, text TEXT, image_url TEXT, product TEXT,
                message_id INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)


def already_seen(tweet_id):
    """Evita reprocessar o mesmo post vindo das duas fontes."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM seen WHERE tweet_id = ?", (tweet_id,)).fetchone():
            return True
        conn.execute("INSERT INTO seen VALUES (?, ?)",
                     (tweet_id, datetime.now(timezone.utc).isoformat()))
    return False


def recent_replies(limit=8):
    """Últimas respostas publicadas — evita repetir ângulo e abertura."""
    with db() as conn:
        rows = conn.execute(
            "SELECT opt_a FROM drafts WHERE status = 'posted' "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [r["opt_a"] for r in rows if r["opt_a"]]


def meta_get(k, default=None):
    with db() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(k, v):
    with db() as conn:
        conn.execute("INSERT INTO meta VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v = ?", (k, str(v), str(v)))


# ----------------------------------------------------------------------------
# CATÁLOGO
# ----------------------------------------------------------------------------


def catalog_terms():
    terms = set()
    for p in CATALOG:
        terms.add(p["character"])
        terms.add(p["show"])
    return sorted(terms)


def match_products(text):
    low = text.lower()
    hits = []
    for p in CATALOG:
        keys = [p["character"].lower(), p["show"].lower()] + \
               [k.lower() for k in p.get("keywords", [])]
        if any(k in low for k in keys):
            hits.append(p)
    return hits


def build_intent_queries():
    """Combina intenção + termos do catálogo, respeitando o limite de tamanho."""
    intents = " OR ".join(INTENT_PHRASES)
    terms = [f'"{t}"' for t in catalog_terms()]
    queries, block = [], []

    for term in terms:
        block.append(term)
        if len(f'({intents}) ({" OR ".join(block)}) -is:retweet') > 450:
            block.pop()
            queries.append(f'({intents}) ({" OR ".join(block)}) -is:retweet')
            block = [term]
    if block:
        queries.append(f'({intents}) ({" OR ".join(block)}) -is:retweet')
    return queries


# ----------------------------------------------------------------------------
# API DA X
# ----------------------------------------------------------------------------

MEDIA_PARAMS = {
    "tweet.fields": "created_at,public_metrics,attachments",
    "expansions": "attachments.media_keys,author_id",
    "media.fields": "url,preview_image_url,type",
    "user.fields": "username",
}


def x_get(path, params=None):
    r = requests.get(f"https://api.twitter.com/2/{path}",
                     headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
                     params=params or {}, timeout=30)
    if r.status_code == 429:
        print("[x] rate limit — pulando")
        return None
    r.raise_for_status()
    return r.json()


def parse_posts(data):
    """Normaliza: junta post + autor + URL da imagem."""
    if not data or "data" not in data:
        return []

    includes = data.get("includes", {})
    media = {m["media_key"]: m for m in includes.get("media", [])}
    users = {u["id"]: u["username"] for u in includes.get("users", [])}

    out = []
    for t in data["data"]:
        img = None
        for k in t.get("attachments", {}).get("media_keys", []):
            m = media.get(k, {})
            img = m.get("url") or m.get("preview_image_url")
            if img:
                break
        out.append({
            "id": t["id"],
            "text": t["text"],
            "author": users.get(t.get("author_id"), ""),
            "created_at": t.get("created_at"),
            "image": img,
            "metrics": t.get("public_metrics", {}),
        })
    return out


def is_fresh(post):
    if not post.get("created_at"):
        return True
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created < timedelta(minutes=MAX_POST_AGE_MIN)


def fetch_from_accounts():
    posts = []
    for username in ACCOUNTS:
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT user_id, last_id FROM accounts WHERE username = ?",
                    (username,)).fetchone()

            user_id = row["user_id"] if row else None
            if not user_id:
                data = x_get(f"users/by/username/{username}")
                if not data or "data" not in data:
                    continue
                user_id = data["data"]["id"]
                with db() as conn:
                    conn.execute(
                        "INSERT INTO accounts (username, user_id) VALUES (?, ?) "
                        "ON CONFLICT(username) DO UPDATE SET user_id = ?",
                        (username, user_id, user_id))

            last_id = row["last_id"] if row else None
            params = {**MEDIA_PARAMS, "max_results": 5, "exclude": "retweets,replies"}
            if last_id:
                params["since_id"] = last_id

            batch = parse_posts(x_get(f"users/{user_id}/tweets", params))
            if batch:
                newest = max(int(p["id"]) for p in batch)
                with db() as conn:
                    conn.execute("UPDATE accounts SET last_id = ? WHERE username = ?",
                                 (str(newest), username))
            if last_id:  # primeira rodada só define baseline
                for p in batch:
                    p["author"] = p["author"] or username
                posts += batch
        except Exception as e:
            print(f"[erro] conta @{username}: {e}")
    return posts


def fetch_from_intent():
    posts = []
    for query in build_intent_queries():
        try:
            posts += parse_posts(x_get("tweets/search/recent",
                                       {**MEDIA_PARAMS, "query": query,
                                        "max_results": 15}))
        except Exception as e:
            print(f"[erro] busca: {e}")
    return posts


def post_reply(tweet_id, text):
    r = requests.post("https://api.twitter.com/2/tweets", auth=oauth,
                      json={"text": text, "reply": {"in_reply_to_tweet_id": tweet_id}},
                      timeout=30)
    if r.status_code >= 300:
        return None, f"{r.status_code}: {r.text[:200]}"
    return r.json()["data"]["id"], None


def upload_media(image_url):
    """
    Sobe uma imagem pra X e devolve o media_id.
    Usa o endpoint v1.1 — a v2 ainda não faz upload de mídia.
    """
    try:
        img = requests.get(image_url, timeout=30)
        img.raise_for_status()
        r = requests.post("https://upload.twitter.com/1.1/media/upload.json",
                          auth=oauth, files={"media": img.content}, timeout=60)
        r.raise_for_status()
        return r.json()["media_id_string"]
    except Exception as e:
        print(f"[media] upload falhou: {e}")
        return None


def post_tweet(text, image_url=None):
    """Publica um post original no perfil (não é resposta)."""
    payload = {"text": text}
    if image_url:
        media_id = upload_media(image_url)
        if media_id:
            payload["media"] = {"media_ids": [media_id]}

    r = requests.post("https://api.twitter.com/2/tweets", auth=oauth,
                      json=payload, timeout=30)
    if r.status_code >= 300:
        return None, f"{r.status_code}: {r.text[:200]}"
    return r.json()["data"]["id"], None


def fetch_image_b64(url):
    """Baixa a imagem pro Claude conseguir olhar a cena."""
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        if len(r.content) > 4_000_000:
            return None, None
        media_type = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return base64.b64encode(r.content).decode(), media_type
    except Exception as e:
        print(f"[img] falhou: {e}")
        return None, None


# ----------------------------------------------------------------------------
# ANÁLISE + RASCUNHO (uma única chamada ao Claude)
# ----------------------------------------------------------------------------

PROMPT = """Você ajuda o dono do DressLikeMe, um site que identifica roupas usadas por
personagens de filmes e séries e aponta onde comprar peças parecidas. Ele responde
posts na X quando tem algo realmente útil a dizer. Ele NÃO é vendedor: é o cara que
sabe identificar a peça da cena.

POST DE @{author}:
\"\"\"{text}\"\"\"
{image_note}

PRODUTOS QUE ELE TEM PARA ESTE CASO:
{products}

RESPOSTAS RECENTES DELE (não repita ângulo, estrutura nem abertura):
{recent}

TAREFA
1. Dê uma nota de 0 a 10 para a oportunidade:
   10 = alguém perguntando explicitamente onde comprar uma peça que ele TEM
    7 = post sobre um look de personagem que ele cobre, com espaço para ajudar
    3 = post do fandom sem relação com roupa
    0 = polêmica, notícia triste, política, ou nada a ver
   Se a lista de produtos estiver vazia, o teto é 5.
2. Se a nota for 7 ou mais, escreva DOIS rascunhos:
   A: identifica a peça de forma concreta e útil (tipo, corte, detalhe da cena)
   B: outro ângulo — um detalhe que só quem conhece a produção saberia,
      ou uma alternativa mais barata para o mesmo look

REGRAS DOS RASCUNHOS
- Máximo 240 caracteres. Mesmo idioma do post.
- NUNCA inclua link. NUNCA cite o nome do site. NUNCA convide para comprar.
- Sem hashtag, sem emoji, sem elogio genérico.
- Tem que soar como fã que manja de figurino, não como loja.
- Se não tiver certeza da peça, não invente marca — descreva o tipo.

Responda APENAS com JSON, sem markdown:
{{"score": 0, "motivo": "curto", "a": "", "b": ""}}"""


def analyze(post, products):
    image_note = ""
    content = []

    if post.get("image"):
        b64, media_type = fetch_image_b64(post["image"])
        if b64:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": b64}})
            image_note = "\n(A imagem do post está anexada — olhe a roupa na cena.)"

    prod_txt = "\n".join(
        f"- {p['name']} ({p['character']}, {p['show']})" for p in products
    ) or "nenhum produto do catálogo bate com este post"

    content.append({"type": "text", "text": PROMPT.format(
        author=post["author"], text=post["text"], image_note=image_note,
        products=prod_txt, recent="\n".join(recent_replies()) or "nenhuma ainda")})

    msg = claude.messages.create(
        model="claude-sonnet-5", max_tokens=700,
        messages=[{"role": "user", "content": content}])

    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[claude] JSON inválido: {raw[:150]}")
        return {"score": 0}


# ----------------------------------------------------------------------------
# POSTS ORIGINAIS PARA O PERFIL
# ----------------------------------------------------------------------------

IDEAS_PROMPT = """Você escreve posts para o perfil do DressLikeMe na X — uma conta que
identifica roupas de personagens de filmes e séries e mostra como montar o look.

O QUE ESTÁ EM ALTA HOJE nas contas de fandom monitoradas:
{trending}

CATÁLOGO DISPONÍVEL:
{catalog}

POSTS RECENTES DO PERFIL (não repita tema, formato nem abertura):
{recent}

Escreva {n} posts originais, cada um sobre um produto DIFERENTE do catálogo.

FORMATOS QUE FUNCIONAM (escolha um diferente para cada post):
- Detalhe de figurino que ninguém nota ("a jaqueta do Tommy muda de corte na 4ª
  temporada e tem motivo narrativo pra isso")
- Como montar o look com 3 peças
- Comparação de época ("o que a Rachel usava em 1996 voltou exatamente assim")
- Curiosidade de produção sobre a peça

REGRAS
- Máximo 260 caracteres. Em inglês (a audiência é majoritariamente dos EUA).
- SEM link e SEM hashtag no texto.
- Abre com o gancho, não com contexto. Primeira linha decide se alguém para.
- Não use "Did you know" nem pergunta retórica de abertura.
- Nada de linguagem de anúncio. É um perfil de fã que entende de figurino.
- Se não souber um detalhe de produção com certeza, não invente — fale do corte,
  do tecido ou da montagem do look, que é observável.

Responda APENAS com JSON, sem markdown:
{{"posts": [{{"slug": "slug-do-produto", "text": "..."}}]}}"""


def recent_ideas(limit=10):
    with db() as conn:
        rows = conn.execute(
            "SELECT text FROM ideas WHERE status='posted' "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [r["text"] for r in rows]


def generate_ideas(trending_posts):
    trending = "\n".join(f"- @{p['author']}: {p['text'][:140]}"
                         for p in trending_posts[:10]) or "nada relevante hoje"
    catalog = "\n".join(
        f"- {p['slug']}: {p['name']} ({p['character']}, {p['show']})" for p in CATALOG)

    msg = claude.messages.create(
        model="claude-sonnet-5", max_tokens=1200,
        messages=[{"role": "user", "content": IDEAS_PROMPT.format(
            trending=trending, catalog=catalog, n=IDEAS_PER_BATCH,
            recent="\n".join(recent_ideas()) or "nenhum ainda")}])

    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw).get("posts", [])
    except json.JSONDecodeError:
        print(f"[claude] ideias com JSON inválido: {raw[:150]}")
        return []


def send_ideas(trending_posts):
    by_slug = {p["slug"]: p for p in CATALOG}

    for idea in generate_ideas(trending_posts):
        product = by_slug.get(idea.get("slug"), {})
        image_url = product.get("image_url")
        idea_id = uuid.uuid4().hex[:8]

        body = (f"📝 <b>Post para o perfil</b>\n"
                f"🎬 {product.get('name', '—')}\n\n"
                f"{idea['text']}\n\n"
                f"<i>{'com imagem do produto' if image_url else 'sem imagem'}"
                f" · ou responda com seu próprio texto</i>")

        result = tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=body,
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[
                        {"text": "✅ Publicar", "callback_data": f"ip:{idea_id}"},
                        {"text": "🗑", "callback_data": f"ix:{idea_id}"}]]})
        if not result:
            continue

        with db() as conn:
            conn.execute(
                "INSERT INTO ideas (id, text, image_url, product, message_id, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (idea_id, idea["text"], image_url, product.get("name", ""),
                 result["message_id"], datetime.now(timezone.utc).isoformat()))
        print(f"  💡 ideia enviada: {idea['text'][:60]}")


def publish_idea(idea, text, chat_id):
    tweet_id, err = post_tweet(text, idea["image_url"])
    if err:
        tg("sendMessage", chat_id=chat_id, text=f"❌ Falhou\n{err}")
        return
    with db() as conn:
        conn.execute("UPDATE ideas SET status='posted', text=? WHERE id=?",
                     (text, idea["id"]))
    tg("sendMessage", chat_id=chat_id, text=f"✅ https://x.com/i/status/{tweet_id}")


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------


def tg(method, **payload):
    r = requests.post(f"{TG}/{method}", json=payload, timeout=30)
    if r.status_code >= 300:
        print(f"[tg] {method}: {r.text[:150]}")
        return None
    return r.json().get("result")


def send_draft(post, analysis, products):
    draft_id = uuid.uuid4().hex[:8]
    url = f"https://x.com/{post['author']}/status/{post['id']}"
    prod = products[0]["name"] if products else "—"

    body = (
        f"⭐ <b>{analysis['score']}/10</b> · {analysis.get('motivo', '')}\n"
        f"🎬 produto: {prod}\n\n"
        f"🐦 <b>@{post['author']}</b>\n<i>{post['text'][:350]}</i>\n\n"
        f"<b>A)</b> {analysis['a']}\n\n<b>B)</b> {analysis['b']}\n\n"
        f'<a href="{url}">ver post</a> · <i>ou responda com seu próprio texto</i>'
    )

    result = tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=body, parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ A", "callback_data": f"a:{draft_id}"},
                    {"text": "✅ B", "callback_data": f"b:{draft_id}"},
                    {"text": "🗑", "callback_data": f"x:{draft_id}"}]]})
    if not result:
        return

    with db() as conn:
        conn.execute(
            "INSERT INTO drafts (id, tweet_id, author, opt_a, opt_b, product, score,"
            " message_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (draft_id, post["id"], post["author"], analysis["a"], analysis["b"],
             prod, analysis["score"], result["message_id"],
             datetime.now(timezone.utc).isoformat()))


def publish(draft, text, chat_id):
    reply_id, err = post_reply(draft["tweet_id"], text)
    if err:
        tg("sendMessage", chat_id=chat_id, text=f"❌ Falhou\n{err}")
        return
    with db() as conn:
        conn.execute("UPDATE drafts SET status='posted', opt_a=? WHERE id=?",
                     (text, draft["id"]))
    tg("sendMessage", chat_id=chat_id, text=f"✅ https://x.com/i/status/{reply_id}")


def handle_callback(cb):
    action, draft_id = cb["data"].split(":", 1)
    chat_id = cb["message"]["chat"]["id"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])

    # ideias de post original (prefixos ip / ix)
    if action in ("ip", "ix"):
        with db() as conn:
            idea = conn.execute("SELECT * FROM ideas WHERE id=?", (draft_id,)).fetchone()
        if not idea or idea["status"] != "pending":
            return
        if action == "ix":
            with db() as conn:
                conn.execute("UPDATE ideas SET status='skipped' WHERE id=?", (draft_id,))
            tg("editMessageReplyMarkup", chat_id=chat_id,
               message_id=cb["message"]["message_id"],
               reply_markup={"inline_keyboard": []})
        else:
            publish_idea(idea, idea["text"], chat_id)
        return

    with db() as conn:
        draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not draft or draft["status"] != "pending":
        return

    if action == "x":
        with db() as conn:
            conn.execute("UPDATE drafts SET status='skipped' WHERE id=?", (draft_id,))
        tg("editMessageReplyMarkup", chat_id=chat_id,
           message_id=cb["message"]["message_id"], reply_markup={"inline_keyboard": []})
        return

    publish(draft, draft["opt_a"] if action == "a" else draft["opt_b"], chat_id)


def poll_telegram():
    offset = int(meta_get("tg_offset", 0))
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates",
                             params={"offset": offset, "timeout": 50}, timeout=60)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                meta_set("tg_offset", offset)
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif "message" in u and "reply_to_message" in u["message"]:
                    mid = u["message"]["reply_to_message"]["message_id"]
                    chat = u["message"]["chat"]["id"]
                    text = u["message"]["text"]
                    with db() as conn:
                        d = conn.execute(
                            "SELECT * FROM drafts WHERE message_id=? AND status='pending'",
                            (mid,)).fetchone()
                        i = conn.execute(
                            "SELECT * FROM ideas WHERE message_id=? AND status='pending'",
                            (mid,)).fetchone()
                    if d:
                        publish(d, text, chat)
                    elif i:
                        publish_idea(i, text, chat)
        except Exception as e:
            print(f"[tg] polling: {e}")
            time.sleep(5)


# ----------------------------------------------------------------------------
# CICLO
# ----------------------------------------------------------------------------


def maybe_send_ideas(trending):
    """Uma leva de ideias por dia, no horário configurado."""
    today = datetime.now().strftime("%Y-%m-%d")
    if datetime.now().hour < IDEAS_HOUR or meta_get("last_ideas") == today:
        return
    print("[ideias] gerando leva do dia")
    send_ideas(trending)
    meta_set("last_ideas", today)


def cycle():
    posts = fetch_from_intent() + fetch_from_accounts()
    maybe_send_ideas(posts)
    candidates = [p for p in posts if is_fresh(p) and not already_seen(p["id"])]
    print(f"[ciclo] {len(posts)} posts, {len(candidates)} novos e recentes")

    scored = []
    for post in candidates:
        try:
            products = match_products(post["text"])
            analysis = analyze(post, products)
            if analysis.get("score", 0) >= MIN_SCORE and analysis.get("a"):
                scored.append((analysis["score"], post, analysis, products))
            else:
                print(f"  descartado ({analysis.get('score')}): {post['text'][:60]}")
        except Exception as e:
            print(f"[erro] análise {post['id']}: {e}")

    # manda só os melhores — notificação demais mata o hábito de usar
    scored.sort(key=lambda x: -x[0])
    for _, post, analysis, products in scored[:MAX_ALERTS_PER_CYCLE]:
        send_draft(post, analysis, products)
        print(f"  ✔ enviado ({analysis['score']}/10) @{post['author']}")


def main():
    init_db()
    threading.Thread(target=poll_telegram, daemon=True).start()
    tg("sendMessage", chat_id=TELEGRAM_CHAT_ID,
       text=f"🤖 v2 no ar · {len(CATALOG)} produtos · corte {MIN_SCORE}/10")
    while True:
        print(f"\n=== {datetime.now():%H:%M} ===")
        try:
            cycle()
        except Exception as e:
            print(f"[erro] ciclo: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
