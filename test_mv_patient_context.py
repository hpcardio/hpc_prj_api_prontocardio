from datetime import datetime

from evolucao_sadt_backend.patient_context import patient_identification


class _Mappings:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return _Mappings(self.row)


class _Session:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _Result(self.row)


def test_patient_context_returns_optional_read_only_mv_fields():
    result = patient_identification(_Session({
        'nome_paciente': 'PACIENTE FICTICIO',
        'data_nascimento': datetime(1960, 1, 2),
        'sexo': 'M',
        'prontuario': 12345,
        'data_atendimento': datetime(2026, 11, 7, 8, 30),
    }), 987654)
    assert result == {
        'nome_paciente': 'PACIENTE FICTICIO',
        'idade': 66,
        'sexo': 'M',
        'prontuario': '12345',
        'data_atendimento': datetime(2026, 11, 7, 8, 30),
    }


def test_patient_context_tolerates_missing_optional_fields():
    assert patient_identification(_Session(None), 987654) == {}
