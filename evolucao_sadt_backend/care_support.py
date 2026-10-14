from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from evolucao_sadt_backend.lc_dac_support import (
    extract_numeric_result,
    load_lc_dac_support,
)
from evolucao_sadt_backend.forms_app import latest_previous_form_value

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


def _attendance_identification(
    session: Session,
    cd_atendimento: int,
) -> dict[str, dict[str, Any]]:
    row = session.execute(
        text(
            '''
            SELECT p.NM_PACIENTE AS nm_paciente,
                   a.DT_ATENDIMENTO AS dt_atendimento
              FROM DBAMV.ATENDIME a
              JOIN DBAMV.PACIENTE p
                ON p.CD_PACIENTE = a.CD_PACIENTE
             WHERE a.CD_ATENDIMENTO = :cd_atendimento
            '''
        ),
        {'cd_atendimento': cd_atendimento},
    ).mappings().first()
    if not row:
        return {}
    fields: dict[str, dict[str, Any]] = {}
    patient = str(row.get('nm_paciente') or '').strip()
    consultation_date = _date(row.get('dt_atendimento'))
    if patient:
        fields['paciente'] = {
            'valor': patient,
            'origem': 'MV - atendimento aberto',
        }
    if consultation_date:
        fields['data_consulta'] = {
            'valor': consultation_date[:10],
            'origem': 'MV - atendimento aberto',
        }
    return fields


def _load_latest_receituario_text(
    session: Session,
    cd_atendimento: int,
) -> str | None:
    rows = session.execute(
        text(
            '''
            SELECT rc.DS_VALOR AS ds_valor,
                   DBMS_LOB.SUBSTR(rc.LO_VALOR, 4000, 1) AS lo_valor
              FROM DBAMV.PW_DOCUMENTO_CLINICO dc
              JOIN DBAMV.PW_EDITOR_CLINICO ec
                ON ec.CD_DOCUMENTO_CLINICO = dc.CD_DOCUMENTO_CLINICO
              JOIN DBAMV.EDITOR_REGISTRO_CAMPO rc
                ON rc.CD_REGISTRO = ec.CD_EDITOR_REGISTRO
              JOIN DBAMV.EDITOR_CAMPO c
                ON c.CD_CAMPO = rc.CD_CAMPO
             WHERE dc.CD_ATENDIMENTO = :cd_atendimento
               AND dc.CD_OBJETO = 529
               AND dc.TP_STATUS = 'FECHADO'
               AND dc.CD_DOCUMENTO_CLINICO = (
                    SELECT MAX(dc2.CD_DOCUMENTO_CLINICO)
                      FROM DBAMV.PW_DOCUMENTO_CLINICO dc2
                     WHERE dc2.CD_ATENDIMENTO = :cd_atendimento
                       AND dc2.CD_OBJETO = 529
                       AND dc2.TP_STATUS = 'FECHADO'
               )
               AND (
                    UPPER(c.DS_IDENTIFICADOR) LIKE 'DS_RECEITUARIO_LIVRE%'
                    OR UPPER(c.DS_IDENTIFICADOR) LIKE 'DS_RECEITUARIO_MEDICAMENTO%'
               )
             ORDER BY rc.CD_CAMPO
            '''
        ),
        {'cd_atendimento': cd_atendimento},
    ).mappings().all()
    values: list[str] = []
    for row in rows:
        value = row.get('lo_valor') or row.get('ds_valor')
        cleaned = str(value or '').strip()
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return '\n'.join(values) or None


def _generic_support(
    raw: dict[str, Any],
    line_code: str,
    identification: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = dict(identification or {})
    exams: dict[str, dict[str, Any]] = {}

    prescription = raw.get('itens_ultima_prescricao')
    if isinstance(prescription, list) and prescription:
        fields['prescricao_atual'] = {
            'valor': [str(item) for item in prescription if item],
            'origem': 'MV - última prescrição medicamentosa ativa',
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
    if line_code in {'LC-IC', 'LC-PREVENCAO'}:
        raw = {**raw, 'itens_ultima_prescricao': []}
    identification = _attendance_identification(session, cd_atendimento)
    result = _generic_support(raw, line_code, identification)
    if line_code in {'LC-IC', 'LC-PREVENCAO'}:
        receituario = _load_latest_receituario_text(session, cd_atendimento)
        if receituario:
            medication_field = (
                'medicacoes_atuais' if line_code == 'LC-IC'
                else 'prescricao_atual'
            )
            result['campos'][medication_field] = {
                'valor': receituario,
                'origem': 'MV - último receituário fechado',
            }
        anchor = latest_previous_form_value(
            cd_atendimento,
            line_code,
            'data_ancora',
        )
        if anchor:
            result['campos']['data_ancora'] = {
                'valor': anchor,
                'origem': 'Última evolução estruturada',
            }
        if line_code == 'LC-IC':
            previous_phenotype = latest_previous_form_value(
                cd_atendimento,
                line_code,
                'fenotipo_atual',
            )
            if previous_phenotype:
                result['campos']['fenotipo_anterior'] = {
                    'valor': previous_phenotype,
                    'origem': 'Última evolução estruturada',
                }
    return result
