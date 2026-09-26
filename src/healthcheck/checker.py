"""Lógica de checagem HTTP de um sistema."""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests

from .config import Target
from .db import CLOUDFLARE_CHALLENGE, ERROR_MAX_LEN

USER_AGENT = "mec-health-check/1.0"
# Redirecionamentos seguidos por checagem. Sites reais usam 1 ou 2
# (http→https, raiz→idioma); mais que isso é loop ou algo estranho.
MAX_REDIRECTS = 5


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


def _is_cloudflare_challenge(response: requests.Response) -> bool:
    """O desafio anti-bot do Cloudflare é respondido pela BORDA do Cloudflare,
    sem a requisição chegar ao servidor do sistema: ele não diz nada sobre o
    sistema estar no ar ou fora. O Cloudflare marca essas respostas com
    `cf-mitigated: challenge`."""
    return response.headers.get("cf-mitigated", "").strip().lower() == "challenge"


def _redirect_problem(url: str) -> str | None:
    """Motivo para NÃO seguir um redirecionamento, ou None se ele é seguro.

    Um site monitorado (ou comprometido) não pode usar o monitor para
    alcançar endereços internos: rede privada da VM, loopback ou o serviço
    de metadados da nuvem (169.254.169.254). Só destinos públicos passam."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return f"esquema/host inválido em {url!r}"
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None  # DNS falhou: a própria requisição vai registrar o erro
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            return f"destino não público ({address})"
    return None


def _get(session: requests.Session, url: str, timeout_seconds: int) -> requests.Response:
    # stream=True: o corpo nunca é baixado — status e cabeçalhos bastam, e um
    # corpo enorme ou gotejado não consome memória nem segura a checagem.
    return session.get(
        url,
        timeout=timeout_seconds,
        allow_redirects=False,
        stream=True,
        headers={"User-Agent": USER_AGENT},
    )


def check_target(target: Target, timeout_seconds: int) -> CheckResult:
    start = time.monotonic()
    url = target.url
    try:
        # Session mantém os cookies entre os saltos, como allow_redirects=True
        # fazia; os redirecionamentos são seguidos à mão para validar cada destino.
        with requests.Session() as session:
            for hop in range(MAX_REDIRECTS + 1):
                response = _get(session, url, timeout_seconds)
                try:
                    if response.is_redirect:
                        next_url = urljoin(url, response.headers["location"])
                        if hop == MAX_REDIRECTS:
                            return CheckResult(
                                False, response.status_code, _elapsed_ms(start),
                                f"redirecionamentos_demais (>{MAX_REDIRECTS})",
                            )
                        problem = _redirect_problem(next_url)
                        if problem:
                            return CheckResult(
                                False, response.status_code, _elapsed_ms(start),
                                _truncate(f"redirecionamento_bloqueado: {problem}"),
                            )
                        url = next_url
                        continue
                    return _evaluate(response, target, _elapsed_ms(start))
                finally:
                    response.close()
    except requests.exceptions.Timeout:
        return CheckResult(False, None, None, "timeout")
    except requests.exceptions.ConnectionError as exc:
        return CheckResult(False, None, None, _truncate(f"connection_error: {exc}"))
    except requests.exceptions.RequestException as exc:
        return CheckResult(False, None, None, _truncate(f"request_error: {exc}"))
    raise AssertionError("inalcançável: o laço sempre retorna")


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _evaluate(response: requests.Response, target: Target, elapsed_ms: float) -> CheckResult:
    if _is_cloudflare_challenge(response):
        # Não é "no ar" nem "fora do ar": é uma rodada sem medição. Vai para o
        # banco como success=False com este marcador, e a apuração (sla.py)
        # tira essas rodadas do uptime em vez de contá-las como queda.
        return CheckResult(False, response.status_code, elapsed_ms, CLOUDFLARE_CHALLENGE)
    success = _status_accepted(response.status_code, target)
    if success:
        error = None
    elif target.expected_status is None:
        error = "status_code_fora_do_esperado (200-399)"
    else:
        aceitos = ", ".join(str(code) for code in sorted(target.expected_status))
        error = _truncate(f"status_code_fora_do_esperado (aceitos: {aceitos})")
    return CheckResult(success, response.status_code, elapsed_ms, error)
