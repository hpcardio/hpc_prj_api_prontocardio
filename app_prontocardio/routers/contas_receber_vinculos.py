# ruff: noqa: E501, PLR0913
"""Contas a Receber — entrega 2: vínculos manuais com auditoria.

Por remessa do MV (a remessa é a unidade da tela Fluxo do recebimento):

- protocolo: vincular/trocar (número e data de entrada no convênio),
  confirmar o protocolo sugerido pelo portal, remover o vínculo;
- solicitação de nota pelo convênio: data e observação (no protocolo);
- NFS: pesquisar as NFS emitidas pelo hospital/PA (``nfse_xml``) e
  vincular ao protocolo; remover. Um protocolo tem uma NFS; uma NFS pode
  cobrir vários protocolos (NF de grupo).

Cada alteração grava um evento em ``cr_eventos`` com o antes e o depois.
Quando a remessa só tem o protocolo sugerido pelo portal (não gravado),
vincular solicitação/NFS grava o protocolo junto (``protocolo_numero``).
Ao trocar ou remover o protocolo de uma remessa, o protocolo antigo não é
apagado (pode ter outras remessas, NF e solicitação).
"""

import json
from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import bindparam, text

from app_prontocardio.routers.contas_pagar_nfse import (
    SessionOracle,
    SessionPostgres,
)
from app_prontocardio.routers.contas_receber import (
    CD_CONVENIO_ISSEC,
    FUSO_HOSPITAL,
    AcessoFluxo,
    remessas_mv,
)
from app_prontocardio.services.contas_receber_fluxo import dinheiro

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-receber',
    tags=['contas-a-receber'],
)

# NFS emitidas pelo hospital e pelo pronto atendimento.
PRESTADORES_HOSPITAL = ('08711085000128', '05613278000158')
LIMITE_BUSCA_NFSE = 20
TAMANHO_CNPJ = 14

SQL_VINCULO_DA_REMESSA = text("""
SELECT pr.cd_remessa, pr.origem AS origem_vinculo, p.id, p.cd_convenio, p.numero,
       p.origem, p.data_protocolo, p.solicitacao_nota_em, p.solicitacao_nota_obs
  FROM api_prontocardio.cr_protocolo_remessas pr
  JOIN api_prontocardio.cr_protocolos p ON p.id = pr.protocolo_id
 WHERE pr.cd_remessa = :cd_remessa
""")

SQL_GARANTIR_PROTOCOLO = text("""
INSERT INTO api_prontocardio.cr_protocolos
       (cd_convenio, numero, origem, data_protocolo, criado_por)
VALUES (:cd_convenio, :numero, :origem, :data_protocolo, :usuario_id)
ON CONFLICT (cd_convenio, numero) DO UPDATE SET
       data_protocolo = COALESCE(EXCLUDED.data_protocolo, api_prontocardio.cr_protocolos.data_protocolo),
       atualizado_por = :usuario_id,
       atualizado_em = now()
RETURNING id
""")

SQL_VINCULAR_REMESSA = text("""
INSERT INTO api_prontocardio.cr_protocolo_remessas (cd_remessa, protocolo_id, origem, criado_por)
VALUES (:cd_remessa, :protocolo_id, :origem, :usuario_id)
ON CONFLICT (cd_remessa) DO UPDATE SET
       protocolo_id = EXCLUDED.protocolo_id,
       origem = EXCLUDED.origem,
       criado_por = EXCLUDED.criado_por,
       criado_em = now()
""")

SQL_DESVINCULAR_REMESSA = text(
    'DELETE FROM api_prontocardio.cr_protocolo_remessas WHERE cd_remessa = :cd_remessa'
)

SQL_SOLICITACAO = text("""
UPDATE api_prontocardio.cr_protocolos
   SET solicitacao_nota_em = :data, solicitacao_nota_obs = :observacao,
       solicitacao_nota_por = :usuario_id, atualizado_por = :usuario_id,
       atualizado_em = now()
 WHERE id = :protocolo_id
""")

SQL_NFSE_DO_PROTOCOLO = text("""
SELECT protocolo_id, prestador_cnpj, numero_nfse, data_emissao, valor, origem
  FROM api_prontocardio.cr_protocolo_nfse WHERE protocolo_id = :protocolo_id
""")

SQL_SALVAR_NFSE = text("""
INSERT INTO api_prontocardio.cr_protocolo_nfse
       (protocolo_id, prestador_cnpj, numero_nfse, data_emissao, valor, row_hash, origem, criado_por)
VALUES (:protocolo_id, :prestador_cnpj, :numero_nfse, :data_emissao, :valor, :row_hash, 'manual', :usuario_id)
ON CONFLICT (protocolo_id) DO UPDATE SET
       prestador_cnpj = EXCLUDED.prestador_cnpj, numero_nfse = EXCLUDED.numero_nfse,
       data_emissao = EXCLUDED.data_emissao, valor = EXCLUDED.valor,
       row_hash = EXCLUDED.row_hash, origem = 'manual',
       atualizado_por = :usuario_id, atualizado_em = now()
""")

SQL_REMOVER_NFSE = text(
    'DELETE FROM api_prontocardio.cr_protocolo_nfse WHERE protocolo_id = :protocolo_id'
)

SQL_EVENTO = text("""
INSERT INTO api_prontocardio.cr_eventos
       (entidade, chave, acao, dados_anteriores, dados_novos, usuario_id, usuario_nome)
VALUES (:entidade, :chave, :acao, CAST(:antes AS jsonb), CAST(:depois AS jsonb),
        :usuario_id, :usuario_nome)
""")

_COLUNAS_NFSE = """
       numero_nfse, prestador_cnpj, prestador_razao_social, tomador_cnpj,
       tomador_razao_social, valor_servicos, NULLIF(valor_liquido_nfse, '') AS valor_liquido_nfse,
       data_hora, row_hash, LEFT(discriminacao, 300) AS discriminacao"""

SQL_NFSE = text(f"""
SELECT DISTINCT ON (prestador_cnpj, numero_nfse){_COLUNAS_NFSE}
  FROM api_prontocardio.nfse_xml
 WHERE prestador_cnpj = :prestador_cnpj AND numero_nfse = :numero_nfse
   AND NULLIF(cancelamento_codigo, '') IS NULL
 ORDER BY prestador_cnpj, numero_nfse, data_hora DESC
""")

SQL_BUSCAR_NFSE = text(f"""
SELECT * FROM (
  SELECT DISTINCT ON (prestador_cnpj, numero_nfse){_COLUNAS_NFSE}
    FROM api_prontocardio.nfse_xml
   WHERE prestador_cnpj IN :prestadores
     AND NULLIF(cancelamento_codigo, '') IS NULL
     AND (numero_nfse = :numero
          OR (CAST(:valor AS numeric) IS NOT NULL
              AND NULLIF(valor_servicos, '')::numeric = CAST(:valor AS numeric))
          OR tomador_razao_social ILIKE :termo
          OR regexp_replace(tomador_cnpj, '[^0-9]', '', 'g') = :digitos)
   ORDER BY prestador_cnpj, numero_nfse, data_hora DESC
) n
ORDER BY data_hora DESC
LIMIT :limite
""").bindparams(bindparam('prestadores', expanding=True))

SQL_PROTOCOLOS_DAS_NFSE = text("""
SELECT n.prestador_cnpj, n.numero_nfse, p.numero AS protocolo, p.cd_convenio
  FROM api_prontocardio.cr_protocolo_nfse n
  JOIN api_prontocardio.cr_protocolos p ON p.id = n.protocolo_id
 WHERE n.numero_nfse = ANY(:numeros)
""")


# ---------------------------------------------------------------- entradas

class ProtocoloInput(BaseModel):
    numero: str = Field(min_length=1, max_length=60)
    data_protocolo: date | None = None
    # true quando o usuário confirma o protocolo sugerido pelo portal.
    confirmado_do_portal: bool = False

    @field_validator('numero')
    @classmethod
    def _numero(cls, valor: str) -> str:
        valor = ' '.join(valor.split())
        if not valor:
            raise ValueError('Informe o número do protocolo.')
        return valor


class SolicitacaoInput(BaseModel):
    data: date
    observacao: str | None = Field(default=None, max_length=400)
    protocolo_numero: str | None = Field(default=None, max_length=60)


class NfseInput(BaseModel):
    prestador_cnpj: str = Field(pattern=r'^\d{14}$')
    numero_nfse: str = Field(min_length=1, max_length=20)
    protocolo_numero: str | None = Field(default=None, max_length=60)


# ---------------------------------------------------------------- apoio

def _remessa(oracle, cd_remessa: int) -> dict:
    remessa = next((r for r in remessas_mv(oracle) if r['cd_remessa'] == cd_remessa), None)
    if remessa is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f'Remessa {cd_remessa} não encontrada entre as remessas fechadas do MV.')
    return remessa


def _vinculo(postgres, cd_remessa: int) -> dict | None:
    linha = postgres.execute(SQL_VINCULO_DA_REMESSA, {'cd_remessa': cd_remessa}).mappings().first()
    return dict(linha) if linha else None


def _evento(postgres, usuario, entidade: str, chave, acao: str, antes, depois) -> None:
    postgres.execute(SQL_EVENTO, {
        'entidade': entidade,
        'chave': str(chave),
        'acao': acao,
        'antes': json.dumps(antes, default=str) if antes is not None else None,
        'depois': json.dumps(depois, default=str) if depois is not None else None,
        'usuario_id': usuario.id,
        'usuario_nome': usuario.nome,
    })


def _origem_protocolo(remessa: dict, confirmado_do_portal: bool) -> str:
    if confirmado_do_portal and remessa['cd_convenio'] == CD_CONVENIO_ISSEC:
        return 'portal_issec'
    return 'manual'


def _gravar_protocolo(postgres, usuario, remessa, numero: str, data_protocolo, origem: str) -> int:
    """Cria (ou reaproveita) o protocolo do convênio e liga a remessa a ele."""
    protocolo_id = postgres.execute(SQL_GARANTIR_PROTOCOLO, {
        'cd_convenio': remessa['cd_convenio'], 'numero': numero, 'origem': origem,
        'data_protocolo': data_protocolo, 'usuario_id': usuario.id,
    }).scalar_one()
    postgres.execute(SQL_VINCULAR_REMESSA, {
        'cd_remessa': remessa['cd_remessa'], 'protocolo_id': protocolo_id,
        'origem': origem, 'usuario_id': usuario.id,
    })
    return int(protocolo_id)


def _protocolo_para_editar(postgres, oracle, usuario, cd_remessa: int, protocolo_numero: str | None) -> tuple[int, dict | None]:
    """Protocolo gravado da remessa; se só houver o sugerido pelo portal, grava-o."""
    vinculo = _vinculo(postgres, cd_remessa)
    if vinculo:
        return int(vinculo['id']), vinculo
    numero = ' '.join((protocolo_numero or '').split())
    if not numero:
        raise HTTPException(HTTPStatus.CONFLICT, 'Vincule o protocolo da remessa antes.')
    remessa = _remessa(oracle, cd_remessa)
    origem = _origem_protocolo(remessa, True)
    protocolo_id = _gravar_protocolo(postgres, usuario, remessa, numero, None, origem)
    _evento(postgres, usuario, 'protocolo_remessa', cd_remessa, 'criar', None,
            {'protocolo_id': protocolo_id, 'numero': numero, 'origem': origem})
    return protocolo_id, None


def _data_emissao(valor):
    if isinstance(valor, datetime):
        return valor.astimezone(FUSO_HOSPITAL).date()
    return valor


def _nfse_publica(linha, ligados=None) -> dict:
    return {
        'prestador_cnpj': linha['prestador_cnpj'],
        'prestador': linha['prestador_razao_social'],
        'numero_nfse': linha['numero_nfse'],
        'tomador_cnpj': linha['tomador_cnpj'],
        'tomador': linha['tomador_razao_social'],
        'data_emissao': _data_emissao(linha['data_hora']),
        'valor': dinheiro(linha['valor_servicos']) if linha['valor_servicos'] not in (None, '') else None,
        'valor_liquido': dinheiro(linha['valor_liquido_nfse']) if linha['valor_liquido_nfse'] is not None else None,
        'discriminacao': linha['discriminacao'],
        'protocolos_vinculados': ligados or [],
    }


# ---------------------------------------------------------------- protocolo

@router.put('/remessas/{cd_remessa}/protocolo')
def vincular_protocolo(cd_remessa: int, dados: ProtocoloInput, oracle: SessionOracle,
                       postgres: SessionPostgres, usuario: AcessoFluxo):
    """Vincula (ou troca) o protocolo da remessa no convênio."""
    remessa = _remessa(oracle, cd_remessa)
    antes = _vinculo(postgres, cd_remessa)
    origem = _origem_protocolo(remessa, dados.confirmado_do_portal)
    protocolo_id = _gravar_protocolo(postgres, usuario, remessa, dados.numero, dados.data_protocolo, origem)
    depois = {'protocolo_id': protocolo_id, 'numero': dados.numero,
              'data_protocolo': dados.data_protocolo, 'origem': origem}
    _evento(postgres, usuario, 'protocolo_remessa', cd_remessa,
            'criar' if antes is None else 'editar', antes, depois)
    postgres.commit()
    return {'cd_remessa': cd_remessa, **depois}


@router.delete('/remessas/{cd_remessa}/protocolo')
def remover_protocolo(cd_remessa: int, postgres: SessionPostgres, usuario: AcessoFluxo):
    """Tira a remessa do protocolo (o protocolo, a NF e a solicitação ficam gravados)."""
    antes = _vinculo(postgres, cd_remessa)
    if antes is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'A remessa não tem protocolo gravado.')
    postgres.execute(SQL_DESVINCULAR_REMESSA, {'cd_remessa': cd_remessa})
    _evento(postgres, usuario, 'protocolo_remessa', cd_remessa, 'remover', antes, None)
    postgres.commit()
    return {'cd_remessa': cd_remessa, 'removido': True}


# ---------------------------------------------------------------- solicitação de nota

@router.put('/remessas/{cd_remessa}/solicitacao-nota')
def registrar_solicitacao(cd_remessa: int, dados: SolicitacaoInput, oracle: SessionOracle,
                          postgres: SessionPostgres, usuario: AcessoFluxo):
    """Registra a solicitação de nota pelo convênio (no protocolo da remessa)."""
    protocolo_id, vinculo = _protocolo_para_editar(postgres, oracle, usuario, cd_remessa, dados.protocolo_numero)
    observacao = ' '.join((dados.observacao or '').split()) or None
    antes = ({'data': vinculo['solicitacao_nota_em'], 'observacao': vinculo['solicitacao_nota_obs']}
             if vinculo and vinculo['solicitacao_nota_em'] else None)
    postgres.execute(SQL_SOLICITACAO, {'protocolo_id': protocolo_id, 'data': dados.data,
                                       'observacao': observacao, 'usuario_id': usuario.id})
    depois = {'data': dados.data, 'observacao': observacao}
    _evento(postgres, usuario, 'solicitacao_nota', protocolo_id, 'criar' if antes is None else 'editar', antes, depois)
    postgres.commit()
    return {'protocolo_id': protocolo_id, **depois}


@router.delete('/remessas/{cd_remessa}/solicitacao-nota')
def remover_solicitacao(cd_remessa: int, postgres: SessionPostgres, usuario: AcessoFluxo):
    vinculo = _vinculo(postgres, cd_remessa)
    if vinculo is None or not vinculo['solicitacao_nota_em']:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'A remessa não tem solicitação de nota registrada.')
    antes = {'data': vinculo['solicitacao_nota_em'], 'observacao': vinculo['solicitacao_nota_obs']}
    postgres.execute(SQL_SOLICITACAO, {'protocolo_id': vinculo['id'], 'data': None,
                                       'observacao': None, 'usuario_id': usuario.id})
    _evento(postgres, usuario, 'solicitacao_nota', vinculo['id'], 'remover', antes, None)
    postgres.commit()
    return {'protocolo_id': vinculo['id'], 'removido': True}


# ---------------------------------------------------------------- NFS

@router.get('/nfse')
def buscar_nfse(postgres: SessionPostgres, _usuario: AcessoFluxo,
                q: Annotated[str, Query(min_length=2, max_length=60)]):
    """NFS emitidas pelo hospital/PA: número, valor (1234,56), tomador ou CNPJ do tomador."""
    termo = q.strip()
    digitos = ''.join(ch for ch in termo if ch.isdigit())
    valor = None
    if any(ch in termo for ch in ',.') and digitos:
        try:
            valor = dinheiro(termo.replace('.', '').replace(',', '.'))
        except Exception:  # noqa: BLE001 — texto que não é valor
            valor = None
    linhas = postgres.execute(SQL_BUSCAR_NFSE, {
        'prestadores': list(PRESTADORES_HOSPITAL), 'numero': digitos.lstrip('0') or termo,
        'valor': valor, 'termo': f'%{termo}%', 'digitos': digitos if len(digitos) == TAMANHO_CNPJ else '-',
        'limite': LIMITE_BUSCA_NFSE,
    }).mappings().all()
    numeros = sorted({linha['numero_nfse'] for linha in linhas})
    ligados: dict[tuple, list] = {}
    if numeros:
        for linha in postgres.execute(SQL_PROTOCOLOS_DAS_NFSE, {'numeros': numeros}).mappings().all():
            ligados.setdefault((linha['prestador_cnpj'], linha['numero_nfse']), []).append(linha['protocolo'])
    return {'notas': [_nfse_publica(linha, ligados.get((linha['prestador_cnpj'], linha['numero_nfse'])))
                      for linha in linhas]}


@router.put('/remessas/{cd_remessa}/nfse')
def vincular_nfse(cd_remessa: int, dados: NfseInput, oracle: SessionOracle,
                  postgres: SessionPostgres, usuario: AcessoFluxo):
    """Vincula a NFS (emitida pelo hospital/PA, não cancelada) ao protocolo da remessa."""
    nota = postgres.execute(SQL_NFSE, {'prestador_cnpj': dados.prestador_cnpj,
                                       'numero_nfse': dados.numero_nfse.strip()}).mappings().first()
    if nota is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f'NFS {dados.numero_nfse} não encontrada (ou cancelada) na base do ISS.')
    protocolo_id, _ = _protocolo_para_editar(postgres, oracle, usuario, cd_remessa, dados.protocolo_numero)
    antes_linha = postgres.execute(SQL_NFSE_DO_PROTOCOLO, {'protocolo_id': protocolo_id}).mappings().first()
    antes = dict(antes_linha) if antes_linha else None
    depois = {
        'prestador_cnpj': nota['prestador_cnpj'], 'numero_nfse': nota['numero_nfse'],
        'data_emissao': _data_emissao(nota['data_hora']),
        'valor': dinheiro(nota['valor_servicos']) if nota['valor_servicos'] not in (None, '') else None,
    }
    postgres.execute(SQL_SALVAR_NFSE, {'protocolo_id': protocolo_id, **depois,
                                       'row_hash': nota['row_hash'], 'usuario_id': usuario.id})
    _evento(postgres, usuario, 'protocolo_nfse', protocolo_id, 'criar' if antes is None else 'editar', antes, depois)
    postgres.commit()
    return {'protocolo_id': protocolo_id, **depois}


@router.delete('/remessas/{cd_remessa}/nfse')
def remover_nfse(cd_remessa: int, postgres: SessionPostgres, usuario: AcessoFluxo):
    vinculo = _vinculo(postgres, cd_remessa)
    antes_linha = (postgres.execute(SQL_NFSE_DO_PROTOCOLO, {'protocolo_id': vinculo['id']}).mappings().first()
                   if vinculo else None)
    if antes_linha is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'A remessa não tem NFS vinculada.')
    postgres.execute(SQL_REMOVER_NFSE, {'protocolo_id': vinculo['id']})
    _evento(postgres, usuario, 'protocolo_nfse', vinculo['id'], 'remover', dict(antes_linha), None)
    postgres.commit()
    return {'protocolo_id': vinculo['id'], 'removido': True}
