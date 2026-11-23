import hashlib
import json
from dataclasses import dataclass

from app_prontocardio.central_atendimento_schema import CentralCommand

RULE_VERSION = 'central-readonly-v1'


@dataclass(frozen=True, slots=True)
class ValidationFacts:
    source_fresh: bool
    source_matches_type: bool
    appointment_cancelled: bool
    ticket_ambiguous: bool
    account_closed: bool
    bed_occupied: bool
    attendance_exists: bool
    official_plan_code: int | None


@dataclass(frozen=True, slots=True)
class RuleDecision:
    accepted: bool
    reason_codes: tuple[str, ...]
    rule_version: str = RULE_VERSION


def canonical_command_hash(command: CentralCommand) -> str:
    payload = command.model_dump(
        mode='json',
        exclude={'attempt_id'},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


def scope_matches(
    unit_code: str,
    attendance_type: str,
    scopes: frozenset[str],
) -> bool:
    return f'{unit_code}:{attendance_type}' in scopes


def validate_command(
    command: CentralCommand,
    facts: ValidationFacts,
) -> RuleDecision:
    reasons: list[str] = []
    checks = (
        (not facts.source_fresh, 'SOURCE_STALE'),
        (not facts.source_matches_type, 'SOURCE_TYPE_DIVERGENT'),
        (facts.appointment_cancelled, 'APPOINTMENT_CANCELLED'),
        (facts.ticket_ambiguous, 'TICKET_AMBIGUOUS'),
        (facts.account_closed, 'ACCOUNT_CLOSED'),
        (facts.bed_occupied, 'BED_OCCUPIED'),
        (facts.attendance_exists, 'ATTENDANCE_EXISTS'),
        (command.plan_code != facts.official_plan_code, 'PLAN_DIVERGENT'),
    )
    reasons.extend(code for failed, code in checks if failed)
    return RuleDecision(accepted=not reasons, reason_codes=tuple(reasons))
