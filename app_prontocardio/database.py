from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app_prontocardio.settings import Settings

engine = create_engine(Settings().DATABASE_URL, thick_mode=True)


def get_session_prod():
    with Session(engine) as session_prd:
        yield session_prd
