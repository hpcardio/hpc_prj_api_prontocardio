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

from collections import defaultdict
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
            'data_protocolo': None,
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
