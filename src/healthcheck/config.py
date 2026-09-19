"""Carregamento e validação do config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class Target:
    key: str
    name: str
    url: str
    expected_status: tuple[int, int] = (200, 399)  # faixa inclusiva


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
    glosa_formula: str | None = None


@dataclass
class Config:
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    check: CheckSettings = field(default_factory=CheckSettings)
    sla: SlaSettings = field(default_factory=SlaSettings)
    targets: list[Target] = field(default_factory=list)


class ConfigError(Exception):
    pass


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        example = config_path.parent / "config.example.yaml"
        raise ConfigError(
            f"Arquivo de configuração não encontrado: {config_path}\n"
            f"Copie {example.name} para {config_path.name} e ajuste os valores."
        )

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    db_raw = raw.get("database") or {}
    database = DatabaseSettings(url=db_raw.get("url", DatabaseSettings.url))

    check_raw = raw.get("check") or {}
    check = CheckSettings(
        interval_seconds=int(check_raw.get("interval_seconds", 60)),
        timeout_seconds=int(check_raw.get("timeout_seconds", 10)),
    )

    sla_raw = raw.get("sla") or {}
    sla = SlaSettings(
        target_pct=sla_raw.get("target_pct"),
        window=sla_raw.get("window", "monthly"),
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
        except KeyError as exc:
            raise ConfigError(f"Sistema inválido em 'targets' (faltando campo {exc}): {t}") from exc

        if key in seen_keys:
            raise ConfigError(f"Chave de sistema duplicada em 'targets': {key}")
        seen_keys.add(key)

        expected = t.get("expected_status")
        expected_status = tuple(expected) if expected else (200, 399)

        targets.append(Target(key=key, name=name, url=url, expected_status=expected_status))

    return Config(database=database, check=check, sla=sla, targets=targets)


def resolve_db_url(config: Config) -> str:
    """Permite sobrepor a connection string via variável de ambiente,
    útil para apontar para o Oracle em produção sem editar o config.yaml."""
    return os.environ.get("HEALTHCHECK_DB_URL", config.database.url)
