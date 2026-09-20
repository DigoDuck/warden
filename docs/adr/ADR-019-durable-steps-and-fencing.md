# ADR-019: passo durável e fencing por lease

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `fix/durable-steps`

## Contexto

O checklist da semana 2 promete: "uma tarefa sobrevive a um crash". O teste que devia provar
isso, `test_a_crashed_run_resumes_and_no_tool_runs_twice`, está verde desde que foi escrito.
A promessa é falsa.

`run_task` (`core/loop.py`) só chamava `session.flush()` ao longo da tarefa inteira. O único
`session.commit()` do caminho normal ficava em `worker.py`, logo depois de
`run_claimed_task` retornar. Uma tarefa inteira era **uma transação só**. Se o processo do
worker morresse de verdade (`kill -9`, OOM, queda de energia), o Postgres desfazia tudo: nenhum
evento sobrevivia, o resume começava do zero, cada chamada de modelo já paga era comprada de
novo e cada tool já executada rodava de novo. Exatamente o oposto do que a ADR-002 diz sobre
recuperação de crash não ter caminho especial: aquela frase só é verdadeira a partir desta ADR.

**Por que o teste antigo não pegou isso.** `_crash_midway`, em `test_resume.py`, simulava a
morte do worker levantando um `RuntimeError` de dentro de uma tool fake e capturando essa
exceção com `pytest.raises`. Depois disso, a própria linha seguinte do teste chamava
`await session.commit()`. Um processo morto de verdade nunca chega a essa linha. O teste
provava que o replay reconstrói a conversa corretamente a partir de um log completo; nunca
provou que o log sobrevive a um processo que simplesmente para de existir. É a diferença entre
testar a lógica de recuperação e testar a recuperação.

Escrito antes de qualquer linha de correção, um teste levantou um segundo problema, suspeitado
mas não assumido por raciocínio: com a tarefa inteira como uma transação, `task.started_at` é
gravado (um `UPDATE`, que no Postgres toma um lock de linha) segundos depois da tarefa começar,
e esse lock fica preso até o commit final. O heartbeat de `worker.py::_beat` roda numa conexão
separada e faz o mesmo tipo de `UPDATE` na mesma linha. `tests/test_durability.py::
test_a_run_in_progress_does_not_starve_the_heartbeat` confirmou vermelho contra o código sem
esta correção: o heartbeat travava (`asyncio.wait_for` estourava) em vez de estender o lease.
Numa tarefa mais longa que o lease, o próprio worker vivo perderia a tarefa para si mesmo,
porque não conseguia avisar que ainda estava vivo.

## Decisão

**Commit a cada passo, não um por tarefa.** Cinco pontos de checkpoint em `core/loop.py`:

- (a) depois de `task.created`, e é este commit que libera o lock de linha que o `UPDATE` de
  `started_at` tinha acabado de tomar, cedo o bastante para o heartbeat não passar fome;
- (b) depois de `model.called`, **junto com todo `tool.requested` daquele turno**, num commit
  só. Emitir cada `tool.requested` no seu próprio commit, como cada tool era decidida, abriria
  uma janela nova: um crash depois da primeira tool executar e antes do `tool.requested` da
  segunda ser gravado faria `core/replay.py` achar que não sobrava nada pendente (pendente é
  `tool.requested` menos `tool.executed`), e a próxima chamada de modelo sairia com um
  `tool_use` sem resposta, que a API rejeita. `finish` é decisão deliberada: não recebe
  `tool.requested`/`tool.executed`, porque não está registrado no registry de tools, e um
  `tool.requested` sem `tool.executed` correspondente faria um resume tentar despachar uma
  tool que não existe (`tests/test_loop.py::
  test_finish_leaves_no_tool_requested_or_tool_executed_event`);
- (c) depois de `policy.decided`, antes de executar a tool: um deny fica registrado mesmo que
  o processo morra em seguida;
- (d) depois de `tool.executed`, junto com as linhas de `tool_calls` e `policy_decisions`;
- (e) em `_finish`.

Cada checkpoint é `_checkpoint()`, que faz `session.commit()` puro quando não há `holder`
(chamada direta de `run_task`, como `demo.py` e os testes de loop fazem hoje) ou, com um
`holder`, comete a decisão em cima de um **fencing**.

**Fencing.** Commit por passo piora um problema que já existia: um worker que perdeu o lease
continua rodando, e sem fence interfolharia eventos com o novo dono e corromperia o replay.
Cada checkpoint verifica, na mesma transação que está prestes a commitar,
`SELECT claimed_by FROM tasks WHERE id = :task_id FOR UPDATE` (`queue.verify_holder`, no estilo
SQL cru que o resto de `queue.py` já usa). Se `claimed_by` não é mais quem chamou, o checkpoint
faz `rollback()` e levanta `queue.LeaseLost`, sem marcar a tarefa como terminada e sem escrever
mais nada. `run_task` ganha `holder: str | None`; o `Worker` passa o próprio id.
`Worker.run_once` trata `LeaseLost` como "pare em silêncio, a tarefa é de outro dono": ainda
destrói o próprio container, e deliberadamente **não** descarta o volume do workspace, porque
quem tem o lease agora ainda precisa dele.

Escrever o teste de fencing revelou algo que o raciocínio sozinho não previa: uma conexão
Postgres que ainda está viva, só pausada, **não** pode ser roubada por `queue.claim()`. O
`INSERT` de `task_events` referencia `tasks.id` por chave estrangeira, e o Postgres toma um
lock de linha implícito (`FOR KEY SHARE`) na linha referenciada enquanto essa transação não
commita; `SELECT ... FOR UPDATE SKIP LOCKED` respeita esse lock e pula a linha em vez de
devolvê-la. Isso está correto: uma conexão genuinamente viva ainda pode acordar e commitar, e
deixar `claim()` roubar a tarefa dela seria a própria corrida que o fencing existe para evitar.
Um processo morto de verdade não deixa esse problema: o kernel fecha o socket na hora, o
Postgres nota a desconexão e desfaz a transação, soltando o lock, tipicamente em milissegundos.
`tests/test_durability.py::test_a_worker_that_lost_its_lease_writes_nothing_after_and_cannot_finish`
simula o crash chamando `_checkpoint`/`_finish` diretamente depois que B já reivindicou a
tarefa, em vez de pausar uma execução com a conexão ainda de pé, que é a razão de o teste não
usar `run_task` ao vivo para essa parte.

**Container órfão.** Um worker morto por `kill -9` nunca roda o `finally` de `run_once`, então
o container que ele criou fica vivo, possivelmente ainda no meio de um comando contra o volume
da tarefa. `Sandbox.create` agora rotula o container com `warden.task=<task_id>` (o volume já
carregava esse rótulo) e, antes de criar o novo container, remove à força qualquer container
existente com o mesmo rótulo: o dono do lease agora é o único dono legítimo. Sandboxes
anônimas (sem `task_id`, usadas em testes avulsos) não são afetadas.

**Ponto de entrada do worker.** `python -m warden.core.worker` ganhou `--script PATH` (roteiro
`FakeProvider` diferente do de demo) e `--policy PATH` (policy diferente da default). O
segundo existe só para `tests/test_durability.py`: a policy default não tem regra que permita
um comando de longa duração (`run-project-commands` só libera pytest/ruff/mypy/npm), e
afrouxar a default para caber um teste afrouxaria toda tarefa real. Uma policy só de teste,
carregada por essa flag, mantém `policies/default.yaml` intocada.

## Alternativas consideradas

**Aceitar a promessa como aspiracional e só corrigir o teste.** Não serve: o teste estava
mascarando exatamente o defeito que o item da semana pede para provar.

**Outbox / two-phase commit com um broker separado.** A ADR-002 já rejeitou broker separado
pela mesma razão que rejeitaria aqui: fila e event log no mesmo banco é o que torna o commit
por passo uma linha de código (`session.commit()`) em vez de um protocolo.

**Commit só por iteração, não por tool dentro dela.** Mais simples, mas não fecha a janela
entre a primeira tool executar e a segunda ser pedida, que é exatamente o defeito descrito em
(b) acima: um turno com duas tool calls ficaria vulnerável a um crash no meio dele produzindo
um `tool_use` sem resposta no próximo turno.
`tests/test_durability.py::test_a_turns_tool_requests_all_commit_before_the_first_one_executes`
prova o contrário a partir de uma conexão separada: só ver os dois `tool.requested` de lá, no
meio da execução da primeira tool, já é a prova de que os commits acontecem no meio da tarefa.

**Marcar `finish` com um `tool.requested`/`tool.executed`, por simetria com as outras tools.**
Rejeitada: `finish` não está no registry, então um resume que achasse um `finish` pendente
tentaria despachá-lo e falharia com `UnknownToolError`. A linha em `tool_calls` (para
auditoria) já existe sem precisar do evento.

## Consequências

- **O limite que sobra, dito em voz alta.** Entre uma tool terminar de executar dentro do
  sandbox e o commit do `tool.executed` (checkpoint d), ainda existe uma janela: um crash bem
  ali faz aquela tool rodar de novo na próxima tentativa. Isso é *at-least-once* nessa janela
  específica, não *exactly-once*; isso exigiria tools idempotentes ou um protocolo de duas
  fases com o sandbox, que não existe hoje. Das tools atuais, `write_file` é idempotente
  (escrever o mesmo conteúdo duas vezes dá o mesmo resultado), um `apply_patch` repetido falha
  limpo no `--check` (o diff já foi aplicado, então não aplica de novo, e o erro volta como
  resultado de tool, não como corrupção), e `run_command` repete o comando de verdade, com
  qualquer efeito colateral que ele tiver. Aceito: o degrau abaixo (a tarefa inteira perdida)
  era pior, e a maioria dos comandos deste projeto (leitura, teste, lint) é naturalmente segura
  de repetir.
- **Mais commits por rodada.** Uma tarefa com três iterações e duas tool calls por turno passa
  de um commit para uma dezena. Irrelevante perto do custo de uma chamada de modelo: um
  round-trip de commit ao Postgres local custa baixos milissegundos, uma chamada de modelo
  custa segundos e centavos. O que se compra com isso (a diferença entre perder uma tarefa
  inteira e perder o último passo dela) vale muito mais do que esse custo.
- **`queue.claim()` só rouba uma tarefa de uma conexão que já foi embora.** Documentado acima
  na decisão de fencing porque foi descoberto escrevendo o teste, não planejado de antemão:
  vale registrar para quem for depurar "por que minha tarefa não foi reivindicada" no futuro.
- **Efeito colateral corrigido de graça.** O heartbeat parar de morrer de fome era consequência
  do mesmo commit por passo, não uma correção separada; sem ele o worker vivo podia perder a
  própria tarefa por não conseguir avisar que ainda respirava.
