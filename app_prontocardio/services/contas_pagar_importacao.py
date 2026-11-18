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

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

TOLERANCIA = Decimal('0.01')
# A função de importação guarda DOCUMENTO_ENTRADA.OBSERVACAO numa variável de 500 caracteres.
OBSERVACAO_MAXIMA = 500
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

TABELAS_GRAVADAS = ('ENT_SERV', 'ITENT_SERV', 'ITENT_SERV_IMPOSTO', 'DOCUMENTO_ENTRADA', 'DUPLICATA', 'ENT_SERV_CONCLUSAO')

# Retenções da NFS-e -> tipos de detalhamento (TIP_DETALHE.TP_DETALHAMENTO) do MV.
TIPOS_POR_IMPOSTO = {
    'iss': {'I', 'O'}, 'ir': {'J'}, 'csrf': {'S'}, 'csll': {'L'}, 'pis': {'P'}, 'cofins': {'C'}, 'inss': {'N'},
}
NOMES_IMPOSTO = {'iss': 'ISS', 'ir': 'IRRF', 'csrf': 'CSRF (PIS/COFINS/CSLL)', 'csll': 'CSLL', 'pis': 'PIS',
                 'cofins': 'COFINS', 'inss': 'INSS', 'outras': 'Outras retenções'}
ALIQUOTA_CSRF = Decimal('4.65')
# A função de importação desconta da parcela 1 os impostos com TP_DATA E/L; os demais têm outra regra.
TP_DATA_NA_EMISSAO = {'E', 'L'}

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
   and table_name in ('ENT_SERV', 'ITENT_SERV', 'ITENT_SERV_IMPOSTO', 'DOCUMENTO_ENTRADA', 'DUPLICATA', 'ENT_SERV_CONCLUSAO')"""

SQL_TIPOS_IMPOSTO = """/* rc:tipos_imposto */
select td.cd_detalhamento, td.ds_detalhamento, td.tp_detalhamento, td.tp_data,
       (select max(fd.vl_percentual) from dbamv.faixa_detalhe fd where fd.cd_detalhamento = td.cd_detalhamento) as vl_percentual,
       (select count(*) from dbamv.itent_serv_imposto i where i.cd_detalhamento = td.cd_detalhamento and i.sn_retem = 'S') as qt_uso
  from dbamv.tip_detalhe td
 where td.tp_detalhamento in ('I', 'O', 'J', 'S', 'L', 'P', 'C', 'N')"""

SQL_IMPOSTOS_DO_FORNECEDOR = """/* rc:impostos_fornecedor */
select distinct i.cd_detalhamento
  from dbamv.ent_serv es
  join dbamv.itent_serv it on it.cd_ent_serv = es.cd_ent_serv
  join dbamv.itent_serv_imposto i on i.cd_itent_serv = it.cd_itent_serv
 where es.cd_fornecedor = :fornecedor and i.sn_retem = 'S'"""


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
            'assinatura': assinatura(self.gravacoes, self.importacao) if self.gravacoes else None,
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


def _centavos(valor: Decimal) -> Decimal:
    return valor.quantize(Decimal('0.01'), ROUND_HALF_UP)


def retencoes_da_nfse(nota: dict) -> tuple[dict[str, Decimal] | None, str | None]:
    """Impostos que a NFS-e realmente reteve: a combinação de campos que fecha serviços - líquido.

    O XML às vezes traz PIS/COFINS só informativos (sem desconto no líquido),
    por isso não basta somar todos os campos. PIS+COFINS+CSLL retidos juntos, ou
    a CSLL com 4,65% (o ISS Fortaleza informa a CSRF no campo da CSLL), viram CSRF.
    """
    total = _dec(nota.get('valor_servicos'))
    retido = retencao_da_nota(nota)
    campos = {
        'iss': _dec(nota.get('valor_iss_retido')) if str(nota.get('iss_retido') or '') == '1' else Decimal('0'),
        'ir': _dec(nota.get('valor_ir')), 'pis': _dec(nota.get('valor_pis')), 'cofins': _dec(nota.get('valor_cofins')),
        'csll': _dec(nota.get('valor_csll')), 'inss': _dec(nota.get('valor_inss')), 'outras': _dec(nota.get('outras_retencoes')),
    }
    campos = {k: v for k, v in campos.items() if v > 0}
    solucoes = [
        combinacao for tamanho in range(1, len(campos) + 1) for combinacao in itertools.combinations(sorted(campos), tamanho)
        if abs(sum(campos[k] for k in combinacao) - retido) <= TOLERANCIA * 2
    ]
    if len(solucoes) != 1:
        detalhe = ', '.join(f'{NOMES_IMPOSTO[k]} R$ {v:.2f}' for k, v in campos.items()) or 'nenhum imposto informado'
        motivo = 'nenhuma combinação' if not solucoes else 'mais de uma combinação'
        return None, (f'A retenção da NFS-e (R$ {retido:.2f}) não fecha com os impostos do XML ({detalhe}): '
                      f'{motivo} soma esse valor.')
    retidos = {k: campos[k] for k in solucoes[0]}
    if {'pis', 'cofins', 'csll'} <= set(retidos):
        retidos['csrf'] = retidos.pop('pis') + retidos.pop('cofins') + retidos.pop('csll')
    elif 'csll' in retidos and abs(retidos['csll'] - _centavos(total * ALIQUOTA_CSRF / 100)) <= TOLERANCIA * 2:
        retidos['csrf'] = retidos.pop('csll')
    return retidos, None


def mapear_impostos(retidos: dict[str, Decimal], total: Decimal, tipos: list[dict],
                    historico: set[int]) -> tuple[list[dict], list[str]]:
    """Cada retenção vira o detalhamento do MV do mesmo tipo e alíquota (preferindo o que o fornecedor já usa)."""
    impostos, problemas = [], []
    for categoria, valor in sorted(retidos.items()):
        aliquota_nota = valor / total * 100 if total else Decimal('0')
        candidatos = [
            t for t in tipos
            if t['tp_detalhamento'] in TIPOS_POR_IMPOSTO.get(categoria, set()) and t['vl_percentual'] is not None
            and abs(_centavos(total * _dec(t['vl_percentual']) / 100) - valor) <= TOLERANCIA * 2
        ]
        if not candidatos:
            problemas.append(f'{NOMES_IMPOSTO.get(categoria, categoria)} de R$ {valor:.2f} ({aliquota_nota:.2f}%) '
                             'não corresponde a nenhum imposto cadastrado no MV.')
            continue
        candidatos.sort(key=lambda t: (t['cd_detalhamento'] not in historico, -(t['qt_uso'] or 0), t['cd_detalhamento']))
        escolhido = candidatos[0]
        if escolhido['tp_data'] not in TP_DATA_NA_EMISSAO:
            problemas.append(f'O imposto {escolhido["cd_detalhamento"]} {escolhido["ds_detalhamento"]} tem outra regra de '
                             'vencimento no MV (não é descontado na emissão); lance esta nota pelo MV.')
            continue
        impostos.append({
            'categoria': categoria, 'cd_detalhamento': escolhido['cd_detalhamento'],
            'ds_detalhamento': escolhido['ds_detalhamento'], 'vl_percentual': _dec(escolhido['vl_percentual']),
            'valor': valor, 'do_historico': escolhido['cd_detalhamento'] in historico,
        })
    return sorted(impostos, key=lambda i: i['cd_detalhamento']), problemas


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


def observacao_de_atraso(boletos: list[dict]) -> str | None:
    """Texto da justificativa dos boletos fora do prazo de 3 dias úteis, para o MV."""
    partes = []
    for b in boletos:
        if not b.get('fora_do_prazo'):
            continue
        quem = b.get('enviado_por_nome') or 'setor'
        envio = b.get('enviado_em')
        quando = f' em {envio:%d/%m/%Y %H:%M}' if isinstance(envio, datetime) else ''
        partes.append(f'Venc. {b["vencimento"]:%d/%m/%Y} fora do prazo de 3 dias úteis (enviado por {quem}{quando}). '
                      f'Justificativa: {b.get("justificativa_atraso") or "-"}')
    if not partes:
        return None
    texto = ' | '.join(partes)
    return texto if len(texto) <= OBSERVACAO_MAXIMA else texto[:OBSERVACAO_MAXIMA - 3] + '...'


def _conferir_boletos(sim: Simulacao, nota: dict, boletos: list[dict], hoje: date) -> None:
    if not boletos:
        sim.pendencia('-20002', 'A NFS-e não tem boleto anexado (a importação exige duplicatas).')
        return
    for b in boletos:
        if b.get('divergencias'):
            sim.pendencia('boleto', f'O boleto {b["id"]} tem divergência: {"; ".join(b["divergencias"])}')
        if b['vencimento'] < hoje:
            sim.pendencia('boleto', f'O boleto {b["id"]} venceu em {b["vencimento"]:%d/%m/%Y}.')
        if b.get('fora_do_prazo'):
            if b.get('justificativa_atraso'):
                sim.avisos.append(f'Boleto {b["id"]} enviado fora do prazo de 3 dias úteis. Justificativa: {b["justificativa_atraso"]}')
            else:
                sim.pendencia('prazo', f'O boleto {b["id"]} foi enviado fora do prazo de 3 dias úteis e não tem justificativa.')
    soma = sum((_dec(b['valor']) for b in boletos), Decimal('0'))
    liquido = _dec(nota.get('valor_liquido_nfse'))
    if abs(soma - liquido) > TOLERANCIA:
        sim.pendencia('boleto', f'A soma dos boletos (R$ {soma:.2f}) difere do valor líquido da NFS-e (R$ {liquido:.2f}).')


def _montar_gravacoes(sim: Simulacao, *, nota: dict, boletos: list[dict], modelo: dict, fornecedor: dict,
                      empresa: int, oficina: int, servico: int, usuario: str, agora: datetime,
                      impostos: list[dict] | None = None) -> None:
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
    observacao = observacao_de_atraso(boletos)
    if observacao:
        ent_serv['ds_observacao'] = _campo(observacao, 'justificativa do atraso (boleto fora do prazo)')
    impostos = impostos or []
    total_retido = sum((i['valor'] for i in impostos), Decimal('0'))
    itent = {
        'cd_itent_serv': _campo('SEQ_ITENT_SERV.NEXTVAL', SEQUENCE),
        'cd_ent_serv': _campo('ENT_SERV.CD_ENT_SERV', 'entrada acima'),
        'cd_servico': _campo(servico, origem_modelo),
        'qt_entrada': _campo(Decimal('1'), 'fixo'),
        'qt_atendida': _campo(Decimal('1'), 'fixo'),
        'vl_unitario': _campo(total, 'NFS-e'),
        'vl_total': _campo(total, 'NFS-e'),
        **{c: _campo(Decimal('0'), 'sem desconto/retenção') for c in (
            'vl_perc_desconto', 'vl_desconto', 'vl_desconto_moeda', 'vl_imposto_moeda',
            'vl_perc_imposto', 'vl_imposto_nao_retido', 'vl_inss_nao_retido', 'vl_base_iss', 'vl_imposto_iss_item')},
        'vl_imposto': _campo(total_retido, 'soma dos impostos retidos na NFS-e' if impostos else 'sem retenção'),
    }
    linhas_imposto = [{
        'cd_itent_serv_imposto': _campo('SEQ_ITENT_SERV_IMPOSTO.NEXTVAL', SEQUENCE),
        'cd_itent_serv': _campo('ITENT_SERV.CD_ITENT_SERV', 'item acima'),
        'cd_servico': _campo(servico, origem_modelo),
        'cd_detalhamento': _campo(i['cd_detalhamento'], f'{NOMES_IMPOSTO[i["categoria"]]} retido na NFS-e → {i["ds_detalhamento"]}'),
        'vl_base_calculo': _campo(total, 'valor dos serviços'),
        'vl_percentual_aliquota': _campo(i['vl_percentual'], 'alíquota do imposto no MV'),
        'vl_total_imposto': _campo(i['valor'], 'valor retido na NFS-e'),
        'sn_retem': _campo('S', 'retido'),
        'vl_perc_reducao': _campo(Decimal('0'), 'fixo'),
    } for i in impostos]
    # A duplicata vai pelo valor bruto: a função de importação desconta os impostos da parcela 1.
    duplicatas = [_dec(b['valor']) for b in boletos]
    duplicatas[0] += total_retido
    soma_dup = sum(duplicatas, Decimal('0'))
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
    if observacao:
        doc['observacao'] = _campo(observacao, 'justificativa do atraso (vai para a observação do título)')
    gravacoes = [
        {'tabela': 'ENT_SERV', 'operacao': 'INSERT', 'campos': ent_serv},
        {'tabela': 'ITENT_SERV', 'operacao': 'INSERT', 'campos': itent,
         'observacao': 'A trigger TRG_I_ITENT_SERV preenche a natureza de despesa.'},
        *({'tabela': 'ITENT_SERV_IMPOSTO', 'operacao': 'INSERT', 'campos': campos,
           'observacao': 'A função de importação cria o título do imposto e desconta o valor da parcela 1.'}
          for campos in linhas_imposto),
        {'tabela': 'DOCUMENTO_ENTRADA', 'operacao': 'INSERT', 'campos': doc},
    ]
    for parcela, (b, valor) in enumerate(zip(boletos, duplicatas, strict=True), start=1):
        origem_valor = (f'boleto {b["id"]} + R$ {total_retido:.2f} de impostos retidos (valor bruto)'
                        if parcela == 1 and total_retido else f'boleto {b["id"]}')
        gravacoes.append({'tabela': 'DUPLICATA', 'operacao': 'INSERT', 'campos': {
            'cd_fornecedor': _campo(fornecedor['cd_fornecedor'], 'fornecedor do CNPJ'),
            'nr_documento': _campo(documento, 'NFS-e'),
            'nr_serie': _campo(serie, origem_modelo),
            'cd_parcela': _campo(parcela, 'ordem dos vencimentos'),
            'dt_vencimento': _campo(b['vencimento'], f'boleto {b["id"]}'),
            'vl_parcela': _campo(valor, origem_valor),
        }})
    gravacoes += [
        {'tabela': 'DOCUMENTO_ENTRADA', 'operacao': 'UPDATE', 'campos': {
            'vl_saldo_documento': _campo(total - soma_dup, 'total - duplicatas (conclusão)')}},
        {'tabela': 'ENT_SERV_CONCLUSAO', 'operacao': 'INSERT', 'campos': {
            'cd_ent_serv': _campo('ENT_SERV.CD_ENT_SERV', 'entrada acima'),
            'cd_usuario': _campo(usuario, 'USER da sessão'),
            'dt_conclusao_serv': _campo('SYSDATE', 'hora da importação')},
         'observacao': 'A trigger TRG_ENT_SERV_CONCLUSAO não importa sozinha (SN_IMPORT_AUT_ESTOQUE = N).'},
    ]
    sim.gravacoes = [{'ordem': ordem, **g} for ordem, g in enumerate(gravacoes, start=1)]
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
            'vl_liquido': total - total_retido,
            'impostos': [{'cd_detalhamento': i['cd_detalhamento'], 'ds_detalhamento': i['ds_detalhamento'],
                          'nr_documento': f'D-{documento}'[:15], 'valor': i['valor']} for i in impostos],
        },
    }


def _conferir_impostos(cursor, sim: Simulacao, *, nota: dict, servico: int | None,
                       fornecedor: int, empresa: int) -> list[dict]:
    """Impostos retidos a gravar em ITENT_SERV_IMPOSTO (vazio se a nota não tem retenção)."""
    retido = retencao_da_nota(nota)
    configurados = []
    if servico is not None:
        configurados = linhas(cursor, SQL_DETALHAMENTOS, servico=servico, fornecedor=fornecedor, empresa=empresa)
        sim.escolhas['detalhamentos'] = configurados
    nomes = ', '.join(f'{d["cd_detalhamento"]} {d["ds_detalhamento"]}' for d in configurados)
    if retido <= TOLERANCIA:
        # Sem linhas de imposto na entrada, a função de importação aplica os impostos do fornecedor
        # (FORN_DETALHAMENTO): o título sairia menor que o boleto.
        if any(d['origem'] == 'fornecedor' for d in configurados):
            sim.pendencia('impostos', f'A NFS-e não tem retenção, mas o cadastro do fornecedor no MV retém impostos ({nomes}). '
                                      'Confira o cadastro ou lance pelo MV.')
        elif configurados:
            sim.avisos.append(f'O serviço tem impostos configurados no MV ({nomes}), mas a NFS-e não tem retenção; nada será retido.')
        return []

    retidos, problema = retencoes_da_nfse(nota)
    if problema:
        sim.pendencia('retencao', problema)
        return []
    if 'outras' in retidos:
        sim.pendencia('retencao', f'A NFS-e tem "outras retenções" (R$ {retidos["outras"]:.2f}); lance esta nota pelo MV.')
        return []
    tipos = linhas(cursor, SQL_TIPOS_IMPOSTO)
    historico = {int(linha['cd_detalhamento']) for linha in linhas(cursor, SQL_IMPOSTOS_DO_FORNECEDOR, fornecedor=fornecedor)}
    impostos, problemas = mapear_impostos(retidos, _dec(nota.get('valor_servicos')), tipos, historico)
    for problema in problemas:
        sim.pendencia('retencao', problema)
    sim.escolhas['impostos_retidos'] = impostos
    if configurados:
        sim.avisos.append(f'O cadastro do MV tem impostos para este serviço/fornecedor ({nomes}); '
                          'a importação usa os impostos retidos na NFS-e.')
    novos = [i for i in impostos if not i['do_historico']]
    if novos and historico:
        sim.avisos.append('Imposto que o fornecedor ainda não tinha nos lançamentos anteriores: '
                          + ', '.join(f'{i["cd_detalhamento"]} {i["ds_detalhamento"]}' for i in novos) + '.')
    return impostos if not problemas else []


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

    impostos = _conferir_impostos(cursor, sim, nota=nota, servico=servico,
                                  fornecedor=fornecedor['cd_fornecedor'], empresa=empresa)

    _conferir_boletos(sim, nota, boletos, agora.date())

    if modelo is None or servico is None:
        return sim.as_dict()
    boletos_ordenados = sorted(boletos, key=lambda b: (b['vencimento'], b['id']))
    if boletos_ordenados:
        _montar_gravacoes(sim, nota=nota, boletos=boletos_ordenados, modelo=modelo, fornecedor=fornecedor,
                          empresa=empresa, oficina=oficina, servico=servico, usuario=usuario, agora=agora,
                          impostos=impostos)
        _conferir_obrigatorios(cursor, sim)
    return sim.as_dict()


# --------------------------------------------------------------- gravação

# Campos que mudam a cada simulação (hora) não entram na assinatura.
CAMPOS_FORA_DA_ASSINATURA = {'hr_entrada', 'hora_status_sped_terc', 'dt_entrada', 'data_status_sped_terc'}
RE_COLUNA = re.compile(r'^[a-z][a-z0-9_]{0,29}$')
SQL_IMPORTAR = """
declare
  v_ok boolean;
begin
  v_ok := dbamv.fnc_importa_mges_fnfi_serv(:fornecedor, :documento, :serie, :empresa);
  :resultado := case when v_ok then 1 else 0 end;
end;"""
SQL_TITULO = """/* rc:titulo */
select cd_con_pag from dbamv.con_pag
 where cd_fornecedor = :fornecedor and nr_documento = :documento
   and nr_serie = :serie and cd_multi_empresa = :empresa"""
SQL_INDICADORES = """/* rc:indicadores */
select sn_importada_fnfi, cd_con_pag from dbamv.ent_serv where cd_ent_serv = :ent_serv"""
SQL_PARCELAS_DO_TITULO = """/* rc:parcelas_titulo */
select count(*) as qt_parcelas, sum(vl_duplicata) as vl_parcelas from dbamv.itcon_pag where cd_con_pag = :con_pag"""
SQL_TITULOS_DE_IMPOSTO = """/* rc:titulos_imposto */
select cd_detalhamento, cd_con_pag_filho from dbamv.tip_detcon_pag where cd_con_pag_pai = :con_pag"""


class ImportacaoFalhou(RuntimeError):
    """Erro na gravação; a transação deve ser desfeita por quem chamou."""


def assinatura(gravacoes: list[dict], importacao: dict) -> str:
    """Resumo do que será gravado; o financeiro confirma exatamente o que viu."""
    conteudo = [
        [g['tabela'], g['operacao'],
         sorted((c, str(v['valor'])) for c, v in g['campos'].items() if c not in CAMPOS_FORA_DA_ASSINATURA)]
        for g in sorted(gravacoes, key=lambda g: g['ordem'])
    ]
    conteudo.append(sorted((k, str(v)) for k, v in (importacao.get('parametros') or {}).items()))
    return hashlib.sha256(json.dumps(conteudo, ensure_ascii=False).encode()).hexdigest()[:20]


def _nextval(cursor, sequence: str) -> int:
    cursor.execute(f'select dbamv.{sequence}.nextval from dual')
    return int(cursor.fetchall()[0][0])


def _bind(valor, ids: dict):
    return {
        'SEQ_ENT_SERV.NEXTVAL': ids['ent_serv'],
        'ENT_SERV.CD_ENT_SERV': ids['ent_serv'],
        'SEQ_ITENT_SERV.NEXTVAL': ids['itent_serv'],
        'ITENT_SERV.CD_ITENT_SERV': ids['itent_serv'],
        'SEQ_ITENT_SERV_IMPOSTO.NEXTVAL': ids.get('itent_serv_imposto'),
    }.get(valor, valor) if isinstance(valor, str) else valor


def _insert(cursor, tabela: str, campos: dict, ids: dict) -> None:
    colunas, valores, binds = [], [], {}
    for coluna, dado in campos.items():
        if dado['valor'] is None:
            continue
        if not RE_COLUNA.match(coluna):
            raise ImportacaoFalhou(f'Coluna inválida no plano: {coluna}')
        colunas.append(coluna)
        if dado['valor'] == 'SYSDATE':
            valores.append('sysdate')
        else:
            valores.append(f':{coluna}')
            binds[coluna] = _bind(dado['valor'], ids)
    cursor.execute(f'insert into dbamv.{tabela.lower()} ({", ".join(colunas)}) values ({", ".join(valores)})', binds)


def executar_importacao(cursor, resultado: dict) -> dict:
    """Grava o plano da simulação e chama FNC_IMPORTA_MGES_FNFI_SERV.

    Não faz commit nem rollback: a transação é de quem chamou.
    """
    if not resultado.get('apta'):
        raise ImportacaoFalhou('A nota não está apta para importar.')
    parametros = resultado['importacao']['parametros']
    chave = {'fornecedor': parametros['p_cd_fornecedor'], 'documento': parametros['p_nr_documento'],
             'serie': parametros['p_nr_serie'], 'empresa': parametros['p_cd_multi_empresa']}
    ids = {'ent_serv': _nextval(cursor, 'seq_ent_serv'), 'itent_serv': _nextval(cursor, 'seq_itent_serv')}
    for g in sorted(resultado['gravacoes'], key=lambda g: g['ordem']):
        if g['tabela'] not in TABELAS_GRAVADAS:
            raise ImportacaoFalhou(f'Tabela fora do escopo: {g["tabela"]}')
        if g['operacao'] == 'INSERT':
            if g['tabela'] == 'ITENT_SERV_IMPOSTO':
                ids['itent_serv_imposto'] = _nextval(cursor, 'seq_itent_serv_imposto')
                ids.setdefault('itent_serv_impostos', []).append(ids['itent_serv_imposto'])
            _insert(cursor, g['tabela'], g['campos'], ids)
        elif g['operacao'] == 'UPDATE' and g['tabela'] == 'DOCUMENTO_ENTRADA':
            cursor.execute(
                'update dbamv.documento_entrada set vl_saldo_documento = :saldo'
                ' where cd_fornecedor = :fornecedor and nr_documento = :documento'
                ' and nr_serie = :serie and cd_multi_empresa = :empresa',
                {'saldo': g['campos']['vl_saldo_documento']['valor'], **chave})
            if cursor.rowcount != 1:
                raise ImportacaoFalhou(f'O documento de entrada não foi encontrado para atualizar ({cursor.rowcount} linhas).')
        else:
            raise ImportacaoFalhou(f'Operação fora do escopo: {g["operacao"]} {g["tabela"]}')

    retorno = cursor.var(int)
    cursor.execute(SQL_IMPORTAR, {**chave, 'resultado': retorno})
    if retorno.getvalue() != 1:
        raise ImportacaoFalhou('FNC_IMPORTA_MGES_FNFI_SERV retornou falso.')
    titulos = linhas(cursor, SQL_TITULO, **chave)
    if len(titulos) != 1:
        raise ImportacaoFalhou(f'Esperado 1 título no Contas a Pagar, encontrados {len(titulos)}.')
    indicadores = linhas(cursor, SQL_INDICADORES, ent_serv=ids['ent_serv'])
    if not indicadores or indicadores[0]['sn_importada_fnfi'] != 'S':
        raise ImportacaoFalhou('A entrada não ficou marcada como importada.')
    cd_con_pag = int(titulos[0]['cd_con_pag'])
    _conferir_titulo(cursor, resultado, cd_con_pag)
    return {'ent_serv': ids['ent_serv'], 'itent_serv': ids['itent_serv'], 'cd_con_pag': cd_con_pag}


def _conferir_titulo(cursor, resultado: dict, cd_con_pag: int) -> None:
    """O título gerado precisa fechar com os boletos e ter um título filho por imposto retido."""
    previsto = resultado['importacao']['titulo_previsto']
    esperado = sum((_dec(p['vl_duplicata']) for p in previsto['parcelas']), Decimal('0'))
    parcelas = linhas(cursor, SQL_PARCELAS_DO_TITULO, con_pag=cd_con_pag)[0]
    if int(parcelas['qt_parcelas'] or 0) != len(previsto['parcelas']) or abs(_dec(parcelas['vl_parcelas']) - esperado) > TOLERANCIA:
        raise ImportacaoFalhou(
            f'As parcelas do título somam R$ {_dec(parcelas["vl_parcelas"]):.2f} em {parcelas["qt_parcelas"]} parcela(s); '
            f'os boletos somam R$ {esperado:.2f} em {len(previsto["parcelas"])}.')
    impostos = {int(i['cd_detalhamento']) for i in previsto.get('impostos') or []}
    filhos = {int(f['cd_detalhamento']) for f in linhas(cursor, SQL_TITULOS_DE_IMPOSTO, con_pag=cd_con_pag)}
    if filhos != impostos:
        raise ImportacaoFalhou(f'Títulos de imposto gerados {sorted(filhos)} diferentes dos retidos na NFS-e {sorted(impostos)}.')
