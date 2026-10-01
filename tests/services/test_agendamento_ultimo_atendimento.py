from fastapi import HTTPException

from app_prontocardio.models import Usuario
from app_prontocardio.services import agendamento_ultimo_atendimento as service


class FakeMappings:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class FakeResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return FakeMappings(self.row)


class FakeOracle:
    def __init__(self, row):
        self.row = row
        self.statement = None
        self.params = None

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return FakeResult(self.row)


class FakePostgres:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1


def make_user(*, permission=True):
    permissions = [service.PERMISSAO_ULTIMO_ATENDIMENTO] if permission else []
    return Usuario(
        nome="INTEGRACAO TESTE",
        email="integracao@example.invalid",
        senha="nao-utilizada",
        perfil="integracao",
        ativo=True,
        origem_agendamento="INTEGRACAO",
        telas_permitidas=permissions,
    )


def test_permission_is_exclusive():
    allowed = make_user(permission=True)
    assert service.exigir_permissao_ultimo_atendimento(allowed) is allowed

    try:
        service.exigir_permissao_ultimo_atendimento(
            make_user(permission=False)
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Credencial sem permissão foi aceita.")


def test_returns_last_effective_medical_attendance_and_audits():
    oracle = FakeOracle(
        {
            "cd_atendimento": 987,
            "data_atendimento": "2026-09-10T08:30:00",
            "cd_especialidade": 7,
            "especialidade": "CARDIOLOGIA",
            "cd_prestador": 321,
            "medico": "MEDICO TESTE",
            "cd_it_agenda_central": 654,
            "cd_agenda_central": 456,
        }
    )
    postgres = FakePostgres()

    result = service.consultar_ultimo_atendimento_procedimento(
        oracle=oracle,
        postgres=postgres,
        usuario=make_user(),
        cd_paciente=123,
        cd_item_agendamento=42,
    )

    assert result == {
        "encontrado": True,
        "cd_atendimento": 987,
        "data_atendimento": "2026-09-10T08:30:00",
        "cd_especialidade": 7,
        "especialidade": "CARDIOLOGIA",
        "cd_prestador": 321,
        "medico": "MEDICO TESTE",
    }
    assert oracle.params == {
        "cd_paciente": 123,
        "cd_item_agendamento": 42,
    }
    assert "a.TP_ATENDIMENTO = 'A'" in oracle.statement
    assert "a.DT_ALTA_MEDICA IS NOT NULL" in oracle.statement
    assert "a.HR_ALTA_MEDICA IS NOT NULL" in oracle.statement
    assert "a.HR_ATENDIMENTO_MEDICO IS NOT NULL" not in oracle.statement
    assert "a.CD_PACIENTE = :cd_paciente" in oracle.statement
    assert "i.CD_ITEM_AGENDAMENTO = :cd_item_agendamento" in oracle.statement
    assert postgres.commits == 1
    assert len(postgres.added) == 1
    assert postgres.added[0].status == "ultimo_atendimento_encontrado"


def test_returns_not_found_without_exposing_history_and_audits():
    oracle = FakeOracle(None)
    postgres = FakePostgres()

    result = service.consultar_ultimo_atendimento_procedimento(
        oracle=oracle,
        postgres=postgres,
        usuario=make_user(),
        cd_paciente=123,
        cd_item_agendamento=42,
    )

    assert result == {"encontrado": False}
    assert postgres.commits == 1
    assert len(postgres.added) == 1
    assert postgres.added[0].status == "ultimo_atendimento_ausente"


if __name__ == "__main__":
    test_permission_is_exclusive()
    test_returns_last_effective_medical_attendance_and_audits()
    test_returns_not_found_without_exposing_history_and_audits()
    print("ultimo atendimento tests: ok")
