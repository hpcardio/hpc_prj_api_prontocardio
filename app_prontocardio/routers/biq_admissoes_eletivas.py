import unicodedata
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.routers.biq import ValidaUsuarioAtual, _json_value

router = APIRouter(prefix='/biq', tags=['biq'])
VALUE = Decimal('70.00')
HEADER = '### ADMISSAO MEDICA PARA PROCEDIMENTO ELETIVO ###'

QUERY = text("""
WITH remessas_distintas AS (
    SELECT DISTINCT rf.CD_ATENDIMENTO, rf.CD_REMESSA
      FROM DBAMV.REG_FAT rf
     WHERE rf.CD_REMESSA IS NOT NULL
), remessas AS (
    SELECT rd.CD_ATENDIMENTO,
           LISTAGG(TO_CHAR(rd.CD_REMESSA), ', ') WITHIN GROUP (ORDER BY rd.CD_REMESSA) AS REMESSA
      FROM remessas_distintas rd
     GROUP BY rd.CD_ATENDIMENTO
), faturamento AS (
    SELECT rf.CD_ATENDIMENTO,
           CASE WHEN COUNT(rf.CD_REMESSA) > 0 THEN 1 ELSE 0 END AS EM_REMESSA_FATURAMENTO,
           MAX(r.REMESSA) AS REMESSA,
           MAX(rf.CD_REG_FAT) AS CONTA_FATURAMENTO
      FROM DBAMV.REG_FAT rf
      LEFT JOIN remessas r ON r.CD_ATENDIMENTO = rf.CD_ATENDIMENTO
     GROUP BY rf.CD_ATENDIMENTO
)
SELECT p.CD_PRE_MED AS "cd_pre_med",
       p.CD_DOCUMENTO_CLINICO AS "cd_documento_clinico",
       p.CD_ATENDIMENTO AS "cd_atendimento",
       p.CD_PRESTADOR AS "cd_prestador",
       pr.NM_PRESTADOR AS "nm_prestador",
       pr.DS_CODIGO_CONSELHO AS "crm",
       a.CD_PACIENTE AS "cd_paciente",
       pac.NM_PACIENTE AS "nm_paciente",
       a.CD_CONVENIO AS "cd_convenio",
       (SELECT c.NM_CONVENIO FROM DBAMV.CONVENIO c WHERE c.CD_CONVENIO = a.CD_CONVENIO) AS "nm_convenio",
       a.DT_ATENDIMENTO AS "dt_atendimento",
       d.DH_FECHAMENTO AS "dh_fechamento",
       p.SN_FECHADO AS "sn_fechado",
       d.TP_STATUS AS "tp_status",
       p.DS_EVOLUCAO AS "texto",
       NVL(f.EM_REMESSA_FATURAMENTO, 0) AS "em_remessa_faturamento",
       f.REMESSA AS "remessa",
       f.CONTA_FATURAMENTO AS "conta_faturamento"
  FROM DBAMV.PRE_MED p
  JOIN DBAMV.PW_DOCUMENTO_CLINICO d
    ON d.CD_DOCUMENTO_CLINICO = p.CD_DOCUMENTO_CLINICO
  JOIN DBAMV.ATENDIME a ON a.CD_ATENDIMENTO = p.CD_ATENDIMENTO
  JOIN DBAMV.PRESTADOR pr ON pr.CD_PRESTADOR = p.CD_PRESTADOR
  JOIN DBAMV.PACIENTE pac ON pac.CD_PACIENTE = a.CD_PACIENTE
  LEFT JOIN faturamento f ON f.CD_ATENDIMENTO = a.CD_ATENDIMENTO
 WHERE d.DH_FECHAMENTO >= :data_inicio
   AND d.DH_FECHAMENTO < :data_fim_exclusiva
   AND d.CD_TIPO_DOCUMENTO = 36
   AND p.CD_OBJETO = 511
   AND p.TP_PRE_MED = 'M'
   AND p.SN_FECHADO = 'S'
   AND d.TP_STATUS = 'FECHADO'
   AND a.TP_ATENDIMENTO = 'I'
   AND p.CD_PRESTADOR IS NOT NULL
 ORDER BY d.DH_FECHAMENTO DESC, p.CD_PRE_MED DESC
""")


def _normalize(value) -> str:
    normalized = unicodedata.normalize('NFD', str(value or ''))
    return ' '.join(
        ''
        .join(
            char for char in normalized if unicodedata.category(char) != 'Mn'
        )
        .upper()
        .split()
    )


def build_payload(rows, data_inicio: date, data_fim: date) -> dict:
    admissions = []
    doctors = {}
    for source in rows:
        row = dict(source)
        closed = (
            row.get('sn_fechado') == 'S'
            and row.get('tp_status') == 'FECHADO'
            and row.get('dh_fechamento') is not None
        )
        if not closed or not _normalize(row.pop('texto', '')).startswith(
            HEADER
        ):
            continue
        item = {
            key: _json_value(value)
            for key, value in row.items()
            if key not in {'sn_fechado', 'tp_status'}
        }
        item['valor'] = float(VALUE)
        admissions.append(item)
        code = int(item['cd_prestador'])
        doctor = doctors.setdefault(
            code,
            {
                'cd_prestador': code,
                'nm_prestador': item['nm_prestador'],
                'crm': item['crm'],
                'quantidade_admissoes': 0,
                'valor_total': 0.0,
            },
        )
        doctor['quantidade_admissoes'] += 1
        doctor['valor_total'] = float(
            Decimal(str(doctor['valor_total'])) + VALUE
        )
    summary = sorted(
        doctors.values(),
        key=lambda item: (
            -item['quantidade_admissoes'],
            item['nm_prestador'] or '',
        ),
    )
    return {
        'periodo': {
            'data_inicio': data_inicio.isoformat(),
            'data_fim': data_fim.isoformat(),
        },
        'valor_unitario': float(VALUE),
        'total_admissoes': len(admissions),
        'valor_total': float(VALUE * len(admissions)),
        'medicos': summary,
        'admissoes': admissions,
    }


@router.get('/admissoes-eletivas')
def consultar_admissoes_eletivas(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    if data_fim < data_inicio:
        return build_payload([], data_inicio, data_fim)
    rows = session.execute(
        QUERY,
        {
            'data_inicio': data_inicio,
            'data_fim_exclusiva': data_fim + timedelta(days=1),
        },
    ).mappings()
    return build_payload(rows, data_inicio, data_fim)
