import io
import sys
import types
import unittest
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

database_module = types.ModuleType('app_prontocardio.database')
database_module.oracle_engine = None
package_module = types.ModuleType('app_prontocardio')
package_module.database = database_module
sys.modules.setdefault('app_prontocardio', package_module)
sys.modules.setdefault('app_prontocardio.database', database_module)

from evolucao_sadt_backend.sadt_app import GuideRequest, generate_pdf  # noqa: E402


def request_for(
    attendance_type: str,
    purpose: str = 'procedimentos',
    opme: list[str] | None = None,
) -> GuideRequest:
    return GuideRequest(
        paciente={
            'cd_atendimento': 343092,
            'cd_paciente': 99901,
            'nome': 'PACIENTE FICTICIO DE TESTE',
            'data_nascimento': '1970-02-06',
            'sexo': 'F',
            'convenio': 'IPM SAUDE',
            'plano': 'IPM SAUDE',
            'carteira': '12345678',
            'tipo_atendimento': attendance_type,
            'telefone': '(85) 99999-9999',
        },
        medico={
            'nome': 'MEDICO FICTICIO DE TESTE',
            'conselho': 'CRM',
            'uf': 'CE',
            'numero_conselho': '00000',
            'cbo': '225120',
        },
        formato='ipm',
        natureza='eletivo',
        indicacao_clinica='Dor torácica em investigação.',
        exames=[
            {
                'codigo': '40101010',
                'descricao': 'ECG convencional de até 12 derivações',
                'tipo': 'procedimento',
            }
        ],
        ipm_finalidade=purpose,
        ipm_opme=opme or [],
        hospital='Hospital PRONTOCARDIO',
    )


class IpmPdfTests(unittest.TestCase):
    def read(self, request: GuideRequest) -> PdfReader:
        return PdfReader(io.BytesIO(generate_pdf(request)))

    def assert_portrait_a4(self, page) -> None:
        self.assertLess(float(page.mediabox.width), float(page.mediabox.height))
        self.assertAlmostEqual(float(page.mediabox.width), 595.28, delta=2)
        self.assertAlmostEqual(float(page.mediabox.height), 841.89, delta=2)

    def test_g1_is_portrait_and_contains_mv_values(self):
        reader = self.read(request_for('A'))
        self.assertEqual(len(reader.pages), 1)
        self.assert_portrait_a4(reader.pages[0])
        text = reader.pages[0].extract_text()
        self.assertIn('PACIENTE FICTICIO DE TESTE', text)
        self.assertIn('40101010', text)
        self.assertEqual(reader.metadata.subject, 'IPM G1')

    def test_g2_is_selected_for_new_hospitalization(self):
        reader = self.read(request_for('A', 'nova_internacao'))
        self.assertEqual(len(reader.pages), 1)
        self.assert_portrait_a4(reader.pages[0])
        self.assertEqual(reader.metadata.subject, 'IPM G2')

    def test_g3_and_g4_are_generated_in_order_for_inpatient_opme(self):
        reader = self.read(request_for('I', opme=['Stent farmacológico 3,0 mm']))
        self.assertEqual(len(reader.pages), 2)
        self.assert_portrait_a4(reader.pages[0])
        self.assert_portrait_a4(reader.pages[1])
        self.assertIn('40101010', reader.pages[0].extract_text())
        self.assertIn('Stent farmacológico 3,0 mm', reader.pages[1].extract_text())
        self.assertEqual(reader.metadata.subject, 'IPM G3 + G4')


if __name__ == '__main__':
    unittest.main()
