from datetime import datetime
import pytest
from app_prontocardio.routers import painel_senhas_soulmv as general
from app_prontocardio.routers import painel_senhas_soulmv_clinica1 as clinic


@pytest.mark.parametrize('module', [general, clinic])
@pytest.mark.parametrize('queue', [29, 30])
def test_exam_queues_classified_without_destination_keyword(module, queue):
    assert (
        module._service_label(20, 'SALA 1', queue) == 'Exame ou procedimento'
    )


@pytest.mark.parametrize('module', [general, clinic])
def test_legacy_classification_preserved_without_queue(module):
    assert module._service_label(20, 'SALA 1') == module._service_label(
        20, 'SALA 1', None
    )
    assert (
        module._service_label(20, 'ECOCARDIOGRAMA') == 'Exame ou procedimento'
    )


@pytest.mark.parametrize('module', [general, clinic])
def test_event_propagates_queue_and_keeps_identifier(module):
    result = module._to_event(
        dict(
            event_id='123',
            ticket='A001',
            destination='SALA 1',
            process_type=20,
            queue_id=29,
            called_at=datetime(2026, 1, 1),
            priority=0,
            patient_name=None,
        )
    )
    assert result.id == '123'
    assert result.service == 'Exame ou procedimento'
