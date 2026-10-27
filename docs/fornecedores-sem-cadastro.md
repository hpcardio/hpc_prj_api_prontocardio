# Contas a pagar sem fornecedor cadastrado no MV

## Comportamento

Lancamentos com `CODIGO_DO_FORNECEDOR = NULL` passam a integrar a consulta,
os totais, filtros, paginacao e exportacao. Cada nome de favorecido forma um
grupo separado. Sem nome, a identidade usa a conta a pagar (ou a parcela).
Nao ha associacao automatica a um fornecedor cadastrado com o mesmo nome.

Para fornecedores cadastrados, `codigo_fornecedor` continua inteiro.
Para grupos sem cadastro, o campo carrega `sem-cadastro-<base64url>` da
referencia de origem e `sem_cadastro=true`. Essa chave serve apenas para
consulta e associacao dos detalhes, nunca como codigo MV nem chave de
escrita no PostgreSQL. O frontend mostra "Sem cadastro no MV".

Nao se presume nivel de criticidade: os grupos ficam com classificacao
pendente, aparecem no aviso de pendencias da matriz, e nao entram nos 25
quadrantes. O filtro de nao criticos nao inclui esses grupos. Pagamentos
complementares e classificacao continuam disponiveis somente para codigos
MV inteiros; o cadastro no MV precisa ser regularizado para essas acoes.
Se o nome de origem mudar, a chave de consulta muda; nao ha estado manual
persistido nessa chave. Nenhuma migracao PostgreSQL e necessaria.

## Publicacao coordenada

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

## Validacao em 27/10/2026

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
