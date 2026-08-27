#!/usr/bin/env python3
import json, os, re, sqlite3, secrets, hashlib, io
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
import qrcode

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
DB_PATH = Path(os.environ.get('JANGIL_DB', ROOT / 'data' / 'jangil.db'))
HOST = os.environ.get('HOST','0.0.0.0')
PORT = int(os.environ.get('PORT','8080'))
ENV = os.environ.get('APP_ENV','development').lower()
ADMIN_KEY = os.environ.get('ADMIN_KEY','')
DEMO_PAYMENT = os.environ.get('DEMO_PAYMENT','true' if ENV != 'production' else 'false').lower() == 'true'

SCHEMA = '''
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 slug TEXT UNIQUE NOT NULL,
 title TEXT NOT NULL,
 venue TEXT NOT NULL,
 city TEXT NOT NULL DEFAULT 'Mauritius',
 starts_at TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 poster TEXT NOT NULL DEFAULT '',
 capacity INTEGER NOT NULL DEFAULT 500,
 status TEXT NOT NULL DEFAULT 'published',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_types (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 price INTEGER NOT NULL,
 quantity INTEGER NOT NULL,
 sold INTEGER NOT NULL DEFAULT 0,
 sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 reference TEXT UNIQUE NOT NULL,
 event_id INTEGER NOT NULL REFERENCES events(id),
 customer_name TEXT NOT NULL,
 customer_email TEXT NOT NULL,
 customer_phone TEXT NOT NULL DEFAULT '',
 amount INTEGER NOT NULL,
 payment_method TEXT NOT NULL,
 payment_status TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 public_id TEXT UNIQUE NOT NULL,
 order_id INTEGER NOT NULL REFERENCES orders(id),
 ticket_type_id INTEGER NOT NULL REFERENCES ticket_types(id),
 attendee_name TEXT NOT NULL,
 token_hash TEXT UNIQUE NOT NULL,
 token TEXT UNIQUE NOT NULL,
 checked_in_at TEXT,
 created_at TEXT NOT NULL
);
'''

def now(): return datetime.now(timezone.utc).isoformat()
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        if c.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0:
            c.execute('INSERT INTO events(slug,title,venue,city,starts_at,description,poster,capacity,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      ('wawa-live-mauritius','WAWA LIVE IN MAURITIUS','Jin Fei Business Industrial Park','Mauritius','2026-10-31T22:30:00+04:00','Une nuit live exceptionnelle. Billetterie officielle Jangil Tickets. 18+ • Casual Smart • Management reserves the right of admission.','',800,'published',now()))
            eid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.executemany('INSERT INTO ticket_types(event_id,name,price,quantity,sort_order) VALUES(?,?,?,?,?)',[
                (eid,'Prévente',800,450,1),(eid,'VIP',1200,200,2),(eid,'At the door',1000,150,3)
            ])

def event_payload(c, row):
    tts=[dict(x) for x in c.execute('SELECT id,name,price,quantity,sold,sort_order FROM ticket_types WHERE event_id=? ORDER BY sort_order,id',(row['id'],))]
    d=dict(row); d['ticket_types']=tts; return d

def read_json(handler):
    n=int(handler.headers.get('Content-Length','0') or 0)
    if n>1_000_000: raise ValueError('payload_too_large')
    raw=handler.rfile.read(n) if n else b'{}'
    return json.loads(raw.decode('utf-8'))

def send_json(h, code, obj):
    b=json.dumps(obj,ensure_ascii=False).encode()
    h.send_response(code); h.send_header('Content-Type','application/json; charset=utf-8'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)

def qr_png(data):
    img=qrcode.make(data)
    bio=io.BytesIO(); img.save(bio,format='PNG'); return bio.getvalue()

def is_admin(h):
    if not ADMIN_KEY:
        return ENV != 'production'
    supplied = h.headers.get('X-Admin-Key','')
    return secrets.compare_digest(supplied, ADMIN_KEY)

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        self.send_header('Permissions-Policy','camera=(self), geolocation=()')
        if ENV == 'production':
            self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
        super().end_headers()

    def translate_path(self, path):
        parsed=urlparse(path).path
        rel=parsed.lstrip('/') or 'index.html'
        if rel.startswith('api/'): return str(STATIC/'404')
        return str((STATIC/rel).resolve())

    def do_GET(self):
        p=urlparse(self.path).path
        try:
            if p == '/api/health':
                return send_json(self,200,{'ok':True,'env':ENV})
            if p == '/api/events':
                with db() as c:
                    rows=c.execute("SELECT * FROM events WHERE status='published' ORDER BY starts_at").fetchall()
                    return send_json(self,200,[event_payload(c,r) for r in rows])
            m=re.fullmatch(r'/api/events/(\d+)',p)
            if m:
                with db() as c:
                    r=c.execute('SELECT * FROM events WHERE id=?',(m.group(1),)).fetchone()
                    return send_json(self,200,event_payload(c,r)) if r else send_json(self,404,{'error':'event_not_found'})
            if p == '/api/admin/summary':
                if not is_admin(self): return send_json(self,401,{'error':'unauthorized'})
                with db() as c:
                    revenue=c.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE payment_status='paid'").fetchone()[0]
                    sold=c.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
                    checkins=c.execute('SELECT COUNT(*) FROM tickets WHERE checked_in_at IS NOT NULL').fetchone()[0]
                    orders=[dict(x) for x in c.execute('SELECT reference,customer_name,customer_email,amount,payment_status,created_at FROM orders ORDER BY id DESC LIMIT 10')]
                    events=[]
                    for r in c.execute('SELECT * FROM events ORDER BY starts_at'):
                        x=event_payload(c,r); events.append(x)
                    return send_json(self,200,{'revenue':revenue,'tickets_sold':sold,'checkins':checkins,'orders':orders,'events':events})
            m=re.fullmatch(r'/api/orders/([A-Z0-9-]+)',p)
            if m:
                with db() as c:
                    o=c.execute('SELECT * FROM orders WHERE reference=?',(m.group(1),)).fetchone()
                    if not o: return send_json(self,404,{'error':'order_not_found'})
                    ts=[dict(x) for x in c.execute('''SELECT tickets.public_id,tickets.token,tickets.checked_in_at,ticket_types.name AS type_name,ticket_types.price,events.title,events.venue,events.starts_at
                      FROM tickets JOIN ticket_types ON ticket_types.id=tickets.ticket_type_id JOIN events ON events.id=ticket_types.event_id WHERE tickets.order_id=?''',(o['id'],))]
                    return send_json(self,200,{'order':dict(o),'tickets':ts})
            m=re.fullmatch(r'/api/tickets/([A-Za-z0-9_-]+)/qr',p)
            if m:
                with db() as c:
                    t=c.execute('SELECT token FROM tickets WHERE public_id=?',(m.group(1),)).fetchone()
                    if not t: return send_json(self,404,{'error':'ticket_not_found'})
                    b=qr_png('JANGIL:'+t['token'])
                    self.send_response(200); self.send_header('Content-Type','image/png'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
            return super().do_GET()
        except Exception as e:
            return send_json(self,500,{'error':'server_error','detail':str(e)})

    def do_POST(self):
        p=urlparse(self.path).path
        try:
            if p == '/api/admin/events':
                if not is_admin(self): return send_json(self,401,{'error':'unauthorized'})
                data=read_json(self)
                required=['title','venue','starts_at','capacity','ticket_types']
                if any(not data.get(k) for k in required): return send_json(self,400,{'error':'missing_fields'})
                slug=re.sub(r'[^a-z0-9]+','-',data['title'].lower()).strip('-')+'-'+secrets.token_hex(2)
                with db() as c:
                    c.execute('INSERT INTO events(slug,title,venue,city,starts_at,description,poster,capacity,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (slug,data['title'],data['venue'],data.get('city','Mauritius'),data['starts_at'],data.get('description',''),data.get('poster',''),int(data['capacity']),'published',now()))
                    eid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for i,t in enumerate(data['ticket_types']):
                        c.execute('INSERT INTO ticket_types(event_id,name,price,quantity,sort_order) VALUES(?,?,?,?,?)',(eid,t['name'],int(t['price']),int(t['quantity']),i))
                    r=c.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
                    return send_json(self,201,event_payload(c,r))
            if p == '/api/checkout':
                data=read_json(self)
                items=data.get('items',[])
                if not items: return send_json(self,400,{'error':'empty_cart'})
                name=str(data.get('name','')).strip(); email=str(data.get('email','')).strip()
                if len(name)<2 or '@' not in email: return send_json(self,400,{'error':'invalid_customer'})
                method=data.get('payment_method','demo')
                if method=='demo' and not DEMO_PAYMENT:
                    return send_json(self,409,{'error':'demo_disabled','message':'Le paiement démo est désactivé sur cet environnement.'})
                if method=='mcb' and not os.environ.get('MCB_MERCHANT_ID'):
                    return send_json(self,409,{'error':'gateway_not_configured','message':'MCB MPGS nécessite les identifiants marchand et la configuration webhook.'})
                with db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    event_id=None; amount=0; expanded=[]
                    for item in items:
                        tid=int(item['ticket_type_id']); qty=max(0,min(10,int(item['qty'])))
                        if qty==0: continue
                        tt=c.execute('SELECT * FROM ticket_types WHERE id=?',(tid,)).fetchone()
                        if not tt: raise ValueError('ticket_type_not_found')
                        if event_id is None: event_id=tt['event_id']
                        if event_id != tt['event_id']: raise ValueError('mixed_events_not_allowed')
                        if tt['sold']+qty>tt['quantity']: raise ValueError('not_enough_inventory')
                        amount += tt['price']*qty; expanded.append((tt,qty))
                    if not expanded: raise ValueError('empty_cart')
                    ref='JGL-'+datetime.now().strftime('%y%m%d')+'-'+secrets.token_hex(3).upper()
                    status='paid' if method=='demo' else 'pending'
                    c.execute('INSERT INTO orders(reference,event_id,customer_name,customer_email,customer_phone,amount,payment_method,payment_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                              (ref,event_id,name,email,str(data.get('phone','')),amount,method,status,now()))
                    oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                    # Demo marks as paid to let the entire ticket/check-in flow be tested locally.
                    if status=='paid':
                        for tt,qty in expanded:
                            c.execute('UPDATE ticket_types SET sold=sold+? WHERE id=?',(qty,tt['id']))
                            for _ in range(qty):
                                token=secrets.token_urlsafe(24); h=hashlib.sha256(token.encode()).hexdigest(); pub='TKT-'+secrets.token_hex(5).upper()
                                c.execute('INSERT INTO tickets(public_id,order_id,ticket_type_id,attendee_name,token_hash,token,created_at) VALUES(?,?,?,?,?,?,?)',(pub,oid,tt['id'],name,h,token,now()))
                    return send_json(self,201,{'reference':ref,'amount':amount,'payment_status':status,'next':f'/ticket.html?order={ref}' if status=='paid' else None})
            if p == '/api/checkin':
                if not is_admin(self): return send_json(self,401,{'error':'unauthorized'})
                data=read_json(self); raw=str(data.get('token','')).strip()
                token=raw[7:] if raw.startswith('JANGIL:') else raw
                with db() as c:
                    t=c.execute('''SELECT tickets.*,ticket_types.name AS type_name,events.title AS event_title FROM tickets JOIN ticket_types ON ticket_types.id=tickets.ticket_type_id JOIN events ON events.id=ticket_types.event_id WHERE tickets.token=? OR tickets.public_id=?''',(token,token)).fetchone()
                    if not t: return send_json(self,404,{'valid':False,'status':'invalid'})
                    if t['checked_in_at']:
                        return send_json(self,409,{'valid':False,'status':'already_used','ticket':t['public_id'],'checked_in_at':t['checked_in_at'],'type':t['type_name'],'event':t['event_title']})
                    ts=now(); c.execute('UPDATE tickets SET checked_in_at=? WHERE id=?',(ts,t['id']))
                    return send_json(self,200,{'valid':True,'status':'checked_in','ticket':t['public_id'],'checked_in_at':ts,'type':t['type_name'],'event':t['event_title']})
            return send_json(self,404,{'error':'not_found'})
        except sqlite3.IntegrityError as e:
            return send_json(self,409,{'error':'conflict','detail':str(e)})
        except ValueError as e:
            return send_json(self,400,{'error':str(e)})
        except Exception as e:
            return send_json(self,500,{'error':'server_error','detail':str(e)})

    def log_message(self, fmt, *args):
        print('[Jangil]', fmt % args)

if __name__=='__main__':
    init_db()
    print(f'Jangil Tickets V2 → http://{HOST}:{PORT}')
    print(f'Environment: {ENV} | demo payment: {DEMO_PAYMENT}')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
