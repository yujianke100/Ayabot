#!/usr/bin/env node
/**
 * 真浏览器 UI 回归（零依赖：Node 原生 WebSocket 直接说 CDP）。
 *
 * 为什么要有它：这一堆 UI 故障都不会让任何后端自检变红，是用户先看到的：
 *   - vendor 里的 Vue 没加载 → 整页 `v-cloak` 白屏（v1.2.6 之前所有用户中招）
 *   - 凭证失效 → 需要"引导重新扫码"，而不是把后端异常直接糊在屏幕上
 *   - 崩溃提示里的"下次自动重试"文案格式一旦变，截图看着没问题但用户看不懂
 *   - 日志面板是 `v-html`，日志里出现 `<img onerror>` 就会在本机执行
 *   - 换房间是纯前端状态，后端接口全绿也照样能坏
 *
 * 以前这些断言散落在临时目录的 puppeteer 脚本里，被临时目录的 24h 清理删掉了 ——
 * 也就是最有价值的回归测试从来没进过仓库。这里用 Node 自带的 WebSocket 说 CDP，
 * 不依赖 puppeteer / 不下载 Chromium，只借本机已装的 Chrome。
 *
 *   node scripts/check_ui_dom.js [--keep]
 *
 * 由 scripts/check_ui_dom.py 包一层，好让 run_all_checks.py 认得。
 */
'use strict';

const { spawn, execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const APP_PORT = Number(process.env.AYABOT_UI_PORT || 18091);
const CDP_PORT = Number(process.env.AYABOT_CDP_PORT || 19222);
const ROOM = '1946287911';
const ROOM2 = '2000000002';
const USER = 'preview';
const PASS = 'preview123';
const KEEP = process.argv.includes('--keep');

// 临时目录放在 Hermes 的 scratch 下（系统 temp 会被别的工具乱清）
const SCRATCH = process.env.AYABOT_SCRATCH
  || path.join(process.env.LOCALAPPDATA || os.homedir(), 'hermes', 'cache', 'scratch');
const PREVIEW_ROOT = path.join(SCRATCH, 'preview_root');
const DATA_DIR = path.join(PREVIEW_ROOT, 'rooms', ROOM, 'data');
const PROFILE = path.join(SCRATCH, `ui-cdp-${process.pid}`);

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ─────────────────────────── CDP 最小客户端 ───────────────────────────
class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.seq = 0;
    this.pending = new Map();
    this.listeners = [];
    this.closed = false;
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('CDP 连接超时')), 10000);
      this.ws.addEventListener('open', () => { clearTimeout(timer); resolve(); });
      this.ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('CDP 连接失败')); });
    });
    this.ws.addEventListener('message', (ev) => this._onMessage(ev.data));
    this.ws.addEventListener('close', () => { this.closed = true; });
  }

  _onMessage(raw) {
    let msg;
    try { msg = JSON.parse(typeof raw === 'string' ? raw : Buffer.from(raw).toString('utf8')); }
    catch { return; }
    if (msg.id && this.pending.has(msg.id)) {
      const p = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.error) p.reject(new Error(msg.error.message || 'CDP 错误'));
      else p.resolve(msg.result);
      return;
    }
    if (msg.method) for (const fn of this.listeners) fn(msg);
  }

  on(fn) { this.listeners.push(fn); }

  send(method, params = {}, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP 超时 ${timeoutMs}ms: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true, userGesture: true,
    });
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error('页面 JS 抛异常: ' + (d.exception?.description || d.text));
    }
    return r.result.value;
  }

  close() { try { this.ws.close(); } catch { /* ignore */ } }
}

async function waitFor(cdp, expression, timeoutMs, label) {
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < timeoutMs) {
    try { last = await cdp.eval(expression); } catch (e) { last = 'ERR ' + e.message; }
    if (last) return last;
    await sleep(150);
  }
  throw new Error(`等待超时 ${timeoutMs}ms: ${label}（最后一次：${JSON.stringify(last)}）`);
}

// ─────────────────────────── 按键 / 点击 ───────────────────────────
// puppeteer 的 page.type() 会吞掉部分字符；这里直接用原生 value setter + input 事件，
// 和真实键入走同一条 Vue v-model 通路。
const FILL = (placeholder, value) => `(() => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  const el = [...document.querySelectorAll('input')]
    .find(i => (i.placeholder || '').trim() === ${JSON.stringify(placeholder)});
  if (!el) return 'NO_INPUT';
  setter.call(el, ${JSON.stringify(value)});
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return 'ok';
})()`;

const CLICK_TEXT = (text, tag = 'button') => `(() => {
  const els = [...document.querySelectorAll(${JSON.stringify(tag)})];
  const el = els.find(e => (e.textContent || '').replace(/\\s+/g, '').includes(${JSON.stringify(text)}));
  if (!el) return 'NO_EL';
  el.click();
  return 'ok';
})()`;

async function login(cdp) {
  // 会话已恢复（HttpOnly cookie 还有效）时根本没有登录框 —— 直接算已登录。
  // 以前这里无条件等登录表单，刷新后就会超时。
  try {
    await waitFor(cdp, `!!document.querySelector('input[placeholder="账号"]')`, 3000, '登录表单');
  } catch {
    await waitFor(cdp, `!!document.querySelector('header')`, 12000, '已登录的主界面');
    return;
  }
  for (let i = 0; i < 3; i++) {
    const a = await cdp.eval(FILL('账号', USER));
    const b = await cdp.eval(FILL('密码', PASS));
    if (a !== 'ok' || b !== 'ok') throw new Error(`填表单失败：账号=${a} 密码=${b}`);
    await sleep(120);
    if ((await cdp.eval(CLICK_TEXT('登录'))) !== 'ok') throw new Error('找不到登录按钮');
    try {
      await waitFor(cdp, `!!document.querySelector('header')`, 8000, '登录后主界面');
      return;
    } catch (e) {
      const err = await cdp.eval(`(() => {
        const e = [...document.querySelectorAll('.text-red-500')].map(n => n.textContent.trim());
        return e.join(' | ');
      })()`);
      if (i === 2) throw new Error('登录失败：' + err);
    }
  }
}

async function openRoom(cdp, roomId) {
  const r = await cdp.eval(`(() => {
    const cards = [...document.querySelectorAll('div.cursor-pointer')]
      .filter(d => (d.textContent || '').includes(${JSON.stringify('#' + roomId)}));
    if (!cards.length) return 'NO_CARD';
    cards[0].click();
    return 'ok';
  })()`);
  if (r !== 'ok') throw new Error(`房间列表里找不到 #${roomId}`);
  await waitFor(cdp, `document.body.innerText.includes('主播 UID')`, 8000, `进入房间 #${roomId} 详情`);
}

function writeJson(name, obj) {
  fs.writeFileSync(path.join(DATA_DIR, name), JSON.stringify(obj, null, 2), 'utf8');
}

// ─────────────────────────── 主流程 ───────────────────────────
const results = [];
function check(label, ok, extra) {
  results.push({ label, ok: !!ok, extra });
  const tail = extra === undefined || extra === '' ? '' : ` -> ${extra}`;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${tail}`);
}

async function main() {
  const chromePath = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
  if (!chromePath) {
    console.log('SKIP：本机找不到 Chrome，跳过真浏览器 UI 回归');
    return 0;
  }

  const py = process.platform === 'win32'
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    : 'python3';

  let app = null;
  let chrome = null;
  let cdp = null;
  const children = [];

  const kill = (pid) => new Promise((res) => {
    if (!pid) return res();
    if (process.platform === 'win32') {
      execFile('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true }, () => res());
    } else {
      try { process.kill(pid, 'SIGKILL'); } catch { /* ignore */ }
      res();
    }
  });

  try {
    fs.rmSync(PROFILE, { recursive: true, force: true });

    // 1) 隔离的预览站（只用临时目录，不碰真实 rooms/）
    app = spawn(py, [path.join(ROOT, 'scripts', 'preview_ui.py'), String(APP_PORT)], {
      cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
      env: { ...process.env, AYABOT_PREVIEW_ROOT: PREVIEW_ROOT },
    });
    children.push(app.pid);
    let appLog = '';
    app.stdout.on('data', (d) => { appLog += d.toString(); });
    app.stderr.on('data', (d) => { appLog += d.toString(); });

    let up = false;
    for (let i = 0; i < 200; i++) {
      try {
        const r = await fetch(`http://127.0.0.1:${APP_PORT}/`);
        if (r.ok) { up = true; break; }
      } catch { /* 还没起来 */ }
      if (app.exitCode !== null) break;
      await sleep(150);
    }
    if (!up) throw new Error('预览站没起来：\n' + appLog.slice(-1500));
    console.log(`  预览站 http://127.0.0.1:${APP_PORT} 就绪`);
    console.log(`  Chrome ${chromePath}`);

    // 1.5) 给预览房间造一点真实数据：导出页的日历只让「有数据的日子」可选，
    //      空库的话「全选当月」永远是 1 天，这条路径就等于没测。
    //      用真 StatsStore 写，确保 DB 结构与生产一致（不手写 SQL）。
    const seedPy = path.join(SCRATCH, `seed_${process.pid}.py`);
    fs.mkdirSync(SCRATCH, { recursive: true });
    fs.writeFileSync(seedPy, `
import sys, time, json
from pathlib import Path
sys.path.insert(0, ${JSON.stringify(ROOT)})
from app.storage import GiftEvent, StatsStore
db = Path(${JSON.stringify(DATA_DIR)}) / "bot.db"
db.parent.mkdir(parents=True, exist_ok=True)
st = StatsStore(str(db))
now = int(time.time())
rows = [("辣条", 3, 300), ("小花花", 1, 100), ("情书", 1, 5200)]
for i, (name, num, coin) in enumerate(rows):
    ts = now - 86400 * (i + 1)
    payload = {"giftName": name, "num": num, "total_coin": coin}
    st.record_gift_event(GiftEvent(
        ts=ts, month=time.strftime("%Y-%m", time.localtime(ts)), uid=1234 + i,
        uname="观众" + str(i), event_type="SEND_GIFT", gift_name=name, gift_num=num,
        is_blind_box=0, blind_box_cost=0, actual_value=coin // 100, profit_value=0,
        raw_json=json.dumps(payload, ensure_ascii=False)))
    # 让用户维度也有日期（导出页按 UID 查询时要用）
if hasattr(st, "close"):
    st.close()
print("seeded", db)
`, 'utf8');
    const seed = require('child_process').spawnSync(py, [seedPy], { cwd: ROOT, encoding: 'utf8' });
    if (seed.status !== 0) {
      check('给预览房间造测试数据', false, String(seed.stderr || seed.stdout).slice(0, 200));
    } else {
      check('给预览房间造测试数据', true, String(seed.stdout).trim().slice(0, 60));
    }

    // 2) 真 Chrome（headless），只用本机已装的
    chrome = spawn(chromePath, [
      '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
      '--no-default-browser-check', '--disable-extensions', '--hide-scrollbars',
      '--disable-dev-shm-usage', '--remote-allow-origins=*',
      `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${PROFILE}`,
      '--window-size=1280,900', 'about:blank',
    ], { stdio: 'ignore', windowsHide: true });
    children.push(chrome.pid);

    let target = null;
    for (let i = 0; i < 200; i++) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
        target = list.find((t) => t.type === 'page');
        if (target && target.webSocketDebuggerUrl) break;
      } catch { /* 还没起来 */ }
      await sleep(150);
    }
    if (!target) throw new Error('Chrome DevTools 端口没起来（可能被占用：' + CDP_PORT + '）');

    cdp = new CDP(target.webSocketDebuggerUrl);
    await cdp.ready;

    // 3) 收错误：请求失败 / 页面异常 / console error
    const badAssets = [];
    const exceptions = [];
    const consoleErrors = [];
    cdp.on((m) => {
      if (m.method === 'Network.responseReceived') {
        const { response, type } = m.params;
        const url = response.url || '';
        if (response.status >= 400 && (type === 'Script' || type === 'Stylesheet'
          || type === 'Font' || type === 'Image' || /\.(js|css|woff2?|png|svg)(\?|$)/.test(url))) {
          badAssets.push(`${response.status} ${url}`);
        }
      }
      if (m.method === 'Network.loadingFailed' && !m.params.canceled) {
        badAssets.push(`${m.params.errorText} ${m.params.requestId}`);
      }
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails;
        exceptions.push(d.exception?.description || d.text || '?');
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        consoleErrors.push(m.params.args.map((a) => a.value || a.description || '').join(' '));
      }
    });

    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Network.enable');
    await cdp.send('Page.navigate', { url: `http://127.0.0.1:${APP_PORT}/` });
    await waitFor(cdp, `document.readyState === 'complete'`, 20000, '页面加载');

    // ── 阶段一：前端自己先得能跑起来 ──
    console.log('[1] 前端库自带、Vue 挂载');
    await waitFor(cdp, `document.getElementById('app') && !document.getElementById('app').hasAttribute('v-cloak')`,
      15000, 'Vue 挂载（v-cloak 移除）');
    check('Vue 已挂载（不再是白屏）', true);
    check('Vue 库来自本机 /vendor/，不依赖 CDN',
      await cdp.eval(`(() => {
        const e = performance.getEntriesByType('resource').filter(x => /vue/i.test(x.name));
        return e.length > 0 && e.every(x => new URL(x.name).origin === location.origin);
      })()`));
    check('页面没有 JS 异常', exceptions.length === 0, exceptions.slice(0, 2).join(' | '));
    check('没有资源加载失败（400+）', badAssets.length === 0, badAssets.slice(0, 3).join(' | '));

    console.log('[2] 登录 + 房间列表');
    await login(cdp);
    await waitFor(cdp, `document.body.innerText.includes('#${ROOM}')`, 15000, '房间列表出现');
    check('登录成功并列出房间', true);
    check('两个房间都在', await cdp.eval(
      `document.body.innerText.includes('#${ROOM}') && document.body.innerText.includes('#${ROOM2}')`));

    // ── 阶段一·补：刷新页面不能要求重新登录 ──
    // session cookie 是 httponly 的，`document.cookie` 永远读不到它。
    // 前端以前就是用 document.cookie 猜登录态 → 每按一次 F5 都弹登录框，
    // 而服务端会话其实还有效（实测刷新后 /api/rooms 仍 200）。
    console.log('[1.5] 刷新页面保持登录');
    await cdp.send('Page.reload', { ignoreCache: true });
    await waitFor(cdp, `document.readyState === 'complete'`, 20000, '刷新');
    let keptLogin = false;
    try {
      await waitFor(cdp, `!!document.querySelector('header')`, 12000, '刷新后主界面');
      keptLogin = true;
    } catch { /* 记失败 */ }
    const loginFormAfterReload = await cdp.eval(
      `!!document.querySelector('input[placeholder="账号"]')`);
    check('**刷新后仍然在主界面**（不用重新登录）', keptLogin && !loginFormAfterReload,
      `header=${keptLogin} 登录框=${loginFormAfterReload}`);
    const stillAuthed = await cdp.eval(
      `fetch('/api/rooms',{credentials:'include'}).then(r=>r.status)`);
    check('刷新后会话在服务端依然有效', stillAuthed === 200, stillAuthed);

    // ── 阶段二：三个用户真实抱怨的界面状态 ──
    // 一次性把三种状态写进去，再刷新页面 —— 用户看到这些提示的场景就是「打开后台」。
    console.log('[3] 凭证失效 / 崩溃 / 礼物丢统计 三种提示');
    fs.mkdirSync(DATA_DIR, { recursive: true });
    const nowSec = Math.floor(Date.now() / 1000);
    writeJson('bot_status.json', {
      ts: nowSec, status: 'running', gift_seen: 9, gift_recorded: 0,
      gift_dropped_dup: 0, gift_dropped_decode: 0, dropped_no_uid: 0, gift_dropped_no_uname: 0,
    });
    writeJson('auth_status.json', { status: 'needs_login', reason: 'Cookie 已过期或被踢下线' });
    writeJson('crash_info.json', {
      ts: nowSec, paused: true, reason: '测试用：连续崩溃', exit_code: 1,
      retry_at: nowSec + 600,
    });
    // 日志面板是 v-html —— 塞一条 XSS 载荷，必须只当文本显示
    fs.writeFileSync(path.join(PREVIEW_ROOT, 'rooms', ROOM, 'bot.log'),
      '2026-11-25 10:00:00,000 [INFO] bili-live-robot: 正常一行\n'
      + '2026-11-25 10:00:01,000 [INFO] bili-live-robot: <img src=x onerror="window.__XSS=1">\n'
      + '2026-11-25 10:00:02,000 [ERROR] bili-live-robot: <script>window.__XSS=2</script>\n',
      'utf8');

    await cdp.send('Page.reload', { ignoreCache: true });
    await waitFor(cdp, `document.readyState === 'complete'`, 20000, '刷新后加载');
    await login(cdp);
    await waitFor(cdp, `document.body.innerText.includes('#${ROOM}')`, 15000, '刷新后房间列表');

    check('列表卡片上有「登录已失效」红标',
      await cdp.eval(`document.body.innerText.includes('登录已失效')`));

    await openRoom(cdp, ROOM);
    const text = () => cdp.eval('document.body.innerText');
    let body = await text();

    check('凭证失效：说出「B站 登录凭证已失效」而不是甩后端报错',
      body.includes('登录凭证已失效'));
    check('凭证失效：给出去扫码的按钮',
      await cdp.eval(`[...document.querySelectorAll('button')]
        .some(b => (b.textContent || '').includes('去扫码登录'))`));
    check('凭证失效：说明「扫码后自动恢复，无需重启」',
      body.includes('自动恢复') && body.includes('无需重启'));

    check('崩溃：提示已暂停自动重启', body.includes('暂停自动重启'));
    check('崩溃：写出「下次自动重试」的具体时间（不是只有原因）',
      /下次自动重试：\d{2}-\d{2}\s+\d{2}:\d{2}/.test(body));
    check('崩溃：原因原样透出', body.includes('连续崩溃'));

    check('礼物：收到但一条没记上要明确报出来', body.includes('收到礼物但没能记进统计'));
    check('礼物：说出「排行/统计会是 0」这个后果', body.includes('排行/统计会是 0'));

    console.log('[4] 日志面板不许执行注入');
    await cdp.eval(CLICK_TEXT('日志'));
    await waitFor(cdp, `(() => {
      const el = document.querySelector('.whitespace-pre-wrap.font-mono');
      return !!el && (el.textContent || '').includes('正常一行');
    })()`, 15000, '日志面板渲染');
    const xss = await cdp.eval(`(() => {
      const box = document.querySelector('.whitespace-pre-wrap.font-mono');
      return {
        flag: window.__XSS === undefined ? 0 : window.__XSS,
        img: !!box.querySelector('img'),
        script: !!box.querySelector('script'),
        literal: (box.textContent || '').includes('onerror'),
        rendered: box.innerHTML.includes('&lt;img'),
      };
    })()`);
    check('日志里的 <img onerror> 没有执行', xss.flag === 0, `__XSS=${xss.flag}`);
    check('没有真的插进 <img>/<script> 节点', !xss.img && !xss.script);
    check('载荷以文本形式显示（转义成 &lt;img）', xss.literal && xss.rendered);

    console.log('[5] 换房间');
    // .click() 不是真手势，Vue 的 @click 挂在根上靠事件冒泡 —— 直接派发点击事件即可
    const backOk = await cdp.eval(CLICK_TEXT('返回房间列表'));
    if (backOk !== 'ok') throw new Error('找不到「返回房间列表」');
    await waitFor(cdp, `document.body.innerText.includes('#${ROOM2}')`, 8000, '回到列表');
    await openRoom(cdp, ROOM2);
    const hdr = await text();
    // 详情页头显示的是房间名；第二个房间在预览目录里叫「测试房间02」
    check('换到第二个房间后头部换成它', hdr.includes('测试房间02'), hdr.slice(0, 60).replace(/\s+/g, ' '));
    check('换房间后不再残留上一个房间的凭证提示', !hdr.includes('登录凭证已失效'));

    // 上一个房间的 bot.log 不能继续挂在这个房间的「日志」标签上
    // （以前 _clearRoomState 不清 botLogContent，两间房的日志长得一模一样）
    await cdp.eval(CLICK_TEXT('日志'));
    await sleep(800);
    const log2 = await cdp.eval(`(() => {
      const box = document.querySelector('.whitespace-pre-wrap.font-mono');
      return { found: !!box, text: box ? (box.textContent || '') : '' };
    })()`);
    // 必须真的在「日志」标签里再看：元素不在的话 textContent 也是空，
    // 那种「因为没渲染所以没残留」的通过等于没测。
    check('换房间后日志面板确实是空的（不是没渲染）', log2.found,
      log2.found ? '在页面上' : '找不到日志面板');
    check('换房间后日志面板不残留别的房间的日志', !log2.text.includes('正常一行'),
      JSON.stringify(log2.text.slice(0, 40)));

    // ── 阶段三：迟到的响应 ──
    // 上舰记录是进房间就自动查的；库大时查一次要一两秒。手快切个房间，
    // 上家的响应回来会把数据写进当前页面 —— 排行/弹幕/日志长得一样，看错都不知道。
    // 用 CDP 的 Fetch 域把房间 A 的日志请求扣住，切到 B 之后再放回去。
    console.log('[6] 切房间时迟到的响应');
    const pausedReqs = [];
    cdp.on((m) => { if (m.method === 'Fetch.requestPaused') pausedReqs.push(m.params); });
    const waitPaused = async (pred, timeoutMs, label) => {
      const t0 = Date.now();
      while (Date.now() - t0 < timeoutMs) {
        const i = pausedReqs.findIndex(pred);
        if (i >= 0) return pausedReqs.splice(i, 1)[0];
        await sleep(50);
      }
      throw new Error('等不到被拦下的请求：' + label);
    };

    try {
      await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*/api/rooms/*/log?*' }] });
      const logBox = `(() => {
        const b = document.querySelector('.whitespace-pre-wrap.font-mono');
        return b ? (b.textContent || '') : 'NOBOX';
      })()`;

      await cdp.eval(CLICK_TEXT('返回房间列表'));
      await waitFor(cdp, `document.body.innerText.includes('#${ROOM}')`, 8000, '回到列表');
      await openRoom(cdp, ROOM);
      await cdp.eval(CLICK_TEXT('日志'));
      // 房间 A 有 bot.log（上面写过），请求被扣住 → 界面还拿不到
      const reqA = await waitPaused(
        (p) => p.request.url.includes(`/rooms/${ROOM}/log`), 10000, '房间 A 的日志');

      await cdp.eval(CLICK_TEXT('返回房间列表'));
      await waitFor(cdp, `document.body.innerText.includes('#${ROOM2}')`, 8000, '回到列表 2');
      await openRoom(cdp, ROOM2);
      await cdp.eval(CLICK_TEXT('日志'));
      const reqB = await waitPaused(
        (p) => p.request.url.includes(`/rooms/${ROOM2}/log`), 10000, '房间 B 的日志');
      await cdp.send('Fetch.continueRequest', { requestId: reqB.requestId });
      await sleep(500);
      const beforeLate = await cdp.eval(logBox);
      check('房间 B 自己没日志 → 面板是空的', !beforeLate.includes('正常一行'),
        beforeLate === 'NOBOX' ? 'NOBOX' : JSON.stringify(beforeLate.slice(0, 30)));

      // 现在才放行房间 A 的响应 —— 它已经是一个「过期的」结果
      await cdp.send('Fetch.continueRequest', { requestId: reqA.requestId });
      await sleep(1000);
      const afterLate = await cdp.eval(logBox);
      check('**迟到的房间 A 日志没有写进房间 B 的页面**', !afterLate.includes('正常一行'),
        JSON.stringify(String(afterLate).slice(0, 40)));
      await cdp.send('Fetch.disable').catch(() => {});
    } catch (e) {
      check('切房间迟到响应（CDP 拦截不可用则跳过）', false, String(e.message || e).slice(0, 80));
    }

    // ── 阶段四：把每一个标签都点一遍 ──
    // 用户报「点开某个页面就白/就报错」时，我们从来没有一条断言能自动发现：
    // 房间详情 7 个子标签 + 3 个顶层标签，只有「日志」一条被覆盖过。
    // 主界面任何一处渲染抛错，Vue 都会把那一块留成空白，用户看到的就是「坏了」。
    console.log('[7] 每个标签都要能打开');
    await cdp.send('Page.reload', { ignoreCache: true });
    await waitFor(cdp, `document.readyState === 'complete'`, 20000, '刷新（走标签前）');
    await login(cdp);
    await waitFor(cdp, `document.body.innerText.includes('#${ROOM}')`, 15000, '房间列表');
    await openRoom(cdp, ROOM);

    const SUBTABS = [
      { label: '送礼排行', expect: '上舰' },
      { label: '精美导出', expect: '所有人' },   // 日历要点开才出现，这里只断言面板本体
      { label: '弹幕记录', expect: '弹幕' },
      { label: 'AI回复', expect: 'LLM' },
      { label: '机器人配置', expect: '机器人配置' },
      { label: '数据管理', expect: '数据管理' },
      { label: '日志', expect: 'Bot 日志' },
    ];
    const TOPTABS = [
      { label: '全局配置', expect: '全局配置' },
      { label: '用户管理', expect: '用户管理' },
      { label: '帮助', expect: '使用指南' },
    ];

    let excMark = exceptions.length;
    let conMark = consoleErrors.length;
    const tabFailures = [];

    for (const t of SUBTABS) {
      const clicked = await cdp.eval(CLICK_TEXT(t.label));
      if (clicked !== 'ok') { tabFailures.push(`${t.label}: 找不到标签按钮`); continue; }
      let shown = false;
      try {
        await waitFor(cdp, `document.body.innerText.includes(${JSON.stringify(t.expect)})`,
          10000, `子标签 ${t.label} 内容`);
        shown = true;
      } catch { /* 下面记失败 */ }
      const newExc = exceptions.slice(excMark);
      const newCon = consoleErrors.slice(conMark)
        .filter((s) => !/favicon|Failed to load resource/i.test(s));
      excMark = exceptions.length;
      conMark = consoleErrors.length;
      if (!shown) tabFailures.push(`${t.label}: 面板没有渲染出「${t.expect}」`);
      if (newExc.length) tabFailures.push(`${t.label}: 渲染抛异常 ${newExc[0].slice(0, 60)}`);
      if (newCon.length) tabFailures.push(`${t.label}: console error ${newCon[0].slice(0, 60)}`);
    }
    check('房间详情 7 个子标签全部能打开且不报错', tabFailures.length === 0,
      tabFailures.join(' / '));

    // 导出页的日历是「点日期框才展开」的，所以要先把日历打开再断言。
    // 这一页出过真 bug（731baa4：进页面没加载日期 → 整月全灰、点不动），
    // 所以这里不只断言「渲染出来了」，还要断言「全选当月 → 确定」这条实际路径能走完。
    // 上面的循环最后停在「日志」，断言日历前必须先切回「精美导出」
    await cdp.eval(CLICK_TEXT('精美导出'));
    await waitFor(cdp, `document.body.innerText.includes('所有人')`, 8000, '回到精美导出');
    const clickedDate = await cdp.eval(`(() => {
      const el = [...document.querySelectorAll('input[placeholder="点击选择日期"]')][0];
      if (!el) return 'NO_INPUT';
      el.click();
      return 'ok';
    })()`);
    check('导出页有日期输入框', clickedDate === 'ok', clickedDate);
    // eDateLabel 曾经没出现在 setup 的 return 里 → 这个框永远是空的（选了哪天看不见）
    const dateShown = await cdp.eval(`(() => {
      const el = [...document.querySelectorAll('input[placeholder="点击选择日期"]')][0];
      return el ? el.value : 'NO_INPUT';
    })()`);
    check('**日期框显示出了当前日期**（不是空框）', /^\d{4}-\d{2}-\d{2}$/.test(dateShown),
      JSON.stringify(dateShown));
    let calOpen = false;
    try {
      await waitFor(cdp, `document.body.innerText.includes('全选当月')`, 5000, '日历展开');
      calOpen = true;
    } catch { /* 记失败 */ }
    check('点日期框能展开日历', calOpen);
    const dayCells = await cdp.eval(`(() => {
      const grid = [...document.querySelectorAll('div.grid.grid-cols-7.gap-1')];
      const last = grid[grid.length - 1];
      return last ? last.querySelectorAll('button').length : -1;
    })()`);
    check('日历画出了日期格子（28~31 天）', dayCells >= 28 && dayCells <= 31, dayCells);

    if (calOpen) {
      const all = await cdp.eval(CLICK_TEXT('全选当月'));
      check('「全选当月」点得动', all === 'ok', all);
      let multiShown = false;
      try {
        await waitFor(cdp, `/确定（\\d+天）/.test(document.body.innerText)`, 5000, '多选确认按钮');
        multiShown = true;
      } catch { /* 记失败 */ }
      const nDays = await cdp.eval(`(() => {
        const m = document.body.innerText.match(/确定（(\\d+)天）/);
        return m ? Number(m[1]) : -1;
      })()`);
      // 数据是分布在本月的 3 天 + 默认今天，所以「全选当月」至少要选中 3 天
      check('「全选当月」选中了有数据的那些天（≥ 3 天）', multiShown && nDays >= 3, nDays);

      const applied = await cdp.eval(CLICK_TEXT('确定（'));
      check('「确定」点得动', applied === 'ok', applied);
      const dateVal = await cdp.eval(`(() => {
        const el = [...document.querySelectorAll('input[placeholder="点击选择日期"]')][0];
        return el ? el.value : '';
      })()`);
      // 多选后输入框会显示成一段区间/多日期，绝不能是空的
      check('选完之后日期框显示出来了（不是空）', !!dateVal && dateVal.length > 3, JSON.stringify(dateVal));
    }

    const topFailures = [];
    excMark = exceptions.length;
    conMark = consoleErrors.length;
    for (const t of TOPTABS) {
      const clicked = await cdp.eval(CLICK_TEXT(t.label));
      if (clicked !== 'ok') { topFailures.push(`${t.label}: 找不到按钮`); continue; }
      let shown = false;
      try {
        await waitFor(cdp, `document.body.innerText.includes(${JSON.stringify(t.expect)})`,
          10000, `顶层标签 ${t.label}`);
        shown = true;
      } catch { /* 记失败 */ }
      const newExc = exceptions.slice(excMark);
      const newCon = consoleErrors.slice(conMark)
        .filter((s) => !/favicon|Failed to load resource/i.test(s));
      excMark = exceptions.length;
      conMark = consoleErrors.length;
      if (!shown) topFailures.push(`${t.label}: 没渲染`);
      if (newExc.length) topFailures.push(`${t.label}: 抛异常 ${newExc[0].slice(0, 60)}`);
      if (newCon.length) topFailures.push(`${t.label}: console error ${newCon[0].slice(0, 60)}`);
    }
    check('全局配置 / 用户管理 / 帮助 全部能打开且不报错', topFailures.length === 0,
      topFailures.join(' / '));

    // ── 阶段五：退出登录必须真的注销 ──
    // 以前 doLogout 只写 `document.cookie='session=;max-age=0'`，
    // 而 cookie 是 httponly 的 —— 这行代码改不动它，token 一直有效到过期。
    console.log('[8] 退出登录');
    await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('button')]
        .find(x => (x.textContent || '').includes('preview') && (x.textContent || '').includes('▾'));
      if (b) b.click();
      return 'ok';
    })()`);
    await sleep(300);
    const logoutClicked = await cdp.eval(CLICK_TEXT('退出登录'));
    check('找到「退出登录」', logoutClicked === 'ok', logoutClicked);
    await sleep(1500);
    const backToLogin = await cdp.eval(
      `!!document.querySelector('input[placeholder="账号"]')`);
    check('退回登录框', backToLogin);
    const afterLogout = await cdp.eval(
      `fetch('/api/rooms',{credentials:'include'}).then(r=>r.status)`);
    check('**服务端会话真的失效了**（不是只把界面藏起来）', afterLogout === 401, afterLogout);
    // 刷新一次确认没有「退出后又自己登回去」
    await cdp.send('Page.reload', { ignoreCache: true });
    await waitFor(cdp, `document.readyState === 'complete'`, 20000, '注销后刷新');
    await sleep(1200);
    check('注销后刷新仍停在登录框',
      await cdp.eval(`!!document.querySelector('input[placeholder="账号"]')`));

    check('全程没有未捕获的页面异常', exceptions.length === 0,
      exceptions.slice(0, 2).join(' | '));
    const hardConsole = consoleErrors.filter((s) => !/favicon|404|Failed to load resource/i.test(s));
    check('没有 JS console error', hardConsole.length === 0, hardConsole.slice(0, 2).join(' | '));
  } finally {
    try { cdp?.close(); } catch { /* ignore */ }
    if (KEEP) {
      console.log(`\n--keep：预览站 :${APP_PORT}，CDP :${CDP_PORT} 保持运行，自行清理`);
    } else {
      for (const pid of children.reverse()) await kill(pid);
      await sleep(300);
      fs.rmSync(PROFILE, { recursive: true, force: true });
    }
  }

  const failed = results.filter((r) => !r.ok);
  console.log('');
  if (failed.length) {
    console.log(`FAILED ${failed.length} 项: ${failed.map((f) => f.label).join(' / ')}`);
    return 1;
  }
  console.log(`UI DOM OK (${results.length} 项断言)`);
  return 0;
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('FAIL ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
