"""Repasse manual SUS: procedimento-paciente ou paciente-procedimento.

Recuperado de produção (imagem manual-sus-diego-prontorede-20261123). Os nomes
de pacientes do teste original foram trocados por nomes fictícios e o teste
passou a importar a função do repositório (antes lia /app no container).
"""

from pathlib import Path

from app_prontocardio.routers.repasse_medico import (
    _parse_manual_repasse_description as parse,
)

SOURCE = (
    Path(__file__).resolve().parents[1]
    / 'app_prontocardio' / 'routers' / 'repasse_medico.py'
)


def test_preserves_procedure_first_manual_description():
    assert parse(
        'ECODOPPLERCARDIOGRAMA TRANSTORACICO-PACIENTE FICTICIO UM-SUS-SETEMBRO'
    ) == {
        'procedimento': 'ECODOPPLERCARDIOGRAMA TRANSTORACICO',
        'paciente': 'PACIENTE FICTICIO UM',
        'convenio': 'SUS',
    }


def test_accepts_patient_first_manual_description():
    assert parse(
        'PACIENTE FICTICIO DOIS-DOPPLER DE CAROTIDAS-SUS-SETEMBRO'
    ) == {
        'procedimento': 'DOPPLER DE CAROTIDAS',
        'paciente': 'PACIENTE FICTICIO DOIS',
        'convenio': 'SUS',
    }
    assert parse(
        'PACIENTE FICTICIO TRES-ECOCARDIOGRAMA TRANSTORACICO-SUS-SETEMBRO'
    ) == {
        'procedimento': 'ECOCARDIOGRAMA TRANSTORACICO',
        'paciente': 'PACIENTE FICTICIO TRES',
        'convenio': 'SUS',
    }


def test_manual_repass_date_is_not_used_as_attendance_date():
    source = SOURCE.read_text(encoding='utf-8')

    assert "'data_atendimento': None" in source
    assert "'data_repasse': row.get('dt_repasse_manual')" in source
