# Unidades públicas nas rotas de agendamento — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer as duas rotas consumidas pela G4 devolverem o mesmo nome público com endereço para as Unidades Diagnósticas 01 e 02.

**Architecture:** Um módulo puro centralizará o mapeamento por `cd_unidade_atendimento`. O endpoint de confirmações continuará usando o mesmo campo já publicado; a rota de agendamentos do paciente passará a buscar o código apenas internamente, normalizará o nome durante a deduplicação e manterá o contrato público atual.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Pytest, Oracle/MV, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-03-unidades-publicas-rotas-agendamento-design.md`

## Global Constraints

- Não alterar cadastros no MV.
- Preservar `ds_unidade_atendimento` como o campo público já consumido pela G4.
- Unidade 1: `Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart - Nº 560 - Meireles`.
- Unidade 2: `Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart - Nº 600 - Meireles`.
- Preservar nomes desconhecidos e valores nulos.
- Não alterar recibos, telas ou rotas fora dos dois contratos descritos na especificação.
- Publicar imagem imutável, validar saúde e resposta externa, e manter rollback.

## Review Focus

- Código numérico ou texto com espaços deve produzir o mesmo texto público; Task 1 testa ambos.
- Unidade desconhecida deve preservar exatamente o nome original; Task 1 testa esse caso.
- Código/nome nulos não devem gerar endereço inventado; Task 1 testa esse caso.
- Linhas duplicadas do mesmo agendamento devem continuar escolhendo o maior `item_movimento_id`; Task 2 testa deduplicação e contagem.
- O código incluído apenas para normalização não deve aparecer como novo campo na rota do paciente; Task 2 testa compatibilidade do contrato.

---

### Task 1: Centralizar o nome público das unidades

**Files:**
- Create: `app_prontocardio/unidades.py`
- Modify: `app_prontocardio/routers/operacional.py:31-84`
- Create: `tests/test_unidades.py`
- Test: `tests/routers/test_operacional_confirmacoes.py`

**Interfaces:**
- Consumes: `codigo: object` e `nome: str | None` obtidos das consultas Oracle.
- Produces: `nome_unidade_publica(codigo: object, nome: str | None) -> str | None`.

- [ ] **Step 1: Escrever os testes falhos do módulo compartilhado**

Criar testes parametrizados para códigos `1`, `' 1 '`, `2`, `'2'`, código `3`, código nulo e nome nulo, afirmando os textos exatos da especificação e a preservação dos demais valores.

- [ ] **Step 2: Executar os testes e confirmar a falha**

Run: `python -m pytest tests/test_unidades.py -q`

Expected: FAIL porque `app_prontocardio.unidades` ainda não existe.

- [ ] **Step 3: Implementar o módulo puro**

Criar `nome_unidade_publica(codigo: object, nome: str | None) -> str | None` e a constante privada de mapeamento em `app_prontocardio/unidades.py`.

- [ ] **Step 4: Migrar o endpoint de confirmações para o módulo compartilhado**

Remover a constante e a função duplicadas de `operacional.py`, importar `nome_unidade_publica` e manter `montar_resposta_confirmacoes(rows)` com o mesmo contrato.

- [ ] **Step 5: Executar os testes focados**

Run: `python -m pytest tests/test_unidades.py tests/routers/test_operacional_confirmacoes.py -q`

Expected: PASS, sem alteração de contagens nem mutação das linhas de entrada.

- [ ] **Step 6: Commit**

```bash
git add app_prontocardio/unidades.py app_prontocardio/routers/operacional.py tests/test_unidades.py tests/routers/test_operacional_confirmacoes.py
git commit -m "refactor: centraliza nomes publicos das unidades"
```

### Task 2: Normalizar a rota de agendamentos do paciente

**Files:**
- Modify: `app_prontocardio/routers/agendamentos.py:2193-2235,4053-4089`
- Create: `tests/routers/test_agendamentos_paciente_unidades.py`

**Interfaces:**
- Consumes: `nome_unidade_publica(codigo: object, nome: str | None) -> str | None` da Task 1 e linhas Oracle com `cd_unidade_atendimento`.
- Produces: `montar_resposta_agendamentos_paciente(rows) -> dict[str, object]`, preservando `{'agendamentos': list, 'total': int}`.

- [ ] **Step 1: Escrever o teste falho da montagem da resposta**

Criar `test_montar_resposta_agendamentos_paciente_normaliza_unidade_e_preserva_deduplicacao`, com duas versões do mesmo `cd_it_agenda_central`, afirmando que vence o maior `item_movimento_id`, o texto da unidade 2 contém o endereço, `total == 1` e `cd_unidade_atendimento` não aparece no item público.

Adicionar casos para unidade 1, unidade desconhecida e nome nulo.

- [ ] **Step 2: Executar o teste e confirmar a falha**

Run: `python -m pytest tests/routers/test_agendamentos_paciente_unidades.py -q`

Expected: FAIL porque o helper ainda não existe ou o nome permanece bruto.

- [ ] **Step 3: Disponibilizar o código da unidade na consulta interna**

Adicionar `ac.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento` a `CONSULTA_AGENDAMENTOS_PACIENTE`, sem adicionar o campo ao modelo `AgendamentoPaciente`.

- [ ] **Step 4: Extrair e usar a montagem pura da resposta**

Implementar `montar_resposta_agendamentos_paciente(rows) -> dict[str, object]`: copiar cada linha, normalizar `ds_unidade_atendimento`, remover `cd_unidade_atendimento` da cópia pública, preservar a regra atual de deduplicação e calcular o total. Fazer `consultar_agendamentos_paciente` delegar ao helper.

- [ ] **Step 5: Executar os testes das duas rotas**

Run: `python -m pytest tests/test_unidades.py tests/routers/test_operacional_confirmacoes.py tests/routers/test_agendamentos_paciente_unidades.py -q`

Expected: PASS.

- [ ] **Step 6: Executar validações estáticas e a suíte completa**

Run: `python -m py_compile app_prontocardio/unidades.py app_prontocardio/routers/operacional.py app_prontocardio/routers/agendamentos.py`

Run: `python -m ruff check app_prontocardio/unidades.py tests/test_unidades.py tests/routers/test_agendamentos_paciente_unidades.py`

Run: `python -m pytest -q`

Expected: nenhum novo erro em relação à linha de base conhecida; registrar separadamente qualquer falha preexistente e não relacionada.

- [ ] **Step 7: Commit**

```bash
git add app_prontocardio/routers/agendamentos.py tests/routers/test_agendamentos_paciente_unidades.py
git commit -m "fix: inclui endereco na unidade dos agendamentos"
```

### Task 3: Publicar e validar em produção

**Files:**
- Modify: `docs/superpowers/specs/2026-11-03-unidades-publicas-rotas-agendamento-design.md`
- Modify: `docs/superpowers/plans/2026-11-03-unidades-publicas-rotas-agendamento.md`

**Interfaces:**
- Consumes: commits testados das Tasks 1 e 2.
- Produces: revisão canônica, imagem imutável, Compose de implantação e rollback.

- [ ] **Step 1: Revisar o diff completo contra a especificação**

Run: `git diff --check <base>..HEAD && git diff --stat <base>..HEAD`

Expected: somente módulo compartilhado, duas rotas, testes e documentação do escopo.

- [ ] **Step 2: Preparar a revisão canônica e a imagem imutável**

Criar a revisão a partir do `main` canônico vigente, confirmar que a árvore publicada corresponde à árvore testada e construir `api_prontocardio:<revision>` sem alterar o contêiner ativo durante o build.

- [ ] **Step 3: Implantar com rollback**

Registrar imagem/Compose anteriores, trocar para a nova imagem somente após build concluído e manter `rollback.compose.yml` apontando para a versão anterior.

- [ ] **Step 4: Verificar saúde e contrato externo**

Validar contêiner saudável, OpenAPI e ausência de erros recentes. Com a credencial de integração, consultar:

`GET /agendamentos/pacientes/137703/agendamentos`

Expected: `ds_unidade_atendimento` igual ao texto completo da Unidade Diagnóstica 02, sem novo campo público obrigatório.

Consultar também `GET /operacional/agendamentos/confirmacoes` em uma data com unidades 1 e 2 e confirmar os mesmos textos públicos.

- [ ] **Step 5: Registrar evidências e revisão publicada**

Documentar revisão, imagem, hash dos módulos, saúde, resultado dos dois smokes e caminho do rollback; publicar o commit no `main` canônico somente após todas as verificações.
