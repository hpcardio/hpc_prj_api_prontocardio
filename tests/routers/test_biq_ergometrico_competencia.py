from datetime import date, datetime

from app_prontocardio.routers import biq


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
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        return Resultado(self.rows)


def test_competencia_faturamento_retorna_exame_clinico_de_mes_anterior():
    sessao = Sessao([{
        "cd_atendimento": 350861,
        "dt_atendimento": datetime(2026, 7, 30, 9, 0),
        "nm_paciente": "BEATRIZ DUTRA MORAIS",
        "cd_prestador": 1187,
        "nm_prestador": "BEATRIZ JARDIM DE SIQUEIRA BRANCO",
        "crm": "30097",
        "cd_repasse_consolidado": 1723743,
        "cd_remessa": 19618,
        "competencia_faturamento": date(2026, 8, 1),
    }])

    payload = biq.consultar_teste_ergometrico(
        usuario_atual=object(),
        data_inicio=date(2026, 8, 1),
        data_fim=date(2026, 9, 1),
        competencia_faturamento=date(2026, 8, 1),
        session=sessao,
    )

    assert payload["exames"][0]["nm_paciente"] == "BEATRIZ DUTRA MORAIS"
    assert payload["exames"][0]["dt_atendimento"].startswith("2026-07-30")
    assert payload["exames"][0]["competencia_faturamento"] == "2026-08-01"
    assert len(sessao.calls) == 2
    assert sessao.calls[0][1] == {
        "data_inicio": date(2026, 8, 1),
        "data_fim": date(2026, 9, 1),
    }


def test_consulta_por_competencia_nao_usa_valor_do_repasse():
    sql = str(biq.CONSULTA_TESTE_ERGOMETRICO_POR_COMPETENCIA).upper()

    assert "REPASSE_CONSOLIDADO" in sql
    assert "DT_COMPETENCIA" in sql
    assert "VL_REPASSE" not in sql


def test_sem_competencia_preserva_consultas_e_lista_vazia():
    sessao = Sessao([])
    payload = biq.consultar_teste_ergometrico(
        usuario_atual=object(), data_inicio=date(2026, 8, 1),
        data_fim=date(2026, 9, 1), session=sessao,
    )
    assert payload['divergencias'] == []
    assert payload['resumo']['divergencias_faturamento_sem_laudo'] == 0
    assert len(sessao.calls) == 5


def test_divergencias_nao_aumentam_exames_conciliados():
    class Separada(Sessao):
        def execute(self, statement, params=None):
            from app_prontocardio.services.ergometrico_divergencias import CONSULTA_DIVERGENCIAS
            self.calls.append((statement, params))
            return Resultado([{'cd_atendimento': 1, 'motivo_divergencia': 'faturado_sem_laudo'}]
                             if statement is CONSULTA_DIVERGENCIAS else [])

    sessao = Separada([])
    payload = biq.consultar_teste_ergometrico(
        usuario_atual=object(), data_inicio=date(2026, 8, 1),
        data_fim=date(2026, 9, 1), competencia_faturamento=date(2026, 8, 1),
        session=sessao,
    )
    assert payload['total'] == 0
    assert payload['resumo']['conciliados'] == 0
    assert payload['resumo']['divergencias_faturamento_sem_laudo'] == 1
    assert payload['divergencias'][0]['cd_atendimento'] == 1
