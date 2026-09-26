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
from .db import CLOUDFLARE_CHALLENGE, Check, init_db, make_engine, make_session_factory, utcnow
from .sla import (
    EndpointRow,
    blocked_since,
    combine_endpoint_rows,
    compute_current_status,
    compute_incidents,
    failed_keys_between,
    format_duration,
    measured_rows,
    month_length_seconds,
    month_window,
    observed_interval_seconds,
    sla_compliance,
    sla_definitely_missed,
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


def _accepted_label(target) -> str:
    """Códigos que contam como "no ar" para o endpoint, como exibido no painel."""
    if target.expected_status is None:
        base = "200–399"
    else:
        base = ", ".join(str(code) for code in sorted(target.expected_status))
    extra = sorted(target.also_accept - (target.expected_status or frozenset()))
    return base + "".join(f", {code}" for code in extra)


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
                }
            )

    return {
        "generated_at": to_local(now, tz_name),
        "timezone": tz_name,
        "systems_data": systems_data,
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
