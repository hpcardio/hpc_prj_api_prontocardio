from unittest.mock import Mock
from sqlalchemy.dialects import oracle
from app_prontocardio.routers import financeiro


def test_oracle_query_only_reads_requested_guides_and_remittances():
    session = Mock()
    session.execute.return_value.all.return_value = []
    financeiro._itens_oracle_remessas_ipm(session, {17980}, guias=['435190'])
    query = session.execute.call_args.args[0]
    sql = str(
        query.compile(
            dialect=oracle.dialect(), compile_kwargs={'literal_binds': True}
        )
    )
    assert '435190' in sql and '17980' in sql and 'trim(' in sql.lower()


def test_empty_guides_do_not_query_oracle():
    session = Mock()
    assert financeiro._itens_oracle_remessas_ipm(
        session, {17980}, guias=[]
    ) == ({}, {})
    session.execute.assert_not_called()


def test_no_maida_glosas_avoids_oracle(monkeypatch):
    pg, mv = Mock(), Mock()
    pg.execute.return_value.mappings.return_value = []
    monkeypatch.setattr(financeiro, '_tabela_ipm_existe', lambda *a: True)
    financeiro._detalhar_cards_maida_issec(pg, mv, [dict(cd_remessa=17980)])
    mv.execute.assert_not_called()


def test_guides_are_batched_below_oracle_limit():
    session = Mock()
    session.execute.return_value.all.return_value = []
    financeiro._itens_oracle_remessas_ipm(
        session, {1}, guias=[str(i) for i in range(1801)]
    )
    sql = str(
        session.execute.call_args.args[0].compile(dialect=oracle.dialect())
    )
    assert sql.count('trim(') == 3
