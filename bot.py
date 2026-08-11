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
import html
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
    '"where did he get"', '"what jacket"', '"what dress"', '"what coat"',
    '"outfit id"', '"same jacket"', '"need that jacket"', '"need this coat"',
    '"obsessed with her outfit"', '"where is her dress from"',
    '"how to dress like"', '"dress like"', '"recreate her look"',
    '"outfit inspo"', '"looking for a jacket like"',
]

# Vocabulário de roupa usado NAS BUSCAS: é o que separa conversa de fandom sobre
# figurino de conversa de fandom sobre enredo. Sem isso, "buscar por fandom" traz
# spoiler, ator e teoria — e cada post inútil custa uma análise de imagem.
GARMENT_TERMS = [
    "outfit", "jacket", "coat", "dress", "skirt", "sweater", "blazer",
    "costume", "wardrobe", "wearing", "style", "boots", "look",
]

CHECK_INTERVAL = 10 * 60      # 10 min: resposta tardia nasce enterrada
MAX_POST_AGE_MIN = 60         # post de timeline de fandom envelhece rápido
MAX_INTENT_AGE_MIN = 6 * 60   # pergunta de compra sem resposta continua valendo
MIN_SCORE = 7                 # corte de relevância do Claude (0-10)
MAX_ALERTS_PER_CYCLE = 6      # teto anti-enxurrada no Telegram
SEEN_RETENTION_DAYS = 7       # limpeza da tabela seen

# Teto de gasto: cada análise é uma chamada de visão do Claude. Alargar a busca
# sem alargar isto é como o custo saía do controle.
SEARCH_RESULTS_PER_QUERY = 20
MAX_ANALYSIS_PER_CYCLE = 25
PRE_MIN_SCORE = 4             # corte do pré-filtro local, que é de graça

# A X cobra por post LIDO e o plano tem cota mensal. A camada fandom é ampla e
# pode devolver o máximo em todo ciclo: 20 × 144 ciclos/dia estoura qualquer
# plano de entrada. Este teto é o freio — ao bater, o bot para de buscar até
# meia-noite em vez de gerar conta ou bloqueio.
MAX_READS_PER_DAY = 300

# Camada 3: intenção de compra + roupa, SEM exigir termo do catálogo. Pega o
# "where can I get this coat" embaixo de um print sem legenda — só a imagem
# identifica a série. Recall alto e precisão baixa: ligue depois de calibrar as
# duas primeiras camadas, e olhando a conta de custo.
ENABLE_GENERIC_TIER = False

# Posts originais para o perfil: uma leva de ideias por dia, neste horário (0-23,
# hora local do servidor). A resposta traz o visitante; o perfil é quem converte.
IDEAS_HOUR = 9
IDEAS_PER_BATCH = 3

REQUIRED_ENV = [
    "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET",
    "X_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
]

missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if missing:
    raise SystemExit(
        "Faltam variáveis de ambiente: " + ", ".join(missing) +
        "\nVeja o README. Nenhuma delas vai no código nem no Git.")

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

REQUIRED_PRODUCT_FIELDS = ("slug", "name", "character", "show")
for _p in CATALOG:
    _faltando = [f for f in REQUIRED_PRODUCT_FIELDS if not _p.get(f)]
    if _faltando:
        raise SystemExit(
            f"catalogo.json: produto {_p.get('slug', '?')} sem {', '.join(_faltando)}")

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
        return conn.execute(
            "SELECT 1 FROM seen WHERE tweet_id = ?", (tweet_id,)).fetchone() is not None


def mark_seen(tweet_id):
    """
    Marca depois da análise, não antes: se a chamada ao Claude falhar (rate limit,
    timeout), o post continua elegível no próximo ciclo em vez de sumir para sempre.
    """
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                     (tweet_id, datetime.now(timezone.utc).isoformat()))


def cleanup_seen(days=SEEN_RETENTION_DAYS):
    """A tabela seen só precisa cobrir a janela de frescor. O resto é peso morto."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM seen WHERE at < ?", (cutoff,))


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


# Termos de uma palavra só que também são palavras comuns em inglês. "Friends" e
# "Eleven" casariam com metade da timeline se valessem sozinhos, e cada falso
# positivo custa uma chamada de visão do Claude. Só contam se o post também falar
# de roupa.
AMBIGUOUS_TERMS = {"friends", "eleven", "it", "you", "us", "him", "her"}

GARMENT_WORDS = [
    "outfit", "look", "wear", "wearing", "wore", "style", "styled", "fashion",
    "jacket", "coat", "blazer", "dress", "skirt", "jeans", "pants", "trousers",
    "sweater", "jumper", "cardigan", "shirt", "blouse", "top", "hoodie",
    "boots", "shoes", "sneakers", "bag", "hat", "cap", "costume", "uniform",
    "closet", "wardrobe", "fit", "fits",
]


def _mentions(text_low, term):
    """Casa por palavra inteira: 'cap' não pode casar dentro de 'capture'."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text_low) is not None


def mentions_clothing(text_low):
    return any(_mentions(text_low, w) for w in GARMENT_WORDS)


def match_products(text):
    low = text.lower()
    about_clothes = mentions_clothing(low)
    hits = []

    for p in CATALOG:
        keys = [p["character"].lower(), p["show"].lower()] + \
               [k.lower() for k in p.get("keywords", [])]
        for k in keys:
            if not _mentions(low, k):
                continue
            # termo ambíguo de uma palavra só vale se o post for sobre roupa
            if k in AMBIGUOUS_TERMS and not about_clothes:
                continue
            hits.append(p)
            break
    return hits


MAX_QUERY_LEN = 450
QUERY_SUFFIX = "-is:retweet lang:en"


def _pack(fixed_group, terms, suffix=QUERY_SUFFIX):
    """
    Monta `(grupo fixo) (termos) sufixo`, quebrando em várias queries quando passa
    do limite de tamanho da X.
    """
    out, block = [], []
    for term in terms:
        block.append(term)
        if len(f'{fixed_group} ({" OR ".join(block)}) {suffix}') > MAX_QUERY_LEN \
                and len(block) > 1:
            block.pop()
            out.append(f'{fixed_group} ({" OR ".join(block)}) {suffix}')
            block = [term]
    if block:
        out.append(f'{fixed_group} ({" OR ".join(block)}) {suffix}')
    return out


def build_queries():
    """
    Três camadas, da mais precisa para a mais ampla:

      intent   frase de compra + personagem/série — quem pergunta e diz de quê
      fandom   personagem/série + palavra de roupa — quem comenta o figurino
               sem usar frase de compra ("Rachel's blazer in this scene")
      generic  frase de compra + roupa, sem catálogo, só com imagem — a legenda
               não diz a série, a imagem diz. Desligado por padrão.

    A camada vira prioridade no pré-filtro: com orçamento limitado de análise,
    a pergunta explícita passa na frente do comentário casual.
    """
    intents = "(" + " OR ".join(INTENT_PHRASES) + ")"
    garments = "(" + " OR ".join(GARMENT_TERMS) + ")"
    terms = [f'"{t}"' for t in catalog_terms()]

    queries = [("intent", q) for q in _pack(intents, terms)]
    queries += [("fandom", q) for q in _pack(garments, terms)]
    if ENABLE_GENERIC_TIER:
        queries.append(
            ("generic", f"{intents} {garments} has:media {QUERY_SUFFIX}"))
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
    """
    Uma pergunta de compra sem resposta continua valendo horas depois; um post de
    timeline de fandom, não. Prazos diferentes por fonte.
    """
    if not post.get("created_at"):
        return True
    limit = MAX_INTENT_AGE_MIN if post.get("source") in ("intent", "generic") \
        else MAX_POST_AGE_MIN
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created < timedelta(minutes=limit)


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
                    p["source"] = "accounts"
                posts += batch
        except Exception as e:
            print(f"[erro] conta @{username}: {e}")
    return posts


def reads_today():
    return int(meta_get(f"reads:{datetime.now(timezone.utc):%Y-%m-%d}", 0))


def add_reads(n):
    key = f"reads:{datetime.now(timezone.utc):%Y-%m-%d}"
    meta_set(key, reads_today() + n)


def fetch_from_search():
    posts = []
    for tier, query in build_queries():
        if reads_today() >= MAX_READS_PER_DAY:
            print(f"[cota] teto diário de {MAX_READS_PER_DAY} posts lidos atingido")
            break
        try:
            batch = parse_posts(x_get("tweets/search/recent",
                                      {**MEDIA_PARAMS, "query": query,
                                       "max_results": SEARCH_RESULTS_PER_QUERY}))
            add_reads(len(batch))
            for p in batch:
                p["source"] = tier
            posts += batch
        except Exception as e:
            print(f"[erro] busca {tier}: {e}")
    return posts


# pontuação do pré-filtro: quanto vale cada sinal antes de gastar visão
TIER_BONUS = {"intent": 3, "generic": 2, "fandom": 1, "accounts": 0}


def has_intent(text_low):
    return any(_mentions(text_low, ph.strip('"')) for ph in INTENT_PHRASES)


def prescore(post, products):
    """
    Triagem local, de graça, antes de mandar pro Claude. O ranking decide quem
    ganha as MAX_ANALYSIS_PER_CYCLE chamadas de visão disponíveis — sem isso,
    alargar a busca só multiplica a conta da API.
    """
    low = post["text"].lower()
    intent, clothing = has_intent(low), mentions_clothing(low)

    # Duas regras duras, antes de qualquer pontuação:
    # sem falar de roupa e sem perguntar onde comprar não há o que responder,
    # nem que o post cite a série ("the vampire diaries finale made me cry");
    if not (intent or clothing):
        return 0, "sem roupa nem intenção"
    # e intenção de compra sem roupa nem produto é sobre outra coisa
    # ("where to buy tickets for the tour").
    if not (clothing or products):
        return 0, "intenção fora do nicho"

    score, why = TIER_BONUS.get(post.get("source", "accounts"), 0), []

    if intent:
        score += 4
        why.append("intenção")
    if any(_mentions(low, p["character"].lower()) for p in products):
        score += 3
        why.append("personagem")
    elif products:
        score += 1
        why.append("série")
    if clothing:
        score += 2
        why.append("roupa")
    if post.get("image"):
        score += 1
        why.append("imagem")
    # pergunta com poucas respostas ainda não foi respondida — é onde você entra
    if post.get("metrics", {}).get("reply_count", 0) < 20:
        score += 1
        why.append("pouca concorrência")

    return score, "+".join(why) or "nada"


def post_reply(tweet_id, text):
    r = requests.post("https://api.twitter.com/2/tweets", auth=oauth,
                      json={"text": text, "reply": {"in_reply_to_tweet_id": tweet_id}},
                      timeout=30)
    if r.status_code >= 300:
        return None, f"{r.status_code}: {r.text[:200]}"
    return r.json()["data"]["id"], None


MEDIA_ENDPOINTS = [
    "https://api.x.com/2/media/upload",                    # atual
    "https://upload.twitter.com/1.1/media/upload.json",    # legado, em descontinuação
]


def upload_media(image_url):
    """
    Sobe uma imagem pra X e devolve o media_id.
    Tenta o endpoint v2 e cai no v1.1 se ele não responder — a X migrou o upload
    e o legado pode sumir a qualquer momento.
    """
    try:
        img = requests.get(image_url, timeout=30)
        img.raise_for_status()
    except Exception as e:
        print(f"[media] não consegui baixar {image_url}: {e}")
        return None

    for endpoint in MEDIA_ENDPOINTS:
        try:
            r = requests.post(endpoint, auth=oauth,
                              files={"media": img.content}, timeout=60)
            r.raise_for_status()
            data = r.json()
            media_id = data.get("media_id_string") or data.get("id") or \
                data.get("data", {}).get("id")
            if media_id:
                return str(media_id)
            print(f"[media] {endpoint} respondeu sem media_id: {str(data)[:120]}")
        except Exception as e:
            print(f"[media] {endpoint} falhou: {e}")
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
                f"🎬 {html.escape(product.get('name', '—'))}\n\n"
                f"{html.escape(idea['text'])}\n\n"
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

    # o Claude nem sempre devolve os dois rascunhos; o alerta não pode morrer por isso
    opt_a = (analysis.get("a") or "").strip()
    opt_b = (analysis.get("b") or "").strip()

    origem = {"intent": "pergunta de compra", "fandom": "comentário de figurino",
              "generic": "compra sem série identificada",
              "accounts": "conta monitorada"}.get(post.get("source"), "—")

    body = (
        f"⭐ <b>{analysis.get('score', '?')}/10</b> · {analysis.get('motivo', '')}\n"
        f"🔎 {origem}\n"
        f"🎬 produto: {prod}\n\n"
        f"🐦 <b>@{post['author']}</b>\n<i>{html.escape(post['text'][:350])}</i>\n\n"
        f"<b>A)</b> {html.escape(opt_a)}\n"
        + (f"\n<b>B)</b> {html.escape(opt_b)}\n" if opt_b else "")
        + f'\n<a href="{url}">ver post</a> · <i>ou responda com seu próprio texto</i>'
    )

    buttons = [{"text": "✅ A", "callback_data": f"a:{draft_id}"}]
    if opt_b:
        buttons.append({"text": "✅ B", "callback_data": f"b:{draft_id}"})
    buttons.append({"text": "🗑", "callback_data": f"x:{draft_id}"})

    result = tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=body, parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                reply_markup={"inline_keyboard": [buttons]})
    if not result:
        return

    with db() as conn:
        conn.execute(
            "INSERT INTO drafts (id, tweet_id, author, opt_a, opt_b, product, score,"
            " message_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (draft_id, post["id"], post["author"], opt_a, opt_b,
             prod, analysis.get("score"), result["message_id"],
             datetime.now(timezone.utc).isoformat()))


def publish(draft, text, chat_id):
    if not (text or "").strip():
        tg("sendMessage", chat_id=chat_id, text="❌ Rascunho vazio, nada publicado.")
        return
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
    cleanup_seen()
    posts = fetch_from_search() + fetch_from_accounts()
    maybe_send_ideas(posts)

    # dedupe dentro do próprio ciclo: o mesmo post pode vir de várias camadas
    candidates, batch_ids = [], set()
    for p in posts:
        if p["id"] in batch_ids or not is_fresh(p) or already_seen(p["id"]):
            continue
        batch_ids.add(p["id"])
        candidates.append(p)

    # triagem local antes de gastar visão. Reprovado aqui já pode ir pro seen:
    # a decisão é determinística, reavaliar no próximo ciclo daria o mesmo.
    ranked = []
    for p in candidates:
        products = match_products(p["text"])
        pre, why = prescore(p, products)
        if pre >= PRE_MIN_SCORE:
            ranked.append((pre, why, p, products))
        else:
            mark_seen(p["id"])

    ranked.sort(key=lambda r: -r[0])
    budget = ranked[:MAX_ANALYSIS_PER_CYCLE]
    print(f"[ciclo] {len(posts)} coletados · {len(candidates)} novos · "
          f"{len(ranked)} passaram no pré-filtro · {len(budget)} analisados")

    scored = []
    for pre, why, post, products in budget:
        try:
            analysis = analyze(post, products)
            mark_seen(post["id"])  # só depois de analisar de verdade
            if analysis.get("score", 0) >= MIN_SCORE and analysis.get("a"):
                scored.append((analysis["score"], post, analysis, products))
            else:
                print(f"  descartado ({analysis.get('score')}, pré {pre} {why}): "
                      f"{post['text'][:60]}")
        except Exception as e:
            print(f"[erro] análise {post['id']}: {e}")

    # manda só os melhores — notificação demais mata o hábito de usar
    scored.sort(key=lambda x: -x[0])
    for _, post, analysis, products in scored[:MAX_ALERTS_PER_CYCLE]:
        send_draft(post, analysis, products)
        print(f"  ✔ enviado ({analysis['score']}/10) @{post['author']}")


def check():
    """
    Verificação de fumaça: confere credenciais e conectividade sem publicar nada.
    Rode isto ANTES do primeiro ciclo de verdade — `python bot.py --check`.
    """
    ok = True
    print(f"catálogo: {len(CATALOG)} produtos, "
          f"{len({p['character'] for p in CATALOG})} personagens")
    queries = build_queries()
    for tier in ("intent", "fandom", "generic"):
        n = sum(1 for t, _ in queries if t == tier)
        print(f"  camada {tier}: {n} query(s)")

    me = tg("getMe")
    print(f"telegram: {'@' + me['username'] if me else 'FALHOU'}")
    ok &= bool(me)

    sent = tg("sendMessage", chat_id=TELEGRAM_CHAT_ID,
              text="🔍 Teste de conexão — nada foi publicado na X.")
    print(f"telegram chat_id: {'ok' if sent else 'FALHOU — confira TELEGRAM_CHAT_ID'}")
    ok &= bool(sent)

    try:
        r = requests.get("https://api.twitter.com/2/users/me", auth=oauth, timeout=30)
        if r.status_code < 300:
            print(f"x (escrita): ok, conta @{r.json()['data']['username']}")
        else:
            print(f"x (escrita): FALHOU {r.status_code} — {r.text[:150]}")
            print("  403 aqui = app não está como Read and write, ou o Access Token")
            print("  foi gerado antes da permissão. Regenere o token no portal.")
            ok = False
    except Exception as e:
        print(f"x (escrita): FALHOU {e}")
        ok = False

    try:
        for tier, query in queries:
            data = x_get("tweets/search/recent",
                         {**MEDIA_PARAMS, "query": query, "max_results": 10})
            found = parse_posts(data)
            for p in found:
                p["source"] = tier
            print(f"x (leitura) camada {tier}: {len(found)} posts")
            for p in found[:2]:
                pre, why = prescore(p, match_products(p["text"]))
                print(f"    pré {pre} ({why}) @{p['author']}: {p['text'][:70]}")
    except Exception as e:
        print(f"x (leitura): FALHOU {e}")
        ok = False

    try:
        claude.messages.create(model="claude-sonnet-5", max_tokens=10,
                               messages=[{"role": "user", "content": "responda: ok"}])
        print("claude: ok")
    except Exception as e:
        print(f"claude: FALHOU {e}")
        ok = False

    print("\n" + ("tudo pronto." if ok else "corrija os itens acima antes de rodar."))
    return ok


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
    import sys

    if "--check" in sys.argv:
        init_db()
        raise SystemExit(0 if check() else 1)
    main()
