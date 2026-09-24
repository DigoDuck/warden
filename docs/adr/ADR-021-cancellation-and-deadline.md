# ADR-021: cancelamento cooperativo e deadline por tempo de parede

**Status:** aceita · **Data:** 2026-09-22 · **PR:** `feat/cancel-and-deadline`

## Contexto

Dois itens da semana 2 seguiam abertos: cancelar uma tarefa em execução (a parte do control
plane; o endpoint HTTP fica para quando `api` existir) e um teto de `max_seconds` para o
loop, hoje só limitado por `max_iterations`. Briefing §16 já descreve os dois:

> Cancelamento cooperativo: o loop checa uma flag a cada iteração e antes de cada tool.
> Comandos longos no sandbox recebem `docker kill`.
> Timeout em duas camadas: por tool (`asyncio.wait_for`) e por tarefa (`max_seconds`,
> checado no loop).

Não existe forma segura de interromper um `provider.generate()` ou um `sandbox.exec()` já em
voo a partir de outro processo: nenhuma das duas APIs oferece um jeito de abortar uma
chamada em andamento sem simplesmente derrubar o que a sustenta (a conexão HTTP, ou o
container). "Cooperativo" é a palavra certa: quem pede o cancelamento só grava uma intenção;
quem está rodando a tarefa é quem decide quando é seguro parar.

## Decisão

### O marcador

`tasks.cancel_requested_at timestamptz NULL` (migração `0005`), verificado pelo loop, nunca
pelo próprio worker que o serve. Uma tarefa `QUEUED` nunca tem o marcador setado: como nada
a está executando, não existe "cooperar" ali, então `request_cancel` a cancela na hora
(`core/cancel.py`). O banco garante essa invariante com um `CHECK`
(`ck_tasks_cancel_requested_only_after_claim`, `status != 'QUEUED' OR cancel_requested_at
IS NULL`), constraint antes de regra em código, como o resto deste projeto prefere.

### `request_cancel`: uma tarefa QUEUED encerra, uma RUNNING ganha o marcador

`core/cancel.py::request_cancel` é um único `UPDATE` guardado por `status`, no mesmo estilo
de `queue._CLAIM`/`_HEARTBEAT`:

```sql
UPDATE tasks
   SET status = CASE WHEN status = 'QUEUED' THEN 'CANCELLED' ELSE status END,
       finished_at = CASE WHEN status = 'QUEUED' THEN now() ELSE finished_at END,
       cancel_requested_at = CASE
           WHEN status = 'RUNNING' THEN COALESCE(cancel_requested_at, now())
           ELSE cancel_requested_at
       END
 WHERE id = :task_id AND status IN ('QUEUED', 'RUNNING')
RETURNING status
```

Uma instrução só, não "ler status, decidir, escrever": o Postgres avalia toda a lista `SET`
de um `UPDATE` contra a imagem *anterior* da linha, nunca contra uma coluna que a mesma
instrução está no meio de mudar, então os dois ramos (QUEUED e RUNNING) nunca podem
enxergar um `status` que já mudou por conta própria. Isso é o que torna a operação
race-safe contra `queue.claim()`: as duas tomam o lock da mesma linha, e quem pega o lock
primeiro é quem decide. Se o claim vem antes, este `UPDATE` espera, reavalia `WHERE` e `SET`
contra a linha que o claim commitou, lê `RUNNING` e seta o marcador. Se o cancel vem antes,
o `SKIP LOCKED` do claim pula a linha e depois a encontra `CANCELLED`. `tests/test_cancel.py::
test_cancel_racing_claim_never_lets_the_worker_win_a_task_that_was_cancelled` roda as duas
de verdade, concorrentes, e prova que não existe terceiro resultado (reivindicada sem
marcador e sem cancelamento registrado).

`COALESCE` faz um segundo pedido de cancelamento ser idempotente: não empurra o timestamp
para frente. Um evento `cancel.requested` é gravado a cada pedido de qualquer forma, para
auditoria de quantas vezes foi pedido; só o timestamp em si não se move.

### Dois escritores no mesmo log de eventos: `seq` sob lock da linha da tarefa

`cancel.requested` é o primeiro evento gravado por alguém que não é o worker dono da
tarefa, e isso quebrou uma premissa antiga de `events.append_event`: `seq` era
`max(seq)+1` sem lock, seguro só com um escritor por tarefa. Com dois, os dois leem o mesmo
máximo e escolhem o mesmo `seq`. O cancel então espera no índice único pela transação do
worker, e o worker, no fence do checkpoint (`queue.verify_holder`, `FOR UPDATE`), espera
pelo lock de linha que o `UPDATE` do cancel já tomou. É um deadlock de verdade: o Postgres
mata um dos dois, e qualquer perdedor é um bug (worker que cai no meio do run, ou pedido de
cancelamento que falha). `test_cancel.py::test_a_cancel_landing_while_the_worker_holds_an_
uncommitted_event_does_not_collide` reproduz a sequência exata e ficou vermelho antes da
correção com `DeadlockDetectedError`.

A correção: `append_event` toma `SELECT 1 FROM tasks WHERE id = :id FOR NO KEY UPDATE`
antes de ler o máximo. Todo escritor passa a se ordenar no mesmo lock antes de tocar o
índice, e o `max` é lido por uma instrução nova depois da espera, então enxerga o que o
anterior commitou. `NO KEY UPDATE` basta para serializar escritores sem bloquear as checagens
de chave estrangeira de outras tabelas. O custo é uma query a mais por evento e o heartbeat
esperando, por milissegundos, entre um `append_event` e o checkpoint seguinte, janelas que o
ADR-019 já mantém curtas (nada lento roda com evento não commitado).

Uma tarefa já terminal, ou um id desconhecido, não é exceção: `CancelOutcome` tem os quatro
resultados (`CANCELLED`, `MARKED`, `ALREADY_TERMINAL`, `NOT_FOUND`) porque um cancelamento
tarde demais é uma resposta normal, não uma falha da operação.

### O loop: `_check_stoppable`, chamado nos dois pontos do briefing

`core/loop.py::_check_stoppable(session, task, budget)` lê `cancel.is_requested` (uma
query direta, sem lock: não há nada aqui para proteger, só uma decisão de continuar ou não)
e, se `budget.max_seconds` estiver setado, compara `_now() - task.started_at` contra ele.
Uma função só para os dois motivos de parar, porque os dois têm que vencer "continuar"
exatamente nos mesmos dois pontos: no topo de cada iteração (antes de
`provider.generate`) e dentro de `_run_tools`, antes de cada tool (antes de `_decide`).
Uma tarefa cancelada ou expirada nunca faz mais uma chamada de modelo, porque parar bem
ali, antes do `generate`, é exatamente essa garantia.

Os dois compartilham `_RunStopped(status, reason)`, levantada de dentro de
`_check_stoppable`/`_run_tools` e capturada uma vez em `run_task`, que termina a tarefa por
`_finish` (checkpoint (e) de ADR-019, então nada aqui abre um commit próprio). Devolver um
sentinel por várias camadas de chamador seria mais ruído que levantar uma vez e capturar
onde a tarefa de fato termina; é o mesmo raciocínio de `queue.LeaseLost`, só que a exceção
mora no módulo que decide o desfecho, não no módulo que só observa o marcador.

**`task.started_at` é o relógio, e uma retomada não o reinicia.** `queue.claim()` já fazia
`started_at = COALESCE(started_at, now())`; `max_seconds` só passou a depender dessa
garantia existir. Uma tarefa que crasha e retoma mantém o mesmo relógio, então o deadline
não "ganha" tempo de graça a cada crash. `datetime.now(UTC)` virou `_now()`, um wrapper de
uma linha, só para o teste poder congelar e adiantar o tempo com `monkeypatch.setattr`
em vez de dormir de verdade — `test_a_run_past_its_max_seconds_deadline_stops_before_
the_next_tool` não tem um único `asyncio.sleep`.

### Matando uma tool longa: o vigia do worker, não o loop

O loop não conhece o Docker (`core` decide, não executa — briefing §10); ele só sabe olhar
o marcador no banco. Quem tem o container é `Worker`, em `core/worker.py`, então é lá que
mora o mecanismo que faz um `run_command "sleep 30"` já em voo morrer depressa em vez de
esperar seu próprio `kill_after`.

`_watch_cancel`, uma task irmã de `_beat`, não dobrada nela: as duas existem para
perguntas diferentes em relógios diferentes. `_beat` estende um lease de minutos; isso
tem que notar em segundos. Faz *poll* de `cancel.is_requested` a cada `CANCEL_POLL_SECONDS`
(1s, contra a fração do lease de `_beat`) e, ao ver o marcador, chama
`Sandbox.kill_for_cancel()` e termina.

`kill_for_cancel` é deliberadamente **não** `_kill_sync`. `_kill_sync` existe para quando um
comando estoura o próprio `kill_after`: mata o container e o reinicia, porque o resto da
tarefa ainda vem por aí e precisa de um container vivo. Uma tarefa cancelada não tem "resto
da tarefa" — o loop está prestes a terminá-la, e `Worker.run_once` destrói esse sandbox logo
em seguida — então reiniciar aqui seria trabalho perdido correndo contra esse desmonte, e o
container pareceria vivo de novo por um instante para ninguém. `kill_for_cancel` reusa
`_wait_until_stopped()` sem tocar nela, só sem o `start()`/`_wait_until_started()` que o
`_kill_sync` chama depois.

Isso não muda em nada o caminho de timeout por comando (`Sandbox.exec`, `_kill_sync`,
`_wait_until_started`): é um método novo, irmão, que nunca é chamado por esse caminho. O
flake conhecido e não reproduzido de `test_a_command_past_its_deadline_is_a_tool_error_and_
recovers` ("cannot exec in a stopped state", visto uma vez em uma execução completa,
0 falhas em mais de 40 tentativas de reproduzir) mora inteiramente em `_kill_sync`/
`_wait_until_started`, que este PR não toca; o suspeito, se voltar a aparecer, continua
sendo uma corrida ali, não algo que este trabalho poderia ter introduzido.

**Causa provável corrigida na trilha de manutenção (2026-09-23), não confirmada no daemon
real.** Ocorreu 3 vezes no total, nunca sob demanda. Uma tentativa real de reprodução (652
ciclos de kill-and-restart, 8 depois 16 sandboxes em paralelo mais carga de CPU, contra Docker
de verdade) deu 0 falhas: a taxa real é baixa demais para uma sessão de trabalho reproduzir.
Causa provável: em `sandbox/docker.py::_wait_until_stopped`, um `APIError` vindo de `reload()`
era tratado exatamente como `NotFound` (container confirmado morto). `NotFound` prova mesmo;
`APIError` não, um erro ambíguo do daemon durante o kill não é a mesma coisa que o container
ter efetivamente morrido. Um `APIError` isolado bastava para `_wait_until_stopped` devolver
"parado" cedo demais, `start()` rodava (sem efeito real) num container que um daemon de
verdade recusa reiniciar por ainda estar rodando, e `_wait_until_started` via `Running=True`
desse mesmo container nunca reiniciado e declarava sucesso. O kill de verdade, pedido
instantes antes, terminava de derrubar o container mais tarde, e o próximo `exec` encontrava
um container que já não existia mais. O defeito de código foi provado por injeção de falha
determinística na fronteira do `reload()` (o real não era alcançável em tempo hábil):
`tests/test_sandbox.py::test_an_ambiguous_reload_error_mid_kill_is_retried_not_trusted`.
Correção: `_wait_until_stopped` agora só aceita `NotFound` como prova de morte; um `APIError`
é reexperimentado dentro do mesmo prazo (`KILL_GRACE_SECONDS`), igual a qualquer outro poll
inconclusivo, em vez de virar "parado" na primeira resposta ambígua. O que a injeção **não**
prova é que o daemon de verdade devolveu esse `APIError` nas 3 ocorrências: nenhuma deixou log
do `reload()`. Se o flake voltar com esta correção, a hipótese cai e a próxima candidata é o
fallback de `KILL_GRACE_SECONDS` esgotado sob carga (H2 do commit `d82442f`).

**Do lado do loop:** `registry.execute()` matado no meio nem sempre levanta `ToolError` (o
container pode simplesmente devolver um resultado esquisito, ou uma exceção do docker-py que
não é `ToolError`). `_run_tools` cobre os dois casos: um `except Exception` mais amplo em
volta da execução, que só absorve a falha depois de confirmar `cancel.is_requested` (senão
relança — um bug de verdade continua sendo um bug de verdade, não vira "cancelamento" por
suposição), e uma checagem depois de toda execução, sucesso ou erro, que também levanta
`_RunStopped` se o marcador estiver setado. As duas juntas fecham o caso em que a tool
matada termina sem lançar nada que a primeira checagem visse.

## Alternativas consideradas

**Deletar/matar o container direto do handler de `request_cancel`, sem vigia.** Recusada:
`request_cancel` mora em `core/cancel.py`, que não tem (e não deveria ter) uma referência ao
`Sandbox` de um worker específico rodando em outro processo. Rotear cancelamento pelo
Docker exigiria uma tabela ou canal só para isso, quando o próprio marcador já é esse canal.

**Um único evento/`asyncio.Event` compartilhado entre a tool em execução e um observador,
em vez de dois pollers de banco.** Funcionaria dentro de um processo, mas o pedido de
cancelamento chega de uma sessão/processo diferente (API, outro worker); um `Event` em
memória não atravessa esse limite. O poll de 1s do `_watch_cancel` é a mesma tradeoff que
ADR-002 já aceitou para o claim da fila.

**Matar o container achando pela flag em vez de confirmar com uma query.** Rejeitada em
`_run_tools`: uma exceção genérica do sandbox por qualquer outro motivo (imagem corrompida,
daemon caiu) seria silenciosamente relabelada como cancelamento e escondida do log.

## Consequências

- **`max_seconds: float | None = None` em `Budget`.** `None` mantém o comportamento de hoje
  (sem teto de tempo); `max_iterations` continua terminando `TIMED_OUT` exatamente como
  antes — `test_max_iterations_still_ends_timed_out_when_no_deadline_is_set` fixa isso.
- **Uma tool já requisitada (`tool.requested` commitado) pode nunca ganhar
  `tool.executed`/`policy.decided`.** Acontece quando o cancelamento é visto entre o commit
  do lote e a decisão daquela call específica. Honesto, não um bug: a tarefa termina
  `CANCELLED`, é terminal, e `queue.claim()` nunca a reivindica de novo, então o replay
  (que constrói "pendente" como requested-menos-executed) nunca precisa explicar essa
  lacuna a um resume.
- **O workspace de uma tarefa `CANCELLED` some como o de qualquer terminal.**
  `worker.py::TERMINAL_STATUSES` já incluía `CANCELLED`; nada mudou aí.
- **Corrida aceita entre `_check_stoppable` e o fim natural de uma iteração.** Um
  cancelamento pode chegar depois do último ponto checado e antes de `_finish` gravar
  `SUCCEEDED`; a tarefa termina `SUCCEEDED` com `cancel_requested_at` ainda setado. Não é um
  defeito, é a definição de cooperativo: o pedido só é honrado no próximo ponto de checagem,
  e às vezes esse ponto não chega a existir porque a tarefa já tinha acabado.

## Adendo (2026-09-22): orçamento por tarefa, aplicado de verdade

O `Budget` desta ADR sempre existiu (`max_iterations`, `max_usd`, `max_seconds`), mas até
`feat/cancel-endpoint-and-budget` só o teto fixo passado a `Worker.__init__` valia — o
`tasks.budget` que `POST /tasks` já aceita e valida (`BudgetIn`) nunca era lido pelo worker.
Esta PR fecha essa lacuna e adiciona `max_seconds` a `BudgetIn` (faltava; só `max_iterations`
e `max_usd` existiam).

**Regra de segurança: `tasks.budget` só pode apertar o teto do worker, nunca afrouxar.**
`tasks.budget` chega de um chamador da API; tratá-lo como confiável ao ponto de *substituir*
o teto do worker deixaria qualquer usuário pedir `max_usd: 999999` e gastar o que quisesse.
`core/worker.py::merge_budget` é uma função pura que faz `min()` campo a campo (o valor do
worker quando a tarefa não define um) e nunca o inverso. Testada exaustivamente em
`tests/test_worker.py` sem precisar de banco nem Docker.

**Valor inválido no JSON não derruba o worker.** `tasks.budget` é `JSONB`; nada no banco
impede outro código de escrever uma string, um negativo, `Infinity` (como texto) ou até algo
que não seja um objeto ali, mesmo com `BudgetIn` validando a entrada da API. `merge_budget`
trata qualquer valor que não seja um número positivo e finito exatamente como "a tarefa não
definiu este campo": ignora e mantém o teto do worker. Alternativa descartada: falhar a
tarefa (`FAILED`) num valor ruim. Rejeitada pelo mesmo motivo que a API devolve 422 em vez de
500 num corpo malformado — um problema de forma do dado do lado de fora não deveria derrubar
o controle do lado de dentro, e o teto do worker já protege igual sem o campo da tarefa.

**`Decimal(str(valor))`, não `Decimal(valor)`, para `max_usd`.** O JSON decodifica número
para `float`/`int` do Python, nunca `Decimal`. `Decimal(0.1)` carrega a imprecisão binária do
float (`0.1000000000000000055511151231257827021181583404541015625`); `Decimal(str(0.1))` não.
Testado explicitamente (`test_a_tighter_max_usd_wins_and_comes_back_as_a_decimal`).

**`max_seconds: float | None = None` em `Budget` significa "sem teto", e uma tarefa ainda
pode apertá-lo a partir daí.** `_tightened_seconds` trata `ceiling=None` como "nada para
comparar, use o valor da tarefa", não como zero — do contrário nenhuma tarefa conseguiria
pedir um deadline quando o worker roda sem um por padrão, o que inverteria a regra de
"aperta, nunca afrouxa" para esse campo específico.

**`BudgetIn.max_seconds` ganha o mesmo teto de 86 400s (um dia) que `identity
.USER_TTL_CAP_SECONDS` já usa (ADR-020) para a validade de um token.** Não protege nada
sozinho (`merge_budget` é quem protege o worker), é só sanidade na entrada: 422 antes de
gravar um número absurdo em vez de deixar a coluna aceitar qualquer coisa.
