'use strict';

const el = id => document.getElementById(id);
const POLL_MS = 30000, REQUEST_TIMEOUT_MS = 15000, IMAGE_TIMEOUT_MS = 30000;
let manifest = null, pending = null, index = -1, playing = null;
let timer, loadTimer, hideTimer, pollTimer, stallTimer;
let started = false, paused = false, disposed = false, imageReady = false;
let generation = 0, failedGeneration = -1, lifecycle = 0;
let lastTime = -1, stalled = 0, activeRequest = null;
let connectionMessage = '', playbackMessage = '';

function status(text, connection = false) {
  if (connection) connectionMessage = text; else playbackMessage = text;
  el('connection').textContent = [connectionMessage, playbackMessage].filter(Boolean).join(' · ');
  el('connection').hidden = !el('connection').textContent;
}

function validateManifest(value) {
  if (!value || !Number.isSafeInteger(value.revision) || value.revision < 0 ||
      !Array.isArray(value.items) || value.items.length > 5000) throw new Error('清单格式无效');
  for (const item of value.items) {
    if (!item || !['image', 'video'].includes(item.type) || typeof item.url !== 'string' ||
        typeof item.name !== 'string' || !Number.isFinite(item.duration) || item.duration < 1 || item.duration > 3600)
      throw new Error('素材信息无效');
    const url = new URL(item.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/media/')) throw new Error('素材地址无效');
  }
  return value;
}

function clearPlaybackTimers() {
  clearTimeout(timer); clearTimeout(loadTimer);
  timer = loadTimer = undefined;
}

function releaseMedia() {
  const video = el('video'), image = el('image');
  video.onended = video.onerror = null;
  video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
  image.onload = image.onerror = null;
  image.removeAttribute('src'); image.hidden = true;
  imageReady = false;
}

function stopCurrent() {
  generation++;
  clearPlaybackTimers(); releaseMedia();
  playing = null; failedGeneration = -1; lastTime = -1; stalled = 0;
}

function showEmpty() {
  el('empty').hidden = false; el('start').hidden = true;
  el('message').textContent = '当前播放清单为空，等待发布素材。';
  el('now').textContent = '当前播放清单为空';
}

function acceptManifest(value) {
  el('login').hidden = true;
  // Withdrawal must release current media even while playback is paused or loading.
  if (!value.items.length) {
    stopCurrent(); manifest = value; pending = null; index = -1;
    status(''); showEmpty(); controls(); return;
  }
  if (!started) {
    manifest = value; pending = null; index = -1;
    el('empty').hidden = false; el('start').hidden = false;
    el('message').textContent = '点击开始，预览已发布清单。';
  } else if (value.revision !== (pending || manifest)?.revision) {
    pending = value;
    if (!playing && !paused) next();
  }
}

function requireLogin() {
  stopCurrent(); manifest = pending = null; index = -1; started = paused = false;
  el('empty').hidden = false; el('start').hidden = true; el('login').hidden = false;
  el('pause').textContent = '暂停'; el('now').textContent = '等待登录';
  el('message').textContent = '请先登录管理中心，再打开播放预览。';
  status(''); status('登录已过期，请返回管理中心登录。', true); controls();
}

async function poll() {
  if (disposed || activeRequest) return;
  clearTimeout(pollTimer);
  const epoch = lifecycle, controller = new AbortController();
  const request = {controller, timeout: undefined, cancel: null};
  activeRequest = request;
  try {
    // The race settles even if a broken fetch implementation ignores abort.
    const deadline = new Promise((_, reject) => {
      request.cancel = () => { controller.abort(); reject(new Error('请求已取消')); };
      request.timeout = setTimeout(request.cancel, REQUEST_TIMEOUT_MS);
    });
    const result = await Promise.race([
      (async () => {
        const response = await fetch('/api/preview/manifest', {signal: controller.signal, cache: 'no-store'});
        if (response.status === 401) return {login: true};
        if (!response.ok) throw new Error('清单请求失败');
        return {value: validateManifest(await response.json())};
      })(), deadline
    ]);
    if (disposed || epoch !== lifecycle || activeRequest !== request) return;
    if (result.login) requireLogin();
    else { acceptManifest(result.value); status('', true); }
  } catch (_) {
    if (!disposed && epoch === lifecycle && activeRequest === request)
      status('清单暂时无法刷新，正在重试；保留当前清单。网页预览不提供离线缓存。', true);
  } finally {
    clearTimeout(request.timeout);
    if (activeRequest === request) activeRequest = null;
    if (!disposed && epoch === lifecycle) pollTimer = setTimeout(poll, POLL_MS);
  }
}

function scheduleNext(g, delay) {
  clearTimeout(timer);
  timer = setTimeout(() => { timer = undefined; if (!disposed && g === generation) next(); }, delay);
}

function skipFailed(text, g) {
  if (disposed || g !== generation || failedGeneration === g) return;
  failedGeneration = g; clearPlaybackTimers(); releaseMedia();
  status(paused ? text.replace('3 秒后切换。', '继续播放后切换。') : text);
  if (!paused) scheduleNext(g, 3000);
}

function playVideo(g) {
  let result;
  try { result = el('video').play(); }
  catch (error) { handlePlayError(error, g); return; }
  if (result) result.catch(error => handlePlayError(error, g));
}

function handlePlayError(error, g) {
  if (disposed || g !== generation || failedGeneration === g || paused) return;
  if (error?.name === 'NotAllowedError') {
    paused = true; lastTime = -1; stalled = 0; clearTimeout(timer); el('pause').textContent = '继续播放';
    status('浏览器暂停了自动播放，请点击继续播放。'); controls();
  } else skipFailed('视频无法播放，3 秒后切换。', g);
}

function next() {
  if (disposed || paused) return;
  stopCurrent(); status('');
  if (pending) { manifest = pending; pending = null; index = -1; }
  if (!manifest?.items.length) { showEmpty(); return; }
  index = (index + 1) % manifest.items.length;
  playing = manifest.items[index];
  const item = playing, g = generation;
  el('empty').hidden = true;
  el('now').textContent = (index + 1) + ' / ' + manifest.items.length + ' · ' + item.name;
  if (item.type === 'video') {
    const video = el('video'); video.hidden = false;
    video.onended = () => { if (!disposed && g === generation && failedGeneration !== g) next(); };
    video.onerror = () => skipFailed('视频无法播放，3 秒后切换。', g);
    video.src = item.url; playVideo(g);
  } else {
    const image = el('image'); image.hidden = false;
    image.onload = () => {
      if (disposed || g !== generation || failedGeneration === g || imageReady) return;
      clearTimeout(loadTimer); loadTimer = undefined; imageReady = true;
      if (!paused) scheduleNext(g, item.duration * 1000);
    };
    image.onerror = () => skipFailed('图片读取失败，3 秒后切换。', g);
    loadTimer = setTimeout(() => skipFailed('图片加载超时，3 秒后切换。', g), IMAGE_TIMEOUT_MS);
    image.src = item.url;
  }
}

el('start').onclick = () => { started = true; paused = false; el('pause').textContent = '暂停'; next(); controls(); };
el('next').onclick = () => { if (!started) return; paused = false; el('pause').textContent = '暂停'; next(); controls(); };
el('pause').onclick = () => {
  if (!started) return;
  paused = !paused; el('pause').textContent = paused ? '继续播放' : '暂停';
  lastTime = -1; stalled = 0;
  if (paused) { clearTimeout(timer); timer = undefined; el('video').pause(); }
  else if (pending || !playing || failedGeneration === generation) next();
  else if (playing.type === 'video') { status(''); playVideo(generation); }
  else if (imageReady) scheduleNext(generation, playing.duration * 1000);
  controls();
};
el('fullscreen').onclick = () => {
  const result = document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  if (result) result.catch(() => status('请使用浏览器菜单进入全屏。'));
};

function controls() {
  el('controls').classList.remove('quiet'); clearTimeout(hideTimer);
  if (!disposed && started && !paused) hideTimer = setTimeout(() => el('controls').classList.add('quiet'), 5000);
}

function checkStall() {
  if (disposed || !started || paused || playing?.type !== 'video' || failedGeneration === generation) return;
  const time = el('video').currentTime;
  if (time === lastTime) stalled++; else stalled = 0;
  lastTime = time;
  if (stalled >= 15) skipFailed('视频长时间无响应，3 秒后切换。', generation);
}

function dispose() {
  disposed = true; lifecycle++;
  clearTimeout(pollTimer); clearTimeout(hideTimer); clearInterval(stallTimer);
  if (activeRequest) activeRequest.cancel();
  activeRequest = null; stopCurrent();
}

document.addEventListener('mousemove', controls);
document.addEventListener('focusin', controls);
document.addEventListener('keydown', event => {
  controls();
  if (event.key === 'ArrowRight') el('next').click();
  if (event.code === 'Space' && event.target === document.body) { event.preventDefault(); el('pause').click(); }
});
window.addEventListener('pagehide', dispose);
window.addEventListener('pageshow', () => {
  if (!disposed) return;
  disposed = false; stallTimer = setInterval(checkStall, 1000);
  if (started && !paused) next();
  controls(); poll();
});
stallTimer = setInterval(checkStall, 1000);
poll();
