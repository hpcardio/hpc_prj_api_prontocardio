# ruff: noqa: E501
"""Desfazer, no MV, uma importação feita pelo Receita Certa (título + entrada de serviço).

Reproduz o caminho do ERP, mapeado em querys_oracle/FINANCEIRO/telas_financeira_mv:

1. O_IMP_MGES_FNFI ("desfazer importação"): volta os indicadores da entrada e
   do documento e exclui o título. A limpeza do título segue a pré-exclusão
   do cadastro de contas a pagar (PKG_FNCP_M_CONPAG_RES.P_B_PD_CON_PAG): logs,
   cotas, rateio, títulos de imposto filhos, retenções, histórico de
   prorrogação e parcelas, nessa ordem.
2. Como a nota volta para o Receita Certa, a entrada que a importação criou
   também é excluída: conclusão, duplicatas, documento, impostos do item,
   item e entrada.

Só para importações registradas pelo Receita Certa e só enquanto nada foi
pago. Bloqueia nos mesmos casos do MV: parcela quitada ou paga, lote de
pagamento/remessa, movimento bancário, devolução vinculada, imposto pago.
Qualquer outro registro dependente faz o Oracle recusar (FK NO ACTION) e a
transação inteira é desfeita por quem chamou.
"""

from __future__ import annotations

from decimal import Decimal

from app_prontocardio.services.contas_pagar_importacao import (
    SQL_ATRIBUI_EMPRESA,
    ImportacaoFalhou,
    linhas,
)

QUITACAO_EM_ABERTO = {'C', 'V'}

SQL_TITULO = """/* rc:desfazer_titulo */
select cd_con_pag, cd_fornecedor, nr_documento, nr_serie, cd_multi_empresa, tp_con_pag,
       vl_bruto_conta, cd_con_pag_previsao, cd_usuario_ins, dt_lancamento
  from dbamv.con_pag where cd_con_pag = :con_pag"""

SQL_ENTRADA = """/* rc:desfazer_entrada */
select cd_ent_serv, cd_fornecedor, nr_documento, nr_serie, cd_con_pag, sn_importada_fnfi, cd_oficina, vl_total
  from dbamv.ent_serv where cd_ent_serv = :ent_serv"""

SQL_FILHOS = """/* rc:desfazer_filhos */
select t.cd_con_pag_filho, t.cd_detalhamento, c.vl_bruto_conta, c.nr_documento
  from dbamv.tip_detcon_pag t
  left join dbamv.con_pag c on c.cd_con_pag = t.cd_con_pag_filho
 where t.cd_con_pag_pai = :con_pag
 order by t.cd_detalhamento"""

SQL_PARCELAS = """/* rc:desfazer_parcelas */
select cd_con_pag, cd_itcon_pag, nr_parcela, dt_vencimento, vl_duplicata, tp_quitacao
  from dbamv.itcon_pag where cd_con_pag in ({titulos})
 order by cd_con_pag, nr_parcela"""

SQL_BLOQUEIOS = """/* rc:desfazer_bloqueios */
select 'pagamento' as motivo, it.cd_con_pag, it.nr_parcela
  from dbamv.pagcon_pag pg join dbamv.itcon_pag it on it.cd_itcon_pag = pg.cd_itcon_pag
 where it.cd_con_pag in ({titulos})
union all
select 'lote de pagamento', it.cd_con_pag, it.nr_parcela
  from dbamv.lote_pgto_itcon_pag l join dbamv.itcon_pag it on it.cd_itcon_pag = l.cd_itcon_pag
 where it.cd_con_pag in ({titulos})
union all
select 'remessa bancária', it.cd_con_pag, it.nr_parcela
  from dbamv.lote_rem_itcon_pag l join dbamv.itcon_pag it on it.cd_itcon_pag = l.cd_itcon_pag
 where it.cd_con_pag in ({titulos})
union all
select 'movimento bancário', it.cd_con_pag, it.nr_parcela
  from dbamv.mov_concor m join dbamv.itcon_pag it on it.cd_itcon_pag = m.cd_itcon_pag
 where it.cd_con_pag in ({titulos})
union all
select 'devolução', d.cd_con_pag_dev, null from dbamv.con_pag d where d.cd_con_pag_dev in ({titulos})"""

# Alterações feitas no MV depois da importação (ex.: observação, portador): o desfazer as apagaria.
SQL_ALTERACOES = """/* rc:desfazer_alteracoes */
select l.dt_modificacao, l.cd_usuario, substr(l.campos_modificados, 1, 300) as campos
  from dbamv.log_con_pag_con_rec l
 where l.tp_conta = 'P' and l.cd_conta = :con_pag and l.tp_modificacao = 'U'
 order by l.cd_log_con_pag_con_rec"""

SQL_ITENS = """/* rc:desfazer_itens */
select it.cd_itent_serv, it.cd_servico, it.vl_total,
       (select count(*) from dbamv.itent_serv_imposto i where i.cd_itent_serv = it.cd_itent_serv) as qt_impostos
  from dbamv.itent_serv it where it.cd_ent_serv = :ent_serv"""

SQL_DUPLICATAS = """/* rc:desfazer_duplicatas */
select count(*) as qt from dbamv.duplicata
 where cd_fornecedor = :fornecedor and nr_documento = :documento and nr_serie = :serie"""


def _binds_titulos(ids: list[int]) -> tuple[str, dict]:
    binds = {f't{i}': v for i, v in enumerate(ids)}
    return ', '.join(f':{k}' for k in binds), binds


def planejar_desfazer(cursor, *, registro: dict) -> dict:
    """O que será removido do MV e o que impede. Só leitura (além da empresa da sessão)."""
    pendencias: list[str] = []
    con_pag, ent_serv = registro.get('cd_con_pag'), registro.get('cd_ent_serv')
    if not con_pag or not ent_serv:
        return {'pode_desfazer': False, 'pendencias': ['A importação registrada não tem título e entrada do MV.']}
    cursor.execute(SQL_ATRIBUI_EMPRESA, {'empresa': registro['cd_multi_empresa']})
    titulo = next(iter(linhas(cursor, SQL_TITULO, con_pag=con_pag)), None)
    entrada = next(iter(linhas(cursor, SQL_ENTRADA, ent_serv=ent_serv)), None)
    if titulo is None:
        pendencias.append(f'O título {con_pag} não existe mais no MV (foi excluído por lá?).')
    if entrada is None:
        pendencias.append(f'A entrada de serviço {ent_serv} não existe mais no MV.')
    if titulo is None or entrada is None:
        return {'pode_desfazer': False, 'pendencias': pendencias, 'titulo': titulo, 'entrada': entrada}

    if (titulo['cd_fornecedor'], str(titulo['nr_documento'])) != (entrada['cd_fornecedor'], str(entrada['nr_documento'])):
        pendencias.append('O título e a entrada registrados não são do mesmo fornecedor e documento.')
    if entrada['cd_con_pag'] not in (None, con_pag):
        pendencias.append(f'A entrada {ent_serv} está ligada a outro título ({entrada["cd_con_pag"]}).')
    if titulo['cd_con_pag_previsao']:
        pendencias.append('O título veio de uma previsão; desfaça pelo MV.')

    filhos = linhas(cursor, SQL_FILHOS, con_pag=con_pag)
    titulos = [con_pag, *[int(f['cd_con_pag_filho']) for f in filhos]]
    lista, binds = _binds_titulos(titulos)
    parcelas = linhas(cursor, SQL_PARCELAS.format(titulos=lista), **binds)
    for p in parcelas:
        if p['tp_quitacao'] not in QUITACAO_EM_ABERTO:
            quem = 'do título' if p['cd_con_pag'] == con_pag else f'do imposto (título {p["cd_con_pag"]})'
            pendencias.append(f'A parcela {p["nr_parcela"]} {quem} não está em aberto (situação {p["tp_quitacao"]}).')
    for b in linhas(cursor, SQL_BLOQUEIOS.format(titulos=lista), **binds):
        quem = 'o título' if b['cd_con_pag'] == con_pag else f'o imposto (título {b["cd_con_pag"]})'
        parcela = f', parcela {b["nr_parcela"]}' if b['nr_parcela'] else ''
        pendencias.append(f'Existe {b["motivo"]} para {quem}{parcela}.')

    alteracoes = linhas(cursor, SQL_ALTERACOES, con_pag=con_pag)
    itens = linhas(cursor, SQL_ITENS, ent_serv=ent_serv)
    duplicatas = linhas(cursor, SQL_DUPLICATAS, fornecedor=entrada['cd_fornecedor'],
                        documento=entrada['nr_documento'], serie=entrada['nr_serie'])[0]['qt']
    return {
        'pode_desfazer': not pendencias,
        'pendencias': pendencias,
        'titulo': titulo,
        'impostos': filhos,
        'parcelas': [p for p in parcelas if p['cd_con_pag'] == con_pag],
        'entrada': entrada,
        'itens': itens,
        'qt_duplicatas': int(duplicatas or 0),
        'alteracoes_no_mv': alteracoes,
    }


def _executar(cursor, sql: str, binds: dict, *, esperado: int | None = None, descricao: str) -> int:
    cursor.execute(sql, binds)
    if esperado is not None and cursor.rowcount != esperado:
        raise ImportacaoFalhou(f'{descricao}: esperado {esperado} registro(s), afetados {cursor.rowcount}.')
    return cursor.rowcount


def executar_desfazer(cursor, plano: dict) -> dict:
    """Executa o plano. Não faz commit nem rollback: a transação é de quem chamou."""
    if not plano.get('pode_desfazer'):
        raise ImportacaoFalhou('A importação não pode ser desfeita: ' + ' '.join(plano.get('pendencias') or []))
    titulo, entrada = plano['titulo'], plano['entrada']
    cp, es = int(titulo['cd_con_pag']), int(entrada['cd_ent_serv'])
    doc = {'fornecedor': entrada['cd_fornecedor'], 'documento': entrada['nr_documento'], 'serie': entrada['nr_serie']}
    filhos = [int(f['cd_con_pag_filho']) for f in plano['impostos']]

    # 1) O_IMP_MGES_FNFI: indicadores de volta (blocos [04]-[06]).
    _executar(cursor, 'update dbamv.ent_serv set cd_con_pag = null where cd_con_pag = :cp', {'cp': cp}, descricao='Entrada ligada ao título')
    _executar(cursor, "update dbamv.documento_entrada set sn_importado_financeiro = 'N', sn_importada_fnfi = 'N'"
                      ' where cd_fornecedor = :fornecedor and cd_multi_empresa = :empresa and nr_documento = :documento and nr_serie = :serie',
              {**doc, 'empresa': titulo['cd_multi_empresa']}, esperado=1, descricao='Documento de entrada')
    _executar(cursor, "update dbamv.ent_serv set sn_importada_fnfi = 'N', cd_con_pag = null"
                      ' where cd_fornecedor = :fornecedor and nr_documento = :documento and nr_serie = :serie',
              doc, esperado=1, descricao='Entrada de serviço')

    # 2) Pré-exclusão do título (PKG_FNCP_M_CONPAG_RES.P_B_PD_CON_PAG), na mesma ordem.
    _executar(cursor, 'delete from dbamv.fnfi_log where cd_con_pag = :cp', {'cp': cp}, descricao='Log do título')
    _executar(cursor, 'delete from dbamv.itmvto_cotas where cd_ratcon_pag in (select cd_ratcon_pag from dbamv.ratcon_pag where cd_con_pag = :cp)',
              {'cp': cp}, descricao='Cotas do rateio')
    _executar(cursor, 'delete from dbamv.ratcon_pag where cd_con_pag = :cp', {'cp': cp}, descricao='Rateio do título')
    _executar(cursor, 'delete from dbamv.tip_detcon_pag where cd_con_pag_pai = :cp', {'cp': cp},
              esperado=len(filhos), descricao='Ligação com os títulos de imposto')
    _executar(cursor, 'delete from dbamv.itretencao_tributo where cd_con_pag = :cp', {'cp': cp}, descricao='Retenções do título')
    for filho in filhos:
        _executar(cursor, 'delete from dbamv.itcon_pag where cd_con_pag = :cp', {'cp': filho}, descricao=f'Parcelas do imposto {filho}')
        _executar(cursor, 'delete from dbamv.fnfi_log where cd_con_pag = :cp', {'cp': filho}, descricao=f'Log do imposto {filho}')
        _executar(cursor, 'delete from dbamv.ratcon_pag where cd_con_pag = :cp', {'cp': filho}, descricao=f'Rateio do imposto {filho}')
        _executar(cursor, 'delete from dbamv.con_pag where cd_con_pag = :cp', {'cp': filho}, esperado=1, descricao=f'Título do imposto {filho}')
    _executar(cursor, 'delete from dbamv.rep_desc where cd_con_pag = :cp', {'cp': cp}, descricao='Descontos de repasse')
    _executar(cursor, 'update dbamv.repasse_prestador set cd_con_pag = null where cd_con_pag = :cp', {'cp': cp}, descricao='Repasse do prestador')
    _executar(cursor, 'delete from dbamv.historico_prorrogacao where cd_itcon_pag in (select cd_itcon_pag from dbamv.itcon_pag where cd_con_pag = :cp)',
              {'cp': cp}, descricao='Histórico de prorrogação')
    _executar(cursor, 'delete from dbamv.itcon_pag where cd_con_pag = :cp', {'cp': cp},
              esperado=len(plano['parcelas']), descricao='Parcelas do título')
    # 3) Exclusão do título (bloco [15]); as triggers do MV gravam log e estornam contabilidade/fluxo.
    _executar(cursor, 'delete from dbamv.con_pag where cd_con_pag = :cp', {'cp': cp}, esperado=1, descricao='Título')

    # 4) Entrada criada pela importação.
    _executar(cursor, 'delete from dbamv.ent_serv_conclusao where cd_ent_serv = :es', {'es': es}, esperado=1, descricao='Conclusão da entrada')
    _executar(cursor, 'delete from dbamv.duplicata where cd_fornecedor = :fornecedor and nr_documento = :documento and nr_serie = :serie',
              doc, esperado=plano['qt_duplicatas'], descricao='Duplicatas')
    _executar(cursor, 'delete from dbamv.documento_entrada where cd_fornecedor = :fornecedor and cd_multi_empresa = :empresa'
                      ' and nr_documento = :documento and nr_serie = :serie',
              {**doc, 'empresa': titulo['cd_multi_empresa']}, esperado=1, descricao='Documento de entrada')
    qt_impostos = sum(int(i['qt_impostos'] or 0) for i in plano['itens'])
    _executar(cursor, 'delete from dbamv.itent_serv_imposto where cd_itent_serv in (select cd_itent_serv from dbamv.itent_serv where cd_ent_serv = :es)',
              {'es': es}, esperado=qt_impostos, descricao='Impostos do item')
    _executar(cursor, 'delete from dbamv.itent_serv where cd_ent_serv = :es', {'es': es}, esperado=len(plano['itens']), descricao='Itens da entrada')
    _executar(cursor, 'delete from dbamv.ent_serv where cd_ent_serv = :es', {'es': es}, esperado=1, descricao='Entrada de serviço')
    return {'cd_con_pag': cp, 'cd_ent_serv': es, 'impostos': filhos,
            'vl_bruto': Decimal(str(titulo['vl_bruto_conta'] or 0))}
