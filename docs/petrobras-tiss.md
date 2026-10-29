# Petrobras TISS no Follow-Up de Glosas

## Estado da entrega

Implementação isolada para o convênio 37 (Saúde Petrobras), desabilitada por padrão.
Não houve migração de produção, importação real validada ou envio de recurso.
As consultas de pagamento das competências 202609 e 202610 retornaram ausência
de informações. Uma resposta de negócio não comprova, sozinha, que todas as
permissões do prestador estão habilitadas.

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
envio. A migração foi exercitada em SQLite com schema; validar também em
PostgreSQL de homologação antes de produção.

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
