from evolucao_sadt_backend.ipm_guides import ipm_page_count, select_ipm_models


def test_selects_g1_for_outpatient_attendance():
    assert select_ipm_models('A', 'procedimentos', False) == ('g1',)


def test_selects_g3_for_inpatient_attendance():
    assert select_ipm_models('I', 'procedimentos', False) == ('g3',)


def test_selects_g2_for_new_hospitalization_request():
    assert select_ipm_models('A', 'nova_internacao', False) == ('g2',)


def test_appends_g4_when_opme_is_requested():
    assert select_ipm_models('I', 'procedimentos', True) == ('g3', 'g4')
    assert select_ipm_models('A', 'nova_internacao', True) == ('g2', 'g4')


def test_unknown_attendance_defaults_to_outpatient_guide():
    assert select_ipm_models(None, 'procedimentos', False) == ('g1',)


def test_counts_primary_pages_and_appended_g4():
    assert ipm_page_count(6, ('g1',)) == 2
    assert ipm_page_count(11, ('g3', 'g4')) == 3
    assert ipm_page_count(12, ('g2',)) == 1
