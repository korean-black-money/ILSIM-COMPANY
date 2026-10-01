"""ILSIM storefront: same-origin API, SQLite storage and server-side admin sessions."""
import argparse, base64, hashlib, hmac, json, mimetypes, os, re, secrets, sqlite3, time
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, unquote
from http.cookies import SimpleCookie

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get('ILSIM_DATA_DIR', ROOT / '.runtime'))
DB = RUNTIME / 'store.sqlite3'
ORIGIN = os.environ.get('ILSIM_ORIGIN', '')
SESSION_TTL = 8 * 3600
LOGIN_LIMIT = {}
WRITE_LIMIT = {}
LAST_CLEANUP = 0
STATES = ('pending','processing','confirmed','completed','cancelled')

def now(): return datetime.now(timezone.utc).isoformat()
@contextmanager
def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        with con:yield con
    finally:con.close()

def init_db():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUNTIME,0o700)
    with connect() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT NOT NULL, admin INTEGER DEFAULT 0, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, created TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS inquiries (id TEXT PRIMARY KEY, created TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, post_id TEXT NOT NULL, author TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL, password TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS likes (post_id TEXT NOT NULL, visitor TEXT NOT NULL, PRIMARY KEY(post_id,visitor));
        CREATE TABLE IF NOT EXISTS visits (day TEXT NOT NULL, visitor TEXT NOT NULL, PRIMARY KEY(day,visitor));
        ''')
        if 'request_key' not in [r[1] for r in c.execute('PRAGMA table_info(inquiries)')]:
            c.execute('ALTER TABLE inquiries ADD COLUMN request_key TEXT')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS inquiry_request_key ON inquiries(request_key)')
        if not c.execute("SELECT 1 FROM config WHERE key='initialized'").fetchone():
            catalog=json.loads((ROOT/'data/catalog.json').read_text())
            for p in catalog['products']: c.execute('INSERT INTO products VALUES (?,?)',(p['id'],json.dumps(p,ensure_ascii=False)))
            for p in json.loads((ROOT/'data/posts.json').read_text()): c.execute('INSERT INTO posts VALUES (?,?)',(p['id'],json.dumps(p,ensure_ascii=False)))
            c.execute('INSERT INTO config VALUES (?,?)',('storeUrl',catalog['storeUrl']))
            c.execute('INSERT INTO config VALUES (?,?)',('initialized','1'))
        if not c.execute("SELECT 1 FROM config WHERE key='admin_hash'").fetchone():
            password=os.environ.get('ILSIM_ADMIN_PASSWORD') or secrets.token_urlsafe(20)
            if len(password)<12: raise ValueError('ILSIM_ADMIN_PASSWORD must contain at least 12 characters')
            c.execute('INSERT INTO config VALUES (?,?)',('admin_hash',hash_password(password)))
            if not os.environ.get('ILSIM_ADMIN_PASSWORD'):
                p=RUNTIME/'admin-access.txt'
                fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'w') as f:f.write('관리자 아이디: ilsim\n초기 비밀번호: '+password+'\n')
        c.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=90)).isoformat()
        c.execute('DELETE FROM inquiries WHERE created < ?', (cutoff,))
        c.execute('DELETE FROM orders WHERE created < ?', (cutoff,))
        c.execute('DELETE FROM visits WHERE day < ?', (cutoff[:10],))
    os.chmod(DB,0o600)

def hash_password(password):
    salt=secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()
def verify_password(password, encoded):
    salt,value=encoded.split(':',1)
    return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex(),value)
def text(value,limit=500,required=False):
    if not isinstance(value,str) or len(value)>limit or (required and not value.strip()):raise ValueError('입력 내용을 확인해 주세요.')
    return value.strip()
def store_url(value):
    value=text(value,500)
    if value:
        u=urlsplit(value)
        if u.scheme!='https' or u.hostname not in ('smartstore.naver.com','brand.naver.com') or u.username or u.password or u.port:raise ValueError('네이버 스마트스토어 https 주소를 입력해 주세요.')
    return value

def image_url(value):
    value=text(value,500)
    if not re.fullmatch(r'images/[a-zA-Z0-9_./-]+\.(?:webp|png|jpe?g)',value) or '..' in value:raise ValueError('업로드한 상품 이미지를 선택해 주세요.')
    return value

def product_data(data, pid):
    opts=data.get('options')
    if not isinstance(opts,list) or not 1<=len(opts)<=20:raise ValueError('옵션을 1~20개 입력해 주세요.')
    clean=[]
    for o in opts:
        price=o.get('price')
        if type(price)!=int or not 0<price<=100000000:raise ValueError('가격은 0보다 큰 정수로 입력해 주세요.')
        clean.append({'name':text(o.get('name'),80,True),'price':price})
    if len({o['name'] for o in clean})!=len(clean):raise ValueError('옵션명이 중복되었습니다.')
    if data.get('category') not in ('fig','gecko','other') or data.get('stock') not in ('available','soldout','preparing','inquiry'):raise ValueError('상품 분류와 판매 상태를 확인해 주세요.')
    details=data.get('details','')
    if details not in ('','red-fig','green-fig'):raise ValueError('잘못된 상세 이미지입니다.')
    return {'id':pid,'name':text(data.get('name'),100,True),'category':data['category'],'stock':data['stock'],'options':clean,'image':image_url(data.get('image','')),'storeUrl':store_url(data.get('storeUrl','')),'details':details,**{k:text(data.get(k,''),2000) for k in ('tag','subtitle','description','shipping','priceNote')}}

def build_order(data,c):
    items=data.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=30:raise ValueError('선택한 상품을 확인해 주세요.')
    rows=[]; total=0
    for item in items:
        r=c.execute('SELECT body FROM products WHERE id=?',(text(item.get('productId'),100,True),)).fetchone()
        if not r:raise ValueError('판매가 종료된 상품입니다. 장바구니를 확인해 주세요.')
        p=json.loads(r['body'])
        if p['stock'] in ('soldout','preparing'):raise ValueError('품절 또는 준비 중인 상품이 포함되어 있습니다.')
        option=next((o for o in p['options'] if o['name']==item.get('option')),None)
        qty=item.get('qty')
        if not option or type(qty)!=int or not 1<=qty<=99:raise ValueError('옵션과 수량을 다시 확인해 주세요.')
        if item.get('unitPrice')!=option['price']:raise ValueError('상품 가격이 변경되었습니다. 새로고침 후 다시 확인해 주세요.')
        rows.append({'productId':p['id'],'product':p['name'],'option':option['name'],'price':option['price'],'qty':qty})
        total+=option['price']*qty
    return {'items':rows,'total':total,'name':text(data.get('name'),80,True),'phone':phone(data.get('phone')),'memo':text(data.get('memo',''),2000)}
def phone(value):
    value=text(value,30,True)
    if not re.fullmatch(r'[0-9+() -]{8,30}',value):raise ValueError('연락 가능한 전화번호를 입력해 주세요.')
    return value

class Handler(BaseHTTPRequestHandler):
    server_version='ILSIM'
    def log_message(self,fmt,*args): pass
    def send_json(self,data,status=200,cookie=None):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store')
        self.security_headers()
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def security_headers(self):
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https://*.pstatic.net; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    def session(self,c):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return None
        token=cookie.get('ilsim_session')
        if not token:return None
        return c.execute('SELECT * FROM sessions WHERE token=? AND expires>?',(hashlib.sha256(token.value.encode()).hexdigest(),time.time())).fetchone()
    def new_session(self,c,admin=False):
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,int(admin),time.time()+SESSION_TTL))
        cookie=f'ilsim_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}'
        if ORIGIN.startswith('https://'):cookie+='; Secure'
        return csrf,cookie
    def body(self):
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=7500000:raise ValueError('요청 크기를 확인해 주세요.')
        if 'application/json' not in self.headers.get('Content-Type',''):raise ValueError('잘못된 요청 형식입니다.')
        d=json.loads(self.rfile.read(length))
        if not isinstance(d,dict):raise ValueError('잘못된 요청입니다.')
        return d
    def do_GET(self):
        path=urlsplit(self.path).path
        if path.startswith('/api/'):
            try:self.api_get(path)
            except Exception:self.send_json({'error':'데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.'},500)
            return
        decoded=unquote(path)
        if decoded in ('/','/admin','/admin/') or decoded.startswith('/product/'):p=ROOT/'index.html'
        elif decoded=='/naveraa12cb19ff5b0b79be28136fa909732b.html':p=ROOT/decoded.lstrip('/')
        elif decoded in ('/assets/app.js','/assets/styles.css'):p=ROOT/decoded.lstrip('/')
        elif decoded.startswith('/images/') and '..' not in decoded:p=ROOT/decoded.lstrip('/')
        else:self.send_error(404);return
        if not p.is_file() or not p.resolve().is_relative_to(ROOT):self.send_error(404);return
        raw=p.read_bytes();self.send_response(200);self.security_headers()
        self.send_header('Content-Type',mimetypes.guess_type(p)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-cache' if p.suffix in ('.html','.js','.css') else 'public, max-age=3600');self.end_headers();self.wfile.write(raw)
    def api_get(self,path):
        global LAST_CLEANUP
        with connect() as c:
            if time.time()-LAST_CLEANUP>3600:
                cutoff=(datetime.now(timezone.utc)-timedelta(days=90)).isoformat()
                for table in ('orders','inquiries'):c.execute(f'DELETE FROM {table} WHERE created < ?', (cutoff,))
                c.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
                c.execute('DELETE FROM visits WHERE day < ?', (cutoff[:10],))
                LAST_CLEANUP=time.time()
            s=self.session(c)
            if path=='/api/session':
                cookie=None
                if not s:csrf,cookie=self.new_session(c)
                else:csrf=s['csrf']
                self.send_json({'csrf':csrf,'admin':bool(s and s['admin'])},cookie=cookie);return
            if path=='/api/catalog':
                self.send_json({'products':[json.loads(r[0]) for r in c.execute('SELECT body FROM products')],'storeUrl':c.execute("SELECT value FROM config WHERE key='storeUrl'").fetchone()[0]});return
            if path=='/api/reviews':self.send_json(json.loads((ROOT/'data/reviews.json').read_text()));return
            if path=='/api/posts':
                posts=[json.loads(r[0]) for r in c.execute('SELECT body FROM posts')]
                for p in posts:
                    p['likes']=c.execute('SELECT count(*) FROM likes WHERE post_id=?',(p['id'],)).fetchone()[0]
                    p['comments']=c.execute('SELECT count(*) FROM comments WHERE post_id=?',(p['id'],)).fetchone()[0]
                    p['liked']=bool(s and c.execute('SELECT 1 FROM likes WHERE post_id=? AND visitor=?',(p['id'],s['token'])).fetchone())
                self.send_json([p for p in posts if p['visible'] or (s and s['admin'])]);return
            if re.fullmatch('/api/posts/[^/]+/comments',path):
                pid=path.split('/')[3];p=c.execute('SELECT body FROM posts WHERE id=?',(pid,)).fetchone()
                if not p or not (json.loads(p[0])['visible'] or (s and s['admin'])):self.send_json({'error':'게시물이 없습니다.'},404);return
                self.send_json([dict(r) for r in c.execute('SELECT id,author,content,created FROM comments WHERE post_id=? ORDER BY created',(pid,))]);return
            if path.startswith('/api/admin/'):
                if not s or not s['admin']:self.send_json({'error':'관리자 로그인이 필요합니다.'},401);return
                if path in ('/api/admin/orders','/api/admin/inquiries'):
                    table='orders' if path.endswith('orders') else 'inquiries'
                    self.send_json([{'id':r['id'],'created':r['created'],'status':r['status'],**json.loads(r['body'])} for r in c.execute(f'SELECT * FROM {table} ORDER BY created DESC')]);return
                if path=='/api/admin/stats':
                    orders=c.execute('SELECT status,body FROM orders').fetchall()
                    daily=[dict(r) for r in c.execute('SELECT day,count(*) AS visitors FROM visits GROUP BY day ORDER BY day DESC LIMIT 14')]
                    self.send_json({'orders':len(orders),'pending':sum(r['status']=='pending' for r in orders),'confirmed':sum(json.loads(r['body'])['total'] for r in orders if r['status'] in ('confirmed','completed')),'inquiries':c.execute("SELECT count(*) FROM inquiries WHERE status='pending'").fetchone()[0],'visits':daily});return
            self.send_json({'error':'찾을 수 없습니다.'},404)
    def do_POST(self):self.mutate()
    def do_PUT(self):self.mutate()
    def do_DELETE(self):self.mutate()
    def mutate(self):
        path=urlsplit(self.path).path
        try:
            with connect() as c:
                s=self.session(c)
                expected=ORIGIN or 'http://'+self.headers.get('Host','')
                if self.headers.get('Origin')!=expected or not s or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),s['csrf']):
                    self.send_json({'error':'세션이 만료되었습니다. 새로고침 후 다시 시도해 주세요.'},403);return
                if not s['admin'] and path not in ('/api/login','/api/visit','/api/logout'):
                    key=self.client_address[0]
                    times=[t for t in WRITE_LIMIT.get(key,[]) if t>time.time()-60]
                    WRITE_LIMIT[key]=times
                    if len(times)>=30:self.send_json({'error':'요청이 많습니다. 잠시 후 다시 시도해 주세요.'},429);return
                    times.append(time.time())
                data=self.body()
                if path.startswith('/api/admin/') and not s['admin']:self.send_json({'error':'관리자 로그인이 필요합니다.'},401);return
                if path=='/api/login':
                    ip=self.client_address[0];attempts=[t for t in LOGIN_LIMIT.get(ip,[]) if t>time.time()-900];LOGIN_LIMIT[ip]=attempts
                    if len(attempts)>=8:self.send_json({'error':'로그인 시도가 많습니다. 15분 후 다시 시도해 주세요.'},429);return
                    attempts.append(time.time())
                    encoded=c.execute("SELECT value FROM config WHERE key='admin_hash'").fetchone()[0]
                    if data.get('username')!='ilsim' or not verify_password(text(data.get('password'),200),encoded):self.send_json({'error':'아이디 또는 비밀번호가 올바르지 않습니다.'},401);return
                    LOGIN_LIMIT.pop(ip,None);c.execute('DELETE FROM sessions WHERE token=?',(s['token'],));csrf,cookie=self.new_session(c,True)
                    self.send_json({'csrf':csrf,'admin':True},cookie=cookie);return
                if path=='/api/logout':
                    c.execute('DELETE FROM sessions WHERE token=?',(s['token'],));csrf,cookie=self.new_session(c);self.send_json({'csrf':csrf,'admin':False},cookie=cookie);return
                if path=='/api/visit':
                    c.execute('INSERT OR IGNORE INTO visits VALUES (?,?)',(datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).date().isoformat(),s['token']));self.send_json({'ok':True});return
                if path in ('/api/orders','/api/inquiries'):
                    if data.get('consent') is not True:raise ValueError('개인정보 수집·이용에 동의해 주세요.')
                    key=text(data.get('requestKey'),100,True)
                    if path=='/api/orders':
                        old=c.execute('SELECT id FROM orders WHERE request_key=?',(key,)).fetchone()
                        if old:self.send_json({'id':old[0]});return
                        record=build_order(data,c);id='IL-'+secrets.token_hex(6).upper()
                        c.execute('INSERT INTO orders VALUES (?,?,?,?,?)',(id,now(),'pending',json.dumps(record,ensure_ascii=False),key))
                    else:
                        old=c.execute('SELECT id FROM inquiries WHERE request_key=?',(key,)).fetchone()
                        if old:self.send_json({'id':old[0]});return
                        id='IQ-'+secrets.token_hex(6).upper()
                        record={'name':text(data.get('name'),80,True),'phone':phone(data.get('phone')),'category':text(data.get('category'),80,True),'message':text(data.get('message'),5000,True)}
                        c.execute('INSERT INTO inquiries VALUES (?,?,?,?,?)',(id,now(),'pending',json.dumps(record,ensure_ascii=False),key))
                    self.send_json({'id':id},201);return
                if re.fullmatch('/api/posts/[^/]+/(comments|like)',path):
                    pid=path.split('/')[3];post=c.execute('SELECT body FROM posts WHERE id=?',(pid,)).fetchone()
                    if not post or not json.loads(post[0])['visible']:raise ValueError('게시물이 없습니다.')
                    if path.endswith('/like'):
                        if c.execute('SELECT 1 FROM likes WHERE post_id=? AND visitor=?',(pid,s['token'])).fetchone():c.execute('DELETE FROM likes WHERE post_id=? AND visitor=?',(pid,s['token']))
                        else:c.execute('INSERT INTO likes VALUES (?,?)',(pid,s['token']))
                    else:
                        password=text(data.get('password'),100,True)
                        if len(password)<4:raise ValueError('삭제 비밀번호는 4자 이상 입력해 주세요.')
                        c.execute('INSERT INTO comments VALUES (?,?,?,?,?,?)',(secrets.token_hex(12),pid,text(data.get('author'),40,True),text(data.get('content'),2000,True),now(),hash_password(password)))
                    self.send_json({'ok':True});return
                if re.fullmatch('/api/comments/[a-z0-9]+',path) and self.command=='DELETE':
                    row=c.execute('SELECT * FROM comments WHERE id=?',(path.split('/')[-1],)).fetchone()
                    if not row or not (s['admin'] or verify_password(text(data.get('password'),100),row['password'])):raise ValueError('비밀번호가 일치하지 않습니다.')
                    c.execute('DELETE FROM comments WHERE id=?',(row['id'],));self.send_json({'ok':True});return
                if path=='/api/admin/settings':
                    c.execute("UPDATE config SET value=? WHERE key='storeUrl'",(store_url(data.get('storeUrl','')),));self.send_json({'ok':True});return
                if path=='/api/admin/upload':
                    raw=base64.b64decode(text(data.get('data'),7000000),validate=True)
                    if len(raw)>5000000:raise ValueError('이미지는 5MB 이하로 올려 주세요.')
                    ext='png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if raw.startswith(b'\xff\xd8\xff') else 'webp' if raw[:4]==b'RIFF' and raw[8:12]==b'WEBP' else None
                    if not ext:raise ValueError('PNG, JPG, WEBP 이미지를 사용해 주세요.')
                    dest=ROOT/'images/uploads';dest.mkdir(exist_ok=True);name=secrets.token_hex(16)+'.'+ext;(dest/name).write_bytes(raw)
                    self.send_json({'url':'images/uploads/'+name});return
                if path=='/api/admin/products':
                    pid=text(data.get('id',''),100) or 'p-'+secrets.token_hex(8)
                    if not re.fullmatch(r'[a-z0-9-]+',pid):raise ValueError('잘못된 상품 번호입니다.')
                    product=product_data(data,pid)
                    c.execute('INSERT OR REPLACE INTO products VALUES (?,?)',(pid,json.dumps(product,ensure_ascii=False)));self.send_json(product);return
                if re.fullmatch('/api/admin/products/[a-z0-9-]+',path) and self.command=='DELETE':
                    c.execute('DELETE FROM products WHERE id=?',(path.split('/')[-1],));self.send_json({'ok':True});return
                if path=='/api/admin/posts':
                    pid=text(data.get('id',''),100) or 'post-'+secrets.token_hex(8)
                    if not re.fullmatch(r'[a-z0-9-]+',pid):raise ValueError('잘못된 게시물 번호입니다.')
                    post={'id':pid,'title':text(data.get('title'),200,True),'category':text(data.get('category'),40,True),'content':text(data.get('content'),15000,True),'author':'일심','date':text(data.get('date',now()[:10]),10),'visible':bool(data.get('visible',True))}
                    c.execute('INSERT OR REPLACE INTO posts VALUES (?,?)',(pid,json.dumps(post,ensure_ascii=False)));self.send_json(post);return
                if re.fullmatch('/api/admin/posts/[a-z0-9-]+',path) and self.command=='DELETE':
                    pid=path.split('/')[-1];c.execute('DELETE FROM posts WHERE id=?',(pid,));c.execute('DELETE FROM comments WHERE post_id=?',(pid,));c.execute('DELETE FROM likes WHERE post_id=?',(pid,));self.send_json({'ok':True});return
                if re.fullmatch('/api/admin/(orders|inquiries)/[A-Za-z0-9-]+',path):
                    table=path.split('/')[3];id=path.split('/')[-1]
                    if self.command=='DELETE':c.execute(f'DELETE FROM {table} WHERE id=?',(id,))
                    else:
                        if data.get('status') not in STATES:raise ValueError('잘못된 처리 상태입니다.')
                        c.execute(f'UPDATE {table} SET status=? WHERE id=?',(data['status'],id))
                    self.send_json({'ok':True});return
                self.send_json({'error':'찾을 수 없습니다.'},404)
        except (ValueError,TypeError,KeyError,AttributeError) as e:self.send_json({'error':str(e) if isinstance(e,ValueError) else '입력 내용을 확인해 주세요.'},400)
        except Exception:self.send_json({'error':'저장하지 못했습니다. 잠시 후 다시 시도해 주세요.'},500)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8931);parser.add_argument('--host',default='127.0.0.1');args=parser.parse_args()
    if args.host not in ('127.0.0.1','localhost') and not ORIGIN:raise SystemExit('외부 서비스에는 ILSIM_ORIGIN=https://도메인 설정이 필요합니다.')
    init_db();print(f'ILSIM: http://{args.host}:{args.port} | Admin: /#admin',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
