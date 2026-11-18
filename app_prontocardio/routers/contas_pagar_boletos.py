# ruff: noqa: E501, PLR0913
"""Contas a Pagar: boletos (duplicatas) das NFS-e recebidas.

O setor anexa o PDF do boleto à NFS-e; a linha digitável é lida e validada
(FEBRABAN) e vira a duplicata da nota. Divergências com a NFS-e ficam
registradas e impedem que a nota siga como "pronta" para o financeiro.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from app_prontocardio.routers.contas_pagar_nfse import (
    FUSO_HOSPITAL,
    SQL_OFICINAS_DO_USUARIO,
    SQL_TABELA_SITUACAO,
    AcessoFila,
    SessionOracle,
    SessionPostgres,
    _decimal,
    _empresas_mv,
    _gestor,
    _regras,
    nfse_cancelada,
    status_nfse,
)
from app_prontocardio.services.payables_boleto import (
    BoletoInvalido,
    ler_boleto_pdf,
    ler_linha_digitavel,
)
from app_prontocardio.services.payables_oficinas import (
    STATUS_DEFINIDA,
    somente_digitos,
)

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-pagar/nfse',
    tags=['contas-a-pagar-nfse'],
)

TAMANHO_MAXIMO_PDF = 5 * 1024 * 1024
TOLERANCIA = Decimal('0.01')

SQL_NOTA = text("""
SELECT prestador_cnpj, prestador_razao_social, numero_nfse, valor_servicos,
       valor_liquido_nfse, tomador_cnpj
  FROM api_prontocardio.nfse_xml_tomador
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY data_hora DESC NULLS LAST
 LIMIT 1
""")

SQL_SITUACAO_XML = text("""
SELECT CASE WHEN coalesce(cancelamento_data_hora, '') <> ''
              OR coalesce(motivo_cancelamento_nfse, '') <> ''
            THEN 'CANCELADA' END
  FROM api_prontocardio.nfse_xml_tomador
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY data_hora DESC NULLS LAST
 LIMIT 1
""")

SQL_SITUACAO_PORTAL = text("""
SELECT situacao FROM api_prontocardio.nfse_xml_tomador_situacao
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
""")


def status_da_nota(session, cnpj: str, numero: str) -> str | None:
    chave = {'cnpj': cnpj, 'numero': numero}
    portal = (
        session.execute(SQL_SITUACAO_PORTAL, chave).scalar_one_or_none()
        if session.execute(SQL_TABELA_SITUACAO).scalar_one()
        else None
    )
    return status_nfse(session.execute(SQL_SITUACAO_XML, chave).scalar_one_or_none(), portal)


SQL_BOLETOS_DA_NOTA = text("""
SELECT id, prestador_cnpj, numero_nfse, cd_multi_empresa, nome_arquivo, tipo,
       linha_digitavel, banco, valor, vencimento, valor_impresso, nosso_numero,
       origem_leitura, divergencias, enviado_por, enviado_em
  FROM api_prontocardio.contas_pagar_boletos
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY vencimento, id
""")

SQL_BOLETOS_DAS_NOTAS = text("""
SELECT prestador_cnpj, numero_nfse, count(*) AS quantidade, sum(valor) AS soma,
       min(vencimento) AS primeiro_vencimento,
       bool_or(divergencias <> '[]') AS com_divergencia
  FROM api_prontocardio.contas_pagar_boletos
 WHERE prestador_cnpj = ANY(:cnpjs)
 GROUP BY prestador_cnpj, numero_nfse
""")

SQL_INSERIR = text("""
INSERT INTO api_prontocardio.contas_pagar_boletos
    (prestador_cnpj, numero_nfse, cd_multi_empresa, nome_arquivo, arquivo, sha256,
     tipo, linha_digitavel, codigo_barras, banco, valor, vencimento, valor_impresso,
     nosso_numero, cnpjs_texto, origem_leitura, divergencias, enviado_por)
VALUES (:cnpj, :numero, :empresa, :nome_arquivo, :arquivo, :sha256, :tipo,
        :linha_digitavel, :codigo_barras, :banco, :valor, :vencimento, :valor_impresso,
        :nosso_numero, :cnpjs_texto, :origem_leitura, :divergencias, :enviado_por)
RETURNING id
""").bindparams(bindparam('arquivo'))


def _hoje() -> date:
    return datetime.now(FUSO_HOSPITAL).date()


def _nota_ou_404(session, oracle, usuario, cnpj: str, numero: str) -> dict:
    """Carrega a NFS-e e confere se o usuário pode atuar nela (setor da oficina ou financeiro)."""
    nota = session.execute(SQL_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().first()
    if nota is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'NFS-e recebida não encontrada.')
    empresa = _empresas_mv(oracle).get(somente_digitos(nota['tomador_cnpj']))
    cd_empresa = int(empresa['cd_multi_empresa']) if empresa else None
    regra = _regras(session).get((cnpj, cd_empresa)) if cd_empresa is not None else None
    cd_oficina = regra['cd_oficina'] if regra and regra['status'] == STATUS_DEFINIDA else None
    if not _gestor(usuario):
        permitidas = {
            int(cd) for cd in session.execute(
                SQL_OFICINAS_DO_USUARIO, {'usuario_id': usuario.id}
            ).scalars()
        }
        if cd_oficina is None or cd_oficina not in permitidas:
            raise HTTPException(HTTPStatus.FORBIDDEN, 'Esta NFS-e não é de uma oficina do seu setor.')
    return {**dict(nota), 'cd_multi_empresa': cd_empresa, 'cd_oficina': cd_oficina}


def divergencias_do_boleto(lido, nota: dict, soma_outros: Decimal, hoje: date) -> list[str]:
    """Conferências entre o boleto e a NFS-e. Lista vazia = boleto ok."""
    problemas = []
    cnpj = somente_digitos(nota['prestador_cnpj'])
    if lido.cnpjs_no_texto and cnpj not in lido.cnpjs_no_texto:
        problemas.append('O CNPJ do prestador da NFS-e não aparece no boleto.')
    if lido.valor_impresso is not None and abs(lido.valor_impresso - lido.valor) > TOLERANCIA:
        problemas.append('O valor da linha digitável difere do "Valor do Documento" impresso.')
    limite = _decimal(nota['valor_liquido_nfse']) or _decimal(nota['valor_servicos'])
    if limite is not None and soma_outros + lido.valor > limite + TOLERANCIA:
        problemas.append(
            f'A soma dos boletos (R$ {soma_outros + lido.valor:.2f}) passa do valor líquido da NFS-e (R$ {limite:.2f}).'
        )
    if lido.vencimento < hoje:
        problemas.append('Boleto vencido.')
    return problemas


def _serializar(linha) -> dict:
    item = dict(linha)
    item['divergencias'] = json.loads(item.get('divergencias') or '[]')
    return item


@router.get('/recebidas/{cnpj}/{numero}/boletos')
def listar_boletos(cnpj: str, numero: str, usuario: AcessoFila,
                   session: SessionPostgres, oracle: SessionOracle):
    cnpj = somente_digitos(cnpj)
    nota = _nota_ou_404(session, oracle, usuario, cnpj, numero)
    boletos = [_serializar(b) for b in session.execute(
        SQL_BOLETOS_DA_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().all()]
    return {'nota': nota, 'boletos': boletos, 'resumo': resumo_boletos(boletos, nota)}


def resumo_boletos(boletos: list[dict], nota: dict) -> dict:
    soma = sum((Decimal(b['valor']) for b in boletos), Decimal('0'))
    divergente = any(b['divergencias'] for b in boletos)
    return {
        'quantidade': len(boletos),
        'soma': soma,
        'valor_liquido': _decimal(nota.get('valor_liquido_nfse')) or _decimal(nota.get('valor_servicos')),
        'com_divergencia': divergente,
        'pronta': bool(boletos) and not divergente,
    }


@router.post('/recebidas/{cnpj}/{numero}/boletos', status_code=HTTPStatus.CREATED)
async def enviar_boleto(
    cnpj: str,
    numero: str,
    usuario: AcessoFila,
    session: SessionPostgres,
    oracle: SessionOracle,
    arquivo: Annotated[UploadFile, File()],
    linha_digitavel: Annotated[str | None, Form(max_length=60)] = None,
    vencimento: Annotated[date | None, Form()] = None,
):
    """Anexa o PDF do boleto. Se o PDF for imagem (escaneado), informe a linha digitável."""
    cnpj = somente_digitos(cnpj)
    nota = _nota_ou_404(session, oracle, usuario, cnpj, numero)
    status = status_da_nota(session, cnpj, numero)
    if nfse_cancelada(status):
        raise HTTPException(
            HTTPStatus.CONFLICT,
            f'A NFS-e {numero} está {status} no ISS Fortaleza. Peça ao fornecedor a nota correta.',
        )
    conteudo = await arquivo.read(TAMANHO_MAXIMO_PDF + 1)
    if len(conteudo) > TAMANHO_MAXIMO_PDF:
        raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'O PDF deve ter até 5 MB.')
    if not conteudo.startswith(b'%PDF'):
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'Envie o boleto em PDF.')

    hoje = _hoje()
    try:
        if linha_digitavel:
            lido = ler_linha_digitavel(linha_digitavel, hoje)
            origem = 'manual'
        else:
            lido = ler_boleto_pdf(conteudo, hoje)
            origem = 'pdf'
    except BoletoInvalido as exc:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
    if lido.vencimento is None:
        if vencimento is None:
            raise HTTPException(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                'Este boleto não traz o vencimento na linha digitável. Informe o vencimento.',
            )
        lido.vencimento = vencimento
    if lido.valor is None:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'O boleto não informa valor.')

    existentes = session.execute(SQL_BOLETOS_DA_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().all()
    soma_outros = sum((Decimal(b['valor']) for b in existentes), Decimal('0'))
    problemas = divergencias_do_boleto(lido, nota, soma_outros, hoje)

    try:
        novo_id = session.execute(SQL_INSERIR, {
            'cnpj': cnpj,
            'numero': numero,
            'empresa': nota['cd_multi_empresa'],
            'nome_arquivo': (arquivo.filename or 'boleto.pdf')[:255],
            'arquivo': conteudo,
            'sha256': sha256(conteudo).hexdigest(),
            'tipo': lido.tipo,
            'linha_digitavel': lido.linha_digitavel,
            'codigo_barras': lido.codigo_barras,
            'banco': lido.banco,
            'valor': lido.valor,
            'vencimento': lido.vencimento,
            'valor_impresso': lido.valor_impresso,
            'nosso_numero': (lido.nosso_numero or '')[:40] or None,
            'cnpjs_texto': ','.join(lido.cnpjs_no_texto) or None,
            'origem_leitura': origem,
            'divergencias': json.dumps(problemas, ensure_ascii=False),
            'enviado_por': usuario.id,
        }).scalar_one()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(HTTPStatus.CONFLICT, 'Este boleto já foi anexado a esta NFS-e.') from exc

    return {'id': novo_id, **lido.as_dict(), 'origem_leitura': origem, 'divergencias': problemas}


@router.get('/boletos/{boleto_id}/arquivo')
def baixar_boleto(boleto_id: int, usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle):
    linha = session.execute(
        text('SELECT prestador_cnpj, numero_nfse, nome_arquivo, arquivo FROM api_prontocardio.contas_pagar_boletos WHERE id = :id'),
        {'id': boleto_id},
    ).mappings().first()
    if linha is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Boleto não encontrado.')
    _nota_ou_404(session, oracle, usuario, linha['prestador_cnpj'], linha['numero_nfse'])
    nome = linha['nome_arquivo'].replace('"', '')
    return Response(
        bytes(linha['arquivo']),
        media_type='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{nome}"', 'Cache-Control': 'private, no-store'},
    )


@router.delete('/boletos/{boleto_id}', status_code=HTTPStatus.NO_CONTENT)
def excluir_boleto(boleto_id: int, usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle):
    linha = session.execute(
        text('SELECT prestador_cnpj, numero_nfse, enviado_por FROM api_prontocardio.contas_pagar_boletos WHERE id = :id'),
        {'id': boleto_id},
    ).mappings().first()
    if linha is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Boleto não encontrado.')
    _nota_ou_404(session, oracle, usuario, linha['prestador_cnpj'], linha['numero_nfse'])
    if not _gestor(usuario) and linha['enviado_por'] != usuario.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, 'Só quem anexou o boleto (ou o financeiro) pode excluí-lo.')
    session.execute(text('DELETE FROM api_prontocardio.contas_pagar_boletos WHERE id = :id'), {'id': boleto_id})
    session.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


def boletos_por_nota(session, cnpjs: list[str]) -> dict[tuple[str, str], dict]:
    """Resumo de boletos de várias notas, para a fila (uma consulta)."""
    if not cnpjs:
        return {}
    linhas = session.execute(SQL_BOLETOS_DAS_NOTAS, {'cnpjs': sorted(set(cnpjs))}).mappings().all()
    return {(linha['prestador_cnpj'], linha['numero_nfse']): dict(linha) for linha in linhas}
