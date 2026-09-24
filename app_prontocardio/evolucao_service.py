from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.evolucao_schema import ContextoEvolucaoMv, EvolucaoMv


class EvolucaoMvErro(Exception):
    pass


class EvolucaoMvNaoEncontrada(EvolucaoMvErro):
    pass


class EvolucaoMvConflito(EvolucaoMvErro):
    pass


class EvolucaoMvConfiguracaoInvalida(EvolucaoMvErro):
    pass


@dataclass(frozen=True)
class ConfiguracaoEvolucaoMv:
    usuario_mv: str
    cd_tipo_documento: int
    cd_objeto: int
    hora_validade: int


def _mapping_one_or_none(result):
    return result.mappings().one_or_none()


def buscar_contexto(
    session: Session,
    cd_atendimento: int,
    configuracao: ConfiguracaoEvolucaoMv,
    escrita_habilitada: bool,
) -> ContextoEvolucaoMv:
    atendimento = _mapping_one_or_none(
        session.execute(
            text(
                """
                SELECT A.CD_ATENDIMENTO,
                       A.CD_PACIENTE,
                       A.CD_MULTI_EMPRESA,
                       A.TP_ATENDIMENTO,
                       A.DT_ALTA
                  FROM DBAMV.ATENDIME A
                 WHERE A.CD_ATENDIMENTO = :cd_atendimento
                """
            ),
            {'cd_atendimento': cd_atendimento},
        )
    )
    if atendimento is None:
        raise EvolucaoMvNaoEncontrada('Atendimento nao encontrado no MV.')
    if atendimento['cd_paciente'] is None:
        raise EvolucaoMvConfiguracaoInvalida(
            'O atendimento nao possui paciente vinculado.'
        )

    usuario = _mapping_one_or_none(
        session.execute(
            text(
                """
                SELECT U.CD_USUARIO,
                       U.CD_PRESTADOR,
                       AU.USER_ID CD_ID_USUARIO
                  FROM DBASGU.USUARIOS U
                  JOIN ALL_USERS AU
                    ON AU.USERNAME = U.CD_USUARIO
                 WHERE U.CD_USUARIO = :usuario_mv
                """
            ),
            {'usuario_mv': configuracao.usuario_mv},
        )
    )
    if usuario is None or usuario['cd_prestador'] is None:
        raise EvolucaoMvConfiguracaoInvalida(
            'Usuario MV configurado nao possui prestador vinculado.'
        )

    documento = _mapping_one_or_none(
        session.execute(
            text(
                """
                SELECT TD.CD_TIPO_DOCUMENTO,
                       TD.DS_TIPO_DOCUMENTO,
                       TD.SN_ASSINATURA_DIGITAL,
                       TD.SN_ATIVO,
                       PO.CD_OBJETO,
                       PO.NM_OBJETO,
                       PO.CD_TIPO_DOCUMENTO CD_TIPO_DOCUMENTO_OBJETO
                  FROM DBAMV.PW_TIPO_DOCUMENTO TD
                  JOIN DBAMV.PAGU_OBJETO PO
                    ON PO.CD_OBJETO = :cd_objeto
                 WHERE TD.CD_TIPO_DOCUMENTO = :cd_tipo_documento
                """
            ),
            {
                'cd_objeto': configuracao.cd_objeto,
                'cd_tipo_documento': configuracao.cd_tipo_documento,
            },
        )
    )
    if (
        documento is None
        or documento['sn_ativo'] != 'S'
        or documento['cd_tipo_documento_objeto']
        != configuracao.cd_tipo_documento
    ):
        raise EvolucaoMvConfiguracaoInvalida(
            'Tipo de documento ou objeto de evolucao invalido no MV.'
        )

    return ContextoEvolucaoMv(
        cd_atendimento=atendimento['cd_atendimento'],
        cd_paciente=atendimento['cd_paciente'],
        cd_prestador=usuario['cd_prestador'],
        usuario_mv=usuario['cd_usuario'],
        cd_id_usuario=usuario['cd_id_usuario'],
        cd_tipo_documento=documento['cd_tipo_documento'],
        cd_objeto=documento['cd_objeto'],
        tipo_documento=documento['ds_tipo_documento'],
        objeto=documento['nm_objeto'],
        assinatura_digital_configurada=(
            documento['sn_assinatura_digital'] == 'S'
        ),
        escrita_habilitada=escrita_habilitada,
    )


def _row_to_evolucao(row) -> EvolucaoMv:
    return EvolucaoMv(
        cd_pre_med=row['cd_pre_med'],
        cd_documento_clinico=row['cd_documento_clinico'],
        cd_atendimento=row['cd_atendimento'],
        cd_paciente=row['cd_paciente'],
        cd_prestador=row['cd_prestador'],
        cd_tipo_documento=row['cd_tipo_documento'],
        cd_objeto=row['cd_objeto'],
        tp_pre_med=row['tp_pre_med'],
        tp_status=row['tp_status'],
        sn_fechado=row['sn_fechado'],
        fl_impresso=row['fl_impresso'],
        usuario_mv=row['usuario_mv'],
        usuario_autorizador=row['usuario_autorizador'],
        dh_criacao=row['dh_criacao'],
        dh_fechamento=row['dh_fechamento'],
        dh_impressao=row['dh_impressao'],
        texto=row['texto'],
        assinatura_certificada=row['cd_documento_digital'] is not None,
    )


def obter_evolucao(session: Session, cd_pre_med: int) -> EvolucaoMv:
    row = _mapping_one_or_none(
        session.execute(
            text(
                """
                SELECT P.CD_PRE_MED,
                       P.CD_DOCUMENTO_CLINICO,
                       P.CD_ATENDIMENTO,
                       D.CD_PACIENTE,
                       P.CD_PRESTADOR,
                       D.CD_TIPO_DOCUMENTO,
                       P.CD_OBJETO,
                       P.TP_PRE_MED,
                       D.TP_STATUS,
                       P.SN_FECHADO,
                       P.FL_IMPRESSO,
                       P.NM_USUARIO USUARIO_MV,
                       D.CD_USUARIO_AUTORIZADOR USUARIO_AUTORIZADOR,
                       P.DH_CRIACAO,
                       D.DH_FECHAMENTO,
                       P.DH_IMPRESSAO,
                       P.DS_EVOLUCAO TEXTO,
                       D.CD_DOCUMENTO_DIGITAL
                  FROM DBAMV.PRE_MED P
                  JOIN DBAMV.PW_DOCUMENTO_CLINICO D
                    ON D.CD_DOCUMENTO_CLINICO = P.CD_DOCUMENTO_CLINICO
                 WHERE P.CD_PRE_MED = :cd_pre_med
                """
            ),
            {'cd_pre_med': cd_pre_med},
        )
    )
    if row is None:
        raise EvolucaoMvNaoEncontrada('Evolucao nao encontrada no MV.')
    return _row_to_evolucao(row)


def listar_evolucoes(
    session: Session, cd_atendimento: int
) -> list[EvolucaoMv]:
    result = session.execute(
        text(
            """
            SELECT P.CD_PRE_MED,
                   P.CD_DOCUMENTO_CLINICO,
                   P.CD_ATENDIMENTO,
                   D.CD_PACIENTE,
                   P.CD_PRESTADOR,
                   D.CD_TIPO_DOCUMENTO,
                   P.CD_OBJETO,
                   P.TP_PRE_MED,
                   D.TP_STATUS,
                   P.SN_FECHADO,
                   P.FL_IMPRESSO,
                   P.NM_USUARIO USUARIO_MV,
                   D.CD_USUARIO_AUTORIZADOR USUARIO_AUTORIZADOR,
                   P.DH_CRIACAO,
                   D.DH_FECHAMENTO,
                   P.DH_IMPRESSAO,
                   P.DS_EVOLUCAO TEXTO,
                   D.CD_DOCUMENTO_DIGITAL
              FROM DBAMV.PRE_MED P
              JOIN DBAMV.PW_DOCUMENTO_CLINICO D
                ON D.CD_DOCUMENTO_CLINICO = P.CD_DOCUMENTO_CLINICO
             WHERE P.CD_ATENDIMENTO = :cd_atendimento
               AND D.CD_TIPO_DOCUMENTO = 36
             ORDER BY P.DH_CRIACAO DESC
            """
        ),
        {'cd_atendimento': cd_atendimento},
    )
    return [_row_to_evolucao(row) for row in result.mappings()]


def criar_rascunho(
    session: Session,
    contexto: ContextoEvolucaoMv,
    configuracao: ConfiguracaoEvolucaoMv,
) -> EvolucaoMv:
    ids = session.execute(
        text(
            """
            SELECT DBAMV.SEQ_PRE_MED.NEXTVAL CD_PRE_MED,
                   DBAMV.SEQ_PW_DOCUMENTO_CLINICO.NEXTVAL
                       CD_DOCUMENTO_CLINICO
              FROM DUAL
            """
        )
    ).mappings().one()
    cd_pre_med = ids['cd_pre_med']
    cd_documento_clinico = ids['cd_documento_clinico']

    try:
        session.execute(
            text(
                """
                INSERT INTO DBAMV.PW_DOCUMENTO_CLINICO (
                    CD_DOCUMENTO_CLINICO,
                    CD_TIPO_DOCUMENTO,
                    TP_STATUS,
                    DH_REFERENCIA,
                    DH_CRIACAO,
                    DH_FECHAMENTO,
                    DH_IMPRESSO,
                    CD_PACIENTE,
                    CD_PRESTADOR,
                    CD_ATENDIMENTO,
                    CD_USUARIO,
                    CD_OBJETO,
                    DH_DOCUMENTO
                ) VALUES (
                    :cd_documento_clinico,
                    :cd_tipo_documento,
                    'ABERTO',
                    TRUNC(SYSDATE, 'MI'),
                    SYSDATE,
                    NULL,
                    NULL,
                    :cd_paciente,
                    :cd_prestador,
                    :cd_atendimento,
                    :usuario_mv,
                    :cd_objeto,
                    SYSDATE
                )
                """
            ),
            {
                'cd_documento_clinico': cd_documento_clinico,
                'cd_tipo_documento': configuracao.cd_tipo_documento,
                'cd_paciente': contexto.cd_paciente,
                'cd_prestador': contexto.cd_prestador,
                'cd_atendimento': contexto.cd_atendimento,
                'usuario_mv': configuracao.usuario_mv,
                'cd_objeto': configuracao.cd_objeto,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO DBAMV.PRE_MED (
                    CD_PRE_MED,
                    CD_ATENDIMENTO,
                    CD_PRESTADOR,
                    CD_UNID_INT,
                    DT_PRE_MED,
                    HR_PRE_MED,
                    DS_EVOLUCAO,
                    CD_ID_USUARIO,
                    CD_SOLSAI_PRO,
                    SN_FECHADO,
                    SN_RN,
                    DT_VALIDADE,
                    FL_PRINCIPAL,
                    FL_IMPRESSO,
                    TP_PRE_MED,
                    NM_USUARIO,
                    CD_SETOR,
                    DT_REFERENCIA,
                    SN_TRANSCRICAO,
                    DH_CRIACAO,
                    CD_PRE_PAD,
                    CD_OBJETO,
                    CD_DOCUMENTO_CLINICO,
                    SN_PRESCRICAO_DIA_SEGUINTE
                ) VALUES (
                    :cd_pre_med,
                    :cd_atendimento,
                    :cd_prestador,
                    NULL,
                    TRUNC(SYSDATE),
                    TRUNC(SYSDATE, 'MI'),
                    NULL,
                    :cd_id_usuario,
                    NULL,
                    'N',
                    'N',
                    TRUNC(SYSDATE + 1) + (:hora_validade / 24),
                    'S',
                    'N',
                    'M',
                    :usuario_mv,
                    NULL,
                    TRUNC(SYSDATE),
                    'N',
                    SYSDATE,
                    NULL,
                    :cd_objeto,
                    :cd_documento_clinico,
                    'N'
                )
                """
            ),
            {
                'cd_pre_med': cd_pre_med,
                'cd_atendimento': contexto.cd_atendimento,
                'cd_prestador': contexto.cd_prestador,
                'cd_id_usuario': contexto.cd_id_usuario,
                'hora_validade': configuracao.hora_validade,
                'usuario_mv': configuracao.usuario_mv,
                'cd_objeto': configuracao.cd_objeto,
                'cd_documento_clinico': cd_documento_clinico,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return obter_evolucao(session, cd_pre_med)


def salvar_texto(
    session: Session,
    cd_pre_med: int,
    texto_evolucao: str,
    atendimentos_permitidos: set[int],
) -> EvolucaoMv:
    row = _mapping_one_or_none(
        session.execute(
            text(
                """
                SELECT P.CD_ATENDIMENTO,
                       P.FL_IMPRESSO,
                       D.TP_STATUS
                  FROM DBAMV.PRE_MED P
                  JOIN DBAMV.PW_DOCUMENTO_CLINICO D
                    ON D.CD_DOCUMENTO_CLINICO = P.CD_DOCUMENTO_CLINICO
                 WHERE P.CD_PRE_MED = :cd_pre_med
                   FOR UPDATE NOWAIT
                """
            ),
            {'cd_pre_med': cd_pre_med},
        )
    )
    if row is None:
        session.rollback()
        raise EvolucaoMvNaoEncontrada('Evolucao nao encontrada no MV.')
    if row['cd_atendimento'] not in atendimentos_permitidos:
        session.rollback()
        raise EvolucaoMvConflito(
            'Evolucao fora dos atendimentos permitidos para teste.'
        )
    if row['fl_impresso'] == 'S' or row['tp_status'] != 'ABERTO':
        session.rollback()
        raise EvolucaoMvConflito(
            'Evolucao fechada nao pode receber alteracao de texto.'
        )

    try:
        result = session.execute(
            text(
                """
                UPDATE DBAMV.PRE_MED
                   SET DS_EVOLUCAO = :texto_evolucao
                 WHERE CD_PRE_MED = :cd_pre_med
                """
            ),
            {
                'texto_evolucao': texto_evolucao,
                'cd_pre_med': cd_pre_med,
            },
        )
        if result.rowcount != 1:
            raise EvolucaoMvConflito(
                'O MV nao confirmou a gravacao do texto.'
            )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return obter_evolucao(session, cd_pre_med)


def assinar_evolucao(
    session: Session,
    cd_pre_med: int,
    usuario_mv: str,
    atendimentos_permitidos: set[int],
) -> EvolucaoMv:
    row = _mapping_one_or_none(
        session.execute(
            text(
                """
                SELECT P.CD_ATENDIMENTO,
                       P.CD_DOCUMENTO_CLINICO,
                       P.FL_IMPRESSO,
                       P.DS_EVOLUCAO TEXTO,
                       D.TP_STATUS
                  FROM DBAMV.PRE_MED P
                  JOIN DBAMV.PW_DOCUMENTO_CLINICO D
                    ON D.CD_DOCUMENTO_CLINICO = P.CD_DOCUMENTO_CLINICO
                 WHERE P.CD_PRE_MED = :cd_pre_med
                   FOR UPDATE NOWAIT
                """
            ),
            {'cd_pre_med': cd_pre_med},
        )
    )
    if row is None:
        session.rollback()
        raise EvolucaoMvNaoEncontrada('Evolucao nao encontrada no MV.')
    if row['cd_atendimento'] not in atendimentos_permitidos:
        session.rollback()
        raise EvolucaoMvConflito(
            'Evolucao fora dos atendimentos permitidos para teste.'
        )
    if row['fl_impresso'] == 'S' or row['tp_status'] != 'ABERTO':
        session.rollback()
        raise EvolucaoMvConflito('Evolucao ja esta fechada.')
    if row['texto'] is None or not str(row['texto']).strip():
        session.rollback()
        raise EvolucaoMvConflito(
            'A evolucao precisa ter texto antes do fechamento.'
        )

    try:
        documento = session.execute(
            text(
                """
                UPDATE DBAMV.PW_DOCUMENTO_CLINICO
                   SET TP_STATUS = 'FECHADO',
                       DH_FECHAMENTO = SYSDATE,
                       CD_USUARIO_AUTORIZADOR = :usuario_mv
                 WHERE CD_DOCUMENTO_CLINICO = :cd_documento_clinico
                   AND TP_STATUS = 'ABERTO'
                """
            ),
            {
                'usuario_mv': usuario_mv,
                'cd_documento_clinico': row['cd_documento_clinico'],
            },
        )
        evolucao = session.execute(
            text(
                """
                UPDATE DBAMV.PRE_MED
                   SET SN_FECHADO = 'S',
                       FL_IMPRESSO = 'S',
                       DH_IMPRESSAO = SYSDATE
                 WHERE CD_PRE_MED = :cd_pre_med
                   AND FL_IMPRESSO = 'N'
                """
            ),
            {'cd_pre_med': cd_pre_med},
        )
        if documento.rowcount != 1 or evolucao.rowcount != 1:
            raise EvolucaoMvConflito(
                'O MV nao confirmou o fechamento integral da evolucao.'
            )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return obter_evolucao(session, cd_pre_med)
