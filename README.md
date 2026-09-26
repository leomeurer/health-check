# Health Check — Uptime e SLA de sistemas web

Monitor de disponibilidade que checa uma lista de sistemas web via HTTP a
cada minuto, grava cada resultado em banco de dados e gera um painel HTML
estático com status atual, uptime, incidentes e conformidade com uma meta de
SLA por sistema.

O `config.yaml` deste repositório vem configurado com sistemas do Ministério
da Educação (MEC), mas qualquer lista de URLs pode ser monitorada.

## Funcionalidades

- **Checagem a cada minuto** por um daemon Python de longa duração (com
  correção de drift), rodando via `systemd`.
- **Sistemas com mais de um endpoint** (ex: página inicial + API): o sistema
  só conta como no ar numa rodada se **todos** os endpoints responderam OK.
  Isso evita que uma página inicial servida de cache/CDN esconda uma queda do
  backend.
- **Detecção do desafio anti-bot do Cloudflare**: a rodada é registrada como
  *sem medição* — não conta como no ar nem como fora (ver
  [Sistemas atrás do Cloudflare](#sistemas-atrás-do-cloudflare)).
- **Painel HTML estático**, sem servidor de aplicação e sem CDN externo
  (funciona em redes restritas):
  - um bloco por sistema com status, uptime em 24h / 7 dias / 30 dias / mês
    corrente, cobertura de dados, conformidade com a meta e incidentes
    (indicando qual endpoint falhou);
  - gráfico mensal (heatmap) com o SLA apurado de cada sistema por mês, com
    um botão para simular o ano preenchido (claramente marcado como
    simulação).
- **Banco via SQLAlchemy**: SQLite por padrão; Oracle (ou outro banco
  suportado) trocando apenas a connection string.

## Como os números são calculados

- **Uptime por amostragem.** Cada checagem representa o intervalo até a
  seguinte: `uptime % = rodadas OK / rodadas medidas` na janela. **Uma falha
  isolada já conta como indisponibilidade** (não há tolerância de N falhas
  consecutivas).
- **Cobertura.** Um período sem checagem (daemon parado, servidor
  reiniciado) não entra na conta e faria o sistema parecer 100% disponível.
  Por isso cada janela mostra quantas rodadas foram medidas contra quantas
  eram esperadas. **Com cobertura abaixo de 95%, o SLA do mês aparece como
  "não apurável"** — exceto quando o downtime já medido passa, sozinho, da
  margem que a meta permite no mês inteiro (aí o descumprimento é certo).
- **Rodadas sem medição** (desafio do Cloudflare) ficam fora do uptime e
  reduzem a cobertura. Se outro endpoint do mesmo sistema falhou de verdade
  na mesma rodada, a falha prevalece.
- **Duração de incidente** = nº de rodadas com falha × intervalo entre
  checagens, coerente com o uptime %. O horário de "fim" é quando a
  recuperação foi *detectada*; a amostragem não permite precisar o instante
  exato do retorno.
- **Cadência medida, não configurada.** O intervalo mostrado no painel é a
  mediana dos intervalos reais entre checagens.
- **Mês no fuso do contrato** (`sla.timezone`, padrão `America/Sao_Paulo`),
  e não em UTC — senão as últimas horas de cada mês cairiam na apuração do
  mês seguinte.
- **Valor financeiro (glosa)**: não é calculado; depende da cláusula de cada
  contrato. O painel mostra o uptime e a conformidade com a meta
  (`sla.target_pct`).

## Configuração (`config.yaml`)

```yaml
database:
  url: "sqlite:///data/healthcheck.db"   # ou HEALTHCHECK_DB_URL no ambiente

check:
  interval_seconds: 60    # intervalo entre rodadas
  timeout_seconds: 10     # timeout HTTP por endpoint (menor que o intervalo)

sla:
  target_pct: 98.0        # meta mensal de disponibilidade, em %
  timezone: "America/Sao_Paulo"

targets:
  - key: exemplo          # identificador estável: não mude depois de coletar dados
    name: "Sistema Exemplo"
    short_name: "Exemplo" # opcional: nome no gráfico mensal
    url: "https://exemplo.gov.br/"
    # expected_status: [200]   # opcional: só estes códigos contam como no ar
    #                          # (padrão: qualquer código de 200 a 399)
    # also_accept: [403]       # opcional: códigos extras aceitos
    # label: "Página inicial"  # opcional: rótulo do endpoint principal
    extra_checks:              # opcional: outros endpoints do mesmo sistema
      - key: exemplo_api
        label: "API (backend)"
        url: "https://api.exemplo.gov.br/health"
        expected_status: [200, 401]
```

Credenciais nunca vão no arquivo: a connection string de produção deve vir da
variável de ambiente `HEALTHCHECK_DB_URL` (ex:
`oracle+oracledb://usuario:senha@host:1521/?service_name=ORCLPDB1`, que exige
`pip install oracledb`).

## Uso local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# uma única rodada de checagem (bom para validar a configuração)
PYTHONPATH=src python -m healthcheck.daemon --once

# gera o painel a partir do que já foi coletado → abra data/report.html
PYTHONPATH=src python -m healthcheck.report

# loop contínuo (Ctrl+C para parar)
PYTHONPATH=src python -m healthcheck.daemon
```

## Testes

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

Cobrem a aritmética da apuração: uptime, cobertura, incidentes, consolidação
de vários endpoints, rodadas sem medição, SLA mensal, fronteira do mês no
fuso correto, cadência observada e validação da configuração.

## Estrutura

```
config.yaml                  # sistemas monitorados e meta de SLA
src/healthcheck/
  config.py                  # leitura e validação do config.yaml
  db.py                      # modelo SQLAlchemy (SQLite/Oracle)
  checker.py                 # checagem HTTP de um endpoint
  daemon.py                  # loop que popula o banco
  sla.py                     # uptime, cobertura, incidentes, SLA mensal
  report.py                  # gera o painel (data/report.html)
  reclassify_cloudflare.py   # manutenção: reclassifica histórico com o desafio do Cloudflare
  templates/report.html.j2
  static/echarts.min.js      # Apache ECharts 5.6.0, copiado ao lado do painel
systemd/
  healthcheck.service        # daemon de checagem
  healthcheck-report.service # gera o painel (oneshot)
  healthcheck-report.timer   # dispara a geração a cada 5 min
nginx/healthcheck.conf       # publica somente o painel e a biblioteca
data/                        # banco SQLite e painel gerado (fora do git)
```

## Implantação (Linux com systemd + nginx)

```bash
sudo apt update && sudo apt install -y python3-venv git nginx
git clone https://github.com/leomeurer/health-check.git
sudo useradd --system --home /opt/health-check --shell /usr/sbin/nologin healthcheck
sudo mv health-check /opt/health-check
cd /opt/health-check
sudo python3 -m venv .venv
sudo ./.venv/bin/pip install -r requirements.txt
sudo chown -R healthcheck:healthcheck /opt/health-check

# checagem contínua + geração do painel a cada 5 minutos
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthcheck.service
sudo systemctl enable --now healthcheck-report.timer
journalctl -u healthcheck.service -f   # acompanhar as rodadas

# publicação do painel
sudo cp nginx/healthcheck.conf /etc/nginx/sites-available/healthcheck.conf
sudo ln -s /etc/nginx/sites-available/healthcheck.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

O painel fica disponível em `http://<servidor>/`. A pasta `/opt/health-check`
pertence ao usuário do serviço; para atualizar o código, use
`sudo -u healthcheck git pull` e reinicie `healthcheck.service`.

### Em uma VM Always Free da Oracle Cloud

Uma instância *Always Free eligible* (ex: `VM.Standard.E2.1.Micro`, Ubuntu)
é suficiente. Além dos passos acima, é preciso liberar a porta 80 em **dois**
lugares:

- na *Security List* da VCN (`Networking` → `Virtual Cloud Networks` → VCN →
  `Security Lists` → `Add Ingress Rules`: origem `0.0.0.0/0`, TCP, porta
  `80`);
- no firewall da própria VM, que vem com `iptables` restritivo:
  ```bash
  sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
  sudo netfilter-persistent save
  ```

### Segurança

- O `nginx/healthcheck.conf` usa uma **lista branca**: serve apenas
  `report.html` e `echarts.min.js`. O banco SQLite (incluindo os arquivos
  `-wal`/`-shm`) nunca é exposto.
- A configuração publica o painel em HTTP, sem autenticação. Para dados que
  não devam ser públicos, adicione HTTPS (exige um domínio) e/ou autenticação
  no nginx.

## Sistemas atrás do Cloudflare

Sites protegidos pelo *managed challenge* do Cloudflare respondem `403` com o
cabeçalho `cf-mitigated: challenge` (página "Just a moment...") a clientes
que não são navegadores. Essa resposta sai da borda do Cloudflare **sem a
requisição chegar ao sistema** — não prova que ele está no ar nem fora.

O monitor reconhece esse cabeçalho e grava a rodada como `bloqueio_cloudflare`:
o sistema recebe o selo "BLOQUEIO CLOUDFLARE", as rodadas aparecem em cinza e
ficam fora do uptime. Um `403` que não seja o desafio continua sendo falha.

Para medir de verdade, o administrador do site precisa liberar o monitor no
Cloudflare — por exemplo, uma regra **Skip** no WAF para o IP do servidor de
monitoramento ou para um cabeçalho secreto. A partir daí as respostas reais
passam a contar automaticamente; se o servidor de origem cair, o Cloudflare
responde 502/503/504, o que é detectado como falha.

Se o histórico tiver checagens antigas em que o `403` do desafio foi aceito
como "no ar", elas podem ser reclassificadas:

```bash
PYTHONPATH=src python -m healthcheck.reclassify_cloudflare --keys <chaves>          # mostra o que mudaria
PYTHONPATH=src python -m healthcheck.reclassify_cloudflare --keys <chaves> --apply  # grava (idempotente)
```

## Componentes de terceiros

- [Apache ECharts](https://echarts.apache.org/) 5.6.0 — Apache License 2.0,
  distribuído em `src/healthcheck/static/echarts.min.js`.
