# ADR-022: aprovação humana como terceiro ramo da decisão de policy

**Status:** aceita · **Data:** 2026-09-22 · **PR:** `feat/approvals`

## Contexto

A checklist da semana 3 pede o round trip completo: "tarefa pausa, aprovar retoma do
checkpoint, rejeitar injeta erro e o loop continua". Até este PR, `REQUIRE_APPROVAL`
(`policy/engine.py::Effect`) já existe como valor do enum e a policy já sabe decidir por
ele, mas `core/loop.py` tratava o efeito como uma recusa qualquer: `_refusal_message`
devolvia "cannot run yet" ao modelo e a tarefa seguia rodando, exatamente como um `DENY`.
Não existia pausa, não existia tabela para a pergunta pendente, não existia endpoint para
responder.

Este PR fecha isso: `REQUIRE_APPROVAL` deixa de ser uma variação de recusa e vira um
terceiro desfecho de verdade, com estado próprio (`WAITING_APPROVAL`), uma pergunta durável
(`approvals`) e um jeito de retomar a partir exatamente do checkpoint que pausou.

## Decisão

### Pausar, não recusar

`core/loop.py::_run_tools` decide cada chamada como antes (`_decide`, que já devolveria
`REQUIRE_APPROVAL` desde a semana 2), mas agora, ao ver esse efeito, não executa a tool nem
a responde com uma recusa: `_pause_for_approval` grava a pergunta
(`core/approvals.py::request_approval`, uma linha em `approvals` com os argumentos redigidos
do jeito que `tool_calls.args_safe` já é), acrescenta o evento `approval.requested`, grava
uma entrada de audit (`approval.requested`, ator `system`/`loop`) e marca a tarefa
`WAITING_APPROVAL` com o lease liberado (`claimed_by`/`claimed_until` a `NULL`), tudo no
mesmo checkpoint fenced que qualquer outro passo do loop usa (ADR-019: `queue.verify_holder`
antes do commit). Em seguida `_RunStopped("WAITING_APPROVAL", ...)` desenrola até
`run_task`, que devolve o `RunResult` **sem** passar por `_finish`: `WAITING_APPROVAL` não é
terminal (`worker.py::TERMINAL_STATUSES` já não o listava, então o volume do workspace
sobrevive de graça) e não existe `task.finished` nem `finished_at` para uma tarefa que só
está esperando um humano, não acabou.

Chamadas do mesmo turno que já tinham rodado antes da pausada continuam rodadas; chamadas
depois dela na mesma lista ficam pendentes exatamente como ficariam se o processo tivesse
morrido ali (o `tool.requested` de todas já é durável desde `_request_tools`, ADR-019
checkpoint b). Isso significa que uma pausa por aprovação e um crash deixam o mesmo tipo de
rastro no log de eventos, e é por isso que retomar uma é retomar a outra: `core/replay.py`
não sabe a diferença, e não precisa saber.

### `approvals`: uma pergunta pendente por tarefa, nunca duas vezes a mesma

Migração `0006`. Colunas: `task_id`, `tool_call_id` (o id do provider, ex. `"call-a"`,
**não** FK para `tool_calls`: no momento do pedido não existe linha em `tool_calls` para uma
chamada que nunca rodou, e depois de um reject nunca vai existir), `tool`, `args_safe`,
`matched_rules`, `reason`, `scopes` (tudo copiado da `Decision` que pausou), `status`
(`CHECK` em `pending|approved|rejected|expired`), `requested_at`, `decided_at`,
`decided_by`, `note`.

Dois índices carregam a semântica:
- `uq_approvals_one_pending_per_task` (parcial, `WHERE status = 'pending'`): no máximo uma
  pergunta em aberto por tarefa, o espelho em banco do fato de que `_pause_for_approval` só
  roda depois que a tarefa já está `WAITING_APPROVAL` e uma tarefa só é reivindicada por um
  worker de cada vez.
- `uq_approvals_task_tool_call` (`task_id`, `tool_call_id`): a mesma chamada nunca é
  perguntada duas vezes, decidida ou não. Sem isso, um resume que pausasse de novo na mesma
  chamada (não deveria acontecer, mas nada além deste índice garante) criaria uma segunda
  pergunta que o replay não saberia distinguir da primeira pelo id.

### Decidir: uma UPDATE guardada, não ler-decidir-escrever

`core/approvals.py::decide_approval` é uma única `UPDATE ... WHERE status = 'pending'`
(mesmo estilo de `queue._CLAIM`/`_HEARTBEAT`, ADR-002), então duas decisões concorrentes
sobre a mesma aprovação (dois cliques, dois reviewers) nunca podem as duas vencer: quem
commita primeiro é quem decide, a segunda `UPDATE` casa zero linhas e vira
`ApprovalAlreadyDecided`. Rejeitar sem nota não passa da validação em Python
(`ValueError`, antes de qualquer SQL): "reject exige nota" é regra de produto desde a semana
4, adiantada aqui porque a mesma função já tinha que decidir os outros casos.

A mesma função devolve a tarefa para `QUEUED` (`UPDATE tasks SET status = 'QUEUED' WHERE
status = 'WAITING_APPROVAL'`, guardada do mesmo jeito, porque "aprovação pendente implica
tarefa esperando" é um invariante que só esta função escreve, não algo para assumir) e grava,
na mesma transação, o evento (`approval.granted` ou `approval.rejected`, carregando o id da
aprovação, o id da chamada do provider e, se for reject, a nota) e a entrada de audit
(`approval.granted`/`approval.rejected`, ator o usuário que decidiu).

### Retomar: o replay diz o que aconteceu, o loop decide o que fazer

`core/replay.py::rebuild` ganhou `ApprovalOutcome` e `ResumeState.approval_decisions`, um
dicionário por id de chamada do provider, lido diretamente de `approval.granted`/
`approval.rejected` no log de eventos, nunca da tabela `approvals` (o mesmo motivo que já
vale para `pending_tool_calls`: tudo que o resume precisa está no log, porque é o log que
sobrevive a um crash, não o estado de uma tabela auxiliar; este módulo continua puro).

Em `core/loop.py::_run_tools`, cada chamada pendente é olhada nesse dicionário antes de
decidir:

- **Rejeitada:** nunca chega à policy de novo. A resposta é sintetizada direto (`is_error`,
  mensagem carregando a nota do revisor), com seu próprio `tool.executed` (para o replay não
  achar essa chamada pendente de novo numa terceira tentativa) e nenhum `policy.decided`
  novo, porque não houve decisão de policy nenhuma dessa vez, foi decisão humana.
- **Aprovada:** passa por `_decide()` de novo, contra a policy que está valendo agora, não a
  de quando o pedido foi feito. Só quando o resultado fresco **não** é `DENY` é que a decisão
  é sobrescrita para `ALLOW` (motivo do reason: `"approved by a human reviewer (approval
  <id>)"`, guardado em `policy_decisions.reason`, então o rastro de auditoria diz exatamente
  por que essa chamada rodou). Um `DENY` novo (alguém apertou o freio de mão depois do
  pedido) vence sempre: **uma aprovação nunca é um jeito de contornar um deny**, é só a
  resposta para a pergunta que a policy fez.

Qualquer outra chamada pendente no mesmo lote (uma pausa pode deixar mais de uma pendente,
como no caso do turno de três chamadas em `test_calls_before_the_paused_one_still_ran_...`)
é decidida normalmente, como se fosse nova.

### Cancelar uma tarefa esperando aprovação

`core/cancel.py::request_cancel` tratava só `QUEUED` (encerra na hora) e `RUNNING` (marca e
espera o loop cooperar). `WAITING_APPROVAL` entra no mesmo ramo do `QUEUED`: não existe
worker rodando essa tarefa para cooperar com um marcador (o próprio ponto de
`_pause_for_approval` liberar o lease), então esperar um `_check_stoppable` que nunca vai
rodar seria esperar para sempre. A aprovação pendente, se existir, vira `expired` na mesma
leva: ninguém pode decidir uma pergunta sobre uma tarefa que não vai mais rodar, e sem isso o
índice parcial (`uq_approvals_one_pending_per_task`) ficaria com uma linha pendente presa
para sempre numa tarefa cancelada.

### Ordem de locks e as duas corridas com cancel

Três escritores tocam a mesma tarefa pausada: o loop (ao pausar), `request_cancel` e
`decide_approval`. Todos pegam **primeiro a linha da tarefa, depois a da aprovação**:

- `request_cancel` já fazia isso (UPDATE em `tasks`, depois expira a aprovação).
- `decide_approval` fazia o contrário (UPDATE guardada em `approvals`, depois `tasks`), e um
  cancel e um approve no mesmo instante podiam cada um segurar um lock esperando o outro:
  Postgres matava um dos dois como deadlock, um 500 para quem clicou. Agora ela trava a
  tarefa antes (`FOR NO KEY UPDATE OF t`, via join com a aprovação, que também responde
  "não existe" na mesma consulta). Quem chegou primeiro vence; um cancel primeiro deixa a
  aprovação `expired` e a decisão recebe `ApprovalAlreadyDecided` (409).
- `_pause_for_approval` trava a tarefa e **relê o marcador de cancel sob esse lock** antes de
  gravar a pergunta. Sem isso, um cancel que commitasse depois do último
  `_check_stoppable` e antes da pausa marcava uma tarefa `RUNNING` (resposta `MARKED`, "o loop
  vai parar") que em seguida virava `WAITING_APPROVAL` com o marcador preso: nada rodando
  para vê-lo, e toda decisão posterior violava `ck_tasks_cancel_requested_only_after_claim`
  ao tentar voltar para `QUEUED`. Com o lock, um cancel anterior vence (a tarefa termina
  `CANCELLED`), e um posterior espera o commit da pausa e cai no ramo imediato de
  `WAITING_APPROVAL`.

A nota do revisor é limitada a 2.000 caracteres na API (mesmo teto de
`events._MAX_ARG_CHARS`): ela vai literal para o evento, para o audit e, num reject, para o
`tool_result` que o modelo lê.

### API: só mapeamento de exceção para status code

`api/routes_approvals.py`: `GET /approvals?status=pending` (escopo `approvals:read`) e
`POST /approvals/{id}/approve|reject` (escopo `approvals:decide`, corpo
`{"note": str | null}`). A rota não decide nada, só traduz o que `core/approvals.py` já
decidiu: `ApprovalNotFound` → 404, `ApprovalAlreadyDecided` → 409, `ValueError` (nota vazia
num reject) → 422. Modelos em `api/approval_schemas.py`, não em `api/schemas.py`: aquele
módulo é de outra trilha desta semana.

## Decisão de produto que não se reabre sem motivo novo

**Rejeitar não cancela a tarefa.** Injeta um `tool_result` com `is_error` carregando a nota
do revisor, e o loop continua: o próximo turno do modelo vê a recusa, entende por que, e
pode tentar outro caminho, exatamente como já acontece com um `DENY` de policy hoje
(`_refusal_message`). Um humano que quer *parar* a tarefa usa `cancel`, que já existe e já
tem semântica clara. Misturar os dois (rejeitar = cancelar) obrigaria a decidir se um reject
é "pare tudo" ou "não faça isso, tente outra coisa", e a resposta muda por tarefa: um deploy
recusado pode ter dez outras formas de terminar o trabalho sem aquele passo. Separar as duas
ações deixa a escolha para quem está revisando, não para uma regra fixa neste código.

## Alternativas consideradas

**Guardar o resultado da aprovação só na tabela `approvals`, sem eventos novos.** Rejeitada:
`core/replay.py` é proposital e permanentemente puro (nunca consulta banco fora do log de
eventos que recebe), porque é o log que sobrevive a um crash e é reconstruído sem depender de
mais nenhuma tabela. Se o resume precisasse de `approvals`, ganharia uma segunda fonte de
verdade para o mesmo fato, e as duas poderiam divergir.

**Reavaliar a policy inteira nas chamadas pendentes que não têm aprovação, ignorando o que já
rodou.** Não é o desenho: as chamadas antes da pausa já executaram (checkpoint (d), ADR-019),
e desfazê-las ou re-julgá-las quebraria "nenhuma tool roda duas vezes", que é a garantia que
todo o resume da semana 2 já protege.

**Deixar uma aprovação sobrescrever um deny mais novo.** Rejeitada de propósito: a ADR-003 já
decidiu que o efeito mais restritivo vence sem exceção ("não existe exceção a um deny, você
escreve o deny mais específico em vez disso"). Uma aprovação é a resposta a uma pergunta que a
policy fez; não é, e não deveria virar, um jeito de a policy de ontem vencer a de hoje.

**Sandbox/container mantido vivo durante a espera.** Não cabe: uma aprovação pode demorar
minutos ou dias, e seria custo real (memória, containers órfãos) parado à toa.
`_pause_for_approval` libera o lease exatamente para que outro worker use a capacidade
enquanto isso, e o volume do workspace (não o container) é o único estado que precisa
sobreviver, e já sobrevive, porque `WAITING_APPROVAL` não é terminal.

## Consequências

- **Uma pergunta pendente por tarefa, não uma fila delas.** Uma tarefa cujo próximo turno
  pede duas chamadas que precisam de aprovação só pausa na primeira; a segunda nem chega a
  ser julgada pela policy (o loop para ali). Aceito: cobre o caso do checklist, e uma fila de
  aprovações por tarefa é complexidade que nenhum teste ou cenário real pediu ainda.
- **Uma aprovação nunca expira sozinha por tempo.** Ela só sai de `pending` por decisão
  (`decide_approval`) ou por cancelamento da tarefa (`request_cancel`). Sem TTL: nada no
  briefing pede um, e adicionar um significaria decidir o que fazer quando ele vence (recusar
  automaticamente? cancelar a tarefa?) sem um caso de uso concreto para guiar a escolha.
- **Audit do loop cresce um pouco.** `policy.deny`, `approval.requested` e `task.finished`
  agora passam por `audit.append`, que seguindo o próprio contrato do módulo (ADR-007) toma
  um lock consultivo transacional; feito sempre dentro do checkpoint curto que já ia commitar
  de qualquer forma (ADR-019: nenhuma transação de audit fica aberta atravessando uma chamada
  de provider ou sandbox), então o custo é o mesmo de qualquer outro passo do loop.
- **Cancelar uma tarefa em `WAITING_APPROVAL` não descarta o volume do workspace na hora.**
  Só o `finally` de `Worker.run_once` descarta volume, e uma tarefa cancelada enquanto espera
  não tem worker nenhum. **Resolvido na trilha de manutenção** (`core/worker.py::discard_
  orphaned_workspace_volumes`): um janitor varre os volumes rotulados `warden.task`, lê o
  status de cada tarefa candidata numa única consulta e descarta **só** os que o próprio banco
  vê como terminais, deixando intocado qualquer um em `QUEUED`/`RUNNING`/`WAITING_APPROVAL`.
  Roda uma vez ao iniciar o worker e depois a cada `JANITOR_INTERVAL_SECONDS` dentro de
  `run_forever`; até a próxima varredura (no máximo esse intervalo), o volume de uma tarefa
  recém-cancelada ainda fica órfão, o que é aceitável porque o custo é disco, não corretude.
  **Tarefa sem linha no banco não é descartada (decidido em 2026-09-23).** O Docker é um só
  por máquina e atende todos os bancos (o `warden` de dev, cada `WARDEN_TEST_DB`, trilhas
  paralelas), então "sem linha aqui" quase sempre é "tarefa de outro banco", às vezes pausada
  e prestes a retomar sobre esse volume. A primeira versão descartava esses volumes, e rodar a
  suíte de testes apagaria o workspace de uma tarefa pausada do banco de dev. UUIDs aleatórios
  garantem que um id terminal num banco nunca é uma tarefa viva em outro. O custo é vazar o
  volume de uma tarefa apagada do banco, e nada no Warden apaga tarefa. Rotular o volume com a
  identidade do banco resolveria as duas coisas; fica para quando isso valer o custo.
- **O tempo de espera humana não conta no `max_seconds` (decidido em 2026-09-23).** O
  prazo mede o tempo do agente, não o do revisor. Sem isso, uma tarefa com `max_seconds: 60`
  aprovada duas horas depois terminava `TIMED_OUT` no primeiro `_check_stoppable` do resume,
  e a chamada aprovada nunca rodava; ficou latente até o budget por tarefa ser ligado ao
  worker. O replay soma, para cada aprovação decidida, o intervalo entre o `created_at` do
  `approval.requested` e o da decisão (os dois vêm do relógio do banco), e o `run_task`
  estende o `max_seconds` por esse total. `task.started_at` continua intocado: o relógio não
  aprende sobre pausas, o orçamento é que cresce. O tempo entre a decisão e o próximo claim
  (fila) ainda conta, igual ao de qualquer resume; normalmente são segundos.
