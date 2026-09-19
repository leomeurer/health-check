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

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from .config import PROJECT_ROOT, load_config, resolve_db_url
from .db import Check, make_engine, make_session_factory
from .sla import (
    compute_current_status,
    compute_incidents,
    compute_uptime_pct,
    current_month_window,
    sla_compliance,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
LOOKBACK_WINDOW = timedelta(days=30)
SPARKLINE_MAX_TICKS = 60


def _fmt_pct(pct: float | None) -> str:
    return "-" if pct is None else f"{pct:.2f}%"


def _fmt_dt(dt) -> str:
    return "-" if dt is None else dt.strftime("%d/%m/%Y %H:%M:%S UTC")


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "em andamento"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}min {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}min"


def build_report_data(config, session_factory, now_fn) -> dict:
    now = now_fn()
    windows = [
        ("24h", now - timedelta(hours=24)),
        ("7 dias", now - timedelta(days=7)),
        ("30 dias", now - timedelta(days=30)),
    ]
    month_start, _ = current_month_window(now)
    windows.append(("Mês corrente", month_start))

    targets_data = []
    with session_factory() as session:
        for target in config.targets:
            stmt = (
                select(Check)
                .where(Check.target_key == target.key, Check.checked_at >= now - LOOKBACK_WINDOW)
                .order_by(Check.checked_at.asc())
            )
            checks_asc = list(session.execute(stmt).scalars())
            checks_desc = list(reversed(checks_asc))

            status = compute_current_status(checks_desc)

            uptime_by_window = []
            month_uptime_pct = None
            for label, since in windows:
                relevant = [c for c in checks_asc if c.checked_at >= since]
                summary = compute_uptime_pct(relevant, target.key, label)
                uptime_by_window.append((label, summary))
                if label == "Mês corrente":
                    month_uptime_pct = summary.uptime_pct

            incidents = compute_incidents(checks_asc, limit=10)
            sparkline = checks_asc[-SPARKLINE_MAX_TICKS:]
            sla_summary = sla_compliance(month_uptime_pct, config.sla)

            targets_data.append(
                {
                    "target": target,
                    "status": status,
                    "uptime_by_window": uptime_by_window,
                    "incidents": incidents,
                    "sparkline": sparkline,
                    "sla_summary": sla_summary,
                    "total_checks_30d": len(checks_asc),
                }
            )

    return {
        "generated_at": now,
        "targets_data": targets_data,
        "sla_configured": config.sla.target_pct is not None,
        "sla_target_pct": config.sla.target_pct,
    }


def render_report(data: dict, output_path: Path) -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["fmt_pct"] = _fmt_pct
    env.filters["fmt_dt"] = _fmt_dt
    env.filters["fmt_duration"] = _fmt_duration
    template = env.get_template("report.html.j2")
    html = template.render(**data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def main() -> None:
    from .db import utcnow

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
    session_factory = make_session_factory(engine)

    data = build_report_data(config, session_factory, utcnow)
    output_path = Path(args.output)
    render_report(data, output_path)
    print(f"Relatório gerado em {output_path}")


if __name__ == "__main__":
    sys.exit(main())
