"""Associação manual de itens glosados da Maida (ISSEC) a lançamentos do MV.

Itens do demonstrativo Maida sem correspondência automática entram no
Follow-Up com contexto provisório: remessa = lote Maida, conta = guia,
atendimento 0. Aqui o usuário informa o atendimento do MV e escolhe o
lançamento; a glosa passa a exibir remessa, conta e atendimento do MV.

Nada é inferido: o vínculo só é gravado para o lançamento escolhido, e
somente se for ISSEC e do mesmo procedimento do item glosado. O
lançamento sintético da Maida (``cd_lancamento``), a guia, o procedimento
e o processo são preservados, para que a materialização do Airflow
continue reconhecendo a glosa e não a duplique.
"""

from decimal import Decimal
from http import HTTPStatus

from fastapi import HTTPException
from sqlalchemy import bindparam, text

from app_prontocardio.models import AssociacaoItemIpmManual

NR_MAIDA = 'MAIDA'
CRITERIO_MAIDA = 'maida_associacao_manual_atendimento'
CD_CONVENIO_ISSEC = 46
MOTIVOS_PENDENTES = ('remessa_nao_encontrada_ou_ambigua', 'nao_encontrado')

_ITEM_SQL = """
    SELECT glosa.id_registro,
           BTRIM(demo.numero_lote) AS lote,
           BTRIM(demo.numero_guia_senha) AS guia,
           BTRIM(demo.codigo_beneficiario) AS carteira,
           BTRIM(demo.nome_beneficiario) AS beneficiario,
           BTRIM(demo.codigo_servico) AS codigo_servico,
           demo.descricao_servico,
           NULLIF(BTRIM(demo.codigo_glosa), '') AS codigo_glosa,
           demo.data_realizacao,
           demo.referencia,
           demo.quantidade_executada,
           demo.valor_processado,
           demo.valor_glosa,
           registro.id AS registro_glosa_id,
           registro.cd_remessa AS registro_cd_remessa,
           registro.conta AS registro_conta,
           registro.cd_atendimento AS registro_cd_atendimento,
           associacao.id AS associacao_id,
           associacao.cd_remessa AS associacao_cd_remessa,
           associacao.conta AS associacao_conta,
           associacao.cd_lancamento AS associacao_cd_lancamento,
           associacao.data_criacao AS associacao_data
      FROM api_prontocardio.glossas_nao_vinculadas_ipm AS glosa
      JOIN api_prontocardio.demonstrativo_conta_ipm AS demo
        ON demo.id_registro = glosa.id_registro
       AND demo.origem_maida IS NOT NULL
       AND COALESCE(demo.valor_glosa, 0) > 0
      JOIN LATERAL (
          SELECT registro.*
            FROM api_prontocardio.registros_glosa_demonstrativo_ipm rastreio
            JOIN api_prontocardio.registros_glosa registro
              ON registro.id = rastreio.registro_glosa_id
             AND registro.sn_ativo = 'true'
           WHERE rastreio.id_registro = glosa.id_registro
           ORDER BY registro.id
           LIMIT 1
      ) AS registro ON TRUE
      LEFT JOIN api_prontocardio.associacoes_itens_ipm_manuais associacao
        ON associacao.glosa_id_registro = glosa.id_registro
       AND associacao.nr = :nr_maida
     WHERE glosa.motivo IN :motivos
"""


def _sem_zeros(valor) -> str:
    return str(valor or '').strip().lstrip('0').upper()


def _params_base() -> dict:
    return {'nr_maida': NR_MAIDA, 'motivos': MOTIVOS_PENDENTES}


def _query_item(sql: str):
    return text(sql).bindparams(bindparam('motivos', expanding=True))


def listar_itens(  # noqa: PLR0913
    session,
    *,
    lote: str | None = None,
    guia: str | None = None,
    situacao: str = 'pendentes',
    limit: int = 20,
    offset: int = 0,
) -> dict:
    filtros = []
    params = _params_base()
    if lote:
        filtros.append('BTRIM(demo.numero_lote) = :lote')
        params['lote'] = lote.strip()
    if guia:
        filtros.append(
            "LTRIM(BTRIM(demo.numero_guia_senha), '0') = LTRIM(:guia, '0')"
        )
        params['guia'] = guia.strip()
    if situacao == 'associadas':
        filtros.append('associacao.id IS NOT NULL')
    elif situacao == 'pendentes':
        filtros.append(
            'associacao.id IS NULL'
            ' AND COALESCE(registro.cd_atendimento, 0) = 0'
        )
    where = ''.join(f' AND {filtro}' for filtro in filtros)
    base = _ITEM_SQL + where
    total = session.execute(
        _query_item(f'SELECT COUNT(*) FROM ({base}) AS itens'), params
    ).scalar() or 0
    linhas = session.execute(
        _query_item(
            base + ' ORDER BY demo.data_realizacao DESC NULLS LAST,'
            ' glosa.id_registro LIMIT :limit OFFSET :offset'
        ),
        {**params, 'limit': limit, 'offset': offset},
    ).mappings().all()
    return {
        'itens': [dict(linha) for linha in linhas],
        'total': int(total),
        'limit': limit,
        'offset': offset,
    }


def buscar_item(session, id_registro: str) -> dict:
    linha = session.execute(
        _query_item(_ITEM_SQL + ' AND glosa.id_registro = :id_registro'),
        {**_params_base(), 'id_registro': id_registro.strip()},
    ).mappings().first()
    if linha is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Item da Maida pendente não encontrado.',
        )
    return dict(linha)


def lancamentos_atendimento(session_oracle, cd_atendimento: int) -> list[dict]:
    linhas = session_oracle.execute(
        text(
            """
            SELECT cd_remessa, cd_reg, cd_lancamento, cd_atendimento,
                   cd_paciente, nm_paciente, cd_convenio, nm_convenio,
                   tp_atendimento, nr_guia, nr_carteira, cd_pro_fat, cd_tuss,
                   descricao, dt_atendimento, dt_alta, dt_lancamento,
                   qt_lancamento, vl_total_conta
              FROM dbamv.hpc_v_conta_atendimento
             WHERE cd_atendimento = :cd_atendimento
             ORDER BY dt_lancamento, cd_reg, cd_lancamento
            """
        ),
        {'cd_atendimento': cd_atendimento},
    ).mappings().all()
    vistos = set()
    resultado = []
    for linha in linhas:
        chave = (linha['cd_remessa'], linha['cd_reg'], linha['cd_lancamento'])
        if chave in vistos:
            continue
        vistos.add(chave)
        resultado.append(dict(linha))
    return resultado


def _codigos_brasindice(session, codigo_servico: str) -> set[str]:
    try:
        return {
            _sem_zeros(codigo)
            for codigo in session.execute(
                text(
                    """
                    SELECT cd_pro_fat
                      FROM api_prontocardio_staging.ipm_brasindice_oracle
                     WHERE cd_tiss = :codigo
                    """
                ),
                {'codigo': _sem_zeros(codigo_servico)},
            ).scalars()
        }
    except Exception:  # noqa: BLE001 - staging table is optional
        session.rollback()
        return set()


def marcar_candidatos(
    session, item: dict, lancamentos: list[dict]
) -> list[dict]:
    codigo = _sem_zeros(item.get('codigo_servico'))
    brasindice = None
    candidatos = []
    for lancamento in lancamentos:
        mesmo = codigo and codigo in {
            _sem_zeros(lancamento.get('cd_pro_fat')),
            _sem_zeros(lancamento.get('cd_tuss')),
        }
        if not mesmo and codigo:
            if brasindice is None:
                brasindice = _codigos_brasindice(session, codigo)
            mesmo = _sem_zeros(lancamento.get('cd_pro_fat')) in brasindice
        issec = int(lancamento.get('cd_convenio') or 0) == CD_CONVENIO_ISSEC
        candidatos.append({
            'cd_remessa': lancamento.get('cd_remessa'),
            'conta': lancamento.get('cd_reg'),
            'cd_lancamento': lancamento.get('cd_lancamento'),
            'cd_atendimento': lancamento.get('cd_atendimento'),
            'nm_convenio': lancamento.get('nm_convenio'),
            'tp_atendimento': lancamento.get('tp_atendimento'),
            'nr_guia': lancamento.get('nr_guia'),
            'cd_pro_fat': lancamento.get('cd_pro_fat'),
            'cd_tuss': lancamento.get('cd_tuss'),
            'descricao': lancamento.get('descricao'),
            'dt_lancamento': lancamento.get('dt_lancamento'),
            'qt_lancamento': lancamento.get('qt_lancamento'),
            'valor_item': Decimal(str(lancamento.get('vl_total_conta') or 0)),
            'mesmo_procedimento': bool(mesmo),
            'issec': issec,
            'faturado': lancamento.get('cd_remessa') is not None,
            'selecionavel': bool(
                mesmo and issec and lancamento.get('cd_remessa') is not None
            ),
        })
    candidatos.sort(key=lambda c: not c['selecionavel'])
    return candidatos


def _validar_escolha(  # noqa: PLR0913
    session, session_oracle, item, *, cd_atendimento, cd_remessa, conta,
    cd_lancamento,
):
    lancamentos = lancamentos_atendimento(session_oracle, cd_atendimento)
    candidatos = marcar_candidatos(session, item, lancamentos)
    escolhido = next(
        (
            c for c in candidatos
            if (c['cd_remessa'], c['conta'], c['cd_lancamento'])
            == (cd_remessa, conta, cd_lancamento)
        ),
        None,
    )
    if escolhido is None:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O lançamento escolhido não pertence a esse atendimento.',
        )
    if not escolhido['selecionavel']:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                'O lançamento precisa ser ISSEC, estar em remessa e ter o '
                'mesmo procedimento do item glosado.'
            ),
        )
    ocupado = session.execute(
        text(
            """
            SELECT 1
              FROM api_prontocardio.associacoes_itens_ipm_manuais
             WHERE cd_remessa = :cd_remessa AND conta = :conta
               AND cd_lancamento = :cd_lancamento
            UNION ALL
            SELECT 1
              FROM api_prontocardio.registros_glosa
             WHERE sn_ativo = 'true' AND cd_remessa = :cd_remessa
               AND conta = :conta AND cd_lancamento = :cd_lancamento
             LIMIT 1
            """
        ),
        {
            'cd_remessa': cd_remessa,
            'conta': conta,
            'cd_lancamento': cd_lancamento,
        },
    ).first()
    if ocupado:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Esse lançamento do MV já está vinculado a outra glosa.',
        )
    return next(
        lancamento for lancamento in lancamentos
        if (
            lancamento['cd_remessa'], lancamento['cd_reg'],
            lancamento['cd_lancamento'],
        ) == (cd_remessa, conta, cd_lancamento)
    )


# Same item key used by the Follow-Up to group a glosa and its treatments.
_MESMO_ITEM_SQL = """
    FROM api_prontocardio.registros_glosa origem
   WHERE origem.id = :registro_glosa_id
     AND destino.sn_ativo = 'true'
     AND destino.cd_remessa = origem.cd_remessa
     AND destino.cd_atendimento IS NOT DISTINCT FROM origem.cd_atendimento
     AND destino.conta = origem.conta
     AND destino.cd_lancamento = origem.cd_lancamento
     AND destino.motivo_glosa IS NOT DISTINCT FROM origem.motivo_glosa
     AND destino.guia IS NOT DISTINCT FROM origem.guia
     AND destino.procedimento IS NOT DISTINCT FROM origem.procedimento
"""


def _aplicar_contexto_mv(session, item: dict, lancamento: dict) -> int:
    resultado = session.execute(
        text(
            """
            UPDATE api_prontocardio.registros_glosa AS destino
               SET cd_remessa = :cd_remessa,
                   conta = :conta,
                   cd_atendimento = :cd_atendimento,
                   codigo_paciente = :codigo_paciente,
                   nm_paciente = COALESCE(:nm_paciente, destino.nm_paciente),
                   tp_atendimento = COALESCE(:tp_atendimento,
                                             destino.tp_atendimento),
                   data_atendimento = COALESCE(:dt_atendimento,
                                               destino.data_atendimento),
                   data_alta = :dt_alta,
                   cd_convenio = :cd_convenio
            """ + _MESMO_ITEM_SQL
        ),
        {
            'registro_glosa_id': item['registro_glosa_id'],
            'cd_remessa': lancamento['cd_remessa'],
            'conta': lancamento['cd_reg'],
            'cd_atendimento': lancamento['cd_atendimento'],
            'codigo_paciente': lancamento.get('cd_paciente') or 0,
            'nm_paciente': lancamento.get('nm_paciente'),
            'tp_atendimento': lancamento.get('tp_atendimento'),
            'dt_atendimento': lancamento.get('dt_atendimento'),
            'dt_alta': lancamento.get('dt_alta'),
            'cd_convenio': lancamento.get('cd_convenio') or CD_CONVENIO_ISSEC,
        },
    )
    return max(resultado.rowcount or 0, 0)


def _restaurar_contexto_maida(session, item: dict) -> int:
    """Restore the provisional context exactly as the Airflow builds it."""
    resultado = session.execute(
        text(
            """
            UPDATE api_prontocardio.registros_glosa AS destino
               SET cd_remessa = CASE
                       WHEN BTRIM(COALESCE(demo.numero_lote, '')) ~ '^[0-9]+$'
                           THEN LEAST(demo.numero_lote::BIGINT,
                                      2147483647)::INTEGER
                       ELSE 1000000000 + (ABS(HASHTEXTEXTENDED(
                           COALESCE(demo.numero_lote, demo.id_registro), 0))
                           % 1000000000)::INTEGER
                   END,
                   conta = CASE
                       WHEN BTRIM(COALESCE(demo.numero_guia_senha, ''))
                            ~ '^[0-9]+$'
                           THEN LEAST(demo.numero_guia_senha::BIGINT,
                                      2147483647)::INTEGER
                       ELSE 1000000000 + (ABS(HASHTEXTEXTENDED(
                           demo.id_registro || '|conta', 0))
                           % 1000000000)::INTEGER
                   END,
                   cd_atendimento = 0,
                   codigo_paciente = 0,
                   nm_paciente = COALESCE(
                       NULLIF(BTRIM(demo.nome_beneficiario), ''),
                       'Guia ' || COALESCE(
                           NULLIF(BTRIM(demo.numero_guia_senha), ''),
                           'não informada')),
                   tp_atendimento = 'Externo',
                   data_atendimento = COALESCE(
                       demo.data_realizacao::TIMESTAMP,
                       demo.referencia::TIMESTAMP, destino.data_atendimento),
                   data_alta = NULL,
                   cd_convenio = 10
              FROM api_prontocardio.registros_glosa origem,
                   api_prontocardio.demonstrativo_conta_ipm demo
             WHERE demo.id_registro = :id_registro
               AND origem.id = :registro_glosa_id
               AND destino.sn_ativo = 'true'
               AND destino.cd_remessa = origem.cd_remessa
               AND destino.cd_atendimento IS NOT DISTINCT FROM
                   origem.cd_atendimento
               AND destino.conta = origem.conta
               AND destino.cd_lancamento = origem.cd_lancamento
               AND destino.motivo_glosa IS NOT DISTINCT FROM
                   origem.motivo_glosa
               AND destino.guia IS NOT DISTINCT FROM origem.guia
               AND destino.procedimento IS NOT DISTINCT FROM
                   origem.procedimento
            """
        ),
        {
            'id_registro': item['id_registro'],
            'registro_glosa_id': item['registro_glosa_id'],
        },
    )
    return max(resultado.rowcount or 0, 0)


def associar(  # noqa: PLR0913
    session, session_oracle, *, id_registro, cd_atendimento, cd_remessa,
    conta, cd_lancamento, usuario_id,
) -> dict:
    item = buscar_item(session, id_registro)
    if item['associacao_id'] is not None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Esse item da Maida já foi associado.',
        )
    if int(item['registro_cd_atendimento'] or 0) != 0:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Essa glosa já possui atendimento do MV.',
        )
    lancamento = _validar_escolha(
        session, session_oracle, item,
        cd_atendimento=cd_atendimento, cd_remessa=cd_remessa,
        conta=conta, cd_lancamento=cd_lancamento,
    )
    referencia = item.get('data_realizacao') or item.get('referencia')
    associacao = AssociacaoItemIpmManual(
        glosa_id_registro=item['id_registro'],
        numero_processo=str(item.get('lote') or '-')[:100],
        competencia_producao=(
            referencia.strftime('%m/%Y') if referencia else '-'
        ),
        nr=NR_MAIDA,
        cd_remessa=cd_remessa,
        conta=conta,
        cd_lancamento=cd_lancamento,
        criterio_correspondencia=CRITERIO_MAIDA,
        usuario_id=usuario_id,
    )
    session.add(associacao)
    session.flush()
    atualizados = _aplicar_contexto_mv(session, item, lancamento)
    session.commit()
    return {
        'associacao_id': associacao.id,
        'cd_remessa': cd_remessa,
        'conta': conta,
        'cd_lancamento': cd_lancamento,
        'cd_atendimento': lancamento['cd_atendimento'],
        'registros_atualizados': atualizados,
    }


def excluir(session, associacao_id: int) -> dict:
    associacao = session.get(AssociacaoItemIpmManual, associacao_id)
    if associacao is None or associacao.nr != NR_MAIDA:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Associação da Maida não encontrada.',
        )
    item = buscar_item(session, associacao.glosa_id_registro)
    restaurados = _restaurar_contexto_maida(session, item)
    session.delete(associacao)
    session.commit()
    return {'registros_restaurados': restaurados}
