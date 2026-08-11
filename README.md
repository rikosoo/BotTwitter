# DressLikeMe — bot de oportunidades na X

Acha gente perguntando onde comprar a roupa de um personagem, cruza com o catálogo do
site, e manda pro seu Telegram um alerta com dois rascunhos de resposta prontos.
Também sugere uma leva de posts originais para o perfil, uma vez por dia.

**Nada vai pro ar sem você tocar num botão.** Resposta automática viola as regras da X
e o resultado é alcance cortado ou suspensão.

## O que ele faz

**1. Oportunidades de resposta** — a cada 10 minutos, em duas camadas e dois idiomas
(`LANGUAGES = ["en", "pt"]`):

| Camada | O que busca |
|---|---|
| `intent` | frase de compra + personagem/série — quem pergunta e diz de quê |
| `fandom` | personagem/série + palavra de roupa — quem comenta o figurino sem usar frase de compra ("Rachel's blazer in this scene") |
| `accounts` | timeline das contas em `ACCOUNTS` |

Buscar "por fandom" sem a palavra de roupa não funciona: conversa de fandom é
majoritariamente enredo, ator e spoiler. O que separa o figurino do resto é o
vocabulário de roupa (`GARMENT_TERMS`), não o nome da série.

Janela de 7 dias, que é o alcance máximo da busca recente da X. Post velho rende menos
alcance, mas não custa nada a mais tentar.

Pipeline: coleta → filtro de idade → dedupe → casa com o catálogo → **pré-filtro
local** → o Claude lê **o texto** dos sobreviventes e dá nota 0–10 → acima de
`MIN_SCORE` vira alerta.

O pré-filtro (`prescore`) é a peça que torna a busca ampla viável. Ele pontua de graça,
sem chamar a API: intenção de compra, personagem citado, palavra de roupa, imagem
presente, poucas respostas (pergunta sem resposta é onde você entra) e tração do post
(mais gente vê a sua resposta). Só os `MAX_ANALYSIS_PER_CYCLE` melhores viram chamada
ao Claude. Uma regra derruba o post na hora: intenção de compra sem roupa e sem produto
é sobre outra coisa ("where to buy tickets for the tour").

O objetivo da resposta é **engajar o fandom**, não acertar o produto — like da
comunidade já leva gente ao perfil. Por isso um post sem produto correspondente pode
tirar nota alta, e o corte é deliberadamente frouxo.

**O bot não lê a imagem do post, só o texto.** Foi uma decisão consciente: ler imagem
encarecia cada análise a ponto de limitar o volume, e volume é o que importa quando a
meta é engajamento. O prompt instrui o modelo a nunca afirmar detalhe visual que não
esteja escrito no texto.

**2. Posts originais para o perfil** — uma vez por dia, no horário `IDEAS_HOUR`.
Cruza o que está em alta nas contas monitoradas com o catálogo e sugere 3 posts, cada
um sobre um produto diferente, com a imagem do produto anexada quando há `image_url`.

A resposta traz o visitante ao perfil; o perfil é quem converte em clique para o site.

## Instalação

```bash
pip install -r requirements.txt
```

## Credenciais

### 1. X (developer.x.com)

Crie um app no portal de desenvolvedor. Em **User authentication settings**, marque
`Read and write` — sem isso o app só lê e a publicação falha com erro 403.

Pegue: API Key, API Secret, Bearer Token, Access Token e Access Token Secret.
Se você gerou o Access Token *antes* de mudar a permissão para read/write, regenere:
o token guarda a permissão do momento em que foi criado.

### 2. Telegram

1. Fale com o [@BotFather](https://t.me/BotFather) → `/newbot` → guarde o token.
2. Mande qualquer mensagem pro seu bot novo.
3. Abra `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` e copie o `chat.id`.

### 3. Variáveis de ambiente

Nunca no código, nunca no Git — o `.gitignore` já bloqueia `.env`.

```bash
export X_API_KEY="..."
export X_API_SECRET="..."
export X_ACCESS_TOKEN="..."
export X_ACCESS_SECRET="..."
export X_BEARER_TOKEN="..."
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export ANTHROPIC_API_KEY="..."
```

Se faltar alguma, o bot recusa a subir e diz qual.

## Rodando

Antes do primeiro ciclo de verdade, rode a verificação de fumaça. Ela confere
credenciais, permissão de escrita na X, Telegram e Claude **sem publicar nada**:

```bash
python bot.py --check
```

Depois:

```bash
python bot.py
```

A primeira execução só marca o ponto de partida de cada conta monitorada (não gera
rascunhos a partir da timeline), para você não receber 30 posts antigos de uma vez.
A busca por intenção funciona desde o primeiro ciclo.

Precisa ficar rodando de forma contínua — o long polling do Telegram é o que faz os
botões responderem na hora. **Cron não serve**: você só veria a resposta do botão na
rodada seguinte.

## Deploy

Alvo: **Render Background Worker** ($7/mês).

- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Adicione um **Disk** montado em `/data` e defina `DB_PATH=/data/bot.db`

O `DB_PATH` no disco persistente não é opcional. Sem ele o SQLite vive no sistema de
arquivos efêmero e some a cada deploy — você perde o `last_id` de cada conta (recebe
um monte de post repetido) e o offset do Telegram (os botões param de responder).

Alternativas: Oracle Cloud Always Free ($0) ou Hetzner (~€4/mês), ambos com `systemd`.

## Como usar no dia a dia

Manual completo em [`MANUAL.md`](MANUAL.md) — como ler os alertas, o que ajustar em
cada situação, rotina sugerida e o que fazer quando algo dá errado. O resumo:

Chega uma mensagem com a nota, o produto que casou, o post original e os rascunhos:

- **✅ A / ✅ B** → publica o rascunho como resposta
- **🗑** → descarta
- **Responder a mensagem com seu próprio texto** → publica o que você escreveu

A terceira é a mais útil: use o rascunho como ponto de partida e ajuste.
Nos posts do perfil, os botões são **✅ Publicar** e **🗑**.

## Custos aproximados

| Item | Preço | Observação |
|---|---|---|
| Leitura de posts (X) | ~$0,005 por post lido | 6 contas × 144 checagens/dia; a maioria volta vazia |
| Publicar resposta (X) | $0,015 sem link / $0,20 com link | por isso o rascunho nunca tem link |
| Claude Sonnet | centavos por análise | só texto, sem imagem — bem mais barato |
| Hospedagem | $0–$7/mês | Render, Oracle Free ou Hetzner |

Se as contas monitoradas postarem muito, aumente `CHECK_INTERVAL` ou corte a lista.

## Ajustes que mais importam

Todos no topo do `bot.py`:

Recall — quantas oportunidades o bot enxerga:

- **`LANGUAGES`** (`["en", "pt"]`) — idiomas monitorados. Cada um dobra o número de
  buscas e o consumo de cota. Os rascunhos sempre saem no idioma do post original.
- **`INTENT_PHRASES`** — as frases de intenção de compra, por idioma.
- **`GARMENT_TERMS`** — o vocabulário de roupa da camada `fandom`, por idioma. Ampliar
  aqui é o jeito mais direto de achar mais gente falando de figurino.
- **`ACCOUNTS`** — contas de fandom, não de moda. Conta gigante significa competir com
  centenas de replies; contas médias (10k–200k) do seu nicho rendem muito mais.
- **`MAX_POST_AGE_MIN`** (7 dias) — o máximo que a busca recente da X alcança.
- O próprio **`catalogo.json`**: cada personagem novo amplia as duas camadas de uma vez.
  É o ajuste de maior efeito sobre o recall.

Precisão — quanto lixo chega no seu Telegram:

- **`MIN_SCORE`** (6) — o corte do Claude. Notificação demais faz você ignorar o bot e
  abandonar em duas semanas. Se estiver chegando lixo, suba para 7 ou 8.
- **`MAX_ALERTS_PER_CYCLE`** (8) — teto por ciclo, mesma lógica.
- **`PRE_MIN_SCORE`** (4) — corte do pré-filtro local.

Custo — leia antes de alargar a busca:

- **`MAX_READS_PER_DAY`** (300) — teto de posts lidos por dia. A X cobra por post lido
  e o plano tem cota mensal; a camada `fandom` é ampla e pode devolver o máximo em
  todo ciclo. Ao bater o teto o bot para de buscar até a virada do dia, em vez de
  gerar conta ou bloqueio. 300/dia ≈ 9.000/mês.
- **`SEARCH_RESULTS_PER_QUERY`** (20) — resultados por query. Multiplica direto a cota.
- **`MAX_ANALYSIS_PER_CYCLE`** (25) — teto de chamadas ao Claude por ciclo.
- **`CHECK_INTERVAL`** (10 min) — resposta tardia nasce enterrada, mas cada ciclo lê
  posts. Se a cota apertar, suba isto antes de cortar as camadas.
- **`IDEAS_HOUR`** / **`IDEAS_PER_BATCH`** — a leva diária de posts do perfil.

## Catálogo

`catalogo.json` é uma lista de produtos. Campos obrigatórios: `slug`, `name`,
`character`, `show`. Opcionais: `keywords` (sinônimos que o bot procura no post) e
`image_url` (usada nos posts do perfil).

Quanto mais completo, menos oportunidade boa o bot descarta por "não tenho o produto".
Hoje tem 10 itens, montados à mão — sincronizar com o site é o item 1 do backlog.

Termos de uma palavra que também são palavras comuns em inglês (`friends`, `eleven`)
estão em `AMBIGUOUS_TERMS`: só casam se o post também falar de roupa. Sem isso,
metade da timeline viraria falso positivo e cada um custaria uma análise de imagem.

## Cuidado com as regras da X

Como quem aprova é você, isso não é automação de engajamento. Mas se você publicar
dezenas de respostas por dia, com textos parecidos, o filtro de spam não vê diferença:
alcance reduzido ou suspensão. Poucas respostas boas por dia funcionam melhor.

## Sobre monetização da X

O objetivo aqui **não** é o programa de receita da X, e vale entender por quê antes de
otimizar para a métrica errada. Desde o início de 2026 a X não conta impressões de
reply para pagamento — só views orgânicas na home timeline. O limite de 5M de
impressões é de usuários Premium, e o CPM fica entre $0,02 e $0,15.

Na prática: o valor de responder é **levar tráfego ao perfil e ao site**, onde entra a
comissão de afiliado. É isso que sustenta a regra de nunca pôr link no rascunho de
resposta — o link fica na bio.
