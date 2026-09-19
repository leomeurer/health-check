"""Testes da aritmética que sustenta a apuração de SLA e glosa.

Rodar com:  PYTHONPATH=src python -m pytest tests/ -q
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from healthcheck.config import ConfigError, SlaSettings, _validate_url
from healthcheck.sla import (
    compute_current_status,
    compute_incidents,
    month_window,
    observed_interval_seconds,
    sla_compliance,
    summarize_window,
)

INTERVAL = 60


@dataclass
class Row:
    checked_at: datetime
    success: bool


def make_rows(pattern: str, start: datetime, interval: int = INTERVAL) -> list[Row]:
    """'oox' = sucesso, sucesso, falha — uma checagem por intervalo."""
    return [
        Row(checked_at=start + timedelta(seconds=i * interval), success=char == "o")
        for i, char in enumerate(pattern)
    ]


BASE = datetime(2026, 3, 10, 12, 0, 0)


def test_uptime_conta_falhas_como_proporcao_das_checagens():
    rows = make_rows("oooooooooX".replace("X", "x"), BASE)
    summary = summarize_window(rows, "teste", BASE, BASE + timedelta(seconds=10 * INTERVAL), INTERVAL)
    assert summary.total_checks == 10
    assert summary.successful_checks == 9
    assert summary.uptime_pct == pytest.approx(90.0)


def test_cobertura_denuncia_janela_sem_dados():
    """O ponto crítico: 10 checagens numa janela de 30 dias são 100% de
    uptime, mas apenas ~0,02% de cobertura — não sustenta glosa."""
    rows = make_rows("oooooooooo", BASE)
    summary = summarize_window(rows, "30 dias", BASE, BASE + timedelta(days=30), INTERVAL)
    assert summary.uptime_pct == pytest.approx(100.0)
    assert summary.coverage_pct < 1


def test_cobertura_nao_passa_de_100():
    rows = make_rows("oooooooooo", BASE)
    summary = summarize_window(rows, "teste", BASE, BASE + timedelta(seconds=5 * INTERVAL), INTERVAL)
    assert summary.coverage_pct == 100.0


def test_incidente_isolado_conta_um_intervalo():
    rows = make_rows("ooxoo", BASE)
    (incident,) = compute_incidents(rows, INTERVAL)
    assert incident.failed_checks == 1
    assert incident.downtime_seconds == INTERVAL
    assert incident.start == BASE + timedelta(seconds=2 * INTERVAL)
    assert incident.recovered_at == BASE + timedelta(seconds=3 * INTERVAL)


def test_incidente_em_andamento_nao_tem_recuperacao():
    rows = make_rows("ooxxx", BASE)
    (incident,) = compute_incidents(rows, INTERVAL)
    assert incident.recovered_at is None
    assert incident.failed_checks == 3
    assert incident.downtime_seconds == 3 * INTERVAL


def test_incidentes_multiplos_vem_do_mais_recente_para_o_mais_antigo():
    rows = make_rows("xoooxxo", BASE)
    incidents = compute_incidents(rows, INTERVAL)
    assert len(incidents) == 2
    assert incidents[0].start > incidents[1].start
    assert incidents[0].failed_checks == 2
    assert incidents[1].failed_checks == 1


def test_status_atual_aponta_inicio_da_queda_em_curso():
    rows = make_rows("ooxxx", BASE)
    status = compute_current_status(rows)
    assert status.up is False
    assert status.down_since == BASE + timedelta(seconds=2 * INTERVAL)


def test_status_atual_online_nao_tem_down_since():
    status = compute_current_status(make_rows("xxo", BASE))
    assert status.up is True
    assert status.down_since is None


def test_janela_do_mes_usa_fuso_do_contrato_e_nao_utc():
    """1º de março 01:00 UTC ainda é 28 de fevereiro no Brasil: a janela do
    mês tem que começar em 1º de março 03:00 UTC (00:00 local)."""
    start, _ = month_window(datetime(2026, 3, 15, 12, 0), "America/Sao_Paulo")
    assert start == datetime(2026, 3, 1, 3, 0)


def test_cadencia_observada_ignora_a_configurada():
    rows = make_rows("ooooo", BASE, interval=300)
    assert observed_interval_seconds(rows, fallback_seconds=60) == 300


def test_cadencia_observada_usa_fallback_sem_dados_suficientes():
    assert observed_interval_seconds([], fallback_seconds=60) == 60


def test_sla_compliance_calcula_deficit():
    result = sla_compliance(99.0, SlaSettings(target_pct=99.5))
    assert result["met"] is False
    assert result["deficit_pct"] == pytest.approx(0.5)


def test_sla_compliance_sem_meta_configurada():
    assert sla_compliance(99.0, SlaSettings(target_pct=None)) is None


def test_url_sem_esquema_e_rejeitada_na_configuracao():
    """Uma URL com erro de digitação viraria downtime fictício."""
    with pytest.raises(ConfigError):
        _validate_url("acessounico.mec.gov.br", "acesso_unico")


def test_url_valida_passa():
    assert _validate_url("https://acessounico.mec.gov.br/", "acesso_unico")
