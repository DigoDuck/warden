# Warden

> Run agents like you run deploys: identity, policy, sandbox, evidence, audit.

Control plane para workloads agentic. Agentes não rodam com as credenciais do desenvolvedor nem
com acesso livre à máquina: cada execução recebe identidade própria e curta, passa por uma policy
determinística, executa dentro de um sandbox sem rede, tem o resultado verificado por evidência
independente do relato do agente e deixa uma trilha de auditoria imutável. Tese: **o modelo
propõe, o control plane decide.**

## Estado

Em construção. Fonte de verdade do escopo:

- [`docs/briefing.md`](docs/briefing.md) — produto, arquitetura, stack e decisões fechadas.
- [`docs/plano-12-semanas.md`](docs/plano-12-semanas.md) — plano de execução, semana a semana.
- [`docs/metrics.md`](docs/metrics.md) — o que já foi medido de verdade (behavioral evals hoje;
  capability evals quando a chave da Anthropic chegar).
- [`docs/adr/`](docs/adr/) — decisões não óbvias, uma por arquivo.

## Quickstart (Docker, sem chave de API)

Só precisa de Docker (Compose v2). Sem `uv`, sem Node, sem chave: o worker usa o
`FakeProvider` (briefing §19), um roteiro fixo de tool calls no lugar do modelo.

```bash
docker compose up -d --build   # db, migrações, chave JWT, api, worker e UI
docker compose exec api python -m warden.api.user_token   --email voce@exemplo.com --scopes tasks:write tasks:read audit:read --role worker
```

1. Abra <http://localhost:5180>, vá em **Configurações** e cole o token impresso acima.
2. Em **Nova tarefa**, envie qualquer texto. O detalhe mostra os eventos chegando ao vivo e a
   tarefa termina **Concluída** (a verificação roda lint, tipos e testes no sandbox, leva ~1 min).
3. `docker compose down -v` apaga tudo, inclusive o banco e a chave.

Portas no host (todas configuráveis, ver `.env.example`): UI `WARDEN_WEB_PORT` (5180), API
`WARDEN_API_PORT` (8010), Postgres `WARDEN_DB_PORT` (5434). A chave JWT é gerada num volume
próprio na primeira subida e nunca sobrescrita; isso é conveniência de **dev**, em produção ela
vem de um segredo. O worker monta `/var/run/docker.sock` para criar os sandboxes (risco aceito
na [ADR-012](docs/adr/ADR-012-worker-docker-socket.md)).

## Quickstart (desenvolvimento local, com `uv`)

Nenhum passo abaixo pede chave de API: o primeiro contato com o projeto usa só o
`FakeProvider` (briefing §19), um roteiro fixo de tool calls que substitui o modelo. O control
plane sob teste é o mesmo dos dois casos; o que muda depois é só quem decide o próximo passo.

```bash
cp .env.example .env         # ANTHROPIC_API_KEY fica vazio; nada abaixo precisa dela
docker compose up -d db      # Postgres 16 em :5434 (ver .env.example se a porta já estiver em uso)
make migrate                 # aplica as migrações Alembic
make keys                    # gera .keys/jwt-private.pem (só na primeira vez; nunca commitado)
make sandbox-image           # builda a imagem sem rede em que as tools de um agente rodam
make demo-fake                # uma tarefa ponta a ponta: lista arquivos, tenta ler .env (negado
                               # pela policy), lê src/app.py, resume o que encontrou — sem custo
make evals-behavioral        # os 12 casos comportamentais do briefing §46, sem custo (§19)
```

`make demo-fake` imprime cada evento como o control plane os gravou (`task.created`,
`policy.decided`, `tool.executed`, ...): é a mesma trilha que a UI e o `/audit` leem depois, não
um resumo à parte. `make evals-behavioral` roda o mesmo tipo de tarefa 12 vezes, cada uma
provando um comportamento específico (nega segredo, respeita cancelamento, para no orçamento,
sobrevive a um crash real do worker, redige argumento com cara de segredo, ...); 3 dos 12 ficam
`pending` porque dependem de features fora do escopo desta trilha (manifesto de capacidades,
ciclo de vida do agente, policy com estado) — ver `evals/datasets/behavioral_v1.yaml`.

Para ver o control plane decidindo em tempo real pela UI (a mesma tarefa acima, mas com API,
frontend e um passo de aprovação humana no meio): [`examples/walkthrough/README.md`](examples/walkthrough/README.md).

Com `ANTHROPIC_API_KEY` no `.env`, `make demo` roda a mesma tarefa contra o modelo real.

## Stack

FastAPI · PostgreSQL 16 · SQLAlchemy 2 async · Docker · OpenTelemetry · React + Vite + TypeScript
