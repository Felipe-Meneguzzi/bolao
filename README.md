# 🏆 Bolão Copa do Mundo 2026

Sistema web do bolão da empresa. Cada pessoa cria seu usuário, preenche os
palpites e acompanha o placar geral, a premiação e a auditoria de tudo que
acontece. Flask + SQLite, sem dependência externa em produção.

## Instalação do zero

Requisitos: **Python 3.9+** (funciona no 3.8 — o `requirements.txt` puxa o
backport do `zoneinfo` automaticamente) e o pacote de venv do sistema
(`sudo apt install python3-venv` no Ubuntu/Debian).

```bash
# 1. obter o código
git clone <url-do-repositorio> bolao
cd bolao

# 2. criar o ambiente e instalar dependências
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. rodar
./venv/bin/python app.py
```

O servidor sobe em `http://0.0.0.0:8081`. Na **primeira execução** são criados
automaticamente:

- `data/bolao.db` — banco SQLite com os 72 jogos da fase de grupos
  (a partir de `data/seed_copa2026.json`);
- `data/secret_key` — chave das sessões de login;
- usuário **`admin`** com senha **`admin123`** — ⚠️ **troque imediatamente**:
  faça login e use o menu **Conta → Alterar senha**.

### Rodar como serviço (recomendado em servidor)

O unit file está em `deploy/bolao.service` (instruções de instalação no
próprio arquivo). Com ele o app sobe no boot e reinicia sozinho se cair:

```bash
sudo cp deploy/bolao.service /etc/systemd/system/bolao.service
sudo systemctl daemon-reload
sudo systemctl enable --now bolao
journalctl -u bolao -f   # logs
```

Para rodar avulso em segundo plano (sem systemd):
`nohup ./venv/bin/python app.py >> bolao.log 2>&1 &`

### Acesso pela rede (se rodar em WSL2)

É preciso encaminhar a porta no Windows. Num **PowerShell como administrador**:

```powershell
netsh interface portproxy add v4tov4 listenport=8081 listenaddress=0.0.0.0 connectport=8081 connectaddress=(wsl hostname -I).Trim()
New-NetFirewallRule -DisplayName "Bolao Copa 2026" -Direction Inbound -LocalPort 8081 -Protocol TCP -Action Allow
```

Aí todo mundo acessa por `http://IP_DO_WINDOWS:8081`. Se o IP do WSL mudar
após reiniciar, rode o `netsh ... add` de novo.

### Deploy num servidor

Primeira vez: `git clone` + venv + systemd (acima). Atualizações:

```bash
cd ~/bolao
git pull
sudo systemctl restart bolao
```

O `data/` (banco, chave de sessão) fica fora do git — os palpites reais nunca
são tocados pelo deploy. Migrações de banco rodam sozinhas na subida e o CSS
tem cache-busting automático (`?v=mtime`).

## Dados e backup

Tudo fica em **`data/bolao.db`** (usuários, palpites, resultados e o log de
auditoria). Backup = copiar esse arquivo. O `data/secret_key` invalida as
sessões de login se for perdido (sem outro efeito).

## Funcionalidades

- **Palpites** com salvamento automático, travamento **30 min antes do jogo**
  (ou ao confirmar resultado), countdown ao vivo e aviso de pendências;
- **Calendário** mês a mês com cor por status do seu palpite;
- **Placar geral** com desempate por placares exatos, caixa teórico e
  **caixa confirmado** (só quem pagou), gráfico de evolução e perfil clicável
  de cada participante;
- **Auditoria completa e pública**: todo palpite, resultado, alteração e
  pagamento fica registrado com data/hora; snapshot dos palpites no momento
  em que cada jogo é encerrado; filtros por usuário/jogo/evento/data e
  exportação CSV;
- **Admin**: lançar resultados (com snapshot), criar jogos do mata-mata,
  remarcar data/hora, anular/reativar jogo, marcar pagamento, renomear e
  excluir usuários;
- **Conta**: cada usuário troca o próprio nome de exibição e senha;
- **Dark mode** com detecção do tema do sistema;
- Bandeiras em PNG local (emoji de bandeira não renderiza no Windows).

## Regras do bolão

(Disponíveis no menu **Regras** dentro do sistema, sempre atualizadas.)

- **Aposta**: R$ 30,00 por participante que fizer ao menos um palpite —
  pagamento **em dinheiro** com o organizador; o admin marca quem pagou.
- **Pontuação**: placar exato = **3 pts** · acertou só o resultado = **1 pt** ·
  errou = 0.
- **Travamento**: palpite editável até **30 minutos antes** do jogo (Brasília).
- **Mata-mata**: vale o placar do tempo normal.
- **Premiação**: 🥇 60% · 🥈 30% · 🥉 10% do caixa.
- **Desempate**: mais placares exatos; persistindo, prêmios somados e divididos.
- Jogo **anulado** não pontua; jogo **remarcado** vale na nova data.

## Estrutura

| Caminho | O que é |
|---|---|
| `app.py` | Aplicação Flask completa (rotas, banco, pontuação, auditoria) |
| `templates/` | Telas (palpites, calendário, placar, perfil, auditoria, admin, conta…) |
| `static/style.css` | Visual — temas claro/escuro via variáveis CSS |
| `static/flags/` | Bandeiras PNG dos 48 times |
| `data/seed_copa2026.json` | Jogos reais da fase de grupos (sorteio 05/12/2025) |
| `data/bolao.db` | Banco SQLite — **é aqui que tudo fica salvo** (fora do git) |
| `requirements.txt` | Dependências (Flask; backport de zoneinfo p/ Python 3.8) |
