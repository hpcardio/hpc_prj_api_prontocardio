# Contas a pagar sem fornecedor cadastrado no MV

## Comportamento

Lancamentos com `CODIGO_DO_FORNECEDOR = NULL` passam a integrar a consulta,
os totais, filtros, paginacao e exportacao. Cada nome de favorecido forma um
grupo separado. Sem nome, a identidade usa a conta a pagar (ou a parcela).
Nao ha associacao automatica a um fornecedor cadastrado com o mesmo nome.

Para fornecedores cadastrados, `codigo_fornecedor` continua inteiro.
Para grupos sem cadastro, o campo carrega `sem-cadastro-<base64url>` da
referencia de origem e `sem_cadastro=true`. O frontend mostra "Sem cadastro
no MV". A chave identifica o grupo no Receita Certa; nunca e um codigo MV.

## Classificacao sem cadastro

O formulario aceita nivel de 1 a 5 e justificativa. A API verifica se a chave
canonica corresponde a um grupo presente na consulta atual do MV antes de
salvar. Cada alteracao acrescenta um evento na tabela
`contas_pagar_classificacoes_sem_cadastro`, com nome de origem, nivel,
justificativa, usuario autenticado e data. O maior ID por chave representa
a classificacao atual; revisoes anteriores permanecem preservadas.

Sem classificacao, o grupo fica pendente e fora dos 25 quadrantes. Depois
de classificado, participa dos filtros, prioridade, matriz e exportacao
com o nivel informado, inclusive nivel 1 (nao critico). Nao ha associacao
a fornecedor cadastrado com o mesmo nome. Pagamentos complementares
continuam exigindo codigo MV inteiro.

Se o nome de origem mudar ou o MV fornecer um codigo posteriormente, a
identidade muda. Nao copiar automaticamente a classificacao: revisar a
nova identidade; o historico anterior permanece registrado.

### Publicacao desta funcionalidade

1. Fazer backup do PostgreSQL oficial `187.127.6.75/prontocardio_db`.
2. Conferir a revisao Alembic vigente e aplicar a migracao aditiva
   `20261027_062` (depende de `20261026_061`) antes de publicar a API.
3. Publicar a API e depois o frontend dos respectivos commits aprovados.
4. Conferir listagem, classificacao e reclassificacao com justificativa,
   filtros de pendencias e quadrantes. Nao alterar classificacoes reais
   apenas para testar sem escolher um registro apropriado com o usuario.

A mudanca nao altera a view Oracle nem o tratamento de fornecedores
numericos. Em rollback, restaurar as imagens anteriores e preservar a
nova tabela de auditoria. Nao executar downgrade depois de haver registros,
pois ele remove a tabela e seu historico.

### Validacao da classificacao sem cadastro

- 104 testes de Contas a Pagar/criticidade/filtros da API e 248 do frontend.
- Lint dos arquivos Python alterados.
- Migracao real em PostgreSQL 16 descartavel: duas revisoes preservadas,
  leitura da ultima revisao, bloqueio de nivel invalido e justificativa
  vazia, e protecao do usuario de auditoria por chave estrangeira.
- Nenhuma classificacao de producao foi alterada na validacao.

## Publicacao original dos grupos sem cadastro

Publicar o frontend compativel antes de habilitar os grupos na API/view.
O script `oracle/contas_pagar_incluir_sem_cadastro.sql` preserva a definicao
consultada em producao em 27/10/2026, alterando somente o predicado:

```sql
AND (C.CD_FORNECEDOR IS NULL OR C.CD_FORNECEDOR <> 1337)
```

`C.CD_FORNECEDOR <> 1337` sozinho tambem elimina NULL. A API nao recupera
registros que a view ja excluiu. Antes de executar o DDL, salvar a definicao
vigente para rollback e conferir se houve outras alteracoes desde a revisao.
DDL Oracle implica commit; nao tratar a execucao como transacao reversivel.
Executar o script somente na publicacao aprovada, junto da API e frontend.
Se precisar reverter, restaurar a definicao salva e as imagens anteriores.

## Validacao original dos grupos em 27/10/2026

- 56 testes da API e 242 testes do frontend, mais lint dos arquivos Python.
- Consulta real em transacoes `READ ONLY` no Oracle e PostgreSQL oficial.
  A definicao corrigida foi usada como CTE, sem substituir a view.
- 419 fornecedores cadastrados mantiveram seus saldos Oracle.
- Recuperados 66 grupos sem cadastro, 456 titulos, R$ 132.298,75 vencidos
  e R$ 8.552.597,98 em aberto, incluindo vencimentos correntes e futuros.
- CSRF: R$ 71.116,99 vencidos; IRRF PJ: R$ 23.363,34;
  MOESIO: R$ 33.833,68. Tambem recuperados os ISS do comparativo.
- FOLHA PJ aparece com tres titulos, R$ 1.026.000,00 em aberto e nenhum
  vencido nessa data. Isso nao confirma o valor divergente da planilha MV.
- Cada grupo sem cadastro retornou seus titulos; a soma dos detalhes
  correspondeu ao saldo agregado. Nenhuma escrita em producao foi feita.
