'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let assertions=0;
const check=new Proxy({}, {get:(_,name)=>(...args)=>{assertions++;return assert[name](...args);}});
const source=fs.readFileSync(__dirname+'/../../web/app.js','utf8').replace(/boot\(\);\s*$/,'');
const html=fs.readFileSync(__dirname+'/../../web/index.html','utf8');
const ok=value=>({ok:true,status:200,json:async()=>value});
const error=(status,message='synthetic rejection')=>({ok:false,status,json:async()=>({error:message})});
const deferred=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return {promise,resolve,reject};};
const record=(revision=9,extra={})=>({revision,isCurrent:revision===10,source:'publish',createdAt:1800000000,recordedAt:1800000000,itemCount:0,pageCount:0,targetCount:0,appliedCount:0,counts:{},items:[],targets:[],...extra});
const dashboard=(revision=10)=>({media:[],archivedMedia:[],devices:[],draft:[],published:[],revision,currentPublication:record(revision,{isCurrent:true}),cachePolicy:{maxBytes:2147483648,reserveBytes:268435456},displayToken:'Synthetic-Shared-Key',capabilities:{office:true,ffmpeg:true},diskFree:1073741824});
const preview=(extra={})=>({revisions:[9],deleteCount:1,targetCount:0,totalCandidates:1,hasMore:false,currentRevision:10,confirmationToken:'synthetic-frozen-token',expiresAt:1800000300,expiresIn:300,...extra});
const result=(extra={})=>({ok:true,deletedCount:1,targetCount:0,remainingCount:2,currentRevision:10,alreadyDeletedCount:0,...extra});
const harnesses=[];
class Element {
  constructor(){this.value='';this.hidden=false;this.disabled=false;this.checked=false;this.open=false;this.textContent='';this.innerHTML='';this.dataset={};this.events={};this.children=[];this.classList={toggle(){},add(){},remove(){}};}
  addEventListener(name,fn){this.events[name]=fn;}setAttribute(){}replaceChildren(...nodes){this.children=nodes;}append(...nodes){this.children.push(...nodes);}
  closest(){return null;}focus(){}select(){}showModal(){this.open=true;}close(){if(this.open){this.open=false;this.events.close?.({});}}
}
function harness({now=1800000000000,supported=true}={}){
  const elements=new Map(),requests=[],timers=new Map();let serial=0,clock=now;
  const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const FakeDate=class extends Date{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}};
  const h={element,elements,requests,timers,serverRevision:10,serverItems:[record(10),record(9),record(8)],page:1,total:3,supported};
  h.list=()=>({page:h.page,pageSize:10,total:h.total,hasNext:false,items:h.serverItems,...(h.supported?{cleanupSupported:true}:{})});
  h.defaultHandler=async path=>path==='/api/dashboard'?ok(dashboard(h.serverRevision)):path.startsWith('/api/publications?')?ok(h.list()):path.startsWith('/api/publications/')?ok(record(Number(path.split('/').at(-1)),{isCurrent:Number(path.split('/').at(-1))===h.serverRevision})):ok({});
  h.handler=h.defaultHandler;
  const context={console,Date:FakeDate,AbortController,document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>new Element(),createDocumentFragment:()=>new Element(),activeElement:null},window:{addEventListener(){}},location:{origin:'http://localhost',hostname:'localhost'},navigator:{},crypto:require('node:crypto').webcrypto,confirm:()=>true,
    setInterval(){},setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,at:clock+delay});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(path,options)=>{requests.push({path,options});return h.handler(path,options);}};
  vm.createContext(context);vm.runInContext(source,context);
  h.run=code=>vm.runInContext(code,context);h.assign=(name,value)=>h.run(name+'='+JSON.stringify(value));
  h.assign('state',dashboard());h.assign('historyItems',h.serverItems);h.assign('historyDetail',record(9));h.assign('historyCleanupSupported',supported);
  h.run("csrf='test-csrf';historySelection=9;historyTotal=3;activeView='publications';renderPublicationHistory();renderPublicationHistoryDetail()");
  element('history-keep-latest').value='100';
  h.advance=async ms=>{clock+=ms;for(const [id,timer]of[...timers])if(timer.at<=clock){timers.delete(id);timer.fn();}for(let i=0;i<16;i++)await Promise.resolve();};
  h.open=async(p=preview(),mode={revisions:[9]})=>{h.handler=async(path,options)=>path.endsWith('/cleanup-preview')?ok(p):h.defaultHandler(path,options);return h.run('openHistoryCleanup('+JSON.stringify(mode)+')');};
  h.ack=()=>{element('history-cleanup-ack').checked=true;element('history-cleanup-ack').events.change();};
  h.submit=()=>element('history-cleanup-form').events.submit({preventDefault(){}});
  h.posts=path=>requests.filter(r=>r.options.method==='POST'&&(!path||r.path===path));
  h.deleted=()=>h.posts('/api/publications/cleanup');
  h.dispose=()=>{h.run('resetHistoryCleanup(true)');check.equal(timers.size,0,'all simulated request/expiry timers disposed');};
  harnesses.push(h);return h;
}
(async()=>{
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);check.equal(ids.length,new Set(ids).size,'HTML element IDs unique');
  for(const id of ['history-cleanup-dialog','history-cleanup-form','history-cleanup-batch','history-keep-latest','history-cleanup-versions','history-cleanup-ack','confirm-history-cleanup'])check.equal(ids.includes(id),true);
  for(const phrase of ['value="100"','min="1" max="5000"','不会启用自动删除','数据库空间可先供后续记录复用','应用内不可恢复','不清除电视缓存'])check.equal(html.includes(phrase),true);
  {
    const h=harness({supported:false});check.equal(h.element('history-cleanup-batch').disabled,true);check.equal(h.element('publication-history-detail').innerHTML.includes('data-delete-publication'),false);
    await h.open();check.equal(h.requests.length,0);check.match(h.element('history-cleanup-feedback').textContent,/1\.3\.1/);
    h.supported=true;await h.run('loadPublicationHistory()');check.equal(h.run('historyCleanupSupported'),true);check.equal(h.element('history-cleanup-batch').disabled,false);
    h.supported=false;await h.run('loadPublicationHistory()');check.equal(h.run('historyCleanupSupported'),false);h.dispose();
  }
  {
    const h=harness();await h.open(preview(),{revisions:[10]});await h.open(preview(),{revisions:[100]});check.equal(h.posts().length,0);
    h.assign('historyDetail',record(10));h.run('historySelection=10;renderPublicationHistoryDetail()');check.match(h.element('publication-history-detail').innerHTML,/data-delete-publication="10" disabled/);
    for(const keepLatest of [0,-1,1.5,5001])await h.open(preview(),{keepLatest});check.equal(h.posts().length,0);
    await h.open(preview(),{keepLatest:5000});check.deepEqual(JSON.parse(h.posts().at(-1).options.body),{keepLatest:5000});h.element('history-cleanup-cancel').onclick();
    await h.element('history-cleanup-batch').onclick();check.deepEqual(JSON.parse(h.posts().at(-1).options.body),{keepLatest:100});
    check.equal(h.element('confirm-history-cleanup').disabled,true);h.ack();check.equal(h.element('confirm-history-cleanup').disabled,false);
    h.element('history-cleanup-cancel').onclick();check.equal(h.element('history-cleanup-dialog').open,false);check.equal(h.deleted().length,0);check.equal(h.run('historyCleanup'),null);h.dispose();
  }
  {
    const h=harness(),pending=deferred();h.handler=async()=>pending.promise;
    const opening=h.run('openHistoryCleanup({revisions:[9]})');h.element('history-cleanup-cancel').onclick();pending.resolve(ok(preview()));await opening;
    check.equal(h.run('historyCleanup'),null);check.equal(h.element('history-cleanup-dialog').open,false);check.equal(h.deleted().length,0);h.dispose();
  }
  {
    const h=harness();await h.open(preview({revisions:[],deleteCount:0,totalCandidates:0,confirmationToken:null,expiresAt:null,expiresIn:0}),{keepLatest:100});
    check.match(h.element('history-cleanup-versions').textContent,/不会删除任何记录/);h.ack();await h.submit();check.equal(h.deleted().length,0);check.equal(h.element('confirm-history-cleanup').disabled,true);h.dispose();
  }
  {
    const h=harness();await h.open(preview());check.match(h.element('history-cleanup-summary').textContent,/关联 0 条/);check.match(h.element('history-cleanup-summary').textContent,/预览时生效/);
    check.equal(h.element('history-cleanup-versions').textContent,'v9');check.equal(h.posts().at(-1).options.headers['X-CSRF-Token'],'test-csrf');
    check.deepEqual(JSON.parse(h.posts().at(-1).options.body),{revisions:[9]});check.equal([...h.elements.values()].some(e=>(e.textContent+e.innerHTML).includes('synthetic-frozen-token')),false);
    await h.submit();check.equal(h.deleted().length,0,'acknowledgement required');
    h.ack();h.handler=async(path,options)=>{if(path.endsWith('/cleanup')){h.serverItems=[record(10),record(8)];h.total=2;return ok(result());}return h.defaultHandler(path,options);};
    await h.submit();check.equal(h.deleted().length,1);check.deepEqual(JSON.parse(h.deleted()[0].options.body),{confirmationToken:'synthetic-frozen-token'});
    check.equal(h.run('historySelection'),10);check.equal(h.run('historyDetail.revision'),10);check.equal(h.run('state.currentPublication.revision'),10);check.equal(h.element('history-cleanup-dialog').open,false);check.match(h.element('history-cleanup-feedback').textContent,/已删除 1 条发布记录及 0 条关联回报/);h.dispose();
  }
  {
    const h=harness();h.serverRevision=400;h.assign('state',dashboard(400));const revisions=Array.from({length:200},(_,i)=>i+1);
    await h.open(preview({revisions,deleteCount:200,totalCandidates:299,hasMore:true,currentRevision:400,targetCount:700}),{keepLatest:100});
    check.equal(h.element('history-cleanup-versions').textContent.split('、').length,200);check.match(h.element('history-cleanup-versions').textContent,/v1、v2/);check.match(h.element('history-cleanup-versions').textContent,/v200$/);
    check.match(h.element('history-cleanup-more').textContent,/299 条候选/);check.match(h.element('history-cleanup-more').textContent,/仅核对以上 200 条/);
    h.ack();h.element('history-keep-latest').value='1';h.serverRevision=401;h.assign('state',dashboard(401));
    const pending=deferred();h.handler=async(path,options)=>path.endsWith('/cleanup')?pending.promise:h.defaultHandler(path,options);
    const confirming=h.submit();await h.submit();const count=h.requests.length;await h.run('automaticRefresh()');await h.run('loadPublicationHistory(2)');await h.run('selectPublication(8)');check.equal(h.requests.length,count,'busy prevents polling/list/detail races');
    check.equal(h.deleted().length,1);check.deepEqual(JSON.parse(h.deleted()[0].options.body),{confirmationToken:'synthetic-frozen-token'});
    let prevented=false;h.element('history-cleanup-dialog').events.cancel({preventDefault(){prevented=true;}});h.element('history-cleanup-cancel').onclick();check.equal(prevented,true);check.equal(h.element('history-cleanup-dialog').open,true);
    h.serverItems=[record(401,{isCurrent:true})];h.total=201;pending.resolve(ok(result({deletedCount:200,targetCount:700,remainingCount:201,currentRevision:401})));await confirming;
    check.equal(h.run('state.revision'),401);check.equal(h.run('historySelection'),401);check.equal(h.posts('/api/publications/cleanup-preview').length,1,'confirmation never recalculates targets');h.dispose();
  }
  for(const offset of [-86400000,86400000]){
    const h=harness({now:1800000000000+offset});await h.open(preview());h.ack();check.equal(h.element('confirm-history-cleanup').disabled,false,'server relative TTL tolerates client clock skew');
    await h.advance(299000);check.equal(h.element('confirm-history-cleanup').disabled,false);await h.advance(1000);check.equal(h.element('confirm-history-cleanup').disabled,true);check.match(h.element('history-cleanup-error').textContent,/已过期/);await h.submit();check.equal(h.deleted().length,0);h.dispose();
  }
  {
    const h=harness();const old=preview();delete old.expiresIn;await h.open(old);h.ack();check.equal(h.element('confirm-history-cleanup').disabled,false,'legacy test response absolute TTL fallback');h.dispose();
  }
  for(const bad of [{revisions:[10]},{revisions:[9,9],deleteCount:2},{deleteCount:2},{targetCount:-1},{revisions:[0]},{expiresIn:301},{expiresIn:-1},{expiresIn:'300'},{confirmationToken:''}]){
    const h=harness();await h.open(preview(bad));check.equal(h.run('historyCleanup'),null);check.match(h.element('history-cleanup-error').textContent,/无法预览/);h.ack();await h.submit();check.equal(h.deleted().length,0);h.dispose();
  }
  for(const status of [400,409]){
    const h=harness();await h.open();h.ack();h.handler=async()=>error(status,'预览过期或版本变更');await h.submit();check.match(h.element('history-cleanup-error').textContent,/重新预览/);check.equal(h.run('historyCleanup'),null);await h.submit();check.equal(h.deleted().length,1);
    h.handler=async()=>ok(preview({confirmationToken:'new-frozen-token'}));await h.element('history-cleanup-reload').onclick();check.equal(h.run('historyCleanup.confirmationToken'),'new-frozen-token');check.equal(h.element('history-cleanup-ack').checked,false);h.dispose();
  }
  for(const fail of [async()=>{const e=new Error('aborted');e.name='AbortError';throw e;},async()=>error(503,'temporary failure')]){
    const h=harness();await h.open();h.ack();h.handler=async(path,options)=>path.endsWith('/cleanup')?fail():h.defaultHandler(path,options);await h.submit();
    check.match(h.element('history-cleanup-error').textContent,/结果尚未确认/);check.match(h.element('history-cleanup-error').textContent,/不会自动重试/);check.equal(h.requests.some(r=>r.path.startsWith('/api/publications?')),true);check.equal(h.deleted().length,1);await h.submit();check.equal(h.deleted().length,1);check.equal(h.element('history-cleanup-dialog').open,true);h.dispose();
  }
  {
    const h=harness();await h.open();h.ack();h.handler=async(path,options)=>path.endsWith('/cleanup')?new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=new Error('synthetic abort');error.name='AbortError';reject(error);})):h.defaultHandler(path,options);
    const confirming=h.submit();await h.advance(15000);await confirming;check.equal(h.deleted().length,1);check.equal(h.deleted()[0].options.signal.aborted,true);check.match(h.element('history-cleanup-error').textContent,/超时/);check.match(h.element('history-cleanup-error').textContent,/尚未确认/);h.dispose();
  }
  {
    const h=harness();await h.open();const older=deferred();h.handler=async()=>older.promise;const reload=h.element('history-cleanup-reload').onclick();
    h.handler=async()=>ok(preview({confirmationToken:'latest-preview-token'}));await h.element('history-cleanup-reload').onclick();older.resolve(ok(preview({confirmationToken:'older-preview-token'})));await reload;
    check.equal(h.run('historyCleanup.confirmationToken'),'latest-preview-token');check.equal(h.element('history-cleanup-ack').checked,false);h.dispose();
  }
  for(const failPath of ['/api/dashboard','/api/publications?']){
    const h=harness();await h.open();h.ack();h.handler=async(path,options)=>path.endsWith('/cleanup')?ok(result()):path.startsWith(failPath)?error(503,'refresh failed'):h.defaultHandler(path,options);
    await h.submit();check.match(h.element('history-cleanup-feedback').textContent,/已删除 1 条/);check.match(h.element('history-cleanup-feedback').textContent,/刷新失败/);check.match(h.element('history-cleanup-feedback').textContent,/无需重复删除/);check.equal(h.deleted().length,1);check.equal(h.run('historyItems.some(x=>x.revision===9)'),false);check.equal(h.element('history-cleanup-dialog').open,false);h.dispose();
  }
  {
    const h=harness();h.run('historyPage=3');await h.open();h.ack();h.page=2;h.serverItems=[record(8)];h.total=11;
    h.handler=async(path,options)=>path.endsWith('/cleanup')?ok(result({deletedCount:0,alreadyDeletedCount:1,remainingCount:11})):h.defaultHandler(path,options);await h.submit();
    check.equal(h.run('historyPage'),2);check.equal(h.run('historySelection'),8);check.equal(h.run('historyDetail.revision'),8);check.match(h.element('history-cleanup-feedback').textContent,/此前已删除/);check.match(h.element('history-page').textContent,/第 2 页/);check.equal(h.run('state.currentPublication.revision'),10);h.dispose();
  }
  {
    const h=harness();await h.open();const oldList=deferred(),oldDetail=deferred();h.handler=async path=>path.startsWith('/api/publications?')?oldList.promise:oldDetail.promise;
    const loading=h.run('loadPublicationHistory()'),selecting=h.run('selectPublication(9)');h.ack();
    h.handler=async(path,options)=>{if(path.endsWith('/cleanup')){h.serverItems=[record(10),record(8)];h.total=2;return ok(result());}return h.defaultHandler(path,options);};await h.submit();
    oldList.resolve(ok({page:1,total:3,hasNext:false,items:[record(10),record(9),record(8)],cleanupSupported:true}));oldDetail.resolve(ok(record(9)));await loading;await selecting;
    check.equal(h.run('historyItems.some(x=>x.revision===9)'),false);check.equal(h.run('historySelection'),10);check.equal(h.run('historyDetail.revision'),10);h.dispose();
  }
  for(const phase of ['preview','confirm']){
    const h=harness();if(phase==='confirm'){await h.open();h.ack();}
    h.handler=async()=>error(401,'login expired');if(phase==='preview')await h.run('openHistoryCleanup({revisions:[9]})');else await h.submit();
    check.equal(h.element('dashboard').hidden,true);check.equal(h.element('history-cleanup-dialog').open,false);check.equal(h.run('historyCleanup'),null);check.equal(h.run('historyItems.length'),0);check.equal(h.element('history-cleanup-feedback').textContent,'');h.dispose();
  }
  for(const phase of ['preview','confirm']){
    const h=harness();if(phase==='confirm'){await h.open();h.ack();}const pending=deferred();h.handler=async()=>pending.promise;
    const running=phase==='preview'?h.run('openHistoryCleanup({revisions:[9]})'):h.submit();h.run('invalidatePublicationSession()');pending.resolve(ok(phase==='preview'?preview():result()));await running;
    check.equal(h.element('history-cleanup-dialog').open,false);check.equal(h.run('historyCleanup'),null);check.equal(h.run('historyItems.length'),0);check.equal(h.element('history-cleanup-feedback').textContent,'');h.dispose();
  }
  {
    const h=harness();h.assign('draft',[{mediaId:'keep-draft',duration:15}]);h.run("dirty=true;tokenDirty=true;tokenBase='unchanged-baseline';policyDirty=true;deletion={name:'other-material'}");await h.open();h.ack();h.handler=async(path,options)=>path.endsWith('/cleanup')?ok(result()):h.defaultHandler(path,options);await h.submit();
    check.equal(h.run('draft[0].mediaId'),'keep-draft');check.equal(h.run('dirty'),true);check.equal(h.run('tokenDirty'),true);check.equal(h.run('tokenBase'),'unchanged-baseline');check.equal(h.run('policyDirty'),true);check.equal(h.run('deletion.name'),'other-material');h.dispose();
  }
  check.equal(harnesses.every(h=>h.timers.size===0),true);
  console.log('PASS: '+assertions+' publication-history cleanup UI assertions; simulated requests, clock and timers only; all harnesses disposed, no file deletion, real server, port or background process.');
})().catch(error=>{console.error(error);process.exitCode=1;});
