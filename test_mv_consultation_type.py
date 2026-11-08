from evolucao_sadt_backend import care_support


class ReadOnlyGuardSession:
    def __init__(self):
        self.statements = []

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement).strip().upper()
        self.statements.append(sql)
        assert sql.startswith('SELECT')
        raise AssertionError('Nenhuma fonte MV foi comprovada para consulta automática.')


def test_unverified_mv_source_keeps_consultation_type_manual():
    session = ReadOnlyGuardSession()
    assert care_support.load_mv_consultation_type(session, 343092) is None
    assert session.statements == []


def test_unknown_blank_and_ambiguous_values_are_not_mapped():
    for value in (None, '', 'RETORNO', 'CONSULTA CARDIOLOGIA', 'CAL/CIR'):
        assert care_support.map_verified_consultation_value(value) is None


def test_support_only_adds_type_when_loader_returns_verified_mapping(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: {
        'cd_atendimento': 343092,
        'campos': {},
        'exames': {},
        'alertas': [],
    })
    monkeypatch.setattr(care_support, 'load_mv_consultation_type', lambda *_: None)
    result = care_support.load_care_support(object(), 343092, 'LC-DAC')
    assert 'tipo_consulta' not in result.get('campos', {})

    monkeypatch.setattr(
        care_support,
        'load_mv_consultation_type',
        lambda *_: {'valor': 'CIR', 'origem': 'MV - fonte verificada'},
    )
    result = care_support.load_care_support(object(), 343092, 'LC-DAC')
    assert result['campos']['tipo_consulta'] == {
        'valor': 'CIR',
        'origem': 'MV - fonte verificada',
    }

