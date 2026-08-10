# Bot de sugestão de comentários — X + Telegram

Monitora contas na X, gera dois rascunhos de resposta com o Claude e manda pro seu
Telegram. Nada vai pro ar sem você tocar num botão.

## Instalação

```bash
pip install requests requests-oauthlib anthropic
```

## Credenciais

### 1. X (developer.x.com)

Crie um app no portal de desenvolvedor. Em **User authentication settings**, marque
`Read and write` — sem isso o app só lê e a publicação falha com erro 403.

Pegue: API Key, API Secret, Bearer Token, Access Token e Access Token Secret.
Se você gerou o Access Token *antes* de mudar a permissão para read/write, regenere.

### 2. Telegram

1. Fale com o [@BotFather](https://t.me/BotFather) → `/newbot` → guarde o token.
2. Mande qualquer mensagem pro seu bot novo.
3. Abra `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` e copie o `chat.id`.

### 3. Variáveis de ambiente

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

## Rodando

```bash
python bot.py
```

A primeira execução só marca o ponto de partida de cada conta (não gera rascunhos),
para você não receber 30 posts antigos de uma vez. A partir da segunda, só chega
coisa nova.

Precisa ficar rodando de forma contínua — o polling do Telegram é o que faz os botões
responderem na hora. Railway, Fly.io, ou uma VPS com `systemd`/`tmux` resolvem. Um cron
a cada 30 min **não** funciona aqui: você só veria a resposta do botão na rodada seguinte.

## Como usar no dia a dia

Chega uma mensagem com o post e duas opções:

- **Publicar A / B** → posta o rascunho como resposta
- **🗑** → descarta
- **Responder a mensagem com seu próprio texto** → publica o que você escreveu

A terceira é a mais útil: use o rascunho como ponto de partida e ajuste.

## Custos aproximados

| Item | Preço | Uso típico/mês |
|---|---|---|
| Leitura de posts (X) | $0,005 por post lido | 6 contas × 48 checagens/dia — a maioria volta vazia |
| Publicar resposta (X) | $0,015 (sem link) / $0,20 (com link) | evite links nas respostas |
| Claude Sonnet | centavos por rascunho | — |

Estimativa: uso pessoal moderado fica na casa de poucos dólares por mês. Se as contas
monitoradas postarem muito, aumente `CHECK_INTERVAL` ou corte a lista.

## Ajustes que mais importam

- **`ACCOUNTS`** — troque pelas contas do seu nicho. Comentar em conta gigante como
  @elonmusk significa competir com milhares de replies; contas médias (10k–200k
  seguidores) no seu assunto dão muito mais retorno.
- **`PERSONA`** — o campo que mais muda a qualidade do rascunho. Quanto mais específico
  sobre quem você é e o que você sabe, menos genérico sai o texto.
- **`MAX_POSTS_PER_ACCOUNT`** — quantos posts novos considerar por checagem.

## Cuidado com as regras da X

Como quem aprova é você, isso não é automação de engajamento. Mas se você começar a
publicar dezenas de respostas por dia em contas grandes, com textos parecidos, o
resultado prático é o mesmo aos olhos do filtro de spam: alcance reduzido ou suspensão.
Poucas respostas boas por dia funcionam melhor de qualquer forma.
