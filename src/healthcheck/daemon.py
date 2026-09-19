"""Daemon que checa todos os sistemas configurados a cada N segundos
(padrão 60s) e grava o resultado no banco.

Uso:
    python -m healthcheck.daemon                # loop contínuo
    python -m healthcheck.daemon --once          # roda uma única rodada (teste)
    python -m healthcheck.daemon --config path/to/config.yaml
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .checker import check_target
from .config import Config, load_config, resolve_db_url
from .db import Check, init_db, make_engine, make_session_factory, utcnow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("healthcheck.daemon")

_shutdown_requested = False


def _handle_shutdown_signal(signum, _frame):
    global _shutdown_requested
    log.info("Sinal %s recebido, encerrando após a rodada atual...", signum)
    _shutdown_requested = True


def run_round(config: Config, session_factory) -> None:
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(config.targets))) as pool:
        future_to_target = {
            pool.submit(check_target, target, config.check.timeout_seconds): target
            for target in config.targets
        }
        for future in as_completed(future_to_target):
            target = future_to_target[future]
            try:
                results[target.key] = future.result()
            except Exception:  # falha inesperada no próprio checker
                log.exception("Erro inesperado checando %s", target.key)
                results[target.key] = None

    now = utcnow()
    with session_factory() as session:
        for target in config.targets:
            result = results.get(target.key)
            if result is None:
                continue
            session.add(
                Check(
                    target_key=target.key,
                    checked_at=now,
                    success=result.success,
                    status_code=result.status_code,
                    response_time_ms=result.response_time_ms,
                    error=result.error,
                )
            )
            status = "OK" if result.success else f"FALHA ({result.error})"
            log.info("%-20s %-6s %s", target.key, status, target.url)
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Health check daemon (MEC)")
    parser.add_argument("--config", default=None, help="Caminho para config.yaml")
    parser.add_argument("--once", action="store_true", help="Roda uma única rodada e sai")
    args = parser.parse_args()

    config = load_config(args.config)
    engine = make_engine(resolve_db_url(config))
    init_db(engine)
    session_factory = make_session_factory(engine)

    log.info(
        "Iniciando health check: %d sistema(s), intervalo=%ds, timeout=%ds",
        len(config.targets),
        config.check.interval_seconds,
        config.check.timeout_seconds,
    )

    if args.once:
        run_round(config, session_factory)
        return

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    interval = config.check.interval_seconds
    while not _shutdown_requested:
        t0 = time.monotonic()
        try:
            run_round(config, session_factory)
        except Exception:
            log.exception("Erro inesperado durante a rodada de checagem")
        elapsed = time.monotonic() - t0
        sleep_for = max(0.0, interval - elapsed)
        if elapsed > interval:
            log.warning(
                "Rodada de checagem levou %.1fs, mais que o intervalo configurado (%ds)",
                elapsed,
                interval,
            )
        # dorme em fatias curtas para reagir rápido a um sinal de shutdown
        slept = 0.0
        while slept < sleep_for and not _shutdown_requested:
            step = min(1.0, sleep_for - slept)
            time.sleep(step)
            slept += step

    log.info("Encerrado.")


if __name__ == "__main__":
    sys.exit(main())
