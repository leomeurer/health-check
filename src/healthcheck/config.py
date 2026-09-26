"""Carregamento e validação do config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_TIMEZONE = "America/Sao_Paulo"
MIN_INTERVAL_SECONDS = 5
DEFAULT_MAIN_LABEL = "Página inicial"


@dataclass
class Target:
    key: str
    name: str
    url: str
    # None = aceita qualquer código 200-399. Caso contrário, apenas os
    # códigos explicitamente listados contam como "no ar".
    expected_status: frozenset[int] | None = None
    # Códigos extras aceitos ALÉM da regra acima. Uso previsto: sistemas
    # atrás de um WAF que responde 403 ao IP do monitor — aí o 403 prova
    # que o serviço está de pé e respondendo, ainda que não sirva a página.
    # Fica registrado por alvo (e exibido no painel) para a apuração ser
    # auditável.
    also_accept: frozenset[int] = frozenset()
    # Sistema ao qual este endpoint pertence (ver System) e o rótulo dele
    # dentro do sistema, ex: "Página inicial", "API (backend)".
    system_key: str = ""
    label: str = DEFAULT_MAIN_LABEL


@dataclass
class System:
    """Um sistema do painel, medido por um ou mais endpoints. Ele só conta
    como "no ar" numa rodada quando TODOS os endpoints responderam OK: a
    página inicial pode vir de cache enquanto a API por trás está fora, e é
    exatamente essa queda que o usuário sente."""

    key: str
    name: str
    endpoints: list[Target]
    # Nome curto para o gráfico mensal do painel.
    short_name: str = ""


@dataclass
class DatabaseSettings:
    url: str = "sqlite:///data/healthcheck.db"


@dataclass
class CheckSettings:
    interval_seconds: int = 60
    timeout_seconds: int = 10


@dataclass
class SlaSettings:
    target_pct: float | None = None
    window: str = "monthly"
    timezone: str = DEFAULT_TIMEZONE
    glosa_formula: str | None = None


@dataclass
class Config:
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    check: CheckSettings = field(default_factory=CheckSettings)
    sla: SlaSettings = field(default_factory=SlaSettings)
    # Lista plana de todos os endpoints (o que o daemon checa) e o
    # agrupamento deles por sistema (o que o painel apura).
    targets: list[Target] = field(default_factory=list)
    systems: list[System] = field(default_factory=list)


class ConfigError(Exception):
    pass


def _get(section: dict, key: str, default):
    """Como dict.get, mas tratando `chave: null` no YAML como "não informado"."""
    value = section.get(key)
    return default if value is None else value


def _positive_int(section: dict, key: str, default: int, minimum: int) -> int:
    value = _get(section, key, default)
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' deve ser um número inteiro, recebido: {value!r}") from exc
    if value < minimum:
        raise ConfigError(f"'{key}' deve ser >= {minimum}, recebido: {value}")
    return value


def _parse_status_list(raw, field: str, target_key: str) -> frozenset[int]:
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            f"'{field}' do sistema '{target_key}' deve ser uma lista de códigos HTTP, "
            f"recebido: {raw!r}"
        )
    try:
        codes = frozenset(int(code) for code in raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"'{field}' do sistema '{target_key}' contém valor não numérico: {raw!r}"
        ) from exc
    if not codes:
        raise ConfigError(f"'{field}' do sistema '{target_key}' está vazio.")
    return codes


def _parse_expected_status(raw, target_key: str) -> frozenset[int] | None:
    if raw is None:
        return None
    return _parse_status_list(raw, "expected_status", target_key)


def _parse_also_accept(raw, target_key: str) -> frozenset[int]:
    if raw is None:
        return frozenset()
    return _parse_status_list(raw, "also_accept", target_key)


def _validate_url(url: str, target_key: str) -> str:
    if not isinstance(url, str):
        raise ConfigError(f"'url' do sistema '{target_key}' deve ser texto, recebido: {url!r}")
    parsed = urlparse(url)
    # Sem isso, uma URL com erro de digitação viraria uma falha de checagem
    # registrada no banco — ou seja, downtime fictício na apuração de glosa.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"'url' do sistema '{target_key}' é inválida: {url!r}. "
            "Use uma URL absoluta, ex: https://exemplo.gov.br/"
        )
    return url


def _parse_endpoint(
    raw, seen_keys: set[str], system_key: str | None, system_name: str | None
) -> Target:
    """Lê um endpoint. Sem `system_key`, é o endpoint principal de um sistema
    (define a chave e o nome do sistema); com ela, é um `extra_checks`."""
    is_main = system_key is None
    required = ("key", "name", "url") if is_main else ("key", "label", "url")
    missing = [name for name in required if not isinstance(raw, dict) or name not in raw]
    if missing:
        where = "em 'targets'" if is_main else f"em 'extra_checks' do sistema '{system_key}'"
        raise ConfigError(f"Endpoint inválido {where} (faltando campo {missing}): {raw}")
    key = raw["key"]

    # A chave identifica o histórico no banco: repetida, dois endpoints
    # misturariam as checagens.
    if key in seen_keys:
        raise ConfigError(f"Chave duplicada em 'targets'/'extra_checks': {key}")
    seen_keys.add(key)

    label = raw.get("label") or DEFAULT_MAIN_LABEL
    return Target(
        key=key,
        name=raw["name"] if is_main else f"{system_name} — {label}",
        url=_validate_url(raw["url"], key),
        expected_status=_parse_expected_status(raw.get("expected_status"), key),
        also_accept=_parse_also_accept(raw.get("also_accept"), key),
        system_key=key if is_main else system_key,
        label=label,
    )


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"Arquivo de configuração não encontrado: {config_path}\n"
            "Esperado na raiz do projeto, versionado junto com o código."
        )

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    db_raw = raw.get("database") or {}
    db_url = _get(db_raw, "url", DatabaseSettings.url)
    if not isinstance(db_url, str) or not db_url.strip():
        raise ConfigError(f"'database.url' inválida: {db_url!r}")
    database = DatabaseSettings(url=db_url)

    check_raw = raw.get("check") or {}
    interval = _positive_int(check_raw, "interval_seconds", 60, MIN_INTERVAL_SECONDS)
    timeout = _positive_int(check_raw, "timeout_seconds", 10, 1)
    if timeout >= interval:
        raise ConfigError(
            f"'timeout_seconds' ({timeout}) deve ser menor que 'interval_seconds' ({interval}), "
            "senão uma rodada lenta atrasa a seguinte."
        )
    check = CheckSettings(interval_seconds=interval, timeout_seconds=timeout)

    sla_raw = raw.get("sla") or {}
    target_pct = sla_raw.get("target_pct")
    if target_pct is not None:
        try:
            target_pct = float(target_pct)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"'sla.target_pct' deve ser numérico, recebido: {target_pct!r}") from exc
        if not 0 < target_pct <= 100:
            raise ConfigError(f"'sla.target_pct' deve estar entre 0 e 100, recebido: {target_pct}")
    sla = SlaSettings(
        target_pct=target_pct,
        window=_get(sla_raw, "window", "monthly"),
        timezone=_get(sla_raw, "timezone", DEFAULT_TIMEZONE),
        glosa_formula=sla_raw.get("glosa_formula"),
    )

    targets_raw = raw.get("targets") or []
    if not targets_raw:
        raise ConfigError("Nenhum sistema definido em 'targets' no config.yaml.")

    seen_keys: set[str] = set()
    targets: list[Target] = []
    systems: list[System] = []
    for t in targets_raw:
        main = _parse_endpoint(t, seen_keys, system_key=None, system_name=None)
        endpoints = [main]
        extras_raw = t.get("extra_checks") or []
        if not isinstance(extras_raw, list):
            raise ConfigError(f"'extra_checks' do sistema '{main.key}' deve ser uma lista.")
        for extra in extras_raw:
            endpoints.append(
                _parse_endpoint(extra, seen_keys, system_key=main.key, system_name=main.name)
            )
        targets.extend(endpoints)
        short_name = t.get("short_name") or main.name
        if not isinstance(short_name, str):
            raise ConfigError(f"'short_name' do sistema '{main.key}' deve ser texto.")
        systems.append(
            System(key=main.key, name=main.name, endpoints=endpoints, short_name=short_name)
        )

    return Config(database=database, check=check, sla=sla, targets=targets, systems=systems)


def resolve_db_url(config: Config) -> str:
    """Permite sobrepor a connection string via variável de ambiente,
    útil para apontar para o Oracle em produção sem editar o config.yaml."""
    return os.environ.get("HEALTHCHECK_DB_URL", config.database.url)
