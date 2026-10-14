import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

LIMITE_TEXTO_MV = 4000
FORM_VERSION = '3.1'
SUPPORTED_FORM_VERSIONS = {'1.0', '2.0', '3.0', '3.1', '4.0'}
SUPPORTED_LINES = {'LC-DAC', 'LC-IC', 'LC-PREVENCAO'}

default_data_dir = (
    Path(tempfile.gettempdir()) / 'evolucao_sadt_data'
    if os.name == 'nt'
    else Path('/app/evolucao_sadt_data')
)
DATA_DIR = Path(os.getenv('EVOLUCAO_SADT_DATA_DIR', str(default_data_dir)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = DATA_DIR / 'formularios.sqlite3'


class FormInput(BaseModel):
    cd_atendimento: int
    linha_cuidado: Literal['LC-DAC', 'LC-IC', 'LC-PREVENCAO']
    versao: str = Field(default=FORM_VERSION, max_length=20)
    dados: dict[str, Any]
    copiado_de_cd_pre_med: int | None = None


class FormRecord(FormInput):
    cd_pre_med: int
    texto_mv: str
    caracteres: int
    criado_em: str
    atualizado_em: str


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


def _initialize_database() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS formularios_evolucao (
                cd_pre_med INTEGER PRIMARY KEY,
                cd_atendimento INTEGER NOT NULL,
                linha_cuidado TEXT NOT NULL,
                versao TEXT NOT NULL,
                dados_json TEXT NOT NULL,
                texto_mv TEXT NOT NULL,
                texto_hash TEXT NOT NULL,
                copiado_de_cd_pre_med INTEGER,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_formularios_atendimento
            ON formularios_evolucao (cd_atendimento, atualizado_em DESC)
            """
        )


def _text(data: dict[str, Any], key: str, fallback: str = '') -> str:
    value = data.get(key, '')
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return fallback
    return str(value).replace('\x00', '').strip()[:2500] or fallback


def _items(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        return []
    return [
        str(item).replace('\x00', '').strip()[:200] for item in value if item
    ]


def _label(value: str, labels: dict[str, str]) -> str:
    return labels.get(value, value or 'Não informado')


def _section(lines: list[str], title: str, value: str) -> None:
    lines.extend(['', title, value or 'Não informado.'])


TIME_LABELS = {
    'cal1': 'CAL1 - 1ª consulta / 30 dias pós-alta',
    'cir1': 'CIR1',
    'cal2': 'CAL2 - 6 meses',
    'cir2': 'CIR2',
    'cal3': 'CAL3 - 12 meses',
    'cir3': 'CIR3',
    'retorno_semestral': 'Retorno semestral após 1 ano',
    'consulta_extra': 'Consulta extra',
}

TIME_LABELS.update({
    'retorno_semestral': 'Seguimento semestral após o primeiro ano',
    'consulta_extra': 'Consulta extraordinária',
})


def _render_dac(data: dict[str, Any]) -> str:
    diagnosis_labels = {
        'dac_cronica_estavel': 'DAC crônica estável',
        'pos_angioplastia_dac': 'Pós-angioplastia de DAC crônica',
        'pos_angioplastia_sca': 'Pós-angioplastia de SCA',
        'pos_rm': 'Pós-RM',
    }
    reason_labels = {
        'alergia_intolerancia': 'Alergia ou intolerância',
        'sangramento_risco': 'Sangramento ativo ou alto risco hemorrágico',
        'anticoagulacao': 'Anticoagulação',
        'recusa': 'Recusa do paciente',
        'sangramento_ativo': 'Sangramento ativo',
        'contraindicacao': 'Contraindicação formal',
        'anticoagulacao_suspensa': 'Anticoagulação com suspensão documentada',
        'intolerancia_documentada': 'Intolerância documentada',
        'hepatopatia_grave': 'Hepatopatia grave',
        'recusa_paciente': 'Recusa do paciente',
        'iniciando': 'Tratamento sendo iniciado',
        'outro': 'Outro',
    }
    ldl_labels = {
        'prescricao_inicial': 'Prescrição inicial de estatina',
        'aumento_estatina': 'Intensificação com aumento de dose da estatina',
        'ezetimiba': 'Intensificação com inclusão de ezetimiba',
        'acido_bempedoico': 'Intensificação com inclusão de ácido bempedoico',
        'pcsk9': 'Intensificação com inclusão de inibidor PCSK9',
        'nao_intensificado': (
            'Não intensificado por intolerância ou impossibilidade de acesso'
        ),
    }
    lines = [
        (
            'LINHA DE CUIDADO: LC-DAC | '
            'Doença Arterial Coronariana e Dor Torácica'
        ),
        'FORMULÁRIO CLÍNICO: LC-DAC v1.0',
        '',
        'CLASSIFICAÇÃO DO PACIENTE',
        (
            'Diagnóstico: '
            f'{_label(_text(data, "diagnostico"), diagnosis_labels)}.'
        ),
        f'Momento da consulta: {_label(_text(data, "tempo"), TIME_LABELS)}.',
    ]
    _section(lines, 'HISTÓRIA CLÍNICA', _text(data, 'historia_clinica'))
    _section(lines, 'COMORBIDADES', _text(data, 'comorbidades'))
    _section(
        lines,
        'RESULTADOS DE EXAMES RELEVANTES',
        _text(data, 'exames_relevantes'),
    )
    lines.extend(['', 'OBJETIVOS TERAPÊUTICOS'])

    aas = _text(data, 'aas')
    aas_line = f'Uso de AAS: {_label(aas, {"sim": "Sim", "nao": "Não"})}.'
    if aas == 'nao':
        reason = _label(_text(data, 'aas_justificativa'), reason_labels)
        other = _text(data, 'aas_justificativa_outro')
        aas_line += (
            f' Justificativa: {reason}{f" - {other}" if other else ""}.'
        )
    lines.append(aas_line)

    dapt = _text(data, 'segundo_antiagregante')
    if dapt:
        dapt_labels = {
            'nao': 'Não',
            'clopidogrel': 'Clopidogrel',
            'ticagrelor': 'Ticagrelor',
            'prasugrel': 'Prasugrel',
        }
        dapt_line = f'Segundo antiagregante: {_label(dapt, dapt_labels)}.'
        if dapt == 'nao':
            reason = _label(_text(data, 'dapt_justificativa'), reason_labels)
            other = _text(data, 'dapt_justificativa_outro')
            dapt_line += (
                f' Justificativa: {reason}{f" - {other}" if other else ""}.'
            )
        lines.append(dapt_line)
        if _text(data, 'meta_dapt'):
            lines.append(f'Meta de tempo de DAPT: {_text(data, "meta_dapt")}.')
        if _text(data, 'data_angioplastia'):
            lines.append(
                f'Data da angioplastia: {_text(data, "data_angioplastia")}.'
            )

    statin = _text(data, 'estatina')
    statin_line = (
        f'Uso de estatina: {_label(statin, {"sim": "Sim", "nao": "Não"})}.'
    )
    if statin == 'nao':
        reason = _label(_text(data, 'estatina_justificativa'), reason_labels)
        other = _text(data, 'estatina_justificativa_outro')
        statin_line += (
            f' Justificativa: {reason}{f" - {other}" if other else ""}.'
        )
    lines.append(statin_line)

    ldl = _text(data, 'ldl_resultado')
    ldl_date = _text(data, 'ldl_data')
    if ldl or ldl_date:
        lines.append(
            f'LDL: {ldl or "não informado"}'
            f'{f" em {ldl_date}" if ldl_date else ""}.'
        )
    if _text(data, 'ldl_conduta'):
        lines.append(
            'Conduta para LDL: '
            f'{_label(_text(data, "ldl_conduta"), ldl_labels)}.'
        )

    bp = _text(data, 'pressao_arterial')
    bp_target = '< 130/80 mmHg'
    bp_status = _label(
        _text(data, 'pressao_status'),
        {'no_alvo': 'No alvo', 'fora_meta': 'Fora da meta'},
    )
    lines.append(
        f'Pressão arterial: {bp or "não informada"}. '
        f'Meta: {bp_target or "não definida"}. Situação: {bp_status}.'
    )

    hba1c = _text(data, 'hba1c_resultado')
    hba1c_target = '< 7,0%'
    hba1c_status = _label(
        _text(data, 'hba1c_status'),
        {'no_alvo': 'No alvo', 'fora_meta': 'Fora da meta'},
    )
    lines.append(
        f'HbA1c: {hba1c or "não informada"}. '
        f'Meta: {hba1c_target}. Situação: {hba1c_status}.'
    )

    tobacco = _text(data, 'tabagismo_status')
    tobacco_burden = _text(data, 'carga_tabagica')
    if tobacco or tobacco_burden:
        tobacco_labels = {
            'ativo': 'Ativo',
            'pregresso': 'Pregresso',
            'passivo': 'Passivo',
            'nunca': 'Nunca fumou',
        }
        lines.extend(['', 'TABAGISMO'])
        if tobacco:
            lines.append(f'Situação tabágica: {_label(tobacco, tobacco_labels)}.')
        if tobacco_burden:
            lines.append(f'Carga tabágica: {tobacco_burden}.')

    metabolic_lines: list[str] = []
    weight = _text(data, 'peso')
    imc = _text(data, 'imc')
    imc_class = _text(data, 'imc_classificacao')
    if weight:
        metabolic_lines.append(f'Peso: {weight}.')
    if imc:
        suffix = f' ({imc_class})' if imc_class else ''
        metabolic_lines.append(f'IMC: {imc}{suffix}.')
    uric = _text(data, 'acido_urico')
    uric_date = _text(data, 'acido_urico_data')
    if uric or uric_date:
        metabolic_lines.append(
            f'Ácido úrico: {uric or "não informado"}'
            f'{f" em {uric_date}" if uric_date else ""}.'
        )
    waist = _text(data, 'circunferencia_abdominal')
    if waist:
        metabolic_lines.append(f'Circunferência abdominal: {waist}.')
    yes_no = {'sim': 'Sim', 'nao': 'Não'}
    steatosis = _text(data, 'esteatose')
    if steatosis:
        metabolic_lines.append(
            f'Esteatose hepática: {_label(steatosis, yes_no)}.'
        )
    apnea = _text(data, 'apneia')
    if apnea:
        apnea_line = f'Apneia do sono: {_label(apnea, yes_no)}.'
        cpap = _text(data, 'cpap')
        if apnea == 'sim' and cpap:
            apnea_line += f' Uso de CPAP: {_label(cpap, yes_no)}.'
        metabolic_lines.append(apnea_line)
    polysomnography = _text(data, 'polissonografia')
    polysomnography_date = _text(data, 'polissonografia_data')
    if polysomnography or polysomnography_date:
        metabolic_lines.append(
            f'Polissonografia: {polysomnography or "resultado não informado"}'
            f'{f" em {polysomnography_date}" if polysomnography_date else ""}.'
        )
    glp1 = _text(data, 'obesidade_glp1')
    if glp1:
        metabolic_lines.append(f'Agonista de GLP-1: {glp1}.')
    bariatric = _text(data, 'cirurgia_bariatrica')
    if bariatric:
        metabolic_lines.append(f'Cirurgia bariátrica: {bariatric}.')
    if metabolic_lines:
        lines.extend(['', 'IMC E RISCO METABÓLICO', *metabolic_lines])

    activity = _text(data, 'atividade_fisica_tipo')
    if activity:
        activity_labels = {
            'aerobica': 'Aeróbica',
            'resistencia': 'Resistência',
            'mista': 'Mista',
        }
        lines.extend(
            ['', 'ATIVIDADE FÍSICA', _label(activity, activity_labels) + '.']
        )

    diet = _items(data, 'alimentacao_restricoes')
    if diet:
        diet_labels = {
            'sodio': 'Restrição de sódio',
            'gordura_saturada': 'Restrição de gordura saturada',
            'acucar_adicionado': 'Restrição de açúcar adicionado',
        }
        lines.extend(
            [
                '',
                'ALIMENTAÇÃO CARDIOPROTETORA',
                '; '.join(_label(item, diet_labels) for item in diet) + '.',
            ]
        )
    _section(lines, 'CONDUTA', _text(data, 'conduta'))
    return '\n'.join(lines).strip()


def _medication_line(
    data: dict[str, Any], prefix: str, title: str, reasons: dict[str, str]
) -> str:
    status = _text(data, f'{prefix}_status')
    if status == 'sim':
        drug = _text(data, f'{prefix}_qual', 'Não informado')
        dose = _text(data, f'{prefix}_dose', 'Não informada')
        return f'{title}: Sim. Medicamento: {drug}. Dose: {dose}.'
    if status == 'nao':
        reason = _label(_text(data, f'{prefix}_justificativa'), reasons)
        other = _text(data, f'{prefix}_justificativa_outro')
        suffix = f' - {other}' if other else ''
        return f'{title}: Não. Justificativa: {reason}{suffix}.'
    return f'{title}: Não informado.'


def _render_ic(data: dict[str, Any]) -> str:
    diagnosis_labels = {
        'icfer_melhorada': 'ICFER ou IC melhorada',
        'icfelr': 'ICFELR',
        'icfep': 'ICFEP',
    }
    etiology_labels = {
        'isquemica_dac': 'Isquêmica/DAC',
        'has': 'HAS',
        'valvopatia': 'Valvopatia',
        'taquicardiomiopatia': 'Taquicardiomiopatia',
        'amiloidose': 'Amiloidose',
        'chagas': 'Chagas',
        'miocardite': 'Miocardite',
        'outras': 'Outras',
    }
    reasons = {
        'intolerancia_documentada': 'Intolerância documentada',
        'nefropatia_hipercalemia': 'Nefropatia grave ou hipercalemia',
        'nefropatia_grave': 'Nefropatia grave',
        'hipotensao_grave': 'Hipotensão grave',
        'risco_cetoacidose': 'Risco elevado de cetoacidose',
        'bradicardia_hipotensao': 'Bradicardia ou hipotensão grave',
        'recusa_paciente': 'Recusa do paciente',
        'iniciando': 'Tratamento sendo iniciado',
        'outro': 'Outro',
    }
    lines = [
        'LINHA DE CUIDADO: LC-IC | Insuficiência Cardíaca',
        'FORMULÁRIO CLÍNICO: LC-IC v1.0',
        '',
        'CLASSIFICAÇÃO DO PACIENTE',
        (
            'Diagnóstico: '
            f'{_label(_text(data, "diagnostico"), diagnosis_labels)}.'
        ),
        f'Momento da consulta: {_label(_text(data, "tempo"), TIME_LABELS)}.',
    ]
    _section(lines, 'HISTÓRIA CLÍNICA', _text(data, 'historia_clinica'))
    _section(lines, 'COMORBIDADES', _text(data, 'comorbidades'))
    _section(
        lines,
        'RESULTADOS DE EXAMES RELEVANTES',
        _text(data, 'exames_relevantes'),
    )
    _section(lines, 'EXAME FÍSICO', _text(data, 'exame_fisico'))

    etiologies = [
        _label(item, etiology_labels) for item in _items(data, 'etiologias')
    ]
    other = _text(data, 'etiologia_outra')
    if other:
        etiologies.append(other)
    lines.extend([
        '',
        'INVESTIGAÇÃO DE ETIOLOGIA',
        ', '.join(etiologies) or 'Não informada.',
    ])

    echo = _text(data, 'eco_resultado')
    echo_date = _text(data, 'eco_data')
    lines.extend([
        '',
        'ÚLTIMO ECOCARDIOGRAMA',
        f'{echo or "Não informado"}'
        f'{f" (data: {echo_date})" if echo_date else ""}.',
    ])

    lines.extend(['', 'OBJETIVOS TERAPÊUTICOS'])
    lines.append(_medication_line(data, 'ieca', 'IECA/BRA/ARNI', reasons))
    lines.append(_medication_line(data, 'sglt2', 'SGLT2-i', reasons))
    lines.append(
        _medication_line(data, 'betabloqueador', 'Betabloqueador', reasons)
    )
    lines.append(_medication_line(data, 'mra', 'MRA', reasons))

    bnp = _text(data, 'bnp_resultado')
    bnp_date = _text(data, 'bnp_data')
    lines.append(
        f'BNP ou PRO-BNP: {bnp or "não informado"}'
        f'{f" em {bnp_date}" if bnp_date else ""}.'
    )
    _section(
        lines,
        'TERAPIAS COMPLEMENTARES OU ADICIONAIS',
        _text(data, 'terapias_complementares'),
    )

    compensated = _label(
        _text(data, 'compensado'), {'sim': 'Sim', 'nao': 'Não'}
    )
    nyha = _label(
        _text(data, 'nyha'), {'i': 'I', 'ii': 'II', 'iii': 'III', 'iv': 'IV'}
    )
    lines.extend([
        '',
        'AVALIAÇÃO CLÍNICA',
        f'Clinicamente compensado: {compensated}. '
        f'Classe funcional NYHA: {nyha}.',
    ])
    _section(lines, 'CONDUTA', _text(data, 'conduta'))
    return '\n'.join(lines).strip()


def _render_prevent(data: dict[str, Any]) -> str:
    diagnosis_labels = {
        'hipertensao': 'Hipertensão',
        'diabetes_tipo_1': 'Diabetes tipo 1',
        'diabetes_tipo_2': 'Diabetes tipo 2',
        'dislipidemia': 'Dislipidemia',
        'doenca_renal_cronica': 'Doença renal crônica',
        'apneia_sono': 'Apneia do sono',
        'tabagismo': 'Tabagismo',
        'etilismo': 'Etilismo',
        'obesidade': 'Obesidade',
        'sedentarismo': 'Sedentarismo',
    }
    yes_no = {'sim': 'Sim', 'nao': 'Não', 'nao_avaliado': 'Não avaliado'}

    def selected(key: str, labels: dict[str, str]) -> str:
        return ', '.join(_label(item, labels) for item in _items(data, key))

    def result(label: str, value_key: str, date_key: str = '') -> str:
        value = _text(data, value_key)
        date = _text(data, date_key) if date_key else ''
        if not value and not date:
            return ''
        return f'{label}: {value or "não informado"}{f" em {date}" if date else ""}.'

    diagnoses = selected('diagnosticos', diagnosis_labels)
    lines = [
        (
            'LINHA DE CUIDADO: LC-PREVENCAO | '
            'Prevenção Cardiovascular e Cardiometabólica'
        ),
        'FORMULÁRIO CLÍNICO: LC-PREVENCAO v1.0',
        '',
        'CLASSIFICAÇÃO DO PACIENTE',
        f'Diagnósticos: {diagnoses or "Não informados"}.',
        f'Momento da consulta: {_label(_text(data, "tempo"), TIME_LABELS)}.',
    ]
    _section(lines, 'HISTÓRIA CLÍNICA', _text(data, 'historia_clinica'))
    _section(lines, 'COMORBIDADES', _text(data, 'comorbidades'))
    _section(lines, 'MEDICAÇÕES EM USO', _text(data, 'medicacoes_uso'))
    lines.extend([
        '',
        'EXAME FÍSICO',
        (
            f'PA: {_text(data, "pa", "não informada")}; '
            f'FC: {_text(data, "fc", "não informada")}; '
            f'Peso: {_text(data, "peso", "não informado")}; '
            f'IMC: {_text(data, "imc", "não informado")}; '
            'Circunferência abdominal: '
            f'{_text(data, "circunferencia_abdominal", "não informada")}.'
        ),
        (
            f'Pulsos: {_text(data, "pulsos", "não informados")}; '
            f'edema: {_text(data, "edema", "não informado")}.'
        ),
    ])
    _section(lines, 'OBSERVAÇÕES', _text(data, 'observacoes'))
    _section(
        lines,
        'RESULTADOS DE EXAMES RELEVANTES',
        _text(data, 'exames_relevantes'),
    )

    if 'hipertensao' in _items(data, 'diagnosticos'):
        hypertension_labels = {
            'essencial': 'Essencial',
            'secundaria': 'Secundária',
            'resistente': 'Resistente',
            'refrataria': 'Refratária',
            'mascarada': 'Mascarada',
            'avental_branco': 'Avental branco',
            'outro': 'Outro',
        }
        aggravating_labels = {
            'hve': 'HVE',
            'apneia_sono': 'Apneia do sono',
            'doenca_renal': 'Doença renal',
            'albuminuria': 'Albuminúria',
            'hipotensao_postural': 'Hipotensão postural',
            'risco_queda': 'Risco de queda',
        }
        lines.extend([
            '',
            'HIPERTENSÃO',
            (
                'Classificação: '
                f'{_label(_text(data, "hipertensao_tipo"), hypertension_labels)}'
                f'{f" - {_text(data, "hipertensao_outro")}" if _text(data, "hipertensao_outro") else ""}.'
            ),
            'Fatores agravantes: '
            f'{selected("hipertensao_agravantes", aggravating_labels) or "Nenhum informado"}.',
            (
                'Tempo de diagnóstico: '
                f'{_text(data, "hipertensao_tempo_diagnostico", "não informado")}. '
                f'Meta pressórica: {_text(data, "pressao_meta", "não definida")}.'
            ),
        ])
        mapa = result('Último MAPA', 'mapa_resultado', 'mapa_data')
        if mapa:
            lines.append(mapa)
        if _text(data, 'hipertensao_anotacoes'):
            lines.append(f'Anotações: {_text(data, "hipertensao_anotacoes")}')

    if 'dislipidemia' in _items(data, 'diagnosticos'):
        site_labels = {
            'coronariana': 'Coronariana',
            'carotidea': 'Carotídea',
            'periferica': 'Periférica',
            'aorta': 'Aorta',
            'outro': 'Outro sítio',
        }
        lines.extend([
            '',
            'DISLIPIDEMIA',
            'Doença aterosclerótica: '
            f'{_label(_text(data, "aterosclerose"), yes_no)}.',
        ])
        sites = selected('aterosclerose_sites', site_labels)
        if sites or _text(data, 'aterosclerose_outro'):
            lines.append(
                'Sítios: '
                f'{sites}{f" - {_text(data, "aterosclerose_outro")}" if _text(data, "aterosclerose_outro") else ""}.'
            )
        lines.append(
            'Uso de estatina: '
            f'{_label(_text(data, "estatina"), yes_no)}'
            f'{f" - {_text(data, "estatina_qual")}" if _text(data, "estatina_qual") else ""}.'
        )
        for line in [
            result('Colesterol total', 'colesterol_total', 'perfil_lipidico_data'),
            result('LDL', 'ldl_resultado', 'perfil_lipidico_data'),
            result('HDL', 'hdl_resultado', 'perfil_lipidico_data'),
            result('Triglicerídeos', 'triglicerideos', 'perfil_lipidico_data'),
        ]:
            if line:
                lines.append(line)
        if _text(data, 'lipid_anotacoes'):
            lines.append(f'Anotações: {_text(data, "lipid_anotacoes")}')

    diabetes_diagnoses = {'diabetes_tipo_1', 'diabetes_tipo_2'}
    if diabetes_diagnoses.intersection(_items(data, 'diagnosticos')):
        diabetes_labels = {
            'tipo_1': 'Tipo 1',
            'tipo_2_oral': 'Tipo 2 com terapia oral',
            'tipo_2_insulina': 'Tipo 2 insulinodependente',
            'pre_diabetes': 'Pré-diabetes/resistência à insulina',
        }
        complication_labels = {
            'dac': 'DAC',
            'avc': 'AVC',
            'aneurisma_aorta': 'Aneurisma de aorta',
            'retinopatia': 'Retinopatia',
            'neuropatia': 'Neuropatia',
            'doenca_renal_cronica': 'Doença renal crônica',
            'albuminuria': 'Albuminúria',
            'outros': 'Outros',
        }
        therapy_labels = {
            'insulina': 'Insulina',
            'metformina': 'Metformina',
            'sglt2': 'i-SGLT2',
            'glp1': 'Agonista GLP-1',
            'outros_orais': 'Outros hipoglicemiantes orais',
        }
        lines.extend([
            '',
            'DIABETES / PRÉ-DIABETES',
            f'Classificação: {_label(_text(data, "diabetes_tipo"), diabetes_labels)}.',
            f'Meta de HbA1c: {_text(data, "hba1c_meta", "não definida")}.',
            'Complicações macrovasculares: '
            f'{selected("complicacoes_macro", complication_labels) or "Nenhuma informada"}'
            f'{f" - {_text(data, "complicacoes_macro_outros")}" if _text(data, "complicacoes_macro_outros") else ""}.',
            'Complicações microvasculares: '
            f'{selected("complicacoes_micro", complication_labels) or "Nenhuma informada"}'
            f'{f" - {_text(data, "complicacoes_micro_outros")}" if _text(data, "complicacoes_micro_outros") else ""}.',
        ])
        for line in [
            result('HbA1c', 'hba1c_resultado', 'hba1c_data'),
            result('Glicemia de jejum', 'glicemia_jejum', 'glicemia_data'),
            result('Albuminúria', 'albuminuria_resultado', 'albuminuria_data'),
        ]:
            if line:
                lines.append(line)
        lines.append(
            'Terapia em uso: '
            f'{selected("diabetes_terapias", therapy_labels) or "Não informada"}'
            f'{f" - {_text(data, "diabetes_terapia_outro")}" if _text(data, "diabetes_terapia_outro") else ""}.'
        )
        if _text(data, 'fundo_olho'):
            lines.append(f'Fundo de olho: {_text(data, "fundo_olho")}')

    metabolic_diagnoses = {'obesidade', 'apneia_sono', 'diabetes_tipo_2'}
    if metabolic_diagnoses.intersection(_items(data, 'diagnosticos')):
        lines.extend([
            '',
            'OBESIDADE E SÍNDROME METABÓLICA',
            result('Ácido úrico', 'acido_urico', 'acido_urico_data'),
            f'Esteatose hepática: {_label(_text(data, "esteatose"), yes_no)}.',
            f'Apneia do sono: {_label(_text(data, "apneia"), yes_no)}.',
            f'Uso de CPAP: {_label(_text(data, "cpap"), yes_no)}.',
        ])
        polysom = result('Polissonografia', 'polissonografia', 'polissonografia_data')
        if polysom:
            lines.append(polysom)
        treatment = ', '.join(
            part
            for part in [
                f'Agonista GLP-1: {_text(data, "obesidade_glp1")}' if _text(data, 'obesidade_glp1') else '',
                f'Cirurgia bariátrica: {_text(data, "cirurgia_bariatrica")}' if _text(data, 'cirurgia_bariatrica') else '',
            ]
            if part
        )
        if treatment:
            lines.append(f'Tratamento: {treatment}.')
        if _text(data, 'obesidade_anotacoes'):
            lines.append(f'Anotações: {_text(data, "obesidade_anotacoes")}')

    if 'tabagismo' in _items(data, 'diagnosticos'):
        smoking_labels = {
            'ativo': 'Ativo',
            'pregresso': 'Pregresso',
            'passivo': 'Tabagista passivo',
            'nunca': 'Nunca fumou',
        }
        lines.extend([
            '',
            'TABAGISMO / NICOTINISMO',
            f'Situação: {_label(_text(data, "tabagismo_status"), smoking_labels)}.',
            f'Carga tabágica: {_text(data, "carga_tabagica", "não informada")}.',
        ])
        if _text(data, 'tabagismo_anotacoes'):
            lines.append(f'Anotações: {_text(data, "tabagismo_anotacoes")}')

    if {'doenca_renal_cronica', 'hipertensao', 'diabetes_tipo_1', 'diabetes_tipo_2'}.intersection(_items(data, 'diagnosticos')):
        lines.extend([
            '',
            'AVALIAÇÃO RENAL',
            f'Albuminúria: {_text(data, "renal_albuminuria", "não informada")}.',
            f'TFG: {_text(data, "tfg", "não informada")}.',
            'Antecedente de doença renal: '
            f'{_label(_text(data, "antecedente_renal"), yes_no)}'
            f'{f" - {_text(data, "antecedente_renal_descricao")}" if _text(data, "antecedente_renal_descricao") else ""}.',
        ])

    _section(lines, 'ESTILO DE VIDA', _text(data, 'estilo_vida'))
    risk_labels = {
        'baixo': 'Baixo',
        'limitrofe': 'Limítrofe',
        'intermediario': 'Intermediário',
        'alto': 'Alto',
        'dcv_estabelecida': 'DCV estabelecida',
    }
    lines.extend(['', 'ESTRATIFICAÇÃO DE RISCO GLOBAL'])
    if _text(data, 'prevent_resultado'):
        lines.append(f'Risco PREVENT: {_text(data, "prevent_resultado")}.')
    for line in [
        result('Lp(a)', 'lpa'),
        result('Apo B', 'apo_b'),
        result('CAC', 'cac'),
        result('Angiotomografia de coronárias', 'angio_coronarias', 'angio_data'),
    ]:
        if line:
            lines.append(line)
    if _text(data, 'outros_agravantes'):
        lines.append(f'Outros agravantes: {_text(data, "outros_agravantes")}')
    lines.append(
        f'Classificação de risco: {_label(_text(data, "risco_global"), risk_labels)}.'
    )

    final_labels = {
        'pa_alvo': 'PA no alvo',
        'lipidico_alvo': 'Perfil lipídico no alvo',
        'hba1c_alvo': 'HbA1c no alvo',
        'imc_alvo': 'IMC no alvo',
        'sodio_controlado': 'Ingesta de sódio controlada',
        'atividade_fisica': 'Atividade física adequada',
        'tabagismo_ativo_final': 'Tabagismo ativo',
    }
    lines.extend(['', 'INDICADORES FINAIS'])
    for key, label in final_labels.items():
        lines.append(f'{label}: {_label(_text(data, key), yes_no)}.')
    _section(lines, 'CONDUTA', _text(data, 'conduta'))
    return '\n'.join(line for line in lines if line is not None).strip()


def _render_dac_v2(data: dict[str, Any], version: str = '2.0') -> str:
    is_v3 = version in {'3.0', '3.1'}
    diagnosis_labels = {
        'dac_cronica_estavel': 'DAC crônica estável',
        'pos_angioplastia_dac': 'Pós-angioplastia de DAC crônica',
        'pos_angioplastia_sca': 'Pós-angioplastia de síndrome coronariana aguda',
        'pos_rm_dac': 'Pós-cirurgia de revascularização miocárdica - DAC crônica',
        'pos_rm_sca': 'Pós-cirurgia de revascularização miocárdica - SCA',
        'pos_rm': 'Pós-cirurgia de revascularização miocárdica',
    }
    yes_no = {'sim': 'Sim', 'nao': 'Não'}

    def choice(key: str, labels: dict[str, str]) -> str:
        return _label(_text(data, key), labels)

    def selected(key: str, labels: dict[str, str]) -> str:
        return ', '.join(_label(item, labels) for item in _items(data, key))

    def section(title: str, values: list[str]) -> None:
        clean = [value.strip() for value in values if value and value.strip()]
        if clean:
            lines.extend(['', title, ' '.join(clean)])

    diagnosis = _text(data, 'diagnostico')
    moment = _text(data, 'tempo')
    is_cir = moment.startswith('cir')
    cir_changed = _text(data, 'mudanca_clinica_cir')
    lines = [
        'LINHA DE CUIDADO: LC-DAC | Doença Arterial Coronariana e Dor Torácica',
        f'FORMULÁRIO CLÍNICO: LC-DAC v{version}',
        '',
        'CLASSIFICAÇÃO DO PACIENTE',
        f'Diagnóstico: {_label(diagnosis, diagnosis_labels)}. '
        f'Momento: {_label(moment, TIME_LABELS)}.',
    ]
    diagnosis_details = []
    if is_v3 and _text(data, 'contexto_revascularizacao'):
        diagnosis_details.append(
            'Contexto da revascularização: '
            f'{choice("contexto_revascularizacao", {"dac_cronica": "DAC crônica", "sca": "Síndrome coronariana aguda"})}.'
        )
    if diagnosis == 'dac_cronica_estavel' and _text(data, 'data_entrada'):
        diagnosis_details.append(
            f'Entrada na linha de cuidado: {_text(data, "data_entrada")}.'
        )
    if diagnosis.startswith('pos_angioplastia'):
        if _text(data, 'data_angioplastia'):
            diagnosis_details.append(
                f'{"Data da ICP" if is_v3 else "Data da angioplastia"}: {_text(data, "data_angioplastia")}.'
            )
        if _text(data, 'icp_chip'):
            chip_label = 'CHIP' if is_v3 else 'ICP CHIP'
            chip_text = f'{chip_label}: {choice("icp_chip", yes_no)}.'
            chip_criteria = selected('chip_criterios', {
                'tronco': 'ICP em tronco da coronária esquerda',
                'bifurcacao': 'Bifurcação com 2 stents',
                'cto': 'CTO',
                'multiarterial': 'ICP multiarterial',
                'stent_60': 'Stent >60 mm',
                'diabetico_multiarterial': 'Diabético com doença multiarterial',
            })
            if chip_criteria:
                chip_text += f' Critérios: {chip_criteria}.'
            diagnosis_details.append(chip_text)
    if diagnosis.startswith('pos_rm'):
        if _text(data, 'data_cirurgia'):
            diagnosis_details.append(
                f'Data da cirurgia: {_text(data, "data_cirurgia")}.'
            )
        if _text(data, 'pontes'):
            diagnosis_details.append(f'Pontes: {_text(data, "pontes")}.')
    section('DADOS DA LINHA DE CUIDADO', diagnosis_details)

    if is_cir:
        lines.extend([
            '',
            'CONSULTA DE INTEGRAÇÃO DE RESULTADOS',
            'Mudança clínica relevante desde a CAL: '
            f'{_label(cir_changed, yes_no)}.',
        ])

    symptom_labels = {
        'assintomatico': 'Assintomático/sem novos sintomas cardiovasculares',
        'angina_estavel': 'Angina estável',
        'angina_nova': 'Angina nova ou piora da angina',
        'dispneia_esforcos': 'Dispneia aos esforços',
        'fadiga_esforcos': 'Redução da capacidade funcional/fadiga aos esforços',
        'palpitacoes': 'Palpitações',
        'sincope': 'Síncope ou pré-síncope',
        'outro': 'Outro sintoma cardiovascular relevante',
    }
    event_labels = {
        'nenhum': 'Nenhum',
        'emergencia_cv': 'Atendimento em emergência cardiovascular',
        'internacao_cv': 'Internação cardiovascular',
        'sca_iam': 'Síndrome coronariana aguda/IAM',
        'nova_angioplastia': 'Nova angioplastia coronariana',
        'nova_rm': 'Cirurgia de revascularização miocárdica',
        'avc_ait': 'AVC/AIT',
        'ic_descompensacao': 'Insuficiência cardíaca/descompensação',
        'outro_cv': 'Outro evento cardiovascular',
        'outro_nao_cv': 'Outro evento não cardiovascular',
    }
    if not is_cir or cir_changed == 'sim':
        symptoms = selected('sintomas', symptom_labels)
        symptom_other = _text(data, 'sintoma_outro')
        symptom_values = [
            f'Sintomas: {symptoms}{f" - {symptom_other}" if symptom_other else ""}.'
            if symptoms or symptom_other
            else '',
        ]
        if {'angina_estavel', 'angina_nova'}.intersection(_items(data, 'sintomas')):
            symptom_values.append(
                f'Classe CCS: {choice("ccs", {"i": "I", "ii": "II", "iii": "III", "iv": "IV"})}.'
            )
            symptom_values.append(
                'Tratamento antianginoso: '
                f'{choice("tratamento_antianginoso", {"adequado": "Adequado", "otimizado": "Otimizado nesta consulta", "investigacao": "Investigação adicional indicada"})}.'
            )
        section('SITUAÇÃO CLÍNICA ATUAL', symptom_values)
        events = selected('eventos', event_labels)
        event_details = '; '.join(
            value
            for value in (
                _text(data, 'evento_outro_cv'),
                _text(data, 'evento_outro_nao_cv'),
            )
            if value
        )
        section(
            'EVENTOS DESDE A ÚLTIMA CONSULTA',
            [f'{events}{f" - {event_details}" if event_details else ""}.'],
        )

        systolic = _text(data, 'pa_sistolica')
        diastolic = _text(data, 'pa_diastolica')
        bp_status = ''
        try:
            bp_status = 'No alvo' if int(systolic) < 130 and int(diastolic) < 80 else 'Fora da meta'
        except ValueError:
            bp_status = _label(
                _text(data, 'pressao_status'),
                {'no_alvo': 'No alvo', 'fora_meta': 'Fora da meta'},
            )
        bp = f'{systolic}/{diastolic} mmHg' if systolic and diastolic else _text(data, 'pressao_arterial')
        pa_action_labels = {
            'mantido': 'Mantido tratamento/reavaliação programada',
            'dose_ajustada': 'Dose ajustada',
            'medicamento_adicionado': 'Medicamento adicionado',
            'investigacao': 'Investigação complementar',
            'outra': 'Outra conduta',
        }
        vitals = [
            f'HAS: {choice("has", yes_no)}.' if _text(data, 'has') else '',
            f'Pressão arterial: {bp}. Situação: {bp_status}.' if bp else '',
            f'Conduta para PA: {selected("pa_condutas", pa_action_labels)}'
            f'{f" - {_text(data, "pa_conduta_outro")}" if _text(data, "pa_conduta_outro") else ""}.'
            if _items(data, 'pa_condutas')
            else '',
            f'FC: {_text(data, "fc")} bpm.' if _text(data, 'fc') else '',
            f'Peso: {_text(data, "peso")} kg.' if _text(data, 'peso') else '',
            f'Altura: {_text(data, "altura")} m.' if _text(data, 'altura') else '',
            f'IMC: {_text(data, "imc")} kg/m².' if _text(data, 'imc') else '',
        ]
        if version == '3.1' and _text(data, 'exame_fisico_status'):
            exam_status = choice(
                'exame_fisico_status',
                {
                    'sem_alteracoes': 'Sem alterações relevantes',
                    'com_alteracoes': 'Com alterações relevantes',
                },
            )
            exam_description = _text(data, 'exame_fisico_descricao')
            vitals.append(
                f'Exame físico cardiovascular: {exam_status}'
                f'{f" - {exam_description}" if exam_description else ""}.'
            )
        section('DADOS CLÍNICOS DA CONSULTA', vitals)

    ldl = _text(data, 'ldl_resultado')
    ldl_status = ''
    try:
        ldl_status = 'No alvo' if float(ldl.replace(',', '.')) < 55 else 'Fora da meta'
    except ValueError:
        ldl_status = _label(
            _text(data, 'ldl_status'), {'no_alvo': 'No alvo', 'fora_meta': 'Fora da meta'}
        )
    ldl_actions = {
        'iniciada_estatina': 'Iniciada estatina',
        'aumento_estatina': 'Aumentada dose da estatina',
        'ezetimiba': 'Adicionada ezetimiba',
        'acido_bempedoico': 'Adicionado ácido bempedoico',
        'pcsk9': 'Adicionado inibidor de PCSK9',
        'outra': 'Outra intensificação',
        'nao_intensificado': 'Não intensificado',
    }
    lipid = [
        f'LDL: {ldl} mg/dL{f" em {_text(data, "ldl_data")}" if _text(data, "ldl_data") else ""}. Situação: {ldl_status}.'
        if ldl
        else '',
        f'Conduta: {selected("ldl_condutas", ldl_actions)}'
        f'{f" - {_text(data, "ldl_conduta_outro")}" if _text(data, "ldl_conduta_outro") else ""}.'
        if _items(data, 'ldl_condutas') else '',
        f'Justificativa para não intensificação: {choice("ldl_nao_intensificado_justificativa", {"intolerancia": "Intolerância", "contraindicacao": "Contraindicação", "acesso_custo": "Limitação de acesso / custo", "recusa": "Recusa", "intensificacao_recente": "Intensificação recente / aguardando controle", "outra": "Outra justificativa"})}'
        f'{f" - {_text(data, "ldl_nao_intensificado_outro")}" if _text(data, "ldl_nao_intensificado_outro") else ""}.'
        if _text(data, 'ldl_nao_intensificado_justificativa') else '',
        f'Estatina prescrita: {choice("estatina", yes_no)}.' if _text(data, 'estatina') else '',
        f'Justificativa: {_text(data, "estatina_justificativa")}'
        f'{f" - {_text(data, "estatina_justificativa_outro")}" if _text(data, "estatina_justificativa_outro") else ""}.'
        if _text(data, 'estatina_justificativa') else '',
        f'Estatina de alta intensidade: {choice("estatina_alta_intensidade", yes_no)}.'
        if _text(data, 'estatina_alta_intensidade') else '',
        f'Justificativa da intensidade: {_text(data, "estatina_alta_justificativa")}'
        f'{f" - {_text(data, "estatina_alta_outro")}" if _text(data, "estatina_alta_outro") else ""}.'
        if _text(data, 'estatina_alta_justificativa') else '',
    ]
    section('CONTROLE DA DISLIPIDEMIA', lipid)

    antithrombotic = [
        f'AAS prescrito: {choice("aas", yes_no)}.' if _text(data, 'aas') else '',
        f'Justificativa para ausência de AAS: {_text(data, "aas_justificativa")}'
        f'{f" - {_text(data, "aas_justificativa_outro")}" if _text(data, "aas_justificativa_outro") else ""}.'
        if _text(data, 'aas_justificativa') else '',
        f'Segundo antiagregante: {_text(data, "segundo_antiagregante")}.'
        if _text(data, 'segundo_antiagregante') else '',
        f'Justificativa: {_text(data, "dapt_justificativa")}'
        f'{f" - {_text(data, "dapt_justificativa_outro")}" if _text(data, "dapt_justificativa_outro") else ""}.'
        if _text(data, 'dapt_justificativa') else '',
        f'Anticoagulante oral: {choice("anticoagulante", yes_no)}.'
        if _text(data, 'anticoagulante') else '',
        f'Estratégia antitrombótica: {choice("anticoagulante_estrategia", {"isolado": "Anticoagulante isolado", "aas": "Anticoagulante + AAS", "clopidogrel": "Anticoagulante + Clopidogrel", "ticagrelor": "Anticoagulante + Ticagrelor", "prasugrel": "Anticoagulante + Prasugrel", "tripla": "Anticoagulante + AAS + segundo antiagregante"})}.'
        if _text(data, 'anticoagulante_estrategia') else '',
        f'Situação do Plano Terapêutico: {choice("plano_terapeutico", {"adequado": "Adequado ao momento clínico", "ajustado": "Ajustado nesta consulta", "excecao": "Exceção clinicamente justificada"})}.'
        if _text(data, 'plano_terapeutico') else '',
    ]
    section('TERAPIA ANTIPLAQUETÁRIA / ANTITROMBÓTICA', antithrombotic)

    hba1c = _text(data, 'hba1c_resultado')
    hba1c_lines = []
    if version == '3.1':
        hba1c_status = _text(data, 'hba1c_status')
        if not hba1c_status and hba1c:
            try:
                hba1c_status = 'na_meta' if float(hba1c.replace(',', '.')) < 7 else 'fora_meta'
            except ValueError:
                hba1c_status = 'nao_disponivel'
        status_label = _label(
            hba1c_status,
            {'na_meta': 'Na meta', 'fora_meta': 'Fora da meta', 'nao_disponivel': 'Não disponível'},
        )
        if hba1c:
            hba1c_lines.append(
                f'HbA1c: {hba1c}%'
                f'{f" em {_text(data, "hba1c_data")}" if _text(data, "hba1c_data") else ""}.'
                f'{f" Situação: {status_label}." if status_label else ""}'
            )
        elif status_label:
            hba1c_lines.append(f'HbA1c: {status_label}.')
        if _items(data, 'hba1c_condutas'):
            hba1c_lines.append(
                'Conduta para HbA1c: '
                + selected('hba1c_condutas', {
                    'mantido': 'Mantido tratamento / reavaliação programada',
                    'medicamento_ajustado': 'Tratamento medicamentoso ajustado',
                    'medicamento_adicionado': 'Medicamento adicionado',
                    'avaliacao_especializada': 'Encaminhado para avaliação especializada',
                    'estilo_vida': 'Orientação de estilo de vida reforçada',
                    'outra': 'Outra conduta',
                    'nao_ajustado': 'Não ajustado',
                })
                + (f' - {_text(data, "hba1c_conduta_outro")}' if _text(data, 'hba1c_conduta_outro') else '')
                + '.'
            )
        if _text(data, 'hba1c_nao_ajustado_justificativa'):
            hba1c_lines.append(
                'Justificativa para não ajuste: '
                + choice('hba1c_nao_ajustado_justificativa', {
                    'meta_individualizada': 'Meta individualizada',
                    'ajuste_recente': 'Ajuste recente / aguardando controle',
                    'intolerancia_contraindicacao': 'Intolerância / contraindicação',
                    'acesso_custo': 'Limitação de acesso / custo',
                    'recusa': 'Recusa do paciente',
                    'outra': 'Outra justificativa',
                })
                + (f' - {_text(data, "hba1c_nao_ajustado_outro")}' if _text(data, 'hba1c_nao_ajustado_outro') else '')
                + '.'
            )
        if hba1c_status == 'nao_disponivel' and _text(data, 'solicitar_hba1c'):
            hba1c_lines.append(f'Solicitar HbA1c: {choice("solicitar_hba1c", yes_no)}.')
    else:
        hba1c_lines = [
            f'HbA1c: {hba1c}%{f" em {_text(data, "hba1c_data")}" if _text(data, "hba1c_data") else ""}.' if hba1c else '',
            f'HbA1c: {choice("hba1c_faixa", {"menor_7": "<7%", "7_8_9": "7–8,9%", "maior_igual_9": "≥9%", "nao_disponivel": "Não disponível"})}.' if _text(data, 'hba1c_faixa') else '',
        ]
    metabolic = [
        f'Diabete: {choice("diabetes", yes_no)}.' if _text(data, 'diabetes') else '',
        *hba1c_lines,
        f'Tabagismo: {choice("tabagismo_status", {"nunca": "Nunca fumou", "ex_tabagista": "Ex-tabagista", "ativo": "Tabagista ativo", "passivo": "Tabagismo passivo relevante"})}.' if _text(data, 'tabagismo_status') else '',
        f'Intervenção tabágica: {selected("tabagismo_intervencoes", {"orientacao": "Orientação para cessação", "tratamento": "Tratamento indicado", "nao_aceita": "Não aceita intervenção"})}.' if _items(data, 'tabagismo_intervencoes') else '',
        f'Atividade física: {choice("atividade_fisica_status", {"adequada": "Adequada", "insuficiente": "Insuficiente", "sedentario": "Sedentário", "limitada_osteomuscular": "Limitada por condição osteomuscular", "limitada_cardiovascular": "Limitada por condição cardiovascular", "limitada_outra": "Limitada por outra condição clínica", "contraindicacao_temporaria": "Contraindicação médica temporária"})}.' if _text(data, 'atividade_fisica_status') else '',
        'Orientação para atividade física realizada.' if _text(data, 'atividade_orientacao') == 'sim' else '',
        f'Alimentação cardioprotetora: {choice("alimentacao_status", {"adequada": "Adequada", "necessita_intervencao": "Necessita intervenção", "parcial": "Parcial", "inadequada": "Inadequada"})}.' if _text(data, 'alimentacao_status') else '',
        f'Intervenção alimentar: {selected("alimentacao_intervencoes", {"orientacao": "Orientação realizada", "nutricao": "Encaminhamento para Nutrição"})}.' if _items(data, 'alimentacao_intervencoes') else '',
    ]
    section('RISCO CARDIOMETABÓLICO E ESTILO DE VIDA', metabolic)

    adherence = [
        f'Adesão medicamentosa: {_text(data, "adesao")}.' if _text(data, 'adesao') else '',
        f'Motivos: {selected("adesao_motivos", {"efeito_adverso": "Efeito adverso", "custo": "Custo/dificuldade de acesso", "esquecimento": "Esquecimento/dificuldade de organização", "decisao": "Decisão do paciente", "compreensao": "Dificuldade de compreensão", "outro": "Outro"})}'
        f'{f" - {_text(data, "adesao_outro")}" if _text(data, "adesao_outro") else ""}.' if _items(data, 'adesao_motivos') else '',
        f'Efeitos adversos: {choice("efeitos_adversos", yes_no)}.' if _text(data, 'efeitos_adversos') else '',
        f'Relacionados a: {selected("efeitos_adversos_tipos", {"hipolipemiante": "Hipolipemiante", "antitrombotico": "Antiagregante/antitrombótico", "anti_hipertensivo": "Anti-hipertensivo", "outro": "Outro"})}'
        f'{f" - {_text(data, "efeito_adverso_outro")}" if _text(data, "efeito_adverso_outro") else ""}.' if _items(data, 'efeitos_adversos_tipos') else '',
    ]
    section('ADESÃO E TOLERÂNCIA AO TRATAMENTO', adherence)

    if moment == 'cal1':
        cal1 = []
        if diagnosis.startswith('pos_angioplastia') and _text(data, 'acesso_vascular'):
            cal1.append(
                f'Acesso vascular: {_text(data, "acesso_vascular")}'
                f'{f" - {_text(data, "acesso_vascular_alteracao")}" if _text(data, "acesso_vascular_alteracao") else ""}.'
            )
        if diagnosis.startswith('pos_rm') and _text(data, 'ferida_operatoria'):
            cal1.append(
                f'Ferida operatória: {_text(data, "ferida_operatoria")}'
                f'{f" - {_text(data, "ferida_operatoria_alteracao")}" if _text(data, "ferida_operatoria_alteracao") else ""}.'
            )
        section('AVALIAÇÃO ESPECÍFICA DO CAL1', cal1)

    investigation = [
        f'Necessita investigação adicional: {choice("investigacao_adicional", yes_no)}.' if _text(data, 'investigacao_adicional') else '',
        f'Exames: {selected("investigacao_tipos", {"teste_funcional": "Teste funcional", "angiotc": "AngioTC de coronárias", "cinecoronariografia": "Cinecoronariografia", "outro": "Outro exame"})}'
        f'{f" - {_text(data, "investigacao_outro")}" if _text(data, "investigacao_outro") else ""}.' if _items(data, 'investigacao_tipos') else '',
        f'Teste funcional: {selected("teste_funcional_tipos", {"eco_estresse": "Ecocardiograma sob estresse", "cintilografia": "Cintilografia miocárdica", "rm_estresse": "Ressonância cardíaca de estresse", "teste_ergometrico": "Teste ergométrico"})}.' if _items(data, 'teste_funcional_tipos') else '',
    ]
    section('INVESTIGAÇÃO ADICIONAL', investigation)
    if is_v3:
        exam_names = {
            'exame_ecg': 'ECG',
            'exame_hemoglobina': 'Hemoglobina',
            'exame_hematocrito': 'Hematócrito',
            'exame_perfil_lipidico': 'Perfil lipídico',
            'exame_glicemia': 'Glicemia',
            'exame_hba1c': 'HbA1c',
            'exame_tgo': 'TGO',
            'exame_tgp': 'TGP',
            'exame_ureia': 'Ureia',
            'exame_creatinina': 'Creatinina',
            'exame_mapa': 'MAPA',
            'exame_ecocardiograma': 'Ecocardiograma',
            'exame_eco_doppler_de_arterias_carotidas_e_vertebrais': 'Eco Doppler de artérias carótidas e vertebrais',
            'exame_angiotc_de_coronarias': 'AngioTC de coronárias',
        }
        exam_states = {
            'disponivel_mv': 'Disponível no MV',
            'externo': 'Exame externo',
            'solicitar': 'Solicitar',
        }
        exam_lines = []
        for key, name in exam_names.items():
            state = _text(data, key)
            if not state:
                continue
            result = _text(data, f'{key}_resultado')
            exam_lines.append(
                f'{name}: {_label(state, exam_states)}{f" - {result}" if result else ""}.'
            )
        section('EXAMES PROTOCOLARES DO MOMENTO ATUAL', exam_lines)
    section(
        'DECISÃO CLÍNICA FINAL',
        [selected('decisoes_finais', {
            'estavel': 'Estável - manter estratégia atual',
            'otimizacao_terapeutica': 'Necessita otimização terapêutica',
            'investigacao': 'Necessita investigação complementar',
            'retorno_antecipado': 'Necessita retorno antecipado',
            'urgente': 'Necessita avaliação hospitalar/urgente',
        }) + '.'],
    )
    followup = _text(data, 'proximo_acompanhamento')
    section(
        'PRÓXIMO ACOMPANHAMENTO',
        [
            f'{_label(followup, {"cronograma": "Conforme cronograma da Linha de Cuidado", "antecipado": "Retorno antecipado"})}'
            f'{f" - {choice("retorno_intervalo", {"ate_30_dias": "Até 30 dias", "1_3_meses": "1–3 meses", "outro": "Outro intervalo"})}" if _text(data, "retorno_intervalo") else ""}'
            f'{f" - {_text(data, "retorno_outro")}" if _text(data, "retorno_outro") else ""}.'
            if followup else ''
        ],
    )
    section('OBSERVAÇÃO CLÍNICA COMPLEMENTAR', [_text(data, 'observacao_clinica')])
    return '\n'.join(lines).strip()


def _human(value: str) -> str:
    return value.replace('_', ' ').strip().upper()


def _confirmed_items(data: dict[str, Any], key: str) -> str:
    return '; '.join(_human(str(item)) for item in _items(data, key))


def _render_prevent_current_legacy(data: dict[str, Any]) -> str:
    consultation = _text(data, 'tipo_consulta') or (
        'CIR' if _text(data, 'tempo').startswith('cir') else 'CAL'
    )
    lines = [
        'LINHA DE CUIDADO — PREVENÇÃO CARDIOVASCULAR E CARDIOMETABÓLICA',
        'FORMULÁRIO CLÍNICO: LC-PREVENCAO v4.0',
        f'Consulta atual: {consultation}.',
    ]
    if consultation == 'CIR':
        lines.extend(['', '14. CIR — CONSULTA DE INTEGRAÇÃO DE RESULTADOS'])
        change = _text(data, 'mudanca_clinica_cir')
        if change:
            lines.extend([
                '14.2. MUDANÇA CLÍNICA DESDE A CAL',
                'Houve mudança clínica relevante: '
                f'{"Sim" if change == "sim" else "Não"}.',
            ])
            if change == 'sim' and _text(data, 'mudanca_clinica_descricao'):
                lines.append(_text(data, 'mudanca_clinica_descricao'))
        if _text(data, 'decisoes_cir'):
            lines.extend([
                '', '14.5. DECISÃO CLÍNICA', _text(data, 'decisoes_cir')
            ])
        if _text(data, 'conduta_adicional'):
            lines.extend([
                '', '18. VISÃO FINAL DA TELA — CIR',
                _text(data, 'conduta_adicional'),
            ])
        return '\n'.join(lines).strip()

    if _text(data, 'intercorrencia'):
        lines.extend([
            '', '4. EVOLUÇÃO CLÍNICA DESDE A ÚLTIMA AVALIAÇÃO',
            'Houve intercorrência clínica: '
            f'{"Sim" if _text(data, "intercorrencia") == "sim" else "Não"}.',
        ])
        if _items(data, 'intercorrencias'):
            lines.append(_confirmed_items(data, 'intercorrencias') + '.')
        if _text(data, 'intercorrencia_conduta'):
            lines.append(_text(data, 'intercorrencia_conduta'))

    pa_situation = _text(data, 'pa_situacao')
    if pa_situation:
        lines.extend(['', '6.1. PRESSÃO ARTERIAL'])
        systolic = _text(data, 'pa_sistolica')
        diastolic = _text(data, 'pa_diastolica')
        if systolic or diastolic:
            lines.append(f'PA atual: {systolic or "?"}/{diastolic or "?"} mmHg.')
        lines.append(f'Situação: {_human(pa_situation)}.')
        if _items(data, 'pa_condutas'):
            lines.append('Conduta: ' + _confirmed_items(data, 'pa_condutas') + '.')
        if _text(data, 'pa_conduta_outra'):
            lines.append('Outra conduta: ' + _text(data, 'pa_conduta_outra'))

    ldl_situation = _text(data, 'ldl_situacao')
    ldl_repeat = _text(data, 'ldl_nova_dosagem')
    if ldl_situation or ldl_repeat:
        lines.extend(['', '6.2. LDL-C E RISCO ATEROSCLERÓTICO'])
        ldl = _text(data, 'ldl_resultado')
        ldl_date = _text(data, 'ldl_data')
        if ldl:
            lines.append(
                f'LDL-C: {ldl} mg/dL{f" em {ldl_date}" if ldl_date else ""}.'
            )
        if ldl_situation:
            lines.append(f'Situação: {_human(ldl_situation)}.')
        if ldl_repeat:
            lines.append(f'Nova dosagem: {_human(ldl_repeat)}.')

    for heading, prefix in (
        ('6.3. CONTROLE GLICÊMICO', 'hba1c'),
        ('6.5. FUNÇÃO RENAL E ALBUMINÚRIA', 'renal'),
    ):
        situation = _text(data, f'{prefix}_situacao')
        repeat = _text(data, f'{prefix}_nova_dosagem') or _text(
            data, f'{prefix}_novo_controle'
        )
        if situation or repeat:
            lines.extend(['', heading])
            if situation:
                lines.append(f'Situação: {_human(situation)}.')
            if repeat:
                lines.append(f'Nova avaliação: {_human(repeat)}.')

    if _text(data, 'conduta_adicional'):
        lines.extend([
            '', '11. PLANO TERAPÊUTICO CONSOLIDADO',
            _text(data, 'conduta_adicional'),
        ])
    if _items(data, 'prioridades'):
        lines.extend([
            '', '12. PRIORIDADES ATÉ A PRÓXIMA AVALIAÇÃO',
            _confirmed_items(data, 'prioridades') + '.',
        ])
    return '\n'.join(lines).strip()


def _render_prevent_current(data: dict[str, Any]) -> str:
    consultation = _text(data, 'tipo_consulta') or (
        'CIR' if _text(data, 'tempo').startswith('cir') else 'CAL'
    )
    lines = ['LINHA DE CUIDADO — PREVENÇÃO CARDIOVASCULAR E CARDIOMETABÓLICA']

    def section(title: str, values: list[str]) -> None:
        filled = [value.strip() for value in values if value and value.strip()]
        if filled:
            lines.extend(['', title, *filled])

    cycle_labels = {
        'cal1': 'CAL 1',
        'cir1': 'CIR/CAL 1 — integração de resultados',
        'cal2': 'CAL 2',
        'cir2': 'CIR/CAL 2 — integração de resultados',
        'cal3': 'CAL 3',
        'cir3': 'CIR/CAL 3 — integração de resultados',
        'retorno_semestral': 'Seguimento semestral',
        'consulta_extra': 'Consulta extraordinária',
    }
    situation_labels = {
        'na_meta': 'NA META',
        'fora_da_meta': 'FORA DA META',
        'adequado': 'ADEQUADA',
        'alterado': 'ALTERADA',
        'sem_resultado_valido': 'SEM RESULTADO VÁLIDO',
        'indeterminado': 'INDETERMINADA',
    }
    conduct_labels = {
        'adesao': 'Avaliar/corrigir adesão ao tratamento',
        'ajustar': 'Ajustar tratamento',
        'mapa': 'Solicitar MAPA',
        'investigar': 'Investigar hipertensão resistente/secundária',
        'manter': 'Manter tratamento atual com justificativa clínica',
        'intensificar': 'Intensificar tratamento hipolipemiante',
        'associar': 'Associar terapia adicional',
        'nutricao': 'Reforçar orientação alimentar',
        'atividade': 'Reforçar atividade física',
        'outra': 'Outra conduta',
        'ecg': 'Eletrocardiograma',
        'holter': 'Holter',
        'eco': 'Ecocardiograma transtorácico',
        'teste_ergometrico': 'Teste ergométrico',
        'escore_calcio': 'Escore de cálcio coronariano',
        'angio_tc': 'Angiotomografia de artérias coronárias',
        'cine': 'Cinecoronariografia',
    }

    def listed(key: str, overrides: dict[str, str] | None = None) -> str:
        labels = {**conduct_labels, **(overrides or {})}
        return '; '.join(labels.get(item, _human(item)) for item in _items(data, key))

    def numeric(key: str) -> float | None:
        raw = _text(data, key).replace(',', '.')
        try:
            return float(''.join(char for char in raw if char in '0123456789.-'))
        except ValueError:
            return None

    pa_situation = _text(data, 'pa_situacao')
    systolic, diastolic = numeric('pa_sistolica'), numeric('pa_diastolica')
    if not pa_situation and systolic is not None and diastolic is not None:
        pa_situation = (
            'na_meta' if systolic < 130 and diastolic < 80 else 'fora_da_meta'
        )
    ldl_situation = _text(data, 'ldl_situacao')
    ldl_result, ldl_target = numeric('ldl_resultado'), numeric('ldl_meta')
    if not ldl_situation and ldl_result is not None and ldl_target is not None:
        ldl_situation = 'na_meta' if ldl_result < ldl_target else 'fora_da_meta'
    hba1c_situation = _text(data, 'hba1c_situacao')
    hba1c_result = numeric('hba1c_resultado')
    if not hba1c_situation and hba1c_result is not None:
        hba1c_target = numeric('hba1c_meta') or 7
        hba1c_situation = (
            'na_meta' if hba1c_result < hba1c_target else 'fora_da_meta'
        )
    renal_situation = _text(data, 'renal_situacao')
    tfg, rac = numeric('tfg'), numeric('rac_resultado')
    if not renal_situation and (tfg is not None or rac is not None):
        renal_situation = (
            'alterado' if (tfg or 90) < 60 or (rac or 0) >= 30 else 'adequado'
        )

    section('POSIÇÃO NA LINHA DE CUIDADO', [
        f'Consulta atual: {consultation}.',
        f'Data-âncora: {_text(data, "data_ancora")}.'
        if _text(data, 'data_ancora') else '',
        f'Momento: {cycle_labels.get(_text(data, "tempo"), _human(_text(data, "tempo")))}.'
        if _text(data, 'tempo') else '',
    ])
    if consultation == 'CIR':
        change = _text(data, 'mudanca_clinica_cir')
        section('INTEGRAÇÃO DE RESULTADOS — CIR', [
            f'Houve mudança clínica relevante: {"Sim" if change == "sim" else "Não"}.'
            if change else '',
            _text(data, 'mudanca_clinica_descricao'),
            _text(data, 'integracao_resultados'),
            _text(data, 'pendencias_decisao'),
        ])
        section('DECISÃO CLÍNICA', [
            _text(data, 'decisoes_cir'),
            _text(data, 'conduta_adicional'),
        ])
        return '\n'.join(lines).strip()

    profile_labels = {
        'hipertensao': 'Hipertensão arterial',
        'dislipidemia': 'Dislipidemia',
        'pre_diabete': 'Pré-diabete',
        'diabete_melito': 'Diabete melito',
        'sindrome_metabolica': 'Síndrome metabólica',
        'doenca_renal_cronica': 'Doença renal crônica',
        'albuminuria': 'Albuminúria',
        'apneia_obstrutiva_sono': 'Apneia obstrutiva do sono',
        'aterosclerose_subclinica': 'Aterosclerose subclínica',
        'doenca_cardiovascular_estabelecida': 'Doença cardiovascular estabelecida',
        'historia_familiar_dcv_prematura': 'História familiar de doença cardiovascular prematura',
    }
    active_profile = [
        label for key, label in profile_labels.items()
        if _text(data, key) == 'sim'
    ]
    section('PERFIL CARDIOVASCULAR E CARDIOMETABÓLICO', [
        f'Condições identificadas: {"; ".join(active_profile)}.'
        if active_profile else '',
        _text(data, 'outros_fatores_relevantes'),
    ])

    section('EVOLUÇÃO CLÍNICA', [
        f'Houve intercorrência clínica: {"Sim" if _text(data, "intercorrencia") == "sim" else "Não"}.'
        if _text(data, 'intercorrencia') else '',
        f'Intercorrências: {_confirmed_items(data, "intercorrencias")}.'
        if _items(data, 'intercorrencias') else '',
        _text(data, 'intercorrencia_outra'),
        _text(data, 'intercorrencia_conduta'),
    ])
    section('AVALIAÇÃO CLÍNICA E EXAME FÍSICO', [
        f'PA atual: {_text(data, "pa_sistolica")}/{_text(data, "pa_diastolica")} mmHg.'
        if _text(data, 'pa_sistolica') or _text(data, 'pa_diastolica') else '',
        f'Frequência cardíaca: {_text(data, "frequencia_cardiaca")} bpm.'
        if _text(data, 'frequencia_cardiaca') else '',
        f'Peso: {_text(data, "peso_atual")} kg.' if _text(data, 'peso_atual') else '',
        f'Altura: {_text(data, "altura")} m.' if _text(data, 'altura') else '',
        f'IMC: {_text(data, "imc")} kg/m².' if _text(data, 'imc') else '',
        f'Peso anterior: {_text(data, "peso_anterior")} kg.'
        if _text(data, 'peso_anterior') else '',
        f'Meta de peso: {_text(data, "meta_peso")} kg.'
        if _text(data, 'meta_peso') else '',
        f'Exame cardiovascular: {_text(data, "exame_cardiovascular_descricao")}.'
        if _text(data, 'exame_cardiovascular_descricao') else '',
        f'Exame respiratório: {_text(data, "exame_respiratorio_descricao")}.'
        if _text(data, 'exame_respiratorio_descricao') else '',
    ])
    ldl_repeat = _text(data, 'ldl_nova_dosagem')
    section('METAS CARDIOMETABÓLICAS', [
        f'Situação da pressão arterial: {situation_labels.get(pa_situation, _human(pa_situation))}.'
        if pa_situation else '',
        f'LDL-C: {_text(data, "ldl_resultado")} mg/dL'
        f'{f" em {_text(data, "ldl_data")}" if _text(data, "ldl_data") else ""}.'
        if _text(data, 'ldl_resultado') else '',
        f'Situação do LDL-C: {situation_labels.get(ldl_situation, _human(ldl_situation))}.'
        if ldl_situation else '',
        f'Meta de LDL-C: {_text(data, "ldl_meta")} mg/dL.'
        if _text(data, 'ldl_meta') else '',
        f'Nova dosagem: {_human(ldl_repeat)}.' if ldl_repeat else '',
        f'HbA1c: {_text(data, "hba1c_resultado")}.'
        if _text(data, 'hba1c_resultado') else '',
        f'Situação da HbA1c: {situation_labels.get(hba1c_situation, _human(hba1c_situation))}.'
        if hba1c_situation else '',
        f'Meta de HbA1c: {_text(data, "hba1c_meta")}%.'
        if _text(data, 'hba1c_meta') else '',
        f'Situação da função renal/albuminúria: {situation_labels.get(renal_situation, _human(renal_situation))}.'
        if renal_situation else '',
        f'Creatinina: {_text(data, "creatinina")} mg/dL.'
        if _text(data, 'creatinina') else '',
        f'TFGe: {_text(data, "tfg")} mL/min/1,73m².'
        if _text(data, 'tfg') else '',
        f'RAC: {_text(data, "rac_resultado")} mg/g.'
        if _text(data, 'rac_resultado') else '',
    ])
    section('ESTILO DE VIDA E FATORES MODIFICÁVEIS', [
        f'Atividade física: {_text(data, "atividade_fisica_conduta")}.'
        if _text(data, 'atividade_fisica_conduta') else '',
        f'Alimentação: {_text(data, "alimentacao_conduta")}.'
        if _text(data, 'alimentacao_conduta') else '',
        f'Tabagismo: {_text(data, "tabagismo_conduta")}.'
        if _text(data, 'tabagismo_conduta') else '',
        f'Sono/AOS: {_text(data, "aos_conduta")}.'
        if _text(data, 'aos_conduta') else '',
    ])
    section('ADESÃO E TOLERÂNCIA AO TRATAMENTO', [
        f'Medicações atuais: {_text(data, "prescricao_atual")}.'
        if _text(data, 'prescricao_atual') else '',
        f'Adesão: {_human(_text(data, "adesao"))}.' if _text(data, 'adesao') else '',
        f'Barreiras à adesão: {_text(data, "adesao_barreiras")}.'
        if _text(data, 'adesao_barreiras') else '',
        f'Efeitos adversos/intolerância: {_human(_text(data, "efeitos_adversos"))}.'
        if _text(data, 'efeitos_adversos') else '',
        _text(data, 'efeitos_adversos_descricao'),
    ])
    section('EXAMES E INVESTIGAÇÕES COMPLEMENTARES', [
        f'Exames selecionados para solicitação: {_confirmed_items(data, "exames_solicitados")}.'
        if _items(data, 'exames_solicitados') else '',
        f'Avaliação cardiovascular complementar: {listed("avaliacao_cardiovascular_complementar")}.'
        if _items(data, 'avaliacao_cardiovascular_complementar') else '',
        _text(data, 'outro_exame_cardiovascular'),
        _text(data, 'outra_investigacao'),
    ])
    section('PLANO TERAPÊUTICO CONSOLIDADO', [
        f'Pressão arterial: {listed("pa_condutas", {"ajustar": "Ajustar tratamento anti-hipertensivo"})}.'
        if _items(data, 'pa_condutas') else '',
        _text(data, 'pa_manter_justificativa'),
        _text(data, 'pa_conduta_outra'),
        f'LDL-C: {listed("ldl_condutas")}.' if _items(data, 'ldl_condutas') else '',
        _text(data, 'ldl_manter_justificativa'),
        _text(data, 'ldl_conduta_outra'),
        f'Controle glicêmico: {listed("hba1c_condutas", {"ajustar": "Ajustar tratamento do diabete melito"})}.'
        if _items(data, 'hba1c_condutas') else '',
        _text(data, 'hba1c_conduta_outra'),
        _text(data, 'atividade_fisica_conduta'),
        _text(data, 'alimentacao_conduta'),
        _text(data, 'tabagismo_conduta'),
        _text(data, 'aos_conduta'),
        _text(data, 'conduta_adicional'),
    ])
    section('PRIORIDADES ATÉ A PRÓXIMA AVALIAÇÃO', [
        f'{_confirmed_items(data, "prioridades")}.'
        if _items(data, 'prioridades') else '',
        _text(data, 'prioridade_outra'),
    ])
    section('PRÓXIMO PASSO', [
        f'Próxima etapa: {cycle_labels.get(_text(data, "proxima_etapa"), _human(_text(data, "proxima_etapa")))}.'
        if _text(data, 'proxima_etapa') else '',
        f'Data prevista: {_text(data, "data_proxima_etapa")}.'
        if _text(data, 'data_proxima_etapa') else '',
        f'Retorno extraordinário em {_text(data, "retorno_extraordinario_dias")} dias.'
        if _text(data, 'retorno_extraordinario_dias') else '',
    ])
    return '\n'.join(lines).strip()


def _render_ic_current(data: dict[str, Any]) -> str:
    consultation = _text(data, 'tipo_consulta') or (
        'CIR' if _text(data, 'tempo').startswith('cir') else 'CAL'
    )
    lines = [
        'LINHA DE CUIDADO — INSUFICIÊNCIA CARDÍACA',
        'FORMULÁRIO CLÍNICO: LC-IC v4.0',
        f'Consulta atual: {consultation}.',
    ]
    if consultation == 'CIR':
        lines.extend(['', '18. CIR — CONSULTA DE INTEGRAÇÃO DE RESULTADOS'])
        change = _text(data, 'mudanca_clinica_cir')
        if change:
            lines.extend([
                '18.2. Mudança clínica desde a CAL',
                'Houve mudança clínica relevante: '
                f'{"Sim" if change == "sim" else "Não"}.',
            ])
        if _text(data, 'mudanca_clinica_descricao'):
            lines.append(_text(data, 'mudanca_clinica_descricao'))
        if _text(data, 'decisoes_cir'):
            lines.extend([
                '', '18.3. Integração dos resultados',
                _text(data, 'decisoes_cir'),
            ])
        if _text(data, 'observacao_decisao_adicional'):
            lines.extend([
                '', '18.4. Fechamento da CIR',
                _text(data, 'observacao_decisao_adicional'),
            ])
        return '\n'.join(lines).strip()

    moment_labels = {
        'cal1': 'CAL1', 'cir1': 'CIR1',
        'cal2': 'CAL2', 'cir2': 'CIR2',
        'cal3': 'CAL3', 'cir3': 'CIR3',
        'retorno_semestral': 'Seguimento semestral',
        'consulta_extra': 'Consulta extraordinária',
    }
    identification: list[str] = []
    if _text(data, 'data_ancora'):
        identification.append(
            f'Início na Linha de Cuidado: {_text(data, "data_ancora")}.'
        )
    if _text(data, 'tempo'):
        identification.append(
            'Momento atual da Linha: '
            + moment_labels.get(_text(data, 'tempo'), _human(_text(data, 'tempo')))
            + '.'
        )
    if _text(data, 'ultima_avaliacao'):
        identification.append(
            'Última avaliação da Linha: '
            + _text(data, 'ultima_avaliacao') + '.'
        )
    if _text(data, 'feve'):
        identification.append(f'Última FEVE: {_text(data, "feve")}%.' )
    if _text(data, 'proxima_etapa'):
        identification.append(
            'Próxima etapa prevista: '
            + moment_labels.get(
                _text(data, 'proxima_etapa'),
                _human(_text(data, 'proxima_etapa')),
            )
            + '.'
        )
    if identification:
        lines.extend(['', '3. IDENTIFICAÇÃO DO CICLO', *identification])

    physical: list[str] = []
    if _text(data, 'altura'):
        physical.append(f'Altura: {_text(data, "altura")}.')
    if _text(data, 'imc'):
        physical.append(f'IMC: {_text(data, "imc")}.')
    if _text(data, 'exame_dirigido_outro'):
        physical.append(
            'Outras alterações no exame físico: '
            + _text(data, 'exame_dirigido_outro') + '.'
        )
    if physical:
        lines.extend(['', '5. EXAME FÍSICO', *physical])

    phenotype = _text(data, 'fenotipo_atual')
    phenotype_label = {
        'FEVE_MELHORADA': 'FEVE melhorada',
    }.get(phenotype, phenotype)
    feve = _text(data, 'feve')
    feve_date = _text(data, 'feve_data')
    if phenotype or feve:
        lines.extend(['', '6. FENÓTIPO DA INSUFICIÊNCIA CARDÍACA'])
        if feve:
            lines.append(
                f'FEVE: {feve}%{f" em {feve_date}" if feve_date else ""}.'
            )
        if phenotype:
            lines.append(f'Fenótipo atual: {phenotype_label}.')

    risk = _text(data, 'risco_situacao')
    risk_criterion_labels = {
        'feve_menor_35': 'FEVE < 35%',
        'bnp_elevado': 'BNP/NT-proBNP elevado',
        'disfuncao_vd': 'Disfunção ventricular direita',
        'hiponatremia': 'Hiponatremia',
        'hipotensao': 'Hipotensão',
        'internacao_ic_6m': 'Uma ou mais internações por IC nos últimos 6 meses',
        'emergencias_12m': '≥2 atendimentos na Emergência nos últimos 12 meses',
        'arritmia_ventricular': 'Arritmia ventricular',
        'intolerancia_quatro_pilares': 'Intolerância aos quatro pilares',
        'sincope': 'Síncope',
        'piora_rapida_dispneia': 'Piora rápida da dispneia',
        'congestao_persistente': 'Congestão persistente',
        'piora_renal_progressiva': 'Piora renal progressiva',
        'necessidade_crescente_diuretico': 'Necessidade crescente de diurético',
    }
    risk_criteria = [
        risk_criterion_labels.get(str(item), str(item))
        for item in _items(data, 'criterios_alto_risco')
    ]
    risk_conduct_labels = {
        'antecipar': 'Antecipar retorno',
        'navegacao': 'Intensificar monitoramento/navegação',
        'revisar': 'Revisar tratamento',
        'especialista': 'Discutir com especialista em IC',
    }
    risk_conducts = [
        risk_conduct_labels.get(str(item), _human(str(item)))
        for item in _items(data, 'condutas_alto_risco')
    ]
    if risk or risk_criteria or risk_conducts:
        lines.extend(['', '7. ESTRATIFICAÇÃO DE RISCO'])
        if risk:
            lines.append(f'Situação: {_human(risk)}.')
        if risk_criteria:
            lines.append('Critérios identificados: ' + '; '.join(risk_criteria) + '.')
        if risk_conducts:
            lines.append('Condutas: ' + '; '.join(risk_conducts) + '.')

    etiology: list[str] = []
    if _text(data, 'etiologia_status'):
        etiology_status_labels = {
            'suspeita': 'Suspeita',
            'confirmada': 'Confirmada',
            'em_investigacao': 'Em investigação',
        }
        etiology.append(
            'Etiologia da IC: '
            + etiology_status_labels.get(
                _text(data, 'etiologia_status'),
                _human(_text(data, 'etiologia_status')),
            ) + '.'
        )
    valvar_labels = {
        'sem_relevante': 'Sem valvopatia relevante',
        'possivelmente_relacionada': 'Valvopatia possivelmente relacionada à IC',
        'moderada_importante': 'Valvopatia moderada/importante',
    }
    valvar_conduct_labels = {
        'heart_team': 'Encaminhar para Heart Team',
        'nao_indicar': 'Não indicar neste momento',
    }
    if _text(data, 'investigacao_valvar_classificacao'):
        etiology.append(
            'Doença valvar: '
            + valvar_labels.get(
                _text(data, 'investigacao_valvar_classificacao'),
                _human(_text(data, 'investigacao_valvar_classificacao')),
            ) + '.'
        )
    if _text(data, 'investigacao_valvar_conduta'):
        etiology.append(
            'Conduta valvar: '
            + valvar_conduct_labels.get(
                _text(data, 'investigacao_valvar_conduta'),
                _human(_text(data, 'investigacao_valvar_conduta')),
            ) + '.'
        )
    if etiology:
        lines.extend(['', '8. INVESTIGAÇÃO ETIOLÓGICA', *etiology])

    if _text(data, 'medicacoes_atuais'):
        lines.extend([
            '', '9. TRATAMENTO MEDICAMENTOSO',
            'Medicações atuais:', _text(data, 'medicacoes_atuais'),
        ])

    phenotype_heading = {
        'ICFEr': '10. TRATAMENTO — ICFEr',
        'icfer': '10. TRATAMENTO — ICFEr',
        'ICFEmi': '11. TRATAMENTO — ICFEmi',
        'icfemi': '11. TRATAMENTO — ICFEmi',
        'ICFEP': '12. TRATAMENTO — ICFEP',
        'icfep': '12. TRATAMENTO — ICFEP',
    }.get(phenotype)
    treatment_lines: list[str] = []
    if phenotype_heading == '10. TRATAMENTO — ICFEr':
        labels = {
            'arni': 'ARNI/IECA/BRA',
            'betabloqueador': 'Betabloqueador',
            'arm': 'ARM',
            'isglt2': 'iSGLT2',
        }
        decisions = {
            'manter': 'Manter', 'iniciar': 'Iniciar', 'titular': 'Titular',
            'contraindicacao': 'Contraindicação/intolerância',
        }
        for key, label in labels.items():
            decision = _text(data, f'pilar_{key}_decisao')
            if decision:
                treatment_lines.append(
                    f'{label}: {decisions.get(decision, decision)}.'
                )
    elif phenotype_heading == '11. TRATAMENTO — ICFEmi':
        if _text(data, 'icfemi_isglt2'):
            treatment_lines.append(
                'iSGLT2: ' + _human(_text(data, 'icfemi_isglt2')) + '.'
            )
    elif phenotype_heading == '12. TRATAMENTO — ICFEP':
        if _text(data, 'icfep_isglt2'):
            treatment_lines.append(
                'iSGLT2: ' + _human(_text(data, 'icfep_isglt2')) + '.'
            )
    if phenotype_heading and treatment_lines:
        lines.extend(['', phenotype_heading, *treatment_lines])

    if _text(data, 'ferro_endovenoso_decisao'):
        lines.extend([
            '', '13. DEFICIÊNCIA DE FERRO',
            'Decisão médica: '
            + _human(_text(data, 'ferro_endovenoso_decisao')) + '.',
        ])
    if _text(data, 'cdi_decisao') or _text(data, 'trc_decisao'):
        lines.append('')
        lines.append('14. DISPOSITIVOS — CDI E TRC')
        if _text(data, 'cdi_decisao'):
            lines.append('CDI: ' + _human(_text(data, 'cdi_decisao')) + '.')
        if _text(data, 'trc_decisao'):
            lines.append('TRC: ' + _human(_text(data, 'trc_decisao')) + '.')
    advanced_signals = [str(item) for item in _items(data, 'sinais_ic_avancada')]
    advanced_conduct_labels = {
        'intensificar': 'Intensificar acompanhamento',
        'rever': 'Rever tratamento e causas potencialmente corrigíveis',
        'especialista': 'Encaminhar/discutir com cardiologista Sênior/especialista em IC',
    }
    advanced_conducts = [
        advanced_conduct_labels.get(str(item), _human(str(item)))
        for item in _items(data, 'condutas_ic_avancada')
    ]
    if advanced_signals or advanced_conducts:
        lines.extend(['', '15. AVALIAÇÃO PARA IC AVANÇADA'])
        if advanced_signals:
            lines.append('Sinais de alerta: ' + '; '.join(advanced_signals) + '.')
        if advanced_conducts:
            lines.append('Condutas: ' + '; '.join(advanced_conducts) + '.')
    if _text(data, 'observacao_decisao_adicional'):
        lines.extend([
            '', '17. PLANO TERAPÊUTICO CONSOLIDADO',
            _text(data, 'observacao_decisao_adicional'),
        ])
    return '\n'.join(lines).strip()


def render_form(payload: FormInput) -> str:
    if payload.versao not in SUPPORTED_FORM_VERSIONS:
        raise HTTPException(
            status_code=422, detail='Versão de formulário não suportada.'
        )
    renderers = {
        'LC-DAC': _render_dac,
        'LC-IC': _render_ic,
        'LC-PREVENCAO': _render_prevent,
    }
    if payload.versao == '4.0' and payload.linha_cuidado == 'LC-IC':
        text = _render_ic_current(payload.dados)
    elif payload.versao == '4.0' and payload.linha_cuidado == 'LC-PREVENCAO':
        text = _render_prevent_current(payload.dados)
    elif payload.linha_cuidado == 'LC-DAC' and payload.versao in {'2.0', '3.0', '3.1', '4.0'}:
        text = _render_dac_v2(payload.dados, payload.versao)
    else:
        text = renderers[payload.linha_cuidado](payload.dados)
    if len(text) > LIMITE_TEXTO_MV:
        raise HTTPException(
            status_code=422,
            detail=(
                f'O texto gerado possui {len(text)} caracteres e ultrapassa '
                f'o limite de {LIMITE_TEXTO_MV} do MV.'
            ),
        )
    return text


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'cd_pre_med': row['cd_pre_med'],
        'cd_atendimento': row['cd_atendimento'],
        'linha_cuidado': row['linha_cuidado'],
        'versao': row['versao'],
        'dados': json.loads(row['dados_json']),
        'texto_mv': row['texto_mv'],
        'caracteres': len(row['texto_mv']),
        'copiado_de_cd_pre_med': row['copiado_de_cd_pre_med'],
        'criado_em': row['criado_em'],
        'atualizado_em': row['atualizado_em'],
    }


def latest_previous_form_value(
    cd_atendimento: int,
    linha_cuidado: str,
    field_key: str,
) -> str | None:
    """Return the newest non-empty structured value for this care line."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT dados_json
              FROM formularios_evolucao
             WHERE cd_atendimento = ?
               AND linha_cuidado = ?
             ORDER BY atualizado_em DESC
            """,
            (cd_atendimento, linha_cuidado),
        ).fetchall()
    for row in rows:
        try:
            value = json.loads(row['dados_json']).get(field_key)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_initialize_database()

app = FastAPI(
    title='Evolução / SADT - Formulários clínicos',
    description=(
        'Formulários versionados das linhas de cuidado LC-DAC, LC-IC e '
        'LC-PREVENCAO.'
    ),
)


@app.get('/status')
def status():
    return {
        'status': 'ok',
        'versao_formulario': FORM_VERSION,
        'linhas': sorted(SUPPORTED_LINES),
        'limite_texto_mv': LIMITE_TEXTO_MV,
    }


@app.post('/renderizar')
def render(payload: FormInput):
    text = render_form(payload)
    return {
        'texto_mv': text,
        'caracteres': len(text),
        'limite': LIMITE_TEXTO_MV,
    }


@app.get('/lista')
def list_forms(cd_atendimento: int = Query(gt=0)):
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM formularios_evolucao
            WHERE cd_atendimento = ?
            ORDER BY atualizado_em DESC
            """,
            (cd_atendimento,),
        ).fetchall()
    return {'formularios': [_record(row) for row in rows]}


@app.get('/{cd_pre_med}', response_model=FormRecord)
def get_form(cd_pre_med: int):
    with _connect() as connection:
        row = connection.execute(
            'SELECT * FROM formularios_evolucao WHERE cd_pre_med = ?',
            (cd_pre_med,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail='Formulário estruturado não encontrado.'
        )
    return _record(row)


@app.put('/{cd_pre_med}', response_model=FormRecord)
def save_form(cd_pre_med: int, payload: FormInput):
    text = render_form(payload)
    now = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(
        payload.dados, ensure_ascii=False, separators=(',', ':')
    )
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    with _connect() as connection:
        existing = connection.execute(
            'SELECT criado_em FROM formularios_evolucao WHERE cd_pre_med = ?',
            (cd_pre_med,),
        ).fetchone()
        created_at = existing['criado_em'] if existing else now
        connection.execute(
            """
            INSERT INTO formularios_evolucao (
                cd_pre_med, cd_atendimento, linha_cuidado, versao,
                dados_json, texto_mv, texto_hash, copiado_de_cd_pre_med,
                criado_em, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cd_pre_med) DO UPDATE SET
                cd_atendimento = excluded.cd_atendimento,
                linha_cuidado = excluded.linha_cuidado,
                versao = excluded.versao,
                dados_json = excluded.dados_json,
                texto_mv = excluded.texto_mv,
                texto_hash = excluded.texto_hash,
                copiado_de_cd_pre_med = excluded.copiado_de_cd_pre_med,
                atualizado_em = excluded.atualizado_em
            """,
            (
                cd_pre_med,
                payload.cd_atendimento,
                payload.linha_cuidado,
                payload.versao,
                data_json,
                text,
                text_hash,
                payload.copiado_de_cd_pre_med,
                created_at,
                now,
            ),
        )
        row = connection.execute(
            'SELECT * FROM formularios_evolucao WHERE cd_pre_med = ?',
            (cd_pre_med,),
        ).fetchone()
    return _record(row)
