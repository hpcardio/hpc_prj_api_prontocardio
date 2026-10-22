from unittest.mock import Mock

from app_prontocardio.routers.app_glosas import _resolver_filtro_processo
from app_prontocardio.schema import FilterSearch


def test_filtros_recurso_e_protocolo_combinam_sem_ignorar_escopo():
    session = Mock()
    session.execute.return_value.scalars.return_value.all.return_value = [42]
    session.execute.return_value.all.return_value = [(1, 2, 3, 4)]
    result = _resolver_filtro_processo(
        session,
        dict(
            processo='Original',
            processo_recurso='Recurso',
            numero_protocolo='TRUE-1',
            cd_remessa=1,
        ),
    )
    assert result == {'cd_remessa': 1, 'identidades_processo': ((1, 2, 3, 4),)}
    sql = str(session.execute.call_args.args[0])
    assert 'processo_recurso' in sql
    assert 'processo_controle_fatura_gab' in sql
    assert 'registros_glosa.id IN' in sql
    assert session.execute.call_args_list[0].args[1] == {'protocolo': 'true-1'}
    assert (
        FilterSearch(
            processo_recurso='R', numero_protocolo='T'
        ).numero_protocolo
        == 'T'
    )


def test_protocolo_sem_correspondencia_nao_amplia_busca():
    session = Mock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.execute.return_value.all.return_value = []
    result = _resolver_filtro_processo(
        session, {'numero_protocolo': 'inexistente'}
    )
    assert result['identidades_processo'] == ()
    assert 'registros_glosa.id IN' in str(session.execute.call_args.args[0])
