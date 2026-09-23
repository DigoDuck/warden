# ADR-023: base do frontend sem decisão visual

**Status:** aceita · **Data:** 2026-09-22 · **PR:** `chore/frontend-scaffold`

## Contexto

A semana 4 pede um frontend para operar o Warden sem `curl`. O projeto ainda não tem
identidade visual (nenhum `DESIGN.md`, nenhum token de design), e escolher paleta, fonte e
componentes agora, junto com o ferramental, misturaria duas decisões de natureza diferente
num PR só. Este PR entrega só a base: Vite, React 18, TypeScript estrito, TanStack Query,
React Router, Tailwind, ESLint, Vitest, o cliente tipado da API e três telas cruas
(Submeter tarefa, Detalhe da tarefa, Configurações). A estética entra numa onda própria,
com uma skill de design.

Três escolhas deste PR não são óbvias e ficam registradas aqui.

## Decisão

**1. Os tipos da API são gerados, nunca versionados.** `backend/warden/api/openapi_export.py`
monta o app com um par de chaves RSA efêmero e uma session factory fictícia (sem banco, sem
arquivo de chave, sem rede) e imprime o OpenAPI. `frontend/scripts/gen-api.mjs` passa isso
pelo `openapi-typescript` e grava `src/api/schema.d.ts`, que está no `.gitignore`. Os hooks
`predev`, `prebuild`, `pretest` e `typecheck` geram o arquivo antes de rodar. Consequência
desejada: uma mudança em `warden/api/routes_*.py` que quebra o contrato quebra o `tsc` do
frontend no mesmo CI, em vez de ficar escondida atrás de um arquivo gerado velho.

**2. O navegador só fala com a própria origem.** O backend não tem middleware de CORS, e não
ganha um. Em desenvolvimento o Vite faz proxy de `/api` para `127.0.0.1:8000` (IPv4
explícito: o uvicorn do `make api` escuta só IPv4, e `localhost` pode resolver para `::1`
primeiro). Em produção o Caddy da semana 12 faz o mesmo papel. Assim o token bearer nunca
cruza origens e não existe lista de origens permitidas para manter.

**3. Tailwind sem o Preflight.** O reset do Tailwind v4 zera borda e fundo de `input`,
`textarea` e `button`. Sem classes nas páginas ainda, os campos viravam caixas invisíveis.
Só as camadas `theme` e `utilities` são importadas; a onda de design decide se o reset volta,
junto com as classes que redesenham os campos.

**Token de desenvolvimento em `sessionStorage`.** O token colado na tela Configurações vive
só na aba em que foi colado. Isso não é proteção contra XSS, porque `sessionStorage` é tão
legível por script quanto `localStorage`. O que limita o estrago de um token vazado é o TTL
curto do `make user-token` (1 hora) e a revogação por `jti` (ADR-005). O login de verdade,
com cookie `HttpOnly`, é outra decisão, para quando `/auth/login` existir.

## Alternativas consideradas

- **Versionar `schema.d.ts` e checar no CI com `git diff --exit-code`.** Funciona, mas obriga
  todo PR de backend que toca uma rota a regenerar e commitar um arquivo do frontend, e cria
  conflito entre trilhas paralelas que mexem em rotas diferentes. Rejeitada.
- **CORS no FastAPI para `localhost:5173`.** Resolve o desenvolvimento, mas vira configuração
  de segurança que precisa estar certa em produção também. O proxy some com o problema nos
  dois ambientes. Rejeitada.
- **Manter o Preflight e estilizar os campos agora.** É exatamente a decisão visual que este
  PR evita tomar. Rejeitada.

## Consequências

- Rodar o frontend exige o backend Python instalado (`uv sync`), porque a geração dos tipos
  importa o app. O job `frontend` do CI faz `uv sync` por isso. Num monorepo isso é aceitável.
- Não há teste de navegador real nesta semana. O caminho do proxy foi testado com `curl`
  contra o `create_app`; o Playwright entra depois, em poucos fluxos (briefing §11).
- Pendências conhecidas, sem impacto de segurança: o TanStack Query repete 3 vezes em erro
  4xx, então um 404 mostra "Carregando" por uns 7 segundos antes do estado de erro. O id
  colado na Home não é codificado na rota do navegador.
