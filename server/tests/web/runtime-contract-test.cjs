'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
  constructor(){this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.innerHTML='';this.events={};this.classList={toggle(){},add(){},remove(){}};}
  addEventListener(name,fn){this.events[name]=fn;} closest(){return null;} replaceChildren(){} append(){}
}
const elements=new Map(),timers=new Map(),requests=[];
let timerId=0,handler;
const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const dashboard=()=>({media:[],archivedMedia:[],devices:[],draft:[],published:[],revision:0,displayToken:'SyntheticOnly123',diskFree:2*1024**3,capabilities:{office:true,ffmpeg:true},cachePolicy:{maxBytes:2*1024**3,reserveBytes:256*1024**2},runtime:{queueLimit:20,uploadingCount:1,queuedCount:2,processingCount:1,workerReady:true,storageReserveBytes:1024**3}});
const context={console,AbortController,document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>new Element(),createDocumentFragment:()=>new Element(),activeElement:null},window:{addEventListener(){}},location:{origin:'http://localhost',hostname:'localhost'},navigator:{},crypto:require('node:crypto').webcrypto,confirm:()=>true,setInterval(){},setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);},fetch:async(path,options)=>{requests.push({path,options});return handler(path,options);}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(__dirname+'/../../web/app.js','utf8').replace(/boot\(\);\s*$/,''),context);
const run=code=>vm.runInContext(code,context),setState=value=>run('state='+JSON.stringify(value));
const response=body=>({ok:true,status:200,json:async()=>body});
(async()=>{
  setState(dashboard());run('renderRuntime()');
  assert(element('runtime-note').textContent.includes('正在接收 1 · 等待 2 · 转换 1'));
  run('state.diskFree=100;renderRuntime()');
  assert(element('runtime-note').textContent.includes('暂停接收和转换'));
  run('state.runtime.queuedCount=20;state.runtime.workerReady=false;renderRuntime()');
  assert(element('runtime-note').textContent.includes('任务已满'));
  assert(element('runtime-note').textContent.includes('转换服务暂不可用'));
  run('state.runtime=null;renderRuntime()');assert.equal(element('runtime-note').hidden,true);

  setState({...dashboard(),media:[{id:'upload',name:'接收中.png',kind:'image',status:'uploading',size:0,pages:[]}]});
  run('renderMedia()');let markup=element('media-grid').innerHTML;
  assert(markup.includes('正在接收上传'));
  assert(markup.includes('data-delete="upload" disabled'));
  assert(markup.includes('data-archive="upload" disabled'));
  assert(!markup.includes('data-retry'));
  run("state.media[0].status='error';state.media[0].retryAllowed=false;state.media[0].error='<script>bad</script> 上传中断，请重新上传';renderMedia()");
  markup=element('media-grid').innerHTML;
  assert(markup.includes('&lt;script&gt;bad&lt;/script&gt;'));
  assert(!markup.includes('<script>'));
  assert(!markup.includes('data-delete="upload" disabled'));
  assert(!markup.includes('data-retry'));
  run('state.media[0].retryAllowed=true;renderMedia()');assert(element('media-grid').innerHTML.includes('data-retry="upload"'));
  run("state.media[0].kind='unknown';renderMedia()");assert(element('media-grid').innerHTML.includes('占用以删除预览为准'));
  run("state.devices=[{id:'tv',name:'电视',online:true,status:'loading'}];renderDevices()");assert(element('device-list').innerHTML.includes('正在准备播放'));

  // A hung dashboard must not create more requests at each interval.
  let resolvePending;
  handler=async()=>new Promise(resolve=>resolvePending=resolve);
  const before=requests.length,first=run('automaticRefresh()');
  await run('automaticRefresh()');await run('automaticRefresh()');
  assert.equal(requests.length,before+1);assert.equal(run('automaticRefreshBusy'),true);
  resolvePending(response(dashboard()));await first;
  assert.equal(run('automaticRefreshBusy'),false);
  assert.equal(timers.size,0,'completed request releases its timeout');

  // Timeout aborts the actual fetch and allows the following poll to run.
  handler=async(path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=new Error('aborted');e.name='AbortError';reject(e);}));
  const hung=run('automaticRefresh()');
  const timeout=[...timers.values()].find(x=>x.ms===15000);assert(timeout);
  timeout.fn();await hung;
  assert.equal(requests.at(-1).options.signal.aborted,true);
  assert.equal(run('automaticRefreshBusy'),false);
  assert(element('publication-error').textContent.includes('请求超时'));
  assert.equal(timers.size,0);
  handler=async()=>response(dashboard());await run('automaticRefresh()');
  assert.equal(run('automaticRefreshBusy'),false);
  assert.equal(timers.size,0);
  console.log('PASS: 27 runtime UI assertions; visible incomplete uploads, guarded lifecycle actions, recovery retry eligibility, escaped errors, queue/storage/worker hints, bounded polling and abort timeout recovery. No live service or file deletion.');
})().catch(error=>{console.error(error);process.exitCode=1;});
