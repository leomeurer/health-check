"""Lógica de checagem HTTP de um sistema."""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .config import Target


@dataclass
class CheckResult:
    success: bool
    status_code: int | None
    response_time_ms: float | None
    error: str | None


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
        return CheckResult(False, None, None, f"connection_error: {exc}")
    except requests.exceptions.RequestException as exc:
        return CheckResult(False, None, None, f"request_error: {exc}")

    elapsed_ms = (time.monotonic() - start) * 1000
    low, high = target.expected_status
    success = low <= response.status_code <= high
    error = None if success else f"status_code_fora_do_esperado ({low}-{high})"
    return CheckResult(success, response.status_code, elapsed_ms, error)
