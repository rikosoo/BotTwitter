# Manual de uso — bot DressLikeMe

Para o dia a dia. Setup técnico e deploy estão no `README.md`.

---

## Em uma frase

A cada 10 minutos o bot procura, em inglês e português, gente falando de figurino
das séries que você cobre. O que parecer bom chega no seu Telegram com dois rascunhos
prontos. Você toca num botão e vai pro ar. **Nada é publicado sem você.**

---

## 1. Antes da primeira vez

```bash
python bot.py --check
```

Isso confere credenciais, permissão de escrita na X, Telegram e Claude **sem publicar
nada**. Ele mostra, por camada de busca, quantos posts reais aparecem e a nota do
pré-filtro de cada um. Use isso para calibrar antes de deixar rodando.

Se der erro **403** na parte da X: seu app não está como *Read and write*, ou o Access
Token foi gerado antes de você mudar a permissão. Arrume no portal e **regenere o
token** — ele guarda a permissão do momento em que nasceu.

Depois:

```bash
python bot.py
```

Deixe rodando. A primeira execução só marca o ponto de partida das contas monitoradas
(não gera rascunho a partir delas), para você não receber 30 posts velhos de uma vez.
As buscas funcionam desde o primeiro ciclo.

---

## 2. O que chega no Telegram

**Alerta de resposta** — a oportunidade de comentar no post de outra pessoa:

```
⭐ 8/10 · fã comentando o figurino da Rachel
🔎 comentário de figurino · tem imagem
🎬 produto: Oversized Wool Blazer

🐦 @usuaria
o blazer da Rachel nesse episódio é atemporal

A) Aquele blazer oversized é puro início dos anos 90 — ombro
   estruturado e caimento solto ao mesmo tempo...

B) Curioso que o mesmo corte voltou inteiro agora...

ver post · ou responda com seu próprio texto
```

Como ler:

| Linha | O que é |
|---|---|
| ⭐ nota | quanto o Claude achou que vale a pena, de 0 a 10 |
| 🔎 origem | **pergunta de compra** (alguém perguntando onde comprar), **comentário de figurino** (conversa sobre roupa), **conta monitorada** (timeline das contas em `ACCOUNTS`) |
| 🎬 produto | peça do catálogo relacionada, ou `—` se não houver. **Não ter produto não é problema** |
| A / B | os dois rascunhos. A é mais direto, B pega outro ângulo |

Três ações:

- **✅ A** ou **✅ B** → publica aquele rascunho como resposta
- **🗑** → descarta
- **Responder a mensagem com seu texto** → publica o que você escreveu

**A terceira é a mais valiosa.** Use o rascunho como ponto de partida e ajuste com o
que só você sabe. É o que mantém a conta com voz de gente.

**Sugestão de post para o perfil** — uma vez por dia, às 9h (`IDEAS_HOUR`), chegam 3
ideias de post original, cada uma sobre um produto diferente, com a imagem do produto
anexada. Botões: **✅ Publicar** e **🗑**.

---

## 3. As duas metades, e por que as duas importam

**Responder traz o visitante. O perfil converte.**

Quem gosta da sua resposta clica no seu nome. Se o perfil estiver vazio, a pessoa vai
embora e o trabalho foi perdido. Por isso as sugestões diárias de post original não são
enfeite — sem elas, metade do esforço evapora.

O link fica **na bio**, nunca na resposta. Dois motivos: resposta com link custa $0,20
em vez de $0,015 na API da X, e o algoritmo corta o alcance de post com link.

---

## 4. Regras de convivência com a X

O bot só sugere; quem publica é você. Isso não é automação de engajamento — mas o
filtro de spam não julga intenção, julga padrão.

- **Poucas respostas boas por dia.** Dezenas de respostas com texto parecido dão no
  mesmo que um bot aos olhos do filtro: alcance cortado ou suspensão.
- **Nunca a mesma abertura duas vezes.** O bot já evita isso sozinho (manda as últimas
  respostas publicadas para o Claude não repetir ângulo), mas se você escrever à mão,
  vale a atenção.
- **Não force o produto.** Resposta que empurra peça vira anúncio e ninguém curte
  anúncio. Comentário bom sobre figurino ganha like — e like é o que leva ao perfil.

---

## 5. Ajustando na prática

Tudo no topo do `bot.py`. Reinicie o bot depois de mexer.

**"Está chegando lixo demais no Telegram"**
→ suba `MIN_SCORE` de 6 para 7 ou 8. É o botão mais direto.
→ se persistir, baixe `MAX_ALERTS_PER_CYCLE`.

**"Está chegando pouca coisa"**
→ adicione personagens ao `catalogo.json`: cada personagem novo amplia as duas camadas
de busca de uma vez. É o ajuste de maior efeito.
→ amplie `GARMENT_TERMS` com palavras que seu público usa.
→ baixe `MIN_SCORE` para 5.

**"A conta da X está alta"**
→ baixe `MAX_READS_PER_DAY` (padrão 300/dia ≈ 9.000/mês).
→ suba `CHECK_INTERVAL` de 600 para 1800 (30 min).
→ tire `"pt"` de `LANGUAGES` — cada idioma dobra o número de buscas.

**"Quero só inglês"** (ou só português)
→ `LANGUAGES = ["en"]`. Os rascunhos sempre saem no idioma do post original.

**"Os rascunhos estão genéricos"**
→ o Claude só lê o **texto** do post, não a imagem. Post cuja graça está toda na foto
vai render rascunho fraco por natureza — nesses, use a opção de escrever você mesmo.

---

## 6. Rotina sugerida

- **Manhã:** chegam as 3 ideias de post do perfil. Escolha uma, publique.
- **Ao longo do dia:** os alertas vão pingando. Responda os que valem, descarte o resto
  sem culpa — o 🗑 é parte do funcionamento, não um erro do bot.
- **Uma vez por semana:** olhe o que deu like de verdade e ajuste `MIN_SCORE` e o
  catálogo com base nisso.

Meta realista: **3 a 6 respostas boas por dia, mais 1 post no perfil.** Mais que isso
começa a parecer spam para o algoritmo, e o retorno cai em vez de subir.

---

## 7. Quando algo dá errado

| Sintoma | Causa provável |
|---|---|
| Botões não respondem | O processo caiu. O long polling do Telegram precisa estar sempre no ar — cron não serve |
| "❌ Falhou 403" ao publicar | App da X não está como *Read and write*, ou o Access Token é anterior à mudança. Regenere |
| Recebe posts repetidos após deploy | O SQLite foi perdido. `DB_PATH` precisa apontar para disco persistente (`/data/bot.db` no Render) |
| Parou de achar posts no meio do dia | Bateu `MAX_READS_PER_DAY`. Volta sozinho na virada do dia (UTC) |
| Post do perfil sai sem imagem | `image_url` do catálogo aponta para um caminho que não existe. Abra a URL no navegador para conferir |
| Nada chega há horas | Normal em nicho pequeno. Confirme com `--check`, que mostra quantos posts cada camada encontra agora |

---

## 8. O que este bot não faz

- **Não responde sozinho.** Nunca. É decisão de projeto, não limitação.
- **Não lê imagem.** Só o texto do post.
- **Não mede resultado.** Não sabe quais respostas deram like ou clique — isso ainda
  é manual, e é o próximo item mais valioso do backlog.
- **Não te dá receita da X.** A monetização de replies acabou no início de 2026: só
  view orgânica na home timeline conta. O retorno aqui é tráfego pro site e comissão
  de afiliado.
