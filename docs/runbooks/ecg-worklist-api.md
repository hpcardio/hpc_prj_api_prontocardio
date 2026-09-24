# Runbook da API de Worklist de ECG

## Objetivo e limites

Este componente permite ao ECG Gateway consultar a DICOM Modality Worklist
do MV por número exato de pedido ou atendimento. A operação é somente leitura:
não consulta o Oracle como alternativa e não grava no MV, no PACS ou no
prontuário.

O fluxo autorizado é:

1. ECG Gateway autentica no APIHPC com uma conta técnica exclusiva;
2. APIHPC valida a permissão `ecg_worklist`;
3. APIHPC abre uma associação DICOM curta com o MWL;
4. o resultado é pós-filtrado por igualdade exata e devolvido sem cache;
5. a associação é liberada mesmo em caso de erro.

## Configuração

Configurar no `.env` protegido do APIHPC, sem versionar credenciais:

```dotenv
ECG_MWL_HOST=192.168.4.31
ECG_MWL_PORT=1010
ECG_MWL_CALLED_AE=SERVER_WL
ECG_MWL_CALLING_AE=HPC_ECG_GW
ECG_MWL_ATTENDANCE_TAG=0040,1001
ECG_MWL_CONNECT_TIMEOUT_SECONDS=5
ECG_MWL_RESPONSE_TIMEOUT_SECONDS=10
ECG_MWL_MAX_RESULTS=10
ECG_MWL_MAX_CONCURRENCY=2
```

Valores aceitos para `ECG_MWL_ATTENDANCE_TAG`:

- `0040,1001`: `RequestedProcedureID`;
- `0040,0009`: `ScheduledProcedureStepID`.

O AE Title possui no máximo 16 caracteres ASCII. O administrador do MWL deve
cadastrar `HPC_ECG_GW` como Calling AE autorizado. Na validação local de
23/08/2026 o servidor respondeu, mas rejeitou a associação com “Calling AE
title not recognised”; portanto o cadastro é um gate obrigatório antes da
sondagem clínica.

## Conta técnica e autorização

Criar um usuário próprio para o ECG Gateway com:

- perfil `leitura`;
- conta ativa;
- somente a permissão `ecg_worklist`;
- senha forte e revogável, armazenada apenas no secret do ECG Gateway.

`ecg_worklist` existe no catálogo, mas não faz parte das permissões padrão.
Não reutilizar usuário administrativo, credencial SSH ou conta pessoal.

## Endpoints

### Pesquisa

`POST /ecg/worklist/search`

```json
{
  "searchType": "ORDER",
  "identifier": "IDENTIFICADOR_DE_TESTE_APROVADO"
}
```

Tipos permitidos:

- `ORDER`: consulta `AccessionNumber (0008,0050)`;
- `ATTENDANCE`: consulta somente a tag configurada.

A resposta usa `Cache-Control: no-store`, contém no máximo dez itens e nunca
faz pesquisa por nome, fragmento ou curinga.

### Saúde

`GET /ecg/worklist/health`

Exige a mesma permissão. Abre e libera a associação DICOM sem consultar um
paciente. Resposta saudável: `{"status":"ok"}`.

## Códigos técnicos

- `MWL_UNAVAILABLE`: falha de rede ou comunicação;
- `MWL_ASSOCIATION_REJECTED`: associação DICOM recusada;
- `MWL_TIMEOUT`: tempo limite excedido;
- `MWL_INVALID_RESPONSE`: status DICOM inesperado;
- `MWL_BUSY`: limite local de concorrência atingido.

O endpoint devolve HTTP 504 para timeout e HTTP 503 para as demais falhas.
Logs registram apenas usuário, tipo, quantidade, duração, código e SHA-256 do
identificador. Não registrar nome, identificador em texto aberto ou dataset
DICOM bruto.

## Implantação segura no servidor 38

1. Repetir o preflight somente leitura: revisão, alterações locais, disco,
   Compose, saúde, reinícios e conectividade com `192.168.4.31:1010`.
2. Criar backup datado dos arquivos que serão substituídos, do ID da imagem e
   dos metadados necessários ao rollback, sem imprimir o `.env`.
3. Aplicar somente o diff entre a linha de base ativa `ff1ecc0` e a versão da
   funcionalidade.
4. Configurar as variáveis e provisionar a conta técnica.
5. Recriar somente o serviço `api_prontocardio`.
6. Validar saúde, reinícios, OpenAPI, respostas 401/403, health autorizado e
   endpoints legados representativos.
7. Inspecionar logs e confirmar ausência de credenciais e dados clínicos.

## Homologação das tags

Somente depois do cadastro do Calling AE:

1. usar um pedido de teste formalmente aprovado;
2. confirmar busca exata de `ORDER` por `AccessionNumber`;
3. testar o atendimento em `0040,1001`;
4. se não houver igualdade exata, alterar somente para `0040,0009` e repetir;
5. não liberar pesquisa por atendimento se nenhuma tag reproduzir o número;
6. registrar horário, resultado, tag confirmada e aprovador sem copiar PHI para
   o documento ou logs.

## Rollback

Se qualquer gate falhar:

1. restaurar os arquivos do backup datado e as variáveis anteriores;
2. reconstruir/recriar somente `api_prontocardio` com a imagem registrada;
3. confirmar saúde, reinícios e endpoints legados;
4. manter o ECG Gateway sem pesquisa clínica até nova homologação.

## Estado conhecido da linha de base

Antes desta funcionalidade, a suíte possuía 259 testes aprovados e uma falha
preexistente em `test_follow_up_usa_total_registro_no_card_e_total_conta_nos_itens`
(`valor_remessa` esperado 135 e retornado 120). A suíte também usa `openpyxl`
em um teste sem declarar a dependência. Esses débitos não devem ser mascarados
nem atribuídos à integração ECG.
