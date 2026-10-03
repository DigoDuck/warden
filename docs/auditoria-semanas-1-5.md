# Auditoria das checklists das semanas 1 a 5

**Data:** 03/10/2026 · **Commit auditado:** `7873006` (`main` depois do #29)

O plano exige que "pronto" seja verificável por comando ou clique. Até esta auditoria, só um
item das semanas 1 a 5 estava marcado, embora quase tudo já tivesse sido entregue. Aqui cada item
foi ligado a uma prova e a prova foi **rodada**, não só localizada. Um item só foi marcado no plano
quando a prova cobre o que ele diz; prova parcial ou bloqueada fica desmarcada, com o motivo.

## Como foi verificado

- **Testes citados como prova:** os 27 nós de teste abaixo, numa invocação só, com o Docker sem
  outra suíte rodando: `66 passed in 80.19s` (os parametrizados de `test_default_policy_table`
  contam um por caso).
- **`make demo-fake`** contra o banco de dev: imprimiu os 25 eventos lidos do banco, o deny
  `never-read-secrets` no evento 10, a evidência dos quatro checks e o verdict; terminou
  `SUCCEEDED`.
- **CI:** `gh pr checks 2` (1 check, verde) e o CI da `main` em `7873006` (verde).
- **Manual, pela UI** (API, worker e frontend locais; GitHub falso de `backend/tests/fake_github.py`
  numa porta de loopback): tarefa submetida pela tela, eventos chegando ao vivo, aprovação e
  rejeição da publicação pela Decision Queue, e navegação só por teclado.

## Semana 1

| Item | Prova | Situação |
|---|---|---|
| `make demo-fake` roda e imprime os eventos do DB | `make demo-fake`, rodado em 03/10 | ✅ |
| `make demo` com `ANTHROPIC_API_KEY` grava `model_calls` com tokens e custo | só testes unitários do provider e do pricing | ⏳ bloqueado pela chave (~10/10) |
| CI verde no primeiro PR | o #1 era só docs e veio antes do workflow; o #2 é o primeiro com CI: verde | ✅ com ressalva |

## Semana 2

| Item | Prova | Situação |
|---|---|---|
| container non-root, sem rede, fs read-only exceto `/workspace` | `test_sandbox.py`: `test_runs_as_a_non_root_user`, `test_has_no_network`, `test_dns_does_not_resolve_either`, `test_root_filesystem_is_read_only`, `test_workspace_is_writable`, `test_capabilities_are_dropped` | ✅ ressalvas: a rede é testada com socket Python (a imagem não tem `curl`, um teste com `curl` passaria à toa); `/tmp` também é gravável, separado do workspace |
| `read_file(".env")` dá deny com `matched_rules=[never-read-secrets]` e o modelo continua | `test_policy.py::test_default_policy_table[env-at-the-root-is-denied]` (igualdade exata), `test_loop.py::test_the_refusal_reaches_the_model_and_names_the_rule`, `::test_policy_denies_a_readable_secret_and_the_task_carries_on`, eval 1 | ✅ |
| `rm -rf /` negado, `pytest` permitido | `test_default_policy_table[rm-rf-is-denied]`, `[pytest-is-allowed]`, eval 10 | ✅ |
| worker morto, outro retoma, nenhuma tool roda duas vezes | `test_resume.py::test_a_crashed_run_resumes_and_no_tool_runs_twice`, `test_durability.py::test_a_task_survives_the_worker_process_being_killed` (kill de verdade), eval 7 | ✅ |
| `POST /tasks/{id}/cancel` durante uma tool mata o container e dá CANCELLED | `test_api.py::test_cancelling_a_running_task_only_marks_it` (HTTP grava o marcador) + `test_durability.py::test_a_cancel_request_kills_a_long_running_tool_and_the_task_ends_cancelled` (o marcador mata o container) | ✅ por composição: nenhum teste único faz HTTP → kill, e a tool longa é `sleep 30`, não `run_tests` |
| `max_usd` estourado termina BUDGET_EXCEEDED | `test_loop.py::test_spending_over_the_ceiling_stops_the_run`, eval 6 (`max_usd: 0.001`) | ✅ |

## Semana 3

| Item | Prova | Situação |
|---|---|---|
| token expirado ou sem scope falha 401/403 e gera audit | `test_gateway.py::test_an_expired_token_is_never_executed_and_is_audited`, `::test_a_token_missing_the_required_scope_is_never_executed`, eval 8 | ✅ |
| round trip de aprovação via API (já marcado) | `test_api_approvals.py::test_approving_resumes_the_task_to_queued`, `test_approvals_worker.py::test_a_real_worker_pauses_releases_and_a_second_one_resumes_after_approval` | ✅ **o sentido mudou**: com a policy padrão, a única aprovação que existe é a publicação (ADR-028), e rejeitá-la cancela a tarefa. "Rejeitar injeta erro e o loop continua" (ADR-022) só é alcançável com uma policy que mande uma tool do agente para aprovação |
| `UPDATE audit_log` como `warden_app` falha | `test_audit.py::test_update_fails_under_the_app_role` | ✅ |
| adulterar uma linha faz `/audit/verify` apontar a quebrada | `test_api.py::test_audit_verify_points_at_a_tampered_row` | ✅ aponta o id da linha, não uma posição |
| `grep` em logs e em `task_events` não acha o token do GitHub | `test_github_tool.py::test_the_verified_diff_is_published_after_approval_and_no_secret_reaches_the_log` varre eventos, `tool_calls` e `audit_log` | ⏳ parcial: **os logs de processo (stdout do api e do worker) nunca são varridos** |

## Semana 4

| Item | Prova | Situação |
|---|---|---|
| submeter na UI, ver eventos ao vivo, ver deny inline com a regra | manual 03/10: a tela saiu de "Na fila / Nenhum evento ainda" para "Concluída" com a timeline inteira sem recarregar; "Negado `never-read-secrets`" inline | ✅ |
| aprovar `open_pr` na Decision Queue retoma a tarefa e o card some | manual 03/10: aprovar pela tela levou a tarefa a SUCCEEDED, o GitHub falso recebeu exatamente 1 PR (`warden/b9d259d2-x`), o card sumiu e o contador da sidebar caiu de 2 para 1 | ✅ **o sentido mudou**: quem propõe o `open_pr` agora é o control plane, depois do verdict (ADR-028) |
| `tsc --noEmit`, ESLint e Vitest verdes no CI | job `frontend` do CI, verde em `7873006` | ✅ |
| navegação por teclado nas duas telas principais | manual 03/10: Tarefas (linhas alcançáveis por Tab, Enter abre o detalhe, foco vai para o `h1`), Detalhe (Tab chega na aba ativa, setas e End trocam de aba), e Decision Queue (nota, Rejeitar habilitado só com nota, Enter rejeita) | ✅ ressalvas na seção de achados |

## Semana 5

| Item | Prova | Situação |
|---|---|---|
| uma issue vira PR aberto por agente, com relatório, e o CI do repo alvo passa | relatório do PR coberto por `test_publish.py` e pelo end-to-end com GitHub falso; issue 09 resolvida de verdade no runner do `coding_v1` com FakeProvider | ⏳ bloqueado: chave da API, PAT fine-grained e um repo alvo real (o `examples/target-repo` ainda mora dentro do monorepo) |
| verdict separado do resumo; UI mostra os dois com selos | `test_api.py::test_task_out_carries_the_coders_summary_and_the_independent_verdict`, `test_verify_loop.py::test_the_reviewer_never_receives_the_coders_summary`, `DecisionPanel.test.tsx` | ✅ **o sentido mudou**: o verdict leva "Gerado · revisor independente" de propósito; "Verificado" é só da evidência determinística (ADR-010) |
| mesma `Idempotency-Key` duas vezes não abre dois PRs | `test_api.py::test_the_same_idempotency_key_returns_the_same_task_once_created` (uma tarefa) + `test_github_tool.py::test_open_pr_is_idempotent_for_the_same_task_and_branch` (um PR por branch) + `test_publish_resume.py::test_a_worker_killed_while_github_is_called_neither_asks_twice_nor_opens_two_prs` | ✅ por composição |
| PR rejeitado na Decision Queue fecha CANCELLED com nota no audit | `test_publish.py::test_rejecting_the_pull_request_cancels_the_task_with_the_note`, eval 3, e manual 03/10 pela tela, só com teclado: CANCELLED, a nota no `task.finished` e em `approval.rejected` no audit, nenhum PR novo | ✅ |

## Achados (viram trabalho, não estavam em nenhuma checklist)

1. **Pela UI, nenhuma tarefa consegue editar código.** `make user-token` cria o usuário com papel
   `user` (`api/user_token.py`), e a regra `write-source` de `policies/default.yaml` só libera
   escrita para `user.role: worker`. Testes e evals passam porque criam usuários `worker` direto no
   banco. Na auditoria, uma tarefa submetida pela tela teve o `write_file` negado por default deny
   e terminou SUCCEEDED sem mudar nada. Para testar a publicação foi preciso promover o usuário de
   teste no banco de dev (e depois desfazer). **É o achado mais sério:** o fluxo "issue vira PR" da
   semana 5 nunca funcionaria a partir da tela. Pede uma decisão (papel no `user-token`, ou o que
   `user.role` deve significar na policy) e uma ADR.
2. **A Decision Queue mostra o corpo do PR como JSON cru** (`args_safe`), com `\n` literais. Dá para
   decidir, mas é difícil ler. O briefing (§24) pede diff, risco e custo na fila.
3. **O painel das abas do Detalhe não tem `tabindex="0"`.** Um painel sem nada focável (Custo,
   Decisão) não recebe foco pelo teclado; o WAI-ARIA recomenda o painel focável nesse caso.
4. **A confirmação de rejeição usa `window.confirm()` nativo.** Funciona com teclado, mas não
   segue o design do resto da tela, e ferramentas de automação o fecham como "Cancelar".
5. **Logs de processo não são varridos atrás de segredo** (item pendente da semana 3).
6. **A timeline mostra os eventos `verify.*` e `publish.*` como JSON cru** (já registrado na
   ADR-010 e na ADR-028).
7. Cosméticos: o `raw_content` do FakeProvider carrega `delay_seconds`; a fixture de
   `Approvals.test.tsx` usa o nome de regra `require-approval-open-pr`, e a regra real é
   `open-pr-needs-human`.

## O que falta para fechar as semanas 1 a 5

- Com a chave (~10/10): `make demo` real.
- Com chave, PAT e repo alvo real: uma issue vira PR e o CI do repo alvo passa.
- Sem dependência externa: varrer os logs de processo (semana 3) e resolver o achado 1.
