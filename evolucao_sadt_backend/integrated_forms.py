from __future__ import annotations

from typing import Any
from datetime import datetime


LINE_LABELS = {
    'LC-DAC': 'Doença Arterial Coronariana e Dor Torácica',
    'LC-IC': 'Insuficiência Cardíaca',
    'LC-PREVENCAO': 'Prevenção Cardiovascular e Cardiometabólica',
}

ALARM_LABELS = {
    'angina-change': 'Dor torácica nova ou em piora',
    'syncope': 'Síncope ou pré-síncope recente',
    'nyha-iv': 'Classe funcional NYHA IV',
    'hypoperfusion': 'Sinais de hipoperfusão',
    'event-acs': 'SCA/IAM desde a última avaliação',
    'event-stroke': 'AVC/AIT desde a última avaliação',
}


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ''
    return str(value).replace('\x00', '').strip()


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return _unique([_text(item) for item in value])
    rendered = _text(value)
    return [rendered] if rendered else []


def _date_br(value: Any) -> str:
    rendered = _text(value)
    if not rendered:
        return ''
    try:
        return datetime.fromisoformat(rendered[:10]).strftime('%d/%m/%Y')
    except ValueError:
        return rendered


def _active_alarm_ids(common: dict[str, Any]) -> set[str]:
    active: set[str] = set()
    chest_evolution = _text(common.get('risco_dor_toracica_evolucao')).lower()
    chest_triage = ' '.join(_values(common.get('risco_dor_toracica_triagem'))).lower()
    if any(term in chest_evolution or term in chest_triage for term in ('novo', 'piora', 'recente', 'progress', 'repouso', 'prolongada')):
        active.add('angina-change')
    syncope_evolution = _text(common.get('risco_sincope_evolucao')).lower()
    syncope_triage = ' '.join(_values(common.get('risco_sincope_triagem'))).lower()
    if any(term in syncope_evolution or term in syncope_triage for term in ('síncope', 'sincope', 'pré-síncope', 'pre-sincope', 'recente', 'progress')):
        active.add('syncope')
    if _text(common.get('nyha')).upper() == 'IV':
        active.add('nyha-iv')
    if 'frio' in _text(common.get('perfil_hemodinamico')).lower():
        active.add('hypoperfusion')
    events = ' '.join(_values(common.get('eventos_selecionados'))).lower()
    if 'sca' in events or 'iam' in events:
        active.add('event-acs')
    if 'avc' in events or 'ait' in events:
        active.add('event-stroke')
    return active


def render_integrated_form(data: dict[str, Any]) -> str:
    active_lines = [
        line for line in data.get('linhas_ativas', [])
        if line in LINE_LABELS
    ]
    common = data.get('comum') if isinstance(data.get('comum'), dict) else {}
    modules = data.get('modulos') if isinstance(data.get('modulos'), dict) else {}
    consultation = _text(data.get('tipo_consulta')) or 'Não informado'
    labels = [LINE_LABELS[line] for line in active_lines]
    lines = [
        'EVOLUÇÃO INTEGRADA DAS LINHAS DE CUIDADO',
        f'Tipo de consulta: {consultation}.',
        f'Linhas acompanhadas: {"; ".join(labels) or "Nenhuma linha ativa"}.',
    ]

    medications = data.get('medicamentos')
    structured_medications: list[dict[str, Any]] = [
        item for item in medications if isinstance(item, dict)
    ] if isinstance(medications, list) else []
    common_fields = [
        ('HISTÓRIA CLÍNICA', ('historia_clinica',)),
        ('SINTOMAS', ('sintomas_selecionados', 'sintomas')),
        ('EVENTOS DESDE A ÚLTIMA AVALIAÇÃO', ('eventos_selecionados', 'eventos')),
        ('COMORBIDADES', ('comorbidades_selecionadas', 'comorbidades')),
    ]
    if not structured_medications:
        common_fields.append(('MEDICAÇÕES ATUAIS', ('medicacoes_em_uso', 'prescricao_atual')))
    rendered_common: set[str] = set()
    for title, keys in common_fields:
        values = _unique([
            item
            for key in keys
            for item in _values(common.get(key))
            if item not in rendered_common
        ])
        if values:
            rendered_common.update(values)
            lines.extend(['', title, '; '.join(values)])

    medication_lines = _unique([
        f'{_text(item.get("farmaco"))}{(" " + _text(item.get("posologia"))) if _text(item.get("posologia")) else ""}'
        for item in structured_medications
        if _text(item.get('farmaco')) and item.get('presente_no_inicio')
    ])
    if medication_lines:
        lines.extend(['', 'MEDICAÇÕES ATUAIS', '; '.join(medication_lines)])
    systolic = _text(common.get('pa_sistolica'))
    diastolic = _text(common.get('pa_diastolica'))
    heart_rate = _text(common.get('frequencia_cardiaca'))
    if systolic or diastolic or heart_rate:
        vital_parts = []
        if systolic or diastolic:
            vital_parts.append(f'PA: {systolic or "?"}/{diastolic or "?"} mmHg')
        if heart_rate:
            vital_parts.append(f'FC: {heart_rate} bpm')
        lines.extend(['', 'SINAIS VITAIS', '; '.join(vital_parts) + '.'])

    result_parts: list[str] = []
    for key, label, unit in (
        ('ldl', 'LDL-c', 'mg/dL'),
        ('hba1c', 'HbA1c', '%'),
        ('tfge', 'TFGe', ''),
        ('rac', 'RAC', ''),
        ('potassio', 'Potássio', ''),
        ('sodio', 'Sódio', ''),
        ('feve', 'FEVE', '%'),
    ):
        value = _text(common.get(key))
        if value:
            result_parts.append(f'{label}: {value}{(" " + unit) if unit else ""}')
    category = _text(common.get('categoria_risco'))
    if category:
        result_parts.append(f'categoria de risco: {category}')
    if result_parts:
        lines.extend(['', 'RESULTADOS E PERFIL', '; '.join(result_parts) + '.'])

    structured_results = data.get('resultados_exames')
    if isinstance(structured_results, dict):
        rendered_results: list[str] = []
        for result in structured_results.values():
            if not isinstance(result, dict) or not _text(result.get('exame')):
                continue
            value = _text(result.get('valor')) or 'sem resultado'
            unit = f' {_text(result.get("unidade"))}' if _text(result.get('unidade')) else ''
            current_meta = ', '.join(part for part in (_date_br(result.get('data')), _text(result.get('origem'))) if part)
            current = f'{_text(result.get("exame"))}: {value}{unit}'
            if current_meta:
                current += f' ({current_meta})'
            previous = _text(result.get('valor_anterior'))
            if previous:
                previous_meta = _date_br(result.get('data_anterior'))
                current += f'; anterior: {previous}{unit}{(" (" + previous_meta + ")") if previous_meta else ""}'
            rendered_results.append(current + '.')
        if rendered_results:
            lines.extend(['', 'RESULTADOS DE EXAMES', *rendered_results])

    cal_origin = data.get('cal_origem')
    if isinstance(cal_origin, dict) and (_text(cal_origin.get('id')) or _text(cal_origin.get('data'))):
        lines.extend(['', 'CAL DE ORIGEM'])
        lines.append(f'CAL de origem: {_date_br(cal_origin.get("data")) or "data não informada"} ({_text(cal_origin.get("id")) or "sem identificador"}).')
        conducts = _values(cal_origin.get('condutas'))
        if conducts:
            lines.append(f'Condutas da CAL: {"; ".join(conducts)}.')
        origin_exams = cal_origin.get('exames')
        if isinstance(origin_exams, list):
            rendered_origin_exams = [
                f'{_text(item.get("codigo"))}: {_text(item.get("situacao"))}'
                for item in origin_exams if isinstance(item, dict) and _text(item.get('codigo'))
            ]
            if rendered_origin_exams:
                lines.append(f'Exames da CAL: {"; ".join(rendered_origin_exams)}.')

    repeated_plans: list[str] = []
    for line in active_lines:
        module = modules.get(line)
        if not isinstance(module, dict):
            continue
        lines.extend(['', f'{line} — {LINE_LABELS[line]}'])
        for key, label in (
            ('diagnostico', 'Diagnóstico'),
            ('fenotipo_atual', 'Fenótipo atual'),
            ('risco_global', 'Risco global'),
            ('decisao_clinica', 'Decisão clínica'),
        ):
            value = _text(module.get(key))
            if value:
                lines.append(f'{label}: {value}.')
        for key in ('conduta', 'conduta_adicional', 'plano_terapeutico', 'observacao_decisao_adicional'):
            value = _text(module.get(key))
            if value:
                repeated_plans.append(value)

    common_plans = [
        _text(value)
        for key, value in common.items()
        if key.startswith('conduta_') and _text(value)
    ]
    decisions = _values(common.get('decisao_clinica'))
    if decisions:
        common_plans.append(f'Decisão clínica: {"; ".join(decisions)}.')
    next_step = _text(common.get('proximo_acompanhamento'))
    if next_step:
        common_plans.append(f'Próximo acompanhamento: {next_step}.')
    additional = _text(common.get('conduta_adicional'))
    if additional:
        common_plans.append(additional)
    medication_action_labels = {
        'manter': 'Manter', 'iniciar': 'Iniciar',
        'titular': 'Titular', 'suspender': 'Suspender',
    }
    for medication in structured_medications:
        action = _text(medication.get('acao_consulta'))
        drug = _text(medication.get('farmaco'))
        if action not in medication_action_labels or not drug:
            continue
        dosage = _text(medication.get('posologia'))
        common_plans.append(f'{medication_action_labels[action]} {drug}{(" " + dosage) if dosage else ""}.')
    plans = _unique([*common_plans, *repeated_plans])
    if plans:
        lines.extend(['', 'PLANO TERAPÊUTICO CONSOLIDADO', *plans])
    alarm_justifications = data.get('justificativas_alarmes')
    rendered_justifications: list[str] = []
    active_alarm_ids = _active_alarm_ids(common)
    if isinstance(alarm_justifications, dict):
        for alarm_id, content in alarm_justifications.items():
            if str(alarm_id) not in active_alarm_ids:
                continue
            if not isinstance(content, dict):
                continue
            reason = _text(content.get('motivo'))
            details = _text(content.get('complemento'))
            if not reason:
                continue
            label = ALARM_LABELS.get(str(alarm_id), str(alarm_id).replace('-', ' '))
            rendered_justifications.append(f'{label}: {reason}{(" — " + details) if details else ""}.')
    if rendered_justifications:
        lines.extend(['', 'JUSTIFICATIVAS DOS ALARMES CLÍNICOS', *rendered_justifications])
    exams = _unique([
        _text(code) for code in data.get('exames_solicitados', [])
        if _text(code)
    ])
    if exams:
        lines.extend(['', 'EXAMES SOLICITADOS', '; '.join(exams) + '.'])
    return '\n'.join(lines).strip()

