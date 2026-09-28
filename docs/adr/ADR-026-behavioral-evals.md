# ADR-026: behavioral evals como tarefas reais, com casos pendentes visíveis

**Status:** aceita · **Data:** 2026-09-28 · **PR:** `feat/behavioral-evals`

## Contexto

A semana 6 pede "12/12 behavioral evals passam no CI, custo zero" e o plano proíbe cortar
esses evals. Eles testam o control plane, não o modelo: o provider é o `FakeProvider`
replayando um roteiro, e todo o resto (fila, worker, sandbox, policy, gateway, audit) tem que
ser o de produção, senão o eval mede um dublê. Três dos 12 casos do briefing (§46) dependem
de funcionalidades que ainda não existem: manifest de capacidades com incidente (#4), ciclo de
vida de agente com revogação (#9) e regra de policy com estado (#12).

## Decisão

**Cada caso é uma tarefa real.** `evals/runner.py` enfileira pela `queue`, roda um `Worker`
de verdade com um `FakeProvider` `resume_aware` montado do roteiro do caso, e confere as
expectativas lendo o banco: efeitos de policy em ordem, status final, tools executadas
(contando só `effect == allow`), aprovações pendentes, linhas de audit, `args_safe`.

**Casos pendentes são visíveis, nunca pulados.** Os casos 4, 9 e 12 ficam no YAML com
`status: pending` e o motivo. O runner reporta "9/12 pass, 3 pending": aparecem, contam, e não
derrubam o código de saída. Cada um vira ativo quando a funcionalidade dele chegar. O checker
é estrito para o gate não ficar verde por acidente: chave de expectativa desconhecida é erro,
caso ativo sem expectativas é erro, e zero casos pontuados sai com código diferente de zero.

**As ações de cada caso ficam em código, não numa DSL no YAML.** O §19 do briefing esboça um
campo `actions:` (ex.: `after_event: tool.requested, do: cancel`). Só três casos têm ação
(cancelar, rejeitar, matar o processo), cada uma de um tipo diferente; um interpretador de
ações para três chamadas seria abstração sem segundo uso. O custo é que o YAML não mostra
sozinho o que o caso faz, então cada caso com ação tem um comentário apontando a função.

**O caso 8 chama o gateway direto.** O loop emite o token e o gasta no mesmo instante, sem
ponto de interrupção entre os dois, então fazer um token expirar "no meio" pelo loop seria
disputar uma corrida impossível de ganhar de forma determinística. O runner emite um token com
TTL de 1 s, espera ele expirar e chama `tools/gateway.execute` contra uma tarefa realmente
reclamada: é a mesma fronteira verificar-e-executar que o loop usa.

**O caso 7 mata um processo de verdade**, como `test_durability.py`: sobe
`python -m warden.core.worker` e dá `kill`, porque exceção capturada não é crash.

**Isolamento.** O runner usa o banco `warden_evals` por padrão (nunca o `warden_test` da
suíte, que ele derrubaria) e força a URL da API do GitHub para um endereço onde nada escuta:
se a porta de aprovação do caso 3 regredir, nenhum PR real pode ser aberto, mesmo com um PAT
exportado no shell.

## Alternativas consideradas

- **Pular os 3 casos até a funcionalidade existir.** Esconderia a dívida; "12/12" viraria
  "9/9" sem ninguém notar. Rejeitada.
- **Construir manifest, lifecycle e policy com estado agora.** Antecipa a v0.3 e dobra a onda.
  Rejeitada por escopo.
- **Testar o control plane com mocks em vez de tarefas reais.** Mais rápido, mas um eval que
  não passa pelo worker e pelo sandbox de verdade não prova o que o CI promete. Rejeitada.

## Consequências

- O job `behavioral-evals` precisa de Postgres e da imagem do sandbox, como o `backend`.
- "Obrigatório" é configuração de branch protection no GitHub, não algo que o `ci.yml` expresse:
  precisa ser marcado nas configurações do repositório.
- Cada um dos 9 casos foi provado não vazio por mutação (quebrar o comportamento guardado e ver
  o caso falhar), registrado no comentário do caso.
