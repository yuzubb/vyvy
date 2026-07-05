import base64
import re
from urllib.parse import urljoin, urlparse, quote, unquote, parse_qs

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response, render_template_string, redirect

app = Flask(__name__)

TIMEOUT = 25
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

# ---- URL encode/decode (URL-safe base64, path-embeddable) ----

def enc(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode('utf-8')).decode('ascii').rstrip('=')

def dec(token: str) -> str:
    pad = '=' * (-len(token) % 4)
    return base64.urlsafe_b64decode((token + pad).encode('ascii')).decode('utf-8')

def proxied(url: str) -> str:
    return '/p/' + enc(url)

def absolutize(url: str, base: str) -> str:
    if not url:
        return ''
    url = url.strip()
    low = url.lower()
    # skip non-navigational schemes
    if low.startswith(('data:', 'blob:', 'javascript:', 'mailto:', 'tel:', 'about:', '#')):
        return url
    if url.startswith('//'):
        scheme = urlparse(base).scheme or 'https'
        return f'{scheme}:{url}'
    if low.startswith(('http://', 'https://')):
        return url
    try:
        return urljoin(base, url)
    except Exception:
        return url

def rewrite_attr(url: str, base: str) -> str:
    """Turn a resource/link URL into a proxied one."""
    if not url:
        return url
    low = url.strip().lower()
    if low.startswith(('data:', 'blob:', 'javascript:', 'mailto:', 'tel:', 'about:', '#')):
        return url
    return proxied(absolutize(url, base))

# ---- CSS url() rewriting ----

_CSS_URL = re.compile(r'url\(\s*([\'"]?)([^\'")]+)\1\s*\)', re.IGNORECASE)
_CSS_IMPORT = re.compile(r'@import\s+([\'"])([^\'"]+)\1', re.IGNORECASE)

def rewrite_css(css: str, base: str) -> str:
    def _u(m):
        return f"url('{rewrite_attr(m.group(2), base)}')"
    def _i(m):
        return f"@import '{rewrite_attr(m.group(2), base)}'"
    css = _CSS_URL.sub(_u, css)
    css = _CSS_IMPORT.sub(_i, css)
    return css

# ---- srcset rewriting ----

def rewrite_srcset(val: str, base: str) -> str:
    out = []
    for part in val.split(','):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        u = bits[0]
        desc = ' ' + ' '.join(bits[1:]) if len(bits) > 1 else ''
        out.append(rewrite_attr(u, base) + desc)
    return ', '.join(out)

# ---- HTML rewriting ----

URL_ATTRS = {
    'src': ['img', 'script', 'iframe', 'source', 'audio', 'video', 'embed', 'track', 'input'],
    'href': ['a', 'link', 'area', 'base'],
    'poster': ['video'],
    'data': ['object'],
    'action': ['form'],
    'formaction': ['button', 'input'],
}

def rewrite_html(html: str, base: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')

    # remove <base> so our absolutization stays in control
    for b in soup.find_all('base'):
        b.decompose()

    # generic attributes
    for attr, tags in URL_ATTRS.items():
        for tag in soup.find_all(tags):
            if tag.has_attr(attr):
                tag[attr] = rewrite_attr(tag.get(attr, ''), base)

    # srcset
    for tag in soup.find_all(['img', 'source']):
        if tag.has_attr('srcset'):
            tag['srcset'] = rewrite_srcset(tag['srcset'], base)
        if tag.has_attr('imagesrcset'):
            tag['imagesrcset'] = rewrite_srcset(tag['imagesrcset'], base)

    # inline style attrs
    for tag in soup.find_all(style=True):
        tag['style'] = rewrite_css(tag['style'], base)

    # <style> blocks
    for tag in soup.find_all('style'):
        if tag.string:
            tag.string.replace_with(rewrite_css(tag.string, base))

    # meta refresh + meta URL-valued content (og:url, og:image, twitter:image, etc.)
    _META_URL_KEYS = ('og:url', 'og:image', 'og:image:url', 'og:image:secure_url',
                      'og:video', 'og:audio', 'twitter:image', 'twitter:url', 'image')
    for tag in soup.find_all('meta'):
        if (tag.get('http-equiv', '').lower() == 'refresh') and tag.has_attr('content'):
            c = tag['content']
            m = re.search(r'url=(.+)', c, re.IGNORECASE)
            if m:
                tag['content'] = c[:m.start(1)] + rewrite_attr(m.group(1).strip(), base)
        key = (tag.get('property') or tag.get('name') or '').lower()
        if key in _META_URL_KEYS and tag.has_attr('content'):
            val = tag['content'].strip()
            if val.lower().startswith(('http://', 'https://', '//')):
                tag['content'] = rewrite_attr(val, base)

    # catch-all: elements carrying a URL in common data/value attrs
    for tag in soup.find_all(True):
        for a in ('data-src', 'data-href', 'data-url', 'data-lazy-src',
                  'data-background-image', 'value', 'cite', 'longdesc'):
            if tag.has_attr(a):
                v = (tag.get(a) or '').strip()
                if v.lower().startswith(('http://', 'https://', '//')):
                    tag[a] = rewrite_attr(v, base)

    # inject a small script to catch client-side navigation (fetch/XHR/location)
    inject = soup.new_tag('script')
    inject.string = _CLIENT_SHIM.replace('__BASE__', base)
    if soup.head:
        # ensure a mobile-friendly viewport so proxied layouts don't collapse
        if not soup.head.find('meta', attrs={'name': 'viewport'}):
            vp = soup.new_tag('meta')
            vp['name'] = 'viewport'
            vp['content'] = 'width=device-width, initial-scale=1'
            soup.head.insert(0, vp)
        soup.head.insert(0, inject)
    elif soup.body:
        soup.body.insert(0, inject)
    else:
        soup.append(inject)

    return str(soup)

# Client-side shim: rewrite fetch/XHR/dynamic URLs at runtime
_CLIENT_SHIM = r"""
(function(){
  var BASE = "__BASE__";
  // 1) Force "online" so SPAs (YouTube etc.) don't show an offline screen
  try { Object.defineProperty(navigator, 'onLine', { get: function(){ return true; }, configurable: true }); } catch(e){}
  window.addEventListener('offline', function(e){ e.stopImmediatePropagation(); }, true);

  function abs(u){ try { return new URL(u, BASE).href; } catch(e){ return u; } }
  function isProxied(u){ return typeof u === 'string' && u.indexOf('/p/') === 0; }
  function b64(s){ return btoa(unescape(encodeURIComponent(s))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''); }
  function pfx(u){
    if(!u || typeof u !== 'string') return u;
    var low = u.toLowerCase();
    if(low.startsWith('data:')||low.startsWith('blob:')||low.startsWith('javascript:')||low.startsWith('#')||isProxied(u)) return u;
    try { return '/p/' + b64(abs(u)); } catch(e){ return u; }
  }
  // 2) fetch (handles string + Request)
  var _f = window.fetch;
  if(_f){ window.fetch = function(i, init){
    try{ if(typeof i === 'string') i = pfx(i); else if(i && i.url) i = new Request(pfx(i.url), i); }catch(e){}
    return _f.call(this, i, init);
  }; }
  // 3) XHR
  var _o = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u){ try{ arguments[1] = pfx(u); }catch(e){} return _o.apply(this, arguments); };
  // 4) sendBeacon
  if(navigator.sendBeacon){ var _b = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function(u, d){ try{ u = pfx(u); }catch(e){} return _b(u, d); }; }
  // 5) dynamic <img>/<script> src assignment
  ['src','href'].forEach(function(prop){
    ['HTMLImageElement','HTMLScriptElement','HTMLLinkElement','HTMLMediaElement'].forEach(function(cn){
      try{
        var C = window[cn]; if(!C) return;
        var d = Object.getOwnPropertyDescriptor(C.prototype, prop); if(!d || !d.set) return;
        Object.defineProperty(C.prototype, prop, {
          get: d.get,
          set: function(v){ try{ v = pfx(v); }catch(e){} return d.set.call(this, v); },
          configurable: true
        });
      }catch(e){}
    });
  });
  // 6) SPA navigation: keep history + location inside the proxy
  function toProxyPath(u){
    try{
      var p = pfx(u);
      return p; // already like /p/<token>
    }catch(e){ return u; }
  }
  var _ps = history.pushState, _rs = history.replaceState;
  history.pushState = function(s, t, u){
    if(u){ try{ u = toProxyPath(u); }catch(e){} }
    return _ps.call(this, s, t, u);
  };
  history.replaceState = function(s, t, u){
    if(u){ try{ u = toProxyPath(u); }catch(e){} }
    return _rs.call(this, s, t, u);
  };
  // intercept clicks on links the SPA may fully navigate
  document.addEventListener('click', function(ev){
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if(!a) return;
    var href = a.getAttribute('href') || '';
    if(href.indexOf('/p/') === 0 || href.startsWith('#') || href.startsWith('javascript:')) return;
    // bare or absolute link that escaped rewriting -> fix it
    if(href.startsWith('/') || href.startsWith('http')){
      a.setAttribute('href', pfx(href));
    }
  }, true);
})();
"""

# ---- Content-type helpers ----

def is_html(ct: str) -> bool:
    return 'text/html' in ct or 'application/xhtml' in ct

def is_css(ct: str, target: str = '') -> bool:
    if 'text/css' in ct:
        return True
    path = urlparse(target).path.lower()
    return path.endswith('.css') and ('text/' in ct or 'octet-stream' in ct or ct == '')

# ---- Core proxy ----

def _extract_video_id(target: str):
    u = urlparse(target)
    host = u.netloc.lower()
    if host.endswith('youtu.be'):
        vid = u.path.strip('/').split('/')[0]
        return vid or None
    if 'youtube.com' in host:
        if u.path == '/watch':
            qs = parse_qs(u.query)
            v = qs.get('v', [''])[0]
            return v or None
        if u.path.startswith('/shorts/'):
            return u.path.split('/shorts/')[1].split('/')[0] or None
        if u.path.startswith('/embed/'):
            return u.path.split('/embed/')[1].split('/')[0] or None
    return None

_EMBED_PAGE = """<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Player</title>
<style>
  html,body{margin:0;height:100%;background:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  .bar{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#0f0f0f;color:#fff}
  .bar a{color:#8ab4ff;text-decoration:none;font-size:.9rem}
  .bar .t{color:#aaa;font-size:.8rem;margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .wrap{position:relative;width:100%;padding-top:56.25%;background:#000}
  .wrap iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
  .note{color:#888;font-size:.8rem;padding:12px 14px;line-height:1.5}
</style></head><body>
  <div class="bar">
    <a href="__BACK__">&larr; 戻る</a>
    <span class="t">YouTube Player</span>
  </div>
  <div class="wrap">
    <iframe src="https://www.youtube-nocookie.com/embed/__VID__?autoplay=1&playsinline=1"
            allow="accelerator; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
            referrerpolicy="strict-origin-when-cross-origin"
            allowfullscreen></iframe>
  </div>
  <div class="note">公式の埋め込みプレーヤーで直接再生しています。埋め込みが無効な動画・年齢制限付きの動画は再生できません。</div>
</body></html>"""

def render_youtube_embed(video_id: str, origin: str) -> Response:
    # back link: proxied YouTube home so the rest of the UI still works
    back = proxied(origin.rstrip('/') + '/')
    html = _EMBED_PAGE.replace('__VID__', video_id).replace('__BACK__', back)
    return Response(html, content_type='text/html; charset=utf-8')

def do_proxy(target: str):
    parsed = urlparse(target)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return Response('Invalid target URL', status=400)

    # YouTube watch/shorts/youtu.be -> serve the official embed player directly.
    # The embed iframe loads straight from YouTube (not through the proxy),
    # so it uses the user's own IP and isn't treated as a bot.
    vid = _extract_video_id(target)
    if vid:
        return render_youtube_embed(vid, f'{parsed.scheme}://{parsed.netloc}')

    is_navigation = 'text/html' in request.headers.get('Accept', '')
    headers = {
        'User-Agent': UA,
        'Accept': request.headers.get('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'),
        'Accept-Language': request.headers.get('Accept-Language', 'ja,en-US;q=0.9,en;q=0.8'),
        'Referer': f'{parsed.scheme}://{parsed.netloc}/',
        'Origin': f'{parsed.scheme}://{parsed.netloc}',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document' if is_navigation else 'empty',
        'Sec-Fetch-Mode': 'navigate' if is_navigation else 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1',
        'sec-ch-ua': '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }
    # forward the content-type of the original request (needed for API POSTs)
    if request.content_type:
        headers['Content-Type'] = request.content_type

    try:
        upstream = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=request.get_data() if request.method in ('POST', 'PUT', 'PATCH') else None,
            cookies=request.cookies,
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=False,
        )
    except requests.exceptions.Timeout:
        return Response('Upstream timeout', status=504)
    except requests.exceptions.RequestException as e:
        return Response(f'Upstream error: {e}', status=502)

    # follow redirects but keep them inside the proxy
    if upstream.is_redirect or upstream.status_code in (301, 302, 303, 307, 308):
        loc = upstream.headers.get('Location', '')
        if loc:
            return redirect(proxied(absolutize(loc, target)), code=302)

    ct = upstream.headers.get('Content-Type', '')

    if is_html(ct):
        body = rewrite_html(upstream.text, target)
        resp = Response(body, status=upstream.status_code, content_type='text/html; charset=utf-8')
    elif is_css(ct, target):
        body = rewrite_css(upstream.text, target)
        resp = Response(body, status=upstream.status_code, content_type=ct)
    else:
        resp = Response(upstream.content, status=upstream.status_code, content_type=ct or 'application/octet-stream')

    # pass through a few safe headers, strip framing/security blockers
    for h in ('Cache-Control', 'Content-Disposition', 'Last-Modified', 'ETag'):
        if h in upstream.headers:
            resp.headers[h] = upstream.headers[h]
    resp.headers['Access-Control-Allow-Origin'] = '*'
    # remember which origin we're browsing, so bare-path navigations
    # (e.g. SPA routes like /watch?v=...) can be recovered later.
    resp.set_cookie('__px_origin', f'{parsed.scheme}://{parsed.netloc}',
                    max_age=3600, samesite='Lax', path='/')
    return resp

# ---- Routes ----

@app.route('/p/<path:token>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def proxy_route(token):
    # token may include extra path/query appended by relative links the browser resolved
    # we only base64-decoded the first segment; but since we always emit full-url tokens,
    # the whole token is our base64. However browsers may append ?query — keep it.
    q = request.query_string.decode('utf-8')
    try:
        target = dec(token)
    except Exception:
        return Response('Bad token', status=400)
    if q:
        sep = '&' if urlparse(target).query else '?'
        target = target + sep + q
    return do_proxy(target)

@app.route('/go', methods=['GET', 'POST'])
def go():
    url = (request.values.get('url') or '').strip()
    if not url:
        return redirect('/')
    if not re.match(r'^https?://', url, re.IGNORECASE):
        url = 'https://' + url
    return redirect(proxied(url), code=302)

@app.route('/')
def home():
    return render_template_string(HOME_HTML)

@app.route('/health')
def health():
    return {'status': 'ok'}

def _origin_from_context():
    """Recover the site being browsed from Referer (a /p/<token> URL) or cookie."""
    ref = request.headers.get('Referer', '')
    m = re.search(r'/p/([A-Za-z0-9_\-]+)', ref)
    if m:
        try:
            u = urlparse(dec(m.group(1)))
            if u.scheme and u.netloc:
                return f'{u.scheme}://{u.netloc}'
        except Exception:
            pass
    c = request.cookies.get('__px_origin')
    if c and c.startswith(('http://', 'https://')):
        return c
    return None

@app.route('/<path:subpath>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def catchall(subpath):
    # Bare paths that aren't /p/..., /go, /health, / — typically SPA routes
    # like /watch?v=... that the site navigated to without our prefix.
    origin = _origin_from_context()
    if not origin:
        return Response('Not found (no proxy context)', status=404)
    target = origin.rstrip('/') + '/' + subpath
    q = request.query_string.decode('utf-8')
    if q:
        target += '?' + q
    # watch/shorts -> serve embed straight away (do_proxy handles it)
    if _extract_video_id(target):
        return do_proxy(target)
    # for GET navigations, redirect so the URL bar becomes a proper /p/ link;
    # for other methods, proxy in place to preserve the body.
    if request.method == 'GET':
        return redirect(proxied(target), code=302)
    return do_proxy(target)

HOME_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Proxy</title>
<style>
  :root{ --bg:#0b0f1a; --panel:#121826; --line:#1e2740; --text:#e6e9f0; --muted:#8a93a8; --accent:#38e0c8; }
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%}
  body{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:radial-gradient(1200px 600px at 70% -10%, #16233b 0%, var(--bg) 55%);
    color:var(--text); display:flex; align-items:center; justify-content:center; padding:24px;
  }
  .card{ width:100%; max-width:560px; }
  .eyebrow{ font-size:.72rem; letter-spacing:.28em; text-transform:uppercase; color:var(--muted); margin-bottom:14px; }
  h1{ font-size:2.6rem; line-height:1.05; font-weight:700; letter-spacing:-.02em; margin-bottom:10px; }
  h1 .g{ background:linear-gradient(120deg,var(--accent),#6aa8ff); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
  p.lead{ color:var(--muted); font-size:.98rem; line-height:1.55; margin-bottom:28px; }
  form{ display:flex; gap:10px; }
  input{
    flex:1; padding:16px 18px; background:var(--panel); border:1px solid var(--line);
    border-radius:12px; color:var(--text); font-size:1rem; outline:none; transition:border-color .2s, box-shadow .2s;
  }
  input::placeholder{ color:#586178 }
  input:focus{ border-color:var(--accent); box-shadow:0 0 0 4px rgba(56,224,200,.12) }
  button{
    padding:16px 22px; border:none; border-radius:12px; cursor:pointer; font-weight:600; font-size:1rem;
    color:#04120f; background:linear-gradient(120deg,var(--accent),#6aa8ff); transition:transform .15s, filter .2s;
  }
  button:hover{ transform:translateY(-2px); filter:brightness(1.05) }
  .hint{ margin-top:18px; font-size:.82rem; color:var(--muted); }
  .hint code{ background:var(--panel); border:1px solid var(--line); padding:2px 7px; border-radius:6px; color:#b9c2d8 }
</style>
</head>
<body>
  <div class="card">
    <div class="eyebrow">Web Proxy</div>
    <h1>Route anything<br>through <span class="g">the proxy</span>.</h1>
    <p class="lead">URLを入力してEnter。ページ、画像、動画、CSS、JS、リンク遷移まで、すべてこのサーバー経由で取得します。</p>
    <form action="/go" method="get">
      <input name="url" placeholder="example.com" autocomplete="off" autofocus>
      <button type="submit">Open</button>
    </form>
    <div class="hint">直接叩くなら <code>/go?url=example.com</code> も可。</div>
  </div>
</body>
</html>"""

if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
