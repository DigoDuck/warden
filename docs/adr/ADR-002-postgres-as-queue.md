# ADR-002: PostgreSQL como fila e event store

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `feat/queue-and-resume`

## Contexto

O control plane precisa de fila durável: a API aceita uma tarefa e responde, e um worker
executa depois, possivelmente por muitos minutos. Precisa também de event store, porque a
tarefa **é** o log de eventos (§12) e é dele que sai o resume depois de um crash.

A escolha reflexa seria Redis, RabbitMQ ou SQS para a fila, e Postgres para os dados.

## Decisão

**Uma tabela `tasks` no Postgres que já existe, com claim atômico por
`FOR UPDATE SKIP LOCKED` e lease com heartbeat.** Sem Redis, sem broker, sem segundo store.

Dois mecanismos carregam o desenho:

- **`SKIP LOCKED`** faz dois workers pegarem linhas **diferentes** em vez de um esperar o
  lock do outro. Sem ele a fila serializa e o segundo worker não compra nada.
- **Lease com heartbeat** é o que distingue worker morto de worker lento. Quem morre para de
  estender e perde a tarefa quando o lease vence; quem está vivo renova.

O argumento decisivo não é desempenho, é **transação**. Fila e event log no mesmo banco
commitam juntos: não existe tarefa marcada como rodando cujos eventos não foram gravados,
nem o contrário. Com broker separado isso vira two-phase commit ou outbox, que é mais
código para um problema que este projeto não precisa ter.

## Alternativas consideradas

**Redis com BRPOPLPUSH.** Recusada. Acrescenta um serviço ao Compose, um modo de falha e um
store cuja durabilidade tem asteriscos, para ganhar uma vazão que este projeto nunca verá.
E perde o commit atômico com os eventos.

**Broker dedicado (RabbitMQ, SQS).** Mesma objeção, mais operação. Faz sentido quando a fila
atravessa serviços de times diferentes, que não é o caso.

**Celery.** Traz broker, resultado, serialização e um modelo de retry próprio que brigaria
com o budget e o resume do agent loop. O loop já tem a semântica dele.

**Polling com `LISTEN/NOTIFY`.** Adiado, não recusado. O worker faz poll de 1 segundo, que é
irrelevante para tarefas de minutos. Marcado com `ponytail:` no código; troca quando a
latência entre submissão e pickup aparecer numa métrica.

## Consequências

- **Teto documentado.** Um Postgres com `SKIP LOCKED` sustenta com folga a ordem de grandeza
  deste projeto. O sinal de que o teto chegou é contenção visível no claim ou a fila crescer
  mais rápido do que os workers drenam. Aí a saída é `LISTEN/NOTIFY` antes de trocar de
  tecnologia, e só depois um broker.
- **Crash recovery não tem caminho especial.** Worker morto para de estender o lease; outro
  reivindica e reconstrói a conversa do event log. Recuperação é o claim normal encontrando
  uma tarefa que já tem história.
- **O resume exigiu que o event log fosse completo.** Antes deste PR o `model.called` não
  guardava `raw_content` e o `tool.executed` não guardava a saída, então a conversa era
  irreconstituível e a promessa da ADR-016 era falsa. Agora guarda.
- **O event log passa a conter os argumentos completos das tools**, não os redigidos de
  `tool_calls.args_safe`. Argumento redigido não replaya. É seguro porque o modelo nunca vê
  credencial (o broker injeta na execução), então argumento vindo do modelo não contém
  segredo por construção. Revisar junto com o broker na semana 3.
- A retomada acontece **dentro** da iteração interrompida, não na seguinte. Retomar na
  seguinte deixaria um `tool_use` sem resposta; reiniciar a mesma reexecutaria tools que já
  rodaram. Só as pedidas e não executadas rodam, que é o que torna verdadeira a afirmação de
  que nenhuma tool roda duas vezes.
