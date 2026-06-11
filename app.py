# Bolão Copa do Mundo 2026 — sistema interno da empresa
# Roda com: ./venv/bin/python app.py  (porta 8081, acessível na rede local)
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
from calendar import Calendar
from datetime import datetime, timedelta
from functools import wraps
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 (servidor) usa o backport
    from backports.zoneinfo import ZoneInfo

from flask import (Flask, Response, g, redirect, render_template, request,
                   session, url_for, jsonify, flash)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'data', 'bolao.db')
SEED_PATH = os.path.join(BASE, 'data', 'seed_copa2026.json')
KEY_PATH = os.path.join(BASE, 'data', 'secret_key')
TZ = ZoneInfo('America/Sao_Paulo')

VALOR_APOSTA = 30.00           # R$ por apostador
PREMIOS = [0.60, 0.30, 0.10]   # 1º, 2º e 3º lugar

ESTAGIOS = {
    'grupos': 'Fase de grupos',
    '32avos': '32 avos de final',
    'oitavas': 'Oitavas de final',
    'quartas': 'Quartas de final',
    'semi': 'Semifinais',
    'terceiro': 'Disputa de 3º lugar',
    'final': 'Final',
}

app = Flask(__name__)

# chave de sessão persistente em disco (sessões sobrevivem a restart)
if not os.path.exists(KEY_PATH):
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    with open(KEY_PATH, 'w') as f:
        f.write(secrets.token_hex(32))
app.secret_key = open(KEY_PATH).read().strip()

with open(SEED_PATH) as f:
    SEED = json.load(f)
TEAMS = SEED['teams']  # code -> {nome, flag}


# ---------------------------------------------------------------- banco

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            stage TEXT NOT NULL,
            grp TEXT,
            team1 TEXT NOT NULL,
            team2 TEXT NOT NULL,
            kickoff TEXT NOT NULL,
            score1 INTEGER,
            score2 INTEGER,
            voided INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS predictions (
            user_id INTEGER NOT NULL REFERENCES users(id),
            match_id INTEGER NOT NULL REFERENCES matches(id),
            score1 INTEGER NOT NULL,
            score2 INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, match_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT,
            action TEXT NOT NULL,
            match_id INTEGER,
            match_code TEXT,
            detalhe TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_match ON audit_log(match_code);
    ''')
    # migração: coluna paid em bancos criados antes do controle de pagamento
    cols = [r[1] for r in db.execute('PRAGMA table_info(users)').fetchall()]
    if 'paid' not in cols:
        db.execute('ALTER TABLE users ADD COLUMN paid INTEGER NOT NULL DEFAULT 0')
    # migração: coluna voided (jogo anulado) em bancos antigos
    cols = [r[1] for r in db.execute('PRAGMA table_info(matches)').fetchall()]
    if 'voided' not in cols:
        db.execute('ALTER TABLE matches ADD COLUMN voided INTEGER NOT NULL DEFAULT 0')
    # seed dos 72 jogos da fase de grupos (idempotente)
    for m in SEED['matches']:
        db.execute(
            'INSERT OR IGNORE INTO matches (code, stage, grp, team1, team2, kickoff) '
            'VALUES (?, "grupos", ?, ?, ?, ?)',
            (m['code'], m['group'], m['team1'], m['team2'], m['kickoff_brt']))
    # usuário admin inicial (senha deve ser trocada — ver README)
    cur = db.execute('SELECT 1 FROM users WHERE username = "admin"')
    if cur.fetchone() is None:
        db.execute(
            'INSERT INTO users (username, name, password_hash, is_admin, created_at) '
            'VALUES ("admin", "Administrador", ?, 1, ?)',
            (generate_password_hash('admin123'), now_iso()))
    db.commit()
    db.close()


# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(TZ).strftime('%Y-%m-%dT%H:%M')


def now_full():
    """Timestamp com segundos — usado em auditoria e palpites."""
    return datetime.now(TZ).strftime('%Y-%m-%dT%H:%M:%S')


def audit(db, action, match=None, detalhe=None, user_id=None, user_name=None):
    """Grava evento de auditoria. Quem fez vem da sessão; o commit é do chamador."""
    db.execute('''
        INSERT INTO audit_log (ts, user_id, user_name, action, match_id, match_code, detalhe)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (now_full(), user_id or session.get('user_id'),
          user_name or session.get('name'), action,
          match['id'] if match else None, match['code'] if match else None,
          json.dumps(detalhe, ensure_ascii=False) if detalhe else None))


def snapshot_palpites(db, match_id):
    """Foto dos palpites de um jogo num instante — vai pro log quando o jogo encerra."""
    rows = db.execute('''
        SELECT u.name, p.score1, p.score2, p.updated_at
        FROM predictions p JOIN users u ON u.id = p.user_id
        WHERE p.match_id = ? ORDER BY u.name COLLATE NOCASE
    ''', (match_id,)).fetchall()
    return [{'user': r['name'], 'palpite': f"{r['score1']}x{r['score2']}",
             'em': r['updated_at']} for r in rows]


LOCK_ANTECEDENCIA = timedelta(minutes=30)  # palpites encerram 30 min antes do jogo
LOCK_MIN = int(LOCK_ANTECEDENCIA.total_seconds() // 60)


def match_locked(match):
    """Travado se anulado, resultado confirmado ou dentro da antecedência do jogo."""
    if match['voided']:
        return True
    if match['score1'] is not None:
        return True
    kickoff = datetime.strptime(match['kickoff'], '%Y-%m-%dT%H:%M').replace(tzinfo=TZ)
    return datetime.now(TZ) >= kickoff - LOCK_ANTECEDENCIA


def calc_pontos(ps1, ps2, rs1, rs2):
    """Placar exato = 3 pontos; acertou o vencedor (ou empate) = 1; senão 0."""
    if (ps1, ps2) == (rs1, rs2):
        return 3
    if (ps1 - ps2 > 0) == (rs1 - rs2 > 0) and (ps1 == ps2) == (rs1 == rs2):
        return 1
    return 0


def team_nome(code):
    """Nome puro, sem HTML — para flash, auditoria e JSON."""
    t = TEAMS.get(code)
    return t['nome'] if t else code


def team_label(code):
    """Bandeira (imagem local) + nome, para os templates. Emoji de bandeira
    não renderiza no Windows, por isso PNGs em static/flags/."""
    t = TEAMS.get(code)
    if not t:
        return code
    return Markup(f'<img class="flag" src="/static/flags/{code}.png" alt="" '
                  f'width="20" height="14" loading="lazy" '
                  f'onerror="this.remove()">{t["nome"]}')


# versão do CSS = mtime do arquivo: navegador busca de novo a cada deploy
CSS_V = int(os.path.getmtime(os.path.join(BASE, 'static', 'style.css')))

app.jinja_env.globals.update(team_label=team_label, TEAMS=TEAMS,
                             ESTAGIOS=ESTAGIOS, VALOR_APOSTA=VALOR_APOSTA,
                             CSS_V=CSS_V, LOCK_MIN=LOCK_MIN)


@app.after_request
def cache_headers(resp):
    """HTML dinâmico nunca deve ficar preso em cache (navegador ou Cloudflare).
    Estáticos podem: o CSS tem ?v= e as bandeiras não mudam."""
    if resp.mimetype == 'text/html':
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.before_request
def checa_usuario_existe():
    """Sessão de excluído cai na hora; nome/admin renomeados refletem na hora."""
    if request.endpoint == 'static' or 'user_id' not in session:
        return
    u = get_db().execute('SELECT name, is_admin FROM users WHERE id = ?',
                         (session['user_id'],)).fetchone()
    if u is None:
        session.clear()
        return redirect(url_for('login'))
    session['name'] = u['name']
    session['is_admin'] = bool(u['is_admin'])


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


# ------------------------------------------------------------ placar ao vivo
# Fonte: API pública (não-oficial) da ESPN. Só informativo/parcial — o
# resultado oficial continua sendo o que o admin confirma no painel.
LIVE_URL = ('https://site.api.espn.com/apis/site/v2/sports/soccer/'
            'fifa.world/scoreboard')
_live_cache = {'ts': 0.0, 'dados': {}}


def fetch_live(db):
    """{match_id: {'s1','s2','status','state'}} dos jogos em andamento/encerrados
    na ESPN que ainda não têm resultado confirmado aqui. Cache de 60s."""
    agora = time.time()
    if agora - _live_cache['ts'] < 60:
        return _live_cache['dados']
    dados = {}
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=6) as r:
            j = json.load(r)
        pendentes = {frozenset((m['team1'], m['team2'])): m for m in db.execute(
            'SELECT id, team1, team2 FROM matches '
            'WHERE score1 IS NULL AND voided = 0').fetchall()}
        for e in j.get('events', []):
            comp = e['competitions'][0]
            comps = comp.get('competitors', [])
            if len(comps) != 2:
                continue
            placar = {c['team'].get('abbreviation'): int(c.get('score') or 0)
                      for c in comps}
            m = pendentes.get(frozenset(placar))
            estado = comp['status']['type'].get('state')  # pre | in | post
            if m is None or estado == 'pre':
                continue
            dados[m['id']] = {
                's1': placar.get(m['team1'], 0), 's2': placar.get(m['team2'], 0),
                'status': comp['status']['type'].get('shortDetail', ''),
                'state': estado,
            }
    except Exception:
        dados = _live_cache['dados']  # falhou: mantém o último conhecido
    _live_cache.update(ts=agora, dados=dados)
    return dados


@app.route('/api/livescore')
@login_required
def api_livescore():
    return jsonify(fetch_live(get_db()))


# ------------------------------------------------------------ CazéTV
CAZETV_BUSCA = 'https://www.youtube.com/@CazeTV/search?query='
CAZETV_CANAL = 'https://www.youtube.com/@CazeTV/streams'
_caze_cache = {}  # frozenset({time1, time2}) -> {'ts': float, 'video': dict|None}


def _norm_time(s):
    """Remove acentos e caixa pra casar nome de time com título de vídeo."""
    s = unicodedata.normalize('NFD', s)
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn').upper().strip()


def fetch_cazetv_jogo(nome1, nome2):
    """Busca no canal da CazéTV a transmissão oficial do confronto.
    O título segue o padrão 'AO VIVO: TIME1 X TIME2 | COPA...'. Cache 10 min."""
    n1, n2 = _norm_time(nome1), _norm_time(nome2)
    chave = frozenset((n1, n2))
    agora = time.time()
    hit = _caze_cache.get(chave)
    if hit and agora - hit['ts'] < 600:
        return hit['video']
    video = None
    try:
        url = CAZETV_BUSCA + urllib.parse.quote(f'{nome1} X {nome2}')
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept-Language': 'pt-BR,pt;q=0.9'})
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', 'ignore')
        for ch in html.split('"videoRenderer":')[1:]:
            ch = ch[:4000]
            vid = re.search(r'"videoId":"([^"]+)"', ch)
            tit = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"', ch)
            if not (vid and tit):
                continue
            cabeca = _norm_time(tit.group(1).split('|')[0])
            if cabeca in (f'AO VIVO: {n1} X {n2}', f'AO VIVO: {n2} X {n1}'):
                video = {'vid': vid.group(1), 'titulo': tit.group(1)}
                break
    except Exception:
        video = hit['video'] if hit else None  # falhou: mantém o último conhecido
    _caze_cache[chave] = {'ts': agora, 'video': video}
    return video


@app.route('/assistir')
@login_required
def assistir():
    db = get_db()
    hoje = now_iso()[:10]
    jogos = db.execute('SELECT * FROM matches WHERE kickoff LIKE ? AND voided = 0 '
                       'ORDER BY kickoff, code', (hoje + '%',)).fetchall()
    live = fetch_live(db)
    cards = []
    for m in jogos:
        video = fetch_cazetv_jogo(team_nome(m['team1']), team_nome(m['team2']))
        cards.append({'id': m['id'], 'team1': m['team1'], 'team2': m['team2'],
                      'kickoff': m['kickoff'], 'score1': m['score1'],
                      'score2': m['score2'], 'live': live.get(m['id']),
                      'video': video})
    return render_template('assistir.html', cards=cards, canal=CAZETV_CANAL)


def get_ranking(db, live=None):
    """Ranking geral: pontos desc, nº de placares exatos desc, nome asc.
    Com `live`, jogos em andamento contam pontos parciais (não persistidos)."""
    rows = db.execute('''
        SELECT u.id, u.name, u.paid, p.score1 ps1, p.score2 ps2, p.match_id,
               m.score1 rs1, m.score2 rs2, m.voided
        FROM users u
        LEFT JOIN predictions p ON p.user_id = u.id
        LEFT JOIN matches m ON m.id = p.match_id
        WHERE u.is_admin = 0 OR EXISTS
              (SELECT 1 FROM predictions pp WHERE pp.user_id = u.id)
    ''').fetchall()
    stats = {}
    for r in rows:
        s = stats.setdefault(r['id'], {'uid': r['id'], 'name': r['name'],
                                       'pago': bool(r['paid']), 'pontos': 0,
                                       'exatos': 0, 'vencedores': 0, 'erros': 0,
                                       'palpites': 0})
        if r['ps1'] is None:
            continue
        if r['voided']:
            continue  # jogo anulado: palpite não conta pra nada
        s['palpites'] += 1
        rs1, rs2 = r['rs1'], r['rs2']
        if rs1 is None and live and r['match_id'] in live:
            rs1, rs2 = live[r['match_id']]['s1'], live[r['match_id']]['s2']
        if rs1 is None:
            continue
        pts = calc_pontos(r['ps1'], r['ps2'], rs1, rs2)
        s['pontos'] += pts
        if pts == 3:
            s['exatos'] += 1
        elif pts == 1:
            s['vencedores'] += 1
        else:
            s['erros'] += 1
    ranking = sorted(stats.values(),
                     key=lambda s: (-s['pontos'], -s['exatos'], s['name'].lower()))
    apostadores = sum(1 for s in ranking if s['palpites'] > 0)
    caixa = apostadores * VALOR_APOSTA
    return ranking, apostadores, caixa


# ---------------------------------------------------------------- auth

@app.route('/registrar', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')
        if not username or not name or len(password) < 4:
            flash('Preencha tudo — a senha precisa de pelo menos 4 caracteres.')
        else:
            db = get_db()
            try:
                cur = db.execute(
                    'INSERT INTO users (username, name, password_hash, created_at) '
                    'VALUES (?, ?, ?, ?)',
                    (username, name, generate_password_hash(password), now_iso()))
                audit(db, 'registro', user_id=cur.lastrowid, user_name=name,
                      detalhe={'texto': f'usuário "{username}" criado'})
                db.commit()
                flash('Conta criada! Faça login para apostar.')
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                flash('Esse nome de usuário já existe.')
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        user = get_db().execute('SELECT * FROM users WHERE username = ?',
                                (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['name'] = user['name']
            session['is_admin'] = bool(user['is_admin'])
            return redirect(url_for('palpites'))
        flash('Usuário ou senha inválidos.')
    return render_template('login.html')


@app.route('/favicon.ico')
def favicon():
    """Navegadores e ferramentas que buscam direto na raiz."""
    return app.send_static_file('favicon.ico')


@app.route('/sair')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/conta', methods=['GET', 'POST'])
@login_required
def conta():
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id = ?',
                   (session['user_id'],)).fetchone()
    if request.method == 'POST':
        acao = request.form.get('acao')
        if acao == 'nome':
            novo = request.form.get('name', '').strip()
            if not novo or len(novo) > 60:
                flash('Nome inválido (1 a 60 caracteres).')
            elif novo != u['name']:
                db.execute('UPDATE users SET name = ? WHERE id = ?', (novo, u['id']))
                _renomeia_na_auditoria(db, u['id'], u['name'], novo)
                audit(db, 'usuario_renomeado', user_name=novo,
                      detalhe={'texto': f"{u['name']} → {novo} "
                                        f"(login: {u['username']}, pelo próprio usuário)"})
                db.commit()
                session['name'] = novo
                flash(f'Nome de exibição alterado para {novo}.')
        elif acao == 'senha':
            atual = request.form.get('senha_atual', '')
            nova = request.form.get('senha_nova', '')
            conf = request.form.get('senha_conf', '')
            if not check_password_hash(u['password_hash'], atual):
                flash('Senha atual incorreta.')
            elif len(nova) < 4:
                flash('A nova senha precisa de pelo menos 4 caracteres.')
            elif nova != conf:
                flash('A confirmação não confere com a nova senha.')
            else:
                db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                           (generate_password_hash(nova), u['id']))
                audit(db, 'senha_alterada',
                      detalhe={'texto': f"login: {u['username']}"})
                db.commit()
                flash('Senha alterada com sucesso.')
        return redirect(url_for('conta'))
    return render_template('conta.html', u=u)


# ---------------------------------------------------------------- palpites

@app.route('/')
@login_required
def palpites():
    db = get_db()
    matches = db.execute('SELECT * FROM matches ORDER BY kickoff, code').fetchall()
    preds = {r['match_id']: r for r in db.execute(
        'SELECT * FROM predictions WHERE user_id = ?',
        (session['user_id'],)).fetchall()}

    live = fetch_live(db)
    grupos = {}      # 'A' -> [match dicts]
    mata_mata = {}   # stage -> [match dicts]
    jogos_hoje = []  # banner de aviso no topo
    pendentes = 0    # jogos abertos sem palpite do usuário
    hoje = now_iso()[:10]
    for m in matches:
        p = preds.get(m['id'])
        locked = match_locked(m)
        pts = None
        if p and m['score1'] is not None and not m['voided']:
            pts = calc_pontos(p['score1'], p['score2'], m['score1'], m['score2'])
        lv = live.get(m['id'])
        pts_live = None
        if lv and p and m['score1'] is None and not m['voided']:
            pts_live = calc_pontos(p['score1'], p['score2'], lv['s1'], lv['s2'])
        item = {
            'id': m['id'], 'code': m['code'], 'team1': m['team1'], 'team2': m['team2'],
            'kickoff': m['kickoff'], 'locked': locked, 'voided': m['voided'],
            'score1': m['score1'], 'score2': m['score2'],
            'ps1': p['score1'] if p else None, 'ps2': p['score2'] if p else None,
            'pontos': pts, 'live': lv, 'pts_live': pts_live,
        }
        if m['stage'] == 'grupos':
            grupos.setdefault(m['grp'], []).append(item)
        else:
            mata_mata.setdefault(m['stage'], []).append(item)
        if m['kickoff'][:10] == hoje:
            item['tv'] = fetch_cazetv_jogo(team_nome(m['team1']),
                                           team_nome(m['team2']))
            kickoff = datetime.strptime(m['kickoff'], '%Y-%m-%dT%H:%M').replace(tzinfo=TZ)
            trava = (kickoff - LOCK_ANTECEDENCIA).strftime('%H:%M')
            jogos_hoje.append({**item, 'trava': trava})
        if not locked and p is None:
            pendentes += 1

    return render_template('palpites.html', grupos=sorted(grupos.items()),
                           mata_mata=mata_mata, jogos_hoje=jogos_hoje,
                           pendentes=pendentes)


@app.route('/api/palpite', methods=['POST'])
@login_required
def salvar_palpite():
    data = request.get_json(silent=True) or {}
    try:
        match_id = int(data['match_id'])
        s1, s2 = int(data['score1']), int(data['score2'])
        if not (0 <= s1 <= 99 and 0 <= s2 <= 99):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, erro='Placar inválido.'), 400

    db = get_db()
    m = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if m is None:
        return jsonify(ok=False, erro='Jogo não encontrado.'), 404
    if m['voided']:
        return jsonify(ok=False, erro='Jogo anulado — não aceita palpites.'), 403
    if m['score1'] is not None:
        return jsonify(ok=False, erro='Resultado já confirmado — palpite travado.'), 403
    if match_locked(m):
        return jsonify(ok=False,
                       erro=f'Palpites encerram {LOCK_MIN} min antes do jogo.'), 403

    old = db.execute(
        'SELECT score1, score2 FROM predictions WHERE user_id = ? AND match_id = ?',
        (session['user_id'], match_id)).fetchone()
    if old and (old['score1'], old['score2']) == (s1, s2):
        return jsonify(ok=True)  # nada mudou — não grava nem audita

    db.execute('''
        INSERT INTO predictions (user_id, match_id, score1, score2, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id, match_id)
        DO UPDATE SET score1 = excluded.score1, score2 = excluded.score2,
                      updated_at = excluded.updated_at
    ''', (session['user_id'], match_id, s1, s2, now_full()))
    audit(db, 'palpite', match=m,
          detalhe={'de': f"{old['score1']}x{old['score2']}" if old else None,
                   'para': f'{s1}x{s2}', 'kickoff': m['kickoff']})
    db.commit()
    return jsonify(ok=True)


@app.route('/api/palpites-jogo/<int:match_id>')
@login_required
def palpites_jogo(match_id):
    """Último palpite de cada usuário segundo a AUDITORIA — propositalmente não
    lê a tabela predictions, pra evidenciar qualquer mexida manual na base."""
    rows = get_db().execute('''
        SELECT user_id, user_name, ts, detalhe FROM audit_log
        WHERE action = 'palpite' AND match_id = ?
        ORDER BY id DESC
    ''', (match_id,)).fetchall()
    vistos, lista = set(), []
    for r in rows:  # mais recente primeiro; 1ª ocorrência por usuário = último palpite
        if r['user_id'] in vistos:
            continue
        vistos.add(r['user_id'])
        d = json.loads(r['detalhe']) if r['detalhe'] else {}
        lista.append({'user': r['user_name'], 'palpite': d.get('para'), 'em': r['ts']})
    lista.sort(key=lambda p: (p['user'] or '').lower())

    # distribuição dos palpites — só depois do travamento, pra não influenciar aposta
    m = get_db().execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    dist = None
    if m is not None and match_locked(m) and lista:
        v1 = empate = v2 = 0
        placares = {}
        for p in lista:
            if not p['palpite']:
                continue
            a, b = (int(x) for x in p['palpite'].split('x'))
            if a > b:
                v1 += 1
            elif a < b:
                v2 += 1
            else:
                empate += 1
            placares[p['palpite']] = placares.get(p['palpite'], 0) + 1
        top = sorted(placares.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        dist = {'v1': v1, 'empate': empate, 'v2': v2, 'total': v1 + empate + v2,
                'top': [{'placar': k, 'n': n} for k, n in top]}
    return jsonify(ok=True, palpites=lista, dist=dist)


def get_evolucao(db):
    """Posição de cada apostador a cada dia com resultado lançado (p/ gráfico)."""
    finished = db.execute(
        'SELECT id, kickoff, score1, score2 FROM matches '
        'WHERE score1 IS NOT NULL AND voided = 0 ORDER BY kickoff').fetchall()
    if not finished:
        return [], []
    dias = sorted({m['kickoff'][:10] for m in finished})
    by_match = {}
    for p in db.execute('''
            SELECT p.user_id uid, u.name, p.match_id, p.score1 ps1, p.score2 ps2
            FROM predictions p JOIN users u ON u.id = p.user_id''').fetchall():
        by_match.setdefault(p['match_id'], []).append(p)
    series = {}  # uid -> {'uid', 'name', 'pos': {dia: posição}}
    for d in dias:
        tot = {}
        for m in finished:
            if m['kickoff'][:10] > d:
                break
            for p in by_match.get(m['id'], []):
                s = tot.setdefault(p['uid'], {'uid': p['uid'], 'name': p['name'],
                                              'pontos': 0, 'exatos': 0})
                pts = calc_pontos(p['ps1'], p['ps2'], m['score1'], m['score2'])
                s['pontos'] += pts
                if pts == 3:
                    s['exatos'] += 1
        ordem = sorted(tot.values(),
                       key=lambda s: (-s['pontos'], -s['exatos'], s['name'].lower()))
        for pos, s in enumerate(ordem, 1):
            ser = series.setdefault(s['uid'], {'uid': s['uid'], 'name': s['name'],
                                               'pos': {}})
            ser['pos'][d] = pos
    return dias, list(series.values())


# tons médios: legíveis tanto no tema claro quanto no escuro
CORES_GRAFICO = ['#2e9e57', '#4d8fd1', '#d35f5f', '#d9a82e', '#a06cc9', '#e08338',
                 '#3aa8a0', '#d36493', '#9b7d62', '#7d93a3', '#a3a838', '#7a82d4']


def montar_grafico(dias, series, uid_atual):
    """Coordenadas SVG das linhas de posição — renderizado direto no template."""
    if not dias or not series:
        return None
    w, h, pad = 760, 320, 42
    n, maxpos = len(dias), len(series)
    xs = lambda i: pad + i * (w - 2 * pad) / max(n - 1, 1)
    ys = lambda pos: pad + (pos - 1) * (h - 2 * pad) / max(maxpos - 1, 1)
    linhas = []
    ordenadas = sorted(series, key=lambda s: s['pos'].get(dias[-1], 999))
    for i, s in enumerate(ordenadas):
        pontos = [f'{xs(di):.1f},{ys(s["pos"][d]):.1f}'
                  for di, d in enumerate(dias) if d in s['pos']]
        linhas.append({'name': s['name'], 'cor': CORES_GRAFICO[i % len(CORES_GRAFICO)],
                       'points': ' '.join(pontos), 'voce': s['uid'] == uid_atual,
                       'pos_final': s['pos'].get(dias[-1])})
    passo = max(1, (n + 9) // 10)  # no máx ~10 rótulos no eixo x
    rotulos_x = [{'x': xs(i), 'txt': f'{d[8:10]}/{d[5:7]}'}
                 for i, d in enumerate(dias) if i % passo == 0 or i == n - 1]
    rotulos_y = [{'y': ys(p), 'txt': f'{p}º'} for p in range(1, maxpos + 1)]
    return {'w': w, 'h': h, 'pad': pad, 'linhas': linhas,
            'rotulos_x': rotulos_x, 'rotulos_y': rotulos_y}


# ---------------------------------------------------------------- placar geral

@app.route('/classificacao')
@login_required
def classificacao():
    db = get_db()
    live = fetch_live(db)
    ao_vivo = sum(1 for v in live.values() if v['state'] == 'in')
    ranking, apostadores, caixa = get_ranking(db, live=live)
    premios = [round(caixa * p, 2) for p in PREMIOS]
    jogos_com_resultado = db.execute(
        'SELECT COUNT(*) c FROM matches WHERE score1 IS NOT NULL AND voided = 0'
    ).fetchone()['c']
    minha_pos = next((i + 1 for i, s in enumerate(ranking)
                      if s['uid'] == session['user_id'] and s['palpites'] > 0), None)
    # caixa oficial: só quem está marcado como pago
    pagos = sum(1 for s in ranking if s['pago'])
    nao_pagos = sum(1 for s in ranking if s['palpites'] > 0 and not s['pago'])
    caixa_pago = pagos * VALOR_APOSTA
    dias, series = get_evolucao(db)
    grafico = montar_grafico(dias, series, session['user_id'])
    return render_template('classificacao.html', ranking=ranking, caixa=caixa,
                           apostadores=apostadores, premios=premios,
                           jogos_com_resultado=jogos_com_resultado,
                           minha_pos=minha_pos, grafico=grafico,
                           caixa_pago=caixa_pago, pagos=pagos, nao_pagos=nao_pagos,
                           ao_vivo=ao_vivo)


MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
            'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
DIAS_SEMANA = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb']


@app.route('/calendario')
@login_required
def calendario():
    db = get_db()
    matches = db.execute('SELECT * FROM matches ORDER BY kickoff, code').fetchall()
    meus = {r['match_id'] for r in db.execute(
        'SELECT match_id FROM predictions WHERE user_id = ?',
        (session['user_id'],)).fetchall()}
    por_dia = {}
    for m in matches:
        item = dict(m)
        item['palpitei'] = m['id'] in meus
        item['locked'] = match_locked(m)
        por_dia.setdefault(m['kickoff'][:10], []).append(item)
    meses = []
    if por_dia:
        datas = sorted(por_dia)
        primeiro = datetime.strptime(datas[0], '%Y-%m-%d')
        ultimo = datetime.strptime(datas[-1], '%Y-%m-%d')
        cal = Calendar(firstweekday=6)  # semana começa no domingo
        y, mo = primeiro.year, primeiro.month
        while (y, mo) <= (ultimo.year, ultimo.month):
            semanas = []
            for week in cal.monthdatescalendar(y, mo):
                linha = []
                for d in week:
                    ds = d.strftime('%Y-%m-%d')
                    linha.append({'dia': d.day, 'fora': d.month != mo, 'data': ds,
                                  'jogos': por_dia.get(ds, []) if d.month == mo else []})
                semanas.append(linha)
            meses.append({'nome': f'{MESES_PT[mo]} {y}', 'semanas': semanas})
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return render_template('calendario.html', meses=meses, hoje=now_iso()[:10],
                           dias_semana=DIAS_SEMANA)


@app.route('/perfil/<int:user_id>')
@login_required
def perfil(user_id):
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if u is None:
        flash('Usuário não encontrado.')
        return redirect(url_for('classificacao'))
    ranking, _, _ = get_ranking(db)
    stats = next((s for s in ranking if s['uid'] == user_id), None)
    pos = next((i + 1 for i, s in enumerate(ranking)
                if s['uid'] == user_id and s['palpites'] > 0), None)
    rows = db.execute('''
        SELECT m.code, m.team1, m.team2, m.kickoff, m.voided,
               m.score1 rs1, m.score2 rs2, p.score1 ps1, p.score2 ps2
        FROM predictions p JOIN matches m ON m.id = p.match_id
        WHERE p.user_id = ? ORDER BY m.kickoff, m.code''', (user_id,)).fetchall()
    palpites = []
    for r in rows:
        pts = None
        if r['rs1'] is not None and not r['voided']:
            pts = calc_pontos(r['ps1'], r['ps2'], r['rs1'], r['rs2'])
        palpites.append({**dict(r), 'pts': pts})
    return render_template('perfil.html', u=u, stats=stats, pos=pos,
                           palpites=palpites)


@app.route('/regras')
@login_required
def regras():
    db = get_db()
    _, apostadores, caixa = get_ranking(db)
    premios = [round(caixa * p, 2) for p in PREMIOS]
    return render_template('regras.html', caixa=caixa, apostadores=apostadores,
                           premios=premios)


ACOES = {
    'palpite': '✏️ Palpite',
    'resultado': '🏁 Resultado lançado',
    'resultado_removido': '↩️ Resultado removido',
    'jogo_criado': '➕ Jogo criado',
    'jogo_excluido': '🗑️ Jogo excluído',
    'jogo_adiado': '📅 Jogo remarcado',
    'jogo_anulado': '🚫 Jogo anulado',
    'jogo_reativado': '✅ Jogo reativado',
    'pagamento': '💰 Pagamento',
    'registro': '👤 Conta criada',
    'usuario_excluido': '🗑️ Usuário excluído',
    'usuario_renomeado': '✏️ Nome alterado',
    'senha_alterada': '🔑 Senha alterada',
}


def _fmt_dt(s):
    """'2026-06-13T16:00[:ss]' -> '13/06 16:00'."""
    return f'{s[8:10]}/{s[5:7]} {s[11:16]}' if s else '?'


def montar_auditoria(rows):
    """Transforma linhas do audit_log em entradas legíveis (página e CSV)."""
    entries = []
    for r in rows:
        d = json.loads(r['detalhe']) if r['detalhe'] else {}
        a = r['action']
        texto, palpites = '', None
        if a == 'palpite':
            texto = (f"{d['de']} → " if d.get('de') else '') + f"{d.get('para', '')}"
            texto += f" · jogo {_fmt_dt(d.get('kickoff'))}"
        elif a == 'resultado':
            texto = f"placar {d.get('placar')} · jogo {_fmt_dt(d.get('kickoff'))}"
            palpites = d.get('palpites')
        elif a == 'resultado_removido':
            texto = f"placar {d.get('placar')} removido"
        elif a == 'jogo_criado':
            texto = (f"{d.get('confronto')} — {ESTAGIOS.get(d.get('stage'), '')}"
                     f" · jogo {_fmt_dt(d.get('kickoff'))}")
        elif a == 'jogo_excluido':
            texto = f"{d.get('confronto')} · jogo {_fmt_dt(d.get('kickoff'))}"
            palpites = d.get('palpites')
        elif a == 'jogo_adiado':
            texto = f"{_fmt_dt(d.get('de'))} → {_fmt_dt(d.get('para'))}"
        elif a == 'jogo_anulado':
            texto = f"jogo {_fmt_dt(d.get('kickoff'))}"
            palpites = d.get('palpites')
        elif a == 'jogo_reativado':
            texto = f"jogo {_fmt_dt(d.get('kickoff'))}"
        elif a == 'usuario_excluido':
            texto = d.get('texto', '')
            palpites = d.get('palpites')
        else:
            texto = d.get('texto', '')
        entries.append({'ts': r['ts'], 'user': r['user_name'], 'action': a,
                        'code': r['match_code'], 'texto': texto, 'palpites': palpites,
                        'col1': 'Jogo' if a == 'usuario_excluido' else 'Participante'})
    return entries


def _filtros_auditoria(args):
    """Lê os filtros da query string e monta WHERE parametrizado."""
    filtros = {'jogo': args.get('jogo', '').strip().upper(),
               'usuario': args.get('usuario', '').strip(),
               'evento': args.get('evento', '').strip(),
               'de': args.get('de', '').strip(),
               'ate': args.get('ate', '').strip()}
    cond, params = [], []
    if filtros['jogo']:
        cond.append('match_code = ?')
        params.append(filtros['jogo'])
    if filtros['usuario']:
        cond.append('user_name = ?')
        params.append(filtros['usuario'])
    if filtros['evento'] in ACOES:
        cond.append('action = ?')
        params.append(filtros['evento'])
    if filtros['de']:
        cond.append('ts >= ?')
        params.append(filtros['de'])
    if filtros['ate']:
        cond.append('ts <= ?')
        params.append(filtros['ate'] + 'T23:59:59')
    where = (' WHERE ' + ' AND '.join(cond)) if cond else ''
    return filtros, where, params


@app.route('/auditoria')
@login_required
def auditoria():
    db = get_db()
    filtros, where, params = _filtros_auditoria(request.args)
    entries = montar_auditoria(db.execute(
        f'SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT 300',
        params).fetchall())
    usuarios = [r['user_name'] for r in db.execute(
        'SELECT DISTINCT user_name FROM audit_log WHERE user_name IS NOT NULL '
        'ORDER BY user_name COLLATE NOCASE')]
    jogos = [r['match_code'] for r in db.execute(
        'SELECT DISTINCT match_code FROM audit_log WHERE match_code IS NOT NULL '
        'ORDER BY match_code')]
    return render_template('auditoria.html', entries=entries, ACOES=ACOES,
                           usuarios=usuarios, jogos=jogos, filtros=filtros,
                           tem_filtro=any(filtros.values()))


def _csv_response(linhas, filename):
    """CSV com ; e BOM — abre certinho no Excel pt-BR."""
    buf = io.StringIO()
    csv.writer(buf, delimiter=';').writerows(linhas)
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/export/ranking.csv')
@login_required
def export_ranking():
    ranking, _, _ = get_ranking(get_db())
    linhas = [['posicao', 'participante', 'pontos', 'placares_exatos',
               'so_resultado', 'erros', 'palpites', 'pagamento']]
    for i, s in enumerate(ranking, 1):
        linhas.append([i, s['name'], s['pontos'], s['exatos'], s['vencedores'],
                       s['erros'], s['palpites'],
                       'pago' if s['pago'] else 'pendente'])
    return _csv_response(linhas, 'ranking_bolao.csv')


@app.route('/export/auditoria.csv')
@login_required
def export_auditoria():
    _, where, params = _filtros_auditoria(request.args)  # exporta o que está filtrado
    rows = get_db().execute(f'SELECT * FROM audit_log{where} ORDER BY id',
                            params).fetchall()
    linhas = [['data_hora', 'quem', 'acao', 'jogo', 'detalhe', 'palpites_no_momento']]
    for e in montar_auditoria(rows):
        snap = ' | '.join(f"{p['user']} {p['palpite']}" for p in e['palpites']) \
            if e['palpites'] else ''
        linhas.append([e['ts'], e['user'] or '', e['action'], e['code'] or '',
                       e['texto'], snap])
    return _csv_response(linhas, 'auditoria_bolao.csv')


# ---------------------------------------------------------------- admin

@app.route('/admin')
@admin_required
def admin():
    db = get_db()
    matches = db.execute('SELECT * FROM matches ORDER BY kickoff, code').fetchall()
    users = db.execute('''
        SELECT u.id, u.username, u.name, u.is_admin, u.paid,
               COUNT(p.match_id) AS palpites
        FROM users u LEFT JOIN predictions p ON p.user_id = u.id
        GROUP BY u.id ORDER BY u.name
    ''').fetchall()
    hoje = now_iso()[:10]
    tem_hoje = any(m['kickoff'][:10] == hoje for m in matches)
    return render_template('admin.html', matches=matches, users=users,
                           hoje=hoje, tem_hoje=tem_hoje)


@app.route('/admin/resultado', methods=['POST'])
@admin_required
def admin_resultado():
    match_id = request.form.get('match_id', type=int)
    s1 = request.form.get('score1', '').strip()
    s2 = request.form.get('score2', '').strip()
    db = get_db()
    m = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if m is None:
        flash('Jogo não encontrado.')
        return redirect(url_for('admin'))
    if s1 == '' and s2 == '':
        db.execute('UPDATE matches SET score1 = NULL, score2 = NULL WHERE id = ?',
                   (match_id,))
        if m['score1'] is not None:
            audit(db, 'resultado_removido', match=m,
                  detalhe={'placar': f"{m['score1']}x{m['score2']}",
                           'kickoff': m['kickoff']})
        flash('Resultado removido.')
    else:
        try:
            v1, v2 = int(s1), int(s2)
            if not (0 <= v1 <= 99 and 0 <= v2 <= 99):
                raise ValueError
        except ValueError:
            flash('Placar inválido.')
            return redirect(url_for('admin'))
        db.execute('UPDATE matches SET score1 = ?, score2 = ? WHERE id = ?',
                   (v1, v2, match_id))
        audit(db, 'resultado', match=m,
              detalhe={'placar': f'{v1}x{v2}', 'kickoff': m['kickoff'],
                       'palpites': snapshot_palpites(db, match_id)})
        flash('Resultado salvo — placar geral atualizado.')
    db.commit()
    return redirect(url_for('admin'))


@app.route('/admin/pagamento', methods=['POST'])
@admin_required
def admin_pagamento():
    """Marca/desmarca pagamento — só informativo, não altera o caixa."""
    user_id = request.form.get('user_id', type=int)
    db = get_db()
    u = db.execute('SELECT name, paid FROM users WHERE id = ?', (user_id,)).fetchone()
    if u is None:
        flash('Usuário não encontrado.')
        return redirect(url_for('admin'))
    novo = 0 if u['paid'] else 1
    db.execute('UPDATE users SET paid = ? WHERE id = ?', (novo, user_id))
    audit(db, 'pagamento',
          detalhe={'texto': f"{u['name']} → {'pago' if novo else 'pendente'}"})
    db.commit()
    flash(f"{u['name']} marcado como {'✅ pago' if novo else '⏳ pendente'}.")
    return redirect(url_for('admin'))


@app.route('/admin/usuario/renomear', methods=['POST'])
@admin_required
def admin_renomear_usuario():
    user_id = request.form.get('user_id', type=int)
    novo = request.form.get('name', '').strip()
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if u is None:
        flash('Usuário não encontrado.')
        return redirect(url_for('admin'))
    if not novo or len(novo) > 60:
        flash('Nome inválido (1 a 60 caracteres).')
        return redirect(url_for('admin'))
    if novo != u['name']:
        db.execute('UPDATE users SET name = ? WHERE id = ?', (novo, user_id))
        _renomeia_na_auditoria(db, user_id, u['name'], novo)
        audit(db, 'usuario_renomeado',
              detalhe={'texto': f"{u['name']} → {novo} (login: {u['username']})"})
        db.commit()
        flash(f"Nome de exibição alterado: {u['name']} → {novo}.")
    return redirect(url_for('admin'))


def _renomeia_na_auditoria(db, user_id, antigo, novo):
    """Propaga o nome novo pro log: coluna 'quem' dos eventos do usuário e os
    nomes dentro dos snapshots de palpites. O evento 'usuario_renomeado'
    preserva o antes → depois, então a rastreabilidade não se perde."""
    db.execute('UPDATE audit_log SET user_name = ? WHERE user_id = ?',
               (novo, user_id))
    rows = db.execute('''
        SELECT id, detalhe FROM audit_log
        WHERE action IN ('resultado', 'jogo_anulado', 'jogo_excluido')
          AND detalhe LIKE ?''', (f'%{antigo}%',)).fetchall()
    for r in rows:
        d = json.loads(r['detalhe'])
        mudou = False
        for p in d.get('palpites', []):
            if p.get('user') == antigo:
                p['user'] = novo
                mudou = True
        if mudou:
            db.execute('UPDATE audit_log SET detalhe = ? WHERE id = ?',
                       (json.dumps(d, ensure_ascii=False), r['id']))


@app.route('/admin/usuario/excluir', methods=['POST'])
@admin_required
def admin_excluir_usuario():
    user_id = request.form.get('user_id', type=int)
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if u is None:
        flash('Usuário não encontrado.')
        return redirect(url_for('admin'))
    if u['is_admin']:
        flash('Contas de admin não podem ser excluídas.')
        return redirect(url_for('admin'))
    # foto dos palpites antes de apagar — fica preservada no log
    pal = db.execute('''
        SELECT m.code, p.score1, p.score2, p.updated_at
        FROM predictions p JOIN matches m ON m.id = p.match_id
        WHERE p.user_id = ? ORDER BY m.kickoff, m.code''', (user_id,)).fetchall()
    snap = [{'user': r['code'], 'palpite': f"{r['score1']}x{r['score2']}",
             'em': r['updated_at']} for r in pal]
    db.execute('DELETE FROM predictions WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    audit(db, 'usuario_excluido',
          detalhe={'texto': f"{u['name']} (login: {u['username']}) — "
                            f"{len(snap)} palpite(s) apagados",
                   'palpites': snap})
    db.commit()
    flash(f"Usuário {u['name']} excluído — palpites apagados "
          '(histórico preservado na auditoria).')
    return redirect(url_for('admin'))


@app.route('/admin/kickoff', methods=['POST'])
@admin_required
def admin_kickoff():
    """Adiamento/remarcação: altera data e hora de qualquer jogo."""
    match_id = request.form.get('match_id', type=int)
    kickoff = request.form.get('kickoff', '').strip()
    db = get_db()
    m = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if m is None:
        flash('Jogo não encontrado.')
        return redirect(url_for('admin'))
    try:
        datetime.strptime(kickoff, '%Y-%m-%dT%H:%M')
    except ValueError:
        flash('Data/hora inválida.')
        return redirect(url_for('admin'))
    if kickoff != m['kickoff']:
        db.execute('UPDATE matches SET kickoff = ? WHERE id = ?', (kickoff, match_id))
        audit(db, 'jogo_adiado', match=m, detalhe={'de': m['kickoff'], 'para': kickoff})
        db.commit()
        flash(f"Jogo {m['code']} remarcado para {kickoff[8:10]}/{kickoff[5:7]} {kickoff[11:16]}.")
    return redirect(url_for('admin'))


@app.route('/admin/anular', methods=['POST'])
@admin_required
def admin_anular():
    """Anula (ou reativa) um jogo: anulado não pontua nem aceita palpites."""
    match_id = request.form.get('match_id', type=int)
    db = get_db()
    m = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if m is None:
        flash('Jogo não encontrado.')
        return redirect(url_for('admin'))
    if m['voided']:
        db.execute('UPDATE matches SET voided = 0 WHERE id = ?', (match_id,))
        audit(db, 'jogo_reativado', match=m, detalhe={'kickoff': m['kickoff']})
        flash(f"Jogo {m['code']} reativado — volta a valer pontos e palpites.")
    else:
        db.execute('UPDATE matches SET voided = 1 WHERE id = ?', (match_id,))
        audit(db, 'jogo_anulado', match=m,
              detalhe={'kickoff': m['kickoff'],
                       'palpites': snapshot_palpites(db, match_id)})
        flash(f"Jogo {m['code']} anulado — não vale pontos nem aceita palpites.")
    db.commit()
    return redirect(url_for('admin'))


@app.route('/admin/jogo', methods=['POST'])
@admin_required
def admin_novo_jogo():
    stage = request.form.get('stage')
    team1 = request.form.get('team1')
    team2 = request.form.get('team2')
    kickoff = request.form.get('kickoff', '').strip()
    if stage not in ESTAGIOS or stage == 'grupos' or team1 not in TEAMS \
            or team2 not in TEAMS or team1 == team2 or len(kickoff) != 16:
        flash('Dados do jogo inválidos.')
        return redirect(url_for('admin'))
    db = get_db()
    n = db.execute('SELECT COUNT(*) c FROM matches WHERE stage = ?',
                   (stage,)).fetchone()['c']
    code = f'{stage.upper()}-{n + 1}'
    cur = db.execute('INSERT INTO matches (code, stage, grp, team1, team2, kickoff) '
                     'VALUES (?, ?, NULL, ?, ?, ?)', (code, stage, team1, team2, kickoff))
    audit(db, 'jogo_criado', match={'id': cur.lastrowid, 'code': code},
          detalhe={'confronto': f'{team_nome(team1)} x {team_nome(team2)}',
                   'stage': stage, 'kickoff': kickoff})
    db.commit()
    flash(f'Jogo {team_nome(team1)} x {team_nome(team2)} criado ({ESTAGIOS[stage]}).')
    return redirect(url_for('admin'))


@app.route('/admin/jogo/excluir', methods=['POST'])
@admin_required
def admin_excluir_jogo():
    match_id = request.form.get('match_id', type=int)
    db = get_db()
    m = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if m is None or m['stage'] == 'grupos':
        flash('Jogos da fase de grupos não podem ser excluídos.')
        return redirect(url_for('admin'))
    snap = snapshot_palpites(db, match_id)  # antes de apagar — fica preservado no log
    db.execute('DELETE FROM predictions WHERE match_id = ?', (match_id,))
    db.execute('DELETE FROM matches WHERE id = ?', (match_id,))
    audit(db, 'jogo_excluido', match=m,
          detalhe={'confronto': f"{team_nome(m['team1'])} x {team_nome(m['team2'])}",
                   'kickoff': m['kickoff'], 'palpites': snap})
    db.commit()
    flash('Jogo excluído (palpites associados também).')
    return redirect(url_for('admin'))


init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=False)
