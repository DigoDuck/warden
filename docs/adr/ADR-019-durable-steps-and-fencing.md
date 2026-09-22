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

**Commit a cada passo, não um por tarefa.** A regra que organiza os pontos de checkpoint em
`core/loop.py`: **nenhuma transação fica aberta enquanto o processo espera algo de fora**,
seja o provider, seja o sandbox.

- (a) depois de `task.created`, e é este commit que libera o lock de linha que o `UPDATE` de
  `started_at` tinha acabado de tomar, cedo o bastante para o heartbeat não passar fome;
- (a2) depois de `iteration.started`, **antes** de chamar o provider. Não estava no desenho
  original e entrou na revisão final; o motivo está em "Correções registradas na revisão";
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
- (e) em `_finish`;
- um turno `pause_turn` também é commitado na hora. Ele não tem tool call, então não passa
  por (b), mas foi pago como qualquer outro: deixado para o próximo checkpoint, um crash
  durante a chamada seguinte o compraria de novo.

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

O fencing tem dois testes. `test_a_worker_that_lost_its_lease_writes_nothing_after_and_cannot_finish` é o estreito e
rápido, sem Docker: B reivindica, A chama um checkpoint e recebe `LeaseLost`.
`test_a_live_worker_that_loses_its_lease_stops_without_touching_what_is_no_longer_its` é o
zumbi de verdade: um `Worker` vivo, com container e volume reais, preso numa chamada de modelo
que não volta; o lease vence, B reivindica, A acorda. Ele tem que devolver `None` sem exceção,
levar o próprio container, **deixar o volume do workspace**, e nada do que fez depois de
acordar, nem a chamada de modelo que pagou, pode estar no log.

**Container órfão.** Um worker morto por `kill -9` nunca roda o `finally` de `run_once`, então
o container que ele criou fica vivo, possivelmente ainda no meio de um comando contra o volume
da tarefa. `Sandbox.create` agora rotula o container com `warden.task=<task_id>` (o volume já
carregava esse rótulo) e, antes de criar o novo container, remove à força qualquer container
existente com o mesmo rótulo: o dono do lease agora é o único dono legítimo. Sandboxes
anônimas (sem `task_id`, usadas em testes avulsos) não são afetadas.

**Ponto de entrada do worker.** `python -m warden.core.worker` ganhou `--script PATH` (roteiro
`FakeProvider` diferente do de demo) e `--policy PATH` (policy diferente da default). Quem
precisou do segundo primeiro foi `tests/test_durability.py`: a policy default não tem regra
que permita um comando de longa duração (`run-project-commands` só libera
pytest/ruff/mypy/npm), e afrouxar a default para caber um teste afrouxaria toda tarefa real.
Não é porta dos fundos: o `task.created` de toda tarefa grava o `policy_hash`, então uma
policy diferente da default fica visível no log de quem rodou com ela.

## Correções registradas na revisão

Esta ADR conserta um defeito que passou porque o teste simulava a falha em vez de causá-la.
A primeira versão do conserto repetiu o erro duas vezes, e as duas foram achadas do mesmo
jeito: causando a falha de verdade.

**Dois crashes seguidos.** O caminho de resume reemitia `tool.requested` para as calls que o
replay tinha acabado de achar pendentes. Como o replay monta "pendente" a partir desses
eventos, cada crash a mais dobrava a entrada: depois de dois crashes o terceiro worker
executava a tool **duas vezes** e respondia um turno de dois `tool_use` com quatro
`tool_result`, que a API rejeita com 400. O desenho original (PR 7) já fazia isso e chamava o
pedido repetido de "registro honesto" num comentário de teste; era inofensivo só porque um
crash real não commitava nada, então o segundo pedido nunca encontrava o primeiro. Foi o commit
por passo que tornou o defeito alcançável. Correção em dois lugares: o loop não pede de novo
o que já está no log (só passa pelo fence), e `replay.rebuild` mantém o primeiro pedido por
`id`, porque é por ali que todo leitor passa. Nenhum teste crashava mais de uma vez;
`test_two_crashes_in_a_row_still_run_each_tool_once` agora crasha.

**O worker que trava em vez de morrer.** A primeira versão desta ADR registrava como
comportamento correto que `claim()` não consegue tomar a tarefa de uma conexão viva e parada:
o `INSERT` em `task_events` ainda não commitado segura um lock `FOR KEY SHARE` na linha da
tarefa pela chave estrangeira, e `FOR UPDATE SKIP LOCKED` pula linha com lock. O mecanismo
está certo. A conclusão estava errada, e o sinal era o próprio teste de fencing, que por causa
disso teve que **simular** o worker zumbi em vez de criar um. O `iteration.started` ficava sem
commit durante toda a chamada ao provider, então um worker preso numa chamada de rede que
nunca volta mantinha a tarefa irreivindicável para sempre, com o lease vencido há horas. O
lease protegia contra worker que morre, não contra worker que trava, e travar é o modo de
falha mais provável de uma chamada HTTP. Com o checkpoint (a2) nada fica aberto durante a
espera, B consegue reivindicar, e é o fence que segura o zumbi quando ele acorda. O replay
ganhou o caso novo que isso cria: um `iteration.started` sozinho no log (crash dentro da
chamada) é uma iteração que não comprou nada e roda de novo, em vez de ser pulada.

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
- **Uma linha de `tasks` com lock não é reivindicável, vencido o lease ou não.** `claim()` usa
  `SKIP LOCKED`, e qualquer `INSERT` não commitado numa tabela filha segura `FOR KEY SHARE` na
  tarefa. Hoje nada fica aberto durante uma espera, mas a regra vale para código futuro: quem
  abrir transação e for esperar rede com ela aberta recria o defeito. Vale lembrar ao depurar
  "por que minha tarefa não foi reivindicada".
- **O container órfão de um zumbi vivo também é removido.** Quando B cria o sandbox, o
  container de A some debaixo dele. É o resultado desejado (A não é mais dono de nada), e a
  próxima tool de A falha e o próximo checkpoint dele levanta `LeaseLost`. Limite conhecido:
  um erro inesperado do Docker nesse intervalo sobe por `run_once` e derruba o `run_forever`
  daquele worker. O volume não é tocado nesse caminho; quem sofre é só o processo zumbi.
- **Efeito colateral corrigido de graça.** O heartbeat parar de morrer de fome era consequência
  do mesmo commit por passo, não uma correção separada; sem ele o worker vivo podia perder a
  própria tarefa por não conseguir avisar que ainda respirava.
