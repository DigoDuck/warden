# Warden

> Run agents like you run deploys: identity, policy, sandbox, evidence, audit.

Control plane para workloads agentic. Agentes não rodam com as credenciais do desenvolvedor nem
com acesso livre à máquina: cada execução recebe identidade própria e curta, passa por uma policy
determinística, executa dentro de um sandbox sem rede, tem o resultado verificado por evidência
independente do relato do agente e deixa uma trilha de auditoria imutável.

## Estado

Em construção, semana 1 de 12. Ainda não há código executável — o que existe é o escopo:

- [`docs/briefing.md`](docs/briefing.md) — produto, arquitetura, stack e decisões fechadas.
- [`docs/plano-12-semanas.md`](docs/plano-12-semanas.md) — plano de execução com checklists verificáveis.

## Como rodar

Assim que a semana 1 fechar:

```bash
cp .env.example .env         # ANTHROPIC_API_KEY é opcional para a demo fake
docker compose up -d db
make migrate
make demo-fake               # roda uma tarefa ponta a ponta sem custo de API
```

## Stack

FastAPI · PostgreSQL 16 · SQLAlchemy 2 async · Docker · OpenTelemetry · React + Vite + TypeScript
