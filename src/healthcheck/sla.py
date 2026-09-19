"""Cálculo de uptime, incidentes e status atual a partir dos registros
de checagem.

Critério de indisponibilidade adotado (decisão registrada com o
solicitante): UMA falha já conta como início de downtime — não há
tolerância de N falhas consecutivas.

Convenções importantes para a apuração contratual:

- O uptime é medido por amostragem: cada checagem representa um intervalo
  de tempo (o intervalo entre checagens). Uptime % = checagens bem
  sucedidas / total de checagens na janela.
- Por consequência, um período SEM checagem nenhuma (monitor parado, VM
  reiniciada, execução do agendador pulada) não entra no cálculo e faria
  o sistema parecer 100% disponível. Para tornar isso visível em vez de
  silencioso, toda janela também informa a COBERTURA: quantas checagens
  existem contra quantas eram esperadas. Uptime com cobertura baixa não
  sustenta glosa.
- A duração de um incidente é contabilizada como
  (nº de checagens com falha) x (intervalo entre checagens), mantendo a
  coerência com o uptime %. O horário em "fim" é o momento em que a
  recuperação foi detectada, não o instante exato em que o sistema voltou
  (a amostragem não permite essa precisão).

O cálculo de glosa contratual (desconto financeiro por descumprimento de
SLA) depende da cláusula exata do contrato e NÃO é inventado aqui: fica
como placeholder até a fórmula real ser informada (ver config.yaml ->
sla.glosa_formula).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .config import SlaSettings


class CheckRow(Protocol):
    """Qualquer registro de checagem com horário e resultado — tanto o
    modelo Check quanto as linhas enxutas lidas pelo relatório."""

    checked_at: datetime
    success: bool


@dataclass
class CurrentStatus:
    up: bool
    last_checked_at: datetime
    last_status_code: int | None
    last_error: str | None
    down_since: datetime | None  # se up=False, desde quando está fora do ar


@dataclass
class Incident:
    start: datetime  # primeira checagem com falha
    recovered_at: datetime | None  # primeira checagem OK depois (None = em andamento)
    failed_checks: int
    downtime_seconds: float


@dataclass
class WindowSummary:
    label: str
    total_checks: int
    successful_checks: int
    uptime_pct: float | None  # None se não houve checagens na janela
    expected_checks: int
    coverage_pct: float | None


def observed_interval_seconds(rows: Sequence[CheckRow], fallback_seconds: int) -> float:
    """Intervalo real entre checagens, medido pela mediana dos intervalos
    observados. É o que vale para a apuração: o agendador pode não ter
    rodado na cadência configurada (o cron do GitHub Actions, por exemplo,
    é "melhor esforço")."""
    if len(rows) < 2:
        return float(fallback_seconds)
    gaps = [
        (rows[i].checked_at - rows[i - 1].checked_at).total_seconds()
        for i in range(1, len(rows))
    ]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return float(fallback_seconds)
    return median(gaps)


def compute_current_status(
    rows_asc: Sequence[CheckRow],
    last_status_code: int | None = None,
    last_error: str | None = None,
) -> CurrentStatus | None:
    """`rows_asc` deve estar ordenado do mais antigo para o mais recente,
    de um único sistema."""
    if not rows_asc:
        return None

    latest = rows_asc[-1]
    down_since = None
    if not latest.success:
        down_since = latest.checked_at
        for i in range(len(rows_asc) - 2, -1, -1):
            if rows_asc[i].success:
                break
            down_since = rows_asc[i].checked_at

    return CurrentStatus(
        up=latest.success,
        last_checked_at=latest.checked_at,
        last_status_code=last_status_code,
        last_error=last_error,
        down_since=down_since,
    )


def summarize_window(
    rows_in_window: Sequence[CheckRow],
    label: str,
    window_start: datetime,
    window_end: datetime,
    interval_seconds: float,
) -> WindowSummary:
    total = len(rows_in_window)
    successful = sum(1 for row in rows_in_window if row.success)
    uptime_pct = (successful / total * 100) if total else None

    span_seconds = max(0.0, (window_end - window_start).total_seconds())
    expected = int(span_seconds // interval_seconds) if interval_seconds > 0 else 0
    # Acima de 100% não agrega informação (jitter do agendador) e confunde
    # a leitura do painel.
    coverage = min(100.0, total / expected * 100) if expected > 0 else None

    return WindowSummary(
        label=label,
        total_checks=total,
        successful_checks=successful,
        uptime_pct=uptime_pct,
        expected_checks=expected,
        coverage_pct=coverage,
    )


def compute_incidents(
    rows_asc: Sequence[CheckRow],
    interval_seconds: float,
    limit: int | None = None,
) -> list[Incident]:
    """`rows_asc` deve estar ordenado do mais antigo para o mais recente,
    de um único sistema. Agrupa sequências consecutivas de falha."""
    incidents: list[Incident] = []
    start: datetime | None = None
    failed = 0

    for row in rows_asc:
        if not row.success:
            if start is None:
                start = row.checked_at
            failed += 1
        elif start is not None:
            incidents.append(
                Incident(
                    start=start,
                    recovered_at=row.checked_at,
                    failed_checks=failed,
                    downtime_seconds=failed * interval_seconds,
                )
            )
            start = None
            failed = 0

    if start is not None:
        incidents.append(
            Incident(
                start=start,
                recovered_at=None,
                failed_checks=failed,
                downtime_seconds=failed * interval_seconds,
            )
        )

    incidents.sort(key=lambda i: i.start, reverse=True)
    if limit is not None:
        incidents = incidents[:limit]
    return incidents


def month_window(now_utc: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Início do mês calendário NO FUSO DO CONTRATO, devolvido em UTC naive
    (mesmo formato gravado no banco). Ancorar em UTC jogaria as últimas 3
    horas de cada mês brasileiro para o mês seguinte da apuração."""
    tz = ZoneInfo(tz_name)
    local_now = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, now_utc


def to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    """Converte um horário UTC naive (como gravado no banco) para o fuso de
    exibição."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


def sla_compliance(uptime_pct: float | None, sla: SlaSettings) -> dict | None:
    """Resumo de conformidade com a meta de SLA, sem calcular valor de
    glosa (fórmula ainda não informada)."""
    if sla.target_pct is None or uptime_pct is None:
        return None
    return {
        "target_pct": sla.target_pct,
        "actual_pct": uptime_pct,
        "met": uptime_pct >= sla.target_pct,
        "deficit_pct": max(0.0, sla.target_pct - uptime_pct),
    }


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}min {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}min"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {minutes}min"


def window_bounds(now_utc: datetime, days: int) -> tuple[datetime, datetime]:
    return now_utc - timedelta(days=days), now_utc
