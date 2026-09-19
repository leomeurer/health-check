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

## Como os números são apurados

Isto importa porque o painel é usado para embasar glosa:

- **Uptime é medido por amostragem.** Cada checagem representa o intervalo
  até a seguinte, e `uptime % = checagens OK / total de checagens` na
  janela. Uma falha isolada já conta como indisponibilidade.
- **Cobertura de dados é exibida junto com o uptime.** Um período sem
  checagem nenhuma (daemon parado, VM reiniciada, execução do agendador
  pulada) não entra na conta e faria o sistema parecer 100% disponível.
  Por isso cada janela informa quantas checagens existem contra quantas
  eram esperadas. **Uptime com cobertura baixa não sustenta glosa** — o
  painel destaca isso explicitamente.
- **Duração de incidente** = nº de checagens com falha × intervalo entre
  checagens, mantendo coerência com o uptime %. O horário de "fim" é
  quando a recuperação foi *detectada*; a amostragem não permite precisar
  o instante exato do retorno.
- **A cadência mostrada no painel é a medida nos dados** (mediana dos
  intervalos reais), não a configurada — o agendador pode não ter rodado
  na frequência esperada.
- **A janela do mês usa o fuso do contrato** (`sla.timezone`, padrão
  `America/Sao_Paulo`), e não UTC. Ancorar em UTC jogaria as últimas 3
  horas de cada mês brasileiro para a apuração do mês seguinte.

## Testes

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

Cobrem a aritmética que sustenta a apuração: uptime, cobertura, duração e
agrupamento de incidentes, fronteira do mês no fuso correto, cadência
observada e validação de URL.

## Atenção: bloqueio por IP/WAF em alguns sistemas

Ao testar a lista real de sistemas (`config.yaml`) a partir deste
ambiente de desenvolvimento (que sai à internet por um IP de datacenter),
4 dos 9 sistemas do MEC responderam **HTTP 403** de forma consistente
(e-MEC, Sistec, SIMEC, GPEI), mesmo variando User-Agent/Accept/Referer —
o handshake TLS completa normalmente e quem responde 403 é o próprio
servidor, não o proxy. Isso é característico de bloqueio por IP/ASN de
nuvem na WAF (comum em sistemas de governo), não de um simples bot-check
de headers.

Verifiquei ainda que o 403 aparece em **todos** os caminhos desses
sistemas (inclusive `/robots.txt` e `/favicon.ico`), então não há um
endpoint de health que escape do WAF.

**Tratamento adotado (`also_accept: [403]`).** Como o 403 prova que o
serviço está de pé e respondendo, os 4 alvos bloqueados foram configurados
para contar o 403 como "no ar". O painel exibe uma nota em cada um desses
sistemas deixando o critério explícito, para a apuração ser auditável.
É uma medida de "o serviço responde", não de "a página final carrega".

**Antes de confiar no painel para apuração de glosa**, rode
`python -m healthcheck.daemon --once` a partir do servidor real onde o
daemon vai ficar hospedado. Se esse servidor **não** estiver em nuvem
pública, esses sistemas passarão a responder 200 normalmente — e aí o
`also_accept: [403]` deve ser **removido**, senão um 403 real (erro de
fato) ficaria mascarado como disponível. O caminho ideal continua sendo
pedir à equipe de cada sistema a liberação do IP do monitor na WAF e então
tirar o 403 da configuração.

Também identifiquei que a URL informada para o Inep (`https://inep.gov.br`)
não resolve (domínio inexistente); usei `https://www.gov.br/inep/pt-br`
como substituto no exemplo — confirme se é a página/sistema correto.

## Estrutura

```
config.yaml                # sistemas monitorados e meta de SLA (versionado)
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

# a lista de sistemas e a meta de SLA já estão em config.yaml

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

sudo $EDITOR config.yaml   # revisar targets e sla.target_pct
sudo chown -R healthcheck:healthcheck /opt/health-check

sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthcheck.service
sudo systemctl enable --now healthcheck-report.timer

# acompanhar
journalctl -u healthcheck.service -f
```

O `report.html` fica em `/opt/health-check/data/report.html` e é
regerado a cada 5 minutos pelo timer. A seção seguinte mostra como
publicá-lo numa VM gratuita da Oracle Cloud.

## Deploy na Oracle Cloud Always Free (checagem de 1 min + painel público)

Isto resolve, ao mesmo tempo, a checagem de 1 em 1 minuto de verdade (o
GitHub Actions não garante isso) e um link fixo para ver o painel do
celular. A VM Always Free da Oracle nunca expira e não tem custo
recorrente.

**1. Criar a VM** (no console da Oracle Cloud — isso só você consegue
fazer, é a sua conta):
- `Compute` → `Instances` → `Create instance`
- Shape: escolha um dos marcados **"Always Free eligible"** (ex:
  `VM.Standard.E2.1.Micro`, x86, 1 OCPU/1GB — suficiente para este daemon)
- Imagem: Ubuntu (a mais recente LTS listada)
- Em "Networking", mantenha a criação de uma VCN nova com IP público
- Adicione sua chave SSH pública (ou deixe a Oracle gerar um par e baixe
  a chave privada)
- Create. Anote o **IP público** que aparece na página da instância.

**2. Liberar a porta 80** (painel HTTP) e manter a 22 (SSH) — precisa
mexer em DOIS lugares, é a pegadinha mais comum da Oracle Cloud:
- No console: `Networking` → `Virtual Cloud Networks` → sua VCN →
  `Security Lists` → a lista padrão → `Add Ingress Rules` →
  Source CIDR `0.0.0.0/0`, IP Protocol `TCP`, Destination Port `80`
- Dentro da própria VM (o Ubuntu da Oracle vem com `iptables` bloqueando
  por padrão, além da Security List do console):
  ```bash
  sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
  sudo netfilter-persistent save   # ou: sudo apt install iptables-persistent
  ```

**3. Instalar o projeto na VM** (via SSH):
```bash
ssh ubuntu@<IP-publico-da-vm>

sudo apt update && sudo apt install -y python3-venv python3-pip nginx git
git clone https://github.com/leomeurer/health-check.git
sudo useradd --system --home /opt/health-check --shell /usr/sbin/nologin healthcheck
sudo mv health-check /opt/health-check
cd /opt/health-check
sudo python3 -m venv .venv
sudo ./.venv/bin/pip install -r requirements.txt
sudo chown -R healthcheck:healthcheck /opt/health-check
```

**4. Ativar a checagem de 1 em 1 minuto e a geração do painel:**
```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthcheck.service
sudo systemctl enable --now healthcheck-report.timer

# confirma que está coletando
journalctl -u healthcheck.service -f
```

**5. Publicar com nginx** (HTTP público, sem senha — conforme decidido):
```bash
sudo cp nginx/healthcheck.conf /etc/nginx/sites-available/healthcheck.conf
sudo ln -s /etc/nginx/sites-available/healthcheck.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # remove a página padrão do nginx
sudo nginx -t && sudo systemctl reload nginx
```

Pronto: `http://<IP-publico-da-vm>/` mostra o painel, atualizado a cada
5 minutos, com checagem de verdade de 1 em 1 minuto por trás. Adicione
esse IP aos favoritos do navegador do celular.

**Nota de segurança:** por ser HTTP puro (sem HTTPS) e sem autenticação,
qualquer pessoa com o IP vê os dados de disponibilidade dos sistemas do
MEC. Foi a opção escolhida por simplicidade — se depois quiser adicionar
HTTPS (precisa de um domínio, a Oracle não dá HTTPS de graça só com IP)
ou autenticação básica no nginx, é uma mudança pequena a qualquer momento.

## Próximos passos combinados

1. Você envia a lista real de sistemas (URLs) e o que cada um espera
   como resposta "saudável" (hoje: HTTP 200-399).
2. Você informa a meta de SLA do contrato (`sla.target_pct`) e a
   janela de apuração real, se diferente de mês calendário.
3. Você me passa a cláusula de glosa (fórmula/percentuais) para eu
   implementar o cálculo financeiro no relatório.
4. Quando o Oracle de produção estiver disponível, só preciso da
   connection string para trocar `database.url`.
