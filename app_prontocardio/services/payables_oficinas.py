# ruff: noqa: E501
"""Regra fornecedor (CNPJ) + empresa -> oficina do MV para a fila de NFS-e.

A oficina de cada fornecedor é sugerida pelo histórico de entradas de serviço
(``DBAMV.ENT_SERV``), separado por empresa (``OFICINA.CD_MULTI_EMPRESA``),
porque cada empresa do grupo tem as próprias oficinas. Oficinas com o mesmo
nome na mesma empresa (ex.: três "CENTRAL DE RECEBIMENTO FISCAL" na empresa 2)
contam como um único setor; a regra usa o código mais usado entre elas.

Quando o histórico não permite decidir com segurança, o fornecedor fica
"a classificar" e o financeiro escolhe a oficina na tela.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

STATUS_DEFINIDA = 'definida'
STATUS_A_CLASSIFICAR = 'a_classificar'
ORIGEM_HISTORICO = 'historico'
ORIGEM_MANUAL = 'manual'

# Participação mínima do setor principal nas entradas do fornecedor para a
# regra ser definida automaticamente.
PARTICIPACAO_MINIMA = Decimal('0.80')
MESES_HISTORICO = 24
MAX_SETORES_NO_MOTIVO = 3
TAMANHO_CNPJ = 14


def somente_digitos(valor) -> str:
    return re.sub(r'\D', '', str(valor or ''))


def nome_setor(ds_oficina) -> str:
    return re.sub(r'\s+', ' ', str(ds_oficina or '')).strip().upper()


@dataclass(frozen=True)
class RegraSugerida:
    cnpj: str
    cd_multi_empresa: int
    cd_fornecedor: int | None
    nm_fornecedor: str | None
    cd_oficina: int | None
    ds_oficina: str | None
    status: str
    qtd_entradas: int
    participacao: Decimal | None
    motivo: str

    def as_dict(self) -> dict:
        return {
            'cnpj': self.cnpj,
            'cd_multi_empresa': self.cd_multi_empresa,
            'cd_fornecedor': self.cd_fornecedor,
            'nm_fornecedor': self.nm_fornecedor,
            'cd_oficina': self.cd_oficina,
            'ds_oficina': self.ds_oficina,
            'status': self.status,
            'origem': ORIGEM_HISTORICO,
            'qtd_entradas': self.qtd_entradas,
            'participacao': self.participacao,
            'motivo': self.motivo,
        }


def _percentual(valor: Decimal) -> str:
    return f'{(valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)}%'


def sugerir_regras(
    historico,
    participacao_minima: Decimal = PARTICIPACAO_MINIMA,
) -> list[RegraSugerida]:
    """Decide a oficina de cada (CNPJ do fornecedor, empresa).

    ``historico``: linhas com ``cnpj``, ``cd_fornecedor``, ``nm_fornecedor``,
    ``cd_multi_empresa``, ``cd_oficina``, ``ds_oficina`` e ``qtd``. Um CNPJ
    pode ter mais de um ``cd_fornecedor`` (cadastros duplicados no MV); as
    entradas de todos são somadas.
    """
    # (cnpj, empresa) -> setor (nome) -> {'qtd', 'oficinas': {cd: qtd}, 'ds'}
    setores: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    fornecedores: dict[tuple[str, int], tuple[int | None, str | None, int]] = {}

    for linha in historico:
        cnpj = somente_digitos(linha['cnpj'])
        qtd = int(linha['qtd'] or 0)
        if (
            len(cnpj) != TAMANHO_CNPJ
            or qtd <= 0
            or linha['cd_oficina'] is None
            or linha['cd_multi_empresa'] is None
        ):
            continue
        chave = (cnpj, int(linha['cd_multi_empresa']))
        setor = setores[chave].setdefault(
            nome_setor(linha['ds_oficina']),
            {'qtd': 0, 'oficinas': defaultdict(int), 'ds': linha['ds_oficina']},
        )
        setor['qtd'] += qtd
        setor['oficinas'][int(linha['cd_oficina'])] += qtd
        atual = fornecedores.get(chave)
        if atual is None or qtd > atual[2]:
            fornecedores[chave] = (linha['cd_fornecedor'], linha['nm_fornecedor'], qtd)

    regras = []
    for chave, por_setor in sorted(setores.items()):
        cnpj, empresa = chave
        total = sum(s['qtd'] for s in por_setor.values())
        ordenados = sorted(por_setor.values(), key=lambda s: (-s['qtd'], s['ds'] or ''))
        principal = ordenados[0]
        participacao = Decimal(principal['qtd']) / Decimal(total)
        empate = len(ordenados) > 1 and ordenados[1]['qtd'] == principal['qtd']
        cd_fornecedor, nm_fornecedor, _ = fornecedores[chave]

        if participacao >= participacao_minima and not empate:
            cd_oficina = sorted(
                principal['oficinas'].items(), key=lambda item: (-item[1], item[0])
            )[0][0]
            regras.append(
                RegraSugerida(
                    cnpj=cnpj,
                    cd_multi_empresa=empresa,
                    cd_fornecedor=cd_fornecedor,
                    nm_fornecedor=nm_fornecedor,
                    cd_oficina=cd_oficina,
                    ds_oficina=principal['ds'],
                    status=STATUS_DEFINIDA,
                    qtd_entradas=total,
                    participacao=participacao,
                    motivo=f'{principal["qtd"]} de {total} entradas ({_percentual(participacao)}) neste setor',
                )
            )
            continue

        partes = ', '.join(
            f'{s["ds"]} {_percentual(Decimal(s["qtd"]) / total)}'
            for s in ordenados[:MAX_SETORES_NO_MOTIVO]
        )
        regras.append(
            RegraSugerida(
                cnpj=cnpj,
                cd_multi_empresa=empresa,
                cd_fornecedor=cd_fornecedor,
                nm_fornecedor=nm_fornecedor,
                cd_oficina=None,
                ds_oficina=None,
                status=STATUS_A_CLASSIFICAR,
                qtd_entradas=total,
                participacao=participacao,
                motivo=f'Histórico dividido entre setores: {partes}',
            )
        )
    return regras


def situacao_da_nota(regra: dict | None, empresa: int | None) -> tuple[str, str]:
    """Situação de uma NFS-e recebida na fila conforme a regra do fornecedor."""
    if empresa is None:
        return STATUS_A_CLASSIFICAR, 'Tomador da nota não é uma empresa cadastrada no MV'
    if regra is None:
        return STATUS_A_CLASSIFICAR, 'Fornecedor sem entradas de serviço nesta empresa no histórico do MV'
    if regra['status'] != STATUS_DEFINIDA or regra.get('cd_oficina') is None:
        return STATUS_A_CLASSIFICAR, regra.get('motivo') or 'Oficina não definida'
    return STATUS_DEFINIDA, regra.get('motivo') or ''
