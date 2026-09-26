"""Gera um painel HTML estático (sem dependências externas/CDN) a partir
dos dados coletados pelo daemon.

Uso:
    python -m healthcheck.report
    python -m healthcheck.report --config path/to/config.yaml --output data/report.html
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import and_, case, func, or_, select

from .config import PROJECT_ROOT, load_config, resolve_db_url
from .db import CLOUDFLARE_CHALLENGE, Check, init_db, make_engine, make_session_factory, utcnow
from .sla import (
    EndpointRow,
    blocked_since,
    combine_endpoint_rows,
    compute_current_status,
    compute_incidents,
    failed_keys_between,
    format_duration,
    MIN_COVERAGE_PCT,
    measured_rows,
    month_cell,
    month_length_seconds,
    month_window,
    observed_interval_seconds,
    sla_compliance,
    sla_definitely_missed,
    summarize_window,
    to_local,
    window_bounds,
    year_month_bounds,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# Arquivos publicados ao lado do HTML: ECharts 5.6.0 (Apache-2.0), versionado
# no repositório para o painel não depender de CDN, e o script do gráfico
# (separado do HTML para permitir uma CSP sem script inline).
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = ("echarts.min.js", "heatmap.js")
MONTH_LABELS = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
LOOKBACK_DAYS = 30
SPARKLINE_MAX_TICKS = 60
INCIDENTS_SHOWN = 10


def _fmt_pct(pct: float | None) -> str:
    return "-" if pct is None else f"{pct:.2f}%"


def _accepted_label(target) -> str:
    """Códigos que contam como "no ar" para o endpoint, como exibido no painel."""
    if target.expected_status is None:
        base = "200–399"
    else:
        base = ", ".join(str(code) for code in sorted(target.expected_status))
    extra = sorted(target.also_accept - (target.expected_status or frozenset()))
    return base + "".join(f", {code}" for code in extra)


def _monthly_round_counts(session, keys: list[str], bounds) -> dict[int, tuple[int, int, int]]:
    """Por mês do ano (0-11): (rodadas, rodadas com falha real, rodadas só
    bloqueadas) do sistema formado por `keys`. Mesma regra de
    combine_endpoint_rows, mas agregada no banco: uma rodada falha se algum
    endpoint falhou de verdade; é bloqueada se nenhum falhou e algum caiu no
    desafio do Cloudflare."""
    real_failure = case(
        (
            and_(
                Check.success.is_(False),
                or_(Check.error.is_(None), Check.error != CLOUDFLARE_CHALLENGE),
            ),
            1,
        ),
        else_=0,
    )
    blocked = case((Check.error == CLOUDFLARE_CHALLENGE, 1), else_=0)
    rounds = (
        select(
            Check.checked_at.label("at"),
            func.max(real_failure).label("failed"),
            func.max(blocked).label("blocked"),
        )
        .where(Check.target_key.in_(keys), Check.checked_at >= bounds[0], Check.checked_at < bounds[-1])
        .group_by(Check.checked_at)
        .subquery()
    )
    # CASE com os limites já em UTC, em vez de funções de data do banco:
    # funciona igual no SQLite e no Oracle, e respeita o fuso do contrato.
    month = case(
        *[(and_(rounds.c.at >= bounds[i], rounds.c.at < bounds[i + 1]), i) for i in range(12)]
    )
    query = select(
        month.label("month"),
        func.count(),
        func.sum(rounds.c.failed),
        func.sum(case((and_(rounds.c.failed == 0, rounds.c.blocked == 1), 1), else_=0)),
    ).group_by(month)
    return {
        int(m): (int(total), int(failed or 0), int(only_blocked or 0))
        for m, total, failed, only_blocked in session.execute(query).all()
    }


def _heatmap_data(config, session, systems_data, now) -> dict:
    """Dados do gráfico mensal do painel: um valor por sistema e mês, com a
    mesma apuração dos blocos (bloqueio fora da conta, cobertura mínima)."""
    tz_name = config.sla.timezone
    bounds = year_month_bounds(now, tz_name)
    cells = []
    for row_index, item in enumerate(systems_data):
        system = item["system"]
        counts = _monthly_round_counts(session, [t.key for t in system.endpoints], bounds)
        for month_index, (total, failed, only_blocked) in counts.items():
            month_start, month_end = bounds[month_index], bounds[month_index + 1]
            cell = month_cell(
                total,
                failed,
                only_blocked,
                elapsed_seconds=(min(now, month_end) - month_start).total_seconds(),
                month_seconds=(month_end - month_start).total_seconds(),
                interval_seconds=item["interval_seconds"],
                target_pct=config.sla.target_pct,
            )
            if cell is None:
                continue
            cells.append(
                {
                    "x": month_index,
                    "y": row_index,
                    "state": cell.state,
                    "uptime": cell.uptime_pct,
                    "coverage": cell.coverage_pct,
                    "measured": cell.measured_rounds,
                    "failed": cell.failed_rounds,
                    "blocked": cell.blocked_rounds,
                }
            )
    local_now = to_local(now, tz_name)
    return {
        "year": local_now.year,
        "months": MONTH_LABELS,
        "systems": [item["system"].short_name for item in systems_data],
        "system_names": [item["system"].name for item in systems_data],
        "cells": cells,
        "target_pct": config.sla.target_pct,
        "min_coverage_pct": MIN_COVERAGE_PCT,
        "current_month": local_now.month - 1,
    }


def build_report_data(config, session_factory, now) -> dict:
    tz_name = config.sla.timezone
    month_start, _ = month_window(now, tz_name)
    thirty_days_start, _ = window_bounds(now, LOOKBACK_DAYS)
    # A janela do mês pode começar antes dos 30 dias (todo dia 31), e truncar
    # aí falsearia justamente o número que sustenta a glosa.
    lookback_start = min(thirty_days_start, month_start)

    windows = [
        ("24h", now - timedelta(hours=24)),
        ("7 dias", now - timedelta(days=7)),
        ("30 dias", thirty_days_start),
        ("Mês corrente", month_start),
    ]

    systems_data = []
    with session_factory() as session:
        for system in config.systems:
            rows_by_endpoint = {}
            endpoints_data = []
            for target in system.endpoints:
                rows = [
                    EndpointRow(
                        checked_at=row.checked_at,
                        success=row.success,
                        blocked=row.error == CLOUDFLARE_CHALLENGE,
                    )
                    for row in session.execute(
                        select(Check.checked_at, Check.success, Check.error)
                        .where(
                            Check.target_key == target.key,
                            Check.checked_at >= lookback_start,
                        )
                        .order_by(Check.checked_at.asc())
                    ).all()
                ]
                rows_by_endpoint[target.key] = rows

                latest = session.execute(
                    select(Check.checked_at, Check.success, Check.status_code, Check.error)
                    .where(Check.target_key == target.key)
                    .order_by(Check.checked_at.desc())
                    .limit(1)
                ).first()

                month_rows = [
                    row for row in rows if row.checked_at >= month_start and not row.blocked
                ]
                month_ok = sum(1 for row in month_rows if row.success)
                endpoints_data.append(
                    {
                        "target": target,
                        "latest": latest,
                        "latest_blocked": bool(latest) and latest.error == CLOUDFLARE_CHALLENGE,
                        "latest_local": to_local(latest.checked_at, tz_name) if latest else None,
                        "month_uptime_pct": month_ok / len(month_rows) * 100 if month_rows else None,
                        "accepted_label": _accepted_label(target),
                        "also_accept": sorted(target.also_accept),
                    }
                )

            # A apuração do sistema é feita sobre as rodadas consolidadas: uma
            # falha de QUALQUER endpoint derruba o sistema naquela rodada.
            all_rows = combine_endpoint_rows(rows_by_endpoint)
            labels = {t.key: t.label for t in system.endpoints}

            # A cadência é a do monitor, então conta todas as rodadas; o resto
            # da apuração só enxerga as que mediram (bloqueio = lacuna).
            interval = observed_interval_seconds(all_rows, config.check.interval_seconds)
            rows = measured_rows(all_rows)
            status = compute_current_status(rows)
            blocked_from = blocked_since(all_rows)

            summaries = []
            month_summary = None
            for label, start in windows:
                in_window = [row for row in rows if row.checked_at >= start]
                summary = summarize_window(in_window, label, start, now, interval)
                summaries.append(summary)
                if label == "Mês corrente":
                    month_summary = summary

            systems_data.append(
                {
                    "system": system,
                    "endpoints": endpoints_data,
                    "status": status,
                    "blocked_since_local": to_local(blocked_from, tz_name),
                    "blocked_labels": [labels[k] for k in sorted(all_rows[-1].blocked_keys)]
                    if blocked_from
                    else [],
                    "blocked_rounds_month": sum(
                        1 for row in all_rows if row.blocked and row.checked_at >= month_start
                    ),
                    "down_labels": [labels[k] for k in sorted(rows[-1].failed_keys)] if rows else [],
                    "status_down_since_local": to_local(status.down_since, tz_name)
                    if status
                    else None,
                    "last_round_local": to_local(all_rows[-1].checked_at, tz_name)
                    if all_rows
                    else None,
                    "summaries": summaries,
                    "incidents": [
                        {
                            "start": to_local(inc.start, tz_name),
                            "recovered_at": to_local(inc.recovered_at, tz_name),
                            "failed_checks": inc.failed_checks,
                            "downtime": format_duration(inc.downtime_seconds),
                            "failed_labels": [
                                labels.get(k, k)
                                for k in sorted(failed_keys_between(rows, inc.start, inc.recovered_at))
                            ],
                        }
                        for inc in compute_incidents(rows, interval, limit=INCIDENTS_SHOWN)
                    ],
                    "sparkline": [
                        "blocked" if row.blocked else ("ok" if row.success else "down")
                        for row in all_rows[-SPARKLINE_MAX_TICKS:]
                    ],
                    "sla_summary": sla_compliance(
                        month_summary.uptime_pct if month_summary else None, config.sla
                    ),
                    "month_coverage_pct": month_summary.coverage_pct if month_summary else None,
                    "sla_definitely_missed": bool(month_summary)
                    and sla_definitely_missed(
                        month_summary.total_checks - month_summary.successful_checks,
                        interval,
                        month_length_seconds(now, tz_name),
                        config.sla.target_pct,
                    ),
                    "interval_label": format_duration(interval),
                    "interval_seconds": interval,
                }
            )

        heatmap = _heatmap_data(config, session, systems_data, now)

    return {
        "generated_at": to_local(now, tz_name),
        "timezone": tz_name,
        "systems_data": systems_data,
        "heatmap": heatmap,
        "min_coverage_pct": MIN_COVERAGE_PCT,
        "sla_configured": config.sla.target_pct is not None,
        "lookback_days": LOOKBACK_DAYS,
    }


def render_report(data: dict, output_path: Path) -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        # Escapa sempre: o painel é publicado e renderiza texto vindo da
        # configuração e das mensagens de erro dos servidores checados.
        autoescape=True,
    )
    env.filters["fmt_pct"] = _fmt_pct
    env.filters["fmt_dt"] = lambda dt: "-" if dt is None else dt.strftime("%d/%m/%Y %H:%M:%S")
    template = env.get_template("report.html.j2")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(**data), encoding="utf-8")
    _publish_static(output_path.parent)


def _publish_static(output_dir: Path) -> None:
    """Coloca os scripts ao lado do HTML (o painel os referencia pelo
    caminho relativo). Só regrava o que mudou, para o navegador poder
    manter em cache."""
    for name in STATIC_FILES:
        target = output_dir / name
        content = (STATIC_DIR / name).read_bytes()
        if not target.exists() or target.read_bytes() != content:
            target.write_bytes(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera o painel HTML de uptime/downtime")
    parser.add_argument("--config", default=None, help="Caminho para config.yaml")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "data" / "report.html"),
        help="Caminho do HTML de saída",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    engine = make_engine(resolve_db_url(config))
    # O relatório pode rodar antes da primeira execução do daemon (o timer do
    # systemd é independente); sem isso a tabela não existiria.
    init_db(engine)
    session_factory = make_session_factory(engine)

    data = build_report_data(config, session_factory, utcnow())
    output_path = Path(args.output)
    render_report(data, output_path)
    print(f"Relatório gerado em {output_path}")


if __name__ == "__main__":
    sys.exit(main())
