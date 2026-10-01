from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from evolucao_sadt_backend.lc_dac_support import (
    extract_numeric_result,
    load_lc_dac_support,
)

SUPPORTED_LINES = {'LC-DAC', 'LC-IC', 'LC-PREVENCAO'}


def _date(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value) if value else None


def _exam(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    numeric = extract_numeric_result(value.get('valor'))
    if numeric is None:
        return None
    return {
        'valor': numeric,
        'data': _date(value.get('data')),
        'unidade': value.get('unidade'),
        'origem': value.get('fonte') or value.get('origem') or 'MV',
    }


def _generic_support(raw: dict[str, Any], line_code: str) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    exams: dict[str, dict[str, Any]] = {}

    prescription = raw.get('itens_ultima_prescricao')
    if isinstance(prescription, list) and prescription:
        fields['prescricao_atual'] = {
            'valor': [str(item) for item in prescription if item],
            'origem': 'MV - última prescrição ativa',
        }

    for source_key, target_key in (
        ('ldl', 'ldl'),
        ('hba1c', 'hba1c'),
    ):
        item = _exam(raw.get(source_key))
        if item:
            exams[target_key] = item

    aliases = {
        'Hemoglobina': 'hemoglobina',
        'Hematócrito': 'hematocrito',
        'Perfil lipídico': 'perfil_lipidico',
        'Glicemia': 'glicemia',
        'HbA1c': 'hba1c',
        'Ureia': 'ureia',
        'Creatinina': 'creatinina',
        'Sódio': 'sodio',
        'Potássio': 'potassio',
        'TGO': 'tgo',
        'TGP': 'tgp',
    }
    for source_name, value in (raw.get('exames_protocolares') or {}).items():
        item = _exam(value)
        target = aliases.get(source_name)
        if item and target and target not in exams:
            exams[target] = item

    for exam_key, field_key in (
        ('ldl', 'ldl_resultado'),
        ('hba1c', 'hba1c_resultado'),
        ('creatinina', 'creatinina'),
        ('sodio', 'sodio'),
        ('potassio', 'potassio'),
    ):
        if exam_key in exams:
            exam = exams[exam_key]
            fields[field_key] = {
                'valor': exam['valor'],
                'data': exam['data'],
                'origem': exam['origem'],
            }

    return {
        'cd_atendimento': raw.get('cd_atendimento'),
        'linha_cuidado': line_code,
        'campos': fields,
        'exames': exams,
        'alertas': [],
    }


def load_care_support(
    session: Session,
    cd_atendimento: int,
    line_code: str,
) -> dict[str, Any]:
    if line_code not in SUPPORTED_LINES:
        raise ValueError('Linha de cuidado inválida.')
    raw = load_lc_dac_support(session, cd_atendimento)
    if line_code == 'LC-DAC':
        return raw
    return _generic_support(raw, line_code)
