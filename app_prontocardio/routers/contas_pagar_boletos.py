# ruff: noqa: E501, PLR0913
"""Contas a Pagar: boletos (duplicatas) das NFS-e recebidas.

O setor anexa o PDF do boleto à NFS-e; a linha digitável é lida e validada
(FEBRABAN) e vira a duplicata da nota. Divergências com a NFS-e ficam
registradas e impedem que a nota siga como "pronta" para o financeiro.
"""

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from app_prontocardio.routers.contas_pagar_nfse import (
    FUSO_HOSPITAL,
    SQL_TABELA_SITUACAO,
    AcessoFila,
    SessionOracle,
    SessionPostgres,
    _chave_documento,
    _decimal,
    _empresas_mv,
    _gestor,
    _lancadas_mv,
    _regras,
    acesso_do_usuario,
    nfse_cancelada,
    status_nfse,
)
from app_prontocardio.routers.mv_identidade import Config, _vinculo
from app_prontocardio.services import mv_identidade as mv
from app_prontocardio.services.contas_pagar_importacao import (
    ImportacaoFalhou,
    executar_importacao,
    simular_importacao,
)
from app_prontocardio.services.dias_uteis import prazo_minimo
from app_prontocardio.services.nfse_manuais import (
    CAMPOS_NFSE,
    FONTE_NFSE_RECEBIDAS,
)
from app_prontocardio.services.nfse_pdf_fortaleza import (
    PdfIndisponivel,
    baixar_pdf_oficial,
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

logger = logging.getLogger(__name__)

TAMANHO_MAXIMO_PDF = 5 * 1024 * 1024
TOLERANCIA = Decimal('0.01')

SQL_NOTA = text(f"""
SELECT prestador_cnpj, prestador_razao_social, numero_nfse, valor_servicos,
       valor_liquido_nfse, tomador_cnpj
  FROM {FONTE_NFSE_RECEBIDAS} n
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY data_hora DESC NULLS LAST
 LIMIT 1
""")

SQL_SITUACAO_XML = text(f"""
SELECT CASE WHEN coalesce(cancelamento_data_hora, '') <> ''
              OR coalesce(motivo_cancelamento_nfse, '') <> ''
            THEN 'CANCELADA' END
  FROM {FONTE_NFSE_RECEBIDAS} n
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
       origem_leitura, divergencias, enviado_por, enviado_em,
       prazo_minimo, fora_do_prazo, justificativa_atraso,
       (SELECT u.nome FROM api_prontocardio.usuarios_api u WHERE u.id = enviado_por) AS enviado_por_nome
  FROM api_prontocardio.contas_pagar_boletos
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY vencimento, id
""")

SQL_BOLETOS_DAS_NOTAS = text("""
SELECT prestador_cnpj, numero_nfse, count(*) AS quantidade, sum(valor) AS soma,
       min(vencimento) AS primeiro_vencimento,
       bool_or(divergencias <> '[]') AS com_divergencia,
       bool_or(coalesce(fora_do_prazo, false)) AS fora_do_prazo
  FROM api_prontocardio.contas_pagar_boletos
 WHERE prestador_cnpj = ANY(:cnpjs)
 GROUP BY prestador_cnpj, numero_nfse
""")

SQL_INSERIR = text("""
INSERT INTO api_prontocardio.contas_pagar_boletos
    (prestador_cnpj, numero_nfse, cd_multi_empresa, nome_arquivo, arquivo, sha256,
     tipo, linha_digitavel, codigo_barras, banco, valor, vencimento, valor_impresso,
     nosso_numero, cnpjs_texto, origem_leitura, divergencias, enviado_por,
     prazo_minimo, fora_do_prazo, justificativa_atraso)
VALUES (:cnpj, :numero, :empresa, :nome_arquivo, :arquivo, :sha256, :tipo,
        :linha_digitavel, :codigo_barras, :banco, :valor, :vencimento, :valor_impresso,
        :nosso_numero, :cnpjs_texto, :origem_leitura, :divergencias, :enviado_por,
        :prazo_minimo, :fora_do_prazo, :justificativa_atraso)
RETURNING id
""").bindparams(bindparam('arquivo'))


JUSTIFICATIVA_MINIMA = 10
JUSTIFICATIVA_MAXIMA = 400


def _hoje() -> date:
    return datetime.now(FUSO_HOSPITAL).date()


def _agora_local() -> datetime:
    return datetime.now(FUSO_HOSPITAL).replace(tzinfo=None, microsecond=0)


def conferir_prazo(vencimento: date, envio: datetime, justificativa: str | None) -> tuple[datetime, bool, str | None]:
    """Regra das 72h em dias úteis. Fora do prazo, exige a justificativa do atraso."""
    prazo = prazo_minimo(envio)
    fora = vencimento < prazo.date()
    texto = ' '.join((justificativa or '').split()) or None
    if fora and (texto is None or len(texto) < JUSTIFICATIVA_MINIMA):
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f'O boleto vence em {vencimento:%d/%m/%Y}, antes do prazo mínimo de 3 dias úteis '
            f'({prazo:%d/%m/%Y %H:%M}). Informe a justificativa do atraso para enviar ao financeiro.',
        )
    return prazo, fora, texto if fora else None


def _nota_ou_404(session, oracle, usuario, cnpj: str, numero: str, historico_ok: bool = False) -> dict:
    """Carrega a NFS-e e confere se o usuário pode atuar nela (setor da oficina ou financeiro).

    ``historico_ok``: consultas (sem alteração) também valem para as notas do
    histórico do usuário (lançadas por ele no MV ou com boleto enviado por ele).
    """
    nota = session.execute(SQL_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().first()
    if nota is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'NFS-e recebida não encontrada.')
    empresa = _empresas_mv(oracle).get(somente_digitos(nota['tomador_cnpj']))
    cd_empresa = int(empresa['cd_multi_empresa']) if empresa else None
    regra = _regras(session).get((cnpj, cd_empresa)) if cd_empresa is not None else None
    cd_oficina = regra['cd_oficina'] if regra and regra['status'] == STATUS_DEFINIDA else None
    acesso = acesso_do_usuario(session, oracle, usuario)
    if acesso is not None and not acesso.ve_pendente(cd_oficina) and not (
        historico_ok
        and acesso.ve_historico(cnpj, numero, _lancadas_mv(oracle, [cnpj]).get((cnpj, _chave_documento(numero))))
    ):
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
    nota = _nota_ou_404(session, oracle, usuario, cnpj, numero, historico_ok=True)
    boletos = [_serializar(b) for b in session.execute(
        SQL_BOLETOS_DA_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().all()]
    return {'nota': nota, 'boletos': boletos, 'resumo': resumo_boletos(boletos, nota),
            'prazo_minimo_hoje': prazo_minimo(_agora_local())}


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
    justificativa_atraso: Annotated[str | None, Form(max_length=JUSTIFICATIVA_MAXIMA)] = None,
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

    prazo, fora_do_prazo, justificativa = conferir_prazo(lido.vencimento, _agora_local(), justificativa_atraso)
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
            'prazo_minimo': prazo.date(),
            'fora_do_prazo': fora_do_prazo,
            'justificativa_atraso': justificativa,
        }).scalar_one()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(HTTPStatus.CONFLICT, 'Este boleto já foi anexado a esta NFS-e.') from exc

    return {'id': novo_id, **lido.as_dict(), 'origem_leitura': origem, 'divergencias': problemas,
            'prazo_minimo': prazo.date(), 'fora_do_prazo': fora_do_prazo, 'justificativa_atraso': justificativa}



SQL_NFSE_COMPLETA = text(f"""
SELECT data_hora, {', '.join(CAMPOS_NFSE)}, xml_campos, xml_documento, origem_nfse, nfse_manual_id
  FROM {FONTE_NFSE_RECEBIDAS} n
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY data_hora DESC NULLS LAST
 LIMIT 1
""")


def _nfse_completa(session, oracle, usuario, cnpj: str, numero: str) -> dict:
    cnpj = somente_digitos(cnpj)
    _nota_ou_404(session, oracle, usuario, cnpj, numero, historico_ok=True)
    return dict(session.execute(SQL_NFSE_COMPLETA, {'cnpj': cnpj, 'numero': numero}).mappings().first())


@router.get('/recebidas/{cnpj}/{numero}/nfse')
def ver_nfse(cnpj: str, numero: str, usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle):
    """Dados da NFS-e como vieram no XML do ISS Fortaleza (para o espelho da nota)."""
    nota = _nfse_completa(session, oracle, usuario, cnpj, numero)
    try:
        extras = json.loads(nota.pop('xml_campos') or '{}')
    except ValueError:
        extras = {}
    nota.pop('xml_documento', None)
    data_hora = nota.pop('data_hora')
    return {
        **nota,
        'data_emissao': data_hora.astimezone(FUSO_HOSPITAL).replace(tzinfo=None) if data_hora else None,
        'chave_acesso': extras.get('nfse_infnfse_chaveacesso'),
        'rps_numero': extras.get('nfse_infnfse_identificacaorps_numero'),
        'rps_serie': extras.get('nfse_infnfse_identificacaorps_serie'),
        'optante_simples': extras.get('nfse_infnfse_optantesimplesnacional'),
        'status_nfse': status_da_nota(session, somente_digitos(cnpj), numero),
        'origem_nfse': nota.get('origem_nfse'),
    }


DDL_NFSE_PDF = """
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_nfse_pdf (
    prestador_cnpj varchar(14) NOT NULL,
    numero_nfse varchar(30) NOT NULL,
    arquivo bytea NOT NULL,
    sha256 char(64) NOT NULL,
    baixado_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (prestador_cnpj, numero_nfse)
)"""

SQL_GUARDAR_PDF = text("""
INSERT INTO api_prontocardio.contas_pagar_nfse_pdf (prestador_cnpj, numero_nfse, arquivo, sha256)
VALUES (:cnpj, :numero, :arquivo, :sha256)
ON CONFLICT (prestador_cnpj, numero_nfse) DO UPDATE
   SET arquivo = EXCLUDED.arquivo, sha256 = EXCLUDED.sha256, baixado_em = now()
""").bindparams(bindparam('arquivo'))

SQL_PDF_MANUAL = text('SELECT arquivo FROM api_prontocardio.contas_pagar_nfse_manuais WHERE id = :id')

SQL_PDF_GUARDADO = text("""
SELECT arquivo, baixado_em FROM api_prontocardio.contas_pagar_nfse_pdf
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
""")


def _resposta_pdf(conteudo: bytes, cnpj: str, numero: str, origem: str) -> Response:
    nome = f'nfse-{cnpj}-{re.sub(r"[^0-9A-Za-z]", "", numero)}.pdf'
    return Response(conteudo, media_type='application/pdf', headers={
        'Content-Disposition': f'inline; filename="{nome}"', 'Cache-Control': 'private, no-store',
        'X-NFSe-Origem': origem})


@router.get('/recebidas/{cnpj}/{numero}/nfse/pdf')
def baixar_pdf_nfse(cnpj: str, numero: str, usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle):
    """PDF oficial (DANFSe) da NFS-e, baixado do ISS Fortaleza; se o portal falhar, a última cópia guardada."""
    nota = _nfse_completa(session, oracle, usuario, cnpj, numero)
    cnpj = somente_digitos(cnpj)
    chave = {'cnpj': cnpj, 'numero': numero}
    if nota.get('origem_nfse') == 'pdf':
        # NFS-e de outro município: o PDF é o que o usuário enviou no cadastro.
        arquivo = session.execute(SQL_PDF_MANUAL, {'id': nota['nfse_manual_id']}).scalar_one()
        return _resposta_pdf(bytes(arquivo), cnpj, numero, 'cadastro')
    session.execute(text(DDL_NFSE_PDF))
    session.commit()
    try:
        conteudo = baixar_pdf_oficial(numero, nota.get('codigo_verificacao_nfse'), nota.get('prestador_inscricao_municipal'))
    except PdfIndisponivel as exc:
        guardado = session.execute(SQL_PDF_GUARDADO, chave).mappings().first()
        if guardado is None:
            raise HTTPException(HTTPStatus.BAD_GATEWAY, str(exc)) from exc
        logger.warning('PDF da NFS-e %s/%s servido da cópia de %s: %s', cnpj, numero, guardado['baixado_em'], exc)
        return _resposta_pdf(bytes(guardado['arquivo']), cnpj, numero, 'copia')
    session.execute(SQL_GUARDAR_PDF, {**chave, 'arquivo': conteudo, 'sha256': sha256(conteudo).hexdigest()})
    session.commit()
    return _resposta_pdf(conteudo, cnpj, numero, 'iss')


@router.get('/recebidas/{cnpj}/{numero}/nfse/xml')
def baixar_xml_nfse(cnpj: str, numero: str, usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle):
    nota = _nfse_completa(session, oracle, usuario, cnpj, numero)
    if not nota.get('xml_documento'):
        raise HTTPException(HTTPStatus.NOT_FOUND, 'O XML desta NFS-e não foi guardado.')
    nome = f'nfse-{somente_digitos(cnpj)}-{re.sub(r"[^0-9A-Za-z]", "", numero)}.xml'
    return Response(
        nota['xml_documento'].encode('utf-8'),
        media_type='application/xml',
        headers={'Content-Disposition': f'attachment; filename="{nome}"', 'Cache-Control': 'private, no-store'},
    )


@router.get('/boletos/{boleto_id}/arquivo')
def baixar_boleto(boleto_id: int, usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle):
    linha = session.execute(
        text('SELECT prestador_cnpj, numero_nfse, nome_arquivo, arquivo FROM api_prontocardio.contas_pagar_boletos WHERE id = :id'),
        {'id': boleto_id},
    ).mappings().first()
    if linha is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Boleto não encontrado.')
    _nota_ou_404(session, oracle, usuario, linha['prestador_cnpj'], linha['numero_nfse'], historico_ok=True)
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


# ------------------------------------------------------------ importação (MV)


SQL_NOTA_IMPORTACAO = text(f"""
SELECT data_hora, valor_servicos, valor_liquido_nfse, valor_iss_retido, iss_retido,
       valor_pis, valor_cofins, valor_inss, valor_ir, valor_csll, outras_retencoes
  FROM {FONTE_NFSE_RECEBIDAS} n
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY data_hora DESC NULLS LAST
 LIMIT 1
""")

DDL_IMPORTACOES = (
    """CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_importacoes (
        id bigserial PRIMARY KEY,
        prestador_cnpj varchar(14) NOT NULL,
        numero_nfse varchar(30) NOT NULL,
        cd_multi_empresa integer,
        status varchar(20) NOT NULL,
        usuario_id integer NOT NULL,
        cd_usuario_mv varchar(30),
        assinatura varchar(64),
        cd_ent_serv bigint,
        cd_itent_serv bigint,
        cd_con_pag bigint,
        plano jsonb,
        erro text,
        iniciado_em timestamptz NOT NULL DEFAULT now(),
        concluido_em timestamptz
    )""",
    # No máximo uma importação em andamento ou concluída por NFS-e.
    """CREATE UNIQUE INDEX IF NOT EXISTS contas_pagar_importacoes_ativa
        ON api_prontocardio.contas_pagar_importacoes (prestador_cnpj, numero_nfse)
     WHERE status IN ('em_andamento', 'importada')""",
    # Desfazer pelo Receita Certa (migration 20261118_071).
    """ALTER TABLE api_prontocardio.contas_pagar_importacoes
        ADD COLUMN IF NOT EXISTS desfeita_em timestamptz,
        ADD COLUMN IF NOT EXISTS desfeita_por integer,
        ADD COLUMN IF NOT EXISTS cd_usuario_mv_desfazer varchar(30),
        ADD COLUMN IF NOT EXISTS motivo_desfazer text,
        ADD COLUMN IF NOT EXISTS desfazer_detalhe jsonb""",
)

SQL_TABELA_IMPORTACOES = text("SELECT to_regclass('api_prontocardio.contas_pagar_importacoes') IS NOT NULL")

SQL_IMPORTACAO_DA_NOTA = text("""
SELECT id, status, cd_multi_empresa, usuario_id, cd_usuario_mv, cd_ent_serv, cd_con_pag, erro,
       iniciado_em, concluido_em, desfeita_em, desfeita_por, cd_usuario_mv_desfazer, motivo_desfazer
  FROM api_prontocardio.contas_pagar_importacoes
 WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
 ORDER BY id DESC LIMIT 1
""")

SQL_INICIAR_IMPORTACAO = text("""
INSERT INTO api_prontocardio.contas_pagar_importacoes
    (prestador_cnpj, numero_nfse, cd_multi_empresa, status, usuario_id, cd_usuario_mv, assinatura)
VALUES (:cnpj, :numero, :empresa, 'em_andamento', :usuario_id, :login, :assinatura)
RETURNING id
""")

SQL_CONCLUIR_IMPORTACAO = text("""
UPDATE api_prontocardio.contas_pagar_importacoes
   SET status = :status, cd_ent_serv = :ent_serv, cd_itent_serv = :itent_serv, cd_con_pag = :con_pag,
       plano = CAST(:plano AS jsonb), erro = :erro, concluido_em = now()
 WHERE id = :id
""")

SQL_DESCARTAR_IMPORTACAO = text('DELETE FROM api_prontocardio.contas_pagar_importacoes WHERE id = :id')

RE_ERRO_ORACLE = re.compile(r'(ORA-\d{5}): ?([^\n]*)')


class ImportacaoInput(BaseModel):
    assinatura: str = Field(min_length=8, max_length=64)


def mensagem_oracle(exc: Exception) -> str:
    """Primeiro erro ORA da exceção, sem o SQL (que pode conter dados)."""
    encontrado = RE_ERRO_ORACLE.search(str(exc))
    return f'{encontrado.group(1)}: {encontrado.group(2).strip()}'[:500] if encontrado else type(exc).__name__


def _preparar(cnpj: str, numero: str, usuario, session, oracle) -> tuple[dict, list[dict], dict]:
    if not _gestor(usuario):
        raise HTTPException(HTTPStatus.FORBIDDEN, 'Só o financeiro importa notas para o MV.')
    nota = _nota_ou_404(session, oracle, usuario, cnpj, numero)
    status = status_da_nota(session, cnpj, numero)
    if nfse_cancelada(status):
        raise HTTPException(HTTPStatus.CONFLICT, f'A NFS-e {numero} está {status} no ISS Fortaleza.')
    vinculo = _vinculo(session, usuario.id)
    if not vinculo['pode_gravar_no_mv']:
        raise HTTPException(HTTPStatus.PRECONDITION_FAILED, 'Entre com o seu login MV antes.')
    extra = session.execute(SQL_NOTA_IMPORTACAO, {'cnpj': cnpj, 'numero': numero}).mappings().first()
    data_hora = extra['data_hora']
    nota = {**nota, **dict(extra), 'status_nfse': status,
            'data_emissao': data_hora.astimezone(FUSO_HOSPITAL).date() if data_hora else None}
    boletos = [_serializar(b) for b in session.execute(
        SQL_BOLETOS_DA_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().all()]
    return nota, boletos, vinculo


def _sessao(vinculo: dict, usuario, config):
    return mv.sessao_mv(
        vinculo['cd_usuario_mv'],
        proxy_user=config.MV_PROXY_USER,
        proxy_password=config.MV_PROXY_PASSWORD,
        dsn=mv.dsn_do_oracle(config.ORACLE_DATABASE_URL),
        usuario_id=usuario.id,
    )


def _agora() -> datetime:
    return datetime.now(FUSO_HOSPITAL).replace(tzinfo=None, microsecond=0)


def _importacao_registrada(session, cnpj: str, numero: str) -> dict | None:
    if not session.execute(SQL_TABELA_IMPORTACOES).scalar_one():
        return None
    linha = session.execute(SQL_IMPORTACAO_DA_NOTA, {'cnpj': cnpj, 'numero': numero}).mappings().first()
    return dict(linha) if linha else None


def _resumo_nota(nota: dict) -> dict:
    return {k: nota.get(k) for k in ('prestador_cnpj', 'prestador_razao_social', 'numero_nfse', 'valor_servicos',
                                     'valor_liquido_nfse', 'data_emissao', 'cd_multi_empresa', 'cd_oficina')}


@router.post('/recebidas/{cnpj}/{numero}/importacao/simulacao')
def simular_importacao_mv(cnpj: str, numero: str, usuario: AcessoFila, session: SessionPostgres,
                          oracle: SessionOracle, config: Config):
    """Mostra o que a importação gravaria no MV e o que impede a importação. Não grava nada."""
    cnpj = somente_digitos(cnpj)
    nota, boletos, vinculo = _preparar(cnpj, numero, usuario, session, oracle)
    with _sessao(vinculo, usuario, config) as conexao:
        try:
            resultado = simular_importacao(conexao.cursor(), nota=nota, boletos=boletos, agora=_agora())
        finally:
            conexao.rollback()  # a simulação nunca grava
    return {'nota': _resumo_nota(nota), 'status_nfse': nota['status_nfse'], 'simulacao': True,
            'gravacao_habilitada': bool(config.CP_IMPORTACAO_MV_HABILITADA),
            'importacao_registrada': _importacao_registrada(session, cnpj, numero), **resultado}


@router.post('/recebidas/{cnpj}/{numero}/importacao')
def importar_mv(cnpj: str, numero: str, dados: ImportacaoInput, usuario: AcessoFila,
                session: SessionPostgres, oracle: SessionOracle, config: Config):
    """Grava a NFS-e no MV (entrada de serviço + título no Contas a Pagar) com o login MV do operador.

    Refaz a simulação na mesma transação; só grava se a nota continuar apta e
    o plano for o mesmo que o operador conferiu (assinatura). Qualquer erro
    desfaz tudo no MV.
    """
    if not config.CP_IMPORTACAO_MV_HABILITADA:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, 'A gravação no MV está desligada (CP_IMPORTACAO_MV_HABILITADA).')
    cnpj = somente_digitos(cnpj)
    nota, boletos, vinculo = _preparar(cnpj, numero, usuario, session, oracle)
    for ddl in DDL_IMPORTACOES:
        session.execute(text(ddl))
    try:
        registro = session.execute(SQL_INICIAR_IMPORTACAO, {
            'cnpj': cnpj, 'numero': numero, 'empresa': nota.get('cd_multi_empresa'), 'usuario_id': usuario.id,
            'login': vinculo['cd_usuario_mv'], 'assinatura': dados.assinatura,
        }).scalar_one()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(HTTPStatus.CONFLICT, 'Esta NFS-e já foi importada ou está sendo importada agora.') from exc

    resultado = None
    with _sessao(vinculo, usuario, config) as conexao:
        try:
            resultado = simular_importacao(conexao.cursor(), nota=nota, boletos=boletos, agora=_agora())
            if not resultado['apta']:
                raise HTTPException(HTTPStatus.CONFLICT, 'A nota deixou de estar apta: '
                                    + ' '.join(p['mensagem'] for p in resultado['pendencias']))
            if resultado['assinatura'] != dados.assinatura:
                raise HTTPException(HTTPStatus.CONFLICT, 'Os dados mudaram desde a simulação. Simule de novo e confira antes de importar.')
            ids = executar_importacao(conexao.cursor(), resultado)
            conexao.commit()
        except HTTPException:
            conexao.rollback()
            session.execute(SQL_DESCARTAR_IMPORTACAO, {'id': registro})
            session.commit()
            raise
        except Exception as exc:
            conexao.rollback()
            erro = str(exc) if isinstance(exc, ImportacaoFalhou) else mensagem_oracle(exc)
            logger.warning('Importação MV da NFS-e %s/%s desfeita: %s', cnpj, numero, erro)
            session.execute(SQL_CONCLUIR_IMPORTACAO, {
                'id': registro, 'status': 'erro', 'ent_serv': None, 'itent_serv': None, 'con_pag': None,
                'plano': json.dumps(jsonable_encoder(resultado)) if resultado else None, 'erro': erro})
            session.commit()
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, f'O MV recusou a importação; nada foi gravado. {erro}') from exc

    session.execute(SQL_CONCLUIR_IMPORTACAO, {
        'id': registro, 'status': 'importada', 'ent_serv': ids['ent_serv'], 'itent_serv': ids['itent_serv'],
        'con_pag': ids['cd_con_pag'], 'plano': json.dumps(jsonable_encoder(resultado)), 'erro': None})
    session.commit()
    return {'importada': True, 'nota': _resumo_nota(nota), 'cd_usuario_mv': vinculo['cd_usuario_mv'],
            'cd_ent_serv': ids['ent_serv'], 'cd_con_pag': ids['cd_con_pag']}
