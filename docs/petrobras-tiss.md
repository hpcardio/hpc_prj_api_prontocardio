# Petrobras TISS no Follow-Up de Glosas

## Estado em 29/10/2026

Integração implementada em branches isoladas, ainda sem implantação ou migração
em produção. Credenciais usadas somente nas consultas autorizadas; não estão no
Git. Transmissão de recursos permanece desabilitada.

Validação autenticada direta no webservice, de janeiro a outubro de 2026, seguida
de SELECT Oracle em transação somente leitura. Nenhum XML real foi salvo; somente
contagens agregadas foram registradas. Os arquivos fornecidos pelo usuário foram
referências de diagnóstico, não substitutos da consulta ao serviço.

| Competência | Protocolos consultados | Protocolos retornados | Guias retornadas | Itens detalhados | Registros com glosa |
|---|---:|---:|---:|---:|---:|
| 202601 | 4 | 1 | 1 | 84 | 12 |
| 202602 | 3 | 1 | 1 | 67 | 28 |
| 202603 | 3 | — | — | — | — |
| 202604 | 8 | 2 | 2 | 217 | 37 |
| 202605 | 3 | — | — | — | — |
| 202606 | 6 | 3 | 3 | 576 | 102 |
| 202607 | 3 | — | — | — | — |
| 202608 | 6 | 1 | 1 | 391 | 51 |
| 202609 | 3 | — | — | — | — |
| 202610 | 5 | 2 | 2 | 193 | 12 |

Março, maio, julho e setembro retornaram “nenhuma informação” na consulta de
análise dos protocolos descobertos nos pagamentos. Isso não significa ausência
de glosas: o portal mostra dados de setembro. Pagamentos foram obtidos nos dez
meses, mas seus resumos não substituem detalhes de procedimentos.

Foram recebidos 1.528 itens detalhados e 242 registros com motivos/valores de
glosa nas seis competências. As somas não representam itens únicos entre meses
nem cobertura completa. O cruzamento retornou 240 não localizados e dois com
dados insuficientes; nenhum vínculo automático foi confirmado.

Diagnóstico complementar de outubro: as carteiras dos 12 itens glosados existem
na view Oracle; nenhuma guia do prestador ou da operadora corresponde, mesmo
sem zeros iniciais. Dois itens coincidem por carteira, procedimento e data, mas
isso não autoriza associar contas sem confirmar a guia. Nenhum vínculo foi
fabricado por carteira isolada.

## Fluxo de importação

1. Buscar pagamentos por competência com `codigoPrestadorNaOperadora` nos dois
   locais exigidos pelo contrato (cabeçalho e dados do prestador).
2. Descobrir e persistir os protocolos reais da operadora, sem usar protocolos
   Oracle nem substituir protocolo por lote. Protocolo zero não é consultável.
3. Buscar análise em lotes de até 30 protocolos e importar apenas detalhes
   explicitamente retornados. A ação da tela agora realiza essas duas consultas.
4. Persistir fonte, valores, totais de cada escopo, cruzamento e auditoria no
   PostgreSQL da aplicação; preservar rascunhos, anexos e histórico em reconsultas.
5. Consultar Oracle somente para o convênio e as guias recebidas, sem escrita.

Os totais de protocolo/demonstrativo podem abranger guias ausentes da resposta.
São armazenados como metadados; não são distribuídos entre os itens. Saldos de
uma guia não detalhados permanecem separados dos procedimentos. Totais inferiores
à soma dos detalhes geram aviso, mantêm os valores originais e bloqueiam envio
de recurso daquele registro. Não corrigir valores por suposição.

A cobertura das respostas de análise não foi comprovada. Toda execução com
detalhes é sinalizada como parcial; protocolos ausentes permanecem pendentes,
e registros importados anteriormente não são removidos por ausência no retorno.
A interface apresenta essas limitações e os avisos por registro.

## Cruzamento Oracle

A escolha confirmada pelo usuário é `numeroGuiaPrestador` → `NR_GUIA` da
`DBAMV.HPC_V_CONTA_ATENDIMENTO`, padrão `PETROBRAS_ORACLE_GUIA=guia_prestador`.
Não existe fallback silencioso para guia da operadora.

A igualdade exata de guia + procedimento é preferida. Apenas para guia do
prestador numérica, zeros iniciais podem ser removidos da chave de pesquisa;
essa alternativa exige carteira exata em ambas as origens e registra a regra no
resultado. Identificadores originais continuam preservados. Carteira e código
TUSS nunca perdem zeros iniciais. Carteira divergente impede vínculo.

A tabela do código também é considerada: 18, 19, 20 e 22 são pesquisadas em
CD_TUSS; tabela 00/98 ou desconhecida exige mapeamento confirmado e não é assumida
como TUSS. Duplicatas idênticas da view são deduplicadas; procedimentos repetidos
são desambiguados por data/quantidade quando possível. Demais casos ficam ambíguos.
Sequencial TISS não é presumido igual ao lançamento MV.

O segundo XML de análise fornecido pelo usuário confirmou uma coincidência por
guia normalizada + carteira + CD_TUSS, mas declara tabela 00. Esse caso não foi
usado como autorização para mapear automaticamente todos os códigos internos.

## Contratos e compatibilidade de recepção

Manual: https://portaltiss.saudepetrobras.com.br/documentos/manual/manual_web_service.pdf

WSDL/XSD 4.01.00 versionados em `app_prontocardio/services/petrobras_contracts`.
Base HTTPS: `https://webservices.crc.com.br/tiss/ws/40100/`.
Serviços: `tissSolicitacaoDemonstrativoRetorno`, `tissRecursoGlosa`,
`tissSolicitacaoStatusRecursoGlosa`, `tissEnvioDocumentos` (WSDL = nome + `.wsdl`).
O cliente usa HTTPS, sem redirecionamentos, com senha MD5/hash conforme contrato.
Não há logs de mensagens SOAP ou credenciais. Hash não substitui TLS.

ANS atual configurada: 422631. O código legado 366871 encontrado no MV retornou
DESTINATARIO_INVALIDO. Nenhum cadastro Oracle foi alterado.

O valor do código do prestador pode coincidir com o CNPJ, mas a tag precisa ser
`codigoPrestadorNaOperadora`; corrigir isso resolveu a consulta de pagamentos
vazia. `PETROBRAS_CODIGO_PRESTADOR` sobrescreve o valor quando necessário.

Compatibilidades exclusivamente de entrada:

- Conferir o hash original e as identidades exatas da operadora/prestador, também
  quando o serviço devolve o cabeçalho na orientação da solicitação.
- O CRC pode devolver o CNPJ do prestador sem zero inicial no campo de metadados
  da operadora. Validar uma cópia apenas para esse defeito exato, com aviso; a
  identidade da operadora continua sendo conferida pelo registro ANS.
- Códigos de glosa 3202, 3209, 3253, 3292 e 3306 foram recebidos fora do domínio
  enumerado do XSD publicado. Uma variante de recepção aceita códigos numéricos
  de quatro dígitos, mantendo todas as outras restrições estruturais/tipos, o hash
  e as identidades. O valor original é preservado e sinalizado para revisão.
  Isso não confirma o significado normativo desses códigos.
- O schema de envio permanece integralmente original. Código desconhecido e
  totais divergentes não são transmitidos. Demais erros estruturais continuam
  bloqueando a importação, sem zerar os dados anteriores.

## Configuração e execução

Usar as variáveis de `petrobras.env.example` e um gerenciador de segredos. Nunca
copiar senhas para Git, fixtures ou PRs. Instalar pelo `poetry.lock` (inclui lxml).
Antes de ativar, verificar o head Alembic e aplicar a migração aditiva
`20261029_063`, descendente de `20261027_062`, no PostgreSQL da aplicação.
Habilitar `PETROBRAS_ENABLED=true` na API e frontend somente com tabelas/API
disponíveis. Manter `PETROBRAS_TRANSMISSION_ENABLED=false`.

Worker periódico: `python -m scripts.sync_petrobras`. Usa trava PostgreSQL,
intervalo padrão de 360 minutos e descoberta dos últimos três meses, ajustável
por `PETROBRAS_DISCOVERY_MONTHS`. Para o período solicitado:

```sh
python -m scripts.sync_petrobras --inicio 202601 --fim 202610
```

O comando histórico respeita o intervalo das consultas recentes, importa os
protocolos retornados pela operadora e mantém pendências nos retornos incompletos.
O worker nunca envia recursos. Protocolos descobertos continuam acompanhados;
não se presume prazo recursal a partir da emissão.

## Recursos, anexos e operação

A tela mantém os fluxos existentes e adiciona comparação com Oracle, histórico,
justificativa, prazo/origem e anexos autenticados. PDF, PNG e JPEG: limite local
padrão de 5 MiB e 20 anexos por item (não é limite confirmado da operadora).
O proxy precisa suportar o corpo base64; frontend limita a 8 MiB por padrão.

Antes de habilitar transmissão, validar identificação dos itens, prazo recursal,
aceitação do comprovante e recibo da Petrobras. Não presumir confirmação extra no
portal; verificar o fluxo real. O usuário precisa salvar e confirmar o envio.
Intenção é persistida antes da chamada. Timeout/recibo ausente gera envio incerto,
sem reenvio automático. Consultas de recursos protocolados continuam após o prazo.

Há trabalho independente de OpenFinance/segurança em produção. Integrar via PR;
não substituir diretórios do servidor pelos worktrees da integração Petrobras.
Nenhuma migração, carga real no PostgreSQL ou envio foi feito nesta validação.

## Testes e reversão

API: 109 testes sintéticos/regressões; frontend: sete testes Django e ensaio local
Playwright desktop 1440 px/celular 390 px, com carteira, rascunho e ausência de
envio involuntário. Ruff nos arquivos alterados. Os XMLs reais não são fixtures.

Casos novos: lotes parciais, idempotência, rascunhos preservados, protocolo estranho
recusado, totais divergentes, normalização restrita de guia, carteira/código com
zeros preservados, tabela desconhecida e contrato de envio não relaxado.
Migração previamente exercitada em SQLite e PostgreSQL 12.6 descartável. Há três
testes PostgreSQL opt-in em `tests/services/test_petrobras_postgres.py`, restritos
à instância local de testes; a migração não foi alterada nesta revisão.

Para desativar, suspender agendamento e `PETROBRAS_ENABLED=false` nos serviços.
Preservar tabelas e auditoria. Downgrade exclui dados e não deve ser usado como
reversão operacional depois de uma importação real.
