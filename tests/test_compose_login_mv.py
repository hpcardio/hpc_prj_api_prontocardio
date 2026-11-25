"""O compose de produção exige a configuração do login MV.

Em 25/11/2026 a API principal subiu sem MV_CAS_SERVICE_URL/MV_PROXY_* e o
"Entrar com login MV" do Receita Certa passou a falhar sem erro no deploy.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / 'docker-compose.prod.yml'


def test_compose_de_producao_exige_login_mv():
    servicos = yaml.safe_load(COMPOSE.read_text())['services']
    ambiente = servicos['api_prontocardio']['environment']

    assert ambiente['MV_CAS_SERVICE_URL'].startswith('${MV_CAS_SERVICE_URL:?')
    assert ambiente['MV_PROXY_PASSWORD'].startswith('${MV_PROXY_PASSWORD:?')
    assert ambiente['MV_PROXY_USER'] == '${MV_PROXY_USER:-RC_PROXY}'
