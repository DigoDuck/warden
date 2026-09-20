# ADR-003: semântica do policy engine

**Status:** aceita · **Data:** 2026-09-19 · **PR:** `feat/policy-engine`

## Contexto

O policy engine é onde a tese do projeto vira código: **o modelo propõe, o control plane
decide**. Ele existe porque a premissa de segurança do Warden é que o modelo **será**
comprometido, por prompt injection no repo, numa issue, numa descrição de tool de MCP ou
numa página da web. Sanitização detecta e é probabilística; a garantia precisa ser
determinística e não pode ler a saída do modelo como instrução.

Um engine de autorização erra de duas formas, e uma é muito pior que a outra. Negar demais
irrita e aparece na hora. Permitir demais em silêncio só aparece no incidente. Toda decisão
abaixo é escolhida para que a falha caia do lado ruidoso.

## Decisão

Cinco regras, e o teste que prova cada uma:

1. **Default deny.** Contexto sem regra que case resulta em `deny`.
   (`test_nothing_matching_means_deny`)
2. **Todas as regras que casam são coletadas**, sem saída antecipada. `matched_rules` traz
   o conjunto inteiro, não só a vencedora, porque é ele que um auditor lê meses depois.
   (`test_every_matching_rule_is_reported_not_just_the_deciding_one`)
3. **O efeito mais restritivo vence:** `deny > require_approval > allow`.
   (`test_deny_beats_allow`, `test_approval_beats_allow`, `test_deny_beats_approval`)
4. **Sem dependência de ordem.** Embaralhar as regras do arquivo não muda decisão nenhuma.
   (`test_rule_order_never_changes_a_decision`, que roda 20 permutações)
5. **Não existe exceção a um deny.** Quando um deny é largo demais, escreve-se o deny mais
   específico. Não há `unless`, nem prioridade, nem ordem que sobreponha.

Mais três decisões de implementação que seguem do mesmo raciocínio:

**Campo desconhecido numa regra falha ao carregar.** Uma regra que referencia
`agent.trustlevel` quando o campo se chama `agent.trust_level` nunca casaria, e o deny que
ela deveria impor simplesmente não existiria. Ninguém descobre isso por observação. O
carregamento quebra com o nome do campo na mensagem.
(`test_a_rule_naming_an_unknown_field_fails_at_load`)

**Glob por segmento, com `PurePosixPath.full_match`, nunca `fnmatch`.** No `fnmatch` o `*`
atravessa `/`, então `src/*` casaria com `src/a/b/secreto.key`. A contrapartida é que
`.env*` **não** pega `config/.env`, e por isso todo deny em `policies/default.yaml` começa
com `**/`, que casa zero ou mais segmentos iniciais.

**`scopes` só vêm das regras que decidiram.** Uma chamada negada que também casou uma regra
de allow não pode receber os escopos dela. Na semana 3 esses escopos viram claims de um JWT
de execução, e vazar escopo de uma decisão perdedora seria conceder o que foi negado.
(`test_scopes_come_only_from_the_rules_that_decided`)

## Alternativas consideradas

**OPA ou Cedar.** Recusadas por ora, pelo motivo do briefing §17: o objetivo é entender
avaliação de política. Depois de ter engine própria com a tabela de casos, ler o modelo do
Cedar passa a fazer sentido, e migrar vira comparação informada em vez de escolha por
default.

**Primeira regra que casa vence, como em firewall.** Recusada: torna a ordem do arquivo
semântica, e um `allow` inserido acima de um `deny` num PR mal revisado abre um buraco sem
mudar nenhuma linha do deny.

**Prioridade numérica por regra.** Recusada pelo mesmo motivo, com o agravante de que a
prioridade certa deixa de ser evidente na leitura de uma regra isolada.

**`require_approval` tratado como `allow` até a semana 3.** Recusada. Enquanto a máquina de
aprovação não existe, o loop degrada `require_approval` para recusa, que é o lado seguro.

## Consequências

- O engine é puro: não executa nada, não lê arquivo, não toca rede, e não interpreta texto
  do modelo. Dá para testá-lo por tabela, e é isso que `tests/policy_cases.yaml` faz com
  36 casos.
- A contenção de path da tool **não** é substituída pela policy, e vice-versa. A policy
  julga o path normalizado; a tool resolve de novo ao abrir. Um symlink dentro do workspace
  apontando para fora chega à policy como `src/app.py` inocente e é barrado pela tool.
  Nenhuma das duas camadas sozinha cobre os dois casos.
- Toda decisão grava `policy_hash`, o sha256 do documento YAML já parseado. Comentário
  reescrito não muda o hash; regra alterada muda. É o que permite responder, meses depois,
  sob quais regras uma ação foi autorizada.
- Quando `risk` e `agent` existirem (semana 11, ADR-015), as regras que dependem deles
  entram sem mudança no engine: são campos novos no contexto e matchers novos, não uma
  semântica nova.
