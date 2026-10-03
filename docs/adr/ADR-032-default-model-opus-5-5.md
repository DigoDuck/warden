# ADR-032: modelo padrão Claude Opus 5.5, com esforço explícito

**Status:** aceita · **Data:** 2026-10-03 · **PR:** `chore/opus-5-5`

## Contexto

O adaptador da Anthropic usava `claude-opus-5`. O Claude Opus 5.5 é o Opus atual: mesmo contexto
(1M), mesma saída máxima, mesmo tokenizer, e mais barato (US$ 4 / 20 por milhão de tokens contra
5 / 25; leitura de cache a US$ 0,20). A rodada real do `coding_v1` e a tag `v0.1.0` vão publicar
custo por tarefa, e esse número deve sair com o modelo atual.

Trocar só o nome tem duas armadilhas, ambas silenciosas:

1. **O esforço padrão caiu.** No Opus 5.5 o default de `effort` é `medium`, um nível abaixo do
   `high` do Opus 5. Sem enviar `effort`, coder, planner e reviewer ficariam mais rasos sem
   nenhum erro.
2. **O preço da leitura de cache não é 10% da entrada.** É 5% (US$ 0,20 sobre US$ 4). O
   multiplicador fixo de `pricing.py` publicaria o dobro do custo real numa rodada com cache.

## Decisão

- `DEFAULT_MODEL = "claude-opus-5-5"`.
- **`output_config.effort = "high"` em toda chamada**, nunca deixado ao default do modelo. `high`
  é o mínimo documentado para trabalho sensível a qualidade. Esforço mais baixo por chamada
  (planner barato, reviewer barato) é decisão do roteamento da semana 8, medida, não default.
- `thinking` continua sem ser enviado: o pensamento adaptativo fica ligado, e no Opus 5.5 um
  `disabled` explícito é 400.
- `ModelPrice` ganha `cache_read_per_mtok` opcional; o Opus 5.5 usa o preço publicado. A linha do
  `claude-opus-5` fica: chamadas já gravadas foram precificadas com ela.

## Fora desta decisão

- **Fallback de recusa no servidor** (`fallbacks: "default"`, beta). A referência da API
  recomenda ligar por padrão no Opus 5.5. Fica para decisão separada porque muda a chamada para o
  endpoint beta e porque a resposta pode vir de outro modelo: hoje o custo é calculado pelo
  modelo da resposta, e um modelo sem preço levanta `UnknownModelError` no meio da tarefa. Sem o
  fallback, uma recusa chega ao loop como `stop_reason: refusal` (já tratado, ADR-016).
- Nada foi rodado contra a API real: a chave ainda não existe neste ambiente.
