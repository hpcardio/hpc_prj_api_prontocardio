from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app_prontocardio.central_atendimento_models import (
    CentralAtendimentoEvento,
    CentralAtendimentoTentativa,
)
from app_prontocardio.central_atendimento_schema import (
    CentralCommand,
    CommandResponse,
    TentativaEstado,
)
from app_prontocardio.services.central_atendimento_rules import (
    RULE_VERSION,
    ValidationFacts,
    canonical_command_hash,
    validate_command,
)


class ConcurrentRevision(RuntimeError):
    pass


class AttemptNotFound(RuntimeError):
    pass


class CentralCommandService:
    def __init__(self, session: Session):
        self._session = session

    def prevalidate(
        self,
        command: CentralCommand,
        *,
        operator_id: int,
        facts: ValidationFacts,
    ) -> CommandResponse:
        attempt, replay, conflict = self._reserve(command, operator_id)
        if conflict:
            return self._conflict_response(command.attempt_id)
        if replay:
            return self._response(attempt, replay=True)

        decision = validate_command(command, facts)
        if not facts.source_fresh:
            new_state = TentativaEstado.FALHA_LEITURA
        else:
            new_state = TentativaEstado.PRE_VALIDADA
        reason_code = (
            decision.reason_codes[0]
            if decision.reason_codes
            else 'PREVALIDATION_ACCEPTED'
        )
        attempt = self._transition_and_audit(
            attempt,
            operator_id=operator_id,
            route='/central-atendimento/pre-validar',
            new_state=new_state,
            result_code=reason_code,
        )
        self._session.commit()
        return self._response(attempt, reason_code=reason_code)

    def open(
        self,
        command: CentralCommand,
        *,
        operator_id: int,
        facts: ValidationFacts,
    ) -> CommandResponse:
        attempt, replay, conflict = self._reserve(command, operator_id)
        if conflict:
            return self._conflict_response(command.attempt_id)
        if replay:
            return self._response(attempt, replay=True)

        validate_command(command, facts)
        attempt = self._transition_and_audit(
            attempt,
            operator_id=operator_id,
            route='/central-atendimento/abrir',
            new_state=TentativaEstado.BLOQUEADA_ESCRITA,
            result_code='MV_WRITE_DISABLED',
        )
        self._session.commit()
        return self._response(attempt, reason_code='MV_WRITE_DISABLED')

    def resume(self, attempt_id: str, *, operator_id: int) -> CommandResponse:
        attempt = self._find(attempt_id)
        if attempt is None:
            raise AttemptNotFound(attempt_id)
        attempt = self._transition_and_audit(
            attempt,
            operator_id=operator_id,
            route=f'/central-atendimento/tentativas/{attempt_id}/retomar',
            new_state=TentativaEstado.BLOQUEADA_ESCRITA,
            result_code='MV_WRITE_DISABLED',
        )
        self._session.commit()
        return self._response(attempt, reason_code='MV_WRITE_DISABLED')

    def transition(
        self,
        attempt_id: str,
        *,
        expected_revision: int,
        new_state: str,
    ) -> CentralAtendimentoTentativa:
        statement = (
            update(CentralAtendimentoTentativa)
            .where(CentralAtendimentoTentativa.attempt_id == attempt_id)
            .where(CentralAtendimentoTentativa.revision == expected_revision)
            .values(
                state=new_state,
                revision=expected_revision + 1,
            )
        )
        result = self._session.execute(statement)
        if result.rowcount != 1:
            raise ConcurrentRevision(attempt_id)
        self._session.flush()
        attempt = self._find(attempt_id)
        if attempt is None:
            raise AttemptNotFound(attempt_id)
        self._session.refresh(attempt)
        return attempt

    def _reserve(
        self,
        command: CentralCommand,
        operator_id: int,
    ) -> tuple[CentralAtendimentoTentativa, bool, bool]:
        attempt_id = str(command.attempt_id)
        command_hash = canonical_command_hash(command)
        existing = self._find(attempt_id)
        if existing is not None:
            return existing, existing.command_hash == command_hash, (
                existing.command_hash != command_hash
            )

        attempt = CentralAtendimentoTentativa(
            attempt_id=attempt_id,
            command_hash=command_hash,
            state=TentativaEstado.RECEBIDA.value,
            rule_version=RULE_VERSION,
            unit_code=command.unit_code,
            attendance_type=command.attendance_type.value,
        )
        self._session.add(attempt)
        self._session.flush()
        self._audit(
            attempt,
            operator_id=operator_id,
            route='/central-atendimento/tentativas',
            previous_state=None,
            new_state=TentativaEstado.RECEBIDA,
            result_code='ATTEMPT_RECEIVED',
        )
        return attempt, False, False

    def _transition_and_audit(
        self,
        attempt: CentralAtendimentoTentativa,
        *,
        operator_id: int,
        route: str,
        new_state: TentativaEstado,
        result_code: str,
    ) -> CentralAtendimentoTentativa:
        previous_state = attempt.state
        updated = self.transition(
            attempt.attempt_id,
            expected_revision=attempt.revision,
            new_state=new_state.value,
        )
        self._audit(
            updated,
            operator_id=operator_id,
            route=route,
            previous_state=previous_state,
            new_state=new_state,
            result_code=result_code,
        )
        return updated

    def _audit(  # noqa: PLR0913 - explicit sanitized audit fields
        self,
        attempt: CentralAtendimentoTentativa,
        *,
        operator_id: int,
        route: str,
        previous_state: str | None,
        new_state: TentativaEstado,
        result_code: str,
    ) -> None:
        self._session.add(
            CentralAtendimentoEvento(
                attempt_id=attempt.attempt_id,
                operator_id=operator_id,
                route=route,
                previous_state=previous_state,
                state_new=new_state.value,
                result_code=result_code,
                unit_code=attempt.unit_code,
                attendance_type=attempt.attendance_type,
            )
        )

    def _find(self, attempt_id: str) -> CentralAtendimentoTentativa | None:
        return self._session.scalar(
            select(CentralAtendimentoTentativa).where(
                CentralAtendimentoTentativa.attempt_id == attempt_id
            )
        )

    @staticmethod
    def _conflict_response(attempt_id: UUID) -> CommandResponse:
        return CommandResponse(
            attempt_id=attempt_id,
            state=TentativaEstado.CONFLITO,
            replay=False,
            reason_code='ATTEMPT_HASH_CONFLICT',
        )

    @staticmethod
    def _response(
        attempt: CentralAtendimentoTentativa,
        *,
        replay: bool = False,
        reason_code: str | None = None,
    ) -> CommandResponse:
        return CommandResponse(
            attempt_id=UUID(attempt.attempt_id),
            state=TentativaEstado(attempt.state),
            replay=replay,
            reason_code=reason_code or attempt.state,
        )
