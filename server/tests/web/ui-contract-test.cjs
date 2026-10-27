'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
  constructor(){this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.innerHTML='';this.dataset={};this.events={};this.open=false;this.children=[];this.classList={toggle(){},add(){},remove(){}};}
  addEventListener(name,fn){this.events[name]=fn;}
  replaceChildren(...items){this.children=items;}
  append(...items){this.children.push(...items);}
  focus(){} select(){this.selected=true;} closest(){return null;} showModal(){this.open=true;}
  close(){this.open=false;this.events.close?.({});}
}
const elements=new Map(),requests=[],confirmations=[];
let copied='',deferTokenRequest=false,resolveTokenRequest,deferDashboard=false,resolveDashboard;
let tokenNetworkError=false,tokenResponse={ok:true,status:200,body:{ok:true,changed:true}},dashboardToken='SavedToken_123',dashboardUnauthorized=false;
const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const context={console,AbortController,document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>new Element(),createDocumentFragment:()=>new Element(),activeElement:null,execCommand:()=>true},window:{addEventListener(){}},location:{origin:'http://localhost',hostname:'localhost'},navigator:{clipboard:{writeText:async value=>{copied=value;}}},crypto:{getRandomValues(bytes){for(let i=0;i<bytes.length;i++)bytes[i]=(i*37+11)%198;return bytes;}},setInterval(){},setTimeout(){},clearTimeout(){},confirm:message=>{confirmations.push(message);return true;}};
const dashboard=displayToken=>({media:[],archivedMedia:[],devices:[],draft:[],published:[],revision:0,cachePolicy:{maxBytes:2147483648,reserveBytes:268435456},displayToken,capabilities:{office:true,ffmpeg:true},diskFree:1073741824});
context.fetch=async(path,options)=>{
  requests.push({path,options});
  if(path==='/api/display-token'){
    if(deferTokenRequest)await new Promise(resolve=>{resolveTokenRequest=resolve;});
    if(tokenNetworkError)throw new Error('connection lost after submit');
    return {ok:tokenResponse.ok,status:tokenResponse.status,json:async()=>tokenResponse.body};
  }
  if(path==='/api/dashboard'){
    const responseToken=dashboardToken;
    if(deferDashboard)await new Promise(resolve=>{resolveDashboard=resolve;});
    if(dashboardUnauthorized)return {ok:false,status:401,json:async()=>({error:'登录已失效。'})};
    return {ok:true,status:200,json:async()=>dashboard(responseToken)};
  }
  if(path==='/api/logout')return {ok:true,status:200,json:async()=>({ok:true})};
  return {ok:true,status:200,json:async()=>({id:'test-id',name:'素材.png',allowed:true,files:[{path:'test-id/<img src=x>.png',bytes:5}],fileCount:1,totalBytes:5,deletionToken:'mock-confirm-only'})};
};
vm.createContext(context);
const source=fs.readFileSync(process.argv[2]||__dirname+'/../../web/app.js','utf8').replace(/boot\(\);\s*$/,'');vm.runInContext(source,context);
const run=code=>vm.runInContext(code,context);
(async()=>{
  const html=fs.readFileSync(__dirname+'/../../web/index.html','utf8');
  assert(html.includes('id="token-form"'));assert(html.includes('id="generate-token"'));assert(html.includes('pattern="[A-Za-z0-9._~-]{12,128}"'));assert(html.includes('不具备后台管理权限'));assert(html.includes('请在网络稳定时逐台更换'));
  assert.equal(run('bytes(null)'),'未回报');assert.equal(run('bytes(0)'),'0 B');assert.equal(run('revisionLabel(-1)'),'未回报');assert.equal(run('revisionLabel(0)'),'v0');
  run("state={displayToken:'SavedToken_123'};tokenDirty=false;tokenSaving=false;tokenStatus='已保存';tokenBase='';renderTokenEditor();");
  assert.equal(element('display-token').value,'SavedToken_123');assert.equal(run('tokenBase'),'SavedToken_123');assert.equal(element('save-token').disabled,true);
  dashboardToken='OlderPollToken_123';deferDashboard=true;const olderRefresh=run('refresh()');
  dashboardToken='LatestPollToken_456';deferDashboard=false;await run('refresh()');
  assert.equal(run('state.displayToken'),'LatestPollToken_456');resolveDashboard();await olderRefresh;
  assert.equal(run('state.displayToken'),'LatestPollToken_456','an older parallel refresh cannot replace a later request result');
  dashboardToken='SavedToken_123';run("state.displayToken='SavedToken_123';tokenBase='SavedToken_123';renderTokenEditor()");
  element('display-token').value='EditingToken_456';element('display-token').events.input();
  assert.equal(run('tokenDirty'),true);assert.equal(element('token-state').textContent,'设置尚未保存');
  run("state.displayToken='PolledToken_789';renderTokenEditor();");
  assert.equal(element('display-token').value,'EditingToken_456','poll render does not overwrite an edited token');assert.equal(run('tokenBase'),'SavedToken_123','editing keeps the original comparison base');
  run("state.displayToken='SavedToken_123'");
  assert.equal(run("displayTokenError('short')"),'播放密钥需为 12～128 个字符。');assert.equal(run("displayTokenError('Valid_Token-~1')"),'');
  const beforeInvalid=requests.length;element('display-token').value='invalid key!!';element('display-token').events.input();await element('token-form').events.submit({preventDefault(){}});
  assert.equal(requests.length,beforeInvalid,'invalid token never submits');assert(element('token-error').textContent.includes('仅可使用'));
  element('generate-token').onclick();const generated=element('display-token').value;
  assert.equal(generated.length,32);assert(/^[A-Za-z0-9._~-]+$/.test(generated));assert.equal(run('tokenDirty'),true);
  element('display-token').value='CurrentInput_456';element('display-token').events.input();await element('copy-token').onclick();
  assert.equal(copied,'CurrentInput_456','copy uses the current input rather than stale server state');
  const workingClipboard=context.navigator.clipboard.writeText;context.navigator.clipboard.writeText=async()=>{throw new Error('permission denied');};
  element('display-token').type='password';element('show-token').textContent='显示';await element('copy-token').onclick();
  assert.equal(element('display-token').type,'password','a failed copy must not reveal the key');assert.equal(element('show-token').textContent,'显示');context.navigator.clipboard.writeText=workingClipboard;
  dashboardToken='SavedToken_123';deferDashboard=true;const staleRefresh=run('refresh()');
  deferTokenRequest=true;const savePromise=element('token-form').events.submit({preventDefault(){}});
  assert.equal(run('tokenSaving'),true);assert.equal(element('display-token').disabled,true);assert.equal(element('token-state').textContent,'正在保存…');
  const tokenRequests=requests.filter(r=>r.path==='/api/display-token'),tokenRequest=tokenRequests[tokenRequests.length-1];
  assert.equal(tokenRequest.options.method,'PUT');assert.deepEqual(JSON.parse(tokenRequest.options.body),{displayToken:'CurrentInput_456',currentDisplayToken:'SavedToken_123'});
  const tokenConfirmation=confirmations[confirmations.length-1];
  assert(tokenConfirmation.includes('当前服务实例'));assert(tokenConfirmation.includes('旧播放密钥'));assert(tokenConfirmation.includes('每台电视'));assert(tokenConfirmation.includes('不会撤回电视已有的离线缓存'));assert(tokenConfirmation.includes('重新同步'));
  resolveTokenRequest();await savePromise;deferTokenRequest=false;
  assert.equal(run('state.displayToken'),'CurrentInput_456');assert.equal(run('tokenBase'),'CurrentInput_456');assert.equal(run('tokenDirty'),false);assert.equal(run('tokenSaving'),false);assert.equal(element('token-state').textContent,'已保存，新密钥已生效');
  resolveDashboard();await staleRefresh;deferDashboard=false;
  assert.equal(run('state.displayToken'),'CurrentInput_456');assert.equal(element('display-token').value,'CurrentInput_456','stale dashboard response cannot overwrite a successful save');
  element('display-token').value='RecoveredToken_789';element('display-token').events.input();tokenNetworkError=true;
  await element('token-form').events.submit({preventDefault(){}});tokenNetworkError=false;
  assert.equal(run('tokenDirty'),true);assert.equal(element('display-token').value,'RecoveredToken_789');assert.equal(element('token-state').textContent,'保存未完成');
  dashboardToken='RecoveredToken_789';await run('refresh()');
  assert.equal(run('tokenDirty'),false);assert.equal(run('tokenBase'),'RecoveredToken_789');assert.equal(element('token-state').textContent,'已重新核对，密钥已生效');
  element('display-token').value='ConflictToken_987';element('display-token').events.input();
  dashboardToken='OtherPageToken_654';await run('refresh()');
  assert.equal(run('tokenBase'),'RecoveredToken_789','ordinary polling must not advance the base of a dirty edit');assert.equal(element('display-token').value,'ConflictToken_987');
  const confirmsBeforeConflict=confirmations.length;
  tokenResponse={ok:false,status:409,body:{error:'播放密钥已被其他管理员修改。'}};await element('token-form').events.submit({preventDefault(){}});
  assert.equal(confirmations.length,confirmsBeforeConflict+1);assert.equal(run('tokenDirty'),true);assert.equal(run('tokenBase'),'OtherPageToken_654');assert.equal(run('state.displayToken'),'OtherPageToken_654');assert.equal(element('display-token').value,'ConflictToken_987');assert.equal(element('token-state').textContent,'已取得最新密钥，请重新确认保存');assert.equal(element('token-error').textContent,'播放密钥已被其他管理员修改。');
  const conflictRequests=requests.filter(r=>r.path==='/api/display-token'),conflictBody=JSON.parse(conflictRequests[conflictRequests.length-1].options.body);
  assert.deepEqual(conflictBody,{displayToken:'ConflictToken_987',currentDisplayToken:'RecoveredToken_789'});
  tokenResponse={ok:true,status:200,body:{ok:true,changed:true}};await element('token-form').events.submit({preventDefault(){}});
  const retriedRequests=requests.filter(r=>r.path==='/api/display-token'),retriedBody=JSON.parse(retriedRequests[retriedRequests.length-1].options.body);
  assert.equal(confirmations.length,confirmsBeforeConflict+2,'retry requires a second explicit confirmation');assert.deepEqual(retriedBody,{displayToken:'ConflictToken_987',currentDisplayToken:'OtherPageToken_654'});assert.equal(run('tokenBase'),'ConflictToken_987');assert.equal(run('state.displayToken'),'ConflictToken_987');assert.equal(run('tokenDirty'),false);
  run("state.displayToken='VisibleSecret_123';tokenBase='VisibleSecret_123';tokenDirty=false;");element('display-token').value='VisibleSecret_123';element('display-token').type='text';element('show-token').textContent='隐藏';dashboardUnauthorized=true;
  let unauthorized;try{await run("api('/api/dashboard')");}catch(err){unauthorized=err;}dashboardUnauthorized=false;
  assert.equal(unauthorized.status,401);assert.equal(element('display-token').value,'');assert.equal(element('display-token').type,'password');assert.equal(element('show-token').textContent,'显示');assert.equal(run('state.displayToken'),'');
  run("state={displayToken:'LogoutSecret_456'};tokenBase='LogoutSecret_456';tokenDirty=false;dirty=false;policyDirty=false;");element('display-token').value='LogoutSecret_456';element('display-token').type='text';element('show-token').textContent='隐藏';
  await element('logout').onclick();assert.equal(element('display-token').value,'');assert.equal(element('display-token').type,'password');assert.equal(run('state.displayToken'),'');
  await run("openDeletion('test-id')");
  assert.equal(element('confirm-delete').disabled,true,'opening preview never authorizes deletion');
  element('delete-confirm-name').value='素材';run('updateDeleteButton()');assert.equal(element('confirm-delete').disabled,true);
  element('delete-confirm-name').value='素材.png';run('updateDeleteButton()');assert.equal(element('confirm-delete').disabled,false);
  assert.equal(element('delete-files').children[0].children[0].children[0].textContent,'test-id/<img src=x>.png','paths are rendered as text');
  element('delete-dialog').close();assert.equal(requests.filter(r=>r.path.includes('/delete')&&r.options?.method==='POST').length,0,'cancel does not send deletion');
  run("draft=[{mediaId:'test-id',duration:10}]; dirty=true;");await run("openDeletion('test-id')");element('delete-confirm-name').value='素材.png';run('updateDeleteButton()');assert.equal(element('confirm-delete').disabled,true,'unsaved playlist reference blocks deletion');
  run("draft=[]; state={cachePolicy:{maxBytes:2147483648,reserveBytes:268435456}}; policyDirty=false;renderCachePolicy();");
  assert.equal(element('cache-max').value,2048);element('cache-max').value='1024';element('cache-policy-form').events.input();run('renderCachePolicy()');assert.equal(element('cache-max').value,'1024','poll does not overwrite unsaved policy');
  const before=requests.length;element('cache-max').value='255';await element('cache-policy-form').events.submit({preventDefault(){}});assert.equal(requests.length,before,'invalid policy never submits');
  run("state={media:[],archivedMedia:[],revision:8,devices:[{id:'old',name:'<script>x</script>',online:false,cleanupSupported:false,updateState:'unsupported',cleanupDisplayState:'unsupported',version:'1.0.0',last_seen:0}]};renderDevices();");
  assert(element('device-list').innerHTML.includes('&lt;script&gt;'));assert(!element('device-list').innerHTML.includes('<script>'));assert(element('device-list').innerHTML.includes('未回报'));assert(element('device-list').innerHTML.includes('需升级'));assert(!element('device-list').innerHTML.includes('0 B'));
  run("state={media:[{id:'x',name:'<img onerror=x>',kind:'image',status:'ready',size:10,pages:[{url:'/a.png'}]}],archivedMedia:[]};mediaScope='active';renderMedia();");assert(element('media-grid').innerHTML.includes('&lt;img onerror=x&gt;'));assert(!element('media-grid').innerHTML.includes('<img onerror=x>'));
  run("draft=[];"); await run("openDeletion('test-id')"); element('delete-confirm-name').value='素材.png';run('updateDeleteButton()');
  context.fetch=async()=>{throw new Error('connection lost after submit');};
  await element('delete-form').events.submit({preventDefault(){}});
  assert.equal(element('confirm-delete').disabled,true,'uncertain deletion invalidates approval until a fresh preview');
  console.log('PASS: UI contract covers playback-key CAS conflict recovery and retry, fixed dirty-edit baselines, uncertain-save reconciliation, stale-save-poll and parallel-refresh protection, logout/401 clearing, validation, secure generation, current-input copy, copy-failure privacy, safety wording, deletion confirmation, escaped content, draft protection, policy preservation and legacy/offline metrics. No file deletion or live device requests.');
})().catch(e=>{console.error(e);process.exitCode=1;});
