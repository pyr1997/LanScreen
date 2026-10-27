'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const kindNames = {image:'图片', video:'视频', pdf:'PDF', ppt:'PPT'};
let csrf = '', initialized = false, state = null, draft = [], dirty = false, filter = 'all', toastTimer, currentPreview, previewIndex = 0, saving = false, refreshRequestId = 0;
let mediaScope = 'active', deletion = null, deletionBusy = false, deletionLoading = false, deletionGeneration = 0, policyDirty = false, policySaving = false;
let tokenDirty = false, tokenSaving = false, tokenStatus = '已保存', tokenGeneration = 0, tokenBase = '', tokenUncertainValue = '';
let sessionGeneration=0, activeView='library', publicationAttempt=null, publicationRefreshError='', currentPublicationDetail=null, publicationDetailRequestId=0;
let historyPage=1, historyTotal=0, historyHasNext=false, historyItems=[], historyBusy=false, historyRequestId=0, historySelection=null, historyDetail=null, historyDetailRequestId=0, historyError='', historyDetailError='';
let historyCleanupSupported=false,historyCleanup=null,historyCleanupMode=null,historyCleanupLoading=false,historyCleanupBusy=false,historyCleanupGeneration=0,historyCleanupTimer;
let historyCleanupFeedback='';
const cleanupPending = new Set();
const deviceOperations = new Map();
let deviceScope='connected',devicePage=1,deviceFeedback={message:'',error:false};
const DEVICE_PAGE_SIZE=12;
let automaticRefreshBusy = false;
const MiB = 1024 * 1024;
const DISPLAY_TOKEN_CHARACTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-';
const DISPLAY_TOKEN_PATTERN = /^[A-Za-z0-9._~-]+$/;
const allMedia = () => [...(state?.media || []), ...(state?.archivedMedia || [])];
function toast(message, error=false) { $('toast').textContent = message; $('toast').classList.toggle('error',error); $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,6000); }
function bytes(size) { if(size===null||size===undefined||!Number.isFinite(Number(size))||Number(size)<0) return '未回报'; if(size>=1073741824) return (size/1073741824).toFixed(1)+' GiB'; if(size>=1048576) return (size/1048576).toFixed(1)+' MiB'; if(size===0)return '0 B'; return Math.ceil(size/1024)+' KiB'; }
function invalidatePublicationSession(){
  ++sessionGeneration;++refreshRequestId;++publicationDetailRequestId;++historyRequestId;++historyDetailRequestId;
  publicationAttempt=null;publicationRefreshError='';currentPublicationDetail=null;saving=false;$('publish').disabled=false;$('save').disabled=false;
  historyItems=[];historySelection=null;historyDetail=null;historyTotal=0;historyPage=1;historyHasNext=false;historyBusy=false;historyError='';historyDetailError='';
  historyCleanupSupported=false;historyCleanupFeedback='';resetHistoryCleanup(true);
  deviceOperations.clear();cleanupPending.clear();deviceScope='connected';devicePage=1;deviceFeedback={message:'',error:false};
  if(state){state.currentPublication=null;state.devices=[];}
  renderDevices();
  renderPublicationProgress();renderPublicationHistory();renderPublicationHistoryDetail();
}
async function api(path, options={}) {
  const generation=sessionGeneration;
  let response,body;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
  try{response=await fetch(path,{...options,signal:controller.signal,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});body=await response.json();}
  catch(error){if(error.name==='AbortError')error=new Error('请求超时，请核对状态后重试。');if(generation!==sessionGeneration){error.status=401;error.stale=true;}throw error;}
  finally{clearTimeout(timeout);}
  if(generation!==sessionGeneration){const error=new Error('登录状态已改变，请重新读取。');error.status=401;error.stale=true;throw error;}
  if(!response.ok){
    if(response.status===401&&path!=='/api/login'){invalidatePublicationSession();clearDisplayToken();$('dashboard').hidden=true;$('auth').hidden=false;}
    const error=new Error(body.error||'请求失败，请稍后重试。');error.status=response.status;throw error;
  }
  return body;
}
async function boot() { try { const s=await api('/api/session'); csrf=s.csrf; initialized=s.initialized; if(s.loggedIn) await enter(); else showAuth(); } catch(e) { showAuth(); $('auth-error').textContent='暂时无法连接服务，请检查启动窗口。'; } }
function showAuth(){ $('auth').hidden=false; $('dashboard').hidden=true; $('setup-label').hidden=initialized; $('auth-title').textContent=initialized?'欢迎回来':'设置你的宣传屏'; $('auth-description').textContent=initialized?'登录后管理素材与电视播放清单。':'输入服务启动时显示的代码，创建管理员密码。'; $('auth-submit').textContent=initialized?'登录管理中心':'创建管理员并进入'; $('password').minLength=initialized?1:10; $('password').autocomplete=initialized?'current-password':'new-password'; }
$('auth-form').addEventListener('submit',async e=>{ e.preventDefault(); $('auth-error').textContent=''; $('auth-submit').disabled=true; try { const s=await api(initialized?'/api/login':'/api/setup',{method:'POST',body:JSON.stringify({password:$('password').value,setupCode:$('setup-code').value.trim().toUpperCase()})}); csrf=s.csrf; initialized=true; $('password').value=''; await enter(); } catch(err) { $('auth-error').textContent=err.message; } finally { $('auth-submit').disabled=false; } });
async function enter(){ const generation=sessionGeneration;await refresh(true);if(generation!==sessionGeneration)return; $('auth').hidden=true; $('dashboard').hidden=false; }
async function refresh(initial=false){
  const requestId=++refreshRequestId,generation=tokenGeneration,next=await api('/api/dashboard');
  if(requestId!==refreshRequestId)return false;
  if(generation!==tokenGeneration&&state?.displayToken!==undefined)next.displayToken=state.displayToken;
  const inputValue=$('display-token').value,reconciled=!!tokenUncertainValue&&inputValue===tokenUncertainValue&&inputValue===String(next.displayToken??'');
  state=next;
  if(reconciled){tokenBase=inputValue;tokenDirty=false;tokenUncertainValue='';tokenStatus='已重新核对，密钥已生效';tokenGeneration+=1;}
  if(initial||!dirty)draft=state.draft.map(x=>({...x}));
  publicationRefreshError='';
  render();
  await refreshPublicationDetail();
  return true;
}
function render(){ $('media-count').textContent=state.media.length; const pageCount=state.published.reduce((n,x)=>n+(state.media.find(m=>m.id===x.mediaId)?.pages.length||0),0); $('page-count').textContent=pageCount; $('publish-label').textContent=state.revision?'发布版本 '+state.revision:'尚未发布清单'; const authorizedDevices=state.devices.filter(d=>d.bindingState==='active');$('online-count').innerHTML=authorizedDevices.filter(d=>d.online).length+' <em>/ '+authorizedDevices.length+'</em>'; $('disk-free').textContent=bytes(state.diskFree); renderTokenEditor(); $('server-address').textContent=location.origin; $('address-note').textContent=['127.0.0.1','localhost'].includes(location.hostname)?'当前地址仅供本机预览。电视请填写 NAS 或 Mac 的局域网 IP，例如 http://192.168.1.10:8765。':'请在电视端原样填写此地址；建议给服务端设置固定局域网 IP。'; let notes=[]; if(!state.capabilities.office) notes.push('PPT 转换需要安装 LibreOffice，也可先上传 PDF。'); if(!state.capabilities.ffmpeg) notes.push('当前视频请使用 MP4；其他格式需要 FFmpeg。'); $('capability-note').textContent=notes.join(' '); $('capability-note').hidden=notes.length===0; renderRuntime(); renderMedia(); if(!document.activeElement?.closest('.playlist')) renderPlaylist(); renderDevices(); renderCachePolicy(); renderPublicationProgress(); }
function renderRuntime(){
  const runtime=state?.runtime,note=$('runtime-note');
  if(!runtime){note.hidden=true;note.textContent='';return;}
  const active=Number(runtime.uploadingCount||0)+Number(runtime.queuedCount||0)+Number(runtime.processingCount||0);
  const parts=['正在接收 '+Number(runtime.uploadingCount||0)+' · 等待 '+Number(runtime.queuedCount||0)+' · 转换 '+Number(runtime.processingCount||0)+' / 最多 '+Number(runtime.queueLimit||20)+' 个任务'];
  const lowSpace=Number.isFinite(state.diskFree)&&state.diskFree<Number(runtime.storageReserveBytes||0);
  if(lowSpace)parts.push('可用空间低于 '+bytes(runtime.storageReserveBytes)+' 保留线，暂停接收和转换新素材。请核对并删除不再需要的素材后重试。');
  if(runtime.workerReady===false)parts.push('转换服务暂不可用，请检查本项目容器日志。');
  if(active>=Number(runtime.queueLimit||20))parts.push('任务已满，请等待处理完成后再上传或重试。');
  note.textContent=parts.join('；');note.hidden=false;note.classList.toggle('error-text',lowSpace||runtime.workerReady===false);
}
function renderMedia(){
  const search=$('search').value.trim().toLowerCase();
  const source=mediaScope==='archived'?(state.archivedMedia||[]):state.media;
  $('active-media-count').textContent=state.media.length;
  $('archived-media-count').textContent=(state.archivedMedia||[]).length;
  $('archive-note').hidden=mediaScope!=='archived';
  const rows=source.filter(m=>(filter==='all'||m.kind===filter||(filter==='document'&&['pdf','ppt'].includes(m.kind)))&&m.name.toLowerCase().includes(search));
  if(!rows.length){
    const title=source.length?'没有找到匹配的素材':mediaScope==='archived'?'暂无归档素材':'从第一份素材开始';
    const description=source.length?'试试其他名称或素材类型。':mediaScope==='archived'?'归档的素材会显示在这里，原文件会保留。':'上传公司介绍、活动海报或视频，组成你的播放清单。';
    $('media-grid').innerHTML='<div class="empty"><span class="empty-mark">▧</span><strong>'+title+'</strong>'+description+'</div>';return;
  }
  $('media-grid').innerHTML=rows.map(m=>{
    const ready=m.status==='ready', archived=mediaScope==='archived', busy=['uploading','queued','processing','deleting'].includes(m.status), id=esc(m.id), kind=esc(kindNames[m.kind]||'素材');
    const thumb=ready&&m.kind!=='video'?'<img loading="lazy" src="'+esc(m.pages[0]?.url)+'" alt="'+esc(m.name)+'">':'<span class="file-glyph">'+(m.kind==='video'?'▷':kind)+'</span>';
    const status=ready?(m.kind==='video'?'播放至结束':m.pages.length+' 页'):({uploading:'正在接收上传',queued:'等待处理',processing:'正在转换',error:'处理失败',delete_error:'删除未完成',deleting:'正在删除'})[m.status]||'暂不可用';
    const action=archived?'<span class="archived-tag">已归档 · 文件保留</span>':ready?'<button class="add-button" data-add="'+id+'">＋ 加入清单</button>':m.status==='error'&&m.retryAllowed!==false?'<button class="text-button" data-retry="'+id+'">重试转换</button>':'<span class="meta">'+esc(status)+'</span>';
    return '<article class="media-card"><button class="thumbnail" data-preview="'+id+'" '+(!ready?'disabled':'')+' aria-label="预览 '+esc(m.name)+'">'+thumb+'<span class="kind-tag">'+kind+'</span></button><div class="media-info"><h3 title="'+esc(m.name)+'">'+esc(m.name)+'</h3><div class="meta">'+(m.kind==='unknown'?'占用以删除预览为准':bytes(m.size))+' · '+esc(status)+'</div>'+(m.error?'<p class="card-error">'+esc(m.error)+'</p>':'')+'<div class="media-actions">'+action+(!archived?'<button class="archive-button" data-archive="'+id+'" '+(busy?'disabled':'')+'>归档</button>':'')+'</div><div class="media-lifecycle"><button class="delete-link" data-delete="'+id+'" '+(busy?'disabled':'')+'>彻底删除</button></div></div></article>';
  }).join('');
}
function markDirty(){dirty=true; $('draft-state').textContent='未保存'; $('draft-state').classList.add('dirty');}
function renderPlaylist(){ $('draft-state').textContent=dirty?'未保存':'已保存'; $('draft-state').classList.toggle('dirty',dirty); $('playlist-summary').textContent=draft.length+' 个素材'; $('playlist').innerHTML=draft.length?draft.map((entry,i)=>{const m=state.media.find(x=>x.id===entry.mediaId);return '<div class="playlist-item"><span class="order">'+String(i+1).padStart(2,'0')+'</span><div><span class="entry-name" title="'+esc(m?.name)+'">'+esc(m?.name||'素材不可用')+'</span><div class="entry-bottom">'+(m?.kind==='video'?'<span>播放至结束</span>':'<input type="number" min="1" max="3600" step="1" value="'+entry.duration+'" data-duration="'+i+'" aria-label="'+esc(m?.name)+' 每页停留秒数"><span>秒 / 页</span>')+'<div class="entry-actions"><button data-up="'+i+'" aria-label="上移" '+(i===0?'disabled':'')+'>↑</button><button data-down="'+i+'" aria-label="下移" '+(i===draft.length-1?'disabled':'')+'>↓</button><button data-remove="'+i+'" aria-label="从清单移除">×</button></div></div></div></div>';}).join(''):'<div class="playlist-empty">清单还是空的<br>点击素材上的“加入清单”</div>'; }
async function save(){ if(saving)throw new Error('正在保存，请稍候。'); const snapshot=JSON.stringify(draft); saving=true; $('save').disabled=true; $('publish').disabled=true; try { await api('/api/playlist',{method:'PUT',body:JSON.stringify({items:JSON.parse(snapshot)})});dirty=JSON.stringify(draft)!==snapshot;renderPlaylist(); } finally {saving=false;$('save').disabled=false;$('publish').disabled=false;} }
$('save').onclick=async()=>{try{await save();toast('草稿已保存，电视仍播放已发布内容。');}catch(e){toast(e.message,true);}};
$('publish').onclick=async()=>{
  if(saving)return;
  if(!draft.length&&!confirm('发布空清单后，电视将显示等待内容页面。确定继续吗？'))return;
  const snapshot=JSON.stringify(draft),generation=sessionGeneration;
  saving=true;publicationAttempt={status:'submitting'};publicationRefreshError='';
  $('publish').disabled=true;$('save').disabled=true;renderPublicationProgress();
  try{
    const result=await api('/api/publish',{method:'POST',body:JSON.stringify({items:JSON.parse(snapshot)})});
    ++refreshRequestId;++publicationDetailRequestId;
    dirty=JSON.stringify(draft)!==snapshot;
    publicationAttempt={status:'published',revision:result.revision};
    state.revision=result.revision;
    state.currentPublication=result.publication||{revision:result.revision,targetCount:null,appliedCount:0,itemCount:JSON.parse(snapshot).length,isCurrent:true,source:'publish'};
    currentPublicationDetail=null;renderPublicationProgress();renderPlaylist();
    toast('服务器已发布 '+revisionLabel(result.revision)+'，请以电视确认进度为准。');
    try{await refresh();if(activeView==='publications')await loadPublicationHistory(1);}
    catch(error){if(error.status===401)return;publicationRefreshError='服务器已发布 '+revisionLabel(result.revision)+'，但状态刷新失败：'+error.message+' 请刷新进度核对，无需重复发布。';renderPublicationProgress();}
  }catch(error){
    if(error.status===401)return;
    publicationAttempt={status:error.status&&error.status<500?'rejected':'uncertain',message:error.message};
    renderPublicationProgress();
    toast(publicationAttempt.status==='uncertain'?'提交结果暂未确认，请查看发布记录核对，避免重复发布。':error.message,true);
  }finally{if(generation===sessionGeneration){saving=false;$('publish').disabled=false;$('save').disabled=false;}}
};
$('playlist').addEventListener('input',e=>{if(e.target.dataset.duration!==undefined){const value=Number(e.target.value);if(Number.isInteger(value)&&value>=1&&value<=3600){draft[Number(e.target.dataset.duration)].duration=value;markDirty();}}});
$('playlist').addEventListener('change',e=>{if(e.target.dataset.duration!==undefined){const value=Number(e.target.value);if(!Number.isInteger(value)||value<1||value>3600){toast('每页停留时间为 1～3600 秒的整数。',true);renderPlaylist();return;}draft[Number(e.target.dataset.duration)].duration=value;markDirty();}});
$('playlist').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.remove!==undefined)draft.splice(Number(b.dataset.remove),1);else if(b.dataset.up!==undefined){const i=Number(b.dataset.up);[draft[i-1],draft[i]]=[draft[i],draft[i-1]];}else if(b.dataset.down!==undefined){const i=Number(b.dataset.down);[draft[i+1],draft[i]]=[draft[i],draft[i+1]];}else return;markDirty();renderPlaylist();});
$('media-grid').addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  try{
    if(b.dataset.add){draft.push({mediaId:b.dataset.add,duration:10});markDirty();renderPlaylist();toast('已加入清单，发布后电视才会更新。');}
    else if(b.dataset.preview){currentPreview=allMedia().find(m=>m.id===b.dataset.preview);if(!currentPreview?.pages.length)return;previewIndex=0;showPreview();$('preview-dialog').showModal();}
    else if(b.dataset.retry){await api('/api/media/'+encodeURIComponent(b.dataset.retry)+'/retry',{method:'POST',body:'{}'});await refresh();}
    else if(b.dataset.archive){if(draft.some(x=>x.mediaId===b.dataset.archive))throw new Error('请先从轮播清单移除该素材，保存并发布后再归档。');if(!confirm('归档后将从素材库隐藏，原文件保留。确定归档吗？'))return;await api('/api/media/'+encodeURIComponent(b.dataset.archive)+'/archive',{method:'POST',body:'{}'});await refresh();toast('素材已归档，可在“已归档”中查看。原文件已保留。');}
    else if(b.dataset.delete){await openDeletion(b.dataset.delete);}
  }catch(err){toast(err.message,true);}
});
function updateDeleteButton(){
  $('confirm-delete').disabled=deletionBusy||deletionLoading||!deletion?.allowed||$('delete-confirm-name').value!==deletion.name;
}
async function loadDeletion(id){
  const generation=++deletionGeneration;
  deletionLoading=true;deletion=null;$('delete-confirm-name').value='';
  $('delete-error').textContent='';$('delete-blocked').hidden=true;$('delete-file-area').hidden=true;$('delete-confirm-label').hidden=true;
  $('delete-files').replaceChildren();$('delete-summary').textContent='正在核对文件…';$('reload-delete').disabled=true;updateDeleteButton();
  try{
    const result=await api('/api/media/'+encodeURIComponent(id)+'/deletion-preview');
    if(generation!==deletionGeneration||!$('delete-dialog').open)return;
    deletion=result;
    if(draft.some(x=>x.mediaId===id))deletion={...result,allowed:false,reason:'当前轮播清单仍包含此素材。请先移除，保存并发布后再删除。'};
    $('delete-name').textContent=deletion.name;
    $('delete-summary').textContent=deletion.allowed||deletion.fileCount?deletion.fileCount+' 个文件 · '+bytes(deletion.totalBytes):'暂不可删除 · 文件清单尚未确认';
    $('delete-blocked').textContent=deletion.reason||'';$('delete-blocked').hidden=!!deletion.allowed;
    const fragment=document.createDocumentFragment();
    for(const file of deletion.files||[]){const li=document.createElement('li'),path=document.createElement('code'),size=document.createElement('span');path.textContent=file.path;size.textContent=bytes(file.bytes);li.append(path,size);fragment.append(li);}
    $('delete-files').replaceChildren(fragment);$('delete-file-area').hidden=!(deletion.files||[]).length;
    $('delete-confirm-label').hidden=!deletion.allowed;
    if(deletion.allowed)$('delete-confirm-name').focus();
  }catch(err){if(generation===deletionGeneration){$('delete-summary').textContent='无法完成文件核对';$('delete-error').textContent=err.message;}}
  finally{if(generation===deletionGeneration){deletionLoading=false;$('reload-delete').disabled=false;updateDeleteButton();}}
}
async function openDeletion(id){
  $('delete-dialog').dataset.mediaId=id;$('delete-confirm-name').value='';$('delete-name').textContent=allMedia().find(m=>m.id===id)?.name||'';
  $('delete-dialog').showModal();await loadDeletion(id);
}
$('delete-confirm-name').addEventListener('input',updateDeleteButton);
$('close-delete').onclick=()=>{if(!deletionBusy)$('delete-dialog').close();};
$('reload-delete').onclick=()=>loadDeletion($('delete-dialog').dataset.mediaId);
$('delete-dialog').addEventListener('cancel',e=>{if(deletionBusy)e.preventDefault();});
$('delete-dialog').addEventListener('close',()=>{deletionGeneration++;deletion=null;deletionLoading=false;$('delete-confirm-name').value='';});
$('delete-form').addEventListener('submit',async e=>{
  e.preventDefault();updateDeleteButton();if($('confirm-delete').disabled)return;
  const target=deletion;deletionBusy=true;$('close-delete').disabled=true;$('reload-delete').disabled=true;$('delete-confirm-name').disabled=true;$('delete-error').textContent='';$('confirm-delete').textContent='正在删除…';updateDeleteButton();
  try{
    await api('/api/media/'+encodeURIComponent(target.id)+'/delete',{method:'POST',body:JSON.stringify({confirmName:$('delete-confirm-name').value,deletionToken:target.deletionToken})});
    $('delete-dialog').close();toast('服务器素材文件已删除。电视缓存需等待各台电视联网并完成清理，请查看设备状态。');
    try{await refresh();}catch(err){toast('删除已完成，但列表刷新失败：'+err.message,true);}
  }catch(err){deletion=null;$('delete-error').textContent='未确认删除完成：'+err.message+' 请重新核对文件后再操作。';}
  finally{deletionBusy=false;$('close-delete').disabled=false;$('reload-delete').disabled=false;$('delete-confirm-name').disabled=false;$('confirm-delete').textContent='彻底删除';updateDeleteButton();}
});
function showPreview(){const page=currentPreview.pages[previewIndex];$('preview-title').textContent=currentPreview.name;$('preview-content').innerHTML=page.type==='video'?'<video controls autoplay src="'+esc(page.url)+'"></video>':'<img src="'+esc(page.url)+'" alt="'+esc(currentPreview.name)+' 第 '+(previewIndex+1)+' 页">';$('preview-page').textContent=(previewIndex+1)+' / '+currentPreview.pages.length;$('prev-page').disabled=previewIndex===0;$('next-page').disabled=previewIndex===currentPreview.pages.length-1;}
$('prev-page').onclick=()=>{previewIndex--;showPreview();};$('next-page').onclick=()=>{previewIndex++;showPreview();};$('close-preview').onclick=()=>$('preview-dialog').close();$('preview-dialog').addEventListener('close',()=>$('preview-content').innerHTML='');
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));renderMedia();});$('search').oninput=renderMedia;
document.querySelectorAll('[data-scope]').forEach(b=>b.onclick=()=>{mediaScope=b.dataset.scope;document.querySelectorAll('[data-scope]').forEach(x=>x.classList.toggle('active',x===b));renderMedia();});
let uploading=false;
async function uploadFiles(files){if(uploading){toast('上一批素材仍在上传，请稍候。');return;}if(!files.length)return;uploading=true;$('upload-status').hidden=false;let succeeded=0;const failures=[];try{for(const file of files){if(file.size>512*1024*1024){failures.push(file.name+' 超过 512 MB');continue;}try{await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/media');xhr.setRequestHeader('X-CSRF-Token',csrf);xhr.timeout=30*60*1000;xhr.upload.onprogress=e=>{$('upload-status').textContent='正在上传 '+file.name+' · '+(e.lengthComputable?Math.round(e.loaded/e.total*100)+'%':'上传中');};xhr.onerror=()=>reject(new Error('网络连接中断'));xhr.ontimeout=()=>reject(new Error('上传超时'));xhr.onload=()=>{let r;try{r=JSON.parse(xhr.responseText);}catch(_){reject(new Error('服务返回异常'));return;}if(xhr.status>=200&&xhr.status<300)resolve(r);else reject(new Error(r.error||'上传失败'));};const form=new FormData();form.append('file',file);xhr.send(form);});succeeded++;}catch(e){failures.push(file.name+'：'+e.message);}finally{try{await refresh();}catch(_){/* Preserve the upload result if the dashboard is temporarily unavailable. */}}}$('upload-status').textContent=succeeded+' 个素材已上传。'+(failures.length?' '+failures.join('；'):' 转换完成后可预览并加入清单。');}finally{uploading=false;$('files').value='';}}
$('files').onchange=e=>uploadFiles(Array.from(e.target.files));$('dropzone').onclick=()=>$('files').click();$('dropzone').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('files').click();}};['dragenter','dragover'].forEach(n=>$('dropzone').addEventListener(n,e=>{e.preventDefault();$('dropzone').classList.add('drag');}));['dragleave','drop'].forEach(n=>$('dropzone').addEventListener(n,e=>{e.preventDefault();$('dropzone').classList.remove('drag');}));$('dropzone').addEventListener('drop',e=>uploadFiles(Array.from(e.dataTransfer.files)));
function deviceCurrent(d){const media=allMedia().find(m=>m.pages.some(p=>p.id===d.current_item));if(!media)return d.current_item||'暂无';const page=media.pages.findIndex(p=>p.id===d.current_item);return media.name+(media.pages.length>1?' · 第 '+(page+1)+' 页':'');}
function revisionLabel(value){return Number.isInteger(value)&&value>=0?'v'+String(value):'未回报';}
function deviceBinding(d){return ['active','legacy','pending','unbound'].includes(d.bindingState)?d.bindingState:'legacy';}
function deviceGroup(d){const binding=deviceBinding(d);return binding==='pending'?'pending':binding==='unbound'?'unbound':'connected';}
function deviceBindingVersion(d){return Number.isSafeInteger(d.bindingVersion)&&d.bindingVersion>=0?d.bindingVersion:null;}
function bindingActionAllowed(d,action){
  if(deviceBindingVersion(d)===null)return false;
  if(action==='approve')return d.bindingState==='pending';
  return action==='unbind'&&['active','legacy','pending'].includes(d.bindingState);
}
function showDeviceFeedback(message,error=false){deviceFeedback={message,error};renderDevices();}
function renderDevices(){
  const devices=state?.devices||[],groups={connected:[],pending:[],unbound:[]};
  devices.forEach(d=>groups[deviceGroup(d)].push(d));
  for(const group of Object.keys(groups)){
    $("device-count-"+group).textContent=groups[group].length;
    $("device-tab-"+group).classList.toggle('active',deviceScope===group);
    $("device-tab-"+group).setAttribute?.('aria-pressed',String(deviceScope===group));
  }
  const selected=groups[deviceScope],pages=Math.max(1,Math.ceil(selected.length/DEVICE_PAGE_SIZE));
  devicePage=Math.max(1,Math.min(devicePage,pages));
  $('device-page').textContent='第 '+devicePage+' / '+pages+' 页 · 共 '+selected.length+' 台';
  $('device-prev').disabled=devicePage<=1;$('device-next').disabled=devicePage>=pages;
  $('device-pagination').hidden=selected.length<=DEVICE_PAGE_SIZE;
  $('device-feedback').textContent=deviceFeedback.message;$('device-feedback').hidden=!deviceFeedback.message;
  $('device-feedback').classList.toggle('error-text',deviceFeedback.error);
  $('device-scope-note').textContent=({connected:'已批准的电视可以同步；旧版设备需安装 1.3.0 后重新申请接入，不能继续使用旧授权。',pending:'请核对电视名称、设备编号和接入批次后批准。待审批设备不能读取播放清单或素材。',unbound:'解绑记录仅供核对。重新接入须在电视上点击“重新申请绑定”，再到待审批列表批准；不会继承旧批次的发布确认。'})[deviceScope];
  $('device-list').innerHTML=selected.length?selected.slice((devicePage-1)*DEVICE_PAGE_SIZE,devicePage*DEVICE_PAGE_SIZE).map(d=>{
    const binding=deviceBinding(d),known=['active','legacy','pending','unbound'].includes(d.bindingState),version=deviceBindingVersion(d);
    const supported=d.cleanupSupported===true,request=Number(d.cleanupRequest||0),completed=Number(d.cleanupCompletedRequest||0),waiting=request>completed;
    const busy=deviceOperations.has(d.id)||cleanupPending.has(d.id);
    const bindingLabel=known?({active:'已批准接入',legacy:'旧版待升级',pending:'待审批',unbound:'已解绑'})[binding]:'已接入 · 旧服务记录';
    const update=binding==='legacy'?'需升级电视应用至 1.3.0 并重新申请接入':d.updateState==='unsupported'?'需升级电视应用至 1.3.0':d.updateState==='offline'?'离线 · 等待连接后核实':d.updateState==='applied'?'已应用最新清单':'等待下载或切换清单';
    const cleanup=({offline:waiting?'离线 · 清理请求等待执行':'离线 · 待连接后核实',unsupported:'需升级电视应用至 1.3.0',pending:'等待电视执行',running:'正在清理',complete:'已完成清理',error:'清理失败',blocked:'受保护素材占用，暂无法完成清理'})[d.cleanupDisplayState||d.cleanupState]||'尚未回报';
    const identity='<p class="device-small device-identity">设备编号：'+esc(d.id)+'<br>接入批次：'+(version===null?'旧记录未提供':version)+'</p>';
    const heading='<div class="device-top"><h3>'+esc(d.name||d.id)+'</h3><span class="binding-badge binding-'+binding+'">'+bindingLabel+'</span></div>'+identity;
    const operation=action=>'<button class="outline '+(action==='unbind'?'unbind-button':'approve-button')+'" data-device-action="'+action+'" data-device-id="'+esc(d.id)+'" data-binding-version="'+esc(version)+'" '+(busy||!bindingActionAllowed(d,action)?'disabled':'')+'>'+((deviceOperations.get(d.id)?.action===action)?'正在提交…':action==='approve'?'批准接入':'解绑电视')+'</button>';
    const actions='<div class="device-binding-actions">'+(binding==='pending'?operation('approve'):'')+(known&&binding!=='unbound'?operation('unbind'):'')+'</div>';
    if(binding==='pending')return '<article class="device-card">'+heading+'<p>等待管理员批准；尚未获得播放清单和素材访问权限。</p><p class="device-small">申请设备应用版本：'+esc(d.version||'未知')+'<br>最近申请 / 连接：'+esc(publicationTime(d.last_seen))+'</p>'+actions+'</article>';
    if(binding==='unbound')return '<article class="device-card">'+heading+'<p>已退出发布目标，服务器拒绝此接入批次的新联网请求。</p><p class="device-small">解绑时间：'+esc(publicationTime(d.unboundAt))+'</p><p class="device-offline-note">离线缓存可能仍在电视上。此状态不代表现场已经停播或擦除缓存。</p><p class="device-small">需要重新接入时，请在电视上点击“重新申请绑定”，再由管理员批准新申请。</p></article>';
    const metrics=supported?'<div class="device-metrics"><div><span>最近回报缓存占用</span><strong>'+bytes(d.cacheBytes)+'</strong></div><div><span>最近回报可用空间</span><strong>'+bytes(d.freeBytes)+'</strong></div></div><p class="device-small">已回报缓存上限 '+bytes(d.cacheLimitBytes)+' · 保留空间 '+bytes(d.cacheReserveBytes)+'</p>':'<p class="device-legacy">旧版应用未回报缓存占用与清理结果。请升级后查看。</p>';
    const stateText=({playing:'正在播放',loading:'正在准备播放',paused:'已暂停',idle:'等待内容',error:'播放异常'})[d.status]||d.status||'未回报';
    return '<article class="device-card">'+heading+'<p><span class="'+(d.online?'online':'offline')+'">'+(d.online?'近期有回报':'离线')+'</span> · 最近回报状态：'+esc(stateText)+'</p><p>最近回报素材：'+esc(deviceCurrent(d))+'</p><div class="device-progress"><strong>'+esc(update)+'</strong><p>服务器已发布 '+esc(revisionLabel(state?.revision))+'</p><p>电视已下载 '+esc(revisionLabel(d.downloadedRevision))+' · 已应用 '+esc(revisionLabel(d.appliedRevision))+'</p></div>'+metrics+'<div class="cleanup-progress"><p><strong>缓存清理：</strong>'+esc(binding==='active'?cleanup:'旧授权不可下发清理；请升级并批准接入后操作')+'</p>'+(supported?'<p class="device-small">最近清理版本 '+esc(revisionLabel(d.cleanupRevision))+(request?' · 请求 '+request+' / 已完成 '+completed:'')+'</p>':'')+(d.cleanupError?'<p class="card-error">'+esc(d.cleanupError)+'</p>':'')+'</div><div class="device-footer"><p class="device-small">最近连接：'+esc(publicationTime(d.last_seen))+'<br>应用版本：'+esc(d.version||'未知')+'</p><button class="outline cleanup-button" data-cleanup="'+esc(d.id)+'" data-binding-version="'+esc(version)+'" '+(!supported||binding!=='active'||!known||version===null||busy?'disabled':'')+'>清理未使用缓存</button></div>'+actions+(!known?'<p class="device-offline-note">旧服务未提供绑定信息，本页仅兼容显示。升级服务端和电视后才能操作审批或解绑。</p>':binding==='legacy'?'<p class="device-offline-note">以上旧回报不代表仍有同步权限。电视需升级至 1.3.0 并申请绑定。</p>':!d.online?'<p class="device-offline-note">以上为最近一次回报。电视离线时可能继续播放旧内容。</p>':'')+'</article>';
  }).join(''):'<div class="empty"><span class="empty-mark">▣</span><strong>'+({connected:'暂无已接入电视',pending:'暂无待审批申请',unbound:'暂无解绑记录'})[deviceScope]+'</strong>'+({connected:'在电视 1.3.0 中填写连接信息并申请，管理员批准后显示在这里。',pending:'电视申请接入后会出现在这里，请核对后批准。',unbound:'解绑操作会保留记录，方便核对历史发布情况。'})[deviceScope]+'</div>';
}
async function changeDeviceBinding(action,id,expectedVersion){
  const device=state?.devices?.find(d=>d.id===id),generation=sessionGeneration;
  if(!device||deviceOperations.has(id)||cleanupPending.has(id)||!bindingActionAllowed(device,action))return;
  if(deviceBindingVersion(device)!==expectedVersion){
    showDeviceFeedback('设备状态已变化，请刷新后核对接入批次再操作。',true);
    try{await refresh();}catch(error){if(error.status!==401)showDeviceFeedback('设备状态刷新失败：'+error.message,true);}return;
  }
  const description='“'+(device.name||id)+'”（设备编号 '+id+'，接入批次 '+expectedVersion+'）';
  const warning=action==='unbind'?'确认解绑 '+description+'？此接入批次将退出发布目标，服务器将拒绝它的新联网请求。离线电视已有缓存无法立即停播或擦除。解绑不删除 NAS 素材；再次接入须由电视重新申请，并由管理员批准。':'确认批准 '+description+' 接入？批准后该电视可同步播放清单和素材。请先确认是需要接入的公司电视。';
  if(!confirm(warning))return;
  const operation={action,generation};deviceOperations.set(id,operation);deviceFeedback={message:'正在提交设备操作…',error:false};renderDevices();
  let accepted=false;
  try{
    await api('/api/devices/'+encodeURIComponent(id)+'/'+action,{method:'POST',body:JSON.stringify({bindingVersion:expectedVersion})});
    if(generation!==sessionGeneration)return;
    accepted=true;++refreshRequestId;++publicationDetailRequestId;++historyDetailRequestId;++historyRequestId;
    currentPublicationDetail=null;historyDetail=null;
    await refresh();
    if(generation!==sessionGeneration)return;
    showDeviceFeedback(action==='unbind'?'服务器已解绑该接入批次，并更新发布目标。离线电视是否停播、缓存是否已清除仍需现场核实。':'服务器已批准接入，等待电视联网同步。',false);
    if(activeView==='publications')await loadPublicationHistory(historyPage,{quiet:true});
  }catch(error){
    if(error.status===401||generation!==sessionGeneration)return;
    if(accepted)showDeviceFeedback('服务器已接受'+(action==='unbind'?'解绑':'批准')+'，但页面状态刷新失败：'+error.message+' 请刷新核对，无需重复提交。',true);
    else if(error.status===409){
      showDeviceFeedback('设备状态或接入批次已变化，本次操作未执行。正在刷新，请核对后再操作。',true);
      try{await refresh();}catch(refreshError){if(refreshError.status===401)return;showDeviceFeedback('设备状态已变化，刷新失败：'+refreshError.message+' 请稍后刷新核对。',true);}
    }else{
      showDeviceFeedback(error.status&&error.status<500?'设备操作未通过：'+error.message:'提交结果尚未确认：'+error.message+' 请刷新设备状态核对，避免重复提交。',true);
    }
  }finally{if(deviceOperations.get(id)===operation){deviceOperations.delete(id);renderDevices();}}
}
$('device-list').addEventListener('click',async e=>{
  const actionButton=e.target.closest('[data-device-action]');
  if(actionButton){if(!actionButton.disabled)await changeDeviceBinding(actionButton.dataset.deviceAction,actionButton.dataset.deviceId,Number(actionButton.dataset.bindingVersion));return;}
  const b=e.target.closest('[data-cleanup]');if(!b||b.disabled)return;
  const id=b.dataset.cleanup,device=state?.devices?.find(d=>d.id===id),generation=sessionGeneration;
  if(!device||device.bindingState!=='active'||!device.cleanupSupported||deviceBindingVersion(device)===null||deviceBindingVersion(device)!==Number(b.dataset.bindingVersion)||cleanupPending.has(id)||deviceOperations.has(id))return;
  if(!confirm('向“'+device.name+'”发送清理请求？仅清理该应用不再使用的缓存，保留当前清单与正在准备的新清单。离线电视会在重新连接后执行。'))return;
  cleanupPending.add(id);renderDevices();
  try{await api('/api/devices/'+encodeURIComponent(id)+'/cleanup',{method:'POST',body:'{}'});if(generation!==sessionGeneration)return;toast('清理请求已提交，尚不代表清理完成。请以电视回报的结果为准。');await refresh();}
  catch(err){if(err.status!==401&&generation===sessionGeneration)toast(err.message,true);}finally{if(generation===sessionGeneration){cleanupPending.delete(id);renderDevices();}}
});
for(const scope of ['connected','pending','unbound'])$('device-tab-'+scope).onclick=()=>{deviceScope=scope;devicePage=1;renderDevices();};
$('device-prev').onclick=()=>{devicePage--;renderDevices();};$('device-next').onclick=()=>{devicePage++;renderDevices();};
function renderCachePolicy(){
  if(!policyDirty&&!policySaving){$('cache-max').value=(state.cachePolicy?.maxBytes??2147483648)/MiB;$('cache-reserve').value=(state.cachePolicy?.reserveBytes??268435456)/MiB;$('cache-policy-state').textContent='已保存的设置';}
}
$('cache-policy-form').addEventListener('input',()=>{policyDirty=true;$('cache-policy-state').textContent='设置尚未保存';$('cache-policy-error').textContent='';});
$('cache-policy-form').addEventListener('submit',async e=>{
  e.preventDefault();if(policySaving)return;
  const max=Number($('cache-max').value),reserve=Number($('cache-reserve').value);
  if(!Number.isInteger(max)||max<256||max>32768||!Number.isInteger(reserve)||reserve<128||reserve>4096){$('cache-policy-error').textContent='请填写范围内的整数：缓存上限 256～32768 MiB，保留空间 128～4096 MiB。';return;}
  policySaving=true;$('save-cache-policy').disabled=true;$('cache-policy-error').textContent='';$('cache-policy-state').textContent='正在保存…';
  const snapshot=JSON.stringify([max,reserve]);
  try{await api('/api/cache-policy',{method:'PUT',body:JSON.stringify({maxBytes:max*MiB,reserveBytes:reserve*MiB})});policyDirty=JSON.stringify([Number($('cache-max').value),Number($('cache-reserve').value)])!==snapshot;await refresh();$('cache-policy-state').textContent=policyDirty?'还有尚未保存的修改':'设置已保存，等待电视联网同步';toast('缓存设置已保存，电视联网同步后生效。');}
  catch(err){$('cache-policy-error').textContent=err.message;$('cache-policy-state').textContent='保存未完成';}
  finally{policySaving=false;$('save-cache-policy').disabled=false;}
});
function displayTokenError(value){
  if(value.length<12||value.length>128)return '播放密钥需为 12～128 个字符。';
  if(!DISPLAY_TOKEN_PATTERN.test(value))return '播放密钥仅可使用大小写字母、数字和 ._~-。';
  return '';
}
function renderTokenEditor(){
  const input=$('display-token');
  if(!tokenDirty&&!tokenSaving){input.value=String(state?.displayToken??'');tokenBase=input.value;}
  input.disabled=tokenSaving;
  $('generate-token').disabled=tokenSaving;
  $('save-token').disabled=tokenSaving||!tokenDirty;
  $('save-token').textContent=tokenSaving?'正在保存…':'保存播放密钥';
  $('token-state').textContent=tokenSaving?'正在保存…':tokenStatus;
  $('token-state').classList.toggle('dirty',tokenDirty||tokenSaving);
}
function markTokenDirty(){
  if($('display-token').value!==tokenUncertainValue)tokenUncertainValue='';
  tokenDirty=$('display-token').value!==tokenBase;
  tokenStatus=tokenDirty?'设置尚未保存':'已保存';
  $('token-error').textContent='';
  renderTokenEditor();
}
function clearDisplayToken(){
  tokenGeneration+=1;
  tokenBase='';
  tokenDirty=false;
  tokenSaving=false;
  tokenStatus='已保存';
  tokenUncertainValue='';
  if(state)state.displayToken='';
  const input=$('display-token');
  input.value='';
  input.type='password';
  input.disabled=false;
  $('show-token').textContent='显示';
  $('token-error').textContent='';
  renderTokenEditor();
}
function secureDisplayToken(length=32){
  if(!globalThis.crypto?.getRandomValues)throw new Error('当前浏览器不支持安全随机数，请手动填写密钥。');
  const ceiling=256-(256%DISPLAY_TOKEN_CHARACTERS.length);
  let value='';
  while(value.length<length){
    const bytes=new Uint8Array(Math.max(32,(length-value.length)*2));
    globalThis.crypto.getRandomValues(bytes);
    for(const byte of bytes){
      if(byte<ceiling)value+=DISPLAY_TOKEN_CHARACTERS[byte%DISPLAY_TOKEN_CHARACTERS.length];
      if(value.length===length)break;
    }
  }
  return value;
}
$('display-token').addEventListener('input',markTokenDirty);
$('generate-token').onclick=()=>{
  try{$('display-token').value=secureDisplayToken();markTokenDirty();toast('已生成新密钥，保存后才会生效。');}
  catch(err){$('token-error').textContent=err.message;}
};
$('token-form').addEventListener('submit',async e=>{
  e.preventDefault();
  if(tokenSaving)return;
  const snapshot=$('display-token').value;
  const problem=displayTokenError(snapshot);
  if(problem){$('token-error').textContent=problem;tokenStatus='设置尚未保存';renderTokenEditor();return;}
  if(snapshot===tokenBase){tokenDirty=false;tokenStatus='已保存';renderTokenEditor();return;}
  if(!confirm('保存后，当前服务实例会立即停止接受旧播放密钥发起的新请求，每台电视都需重新填写新密钥。仅在服务端更换密钥不会撤回电视已有的离线缓存；电视填写并保存新密钥后会重新同步，期间可能中断播放。请在网络稳定时逐台更换。确定保存吗？'))return;
  tokenSaving=true;tokenUncertainValue='';tokenStatus='正在保存…';$('token-error').textContent='';renderTokenEditor();
  try{
    const result=await api('/api/display-token',{method:'PUT',body:JSON.stringify({displayToken:snapshot,currentDisplayToken:tokenBase})});
    tokenGeneration+=1;
    state.displayToken=snapshot;
    tokenBase=snapshot;
    tokenDirty=false;
    tokenUncertainValue='';
    tokenStatus=result.changed===false?'已保存，密钥未变化':'已保存，新密钥已生效';
    toast(result.changed===false?'播放密钥未变化。':'播放密钥已保存，请在每台电视上重新填写。');
  }catch(err){
    if(err.status===401)return;
    if(tokenBase===snapshot&&state?.displayToken===snapshot){tokenDirty=false;tokenUncertainValue='';tokenStatus='已重新核对，密钥已生效';return;}
    if(err.status===409){
      const conflictMessage=err.message;
      tokenUncertainValue='';
      try{
        const latest=await api('/api/dashboard');
        state=latest;
        if(!dirty)draft=state.draft.map(x=>({...x}));
        tokenBase=String(latest.displayToken??'');
        $('display-token').value=snapshot;
        tokenDirty=snapshot!==tokenBase;
        tokenStatus=tokenDirty?'已取得最新密钥，请重新确认保存':'已重新核对，密钥已生效';
        $('token-error').textContent=conflictMessage;
        tokenGeneration+=1;
        render();
      }catch(refreshErr){
        if(refreshErr.status===401)return;
        tokenDirty=true;
        tokenStatus='保存冲突，取得最新密钥失败';
        $('token-error').textContent=conflictMessage+' '+refreshErr.message;
      }
      return;
    }
    tokenDirty=true;tokenUncertainValue=snapshot;tokenStatus='保存未完成';$('token-error').textContent=err.message;
  }
  finally{tokenSaving=false;renderTokenEditor();}
});
function publicationTime(value){
  if(value===null||value===undefined||!Number.isFinite(Number(value))||Number(value)<=0)return '未回报';
  return new Date(Number(value)*1000).toLocaleString('zh-CN');
}
function publicationSource(value){return ({publish:'发布清单',cache_policy:'缓存设置更新',migrated:'升级前清单导入'})[value]||'清单版本';}
function publicationCreated(record){return record.createdAt===null?'发布时间未知 · 升级前清单':publicationTime(record.createdAt);}
function publicationRatio(record){
  const total=Number(record?.targetCount),applied=Number(record?.appliedCount);
  if(!Number.isInteger(total)||total<=0||!Number.isInteger(applied)||applied<0)return {total:0,applied:0,percent:null};
  return {total,applied:Math.min(applied,total),percent:Math.floor(Math.min(applied,total)/total*100)};
}
function publicationWithdrawn(record){
  const count=record?.withdrawnCount??record?.counts?.withdrawn;
  if(Number.isInteger(count)&&count>=0)return count;
  if(Number.isInteger(record?.recordedTargetCount)&&Number.isInteger(record?.targetCount))return Math.max(0,record.recordedTargetCount-record.targetCount);
  return 0;
}
function publicationWithdrawalText(record){const count=publicationWithdrawn(record);return count?'另 '+count+' 条电视接入记录已退出，不计入确认分母':'';}
function publicationState(record){
  if(!record)return '尚未发布清单';
  if(record.supersededByRevision!==null&&record.supersededByRevision!==undefined)return '已被 '+revisionLabel(record.supersededByRevision)+' 取代；未确认的电视不计为完成';
  const ratio=publicationRatio(record);
  if(!ratio.total)return publicationWithdrawn(record)?'原目标电视均已退出，当前暂无可确认目标':'服务器已发布，暂无电视可确认';
  if(record.offlineCount>0||record.counts?.offline>0)return '等待离线电视回报；请核实各台电视的当前状态';
  if(record.counts?.unsupported>0)return '部分电视未回报版本确认，需升级电视应用';
  if(ratio.applied===ratio.total)return '目标电视均已确认应用清单；当前播放情况请到电视现场核实';
  return '服务器已发布，等待电视下载并确认应用清单';
}
function publicationTargetRows(targets,record={}){
  if(!targets?.length)return '<p class="publication-empty">'+(record.isCurrent===false?'本版本没有目标电视，未记录电视确认。':'没有目标电视；新电视经管理员批准并接入后，当前版本才会增加确认记录。')+'</p>';
  const stages={pending:'等待同步',downloaded:'已下载，等待应用',applied:'已应用清单',offline:'离线，等待回报',unsupported:'旧版未回报，需升级',superseded:'取代前未确认应用',withdrawn:'已解绑 / 已退出目标'};
  return targets.map(target=>{
    const withdrawn=target.stage==='withdrawn'||target.state==='withdrawn'||target.withdrawnAt!=null;
    const version=Number.isSafeInteger(target.bindingVersion)&&target.bindingVersion>=0?String(target.bindingVersion):'旧记录未提供';
    const reason=({device_unbound:'管理员解绑',binding_replaced:'已重新申请，原接入批次退出'})[target.withdrawReason]||'该接入批次已退出';
    return '<article class="publication-target'+(withdrawn?' publication-target-withdrawn':'')+'"><div class="publication-target-name"><strong>'+esc(target.name||target.deviceId)+'</strong><span class="'+(!withdrawn&&target.online?'online':'offline')+'">'+(withdrawn?'已退出':target.online?'在线':'离线')+'</span></div><p class="publication-identity">设备编号：'+esc(target.deviceId)+'<br>接入批次：'+esc(version)+'</p><p class="publication-stage">'+esc(stages[withdrawn?'withdrawn':target.stage]||'尚未确认')+'</p><p>最近回报：'+esc(publicationTime(target.lastSeen))+'</p><p>下载确认：'+esc(publicationTime(target.downloadedAt))+'<br>应用确认：'+esc(publicationTime(target.appliedAt))+'</p>'+(withdrawn?'<p class="publication-later">退出时间：'+esc(publicationTime(target.withdrawnAt))+'<br>'+reason+'。不计入当前目标或完成数；以上时间仅保留原批次的历史事实，重新接入不会继承确认。</p>':target.stage==='superseded'&&target.appliedAt?'<p class="publication-later">被取代后收到应用回报，不计入取代前确认数。</p>':target.stage==='superseded'&&record.supersededAt&&target.downloadedAt>record.supersededAt?'<p class="publication-later">被取代后收到下载回报，尚未确认应用。</p>':'')+(target.joinedLater?'<p class="publication-later">发布后加入的电视</p>':'')+(!target.online||withdrawn?'<p class="publication-later">显示历史回报，不代表当前播放状态。</p>':'')+'</article>';
  }).join('');
}
function renderPublicationProgress(){
  const summary=state?.currentPublication,record=currentPublicationDetail?.revision===summary?.revision?currentPublicationDetail:summary,attempt=publicationAttempt;
  const submitting=attempt?.status==='submitting',uncertain=attempt?.status==='uncertain',rejected=attempt?.status==='rejected';
  $('publication-title').textContent=submitting?'正在提交发布清单':record?revisionLabel(record.revision)+' · '+publicationSource(record.source):'尚未发布清单';
  $('publication-status').textContent=submitting?'正在向服务器提交，请稍候。':uncertain?'本次提交结果尚未确认，请查看发布记录核对，避免重复发布。':rejected?'本次提交未通过：'+attempt.message:publicationState(record);
  $('publication-steps').innerHTML='<li class="'+(submitting?'current':record?'done':'')+'">'+(submitting?'正在提交清单':'提交清单')+'</li><li class="'+(!submitting&&record?'done':'')+'">服务器已发布</li><li class="'+(!submitting&&record?'current':'')+'">'+(record&&publicationRatio(record).total?'核对电视确认':'等待电视确认')+'</li>';
  $('publication-meter-area').hidden=!record||submitting;
  const ratio=publicationRatio(record);
  $('publication-count').textContent=(ratio.total?ratio.applied+' / '+ratio.total+' 台电视已确认应用':'暂无目标电视，尚无确认进度')+(publicationWithdrawn(record)?'；'+publicationWithdrawalText(record):'');
  $('publication-percent').textContent=ratio.percent===null?'—':ratio.percent+'%';
  $('publication-meter').value=ratio.percent??0;
  $('publication-note').textContent='按电视确认数量计算，不是文件下载百分比；电视约每 30 秒回报一次，页面每 5 秒刷新。已应用清单不代表已经正常播放。新电视经管理员批准并接入当前版本后，目标数量会增加。解绑或重新申请会退出旧接入批次；同一电视再次接入会产生独立确认记录。'+(record?.itemCount===0?' 本版本为空清单，应用后将等待内容。':'');
  $('publication-error').textContent=publicationRefreshError||(uncertain?attempt.message:'');
  $('publication-error').hidden=!$('publication-error').textContent;
  $('refresh-publication').disabled=submitting;
  const detail=currentPublicationDetail?.revision===record?.revision?currentPublicationDetail:null;
  $('publication-device-details').hidden=!record||submitting;
  $('publication-targets').innerHTML=detail?publicationTargetRows(detail.targets,detail):'<p class="publication-empty">'+(publicationRefreshError?'暂未取得各台电视的明细，请刷新进度。':'正在读取各台电视的确认状态…')+'</p>';
}
async function refreshPublicationDetail(){
  const revision=state?.currentPublication?.revision;
  if(!Number.isInteger(revision)||revision<0){currentPublicationDetail=null;return;}
  const requestId=++publicationDetailRequestId;
  try{
    const detail=await api('/api/publications/'+encodeURIComponent(revision));
    if(requestId!==publicationDetailRequestId||state?.currentPublication?.revision!==revision)return;
    currentPublicationDetail=detail;
    if(historySelection===revision){++historyDetailRequestId;historyDetail=detail;historyDetailError='';renderPublicationHistoryDetail();}
  }catch(error){
    if(error.status===401)throw error;
    if(requestId!==publicationDetailRequestId)return;
    publicationRefreshError='服务器已发布 '+revisionLabel(revision)+'，电视确认明细暂时刷新失败：'+error.message+' 请稍后刷新，无需重复发布。';
  }
  renderPublicationProgress();
}
function renderPublicationHistory(){
  renderHistoryCleanupAvailability();
  $('history-error').textContent=historyError;$('history-error').hidden=!historyError;
  $('history-page').textContent='第 '+historyPage+' 页 · 共 '+historyTotal+' 条记录';
  $('history-prev').disabled=historyBusy||historyCleanupBusy||historyPage<=1;$('history-next').disabled=historyBusy||historyCleanupBusy||!historyHasNext;
  $('refresh-history').disabled=historyBusy||historyCleanupBusy;
  $('publication-history-list').innerHTML=historyItems.length?historyItems.map(record=>{
    const ratio=publicationRatio(record),selected=historySelection===record.revision;
    return '<button class="history-record'+(selected?' selected':'')+'" data-publication="'+esc(record.revision)+'" aria-pressed="'+selected+'"><span class="history-record-heading"><strong>'+esc(revisionLabel(record.revision))+'</strong><span class="history-tag">'+(record.isCurrent?'当前版本':record.source==='migrated'?'导入记录':'历史版本')+'</span></span><span>'+esc(publicationSource(record.source))+' · '+esc(publicationCreated(record))+'</span><span>'+Number(record.itemCount||0)+' 个素材 · '+Number(record.pageCount||0)+' 个播放页面</span><span class="history-confirmation">'+(ratio.total?ratio.applied+' / '+ratio.total+' 台'+(record.supersededByRevision?'取代前已确认':'已确认应用'):'暂无目标电视')+'</span>'+(publicationWithdrawn(record)?'<span class="history-withdrawn">'+esc(publicationWithdrawalText(record))+'</span>':'')+'</button>';
  }).join(''):'<div class="empty"><strong>'+(historyBusy?'正在读取发布记录…':'暂无发布记录')+'</strong>'+(historyBusy?'':'首次发布后会自动留存版本、清单快照和电视确认。')+'</div>';
}
function renderPublicationHistoryDetail(){
  const record=historyDetail;
  if(!record||record.revision!==historySelection){$('publication-history-detail').innerHTML='<div class="empty"><strong>'+(historyDetailError?'暂时无法读取详情':historySelection===null?'选择一条发布记录':'正在读取版本详情…')+'</strong>'+esc(historyDetailError||'查看当时的清单和各台电视的确认时间。')+'</div>';return;}
  const ratio=publicationRatio(record);
  const items=(record.items||[]).map((item,index)=>'<tr><td>'+String(index+1)+'</td><td><strong>'+esc(item.name)+'</strong><span>'+esc(kindNames[item.kind]||'素材')+' · '+Number(item.pageCount||0)+' 页</span></td><td>'+esc(item.kind==='video'?'播放至结束':String(item.duration)+' 秒 / 页')+'</td></tr>').join('');
  $('publication-history-detail').innerHTML='<div class="history-detail-heading"><div class="eyebrow">'+esc(publicationSource(record.source))+'</div><h2>'+esc(revisionLabel(record.revision))+' 发布详情</h2><p>'+esc(publicationCreated(record))+'</p>'+(record.createdAt===null?'<p>记录导入时间：'+esc(publicationTime(record.recordedAt))+'；此前其他版本未记录，无法追溯。</p>':'')+'</div>'+(historyDetailError?'<p class="publication-error">详情刷新失败，以下为上次记录：'+esc(historyDetailError)+'</p>':'')+'<div class="history-detail-status"><strong>'+esc(publicationState(record))+'</strong><p>'+(ratio.total?ratio.applied+' / '+ratio.total+' 台电视'+(record.supersededByRevision?'在被取代前已确认应用本版本':'已确认应用本版本'):'没有目标电视，不能视为电视已完成更新。')+'</p>'+(publicationWithdrawn(record)?'<p class="history-withdrawn">'+esc(publicationWithdrawalText(record))+'。累计记录 '+Number(record.recordedTargetCount??(ratio.total+publicationWithdrawn(record)))+' 个接入批次。</p>':'')+(record.supersededAt?'<p>被取代时间：'+esc(publicationTime(record.supersededAt))+'</p>':'')+'</div><h3>当时的清单快照</h3><p class="muted-note">名称、顺序和时长保留为发布时的记录。素材后来归档或删除，也不会改写这份快照。</p>'+(items?'<div class="history-table-wrap"><table class="history-snapshot"><thead><tr><th>顺序</th><th>素材</th><th>展示时长</th></tr></thead><tbody>'+items+'</tbody></table></div>':'<p class="publication-empty">本版本为空清单，电视应用后将等待内容。</p>')+'<h3 class="history-target-title">各台电视的历史确认</h3><div class="publication-targets">'+publicationTargetRows(record.targets,record)+'</div>'+historyCleanupDetailAction(record);
}
async function selectPublication(revision){
  if(historyCleanupBusy)return;
  if(!Number.isInteger(revision)||revision<0)return;
  const requestId=++historyDetailRequestId;
  if(historySelection!==revision){historyDetail=null;historyDetailError='';}
  historySelection=revision;renderPublicationHistory();renderPublicationHistoryDetail();
  try{
    const detail=await api('/api/publications/'+encodeURIComponent(revision));
    if(requestId!==historyDetailRequestId||historySelection!==revision)return;
    historyDetail=detail;historyDetailError='';
  }catch(error){if(requestId!==historyDetailRequestId||historySelection!==revision)return;historyDetailError=error.message;}
  renderPublicationHistoryDetail();
}
async function loadPublicationHistory(page=1,{quiet=false,allowDuringCleanup=false}={}){
  if((historyCleanupBusy&&!allowDuringCleanup)||(quiet&&historyBusy))return false;
  const requestId=++historyRequestId;
  historyBusy=true;if(!quiet){historyError='';renderPublicationHistory();}
  try{
    const result=await api('/api/publications?page='+encodeURIComponent(page)+'&pageSize=10');
    if(requestId!==historyRequestId)return false;
    const pageChanged=historyPage!==result.page;
    historyPage=result.page;historyTotal=result.total;historyHasNext=result.hasNext;historyItems=result.items;historyError='';
    historyCleanupSupported=result.cleanupSupported===true;
    if(pageChanged||historySelection===null||!historyItems.some(record=>record.revision===historySelection)){
      historySelection=null;historyDetail=null;historyDetailError='';
      if(historyItems.length){if(!historyCleanupBusy)await selectPublication(historyItems[0].revision);}
      renderPublicationHistoryDetail();
    }else if(historySelection!==null&&!historyCleanupBusy)await selectPublication(historySelection);
    return true;
  }catch(error){if(requestId===historyRequestId&&error.status!==401)historyError='发布记录暂时无法刷新：'+error.message+'；请刷新后核对。';return false;}
  finally{if(requestId===historyRequestId){historyBusy=false;renderPublicationHistory();}}
}
function renderHistoryCleanupAvailability(){
  $('history-cleanup-batch').disabled=!historyCleanupSupported||historyCleanupBusy||historyCleanupLoading;
  $('history-keep-latest').disabled=historyCleanupBusy;
  $('history-cleanup-support').hidden=historyCleanupSupported;
  $('history-cleanup-support').textContent='删除历史记录需要服务端 1.3.1 或更高版本；旧服务仍可查看记录。';
  $('history-cleanup-feedback').textContent=historyCleanupFeedback;$('history-cleanup-feedback').hidden=!historyCleanupFeedback;
}
function historyCleanupDetailAction(record){
  if(!historyCleanupSupported)return '';
  const protectedRecord=record.isCurrent!==false||record.revision===state?.currentPublication?.revision||record.revision===state?.revision;
  return '<div class="history-cleanup-detail-action"><p>'+ (protectedRecord?'当前生效记录受保护，不能删除。':'删除此版本记录及关联回报，不会删除素材文件或改变当前播放。')+'</p><button class="outline history-delete-button" data-delete-publication="'+esc(record.revision)+'" '+(protectedRecord||historyCleanupBusy||historyCleanupLoading?'disabled':'')+'>删除此记录</button></div>';
}
function resetHistoryCleanup(close=false){
  ++historyCleanupGeneration;clearTimeout(historyCleanupTimer);
  historyCleanup=null;historyCleanupMode=null;historyCleanupBusy=false;historyCleanupLoading=false;
  $('history-cleanup-ack').checked=false;
  if(close&&$('history-cleanup-dialog').open)$('history-cleanup-dialog').close();
  updateHistoryCleanupButton();
}
function updateHistoryCleanupButton(){
  const ready=historyCleanup&&historyCleanup.deleteCount>0&&historyCleanup.confirmationToken&&historyCleanup.deadlineAt>Date.now();
  $('confirm-history-cleanup').disabled=!historyCleanupSupported||historyCleanupBusy||historyCleanupLoading||!ready||!$('history-cleanup-ack').checked;
  $('confirm-history-cleanup').textContent=historyCleanupBusy?'正在删除记录…':historyCleanup?.deleteCount?'确认删除 '+historyCleanup.deleteCount+' 条记录':'确认删除记录';
  $('history-cleanup-cancel').disabled=historyCleanupBusy;
  $('history-cleanup-reload').disabled=historyCleanupBusy||historyCleanupLoading||!historyCleanupMode;
  $('history-cleanup-ack').disabled=historyCleanupBusy||historyCleanupLoading||!ready;
}
function validateHistoryCleanupPreview(value){
  const duration=value?.expiresIn;
  const deadlineAt=duration===undefined?Number(value?.expiresAt)*1000:Date.now()+duration*1000;
  if(!value||!Array.isArray(value.revisions)||value.revisions.length>200||new Set(value.revisions).size!==value.revisions.length||
    value.revisions.some(revision=>!Number.isSafeInteger(revision)||revision<1)||value.deleteCount!==value.revisions.length||
    !Number.isSafeInteger(value.targetCount)||value.targetCount<0||!Number.isSafeInteger(value.totalCandidates)||value.totalCandidates<value.deleteCount||
    !Number.isSafeInteger(value.currentRevision)||value.currentRevision<0||value.revisions.includes(value.currentRevision)||
    (value.deleteCount>0&&(typeof value.confirmationToken!=='string'||!value.confirmationToken||!Number.isFinite(value.expiresAt)||!Number.isFinite(deadlineAt)||deadlineAt<=Date.now()||
      (duration!==undefined&&(!Number.isFinite(duration)||duration<=0||duration>300)))))throw new Error('预览响应不完整或已过期，请重新预览。');
  return {...value,revisions:[...value.revisions],deadlineAt};
}
async function loadHistoryCleanupPreview(){
  if(!historyCleanupSupported||historyCleanupBusy||!historyCleanupMode)return;
  const generation=++historyCleanupGeneration,mode=historyCleanupMode;
  clearTimeout(historyCleanupTimer);historyCleanup=null;historyCleanupLoading=true;
  $('history-cleanup-ack').checked=false;$('history-cleanup-error').textContent='';
  $('history-cleanup-summary').textContent='正在核对将删除的历史记录…';$('history-cleanup-versions').textContent='';
  $('history-cleanup-more').textContent='';$('history-cleanup-expiry').textContent='';
  updateHistoryCleanupButton();renderHistoryCleanupAvailability();renderPublicationHistoryDetail();
  try{
    const value=validateHistoryCleanupPreview(await api('/api/publications/cleanup-preview',{method:'POST',body:JSON.stringify(mode)}));
    if(generation!==historyCleanupGeneration||!$('history-cleanup-dialog').open)return;
    historyCleanup=value;
    $('history-cleanup-summary').textContent=value.deleteCount+' 条发布记录，关联 '+value.targetCount+' 条电视回报记录；预览时生效 '+revisionLabel(value.currentRevision)+' 保留。';
    $('history-cleanup-versions').textContent=value.deleteCount?value.revisions.map(revisionLabel).join('、'):'没有符合条件的历史记录，本次不会删除任何记录。';
    $('history-cleanup-more').textContent=value.hasMore?'共有 '+value.totalCandidates+' 条候选；本次仅核对以上 '+value.deleteCount+' 条。若要继续清理，完成后须重新预览并确认。':'确认仅删除以上已冻结的版本；期间新增发布不会扩大本次范围。';
    if(value.deleteCount){
      $('history-cleanup-expiry').textContent='本次预览约 5 分钟内有效，最终有效期由服务器核对。';
      historyCleanupTimer=setTimeout(()=>{if(generation!==historyCleanupGeneration)return;historyCleanup.confirmationToken='';$('history-cleanup-error').textContent='预览已过期，请重新预览并核对版本范围。';updateHistoryCleanupButton();},Math.max(0,value.deadlineAt-Date.now()));
    }
  }catch(error){
    if(generation!==historyCleanupGeneration||error.status===401)return;
    historyCleanup=null;$('history-cleanup-summary').textContent='尚未取得可确认的删除范围';$('history-cleanup-error').textContent='无法预览：'+error.message+' 请刷新记录或重新预览，未执行删除。';
  }finally{if(generation===historyCleanupGeneration){historyCleanupLoading=false;updateHistoryCleanupButton();renderHistoryCleanupAvailability();renderPublicationHistoryDetail();}}
}
async function openHistoryCleanup(mode){
  if(historyCleanupBusy||historyCleanupLoading)return;
  if(!historyCleanupSupported){historyCleanupFeedback='当前服务不支持删除历史记录，请先升级服务端至 1.3.1。';renderHistoryCleanupAvailability();return;}
  if(mode.revisions){
    const revision=mode.revisions[0],record=historyItems.find(item=>item.revision===revision)||historyDetail;
    if(mode.revisions.length!==1||!Number.isSafeInteger(revision)||revision<1||revision===state?.revision||revision===state?.currentPublication?.revision||record?.revision!==revision||record.isCurrent!==false){
      historyCleanupFeedback='当前生效记录或尚未核对的记录不能删除，请刷新后选择历史版本。';renderHistoryCleanupAvailability();return;
    }
    mode={revisions:[revision]};
  }else{
    if(!Number.isSafeInteger(mode.keepLatest)||mode.keepLatest<1||mode.keepLatest>5000){historyCleanupFeedback='保留数量需为 1～5000 的整数，包含当前生效记录。';renderHistoryCleanupAvailability();return;}
    mode={keepLatest:mode.keepLatest};
  }
  resetHistoryCleanup();historyCleanupMode=mode;
  $('history-cleanup-title').textContent=mode.revisions?'删除发布历史记录':'清理较早发布记录';
  $('history-cleanup-scope').textContent=mode.revisions?'请求核对 '+revisionLabel(mode.revisions[0])+' 的记录。':'本次手动保留最近 '+mode.keepLatest+' 条记录（包含当前生效记录）；这不是自动清理策略。';
  $('history-cleanup-dialog').showModal();await loadHistoryCleanupPreview();
}
$('history-cleanup-batch').onclick=()=>openHistoryCleanup({keepLatest:Number($('history-keep-latest').value)});
$('publication-history-detail').addEventListener('click',event=>{const button=event.target.closest('[data-delete-publication]');if(button&&!button.disabled)openHistoryCleanup({revisions:[Number(button.dataset.deletePublication)]});});
$('history-cleanup-ack').addEventListener('change',updateHistoryCleanupButton);
$('history-cleanup-cancel').onclick=()=>{if(!historyCleanupBusy)$('history-cleanup-dialog').close();};
$('history-cleanup-reload').onclick=loadHistoryCleanupPreview;
$('history-cleanup-dialog').addEventListener('cancel',event=>{if(historyCleanupBusy)event.preventDefault();});
$('history-cleanup-dialog').addEventListener('close',()=>{resetHistoryCleanup();renderHistoryCleanupAvailability();renderPublicationHistoryDetail();});
$('history-cleanup-form').addEventListener('submit',async event=>{
  event.preventDefault();updateHistoryCleanupButton();if($('confirm-history-cleanup').disabled)return;
  const plan=historyCleanup,generation=historyCleanupGeneration,session=sessionGeneration;
  historyCleanupBusy=true;clearTimeout(historyCleanupTimer);++historyRequestId;++historyDetailRequestId;historyBusy=false;
  $('history-cleanup-error').textContent='';updateHistoryCleanupButton();renderPublicationHistory();renderPublicationHistoryDetail();
  let accepted=false,result;
  try{
    result=await api('/api/publications/cleanup',{method:'POST',body:JSON.stringify({confirmationToken:plan.confirmationToken})});
    if(generation!==historyCleanupGeneration||session!==sessionGeneration)return;
    if(result.ok!==true||!Number.isSafeInteger(result.deletedCount)||result.deletedCount<0||!Number.isSafeInteger(result.alreadyDeletedCount)||result.alreadyDeletedCount<0||result.deletedCount+result.alreadyDeletedCount!==plan.deleteCount||!Number.isSafeInteger(result.targetCount)||result.targetCount<0||!Number.isSafeInteger(result.remainingCount)||result.remainingCount<0)throw new Error('服务器返回结果不完整，请刷新核对。');
    accepted=true;
    ++historyRequestId;++historyDetailRequestId;++refreshRequestId;++publicationDetailRequestId;
    const removed=new Set(plan.revisions);historyItems=historyItems.filter(record=>!removed.has(record.revision));
    historyTotal=result.remainingCount;
    if(removed.has(historySelection)){historySelection=null;historyDetail=null;historyDetailError='';}
    historyCleanupFeedback='已删除 '+result.deletedCount+' 条发布记录及 '+result.targetCount+' 条关联回报'+(result.alreadyDeletedCount?'；另 '+result.alreadyDeletedCount+' 条在此前已删除':'')+'。素材、当前清单与电视缓存不受影响。';
    renderPublicationHistory();renderPublicationHistoryDetail();
    await refresh();
    const refreshed=await loadPublicationHistory(historyPage,{allowDuringCleanup:true});
    if(session!==sessionGeneration||generation!==historyCleanupGeneration)return;
    if(!refreshed)throw new Error(historyError||'历史列表暂未刷新');
    $('history-cleanup-dialog').close();
    if(historySelection===null&&historyItems.length)await selectPublication(historyItems[0].revision);
  }catch(error){
    if(generation!==historyCleanupGeneration||session!==sessionGeneration||error.status===401)return;
    historyCleanup=null;$('history-cleanup-ack').checked=false;
    if(accepted){
      historyCleanupFeedback+=' 列表刷新失败：'+error.message+' 请手动刷新核对，无需重复删除。';
      $('history-cleanup-dialog').close();
    }else{
      const uncertain=!error.status||error.status>=500;
      $('history-cleanup-error').textContent=uncertain?'删除结果尚未确认：'+error.message+' 正在刷新历史记录供核对；不会自动重试删除。':'本次删除未通过：'+error.message+' 请重新预览并核对版本范围。';
      if(uncertain)await loadPublicationHistory(historyPage,{allowDuringCleanup:true});
    }
  }finally{
    if(generation===historyCleanupGeneration){historyCleanupBusy=false;updateHistoryCleanupButton();renderPublicationHistory();renderPublicationHistoryDetail();}
    else if(session===sessionGeneration)renderHistoryCleanupAvailability();
  }
});
$('refresh-publication').onclick=async()=>{publicationRefreshError='';try{await refresh();}catch(error){if(error.status===401)return;publicationRefreshError='进度刷新失败：'+error.message+'；已确认的服务器发布结果不会因此撤销。';renderPublicationProgress();}};
$('open-publication-history').onclick=()=>showView('publications');
$('refresh-history').onclick=()=>loadPublicationHistory(historyPage);
$('history-prev').onclick=()=>{if(!historyBusy&&historyPage>1)loadPublicationHistory(historyPage-1);};
$('history-next').onclick=()=>{if(!historyBusy&&historyHasNext)loadPublicationHistory(historyPage+1);};
$('publication-history-list').addEventListener('click',event=>{const button=event.target.closest('[data-publication]');if(button)selectPublication(Number(button.dataset.publication));});
const views={library:['素材与编排','素材与播放编排'],publications:['发布记录','发布历史与电视确认'],devices:['电视设备','电视设备与播放状态'],connection:['连接设置','电视连接设置']};
function showView(view){
  if(!views[view])return;
  activeView=view;Object.keys(views).forEach(v=>$(v+'-view').hidden=v!==view);
  document.querySelectorAll('[data-view]').forEach(button=>button.classList.toggle('active',button.dataset.view===view));
  $('breadcrumb').textContent=views[view][0];$('page-title').textContent=views[view][1];
  if(view==='publications')loadPublicationHistory(historyPage).catch(()=>{});
}
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>showView(button.dataset.view));
$('refresh-devices').onclick=()=>refresh().catch(e=>toast(e.message,true));$('show-token').onclick=()=>{const hidden=$('display-token').type==='password';$('display-token').type=hidden?'text':'password';$('show-token').textContent=hidden?'隐藏':'显示';};$('copy-token').onclick=async()=>{const input=$('display-token'),previousType=input.type;try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(input.value);else{input.type='text';input.focus();input.select();if(!document.execCommand?.('copy'))throw new Error('无法访问剪贴板');input.type=previousType;}toast('当前输入的播放密钥已复制。');}catch(e){input.type=previousType;$('show-token').textContent=previousType==='password'?'显示':'隐藏';toast('复制失败。请按需点击“显示”，再手动选择密钥。',true);}};
$('logout').onclick=async()=>{if((dirty||policyDirty||tokenDirty)&&!confirm('还有未保存的清单、缓存设置或播放密钥修改，确定退出吗？'))return;await api('/api/logout',{method:'POST',body:'{}'});invalidatePublicationSession();dirty=false;policyDirty=false;clearDisplayToken();showAuth();};window.addEventListener('beforeunload',e=>{if(dirty||policyDirty||tokenDirty){e.preventDefault();e.returnValue='';}});
async function automaticRefresh(){
  if($('dashboard').hidden||saving||historyCleanupBusy||automaticRefreshBusy)return;
  automaticRefreshBusy=true;
  try{
    await refresh();
    if(activeView==='publications')await loadPublicationHistory(historyPage,{quiet:true});
  }catch(error){if(error.status===401)return;publicationRefreshError='进度暂时无法刷新，以下为最近一次结果：'+error.message;renderPublicationProgress();}
  finally{automaticRefreshBusy=false;}
}
setInterval(automaticRefresh,5000);boot();
