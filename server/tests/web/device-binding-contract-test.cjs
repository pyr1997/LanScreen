'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let assertions=0;
const check=new Proxy({}, {get:(_,name)=>(...args)=>{assertions++;return assert[name](...args);}});
class Element {
  constructor(){this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.innerHTML='';this.dataset={};this.events={};this.children=[];this.classList={toggle(){},add(){},remove(){}};}
  addEventListener(name,fn){this.events[name]=fn;} setAttribute(){} replaceChildren(...nodes){this.children=nodes;} append(...nodes){this.children.push(...nodes);}
  closest(){return null;} focus(){} select(){} showModal(){} close(){}
}
const elements=new Map(),requests=[],confirmations=[],timers=new Set();let serial=0,allowConfirm=true;
const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const device=(id,state='active',extra={})=>({id,name:'电视 '+id,bindingState:state,bindingVersion:1,online:true,cleanupSupported:true,version:'1.3.0',last_seen:1800000000,appliedRevision:2,downloadedRevision:2,...extra});
const dashboard=(devices=[],record=null)=>({media:[],archivedMedia:[],devices,draft:[],published:[],revision:record?.revision||0,currentPublication:record,cachePolicy:{maxBytes:2147483648,reserveBytes:268435456},displayToken:'Test-Shared-Only',capabilities:{office:true,ffmpeg:true},diskFree:1073741824});
const record=(extra={})=>({revision:2,isCurrent:true,source:'publish',createdAt:1800000000,recordedAt:1800000000,itemCount:0,pageCount:0,targetCount:1,appliedCount:1,withdrawnCount:2,recordedTargetCount:3,counts:{applied:1,withdrawn:2},items:[],targets:[],...extra});
const ok=value=>({ok:true,status:200,json:async()=>value});
const error=(status,message='synthetic error')=>({ok:false,status,json:async()=>({error:message})});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
let handler=async()=>ok({}),serverDevices=[];
const context={
  console,AbortController,document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>new Element(),createDocumentFragment:()=>new Element(),activeElement:null},
  window:{addEventListener(){}},location:{origin:'http://localhost',hostname:'localhost'},navigator:{},crypto:require('node:crypto').webcrypto,
  setInterval(){},setTimeout(){const id=++serial;timers.add(id);return id;},clearTimeout:id=>timers.delete(id),
  confirm:message=>{confirmations.push(message);return allowConfirm;},
  fetch:async(path,options)=>{requests.push({path,options});return await handler(path,options);}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(__dirname+'/../../web/app.js','utf8').replace(/boot\(\);\s*$/,''),context);
const run=source=>vm.runInContext(source,context),assign=(name,value)=>run(name+'='+JSON.stringify(value));
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const reset=devices=>{
  serverDevices=devices;allowConfirm=true;assign('state',dashboard(devices));
  run("deviceScope='connected';devicePage=1;deviceOperations.clear();cleanupPending.clear();deviceFeedback={message:'',error:false};currentPublicationDetail=null;historyDetail=null;historyItems=[];historySelection=null;activeView='devices';csrf='test-csrf'");
  handler=async(path)=>path==='/api/dashboard'?ok(dashboard(serverDevices)):ok({ok:true});
};
const clickBinding=(action,id,version=1)=>element('device-list').events.click({target:{closest:selector=>selector==='[data-device-action]'?{disabled:false,dataset:{deviceAction:action,deviceId:id,bindingVersion:String(version)}}:null}});
const clickCleanup=(id,version=1)=>element('device-list').events.click({target:{closest:selector=>selector==='[data-cleanup]'?{disabled:false,dataset:{cleanup:id,bindingVersion:String(version)}}:null}});
const postRequests=()=>requests.filter(request=>request.options.method==='POST');
(async()=>{
  const html=fs.readFileSync(__dirname+'/../../web/index.html','utf8');
  for(const id of ['device-tab-connected','device-tab-pending','device-tab-unbound','device-feedback','device-prev','device-next'])check.equal(html.includes('id="'+id+'"'),true);
  check.equal(html.includes('1.3.0'),true);
  check.equal(html.includes('接入申请与同步'),true);
  const old={id:'compat',name:'旧服务电视',online:true,status:'loading'};
  reset([device('a','active',{name:'<script>name</script>',deviceToken:'NEVER_RENDER_TOKEN',credentialHash:'NEVER_RENDER_HASH'}),device('l','legacy'),device('p','pending'),device('u','unbound',{unboundAt:1800000010}),old]);
  run('renderDevices()');
  let markup=element('device-list').innerHTML;
  check.equal(element('device-count-connected').textContent,3);
  check.equal(element('device-count-pending').textContent,1);
  check.equal(element('device-count-unbound').textContent,1);
  check.equal(markup.includes('&lt;script&gt;name&lt;/script&gt;'),true);
  check.equal(markup.includes('<script>'),false);
  check.equal(markup.includes('NEVER_RENDER_'),false,'credential-like properties are never serialized into UI');
  check.equal(markup.includes('电视 p'),false);
  check.equal(markup.includes('电视 u'),false);
  check.equal(markup.includes('旧服务电视'),true);
  check.equal(markup.includes('旧版待升级'),true);
  check.equal(markup.includes('已接入 · 旧服务记录'),true);
  check.equal(markup.includes('正在准备播放'),true);
  check.equal(markup.includes('data-device-id="compat"'),false,'missing binding metadata never produces a mutation action');
  check.equal(markup.includes('data-cleanup="l" data-binding-version="1" disabled'),true,'legacy cannot request cleanup');
  check.equal(markup.includes('data-cleanup="a" data-binding-version="1" >'),true);
  check.equal(markup.includes('data-device-action="unbind" data-device-id="l"'),true,'explicit legacy records can be unbound');
  element('device-tab-pending').onclick();markup=element('device-list').innerHTML;
  check.equal(markup.includes('电视 p'),true);check.equal(markup.includes('电视 l'),false);
  check.equal(markup.includes('data-device-action="approve"'),true);
  check.equal(markup.includes('data-cleanup='),false);
  element('device-tab-unbound').onclick();markup=element('device-list').innerHTML;
  check.equal(markup.includes('电视 u'),true);
  check.equal(markup.includes('重新申请绑定'),true);
  check.equal(markup.includes('不代表现场已经停播或擦除缓存'),true);
  check.equal(markup.includes('data-device-action='),false,'unbound records have no fake restore/approve action');
  check.equal(markup.includes('data-cleanup='),false);

  reset(Array.from({length:13},(_,i)=>device('page'+i)));
  run('renderDevices()');
  check.equal((element('device-list').innerHTML.match(/<article /g)||[]).length,12);
  check.equal(element('device-pagination').hidden,false);
  check.equal(element('device-prev').disabled,true);
  element('device-next').onclick();
  check.equal((element('device-list').innerHTML.match(/<article /g)||[]).length,1);
  check.equal(element('device-next').disabled,true);
  assign('state.devices',[device('only')]);run('renderDevices()');
  check.equal(run('devicePage'),1,'polling a shorter group clamps the page');
  check.equal(element('device-pagination').hidden,true);

  reset([device('approval','pending')]);
  const approve=deferred();const beforeApprove=postRequests().length;
  handler=async(path)=>path.endsWith('/approve')?approve.promise:path==='/api/dashboard'?ok(dashboard(serverDevices)):ok({});
  const pendingApprove=clickBinding('approve','approval');
  check.equal(postRequests().length,beforeApprove+1);
  check.equal(run("deviceOperations.has('approval')"),true);
  await clickBinding('approve','approval');
  await clickBinding('unbind','approval');
  check.equal(postRequests().length,beforeApprove+1,'double click and a conflicting same-device action submit once');
  const sent=postRequests().at(-1);
  check.equal(sent.path,'/api/devices/approval/approve');
  check.equal(sent.options.headers['X-CSRF-Token'],'test-csrf');
  check.deepEqual(JSON.parse(sent.options.body),{bindingVersion:1});
  check.equal(confirmations.at(-1).includes('设备编号 approval，接入批次 1'),true);
  serverDevices=[device('approval','active')];approve.resolve(ok({ok:true}));await pendingApprove;
  check.equal(run("state.devices[0].bindingState"),'active');
  check.equal(run('deviceOperations.size'),0);
  check.equal(element('device-feedback').textContent.includes('已批准接入'),true);

  reset([device('unbind-me')]);
  const beforeCancel=postRequests().length;allowConfirm=false;
  await clickBinding('unbind','unbind-me');
  check.equal(postRequests().length,beforeCancel,'cancelled confirmation performs no mutation');
  const warning=confirmations.at(-1);
  for(const phrase of ['退出发布目标','新联网请求','离线电视已有缓存无法立即停播或擦除','不删除 NAS 素材','重新申请'])check.equal(warning.includes(phrase),true);
  allowConfirm=true;
  handler=async(path)=>{
    if(path.endsWith('/unbind')){serverDevices=[device('unbind-me','unbound',{unboundAt:1800000011})];return ok({ok:true});}
    return ok(dashboard(serverDevices));
  };
  await clickBinding('unbind','unbind-me');
  check.equal(run("state.devices[0].bindingState"),'unbound');
  check.equal(element('device-list').innerHTML.includes('电视 unbind-me'),false,'successful unbind removes the record from connected group');
  check.equal(element('device-count-unbound').textContent,1);
  check.equal(element('device-feedback').textContent.includes('服务器已解绑'),true);
  check.equal(element('device-feedback').textContent.includes('现场核实'),true);
  const afterUnbind=postRequests().length;
  await clickBinding('approve','unbind-me');await clickBinding('unbind','unbind-me');await clickCleanup('unbind-me');
  check.equal(postRequests().length,afterUnbind);

  reset([device('stale','active',{bindingVersion:2})]);
  let oldPosts=postRequests().length;
  await clickBinding('unbind','stale',1);
  check.equal(postRequests().length,oldPosts,'stale DOM binding generation never submits');
  check.equal(element('device-feedback').textContent.includes('状态已变化'),true);
  reset([device('conflict')]);oldPosts=postRequests().length;
  handler=async(path)=>{
    if(path.endsWith('/unbind')){serverDevices=[device('conflict','unbound',{bindingVersion:2})];return error(409);}
    return ok(dashboard(serverDevices));
  };
  await clickBinding('unbind','conflict');
  check.equal(postRequests().length,oldPosts+1);
  check.equal(run("state.devices[0].bindingState"),'unbound');
  check.equal(element('device-feedback').textContent.includes('本次操作未执行'),true);
  check.equal(element('device-feedback').textContent.includes('服务器已解绑'),false);

  reset([device('not-permitted','pending')]);
  handler=async()=>error(403,'denied');
  await clickBinding('approve','not-permitted');
  check.equal(element('device-feedback').textContent.includes('操作未通过'),true);
  check.equal(run("state.devices[0].bindingState"),'pending');
  reset([device('uncertain')]);
  handler=async()=>{throw new Error('connection lost');};
  await clickBinding('unbind','uncertain');
  check.equal(element('device-feedback').textContent.includes('尚未确认'),true);
  check.equal(run("state.devices[0].bindingState"),'active');
  reset([device('accepted')]);
  handler=async(path)=>{if(path.endsWith('/unbind'))return ok({ok:true});throw new Error('refresh failed');};
  await clickBinding('unbind','accepted');
  check.equal(element('device-feedback').textContent.includes('服务器已接受解绑'),true);
  check.equal(element('device-feedback').textContent.includes('无需重复提交'),true);
  check.equal(run('deviceOperations.size'),0);

  reset([device('cleanup-active'),device('cleanup-legacy','legacy'),device('cleanup-pending','pending'),old]);
  oldPosts=postRequests().length;
  await clickCleanup('cleanup-legacy');await clickCleanup('cleanup-pending');await clickCleanup('compat');
  check.equal(postRequests().length,oldPosts);
  await clickCleanup('cleanup-active');
  check.equal(postRequests().length,oldPosts+1);
  check.equal(postRequests().at(-1).path,'/api/devices/cleanup-active/cleanup');
  check.equal(element('toast').textContent.includes('尚不代表清理完成'),true);
  oldPosts=postRequests().length;
  await clickCleanup('cleanup-active',999);
  check.equal(postRequests().length,oldPosts);

  reset([device('old-session')]);const late=deferred();
  handler=async()=>late.promise;
  const obsolete=clickBinding('unbind','old-session');
  run('invalidatePublicationSession()');
  late.resolve(ok({ok:true}));await obsolete;
  check.equal(run('state.devices.length'),0);
  check.equal(run('deviceOperations.size'),0);
  check.equal(element('device-feedback').textContent,'');
  check.equal(element('device-list').innerHTML.includes('old-session'),false,'late accepted request cannot revive a logged-out device list');
  reset([device('expired')]);handler=async()=>error(401);
  await clickBinding('unbind','expired');
  check.equal(element('dashboard').hidden,true);
  check.equal(run('state.devices.length'),0);
  check.equal(element('device-feedback').textContent,'');

  reset([]);
  assign('state.currentPublication',record());run('renderPublicationProgress()');
  check.equal(element('publication-percent').textContent,'100%');
  check.equal(element('publication-count').textContent.includes('1 / 1 台电视已确认应用'),true);
  check.equal(element('publication-count').textContent.includes('另 2 条电视接入记录已退出'),true);
  check.equal(run('publicationRatio(state.currentPublication).total'),1,'server targetCount is already filtered; do not subtract withdrawn twice');
  assign('state.currentPublication',record({targetCount:0,appliedCount:0}));
  run('renderPublicationProgress()');
  check.equal(element('publication-percent').textContent,'—','all withdrawn never becomes artificial 100 percent');
  check.equal(element('publication-status').textContent.includes('暂无可确认目标'),true);
  check.equal(element('publication-count').textContent.includes('暂无目标电视'),true);
  check.equal(run('publicationWithdrawn({targetCount:2,appliedCount:1})'),0,'old service records remain compatible');
  check.equal(run('publicationWithdrawn({targetCount:1,recordedTargetCount:3})'),2);
  check.equal(run('publicationWithdrawn({counts:{withdrawn:2}})'),2);
  const sameDevice=[
    {deviceId:'same-tv',name:'<img src=x>',bindingVersion:1,stage:'withdrawn',withdrawnAt:1800000010,withdrawReason:'device_unbound',appliedAt:1800000002,downloadedAt:1800000001,online:true,lastSeen:1800000002},
    {deviceId:'same-tv',name:'<img src=x>',bindingVersion:2,stage:'pending',withdrawnAt:null,appliedAt:null,downloadedAt:null,online:true,lastSeen:1800000011}
  ];
  assign('historyDetail',record({targets:sameDevice,withdrawnCount:1,recordedTargetCount:2,appliedCount:0}));run('historySelection=2;renderPublicationHistoryDetail()');
  const history=element('publication-history-detail').innerHTML;
  check.equal((history.match(/<article class="publication-target/g)||[]).length,2,'same TV with two bindings keeps distinct history rows');
  check.equal(history.includes('接入批次：1'),true);check.equal(history.includes('接入批次：2'),true);
  check.equal(history.includes('&lt;img src=x&gt;'),true);check.equal(history.includes('<img src=x>'),false);
  check.equal(history.includes('管理员解绑'),true);
  check.equal(history.includes('重新接入不会继承确认'),true);
  check.equal(history.includes('0 / 1 台电视'),true);
  check.equal(history.includes('另 1 条电视接入记录已退出'),true);
  check.equal(history.includes('累计记录 2 个接入批次'),true);
  check.equal(history.includes(run('publicationTime(1800000002)')),true,'withdrawn historical application time is retained as fact');
  assign('historyItems',[record()]);run('renderPublicationHistory()');
  check.equal(element('publication-history-list').innerHTML.includes('另 2 条电视接入记录已退出'),true);
  check.equal(run("publicationTargetRows([{deviceId:'x',bindingVersion:1,state:'withdrawn',withdrawReason:'binding_replaced'}]).includes('原接入批次退出')"),true);
  check.equal(run("publicationTargetRows([{deviceId:'x',stage:'pending',withdrawnAt:1800000001}]).includes('不计入当前目标或完成数')"),true);
  run('invalidatePublicationSession();clearTimeout(toastTimer)');
  check.equal(run('deviceOperations.size'),0);
  check.equal(timers.size,0,'every request deadline and simulated toast timer has completed or been cleared');
  console.log('PASS: '+assertions+' device binding UI assertions; mocked admin requests only; no NAS/TV calls, ports, or background services.');
})().catch(error=>{console.error(error);process.exitCode=1;});
