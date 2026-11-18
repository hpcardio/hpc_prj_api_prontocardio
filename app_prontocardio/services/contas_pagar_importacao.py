# ruff: noqa: E501, PLR0913
"""Importação de NFS-e recebidas para o MV (Contas a Pagar): simulação.

Monta, sem gravar nada, o que a rotina M_ENTRADA_SERV + O_IMP_MGES_FNFI do
Soul MV gravaria para uma NFS-e com boletos, e confere as validações da
função DBAMV.FNC_IMPORTA_MGES_FNFI_SERV (-20001 a -20011) antes do clique.

Mapeamento: querys_oracle/FINANCEIRO/telas_financeira_mv (M_ENTRADA_SERV e
O_IMP_MGES_FNFI). As leituras rodam na sessão Oracle do operador (proxy
RC_PROXY), a mesma que fará a gravação real; a única chamada que altera
estado é PKG_MV2000.ATRIBUI_EMPRESA, que vale só para essa sessão.

Escopo desta versão: notas sem retenção de impostos. Notas com retenção ou
com imposto configurado no serviço/fornecedor ficam bloqueadas, com os
detalhamentos listados, até o cálculo via PRC_CALC_IMPOSTO ser tratado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

TOLERANCIA = Decimal('0.01')
COMPARTILHAMENTO_CONTA_CUSTO = {'1', '4'}  # -20007 só vale nestes
COMPARTILHAMENTO_CONTA_CONTABIL = {'2', '4'}  # -20003 e -20008 só valem nestes
SEQUENCE = 'sequence'

# Campos da ENT_SERV copiados da última entrada do mesmo fornecedor na
# empresa (o financeiro sempre lança o mesmo fornecedor do mesmo jeito).
CAMPOS_DO_MODELO = (
    'cd_tip_doc', 'tp_documento_entrada', 'tp_entrega', 'cd_cfop', 'sn_atualiza_preco',
    'cd_justifica', 'tp_documento_ntfs', 'tp_tributacao_ntfs', 'tp_iss_retido_ntfs',
    'cd_modelo_entrada', 'cd_id_cprb', 'tp_status_sped_terceiros', 'nr_serie', 'sn_fechado',
)

TABELAS_GRAVADAS = ('ENT_SERV', 'ITENT_SERV', 'DOCUMENTO_ENTRADA', 'DUPLICATA', 'ENT_SERV_CONCLUSAO')

SQL_ATRIBUI_EMPRESA = 'begin dbamv.pkg_mv2000.atribui_empresa(:empresa); end;'

SQL_CONFIG = """/* rc:config */
select dbamv.pkg_mv2000.le_empresa as empresa,
       dbamv.pkt_config_financ.retorna_campo('tp_compartilhamento') as tp_compartilhamento,
       user as usuario
  from dual"""

SQL_FORNECEDORES = """/* rc:fornecedores */
select cd_fornecedor, nm_fornecedor, cd_cidade
  from dbamv.fornecedor
 where regexp_replace(nr_cgc_cpf, '[^0-9]') = :cnpj
 order by cd_fornecedor"""

SQL_MODELO = """/* rc:modelo */
select * from (
  select es.*, it.cd_servico as it_cd_servico,
         (select count(*) from dbamv.itent_serv i2 where i2.cd_ent_serv = es.cd_ent_serv) as qt_itens
    from dbamv.ent_serv es
    join dbamv.oficina o on o.cd_oficina = es.cd_oficina
    left join dbamv.itent_serv it on it.cd_ent_serv = es.cd_ent_serv
   where es.cd_fornecedor in ({fornecedores})
     and o.cd_multi_empresa = :empresa
   order by es.dt_entrada desc, es.hr_entrada desc, es.cd_ent_serv desc
) where rownum = 1"""

SQL_OFICINA = """/* rc:oficina */
select cd_oficina, ds_oficina, cd_setor, cd_multi_empresa
  from dbamv.oficina where cd_oficina = :oficina"""

SQL_SERVICO = """/* rc:servico */
select cd_servico, nm_servico, cd_reduzido, cd_item_res,
       (select count(*) from dbamv.ofi_serv os
         where os.cd_servico = ms.cd_servico and os.cd_oficina = :oficina) as na_oficina
  from dbamv.manu_serv ms where cd_servico = :servico"""

SQL_CONTA_FORNECEDOR = """/* rc:forn_conta */
select fc.cd_reduzido, pc.cd_historico_padrao
  from dbamv.forn_conta fc
  left join dbamv.plano_contas pc on pc.cd_reduzido = fc.cd_reduzido
 where fc.cd_fornecedor = :fornecedor and fc.tp_conta = 'FS' and fc.cd_multi_empresa = :empresa"""

SQL_DUPLICIDADE = """/* rc:duplicidade */
select 'CON_PAG' as tabela, to_char(cd_con_pag) as chave, nr_serie
  from dbamv.con_pag
 where cd_fornecedor = :fornecedor and nr_documento = :documento and cd_multi_empresa = :empresa
union all
select 'ENT_SERV', to_char(cd_ent_serv), nr_serie
  from dbamv.ent_serv where cd_fornecedor = :fornecedor and nr_documento = :documento
union all
select 'DOCUMENTO_ENTRADA', nr_documento, nr_serie
  from dbamv.documento_entrada
 where cd_fornecedor = :fornecedor and nr_documento = :documento and cd_multi_empresa = :empresa"""

# Mesma composição da tela (02_itens_impostos.sql, blocos [07] e [09]).
SQL_DETALHAMENTOS = """/* rc:detalhamentos */
select 'servico' as origem, si.cd_detalhamento, td.ds_detalhamento, td.tp_detalhamento
  from dbamv.manu_serv_imposto si
  join dbamv.tip_detalhe td on td.cd_detalhamento = si.cd_detalhamento
 where si.cd_servico = :servico and si.cd_multi_empresa = :empresa
union all
select 'fornecedor', fd.cd_detalhamento, td.ds_detalhamento, td.tp_detalhamento
  from dbamv.forn_detalhamento fd
  join dbamv.tip_detalhe td on td.cd_detalhamento = fd.cd_detalhamento
 where fd.cd_fornecedor = :fornecedor and fd.cd_multi_empresa = :empresa
   and fd.sn_retem_servico = 'S'
   and td.tp_detalhamento not in (
       select d.tp_detalhamento from dbamv.manu_serv_imposto s
         join dbamv.tip_detalhe d on d.cd_detalhamento = s.cd_detalhamento
        where s.cd_servico = :servico and s.cd_multi_empresa = :empresa)"""

SQL_OBRIGATORIOS = """/* rc:obrigatorios */
select table_name, lower(column_name) as coluna
  from all_tab_columns
 where owner = 'DBAMV' and nullable = 'N'
   and table_name in ('ENT_SERV', 'ITENT_SERV', 'DOCUMENTO_ENTRADA', 'DUPLICATA', 'ENT_SERV_CONCLUSAO')"""


@dataclass
class Simulacao:
    pendencias: list[dict] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    escolhas: dict[str, Any] = field(default_factory=dict)
    gravacoes: list[dict] = field(default_factory=list)
    importacao: dict[str, Any] = field(default_factory=dict)

    def pendencia(self, codigo: str, mensagem: str) -> None:
        self.pendencias.append({'codigo': codigo, 'mensagem': mensagem})

    def as_dict(self) -> dict:
        return {
            'apta': not self.pendencias,
            'pendencias': self.pendencias,
            'avisos': self.avisos,
            'escolhas': self.escolhas,
            'gravacoes': self.gravacoes,
            'importacao': self.importacao,
        }


def linhas(cursor, sql: str, **binds) -> list[dict]:
    cursor.execute(sql, binds)
    colunas = [d[0].lower() for d in cursor.description]
    return [dict(zip(colunas, valores, strict=True)) for valores in cursor.fetchall()]


def _dec(valor) -> Decimal:
    return Decimal(str(valor or '0'))


def retencao_da_nota(nota: dict) -> Decimal:
    """Valor retido segundo a NFS-e: serviços - líquido."""
    return _dec(nota.get('valor_servicos')) - _dec(nota.get('valor_liquido_nfse'))


def _campo(valor, origem: str) -> dict:
    return {'valor': valor, 'origem': origem}


def _escolher_fornecedor(fornecedores: list[dict], modelo: dict | None, sim: Simulacao) -> dict | None:
    if not fornecedores:
        sim.pendencia('fornecedor', 'O CNPJ do prestador não está cadastrado como fornecedor no MV.')
        return None
    if modelo is not None:
        return next(f for f in fornecedores if f['cd_fornecedor'] == modelo['cd_fornecedor'])
    if len(fornecedores) > 1:
        codigos = ', '.join(str(f['cd_fornecedor']) for f in fornecedores)
        sim.pendencia('fornecedor', f'Há {len(fornecedores)} fornecedores com este CNPJ no MV ({codigos}) e nenhum lançamento anterior nesta empresa para decidir.')
        return None
    return fornecedores[0]


def _conferir_cadastros(cursor, sim: Simulacao, *, fornecedor: int, empresa: int, oficina: int,
                        servico: int | None, tp_compartilhamento: str | None) -> dict | None:
    """Validações -20003, -20006/-20009, -20007 e -20008 e serviço x oficina."""
    ofi = next(iter(linhas(cursor, SQL_OFICINA, oficina=oficina)), None)
    if ofi is None or ofi['cd_multi_empresa'] != empresa:
        sim.pendencia('oficina', f'A oficina {oficina} não pertence à empresa {empresa} no MV.')
    elif ofi['cd_setor'] is None:
        sim.pendencia('-20006', f'A oficina {ofi["ds_oficina"]} não possui setor associado.')
    sim.escolhas['oficina'] = ofi

    conta = next(iter(linhas(cursor, SQL_CONTA_FORNECEDOR, fornecedor=fornecedor, empresa=empresa)), None)
    sim.escolhas['conta_fornecedor'] = conta
    if tp_compartilhamento in COMPARTILHAMENTO_CONTA_CONTABIL and (conta is None or conta['cd_reduzido'] is None):
        sim.pendencia('-20003', f'O fornecedor {fornecedor} não possui conta contábil (FORN_CONTA tipo FS) na empresa {empresa}.')

    if servico is None:
        sim.pendencia('servico', 'Não há lançamento anterior deste fornecedor nesta empresa para sugerir o serviço.')
        return ofi
    serv = next(iter(linhas(cursor, SQL_SERVICO, servico=servico, oficina=oficina)), None)
    sim.escolhas['servico'] = serv
    if serv is None:
        sim.pendencia('servico', f'O serviço {servico} não existe mais no MV.')
        return ofi
    if not serv['na_oficina']:
        sim.pendencia('servico', f'O serviço {servico} ({serv["nm_servico"]}) não está liberado para a oficina {oficina}.')
    if tp_compartilhamento in COMPARTILHAMENTO_CONTA_CUSTO and serv['cd_item_res'] is None:
        sim.pendencia('-20007', f'O serviço {servico} não possui conta de custo (item de resultado).')
    if tp_compartilhamento in COMPARTILHAMENTO_CONTA_CONTABIL and serv['cd_reduzido'] is None:
        sim.pendencia('-20008', f'O serviço {servico} não possui conta contábil.')
    return ofi


def _conferir_boletos(sim: Simulacao, nota: dict, boletos: list[dict], hoje: date) -> None:
    if not boletos:
        sim.pendencia('-20002', 'A NFS-e não tem boleto anexado (a importação exige duplicatas).')
        return
    for b in boletos:
        if b.get('divergencias'):
            sim.pendencia('boleto', f'O boleto {b["id"]} tem divergência: {"; ".join(b["divergencias"])}')
        if b['vencimento'] < hoje:
            sim.pendencia('boleto', f'O boleto {b["id"]} venceu em {b["vencimento"]:%d/%m/%Y}.')
    soma = sum((_dec(b['valor']) for b in boletos), Decimal('0'))
    liquido = _dec(nota.get('valor_liquido_nfse'))
    if abs(soma - liquido) > TOLERANCIA:
        sim.pendencia('boleto', f'A soma dos boletos (R$ {soma:.2f}) difere do valor líquido da NFS-e (R$ {liquido:.2f}).')


def _montar_gravacoes(sim: Simulacao, *, nota: dict, boletos: list[dict], modelo: dict, fornecedor: dict,
                      empresa: int, oficina: int, servico: int, usuario: str, agora: datetime) -> None:
    hoje = agora.date()
    origem_modelo = f'última entrada do fornecedor (ENT_SERV {modelo["cd_ent_serv"]})'
    total = _dec(nota['valor_servicos'])
    documento = nota['numero_nfse']
    emissao = nota['data_emissao']
    serie = modelo['nr_serie']

    ent_serv = {'cd_ent_serv': _campo('SEQ_ENT_SERV.NEXTVAL', SEQUENCE)}
    ent_serv.update({c: _campo(modelo.get(c), origem_modelo) for c in CAMPOS_DO_MODELO})
    ent_serv.update({
        'cd_oficina': _campo(oficina, 'regra fornecedor → oficina'),
        'nr_documento': _campo(documento, 'NFS-e'),
        'cd_fornecedor': _campo(fornecedor['cd_fornecedor'], 'fornecedor do CNPJ'),
        'dt_emissao': _campo(emissao, 'NFS-e'),
        'dt_entrada': _campo(hoje, 'data da importação'),
        'hr_entrada': _campo(agora, 'hora da importação'),
        'vl_total': _campo(total, 'NFS-e (valor dos serviços)'),
        'vl_percentual_iss': _campo(Decimal('0'), 'sem retenção'),
        'vl_imposto_iss': _campo(Decimal('0'), 'sem retenção'),
        'vl_percentual_desconto': _campo(Decimal('0'), 'NFS-e'),
        'vl_desconto': _campo(Decimal('0'), 'NFS-e'),
        'vl_outros': _campo(Decimal('0'), 'NFS-e'),
        'cd_usuario': _campo(usuario, 'usuário MV da sessão'),
        'sn_importada_fnfi': _campo('N', 'fixo (a importação muda para S)'),
        'data_status_sped_terc': _campo(hoje, 'data da importação'),
        'hora_status_sped_terc': _campo(agora, 'hora da importação'),
        'cd_cidade': _campo(fornecedor['cd_cidade'], 'cadastro do fornecedor'),
    })
    itent = {
        'cd_itent_serv': _campo('SEQ_ITENT_SERV.NEXTVAL', SEQUENCE),
        'cd_ent_serv': _campo('ENT_SERV.CD_ENT_SERV', 'entrada acima'),
        'cd_servico': _campo(servico, origem_modelo),
        'qt_entrada': _campo(Decimal('1'), 'fixo'),
        'qt_atendida': _campo(Decimal('1'), 'fixo'),
        'vl_unitario': _campo(total, 'NFS-e'),
        'vl_total': _campo(total, 'NFS-e'),
        **{c: _campo(Decimal('0'), 'sem desconto/retenção') for c in (
            'vl_perc_desconto', 'vl_desconto', 'vl_desconto_moeda', 'vl_imposto', 'vl_imposto_moeda',
            'vl_perc_imposto', 'vl_imposto_nao_retido', 'vl_inss_nao_retido', 'vl_base_iss', 'vl_imposto_iss_item')},
    }
    soma_dup = sum((_dec(b['valor']) for b in boletos), Decimal('0'))
    doc = {
        'cd_fornecedor': _campo(fornecedor['cd_fornecedor'], 'fornecedor do CNPJ'),
        'nr_documento': _campo(documento, 'NFS-e'),
        'nr_serie': _campo(serie, origem_modelo),
        'cd_tip_doc': _campo(modelo.get('cd_tip_doc'), origem_modelo),
        'tp_documento_entrada': _campo(modelo.get('tp_documento_entrada'), origem_modelo),
        'dt_emissao': _campo(emissao, 'NFS-e'),
        'dt_entrada': _campo(hoje, 'data da importação'),
        'vl_total': _campo(total, 'NFS-e'),
        'vl_percentual_iss': _campo(Decimal('0'), 'sem retenção'),
        'vl_imposto_iss': _campo(Decimal('0'), 'sem retenção'),
        'vl_percentual_desconto': _campo(Decimal('0'), 'NFS-e'),
        'vl_desconto': _campo(Decimal('0'), 'NFS-e'),
        'vl_outros': _campo(Decimal('0'), 'NFS-e'),
        'vl_saldo_documento': _campo(total, 'igual ao total; zerado na conclusão'),
        'sn_importado_financeiro': _campo('N', 'fixo'),
        'cd_multi_empresa': _campo(empresa, 'tomador da NFS-e'),
        'sn_importada_fnfi': _campo('N', 'fixo (a importação muda para S)'),
        'cd_cfop': _campo(modelo.get('cd_cfop'), origem_modelo),
    }
    sim.gravacoes = [
        {'ordem': 1, 'tabela': 'ENT_SERV', 'operacao': 'INSERT', 'campos': ent_serv},
        {'ordem': 2, 'tabela': 'ITENT_SERV', 'operacao': 'INSERT', 'campos': itent,
         'observacao': 'A trigger TRG_I_ITENT_SERV preenche a natureza de despesa.'},
        {'ordem': 3, 'tabela': 'DOCUMENTO_ENTRADA', 'operacao': 'INSERT', 'campos': doc},
    ]
    for parcela, b in enumerate(boletos, start=1):
        sim.gravacoes.append({'ordem': 3 + parcela, 'tabela': 'DUPLICATA', 'operacao': 'INSERT', 'campos': {
            'cd_fornecedor': _campo(fornecedor['cd_fornecedor'], 'fornecedor do CNPJ'),
            'nr_documento': _campo(documento, 'NFS-e'),
            'nr_serie': _campo(serie, origem_modelo),
            'cd_parcela': _campo(parcela, 'ordem dos vencimentos'),
            'dt_vencimento': _campo(b['vencimento'], f'boleto {b["id"]}'),
            'vl_parcela': _campo(_dec(b['valor']), f'boleto {b["id"]}'),
        }})
    proxima = 4 + len(boletos)
    sim.gravacoes += [
        {'ordem': proxima, 'tabela': 'DOCUMENTO_ENTRADA', 'operacao': 'UPDATE', 'campos': {
            'vl_saldo_documento': _campo(total - soma_dup, 'total - duplicatas (conclusão)')}},
        {'ordem': proxima + 1, 'tabela': 'ENT_SERV_CONCLUSAO', 'operacao': 'INSERT', 'campos': {
            'cd_ent_serv': _campo('ENT_SERV.CD_ENT_SERV', 'entrada acima'),
            'cd_usuario': _campo(usuario, 'USER da sessão'),
            'dt_conclusao_serv': _campo('SYSDATE', 'hora da importação')},
         'observacao': 'A trigger TRG_ENT_SERV_CONCLUSAO não importa sozinha (SN_IMPORT_AUT_ESTOQUE = N).'},
    ]
    vencimento = min(b['vencimento'] for b in boletos)
    sim.importacao = {
        'chamada': 'DBAMV.FNC_IMPORTA_MGES_FNFI_SERV',
        'parametros': {'p_cd_fornecedor': fornecedor['cd_fornecedor'], 'p_nr_documento': documento,
                       'p_nr_serie': serie, 'p_cd_multi_empresa': empresa},
        'titulo_previsto': {
            'tabela': 'CON_PAG (+ RATCON_PAG e ITCON_PAG)',
            'tp_con_pag': 'ES',
            'vl_bruto_conta': total,
            'cd_reduzido': (sim.escolhas.get('conta_fornecedor') or {}).get('cd_reduzido'),
            'cd_setor': (sim.escolhas.get('oficina') or {}).get('cd_setor'),
            'parcelas': [{'nr_parcela': i, 'dt_vencimento': b['vencimento'], 'vl_duplicata': _dec(b['valor'])}
                         for i, b in enumerate(boletos, start=1)],
            'primeiro_vencimento': vencimento,
            'cd_usuario_ins': usuario,
        },
    }


def _conferir_impostos(cursor, sim: Simulacao, *, nota: dict, servico: int | None,
                       fornecedor: int, empresa: int) -> None:
    retido = retencao_da_nota(nota)
    if retido > TOLERANCIA:
        sim.pendencia('retencao', f'A NFS-e tem R$ {retido:.2f} de retenção. Esta versão ainda não importa notas com retenção.')
    if servico is None:
        return
    detalhamentos = linhas(cursor, SQL_DETALHAMENTOS, servico=servico, fornecedor=fornecedor, empresa=empresa)
    sim.escolhas['detalhamentos'] = detalhamentos
    if detalhamentos:
        nomes = ', '.join(f'{d["cd_detalhamento"]} {d["ds_detalhamento"]}' for d in detalhamentos)
        sim.pendencia('impostos', f'O MV calcularia impostos para este serviço/fornecedor ({nomes}). Esta versão ainda não trata impostos.')


def _conferir_obrigatorios(cursor, sim: Simulacao) -> None:
    gravados: dict[str, set[str]] = {}
    for g in sim.gravacoes:
        if g['operacao'] == 'INSERT':
            gravados.setdefault(g['tabela'], set()).update(
                c for c, v in g['campos'].items() if v['valor'] is not None)
    for linha in linhas(cursor, SQL_OBRIGATORIOS):
        tabela = linha['table_name']
        if tabela in gravados and linha['coluna'] not in gravados[tabela]:
            sim.pendencia('obrigatorio', f'{tabela}.{linha["coluna"].upper()} é obrigatório e ficaria sem valor.')


def simular_importacao(cursor, *, nota: dict, boletos: list[dict], agora: datetime) -> dict:
    """Plano de gravação + pendências. `cursor` é da sessão MV do operador."""
    sim = Simulacao()
    empresa = nota.get('cd_multi_empresa')
    oficina = nota.get('cd_oficina')
    if empresa is None:
        sim.pendencia('empresa', 'O CNPJ do tomador não corresponde a uma empresa do MV.')
        return sim.as_dict()
    if oficina is None:
        sim.pendencia('oficina', 'O fornecedor não tem setor (oficina) definido nas regras.')
        return sim.as_dict()

    cursor.execute(SQL_ATRIBUI_EMPRESA, {'empresa': empresa})
    config = linhas(cursor, SQL_CONFIG)[0]
    tp_compartilhamento = config['tp_compartilhamento']
    usuario = config['usuario']
    sim.escolhas.update({'empresa': empresa, 'usuario_mv': usuario, 'tp_compartilhamento': tp_compartilhamento})

    fornecedores = linhas(cursor, SQL_FORNECEDORES, cnpj=nota['prestador_cnpj'])
    modelo = None
    if fornecedores:
        binds = {f'f{i}': f['cd_fornecedor'] for i, f in enumerate(fornecedores)}
        sql = SQL_MODELO.format(fornecedores=', '.join(f':{k}' for k in binds))
        modelo = next(iter(linhas(cursor, sql, empresa=empresa, **binds)), None)
    fornecedor = _escolher_fornecedor(fornecedores, modelo, sim)
    sim.escolhas['fornecedor'] = fornecedor
    sim.escolhas['modelo'] = None if modelo is None else {
        'cd_ent_serv': modelo['cd_ent_serv'], 'nr_documento': modelo['nr_documento'],
        'dt_entrada': modelo['dt_entrada'], 'cd_usuario': modelo['cd_usuario']}
    if len(fornecedores) > 1 and fornecedor is not None:
        sim.avisos.append(f'Há {len(fornecedores)} cadastros com este CNPJ; usado o {fornecedor["cd_fornecedor"]}, do último lançamento.')
    if fornecedor is None:
        return sim.as_dict()

    servico = None
    if modelo is not None:
        if modelo['qt_itens'] != 1:
            sim.avisos.append(f'A entrada modelo {modelo["cd_ent_serv"]} tem {modelo["qt_itens"]} itens; usado o primeiro serviço.')
        servico = modelo['it_cd_servico']
    _conferir_cadastros(cursor, sim, fornecedor=fornecedor['cd_fornecedor'], empresa=empresa,
                        oficina=oficina, servico=servico, tp_compartilhamento=tp_compartilhamento)

    for linha in linhas(cursor, SQL_DUPLICIDADE, fornecedor=fornecedor['cd_fornecedor'],
                        documento=nota['numero_nfse'], empresa=empresa):
        codigo = '-20001' if linha['tabela'] == 'CON_PAG' else 'duplicidade'
        sim.pendencia(codigo, f'Já existe {linha["tabela"]} {linha["chave"]} para este fornecedor e documento.')

    _conferir_impostos(cursor, sim, nota=nota, servico=servico,
                       fornecedor=fornecedor['cd_fornecedor'], empresa=empresa)

    _conferir_boletos(sim, nota, boletos, agora.date())

    if modelo is None or servico is None:
        return sim.as_dict()
    boletos_ordenados = sorted(boletos, key=lambda b: (b['vencimento'], b['id']))
    if boletos_ordenados:
        _montar_gravacoes(sim, nota=nota, boletos=boletos_ordenados, modelo=modelo, fornecedor=fornecedor,
                          empresa=empresa, oficina=oficina, servico=servico, usuario=usuario, agora=agora)
        _conferir_obrigatorios(cursor, sim)
    return sim.as_dict()
