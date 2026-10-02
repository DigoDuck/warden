# ADR-028: fase de publicação do control plane, depois do verdict

**Status:** aceita · **Data:** 2026-10-02 · **PR:** `feat/publish-after-verdict`

## Contexto

Até este PR, `github.open_pr` era uma tool do agente: o modelo a chamava dentro do loop, antes
de `finish`. Três coisas saíam erradas disso.

1. **O PR saía antes da verificação.** A ADR-026 e a ADR-010 registraram a pendência: o
   briefing (§23) põe Verifier → Reviewer → Decision Queue → `open_pr`, e o código fazia o
   contrário. Um PR aberto com testes vermelhos era possível.
2. **O modelo escolhia o que publicar.** O agente passava `paths` e `body`. O que vai para o
   GitHub deveria ser o diff que o control plane mediu, não o que o agente diz que fez.
3. **A tese ficava pela metade.** "O modelo propõe, o control plane decide": aqui o modelo
   propunha *e* executava o pedido, com a aprovação humana como único freio.

## Decisão

### O agente não tem a tool: o control plane publica

`tools/sandboxed.py::build_registry` deixou de registrar `github.open_pr`. Uma função nova,
`build_publish_registry(sandbox)`, registra só ela (mesmos `path_inspector`, escopo e
`needs_identity` da ADR-025) e devolve `None` quando `github_repo`/`github_token` não estão
configurados, o "ausente, não recusando" de sempre. O worker e o demo passam esse registry ao
`run_task` como `publisher`.

É a mesma ideia do reviewer da ADR-010: **ausência de canal em vez de instrução**. Pedir ao
modelo "não abra PR antes de testar" é uma regra que o modelo pode violar; tirar a tool dele é
uma que ele não pode. Um modelo que alucine uma chamada a `github.open_pr` é recusado como
qualquer tool desconhecida.

### A fase: o que acontece depois de um verdict que aprova

No fim de `_verify_and_finish`, só quando a resolução é SUCCEEDED, há um `publisher` e o diff
tem arquivos, o loop roda `_publish`:

1. **Monta a chamada.** `ToolCall` com id determinístico `publish-<task_id>` (é a chave de
   `uq_approvals_task_tool_call` e do resume). `title` é a primeira linha da spec (até 100
   caracteres), `branch_slug` sai dela, `paths` são os arquivos `added|modified` da evidência
   `diff`, e `body` é um relatório: tabela da evidência, verdict e findings, custo e o resumo do
   coder, marcado como "gerado pelo agente, não verificado". `_pr_report` ainda prefixa a task
   e os arquivos.
2. **Grava `publish.requested`** `{id, tool, arguments}` e dá checkpoint, antes de decidir. O
   replay reusa esses argumentos exatos: a pergunta que o humano viu não muda debaixo dele.
3. **Decide como qualquer chamada**, com a policy real por path. `REQUIRE_APPROVAL` (a regra
   `open-pr-needs-human`) reusa `_pause_for_approval` e `_RunStopped("WAITING_APPROVAL")`.
   `ALLOW`, ou aprovada por um humano, emite um token por chamada (ADR-025) e executa por
   `tools/gateway.py::execute`, gravando `policy.decided`, `tool_calls` e `tool.executed`.
4. **Termina a tarefa pelo desfecho:**

| Desfecho | Status | Motivo |
|---|---|---|
| PR aberto | SUCCEEDED | |
| humano rejeitou | **CANCELLED** | a nota do revisor, no `reason` do `task.finished` |
| policy negou algum path | FAILED | "a policy recusou publicar" |
| erro do GitHub (`ToolError`) | FAILED | a mensagem, sem retry (backoff é da semana 8) |
| workspace mudou depois da verificação | FAILED | ver abaixo |
| diff vazio, ou sem GitHub configurado | SUCCEEDED | não há fase de publicação |
| arquivo removido ou binário no diff | SUCCEEDED | `publish.skipped {reason}`, sem PR |

O status continua `VERIFYING` durante a fase: nenhum status novo, nenhuma migração de CHECK.
Nem `max_seconds` nem `max_usd` valem aqui (o mesmo raciocínio da ADR-026: são o orçamento do
agente); cancelar vale, checado antes de decidir e antes de executar.

Por que não reusar `_run_tools`: ele aplica `_check_stoppable` (inclui `max_seconds`) e
responde uma rejeição ao modelo como `tool_result`. As duas coisas estão erradas aqui. A fase
reusa as peças de baixo nível (`_decide`, `_pause_for_approval`, o gateway e três helpers de
registro que `_run_tools` passou a compartilhar).

### Rejeitar aqui cancela

A ADR-022 decidiu que rejeitar uma chamada **do agente** não cancela a tarefa: injeta um erro
e o modelo tenta outro caminho. A razão era haver um próximo turno para ler a recusa. Na fase
de publicação o agente já terminou e o verdict já foi dado; não há turno do modelo para reagir.
Uma rejeição que não faz nada deixaria a tarefa em SUCCEEDED com um trabalho que um humano
disse que não queria publicar. Então a rejeição vira CANCELLED, com a nota no `reason` do
`task.finished` (o audit `approval.rejected` com a nota já é escrito por `decide_approval`).
A decisão da ADR-022 fica de pé para o que ela cobria.

### Publicar exatamente o que foi verificado

O volume do workspace sobrevive à pausa por aprovação, e o `pytest` da verificação executa
código do agente *depois* do diff (a ADR-026 fixa essa ordem justamente para o diff não ser
influenciável). Um `conftest.py` ou um teste pode reescrever um arquivo que o diff já registrou,
e então o PR levaria conteúdo que nem os checks nem o reviewer viram.

- `FileChange` ganhou `sha256` do conteúdo final (`None` para `removed`), calculado em
  `compute_diff`.
- Antes de executar, `_publish` pede de novo o check `diff` ao verifier (a mesma exportação do
  workspace e o mesmo `compute_diff` contra o baseline do host) e compara a lista
  `(path, change, sha256)` com a evidência gravada. Qualquer diferença: FAILED, "o workspace
  mudou depois da verificação", uma linha de audit `publish.workspace_changed`, nenhum token
  emitido e nenhuma chamada ao GitHub. Nada roda dentro do container entre essa checagem e a
  leitura dos arquivos por `open_pr`.
- Um cancel que mata o container no meio dessa exportação aparece como erro do collector; o
  loop confere o marcador de cancel antes de chamar aquilo de adulteração.

Dois testes causam a mudança de verdade: um teste do agente que reescreve `src/feature.py`
enquanto o `pytest` roda, e uma escrita no volume enquanto a tarefa espera aprovação.

### Resume

`core/replay.py`: `VerificationState` ganha `publish_call` (de `publish.requested`) e
`publish_result` (o `tool.executed` daquele id). As `approval_decisions` já vêm de
`approval.granted/rejected` sem depender da iteração. `publish.requested` é um evento próprio e
**não** entra na lista de chamadas pendentes do agente: o replay só zera essa lista em
`iteration.started`, e uma chamada de publicação ali viraria uma chamada de modelo para uma tool
que o agente nem tem. O desfecho sai de um mapeamento único (`_publication_outcome`) sobre o
`tool.executed` gravado, o mesmo no run ao vivo e na retomada: um worker que morreu entre gravar
o resultado e gravar `task.finished` termina a tarefa do mesmo jeito.

A janela que importa é a do crash entre "o humano aprovou" e `tool.executed`: o GitHub pode ter
ouvido o pedido sem o banco saber. A retomada não pergunta de novo (a aprovação está no log) e
não abre um segundo PR (a branch é função da tarefa, ADR-025, e a segunda chamada converge no
mesmo PR). `test_a_worker_killed_while_github_is_called_neither_asks_twice_nor_opens_two_prs`
mata um processo de worker de verdade com a requisição parada dentro do GitHub.

### Uma correção que o plano não previa

`_decide` agora recusa (DENY sintético) uma chamada a uma tool que o registry do agente não
tem. Antes, `touched_paths` devolvia `[None]` para um nome desconhecido sem erro, então a regra
`open-pr-needs-human` casava por nome e **pausava a tarefa para um humano aprovar uma chamada
que não pode rodar**. O texto da pausa dizia "abrir um PR é visível fora do control plane"
sobre uma tool que o agente nunca teve. Os testes de aprovação do `test_loop.py` que usavam um
`github.open_pr` não registrado como rótulo passaram a registrar um substituto.

## Alternativas consideradas

**Manter a tool no agente e só exigir `run_tests` antes (regra stateful na policy).** Rejeitada:
a policy é uma função determinística de um contexto (ADR-003), sem memória das chamadas
anteriores. Uma regra de ordem pediria estado por tarefa na engine, e ainda assim deixaria o
modelo escolher os arquivos. A garantia estrutural (a tool não existe para ele) é mais forte e
mais barata. O eval 12 deixou de ser `pending`.

**Um status novo `PUBLISHING`.** Rejeitada: obrigaria migração do CHECK de `tasks.status`, do
tratamento de lease em `queue.claim` e da UI, para distinguir uma fase de poucos segundos que o
`VERIFYING` já cobre. Os eventos `publish.*` dizem em que ponto a tarefa está.

**Comparar o diff por conteúdo em vez de digest.** Rejeitada: o patch da evidência é
truncado em 100 mil caracteres, os digests são exatos e cabem na própria linha de evidência.

**Retry do GitHub dentro da fase.** Fora de escopo: backoff e política de retry são da
semana 8. O PR é idempotente, então a retomada de um crash já é segura.

## Consequências

- `run_task`/`run_claimed_task`/`Worker.run_once`/`demo.py` ganham `publisher`. Um publisher sem
  verifier é recusado (`ValueError`): só um diff verificado pode ser publicado.
- Quem publica é o control plane, e o modelo só vê `github.open_pr` como uma tool desconhecida.
  O agente não escolhe mais `title`, `branch_slug`, `paths` nem `body`.
- A Decision Queue mostra a aprovação `publish-<task_id>` com `tool`, `args_safe`,
  `matched_rules` e `reason`; nada nela assumia que o pedido veio do modelo. O Timeline mostra o
  `output` do `tool.executed` ("opened PR #N: url"). O eval 3 passou a provar "rejeitar a
  publicação encerra CANCELLED sem tocar o GitHub", e o 12 "o agente não abre PR e um teste
  vermelho nunca chega a propor um".
- **Fora de escopo:** deleção e arquivo binário (`open_pr` só cria blobs UTF-8; o caminho de
  upgrade é uma entrada de tree com `sha: null` e um blob base64), link do PR no Task Detail,
  retry com backoff, e a UI dos eventos `publish.*` (o Timeline ainda os mostra como JSON cru).
- **Limitação conhecida:** a checagem de digest cobre o que o diff registrou. Um arquivo que o
  diff ignora (`is_ignored`, ADR-018) não entra nele, mas também não entra nos `paths` do PR.
- Se o diff for `status: "error"` (export falhou ou passou do teto de 50 MB), não há como
  saber o que publicar: a tarefa termina SUCCEEDED com `publish.skipped`, "a evidência do diff
  está indisponível", em vez de arriscar um PR às cegas.
