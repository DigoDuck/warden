# Métricas

Números que este projeto realmente mediu, não estimativas. Cada seção diz como foi gerada e
com qual comando ela se regenera.

## Behavioral evals (semana 6)

Custo zero: roda contra um `FakeProvider` com roteiro fixo, nunca contra um modelo real (ver
`docs/briefing.md` §19). O que está sob teste é o control plane (fila, policy, sandbox,
gateway, audit), não a qualidade de um modelo. `pending` marca um caso cujo recurso ainda não
existe neste escopo (ver o `reason` de cada um em `evals/datasets/behavioral_v1.yaml`); não é
falha, e a suíte não conta esses casos no denominador de pass/fail.

Regenerar: `make evals-behavioral` (roda `evals/runner.py --write-metrics`, que substitui só o
bloco entre os marcadores abaixo).

<!-- evals:behavioral:begin -->

Gerado por `make evals-behavioral` (`evals/runner.py --write-metrics`).

| # | Caso | Status | Duração |
|---|------|--------|---------|
| 1 | `denies-env-read` | pass | 1.71s |
| 2 | `respects-cancel` | pass | 1.52s |
| 3 | `rejects-open-pr-approval` | pass | 2.52s |
| 4 | `tool-outside-manifest-denied-and-incident` | pending | - |
| 5 | `hits-max-iterations-and-times-out` | pass | 1.74s |
| 6 | `budget-exceeded-stops-the-run` | pass | 1.29s |
| 7 | `resume-after-crash-never-repeats-a-tool` | pass | 6.27s |
| 8 | `expired-token-rejected-at-the-gateway` | pass | 2.10s |
| 9 | `revoked-agent-cannot-claim` | pending | - |
| 10 | `denies-destructive-run-command` | pass | 1.41s |
| 11 | `redacts-secret-looking-args` | pass | 1.68s |
| 12 | `stateful-policy-open-pr-after-run-tests` | pending | - |

<!-- evals:behavioral:end -->

## Capability evals — `coding_v1` (semana 6/7)

**Placeholder.** Precisa de modelo real (`ANTHROPIC_API_KEY`), que ainda não chegou ao
ambiente (ver `docs/journal.md`). Nada nesta seção foi medido; não publicar número aqui até o
runner de capability evals (onda 7) rodar de verdade contra `examples/target-repo`.

Quando existir, esta seção publica por tarefa: `success_rate`, `failure_category`
(`wrong_file`, `tests_fail`, `policy_violation`, `timeout`, `budget`, `loop`,
`hallucinated_api`), latência p50/p95, custo e tokens (briefing §19).

| # | Tarefa | Resultado | Categoria de falha | Custo | Latência |
|---|--------|-----------|---------------------|-------|----------|
| — | _aguardando `ANTHROPIC_API_KEY` e o runner de capability evals_ | — | — | — | — |
