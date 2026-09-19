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
from sqlalchemy import select

from .config import PROJECT_ROOT, load_config, resolve_db_url
from .db import Check, init_db, make_engine, make_session_factory, utcnow
from .sla import (
    compute_current_status,
    compute_incidents,
    format_duration,
    month_window,
    observed_interval_seconds,
    sla_compliance,
    summarize_window,
    to_local,
    window_bounds,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
LOOKBACK_DAYS = 30
SPARKLINE_MAX_TICKS = 60
INCIDENTS_SHOWN = 10


def _fmt_pct(pct: float | None) -> str:
    return "-" if pct is None else f"{pct:.2f}%"


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

    targets_data = []
    with session_factory() as session:
        for target in config.targets:
            rows = list(
                session.execute(
                    select(Check.checked_at, Check.success)
                    .where(
                        Check.target_key == target.key,
                        Check.checked_at >= lookback_start,
                    )
                    .order_by(Check.checked_at.asc())
                ).all()
            )

            latest_detail = session.execute(
                select(Check.status_code, Check.error)
                .where(Check.target_key == target.key)
                .order_by(Check.checked_at.desc())
                .limit(1)
            ).first()

            interval = observed_interval_seconds(rows, config.check.interval_seconds)
            status = compute_current_status(
                rows,
                last_status_code=latest_detail.status_code if latest_detail else None,
                last_error=latest_detail.error if latest_detail else None,
            )

            summaries = []
            month_summary = None
            for label, start in windows:
                in_window = [row for row in rows if row.checked_at >= start]
                summary = summarize_window(in_window, label, start, now, interval)
                summaries.append(summary)
                if label == "Mês corrente":
                    month_summary = summary

            targets_data.append(
                {
                    "target": target,
                    "status": status,
                    "status_down_since_local": to_local(status.down_since, tz_name)
                    if status
                    else None,
                    "status_last_checked_local": to_local(status.last_checked_at, tz_name)
                    if status
                    else None,
                    "summaries": summaries,
                    "incidents": [
                        {
                            "start": to_local(inc.start, tz_name),
                            "recovered_at": to_local(inc.recovered_at, tz_name),
                            "failed_checks": inc.failed_checks,
                            "downtime": format_duration(inc.downtime_seconds),
                        }
                        for inc in compute_incidents(rows, interval, limit=INCIDENTS_SHOWN)
                    ],
                    "sparkline": [row.success for row in rows[-SPARKLINE_MAX_TICKS:]],
                    "sla_summary": sla_compliance(
                        month_summary.uptime_pct if month_summary else None, config.sla
                    ),
                    "month_coverage_pct": month_summary.coverage_pct if month_summary else None,
                    "interval_label": format_duration(interval),
                }
            )

    return {
        "generated_at": to_local(now, tz_name),
        "timezone": tz_name,
        "targets_data": targets_data,
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
