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


@dataclass
class Target:
    key: str
    name: str
    url: str
    # None = aceita qualquer código 200-399. Caso contrário, apenas os
    # códigos explicitamente listados contam como "no ar".
    expected_status: frozenset[int] | None = None


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
    targets: list[Target] = field(default_factory=list)


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


def _parse_expected_status(raw, target_key: str) -> frozenset[int] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            f"'expected_status' do sistema '{target_key}' deve ser uma lista de códigos HTTP, "
            f"recebido: {raw!r}"
        )
    try:
        codes = frozenset(int(code) for code in raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"'expected_status' do sistema '{target_key}' contém valor não numérico: {raw!r}"
        ) from exc
    if not codes:
        raise ConfigError(f"'expected_status' do sistema '{target_key}' está vazio.")
    return codes


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
    for t in targets_raw:
        try:
            key = t["key"]
            name = t["name"]
            url = t["url"]
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"Sistema inválido em 'targets' (faltando campo {exc}): {t}") from exc

        if key in seen_keys:
            raise ConfigError(f"Chave de sistema duplicada em 'targets': {key}")
        seen_keys.add(key)

        targets.append(
            Target(
                key=key,
                name=name,
                url=_validate_url(url, key),
                expected_status=_parse_expected_status(t.get("expected_status"), key),
            )
        )

    return Config(database=database, check=check, sla=sla, targets=targets)


def resolve_db_url(config: Config) -> str:
    """Permite sobrepor a connection string via variável de ambiente,
    útil para apontar para o Oracle em produção sem editar o config.yaml."""
    return os.environ.get("HEALTHCHECK_DB_URL", config.database.url)
