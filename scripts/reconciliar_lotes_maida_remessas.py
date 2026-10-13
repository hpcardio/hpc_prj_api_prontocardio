"""Regulariza lotes técnicos Maida para remessas e processos ISSEC reais."""

from __future__ import annotations

import argparse
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import oracle_engine, postgres_engine
from app_prontocardio.routers.financeiro import (
    _aplicar_processos_portal_issec,
    _identificar_remessa_demonstrativos_oracle,
)


def _lotes_tecnicos(session: Session) -> list[int]:
    return [
        int(lote)
        for lote in session.scalars(
            text(
                """
                SELECT DISTINCT glosa.cd_remessa
                  FROM api_prontocardio.registros_glosa AS glosa
                  JOIN api_prontocardio.registros_glosa_demonstrativo_ipm
                       AS vinculo
                    ON vinculo.registro_glosa_id = glosa.id
                  JOIN api_prontocardio.demonstrativo_conta_ipm AS demo
                    ON demo.id_registro = vinculo.id_registro
                 WHERE UPPER(BTRIM(glosa.convenio)) = 'ISSEC'
                   AND demo.origem_maida IS NOT NULL
                   AND LOWER(BTRIM(glosa.processo_controle_fatura_gab)) IN (
                       glosa.cd_remessa::TEXT,
                       'maida-' || glosa.cd_remessa::TEXT
                   )
                 ORDER BY glosa.cd_remessa
                """
            )
        )
    ]


def _demonstrativos_por_lote(
    session: Session, lotes: list[int]
) -> dict[int, list[dict]]:
    if not lotes:
        return {}
    resultado: dict[int, list[dict]] = defaultdict(list)
    rows = session.execute(
        text(
            """
            SELECT *
              FROM api_prontocardio.demonstrativo_conta_ipm
             WHERE BTRIM(numero_lote) = ANY(:lotes)
               AND COALESCE(valor_glosa, 0) > 0
             ORDER BY numero_lote, id_registro
            """
        ),
        {'lotes': [str(lote) for lote in lotes]},
    ).mappings()
    for row in rows:
        lote = str(row['numero_lote'] or '').strip()
        if lote.isdigit():
            resultado[int(lote)].append(dict(row))
    return dict(resultado)


def reconciliar(*, aplicar: bool) -> dict:
    if postgres_engine is None:
        raise RuntimeError('DATABASE_URL não configurada.')
    with (
        Session(postgres_engine) as postgres,
        Session(oracle_engine) as oracle,
    ):
        lotes = _lotes_tecnicos(postgres)
        demonstrativos = _demonstrativos_por_lote(postgres, lotes)
        remessas = {}
        for lote in lotes:
            remessa = _identificar_remessa_demonstrativos_oracle(
                oracle, demonstrativos.get(lote, [])
            )
            if remessa is not None:
                remessas[lote] = remessa

        cards = [{
            'convenio': 'ISSEC',
            'cd_remessa': remessa,
            'processo': {'numero_processo': ''},
        } for remessa in sorted(set(remessas.values()))]
        _aplicar_processos_portal_issec(postgres, oracle, cards)
        processos_por_remessa = {
            int(card['cd_remessa']): str(
                card['processo']['numero_processo'] or ''
            ).strip()
            for card in cards
        }

        atualizados = 0
        if aplicar:
            for lote, remessa in remessas.items():
                processo = processos_por_remessa.get(remessa) or None
                resultado = postgres.execute(
                    text(
                        """
                        UPDATE api_prontocardio.registros_glosa
                           SET cd_remessa = :remessa,
                               processo_controle_fatura_gab = COALESCE(
                                   :processo,
                                   processo_controle_fatura_gab
                               )
                         WHERE cd_remessa = :lote
                           AND LOWER(BTRIM(
                               processo_controle_fatura_gab
                           )) IN (
                               :lote_texto,
                               'maida-' || :lote_texto
                           )
                        """
                    ),
                    {
                        'lote': lote,
                        'lote_texto': str(lote),
                        'remessa': remessa,
                        'processo': processo,
                    },
                )
                atualizados += int(resultado.rowcount or 0)
            postgres.commit()

        return {
            'modo': 'aplicar' if aplicar else 'simular',
            'lotes_tecnicos': len(lotes),
            'lotes_resolvidos': len(remessas),
            'registros_atualizados': atualizados,
            'associacoes': [
                {
                    'lote_maida': lote,
                    'cd_remessa': remessa,
                    'processo': processos_por_remessa.get(remessa) or None,
                }
                for lote, remessa in sorted(remessas.items())
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Persiste as associações. Sem a opção, apenas simula.',
    )
    args = parser.parse_args()
    print(reconciliar(aplicar=args.apply))


if __name__ == '__main__':
    main()
