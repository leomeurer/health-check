"""Cálculo de uptime, incidentes e status atual a partir dos registros
de checagem.

Critério de indisponibilidade adotado (decisão registrada com o
solicitante): UMA falha já conta como início de downtime — não há
tolerância de N falhas consecutivas.

O cálculo de glosa contratual (desconto financeiro por descumprimento de
SLA) depende da cláusula exata do contrato e NÃO é inventado aqui: fica
como placeholder até a fórmula real ser informada (ver config.yaml ->
sla.glosa_formula).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import SlaSettings
from .db import Check, utcnow


@dataclass
class CurrentStatus:
    target_key: str
    up: bool
    last_checked_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    down_since: datetime | None  # se up=False, desde quando está fora do ar


@dataclass
class Incident:
    target_key: str
    start: datetime
    end: datetime | None  # None = incidente em andamento
    duration_seconds: float | None


@dataclass
class UptimeSummary:
    target_key: str
    window_label: str
    total_checks: int
    successful_checks: int
    uptime_pct: float | None  # None se não houve checagens na janela


def compute_current_status(checks_desc: list[Check]) -> CurrentStatus | None:
    """`checks_desc` deve estar ordenado do mais recente para o mais antigo,
    de um único target_key."""
    if not checks_desc:
        return None

    latest = checks_desc[0]
    up = latest.success
    down_since = None
    if not up:
        down_since = latest.checked_at
        for check in checks_desc[1:]:
            if not check.success:
                down_since = check.checked_at
            else:
                break

    return CurrentStatus(
        target_key=latest.target_key,
        up=up,
        last_checked_at=latest.checked_at,
        last_status_code=latest.status_code,
        last_error=latest.error,
        down_since=down_since,
    )


def compute_uptime_pct(
    checks_in_window: list[Check], target_key: str, window_label: str
) -> UptimeSummary:
    total = len(checks_in_window)
    successful = sum(1 for c in checks_in_window if c.success)
    pct = (successful / total * 100) if total > 0 else None
    return UptimeSummary(
        target_key=target_key,
        window_label=window_label,
        total_checks=total,
        successful_checks=successful,
        uptime_pct=pct,
    )


def compute_incidents(checks_asc: list[Check], limit: int | None = None) -> list[Incident]:
    """`checks_asc` deve estar ordenado do mais antigo para o mais recente,
    de um único target_key. Agrupa sequências consecutivas de falha em
    incidentes."""
    incidents: list[Incident] = []
    current_start: datetime | None = None
    current_end: datetime | None = None

    for check in checks_asc:
        if not check.success:
            if current_start is None:
                current_start = check.checked_at
            current_end = check.checked_at
        else:
            if current_start is not None:
                incidents.append(
                    Incident(
                        target_key=check.target_key,
                        start=current_start,
                        end=check.checked_at,
                        duration_seconds=(check.checked_at - current_start).total_seconds(),
                    )
                )
                current_start = None
                current_end = None

    if current_start is not None:
        # incidente em andamento (ainda não houve checagem de sucesso após a falha)
        target_key = checks_asc[-1].target_key
        incidents.append(
            Incident(
                target_key=target_key,
                start=current_start,
                end=None,
                duration_seconds=None,
            )
        )

    incidents.sort(key=lambda i: i.start, reverse=True)
    if limit is not None:
        incidents = incidents[:limit]
    return incidents


def current_month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now


def sla_compliance(uptime_pct: float | None, sla: SlaSettings) -> dict | None:
    """Retorna um resumo simples de conformidade com a meta de SLA, sem
    calcular valor de glosa (fórmula ainda não informada)."""
    if sla.target_pct is None or uptime_pct is None:
        return None
    return {
        "target_pct": sla.target_pct,
        "actual_pct": uptime_pct,
        "met": uptime_pct >= sla.target_pct,
        "deficit_pct": max(0.0, sla.target_pct - uptime_pct),
    }
