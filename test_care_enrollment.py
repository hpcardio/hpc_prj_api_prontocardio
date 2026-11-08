from types import SimpleNamespace

import pytest

from evolucao_sadt_backend.care_enrollment import (
    CareEnrollmentStore,
    resolve_patient_from_attendance,
)
from evolucao_sadt_backend.mv_context_token import MvContextClaims, MvContextTokenError


def claims(attendance=339070):
    return MvContextClaims(
        cd_atendimento=attendance,
        cd_usuario='MEDICO.TESTE',
        cd_prestador=781,
        crm='CRM-CE 00000',
        expires_at=4102444800,
    )


def test_tables_are_created_idempotently_and_upsert_is_audited(tmp_path):
    store = CareEnrollmentStore(tmp_path / 'care.sqlite3')
    store.initialize()
    store.initialize()
    changed = store.upsert(
        patient_id=99902,
        attendance_id=339070,
        user='MEDICO.TESTE',
        entries=[{'linha': 'LC-IC', 'ativa': True, 'data_ancora': '2026-10-01', 'origem': 'MEDICO'}],
    )
    assert changed == 1
    assert store.list_for_patient(99902) == [{
        'linha': 'LC-IC', 'ativa': True, 'data_ancora': '2026-10-01', 'origem': 'MEDICO',
    }]
    assert store.audit_count() == 1


def test_repeated_put_is_idempotent_without_duplicate_audit(tmp_path):
    store = CareEnrollmentStore(tmp_path / 'care.sqlite3')
    entry = [{'linha': 'LC-DAC', 'ativa': True, 'data_ancora': None, 'origem': 'EVOLUCAO'}]
    assert store.upsert(10, 20, 'MEDICO.TESTE', entry) == 1
    assert store.upsert(10, 20, 'MEDICO.TESTE', entry) == 0
    assert store.audit_count() == 1


def test_invalid_line_is_rejected(tmp_path):
    store = CareEnrollmentStore(tmp_path / 'care.sqlite3')
    with pytest.raises(ValueError, match='Linha de Cuidado inválida'):
        store.upsert(10, 20, 'MEDICO.TESTE', [{'linha': 'LC-FAKE', 'ativa': True}])


def test_patient_is_resolved_only_from_signed_attendance_context():
    resolved = resolve_patient_from_attendance(
        339070,
        claims(),
        lambda attendance: SimpleNamespace(cd_paciente=99902, cd_atendimento=attendance),
    )
    assert resolved == 99902
    with pytest.raises(MvContextTokenError, match='outro atendimento'):
        resolve_patient_from_attendance(
            999,
            claims(),
            lambda attendance: SimpleNamespace(cd_paciente=1, cd_atendimento=attendance),
        )

