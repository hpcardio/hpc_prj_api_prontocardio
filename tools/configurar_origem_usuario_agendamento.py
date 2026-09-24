"""Classifica, de modo explícito, a origem de um usuário de agendamento."""

from __future__ import annotations

import argparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app_prontocardio.database import postgres_engine
from app_prontocardio.models import Usuario
from app_prontocardio.services.agendamento_origens import OrigemAgendamento


def configurar_origem_usuario(
    session: Session,
    usuario: str,
    origem: OrigemAgendamento,
    *,
    aplicar: bool,
) -> tuple[OrigemAgendamento, OrigemAgendamento]:
    encontrados = list(
        session.scalars(
            select(Usuario).where(
                or_(Usuario.nome == usuario, Usuario.email == usuario)
            )
        )
    )
    if len(encontrados) != 1:
        raise ValueError('Usuário deve corresponder a exatamente um registro.')
    registro = encontrados[0]
    anterior = OrigemAgendamento(registro.origem_agendamento)
    if aplicar:
        registro.origem_agendamento = origem.value
        session.commit()
    return anterior, origem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usuario', required=True)
    parser.add_argument(
        '--origem',
        required=True,
        choices=[origem.value for origem in OrigemAgendamento],
    )
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with Session(postgres_engine) as session:
        try:
            anterior, nova = configurar_origem_usuario(
                session,
                args.usuario,
                OrigemAgendamento(args.origem),
                aplicar=args.apply,
            )
        except ValueError as exc:
            parser.error(str(exc))
    modo = 'aplicado' if args.apply else 'simulacao'
    print(
        f'usuario={args.usuario} origem_anterior={anterior.value} '
        f'origem_nova={nova.value} modo={modo}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
