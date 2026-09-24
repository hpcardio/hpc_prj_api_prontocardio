import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookiejar import CookieJar
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.routers.agendamentos import (
    garantir_paciente_autorizado,
    valida_acesso_agendamento,
)
from app_prontocardio.routers.paciente_auth import PrincipalPaciente

router = APIRouter(prefix='/resultados', tags=['resultados laboratoriais'])

PORTAL_LAUDOS_BASE_URL = os.getenv(
    'PORTAL_LAUDOS_BASE_URL',
    'https://2361prd-exames.cloudmv.com.br/portal-laudos',
).rstrip('/')
ValidaUsuarioAtual = Annotated[
    Usuario | PrincipalPaciente,
    Depends(valida_acesso_agendamento),
]
SessaoOracle = Annotated[Session, Depends(get_session_oracle)]
MAX_PORTAL_ITEM_CODE_LENGTH = 256


def _portal_request(
    opener,
    path: str,
    *,
    data: dict | None = None,
    headers: dict | None = None,
    timeout: int = 45,
):
    encoded = (
        urllib.parse.urlencode(data).encode('utf-8')
        if data is not None
        else None
    )
    request = urllib.request.Request(
        f'{PORTAL_LAUDOS_BASE_URL}{path}',
        data=encoded,
        headers={
            'User-Agent': 'API-ProntoCardio/1.0',
            **(headers or {}),
        },
        method='POST' if encoded is not None else 'GET',
    )
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise HTTPException(
                status_code=502,
                detail='O Portal de Laudos recusou a autenticação do pedido.',
            ) from exc
        raise HTTPException(
            status_code=502,
            detail='O Portal de Laudos não conseguiu gerar o documento.',
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503,
            detail='O Portal de Laudos está temporariamente indisponível.',
        ) from exc


def _portal_session(cd_atendimento: int, cd_ped_lab: int):
    cookies = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies),
    )
    account_request = urllib.request.Request(
        f'{PORTAL_LAUDOS_BASE_URL}/api/account',
        headers={'User-Agent': 'API-ProntoCardio/1.0'},
    )
    try:
        with opener.open(account_request, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 405):
            raise HTTPException(
                status_code=502,
                detail='O Portal de Laudos não iniciou uma sessão válida.',
            ) from exc

    csrf = next(
        (
            urllib.parse.unquote(cookie.value.strip('"'))
            for cookie in cookies
            if cookie.name == 'CSRF-TOKEN'
        ),
        '',
    )
    validacao_query = urllib.parse.urlencode({
        'login': str(cd_atendimento),
        'senha': str(cd_ped_lab),
    })
    with _portal_request(
        opener,
        f'/api/validarUsuarioLegado?{validacao_query}',
        data={},
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            **({'X-CSRF-TOKEN': csrf} if csrf else {}),
        },
        timeout=30,
    ) as response:
        if response.status != HTTPStatus.ACCEPTED:
            raise HTTPException(
                status_code=502,
                detail='O Portal de Laudos não reconheceu o pedido informado.',
            )
    with _portal_request(
        opener,
        '/api/authentication',
        data={
            'j_username': str(cd_atendimento),
            'j_password': str(cd_ped_lab),
            'remember-me': 'false',
            'submit': 'Login',
        },
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
            **({'X-CSRF-TOKEN': csrf} if csrf else {}),
        },
        timeout=30,
    ):
        pass

    with _portal_request(
        opener,
        '/api/account',
        headers={'Accept': 'application/json'},
        timeout=30,
    ):
        pass

    csrf = next(
        (
            urllib.parse.unquote(cookie.value.strip('"'))
            for cookie in cookies
            if cookie.name == 'CSRF-TOKEN'
        ),
        '',
    )
    if not csrf:
        raise HTTPException(
            status_code=502,
            detail='O Portal de Laudos não forneceu uma sessão válida.',
        )
    return opener, csrf


def _iso_portal(value: datetime) -> str:
    value_utc = value.astimezone(timezone.utc)
    return value_utc.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _pedido_portal(
    opener,
    csrf: str,
    cd_ped_lab: int,
    dt_pedido: datetime,
) -> dict:
    inicio = dt_pedido - timedelta(days=2)
    fim = dt_pedido + timedelta(days=2)
    inicio_iso = _iso_portal(inicio)
    fim_iso = _iso_portal(fim)
    query = urllib.parse.urlencode({
        'filtroDataInicial': inicio_iso,
        'filtroDataInicialDto': inicio_iso,
        'filtroDataFinal': fim_iso,
        'filtroDataFinalDto': fim_iso,
        'totalPedidosLiberados': 0,
        'totalPedidosNaoLiberados': 0,
        'totalPedidosParcialmenteLiberados': 0,
        'totalRegistros': 0,
        'numeroPagina': 1,
    })
    with _portal_request(
        opener,
        f'/pedidos/listar?{query}',
        data={},
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-CSRF-TOKEN': csrf,
        },
        timeout=60,
    ) as response:
        try:
            payload = json.load(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=502,
                detail='O Portal de Laudos retornou uma listagem inválida.',
            ) from exc

    pedidos = payload.get('pedidos', []) if isinstance(payload, dict) else []
    pedido = next(
        (
            item
            for item in pedidos
            if str(item.get('codigoPedido') or '') == str(cd_ped_lab)
        ),
        None,
    )
    if not pedido:
        raise HTTPException(
            status_code=404,
            detail='O pedido não foi localizado no Portal de Laudos.',
        )

    return pedido


def _itens_liberados_portal(
    opener,
    csrf: str,
    cd_ped_lab: int,
    dt_pedido: datetime,
) -> list[dict]:
    pedido = _pedido_portal(
        opener,
        csrf,
        cd_ped_lab,
        dt_pedido,
    )

    selecionaveis = {'L', 'P', 'D', 'A'}
    itens = []
    for item in pedido.get('itensPedido') or []:
        if str(item.get('situacao') or '') not in selecionaveis:
            continue
        codigo = str(item.get('codigoItemPedido') or '').strip()
        codigo_valido = (
            codigo
            and len(codigo) <= MAX_PORTAL_ITEM_CODE_LENGTH
            and re.fullmatch(r'[A-Za-z0-9+/=_-]+', codigo)
        )
        if codigo_valido:
            itens.append({
                'codigo': codigo,
                'nome': str(item.get('nomeExame') or '').strip(),
                'situacao': str(item.get('situacao') or ''),
            })
    if not itens:
        raise HTTPException(
            status_code=404,
            detail='Este pedido ainda não possui exames liberados.',
        )
    return itens


def _nome_normalizado(valor: str) -> str:
    sem_acentos = ''.join(
        caractere
        for caractere in unicodedata.normalize('NFKD', valor)
        if not unicodedata.combining(caractere)
    )
    return ' '.join(sem_acentos.replace('\xa0', ' ').upper().split())


def _pedido_laboratorial_autorizado(
    session: Session,
    cd_paciente: int,
    cd_ped_lab: int,
) -> dict:
    row = session.execute(
        text(
            '''
            SELECT pl.CD_PED_LAB,
                   pl.CD_ATENDIMENTO,
                   pl.DT_PEDIDO
              FROM DBAMV.PED_LAB pl
              JOIN DBAMV.ATENDIME ate
                ON ate.CD_ATENDIMENTO = pl.CD_ATENDIMENTO
             WHERE ate.CD_PACIENTE = :cd_paciente
               AND pl.CD_PED_LAB = :cd_ped_lab
            '''
        ),
        {'cd_paciente': cd_paciente, 'cd_ped_lab': cd_ped_lab},
    ).mappings().first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail='Pedido laboratorial não encontrado para este paciente.',
        )
    return {str(key).lower(): value for key, value in row.items()}


@router.get('/pacientes/{cd_paciente}/laboratoriais')
def consultar_pedidos_laboratoriais_paciente(
    cd_paciente: int,
    usuario_atual: ValidaUsuarioAtual,
    session_oracle: SessaoOracle,
):
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=422, detail='Código do paciente inválido.'
        )
    rows = session_oracle.execute(
        text(
            '''
            SELECT pl.CD_PED_LAB,
                   pl.CD_ATENDIMENTO,
                   pl.DT_PEDIDO,
                   il.CD_ITPED_LAB,
                   il.CD_EXA_LAB,
                   ex.NM_EXA_LAB,
                   il.DT_LAUDO
              FROM DBAMV.PED_LAB pl
              JOIN DBAMV.ATENDIME ate
                ON ate.CD_ATENDIMENTO = pl.CD_ATENDIMENTO
              JOIN DBAMV.ITPED_LAB il
                ON il.CD_PED_LAB = pl.CD_PED_LAB
              JOIN DBAMV.EXA_LAB ex
                ON ex.CD_EXA_LAB = il.CD_EXA_LAB
             WHERE ate.CD_PACIENTE = :cd_paciente
             ORDER BY pl.DT_PEDIDO DESC,
                      pl.CD_PED_LAB DESC,
                      il.CD_ITPED_LAB
            '''
        ),
        {'cd_paciente': cd_paciente},
    ).mappings().all()

    agrupados = {}
    for row in rows:
        item = {str(key).lower(): value for key, value in row.items()}
        cd_ped_lab = int(item.get('cd_ped_lab') or 0)
        if cd_ped_lab not in agrupados:
            agrupados[cd_ped_lab] = {
                'cd_ped_lab': cd_ped_lab,
                'cd_atendimento': item.get('cd_atendimento'),
                'dt_pedido': item.get('dt_pedido'),
                'itens': [],
            }
        dt_laudo = item.get('dt_laudo')
        agrupados[cd_ped_lab]['itens'].append({
            'cd_itped_lab': item.get('cd_itped_lab'),
            'cd_exa_lab': item.get('cd_exa_lab'),
            'nm_exa_lab': item.get('nm_exa_lab'),
            'dt_laudo': (
                dt_laudo.isoformat()
                if hasattr(dt_laudo, 'isoformat')
                else dt_laudo
            ),
            'liberado': dt_laudo is not None,
        })

    results = []
    for pedido in list(agrupados.values())[:100]:
        itens = pedido['itens']
        dt_pedido = pedido['dt_pedido']
        results.append({
            'cd_ped_lab': pedido['cd_ped_lab'],
            'cd_atendimento': pedido['cd_atendimento'],
            'dt_pedido': (
                dt_pedido.isoformat()
                if hasattr(dt_pedido, 'isoformat')
                else dt_pedido
            ),
            'total_exames': len(itens),
            'total_liberados': sum(1 for item in itens if item['liberado']),
            'itens': itens,
        })
    return {'count': len(results), 'results': results}


@router.get('/pacientes/{cd_paciente}/laboratoriais/{cd_ped_lab}/pdf')
def abrir_laudo_laboratorial_pdf(
    cd_paciente: int,
    cd_ped_lab: int,
    usuario_atual: ValidaUsuarioAtual,
    session_oracle: SessaoOracle,
):
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_paciente <= 0 or cd_ped_lab <= 0:
        raise HTTPException(
            status_code=422,
            detail='Paciente ou pedido laboratorial inválido.',
        )
    pedido = _pedido_laboratorial_autorizado(
        session_oracle,
        cd_paciente,
        cd_ped_lab,
    )
    try:
        cd_atendimento = int(pedido['cd_atendimento'])
        dt_pedido = pedido['dt_pedido']
        if not isinstance(dt_pedido, datetime):
            dt_pedido = datetime.combine(
                dt_pedido,
                datetime.min.time(),
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail='O pedido laboratorial possui dados inválidos no MV.',
        ) from exc

    opener, csrf = _portal_session(cd_atendimento, cd_ped_lab)
    itens = _itens_liberados_portal(
        opener,
        csrf,
        cd_ped_lab,
        dt_pedido,
    )
    with _portal_request(
        opener,
        '/pedidos/abrirPedido',
        data={
            'itensPedido': ','.join(item['codigo'] for item in itens),
            '_csrf': csrf,
        },
        headers={
            'Accept': 'application/pdf',
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRF-TOKEN': csrf,
        },
        timeout=120,
    ) as response:
        content_type = response.headers.get_content_type()
        content = response.read()

    if content_type != 'application/pdf' or not content.startswith(b'%PDF'):
        raise HTTPException(
            status_code=502,
            detail='O Portal de Laudos não retornou um PDF válido.',
        )
    return Response(
        content=content,
        media_type='application/pdf',
        headers={
            'Content-Disposition': (
                f'inline; filename="laudo-laboratorial-{cd_ped_lab}.pdf"'
            ),
            'Cache-Control': 'private, no-store',
        },
    )


@router.get(
    '/pacientes/{cd_paciente}/laboratoriais/{cd_ped_lab}/itens/'
    '{cd_itped_lab}/pdf'
)
def abrir_exame_laboratorial_pdf(
    cd_paciente: int,
    cd_ped_lab: int,
    cd_itped_lab: int,
    usuario_atual: ValidaUsuarioAtual,
    session_oracle: SessaoOracle,
):
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    pedido = _pedido_laboratorial_autorizado(
        session_oracle,
        cd_paciente,
        cd_ped_lab,
    )
    itens_oracle = session_oracle.execute(
        text(
            '''
            SELECT il.CD_ITPED_LAB,
                   il.CD_EXA_LAB,
                   ex.NM_EXA_LAB,
                   il.DT_LAUDO
              FROM DBAMV.ITPED_LAB il
              JOIN DBAMV.EXA_LAB ex
                ON ex.CD_EXA_LAB = il.CD_EXA_LAB
             WHERE il.CD_PED_LAB = :cd_ped_lab
             ORDER BY il.CD_ITPED_LAB
            '''
        ),
        {'cd_ped_lab': cd_ped_lab},
    ).mappings().all()
    itens_oracle = [
        {str(key).lower(): value for key, value in row.items()}
        for row in itens_oracle
    ]
    alvo = next(
        (
            item for item in itens_oracle
            if int(item.get('cd_itped_lab') or 0) == cd_itped_lab
        ),
        None,
    )
    if not alvo:
        raise HTTPException(
            status_code=404,
            detail='Exame não encontrado neste pedido laboratorial.',
        )
    if alvo.get('dt_laudo') is None:
        raise HTTPException(
            status_code=409,
            detail='Este exame ainda não possui resultado liberado.',
        )
    mesmo_exame = [
        item for item in itens_oracle
        if item.get('cd_exa_lab') == alvo.get('cd_exa_lab')
    ]
    ocorrencia = next(
        indice for indice, item in enumerate(mesmo_exame)
        if int(item.get('cd_itped_lab') or 0) == cd_itped_lab
    )

    cd_atendimento = int(pedido['cd_atendimento'])
    dt_pedido = pedido['dt_pedido']
    if not isinstance(dt_pedido, datetime):
        dt_pedido = datetime.combine(dt_pedido, datetime.min.time())
    opener, csrf = _portal_session(cd_atendimento, cd_ped_lab)
    itens_portal = _itens_liberados_portal(
        opener,
        csrf,
        cd_ped_lab,
        dt_pedido,
    )
    candidatos = [
        item for item in itens_portal
        if _nome_normalizado(item['nome'])
        == _nome_normalizado(str(alvo.get('nm_exa_lab') or ''))
    ]
    if ocorrencia >= len(candidatos):
        raise HTTPException(
            status_code=502,
            detail='O exame não foi localizado no documento do Portal de Laudos.',
        )
    codigo_item = candidatos[ocorrencia]['codigo']
    with _portal_request(
        opener,
        '/pedidos/abrirPedido',
        data={'itensPedido': codigo_item, '_csrf': csrf},
        headers={
            'Accept': 'application/pdf',
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRF-TOKEN': csrf,
        },
        timeout=120,
    ) as response:
        content_type = response.headers.get_content_type()
        content = response.read()
    if content_type != 'application/pdf' or not content.startswith(b'%PDF'):
        raise HTTPException(
            status_code=502,
            detail='O Portal de Laudos não retornou um PDF válido.',
        )
    return Response(
        content=content,
        media_type='application/pdf',
        headers={
            'Content-Disposition': (
                f'inline; filename="exame-laboratorial-{cd_itped_lab}.pdf"'
            ),
            'Cache-Control': 'private, no-store',
        },
    )
