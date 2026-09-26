"""Reclassifica o histórico de checagens que caíram no desafio do Cloudflare.

Útil quando endpoints atrás do desafio anti-bot do Cloudflare foram
monitorados com `also_accept: [403]` antes de o checker reconhecer o
desafio: cada 403 do desafio foi gravado como "no ar" (success=True) sem
medir nada. Este script regrava essas linhas como rodada sem medição
(error = CLOUDFLARE_CHALLENGE), o mesmo que o checker grava hoje.

Premissa: para os alvos informados, TODO 403 registrado era o desafio. Não
há como confirmar linha a linha (o cabeçalho `cf-mitigated` não era
gravado), então só use em alvos que respondem 403 apenas no desafio. Linhas
com outros códigos (ex: um 200 real) não são tocadas.

Uso:
    python -m healthcheck.reclassify_cloudflare            # só mostra o que mudaria
    python -m healthcheck.reclassify_cloudflare --apply    # grava
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, or_, select, update

from .config import load_config, resolve_db_url
from .db import CLOUDFLARE_CHALLENGE, Check, make_engine, make_session_factory

DEFAULT_KEYS = ("emec", "sistec", "simec", "gpei", "mec_idiomas", "mais_professores")


def _pending_filter(keys):
    return (
        Check.target_key.in_(keys),
        Check.status_code == 403,
        or_(Check.error.is_(None), Check.error != CLOUDFLARE_CHALLENGE),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None, help="Caminho para config.yaml")
    parser.add_argument("--keys", nargs="+", default=list(DEFAULT_KEYS), help="Alvos a reclassificar")
    parser.add_argument("--apply", action="store_true", help="Grava as alterações (sem isso, só mostra)")
    args = parser.parse_args()

    config = load_config(args.config)
    session_factory = make_session_factory(make_engine(resolve_db_url(config)))

    with session_factory() as session:
        pending = session.execute(
            select(Check.target_key, func.count(), func.min(Check.checked_at), func.max(Check.checked_at))
            .where(*_pending_filter(args.keys))
            .group_by(Check.target_key)
            .order_by(Check.target_key)
        ).all()

        if not pending:
            print("Nada a reclassificar.")
            return

        total = 0
        for key, count, first, last in pending:
            print(f"{key:<18} {count:>6} checagem(ns) 403  de {first:%d/%m/%Y %H:%M} a {last:%d/%m/%Y %H:%M} (UTC)")
            total += count

        if not args.apply:
            print(f"\n{total} linha(s) seriam reclassificadas. Rode com --apply para gravar.")
            return

        result = session.execute(
            update(Check)
            .where(*_pending_filter(args.keys))
            .values(success=False, error=CLOUDFLARE_CHALLENGE)
        )
        session.commit()
        print(f"\n{result.rowcount} linha(s) reclassificadas como '{CLOUDFLARE_CHALLENGE}'.")


if __name__ == "__main__":
    sys.exit(main())
