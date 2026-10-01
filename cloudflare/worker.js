import catalog from '../data/catalog.json';
import initialPosts from '../data/posts.json';
import reviews from '../data/reviews.json';
import verification from '../naveraa12cb19ff5b0b79be28136fa909732b.html';

const TTL=8*3600, enc=new TextEncoder(), ready=new WeakSet();
let lastCleanup=0;
const STATES=['pending','processing','confirmed','completed','cancelled'];
const security={'X-Content-Type-Options':'nosniff','Referrer-Policy':'strict-origin-when-cross-origin','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https://*.pstatic.net; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"};
class UserError extends Error { constructor(message,status=400){super(message);this.status=status;} }
const fail=(message,status=400)=>{throw new UserError(message,status);};
const now=()=>new Date().toISOString();
const seconds=()=>Math.floor(Date.now()/1000);
const hex=bytes=>Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
const random=(n=32)=>hex(crypto.getRandomValues(new Uint8Array(n)));
const digest=async value=>hex(new Uint8Array(await crypto.subtle.digest('SHA-256',enc.encode(value))));
function equal(a,b){if(typeof a!=='string'||typeof b!=='string'||a.length!==b.length)return false;let v=0;for(let i=0;i<a.length;i++)v|=a.charCodeAt(i)^b.charCodeAt(i);return v===0;}
export async function hashPassword(password,salt=random(16)){
  const key=await crypto.subtle.importKey('raw',enc.encode(password),'PBKDF2',false,['deriveBits']);
  const bits=await crypto.subtle.deriveBits({name:'PBKDF2',hash:'SHA-256',salt:enc.encode(salt),iterations:100000},key,256);
  return `${salt}:${hex(new Uint8Array(bits))}`;
}
async function verifyPassword(password,encoded){return typeof encoded==='string'&&equal(await hashPassword(password,encoded.split(':')[0]),encoded);}
function text(value,limit=500,required=false){if(typeof value!=='string'||value.length>limit||(required&&!value.trim()))fail('입력 내용을 확인해 주세요.');return value.trim();}
function phone(v){v=text(v,30,true);if(!/^[0-9+() -]{8,30}$/.test(v))fail('연락 가능한 전화번호를 입력해 주세요.');return v;}
function storeURL(v){v=text(v,500);if(v){let u;try{u=new URL(v);}catch{fail('네이버 스마트스토어 https 주소를 입력해 주세요.');}if(u.protocol!=='https:'||!['smartstore.naver.com','brand.naver.com'].includes(u.hostname)||u.username||u.password||u.port)fail('네이버 스마트스토어 https 주소를 입력해 주세요.');}return v;}
function imageURL(v){v=text(v,500);if(!/^images\/[a-zA-Z0-9_./-]+\.(webp|png|jpe?g)$/.test(v)||v.includes('..'))fail('업로드한 상품 이미지를 선택해 주세요.');return v;}
function productData(data,id){
  if(!Array.isArray(data.options)||data.options.length<1||data.options.length>20)fail('옵션을 1~20개 입력해 주세요.');
  const options=data.options.map(o=>{if(!o||!Number.isInteger(o.price)||o.price<=0||o.price>100000000)fail('가격은 0보다 큰 정수로 입력해 주세요.');return {name:text(o.name,80,true),price:o.price};});
  if(new Set(options.map(o=>o.name)).size!==options.length)fail('옵션명이 중복되었습니다.');
  if(!['fig','gecko','other'].includes(data.category)||!['available','soldout','preparing','inquiry'].includes(data.stock))fail('상품 분류와 판매 상태를 확인해 주세요.');
  if(!['','red-fig','green-fig'].includes(data.details??''))fail('잘못된 상세 이미지입니다.');
  return {id,name:text(data.name,100,true),category:data.category,stock:data.stock,options,image:imageURL(data.image),storeUrl:storeURL(data.storeUrl??''),details:data.details??'',...Object.fromEntries(['tag','subtitle','description','shipping','priceNote'].map(k=>[k,text(data[k]??'',2000)]))};
}
const stmt=(db,sql,args=[])=>db.prepare(sql).bind(...args);
const first=(db,sql,args)=>stmt(db,sql,args).first();
const all=async(db,sql,args)=>(await stmt(db,sql,args).all()).results;
const run=(db,sql,args)=>stmt(db,sql,args).run();
async function initialize(db){
  if(ready.has(db))return;
  if(!await first(db,"SELECT value FROM config WHERE key='initialized'")){
    // Conditions are evaluated inside one atomic batch; deleted products stay deleted.
    const statements=catalog.products.map(p=>stmt(db,"INSERT OR IGNORE INTO products SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM config WHERE key='initialized')",[p.id,JSON.stringify(p)]));
    for(const p of initialPosts)statements.push(stmt(db,"INSERT OR IGNORE INTO posts SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM config WHERE key='initialized')",[p.id,JSON.stringify(p)]));
    statements.push(stmt(db,"INSERT OR IGNORE INTO config VALUES ('storeUrl',?)",[catalog.storeUrl]),stmt(db,"INSERT OR IGNORE INTO config VALUES ('initialized','1')"));
    await db.batch(statements);
  }
  ready.add(db);
}
async function cleanup(db){
  if(Date.now()-lastCleanup<3600000)return;
  const cutoff=new Date(Date.now()-90*86400000).toISOString();
  await db.batch([stmt(db,'DELETE FROM orders WHERE created < ?',[cutoff]),stmt(db,'DELETE FROM inquiries WHERE created < ?',[cutoff]),stmt(db,'DELETE FROM visits WHERE day < ?',[cutoff.slice(0,10)]),stmt(db,'DELETE FROM sessions WHERE expires < ?',[seconds()]),stmt(db,'DELETE FROM limits WHERE expires < ?',[seconds()])]);
  lastCleanup=Date.now();
}
function json(data,status=200,cookie){const h={...security,'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'};if(cookie)h['Set-Cookie']=cookie;return new Response(JSON.stringify(data),{status,headers:h});}
async function session(db,request){const token=request.headers.get('Cookie')?.split(';').map(v=>v.trim()).find(v=>v.startsWith('ilsim_session='))?.slice(14);if(!token||token.length>200)return null;return first(db,'SELECT * FROM sessions WHERE token=? AND expires>?',[await digest(token),seconds()]);}
async function newSession(db,secure,admin=false){const token=random(),csrf=random();await run(db,'INSERT INTO sessions VALUES (?,?,?,?)',[await digest(token),csrf,admin?1:0,seconds()+TTL]);return {csrf,cookie:`ilsim_session=${token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${TTL}${secure?'; Secure':''}`};}
async function rate(db,key,max,window){
  const t=seconds();const row=await first(db,'INSERT INTO limits (key,count,expires) VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN expires<=? THEN 1 ELSE count+1 END, expires=CASE WHEN expires<=? THEN excluded.expires ELSE expires END RETURNING count',[key,t+window,t,t]);
  if(row.count>max)fail('요청이 많습니다. 잠시 후 다시 시도해 주세요.',429);
}
async function body(request){
  if(!request.headers.get('Content-Type')?.includes('application/json'))fail('잘못된 요청 형식입니다.');
  const reader=request.body?.getReader();if(!reader)fail('입력 내용을 확인해 주세요.');
  const chunks=[];let length=0;
  while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>7500000){await reader.cancel();fail('요청 크기를 확인해 주세요.');}chunks.push(value);}
  const joined=new Uint8Array(length);let offset=0;for(const c of chunks){joined.set(c,offset);offset+=c.length;}
  let data;try{data=JSON.parse(new TextDecoder().decode(joined));}catch{fail('입력 내용을 확인해 주세요.');}
  if(!data||typeof data!=='object'||Array.isArray(data))fail('입력 내용을 확인해 주세요.');return data;
}
async function orderData(db,data){
  if(!Array.isArray(data.items)||data.items.length<1||data.items.length>30)fail('선택한 상품을 확인해 주세요.');
  const items=[];let total=0;
  for(const item of data.items){
    if(!item)fail('선택한 상품을 확인해 주세요.');
    const row=await first(db,'SELECT body FROM products WHERE id=?',[text(item.productId,100,true)]);
    if(!row)fail('판매가 종료된 상품입니다. 장바구니를 확인해 주세요.');
    const p=JSON.parse(row.body),o=p.options.find(o=>o.name===item.option);
    if(['soldout','preparing'].includes(p.stock))fail('품절 또는 준비 중인 상품이 포함되어 있습니다.');
    if(!o||!Number.isInteger(item.qty)||item.qty<1||item.qty>99)fail('옵션과 수량을 다시 확인해 주세요.');
    if(item.unitPrice!==o.price)fail('상품 가격이 변경되었습니다. 새로고침 후 다시 확인해 주세요.');
    items.push({productId:p.id,product:p.name,option:o.name,price:o.price,qty:item.qty});total+=o.price*item.qty;
  }
  return {items,total,name:text(data.name,80,true),phone:phone(data.phone),memo:text(data.memo??'',2000)};
}
async function api(request,env,url){
  const db=env.DB;if(!db)fail('서비스 연결을 준비 중입니다. 잠시 후 다시 시도해 주세요.',503);
  await initialize(db);await cleanup(db);
  const s=await session(db,request),path=url.pathname,method=request.method;
  const ip=await digest(request.headers.get('CF-Connecting-IP')||'unknown');
  const secure=url.protocol==='https:';
  if(method==='GET'){
    if(path==='/api/session'){
      if(s)return json({csrf:s.csrf,admin:!!s.admin});
      await rate(db,'session:'+ip,120,60);const n=await newSession(db,secure);return json({csrf:n.csrf,admin:false},200,n.cookie);
    }
    if(path==='/api/catalog')return json({products:(await all(db,'SELECT body FROM products ORDER BY rowid')).map(r=>JSON.parse(r.body)),storeUrl:(await first(db,"SELECT value FROM config WHERE key='storeUrl'")).value});
    if(path==='/api/reviews')return json(reviews);
    if(path==='/api/posts'){
      const rows=await all(db,'SELECT p.body,(SELECT count(*) FROM likes l WHERE l.post_id=p.id) AS likes,(SELECT count(*) FROM comments c WHERE c.post_id=p.id) AS comments,EXISTS(SELECT 1 FROM likes l WHERE l.post_id=p.id AND l.visitor=?) AS liked FROM posts p',[s?.token??'']);
      return json(rows.map(r=>({...JSON.parse(r.body),likes:r.likes,comments:r.comments,liked:!!r.liked})).filter(p=>p.visible||s?.admin));
    }
    if(/^\/api\/posts\/[^/]+\/comments$/.test(path)){
      const id=path.split('/')[3],p=await first(db,'SELECT body FROM posts WHERE id=?',[id]);if(!p||(!JSON.parse(p.body).visible&&!s?.admin))fail('게시물이 없습니다.',404);
      return json(await all(db,'SELECT id,author,content,created FROM comments WHERE post_id=? ORDER BY created',[id]));
    }
    if(path.startsWith('/api/admin/')){
      if(!s?.admin)fail('관리자 로그인이 필요합니다.',401);
      if(['/api/admin/orders','/api/admin/inquiries'].includes(path))return json((await all(db,`SELECT * FROM ${path.endsWith('orders')?'orders':'inquiries'} ORDER BY created DESC`)).map(r=>({id:r.id,created:r.created,status:r.status,...JSON.parse(r.body)})));
      if(path==='/api/admin/stats'){
        const orders=await all(db,'SELECT status,body FROM orders');
        return json({orders:orders.length,pending:orders.filter(o=>o.status==='pending').length,confirmed:orders.filter(o=>['confirmed','completed'].includes(o.status)).reduce((n,o)=>n+JSON.parse(o.body).total,0),inquiries:(await first(db,"SELECT count(*) AS n FROM inquiries WHERE status='pending'")).n,visits:await all(db,'SELECT day,count(*) AS visitors FROM visits GROUP BY day ORDER BY day DESC LIMIT 14')});
      }
    }
    fail('찾을 수 없습니다.',404);
  }
  if(!['POST','PUT','DELETE'].includes(method))fail('지원하지 않는 요청입니다.',405);
  const expected=env.ILSIM_ORIGIN||url.origin;
  if(request.headers.get('Origin')!==expected||!s||!equal(request.headers.get('X-CSRF-Token'),s.csrf))fail('세션이 만료되었습니다. 새로고침 후 다시 시도해 주세요.',403);
  if(path.startsWith('/api/admin/')&&!s.admin)fail('관리자 로그인이 필요합니다.',401);
  if(!s.admin&&!['/api/login','/api/visit','/api/logout'].includes(path))await rate(db,'write:'+ip,30,60);
  const data=await body(request);
  if(path==='/api/login'&&method==='POST'){
    await rate(db,'login:'+ip,8,900);
    if(!env.ILSIM_ADMIN_HASH)fail('관리자 접속 설정을 확인해 주세요.',503);
    if(data.username!=='ilsim'||!await verifyPassword(text(data.password,200),env.ILSIM_ADMIN_HASH))fail('아이디 또는 비밀번호가 올바르지 않습니다.',401);
    await db.batch([stmt(db,'DELETE FROM sessions WHERE token=?',[s.token]),stmt(db,'DELETE FROM limits WHERE key=?',['login:'+ip])]);
    const n=await newSession(db,secure,true);return json({csrf:n.csrf,admin:true},200,n.cookie);
  }
  if(path==='/api/logout'&&method==='POST'){await run(db,'DELETE FROM sessions WHERE token=?',[s.token]);const n=await newSession(db,secure);return json({csrf:n.csrf,admin:false},200,n.cookie);}
  if(path==='/api/visit'&&method==='POST'){await run(db,'INSERT OR IGNORE INTO visits VALUES (?,?)',[new Date(Date.now()+9*3600000).toISOString().slice(0,10),s.token]);return json({ok:true});}
  if(['/api/orders','/api/inquiries'].includes(path)&&method==='POST'){
    if(data.consent!==true)fail('개인정보 수집·이용에 동의해 주세요.');
    const table=path.endsWith('orders')?'orders':'inquiries';
    // Scope idempotency to the session; a guessed key cannot reveal another customer's reference.
    const key=await digest(s.token+':'+text(data.requestKey,100,true));
    const old=await first(db,`SELECT id FROM ${table} WHERE request_key=?`,[key]);if(old)return json({id:old.id});
    const record=table==='orders'?await orderData(db,data):{name:text(data.name,80,true),phone:phone(data.phone),category:text(data.category,80,true),message:text(data.message,5000,true)};
    const id=(table==='orders'?'IL-':'IQ-')+random(6).toUpperCase();
    const result=await run(db,`INSERT OR IGNORE INTO ${table} (id,created,status,body,request_key) VALUES (?,?,?,?,?)`,[id,now(),'pending',JSON.stringify(record),key]);
    const stored=await first(db,`SELECT id FROM ${table} WHERE request_key=?`,[key]);return json({id:stored.id},result.meta.changes?201:200);
  }
  if(/^\/api\/posts\/[^/]+\/(comments|like)$/.test(path)&&method==='POST'){
    const id=path.split('/')[3],p=await first(db,'SELECT body FROM posts WHERE id=?',[id]);if(!p||!JSON.parse(p.body).visible)fail('게시물이 없습니다.');
    if(path.endsWith('/like')){
      const found=await first(db,'SELECT 1 FROM likes WHERE post_id=? AND visitor=?',[id,s.token]);
      if(found)await run(db,'DELETE FROM likes WHERE post_id=? AND visitor=?',[id,s.token]);else await run(db,'INSERT OR IGNORE INTO likes VALUES (?,?)',[id,s.token]);
    }else{
      const password=text(data.password,100,true);if(password.length<4)fail('삭제 비밀번호는 4자 이상 입력해 주세요.');
      await run(db,'INSERT INTO comments VALUES (?,?,?,?,?,?)',[random(12),id,text(data.author,40,true),text(data.content,2000,true),now(),await hashPassword(password)]);
    }
    return json({ok:true});
  }
  if(/^\/api\/comments\/[a-z0-9]+$/.test(path)&&method==='DELETE'){
    const c=await first(db,'SELECT * FROM comments WHERE id=?',[path.split('/').at(-1)]);
    if(!c||(!s.admin&&!await verifyPassword(text(data.password,100),c.password)))fail('비밀번호가 일치하지 않습니다.');
    await run(db,'DELETE FROM comments WHERE id=?',[c.id]);return json({ok:true});
  }
  if(path==='/api/admin/settings'&&method==='PUT'){await run(db,"UPDATE config SET value=? WHERE key='storeUrl'",[storeURL(data.storeUrl??'')]);return json({ok:true});}
  if(path==='/api/admin/upload'&&method==='POST'){
    let raw;try{raw=Uint8Array.from(atob(text(data.data,7000000)),c=>c.charCodeAt(0));}catch{fail('이미지 파일을 확인해 주세요.');}
    if(raw.length>5000000)fail('이미지는 5MB 이하로 올려 주세요.');
    const start=Array.from(raw.slice(0,8)).join(','),str=(a,b)=>String.fromCharCode(...raw.slice(a,b));
    const ext=start==='137,80,78,71,13,10,26,10'?'png':raw[0]===255&&raw[1]===216&&raw[2]===255?'jpg':str(0,4)==='RIFF'&&str(8,12)==='WEBP'?'webp':null;
    if(!ext)fail('PNG, JPG, WEBP 이미지를 사용해 주세요.');
    const name=random(16)+'.'+ext,contentType='image/'+(ext==='jpg'?'jpeg':ext);
    if(env.BUCKET)await env.BUCKET.put(name,raw,{httpMetadata:{contentType}});
    else {
      // Keep each row below D1's 2 MB limit and commit the complete image atomically.
      const chunks=[];
      for(let offset=0;offset<data.data.length;offset+=524288)chunks.push(stmt(db,'INSERT INTO image_chunks (name,part,content_type,data) VALUES (?,?,?,?)',[name,chunks.length,contentType,data.data.slice(offset,offset+524288)]));
      await db.batch(chunks);
    }
    return json({url:'images/uploads/'+name});
  }
  if(path==='/api/admin/products'&&method==='POST'){
    const id=text(data.id??'',100)||'p-'+random(8);if(!/^[a-z0-9-]+$/.test(id))fail('잘못된 상품 번호입니다.');
    const p=productData(data,id);await run(db,'INSERT INTO products VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',[id,JSON.stringify(p)]);return json(p);
  }
  if(/^\/api\/admin\/products\/[a-z0-9-]+$/.test(path)&&method==='DELETE'){await run(db,'DELETE FROM products WHERE id=?',[path.split('/').at(-1)]);return json({ok:true});}
  if(path==='/api/admin/posts'&&method==='POST'){
    const id=text(data.id??'',100)||'post-'+random(8);if(!/^[a-z0-9-]+$/.test(id))fail('잘못된 게시물 번호입니다.');
    const p={id,title:text(data.title,200,true),category:text(data.category,40,true),content:text(data.content,15000,true),author:'일심',date:text(data.date??now().slice(0,10),10),visible:data.visible!==false};
    await run(db,'INSERT INTO posts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',[id,JSON.stringify(p)]);return json(p);
  }
  if(/^\/api\/admin\/posts\/[a-z0-9-]+$/.test(path)&&method==='DELETE'){
    const id=path.split('/').at(-1);await db.batch([stmt(db,'DELETE FROM posts WHERE id=?',[id]),stmt(db,'DELETE FROM comments WHERE post_id=?',[id]),stmt(db,'DELETE FROM likes WHERE post_id=?',[id])]);return json({ok:true});
  }
  if(/^\/api\/admin\/(orders|inquiries)\/[A-Za-z0-9-]+$/.test(path)&&['PUT','DELETE'].includes(method)){
    const table=path.split('/')[3],id=path.split('/').at(-1);
    if(method==='DELETE')await run(db,`DELETE FROM ${table} WHERE id=?`,[id]);
    else {if(!STATES.includes(data.status))fail('잘못된 처리 상태입니다.');await run(db,`UPDATE ${table} SET status=? WHERE id=?`,[data.status,id]);}
    return json({ok:true});
  }
  fail('찾을 수 없습니다.',404);
}
export default {
  async fetch(request,env){
    const url=new URL(request.url);
    try{
      if(url.pathname.startsWith('/api/'))return await api(request,env,url);
      if(!['GET','HEAD'].includes(request.method))return new Response('Method Not Allowed',{status:405,headers:security});
      const origin=env.ILSIM_ORIGIN||url.origin;
      if(url.pathname==='/naveraa12cb19ff5b0b79be28136fa909732b.html')return new Response(request.method==='HEAD'?null:verification,{headers:{...security,'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-cache'}});
      if(url.pathname==='/robots.txt')return new Response(`User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: ${origin}/sitemap.xml\n`,{headers:{...security,'Content-Type':'text/plain; charset=utf-8'}});
      if(url.pathname==='/sitemap.xml')return new Response(`<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${origin}/</loc></url></urlset>`,{headers:{...security,'Content-Type':'application/xml; charset=utf-8'}});
      if(/^\/images\/uploads\/[a-f0-9]{32}\.(png|jpg|webp)$/.test(url.pathname)){
        const name=url.pathname.split('/').at(-1);
        let object=env.BUCKET?await env.BUCKET.get(name):null;
        if(!object&&env.DB){
          const chunks=await all(env.DB,'SELECT content_type,data FROM image_chunks WHERE name=? ORDER BY part',[name]);
          if(chunks.length)object={body:request.method==='HEAD'?null:Uint8Array.from(atob(chunks.map(c=>c.data).join('')),c=>c.charCodeAt(0)),httpMetadata:{contentType:chunks[0].content_type}};
        }
        if(!object)return new Response('Not Found',{status:404,headers:security});
        return new Response(request.method==='HEAD'?null:object.body,{headers:{...security,'Content-Type':object.httpMetadata.contentType,'Cache-Control':'public, max-age=3600'}});
      }
      if(!(['/','/index.html','/admin','/admin/','/naveraa12cb19ff5b0b79be28136fa909732b.html'].includes(url.pathname)||/^\/(assets|images)\/[a-zA-Z0-9_./-]+$/.test(url.pathname)))return new Response('Not Found',{status:404,headers:security});
      if(url.pathname.startsWith('/admin'))url.pathname='/index.html';
      const r=await env.ASSETS.fetch(new Request(url,request));
      const headers=new Headers(r.headers);for(const [k,v] of Object.entries(security))headers.set(k,v);
      if(url.pathname==='/'||/\.(html|js|css)$/.test(url.pathname))headers.set('Cache-Control','no-cache');
      return new Response(r.body,{status:r.status,headers});
    }catch(e){
      if(e instanceof UserError)return json({error:e.message},e.status);
      console.error('Request failed',url.pathname,e?.message);
      return json({error:'저장하거나 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.'},503);
    }
  }
};
