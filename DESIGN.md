# DESIGN.md — Warden

Fonte de verdade visual do frontend. Toda tela lê daqui; nenhum componente usa cor, fonte ou
espaçamento fora destes tokens. Gerado com a skill `ui-ux-pro-max` (estilo *Data-Dense
Dashboard*) e ajustado onde a recomendação falhava em acessibilidade (ver "Desvios da
recomendação").

## Princípios

1. **Operação, não vitrine.** Quem usa o Warden está decidindo se um agente pode agir. A tela
   prioriza estado, decisão e evidência. Nada decorativo.
2. **Denso, mas escaneável.** Tabelas e timelines com muita informação por linha, hierarquia
   por peso e cor semântica, nunca por tamanho de caixa.
3. **Cor nunca sozinha.** Todo estado (status de tarefa, efeito de policy) tem cor **e** ícone
   **e** texto. Daltonismo e leitor de tela leem a mesma coisa.
4. **Teclado primeiro.** Todo fluxo das telas principais funciona sem mouse, com foco sempre
   visível.
5. **Sem animação nesta fase.** O plano da semana 4 proíbe animar. Mudança de estado é
   instantânea; `prefers-reduced-motion` fica respeitado de graça.

## Tema

Escuro como padrão e único tema por enquanto. Um modo claro entra só se houver pedido: os tokens
são semânticos, então ele vira um segundo bloco de variáveis, sem tocar em componente.

### Superfícies e texto

| Token | Hex | Uso | Contraste |
|---|---|---|---|
| `--bg` | `#0F172A` | fundo da página | — |
| `--surface` | `#1E293B` | cards, tabela, painéis | — |
| `--surface-raised` | `#272F42` | linha em hover, cabeçalho de tabela, popover | — |
| `--fg` | `#F8FAFC` | texto principal | 17,1 no bg · 14,0 no surface |
| `--fg-muted` | `#94A3B8` | texto secundário, rótulos, timestamps | 7,0 no bg · 5,7 no surface |
| `--border-control` | `#71809A` | borda de input, select, botão secundário | 4,5 no bg · 3,7 no surface · 3,3 no raised |
| `--border-subtle` | `#334155` | divisórias decorativas entre linhas | decorativo (sem mínimo) |
| `--ring` | `#38BDF8` | anel de foco, 2px + offset 2px | 8,3 no bg |

### Ação

| Token | Hex | Uso |
|---|---|---|
| `--accent` | `#22C55E` | ação primária (Submeter, Aprovar). Texto sobre ele: `--bg` (7,8:1) |
| `--danger` | `#F87171` | texto e ícone de erro, Rejeitar, Cancelar (5,3 no surface) |
| `--danger-solid` | `#DC2626` | fundo do botão destrutivo, com texto `--fg` (4,6:1; o botão contra o surface: 3,0:1) |

Uma ação primária por tela. Ações destrutivas (Cancelar tarefa, Rejeitar) ficam separadas da
primária e pedem confirmação.

### Semântica do domínio

Efeito de policy, o coração da tese "o modelo propõe, o control plane decide":

| Efeito | Cor | Ícone (Lucide) | Texto |
|---|---|---|---|
| allow | `--ok` `#4ADE80` | `check-circle` | Permitido |
| deny | `--danger` `#F87171` | `shield-x` | Negado |
| require_approval | `--warn` `#F59E0B` | `hand` | Aguarda aprovação |

Status de tarefa:

| Status | Cor | Ícone | Texto |
|---|---|---|---|
| QUEUED | `--fg-muted` | `clock` | Na fila |
| RUNNING | `--info` `#38BDF8` | `loader` (estático) | Executando |
| WAITING_APPROVAL | `--warn` | `hand` | Aguardando aprovação |
| SUCCEEDED | `--ok` | `check-circle` | Concluída |
| FAILED | `--danger` | `x-circle` | Falhou |
| CANCELLED | `--fg-muted` | `ban` | Cancelada |
| TIMED_OUT | `--caution` `#FB923C` | `timer-off` | Tempo esgotado |
| BUDGET_EXCEEDED | `--caution` | `wallet` | Orçamento excedido |

Todas as cores de status passam de 4,5:1 (AA) sobre `--surface` e `--surface-raised`; a mais
justa é `--danger` no raised, com 4,8:1. O componente
`StatusBadge` é o único lugar que traduz status em cor/ícone/texto.

## Tipografia

| Papel | Fonte | Tamanho / peso |
|---|---|---|
| Título de página (h1) | Fira Sans | 24px / 600 |
| Título de seção (h2) | Fira Sans | 18px / 600 |
| Corpo e formulários | Fira Sans | 16px / 400, line-height 1.5 |
| Tabela e timeline | Fira Sans | 14px / 400, line-height 1.4 |
| Rótulo, legenda | Fira Sans | 13px / 500 |
| Dados técnicos: ids, paths, nomes de tool, regras, JSON, diffs | Fira Code | 13px / 400 |

- Números (custo, contagem, duração) com `font-variant-numeric: tabular-nums`.
- Nada abaixo de 12px.
- Fontes **self-hosted** via `@fontsource/fira-sans` e `@fontsource/fira-code`, sem Google
  Fonts: um control plane de segurança não faz requisição a terceiro para abrir a tela, e a CSP
  fica `font-src 'self'`.

## Espaço, forma e densidade

- Escala de 4px: 4, 8, 12, 16, 24, 32, 48.
- Raio único de 6px; badges com raio de 9999px.
- Sem sombra. Profundidade por camada de superfície (`bg` → `surface` → `surface-raised`) e
  borda.
- Linha de tabela com 40px de altura; controles com no mínimo 32px de altura e área de clique
  de 24×24px ou mais (WCAG 2.2, 2.5.8). É uma ferramenta de desktop, então não usamos o alvo de
  44px de app mobile.
- Largura máxima do conteúdo: 1280px. Texto corrido limitado a ~72 caracteres.

## Layout e navegação

- ≥1024px: barra lateral fixa com os destinos principais (Tarefas, Nova tarefa, Aprovações,
  Configurações), com ícone e texto, e o item atual destacado por cor e barra à esquerda.
- <1024px: a barra vira um cabeçalho com menu. Nunca rolagem horizontal da página; tabela
  larga rola dentro do próprio contêiner.
- Toda tela tem URL própria (deep link). O contador de aprovações pendentes aparece como badge
  no item Aprovações.
- Link "Pular para o conteúdo" como primeiro elemento focável; troca de rota move o foco para
  o `h1`.

## Estados obrigatórios

Toda lista e todo painel de dados tem os quatro: **carregando** (esqueleto do tamanho final,
sem pular layout), **vazio** (frase + ação, ex.: "Nenhuma tarefa ainda. Submeter tarefa"),
**erro** (causa + "Tentar de novo") e **com dados**. Botões ficam desabilitados com rótulo de
progresso enquanto a requisição roda.

## Ícones

Lucide (`lucide-react`), traço de 1.5px, tamanhos 16px (inline) e 20px (navegação). Nenhum
emoji como ícone. Ícone sozinho, sem texto, só com `aria-label`.

## Desvios da recomendação da skill

| Recomendado | Adotado | Motivo |
|---|---|---|
| `--ring` `#1E293B` | `#38BDF8` | contraste 1,22:1 contra o fundo: o foco ficaria invisível |
| borda `#475569` | `#71809A` para controles | 1,93:1 sobre o card; controles exigem 3:1 (WCAG 1.4.11) |
| títulos em Fira Code | Fira Sans; Fira Code só em dado técnico | monoespaçada em título pesa e compete com o dado que ela deveria destacar |
| hover com transição suave | sem transição | plano da semana 4: nada animado |
| destrutivo `#EF4444` com texto branco | `#DC2626` | 3,6:1 no texto do botão, abaixo do AA |
| Google Fonts | `@fontsource` | sem requisição a terceiro; CSP restrita |
