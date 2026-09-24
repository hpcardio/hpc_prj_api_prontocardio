import base64
import io
import json
import os
import re
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageFile
from pydantic import BaseModel, Field, field_validator
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from sqlalchemy import text

from app_prontocardio.database import oracle_engine
from evolucao_sadt_backend.ipm_guides import ipm_page_count, select_ipm_models
BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / 'evolucao_sadt_assets'
MV_WRITE_ENABLED = (
    os.getenv('SADT_MV_WRITE_ENABLED', 'false').strip().lower() == 'true'
)
MV_PW_TIPO_DOCUMENTO_REQUISICAO_MEDICA = 2
MV_STATUS_ARQUIVO_ANEXADO = 2
MV_OBJETO_ANEXO_PRONTUARIO = 397
MV_STATUS_DOCUMENTO_FECHADO = 'FECHADO'
MV_EXTENSAO_DOCUMENTO_ANEXO = 'PDF_ANEXO'
ImageFile.LOAD_TRUNCATED_IMAGES = True


def _load_json(name: str):
    return json.loads((ASSET_DIR / name).read_text(encoding='utf-8'))


CONVENIOS = _load_json('convenios.json')
TUSS = _load_json('tuss.json')
TUSS_BY_CODE = {item['codigo']: item for item in TUSS}
DOCTOR_LOGO_PATH = ASSET_DIR / 'doctor-logo.jpg'

font_path = Path(r'C:\Windows\Fonts\arial.ttf')
font_bold_path = Path(r'C:\Windows\Fonts\arialbd.ttf')
if font_path.exists() and font_bold_path.exists():
    pdfmetrics.registerFont(TTFont('SADTRegular', str(font_path)))
    pdfmetrics.registerFont(TTFont('SADTBold', str(font_bold_path)))
    PDF_FONT = 'SADTRegular'
    PDF_FONT_BOLD = 'SADTBold'
else:
    PDF_FONT = 'Helvetica'
    PDF_FONT_BOLD = 'Helvetica-Bold'


class ExamRequest(BaseModel):
    codigo: str = Field(min_length=1, max_length=16)
    descricao: str = Field(min_length=1, max_length=350)
    tipo: str = Field(default='procedimento', max_length=30)


class PatientRequest(BaseModel):
    cd_atendimento: int
    cd_paciente: int
    nome: str = Field(min_length=2, max_length=200)
    data_nascimento: str | None = None
    sexo: str | None = None
    cns: str | None = None
    convenio: str = Field(default='', max_length=200)
    convenio_id: str | None = Field(default=None, max_length=80)
    plano: str = Field(default='', max_length=200)
    carteira: str = Field(default='', max_length=80)
    validade_carteira: str | None = None
    tipo_atendimento: str | None = Field(default=None, max_length=40)
    telefone: str | None = Field(default=None, max_length=80)


class DoctorRequest(BaseModel):
    nome: str = Field(default='Dr. David Neto', max_length=160)
    conselho: str = Field(default='CRM', max_length=20)
    uf: str = Field(default='ES', max_length=2)
    numero_conselho: str = Field(default='', max_length=30)
    cbo: str = Field(default='', max_length=20)
    cnes: str = Field(default='', max_length=20)


class GuideRequest(BaseModel):
    paciente: PatientRequest
    medico: DoctorRequest
    formato: str = Field(default='sadt')
    natureza: str = Field(default='eletivo')
    indicacao_clinica: str = Field(min_length=3, max_length=2000)
    exames: list[ExamRequest] = Field(min_length=1, max_length=100)
    ipm_finalidade: str = Field(default='procedimentos', max_length=30)
    ipm_opme: list[str] = Field(default_factory=list, max_length=40)
    hospital: str = Field(default='Hospital PRONTOCARDIO', max_length=200)
    data_internacao: str | None = Field(default=None, max_length=30)
    ipm_numero_servico: str | None = Field(default=None, max_length=20)

    @field_validator('formato')
    @classmethod
    def validate_format(cls, value: str):
        if value not in {
            'ans',
            'sadt',
            'ipm',
            'issec',
            'funsa',
            'fusex',
            'fusma',
        }:
            raise ValueError('Formato de guia inválido.')
        return value

    @field_validator('ipm_finalidade')
    @classmethod
    def validate_ipm_purpose(cls, value: str):
        if value not in {'procedimentos', 'nova_internacao'}:
            raise ValueError('Finalidade IPM inválida.')
        return value


class PrescriptionExamRequest(BaseModel):
    usuario_mv: str = Field(min_length=1, max_length=30)
    codigos: list[str] = Field(min_length=1, max_length=100)

    @field_validator('usuario_mv')
    @classmethod
    def validate_user(cls, value: str):
        normalized = value.strip().upper()
        if not re.fullmatch(r'[A-Z0-9_.-]{1,30}', normalized):
            raise ValueError('Usuário MV inválido.')
        return normalized

    @field_validator('codigos')
    @classmethod
    def validate_codes(cls, values: list[str]):
        normalized = list(dict.fromkeys(str(value).strip() for value in values))
        if any(not re.fullmatch(r'\d{1,16}', value) for value in normalized):
            raise ValueError('Código TUSS inválido.')
        return normalized


app = FastAPI(
    title='Solicitação SADT + MV',
    description='Protótipo local para gerar PDF e anexar ao prontuário MV.',
)


def _plain(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _normalize(value: str) -> str:
    value = unicodedata.normalize('NFKD', value or '')
    return ''.join(ch for ch in value if not unicodedata.combining(ch)).lower()


CONVENIO_ALIASES = {
    'ipm_fortaleza': (
        'ipm',
        'instituto de previdencia do municipio',
        'prefeitura de fortaleza',
    ),
    'issec': ('issec', 'instituto de saude dos servidores'),
    'unimed_fortaleza': ('unimed',),
    'bradesco_saude': ('bradesco',),
    'saude_caixa': ('saude caixa', 'caixa economica'),
    'saude_petrobras': ('petrobras',),
    'notredame_intermedica': ('intermedica', 'notredame'),
    'sulamerica': ('sulamerica',),
    'arcelormittal_aberta_saude': ('arcelormittal', 'abertta'),
}
REFERENCE_LOGOS = {
    'issec': {
        'file': 'reference-issec.png',
        'source': (868, 1225),
        'crop': (35, 25, 205, 100),
    },
    'ipm_fortaleza': {
        'file': 'reference-ipm.png',
        'source': (1117, 739),
        'crop': (38, 8, 123, 35),
    },
    'funsa': {
        'file': 'reference-funsa.png',
        'source': (556, 789),
        'crop': (185, 18, 385, 125),
    },
    'fusex': {
        'file': 'reference-fusex.png',
        'source': (550, 789),
        'crop': (100, 55, 195, 112),
    },
    'fusma': {
        'file': 'reference-fusma.png',
        'source': (582, 786),
        'crop': (80, 45, 180, 103),
    },
}


def _guide_format(convenio_id: str | None) -> str:
    own_guides = {
        'ipm_fortaleza': 'ipm',
        'issec': 'issec',
        'funsa': 'funsa',
        'fusex': 'fusex',
        'fusma': 'fusma',
    }
    if convenio_id in own_guides:
        return own_guides[convenio_id]
    return 'sadt'


def _resolve_insurer(name: str | None):
    normalized_name = _normalize(name or '')
    if not normalized_name or normalized_name == 'particular':
        return None
    for item in CONVENIOS:
        item_name = _normalize(item.get('nome', ''))
        if item_name and (
            item_name in normalized_name or normalized_name in item_name
        ):
            return item
    for convenio_id, aliases in CONVENIO_ALIASES.items():
        if any(alias in normalized_name for alias in aliases):
            return next(
                (item for item in CONVENIOS if item.get('id') == convenio_id),
                None,
            )
    return None


def _insurer_config(name: str | None):
    item = _resolve_insurer(name)
    is_particular = _normalize(name or '').strip() == 'particular'
    convenio_id = item.get('id') if item else None
    format_name = 'ans' if is_particular else _guide_format(convenio_id)
    reference = REFERENCE_LOGOS.get(convenio_id)
    return {
        'id': convenio_id,
        'nome_modelo': (
            item.get('nome')
            if item
            else ('PARTICULAR' if is_particular else None)
        ),
        'logo_url': (
            f"/api/referencias/{reference['file']}"
            if reference
            else (
                f'/api/convenios/{convenio_id}/logo'
                if convenio_id
                else None
            )
        ),
        'logo_recorte': (
            {'origem': reference['source'], 'recorte': reference['crop']}
            if reference
            else None
        ),
        'formato': format_name,
        'modelo': {
            'ans': 'Guia SP/SADT tradicional da ANS',
            'sadt': 'Guia padrão SP/SADT',
            'ipm': 'Guia SP/SADT IPM Fortaleza',
            'issec': 'Guia de Serviço I - ISSEC',
            'funsa': 'Guia própria FUNSA',
            'fusex': 'Guia própria FUSEX',
            'fusma': 'Guia própria FUSMA',
        }[format_name],
        'mapeado': bool(item) or is_particular,
    }


@app.get('/')
def screen():
    return {'service': 'Evolução / SADT', 'module': 'SADT'}


@app.get('/api/status')
def status():
    return {
        'status': 'ok',
        'modo': 'producao',
        'restricao_por_atendimento': False,
        'consulta_atendimento_livre': True,
        'escrita_mv': MV_WRITE_ENABLED,
        'tuss_versao': '202603',
        'total_tuss': len(TUSS),
        'total_convenios': len(CONVENIOS),
    }


@app.get('/api/convenios')
def insurers():
    return [
        {
            key: value
            for key, value in item.items()
            if key not in {'logo', 'logoData', 'imagem', 'base64'}
        }
        for item in CONVENIOS
    ]


@app.get('/api/convenios/{convenio_id}/logo')
def insurer_logo(convenio_id: str):
    item = next(
        (item for item in CONVENIOS if item.get('id') == convenio_id),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail='Convênio não encontrado.')
    data_uri = next(
        (
            item.get(key)
            for key in ('logo', 'logoData', 'imagem', 'base64')
            if item.get(key)
        ),
        None,
    )
    if not data_uri:
        raise HTTPException(status_code=404, detail='Logo não disponível.')
    match = re.match(r'data:([^;]+);base64,(.+)', data_uri, re.DOTALL)
    if not match:
        raise HTTPException(status_code=422, detail='Logo inválido.')
    return Response(
        content=base64.b64decode(match.group(2)),
        media_type=match.group(1),
        headers={'Cache-Control': 'public, max-age=86400'},
    )


@app.get('/api/referencias/{image_name}')
def reference_image(image_name: str):
    allowed = {item['file'] for item in REFERENCE_LOGOS.values()}
    if image_name not in allowed:
        raise HTTPException(
            status_code=404, detail='Referência não encontrada.'
        )
    return FileResponse(
        ASSET_DIR / image_name,
        media_type='image/png',
        headers={'Cache-Control': 'public, max-age=86400'},
    )


@app.get('/api/tuss')
def search_tuss(
    q: str = Query(default='', max_length=120),
    tipo: str = Query(default='todos', max_length=30),
    limite: int = Query(default=30, ge=1, le=50),
):
    query = _normalize(q.strip())
    terms = [term for term in query.split() if term]
    result = []
    for item in TUSS:
        if tipo != 'todos' and item.get('tipo') != tipo:
            continue
        haystack = _normalize(f"{item['codigo']} {item['descricao']}")
        if terms and not all(term in haystack for term in terms):
            continue
        result.append(item)
        if len(result) >= limite:
            break
    return {'itens': result, 'limite': limite}


@app.get('/api/paciente/{cd_atendimento}')
def patient_context(cd_atendimento: int):
    if cd_atendimento <= 0:
        raise HTTPException(
            status_code=422, detail='Informe um atendimento válido.'
        )
    query = text(
        """
        SELECT A.CD_ATENDIMENTO AS cd_atendimento,
               A.CD_PACIENTE AS cd_paciente,
               P.NM_PACIENTE AS nome,
               P.DT_NASCIMENTO AS data_nascimento,
               P.TP_SEXO AS sexo,
               P.NR_CNS AS cns,
               A.CD_CONVENIO AS cd_convenio,
               C.NM_CONVENIO AS convenio,
               A.CD_CON_PLA AS cd_plano,
               CP.DS_CON_PLA AS plano,
               A.TP_ATENDIMENTO AS tipo_atendimento,
               A.CD_PRESTADOR AS cd_prestador,
               PR.NM_PRESTADOR AS prestador
          FROM DBAMV.ATENDIME A
          JOIN DBAMV.PACIENTE P
            ON P.CD_PACIENTE = A.CD_PACIENTE
          LEFT JOIN DBAMV.CONVENIO C
            ON C.CD_CONVENIO = A.CD_CONVENIO
          LEFT JOIN DBAMV.CON_PLA CP
            ON CP.CD_CONVENIO = A.CD_CONVENIO
           AND CP.CD_CON_PLA = A.CD_CON_PLA
          LEFT JOIN DBAMV.PRESTADOR PR
            ON PR.CD_PRESTADOR = A.CD_PRESTADOR
         WHERE A.CD_ATENDIMENTO = :cd_atendimento
        """
    )
    with oracle_engine.connect() as connection:
        row = connection.execute(
            query, {'cd_atendimento': cd_atendimento}
        ).mappings().first()
        if not row:
            raise HTTPException(
                status_code=404, detail='Atendimento não encontrado no MV.'
            )
        carteira = connection.execute(
            text(
                """
                SELECT NR_CARTEIRA AS carteira,
                       DT_VALIDADE AS validade_carteira
                  FROM (
                    SELECT CA.NR_CARTEIRA,
                           CA.DT_VALIDADE
                      FROM DBAMV.CARTEIRA CA
                     WHERE CA.CD_PACIENTE = :cd_paciente
                       AND CA.CD_CONVENIO = :cd_convenio
                       AND NVL(CA.SN_CARTEIRA_ATIVO, 'S') = 'S'
                     ORDER BY
                       NVL(CA.SN_ULTIMA_CARTEIRA_UTILIZADA, 'N') DESC,
                       CA.DT_VALIDADE DESC NULLS LAST
                  )
                 WHERE ROWNUM = 1
                """
            ),
            {
                'cd_paciente': row['cd_paciente'],
                'cd_convenio': row['cd_convenio'],
            },
        ).mappings().first()
    payload = {key: _plain(value) for key, value in row.items()}
    payload.update(
        {
            'carteira': _plain(carteira['carteira']) if carteira else '',
            'validade_carteira': (
                _plain(carteira['validade_carteira']) if carteira else None
            ),
            'origem': 'MV',
            'convenio_config': _insurer_config(row['convenio']),
        }
    )
    return payload


@app.get('/api/medico')
def doctor_context(usuario_mv: str | None = Query(default=None, max_length=30)):
    usuario = (
        usuario_mv
        or os.getenv('EVOLUCAO_MV_USUARIO')
        or 'DBAMV'
    ).strip().upper()
    if not usuario or not re.fullmatch(r'[A-Z0-9_.-]{1,30}', usuario):
        raise HTTPException(
            status_code=422,
            detail='Usuário MV inválido ou não informado.',
        )

    query = text(
        """
        SELECT U.CD_USUARIO AS usuario_mv,
               P.CD_PRESTADOR AS cd_prestador,
               P.NM_PRESTADOR AS nome,
               NVL(CO.DS_CONSELHO, 'CRM') AS conselho,
               P.CD_UF_ORGAO_EMISSOR AS uf,
               P.DS_CODIGO_CONSELHO AS numero_conselho,
               P.CD_CBOS AS cbo
          FROM DBASGU.USUARIOS U
          JOIN DBAMV.PRESTADOR P
            ON P.CD_PRESTADOR = U.CD_PRESTADOR
          LEFT JOIN DBAMV.CONSELHO CO
            ON CO.CD_CONSELHO = P.CD_CONSELHO
         WHERE U.CD_USUARIO = :usuario_mv
        """
    )
    with oracle_engine.connect() as connection:
        row = connection.execute(
            query,
            {'usuario_mv': usuario},
        ).mappings().first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail='O usuário conectado ao MV não possui médico/prestador vinculado.',
        )
    payload = {key: _plain(value) for key, value in row.items()}
    payload.update({'cnes': '', 'origem': 'MV'})
    return payload


@app.get('/api/prescricao/{cd_atendimento}/itens')
def prescription_items(cd_atendimento: int):
    if cd_atendimento <= 0:
        raise HTTPException(
            status_code=422, detail='Informe um atendimento válido.'
        )

    query = text(
        """
        SELECT pm.cd_pre_med,
               pm.hr_pre_med,
               pm.sn_fechado,
               ipm.cd_itpre_med,
               ipm.cd_tip_presc,
               tp.ds_tip_presc,
               COALESCE(
                   rx.exa_rx_cd_pro_fat,
                   lab.cd_pro_fat,
                   tp.cd_pro_fat
               ) codigo_tuss,
               COALESCE(
                   rx.ds_exa_rx,
                   lab.nm_exa_lab,
                   pf.ds_pro_fat,
                   tp.ds_tip_presc
               ) descricao_mv
          FROM dbamv.pre_med pm
          JOIN dbamv.itpre_med ipm
            ON ipm.cd_pre_med = pm.cd_pre_med
          JOIN dbamv.tip_presc tp
            ON tp.cd_tip_presc = ipm.cd_tip_presc
          LEFT JOIN dbamv.exa_rx rx
            ON rx.cd_exa_rx = tp.cd_exa_rx
          LEFT JOIN dbamv.exa_lab lab
            ON lab.cd_exa_lab = tp.cd_exa_lab
          LEFT JOIN dbamv.pro_fat pf
            ON pf.cd_pro_fat = COALESCE(
                rx.exa_rx_cd_pro_fat,
                lab.cd_pro_fat,
                tp.cd_pro_fat
            )
         WHERE pm.cd_atendimento = :cd_atendimento
           AND pm.cd_pre_med = (
                SELECT MAX(pm2.cd_pre_med)
                  FROM dbamv.pre_med pm2
                 WHERE pm2.cd_atendimento = :cd_atendimento
                   AND EXISTS (
                        SELECT 1
                          FROM dbamv.itpre_med ipm2
                         WHERE ipm2.cd_pre_med = pm2.cd_pre_med
                           AND NVL(ipm2.sn_cancelado, 'N') = 'N'
                   )
           )
           AND NVL(ipm.sn_cancelado, 'N') = 'N'
           AND tp.sn_solicitacao = 'S'
         ORDER BY ipm.cd_itpre_med
        """
    )

    with oracle_engine.connect() as connection:
        rows = connection.execute(
            query, {'cd_atendimento': cd_atendimento}
        ).mappings().all()

    if not rows:
        return {
            'cd_atendimento': cd_atendimento,
            'cd_pre_med': None,
            'prescricao_fechada': False,
            'itens': [],
            'nao_mapeados': [],
        }

    items = []
    unmapped = []
    for row in rows:
        code = str(row['codigo_tuss']) if row['codigo_tuss'] else ''
        tuss_item = TUSS_BY_CODE.get(code)
        if tuss_item:
            items.append(
                {
                    **tuss_item,
                    'cd_itpre_med': int(row['cd_itpre_med']),
                    'cd_tip_presc': int(row['cd_tip_presc']),
                }
            )
        else:
            unmapped.append(
                {
                    'cd_itpre_med': int(row['cd_itpre_med']),
                    'cd_tip_presc': int(row['cd_tip_presc']),
                    'codigo_mv': code or None,
                    'descricao': (
                        row['descricao_mv'] or row['ds_tip_presc']
                    ),
                }
            )

    first = rows[0]
    return {
        'cd_atendimento': cd_atendimento,
        'cd_pre_med': int(first['cd_pre_med']),
        'horario': first['hr_pre_med'],
        'prescricao_fechada': first['sn_fechado'] == 'S',
        'itens': items,
        'nao_mapeados': unmapped,
    }


@app.get('/api/prescricao/{cd_atendimento}/historico')
def prescription_history(cd_atendimento: int):
    if cd_atendimento <= 0:
        raise HTTPException(
            status_code=422, detail='Informe um atendimento válido.'
        )

    query = text(
        """
        SELECT pm.cd_pre_med,
               pm.hr_pre_med,
               pm.sn_fechado,
               pm.tp_pre_med,
               ipm.cd_itpre_med,
               ipm.cd_tip_presc,
               NVL(ipm.sn_cancelado, 'N') sn_cancelado,
               tp.ds_tip_presc,
               tp.sn_solicitacao,
               COALESCE(
                   rx.exa_rx_cd_pro_fat,
                   lab.cd_pro_fat,
                   tp.cd_pro_fat
               ) codigo_tuss,
               COALESCE(
                   rx.ds_exa_rx,
                   lab.nm_exa_lab,
                   pf.ds_pro_fat,
                   tp.ds_tip_presc
               ) descricao_mv
          FROM dbamv.pre_med pm
          LEFT JOIN dbamv.itpre_med ipm
            ON ipm.cd_pre_med = pm.cd_pre_med
          LEFT JOIN dbamv.tip_presc tp
            ON tp.cd_tip_presc = ipm.cd_tip_presc
          LEFT JOIN dbamv.exa_rx rx
            ON rx.cd_exa_rx = tp.cd_exa_rx
          LEFT JOIN dbamv.exa_lab lab
            ON lab.cd_exa_lab = tp.cd_exa_lab
          LEFT JOIN dbamv.pro_fat pf
            ON pf.cd_pro_fat = COALESCE(
                rx.exa_rx_cd_pro_fat,
                lab.cd_pro_fat,
                tp.cd_pro_fat
            )
         WHERE pm.cd_atendimento = :cd_atendimento
         ORDER BY pm.cd_pre_med DESC, ipm.cd_itpre_med
        """
    )

    with oracle_engine.connect() as connection:
        rows = connection.execute(
            query, {'cd_atendimento': cd_atendimento}
        ).mappings().all()

    prescriptions: dict[int, dict] = {}
    for row in rows:
        prescription_id = int(row['cd_pre_med'])
        prescription = prescriptions.setdefault(
            prescription_id,
            {
                'cd_pre_med': prescription_id,
                'horario': _plain(row['hr_pre_med']),
                'tipo': row['tp_pre_med'],
                'prescricao_fechada': row['sn_fechado'] == 'S',
                'status': (
                    'FECHADA' if row['sn_fechado'] == 'S' else 'ABERTA'
                ),
                'itens': [],
                'nao_mapeados': [],
                'total_itens': 0,
            },
        )
        if row['cd_itpre_med'] is None:
            continue
        prescription['total_itens'] += 1
        code = str(row['codigo_tuss']) if row['codigo_tuss'] else ''
        tuss_item = TUSS_BY_CODE.get(code)
        base_item = {
            'cd_itpre_med': int(row['cd_itpre_med']),
            'cd_tip_presc': int(row['cd_tip_presc']),
            'cancelado': row['sn_cancelado'] == 'S',
            'solicitacao': row['sn_solicitacao'] == 'S',
        }
        if tuss_item:
            prescription['itens'].append({**tuss_item, **base_item})
        else:
            prescription['nao_mapeados'].append(
                {
                    **base_item,
                    'codigo_mv': code or None,
                    'descricao': (
                        row['descricao_mv'] or row['ds_tip_presc'] or 'Item MV'
                    ),
                }
            )

    return {
        'cd_atendimento': cd_atendimento,
        'prescricoes': list(prescriptions.values()),
    }


@app.post('/api/prescricao/{cd_atendimento}/exames')
def add_prescription_exams(
    cd_atendimento: int,
    request: PrescriptionExamRequest,
):
    if not MV_WRITE_ENABLED:
        raise HTTPException(
            status_code=403,
            detail='A escrita no MV não está habilitada neste ambiente.',
        )
    if cd_atendimento <= 0:
        raise HTTPException(
            status_code=422, detail='Informe um atendimento válido.'
        )

    doctor_query = text(
        """
        SELECT p.cd_prestador
          FROM dbasgu.usuarios u
          JOIN dbamv.prestador p ON p.cd_prestador = u.cd_prestador
         WHERE u.cd_usuario = :usuario_mv
        """
    )
    target_query = text(
        """
        SELECT pm.cd_pre_med
          FROM dbamv.pre_med pm
         WHERE pm.cd_pre_med = (
                SELECT MAX(candidate.cd_pre_med)
                  FROM dbamv.pre_med candidate
                 WHERE candidate.cd_atendimento = :cd_atendimento
                   AND candidate.cd_prestador = :cd_prestador
                   AND NVL(candidate.sn_fechado, 'N') = 'N'
                   AND NVL(candidate.fl_impresso, 'N') = 'N'
         )
        FOR UPDATE
        """
    )
    mapping_query = text(
        """
        SELECT * FROM (
            SELECT tp.cd_tip_presc,
                   tp.cd_tip_esq,
                   source.qt_itpre_med,
                   source.cd_set_exa,
                   ROW_NUMBER() OVER (
                       ORDER BY source.cd_itpre_med DESC NULLS LAST,
                                tp.cd_tip_presc DESC
                   ) position
              FROM dbamv.tip_presc tp
              LEFT JOIN dbamv.exa_rx rx ON rx.cd_exa_rx = tp.cd_exa_rx
              LEFT JOIN dbamv.exa_lab lab ON lab.cd_exa_lab = tp.cd_exa_lab
              LEFT JOIN dbamv.itpre_med source
                ON source.cd_itpre_med = (
                    SELECT MAX(previous.cd_itpre_med)
                      FROM dbamv.itpre_med previous
                     WHERE previous.cd_tip_presc = tp.cd_tip_presc
                       AND NVL(previous.sn_cancelado, 'N') = 'N'
                )
             WHERE TO_CHAR(COALESCE(
                       rx.exa_rx_cd_pro_fat,
                       lab.cd_pro_fat,
                       tp.cd_pro_fat
                   )) = :codigo
               AND NVL(tp.sn_ativo, 'S') = 'S'
               AND tp.sn_solicitacao = 'S'
        ) WHERE position = 1
        """
    )
    duplicate_query = text(
        """
        SELECT COUNT(*)
          FROM dbamv.itpre_med
         WHERE cd_pre_med = :cd_pre_med
           AND cd_tip_presc = :cd_tip_presc
           AND NVL(sn_cancelado, 'N') = 'N'
        """
    )
    insert_query = text(
        """
        INSERT INTO dbamv.itpre_med (
            cd_itpre_med,
            cd_tip_esq,
            cd_tip_presc,
            cd_pre_med,
            qt_itpre_med,
            cd_set_exa,
            sn_cancelado
        ) VALUES (
            dbamv.seq_itpre_med.nextval,
            :cd_tip_esq,
            :cd_tip_presc,
            :cd_pre_med,
            :qt_itpre_med,
            :cd_set_exa,
            'N'
        )
        """
    )

    added = []
    existing = []
    unmapped = []
    try:
        with oracle_engine.begin() as connection:
            doctor = connection.execute(
                doctor_query, {'usuario_mv': request.usuario_mv}
            ).mappings().first()
            if not doctor:
                raise HTTPException(
                    status_code=404,
                    detail='O usuário MV não possui médico/prestador vinculado.',
                )
            target = connection.execute(
                target_query,
                {
                    'cd_atendimento': cd_atendimento,
                    'cd_prestador': doctor['cd_prestador'],
                },
            ).mappings().first()
            if not target:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        'Inicie uma prescrição médica aberta e ainda não impressa '
                        'no MV antes de adicionar os exames.'
                    ),
                )
            prescription_id = int(target['cd_pre_med'])
            for code in request.codigos:
                mapped = connection.execute(
                    mapping_query, {'codigo': code}
                ).mappings().first()
                if not mapped:
                    unmapped.append(code)
                    continue
                parameters = {
                    'cd_pre_med': prescription_id,
                    'cd_tip_presc': int(mapped['cd_tip_presc']),
                }
                if connection.execute(
                    duplicate_query, parameters
                ).scalar_one():
                    existing.append(code)
                    continue
                connection.execute(
                    insert_query,
                    {
                        **parameters,
                        'cd_tip_esq': mapped['cd_tip_esq'],
                        'qt_itpre_med': (
                            mapped['qt_itpre_med']
                            if mapped['qt_itpre_med'] is not None
                            else (1 if mapped['cd_tip_esq'] == 'EXI' else None)
                        ),
                        'cd_set_exa': mapped['cd_set_exa'],
                    },
                )
                added.append(code)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                'O MV recusou a inclusão dos exames na prescrição. '
                'Atualize a tela e confirme se a prescrição continua aberta.'
            ),
        ) from exc

    return {
        'cd_atendimento': cd_atendimento,
        'cd_pre_med': prescription_id,
        'adicionados': added,
        'ja_existentes': existing,
        'nao_mapeados': unmapped,
    }


def _draw_wrapped(  # noqa: PLR0913
    pdf: canvas.Canvas,
    text_value: str,
    x: float,
    y: float,
    width: float,
    height: float,
    size: int = 9,
    bold: bool = False,
):
    style = ParagraphStyle(
        'field',
        fontName=PDF_FONT_BOLD if bold else PDF_FONT,
        fontSize=size,
        leading=size + 2,
        textColor=colors.HexColor('#17243d'),
        alignment=TA_LEFT,
    )
    paragraph = Paragraph(
        (text_value or '—').replace('&', '&amp;').replace('<', '&lt;'),
        style,
    )
    _, used_height = paragraph.wrap(width, height)
    paragraph.drawOn(pdf, x, y + height - used_height)


def _field(  # noqa: PLR0913
    pdf: canvas.Canvas,
    label: str,
    value: str,
    x: float,
    y: float,
    width: float,
    height: float = 31,
):
    pdf.setStrokeColor(colors.HexColor('#d8e0ec'))
    pdf.setFillColor(colors.white)
    pdf.roundRect(x, y, width, height, 5, stroke=1, fill=1)
    pdf.setFillColor(colors.HexColor('#6d7b91'))
    pdf.setFont(PDF_FONT_BOLD, 6.5)
    pdf.drawString(x + 7, y + height - 10, label.upper())
    _draw_wrapped(pdf, value, x + 7, y + 4, width - 14, height - 15, 8.5)


@lru_cache(maxsize=40)
def _insurer_logo_bytes(convenio_id: str | None):
    if not convenio_id:
        return None
    item = next(
        (item for item in CONVENIOS if item.get('id') == convenio_id),
        None,
    )
    data_uri = item.get('logo') if item else None
    match = (
        re.match(r'data:[^;]+;base64,(.+)', data_uri, re.DOTALL)
        if data_uri
        else None
    )
    if not match:
        return None
    source = io.BytesIO(base64.b64decode(match.group(1)))
    image = Image.open(source).convert('RGBA')
    image.thumbnail((1200, 600))
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()


def _draw_sadt_page(  # noqa: PLR0915
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
    page_number: int,
    page_total: int,
):
    page_size = landscape(A4)
    width, height = page_size
    pdf.setPageSize(page_size)
    pdf.setFillColor(colors.HexColor('#f4f7fb'))
    pdf.rect(0, 0, width, height, stroke=0, fill=1)

    margin = 32
    card_x, card_y = margin, margin
    card_w, card_h = width - 2 * margin, height - 2 * margin
    pdf.setFillColor(colors.white)
    pdf.setStrokeColor(colors.HexColor('#dfe6f0'))
    pdf.roundRect(card_x, card_y, card_w, card_h, 12, stroke=1, fill=1)

    pdf.setFillColor(colors.HexColor('#0d67e8'))
    pdf.roundRect(
        card_x,
        height - margin - 74,
        card_w,
        74,
        12,
        stroke=0,
        fill=1,
    )
    pdf.rect(card_x, height - margin - 74, card_w, 12, stroke=0, fill=1)

    try:
        insurer_logo = _insurer_logo_bytes(request.paciente.convenio_id)
        logo = (
            ImageReader(io.BytesIO(insurer_logo))
            if insurer_logo
            else ImageReader(str(DOCTOR_LOGO_PATH))
        )
        if insurer_logo or DOCTOR_LOGO_PATH.exists():
            pdf.drawImage(
                logo,
                card_x + 14,
                height - margin - 63,
                width=92,
                height=50,
                preserveAspectRatio=True,
                anchor='c',
                mask='auto',
            )
    except Exception:
        pass

    pdf.setFillColor(colors.white)
    pdf.setFont(PDF_FONT_BOLD, 15)
    title = 'SOLICITAÇÃO DE SERVIÇOS AUXILIARES'
    pdf.drawString(card_x + 118, height - margin - 29, title)
    pdf.setFont(PDF_FONT, 8)
    pdf.drawString(
        card_x + 118,
        height - margin - 47,
        f'Gerado a partir do atendimento MV {request.paciente.cd_atendimento}',
    )
    pdf.setFont(PDF_FONT_BOLD, 8)
    pdf.drawRightString(
        width - margin - 16,
        height - margin - 29,
        f'PÁGINA {page_number}/{page_total}',
    )
    pdf.setFont(PDF_FONT, 7)
    pdf.drawRightString(
        width - margin - 16,
        height - margin - 45,
        datetime.now().strftime('%d/%m/%Y %H:%M'),
    )

    content_top = height - margin - 92
    gap = 8
    half = (card_w - 32 - gap) / 2
    _field(
        pdf,
        'Paciente',
        request.paciente.nome,
        card_x + 16,
        content_top - 32,
        half,
    )
    _field(
        pdf,
        'Atendimento / Prontuário',
        (
            f"{request.paciente.cd_atendimento} / "
            f"{request.paciente.cd_paciente}"
        ),
        card_x + 16 + half + gap,
        content_top - 32,
        half,
    )
    third = (card_w - 32 - 2 * gap) / 3
    row2_y = content_top - 71
    _field(
        pdf,
        'Nascimento',
        request.paciente.data_nascimento or '',
        card_x + 16,
        row2_y,
        third,
    )
    _field(
        pdf,
        'Convênio / Plano',
        ' · '.join(
            part
            for part in (
                request.paciente.convenio,
                request.paciente.plano,
            )
            if part
        ),
        card_x + 16 + third + gap,
        row2_y,
        third,
    )
    _field(
        pdf,
        'Carteirinha / Validade',
        ' · '.join(
            part
            for part in (
                request.paciente.carteira,
                request.paciente.validade_carteira,
            )
            if part
        ),
        card_x + 16 + 2 * (third + gap),
        row2_y,
        third,
    )
    indication_y = row2_y - 58
    _field(
        pdf,
        'Indicação clínica',
        request.indicacao_clinica,
        card_x + 16,
        indication_y,
        card_w - 32,
        49,
    )

    table_top = indication_y - 15
    table_x = card_x + 16
    table_w = card_w - 32
    pdf.setFillColor(colors.HexColor('#eaf2ff'))
    pdf.roundRect(table_x, table_top - 24, table_w, 24, 5, stroke=0, fill=1)
    pdf.setFillColor(colors.HexColor('#245188'))
    pdf.setFont(PDF_FONT_BOLD, 7.5)
    pdf.drawString(table_x + 8, table_top - 16, 'CÓDIGO TUSS')
    pdf.drawString(table_x + 103, table_top - 16, 'PROCEDIMENTO SOLICITADO')
    pdf.drawRightString(table_x + table_w - 8, table_top - 16, 'QTDE.')

    row_height = 24
    y = table_top - 24
    for index, exam in enumerate(exams):
        y -= row_height
        pdf.setFillColor(
            colors.white
            if index % 2 == 0
            else colors.HexColor('#f8fafd')
        )
        pdf.rect(table_x, y, table_w, row_height, stroke=0, fill=1)
        pdf.setStrokeColor(colors.HexColor('#e7ecf3'))
        pdf.line(table_x, y, table_x + table_w, y)
        pdf.setFillColor(colors.HexColor('#17243d'))
        pdf.setFont(PDF_FONT_BOLD, 8)
        pdf.drawString(table_x + 8, y + 8, exam.codigo)
        _draw_wrapped(
            pdf,
            exam.descricao,
            table_x + 103,
            y + 3,
            table_w - 155,
            row_height - 5,
            7.5,
        )
        pdf.setFont(PDF_FONT_BOLD, 8)
        pdf.drawRightString(table_x + table_w - 18, y + 8, '1')

    footer_y = card_y + 20
    pdf.setStrokeColor(colors.HexColor('#90a2ba'))
    pdf.line(card_x + 20, footer_y + 22, card_x + 235, footer_y + 22)
    pdf.setFillColor(colors.HexColor('#53647b'))
    pdf.setFont(PDF_FONT, 7)
    pdf.drawString(
        card_x + 20,
        footer_y + 10,
        (
            f'{request.medico.nome} · {request.medico.conselho} '
            f'{request.medico.uf} {request.medico.numero_conselho}'
        ).strip(),
    )
    pdf.drawRightString(
        width - margin - 16,
        footer_y + 10,
        'Documento gerado pelo protótipo SADT + MV',
    )


def _draw_insurer_logo(  # noqa: PLR0913
    pdf: canvas.Canvas,
    convenio_id: str,
    x: float,
    y: float,
    width: float,
    height: float,
):
    try:
        content = _insurer_logo_bytes(convenio_id)
        if content:
            pdf.drawImage(
                ImageReader(io.BytesIO(content)),
                x,
                y,
                width=width,
                height=height,
                preserveAspectRatio=True,
                anchor='c',
                mask='auto',
            )
    except Exception:
        return


def _draw_reference_crop(  # noqa: PLR0913
    pdf: canvas.Canvas,
    image_name: str,
    crop: tuple[float, float, float, float],
    x: float,
    y: float,
    width: float,
    height: float,
):
    path = ASSET_DIR / image_name
    if not path.exists():
        return
    image = ImageReader(str(path))
    image_width, image_height = image.getSize()
    left, top, right, bottom = crop
    crop_width = right - left
    crop_height = bottom - top
    scale_x = width / crop_width
    scale_y = height / crop_height
    draw_x = x - left * scale_x
    draw_y = y - (image_height - bottom) * scale_y
    clip = pdf.beginPath()
    clip.rect(x, y, width, height)
    pdf.saveState()
    pdf.clipPath(clip, stroke=0, fill=0)
    pdf.drawImage(
        image,
        draw_x,
        draw_y,
        width=image_width * scale_x,
        height=image_height * scale_y,
        mask='auto',
    )
    pdf.restoreState()


def _classic_box(  # noqa: PLR0913
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    line_width: float = 0.8,
):
    pdf.setStrokeColor(colors.black)
    pdf.setLineWidth(line_width)
    pdf.rect(x, y, width, height, stroke=1, fill=0)


def _classic_text(  # noqa: PLR0913
    pdf: canvas.Canvas,
    value: str,
    x: float,
    y: float,
    size: float = 7,
    bold: bool = False,
):
    pdf.setFillColor(colors.black)
    pdf.setFont(PDF_FONT_BOLD if bold else PDF_FONT, size)
    pdf.drawString(x, y, value or '')


def _classic_center(  # noqa: PLR0913
    pdf: canvas.Canvas,
    value: str,
    x: float,
    y: float,
    size: float = 8,
    bold: bool = False,
):
    pdf.setFillColor(colors.black)
    pdf.setFont(PDF_FONT_BOLD if bold else PDF_FONT, size)
    pdf.drawCentredString(x, y, value or '')


def _classic_paragraph(  # noqa: PLR0913
    pdf: canvas.Canvas,
    value: str,
    x: float,
    y: float,
    width: float,
    height: float,
    size: float = 7,
    bold: bool = False,
):
    style = ParagraphStyle(
        'classic',
        fontName=PDF_FONT_BOLD if bold else PDF_FONT,
        fontSize=size,
        leading=size + 1.5,
        textColor=colors.black,
    )
    paragraph = Paragraph(
        (value or '').replace('&', '&amp;').replace('<', '&lt;'),
        style,
    )
    _, used_height = paragraph.wrap(width, height)
    paragraph.drawOn(pdf, x, y + height - used_height)


def _issec_category(exams: list[ExamRequest]) -> str:
    combined = _normalize(
        ' '.join(f'{exam.tipo} {exam.descricao}' for exam in exams)
    )
    if all(exam.tipo == 'laboratorio' for exam in exams):
        return 'Laboratório'
    if 'ultra' in combined or 'doppler' in combined:
        return 'Ultra-som'
    if 'endoscop' in combined:
        return 'Endoscópico'
    if any(
        term in combined
        for term in ('cardio', 'ecg', 'eletrocard', 'holter', 'mapa')
    ):
        return 'Cardiológico'
    if any(term in combined for term in ('radio', 'tomografia', 'raio x')):
        return 'Radiológico'
    return 'Outros'


def _draw_issec_page(  # noqa: PLR0915
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
    page_number: int,
    page_total: int,
):
    width, height = A4
    pdf.setPageSize(A4)
    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, width, height, stroke=0, fill=1)
    margin = 11 * mm
    content_width = width - 2 * margin

    _draw_reference_crop(
        pdf,
        'reference-issec.png',
        (35, 25, 205, 100),
        margin,
        height - 28 * mm,
        42 * mm,
        18 * mm,
    )
    _draw_reference_crop(
        pdf,
        'reference-issec.png',
        (640, 20, 840, 105),
        width - margin - 46 * mm,
        height - 29 * mm,
        46 * mm,
        20 * mm,
    )
    _classic_center(
        pdf, 'GUIA DE SERVIÇO - I', width / 2, height - 39 * mm, 13, True
    )
    _classic_text(
        pdf,
        f'Folha {page_number}/{page_total}',
        width - margin - 25 * mm,
        height - 39 * mm,
        6,
    )

    procedure_y = height - 73 * mm
    procedure_h = 25 * mm
    _classic_box(pdf, margin, procedure_y, content_width, procedure_h, 1.1)
    band_h = 5 * mm
    pdf.setFillColor(colors.HexColor('#dedede'))
    pdf.rect(
        margin,
        procedure_y + procedure_h - band_h,
        content_width,
        band_h,
        stroke=1,
        fill=1,
    )
    _classic_center(
        pdf,
        'PROCEDIMENTO',
        width / 2,
        procedure_y + procedure_h - 3.7 * mm,
        8,
        True,
    )
    _classic_text(
        pdf, 'EXAME', margin + 4 * mm, procedure_y + 9 * mm, 8, True
    )
    categories = [
        ('Laboratório', 28, 16),
        ('Ultra-som', 78, 16),
        ('Endoscópico', 129, 16),
        ('Cardiológico', 28, 7),
        ('Radiológico', 78, 7),
        ('Outros', 129, 7),
    ]
    marked = _issec_category(exams)
    for label, x_mm, y_mm in categories:
        _classic_text(
            pdf, label, margin + x_mm * mm, procedure_y + y_mm * mm, 7
        )
        square_x = margin + (x_mm + 34) * mm
        square_y = procedure_y + (y_mm - 1.2) * mm
        _classic_box(pdf, square_x, square_y, 6 * mm, 5 * mm, 0.6)
        if label == marked:
            _classic_center(
                pdf, 'X', square_x + 3 * mm, square_y + 1.2 * mm, 8, True
            )

    request_y = height - 171 * mm
    request_h = 92 * mm
    _classic_box(pdf, margin, request_y, content_width, request_h, 1.1)
    pdf.setFillColor(colors.HexColor('#dedede'))
    pdf.rect(
        margin,
        request_y + request_h - band_h,
        content_width,
        band_h,
        stroke=1,
        fill=1,
    )
    _classic_center(
        pdf,
        'RESERVADO AO MÉDICO REQUISITANTE',
        width / 2,
        request_y + request_h - 3.7 * mm,
        8,
        True,
    )
    head_y = request_y + request_h - 18 * mm
    _classic_text(pdf, 'Nome do Beneficiário:', margin + 2 * mm, head_y, 7)
    _classic_text(
        pdf, request.paciente.nome, margin + 37 * mm, head_y, 7, True
    )
    pdf.line(
        margin + 36 * mm,
        head_y - 1 * mm,
        margin + 140 * mm,
        head_y - 1 * mm,
    )
    card_x = margin + 148 * mm
    _classic_box(pdf, card_x, head_y - 8 * mm, 31 * mm, 12 * mm, 0.6)
    _classic_center(
        pdf, 'CARTÃO ISSEC', card_x + 15.5 * mm, head_y + 5.5 * mm, 7
    )
    _classic_center(
        pdf,
        request.paciente.carteira or '',
        card_x + 15.5 * mm,
        head_y - 4 * mm,
        6.5,
    )

    table_top = request_y + request_h - 27 * mm
    row_h = 8 * mm
    pdf.line(margin, table_top, margin + content_width, table_top)
    pdf.line(
        margin + 13 * mm,
        table_top,
        margin + 13 * mm,
        table_top - 32 * mm,
    )
    _classic_center(
        pdf, 'N.º', margin + 6.5 * mm, table_top - 5.5 * mm, 7
    )
    _classic_center(
        pdf,
        'PROCEDIMENTOS SOLICITADOS',
        margin + 102 * mm,
        table_top - 5.5 * mm,
        7,
    )
    pdf.line(
        margin,
        table_top - row_h,
        margin + content_width,
        table_top - row_h,
    )
    for index in range(3):
        row_y = table_top - (index + 2) * row_h
        pdf.line(margin, row_y, margin + content_width, row_y)
        _classic_center(
            pdf,
            f'{index + 1:02d}',
            margin + 6.5 * mm,
            row_y + 2.5 * mm,
            7,
        )
        if index < len(exams):
            exam = exams[index]
            _classic_paragraph(
                pdf,
                f'{exam.codigo} - {exam.descricao}',
                margin + 16 * mm,
                row_y + 1 * mm,
                content_width - 19 * mm,
                row_h - 2 * mm,
                7,
                True,
            )

    lower_top = table_top - 32 * mm
    split_x = margin + 116 * mm
    pdf.line(split_x, request_y, split_x, lower_top)
    _classic_text(
        pdf, 'Justificativa:', margin + 2 * mm, lower_top - 5 * mm, 7
    )
    _classic_paragraph(
        pdf,
        request.indicacao_clinica,
        margin + 12 * mm,
        request_y + 8 * mm,
        98 * mm,
        lower_top - request_y - 14 * mm,
        7,
    )
    right_x = split_x
    mid_y = request_y + 23 * mm
    pdf.line(right_x, mid_y, margin + content_width, mid_y)
    _classic_text(
        pdf,
        'Senha de Autorização da Consulta',
        right_x + 2 * mm,
        lower_top - 5 * mm,
        6.5,
    )
    _classic_text(
        pdf,
        f'Data da solicitação:  {datetime.now():%d/%m/%Y}',
        right_x + 2 * mm,
        mid_y - 6 * mm,
        6.5,
    )
    _classic_center(
        pdf,
        'Carimbo e Assinatura do Médico',
        right_x + (content_width - 116 * mm) / 2,
        request_y + 3 * mm,
        6.5,
    )

    executed_y = height - 237 * mm
    executed_h = 60 * mm
    _classic_box(pdf, margin, executed_y, content_width, executed_h, 1.1)
    pdf.setFillColor(colors.HexColor('#dedede'))
    pdf.rect(
        margin,
        executed_y + executed_h - band_h,
        content_width,
        band_h,
        stroke=1,
        fill=1,
    )
    _classic_center(
        pdf,
        'SERVIÇO EXECUTADO',
        width / 2,
        executed_y + executed_h - 3.7 * mm,
        10,
        True,
    )
    exec_top = executed_y + executed_h - band_h
    col_x = [
        margin,
        margin + 10 * mm,
        margin + 105 * mm,
        margin + 157 * mm,
        margin + content_width,
    ]
    for x_pos in col_x[1:-1]:
        pdf.line(x_pos, exec_top, x_pos, executed_y + 23 * mm)
    headers = ['N.º', 'PROCEDIMENTO', 'CÓDIGO TABELA', 'VALOR R$']
    for index, header in enumerate(headers):
        _classic_center(
            pdf,
            header,
            (col_x[index] + col_x[index + 1]) / 2,
            exec_top - 4 * mm,
            6.5,
        )
    exec_row_h = 6.4 * mm
    for row in range(5):
        y_line = exec_top - (row + 1) * exec_row_h
        pdf.line(margin, y_line, margin + content_width, y_line)
        if 0 < row <= min(len(exams), 3):
            _classic_center(
                pdf,
                f'{row:02d}',
                margin + 5 * mm,
                y_line + 2.25 * mm,
                6.5,
            )
    split_exec = margin + 102 * mm
    pdf.line(split_exec, executed_y, split_exec, executed_y + 23 * mm)
    _classic_text(
        pdf,
        'Data da realização do procedimento ____/____/________',
        margin + 2 * mm,
        executed_y + 17 * mm,
        6.5,
    )
    _classic_center(
        pdf,
        'Assinatura do Beneficiário ou Responsável',
        margin + 51 * mm,
        executed_y + 2 * mm,
        6,
    )
    _classic_text(
        pdf,
        'Carimbo e Assinatura do Credenciado',
        split_exec + 2 * mm,
        executed_y + 17 * mm,
        6.5,
    )

    auth_y = 16 * mm
    auth_h = 38 * mm
    _classic_box(pdf, margin, auth_y, content_width, auth_h, 1.1)
    pdf.setFillColor(colors.HexColor('#dedede'))
    pdf.rect(
        margin,
        auth_y + auth_h - band_h,
        content_width,
        band_h,
        stroke=1,
        fill=1,
    )
    _classic_center(
        pdf,
        'RESERVADO PARA AUTORIZAÇÃO',
        width / 2,
        auth_y + auth_h - 3.7 * mm,
        8,
        True,
    )
    _classic_box(
        pdf, margin + 12 * mm, auth_y + 7 * mm, 72 * mm, 20 * mm, 0.6
    )
    _classic_box(
        pdf, margin + 114 * mm, auth_y + 7 * mm, 64 * mm, 20 * mm, 0.6
    )
    _classic_center(
        pdf,
        'SENHA DE AUTORIZAÇÃO',
        margin + 48 * mm,
        auth_y + 22 * mm,
        7,
        True,
    )
    _classic_center(
        pdf,
        '□ □ □ □ □ □ □ □',
        margin + 48 * mm,
        auth_y + 11 * mm,
        10,
    )
    _classic_center(
        pdf,
        'DATA DA AUTORIZAÇÃO',
        margin + 146 * mm,
        auth_y + 22 * mm,
        7,
        True,
    )
    _classic_center(
        pdf, '____/____/________', margin + 146 * mm, auth_y + 11 * mm, 8
    )
    pdf.setFillColor(colors.red)
    pdf.setFont(PDF_FONT_BOLD, 6.5)
    pdf.drawCentredString(
        width / 2,
        11 * mm,
        (
            'OBS.: ESTA GUIA DEVERÁ SER UTILIZADA PARA CADA TIPO '
            'DE SERVIÇO, DISTINTAMENTE.'
        ),
    )


def _small_field(  # noqa: PLR0913
    pdf: canvas.Canvas,
    label: str,
    value: str,
    x: float,
    y: float,
    width: float,
    height: float,
    size: float = 5.2,
):
    _classic_box(pdf, x, y, width, height, 0.45)
    _classic_text(pdf, label, x + 1.2 * mm, y + height - 3 * mm, size)
    if value:
        _classic_paragraph(
            pdf,
            value,
            x + 1.5 * mm,
            y + 1 * mm,
            width - 3 * mm,
            height - 5 * mm,
            size + 0.5,
            True,
        )


def _draw_ipm_page(  # noqa: PLR0915
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
    page_number: int,
    page_total: int,
):
    page_size = landscape(A4)
    width, height = page_size
    pdf.setPageSize(page_size)
    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, width, height, stroke=0, fill=1)
    margin = 7 * mm
    content_width = width - 2 * margin
    content_height = height - 2 * margin
    _classic_box(pdf, margin, margin, content_width, content_height, 0.9)

    _draw_reference_crop(
        pdf,
        'reference-ipm.png',
        (38, 8, 123, 35),
        margin + 4 * mm,
        height - margin - 14 * mm,
        23 * mm,
        10 * mm,
    )
    _classic_center(
        pdf,
        (
            'GUIA DE SERVIÇO PROFISSIONAL/SERVIÇO AUXILIAR DE '
            'DIAGNÓSTICO E TERAPIA - SP/SADT'
        ),
        width / 2,
        height - margin - 7 * mm,
        9,
        True,
    )
    _classic_text(
        pdf,
        f'Folha {page_number}/{page_total}',
        width - margin - 25 * mm,
        height - margin - 7 * mm,
        5.5,
    )

    x = margin + 4 * mm
    top = height - margin - 17 * mm
    gap = 1.4 * mm
    field_h = 10 * mm
    fields_width = content_width - 8 * mm

    def fit_row_widths(widths_mm: list[float]) -> list[float]:
        gaps_width = gap * (len(widths_mm) - 1)
        scale = (fields_width - gaps_width) / (sum(widths_mm) * mm)
        return [width_mm * mm * scale for width_mm in widths_mm]

    widths = [32, 33, 29, 86, 43, 50]
    labels = [
        '1 - Registro ANS',
        '3 - Nº Guia Principal',
        '4 - Data da Autorização',
        '5 - Senha',
        '6 - Validade da Senha',
        '7 - Emissão da Guia',
    ]
    values = ['', '', '', '', '', f'{datetime.now():%d/%m/%Y}']
    cursor = x
    fitted_widths = fit_row_widths(widths)
    for label, value, field_width in zip(
        labels, values, fitted_widths, strict=True
    ):
        _small_field(
            pdf, label, value, cursor, top - field_h, field_width, field_h
        )
        cursor += field_width + gap

    _classic_text(pdf, 'DADOS DO BENEFICIÁRIO', x, top - 13 * mm, 5.5, True)
    row_y = top - 27 * mm
    beneficiary = [
        ('8 - Número da Carteira', request.paciente.carteira, 63),
        ('9 - Plano', request.paciente.plano, 58),
        (
            '10 - Validade da Carteira',
            request.paciente.validade_carteira or '',
            37,
        ),
        ('11 - Nome', request.paciente.nome, 62),
        ('12 - Cartão Nacional de Saúde', request.paciente.cns or '', 47),
    ]
    cursor = x
    fitted_widths = fit_row_widths([item[2] for item in beneficiary])
    for (label, value, _), field_width in zip(
        beneficiary, fitted_widths, strict=True
    ):
        _small_field(pdf, label, value, cursor, row_y, field_width, field_h)
        cursor += field_width + gap

    _classic_text(
        pdf, 'DADOS DO CONTRATADO SOLICITANTE', x, row_y - 4 * mm, 5.5, True
    )
    row2_y = row_y - 16 * mm
    provider = [
        ('13 - Código na Operadora/CNPJ-CPF', '', 65),
        ('14 - Nome do Contratado', 'Hospital PRONTOCARDIO', 170),
        ('15 - Código CNES', request.medico.cnes, 40),
    ]
    cursor = x
    fitted_widths = fit_row_widths([item[2] for item in provider])
    for (label, value, _), field_width in zip(
        provider, fitted_widths, strict=True
    ):
        _small_field(pdf, label, value, cursor, row2_y, field_width, field_h)
        cursor += field_width + gap
    row3_y = row2_y - 11.5 * mm
    professional = [
        ('16 - Nome do Profissional Solicitante', request.medico.nome, 135),
        ('17 - Conselho Profissional', request.medico.conselho, 48),
        ('18 - Número no Conselho', request.medico.numero_conselho, 48),
        ('19 - UF', request.medico.uf, 17),
        ('20 - Código CBO', request.medico.cbo, 27),
    ]
    cursor = x
    fitted_widths = fit_row_widths([item[2] for item in professional])
    for (label, value, _), field_width in zip(
        professional, fitted_widths, strict=True
    ):
        _small_field(pdf, label, value, cursor, row3_y, field_width, field_h)
        cursor += field_width + gap

    _classic_text(
        pdf,
        'DADOS DA SOLICITAÇÃO/PROCEDIMENTO E EXAMES',
        x,
        row3_y - 4 * mm,
        5.5,
        True,
    )
    request_y = row3_y - 16 * mm
    request_fields = [
        (
            '21 - Data/Hora da Solicitação',
            f'{datetime.now():%d/%m/%Y %H:%M}',
            59,
        ),
        (
            '22 - Caráter da Solicitação',
            (
                '1 - Eletiva'
                if request.natureza == 'eletivo'
                else '2 - Urgência/Emergência'
            ),
            52,
        ),
        ('23 - CID', '', 37),
        ('24 - Indicação Clínica', request.indicacao_clinica, 127),
    ]
    cursor = x
    fitted_widths = fit_row_widths([item[2] for item in request_fields])
    for (label, value, _), field_width in zip(
        request_fields, fitted_widths, strict=True
    ):
        _small_field(
            pdf, label, value, cursor, request_y, field_width, field_h
        )
        cursor += field_width + gap

    procedures_top = request_y - 3 * mm
    _classic_text(pdf, '25 - Tabela', x, procedures_top, 5)
    _classic_text(
        pdf,
        '26 - Código do Procedimento',
        x + 25 * mm,
        procedures_top,
        5,
    )
    _classic_text(pdf, '27 - Descrição', x + 78 * mm, procedures_top, 5)
    _classic_text(pdf, '28 - Qtde.Solic.', x + 225 * mm, procedures_top, 5)
    _classic_text(pdf, '29 - Qtde.Autor.', x + 263 * mm, procedures_top, 5)
    for index in range(5):
        y = procedures_top - (index + 1) * 8 * mm
        _classic_text(pdf, f'{index + 1} -', x + 2 * mm, y, 5.5)
        if index < len(exams):
            exam = exams[index]
            _classic_text(pdf, '22', x + 15 * mm, y, 5.5)
            _classic_text(pdf, exam.codigo, x + 27 * mm, y, 6, True)
            _classic_paragraph(
                pdf,
                exam.descricao,
                x + 78 * mm,
                y - 1.5 * mm,
                140 * mm,
                5 * mm,
                5.8,
            )
            _classic_text(pdf, '1', x + 236 * mm, y, 6)
        pdf.line(x + 25 * mm, y - 1 * mm, x + 73 * mm, y - 1 * mm)
        pdf.line(x + 78 * mm, y - 1 * mm, x + 218 * mm, y - 1 * mm)

    lower_y = margin + 10 * mm
    _classic_box(pdf, x, lower_y, content_width - 8 * mm, 39 * mm, 0.45)
    _classic_text(
        pdf,
        '64 - Observação / Fone do Prestador',
        x + 2 * mm,
        lower_y + 34 * mm,
        5.5,
    )
    _classic_paragraph(
        pdf,
        request.indicacao_clinica,
        x + 2 * mm,
        lower_y + 8 * mm,
        132 * mm,
        23 * mm,
        6,
    )
    pdf.line(x + 140 * mm, lower_y, x + 140 * mm, lower_y + 39 * mm)
    _classic_center(
        pdf,
        'AUTORIZO O PRESTADOR A DISPONIBILIZAR OS RESULTADOS',
        x + 210 * mm,
        lower_y + 25 * mm,
        5.5,
        True,
    )
    _classic_center(
        pdf,
        'Assinatura do Usuário / Representante',
        x + 246 * mm,
        lower_y + 4 * mm,
        5,
    )


def _draw_military_page(  # noqa: PLR0913, PLR0915
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
    page_number: int,
    page_total: int,
    convenio_id: str,
    title: str,
):
    width, height = A4
    pdf.setPageSize(A4)
    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, width, height, stroke=0, fill=1)
    margin = 19 * mm
    content_width = width - 2 * margin

    header_y = height - 47 * mm
    _classic_box(pdf, margin, header_y, content_width, 27 * mm, 0.9)
    logo_crops = {
        'funsa': ('reference-funsa.png', (185, 18, 385, 125)),
        'fusex': ('reference-fusex.png', (100, 55, 195, 112)),
        'fusma': ('reference-fusma.png', (80, 45, 180, 103)),
    }
    image_name, crop = logo_crops[convenio_id]
    _draw_reference_crop(
        pdf,
        image_name,
        crop,
        margin + 7 * mm,
        header_y + 4 * mm,
        40 * mm,
        19 * mm,
    )
    _classic_center(
        pdf, title, width / 2 + 20 * mm, header_y + 18 * mm, 9, True
    )
    _classic_center(
        pdf,
        'PROCEDIMENTOS E EXAMES',
        width / 2 + 20 * mm,
        header_y + 9 * mm,
        9,
        True,
    )
    _classic_text(
        pdf,
        f'Folha {page_number}/{page_total}',
        width - margin - 22 * mm,
        header_y + 3 * mm,
        5.5,
    )

    data_y = height - 107 * mm
    data_h = 45 * mm
    _classic_box(pdf, margin, data_y, content_width, data_h, 0.9)
    _classic_center(
        pdf, 'DADOS GERAIS', width / 2, data_y + data_h - 6 * mm, 7, True
    )
    _classic_text(pdf, 'PACIENTE:', margin + 2 * mm, data_y + 27 * mm, 6, True)
    _classic_text(
        pdf, request.paciente.nome, margin + 23 * mm, data_y + 27 * mm, 8
    )
    pdf.line(
        margin + 22 * mm,
        data_y + 25.5 * mm,
        width - margin - 3 * mm,
        data_y + 25.5 * mm,
    )
    _classic_text(pdf, 'CONVÊNIO:', margin + 2 * mm, data_y + 16 * mm, 6, True)
    _classic_text(
        pdf,
        request.paciente.convenio,
        margin + 23 * mm,
        data_y + 16 * mm,
        8,
        True,
    )
    _classic_text(
        pdf, 'MÉDICO SOLICITANTE:', margin + 2 * mm, data_y + 5 * mm, 6, True
    )
    _classic_text(
        pdf, request.medico.nome, margin + 39 * mm, data_y + 5 * mm, 7
    )
    _classic_text(
        pdf,
        f'{request.medico.conselho}: {request.medico.numero_conselho}',
        width - margin - 48 * mm,
        data_y + 5 * mm,
        7,
    )

    indication_y = height - 145 * mm
    _classic_box(pdf, margin, indication_y, content_width, 31 * mm, 0.9)
    _classic_text(
        pdf,
        'INDICAÇÃO CLÍNICA:',
        margin + 2 * mm,
        indication_y + 26 * mm,
        6,
        True,
    )
    _classic_paragraph(
        pdf,
        request.indicacao_clinica,
        margin + 2 * mm,
        indication_y + 3 * mm,
        content_width - 4 * mm,
        20 * mm,
        7,
    )

    procedure_y = height - 197 * mm
    procedure_h = 43 * mm
    _classic_box(pdf, margin, procedure_y, content_width, procedure_h, 0.9)
    _classic_text(
        pdf,
        'PROCEDIMENTOS SOLICITADOS:',
        margin + 2 * mm,
        procedure_y + procedure_h - 6 * mm,
        6,
        True,
    )
    _classic_center(
        pdf,
        'CÓDIGO DO PROCEDIMENTO',
        margin + 43 * mm,
        procedure_y + 30 * mm,
        5,
    )
    _classic_center(
        pdf, 'DESCRIÇÃO', margin + 106 * mm, procedure_y + 30 * mm, 5
    )
    _classic_center(
        pdf, 'QTDE. SOLICIT.', margin + 153 * mm, procedure_y + 30 * mm, 5
    )
    for index in range(5):
        row_y = procedure_y + 24 * mm - index * 5.8 * mm
        if index < len(exams):
            exam = exams[index]
            _classic_text(pdf, exam.codigo, margin + 15 * mm, row_y, 6.5, True)
            _classic_paragraph(
                pdf,
                exam.descricao,
                margin + 66 * mm,
                row_y - 1.5 * mm,
                76 * mm,
                5 * mm,
                6,
            )
            _classic_text(pdf, '1', margin + 151 * mm, row_y, 6.5)
        pdf.line(
            margin + 14 * mm,
            row_y - 1 * mm,
            margin + 57 * mm,
            row_y - 1 * mm,
        )
        pdf.line(
            margin + 66 * mm,
            row_y - 1 * mm,
            margin + 142 * mm,
            row_y - 1 * mm,
        )

    justification_y = height - 249 * mm
    justification_h = 43 * mm
    _classic_box(
        pdf, margin, justification_y, content_width, justification_h, 0.9
    )
    _classic_text(
        pdf,
        'JUSTIFICATIVA:',
        margin + 2 * mm,
        justification_y + justification_h - 6 * mm,
        6,
        True,
    )
    _classic_paragraph(
        pdf,
        request.indicacao_clinica,
        margin + 2 * mm,
        justification_y + 3 * mm,
        content_width - 4 * mm,
        justification_h - 12 * mm,
        7,
    )
    for line in range(5):
        line_y = justification_y + 7 * mm + line * 6 * mm
        pdf.setStrokeColor(colors.HexColor('#777777'))
        pdf.setLineWidth(0.3)
        pdf.line(
            margin + 2 * mm,
            line_y,
            width - margin - 2 * mm,
            line_y,
        )

    signature_y = 16 * mm
    _classic_box(pdf, margin, signature_y, content_width, 19 * mm, 0.9)
    _classic_text(
        pdf,
        'DATA E ASSINATURA DO MÉDICO SOLICITANTE:',
        margin + 2 * mm,
        signature_y + 13 * mm,
        6,
        True,
    )
    _classic_text(
        pdf,
        f'{datetime.now():%d/%m/%Y}',
        margin + 2 * mm,
        signature_y + 4 * mm,
        7,
    )


def _ipm_template_page(pdf: canvas.Canvas, model: str):
    width, height = A4
    template_path = ASSET_DIR / f'ipm-{model}.png'
    with Image.open(template_path) as template:
        source_width, source_height = template.size
    pdf.setPageSize(A4)
    pdf.drawImage(
        ImageReader(str(template_path)),
        0,
        0,
        width,
        height,
        preserveAspectRatio=False,
        mask='auto',
    )

    scale_x = width / source_width
    scale_y = height / source_height

    def put(
        text_value: str | None,
        x_px: float,
        top_px: float,
        size: float = 7,
        bold: bool = True,
        max_width_px: float | None = None,
    ):
        if not text_value:
            return
        rendered = str(text_value).strip()
        font_name = PDF_FONT_BOLD if bold else PDF_FONT
        max_width = max_width_px * scale_x if max_width_px else None
        if max_width:
            while (
                len(rendered) > 4
                and pdfmetrics.stringWidth(rendered, font_name, size)
                > max_width
            ):
                rendered = f'{rendered[:-4]}...'
        pdf.setFillColor(colors.black)
        pdf.setFont(font_name, size)
        pdf.drawString(x_px * scale_x, height - top_px * scale_y - size, rendered)

    def block(
        text_value: str | None,
        x_px: float,
        top_px: float,
        width_px: float,
        height_px: float,
        size: float = 7,
    ):
        if not text_value:
            return
        safe = (
            str(text_value)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('\n', '<br/>')
        )
        style = ParagraphStyle(
            f'ipm-{model}-{x_px}-{top_px}',
            fontName=PDF_FONT,
            fontSize=size,
            leading=size + 1.5,
            textColor=colors.black,
            spaceAfter=0,
            spaceBefore=0,
        )
        paragraph = Paragraph(safe, style)
        box_width = width_px * scale_x
        box_height = height_px * scale_y
        _, rendered_height = paragraph.wrap(box_width, box_height)
        paragraph.drawOn(
            pdf,
            x_px * scale_x,
            height - top_px * scale_y - rendered_height,
        )

    def mark(x_px: float, top_px: float):
        put('X', x_px, top_px, 8, True)

    return put, block, mark


def _ipm_doctor_text(request: GuideRequest) -> str:
    registration = ' '.join(
        item
        for item in (
            request.medico.conselho,
            request.medico.numero_conselho,
            request.medico.uf,
        )
        if item
    )
    return f'{request.medico.nome} - {registration}'.strip(' -')


def _draw_ipm_g1_page(
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
):
    put, block, mark = _ipm_template_page(pdf, 'g1')
    patient = request.paciente
    put(patient.nome, 88, 489, 7.5, True, 625)
    put(patient.carteira, 735, 489, 7.5, True, 220)
    put(patient.data_nascimento, 88, 548, 7, True, 285)
    if (patient.sexo or '').upper().startswith('M'):
        mark(399, 548)
    elif (patient.sexo or '').upper().startswith('F'):
        mark(433, 548)
    put(patient.telefone, 620, 548, 7, True, 335)
    block(_ipm_doctor_text(request), 88, 610, 475, 55, 7.3)
    put(request.hospital, 88, 715, 7.3, True, 860)
    block(request.indicacao_clinica, 88, 775, 860, 120, 7.3)
    put(request.ipm_numero_servico or '3', 424, 944, 7.3, True, 40)
    for index, exam in enumerate(exams[:5]):
        top = 982 + index * 23
        put(exam.descricao, 245, top, 7, False, 525)
        put(exam.codigo, 850, top, 7, True, 85)
    put(_ipm_doctor_text(request), 88, 1160, 7, False, 665)
    put(f'{datetime.now():%d/%m/%Y}', 786, 1160, 7, True, 150)


def _draw_ipm_g2_page(
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
    has_opme: bool,
):
    put, block, mark = _ipm_template_page(pdf, 'g2')
    patient = request.paciente
    mark(271 if request.natureza == 'eletivo' else 371, 302)
    mark(773, 302)
    put(patient.nome, 48, 365, 7.5, True, 725)
    put(patient.carteira, 795, 365, 7.5, True, 160)
    put(patient.data_nascimento, 48, 414, 7, True, 300)
    if (patient.sexo or '').upper().startswith('M'):
        mark(370, 414)
    elif (patient.sexo or '').upper().startswith('F'):
        mark(397, 414)
    put(patient.telefone, 605, 414, 7, True, 350)
    block(_ipm_doctor_text(request), 48, 485, 495, 62, 7.3)
    put(request.hospital, 48, 590, 7.3, True, 690)
    put(request.data_internacao, 760, 590, 7.3, True, 190)
    block(request.indicacao_clinica, 48, 662, 900, 125, 7.3)
    mark(51, 851 if not has_opme else 874)
    procedures = '\n'.join(
        f'{exam.codigo} - {exam.descricao}' for exam in exams[:8]
    )
    block(procedures, 75, 894, 860, 176, 7)
    put(_ipm_doctor_text(request), 48, 1140, 7, False, 725)
    put(f'{datetime.now():%d/%m/%Y}', 796, 1140, 7, True, 155)


def _draw_ipm_g3_page(
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
):
    put, block, mark = _ipm_template_page(pdf, 'g3')
    patient = request.paciente
    put(patient.nome, 48, 510, 7.5, True, 660)
    put(patient.carteira, 725, 510, 7.5, True, 225)
    put(patient.data_nascimento, 48, 573, 7, True, 300)
    if (patient.sexo or '').upper().startswith('M'):
        mark(370, 573)
    elif (patient.sexo or '').upper().startswith('F'):
        mark(405, 573)
    put(patient.telefone, 605, 573, 7, True, 350)
    block(_ipm_doctor_text(request), 48, 635, 885, 22, 7.3)
    put(request.hospital, 48, 694, 7.3, True, 885)
    block(request.indicacao_clinica, 48, 760, 885, 110, 7.3)
    put(request.ipm_numero_servico or '7', 424, 919, 7.3, True, 40)
    for index, exam in enumerate(exams[:10]):
        top = 956 + index * 24.5
        put(exam.descricao, 245, top, 6.8, False, 525)
        put(exam.codigo, 850, top, 6.8, True, 85)
    put(_ipm_doctor_text(request), 48, 1235, 7, False, 720)
    put(f'{datetime.now():%d/%m/%Y}', 794, 1235, 7, True, 155)


def _draw_ipm_g4_page(pdf: canvas.Canvas, request: GuideRequest):
    put, block, mark = _ipm_template_page(pdf, 'g4')
    patient = request.paciente
    put(patient.nome, 48, 330, 7.5, True, 725)
    put(patient.carteira, 795, 330, 7.5, True, 160)
    put(patient.data_nascimento, 48, 380, 7, True, 300)
    if (patient.sexo or '').upper().startswith('M'):
        mark(370, 380)
    elif (patient.sexo or '').upper().startswith('F'):
        mark(397, 380)
    put(patient.telefone, 605, 380, 7, True, 350)
    block(_ipm_doctor_text(request), 48, 452, 495, 58, 7.3)
    put(request.hospital, 48, 558, 7.3, True, 690)
    put(request.data_internacao, 760, 558, 7.3, True, 190)
    block('\n'.join(request.ipm_opme), 48, 625, 900, 440, 8)
    put(_ipm_doctor_text(request), 48, 1140, 7, False, 725)
    put(f'{datetime.now():%d/%m/%Y}', 796, 1140, 7, True, 155)


def _draw_ipm_official_page(  # noqa: PLR0915
    pdf: canvas.Canvas,
    request: GuideRequest,
    exams: list[ExamRequest],
    hide_operator_logo: bool = False,
):
    width = 842.04
    height = 555.0
    pdf.setPageSize((width, height))
    pdf.drawImage(
        ImageReader(str(ASSET_DIR / 'ipm-sadt-oficial.png')),
        0,
        0,
        width,
        height,
        preserveAspectRatio=False,
        mask='auto',
    )

    def cover(x0: float, top: float, x1: float, bottom: float):
        pdf.setFillColor(colors.white)
        pdf.rect(x0, height - bottom, x1 - x0, bottom - top, stroke=0, fill=1)

    def value(  # noqa: PLR0913
        text_value: str | None,
        x_pos: float,
        top: float,
        size: float = 6.2,
        bold: bool = True,
        max_width: float | None = None,
    ):
        if not text_value:
            return
        rendered = str(text_value)
        font_name = PDF_FONT_BOLD if bold else PDF_FONT
        if max_width:
            ellipsis_length = 3
            while (
                len(rendered) > ellipsis_length
                and pdfmetrics.stringWidth(rendered, font_name, size)
                > max_width
            ):
                rendered = f'{rendered[:-4]}...'
        pdf.setFillColor(colors.black)
        pdf.setFont(font_name, size)
        pdf.drawString(x_pos, height - top - size, rendered)

    def boxed_digits(
        text_value: str | None,
        centers: list[float],
        top: float,
        size: float = 5.8,
    ):
        digits = re.sub(r'\D', '', text_value or '')
        pdf.setFillColor(colors.black)
        pdf.setFont(PDF_FONT_BOLD, size)
        for digit, center in zip(digits, centers, strict=False):
            pdf.drawCentredString(center, height - top - size, digit)

    # No atendimento particular, preserva o formulário TISS tradicional sem
    # identificar uma operadora específica no cabeçalho.
    if hide_operator_logo:
        cover(20, 5, width - 20, 29)
        pdf.setFillColor(colors.black)
        pdf.setFont(PDF_FONT, 11)
        pdf.drawCentredString(
            width / 2,
            height - 21,
            (
                'GUIA DE SERVIÇO PROFISSIONAL/SERVIÇO AUXILIAR DE '
                'DIAGNÓSTICO E TERAPIA - SP/SADT Nº'
            ),
        )

    # Remove os dados de exemplo incorporados ao arquivo oficial.
    cover(668, 10.5, 733, 25.5)
    cover(495, 69, 671, 81)
    cover(461, 153, 813, 163)
    cover(244, 184.5, 590, 194)

    # Beneficiário.
    value(request.paciente.carteira, 34, 75, 5.7)
    value(request.paciente.plano, 221, 70)
    boxed_digits(
        request.paciente.validade_carteira,
        [
            393.25,
            404.77,
            422.88,
            434.47,
            452.64,
            464.17,
            475.69,
            487.21,
        ],
        69.4,
        5.5,
    )
    value(request.paciente.nome, 497, 70, 7.2)
    value(request.paciente.cns, 681, 70)

    # Contratado e profissional solicitante.
    value('Hospital PRONTOCARDIO', 222, 101)
    value(request.medico.cnes, 716, 101)
    value(request.medico.nome, 33, 126)
    value(request.medico.conselho, 410, 126)
    value(request.medico.numero_conselho, 551, 126)
    value(request.medico.uf, 694, 126)
    value(request.medico.cbo, 744, 126)

    # Solicitação.
    now = datetime.now()
    boxed_digits(
        f'{now:%d%m%y%H%M}',
        [
            39.42,
            50.94,
            69.06,
            80.66,
            98.84,
            110.36,
            125.12,
            136.70,
            154.87,
            166.40,
        ],
        154.8,
        5.5,
    )
    value('E' if request.natureza == 'eletivo' else 'U', 213, 151, 6.5)
    value(request.indicacao_clinica, 463, 154, 6.2)

    # Procedimentos solicitados. O modelo IPM aceita cinco itens por folha.
    first_top = 185.5
    row_step = 13.0
    table_centers = [52.86, 64.38]
    procedure_centers = [116.12 + 11.58 * index for index in range(10)]
    requested_centers = [705.42, 717.0]
    for index, exam in enumerate(exams[:5]):
        row_top = first_top + index * row_step
        boxed_digits('22', table_centers, row_top, 5.6)
        boxed_digits(exam.codigo, procedure_centers, row_top, 5.6)
        value(exam.descricao, 247, row_top, 6.1, False, 340)
        boxed_digits('01', requested_centers, row_top, 5.6)


def generate_pdf(request: GuideRequest) -> bytes:
    if request.formato == 'ipm':
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
        has_opme = bool(request.ipm_opme) or any(
            _normalize(exam.tipo) == 'opme' for exam in request.exames
        )
        models = select_ipm_models(
            request.paciente.tipo_atendimento,
            request.ipm_finalidade,
            has_opme,
        )
        primary = models[0]
        capacity = 10 if primary == 'g3' else 5 if primary == 'g1' else 100
        pages = [
            request.exames[index : index + capacity]
            for index in range(0, len(request.exames), capacity)
        ]
        pdf.setTitle(
            f'Guia IPM - atendimento {request.paciente.cd_atendimento}'
        )
        pdf.setAuthor(request.medico.nome)
        pdf.setSubject(f"IPM {' + '.join(model.upper() for model in models)}")
        for exams in pages:
            if primary == 'g1':
                _draw_ipm_g1_page(pdf, request, exams)
            elif primary == 'g2':
                _draw_ipm_g2_page(pdf, request, exams, has_opme)
            else:
                _draw_ipm_g3_page(pdf, request, exams)
            pdf.showPage()
        if 'g4' in models:
            _draw_ipm_g4_page(pdf, request)
            pdf.showPage()
        pdf.save()
        return output.getvalue()

    capacities = {
        'ans': 5,
        'sadt': 10,
        'ipm': 5,
        'issec': 3,
        'funsa': 5,
        'fusex': 5,
        'fusma': 5,
    }
    capacity = capacities[request.formato]
    pages = [
        request.exames[index : index + capacity]
        for index in range(0, len(request.exames), capacity)
    ]
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    pdf.setTitle(
        f'Solicitação SADT - atendimento {request.paciente.cd_atendimento}'
    )
    pdf.setAuthor(request.medico.nome)
    for index, exams in enumerate(pages, start=1):
        if request.formato == 'issec':
            _draw_issec_page(pdf, request, exams, index, len(pages))
        elif request.formato == 'ans':
            _draw_ipm_official_page(
                pdf,
                request,
                exams,
                hide_operator_logo=True,
            )
        elif request.formato in {'funsa', 'fusex', 'fusma'}:
            titles = {
                'funsa': 'GUIA DE SOLICITAÇÃO DE',
                'fusex': 'GUIA DE SOLICITAÇÃO DE',
                'fusma': 'GUIA DE SOLICITAÇÃO DE',
            }
            _draw_military_page(
                pdf,
                request,
                exams,
                index,
                len(pages),
                request.formato,
                titles[request.formato],
            )
        else:
            _draw_sadt_page(pdf, request, exams, index, len(pages))
        pdf.showPage()
    pdf.save()
    return output.getvalue()


@app.post('/api/pdf')
def pdf(request: GuideRequest):
    for exam in request.exames:
        official = TUSS_BY_CODE.get(exam.codigo)
        if not official:
            raise HTTPException(
                status_code=422,
                detail=f'Código TUSS não reconhecido: {exam.codigo}.',
            )
    content = generate_pdf(request)
    filename = f'sadt-atendimento-{request.paciente.cd_atendimento}.pdf'
    capacities = {
        'ans': 5,
        'sadt': 10,
        'ipm': 5,
        'issec': 3,
        'funsa': 5,
        'fusex': 5,
        'fusma': 5,
    }
    capacity = capacities[request.formato]
    page_count = (len(request.exames) + capacity - 1) // capacity
    if request.formato == 'ipm':
        has_opme = bool(request.ipm_opme) or any(
            _normalize(exam.tipo) == 'opme' for exam in request.exames
        )
        models = select_ipm_models(
            request.paciente.tipo_atendimento,
            request.ipm_finalidade,
            has_opme,
        )
        page_count = ipm_page_count(len(request.exames), models)
    return Response(
        content=content,
        media_type='application/pdf',
        headers={
            'Content-Disposition': f'inline; filename="{filename}"',
            'X-Document-Pages': str(page_count),
        },
    )


@app.post('/api/mv/anexar')
def attach_to_mv(request: GuideRequest):
    if not MV_WRITE_ENABLED:
        raise HTTPException(
            status_code=403,
            detail=(
                'Envio ao MV está bloqueado neste protótipo. '
                'Gere e valide o PDF antes de habilitar a gravação.'
            ),
        )
    pdf_content = generate_pdf(request)
    timestamp = datetime.now()
    filename = (
        f'SADT-{request.paciente.cd_atendimento}-{request.formato}-'
        f'{timestamp:%Y%m%d-%H%M%S}.pdf'
    )
    description = (
        f'SADT {request.formato.upper()} - '
        f'{request.indicacao_clinica.strip()}'
    )[:4000]

    try:
        with oracle_engine.begin() as connection:
            attendance = connection.execute(
                text(
                    """
                    SELECT cd_paciente,
                           cd_prestador
                      FROM dbamv.atendime
                     WHERE cd_atendimento = :cd_atendimento
                    """
                ),
                {'cd_atendimento': request.paciente.cd_atendimento},
            ).mappings().one_or_none()
            if attendance is None:
                raise HTTPException(
                    status_code=404,
                    detail='Atendimento não encontrado no MV.',
                )
            if int(attendance['cd_paciente']) != request.paciente.cd_paciente:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        'O paciente informado não corresponde ao atendimento '
                        'no MV.'
                    ),
                )

            mv_user = connection.execute(
                text('SELECT USER FROM dual')
            ).scalar_one()
            document_id = connection.execute(
                text('SELECT dbamv.seq_arquivo_documento.NEXTVAL FROM dual')
            ).scalar_one()
            attachment_id = connection.execute(
                text('SELECT dbamv.seq_arquivo_atendimento.NEXTVAL FROM dual')
            ).scalar_one()
            clinical_document_id = connection.execute(
                text(
                    'SELECT dbamv.seq_pw_documento_clinico.NEXTVAL FROM dual'
                )
            ).scalar_one()

            connection.execute(
                text(
                    """
                    INSERT INTO dbamv.pw_documento_clinico (
                        cd_documento_clinico,
                        cd_tipo_documento,
                        cd_paciente,
                        cd_atendimento,
                        cd_usuario,
                        cd_prestador,
                        tp_status,
                        dh_referencia,
                        dh_criacao,
                        dh_fechamento,
                        tp_extensao,
                        cd_objeto,
                        nm_documento,
                        dh_documento
                    ) VALUES (
                        :clinical_document_id,
                        :document_type,
                        :cd_paciente,
                        :cd_atendimento,
                        :mv_user,
                        :cd_prestador,
                        :document_status,
                        TRUNC(SYSDATE),
                        SYSDATE,
                        SYSDATE,
                        :document_extension,
                        :document_object,
                        :filename,
                        TRUNC(SYSDATE)
                    )
                    """
                ),
                {
                    'clinical_document_id': clinical_document_id,
                    'document_type': (
                        MV_PW_TIPO_DOCUMENTO_REQUISICAO_MEDICA
                    ),
                    'cd_paciente': request.paciente.cd_paciente,
                    'cd_atendimento': request.paciente.cd_atendimento,
                    'mv_user': mv_user,
                    'cd_prestador': attendance['cd_prestador'],
                    'document_status': MV_STATUS_DOCUMENTO_FECHADO,
                    'document_extension': MV_EXTENSAO_DOCUMENTO_ANEXO,
                    'document_object': MV_OBJETO_ANEXO_PRONTUARIO,
                    'filename': filename,
                },
            )

            connection.execute(
                text(
                    """
                    INSERT INTO dbamv.arquivo_documento (
                        cd_arquivo_documento,
                        lo_arquivo_documento,
                        tp_extensao,
                        ds_autor,
                        ds_origem,
                        dt_documento,
                        ds_nome_arquivo
                    ) VALUES (
                        :document_id,
                        :pdf_content,
                        'pdf',
                        :author,
                        'SADT FASTAPI',
                        TRUNC(SYSDATE),
                        :filename
                    )
                    """
                ),
                {
                    'document_id': document_id,
                    'pdf_content': pdf_content,
                    'author': request.medico.nome[:50],
                    'filename': filename,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO dbamv.arquivo_atendimento (
                        cd_arquivo_atendimento,
                        cd_arquivo_documento,
                        cd_atendimento,
                        dh_criacao,
                        nm_usuario,
                        cd_paciente,
                        cd_pw_tipo_documento,
                        cd_documento_clinico,
                        cd_objeto_selecionado,
                        ds_descricao,
                        cd_status_arquivo_atendimento
                    ) VALUES (
                        :attachment_id,
                        :document_id,
                        :cd_atendimento,
                        SYSDATE,
                        :mv_user,
                        :cd_paciente,
                        :document_type,
                        :clinical_document_id,
                        :document_object,
                        :description,
                        :attachment_status
                    )
                    """
                ),
                {
                    'attachment_id': attachment_id,
                    'document_id': document_id,
                    'cd_atendimento': request.paciente.cd_atendimento,
                    'mv_user': mv_user,
                    'cd_paciente': request.paciente.cd_paciente,
                    'document_type': (
                        MV_PW_TIPO_DOCUMENTO_REQUISICAO_MEDICA
                    ),
                    'clinical_document_id': clinical_document_id,
                    'document_object': MV_OBJETO_ANEXO_PRONTUARIO,
                    'description': description,
                    'attachment_status': MV_STATUS_ARQUIVO_ANEXADO,
                },
            )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail='Não foi possível anexar o PDF ao prontuário MV.',
        ) from error

    return {
        'status': 'anexado',
        'cd_arquivo_atendimento': int(attachment_id),
        'cd_arquivo_documento': int(document_id),
        'cd_documento_clinico': int(clinical_document_id),
        'cd_atendimento': request.paciente.cd_atendimento,
        'arquivo': filename,
    }
