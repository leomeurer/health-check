# Health Check — Uptime/Downtime dos sistemas MEC

Daemon que checa uma lista de sistemas web via HTTP a cada 1 minuto,
grava o resultado em banco (SQLite nos testes, Oracle em produção) e
gera um painel HTML estático com uptime, incidentes e conformidade de
SLA por sistema.

## Decisões registradas

- **Execução**: daemon Python de longa duração (loop de 60s com
  correção de drift), rodando via `systemd` em servidor/VM próprio —
  não usa GitHub Actions porque o cron do Actions não garante
  granularidade de 1 minuto.
- **Critério de downtime**: **1 falha já conta como indisponível**
  (sem tolerância de N falhas consecutivas). Isso é o que define o
  início/fim de um incidente para fins de apuração de SLA.
- **Armazenamento**: SQLite em arquivo local para os testes iniciais.
  O acesso ao banco é feito via SQLAlchemy, então migrar para o Oracle
  definitivo é só trocar a connection string (`database.url` no
  `config.yaml` ou a env var `HEALTHCHECK_DB_URL`) — o schema usa
  apenas tipos padrão (String, DateTime, Boolean, Integer, Float).
- **Painel**: HTML estático gerado pelo próprio script (sem servidor
  web adicional, sem dependência de CDN externo — importante numa rede
  corporativa/governamental que pode ser restrita).
- **SLA / glosa**: a meta percentual (`sla.target_pct`) e a fórmula de
  glosa ainda **não foram informadas**. O painel já calcula e mostra o
  uptime % e a conformidade com a meta assim que `target_pct` for
  preenchido em `config.yaml`, mas o **valor financeiro da glosa não é
  calculado** — isso depende da cláusula exata do contrato e será
  implementado quando ela for repassada.

## Estrutura

```
config.example.yaml        # copie para config.yaml e ajuste
src/healthcheck/
  config.py                 # leitura do config.yaml
  db.py                      # modelos SQLAlchemy (SQLite/Oracle)
  checker.py                 # checagem HTTP de um sistema
  daemon.py                  # loop de 60s que popula o banco
  sla.py                      # uptime %, incidentes, status atual
  report.py                   # gera data/report.html
  templates/report.html.j2
systemd/
  healthcheck.service          # daemon de checagem
  healthcheck-report.service   # gera o relatório (oneshot)
  healthcheck-report.timer     # dispara o relatório a cada 5 min
data/                        # banco sqlite + report.html (gerados, fora do git)
```

## Uso local (testes)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# edite config.yaml e preencha a lista real de sistemas em `targets`

# roda uma única rodada de checagem (não fica em loop) — bom para validar
PYTHONPATH=src python -m healthcheck.daemon --once

# gera o painel a partir do que já foi coletado
PYTHONPATH=src python -m healthcheck.report
# abra data/report.html no navegador

# loop contínuo (Ctrl+C para parar)
PYTHONPATH=src python -m healthcheck.daemon
```

## Deploy em produção (servidor com systemd)

```bash
sudo useradd --system --home /opt/health-check --shell /usr/sbin/nologin healthcheck
sudo mkdir -p /opt/health-check
sudo cp -r . /opt/health-check
cd /opt/health-check
sudo python3 -m venv .venv
sudo ./.venv/bin/pip install -r requirements.txt
# se o banco final for Oracle:
sudo ./.venv/bin/pip install oracledb

sudo cp config.example.yaml config.yaml
sudo $EDITOR config.yaml   # preencher targets, sla.target_pct, etc.
sudo chown -R healthcheck:healthcheck /opt/health-check

sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthcheck.service
sudo systemctl enable --now healthcheck-report.timer

# acompanhar
journalctl -u healthcheck.service -f
```

O `report.html` fica em `/opt/health-check/data/report.html` e é
regerado a cada 5 minutos pelo timer. Sirva esse arquivo com qualquer
servidor web estático (nginx, Apache, etc.) apontando para essa pasta,
ou copie-o periodicamente para onde for publicado.

## Próximos passos combinados

1. Você envia a lista real de sistemas (URLs) e o que cada um espera
   como resposta "saudável" (hoje: HTTP 200-399).
2. Você informa a meta de SLA do contrato (`sla.target_pct`) e a
   janela de apuração real, se diferente de mês calendário.
3. Você me passa a cláusula de glosa (fórmula/percentuais) para eu
   implementar o cálculo financeiro no relatório.
4. Quando o Oracle de produção estiver disponível, só preciso da
   connection string para trocar `database.url`.
