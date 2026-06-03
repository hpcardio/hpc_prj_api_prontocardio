from http import HTTPStatus


def test_docs_endpoint_disponivel(cliente):
    # Garante que a documentação Swagger está disponível.
    response = cliente.get('/docs')

    assert response.status_code == HTTPStatus.OK


def test_openapi_contem_todos_os_routers(cliente):
    # Confirma que as rotas principais existem no esquema OpenAPI.
    response = cliente.get('/openapi.json')

    assert response.status_code == HTTPStatus.OK

    schema = response.json()
    paths = set(schema.get('paths', {}).keys())
    assert '/autenticacao/token' in paths
    assert '/livre/' in paths
    assert '/usuarios/' in paths
    assert '/app_glosas/conta_atendimento/{cd_atendimento}' in paths
