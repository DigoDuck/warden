# Warden como Claude Code Project

Setup do projeto na feature **Projects** (beta) do Claude Code: uma conversa coordenadora que
abre threads (cloud sessions) em paralelo, cada uma num branch e num PR próprios.

Docs oficiais: <https://code.claude.com/docs/en/claude-projects>

> **Status em 2026-09-17: o rollout não chegou nesta conta** (web, desktop e CLI). O setup abaixo
> fica pronto para quando chegar. Até lá, ver [Plano B](#plano-b-enquanto-o-rollout-não-chega).

## Pré-requisitos

- [ ] Plano Pro ou Max e **Projects** visível na sidebar de <https://claude.ai/code> (rollout gradual).
- [ ] Repo em github.com com push access: `DigoDuck/warden`.
- [ ] **Claude GitHub App instalado em `DigoDuck/warden`.** O token de `/web-setup` não basta para
      threads de project. Instalar em <https://github.com/apps/claude/installations/new>.
- [ ] Connectors que as threads precisarem ligados em <https://claude.ai/customize/connectors>
      (a conversa coordenadora não usa connector; só as threads).

## Criar

<https://claude.ai/code> → **Projects** → **New project**

- **Name:** `Warden`
- **Goal:** `Entregar o Warden até a v1.0 do plano de 12 semanas: cada semana fechada só quando toda a checklist "Pronto quando" passa por comando.`
- **Context:** repositório `DigoDuck/warden`.

Depois de criado, colar o texto abaixo em **Project settings → Memory → Project instructions**.

## Project instructions

```text
Este projeto constrói o Warden, um control plane para workloads agentic, no repositório
DigoDuck/warden. O escopo inteiro está no repo: docs/briefing.md (produto, arquitetura, módulos,
stack, modelo de dados, decisões fechadas) e docs/plano-12-semanas.md (semana a semana, com uma
checklist "Pronto quando" verificável por comando). CLAUDE.md na raiz tem as convenções.

Antes de começar qualquer tarefa, leia a semana corrente no plano. Trabalhe só em itens da
checklist da semana corrente. Itens marcados [EVO] ou [ADV] no briefing estão fora do núcleo:
não implemente sem eu pedir explicitamente. Se uma tarefa que eu mandar não estiver na checklist
da semana, diga a qual semana ela pertence antes de começar.

Decisões já fechadas, não reabrir sem motivo novo: agent loop próprio (sem LangChain/LangGraph);
PostgreSQL como fila (FOR UPDATE SKIP LOCKED) e event store, sem Redis no MVP; policy engine
própria em YAML com default deny e efeito mais restritivo vencendo; sandbox Docker sem rede;
secret broker onde o modelo nunca vê credencial; JWT RS256 curto por run revogável por jti;
FakeProvider com roteiro YAML como base de testes; auditoria com hash chain e UPDATE/DELETE
revogados no banco para o role da aplicação.

Onde o trabalho acontece:
- Branch de main, um branch por thread, nome feat/<slug>, fix/<slug>, docs/<slug> ou chore/<slug>.
- Um draft pull request por thread, título imperativo em inglês, escopo de um item da checklist.
- No corpo do PR: o que foi feito, qual conceito está em jogo, qual trade-off foi aceito, e a
  saída dos comandos de verificação.
- Nunca adicionar trailer de coautoria, assinatura ou selo de IA em commit, PR ou issue.

Antes de chamar um trabalho de pronto, rode `make lint` e `make test` (ou, enquanto o Makefile
não existir, `ruff check .`, `mypy .` e `pytest`) e cole as linhas de resumo na mensagem final.
Trabalho sem saída de comando colada não está pronto. Se o item da checklist descreve um teste
específico (container sem rede, resume sem executar tool duas vezes, UPDATE em audit_log
falhando por permissão), o PR tem que conter esse teste, não uma aproximação dele.

Se algo que você precisa estiver inacessível (o repositório, um segredo, uma API, um connector,
Docker), diga exatamente o que falta na sua primeira mensagem e pare. Não substitua, não mocke em
silêncio, não adivinhe. Uma tool de terceiro indisponível é motivo para parar, nunca para trocar
de fonte.

Precisa do meu go-ahead antes: merge ou force-push; mudar configuração de CI; adicionar
dependência nova (justifique por que não cabe na stdlib nem no que já está instalado); mudar o
schema de uma tabela que já tem migração aplicada; qualquer coisa marcada [EVO] ou [ADV].

Eu sou dev full stack júnior em transição para Applied AI, e este projeto é portfólio. Escreva em
português comigo. Comente o porquê nos trechos de lógica densa (claim com lease, resume por replay
de eventos, avaliação de policy, hash chain, flags de hardening do container): preciso conseguir
defender em entrevista um código que não digitei. Aponte furos na minha lógica em vez de
concordar por cortesia.
```

## Como usar depois

- Mandar trabalho na conversa do projeto, não abrir thread na mão: "toca os itens 1 e 2 da
  checklist da semana 2".
- Correção feita numa thread → pedir para o Claude **lembrar** a correção: vai para a memória do
  projeto e as próximas threads já começam com ela.
- Regra sobre o repo (comando de build, convenção) vai no `CLAUDE.md`. Regra sobre como as threads
  trabalham vai nas project instructions.
- Skills e subagents só chegam às threads se estiverem commitados em `.claude/skills/` ou
  `.claude/agents/` do repo — o `.gitignore` daqui já abre exceção para eles.

## Ambiente das threads

Thread = VM nova, sem nada da máquina local. O que já vem instalado e importa aqui: **Docker
(docker, dockerd, docker compose)** e **PostgreSQL 16** — ambos presentes, nenhum rodando por
padrão. Ou seja, o sandbox Docker da semana 2 e a fila em Postgres da semana 1 rodam em thread.

Em **Project settings → Environment**, setup script do ambiente:

```bash
service postgresql start
```

Se as imagens do `sandbox-images/` ficarem pesadas, acrescentar `docker compose pull` ou
`docker compose build` ao setup script: o cache do ambiente guarda as imagens no disco, então cada
sessão nova já começa com elas.

Network access fica em **Trusted** (padrão): PyPI, npm e Docker Hub liberados, o resto bloqueado.
O sandbox do Warden é `--network none` de propósito, então isso não conflita.

`ANTHROPIC_API_KEY` **não** vai para o ambiente das threads: elas testam com `FakeProvider`, que é
determinístico e de graça. A demo com modelo real (`make demo`) roda na máquina local.

## Plano B: enquanto o rollout não chega

### O que abre a fila

O primeiro lote vai para contas que **já usaram cloud sessions** e que **não têm projects antigos**
no chat do claude.ai nem no Cowork.

1. Entrar na waitlist: <https://claude.com/form/projects>
2. Usar cloud session ao menos uma vez (é o pré-requisito citado na doc, e research preview já está
   liberado no Pro): abrir <https://claude.ai/code>, conectar o GitHub no fluxo da primeira visita,
   e rodar uma tarefa pequena. Do terminal, depois de conectado: `claude --cloud "..."`.
3. Conferir <https://claude.ai/projects>. Se houver projects antigos ali, eles são o que segura a
   conta fora do primeiro lote. Não deletar nada sem antes salvar o conteúdo que importa.

### Como paralelizar sem Project

| Preciso de | Ferramenta |
|---|---|
| Trabalho que continua com a máquina desligada, um branch e um PR por tarefa | **cloud session**: `claude --cloud "implementa o item 2 da checklist da semana 1"`. É a mesma coisa que uma thread, só que a coordenação é sua |
| Várias tarefas independentes na máquina local, com Docker real e Postgres local | **agent view**: `claude agents` dispatcha sessões em background, cada uma no seu worktree, e mostra qual precisa de você |
| Uma tarefa lateral que ia poluir a conversa (varredura, pesquisa, log grande) | subagent, dentro da própria sessão |

O `CLAUDE.md` da raiz já serve os dois caminhos: cloud session clona o repo e lê o `CLAUDE.md` do
mesmo jeito que uma thread de project leria. O que **não** existe fora do Project é a memória
compartilhada entre sessões: correção feita numa sessão não chega na próxima sozinha. Enquanto
isso, correção que vale para sempre vai para o `CLAUDE.md` por PR, não para o chat.

Para o Warden especificamente: os testes de sandbox (container non-root, sem rede, fs read-only) e
os de resume passam melhor na máquina local no começo, porque você vai querer olhar `docker ps` e
`docker inspect` com o próprio olho antes de confiar no relato de qualquer agente.
