# Unidades com endereço nas confirmações da API

## Intenção

O parceiro usa `GET /operacional/agendamentos/confirmacoes` para montar mensagens aos pacientes. O endpoint hoje expõe os nomes cadastrais brutos do MV, que não identificam claramente a unidade nem apresentam o endereço.

## Resultado esperado

- A unidade de código `1` será apresentada como `Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart - Nº 560 - Meireles`.
- A unidade de código `2` será apresentada como `Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart - Nº 600 - Meireles`.
- As demais unidades continuarão com o valor original de `ds_unidade_atendimento`.
- Contagens, filtros, códigos e todos os demais campos do endpoint permanecerão inalterados.

## Restrições e não objetivos

- Não alterar `DBAMV.UNIDADE_ATENDIMENTO` nem outro cadastro do MV.
- Não alterar outras rotas de agendamento nesta entrega.
- Não criar uma tabela ou configuração nova para somente duas unidades fixas.
- O endereço solicitado pelo usuário é a fonte para o texto público, mesmo que `DS_LOCAL_UNIDADE_ATENDIMENTO` atualmente registre `RUA BARAO DE STUDART`.

## Abordagens consideradas

1. **Mapear por `cd_unidade_atendimento` na resposta do endpoint — escolhida.** É estável, limitada ao contrato do parceiro e independe de pequenas mudanças no nome cadastral.
2. Alterar o cadastro mestre do MV. Rejeitada por ampliar o impacto para telas, relatórios e integrações fora do escopo.
3. Adicionar campos separados de nome e endereço. Rejeitada porque exigiria mudança imediata no consumidor e não atende à solicitação de substituir o texto já exibido.

## Validação e implantação

Testes unitários devem cobrir os códigos `1` e `2`, uma unidade diferente e valor nulo. A versão canônica deve ser compilada, ter os testes executados, ser publicada como nova imagem imutável e validada no endpoint autenticado. A versão anterior permanecerá disponível para rollback.
