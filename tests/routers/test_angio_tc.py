from datetime import date

from app_prontocardio.routers import angio_tc

CODIGO_REPASSE = 10


class Resultado:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Sessao:
    def __init__(self, rows):
        self.rows = rows
        self.executions = 0

    def execute(self, _statement, _params=None):
        self.executions += 1
        return Resultado(self.rows)


def test_montar_lancamentos_filtra_apenas_angio_tc_e_prestadores_dedicados():
    elegivel = {
        'cd_repasse_consolidado': CODIGO_REPASSE,
        'cd_prestador_destino': 245,
        'nm_prestador_destino': 'MEDICO TESTE',
        'cd_atendimento': 20,
        'nm_paciente': 'PACIENTE TESTE',
        'cd_pro_fat': '41001230',
        'vl_total_conta': '123.45',
    }
    outro_procedimento = {**elegivel, 'cd_pro_fat': '99999999'}
    outro_prestador = {**elegivel, 'cd_prestador_destino': 999}

    resultado = angio_tc.montar_lancamentos(
        Sessao([elegivel, outro_procedimento, outro_prestador]),
        date(2026, 9, 15),
        empresa=1,
    )

    assert len(resultado['itens']) == 1
    assert (
        resultado['itens'][0]['codigo_repasse_consolidado']
        == CODIGO_REPASSE
    )
    assert resultado['itens'][0]['valor_faturado'] == '123.45'


def test_montar_lancamentos_nao_consulta_competencia_anterior_ao_inicio():
    session = Sessao([])

    resultado = angio_tc.montar_lancamentos(
        session, date(2026, 6, 1), empresa=1
    )

    assert resultado['itens'] == []
    assert session.executions == 0
