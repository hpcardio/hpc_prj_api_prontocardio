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

## Triagem sênior

Médicos de nível **sênior** (vínculo com `nivel = 'senior'`, definido pelo
administrador em `POST /agenda-medica/vinculos`) escolhem quais pacientes das
consultas ambulatoriais dos próximos dias ficam com eles e quais seguem para os
demais prestadores.

| Rota | Quem | O que faz |
|---|---|---|
| `GET /agenda-medica/triagem?dias=30` | sênior ou administrador | Pacientes agendados (SoulMV, somente leitura) e decisões já tomadas |
| `POST /agenda-medica/triagem/decisoes` | sênior | `{"cd_it_agenda_central": 1, "decisao": "senior" \| "demais" \| null}`; `null` desfaz a própria decisão |
| `POST /agenda-medica/triagem/evolucoes` | sênior | As duas últimas evoluções médicas do paciente (documento tipo 36, fechadas, não confidenciais) |

- Uma decisão por agendamento; outro sênior não sobrescreve (409).
- Decisões e consultas às evoluções ficam em `agenda_medica_eventos`.
- A triagem não altera o SoulMV: a consulta continua na agenda original.
- O administrador acompanha a lista, mas não decide nem lê evoluções.

## Gravação da grade aprovada no SoulMV

Controlada pela variável `AGENDA_MEDICA_MV_GRAVACAO_HABILITADA` (padrão `false`),
separada da `MV_GRAVACAO_HABILITADA` do agendamento de pacientes.

Com a chave ligada, aprovar o interesse de um médico (`POST /agenda-medica/solicitacoes/{id}/decisao`)
faz, numa única transação Oracle (rollback em qualquer falha):

1. nova `ESCALA_CENTRAL` do médico (`SEQ_ESCALA_CENTRAL`), cópia da escala-modelo, e cópia da
   configuração: `ESCALA_CENTRAL_CONVENIO`, `_ITEM_AGENDA`, `_SER_TIPO`, `_HORARIO`, `_USUARIO`
   (essas tabelas só têm gatilhos de log);
2. `GERACAO_AGENDA` + `IT_GERACAO_AGENDA` e `DBAMV.PKG_SCMA_M_GERA_AGENDA.FNC_GERA_AGENDA`,
   como no job oficial `PRC_SCMA_GERAR_AGENDA` (retorno 1 = sucesso);
3. conferência de que a `AGENDA_CENTRAL` do dia foi criada; só então `COMMIT`.

`AGENDA_CENTRAL` e `IT_AGENDA_CENTRAL` nunca são escritas diretamente. Se a geração falhar, a
solicitação continua `PENDENTE` e a resposta é 409 com o motivo. O usuário registrado na geração
vem de `AGENDA_MEDICA_USUARIO_MV` (padrão `DBAMV`, como no job oficial).

Antes de ligar em produção: primeiro teste controlado com uma oferta real num dia sem pacientes
e conferência no SoulMV.

## Bloqueio da agenda após cancelamento aprovado

Controlado separadamente por `AGENDA_MEDICA_MV_CANCELAMENTO_HABILITADO`
(padrão `false`). Com a chave ligada, a aprovação do responsável institucional:

1. recusa a operação se houver pacientes marcados;
2. registra `LOG_OPERA_AGENDA_CENTRAL` como bloqueio do tipo 6,
   **A PEDIDO DO MEDICO**, com a justificativa da solicitação;
3. chama `DBAMV.FNC_BLOQUEIA_DESBLOQUEIA`, a mesma função oficial usada pelo
   fluxo do SoulMV;
4. confere que todos os horários da agenda ficaram bloqueados e só então faz
   `COMMIT`.

Falha, concorrência ou bloqueio incompleto provocam `ROLLBACK`, resposta 409 e
mantêm a solicitação pendente. Antes de ligar a chave em produção, validar uma
agenda futura sem pacientes no ambiente de homologação e conferir o bloqueio
pela tela do SoulMV.

### Geração automática do SoulMV (atenção)

O job `SCMA_GERAR_AGENDA` (diário, 00:10) chama `PRC_SCMA_GERAR_AGENDA`, que estende para frente
toda escala ativa (`SN_ATIVO = 'S'`) que já tenha agenda futura. Hoje ele **não gera nada**, porque
`CONFIG_SCMA.SN_GERA_AGENDA_AMBULATORIO` está vazio (levantado em 20/10/2026, sem gerações automáticas
nos 7 dias anteriores). Se essa configuração for ligada no SoulMV, a escala criada por uma aprovação
passa a gerar agendas toda semana — a grade aprovada vira recorrente.
