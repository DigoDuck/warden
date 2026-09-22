# Frontend do Warden

Base de ferramental da Semana 4 (`docs/plano-12-semanas.md`), **sem decisão de design**: telas
simples, sem estilo, só funcionais. Uma skill de design cuida da aparência numa onda futura, por
isso não há paleta, fonte ou kit de componentes aqui.

## Rodar localmente

```bash
npm ci
npm run dev        # http://localhost:5173
npm run typecheck  # tsc --noEmit
npm run lint       # eslint .
npm test -- --run  # vitest
npm run build      # build de produção
```

Requer Node **22.12+** (a máquina de desenvolvimento usa 22.14.0) e o backend em
`../backend` com dependências instaladas (`uv sync --directory backend`): o cliente
tipado é gerado a partir do schema OpenAPI real do backend, não de um arquivo estático.

## Cliente de API tipado (`npm run gen:api`)

`src/api/schema.d.ts` **não é versionado** (veja `.gitignore` na raiz do repo). Ele é gerado
por `scripts/gen-api.mjs`, que roda `warden.api.openapi_export` (novo arquivo do backend, o
único que este track pode adicionar) via `uv` e converte o JSON resultante com
`openapi-typescript`. Esse script roda antes de `dev`, `build`, `test` e `typecheck`
(hooks `pre*` no `package.json`), então o schema nunca fica desatualizado em relação às
rotas de `warden/api/routes_*.py`: se o backend mudar um contrato, o próximo comando já
gera o tipo novo, e um uso incompatível quebra o `tsc --noEmit` na hora.

`src/api/client.ts` é um wrapper fino sobre `fetch`, tipado a partir de `schema.d.ts`: anexa
o bearer token (ver abaixo), e transforma qualquer resposta não 2xx em um `ApiError`
tipado em vez de deixar o chamador checar `response.status` manualmente.

## Autenticação de desenvolvimento

Não existe `/auth/login` ainda (ADR-020). A tela "Configurações" pede que o usuário cole o
token gerado por `make user-token email=... scopes="tasks:write tasks:read audit:read"` no
backend. O token fica em `sessionStorage`, nunca em `localStorage`: é um token de dev colado
à mão, válido só para a aba/janela atual, e não deve sobreviver a um "abrir em nova aba" nem
ser reaproveitado achando que ainda é válido depois de fechado o navegador. Uma resposta 401
de qualquer chamada devolve o usuário a essa tela.

## Por que cada dependência não óbvia

- **`@tailwindcss/vite`** em vez do PostCSS clássico: Tailwind v4 troca a configuração em
  JS por um plugin de build; menos arquivo de config para uma base que ainda não tem tema.
- **`openapi-typescript`**: gera tipos a partir do OpenAPI em vez de escrever `interface`
  manualmente para cada rota, a fonte de verdade fica no backend.
- **`vitest` + `@testing-library/react` + `jsdom`**: mesma escolha do `docs/briefing.md`
  (tabela de stack). `jsdom` fica fixado em `29.x` porque a versão `30` exige uma patch
  de Node mais nova do que a instalada na máquina de dev (`22.14.0`); ver `engines.node`
  do pacote.
- **`typescript` fixado em `5.9.x`**: `typescript-eslint` ainda não suporta a major `7`
  (`typescript: '>=4.8.4 <6.1.0'` no `peerDependencies`).
- **`react`/`react-dom` fixados em `18.3.x`**: a stack do briefing pede React 18, não a
  major mais recente do registry.
- Nenhum kit de UI (shadcn ou outro) foi instalado: decisão deliberada desta onda, ver
  primeira frase deste README.
