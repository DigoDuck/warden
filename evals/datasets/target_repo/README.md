# `target_repo`: testes ocultos para as issues do `examples/target-repo`

Cada issue em `examples/target-repo/issues/NN-slug.md` tem aqui um teste de
aceitação **oculto**: `test_issue_NN.py`. "Oculto" quer dizer isto —
literalmente, física e deliberadamente fora de `examples/target-repo`: o
workspace de um agente é uma cópia só de `examples/target-repo`, então nada
que more aqui em `evals/datasets/` é visível para ele. Isso é o que torna o
teste um critério de aceitação e não uma dica.

Este diretório é o insumo que a semana 6 (`evals/datasets/coding_v1/`, ver
`docs/plano-12-semanas.md`) vai consumir: cada item do dataset de capability
evals aponta para uma issue + o teste oculto correspondente.

## Como cada teste importa o app

Igual aos testes do próprio repo alvo: `from src.app import app`, depois
`TestClient(app)`. Nenhum teste aqui mexe em `sys.path` ou faz truque de
import — quem roda o teste é responsável por deixar `src/` do workspace no
caminho, exatamente como `examples/target-repo/pyproject.toml` já faz por
`[tool.pytest.ini_options] pythonpath = ["."]` quando pytest roda a partir
daquele diretório.

## Como rodar um teste oculto contra um workspace

O ponto chave: **o diretório de onde você invoca o interpretador decide qual
`src/app.py` é importado**, porque `src/` é um pacote de namespace (sem
`__init__.py`) e Python resolve `import src.app` pelo primeiro `src/` que
encontrar em `sys.path` — e ao rodar com `python -m pytest`, o diretório
atual entra em `sys.path` na frente de tudo. Então: entre no diretório do
workspace (a cópia de `examples/target-repo` que o agente editou, ou a
própria `examples/target-repo` para checar a baseline) e rode o teste oculto
por caminho absoluto ou relativo, de lá:

```bash
cd <workspace>          # ex.: examples/target-repo, ou uma cópia dele
uv run pytest ../../evals/datasets/target_repo/test_issue_03.py -q
```

(ajuste o `../../` para a distância real entre `<workspace>` e este
diretório — se o workspace for uma cópia solta em outro lugar, use o
caminho absoluto do teste.)

**Regra dura: um teste oculto por invocação do pytest.** O app de exemplo
guarda seu estado em duas variáveis de módulo (`_WIDGETS`, `_NEXT_ID` —
exatamente o ponto da issue 07). Rodar dois arquivos de teste na mesma
invocação do pytest os faz compartilhar esse estado dentro do mesmo
processo Python, e um widget criado pelo teste da issue 4 vaza para a
contagem que o teste da issue 8 espera. Confirmado na prática enquanto este
dataset era escrito: rodar `test_issue_04.py` e `test_issue_08.py` juntos
fez `average_price_cents` sair `430.0` em vez de `330.0`, só por causa da
ordem de coleta do pytest. Isoladas, cada uma dá o número certo. Um runner
real (semana 6) deve invocar `pytest` uma vez por issue avaliada, nunca o
diretório inteiro de uma vez.

## Evidência red/green

Para cada issue: o teste oculto rodou contra o `examples/target-repo` atual
(sem a implementação — **red**, uma linha de asserção real, nunca
`ImportError`/`AttributeError`) e depois contra uma cópia descartável com
uma solução correta escrita só para gerar esta evidência (**green**, nunca
commitada). Comandos usados, de dentro de cada diretório:

```bash
# red (contra a baseline)
cd examples/target-repo
uv run pytest ../../evals/datasets/target_repo/test_issue_NN.py -q

# green (contra uma cópia descartável com a issue implementada)
cd <cópia descartável de src/+tests/, com o fix só daquela issue>
<mesmo venv de examples/target-repo> -m pytest <caminho absoluto do teste> -q
```

| # | Issue | Red (contra a baseline) | Green (contra a solução) |
|---|---|---|---|
| 01 | Delete a widget | `assert 405 == 204` — sem rota `DELETE`, 3 failed | `3 passed` |
| 02 | Partially update a widget | `assert 405 == 422` — sem rota `PATCH`, 4 failed | `4 passed` |
| 03 | Reject duplicate widget names | `assert 201 == 409` — duplicata aceita, 2 failed, 1 passed | `3 passed` |
| 04 | Reject blank widget names | `assert 201 == 422` — nome só de espaços aceito, 2 failed, 2 passed | `4 passed` |
| 05 | Filter widgets by minimum price | `assert 200 == 422` — `min_price` negativo aceito, 3 failed, 2 passed | `5 passed` |
| 06 | One error shape for every 4xx | `assert isinstance([...], str)` é `False` — `detail` de um 422 é lista, 1 failed, 2 passed | `3 passed` |
| 07 | Widget store reset | `assert 405 == 204` — sem rota `POST /widgets/reset`, 3 failed | `3 passed` |
| 08 | `/widgets/stats` average bug | `assert 330.0 == 165` — retorna a soma, não a média, 2 failed | `2 passed` |
| 09 | `/widgets/search` case bug | `assert 'bolt' in []` — busca sensível a maiúsculas, 2 failed, 2 passed | `4 passed` |
| 10 | Safe request logging | `assert 'GET' in ''` — nenhum log é emitido, 1 failed, 2 passed | `3 passed` |

Em todos os dez, `uv run pytest` (as cinco original de `tests/test_app.py`)
continuou verde tanto na baseline quanto em cada cópia com a correção —
nenhuma issue quebra o que já existia.

## O que não é redundante aqui

O teste da issue 10 é o único que lê `caplog` em vez de só a resposta HTTP:
a propriedade sob teste (o valor de um header nunca vazar para um log) não
é observável pela API pública sozinha. Ele também neutraliza o log próprio
do `httpx`/`httpcore` (que por padrão loga `"HTTP Request: GET ... 200"` e
faria as asserções passarem por acidente, contra o app errado) antes de
inspecionar `caplog` — sem isso, os três testes passavam mesmo contra a
baseline sem nenhuma linha de log própria do serviço.
