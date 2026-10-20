import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import vm from 'node:vm';
const html=fs.readFileSync(new URL('../../tma_static_v2/index.html',import.meta.url),'utf8');
function fixture(){
 const a=html.indexOf('// HOME_SESSION_BEGIN'),b=html.indexOf('// HOME_SESSION_END');assert.ok(a>=0&&b>a,'Home session guard missing');
 const token='h.'+Buffer.from(JSON.stringify({id:1001,home_entitlement:true})).toString('base64url')+'.s';const storage=new Map([['rb_v2_jwt',token],['rb-v2-state-1',JSON.stringify({cart:['OLD'],favorites:['OLD']})]]);const root={innerHTML:'PRIVATE'};
 const state={data:null,cart:[],favorites:[],contact:{name:'PRIVATE',phone:'PRIVATE',delivery:{}},myOrders:[{id:'PRIVATE'}],history:[],isAdmin:false};
 const c=vm.createContext({console,atob:s=>Buffer.from(s,'base64').toString(),STATE:state,JWT_KEY:'rb_v2_jwt',getJwt:()=>storage.get('rb_v2_jwt')||'',setJwt:t=>t?storage.set('rb_v2_jwt',t):storage.delete('rb_v2_jwt'),localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},document:{getElementById:()=>root,querySelectorAll:()=>[]},fetch:null,URL,location:{origin:'http://fixture'},authHeaders:()=>({Authorization:'Bearer '+(storage.get('rb_v2_jwt')||'')})});
 vm.runInContext(html.slice(a,b),c);return {c,state,root,storage,run:code=>vm.runInContext(code,c)};
}
test('Home never reads legacy cart before verified principal',()=>{const f=fixture();assert.deepEqual(JSON.parse(f.run('JSON.stringify(_shopReadJson("rb-v2-state-1",{}))')),{});});
test('verified namespaces isolate two principals and legacy storage',()=>{const f=fixture();f.run('_shopPrincipal="1001";_shopWriteJson("rb-v2-state-1",{cart:["A"]})');f.run('_shopPrincipal="1002";_shopWriteJson("rb-v2-state-1",{cart:["B"]})');assert.ok(f.storage.get('rb-v2-state-1').includes('OLD'));f.run('_shopPrincipal="1001"');assert.equal(f.run('_shopReadJson("rb-v2-state-1",{}).cart[0]'),'A');});
test('403 and network errors clear private state and DOM',async()=>{for(const network of [false,true]){const f=fixture();f.c.fetch=async()=>{if(network)throw Error('offline');return {status:403,ok:false,headers:{get:()=>null}}};await assert.rejects(f.run('_shopFetch("/tma/api/user/me")'));assert.equal(f.state.myOrders.length,0);assert.equal(f.state.contact.name,'');assert.ok(!f.root.innerHTML.includes('PRIVATE'));}});
test('late decoded JSON after newer denial is rejected',async()=>{const f=fixture();let finish;f.c.fetch=async()=>({status:200,ok:true,headers:{get:()=> '1001'},json:()=>new Promise(r=>finish=r)});const r=await f.run('_shopFetch("/tma/api/user/me")');const old=r.json();f.c.fetch=async()=>({status:403,ok:false,headers:{get:()=>null}});await assert.rejects(f.run('_shopFetch("/tma/api/my_orders")'));finish({private:'OLD'});await assert.rejects(old);});
test('server principal is required before private payload is returned',async()=>{const f=fixture();f.c.fetch=async()=>({status:200,ok:true,headers:{get:()=>null},json:async()=>({private:'OLD'})});await assert.rejects(f.run('_shopFetch("/tma/api/user/me")'));});
test('private request sets no-store',async()=>{const f=fixture();let seen;f.c.fetch=async(u,o)=>(seen=o,{status:200,ok:true,headers:{get:()=> '1001'},json:async()=>({})});await f.run('_shopFetch("/tma/api/user/me")');assert.equal(seen.cache,'no-store');});
test('principal change in trusted response clears old private state',async()=>{const f=fixture();f.run('_shopPrincipal="1001"');f.c.fetch=async()=>({status:200,ok:true,headers:{get:()=> '1002'},json:async()=>({private:'B'})});await assert.rejects(f.run('_shopFetch("/tma/api/user/me")'));assert.equal(f.state.myOrders.length,0);assert.equal(f.state.contact.name,'');});
test('JSON decode failure clears and stale older failure cannot clear a newer epoch',async()=>{const f=fixture();f.c.fetch=async()=>({status:200,ok:true,headers:{get:()=> '1001'},json:async()=>{throw Error('broken')}});const response=await f.run('_shopFetch("/tma/api/user/me")');await assert.rejects(response.json());assert.equal(f.state.myOrders.length,0);
 const g=fixture();let fail;g.c.fetch=()=>new Promise((_,reject)=>fail=reject);const old=g.run('_shopFetch("/tma/api/user/me")');g.run('_shopEpoch++;STATE.contact.name="NEW"');fail(Error('old offline'));await assert.rejects(old);assert.equal(g.state.contact.name,'NEW');});
test('private favorites migration never merges origin-global list for Home',()=>{const body=html.match(/async function syncFavoritesFromServer\(\) \{[\s\S]*?\n\}/)[0];assert.ok(body.includes('!_shopHomeMode &&'));});
test('actual save/loadLocal uses verified namespace, preserving legacy behavior',()=>{const f=fixture();f.c.safeStorageGet=(storage,key,fallback)=>storage.getItem(key)??fallback;f.c.safeStorageJson=(storage,key,fallback)=>JSON.parse(storage.getItem(key)||'null')??fallback;f.c.safeStorageSet=(storage,key,value)=>storage.setItem(key,value);f.run('const STORAGE_KEY="rb-v2-state-1", PROFILE_KEY="rb-v2-profile-1"');
 for(const name of ['saveLocal','loadLocal','saveProfile']){const body=html.match(new RegExp(`function ${name}\\(\\) \\{[\\s\\S]*?\\n\\}`))[0];f.run(body);}
 f.run('loadLocal()');assert.deepEqual(Array.from(f.state.cart),[]);f.run('_shopPrincipal="1001";STATE.cart=["A"];saveLocal();STATE.cart=[];loadLocal()');assert.equal(f.state.cart[0],'A');f.run('_shopPrincipal="1002";loadLocal()');assert.equal(f.state.cart.length,0);f.run('_shopHomeMode=false;loadLocal()');assert.equal(f.state.cart[0],'OLD');});
test('service worker bypasses API before any cache strategy',()=>{const sw=fs.readFileSync(new URL('../../tma_static_v2/sw.js',import.meta.url),'utf8');assert.ok(sw.indexOf("url.pathname.startsWith('/tma/api/')")<sw.indexOf('// 1. HTML'));});
test('revoked Home context remains bound after token removal and reload',()=>{assert.ok(html.includes("_shopHasHomeContext()"),'Home context must survive cleared credential');assert.ok(html.includes("params.set('home', '1')"),'Home context marker must be retained without putting token in HTTP URL');});
test('storage event clears A even when browser storage already contains B',()=>{const f=fixture();let handler;let reloads=0;f.c.window={addEventListener:(name,fn)=>handler=fn};f.c.location.reload=()=>reloads++;const listener=html.match(/window.addEventListener\('storage', event => \{[\s\S]*?\n\}\);/)[0];f.run(listener);const next='h.'+Buffer.from(JSON.stringify({id:1002,home_entitlement:true})).toString('base64url')+'.s';f.storage.set('rb_v2_jwt',next);handler({key:'rb_v2_jwt',newValue:next});assert.equal(f.state.myOrders.length,0);assert.equal(reloads,1);});
test('Home marker prevents legacy storage reload after credential removal',()=>{const f=fixture();f.storage.delete('rb_v2_jwt');f.c.URLSearchParams=URLSearchParams;f.c.location.search='?home=1';f.run('_shopHomeMode=_shopHasHomeContext()');assert.deepEqual(JSON.parse(f.run('JSON.stringify(_shopReadJson("rb-v2-state-1",{}))')),{});});
test('actual Home profile renders verified account without reading unsafe Telegram identity',()=>{
 const code=html.slice(html.indexOf('function screenProfile()'),html.indexOf('function screenMyOrder()'));
 for(const verified of ['Verified Native Customer','']){
  const c=vm.createContext({_shopHomeMode:true,IS_TG:true,tg:{get initDataUnsafe(){throw Error('unsafe Telegram identity read');}},
   STATE:{contact:{name:verified},boughtBefore:[],myOrders:[],data:{},isAdmin:false},
   appHeader:()=>'',icon:()=>'',tabBar:()=>'',esc:v=>String(v),fmt:String});
  vm.runInContext(code+';this.output=screenProfile();',c);
  assert.ok(c.output.includes(verified||'Аккаунт'));assert.ok(c.output.includes('>Аккаунт</span>'));
  assert.ok(!c.output.includes('>Гость</span>'));
 }
});
test('actual legacy profile keeps Telegram name and badge',()=>{
 const code=html.slice(html.indexOf('function screenProfile()'),html.indexOf('function screenMyOrder()'));
 const c=vm.createContext({_shopHomeMode:false,IS_TG:true,tg:{initDataUnsafe:{user:{first_name:'Legacy',last_name:'Telegram'}}},
  STATE:{contact:{name:'Unrelated'},boughtBefore:[],myOrders:[],data:{},isAdmin:false},
  appHeader:()=>'',icon:()=>'',tabBar:()=>'',esc:v=>String(v),fmt:String});
 vm.runInContext(code+';this.output=screenProfile();',c);assert.ok(c.output.includes('Legacy Telegram'));assert.ok(c.output.includes('>Telegram</span>'));
});
test('Home checkout cannot fill an empty verified name from unsafe Telegram identity',()=>{
 const code=html.slice(html.indexOf('  checkout() {'),html.indexOf('  updateContact(key, value)'));
 const c=vm.createContext({_shopHomeMode:true,tg:{get initDataUnsafe(){throw Error('unsafe Telegram identity read');}},STATE:{cart:[1],contact:{name:''}},haptic:()=>{},rb:{go:()=>{}}});
 vm.runInContext('const actions={'+code+'};actions.checkout();',c);assert.equal(c.STATE.contact.name,'');
});
