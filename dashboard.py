"""Small OAuth2 dashboard for ourhope. Configure DISCORD_CLIENT_ID/SECRET and DASHBOARD_SECRET."""
import os, secrets, sqlite3, urllib.parse
from pathlib import Path
import aiohttp
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

DB_PATH = os.getenv("DB_PATH", "ourhope.sqlite3")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8080/callback")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")
API = "https://discord.com/api/v10"
app = FastAPI(title="ourhope dashboard")
app.add_middleware(SessionMiddleware, secret_key=DASHBOARD_SECRET or secrets.token_urlsafe(48), https_only=REDIRECT_URI.startswith("https://"), same_site="lax")


def db():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}')")
    return con


def page(body: str, title="ourhope dashboard") -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>
    :root{{--bg:#0b0d12;--panel:#151923;--line:#272d3a;--text:#edf0f7;--muted:#9ba5b8;--accent:#9a7bff}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(ellipse at top,#1b1a2d,var(--bg) 55%);color:var(--text);font:15px system-ui,sans-serif}}main{{max-width:900px;margin:48px auto;padding:20px}}header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}}h1{{font-size:28px;margin:0}}.muted{{color:var(--muted)}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px;margin:16px 0}}a,button{{color:white;background:#7057dc;text-decoration:none;border:0;border-radius:10px;padding:11px 15px;cursor:pointer;font-weight:650}}a.secondary{{background:#252b38}}label{{display:block;margin:15px 0 7px;color:#c9d0df}}input,select{{width:100%;background:#0c1018;color:white;border:1px solid #343b4c;border-radius:9px;padding:12px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}.row{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}small{{color:var(--muted)}}.tag{{border:1px solid var(--line);border-radius:99px;padding:4px 9px;color:#c5b8ff;font-size:12px}}</style></head><body><main>{body}</main></body></html>''')


def user(request: Request):
    u=request.session.get("user")
    if not u: raise HTTPException(401, "Sign in first")
    return u

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    u=request.session.get("user")
    if not u:
        return page('''<header><h1>ourhope <span class="tag">CONTROL PANEL</span></h1></header><section class="panel"><h2>One dashboard. Your whole server.</h2><p class="muted">Manage security settings, moderation defaults, logs, tickets, and community modules.</p><p><a href="/login">Continue with Discord</a></p><small>Requires Discord OAuth credentials configured by the bot owner.</small></section>''')
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer "+request.session["access_token"]}) as s:
        async with s.get(API+"/users/@me/guilds") as r:
            if r.status != 200: return RedirectResponse('/logout')
            guilds=await r.json()
    manageable=[g for g in guilds if (int(g.get("permissions","0")) & 0x20) or (int(g.get("permissions","0")) & 0x8)]
    cards=''.join(f'<section class="panel"><h3>{html_escape(g["name"])}</h3><p class="muted">Server ID: {g["id"]}</p><a href="/guild/{g["id"]}">Configure server</a></section>' for g in manageable)
    return page(f'<header><h1>ourhope</h1><div class="row"><span class="muted">Signed in as {html_escape(u.get("username","Discord user"))}</span><a class="secondary" href="/logout">Sign out</a></div></header><p class="muted">Choose a server where you have Manage Server or Administrator.</p>{cards or "<section class=panel>No manageable servers found.</section>"}')

def html_escape(s):
    import html
    return html.escape(str(s))

@app.get('/login')
async def login():
    if not CLIENT_ID or not CLIENT_SECRET: return page('<section class="panel"><h2>Dashboard not configured</h2><p>Set DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DASHBOARD_SECRET and DISCORD_REDIRECT_URI in your hosting variables.</p></section>')
    state=secrets.token_urlsafe(24)
    # State is signed in session cookie and checked on callback.
    url=API.replace('/api/v10','')+'/oauth2/authorize?'+urllib.parse.urlencode({'client_id':CLIENT_ID,'redirect_uri':REDIRECT_URI,'response_type':'code','scope':'identify guilds','state':state})
    response=RedirectResponse(url); response.set_cookie('oauth_state',state,httponly=True,secure=REDIRECT_URI.startswith('https://'),samesite='lax',max_age=600)
    return response

@app.get('/callback')
async def callback(request: Request, code: str, state: str):
    # OAuth state cookie prevents login CSRF.
    if not request.cookies.get('oauth_state') or not secrets.compare_digest(request.cookies.get('oauth_state',''),state):
        raise HTTPException(400,'OAuth state mismatch. Please try again.')
    async with aiohttp.ClientSession() as s:
        async with s.post(API.replace('/api/v10','')+'/oauth2/token', data={'client_id':CLIENT_ID,'client_secret':CLIENT_SECRET,'grant_type':'authorization_code','code':code,'redirect_uri':REDIRECT_URI}, headers={'Content-Type':'application/x-www-form-urlencoded'}) as r:
            data=await r.json()
            if r.status != 200: raise HTTPException(400,'Discord OAuth failed; check redirect URI and credentials.')
        async with s.get(API+'/users/@me',headers={'Authorization':'Bearer '+data['access_token']}) as r: u=await r.json()
    response=RedirectResponse('/'); response.delete_cookie('oauth_state'); response.set_cookie('session_marker','1',httponly=True,secure=REDIRECT_URI.startswith('https://'),samesite='lax')
    # Starlette session cookie stores OAuth token. Use HTTPS in production.
    response.set_cookie('dashboard_login','1',httponly=True,secure=REDIRECT_URI.startswith('https://'),samesite='lax')
    # Session middleware is updated via request session and persists on response.
    request.session['user']={'id':u.get('id'),'username':u.get('username')}; request.session['access_token']=data['access_token']; request.session['expires_at']=data.get('expires_in',0)
    return response

@app.get('/logout')
async def logout(request: Request):
    request.session.clear(); return RedirectResponse('/')

@app.get('/guild/{guild_id}', response_class=HTMLResponse)
async def guild_page(request: Request, guild_id: str):
    user(request)
    async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+request.session['access_token']}) as s:
        async with s.get(API+'/users/@me/guilds') as r: guilds=await r.json()
    g=next((x for x in guilds if x['id']==guild_id and ((int(x.get('permissions','0'))&0x20) or (int(x.get('permissions','0'))&0x8))),None)
    if not g: raise HTTPException(403,'You do not have permission to configure this server.')
    with db() as con:
        row=con.execute('SELECT settings FROM guild_settings WHERE guild_id=?',(int(guild_id),)).fetchone()
    import json
    settings=json.loads(row['settings']) if row else {}
    fields=[('anti_link','Delete links and apply configured timeout','checkbox'),('anti_spam','Timeout repeated spam','checkbox'),('anti_raid','Enable join-spike protection','checkbox'),('log_channel','Moderation log channel ID','text'),('support_role','Support team role ID','text'),('ticket_category','Existing ticket category ID (optional)','text'),('prefix','Prefix for legacy commands','text')]
    inputs=''.join(f'<div><label for="{k}">{label}</label>'+ (f'<select name="{k}" id="{k}"><option value="true" {"selected" if settings.get(k,True) else ""}>Enabled</option><option value="false" {"selected" if not settings.get(k,True) else ""}>Disabled</option></select>' if typ=='checkbox' else f'<input name="{k}" id="{k}" value="{html_escape(settings.get(k,"?" if k=="prefix" else ""))}" maxlength="100">')+'</div>' for k,label,typ in fields)
    return page(f'<header><h1>{html_escape(g["name"])}</h1><a class="secondary" href="/">← Servers</a></header><section class="panel"><p class="muted">Changes are saved per server. Some controls require the bot to have the matching Discord permissions.</p><form method="post" action="/guild/{guild_id}"><div class="grid">{inputs}</div><p style="margin-top:22px"><button>Save settings</button></p></form></section>')

@app.post('/guild/{guild_id}')
async def save_guild(request: Request, guild_id: str):
    user(request)
    async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+request.session['access_token']}) as s:
        async with s.get(API+'/users/@me/guilds') as r: guilds=await r.json()
    g=next((x for x in guilds if x['id']==guild_id and ((int(x.get('permissions','0'))&0x20) or (int(x.get('permissions','0'))&0x8))),None)
    if not g: raise HTTPException(403,'Forbidden')
    form=await request.form()
    import json
    settings={k:(form.get(k)=='true' if k in ('anti_link','anti_spam','anti_raid') else str(form.get(k,''))[:100]) for k in ('anti_link','anti_spam','anti_raid','log_channel','support_role','ticket_category','prefix')}
    with db() as con: con.execute('INSERT INTO guild_settings(guild_id,settings) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET settings=excluded.settings',(int(guild_id),json.dumps(settings)))
    return RedirectResponse(f'/guild/{guild_id}?saved=1',status_code=303)
