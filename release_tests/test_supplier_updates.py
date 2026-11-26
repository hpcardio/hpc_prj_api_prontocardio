import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app_prontocardio.services.supplier_updates import SupplierResponse, aggregate_position, classify_difference
from app_prontocardio.services.supplier_updates_store import SupplierStore, metadata, cases


class PositionTests(unittest.TestCase):
    def test_payments_are_unique_and_paid_supplier_is_retained(self):
        row = dict(company=1, company_name='Hospital', supplier='12', name='Fornecedor', document='123', parcel=9,
                   invoice='NF1', installment=1, due=date(2026, 11, 1), amount='100', status='quitado',
                   payment=42, paid='100', paid_at=date(2026, 11, 20), reversed_at=None)
        result = aggregate_position([row, row, {**row, 'payment': 43, 'paid': '10', 'reversed_at': date(2026, 11, 21)}], date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['paid_period'], '100.00')
        self.assertEqual(result[0]['system_balance'], '0.00')
        self.assertEqual(len(result[0]['titles']), 1)

    def test_companies_remain_separate_and_no_code_uses_account(self):
        rows = [dict(company=c, company_name='Empresa', supplier=None, name=None, document=None,
                     account=c, parcel=c, invoice='1', installment=1, due=date(2026, 12, 1), amount=10,
                     status='comprometido', payment=None, paid=0, paid_at=None, reversed_at=None) for c in (1, 2)]
        result = aggregate_position(rows, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(result), 2)
        self.assertNotEqual(result[0]['supplier'], result[1]['supplier'])

    def test_same_unknown_name_in_one_company_does_not_merge_accounts(self):
        rows = [dict(company=1, company_name='Hospital', supplier=None, name='SEM CADASTRO', document=None,
            account=account, parcel=account, invoice='1', installment=1, due=date(2026,12,1), amount=100,
            status='comprometido', payment=None, paid=0, paid_at=None, reversed_at=None) for account in (1,2)]
        result = aggregate_position(rows, date(2026,1,1), date(2026,12,31))
        self.assertEqual(len(result), 2)

    def test_partial_payment_subtracts_once_and_fingerprint_changes(self):
        row = dict(company=1, company_name='Hospital', supplier='12', name='F', document='', parcel=9,
                   invoice='NF1', installment=1, due=date(2026, 11, 1), amount='100', status='parcialmente pago',
                   payment=42, paid='30', paid_at=date(2026, 11, 20), reversed_at=None)
        first = aggregate_position([row, row], date(2026, 1, 1), date(2026, 12, 31))[0]
        second = aggregate_position([{**row, 'paid': '40'}], date(2026, 1, 1), date(2026, 12, 31))[0]
        self.assertEqual(first['system_balance'], '70.00')
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])

    def test_agreement_replaces_referenced_titles(self):
        payload = SupplierResponse.model_validate(dict(legal_name='Empresa', document='12345678000195', contact='Ana',
            email='ana@example.com', phone='85999999999', whatsapp='', as_of='2026-11-26', no_debt=False,
            titles=[dict(reference='NF1', due='2026-12-01', amount='100', balance='100')],
            agreements=[dict(reference='A1', replaces=['NF1'], balance='80', installments=[dict(reference='1', due='2026-12-01', amount='80')])], confirmed=True))
        self.assertEqual(payload.declared_balance(), Decimal('80.00'))
        self.assertEqual(classify_difference('100', payload), 'divergencia')

    def test_zero_declaration_is_not_system_clearance(self):
        payload = SupplierResponse.model_validate(dict(legal_name='Empresa', document='12345678000195', contact='Ana',
            email='ana@example.com', phone='85999999999', as_of='2026-11-26', no_debt=True, confirmed=True))
        self.assertEqual(classify_difference('500', payload), 'divergencia')

    def test_crosscheck_lists_missing_and_changed_titles(self):
        from app_prontocardio.services import supplier_updates as domain
        self.assertTrue(hasattr(domain, 'crosscheck_titles'))
        payload = SupplierResponse.model_validate(dict(legal_name='Empresa', document='12345678000195', contact='Ana',
            email='ana@example.com', phone='85999999999', as_of='2026-11-26', no_debt=False, confirmed=True,
            titles=[dict(reference='NF1/1', invoice='NF1', due='2026-12-01', amount='100', balance='80')]))
        position = {'titles': [dict(reference='NF1/1', invoice='NF1', parcel='9', due='2026-12-01', balance='100'),
                               dict(reference='NF2/1', invoice='NF2', parcel='10', due='2026-12-01', balance='20')]}
        rows = domain.crosscheck_titles(position, payload)
        self.assertEqual({r['status'] for r in rows}, {'valor_divergente', 'nao_declarado'})

    def test_document_reference_is_not_automatic_payment_proof(self):
        from app_prontocardio.services import supplier_updates as domain
        self.assertTrue(hasattr(domain, 'crosscheck_titles'))
        payload = SupplierResponse.model_validate(dict(legal_name='Empresa', document='12345678000195', contact='Ana',
            email='ana@example.com', phone='85999999999', as_of='2026-11-26', no_debt=False, confirmed=True,
            titles=[dict(reference='X', invoice='NF1', due='2026-12-01', amount='100', balance='100')]))
        position = {'titles': [dict(reference='NF1/1', invoice='NF1', parcel=str(p), due='2026-12-01', balance='100') for p in (1,2)]}
        self.assertEqual(domain.crosscheck_titles(position, payload)[0]['status'], 'correspondencia_ambigua')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', execution_options={'schema_translate_map': {'api_prontocardio': None}})
        metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.store = SupplierStore(self.session)
        self.case = self.store.create_case(dict(company=1, supplier='12', name='Fornecedor', document='12345678000195'), 7)
        self.session.commit()

    def test_invite_expires_and_revocation_is_effective(self):
        invite = self.store.invite(self.case['id'], 7)
        self.assertEqual(self.store.resolve(invite['token'])['case_id'], self.case['id'])
        self.store.revoke(self.case['id'], 7)
        with self.assertRaises(ValueError):
            self.store.resolve(invite['token'])

    def test_submission_is_one_time_and_does_not_change_debt(self):
        invitation = self.store.invite(self.case['id'], 7)
        payload = SupplierResponse.model_validate(dict(legal_name='Fornecedor', document='12345678000195', contact='Ana',
            email='ana@example.com', phone='85999999999', as_of='2026-11-26', no_debt=True, confirmed=True))
        response = self.store.submit(invitation['token'], payload, [])
        self.session.commit()
        self.assertTrue(response['protocol'])
        with self.assertRaises(ValueError):
            self.store.submit(invitation['token'], payload, [])
        stored = dict(self.session.execute(select(cases)).mappings().one())
        self.assertEqual(stored['stage'], 'respondido')
        self.assertIsNone(stored['validated_balance'])

    def test_stale_review_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'atualizada'):
            self.store.review(self.case['id'], 7, 'aprovar', '0', 'Documentos conferidos', 1, 'old', 'new')


if __name__ == '__main__':
    unittest.main()
