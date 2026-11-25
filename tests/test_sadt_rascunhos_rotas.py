# ruff: noqa: E501
"""Rotas de rascunho do SADT recuperadas da produção (imagem sadt-resume-restore-20261125-v1)."""

from evolucao_sadt_backend import sadt_app


def test_rotas_de_rascunho_existem():
    rotas = {(tuple(sorted(r.methods)), r.path) for r in sadt_app.app.routes if hasattr(r, 'methods')}
    for metodo, caminho in [
        ('GET', '/api/rascunhos/{cd_atendimento}'),
        ('PUT', '/api/rascunhos/{cd_atendimento}'),
        ('GET', '/api/rascunhos/{cd_atendimento}/identidade'),
        ('GET', '/api/rascunhos/{cd_atendimento}/pdf'),
        ('GET', '/api/rascunhos/{cd_atendimento}/prescricao'),
        ('POST', '/api/rascunhos/{cd_atendimento}/encerrar'),
    ]:
        assert any(metodo in m and p == caminho for m, p in rotas), (metodo, caminho)
