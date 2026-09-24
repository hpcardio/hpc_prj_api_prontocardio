"""Persistência auditável da origem de agendamentos, sem alterar o MV."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Sequence

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_prontocardio.models import (
    AgendamentoOrigem,
    AgendamentoOrigemEvento,
    Usuario,
)
from app_prontocardio.routers.paciente_auth import PrincipalPaciente


class OrigemAgendamento(str, Enum):
    MEU_PRONTOCARDIO = 'MEU_PRONTOCARDIO'
    PRONTOREDE = 'PRONTOREDE'
    PRONTOCHECKUP = 'PRONTOCHECKUP'
    CALL_CENTER = 'CALL_CENTER'
    MV = 'MV'
    NAO_IDENTIFICADA = 'NAO_IDENTIFICADA'


class EvidenciaOrigem(str, Enum):
    GRAVACAO_DIRETA = 'GRAVACAO_DIRETA'
    HERDADA_REMARCACAO = 'HERDADA_REMARCACAO'
    RECONCILIACAO_SLOT = 'RECONCILIACAO_SLOT'
    RECONCILIACAO_PROTOCOLO = 'RECONCILIACAO_PROTOCOLO'
    MARCADOR_EXPLICITO = 'MARCADOR_EXPLICITO'


def origem_do_principal(
    principal: Usuario | PrincipalPaciente,
) -> OrigemAgendamento:
    if isinstance(principal, PrincipalPaciente):
        return OrigemAgendamento.MEU_PRONTOCARDIO
    try:
        return OrigemAgendamento(principal.origem_agendamento)
    except (AttributeError, ValueError):
        return OrigemAgendamento.NAO_IDENTIFICADA


@dataclass(frozen=True)
class RegistroOrigemInput:
    cd_it_agenda_central: int
    cd_paciente: int
    cd_agenda_central: int | None
    protocolo_mv: int
    origem: OrigemAgendamento
    evidencia: EvidenciaOrigem
    referencia_externa: str | None
    registrada_por_id: int | None
    registrada_por_nome: str
    slot_origem_anterior: int | None = None
    protocolo_origem_anterior: int | None = None


@dataclass(frozen=True)
class RegistroOrigemResultado:
    status: Literal[
        'criado',
        'inalterado',
        'conflito',
        'registro_desabilitado',
        'pendente_protocolo',
    ]
    origem: OrigemAgendamento


class AgendamentoOrigemService:
    def __init__(self, session: Session):
        self.session = session

    def registrar(
        self,
        entrada: RegistroOrigemInput,
        *,
        finalizar_transacao: bool = True,
    ) -> RegistroOrigemResultado:
        try:
            return self._registrar_bloqueado(entrada, finalizar_transacao)
        except IntegrityError:
            if not finalizar_transacao:
                raise
            self.session.rollback()
            return self._registrar_bloqueado(entrada, finalizar_transacao)

    def _registrar_bloqueado(
        self,
        entrada: RegistroOrigemInput,
        finalizar_transacao: bool,
    ) -> RegistroOrigemResultado:
        if (
            entrada.cd_it_agenda_central <= 0
            or entrada.protocolo_mv is None
            or entrada.protocolo_mv <= 0
        ):
            raise ValueError('slot e protocolo MV autoritativos são obrigatórios')
        if (entrada.slot_origem_anterior is None) != (
            entrada.protocolo_origem_anterior is None
        ):
            raise ValueError(
                'slot e protocolo anteriores devem identificar a mesma instância'
            )
        atual = self.session.scalar(
            select(AgendamentoOrigem)
            .where(
                AgendamentoOrigem.cd_it_agenda_central
                == entrada.cd_it_agenda_central,
                AgendamentoOrigem.protocolo_mv == entrada.protocolo_mv,
            )
            .with_for_update()
        )
        if atual is None:
            atual = AgendamentoOrigem(
                cd_it_agenda_central=entrada.cd_it_agenda_central,
                cd_paciente=entrada.cd_paciente,
                cd_agenda_central=entrada.cd_agenda_central,
                protocolo_mv=entrada.protocolo_mv,
                origem=entrada.origem.value,
                evidencia=entrada.evidencia.value,
                referencia_externa=entrada.referencia_externa,
                slot_origem_anterior=entrada.slot_origem_anterior,
                protocolo_origem_anterior=(
                    entrada.protocolo_origem_anterior
                ),
                registrada_por_id=entrada.registrada_por_id,
                registrada_por_nome=entrada.registrada_por_nome,
            )
            self.session.add(atual)
            self.session.flush()
            self._evento(entrada, anterior=None, resultado='criado')
            if finalizar_transacao:
                self.session.commit()
            return RegistroOrigemResultado('criado', entrada.origem)

        origem_atual = OrigemAgendamento(atual.origem)
        identidade_divergente = (
            atual.cd_paciente != entrada.cd_paciente
            or atual.cd_agenda_central != entrada.cd_agenda_central
        )
        if not identidade_divergente and entrada.origem in {
            origem_atual,
            OrigemAgendamento.NAO_IDENTIFICADA,
        }:
            if finalizar_transacao:
                self.session.rollback()
            return RegistroOrigemResultado('inalterado', origem_atual)

        conflito_existente = self.session.scalar(
            select(AgendamentoOrigemEvento.id).where(
                AgendamentoOrigemEvento.cd_it_agenda_central
                == entrada.cd_it_agenda_central,
                AgendamentoOrigemEvento.protocolo_mv == entrada.protocolo_mv,
                AgendamentoOrigemEvento.origem_anterior
                == origem_atual.value,
                AgendamentoOrigemEvento.origem_nova == entrada.origem.value,
                AgendamentoOrigemEvento.evidencia == entrada.evidencia.value,
                AgendamentoOrigemEvento.resultado == 'conflito',
                AgendamentoOrigemEvento.registrada_por_id
                == entrada.registrada_por_id,
                AgendamentoOrigemEvento.registrada_por_nome
                == entrada.registrada_por_nome,
            )
        )
        if conflito_existente is not None:
            if finalizar_transacao:
                self.session.rollback()
            return RegistroOrigemResultado('conflito', origem_atual)

        self._evento(
            entrada,
            anterior=origem_atual,
            resultado='conflito',
            justificativa=(
                'Identidade MV divergente; valor atual preservado.'
                if identidade_divergente
                else 'Origem confirmada divergente; valor atual preservado.'
            ),
        )
        if finalizar_transacao:
            self.session.commit()
        return RegistroOrigemResultado('conflito', origem_atual)

    def herdar(  # noqa: PLR0913 - public contract mirrors the rescheduling data.
        self,
        *,
        slot_anterior: int,
        protocolo_anterior: int,
        slot_novo: int,
        cd_paciente: int,
        cd_agenda_central: int | None,
        protocolo_mv: int,
        registrada_por_id: int | None,
        registrada_por_nome: str,
    ) -> RegistroOrigemResultado:
        anterior = self.session.get(
            AgendamentoOrigem, (slot_anterior, protocolo_anterior)
        )
        if anterior is None:
            raise ValueError('instância anterior não encontrada para herança')
        origem = OrigemAgendamento(anterior.origem)
        return self.registrar(
            RegistroOrigemInput(
                cd_it_agenda_central=slot_novo,
                cd_paciente=cd_paciente,
                cd_agenda_central=cd_agenda_central,
                protocolo_mv=protocolo_mv,
                origem=origem,
                evidencia=EvidenciaOrigem.HERDADA_REMARCACAO,
                referencia_externa=(
                    anterior.referencia_externa
                ),
                registrada_por_id=registrada_por_id,
                registrada_por_nome=registrada_por_nome,
                slot_origem_anterior=slot_anterior,
                protocolo_origem_anterior=protocolo_anterior,
            )
        )

    def buscar_por_instancias(
        self, instancias: Sequence[tuple[int, int]]
    ) -> dict[tuple[int, int], AgendamentoOrigem]:
        chaves = {(int(slot), int(protocolo)) for slot, protocolo in instancias}
        if not chaves:
            return {}
        rows = self.session.scalars(
            select(AgendamentoOrigem).where(
                tuple_(
                    AgendamentoOrigem.cd_it_agenda_central,
                    AgendamentoOrigem.protocolo_mv,
                ).in_(chaves)
            )
        )
        return {
            (row.cd_it_agenda_central, row.protocolo_mv): row for row in rows
        }

    def listar_instancias_por_origem(
        self,
        *,
        cd_paciente: int,
        origem: OrigemAgendamento,
    ) -> list[tuple[int, int]]:
        """Retorna identidades completas para filtro de leitura interna."""
        return [
            (int(row[0]), int(row[1]))
            for row in self.session.execute(
                select(
                    AgendamentoOrigem.cd_it_agenda_central,
                    AgendamentoOrigem.protocolo_mv,
                ).where(
                    AgendamentoOrigem.cd_paciente == cd_paciente,
                    AgendamentoOrigem.origem == origem.value,
                )
            )
        ]

    def listar_instancias_identificadas(
        self,
        *,
        cd_paciente: int,
    ) -> list[tuple[int, int]]:
        """Retorna instâncias que não pertencem ao filtro não identificada."""
        return [
            (int(row[0]), int(row[1]))
            for row in self.session.execute(
                select(
                    AgendamentoOrigem.cd_it_agenda_central,
                    AgendamentoOrigem.protocolo_mv,
                ).where(
                    AgendamentoOrigem.cd_paciente == cd_paciente,
                    AgendamentoOrigem.origem
                    != OrigemAgendamento.NAO_IDENTIFICADA.value,
                )
            )
        ]

    def _evento(
        self,
        entrada: RegistroOrigemInput,
        *,
        anterior: OrigemAgendamento | None,
        resultado: Literal['criado', 'conflito'],
        justificativa: str = 'Registro inicial da origem.',
    ) -> None:
        self.session.add(
            AgendamentoOrigemEvento(
                cd_it_agenda_central=entrada.cd_it_agenda_central,
                protocolo_mv=entrada.protocolo_mv,
                origem_anterior=anterior.value if anterior else None,
                origem_nova=entrada.origem.value,
                evidencia=entrada.evidencia.value,
                resultado=resultado,
                justificativa=justificativa,
                registrada_por_id=entrada.registrada_por_id,
                registrada_por_nome=entrada.registrada_por_nome,
            )
        )
