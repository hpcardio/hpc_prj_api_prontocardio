from unittest.mock import Mock

import pytest

from app_prontocardio.routers.agendamentos import (
    persistir_convenio_plano_agendamento,
)
from evolucao_sadt_backend import care_support, forms_app


@pytest.mark.parametrize('result', [None, (0, None), (2, 1)])
def test_convenio_incompleto_impede_confirmacao(result):
    cursor = Mock()
    cursor.execute.return_value.fetchone.return_value = result
    with pytest.raises(RuntimeError, match='todos os horarios'):
        persistir_convenio_plano_agendamento(
            cursor, cd_it_agenda_inicio=10, cd_it_agenda_fim=11,
            cd_agenda_central=20, cd_paciente=30,
            cd_convenio=40, cd_con_pla=50,
        )
    cursor.connection.commit.assert_not_called()


@pytest.mark.parametrize('end', [None, 12])
def test_convenio_preserva_escopo_e_transacao(end):
    cursor = Mock()
    cursor.execute.return_value.fetchone.return_value = (2, 2)
    persistir_convenio_plano_agendamento(
        cursor, cd_it_agenda_inicio=10, cd_it_agenda_fim=end,
        cd_agenda_central=20, cd_paciente=30,
        cd_convenio=40, cd_con_pla=50,
    )
    update, verification = cursor.execute.call_args_list
    assert update.args[1] == verification.args[1] == {
        'cd_it_agenda_inicio': 10, 'cd_it_agenda_fim': end or 10,
        'cd_agenda_central': 20, 'cd_paciente': 30,
        'cd_convenio': 40, 'cd_con_pla': 50,
    }
    assert 'AND CD_AGENDA_CENTRAL = :cd_agenda_central' in update.args[0]
    assert 'AND CD_PACIENTE = :cd_paciente' in update.args[0]
    cursor.connection.commit.assert_not_called()


@pytest.mark.parametrize('prescription', [None, 'Receituário fechado'])
def test_ic_usa_receituario_e_historico_sem_prescricao_ativa(
    monkeypatch, prescription,
):
    raw = {'itens_ultima_prescricao': ['Prescrição ativa']}
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: raw)
    monkeypatch.setattr(care_support, '_attendance_identification', lambda *_: {})
    monkeypatch.setattr(
        care_support, '_load_latest_receituario_text', lambda *_: prescription,
    )
    previous = {'data_ancora': '2026-09-01', 'fenotipo_atual': 'ICFEr'}
    monkeypatch.setattr(
        care_support, 'latest_previous_form_value',
        lambda attendance, line, key: previous[key],
    )
    result = care_support.load_care_support(object(), 123, 'LC-IC')['campos']
    assert result['data_ancora']['valor'] == '2026-09-01'
    assert result['fenotipo_anterior']['valor'] == 'ICFEr'
    assert 'prescricao_atual' not in result
    assert raw['itens_ultima_prescricao'] == ['Prescrição ativa']
    if prescription:
        assert result['medicacoes_atuais']['valor'] == prescription
    else:
        assert 'medicacoes_atuais' not in result


def test_dac_preserva_suporte_original(monkeypatch):
    raw = {'itens_ultima_prescricao': ['Prescrição ativa']}
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: raw)
    assert care_support.load_care_support(object(), 123, 'LC-DAC') is raw


def test_ic_renderiza_campos_recuperados_sem_exigir_risco():
    result = forms_app._render_ic_current({
        'altura': '170', 'imc': '25', 'exame_dirigido_outro': 'Observação',
        'criterios_alto_risco': ['feve_menor_35'],
        'condutas_alto_risco': ['antecipar'],
        'etiologia_status': 'em_investigacao',
        'investigacao_valvar_classificacao': 'moderada_importante',
        'investigacao_valvar_conduta': 'heart_team',
        'medicacoes_atuais': 'Medicação informada',
        'sinais_ic_avancada': ['Sinal informado'],
        'condutas_ic_avancada': ['rever'],
    })
    for expected in [
        'Altura: 170.', 'IMC: 25.', 'Observação', 'FEVE < 35%',
        'Antecipar retorno', 'Em investigação',
        'Valvopatia moderada/importante', 'Encaminhar para Heart Team',
        'Medicação informada', 'Sinal informado',
        'Rever tratamento e causas potencialmente corrigíveis',
    ]:
        assert expected in result


def test_ic_vazio_nao_inventa_secoes_clinicas():
    result = forms_app._render_ic_current({})
    for section in ['5. EXAME FÍSICO', '7. ESTRATIFICAÇÃO DE RISCO',
                    '8. INVESTIGAÇÃO ETIOLÓGICA',
                    '9. TRATAMENTO MEDICAMENTOSO',
                    '15. AVALIAÇÃO PARA IC AVANÇADA']:
        assert section not in result
