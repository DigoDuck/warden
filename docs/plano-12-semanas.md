# Warden: plano de execução em 12 semanas

> Companheiro de `briefing.md`. Premissa: **15 a 20 h por semana**. Com menos horas, mantenha a ordem e estique o calendário; não pule a semana 6 (fechamento do MVP).
>
> Regras do plano:
> 1. "Pronto" é verificável por comando ou clique, nunca por sensação.
> 2. Sexta-feira de cada semana: rodar a checklist, escrever 5 linhas em `docs/journal.md` (o que funcionou, o que não, número da semana). Isso vira material de entrevista.
> 3. Se uma semana estourar, corte escopo da semana, não a checklist. O que sair vira issue.
> 4. Toda decisão não óbvia vira ADR no mesmo dia.

---

## Semana 1: Fundação e o primeiro loop

**Objetivo:** um agente com loop próprio lê um arquivo e termina, com custo registrado, sem Docker ainda.

**Estudar antes (4 h):** tool use na API da Anthropic (mensagens, `tool_use`/`tool_result`, `stop_reason`, `usage`); asyncio básico; Alembic.

**Entregas:**
- Monorepo, `pyproject.toml` (ruff, mypy, pytest-asyncio), `docker-compose.yml` com Postgres.
- Migração inicial: `users`, `tasks`, `task_events`, `tool_calls`, `model_calls`.
- `providers/base.py`, `providers/fake.py` (roteiro YAML), `providers/anthropic.py`, `providers/pricing.py`.
- `tools/registry.py` com `read_file`, `list_files`, `finish` executando direto no host (temporário).
- `core/loop.py` mínimo: gera → executa tools → grava eventos → finaliza.
- `examples/target-repo/` com um FastAPI pequeno (3 rotas, 5 testes).
- CI: ruff + mypy + pytest.

**Pronto quando:**
- [ ] `make demo-fake` roda uma tarefa com FakeProvider e imprime os eventos do DB
- [ ] `make demo` com `ANTHROPIC_API_KEY` executa "liste os arquivos e resuma o projeto" e grava `model_calls` com tokens e custo em USD
- [ ] CI verde no primeiro PR

**Risco da semana:** gastar tempo em estrutura de pastas. Copie o layout do briefing e siga.

---

## Semana 2: Policy, sandbox e durabilidade

**Objetivo:** o modelo propõe, a policy decide, o sandbox executa, e a tarefa sobrevive a crash.

**Estudar antes (5 h):** Docker Engine API e flags de hardening (`--user`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--network none`, `--pids-limit`, `--memory`, `--cpus`); `FOR UPDATE SKIP LOCKED`; glob e regex em Python.

**Entregas:**
- `policy/engine.py`, `policy/matchers.py`, `policies/default.yaml`, `tests/policy_cases.yaml` com 30+ casos.
- `sandbox/docker.py`: create/exec/destroy com perfil endurecido; `sandbox-images/Dockerfile`.
- Tools passam a rodar no sandbox: `read_file`, `list_files`, `write_file`, `apply_patch`, `run_command` (allowlist), `run_tests`.
- `core/queue.py` (enqueue idempotente, claim com lease, heartbeat), `core/worker.py` (processo separado).
- Loop com `max_iterations`, `max_seconds`, `max_usd`, cancelamento cooperativo, checkpoint por iteração, resume por reconstrução de eventos.
- ADR-001 (runtime próprio), ADR-002 (Postgres como fila), ADR-003 (semântica da policy), ADR-004 (sandbox sem rede).

**Pronto quando:**
- [ ] teste prova que o container roda non-root, sem rede (`curl` falha) e com fs read-only exceto `/workspace`
- [ ] `read_file(".env")` retorna deny com `matched_rules=[never-read-secrets]` e o modelo recebe o erro e continua
- [ ] `run_command("rm -rf /")` negado; `run_command("pytest")` permitido
- [ ] teste de resume: worker morto após o evento N; novo worker retoma; nenhuma tool executa duas vezes (assert sobre `tool_calls`)
- [ ] `POST /tasks/{id}/cancel` durante `run_tests` mata o container e finaliza CANCELLED
- [ ] tarefa com `max_usd: 0.01` termina BUDGET_EXCEEDED

**Risco:** Docker Desktop no Windows. Se o socket der problema, rode `api` e `worker` dentro do WSL2 já nesta semana.

---

## Semana 3: Identidade, aprovação e auditoria

**Objetivo:** cada execução tem identidade própria e curta; ações de risco pausam; tudo fica num registro que não pode ser editado.

**Estudar antes (4 h):** JWT RS256 (claims, `exp`, `jti`), revogação, GRANT/REVOKE no Postgres, hash chain (por que `sha256(prev_hash + payload)` é tamper-evident e o que ele não protege).

**Entregas:**
- `identity/jwt.py`: par de chaves, emissão de token de usuário e de token de agente por run (TTL 15 min, scopes da decisão de policy), `issued_tokens` com revogação por `jti`.
- `identity/broker.py`: `get_credential(task, scope)` para o token do GitHub (lido do env do control plane, nunca passado ao modelo).
- `approvals`: `require_approval` → checkpoint → WAITING_APPROVAL → `POST /approvals/{id}/approve|reject` → resume ou erro injetado.
- `audit/log.py`: append com hash chain; `GET /audit/verify`; migração cria role `warden_app` sem UPDATE/DELETE em `audit_log`.
- Eventos de audit: task submitted/finished, policy deny, approval requested/granted/rejected, token issued/revoked.
- ADR-005 (identidade), ADR-006 (broker), ADR-007 (audit), ADR-008 (observabilidade × audit).

**Pronto quando:**
- [ ] tool executada por token expirado ou sem scope falha com 401/403 e gera evento de audit
- [ ] round trip de aprovação via API: tarefa pausa, aprovar retoma do checkpoint, rejeitar injeta erro e o loop continua
- [ ] `UPDATE audit_log SET ...` como `warden_app` falha por permissão (teste de integração)
- [ ] teste adultera uma linha via superuser e `/audit/verify` aponta o índice quebrado
- [ ] `grep` em logs e em `task_events` não encontra o token do GitHub

---

## Semana 4: Frontend do control plane

**Objetivo:** operar o sistema sem `curl`.

**Estudar antes (4 h):** SSE no browser (`EventSource`), TanStack Query (invalidação), React Router, shadcn/ui ou Tailwind básico.

**Entregas:**
- Vite + React + TS + TanStack Query + Router; cliente gerado do OpenAPI (`openapi-typescript`).
- Telas: Home (tabela com SSE), Submit Task, Task Detail (Spec, Execution timeline com decisões inline, Cost), Decision Queue.
- `GET /tasks/{id}/stream` (SSE) e listagem com filtros no backend.
- Estados loading/empty/error em todas as listas; foco visível; contraste ok.
- Vitest: Decision Queue (approve, reject exige nota) e timeline (renderiza deny com regra).
- ESLint + tsc no CI.

**Pronto quando:**
- [ ] submeter tarefa na UI, ver eventos chegando ao vivo, ver deny inline com a regra
- [ ] aprovar `open_pr` na Decision Queue retoma a tarefa e o card some
- [ ] `tsc --noEmit`, ESLint e Vitest verdes no CI
- [ ] navegação por teclado funciona nas duas telas principais

**Risco:** polir UI. Proibido animar qualquer coisa nesta semana.

---

## Semana 5: Workflow de código completo

**Objetivo:** spec entra, PR sai, com evidência que não depende do coder.

**Estudar antes (4 h):** GitHub REST (branches, PRs, comentários), fine-grained PATs e escopos, structured output (Pydantic) nos providers.

**Entregas:**
- Planner (structured output: passos, arquivos prováveis, testes a criar) como primeira fase do loop.
- `verify/runner.py`: pytest, ruff, mypy, diff stats → `evidence`.
- `verify/reviewer.py`: reviewer com prompt independente → `verdict` (passed, findings).
- Tool server-side `github.open_pr` via broker; PR com relatório Markdown (plano, diff stat, evidência, custo, verdict, link do trace futuro).
- `examples/target-repo` com CI próprio (ruff + pytest) e 10 issues escritas como specs.
- Task Detail: abas Diff, Tests/Evidence, Decision (verdict).
- ADR-010 (verificação independente e definição de sucesso).

**Pronto quando:**
- [ ] uma issue do repo de exemplo vira PR aberto por agente, com relatório, e o CI do repo alvo passa
- [ ] verdict é gravado separado do resumo do coder; UI mostra os dois com selos "generated" e "verified"
- [ ] rodar a mesma tarefa duas vezes com a mesma `Idempotency-Key` não abre dois PRs
- [ ] PR rejeitado na Decision Queue fecha a tarefa como CANCELLED com nota no audit

---

## Semana 6: Evals e fechamento do MVP

**Objetivo:** comportamento do control plane testado em CI; qualidade do agente medida com número.

**Estudar antes (3 h):** desenho de datasets, categorias de falha, por que LLM-as-judge sozinho é fraco.

**Entregas:**
- `evals/runner.py`; `evals/datasets/behavioral_v1.yaml` com os 12 casos do briefing (seção 46); job obrigatório no CI.
- `evals/datasets/coding_v1/` com 10 itens (spec, fixture, testes ocultos, ações proibidas).
- `docs/metrics.md` gerado pelo runner; `make evals-behavioral`, `make evals-coding` com cap de custo.
- README inicial com quickstart sem chave de API.
- Fechamento: checklist do MVP (briefing, seção 42) inteira verde.

**Pronto quando:**
- [ ] 12/12 behavioral evals passam no CI, custo zero
- [ ] `coding_v1` rodado com Claude: success rate, custo por tarefa e categorias de falha em `docs/metrics.md`
- [ ] checklist do MVP completa; tag `v0.1.0`
- [ ] journal das 6 semanas escrito

**Se atrasar:** corte itens do dataset para 6, nunca corte os behavioral evals.

---

## Semana 7: Observabilidade

**Objetivo:** cada tarefa é um trace; métricas respondem perguntas de negócio.

**Estudar antes (4 h):** OpenTelemetry (tracer, spans, atributos, propagação, OTLP), structlog, Jaeger.

**Entregas:**
- OTel SDK no api e no worker; spans conforme seção 20 do briefing; `trace_id` em `tasks`.
- Jaeger no Compose; link no Task Detail.
- Redator central de argumentos (`args_safe`) usado por spans, logs e DB.
- `GET /metrics/summary` e tela Metrics (success, custo/tarefa, tokens/tarefa, latência, iterações, tool failures, intervenções, deny rate).
- Logs JSON com `trace_id`.

**Pronto quando:**
- [ ] abrir uma tarefa no Jaeger e ver `task.run → agent.iteration → model.generate/policy.evaluate/tool.execute → sandbox.exec → verify.run`
- [ ] teste garante que atributos de span nunca contêm padrão de segredo nem conteúdo de arquivo
- [ ] tela Metrics reflete os números de `docs/metrics.md` para o mesmo período

---

## Semana 8: Provider independence e Model Router

**Objetivo:** dois providers, resiliência, três estratégias de routing e um experimento publicado.

**Estudar antes (4 h):** API OpenAI-compatible, Ollama, circuit breaker, backoff com jitter, desenho de experimento (mesma semente de dataset, uma variável por vez).

**Entregas:**
- `providers/openai_compat.py` (OpenAI, Ollama, OpenRouter), `capabilities` por provider.
- `ResilientProvider`: timeout, retry em transientes, circuit breaker (40 linhas), fallback, métricas por provider.
- `TaskProfile` heurístico; estratégias `FrontierAlways`, `CapabilityBased`, `CheapFirstEscalate`.
- `experiment_id` na submissão; `/metrics/summary?strategy=` e gráfico comparativo na tela Evals.
- Experimento A/B/C sobre `coding_v1`; tabela no README.
- ADR-011 (routing e experimento).

**Pronto quando:**
- [ ] derrubar o provider primário (URL inválida) aciona fallback; teste mostra breaker abrir após K falhas e fechar após half-open
- [ ] tabela A/B/C publicada com success, custo, custo por sucesso, latência p50, escalation rate
- [ ] experimento self-review vs reviewer independente publicado (escaped defects)

**Nota honesta:** com 10 tarefas a variância é alta. Publique com essa ressalva e rode 2 ou 3 vezes se o orçamento permitir.

---

## Semana 9: MCP Gateway, servers próprios e supply chain

**Objetivo:** tools sensíveis só existem atrás do gateway; dependências de agente têm hash e aprovação.

**Estudar antes (5 h):** MCP (JSON-RPC, stdio/HTTP, tools/resources, OAuth no MCP), rate limiting (token bucket), tool poisoning.

**Entregas:**
- `mcp/servers/repo` (find_symbol stub, list, read), `mcp/servers/github`, `mcp/servers/security` (semgrep) com o SDK `mcp`.
- `mcp/gateway.py`: authn (JWT do run), authz (policy por request), validação de schema, rate limit por agente, audit, allowlist de servers por manifest.
- `mcp_servers`, `skills` no registry com origem, versão, hash, permissões, trust level, aprovação; `warden.lock`.
- Tela MCP Servers / Skills; bloqueio de vínculo sem aprovação.
- `github.open_pr` migra do tool local para o server via gateway.

**Pronto quando:**
- [ ] chamada ao gateway com JWT expirado, sem scope ou fora da allowlist é rejeitada e auditada (3 testes)
- [ ] rate limit dispara com N+1 chamadas em 1 s e o evento aparece no trace
- [ ] server com hash diferente do lock não carrega; UI mostra "hash mismatch"
- [ ] descrição de tool contendo "ignore previous instructions" é sinalizada no registro

---

## Semana 10: Context engineering e guard

**Objetivo:** o agente lê menos e acerta mais; conteúdo externo é classificado antes de virar contexto.

**Estudar antes (5 h):** `ast` (NodeVisitor, imports, defs, calls), grafo de dependências, Unicode invisível (zero-width, bidi), OWASP LLM Top 10.

**Entregas:**
- `context/indexer.py` (AST do repo alvo → símbolos, imports, callers), tools `find_symbol`, `get_dependencies`, `find_callers`, `get_architecture_context` no server `warden-repo`.
- `guard/pipeline.py`: normalização, heurísticas, classificador opcional, trust level; ingestão com delimitadores para `untrusted`.
- Datasets `context_v1` e `injection_v1`; experimento com/sem contexto; medição detecção × bloqueio.
- Tela Task Detail: aba Context (arquivos e trust level de cada fonte); aba Security (findings do guard e do Semgrep).
- ADR-013 (AST antes de RAG), ADR-014 (guard detecta, policy impõe).

**Pronto quando:**
- [ ] tabela publicada: tokens, arquivos lidos, tool calls, tempo e sucesso com e sem contexto
- [ ] `injection_v1`: policy bloqueia 10/10 ações proibidas; taxa de detecção do guard publicada como está
- [ ] texto com zero-width e bidi override chega normalizado no contexto (teste)

---

## Semana 11: Registry lifecycle, incidentes e segurança no CI

**Objetivo:** governança visível e resposta a incidente automática.

**Estudar antes (3 h):** Semgrep rules, CodeQL no GitHub, pip-audit, gitleaks, template de postmortem.

**Entregas:**
- Lifecycle `draft → pending_review → approved → active → deprecated → revoked` com endpoints, roles e audit; tela Agent Registry completa.
- `incidents/detectors.py` (os 6 detectores da seção 25), `incidents/contain.py` (cancel, revoke jti, preservar sandbox e workspace), tela Incidents com timeline e postmortem.
- Regra: incidente gera stub de behavioral eval em `evals/datasets/from_incidents.yaml`.
- CI: Semgrep, pip-audit, npm audit, gitleaks, CodeQL semanal; `permissions:` mínimas; environments para deploy.
- Se sobrar tempo: `ClaudeAgentSDKRuntime` como segundo adapter e comparação de 5 tarefas (ADR-012).

**Pronto quando:**
- [ ] agente `revoked` não faz claim e seus tokens em aberto são rejeitados
- [ ] injeção simulada dispara incidente: tarefa CANCELLED, jti revogado, sandbox preservado, trace linkado; postmortem salvo
- [ ] CI de segurança verde com zero findings high
- [ ] checklist v0.3 (briefing, seção 42) completa; tag `v0.3.0`

---

## Semana 12: Portfolio-ready

**Objetivo:** alguém que nunca viu o projeto entende e roda em 10 minutos.

**Entregas:**
- README final em inglês: pitch, diagrama, garantias, quickstart sem chave, métricas reais, screenshots, threat model resumido, ADRs, roadmap, "what I'd do differently".
- `docs/threat-model.md`, `docs/metrics.md` atualizado, 14 ADRs revisados, `docs/journal.md` consolidado.
- 10 screenshots da seção 49 e GIF de 60 s.
- Deploy em VPS com Compose + Caddy, modo demo com budget global baixo; ou vídeo de 5 min se preferir não expor.
- Issues de próximos passos com labels (`hardening`, `experiment`, `nice-to-have`).
- Rascunho da linha do currículo e do pitch de 60 s ensaiado em voz alta.
- Tag `v1.0.0`.

**Pronto quando:**
- [ ] uma pessoa externa (ou você com o repo clonado numa pasta limpa) roda `docker compose up && make demo` seguindo só o README
- [ ] todas as tabelas de métricas do README têm data, dataset e versão do agente
- [ ] pitch de 60 s gravado e revisado uma vez
- [ ] release `v1.0.0` publicada com changelog

---

## Visão consolidada

| Semana | Tema | Marco |
|---|---|---|
| 1 | fundação, provider, primeiro loop | `make demo` funciona |
| 2 | policy, sandbox, durabilidade | resume após crash |
| 3 | identidade, aprovação, audit | audit imutável e verificável |
| 4 | frontend | operar pela UI |
| 5 | workflow de código | PR com evidência |
| 6 | evals, MVP | `v0.1.0` + metrics.md |
| 7 | observabilidade | trace completo |
| 8 | providers e router | tabela A/B/C |
| 9 | MCP gateway e supply chain | tools sensíveis só via gateway |
| 10 | contexto e guard | dois experimentos publicados |
| 11 | registry, incidentes, security CI | `v0.3.0` |
| 12 | portfólio | `v1.0.0` |

## O que fazer se você atrasar

1. **Nunca pule a semana 6.** Um MVP fechado e etiquetado vale mais que dez módulos pela metade.
2. Se atrasar em v0.2 ou v0.3, escolha por valor de entrevista: router com números (8) > gateway MCP (9) > guard e contexto (10) > incidentes (11).
3. A semana 12 é fixa. Se chegar nela com v0.2, apresente v0.2 e liste o resto como roadmap. Um projeto apresentável e honesto ganha de um projeto grande e inacabado.
