"""Fluxo do Contas a Receber: remessa -> protocolo -> NFS -> recebimento.

Regras puras (sem banco), usadas pela rota ``contas_receber``:

- cada remessa fechada do MV percorre quatro etapas; a etapa atual é a
  primeira ainda não concluída;
- o protocolo vem do vínculo gravado no Receita Certa ou, no ISSEC, do
  casamento automático com o portal (mês de produção + valor cobrado =
  competência + valor da remessa). Só vira "automático" quando o par é único
  nos dois sentidos; senão fica como sugestão "a confirmar";
- a NFS e o recebimento vêm dos vínculos gravados.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

CENTAVO = Decimal('0.01')

ETAPAS = (
    ('remessa', 'Remessa fechada no MV'),
    ('protocolo', 'Protocolada no convênio'),
    ('nfse', 'NFS emitida'),
    ('recebimento', 'Recebimento financeiro'),
)
CONCLUIDO = 'concluido'
ROTULOS = dict(ETAPAS) | {CONCLUIDO: 'Fluxo concluído'}
# Etapa atual -> etapa que falta (o que o operador precisa fazer).
PENDENCIA = {
    'protocolo': 'Aguardando protocolo',
    'nfse': 'Aguardando NFS',
    'recebimento': 'Aguardando recebimento',
    CONCLUIDO: 'Concluído',
}


def dinheiro(valor) -> Decimal:
    return Decimal(valor or 0).quantize(CENTAVO)


@dataclass(frozen=True)
class ProcessoPortal:
    numero: str
    competencia: date
    valor_cobrado: Decimal
    valor_liberado: Decimal | None
    data_entrada: date | None = None
    situacao: str | None = None


def casar_portal(remessas, processos, remessas_vinculadas, numeros_vinculados):
    """Sugestões de protocolo do portal para as remessas ainda sem vínculo.

    ``remessas``: dicts com cd_remessa, competencia (date, dia 1) e valor.
    Retorna {cd_remessa: {'situacao': 'automatico'|'a_confirmar',
    'candidatos': [ProcessoPortal, ...]}}.
    """
    por_chave = defaultdict(list)
    for r in remessas:
        if r['cd_remessa'] in remessas_vinculadas:
            continue
        por_chave[(r['competencia'], dinheiro(r['valor']))].append(
            r['cd_remessa']
        )
    candidatos_da_remessa = defaultdict(list)
    remessas_do_processo = {}
    for p in processos:
        if p.numero in numeros_vinculados:
            continue
        alvo = por_chave.get((p.competencia, dinheiro(p.valor_cobrado)), [])
        remessas_do_processo[p.numero] = alvo
        for cd in alvo:
            candidatos_da_remessa[cd].append(p)
    sugestoes = {}
    for cd, procs in candidatos_da_remessa.items():
        unico = (
            len(procs) == 1 and len(remessas_do_processo[procs[0].numero]) == 1
        )
        sugestoes[cd] = {
            'situacao': 'automatico' if unico else 'a_confirmar',
            'candidatos': sorted(procs, key=lambda p: p.numero),
        }
    return sugestoes


def _dias(desde: date | None, hoje: date) -> int | None:
    return (hoje - desde).days if desde else None


def montar_item(remessa, vinculo, sugestao, hoje):
    """Uma remessa com a situação de cada etapa.

    ``vinculo``: dados gravados (protocolo, nfse, recebimentos) ou None.
    """
    vinculo = vinculo or {}
    protocolo = vinculo.get('protocolo')
    if protocolo is None and sugestao and sugestao['situacao'] == 'automatico':
        p = sugestao['candidatos'][0]
        protocolo = {
            'id': None,
            'numero': p.numero,
            'origem': 'portal',
            'situacao': 'automatico',
            'valor_cobrado': p.valor_cobrado,
            'valor_liberado': p.valor_liberado,
            'data_protocolo': p.data_entrada,
            'situacao_portal': p.situacao,
            'solicitacao_nota_em': None,
            'solicitacao_nota_obs': None,
        }
    candidatos = []
    if protocolo is None and sugestao:
        candidatos = [
            {
                'numero': p.numero,
                'valor_cobrado': p.valor_cobrado,
                'valor_liberado': p.valor_liberado,
            }
            for p in sugestao['candidatos']
        ]
    nfse = vinculo.get('nfse') if protocolo else None
    recebimentos = (vinculo.get('recebimentos') or []) if nfse else []

    fechamento = remessa.get('dt_fechamento')
    data_protocolo = protocolo.get('data_protocolo') if protocolo else None
    data_nfse = nfse.get('data_emissao') if nfse else None
    data_receb = max((r['data'] for r in recebimentos), default=None)
    concluidas = {
        'remessa': True,
        'protocolo': protocolo is not None,
        'nfse': nfse is not None,
        'recebimento': bool(recebimentos),
    }
    datas = {
        'remessa': fechamento,
        'protocolo': data_protocolo,
        'nfse': data_nfse,
        'recebimento': data_receb,
    }
    atual = next((k for k, _ in ETAPAS if not concluidas[k]), CONCLUIDO)
    # Dias parados: desde a última data conhecida das etapas concluídas.
    ultima = None
    for chave, _ in ETAPAS:
        if concluidas[chave] and datas[chave]:
            ultima = datas[chave]
    etapas = []
    for chave, rotulo in ETAPAS:
        if concluidas[chave]:
            status = 'concluida'
        elif chave == atual:
            status = 'atual'
        else:
            status = 'futura'
        etapas.append({
            'chave': chave,
            'rotulo': rotulo,
            'status': status,
            'data': datas[chave],
        })
    return {
        'cd_remessa': remessa['cd_remessa'],
        'cd_convenio': remessa['cd_convenio'],
        'convenio': remessa['convenio'],
        'competencia': remessa['competencia'],
        'valor': dinheiro(remessa['valor']),
        'qt_contas': remessa.get('qt_contas') or 0,
        'dt_fechamento': fechamento,
        'dt_entrega': remessa.get('dt_entrega'),
        'dt_prevista_pgto': remessa.get('dt_prevista_pgto'),
        'protocolo': protocolo,
        'protocolo_candidatos': candidatos,
        'nfse': nfse,
        'recebimentos': recebimentos,
        'valor_recebido': sum(
            (
                dinheiro(r.get('valor_alocado') or r['valor'])
                for r in recebimentos
            ),
            Decimal('0.00'),
        ),
        'etapa_atual': atual,
        'pendencia': PENDENCIA.get(atual, ''),
        'etapas': etapas,
        'dias_na_etapa': _dias(ultima, hoje) if atual != CONCLUIDO else None,
        'solicitacao_nota': bool(
            protocolo and protocolo.get('solicitacao_nota_em')
        ),
    }


# Retenção estimada: alíquota (valor da NF - líquido) / valor mais frequente
# nas NFs já vinculadas do convênio. Só vale com amostra e consenso suficientes
# (no ISSEC, 433 de 440 NFs retiveram exatamente 4,20%).
MINIMO_NFS_ALIQUOTA = 5
CONSENSO_ALIQUOTA = Decimal('0.8')


def aliquotas_por_convenio(itens):
    amostras = defaultdict(list)
    for item in itens:
        nf = item.get('nfse') or {}
        valor, liquido = nf.get('valor'), nf.get('valor_liquido')
        if valor and liquido is not None and valor > 0:
            amostras[item['cd_convenio']].append(
                ((valor - liquido) / valor * 100).quantize(CENTAVO)
            )
    aliquotas = {}
    for convenio, taxas in amostras.items():
        if len(taxas) < MINIMO_NFS_ALIQUOTA:
            continue
        taxa, vezes = Counter(taxas).most_common(1)[0]
        if Decimal(vezes) / len(taxas) >= CONSENSO_ALIQUOTA:
            aliquotas[convenio] = taxa
    return aliquotas


def bases_das_remessas(itens):
    """{cd_remessa: (base, origem)}: quanto do liberado cabe a cada remessa.

    O portal libera por processo, e um processo pode reunir várias remessas
    (ex.: R$ 42.450,00 para 12 remessas). A base aplica à remessa a proporção
    liberada do processo: valor da remessa x (liberado / cobrado). Com uma
    remessa só, é o próprio liberado. Sem portal, a base é o valor bruto.
    """
    bases = {}
    for item in itens:
        protocolo = item.get('protocolo') or {}
        liberado = protocolo.get('valor_liberado')
        cobrado = protocolo.get('valor_cobrado')
        if liberado is None or not cobrado or dinheiro(cobrado) <= 0:
            bases[item['cd_remessa']] = (item['valor'], 'bruto')
            continue
        liberado, cobrado = dinheiro(liberado), dinheiro(cobrado)
        if abs(item['valor'] - cobrado) <= CENTAVO:
            bases[item['cd_remessa']] = (liberado, 'liberado')
        else:
            base = (item['valor'] * liberado / cobrado).quantize(CENTAVO)
            bases[item['cd_remessa']] = (base, 'liberado_proporcional')
    return bases


def aplicar_estimativa(itens, aliquotas, bases=None):
    """Retenções e líquido estimados para remessas ainda sem NF no ISS.

    Base: parte do valor liberado no portal que cabe à remessa ou, sem ele,
    o valor bruto (``bases_das_remessas``).
    """
    bases = bases if bases is not None else bases_das_remessas(itens)
    for item in itens:
        nf = item.get('nfse') or {}
        aliquota = aliquotas.get(item['cd_convenio'])
        if nf.get('valor_liquido') is not None or aliquota is None:
            item['estimativa'] = None
            continue
        base, origem = bases[item['cd_remessa']]
        retencoes = (base * aliquota / 100).quantize(CENTAVO)
        item['estimativa'] = {
            'aliquota': aliquota,
            'base': base,
            'origem_base': origem,
            'retencoes': retencoes,
            'liquido': base - retencoes,
        }
    return itens


def _chave_nf(nf):
    return (str(nf.get('numero')), nf.get('prestador_cnpj') or '')


def ratear_nfs(itens, bases=None):
    """Parte da NF que cabe a cada remessa (``nf_rateio``).

    NF exclusiva da remessa: valores da própria NF. NF compartilhada (NF de
    grupo, ex.: ISSEC com NUP de grupo cobrindo vários processos): a parte da
    remessa é o valor liberado no portal ou, sem ele, o bruto da remessa; as
    retenções aplicam a alíquota real da NF ((valor - líquido) / valor) sobre
    essa parte. Mesmo critério da planilha DEMO.
    """
    bases = bases if bases is not None else bases_das_remessas(itens)
    remessas_por_nf = Counter(
        _chave_nf(i['nfse'])
        for i in itens
        if (i.get('nfse') or {}).get('numero')
    )
    for item in itens:
        nf = item.get('nfse') or {}
        if not nf.get('numero'):
            item['nf_rateio'] = None
            continue
        valor, liquido = nf.get('valor'), nf.get('valor_liquido')
        quantidade = remessas_por_nf[_chave_nf(nf)]
        aliquota = (
            ((valor - liquido) / valor * 100).quantize(CENTAVO)
            if valor and liquido is not None and valor > 0
            else None
        )
        compartilhada = quantidade > 1 or (
            valor is not None and valor > item['valor'] + CENTAVO
        )
        if not compartilhada:
            item['nf_rateio'] = {
                'compartilhada': False,
                'valor': valor,
                'retencoes': valor - liquido if aliquota is not None else None,
                'liquido': liquido,
                'aliquota': aliquota,
            }
            continue
        base, origem = bases[item['cd_remessa']]
        retencoes = (
            (base * aliquota / 100).quantize(CENTAVO)
            if aliquota is not None
            else None
        )
        item['nf_rateio'] = {
            'compartilhada': True,
            'remessas_na_nf': quantidade,
            'valor_total_nf': valor,
            'origem_base': origem,
            'valor': base,
            'retencoes': retencoes,
            'liquido': base - retencoes if retencoes is not None else None,
            'aliquota': aliquota,
        }
    return itens


def resumo_funil(itens):
    """Quantidade e valor parados em cada etapa (para os cards do topo)."""
    chaves = [k for k, _ in ETAPAS[1:]] + [CONCLUIDO]
    resumo = {
        k: {
            'chave': k,
            'rotulo': PENDENCIA[k],
            'quantidade': 0,
            'valor': Decimal('0.00'),
        }
        for k in chaves
    }
    a_confirmar = 0
    for item in itens:
        r = resumo[item['etapa_atual']]
        r['quantidade'] += 1
        r['valor'] += item['valor']
        if item['protocolo_candidatos']:
            a_confirmar += 1
    return {
        'etapas': [resumo[k] for k in chaves],
        'total_remessas': len(itens),
        'valor_total': sum((i['valor'] for i in itens), Decimal('0.00')),
        'protocolos_a_confirmar': a_confirmar,
    }
