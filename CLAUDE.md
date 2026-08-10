# Contexto do projeto — bot DressLikeMe

Este arquivo existe para que o Claude Code entenda o projeto sem precisar da conversa
original. Leia antes de mexer em qualquer coisa.

## O negócio

**dresslikeme.store** — site de afiliados que identifica roupas usadas por personagens
de filmes e séries e mostra onde comprar peças parecidas. Não vende nem envia nada;
ganha comissão de afiliado (Amazon Associates, entre outros).

Coberturas atuais: Peaky Blinders (Thomas Shelby), Friends (Rachel Green — o
personagem que mais gera cliques), Harry Potter, Stranger Things, The Vampire Diaries.

Canais existentes: Instagram, TikTok, YouTube — todos @dresslikemeus.
**Sem Pinterest**, considerado o maior buraco da estratégia: busca por "roupa de
personagem" é enorme lá, é tráfego perene e aceita link direto (o IG não).

## O que o bot faz

Duas funções, ambas com **aprovação humana obrigatória** via botão no Telegram.
Nada vai ao ar sozinho — automação de resposta viola as regras da X e leva a suspensão.

**1. Oportunidades de resposta.** Duas fontes:
- `fetch_from_intent()` — busca gente perguntando "where to buy", "what jacket" etc.
  cruzado com os personagens do catálogo. É a fonte que converte.
- `fetch_from_accounts()` — contas de fandom (série/filme), não de moda.

Pipeline: coleta → filtro de idade (60 min) → dedupe → match com catálogo → Claude
analisa texto **e imagem** e dá nota 0-10 → acima de 7 vira alerta com 2 rascunhos.

**2. Posts originais para o perfil.** Uma vez por dia (`IDEAS_HOUR`), cruza o que está
em alta nas contas monitoradas com o catálogo e sugere 3 posts, cada um sobre um
produto diferente. Sobe a imagem do produto junto, se `image_url` estiver preenchido.

Racional: a resposta traz o visitante ao perfil; o perfil é quem converte em clique
para o site. Sem conteúdo próprio, a primeira metade é desperdiçada.

## Decisões que NÃO devem ser revertidas sem motivo

1. **Aprovação humana obrigatória.** Não transformar em auto-reply.
2. **Rascunhos de resposta nunca contêm link nem o nome do site.** Resposta com link
   custa $0,20 em vez de $0,015 na API da X, e o algoritmo corta o alcance. Site na bio.
3. **Visão é essencial.** Post de fandom é print de cena. Sem ler a imagem, inútil.
4. **Corte de relevância alto.** Notificação demais faz o dono ignorar e abandonar a
   ferramenta em duas semanas. `MIN_SCORE` e `MAX_ALERTS_PER_CYCLE` existem pra isso.
5. **Tom de fã que entende de figurino, nunca de loja.**

## Contexto importante: monetização da X não é o objetivo

Foi considerada e descartada. No começo de 2026 a X passou a **não contar impressões
de replies** para pagamento — só views orgânicas na home timeline valem. Além disso os
5M de impressões exigidos são de usuários Premium, e o CPM fica entre $0,02 e $0,15.

Consequência prática: o valor de responder é **levar tráfego ao perfil e ao site**,
nunca receita direta da X. Isso reforça a decisão 2 acima.

## Arquivos

- `bot.py` — versão atual (respostas + posts originais)
- `bot_v1.py` — primeira versão, só texto e só contas. Referência histórica.
- `catalogo.json` — produtos. **Incompleto**: só 10 itens, montados a olho pelo site.
- `README.md` — setup de credenciais, deploy e custos

## Stack e deploy

Python + `requests`, `requests-oauthlib`, `anthropic`. SQLite para estado.
Precisa de processo sempre ligado (o long polling do Telegram é o que faz os botões
responderem na hora — cron não serve).

Alvo: Render Background Worker ($7/mês) com Disk montado em `/data` e `DB_PATH=/data/bot.db`.
Alternativas: Oracle Cloud Always Free ($0), Hetzner (~€4/mês).
Modelo usado: `claude-sonnet-5`.

### Variáveis de ambiente (nunca no código, nunca no Git)

X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET, X_BEARER_TOKEN,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY

No portal da X, o app precisa estar como **Read and write** ANTES de gerar o Access
Token. Se gerar antes, publicar falha com 403 e é preciso regenerar.

## Pendências conhecidas

1. **`image_url` do catálogo é um palpite** (`dresslikeme.store/images/{slug}.jpg`).
   Se o caminho real for outro, o upload falha em silêncio e o post sai sem imagem.
2. **Catálogo incompleto.** Com 10 produtos o bot descarta oportunidades boas por
   "não tenho o produto".
3. **Tabela `seen` cresce sem limite.** Falta rotina de limpeza.
4. **Nunca foi executado de verdade.** O código passa em análise sintática, mas não
   houve teste de integração com as APIs.

## Backlog, em ordem de valor

1. **Sincronizar `catalogo.json` com o site automaticamente** (ler sitemap ou API do
   front). Hoje é manual e vai desatualizar em uma semana.
2. **Loop de aprendizado.** Coletar métricas das respostas 24h depois e injetar as 3
   melhores como exemplos no prompt.
3. **Calendário de estreias** (TMDB). Estreia de temporada é o gatilho de maior valor:
   por ~48h todo mundo pergunta onde comprar as roupas.
4. **Modo Halloween.** Setembro e outubro são o pico do produto de costume (Hermione).
5. **Monitorar Reddit.** r/HelpMeFind, r/femalefashionadvice e subs de fandom têm as
   mesmas perguntas, com API mais barata e menos hostil a link.

## Primeiro comando sugerido no Claude Code

    Leia CLAUDE.md e bot.py e me diga por onde começar o item 1 do backlog.
