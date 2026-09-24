import re
import unicodedata
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session


def _normalized(value: Any) -> str:
    raw = str(value or '').strip().lower()
    return ''.join(
        char
        for char in unicodedata.normalize('NFKD', raw)
        if not unicodedata.combining(char)
    )


def extract_numeric_result(value: Any) -> str | None:
    raw = str(value or '').strip()
    if not re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', raw):
        return None
    return raw.replace(',', '.')


def detect_lc_dac_medications(descriptions: Iterable[Any]) -> dict[str, str]:
    raw_items = [str(item).strip() for item in descriptions if item]
    items = [_normalized(item) for item in raw_items]
    joined = '\n'.join(items)
    detected: dict[str, str] = {}

    statin = any(name in joined for name in ('atorvastatina', 'rosuvastatina'))
    if statin:
        detected['estatina'] = 'sim'
        statin_index = next(
            index
            for index, item in enumerate(items)
            if 'atorvastatina' in item or 'rosuvastatina' in item
        )
        detected['estatina_farmaco_dose'] = raw_items[statin_index]
        high_intensity = any(
            re.search(pattern, item)
            for item in items
            for pattern in (
                r'atorvastatina\D*(?:40|80)\s*mg',
                r'rosuvastatina\D*(?:20|40)\s*mg',
            )
        )
        detected['estatina_alta_intensidade'] = (
            'sim' if high_intensity else 'nao'
        )

    if any(
        term in joined
        for term in (
            'acido acetilsalicilico',
            'somalgin',
            'clopin duo',
        )
    ) or re.search(r'(^|\W)aas($|\W)', joined):
        detected['aas'] = 'sim'

    for key in ('clopidogrel', 'ticagrelor', 'prasugrel'):
        if key in joined:
            detected['segundo_antiagregante'] = key
            break

    anticoagulants = (
        'apixabana',
        'dabigatrana',
        'edoxabana',
        'rivaroxabana',
        'varfarina',
        'warfarin',
        'marevan',
        'coumadin',
    )
    if any(name in joined for name in anticoagulants):
        detected['anticoagulante'] = 'sim'
        second = detected.get('segundo_antiagregante')
        if detected.get('aas') == 'sim' and second:
            detected['anticoagulante_estrategia'] = 'tripla'
        elif second:
            detected['anticoagulante_estrategia'] = second
        elif detected.get('aas') == 'sim':
            detected['anticoagulante_estrategia'] = 'aas'
        else:
            detected['anticoagulante_estrategia'] = 'isolado'
    return detected


def _serialize_date(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value) if value else None


def _latest_lab(
    rows: list[dict[str, Any]],
    kind: str,
    *,
    max_age_days: int | None = None,
    today: date | None = None,
) -> dict[str, Any] | None:
    matchers = {
        'ldl': ('ldl',),
        'hba1c': ('hemoglobina glicada', 'hba1c', 'a1c'),
    }[kind]
    for row in rows:
        label = _normalized(
            f'{row.get("nm_exa_lab", "")} {row.get("nm_campo", "")}'
        )
        if not any(term in label for term in matchers):
            continue
        if max_age_days is not None:
            raw_date = row.get('dt_laudo')
            if isinstance(raw_date, datetime):
                result_date = raw_date.date()
            elif isinstance(raw_date, date):
                result_date = raw_date
            else:
                try:
                    result_date = date.fromisoformat(str(raw_date)[:10])
                except (TypeError, ValueError):
                    continue
            if ((today or date.today()) - result_date).days > max_age_days:
                continue
        numeric = extract_numeric_result(row.get('ds_resultado'))
        if numeric is None:
            continue
        return {
            'valor': numeric,
            'data': _serialize_date(row.get('dt_laudo')),
            'exame': row.get('nm_exa_lab'),
            'campo': row.get('nm_campo'),
            'fonte': 'MV/RES_EXA',
        }
    return None


def _latest_protocol_labs(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    matchers = {
        'Hemoglobina': lambda label: (
            'hemoglobina' in label and 'glicada' not in label
        ),
        'Hematócrito': lambda label: 'hematocrito' in label,
        'Perfil lipídico': lambda label: (
            'perfil lipidico' in label or re.search(r'(^|\W)ldl($|\W)', label)
        ),
        'Glicemia': lambda label: 'glicemia' in label or 'glicose' in label,
        'HbA1c': lambda label: any(
            term in label for term in ('hemoglobina glicada', 'hba1c', 'a1c')
        ),
        'TGO': lambda label: 'tgo' in label or re.search(r'(^|\W)ast($|\W)', label),
        'TGP': lambda label: 'tgp' in label or re.search(r'(^|\W)alt($|\W)', label),
        'Ureia': lambda label: 'ureia' in label,
        'Creatinina': lambda label: 'creatinina' in label,
        'Sódio': lambda label: 'sodio' in label,
        'Potássio': lambda label: 'potassio' in label,
        'CPK': lambda label: 'cpk' in label or 'creatinofosfoquinase' in label,
    }
    results: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = _normalized(
            f'{row.get("nm_exa_lab", "")} {row.get("nm_campo", "")}'
        )
        numeric = extract_numeric_result(row.get('ds_resultado'))
        if numeric is None:
            continue
        for exam, matches in matchers.items():
            if exam in results or not matches(label):
                continue
            results[exam] = {
                'valor': numeric,
                'data': _serialize_date(row.get('dt_laudo')),
                'exame': row.get('nm_exa_lab'),
                'campo': row.get('nm_campo'),
                'fonte': 'MV/RES_EXA',
            }
    return results


def load_lc_dac_support(
    session: Session, cd_atendimento: int
) -> dict[str, Any]:
    medication_rows = session.execute(
        text(
            '''
            SELECT tp.DS_TIP_PRESC
              FROM DBAMV.PRE_MED pm
              JOIN DBAMV.ITPRE_MED ipm
                ON ipm.CD_PRE_MED = pm.CD_PRE_MED
              JOIN DBAMV.TIP_PRESC tp
                ON tp.CD_TIP_PRESC = ipm.CD_TIP_PRESC
             WHERE pm.CD_ATENDIMENTO = :cd_atendimento
               AND pm.CD_PRE_MED = (
                    SELECT MAX(pm2.CD_PRE_MED)
                      FROM DBAMV.PRE_MED pm2
                     WHERE pm2.CD_ATENDIMENTO = :cd_atendimento
                       AND EXISTS (
                            SELECT 1
                              FROM DBAMV.ITPRE_MED ipm2
                             WHERE ipm2.CD_PRE_MED = pm2.CD_PRE_MED
                               AND NVL(ipm2.SN_CANCELADO, 'N') = 'N'
                       )
               )
               AND NVL(ipm.SN_CANCELADO, 'N') = 'N'
             ORDER BY ipm.CD_ITPRE_MED
            '''
        ),
        {'cd_atendimento': cd_atendimento},
    ).mappings().all()
    descriptions = [row.get('ds_tip_presc') for row in medication_rows]

    lab_rows = session.execute(
        text(
            '''
            SELECT *
              FROM (
                    SELECT ex.NM_EXA_LAB,
                           re.NM_CAMPO,
                           re.DS_RESULTADO,
                           il.DT_LAUDO
                      FROM DBAMV.PED_LAB pl
                      JOIN DBAMV.ATENDIME ate
                        ON ate.CD_ATENDIMENTO = pl.CD_ATENDIMENTO
                      JOIN DBAMV.ITPED_LAB il
                        ON il.CD_PED_LAB = pl.CD_PED_LAB
                      JOIN DBAMV.EXA_LAB ex
                        ON ex.CD_EXA_LAB = il.CD_EXA_LAB
                      JOIN DBAMV.RES_EXA re
                        ON re.CD_PED_LAB = il.CD_PED_LAB
                       AND re.CD_ITPED_LAB = il.CD_ITPED_LAB
                       AND re.CD_EXA_LAB = il.CD_EXA_LAB
                       AND re.CD_VERSAO = il.CD_VERSAO
                     WHERE ate.CD_PACIENTE = (
                            SELECT CD_PACIENTE
                              FROM DBAMV.ATENDIME
                             WHERE CD_ATENDIMENTO = :cd_atendimento
                       )
                       AND il.DT_LAUDO IS NOT NULL
                       AND il.DT_LAUDO >= SYSDATE - 180
                       AND (
                            UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%HEMOGLOB%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%HEMAT%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%PERFIL LIP%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%LDL%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%GLICADA%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%HBA1C%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%GLICEM%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%GLICOSE%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%CREATIN%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%UREIA%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%SODIO%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%POTASSIO%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%TGO%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%TGP%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%AST%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%ALT%'
                            OR UPPER(ex.NM_EXA_LAB || ' ' || re.NM_CAMPO)
                                LIKE '%CPK%'
                       )
                     ORDER BY il.DT_LAUDO DESC,
                              il.HR_LAUDO DESC,
                              pl.CD_PED_LAB DESC,
                              il.CD_ITPED_LAB
                   )
             WHERE ROWNUM <= 100
            '''
        ),
        {'cd_atendimento': cd_atendimento},
    ).mappings().all()
    labs = [{str(key).lower(): value for key, value in row.items()} for row in lab_rows]

    return {
        'cd_atendimento': cd_atendimento,
        'medicacoes': detect_lc_dac_medications(descriptions),
        'itens_ultima_prescricao': [str(item) for item in descriptions if item],
        'ldl': _latest_lab(labs, 'ldl'),
        'hba1c': _latest_lab(labs, 'hba1c', max_age_days=90),
        'exames_protocolares': _latest_protocol_labs(labs),
    }
