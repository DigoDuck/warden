# CLAUDE.md — Warden

## Contexto

Warden é um control plane para workloads agentic: agentes rodam como deploys, com identidade
própria, política determinística, sandbox sem rede, evidência independente e auditoria
append-only. Tese: **o modelo propõe, o control plane decide.** Projeto de portfólio de um dev
solo, com escopo cortado para terminar em 12 semanas.

Fonte de verdade do escopo, neste repo:

- `docs/briefing.md` — produto, arquitetura, módulos, stack, modelo de dados, policy, decisões fechadas.
- `docs/plano-12-semanas.md` — semana a semana, com checklist "Pronto quando" verificável por comando.
- `docs/adr/` — toda decisão não óbvia vira ADR no mesmo dia.
- `docs/journal.md` — 5 linhas por semana, na sexta.

**Antes de propor escopo, ler a checklist "Pronto quando" da semana corrente.** O maior risco do
projeto não é técnico, é nunca chegar a "pronto" por tentar fazer tudo. Itens marcados `[EVO]` ou
`[ADV]` no briefing estão fora do núcleo: não implementar sem pedido explícito.

## Decisões fechadas (não reabrir sem motivo novo)

- Agent loop próprio. **Sem LangChain/LangGraph.**
- PostgreSQL como fila (`FOR UPDATE SKIP LOCKED`) e event store. Sem Redis no MVP.
- Policy engine própria em YAML: default deny, efeito mais restritivo vence.
- Sandbox Docker sem rede. Toda saída externa é tool do gateway MCP.
- Secret broker: o modelo nunca vê credencial. JWT RS256 curto por run, revogável por `jti`.
- `FakeProvider` (roteiro YAML) é a base dos testes e dos behavioral evals no CI.
- Dois providers: Anthropic nativo + OpenAI-compatible (cobre Ollama, vLLM, OpenRouter).
- Auditoria com hash chain; `UPDATE`/`DELETE` revogados no banco para o role da aplicação.

## Stack

| Camada | Escolha |
|---|---|
| API | FastAPI + Pydantic v2 (async, SSE) |
| DB | PostgreSQL 16 + SQLAlchemy 2 async + Alembic |
| Fila | PostgreSQL (tabela `tasks`, claim atômico com lease) |
| LLM | SDKs oficiais `anthropic` e `openai` |
| MCP | SDK Python oficial `mcp` |
| Sandbox | Docker Engine API via SDK `docker` |
| Auth | PyJWT, RS256 com chave própria |
| Observabilidade | OpenTelemetry + Jaeger (Compose), structlog JSON |
| Frontend | Vite + React 18 + TypeScript + TanStack Query + React Router + Tailwind |
| Testes | pytest + pytest-asyncio + httpx; Vitest + Testing Library; Playwright em poucos fluxos |
| CI | GitHub Actions: ruff, mypy, pytest, Semgrep, pip-audit, gitleaks |
| Deploy | Docker Compose local; VPS única com Compose + Caddy no fim |

**Desvio deliberado do default do perfil:** este projeto usa **FastAPI**, não Django, e
**SQLAlchemy async**, não o ORM do Django. Motivo no briefing (§11): async nativo no agent loop,
streaming/SSE e tipagem em fronteira. Não sugerir migração para Django.

Layout do monorepo: `backend/warden/<subpacote>`, `frontend/`, `policies/`, `evals/datasets/`,
`sandbox-images/`, `examples/target-repo/`. Fronteiras de cada subpacote em `docs/briefing.md` §10
— respeitar o "Não faz" de cada módulo (ex.: `policy` não executa nada, `api` não tem lógica de agente).

## Comandos de dev

Ambiente local: **Windows 11 + Git Bash / PowerShell**. Docker Desktop com WSL2 backend. Se o
socket do Docker der problema no Windows, rodar `api` e `worker` dentro do WSL2.

Os alvos abaixo passam a existir a partir da semana 1 (`Makefile` + `docker-compose.yml`):

```bash
docker compose up -d db      # Postgres
make migrate                 # alembic upgrade head
make demo-fake               # tarefa completa com FakeProvider, sem custo
make demo                    # tarefa real (precisa de ANTHROPIC_API_KEY)
make test                    # pytest
make lint                    # ruff + mypy
```

Nunca commitar `.env`. Chaves e tokens vivem só no ambiente; o broker os injeta, o modelo não os vê.

## Fluxo de trabalho

- Mudança não trivial: **plan mode antes de código**. Plano aprovado, então implementação.
- Cada PR com escopo de um item da checklist da semana. PRs em paralelo só quando não tocam os
  mesmos arquivos e há no máximo uma migração Alembic por onda (duas criariam dois heads).
- Em onda paralela: um worktree por trilha em `.worktrees/`, um `WARDEN_TEST_DB` por trilha, e só
  uma trilha roda os testes `sandbox` localmente, porque eles contam containers e volumes globais.
  Depois dos merges, conferir o CI da `main` combinada: cada PR só foi testado sozinho.
- **Teste de falha tem que causar a falha, não simulá-la.** Matar o processo, não levantar exceção;
  contar container, não confiar no `finally`. Os defeitos mais sérios deste repo foram achados
  olhando estado real, nunca por teste vermelho.
- **Verificação antes de dizer "pronto":** rodar o comando e colar a saída. "Pronto" é verificável
  por comando ou clique, nunca por sensação. Teste que não rodou não conta como passando.
- Se algo necessário estiver inacessível (repo, segredo, API, connector), dizer exatamente o que
  falta e parar. Não substituir, não mockar em silêncio, não adivinhar.
- Decisão não óbvia tomada no PR: escrever a ADR no mesmo PR.

## Modo de trabalho: velocidade

Claude implementa, Diogo revisa o diff. Consequência aceita conscientemente: o código precisa ser
**defensável em entrevista** por quem não o digitou. Portanto, em toda entrega:

- Comentar o **porquê** nos trechos de lógica densa (claim com lease, resume por replay de eventos,
  avaliação de policy, hash chain, flags de hardening do container). O "o quê" o código já diz.
- No PR, um parágrafo curto: o que foi feito, qual conceito está em jogo, qual o trade-off.
- Nenhuma dependência nova sem justificar por que o problema não cabe na stdlib ou no que já existe.

## Convenções de código

- Código, nomes e comentários em **inglês**. UI e docs do projeto em **português**.
- Explicar trechos complexos — este repo também é material de estudo.
- Tarefa repetitiva vira script (Python preferido; shell quando fizer sentido).
- Validação em toda fronteira de confiança (entrada de API, saída do modelo, conteúdo externo).
- Constraint no banco antes de regra em código, quando o banco puder garantir.

## Git

- Mensagens de commit em **inglês**, imperativas (`Add policy matcher for glob paths`).
- **Título e corpo de PR, e descrição de issue, em inglês** também. Só `docs/` e a UI ficam em português.
- **Nunca** adicionar trailer de coautoria, assinatura ou selo de IA em commits, PRs ou issues.
- Branches: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>`.
- Branch base: `main`. Um PR por item da checklist.
