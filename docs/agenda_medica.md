# Agenda Médica — endpoints `/agenda-medica`

Plataforma de grades médicas do Hospital Prontocárdio. O médico manifesta interesse numa
grade oferecida pelo administrador; o administrador aprova; o médico pede cancelamento e o
responsável institucional decide. **Tudo fica registrado no PostgreSQL da API** (tabelas
`agenda_medica_*`, migração `20261014_059`).

## O que este módulo faz e o que não faz

- **Faz:** ofertas, pedidos, decisões, trilha de auditoria e validações lendo o SoulMV
  (especialidade do médico, conflito de horário, pacientes marcados).
- **Não grava no SoulMV.** A aprovação de interesse devolve em `resultado_mv` o plano exato
  do que a rotina oficial receberia. A escrita no MV fica para a equipe da API implementar e
  revisar (seção abaixo), atrás de uma chave própria.

## Permissões

Três telas novas em `permissions.py`, **fora do padrão** (nenhum usuário existente ganha
acesso automaticamente). Perfis `ti` e `administrador` acessam a gestão.

| Tela | Pode |
|---|---|
| `agenda_medica_medico` | ver ofertas, manifestar interesse, pedir cancelamento (precisa de vínculo usuário → prestador) |
| `agenda_medica_admin` | escalas-modelo, vínculos, criar ofertas, decidir interesses |
| `agenda_medica_responsavel` | decidir cancelamentos |

## Endpoints

| Método | Caminho | Quem |
|---|---|---|
| GET | `/agenda-medica/status` | qualquer tela da agenda |
| GET | `/agenda-medica/escalas-modelo?cd_unidade=&tp_dia_semana=` | admin |
| POST/GET | `/agenda-medica/vinculos` | admin |
| POST | `/agenda-medica/ofertas` (`cd_escala_modelo`, `data_agenda`, `especialidade`) | admin |
| GET | `/agenda-medica/ofertas?status=ABERTA` | qualquer tela da agenda |
| POST | `/agenda-medica/ofertas/{id}/interesse` | médico |
| POST | `/agenda-medica/cancelamentos` (`cd_agenda_central`, `motivo`) | médico |
| GET | `/agenda-medica/solicitacoes?tipo=&status=` | médico vê só os seus |
| POST | `/agenda-medica/solicitacoes/{id}/decisao` (`acao`: aprovar/recusar) | admin (interesse) / responsável (cancelamento) |
| GET | `/agenda-medica/eventos` | qualquer tela da agenda |

## Especificação da gravação no SoulMV (para implementação pela equipe)

Levantada em 13–14/10/2026, somente leitura, no Oracle DBAMV.

**Como o SoulMV faz hoje:** cada escala de consulta (`ESCALA_CENTRAL`, `TP_ESCALA='A'`)
pertence a um prestador; a central gera a agenda pela escala. Para pôr um médico novo numa
grade, o caminho oficial é criar a escala dele e gerar a agenda.

Sequência proposta, **numa única transação** (como `confirmar_agendamento`):

1. `SEQ_ESCALA_CENTRAL.NEXTVAL`; `INSERT INTO ESCALA_CENTRAL ... SELECT` da escala-modelo,
   trocando `CD_PRESTADOR` e `SN_AGENDA_PUBLICA='N'` (primeiro teste).
2. Copiar a configuração da escala-modelo para a nova escala:
   `ESCALA_CENTRAL_CONVENIO`, `ESCALA_CENTRAL_ITEM_AGENDA`, `ESCALA_CENTRAL_SER_TIPO`,
   `ESCALA_CENTRAL_HORARIO`, `ESCALA_CENTRAL_USUARIO` (gatilhos dessas tabelas só gravam log).
3. Mesmo caminho do job oficial `PRC_SCMA_GERAR_AGENDA`:
   `INSERT GERACAO_AGENDA` + `INSERT IT_GERACAO_AGENDA`, `PKG_MV2000.ATRIBUI_EMPRESA` e
   `PKG_SCMA_M_GERA_AGENDA.FNC_GERA_AGENDA` (parâmetros em `plano_geracao`).
4. Conferir: retorno **1** = sucesso (`IT_GERACAO_AGENDA.TP_STATUS='FS'`); **2** = erro;
   **3** = nada gerado/conflito (`'FE'`). Conferir também que existe `AGENDA_CENTRAL` para a
   nova escala na data. Caso contrário, `rollback`.

Fatos verificados no código do pacote:

- `PKG_SCMA_M_GERA_AGENDA` **não faz COMMIT** (a única ocorrência está comentada); o
  rollback da API desfaz tudo.
- `TP_DIA_SEMANA`: 1 = domingo … 7 = sábado.
- Setor/empresa das consultas: Clínica Prontocárdio Saúde → setor 56; Unidade Diagnóstico 2
  → setor 137; `CD_MULTI_EMPRESA = 1`.
- **Nunca** `UPDATE`/`DELETE` em `IT_AGENDA_CENTRAL` (12 gatilhos, incluindo a jornada de
  WhatsApp).
- Cancelamento: as rotinas de bloqueio (`PKG_PARA_M_BLOQ_DESBLOQ_AGENDA`) recebem parâmetros
  no formato das telas Forms (CLOB). Confirmar com a MV antes de chamar fora da tela.

Sugestão: chave própria `AGENDA_MEDICA_MV_GRAVACAO_HABILITADA` (padrão `false`), separada
de `MV_GRAVACAO_HABILITADA`, e primeiro teste controlado conforme o roteiro do projeto
Agenda Médica (`docs/roteiro-primeiro-teste.md`).
