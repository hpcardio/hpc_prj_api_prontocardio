# Unidades com endereço nas confirmações Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expor nome padronizado e endereço das unidades 1 e 2 no endpoint de confirmações, sem alterar o MV ou outras unidades.

**Architecture:** A transformação ocorrerá na montagem da resposta de `GET /operacional/agendamentos/confirmacoes`. Uma função pura mapeará o código da unidade para o texto público e devolverá o nome original para qualquer código não mapeado.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-31-unidades-confirmacoes-endereco-design.md`

## Global Constraints

- Não alterar dados em `DBAMV.UNIDADE_ATENDIMENTO`.
- Não alterar filtros, contagens ou outros campos do endpoint.
- Não alterar outras rotas de agendamento.
- Usar exatamente os textos públicos definidos na especificação.

## Review Focus

- Código Oracle numérico ou convertido em string deve produzir o mesmo rótulo.
- Unidade não mapeada deve preservar o nome original sem normalização adicional.
- Nome nulo de unidade não mapeada deve continuar nulo.
- A transformação não pode alterar o objeto de entrada fornecido pelo driver.
- `total`, `total_agendamentos` e `total_pacientes` devem manter a semântica atual.

---

### Task 1: Normalização das unidades no contrato de confirmações

**Files:**
- Modify: `app_prontocardio/routers/operacional.py:60-75`
- Create: `tests/routers/test_operacional_confirmacoes.py`

**Interfaces:**
- Consumes: linhas mapeadas pelo SQLAlchemy contendo `cd_unidade_atendimento` e `ds_unidade_atendimento`.
- Produces: `_nome_unidade_confirmacao(codigo: object, nome: str | None) -> str | None` e resposta de `montar_resposta_confirmacoes(rows)` com o texto público no campo existente.

- [ ] **Step 1: Escrever testes inicialmente falhos**

Criar `test_formata_unidades_diagnosticas_com_endereco`, parametrizado com códigos `1`, `'1'`, `2` e `'2'`, afirmando os textos exatos da especificação. Criar `test_preserva_unidade_nao_mapeada_e_nome_nulo`, afirmando que código `3` mantém `OUTRA UNIDADE` e que código `None` mantém nome nulo. Criar `test_montar_resposta_preserva_contagens_e_nao_muta_linhas`, com três linhas e dois pacientes, afirmando `total == total_agendamentos == 3`, `total_pacientes == 2` e que as linhas de entrada permanecem iguais às cópias anteriores à chamada.

- [ ] **Step 2: Executar os testes e confirmar a falha esperada**

Run: `python -m pytest tests/routers/test_operacional_confirmacoes.py -q`

Expected: FAIL porque a resposta ainda contém os nomes brutos do MV.

- [ ] **Step 3: Implementar a transformação mínima**

Adicionar `_nome_unidade_confirmacao(codigo: object, nome: str | None) -> str | None` em `operacional.py` e aplicá-la somente às cópias em dicionário criadas por `montar_resposta_confirmacoes`.

- [ ] **Step 4: Executar testes focados e a suíte do projeto**

Run: `python -m pytest tests/routers/test_operacional_confirmacoes.py -q`

Expected: PASS.

Run: `python -m pytest -q`

Expected: PASS; qualquer falha preexistente ou externa deve ser registrada antes da publicação.

- [ ] **Step 5: Validar sintaxe e diff**

Run: `python -m py_compile app_prontocardio/routers/operacional.py`

Run: `git diff --check && git diff -- app_prontocardio/routers/operacional.py tests/routers/test_operacional_confirmacoes.py`

Expected: compilação e verificação sem erros; diff limitado ao endpoint e seus testes.

- [ ] **Step 6: Commit da implementação**

```bash
git add app_prontocardio/routers/operacional.py tests/routers/test_operacional_confirmacoes.py docs/superpowers
git commit -m "feat(operacional): identificar unidades nas confirmações"
```

### Task 2: Publicação canônica e verificação

**Files:**
- Modify: fonte canônica apenas por meio do commit da Task 1.
- Create: diretório de deployment imutável no servidor para a nova revisão.

**Interfaces:**
- Consumes: commit testado da Task 1 e release ativa saudável.
- Produces: nova imagem `api_prontocardio:<revision>`, Compose canônico e Compose de rollback.

- [ ] **Step 1: Confirmar pré-condições de produção**

Verificar VPN, saúde do contêiner ativo, revisão ativa igual ao `main`, worktree limpa e ausência de avanço concorrente do repositório remoto.

- [ ] **Step 2: Construir e validar a imagem imutável**

Construir a imagem com o rótulo da revisão e confirmar que o hash de `operacional.py` na imagem corresponde à fonte commitada.

- [ ] **Step 3: Preparar rollback e publicar**

Salvar o Compose ativo como `rollback.compose.yml`, criar o Compose da nova revisão e recriar somente o serviço `api_prontocardio`.

- [ ] **Step 4: Verificar o runtime**

Confirmar estado `healthy`, importação OpenAPI, ausência de erros recentes e executar chamada autenticada ao endpoint com registros das unidades 1 e 2.

Expected: os dois textos públicos exatos aparecem; contagens da resposta permanecem coerentes.

- [ ] **Step 5: Registrar evidências de entrega**

Registrar revisão anterior, nova revisão, imagem, Compose ativo, Compose de rollback, hashes e resultado dos testes/smoke.
