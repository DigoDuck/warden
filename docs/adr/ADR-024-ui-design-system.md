# ADR-024: sistema de design da UI, streaming por SSE e polling da lista

**Status:** aceita · **Data:** 2026-09-23 · **PR:** `feat/ui-screens`

## Contexto

`DESIGN.md` (aprovado antes desta PR) é a fonte de verdade visual: tokens de cor, tipografia,
espaçamento, ícones e regras de acessibilidade. Esta PR aplica esses tokens globalmente, monta o
shell de navegação e implementa as três telas que fecham a semana 4 (Tarefas, Detalhe da tarefa,
Aprovações), além de duas rotas novas no backend (`GET /tasks`, `GET /tasks/{id}/stream`) que essas
telas precisam. Registra aqui as decisões que não são óbvias a partir do código.

## Decisão

### Três dependências novas no frontend

- **`lucide-react`**: DESIGN.md fixa Lucide como o conjunto de ícones (traço 1.5px, 16/20px). Uma
  biblioteca de ícones SVG como componentes React em vez de escrever cada `<svg>` à mão: mesma
  lógica de "não reinventar o que já existe" que `@tanstack/react-query` já segue neste repo.
- **`@fontsource/fira-sans`** e **`@fontsource/fira-code`**: fontes self-hosted, não Google Fonts
  (DESIGN.md "Desvios da recomendação" e `frontend/README.md`'s princípio de não fazer requisição
  a terceiro para abrir a tela). Cada pacote publica um `.css` por peso; `src/index.css` importa
  só os pesos que o DESIGN.md usa (400/500/600 de Fira Sans, 400 de Fira Code), não a família
  inteira.

Nenhuma outra dependência: sem kit de componentes (shadcn ou similar) — `frontend/README.md` já
tinha decidido isso deliberadamente, e os tokens do DESIGN.md mais o Tailwind v4 cobrem o que as
três telas desta onda precisam.

### Tokens como `@theme` do Tailwind v4, Preflight de volta

Cada cor do DESIGN.md vira uma variável `--color-*` dentro de um bloco `@theme` em
`src/index.css`; Tailwind gera as utilities (`bg-surface`, `text-fg-muted`, ...) a partir disso, e
todo componente usa essas utilities, nunca uma cor solta. Fonte (`--font-sans`, `--font-mono`) e
raio (o `--radius-md` padrão do Tailwind já é 6px, DESIGN.md pede exatamente isso) seguem o mesmo
caminho. A escala de espaçamento do DESIGN.md (4/8/12/16/24/32/48) já bate com a escala padrão do
Tailwind (múltiplos de 4px), então nenhuma redefinição foi necessária ali.

ADR-023 tinha desligado o Preflight (o reset de CSS do Tailwind) porque, sem classe nenhuma nas
telas, ele deixava campo de formulário invisível. Esta PR liga o Preflight de volta e estiliza
`input`/`textarea`/`select`/`button` no `@layer base` de `src/index.css`, porque agora existe um
tema para esses elementos seguirem.

### Anel de foco: `:focus-visible` nativo, não uma classe por componente

`:focus-visible { outline: 2px solid var(--color-ring); outline-offset: 2px; }` uma vez, no
`@layer base`. Cobre todo elemento focável do app de graça, nunca esquece um componente novo, e é
exatamente o que DESIGN.md pede ("foco sempre visível"). A alternativa (uma utility Tailwind
`focus-visible:ring-2 ...` repetida em cada botão/link/campo) reproduziria a mesma regra dezenas
de vezes e criaria uma chance real de esquecer um componente.

### Foco no `h1` na troca de rota: pulado no primeiro render

`AppShell` guarda uma ref `isFirstRender` e só move o foco para o `h1` da página a partir da
*segunda* mudança de `location.pathname`. DESIGN.md pede foco no `h1` "a cada troca de tela", não
no carregamento inicial; roubar o foco no primeiro render brigaria com o próprio navegador
(posição de scroll restaurada, foco que o usuário já deu à barra de endereço). O `h1` recebe
`tabIndex = -1` por código no momento do foco, não como atributo fixo no JSX: ele nunca deveria
entrar na ordem de Tab normal, só ser um alvo de foco programático.

### Menu mobile: `<details>`/`<summary>` nativo, não um componente de disclosure escrito à mão

Abaixo de 1024px a navegação vira um `<summary>` clicável dentro de um `<details>`. Foco, teclado
(Enter/Espaço abre e fecha) e o estado aberto/fechado vêm do navegador de graça; nenhum
`useState`, nenhum `aria-expanded` escrito à mão, nenhuma chance de dessincronizar o atributo do
estado real. Sem transição de abertura: a regra da semana 4 ("nenhuma animação") faz esse ser o
comportamento certo, não uma limitação aceita.

### Confirmação de cancelamento: `window.confirm`, não um modal próprio

O botão "Cancelar tarefa" (ação destrutiva) usa `window.confirm` antes de chamar
`POST /tasks/{id}/cancel`. É teclado-acessível e acessível a leitor de tela por construção (é um
diálogo do sistema operacional/navegador), e mais barato que desenhar, focar-trap e testar um
modal próprio para uma única confirmação sim/não. `Rejeitar` na fila de aprovação é diferente
(pede uma nota, não um sim/não) e por isso não usa esse padrão.

### `GET /tasks` sem custo/iterações por linha

A listagem devolve `TaskListItemOut` (sem `cost_usd`/`iterations`): calculá-los por linha seria
uma consulta agregada a mais por tarefa (N+1). A tela de detalhe (`GET /tasks/{id}`) já calcula os
dois numa única consulta agregada (`routes_tasks.py::_to_task_out`); a listagem não precisa deles
para nada que a semana 4 pede (link para a tarefa, status, criada em).

### `GET /tasks/{id}/stream`: sessão curta por poll, nunca uma por conexão

O endpoint faz *polling* do banco a cada ~1s, mas cada poll abre e fecha sua própria sessão via
`request.app.state.session_factory`, nunca a `SessionDep` da requisição. ADR-019 já estabeleceu a
regra ("nenhuma transação aberta enquanto o processo espera algo de fora"); uma conexão SSE que um
navegador mantém aberta por minutos é exatamente esse "esperar algo de fora", e segurar uma
transação (ou só uma conexão do pool) por esse tempo bloquearia o worker que precisa escrever
naquela mesma linha de tarefa. Um teste (`test_stream_does_not_hold_a_transaction_open_between_polls`)
prova isso causando a falha, não simulando: um `UPDATE` concorrente na linha da tarefa tem que
terminar rápido com o stream ainda aberto.

O stream honra `Last-Event-ID` (cabeçalho) com `?after=` como alternativa: o navegador não permite
que `EventSource` mande um cabeçalho `Authorization`, então o frontend nunca usa `EventSource` de
verdade — lê a resposta com `fetch` + `ReadableStream` e um parser (`src/lib/sse.ts`) escrito à
mão, pequeno o bastante para não justificar uma dependência. Ele reconecta sozinho mandando
`Last-Event-ID` com o último `seq` recebido quando a conexão cai antes de um evento terminal.

### Por que a lista faz *polling* e o detalhe faz *streaming*

A tela de Tarefas mostra várias tarefas ao mesmo tempo; abrir uma conexão SSE por linha não
escalaria (N conexões por navegador, a maioria ociosa) e a lista não precisa de latência baixa
por evento, só refletir o estado atual a cada poucos segundos — por isso `useInfiniteQuery` com
`refetchInterval`. A tela de Detalhe olha para *uma* tarefa e é exatamente o caso que justifica
uma conexão dedicada: ver "policy.decided" e o resto da timeline aparecer em tempo real é o item
"pronto quando" da semana 4 ("ver eventos chegando ao vivo").

## Alternativas consideradas

- **EventSource nativo para o stream.** Rejeitado: não manda `Authorization`, e o token na URL
  vazaria em log de acesso (o próprio enunciado da tarefa marca isso como proibido).
- **Uma dependência de parsing SSE (`eventsource-parser` ou similar).** O parser necessário é
  pequeno (buffer de linha, ignorar comentário, lembrar `id`) e tem teste próprio
  (`src/lib/sse.test.ts`); não há necessidade real que uma dependência resolvesse melhor que
  ~60 linhas testadas.
- **Modal customizado para "Cancelar tarefa".** Mais código, mais superfície de acessibilidade
  para acertar (foco preso, `Escape` fecha, `aria-modal`), por um ganho visual que DESIGN.md não
  pede.
