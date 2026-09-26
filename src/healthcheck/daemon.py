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
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from .checker import CheckResult, check_target
from .config import Config, load_config, resolve_db_url
from .db import CLOUDFLARE_CHALLENGE, Check, init_db, make_engine, make_session_factory, utcnow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("healthcheck.daemon")

_shutdown_requested = False

# Folga, além do timeout HTTP, para a rodada inteira terminar. O timeout do
# requests vale por operação de rede (conectar, cada leitura), não para a
# requisição toda: um servidor que responde a conta-gotas não estoura nunca.
ROUND_GRACE_SECONDS = 5

# Endpoints cuja checagem de uma rodada anterior ainda não terminou. Não
# abrimos outra em cima: senão um servidor travado acumularia uma thread e
# uma conexão presas por minuto.
_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def _handle_shutdown_signal(signum, _frame):
    global _shutdown_requested
    log.info("Sinal %s recebido, encerrando após a rodada atual...", signum)
    _shutdown_requested = True


def _checked(check, target, timeout_seconds):
    try:
        return check(target, timeout_seconds)
    finally:
        with _in_flight_lock:
            _in_flight.discard(target.key)


def run_round(
    config: Config,
    session_factory,
    check=check_target,
    grace_seconds: float = ROUND_GRACE_SECONDS,
) -> None:
    timeout = config.check.timeout_seconds
    results: dict[str, CheckResult | None] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, len(config.targets)))
    future_to_target = {}
    for target in config.targets:
        with _in_flight_lock:
            busy = target.key in _in_flight
            if not busy:
                _in_flight.add(target.key)
        if busy:
            log.warning("%s: checagem anterior ainda em andamento; conta como timeout", target.key)
            results[target.key] = CheckResult(False, None, None, "timeout (checagem anterior travada)")
            continue
        future_to_target[pool.submit(_checked, check, target, timeout)] = target

    done, pending = wait(future_to_target, timeout=timeout + grace_seconds)
    # Não espera as travadas: a rodada é gravada com o que terminou no prazo.
    pool.shutdown(wait=False, cancel_futures=True)
    for future in done:
        target = future_to_target[future]
        try:
            results[target.key] = future.result()
        except Exception:  # falha inesperada no próprio checker
            log.exception("Erro inesperado checando %s", target.key)
            results[target.key] = None
    for future in pending:
        target = future_to_target[future]
        log.warning("%s: não terminou em %ss; conta como timeout", target.key, timeout + grace_seconds)
        results[target.key] = CheckResult(False, None, None, "timeout (rodada)")

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
            if result.success:
                status = "OK"
            elif result.error == CLOUDFLARE_CHALLENGE:
                status = "BLOQUEIO (desafio do Cloudflare, sem medição)"
            else:
                status = f"FALHA ({result.error})"
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
        "Iniciando health check: %d sistema(s), %d endpoint(s), intervalo=%ds, timeout=%ds",
        len(config.systems),
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
    logging.shutdown()
    # Uma checagem travada (servidor que responde a conta-gotas) deixa uma
    # thread presa; o concurrent.futures faria join dela na saída e o
    # `systemctl stop/restart` ficaria esperando até o TimeoutStopSec.
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
