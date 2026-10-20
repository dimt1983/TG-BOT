import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import tma_static_server as server


class ShopIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'shop.db'
        con=sqlite3.connect(self.db)
        con.executescript('''
        CREATE TABLE users(user_id INTEGER PRIMARY KEY,name TEXT,phone TEXT,user_type TEXT,price_tier TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER,phone TEXT,total REAL,status TEXT,created_at TEXT,payment_method TEXT,paid_at TEXT);
        CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,product_name TEXT,fasovka TEXT,quantity INTEGER,price REAL,tma_id TEXT);
        CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT);
        INSERT INTO users VALUES(1001,'A','79990000000','individual','standard');
        INSERT INTO users VALUES(1002,'B','79990000000','individual','standard');
        INSERT INTO orders VALUES(1,1001,'79990000000',100,'paid','now','online','now');
        INSERT INTO orders VALUES(2,1002,'79990000000',200,'paid','now','online','now');
        INSERT INTO orders VALUES(3,-555,'79990000000',300,'paid','now','online','now');
        INSERT INTO order_items VALUES(1,1,NULL,'Own','1kg',1,100,'own-sku');
        INSERT INTO order_items VALUES(2,2,NULL,'Foreign','1kg',1,200,'foreign-sku');
        ''');con.commit();con.close()
        self.env=patch.dict(os.environ,{'JWT_SECRET':'synthetic-shop-secret','API_ORDERS_TOKEN':'synthetic-integrations','SHOP_INTERNAL_TOKEN':'synthetic-panel','OWNER_TG_ID':'9001'});self.env.start()
        self.ctx=patch.object(server,'_TMA_CTX',{'bot_token':'synthetic-bot'});self.ctx.start()
        self.dbpatch=patch.object(server,'DB_PATH',str(self.db));self.dbpatch.start()
        self.adminpatch=patch.object(server,'ADMIN_IDS',{1001});self.adminpatch.start()
        self.homepatch=None
        if hasattr(server,'shop_home_access'):
            self.homepatch=patch.object(server.shop_home_access,'allowed',return_value=True);self.allowed=self.homepatch.start()
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()
        if self.homepatch:self.homepatch.stop()
        self.adminpatch.stop();self.dbpatch.stop();self.ctx.stop();self.env.stop();self.tmp.cleanup()
    def auth(self,uid=1001,bound=True,**extra):
        return 'Bearer '+server.jwt_sign({'id':uid,**({'home_entitlement':True,'aud':'rb_home_shop'} if bound else {}),**extra})
    def request(self,path,method='GET',body=None,auth=None):
        c=http.client.HTTPConnection(*self.http.server_address,timeout=2);headers={'Authorization':auth or self.auth()}
        if body is not None:headers['Content-Type']='application/json'
        c.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        r=c.getresponse();data=r.read();result=(r.status,dict(r.getheaders()),json.loads(data) if data and data[:1] in [b'{',b'['] else data.decode());c.close();return result
    def test_home_orders_exclude_phone_collision_legacy_preserved(self):
        status,_,data=self.request('/tma/api/my_orders');self.assertEqual(status,200);self.assertEqual([x['id'] for x in data['orders']],[1])
        _,_,data=self.request('/tma/api/my_orders',auth=self.auth(bound=False));self.assertEqual({x['id'] for x in data['orders']},{1,2,3})
    def test_home_purchased_products_do_not_union_phone(self):
        status,_,data=self.request('/tma/api/my_products');self.assertEqual(status,200);self.assertEqual(data['tma_ids'],['own-sku'])
    def test_phone_collision_cannot_authorize_payment(self):
        status,_,_=self.request('/tma/api/order/2/pay','POST',{});self.assertIn(status,{403,404})
    def test_admin_ids_do_not_grant_home_employee_chat_access(self):
        status,_,_=self.request('/tma/api/chat/2');self.assertIn(status,{403,404})
    def test_home_admin_check_is_false_for_native_admin_list_only(self):
        _,_,data=self.request('/tma/api/admin/check');self.assertIs(data['is_admin'],False)
    def test_home_revocation_every_request(self):
        if not self.homepatch:self.fail('strict checker hook missing')
        self.allowed.side_effect=[True,False]
        self.assertEqual(self.request('/tma/api/my_orders')[0],200)
        self.assertEqual(self.request('/tma/api/my_orders')[0],403)
    def test_existing_user_only_home_issuer(self):
        status,_,data=self.request('/tma/api/auth/home_token','POST',{'tg_id':9999},auth='Bearer synthetic-panel');self.assertEqual(status,403);self.assertEqual(data.get('code'),'unlinked')

    def test_verified_principal_header_and_no_store(self):
        status,headers,_=self.request('/tma/api/user/me');self.assertEqual(status,200);self.assertEqual(headers.get('X-Home-Principal'),'1001');self.assertIn('no-store',headers.get('Cache-Control',''))
    def test_unknown_claim_and_native_admin_mutation_are_blocked(self):
        for path in ['/tma/api/claim','/tma/api/admin/dm/1002']:
            self.assertEqual(self.request(path,'POST',{'token':'synthetic','home_owner':True})[0],403)
    def test_issuer_validates_identity_and_never_creates_user(self):
        for uid in [True,2**64,'1001']:
            self.assertEqual(self.request('/tma/api/auth/home_token','POST',{'tg_id':uid},auth='Bearer synthetic-panel')[0],400)
        self.assertEqual(self.request('/tma/api/auth/home_token','POST',{'tg_id':1001},auth='Bearer wrong')[0],403)
        status,_,data=self.request('/tma/api/auth/home_token','POST',{'tg_id':1001,'role':'boss'},auth='Bearer synthetic-panel');self.assertEqual(status,200)
        claims=server.jwt_verify(data['token']);self.assertIs(claims['home_entitlement'],True);self.assertEqual(claims['aud'],'rb_home_shop');self.assertNotIn('role',claims)
        self.allowed.return_value=False
        self.assertEqual(self.request('/tma/api/my_orders',auth='Bearer '+data['token'])[0],403)
        with sqlite3.connect(self.db) as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM users').fetchone()[0],2)
    def test_wrong_audience_or_boolean_flag_denied(self):
        for changes in [{'aud':'other'},{'home_entitlement':False},{'home_entitlement':'true'},{'id':True}]:
            self.assertEqual(self.request('/tma/api/my_orders',auth=self.auth(**changes))[0],403)
    def test_missing_owner_config_never_uses_native_admin_list(self):
        with patch.dict(os.environ,{'OWNER_TG_ID':''}):
            self.assertIs(self.request('/tma/api/admin/check')[2]['is_admin'],False)
    def test_request_body_cannot_control_trusted_create_policy(self):
        import asyncio,types,sys
        loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start();captured=[]
        async def process(*args,**kw):captured.append(kw.get('strict_identity'));return {'order_id':99}
        try:
            server._TMA_CTX.update(loop=loop,get_db=lambda:None,bot=None,notify_new_order=None)
            with patch.dict(sys.modules,{'tma_handler':types.SimpleNamespace(process_tma_order=process)}):
                for bound in [True,False]:
                    result=self.request('/tma/api/order','POST',{'type':'order','items':[],'strict_identity':not bound,'home_entitlement':not bound},auth=self.auth(bound=bound))
                    self.assertEqual(result[0],200,result)
            self.assertEqual(captured,[True,False])
        finally:loop.call_soon_threadsafe(loop.stop);thread.join();loop.close()

    def test_profile_legacy_and_home_both_work_with_trusted_header_only_home(self):
        for bound in [True,False]:
            status,headers,data=self.request('/tma/api/user/me',auth=self.auth(bound=bound));self.assertEqual(status,200);self.assertEqual(data['name'],'A');self.assertEqual(headers.get('X-Home-Principal'),'1001' if bound else None)
    def test_owner_exact_config_only_and_membership_unmodified(self):
        with patch.dict(os.environ,{'OWNER_TG_ID':'1002'}):
            self.assertIs(self.request('/tma/api/admin/check',auth=self.auth(1002))[2]['is_admin'],True)
            self.assertIs(self.request('/tma/api/admin/check',auth=self.auth(1001,role='boss',home_owner=True))[2]['is_admin'],False)
        self.assertEqual(server.ADMIN_IDS,{1001})
    def test_home_issuer_transport_errors_deny_and_fallback_secret_cannot_sign(self):
        self.allowed.side_effect=RuntimeError('synthetic unavailable')
        self.assertEqual(self.request('/tma/api/auth/home_token','POST',{'tg_id':1001},auth='Bearer synthetic-panel')[0],403)
        self.allowed.side_effect=None;self.allowed.return_value=True
        with patch.dict(os.environ,{'JWT_SECRET':''}),patch.object(server,'_TMA_CTX',None):
            self.assertEqual(self.request('/tma/api/auth/home_token','POST',{'tg_id':1001},auth='Bearer synthetic-panel')[0],403)
    def test_home_auth_has_priority_over_injected_telegram_identity(self):
        with patch.object(server,'verify_tma_init_data',return_value={'id':1002}):
            _,headers,data=self.request('/tma/api/user/me');self.assertEqual(data['name'],'A');self.assertEqual(headers['X-Home-Principal'],'1001')

class OrderCreationIdentityTest(unittest.TestCase):
    def test_home_creation_does_not_absorb_phone_stub_and_legacy_preserves_merge(self):
        import ast,copy
        from types import SimpleNamespace
        from unittest.mock import Mock
        path=Path(__file__).resolve().parents[2]/'tma_handler.py';tree=ast.parse(path.read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_create_order_from_tma')
        code=compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),'exec')
        for strict in [True,False]:
            con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
            con.executescript('''CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER,name TEXT,phone TEXT,address TEXT,total REAL,discount REAL,status TEXT,payment_method TEXT);
            CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,quantity INTEGER,price REAL,tma_id TEXT,roast_choice TEXT);
            INSERT INTO orders(id,user_id,phone) VALUES(1,-555,'79990000000');''')
            def merge(db,uid,phone):db.execute('UPDATE orders SET user_id=? WHERE id=1',(uid,))
            absorb=Mock(side_effect=merge)
            g={'_normalize_personal_data_consent':lambda p:None,'_ensure_order_payment_columns':lambda c:None,'_absorb_phone_stub':absorb,'_apply_personal_pricing':lambda*c:None,'_compute_order_discount':lambda*c:(100,0,{}),'_ensure_tma_id_column':lambda c:None,'_upsert_user_profile':lambda*c:None,'log':SimpleNamespace(warning=lambda*c:None)}
            exec(code,g)
            payload={'items':[{'id':'synthetic','name':'Synthetic','price':100,'qty':1,'_rbo_base':True,'_rbo_db_id':1}], 'contact':{'phone':'79990000000'},'strict_identity':not strict,'home_entitlement':not strict}
            if not any(a.arg=='strict_identity' for a in node.args.args+node.args.kwonlyargs):
                order_id=g['_create_order_from_tma'](con,1001,payload)
            else:order_id=g['_create_order_from_tma'](con,1001,payload,strict_identity=strict)
            self.assertGreater(order_id,1)
            self.assertEqual(con.execute('SELECT user_id FROM orders WHERE id=1').fetchone()[0],-555 if strict else 1001)
            self.assertEqual(absorb.call_count,0 if strict else 1)


class CheckerTransportTest(unittest.TestCase):
    def test_redirect_unknown_decision_and_missing_config_deny_without_following(self):
        import shop_home_access
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        seen=[]
        class Checker(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append(payload)
                self.send_response(302);self.send_header('Location','/allow');self.end_headers()
            def do_GET(self):
                seen.append('FOLLOWED');self.send_response(200);self.end_headers();self.wfile.write(b'{"decision":"allow"}')
        http=ThreadingHTTPServer(('127.0.0.1',0),Checker);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict(os.environ,{'HOME_ACCESS_URL':f'http://127.0.0.1:{http.server_port}/check','HOME_ACCESS_INTERNAL_TOKEN':'synthetic-internal'}):
                self.assertFalse(shop_home_access.allowed(1001))
            self.assertEqual(seen,[{'telegram_id':1001,'app_key':'shop','require_entitlement':True}])
            with patch.dict(os.environ,{'HOME_ACCESS_INTERNAL_TOKEN':''}):self.assertFalse(shop_home_access.allowed(1001))
        finally:http.shutdown();http.server_close();thread.join()
