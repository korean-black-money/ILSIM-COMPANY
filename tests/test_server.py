import unittest, tempfile, threading, os, sys, json, urllib.request, urllib.error, http.cookiejar, shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class Client:
    def __init__(self,base):
        self.base=base;self.csrf='';self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        _,s=self.request('/api/session');self.csrf=s['csrf']
    def request(self,path,method='GET',data=None,csrf=True,origin=True):
        headers={}
        if method!='GET':
            headers={'Content-Type':'application/json'}
            if csrf:headers['X-CSRF-Token']=self.csrf
            if origin:headers['Origin']=self.base
        req=urllib.request.Request(self.base+path,headers=headers,method=method,data=json.dumps(data or {}).encode() if method!='GET' else None)
        try:
            with self.opener.open(req) as r:code,raw=r.status,r.read()
        except urllib.error.HTTPError as r:
            code,raw=r.code,r.read();r.close()
        try:body=json.loads(raw)
        except:body=raw
        return code,body
    def login(self):
        code,s=self.request('/api/login','POST',{'username':'ilsim','password':'test-only-password-123'})
        assert code==200,s
        self.csrf=s['csrf']

class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();server.RUNTIME=Path(cls.temp.name);server.DB=server.RUNTIME/'store.sqlite3'
        os.environ['ILSIM_ADMIN_PASSWORD']='test-only-password-123';server.init_db()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);cls.base='http://127.0.0.1:'+str(cls.http.server_address[1])
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
    def setUp(self):self.public=Client(self.base);self.admin=Client(self.base);self.admin.login()
    def test_01_public_catalog_reviews_and_private_files(self):
        code,c=self.public.request('/api/catalog');self.assertEqual(code,200);self.assertEqual(len(c['products']),3)
        _,r=self.public.request('/api/reviews');self.assertEqual(len(r),15);self.assertEqual(sum(x['rating'] for x in r),71)
        self.assertFalse(any('상품주문번호' in str(x) for x in r))
        for path in ['/server.py','/.runtime/admin-access.txt','/.git/config','/data/reviews.json','/images/../server.py']:
            self.assertEqual(self.public.request(path)[0],404,path)
    def test_02_auth_csrf_and_logout(self):
        for path in ['/api/admin/orders','/api/admin/inquiries','/api/admin/stats']:self.assertEqual(self.public.request(path)[0],401)
        self.assertEqual(self.public.request('/api/admin/settings','PUT',{'storeUrl':''})[0],401)
        self.assertEqual(self.admin.request('/api/admin/settings','PUT',{'storeUrl':''},csrf=False)[0],403)
        self.assertEqual(self.admin.request('/api/admin/settings','PUT',{'storeUrl':''},origin=False)[0],403)
        code,s=self.admin.request('/api/logout','POST',{});self.admin.csrf=s['csrf'];self.assertEqual(code,200)
        self.assertEqual(self.admin.request('/api/admin/orders')[0],401)
    def test_03_product_sync_validation(self):
        _,cat=self.public.request('/api/catalog');p=dict(cat['products'][0]);p['id']='test-product';p['name']='검증 상품';p['options']=[{'name':'새 옵션','price':25000}]
        self.assertEqual(self.admin.request('/api/admin/products','POST',p)[0],200)
        _,cat=Client(self.base).request('/api/catalog');self.assertEqual(next(x for x in cat['products'] if x['id']=='test-product')['options'][0]['price'],25000)
        p['storeUrl']='https://smartstore.naver.com.evil.test/';self.assertEqual(self.admin.request('/api/admin/products','POST',p)[0],400)
        p['storeUrl']='javascript:alert(1)';self.assertEqual(self.admin.request('/api/admin/products','POST',p)[0],400)
        p['storeUrl']='';p['options'][0]['price']=-100;self.assertEqual(self.admin.request('/api/admin/products','POST',p)[0],400)
        self.assertEqual(self.admin.request('/api/admin/products/test-product','DELETE',{})[0],200)
    def test_04_order_numeric_prices_idempotency_and_states(self):
        payload={'name':'검증용 고객','phone':'010-0000-0000','memo':'자동검증','consent':True,'requestKey':'test-order-key','items':[{'productId':'red-fig','option':'500g','qty':2,'unitPrice':16000}],'total':1}
        code,r=self.public.request('/api/orders','POST',payload);self.assertEqual(code,201,r)
        code,r2=self.public.request('/api/orders','POST',payload);self.assertEqual(code,200);self.assertEqual(r['id'],r2['id'])
        _,orders=self.admin.request('/api/admin/orders');o=next(x for x in orders if x['id']==r['id']);self.assertEqual(o['total'],32000);self.assertIsInstance(o['items'][0]['price'],int)
        self.admin.request('/api/admin/orders/'+r['id'],'PUT',{'status':'confirmed'});_,stats=self.admin.request('/api/admin/stats');self.assertGreaterEqual(stats['confirmed'],32000)
        self.admin.request('/api/admin/orders/'+r['id'],'PUT',{'status':'cancelled'});_,stats=self.admin.request('/api/admin/stats');self.assertEqual(stats['confirmed'],0)
        for price,qty in [(1,1),(16000,0),(16000,100),(16000,-1)]:
            bad={**payload,'requestKey':str((price,qty)),'items':[{'productId':'red-fig','option':'500g','qty':qty,'unitPrice':price}]}
            self.assertEqual(self.public.request('/api/orders','POST',bad)[0],400)
        self.assertEqual(self.public.request('/api/orders','POST',{**payload,'requestKey':'no-consent','consent':False})[0],400)
    def test_05_posts_visibility_comments(self):
        p={'id':'test-post','title':'검증 게시물','content':'<script>문자 그대로</script>','category':'공지','visible':False}
        self.assertEqual(self.admin.request('/api/admin/posts','POST',p)[0],200)
        _,posts=self.public.request('/api/posts');self.assertFalse(any(x['id']=='test-post' for x in posts))
        self.assertEqual(self.public.request('/api/posts/test-post/comments')[0],404)
        p['visible']=True;self.admin.request('/api/admin/posts','POST',p)
        self.assertEqual(self.public.request('/api/posts/test-post/comments','POST',{'author':'검증','content':'댓글','password':'abcd'})[0],200)
        _,comments=self.public.request('/api/posts/test-post/comments');self.assertEqual(len(comments),1);self.assertNotIn('password',comments[0])
        cid=comments[0]['id'];self.assertEqual(self.public.request('/api/comments/'+cid,'DELETE',{'password':'bad'})[0],400)
        self.assertEqual(self.public.request('/api/comments/'+cid,'DELETE',{'password':'abcd'})[0],200)
        self.public.request('/api/posts/test-post/like','POST',{});_,posts=self.public.request('/api/posts');self.assertEqual(next(p for p in posts if p['id']=='test-post')['likes'],1)
        self.public.request('/api/posts/test-post/like','POST',{});_,posts=self.public.request('/api/posts');self.assertEqual(next(p for p in posts if p['id']=='test-post')['likes'],0)
        self.admin.request('/api/admin/posts/test-post','DELETE',{})
    def test_06_inquiries_visits(self):
        code,r=self.public.request('/api/inquiries','POST',{'name':'검증','phone':'010-0000-0000','message':'문의 검증','category':'제품','consent':True,'requestKey':'inquiry-test'})
        self.assertEqual(code,201,r);_,rows=self.admin.request('/api/admin/inquiries');self.assertTrue(any(x['id']==r['id'] for x in rows))
        for _ in range(2):self.public.request('/api/visit','POST',{})
        _,stats=self.admin.request('/api/admin/stats');self.assertTrue(stats['visits'])

if __name__=='__main__':unittest.main(verbosity=2)
