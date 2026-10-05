from pathlib import Path

BIQ_SOURCE = (
    Path(__file__).parents[1] / "app_prontocardio" / "routers" / "biq.py"
).read_text(encoding="utf-8")


def _query_source(name: str, next_name: str) -> str:
    start = BIQ_SOURCE.index(f"{name} = text(")
    end = BIQ_SOURCE.index(f"{next_name} = text(", start)
    return BIQ_SOURCE[start:end]


def test_atendimentos_ergometricos_nao_dependem_de_itreg_amb():
    query = _query_source(
        "CONSULTA_TESTE_ERGOMETRICO_ATENDIMENTOS",
        "CONSULTA_TESTE_ERGOMETRICO_ATENDIMENTOS_CHECKUP",
    )

    assert "JOIN DBAMV.EXA_RX exa" in query
    assert "LEFT JOIN DBAMV.ITREG_AMB ira" in query
    assert "ira.CD_PRO_FAT IN (" in query
    assert "ira.CD_PRO_FAT = exa.EXA_RX_CD_PRO_FAT" not in query
    assert query.index("JOIN DBAMV.EXA_RX exa") < query.index(
        "LEFT JOIN DBAMV.ITREG_AMB ira"
    )


def test_laudos_ergometricos_assinados_nao_dependem_de_itreg_amb():
    query = _query_source(
        "CONSULTA_TESTE_ERGOMETRICO_LAUDOS",
        "CONSULTA_TESTE_ERGOMETRICO_LAUDOS_CHECKUP",
    )

    assert "JOIN DBAMV.EXA_RX exa" in query
    assert "JOIN DBAMV.LAUDO_RX lr" in query
    assert "LEFT JOIN DBAMV.ITREG_AMB ira" in query
    assert "ira.CD_PRO_FAT IN (" in query
    assert "ira.CD_PRO_FAT = exa.EXA_RX_CD_PRO_FAT" not in query
    assert query.index("JOIN DBAMV.LAUDO_RX lr") < query.index(
        "LEFT JOIN DBAMV.ITREG_AMB ira"
    )
