import {test,before,after} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFile,readdir} from 'node:fs/promises';
import worker,{hashPassword} from '../_worker.js';
class D1 {
  constructor(){this.db=new DatabaseSync(':memory:');}
  prepare(sql){const db=this.db;let args=[];const q={bind(...values){args=values;return q;},async first(){return db.prepare(sql).get(...args)??null;},async all(){return {results:db.prepare(sql).all(...args)};},async run(){const r=db.prepare(sql).run(...args);return {meta:{changes:Number(r.changes)}};}};return q;}
  async batch(queries){this.db.exec('BEGIN');try{const result=[];for(const q of queries)result.push(await q.run());this.db.exec('COMMIT');return result;}catch(e){this.db.exec('ROLLBACK');throw e;}}
}
const origin='https://ilsim.example',db=new D1(),blobs=new Map();
const env={DB:db,ILSIM_ORIGIN:origin,BUCKET:{async put(k,v,m){blobs.set(k,{body:v,...m});},async get(k){return blobs.get(k);}},ASSETS:{async fetch(){return new Response('public asset');}}};
let sequence=0;
class Client{
  constructor(){this.ip='192.0.2.'+(++sequence);this.csrf='';this.cookie='';}
  async request(path,method='GET',data,options={}){
    const headers={'CF-Connecting-IP':this.ip};if(this.cookie)headers.Cookie=this.cookie;
    if(method!=='GET'&&method!=='HEAD'){headers['Content-Type']='application/json';headers.Origin=options.origin??origin;if(options.csrf!==false)headers['X-CSRF-Token']=this.csrf;}
    const r=await worker.fetch(new Request(origin+path,{method,headers,body:data===undefined?undefined:JSON.stringify(data)}),env);
    if(r.headers.has('Set-Cookie'))this.cookie=r.headers.get('Set-Cookie').split(';')[0];
    const raw=await r.text();let value;try{value=JSON.parse(raw);}catch{value=raw;}
    if(value?.csrf)this.csrf=value.csrf;return {status:r.status,value,headers:r.headers};
  }
  async init(){await this.request('/api/session');return this;}
  async login(){const r=await this.request('/api/login','POST',{username:'ilsim',password:'test-only-password-123'});assert.equal(r.status,200,JSON.stringify(r.value));return this;}
}
before(async()=>{for(const file of (await readdir('cloudflare/migrations')).filter(f=>f.endsWith('.sql')).sort())db.db.exec(await readFile('cloudflare/migrations/'+file,'utf8'));env.ILSIM_ADMIN_HASH=await hashPassword('test-only-password-123');});
after(()=>db.db.close());
test('Public catalog, actual reviews, Naver original bytes and private files',async()=>{
  const c=await new Client().init();const cat=await c.request('/api/catalog');assert.equal(cat.status,200);assert.equal(cat.value.products.length,3);
  const r=await c.request('/api/reviews');assert.equal(r.value.length,15);assert.equal(r.value.reduce((n,x)=>n+x.rating,0),71);
  const name='naveraa12cb19ff5b0b79be28136fa909732b.html';const v=await c.request('/'+name);assert.equal(v.status,200);assert.equal(v.value,await readFile(name,'utf8'));assert.match(v.headers.get('Content-Type'),/text\/html/);
  for(const path of ['/server.py','/.runtime/admin-access.txt','/.openai/hosting.json','/data/catalog.json','/worker/index.js'])assert.equal((await c.request(path)).status,404,path);
  assert.match((await c.request('/robots.txt')).value,/Sitemap: https:\/\/ilsim.example\/sitemap.xml/);
});
test('Server admin auth, Secure HttpOnly session, CSRF, origin and logout',async()=>{
  const c=await new Client().init();assert.equal((await c.request('/api/admin/orders')).status,401);
  assert.equal((await c.request('/api/admin/settings','PUT',{storeUrl:''})).status,401);
  await c.login();const session=await c.request('/api/session');assert.equal(session.value.admin,true);
  assert.equal((await c.request('/api/admin/settings','PUT',{storeUrl:''},{csrf:false})).status,403);
  assert.equal((await c.request('/api/admin/settings','PUT',{storeUrl:''},{origin:'https://evil.test'})).status,403);
  const logout=await c.request('/api/logout','POST',{});assert.match(logout.headers.get('Set-Cookie'),/HttpOnly.*Secure/);assert.equal((await c.request('/api/admin/orders')).status,401);
});
test('Persistent products synchronize across visitors; invalid price/link rejected',async()=>{
  const admin=await (await new Client().init()).login(),customer=await new Client().init();
  const p={...(await customer.request('/api/catalog')).value.products[0],id:'test-product',options:[{name:'새 옵션',price:25000}]};
  assert.equal((await admin.request('/api/admin/products','POST',p)).status,200);
  assert.equal((await customer.request('/api/catalog')).value.products.find(x=>x.id===p.id).options[0].price,25000);
  for(const storeUrl of ['https://smartstore.naver.com.evil.test','javascript:alert(1)'])assert.equal((await admin.request('/api/admin/products','POST',{...p,storeUrl})).status,400);
  assert.equal((await admin.request('/api/admin/products','POST',{...p,options:[{name:'x',price:-1}]})).status,400);
  await admin.request('/api/admin/products/test-product','DELETE',{});
});
test('Order price recalculation, idempotency, cancellation and invalid quantities',async()=>{
  const c=await new Client().init(),admin=await (await new Client().init()).login();
  const data={name:'테스트',phone:'010-0000-0000',memo:'검증',consent:true,requestKey:'unique-key',items:[{productId:'red-fig',option:'500g',qty:2,unitPrice:16000}],total:1};
  const r=await c.request('/api/orders','POST',data);assert.equal(r.status,201,JSON.stringify(r.value));assert.equal((await c.request('/api/orders','POST',data)).value.id,r.value.id);
  const orders=(await admin.request('/api/admin/orders')).value;assert.equal(orders.find(o=>o.id===r.value.id).total,32000);
  await admin.request('/api/admin/orders/'+r.value.id,'PUT',{status:'confirmed'});assert.equal((await admin.request('/api/admin/stats')).value.confirmed,32000);
  await admin.request('/api/admin/orders/'+r.value.id,'PUT',{status:'cancelled'});assert.equal((await admin.request('/api/admin/stats')).value.confirmed,0);
  for(const [unitPrice,qty] of [[1,1],[16000,0],[16000,100],[16000,-1],[16000,1.5]])assert.equal((await c.request('/api/orders','POST',{...data,requestKey:JSON.stringify([unitPrice,qty]),items:[{...data.items[0],unitPrice,qty}]})).status,400);
  await admin.request('/api/admin/orders/'+r.value.id,'DELETE',{});
});
test('Hidden posts, comments password, likes and public visibility',async()=>{
  const c=await new Client().init(),a=await (await new Client().init()).login();const post={id:'test-post',title:'검증',category:'공지',content:'<script>글</script>',visible:false};
  await a.request('/api/admin/posts','POST',post);assert.equal((await c.request('/api/posts')).value.some(p=>p.id===post.id),false);assert.equal((await c.request('/api/posts/test-post/comments')).status,404);
  await a.request('/api/admin/posts','POST',{...post,visible:true});assert.equal((await c.request('/api/posts/test-post/comments','POST',{author:'검증',content:'댓글',password:'abcd'})).status,200);
  const comment=(await c.request('/api/posts/test-post/comments')).value[0];assert.ok(!('password' in comment));
  assert.equal((await c.request('/api/comments/'+comment.id,'DELETE',{password:'bad'})).status,400);assert.equal((await c.request('/api/comments/'+comment.id,'DELETE',{password:'abcd'})).status,200);
  await c.request('/api/posts/test-post/like','POST',{});assert.equal((await c.request('/api/posts')).value.find(p=>p.id==='test-post').likes,1);
  await c.request('/api/posts/test-post/like','POST',{});assert.equal((await c.request('/api/posts')).value.find(p=>p.id==='test-post').likes,0);await a.request('/api/admin/posts/test-post','DELETE',{});
});
test('Inquiries persist, visit counts deduplicate and image uploads persist',async()=>{
  const c=await new Client().init(),a=await (await new Client().init()).login();const data={name:'검증',phone:'010-0000-0000',message:'문의',category:'제품',consent:true,requestKey:'q-test'};
  const r=await c.request('/api/inquiries','POST',data);assert.equal(r.status,201);assert.equal((await c.request('/api/inquiries','POST',data)).value.id,r.value.id);assert.ok((await a.request('/api/admin/inquiries')).value.some(q=>q.id===r.value.id));
  await c.request('/api/visit','POST',{});await c.request('/api/visit','POST',{});assert.equal((await a.request('/api/admin/stats')).value.visits[0].visitors,1);
  const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';const upload=await a.request('/api/admin/upload','POST',{data:png});assert.equal(upload.status,200);assert.equal((await c.request('/'+upload.value.url)).status,200);
  assert.equal((await a.request('/api/admin/upload','POST',{data:btoa('<svg></svg>')})).status,400);await a.request('/api/admin/inquiries/'+r.value.id,'DELETE',{});
});
test('Login throttling persists in database across new sessions',async()=>{
  const c=await new Client().init();for(let i=0;i<8;i++)assert.equal((await c.request('/api/login','POST',{username:'ilsim',password:'wrong'})).status,401);
  assert.equal((await c.request('/api/login','POST',{username:'ilsim',password:'test-only-password-123'})).status,429);
});
