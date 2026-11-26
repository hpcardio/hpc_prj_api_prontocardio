"""Run separately from web workers. No drafts or unapproved recipients are sent."""
import argparse
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true',help='Process at most one authorized recipient')
    parser.add_argument('--interval',type=float,default=1)
    args=parser.parse_args()
    from sqlalchemy.orm import Session
    from app_prontocardio.database import postgres_engine,oracle_engine
    from app_prontocardio.models import Usuario
    from app_prontocardio.settings import Settings
    from app_prontocardio.routers.supplier_updates import access
    from app_prontocardio.services.supplier_bulk import BulkService,Blocked
    from app_prontocardio.services.supplier_bulk_sources import SupplierBulkSource
    from app_prontocardio.services.supplier_bulk_channels import SupplierBulkProvider
    settings=Settings()
    if not postgres_engine:raise SystemExit('PostgreSQL não configurado.')
    if not settings.SUPPLIER_BULK_SEND_ENABLED:raise SystemExit('Envio automático não liberado neste ambiente.')
    interval=max(1,min(args.interval,30))
    while True:
        with Session(postgres_engine) as session,Session(oracle_engine) as oracle:
            def authorize(actor,kind):
                user=session.get(Usuario,actor)
                if not user or not user.ativo:raise Blocked('Operador desativado. Solicite nova autorização.')
                try:access(user,financial=kind=='payments')
                except Exception:raise Blocked('Permissão do operador alterada. Solicite nova autorização.') from None
            engine=BulkService(session,SupplierBulkSource(session,oracle),SupplierBulkProvider(settings),authorize,settings.SECRET_KEY)
            try:engine.dispatch_one()
            except Exception:
                session.rollback()
                logging.error('Worker não concluiu a rodada; resultado em envio será conferido pela recuperação conservadora.')
        if args.once:return
        time.sleep(interval)


if __name__=='__main__':main()
