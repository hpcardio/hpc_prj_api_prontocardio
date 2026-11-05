"""One-time: store the payer's lot number on rows already imported.

Reads the payment demonstrativos of the given competences; nothing is sent.

    python -m scripts.backfill_petrobras_lote 202601 202611
"""

import sys

from sqlalchemy.orm import Session

from app_prontocardio.database import postgres_engine
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_sync import apply_payer_lots
from app_prontocardio.services.petrobras_tiss import (
    PetrobrasClient,
    TissError,
    payment_lots,
)
from scripts.sync_petrobras import competencias


def main(start, end):
    config = PetrobrasConfig()
    client = PetrobrasClient(config)
    total = 0
    with Session(postgres_engine) as session:
        for month in competencias(None, 0, start, end):
            try:
                lots = payment_lots(client.pagamentos(month))
            except TissError as exc:
                print(month, 'sem dados:', exc)
                continue
            changed = apply_payer_lots(session, config, lots)
            session.commit()
            total += changed
            print(month, 'protocolos', len(lots), 'itens alterados', changed)
    print('total', total)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
