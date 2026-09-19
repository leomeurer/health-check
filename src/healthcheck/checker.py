"""Lógica de checagem HTTP de um sistema."""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .config import Target
from .db import ERROR_MAX_LEN


@dataclass
class CheckResult:
    success: bool
    status_code: int | None
    response_time_ms: float | None
    error: str | None


def _truncate(message: str) -> str:
    """A coluna de erro tem tamanho fixo; no Oracle um valor maior que o
    limite aborta o INSERT (e, com ele, a rodada inteira)."""
    if len(message) <= ERROR_MAX_LEN:
        return message
    return message[: ERROR_MAX_LEN - 3] + "..."


def _status_accepted(status_code: int, target: Target) -> bool:
    if status_code in target.also_accept:
        return True
    if target.expected_status is None:
        return 200 <= status_code <= 399
    return status_code in target.expected_status


def check_target(target: Target, timeout_seconds: int) -> CheckResult:
    start = time.monotonic()
    try:
        response = requests.get(
            target.url,
            timeout=timeout_seconds,
            allow_redirects=True,
            headers={"User-Agent": "mec-health-check/1.0"},
        )
    except requests.exceptions.Timeout:
        return CheckResult(False, None, None, "timeout")
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(False, None, None, _truncate(f"connection_error: {exc}"))
    except requests.exceptions.RequestException as exc:
        return CheckResult(False, None, None, _truncate(f"request_error: {exc}"))

    elapsed_ms = (time.monotonic() - start) * 1000
    success = _status_accepted(response.status_code, target)
    if success:
        error = None
    elif target.expected_status is None:
        error = "status_code_fora_do_esperado (200-399)"
    else:
        aceitos = ", ".join(str(code) for code in sorted(target.expected_status))
        error = _truncate(f"status_code_fora_do_esperado (aceitos: {aceitos})")
    return CheckResult(success, response.status_code, elapsed_ms, error)
