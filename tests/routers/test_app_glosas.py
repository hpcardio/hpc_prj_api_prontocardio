from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy.exc import SQLAlchemyError

from app_prontocardio.database import get_session_oracle
from app_prontocardio.security import valida_token_usuario_atual


def test_conta_atendimento_retorna_dados_quando_encontra_registros(
    cliente,
    override_dependencies,
    usuario_autenticado,
):
    # Garante retorno 200 com lista de atendimentos para código válido.
    db_session = Mock()
    query_result = Mock()
    query_result.scalars.return_value.all.return_value = [
        SimpleNamespace(cd_reg=1, cd_lancamento=1, cd_atendimento=123)
    ]
    db_session.execute.return_value = query_result

    def override_session_oracle():
        yield db_session

    override_dependencies[get_session_oracle] = override_session_oracle
    override_dependencies[valida_token_usuario_atual] = lambda: (
        usuario_autenticado
    )

    response = cliente.get('/app_glosas/conta_atendimento/123')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'atendimentos': [
            {
                'cd_reg': 1,
                'cd_lancamento': 1,
                'cd_atendimento': 123,
                'cd_paciente': None,
                'nm_paciente': None,
                'cd_remessa': None,
                'cd_regra': None,
                'ds_regra': None,
                'cd_convenio': None,
                'nm_convenio': None,
                'cd_gru_fat': None,
                'ds_gru_fat': None,
                'cd_pro_fat': None,
                'descricao': None,
                'cd_guia': None,
                'dt_lancamento': None,
                'hr_lancamento': None,
                'cd_prestador': None,
                'nm_prestador': None,
                'sn_pertence_pacote': None,
                'vl_unitario': None,
                'vl_total_conta': None,
                'vl_honorario_unitario': None,
                'vl_acrescimo': None,
                'vl_desconto': None,
                'cd_ati_med': None,
                'ds_ati_med': None,
                'cd_usuario': None,
                'nm_usuario': None,
                'tp_atendimento': None,
                'dt_ordenacao': None,
            }
        ]
    }


def test_conta_atendimento_retorna_404_quando_nao_encontra(
    cliente,
    override_dependencies,
    usuario_autenticado,
):
    # Retorna 404 quando não há itens para o código de atendimento informado.
    db_session = Mock()
    query_result = Mock()
    query_result.scalars.return_value.all.return_value = []
    db_session.execute.return_value = query_result

    def override_session_oracle():
        yield db_session

    override_dependencies[get_session_oracle] = override_session_oracle
    override_dependencies[valida_token_usuario_atual] = lambda: (
        usuario_autenticado
    )

    response = cliente.get('/app_glosas/conta_atendimento/999999')

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert 'não encontrado' in response.json()['detail']


def test_conta_atendimento_retorna_422_para_path_invalido(
    cliente,
    override_dependencies,
    usuario_autenticado,
):
    # Aplica validação de caminho para valor que não atende à regra gt=0.
    override_dependencies[valida_token_usuario_atual] = lambda: (
        usuario_autenticado
    )

    response = cliente.get('/app_glosas/conta_atendimento/0')

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_conta_atendimento_retorna_500_quando_ha_erro_sql(
    cliente,
    override_dependencies,
    usuario_autenticado,
):
    # Traduz erro de banco para resposta HTTP 500 padronizada.
    db_session = Mock()
    db_session.execute.side_effect = SQLAlchemyError('falha simulada')

    def override_session_oracle():
        yield db_session

    override_dependencies[get_session_oracle] = override_session_oracle
    override_dependencies[valida_token_usuario_atual] = lambda: (
        usuario_autenticado
    )

    response = cliente.get('/app_glosas/conta_atendimento/123')

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json()['detail'] == 'Erro ao consultar conta_atendimento.'
