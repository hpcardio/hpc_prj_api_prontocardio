# Petrobras TISS no Follow-Up de Glosas

## Estado da entrega

Consulta real de demonstrativos de pagamento validada para janeiro a outubro de
2026 após corrigir a identificação do prestador: o serviço requer a tag
`codigoPrestadorNaOperadora`, mesmo quando seu valor é o próprio CNPJ.
Os resultados vazios registrados nas primeiras validações foram superados por
esta correção. São evidências históricas, não o estado atual da consulta.

A consulta detalhada de análise dos três protocolos válidos de setembro ainda
retorna ausência de informações. Não houve cruzamento real guia/TUSS/carteira,
migração de produção, carga de dados ou envio de recurso. A integração permanece
em rascunho, com transmissão desabilitada.

## Origem e cruzamento

1. Consultar o demonstrativo de pagamento por competência (AAAAMM) na Petrobras.
2. Persistir os protocolos retornados pela própria operadora.
3. Consultar os demonstrativos de análise desses protocolos e persistir seus itens.
4. Consultar `DBAMV.HPC_V_CONTA_ATENDIMENTO` em transação Oracle somente leitura,
   restrita ao convênio configurado e às guias retornadas.
5. Cruzar guia + código TUSS, preservando zeros à esquerda. A carteira
   (`numeroCarteira` / `NR_CARTEIRA`) serve como conferência adicional.

Não são usados protocolos de faturamento do Oracle. O contrato consultado não
permite buscar demonstrativos pela carteira. Carteira divergente impede o vínculo;
ausência da carteira gera indicação de conferência indisponível. Procedimentos
repetidos são desambiguados por data/quantidade quando possível; o restante fica
ambíguo. O sequencial TISS não é presumido igual ao lançamento MV.

`PETROBRAS_ORACLE_GUIA=pendente` impede vínculos automáticos até verificar, com
uma guia real, se `NR_GUIA` corresponde à `guia_prestador` ou `guia_operadora`.
Não configurar esse parâmetro por suposição. Valores divergentes permanecem
visíveis lado a lado; a integração não escreve no Oracle.

## Contratos

Manual: https://portaltiss.saudepetrobras.com.br/documentos/manual/manual_web_service.pdf

Contratos 4.01.00 versionados em `app_prontocardio/services/petrobras_contracts`.
Base HTTPS: `https://webservices.crc.com.br/tiss/ws/40100/`.
Serviços: `tissSolicitacaoDemonstrativoRetorno`, `tissRecursoGlosa`,
`tissSolicitacaoStatusRecursoGlosa`, `tissEnvioDocumentos`.
Os WSDL correspondem ao mesmo nome com extensão `.wsdl`.
O WSDL anuncia HTTP; o cliente usa somente o mesmo serviço por HTTPS, sem
redirecionamentos. Login e senha são enviados conforme o contrato (senha MD5);
mensagens são validadas por XSD e hash TISS. O hash exigido pelo protocolo não
substitui TLS. Não há logs de credenciais ou de mensagens SOAP completas.

ANS configurada: 422631. O código legado 366871 encontrado no MV retornou
`DESTINATARIO_INVALIDO`. Nenhum cadastro Oracle foi alterado.

## Configuração e ativação

Usar variáveis de ambiente/gerenciador de segredos; o exemplo adjacente não contém
credenciais. Configurar os segredos fornecidos pelo responsável no ambiente da API.
Não copiar senhas para Git, arquivos de teste, screenshots ou descrições de PR.

- Instalar dependências pelo `poetry.lock` (inclui lxml).
- Verificar o head Alembic do ambiente e aplicar a migração aditiva
  `20261029_063` após `20261027_062` no PostgreSQL da aplicação.
- Habilitar `PETROBRAS_ENABLED=true` na API e no frontend apenas com a API
  correspondente e suas tabelas disponíveis.
- Manter `PETROBRAS_TRANSMISSION_ENABLED=false` até validar o fluxo real.
- Executar `python -m scripts.sync_petrobras` periodicamente, por exemplo a cada
  15 minutos, com o mesmo ambiente da API. A trava PostgreSQL evita dois workers;
  o intervalo padrão de consulta é 360 minutos. O worker nunca envia recursos.
- Descoberta automática cobre mês atual e dois anteriores, configurável em
  `PETROBRAS_DISCOVERY_MONTHS`. Competências anteriores podem ser consultadas
  manualmente na tela. Protocolos descobertos continuam sendo acompanhados;
  não se presume prazo recursal a partir da data de emissão.

A base usada para desenvolver foi `hospital/main`. Há trabalho de produção
independente (incluindo OpenFinance/segurança): integrar pelo fluxo normal de PR e
revisar diferenças do ambiente antes do deploy; não substituir diretórios do servidor.

## Uso e validação real pendente

A tela mantém os fluxos existentes e adiciona uma área Petrobras com filtros,
comparação Oracle, histórico, justificativa, prazo/origem e anexos autenticados.
PDF, PNG e JPEG: limite local padrão de 5 MiB e 20 arquivos por item (não constitui
limite confirmado da operadora). Upload base64 exige limite de corpo HTTP/proxy
compatível; o frontend usa 8 MiB por padrão. Não elevar limites sem revisão.

Antes de ativar transmissão: obter demonstrativo com itens, validar carteira,
guia/TUSS, valores e ausência de duplicidades; confirmar prazo recursal e sua
origem; validar aceitação de anexo e recibo de recurso pela operadora. O usuário
precisa salvar o rascunho e confirmar o envio. Não foi presumida a necessidade de
confirmação adicional no portal: verificar o comportamento real da Petrobras.

Intenção de envio é persistida antes da chamada. Timeout/resposta sem recibo gera
`envio_incerto`, sem reenvio automático. Conferir o portal e o histórico antes de
qualquer intervenção; não reverter o estado para rascunho sem investigação.
Acompanhamento de recurso já protocolado continua após o prazo de apresentação.
Respostas parciais preservam os itens e valores acatados individualmente.

## Validação e reversão

Testes sintéticos cobrem contratos XSD, carteira/guia/TUSS, duplicidades,
permissões, concorrência de rascunho, anexos, idempotência, migração e falhas de
envio. A migração foi exercitada em SQLite com schema e PostgreSQL 12.6 descartável
local. A versão/configuração do PostgreSQL oficial ainda deve ser considerada
no procedimento de implantação.

Para desativar: `PETROBRAS_ENABLED=false` em ambos os serviços e suspender o
agendamento. Preservar tabelas/auditoria. O downgrade exclui dados Petrobras e
não deve ser usado como reversão operacional após importação real.

## Evidências de verificação (29/10/2026)

- API: 92 testes aprovados (26 Petrobras e 66 regressões de Follow-Up/recursos).
- Frontend: 7 testes Django aprovados, `node --check` sem erros.
- Navegador local: desktop 1440 px e celular 390 px, sem rolagem horizontal;
  carteira visível nas duas origens e salvamento de rascunho verificado.
- Nenhuma chamada de envio ocorreu no ensaio visual; chamadas API foram simuladas.
- Ruff passou nos módulos novos e testes da API.
- Contratos WSDL/XSD preservados como obtidos, inclusive espaços de comentários.

## Validação adicional solicitada (29/10/2026)

- Consultadas individualmente via HTTPS as competências 202601, 202602, 202603,
  202604, 202605, 202606, 202607, 202608, 202609 e 202610, usando as credenciais
  fornecidas e transmissão desabilitada. Todas retornaram a mensagem de negócio
  “Nenhuma informação de pagamento encontrada para os parâmetros consultados”.
  Não se conclui ausência de glosas a partir dessa resposta, nem habilitação
  completa do prestador no serviço. Nenhum protocolo/item foi retornado.
- PostgreSQL 12.6 local descartável: 3 novos testes aprovados, junto aos 26 testes
  Petrobras já existentes (29 aprovados nessa execução). Verificadas criação das
  cinco tabelas, colunas e índices, downgrade/reupgrade, importação idempotente,
  preservação de justificativa, JSON, binário, timezone, unicidade, chave estrangeira,
  bloqueio de linha NOWAIT e trava de worker entre duas conexões.
- Testes reproduzíveis em `tests/services/test_petrobras_postgres.py`: exigem
  `PETROBRAS_TEST_POSTGRES_URL`, aceitando somente host `127.0.0.1`, usuário
  `petrobras_test` e banco `petrobras_validation`. Usar banco descartável vazio;
  os testes criam e removem exclusivamente o schema dessa instância de teste.
  Sem essa variável, os três testes são ignorados.
- Consultados os comentários do dicionário Oracle para `DBAMV.GUIA` em transação
  somente leitura. `NR_GUIA` não possui comentário que confirme sua correspondência
  à guia do prestador ou operadora. O mapeamento permanece pendente.
- A conferência do portal pelo navegador não foi possível: host de acesso ao
  navegador indisponível e estado de acessibilidade da janela indisponível.

Pendência externa: confirmar a disponibilidade de demonstrativos deste prestador
no webservice Petrobras e obter uma resposta real com itens. Só então será possível
validar guia/TUSS/carteira com dados reais. Nenhuma migração de produção, carga real,
recurso ou comprovante foi enviado durante estas validações.

## Divergência comprovada entre portal e SOAP (29/10/2026)

A imagem fornecida pelo usuário mostra demonstrativos no portal para 31/08/2026,
30/09/2026 e 30/10/2026, todos com itens glosados. Portanto, o retorno vazio por
competência não comprova inexistência de demonstrativos nem falta de cadastro.

Foi executada consulta SOAP de leitura para cada uma dessas datas usando a
alternativa `periodo/dataPagamento` prevista no XSD, em vez de `periodo/competencia`.
Todas retornaram HTTP 500. O diagnóstico de 30/09 identificou SOAP-ENV:Client e
ORA-01403 (no data found) na rotina de solicitação de demonstrativo da operadora.
Payload bruto, credenciais e detalhes de infraestrutura não foram persistidos.

Causa exata ainda não determinada: falta comparar identificadores do prestador,
competência/data de pagamento, número do demonstrativo e protocolos com o XML de
um extrato efetivamente disponibilizado no portal. A data exibida na lista não foi
presumida definitivamente igual à data de pagamento. Não trocar parâmetros ou
habilitar importação/transmissão com base apenas nessa hipótese.

O manual oficial do portal documenta a emissão do XML em Extrato > Demonstrativo
TISS, após selecionar o demonstrativo e o lote:
https://saudepetrobras.com.br/data/files/1E/74/13/4E/A0A719109531B619A9B8F9C2/Manual_Portal_TISS_Ago_2024.pdf

A próxima validação precisa desse XML real ou de acesso funcional ao portal. O
retorno do webservice deve ser confrontado com esse extrato, sem interpretar
HTTP 500/ausência de informação como zero glosas. Nenhuma alteração especulativa
foi feita no mapeamento Oracle ou nos parâmetros publicados da integração.

## Correção validada a partir do XML do portal

O XML fornecido usa `codigoPrestadorNaOperadora` no cabeçalho e nos dados do
prestador. A implementação anterior usava `CNPJ`/`cnpjContratado`. A mudança das
tags fez a consulta por competência retornar os dados. `PETROBRAS_CODIGO_PRESTADOR`
pode configurar o código; quando vazio, o valor do CNPJ é enviado como código.

O XML do portal declara 4.03.00; o serviço 4.01.00 retorna 4.01.00 e os mesmos
10 grupos/64 guias de setembro. Não foi necessária migração de versão TISS para
recuperar os demonstrativos. Sete grupos de setembro têm protocolo zero e não
possuem glosa; os três restantes abrangem 57 guias, 24 com glosa por guia. O XML
é um resumo de pagamento: não fornece TUSS/carteira por procedimento.

Compatibilidade restrita do retorno CRC:
- Conferir hash sobre o documento original, antes de qualquer ajuste.
- Aceitar direção de cabeçalho espelhada somente conferindo ANS e identificação
  exata do prestador; rejeitar identificação ausente ou diferente.
- O serviço devolve, em `cabecalhoDemonstrativo/numeroCNPJ`, o CNPJ do prestador
  sem zero inicial, apesar de esse campo representar a operadora. Validar XSD
  sobre uma cópia apenas para esse defeito exato; preservar o documento original,
  não usar esse campo para identificar a operadora e registrar aviso na execução.
- Demais erros de XSD e hash continuam bloqueando a importação.
- Ignorar números de protocolo compostos somente de zeros.

Reconsulta autenticada, somente leitura e em memória:

| Competência | Protocolos válidos | Guias |
|---|---:|---:|
| 202601 | 4 | 55 |
| 202602 | 3 | 27 |
| 202603 | 3 | 93 |
| 202604 | 8 | 103 |
| 202605 | 3 | 73 |
| 202606 | 6 | 75 |
| 202607 | 3 | 66 |
| 202608 | 6 | 64 |
| 202609 | 3 | 64 |
| 202610 | 5 | 46 |

Não somar guias dessas competências como pacientes/itens distintos. Testados os
três protocolos válidos do XML de setembro na operação de análise; todos retornam
sem informações. Também verificadas as alternativas CNPJ/código previstas no
contrato para um protocolo real, sem mudança no resultado. Não substituir número
de protocolo por número de lote nem fabricar o vínculo com procedimentos Oracle.
O próximo passo depende do demonstrativo de análise com detalhes por procedimento
ou de esclarecimento da operadora sobre essa operação específica.

Após a correção: 96 testes da API/regressões aprovados; Ruff aprovado nos módulos
alterados. Dados reais e credenciais não foram incluídos no repositório.

## Análise do XML detalhado exportado do portal

O anexo de análise do mesmo demonstrativo/lote foi lido somente em memória:
169 itens, 169 sequenciais distintos, 60 códigos distintos, carteira presente e
nenhum `numeroProtocolo`. Há 27 motivos de glosa distribuídos por 21 itens. Todos
os valores de motivo são zero; os totais de guia/protocolo também são zero, mas
`valorGlosaGeral` é positivo. Não distribuir esse total por suposição.

Todos os procedimentos declaram `codigoTabela=00`. Esses códigos não podem ser
assumidos como TUSS apenas por terem formato numérico. A consulta Oracle de leitura
não encontrou nenhuma das duas guias por igualdade exata. Consulta complementar
pela carteira encontrou 300 linhas e 12 códigos em comum, porém nenhuma das guias
coincidiu mesmo em comparação diagnóstica sem zeros iniciais. Isso não autoriza
vincular por carteira+código: podem existir outros atendimentos do beneficiário.
Não houve alteração da normalização ou associação automática.

O parser de análise agora rejeita explicitamente protocolo ausente/zero e total
geral incompatível com o detalhamento, preservando os dados já existentes. Testes
sintéticos cobrem esses casos; o XML real não foi incluído no Git. Os 100 testes
API/regressões passaram após a alteração.

Para completar esse fluxo será necessária uma entrada de demonstrativo por
arquivo/lote independente do protocolo e revisão das inconsistências financeiras,
além de validar o mapeamento de guia e tabela de procedimentos. Não reutilizar
número de lote como protocolo nem fabricar valores de glosa por item. O envio de
recursos para esses registros segue indisponível até existir referência válida.
