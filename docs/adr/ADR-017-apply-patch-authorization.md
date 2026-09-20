# ADR-017: autorização de `apply_patch` por path, sem parsear o diff

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `feat/write-tools`

## Contexto

O policy engine (ADR-003) julga um `PolicyContext` com **um** path. `write_file(path, content)`
tem um path e cabe direto nesse modelo. `apply_patch(diff)` não: um diff toca N arquivos, pode
renomear, criar, editar e apagar, tudo numa chamada só.

A tentação óbvia é o control plane parsear o diff e extrair os paths antes de decidir. É
exatamente essa tentação que esta ADR recusa. Um diff em formato unificado tem ambiguidade real
de parsing: o prefixo `a/`/`b/` pode variar (`-p<n>`), um rename troca o nome do arquivo entre o
lado antigo e o novo, um path pode conter espaço, e nada garante que o parser do control plane
interpreta essas formas do mesmo jeito que o `git apply` que efetivamente vai aplicar o patch.
Numa camada de autorização, uma diferença entre o que o parser lê e o que o `git apply` faz **é**
um bypass: a policy julga um path, o git escreve outro.

## Decisão

**Quem aplica é quem informa.** O git, não um parser próprio, diz quais paths um diff toca, e a
policy julga a partir da resposta do git, nunca da leitura do texto do diff.

Fluxo em `core/loop.py` (`_decide`), disparado em `tool.requested`:

1. O diff é staged em `/sandbox/.warden/` (o mesmo mecanismo de staging que `write_file` já usa).
2. `git apply --numstat -z` roda dentro do container, **somente leitura**: não aplica nada, só
   reporta o que aplicaria.
3. A policy é avaliada uma vez por path reportado.
4. `combine()` (novo, em `policy/engine.py`) funde as decisões: efeito mais restritivo vence,
   `matched_rules` é a união ordenada, e `scopes` só vêm de volta se **toda** decisão foi allow.
5. Só se tudo deu allow: `git apply --check`, depois `git apply`, com `cwd` no workspace.

**Trade-off aceito, e proposital:** a inspeção roda um comando com input do modelo (o diff)
dentro do sandbox **antes** de a decisão de policy existir. O comando é fixo
(`git apply --numstat -z`), somente leitura, dentro de um container sem rede e sem capabilities.
A alternativa, decidir sem saber o que um patch multi-arquivo toca, é pior: ou todo `apply_patch`
é negado por princípio, ou a policy passa a julgar argumento em vez de path.

**Se a inspeção falha** (patch malformado, `git apply --numstat -z` não consegue nem parsear), o
resultado é uma decisão `deny` sintetizada, sem regra, com o motivo "the paths this call would
touch could not be determined". Não saber o que uma chamada tocaria é motivo para recusar, não
para adivinhar. Fica registrada como qualquer outra decisão (`policy.decided`, `tool_calls` com
`decision=deny`, `policy_decisions`), só que sem path e sem regra por trás.

## O que foi verificado no sandbox, e por quê

`git apply --numstat -z` **não é** o `git diff --numstat -z` de que a documentação (e a memória)
falam. Testado num container `warden-sandbox:dev` descartável antes de escrever qualquer linha de
parser, porque a alternativa era confiar em documentação para uma decisão de autorização:

1. **Layout confirmado, byte a byte:** um registro por arquivo tocado, formato
   `<added>\t<deleted>\t<path>`, terminado em NUL. Sem separador extra depois do último NUL.
   `1\t1\tsrc/a.py\0` para uma edição, `1\t0\tsrc/new.py\0` para criação, `0\t1\tsrc/c.py\0` para
   remoção.

2. **A descoberta que mudou o parser: um rename só reporta o destino.** `git apply --numstat -z`
   numa renomeação pura devolve `0\t0\tsrc/b_renamed.py\0`, **sem o path de origem**. Isso é
   diferente de `git diff --cached --numstat -z` num rename real dentro de um repositório, que
   devolve os dois: `0\t0\t\0src/b.py\0src/b_renamed.py\0`. A documentação que existe na cabeça de
   quem já usou `git diff --numstat -z` não se aplica a `git apply --numstat -z`; são comandos
   diferentes com formatos de saída diferentes para o mesmo caso.

3. **De onde vem o path de origem, então.** O cabeçalho estendido do próprio diff sempre traz
   `rename from X` seguido de `rename to Y`, cada um como linha inteira, sem o prefixo `a/`/`b/`
   que torna a linha `diff --git a/X b/Y` ambígua. A pergunta que importava era: será que essas
   duas linhas são só decoração, e o `git apply` na verdade decide o rename por outra coisa (por
   exemplo a linha `diff --git`)? Teste decisivo: um patch com `diff --git a/decoy_old.py
   b/decoy_new.py` mas `rename from real_source.py` / `rename to real_dest.py` (nomes que não
   batem com a linha `diff --git`). Resultado: `--numstat -z` reporta `real_dest.py` (o valor de
   `rename to`), e o `apply` de verdade renomeia `real_source.py` para `real_dest.py`, ignorando
   `decoy_old.py`/`decoy_new.py` por completo. Ou seja, `rename from`/`rename to` não é uma leitura
   alternativa arriscada: é **exatamente** o que o `git apply` usa, e a linha `diff --git` é
   decorativa para esse efeito. Ler essas duas linhas não reintroduz o risco de parser
   differential que esta ADR existe para evitar, porque não há duas interpretações possíveis: o
   `git apply` não lê mais nada além delas para decidir o rename.
   Por segurança adicional, um par `(origem, destino)` só é aceito quando o destino também
   aparece na saída do `--numstat -z`, que é a única coisa aqui que o git de fato computou (em vez
   de apenas ecoar). Implementado em `tools/sandboxed.py::_touched_paths`.

4. **`git apply` recusa path fora do workspace por conta própria.** Um patch com `../outside.py`
   é recusado tanto em `--check` quanto no apply de verdade: `error: invalid path
   '../outside.py'`, sem precisar de `--unsafe-paths`. E isso funciona mesmo o workspace **não**
   sendo um repositório git (o `.git` não é copiado para o container), o que também foi verificado
   e não presumido.

5. **`git apply` recusa escrever através de um symlink que sai do workspace.** Com
   `src/linked -> /tmp/outside_target` já existente, um diff criando `src/linked/through.py` é
   recusado com `error: affected file 'linked/through.py' is beyond a symbolic link`, em
   `--check` e no apply real, sem nada criado em `/tmp/outside_target`. Como o item 6 do pedido
   original previa: **se** o git não recusasse, `apply_patch` precisaria da mesma checagem de
   `realpath` que `write_file` já tem. Ele recusa, então `apply_patch` não duplica esse probe; a
   contenção aqui é do próprio git, testada em `test_apply_patch.py`, não assumida.

6. **`--numstat -z` não valida, só faz parsing.** Um patch cujo contexto não bate com o conteúdo
   real do arquivo ainda é reportado normalmente por `--numstat -z` (exit 0): ele não abre os
   arquivos do workspace, só lê o texto do patch. Só o `--check` (ou o apply de verdade) detecta a
   incompatibilidade de contexto. Consequência direta: a policy pode autorizar um `apply_patch`
   que depois falha como `ToolError` no `--check`, sem nunca tocar o workspace. É esperado, não é
   um furo: autorização é sobre *quais paths* uma chamada tocaria, não sobre se o patch aplica de
   fato.

7. **Patch malformado falha já no `--numstat -z`** (`exit 128`, "No valid patches in input"), o
   que é o sinal que vira `ToolError` na inspeção e, no loop, o deny sintetizado descrito acima.

## Alternativas consideradas

**Parsear `diff --git a/X b/Y` e as linhas `---`/`+++` no control plane.** Recusada: é
exatamente o parser differential que esta ADR evita, e o item 3 acima mostra um caso concreto em
que a linha `diff --git` mente sobre o que o git realmente faz.

**Aplicar de verdade num scratch antes de decidir, e usar `git diff --numstat -z` (que reporta os
dois paths de um rename) para descobrir o que mudou.** Resolveria a lacuna do item 2 sem tocar no
texto do patch, mas custa uma cópia inteira do workspace e um `git init` por chamada só para
inspecionar, antes de saber se a chamada é sequer permitida. Mais caro e mais código para o mesmo
resultado que ler duas linhas de cabeçalho já dá, com a mesma garantia (item 3).

**Negar `apply_patch` sempre que a chamada tocar mais de um path.** Elimina a necessidade de
`combine()`, mas devolve exatamente o problema que motivou este tool: um refactor de um rename
com edição, ou uma correção que toca dois arquivos relacionados, vira duas chamadas artificiais
em vez de uma, e a política de "múltiplos paths, uma decisão" que este design entrega de graça.

**`require_approval` como padrão para qualquer `apply_patch` multi-path.** Recusada pelo mesmo
motivo do ADR-003: sem a máquina de aprovação (semana 3), isso degrada para recusa de qualquer
jeito, então é a mesma coisa que negar sempre, só com um nome mais bonito.

## Consequências

- `policy/engine.py::combine()` é função pura, testada por tabela como `Policy.evaluate` já é.
  `scopes` só saem se **toda** decisão for allow, porque um patch parcialmente negado não pode
  vazar o `repo:write` de metade dos arquivos que teriam sido permitidos.
- `tools/registry.py` ganha `path_inspector`, ortogonal a `path_arg`: um tool declara um dos
  dois (ou nenhum). O loop nunca conhece `apply_patch` pelo nome; ele só chama
  `registry.touched_paths()` e trata o resultado de forma genérica.
- O evento `policy.decided` ganha a chave `paths`, a lista de paths julgados nesta chamada. Toda
  chave existente continua no payload; `core/replay.py` não lê `policy.decided`, então o replay
  não é afetado por essa adição.
- `apply_patch` não tem `path_arg`: sua contenção depende inteiramente do git (itens 4 e 5) mais
  da policy julgando cada path relatado. Não há probe de `realpath` próprio, porque não sobrou
  nada para ele capturar que o git já não recuse primeiro.
- Descoberta reaproveitável: qualquer ferramenta futura que precise saber "o que este diff toca"
  antes de decidir algo sobre ele deve usar `git apply --numstat -z` mais o par `rename
  from`/`rename to`, não `git diff`, porque o alvo não é um repositório real.
