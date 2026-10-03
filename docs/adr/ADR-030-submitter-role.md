# ADR-030: `user.role` na policy é o papel de quem submete a tarefa

**Status:** aceita · **Data:** 2026-10-03 · **PR:** `fix/user-token-role`

## Contexto

A auditoria das semanas 1 a 5 (`docs/auditoria-semanas-1-5.md`, achado 1) mostrou que nenhuma
tarefa submetida pela UI conseguia editar código. A regra `write-source` de
`policies/default.yaml` só libera `write_file`/`apply_patch` quando `user.role: worker`, e o único
jeito de criar usuário fora dos testes, `make user-token`, criava todo mundo com `role="user"`.
Testes e evals nunca pegaram isso porque criam usuários `worker` direto no banco. O fluxo
"issue vira PR" da semana 5 nunca funcionaria a partir da tela.

Havia duas leituras possíveis para `user.role` na policy:

- **(a)** o papel do humano que submeteu a tarefa, em nome de quem o agente age;
- **(b)** um atributo do próprio agente, que não deveria depender de quem submeteu.

## Decisão

**(a).** O agente age em nome de quem pediu, e o que ele pode fazer fica limitado pelo papel
dessa pessoa. Isso já era o que o código fazia (`run_task` monta o `UserRef` a partir do dono da
tarefa); faltava um jeito de criar um usuário com o papel certo.

- `make user-token ... role=worker` cria um usuário `worker`. Sem `role=`, um usuário novo é
  `user`, e as tarefas dele só leem o repositório. Papéis aceitos: `user` e `worker`.
- **Um papel existente nunca é alterado por esse comando.** Pedir `role=worker` para um e-mail que
  já existe como `user` é recusado com a explicação no stderr e stdout vazio, para
  `TOKEN=$(make user-token ...)` capturar um token ou nada. Promover `user` a `worker` amplia o
  que toda tarefa futura daquele usuário pode escrever; isso não pode ser efeito colateral de um
  comando cujo trabalho é emitir token. Para outro papel, outro e-mail.

### Escopo e papel são eixos diferentes

ADR-020 decidiu que a API não lê `users.role`: o que um usuário pode pedir à API são os
**escopos** do token (`tasks:write`, `approvals:decide`). O papel responde a outra pergunta:
o que o agente pode fazer **dentro** de uma tarefa daquele usuário. Um token com `tasks:write`
de um usuário `user` enfileira tarefas; essas tarefas só não escrevem código. As duas fontes não
competem, porque não decidem a mesma coisa.

## Alternativas consideradas

**(b), tirar `user.role` de `write-source`.** Resolveria o sintoma apagando um controle: qualquer
token com `tasks:write` passaria a poder mandar o agente alterar código. Recusado.

**Promover o papel de um usuário existente quando `role=` for diferente.** Mais conveniente para
dev, e exatamente o tipo de mudança de privilégio silenciosa que o projeto evita em todo o resto.

**Um papel padrão `worker` para todo usuário novo.** Inverteria o default seguro: quem esquecesse
o parâmetro ganharia o papel mais amplo.

## Consequências

- A tela de Configurações passa a sugerir o comando com `role=worker` e os escopos de aprovação,
  e explica o que acontece sem ele.
- Usuários de dev já criados como `user` continuam `user`. Para testar edição de código pela UI,
  gere um token com um e-mail novo e `role=worker`.
- Não existe ainda uma forma de gerenciar papéis (nem `/auth/login`). Quando existir, ela herda
  a regra: mudança de papel é uma ação explícita e auditada, nunca um efeito colateral.
