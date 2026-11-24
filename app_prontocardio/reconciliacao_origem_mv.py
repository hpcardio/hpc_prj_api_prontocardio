# ruff: noqa
# Recuperado para o Git em 24/11/2026: em produção desde 23/11 fora do
# versionamento (container api-prontocardio-reconciliation-*-v11).
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


class AlteracaoOrigemMvError(RuntimeError):
    pass


class ConflitoOrigemMv(AlteracaoOrigemMvError):
    pass


class ProtecaoFinanceiraOrigemMv(AlteracaoOrigemMvError):
    pass


class OrigemMvInvalida(AlteracaoOrigemMvError):
    pass


def _valor(mapping: object, nome: str):
    if mapping is None:
        return None
    return mapping.get(nome, mapping.get(nome.lower()))


def validar_protecao_financeira(
    *,
    conta_fechada: bool | None,
    codigo_remessa: int | None,
) -> None:
    if conta_fechada is None:
        raise ProtecaoFinanceiraOrigemMv(
            'Situação financeira indeterminada; alteração bloqueada.'
        )
    if conta_fechada:
        raise ProtecaoFinanceiraOrigemMv(
            'Conta fechada; alteração bloqueada pelo faturamento.'
        )
    if codigo_remessa is not None:
        raise ProtecaoFinanceiraOrigemMv(
            'Conta vinculada a remessa; alteração bloqueada pelo faturamento.'
        )


def _conta_fechada(valor: object) -> bool | None:
    if valor is None:
        return None
    normalizado = str(valor).strip().upper()
    if normalizado in {'S', 'SIM', '1', 'TRUE', 'FECHADA'}:
        return True
    if normalizado in {'N', 'NAO', 'NÃO', '0', 'FALSE', 'ABERTA'}:
        return False
    return None


def _validar_contexto(
    session: Session,
    *,
    atendimento: int,
    origem_anterior: int,
    origem_nova: int,
    bloquear: bool,
) -> None:
    sufixo_bloqueio = ' FOR UPDATE' if bloquear else ''
    atual = session.execute(text(f'''
        SELECT a.CD_ORI_ATE, a.CD_MULTI_EMPRESA
          FROM DBAMV.ATENDIME a
         WHERE a.CD_ATENDIMENTO = :atendimento{sufixo_bloqueio}
    '''), {'atendimento': atendimento}).mappings().one_or_none()
    if atual is None:
        raise OrigemMvInvalida('Atendimento não encontrado no MV.')
    if int(_valor(atual, 'CD_ORI_ATE')) != origem_anterior:
        raise ConflitoOrigemMv(
            'A origem foi alterada por outro usuário. Atualize a tela.'
        )

    destino = session.execute(text('''
        SELECT o.CD_ORI_ATE, o.SN_ATIVO, o.CD_MULTI_EMPRESA
          FROM DBAMV.ORI_ATE o
         WHERE o.CD_ORI_ATE = :origem_nova
    '''), {'origem_nova': origem_nova}).mappings().one_or_none()
    if destino is None or str(_valor(destino, 'SN_ATIVO')).upper() != 'S':
        raise OrigemMvInvalida('A origem selecionada não está ativa no MV.')
    if int(_valor(destino, 'CD_MULTI_EMPRESA')) != int(_valor(atual, 'CD_MULTI_EMPRESA')):
        raise OrigemMvInvalida('A origem selecionada pertence a outra empresa.')

    financeiro = session.execute(text('''
        SELECT
            CASE
              WHEN COUNT(*) = 0 THEN NULL
              WHEN MAX(CASE WHEN UPPER(TRIM(x.SN_FECHADA)) IN
                   ('S','SIM','1','TRUE','FECHADA') THEN 1 ELSE 0 END) = 1
              THEN 'S' ELSE 'N'
            END CONTA_FECHADA,
            MAX(x.CD_REMESSA) CD_REMESSA
          FROM (
            SELECT r.SN_FECHADA, r.CD_REMESSA
              FROM DBAMV.REG_AMB r
              JOIN DBAMV.ITREG_AMB i ON i.CD_REG_AMB = r.CD_REG_AMB
             WHERE i.CD_ATENDIMENTO = :atendimento
            UNION ALL
            SELECT r.SN_FECHADA, r.CD_REMESSA
              FROM DBAMV.REG_FAT r
             WHERE r.CD_ATENDIMENTO = :atendimento
          ) x
    '''), {'atendimento': atendimento}).mappings().one_or_none()
    validar_protecao_financeira(
        conta_fechada=_conta_fechada(_valor(financeiro, 'CONTA_FECHADA')),
        codigo_remessa=(
            int(_valor(financeiro, 'CD_REMESSA'))
            if _valor(financeiro, 'CD_REMESSA') is not None else None
        ),
    )


def pre_validar_alteracao_origem_mv(
    session: Session,
    *,
    atendimento: int,
    origem_anterior: int,
    origem_nova: int,
) -> dict[str, int | bool]:
    if origem_anterior == origem_nova:
        raise OrigemMvInvalida('A nova origem deve ser diferente da origem atual.')
    _validar_contexto(
        session,
        atendimento=atendimento,
        origem_anterior=origem_anterior,
        origem_nova=origem_nova,
        bloquear=False,
    )
    session.rollback()
    return {
        'atendimento': atendimento,
        'origem_anterior': origem_anterior,
        'origem_nova': origem_nova,
        'liberada': True,
    }


def aplicar_alteracao_origem_mv(
    session: Session,
    *,
    atendimento: int,
    origem_anterior: int,
    origem_nova: int,
) -> dict[str, int]:
    if origem_anterior == origem_nova:
        raise OrigemMvInvalida('A nova origem deve ser diferente da origem atual.')

    try:
        _validar_contexto(
            session,
            atendimento=atendimento,
            origem_anterior=origem_anterior,
            origem_nova=origem_nova,
            bloquear=True,
        )

        alterado = session.execute(text('''
            UPDATE DBAMV.ATENDIME
               SET CD_ORI_ATE = :origem_nova
             WHERE CD_ATENDIMENTO = :atendimento
               AND CD_ORI_ATE = :origem_anterior
        '''), {
            'atendimento': atendimento,
            'origem_anterior': origem_anterior,
            'origem_nova': origem_nova,
        })
        if alterado.rowcount != 1:
            raise ConflitoOrigemMv(
                'A origem foi alterada por outro usuário. Atualize a tela.'
            )

        confirmado = session.execute(text('''
            SELECT a.CD_ORI_ATE
              FROM DBAMV.ATENDIME a
             WHERE a.CD_ATENDIMENTO = :atendimento
        '''), {'atendimento': atendimento}).mappings().one_or_none()
        if confirmado is None or int(_valor(confirmado, 'CD_ORI_ATE')) != origem_nova:
            raise ConflitoOrigemMv('O MV não confirmou a nova origem.')

        session.commit()
        return {
            'atendimento': atendimento,
            'origem_anterior': origem_anterior,
            'origem_nova': origem_nova,
        }
    except Exception:
        session.rollback()
        raise
