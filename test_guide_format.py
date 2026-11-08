import sys
import types


database_module = types.ModuleType('app_prontocardio.database')
database_module.oracle_engine = None
package_module = types.ModuleType('app_prontocardio')
package_module.database = database_module
sys.modules.setdefault('app_prontocardio', package_module)
sys.modules.setdefault('app_prontocardio.database', database_module)

from evolucao_sadt_backend.sadt_app import _insurer_config  # noqa: E402


def test_unmapped_insurer_uses_ans_guide_by_default():
    config = _insurer_config('PRONTO CHECK UP')

    assert config['formato'] == 'ans'
    assert config['modelo'] == 'Guia SP/SADT tradicional da ANS'


def test_insurer_with_own_guide_keeps_its_specific_model():
    config = _insurer_config('ISSEC')

    assert config['formato'] == 'issec'
    assert config['modelo'] == 'Guia de Serviço I - ISSEC'
