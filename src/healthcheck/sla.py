"""Cálculo de uptime, incidentes e status atual a partir dos registros
de checagem.

Critério de indisponibilidade: UMA falha já conta como início de
downtime — não há tolerância de N falhas consecutivas.

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


@dataclass(frozen=True)
class EndpointRow:
    """Checagem de um endpoint como a apuração enxerga: `blocked` indica que
    o Cloudflare respondeu com o desafio anti-bot, ou seja, a rodada não
    mediu nada (nem sucesso, nem queda)."""

    checked_at: datetime
    success: bool
    blocked: bool = False


@dataclass(frozen=True)
class SystemRow:
    """Resultado consolidado de uma rodada para um sistema com vários
    endpoints: só é sucesso se todos os endpoints checados na rodada
    responderam OK."""

    checked_at: datetime
    success: bool
    failed_keys: frozenset[str]
    # Rodada sem medição: algum endpoint caiu no desafio do Cloudflare e
    # nenhum outro falhou de verdade. Fica fora do uptime e dos incidentes.
    blocked: bool = False
    blocked_keys: frozenset[str] = frozenset()


def combine_endpoint_rows(rows_by_endpoint: dict[str, Sequence[CheckRow]]) -> list[SystemRow]:
    """Junta as checagens dos endpoints de um sistema por rodada. O daemon
    grava todos os endpoints de uma rodada com o MESMO `checked_at`, então o
    horário identifica a rodada.

    Endpoint sem registro numa rodada (ex: incluído depois no config) não
    entra naquela rodada: antes de a API ser monitorada, o sistema era medido
    só pela página inicial, e contar a ausência como falha criaria downtime
    que ninguém observou.

    Uma falha real de qualquer endpoint prevalece sobre um bloqueio: se a API
    respondeu 503, o sistema estava fora, mesmo que a página tenha caído no
    desafio do Cloudflare."""
    failed: dict[datetime, set[str]] = {}
    blocked: dict[datetime, set[str]] = {}
    for key, rows in rows_by_endpoint.items():
        for row in rows:
            failed.setdefault(row.checked_at, set())
            blocked.setdefault(row.checked_at, set())
            if getattr(row, "blocked", False):
                blocked[row.checked_at].add(key)
            elif not row.success:
                failed[row.checked_at].add(key)
    return [
        SystemRow(
            checked_at=at,
            success=not failed[at] and not blocked[at],
            failed_keys=frozenset(failed[at]),
            blocked=not failed[at] and bool(blocked[at]),
            blocked_keys=frozenset(blocked[at]),
        )
        for at in sorted(failed)
    ]


def measured_rows(rows: Sequence[SystemRow]) -> list[SystemRow]:
    """Só as rodadas que de fato mediram o sistema. As bloqueadas viram
    lacuna: a cobertura cai e o uptime não finge um resultado."""
    return [row for row in rows if not row.blocked]


def blocked_since(rows_asc: Sequence[SystemRow]) -> datetime | None:
    """Se a rodada mais recente está bloqueada, desde quando (início da
    sequência de rodadas bloqueadas). None se a última rodada mediu."""
    if not rows_asc or not rows_asc[-1].blocked:
        return None
    since = rows_asc[-1].checked_at
    for row in reversed(rows_asc[:-1]):
        if not row.blocked:
            break
        since = row.checked_at
    return since


def failed_keys_between(
    rows_asc: Sequence[SystemRow], start: datetime, end: datetime | None
) -> set[str]:
    """Endpoints que falharam em alguma rodada de [start, end) — usado para
    dizer, em cada incidente, QUAL parte do sistema caiu."""
    return {
        key
        for row in rows_asc
        if row.checked_at >= start and (end is None or row.checked_at < end)
        for key in row.failed_keys
    }


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
    rodado na cadência configurada (daemon parado, VM sobrecarregada ou
    reiniciada)."""
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


def month_length_seconds(now_utc: datetime, tz_name: str) -> float:
    """Duração do mês calendário corrente, no fuso do contrato."""
    tz = ZoneInfo(tz_name)
    local_now = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()


def sla_definitely_missed(
    failed_checks: int, interval_seconds: float, month_seconds: float, target_pct: float | None
) -> bool:
    """Com cobertura baixa não dá para dizer que a meta foi cumprida — mas dá
    para dizer que NÃO foi, quando o downtime já medido passa sozinho da
    margem que a meta permite no mês inteiro (ex: 98% num mês de 30 dias =
    14h24min). Nada que falte medir consegue desfazer isso."""
    if target_pct is None or interval_seconds <= 0:
        return False
    allowed_downtime = (1 - target_pct / 100) * month_seconds
    return failed_checks * interval_seconds > allowed_downtime


# Abaixo disso, o percentual do mês não sustenta afirmar se a meta foi
# cumprida (mesmo limiar usado nos blocos de cada sistema no painel).
MIN_COVERAGE_PCT = 95.0


def year_month_bounds(now_utc: datetime, tz_name: str) -> list[datetime]:
    """Os 13 limites (1º de jan. ... 1º de jan. do ano seguinte) dos meses do
    ano corrente NO FUSO DO CONTRATO, em UTC naive como no banco."""
    tz = ZoneInfo(tz_name)
    year = now_utc.replace(tzinfo=timezone.utc).astimezone(tz).year
    bounds = []
    for i in range(13):
        local = datetime(year + i // 12, i % 12 + 1, 1, tzinfo=tz)
        bounds.append(local.astimezone(timezone.utc).replace(tzinfo=None))
    return bounds


@dataclass
class MonthCell:
    """Uma célula do gráfico mensal: o SLA apurado de um sistema num mês."""

    state: str  # "met" | "missed" | "not_assessable" | "blocked" | "measured"
    uptime_pct: float | None
    coverage_pct: float | None
    measured_rounds: int
    failed_rounds: int
    blocked_rounds: int


def month_cell(
    total_rounds: int,
    failed_rounds: int,
    blocked_rounds: int,
    elapsed_seconds: float,
    month_seconds: float,
    interval_seconds: float,
    target_pct: float | None,
) -> MonthCell | None:
    """SLA de um mês a partir das contagens de rodadas. None = mês sem
    nenhuma rodada (futuro ou antes do monitor existir): célula vazia.

    `elapsed_seconds` é o trecho do mês já decorrido (o mês inteiro, se já
    fechou): a cobertura é contra o que era possível medir até agora."""
    if total_rounds == 0:
        return None
    measured = total_rounds - blocked_rounds
    if measured == 0:
        return MonthCell("blocked", None, 0.0, 0, 0, blocked_rounds)

    uptime = (measured - failed_rounds) / measured * 100
    expected = int(elapsed_seconds // interval_seconds) if interval_seconds > 0 else 0
    coverage = min(100.0, measured / expected * 100) if expected > 0 else None

    if target_pct is None:
        state = "measured"
    elif sla_definitely_missed(failed_rounds, interval_seconds, month_seconds, target_pct):
        state = "missed"
    elif coverage is None or coverage < MIN_COVERAGE_PCT:
        state = "not_assessable"
    else:
        state = "met" if uptime >= target_pct else "missed"
    return MonthCell(state, uptime, coverage, measured, failed_rounds, blocked_rounds)


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
