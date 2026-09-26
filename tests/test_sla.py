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


def test_also_accept_trata_403_como_no_ar():
    from healthcheck.checker import _status_accepted
    from healthcheck.config import Target

    bloqueado = Target(key="x", name="X", url="https://x.gov.br/", also_accept=frozenset({403}))
    assert _status_accepted(403, bloqueado) is True
    assert _status_accepted(200, bloqueado) is True
    assert _status_accepted(500, bloqueado) is False


def test_sem_also_accept_403_e_falha():
    from healthcheck.checker import _status_accepted
    from healthcheck.config import Target

    normal = Target(key="x", name="X", url="https://x.gov.br/")
    assert _status_accepted(403, normal) is False
    assert _status_accepted(200, normal) is True


def test_config_carrega_also_accept():
    import tempfile

    import yaml

    from healthcheck.config import load_config

    cfg = {
        "targets": [
            {"key": "emec", "name": "e-MEC", "url": "https://emec.mec.gov.br/", "also_accept": [403]}
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(cfg, f)
        path = f.name
    loaded = load_config(path)
    assert loaded.targets[0].also_accept == frozenset({403})


# --- Sistemas com vários endpoints (página + API) -----------------------

def _endpoint_rows(pattern: str) -> list[Row]:
    return make_rows(pattern, BASE)


def test_sistema_so_esta_no_ar_se_todos_os_endpoints_estao():
    from healthcheck.sla import combine_endpoint_rows

    rows = combine_endpoint_rows({"gpe": _endpoint_rows("oooo"), "gpe_api": _endpoint_rows("oxxo")})
    assert [row.success for row in rows] == [True, False, False, True]
    assert rows[1].failed_keys == frozenset({"gpe_api"})


def test_queda_de_qualquer_endpoint_entra_no_uptime_do_sistema():
    from healthcheck.sla import combine_endpoint_rows

    rows = combine_endpoint_rows({"pagina": _endpoint_rows("ooxoo"), "api": _endpoint_rows("oooox")})
    summary = summarize_window(rows, "teste", BASE, BASE + timedelta(seconds=5 * INTERVAL), INTERVAL)
    assert summary.total_checks == 5
    assert summary.uptime_pct == pytest.approx(60.0)


def test_endpoint_incluido_depois_nao_gera_downtime_retroativo():
    """Antes de a API entrar no config, o sistema era medido só pela página:
    a falta de registro da API nessas rodadas não pode contar como falha."""
    from healthcheck.sla import combine_endpoint_rows

    pagina = _endpoint_rows("oooo")
    api = pagina[2:]  # API só começou a ser checada na 3ª rodada
    rows = combine_endpoint_rows({"pagina": pagina, "api": [Row(r.checked_at, True) for r in api]})
    assert len(rows) == 4
    assert all(row.success for row in rows)


def test_incidente_do_sistema_informa_qual_endpoint_caiu():
    from healthcheck.sla import combine_endpoint_rows, failed_keys_between

    rows = combine_endpoint_rows({"pagina": _endpoint_rows("oxxoo"), "api": _endpoint_rows("ooxoo")})
    (incident,) = compute_incidents(rows, INTERVAL)
    assert incident.failed_checks == 2
    assert failed_keys_between(rows, incident.start, incident.recovered_at) == {"pagina", "api"}


def _load_yaml_config(cfg: dict):
    import tempfile

    import yaml

    from healthcheck.config import load_config

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(cfg, f)
    return load_config(f.name)


def test_config_agrupa_extra_checks_no_mesmo_sistema():
    loaded = _load_yaml_config(
        {
            "targets": [
                {
                    "key": "gpe",
                    "name": "GPE",
                    "url": "https://gpe.gov.br/",
                    "extra_checks": [
                        {
                            "key": "gpe_api",
                            "label": "API",
                            "url": "https://api.gpe.gov.br/",
                            "expected_status": [200, 401],
                        }
                    ],
                },
                {"key": "emec", "name": "e-MEC", "url": "https://emec.gov.br/"},
            ]
        }
    )
    # O daemon continua vendo uma lista plana; a chave antiga "gpe" é mantida.
    assert [t.key for t in loaded.targets] == ["gpe", "gpe_api", "emec"]
    (gpe, emec) = loaded.systems
    assert [t.key for t in gpe.endpoints] == ["gpe", "gpe_api"]
    assert gpe.endpoints[1].system_key == "gpe"
    assert gpe.endpoints[1].expected_status == frozenset({200, 401})
    assert len(emec.endpoints) == 1


def test_config_rejeita_chave_de_extra_check_repetida():
    with pytest.raises(ConfigError):
        _load_yaml_config(
            {
                "targets": [
                    {
                        "key": "gpe",
                        "name": "GPE",
                        "url": "https://gpe.gov.br/",
                        "extra_checks": [{"key": "gpe", "label": "API", "url": "https://api.gpe.gov.br/"}],
                    }
                ]
            }
        )


def test_api_aceita_200_e_401():
    from healthcheck.checker import _status_accepted
    from healthcheck.config import Target

    api = Target(key="api", name="API", url="https://api.gov.br/", expected_status=frozenset({200, 401}))
    assert _status_accepted(200, api) is True
    assert _status_accepted(401, api) is True
    assert _status_accepted(503, api) is False
    assert _status_accepted(302, api) is False


# --- Bloqueio Cloudflare (rodada sem medição) ---------------------------

def _ep(pattern: str):
    """'o' = OK, 'x' = falha real, 'b' = desafio do Cloudflare."""
    from healthcheck.sla import EndpointRow

    return [
        EndpointRow(checked_at=BASE + timedelta(seconds=i * INTERVAL), success=c == "o", blocked=c == "b")
        for i, c in enumerate(pattern)
    ]


def test_bloqueio_nao_conta_como_no_ar_nem_como_queda():
    from healthcheck.sla import combine_endpoint_rows, measured_rows

    rows = combine_endpoint_rows({"emec": _ep("oobbo")})
    medidas = measured_rows(rows)
    summary = summarize_window(medidas, "teste", BASE, BASE + timedelta(seconds=5 * INTERVAL), INTERVAL)
    assert summary.total_checks == 3
    assert summary.uptime_pct == pytest.approx(100.0)
    assert summary.coverage_pct == pytest.approx(60.0)  # a lacuna aparece na cobertura
    assert compute_incidents(medidas, INTERVAL) == []


def test_so_bloqueio_no_mes_deixa_sla_nao_apuravel():
    from healthcheck.sla import combine_endpoint_rows, measured_rows

    medidas = measured_rows(combine_endpoint_rows({"emec": _ep("bbbb")}))
    summary = summarize_window(medidas, "mês", BASE, BASE + timedelta(seconds=4 * INTERVAL), INTERVAL)
    assert summary.uptime_pct is None
    assert sla_compliance(summary.uptime_pct, SlaSettings(target_pct=98.0)) is None


def test_falha_real_de_outro_endpoint_prevalece_sobre_bloqueio():
    from healthcheck.sla import combine_endpoint_rows

    rows = combine_endpoint_rows({"pagina": _ep("bb"), "api": _ep("ox")})
    assert rows[0].blocked is True and rows[0].success is False
    assert rows[1].blocked is False and rows[1].success is False
    assert rows[1].failed_keys == frozenset({"api"})


def test_bloqueado_desde_aponta_inicio_da_sequencia_atual():
    from healthcheck.sla import blocked_since, combine_endpoint_rows

    rows = combine_endpoint_rows({"emec": _ep("obobb")})
    assert blocked_since(rows) == BASE + timedelta(seconds=3 * INTERVAL)
    assert blocked_since(combine_endpoint_rows({"emec": _ep("bbo")})) is None


class _FakeResponse:
    def __init__(self, status_code, headers):
        self.status_code = status_code
        self.headers = headers


def test_checker_reconhece_desafio_do_cloudflare(monkeypatch):
    import requests

    from healthcheck import checker
    from healthcheck.config import Target
    from healthcheck.db import CLOUDFLARE_CHALLENGE

    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(403, {"cf-mitigated": "challenge", "server": "cloudflare"})
    )
    # Mesmo que o 403 fosse aceito no config, o desafio não vira "no ar".
    alvo = Target(key="x", name="X", url="https://x.gov.br/", also_accept=frozenset({403}))
    result = checker.check_target(alvo, timeout_seconds=5)
    assert result.success is False
    assert result.error == CLOUDFLARE_CHALLENGE


def test_checker_403_comum_continua_sendo_falha(monkeypatch):
    import requests

    from healthcheck import checker
    from healthcheck.config import Target

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(403, {"server": "nginx"}))
    result = checker.check_target(Target(key="x", name="X", url="https://x.gov.br/"), timeout_seconds=5)
    assert result.success is False
    assert result.error.startswith("status_code_fora_do_esperado")


def test_sla_descumprido_com_certeza_mesmo_com_pouca_cobertura():
    """98% num mês de 30 dias permite 14h24min fora. 15h de queda medida já
    garante o descumprimento, falte medir o que faltar."""
    from healthcheck.sla import sla_definitely_missed

    mes = 30 * 24 * 3600
    assert sla_definitely_missed(15 * 60, INTERVAL, mes, 98.0) is True
    assert sla_definitely_missed(14 * 60, INTERVAL, mes, 98.0) is False
    assert sla_definitely_missed(10_000, INTERVAL, mes, None) is False


def test_duracao_do_mes_no_fuso_do_contrato():
    from healthcheck.sla import month_length_seconds

    assert month_length_seconds(datetime(2026, 9, 15, 12, 0), "America/Sao_Paulo") == 30 * 24 * 3600
    assert month_length_seconds(datetime(2026, 2, 10, 12, 0), "America/Sao_Paulo") == 28 * 24 * 3600
