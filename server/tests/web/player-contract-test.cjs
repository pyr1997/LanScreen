'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(__dirname + '/../../web/player.js', 'utf8');
let assertions = 0;
const check = new Proxy({}, {get: (_, method) => (...args) => { assertions++; return assert[method](...args); }});
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const item = (type = 'image', id = 'one') => ({id, type, name: id, url: '/media/' + id + '/file', duration: 10});
const list = (revision = 1, items = [item()]) => ({revision, items, refreshSeconds: 30});
const response = value => ({ok: true, status: 200, json: async () => value});
const namedError = name => Object.assign(new Error(name), {name});

function harness(initialHandler = () => response(list())) {
  const elements = new Map(), events = {}, timers = new Map(), requests = [];
  let now = 0, serial = 0, handler = initialHandler;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, textContent: '', src: '', currentTime: 0, pauseCount: 0, loadCount: 0, playCount: 0,
      classList: {add() {}, remove() {}},
      pause() { this.pauseCount++; }, load() { this.loadCount++; },
      play() { this.playCount++; return this.playImpl ? this.playImpl() : Promise.resolve(); },
      removeAttribute(name) { if (name === 'src') this.src = ''; },
      click() { return this.onclick?.(); }
    });
    return elements.get(id);
  }
  const timeout = (fn, delay, repeat = 0) => { const id = ++serial; timers.set(id, {fn, at: now + delay, repeat}); return id; };
  const context = {
    console, URL, AbortController, location: {origin: 'http://localhost', href: 'http://localhost/player'},
    document: {getElementById: element, addEventListener() {}, body: {}, documentElement: {requestFullscreen: () => Promise.resolve()}},
    window: {addEventListener: (name, fn) => { events[name] = fn; }},
    setTimeout: (fn, delay) => timeout(fn, delay),
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => timeout(fn, delay, delay),
    clearInterval: id => timers.delete(id),
    fetch: (path, options) => {
      requests.push({path, options});
      return Promise.resolve().then(() => handler(path, options));
    }
  };
  vm.createContext(context); vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  return {
    element, run, requests, timers, events, setHandler: next => { handler = next; },
    async ready() { await flush(); },
    async refresh(next) { if (next) handler = next; await run('poll()'); await flush(); },
    async advance(duration) {
      const target = now + duration;
      while (true) {
        const due = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!due) break;
        const [id, timer] = due; now = timer.at;
        if (timer.repeat) timer.at += timer.repeat; else timers.delete(id);
        timer.fn(); await flush();
      }
      now = target; await flush();
    },
    async end() {
      run('dispose()'); await flush();
      check.equal(timers.size, 0, 'every simulated timer is stopped at page teardown');
      check.equal(run('activeRequest'), null);
      check.equal(element('image').src, '');
      check.equal(element('video').src, '');
    }
  };
}

(async () => {
  {
    const h = harness(() => response(list(1, [item(), item('video', 'two')]))); await h.ready();
    check.equal(h.element('start').hidden, false);
    check.equal(h.requests[0].path, '/api/preview/manifest');
    check.equal(h.requests[0].options.cache, 'no-store');
    h.element('start').click();
    const oldLoad = h.element('image').onload;
    check.equal(h.element('image').src, '/media/one/file');
    h.element('image').onload();
    check.equal(h.run('loadTimer'), undefined, 'loaded images cancel the loading deadline');
    h.element('next').click();
    check.equal(h.run('playing.id'), 'two');
    check.equal(h.element('image').src, '', 'image reference is released when changing to video');
    check.equal(h.element('image').onload, null);
    check.equal(h.element('image').onerror, null);
    const oldEnded = h.element('video').onended, oldError = h.element('video').onerror;
    await h.refresh(() => response(list(2, [])));
    check.equal(h.run('manifest.revision'), 2);
    check.equal(h.run('playing'), null, 'empty publication stops a currently playing long video immediately');
    check.equal(h.element('video').src, '');
    check.equal(h.element('video').onended, null);
    check.equal(h.element('video').onerror, null);
    check.equal(h.element('empty').hidden, false);
    check.equal(h.run('timer'), undefined);
    check.equal(h.run('loadTimer'), undefined);
    oldLoad(); oldEnded(); oldError();
    await h.advance(10000);
    check.equal(h.run('playing'), null, 'old media events cannot revive withdrawn content');
    await h.end();
  }
  {
    const h = harness(); await h.ready(); h.element('start').click();
    h.element('pause').click();
    check.equal(h.run('paused'), true);
    await h.refresh(() => response(list(2, [])));
    check.equal(h.run('playing'), null, 'paused image is still withdrawn');
    check.equal(h.element('image').src, '');
    check.equal(h.run('loadTimer'), undefined);
    check.equal(h.run('paused'), true, 'withdrawal respects the user pause preference');
    await h.refresh(() => response(list(3, [item('image', 'new')])));
    check.equal(h.run('pending.revision'), 3);
    check.equal(h.run('playing'), null);
    h.element('pause').click();
    check.equal(h.run('playing.id'), 'new');
    check.equal(h.run('manifest.revision'), 3);
    await h.end();
  }
  {
    const first = deferred(); const h = harness(() => first.promise);
    await h.ready();
    await h.run('poll()'); await h.run('poll()'); await h.ready();
    check.equal(h.requests.length, 1, 'busy poll never creates overlapping requests');
    const firstSignal = h.requests[0].options.signal;
    await h.advance(15000);
    check.equal(firstSignal.aborted, true);
    check.equal(h.run('activeRequest'), null, 'deadline settles a fetch which ignores abort');
    check.match(h.element('connection').textContent, /保留当前清单/);
    h.setHandler(() => response(list(3, [item('image', 'new')])));
    await h.advance(29999); check.equal(h.requests.length, 1);
    await h.advance(1); check.equal(h.requests.length, 2);
    check.equal(h.run('manifest.revision'), 3);
    first.resolve(response(list(1, []))); await h.ready();
    check.equal(h.run('manifest.revision'), 3, 'late timed-out response cannot erase the newer list');
    check.equal(h.element('start').hidden, false);
    await h.end();
  }
  {
    const h = harness(); await h.ready(); h.element('start').click(); h.element('image').onload();
    const invalid = [
      {revision: 2}, {revision: 2, items: null}, {revision: -1, items: []},
      list(2, [{...item(), type: 'html'}]),
      list(2, [{...item(), url: 'https://elsewhere.example/media/a'}]),
      list(2, [{...item(), duration: 0}])
    ];
    for (const value of invalid) {
      await h.refresh(() => response(value));
      check.equal(h.run('manifest.revision'), 1, 'malformed response preserves last good list');
      check.equal(h.run('playing.id'), 'one');
    }
    await h.refresh(() => ({ok: false, status: 503}));
    check.equal(h.run('playing.id'), 'one');
    await h.refresh(() => ({ok: true, status: 200, json: async () => { throw new Error('bad JSON'); }}));
    check.equal(h.run('playing.id'), 'one');
    const body = deferred();
    h.setHandler(() => ({ok: true, status: 200, json: () => body.promise}));
    const bodyPoll = h.run('poll()'); await h.ready(); await h.advance(15000); await bodyPoll;
    check.equal(h.run('manifest.revision'), 1, 'body-reading phase is also bounded by request deadline');
    body.resolve(list(2, [])); await h.ready();
    check.equal(h.run('manifest.revision'), 1);
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item(), item('image', 'two')]))); await h.ready();
    h.element('start').click(); const lateLoad = h.element('image').onload, lateError = h.element('image').onerror;
    await h.advance(30000);
    check.match(h.element('connection').textContent, /图片加载超时/);
    check.equal(h.element('image').src, '');
    const failureTimer = h.run('timer');
    lateLoad(); lateError(); lateError();
    check.equal(h.run('timer'), failureTimer, 'image failure and late load schedule only one skip');
    await h.advance(3000);
    check.equal(h.run('playing.id'), 'two');
    check.equal(h.run('index'), 1);
    h.element('image').onload();
    const durationTimer = h.run('timer');
    h.element('image').onload();
    check.equal(h.run('timer'), durationTimer, 'duplicate load does not create/reset multiple timers');
    await h.advance(9999); check.equal(h.run('playing.id'), 'two');
    await h.advance(1); check.equal(h.run('playing.id'), 'one');
    await h.end();
  }
  {
    const h = harness(); await h.ready(); h.element('start').click(); h.element('pause').click();
    h.element('image').onload();
    check.equal(h.run('imageReady'), true);
    check.equal(h.run('timer'), undefined, 'a load event during pause cannot advance playback');
    await h.advance(10000); check.equal(h.run('index'), 0);
    h.element('pause').click();
    check.notEqual(h.run('timer'), undefined);
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item('video'), item('image', 'two')]))); await h.ready();
    const rejected = deferred();
    h.element('video').playImpl = () => rejected.promise;
    h.element('start').click();
    const oldError = h.element('video').onerror;
    oldError();
    const failureTimer = h.run('timer');
    rejected.reject(namedError('NotSupportedError')); await h.ready(); oldError();
    check.equal(h.run('paused'), false, 'codec failure is not treated as autoplay permission denial');
    check.equal(h.run('timer'), failureTimer, 'onerror plus play rejection cannot schedule a second skip');
    await h.advance(3000);
    check.equal(h.run('playing.id'), 'two');
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item('video'), item('image', 'two')]))); await h.ready();
    h.element('video').playImpl = () => Promise.reject(namedError('NotSupportedError'));
    h.element('start').click(); await h.ready();
    check.equal(h.run('paused'), false);
    check.notEqual(h.run('timer'), undefined, 'rejected play without an error event still advances');
    await h.advance(3000); check.equal(h.run('playing.id'), 'two');
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item('video')]))); await h.ready();
    h.element('video').playImpl = () => Promise.reject(namedError('NotAllowedError'));
    h.element('start').click(); await h.ready();
    check.equal(h.run('paused'), true);
    check.equal(h.run('timer'), undefined);
    check.match(h.element('connection').textContent, /点击继续播放/);
    await h.refresh(() => response(list(1, [item('video')])));
    check.match(h.element('connection').textContent, /点击继续播放/, 'a successful poll does not erase the media permission prompt');
    h.element('video').playImpl = () => Promise.resolve();
    h.element('pause').click(); await h.ready();
    check.equal(h.run('paused'), false);
    check.equal(h.element('video').playCount, 2);
    check.equal(h.element('connection').hidden, true);
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item('video'), item('image', 'two')]))); await h.ready();
    const delayed = deferred(); h.element('video').playImpl = () => delayed.promise;
    h.element('start').click(); h.element('next').click();
    delayed.reject(namedError('NotAllowedError')); await h.ready();
    check.equal(h.run('paused'), false, 'late play rejection cannot pause the next image');
    check.equal(h.run('playing.id'), 'two');
    check.equal(h.run('failedGeneration'), -1);
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item('video'), item('image', 'two')]))); await h.ready();
    const delayed = deferred(); h.element('video').playImpl = () => delayed.promise;
    h.element('start').click();
    h.run('stalled = 14; lastTime = 0'); h.element('pause').click();
    check.equal(h.run('stalled'), 0, 'pause resets the watchdog instead of preserving a nearly expired stall');
    delayed.reject(namedError('AbortError')); await h.ready();
    check.equal(h.run('paused'), true, 'intentional pause abort does not enter a failure loop');
    check.equal(h.run('failedGeneration'), -1);
    h.element('video').playImpl = () => Promise.resolve(); h.element('pause').click();
    await h.advance(16000);
    check.match(h.element('connection').textContent, /长时间无响应/);
    check.equal(h.run('paused'), false);
    await h.advance(3000); check.equal(h.run('playing.id'), 'two');
    await h.end();
  }
  {
    const h = harness(() => response(list(1, [item('video')]))); await h.ready(); h.element('start').click();
    await h.refresh(() => ({ok: false, status: 401}));
    check.equal(h.run('playing'), null, 'an expired preview session releases currently playing content');
    check.equal(h.element('video').src, '');
    check.equal(h.element('login').hidden, false);
    check.equal(h.run('started'), false);
    await h.refresh(() => response(list(2)));
    check.equal(h.element('login').hidden, true);
    check.equal(h.element('start').hidden, false);
    check.equal(h.run('manifest.revision'), 2);
    await h.end();
  }
  {
    const h = harness(); await h.ready(); h.element('start').click();
    const oldLoad = h.element('image').onload, pending = deferred();
    h.setHandler(() => pending.promise); const request = h.run('poll()'); await h.ready();
    const signal = h.requests.at(-1).options.signal;
    h.events.pagehide(); await h.ready(); await request;
    check.equal(signal.aborted, true);
    check.equal(h.timers.size, 0, 'pagehide releases timers and the inflight poll');
    oldLoad();
    pending.resolve(response(list(10, []))); await h.ready();
    check.equal(h.run('manifest.revision'), 1, 'post-pagehide response cannot replace the retained list');
    check.equal(h.timers.size, 0);
    h.setHandler(() => response(list(2)));
    h.events.pageshow(); await h.ready();
    check.equal(h.run('disposed'), false);
    check.equal(h.run('pending.revision'), 2);
    check.equal([...h.timers.values()].filter(timer => timer.repeat).length, 1, 'BFCache resume recreates one watchdog only');
    check.equal(h.requests.every(request => request.path === '/api/preview/manifest'), true, 'preview never registers a TV or sends a heartbeat');
    await h.end();
  }
  console.log('PASS: ' + assertions + ' Web preview assertions; simulated requests/timers only; all harnesses disposed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
