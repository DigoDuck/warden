# Journal

Cinco linhas por semana, escritas na sexta. O que entrou, o que ensinou, o que travou, o que
vem. Semanas contadas pelo calendário a partir de 14/09/2026; o conteúdo do plano andou mais
rápido que o calendário, e isso fica registrado aqui em vez de escondido.

## Semana 1 (14/09 a 20/09)

- Entrou: fundação (schema, CI), contrato de provider com FakeProvider e adaptador Anthropic, loop mínimo, policy engine, sandbox endurecido, fila com lease e resume por replay, audit com hash chain, tools de escrita no sandbox (PRs #1 a #11).
- Aprendi: [RASCUNHO do Claude, reescrever com suas palavras: que um teste verde não prova nada se ele não causa a falha que diz testar; e que "fila" e "event store" cabem no Postgres que eu já tinha, sem Redis.] O teste de resume passava sem provar nada: simulava o crash com exceção e depois commitava, coisa que processo morto não faz.
- Decidi: Postgres como fila e event store, policy YAML com default deny, sandbox sem rede; trabalhar em ondas de PRs paralelos (#11) quando os arquivos não se tocam.
- Travou: a API key da Anthropic exige crédito antes de emitir chave, então `make demo` com modelo real ficou para ~10/10; o Postgres nativo na 5432 capturava a conexão do Compose sem erro claro.
- Próximo: tornar a durabilidade real (commit por passo), identidade por run, API.

## Semana 2 (21/09 a 27/09)

- Entrou: identidade RS256 com revogação por `jti`, commit por passo com fencing de lease (o crash agora mata o processo de verdade), API, secret broker, cancelamento e `max_seconds`, aprovação humana com pausa e resume, scaffold do frontend, índice do audit, janitor de volumes, FakeProvider que continua tarefa retomada (PRs #12 a #21).
- Aprendi: [Que experiêcia de usuário é muito importante, as vezes mais que a boniteza da interface, acessibilidade, moderidade e elegância é importante para a parte visual do site]. Cada nível de revisão achou defeito que o anterior deixou passar: deadlock entre cancel e aprovação, transação aberta segurando lock, force push apagando arquivo de PR.
- Decidi: rejeitar aprovação injeta erro e o loop continua; tempo esperando humano não conta no `max_seconds`; TDD com commit do teste vermelho antes do código, e só vale vermelho de asserção.
- Travou: limite de gasto mensal parou uma onda no meio; o Docker Desktop cai entre sessões e o Norby ocupa 8000 e 5173 (em IPv6, o que engana `localhost`).
- Próximo: telas da semana 4, gateway do GitHub com token por chamada, repo alvo com issues.

## Semana 3 (28/09 a 04/10)

- Entrou: gateway com token por chamada e `github.open_pr` pela API Git Data (#23), repo alvo com CI, 10 issues e testes ocultos (#24), verificador determinístico (#25), behavioral evals como tarefas reais no CI (#26), reviewer independente e definição de sucesso (#27), PR aberto só depois do verdict e proposto pelo control plane (#28), runner do `coding_v1` com teste oculto blindado (#29), auditoria das checklists das semanas 1 a 5 (#30), papel de quem submete na policy (#31).
- Aprendi: [RASCUNHO do Claude, reescrever com suas palavras: que "o modelo propõe, o control plane decide" também vale para o resultado da tarefa, não só para as tool calls; que um revisor LLM é opinião (selo "Gerado"), não verificação; e que o furo mais sério da semana, nenhuma tarefa da UI conseguir escrever código, só apareceu testando clicando, com todos os testes verdes.]
- Decidi: sucesso exige evidência verde E verdict aprovado (o modelo nunca passa por cima de um teste vermelho); o agente não tem `open_pr`, quem publica é o control plane depois do verdict, e rejeitar a publicação cancela a tarefa; o teste oculto roda com `python -I`, `--noconftest` e `-c /dev/null`; `user.role` na policy é o papel de quem submete.
- Travou: duas sessões implementaram a verificação em paralelo sem saber uma da outra (a trilha local colidiu com o #25 e foi portada); o meu plano do #28 assumiu que chamar uma tool fora do registry já era negado, e não era; o `make user-token` criava todo usuário sem permissão de escrita.
- Próximo: Planner, `docker compose up` com a stack inteira e quickstart sem chave, cobertura de `policy/` e `core/` ≥ 80% no CI; com a chave (~10/10), `make demo` real, rodada do `coding_v1` e a tag `v0.1.0`.
