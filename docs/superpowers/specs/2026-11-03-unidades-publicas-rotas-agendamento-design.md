# Unidades públicas consistentes nas rotas de agendamento

## Intenção

Garantir que a integração da G4 receba nome e endereço das Unidades Diagnósticas 01 e 02 tanto em `GET /operacional/agendamentos/confirmacoes` quanto em `GET /agendamentos/pacientes/{cd_paciente}/agendamentos`.

## Escopo

- Centralizar o mapeamento público das unidades em um módulo puro compartilhado.
- Usar `cd_unidade_atendimento` como identificador estável sempre que a consulta disponibilizar o código.
- Preservar o campo existente `ds_unidade_atendimento`, evitando mudança obrigatória na integração.
- Retornar exatamente:
  - `Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart - Nº 560 - Meireles` para a unidade 1.
  - `Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart - Nº 600 - Meireles` para a unidade 2.
- Preservar o nome original de qualquer unidade não mapeada e valores nulos.

## Fora do escopo

- Alterar cadastros no MV.
- Alterar recibos, telas ou outras rotas que não participam desses dois contratos.
- Criar campos novos de nome/endereço ou exigir mudança no consumidor da G4.

## Abordagens consideradas

1. **Mapeamento compartilhado pelas duas rotas — escolhida.** Evita divergência futura e mantém um único contrato público.
2. Duplicar o mapeamento na rota de agendamentos do paciente. Seria menor no curto prazo, mas permitiria novo descompasso.
3. Criar campos separados para nome e endereço. É mais estruturado, porém altera o contrato e exige mudança na G4.

## Implementação e segurança

- Extrair a função pura de normalização para um módulo sem dependências de banco ou FastAPI.
- Incluir o código da unidade somente na consulta interna da rota do paciente; a resposta pública continuará compatível com o modelo atual.
- Aplicar a transformação em cópias das linhas, sem alterar dados do MV.
- Cobrir as duas rotas com testes para códigos numéricos/string, unidades desconhecidas, nulos, deduplicação e contagens.

## Aceite

- A chamada mostrada pela G4 para o paciente `137703` deve devolver o texto completo da Unidade Diagnóstica 02.
- O endpoint de confirmações deve continuar devolvendo os textos já publicados.
- Unidades não mapeadas, contagens e demais campos devem permanecer inalterados.
- A publicação deve manter imagem imutável, verificação de saúde e rollback.
