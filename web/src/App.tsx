import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { TerminalView } from './TerminalView';
import { BufferReceiver } from './buffer';
import { commonKeys, extraKeys } from './keys';
import { RelayClock, disconnected, sameTarget, statuses, targetKey, utf8Size, type Device, type Message,
  type Receipt, type Snapshot, type Target, type WindowInfo } from './protocol';

async function api(path: string, body?: unknown) {
  const response = await fetch('/api' + path, { credentials: 'same-origin',
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入');
  return data;
}

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [devices, setDevices] = useState<Device[]>([]);
  const [connection, setConnection] = useState('连接中');
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<WindowInfo | null>(null);
  const [owned, setOwned] = useState(false);
  const [available, setAvailable] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [historyProgress, setHistoryProgress] = useState<number | null>(null);
  const [historyNotice, setHistoryNotice] = useState('');
  const [pinned, setPinned] = useState(false);
  const [keyMode, setKeyMode] = useState(false);
  const [compactViewport, setCompactViewport] = useState(() =>
    window.matchMedia('(max-width: 640px), (max-height: 620px)').matches);
  const [moreKeys, setMoreKeys] = useState(false);
  const [deviceOpen, setDeviceOpen] = useState(false);
  const [receiptOpen, setReceiptOpen] = useState(false);
  const receiver = useRef(new BufferReceiver());
  const [fontSize, setFontSize] = useState(14);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [composing, setComposing] = useState(false);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [creatingWindow, setCreatingWindow] = useState(false);
  const [createNotice, setCreateNotice] = useState('');
  const [pairOpen, setPairOpen] = useState(false);
  const [pairCode, setPairCode] = useState('');
  const [pairInfo, setPairInfo] = useState<{ id: string; name: string } | null>(null);
  const [pairNotice, setPairNotice] = useState('');
  const clock = useRef(new RelayClock());
  const socket = useRef<WebSocket | null>(null);
  const selection = useRef<WindowInfo | null>(null);
  const latestScreen = useRef<Snapshot | null>(null);
  // Once full history is on screen, screen-only snapshots must not replace it while it resyncs
  // (reconnect, resync, transfer error); otherwise the reader's position is lost.
  const historyShown = useRef(false);
  const composingRef = useRef(false);
  const seq = useRef(0);
  const subscribed = useRef(false);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const createPending = useRef<{ requestId: string; source: Target } | null>(null);
  const createTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const media = window.matchMedia('(max-width: 640px), (max-height: 620px)');
    const update = () => { setCompactViewport(media.matches); if (!media.matches) setKeyMode(false); };
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  const send = useCallback((message: object) => {
    if (socket.current?.readyState !== WebSocket.OPEN || socket.current.bufferedAmount > 1024 * 1024) {
      setError('连接不可用，输入尚未发送'); return false;
    }
    socket.current.send(JSON.stringify({ v: 1, ...message })); return true;
  }, []);

  const selectWindow = useCallback((w: WindowInfo | null) => {
    selection.current = w; setSelected(w); setOwned(false); setAvailable(false);
    seq.current = 0; setSnapshot(null);
    latestScreen.current = null; historyShown.current = false; setHistoryNotice('');
    setError('');
    receiver.current = new BufferReceiver(); setHistoryProgress(w ? 0 : null);
    setDeviceOpen(false); setMoreKeys(false); setKeyMode(false);
    if (w) subscribed.current = send({ type: 'subscribe', target: w.target, buffer: true });
    else { subscribed.current = false; send({ type: 'unsubscribe' }); }
  }, [send]);

  useEffect(() => {
    if (!pinned || !authenticated) return;
    const top = window.scrollY;
    document.documentElement.classList.add('console-pinned');
    const update = () => {
      const view = window.visualViewport;
      document.documentElement.style.setProperty('--console-height', `${view?.height ?? window.innerHeight}px`);
      document.documentElement.style.setProperty('--console-top', `${view?.offsetTop ?? 0}px`);
    };
    update();
    window.visualViewport?.addEventListener('resize', update);
    window.visualViewport?.addEventListener('scroll', update);
    window.addEventListener('resize', update);
    return () => {
      document.documentElement.classList.remove('console-pinned');
      document.documentElement.style.removeProperty('--console-height');
      document.documentElement.style.removeProperty('--console-top');
      window.visualViewport?.removeEventListener('resize', update);
      window.visualViewport?.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
      window.scrollTo(0, top);
    };
  }, [pinned, authenticated]);

  useEffect(() => { api('/auth/me').then(() => setAuthenticated(true)).catch(() => setAuthenticated(false)); }, []);
  useEffect(() => {
    if (!authenticated) return;
    let stopped = false, attempts = 0, reconnect: ReturnType<typeof setTimeout>;
    let lastSeen = Date.now();
    const open = () => {
      if (stopped) return;
      setConnection(attempts ? '正在重连' : '连接中');
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/browser`);
      socket.current = ws; clock.current = new RelayClock();
      ws.onopen = () => { attempts = 0; lastSeen = Date.now(); setConnected(true); setConnection('已连接'); };
      ws.onmessage = event => {
        lastSeen = Date.now();
        const msg = JSON.parse(event.data) as Message;
        if (msg.v !== 1) { ws.close(); return; }
        if (msg.type === 'ping') { send({ type: 'ping' }); return; }
        if (msg.type === 'state') {
          clock.current.sync(msg.server_time);
          setDevices(msg.devices);
          if (selection.current) {
            const found = msg.devices.flatMap(d => d.windows).find(w => sameTarget(w.target, selection.current?.target));
            if (!found) {
              selection.current = null; setSelected(null); setOwned(false); setSnapshot(null);
              subscribed.current = false;
              latestScreen.current = null; historyShown.current = false;
              receiver.current = new BufferReceiver(); setHistoryProgress(null); setPinned(false);
            } else {
              selection.current = found; setSelected(found);
              if (!subscribed.current) subscribed.current = send({ type: 'subscribe', target: found.target, buffer: true });
            }
          }
        } else if ((msg.type === 'buffer_start' || msg.type === 'buffer_chunk' || msg.type === 'buffer_end') && sameTarget(msg.target, selection.current?.target)) {
          if (msg.type !== 'buffer_start' && !receiver.current.pending) return;
          try {
            const complete = receiver.current.accept(msg);
            setHistoryProgress(receiver.current.current ? null : receiver.current.progress);
            if (complete) {
              setHistoryNotice('');
              historyShown.current = true; setSnapshot(complete);
            }
          } catch {
            receiver.current = new BufferReceiver(); setHistoryProgress(0);
            setHistoryNotice('历史同步中断，正在重新获取完整内容'); send({ type: 'buffer_resync' });
          }
        } else if (msg.type === 'snapshot' && sameTarget(msg.target, selection.current?.target)) {
          if (msg.seq <= seq.current) return;
          seq.current = msg.seq;
          latestScreen.current = msg;
          if (receiver.current.current || historyShown.current) return;
          setSnapshot(msg);
        } else if (msg.type === 'lease' && sameTarget(msg.target, selection.current?.target)) {
          setOwned(msg.owned); setAvailable(msg.available);
        } else if (msg.type === 'receipt') {
          if (msg.status !== 'received') { clearTimeout(timers.current.get(msg.request_id)); timers.current.delete(msg.request_id); }
          setReceipts(old => old.map(r => r.request_id === msg.request_id ? msg : r));
        } else if (msg.type === 'create_window_result') {
          const pending = createPending.current;
          if (!pending || pending.requestId !== msg.request_id || !sameTarget(pending.source, msg.target)) return;
          if (msg.status === 'received') return;
          if (createTimer.current) clearTimeout(createTimer.current);
          createTimer.current = null; createPending.current = null; setCreatingWindow(false);
          if (msg.status === 'created' && msg.window) {
            setCreateNotice('');
            if (sameTarget(selection.current?.target, pending.source)) selectWindow(msg.window);
          } else {
            setCreateNotice(msg.detail || (msg.status === 'uncertain'
              ? '创建结果不确定，请先检查窗口列表；不会自动重试'
              : '创建窗口失败'));
          }
        } else if (msg.type === 'error') setError(msg.detail);
        else if ((msg.type === 'target_error' || msg.type === 'history_error') && sameTarget(msg.target, selection.current?.target)) {
          if (msg.type === 'target_error') {
            setError(msg.detail);
            subscribed.current = false; setOwned(false); setSnapshot(null);
            latestScreen.current = null; historyShown.current = false;
            receiver.current = new BufferReceiver(); setHistoryProgress(null);
          } else {
            setHistoryNotice(msg.detail);
            receiver.current = new BufferReceiver(); setHistoryProgress(null);
            historyShown.current = false; setSnapshot(latestScreen.current);
          }
        }
      };
      ws.onclose = event => {
        if (stopped) return;
        if (createPending.current) {
          createPending.current = null; setCreatingWindow(false);
          setCreateNotice('连接中断，创建结果不确定；请先检查窗口列表');
          if (createTimer.current) clearTimeout(createTimer.current);
          createTimer.current = null;
        }
        setConnected(false); setOwned(false); setConnection('连接已断开'); subscribed.current = false;
        seq.current = 0; setReceipts(disconnected);
        receiver.current = new BufferReceiver(); setHistoryProgress(null);
        timers.current.forEach(clearTimeout); timers.current.clear();
        if (event.code === 4401 || event.code === 4403) { setAuthenticated(false); return; }
        reconnect = setTimeout(open, Math.min(30000, 1000 * 2 ** attempts++));
      };
      ws.onerror = () => ws.close();
    };
    open();
    const heartbeat = setInterval(() => {
      if (socket.current?.readyState === WebSocket.OPEN) {
        if (receiver.current.pending && performance.now() - receiver.current.started > 30000) {
          receiver.current = new BufferReceiver(); setHistoryProgress(0);
          setHistoryNotice('历史传输超时，正在重新同步'); send({ type: 'buffer_resync' });
        }
        if (Date.now() - lastSeen > 30000) socket.current.close();
        else send({ type: 'ping' });
      }
    }, 10000);
    const wake = () => { if (document.visibilityState === 'visible' && Date.now() - lastSeen > 30000) socket.current?.close(); };
    document.addEventListener('visibilitychange', wake);
    return () => {
      stopped = true; clearTimeout(reconnect); clearInterval(heartbeat);
      document.removeEventListener('visibilitychange', wake);
      socket.current?.close(); socket.current = null;
      timers.current.forEach(clearTimeout); timers.current.clear();
      if (createTimer.current) clearTimeout(createTimer.current);
      createTimer.current = null; createPending.current = null;
    };
  }, [authenticated, send, selectWindow]);

  const login = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try { await api('/auth/login', { username, password }); setPassword(''); setAuthenticated(true); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const logout = async () => {
    try { await api('/auth/logout', {}); setAuthenticated(false); setDevices([]); setSelected(null);
      selection.current = null; latestScreen.current = null; historyShown.current = false; setSnapshot(null); setDrafts({}); setReceipts([]); setPinned(false); setKeyMode(false); setHistoryNotice('');
      setCreatingWindow(false); setCreateNotice(''); createPending.current = null;
      if (createTimer.current) clearTimeout(createTimer.current); createTimer.current = null;
      receiver.current = new BufferReceiver(); }
    catch (e) { setError((e as Error).message); }
  };
  const target = selected?.target;
  const draftKey = target ? targetKey(target) : '';
  const draft = drafts[draftKey] || '';
  const canControl = connected && owned && !!selected;
  const compactPinned = pinned && compactViewport;
  const createWindow = () => {
    if (!selected || !canControl || createPending.current) return;
    const expires_at = clock.current.deadline();
    if (expires_at === null) { setCreateNotice('等待服务时间同步，请稍后重试'); return; }
    const requestId = crypto.randomUUID();
    const source = selected.target;
    if (!send({ type: 'create_window', request_id: requestId, target: source, expires_at })) return;
    createPending.current = { requestId, source }; setCreatingWindow(true); setCreateNotice('');
    createTimer.current = setTimeout(() => {
      if (createPending.current?.requestId !== requestId) return;
      createPending.current = null; createTimer.current = null; setCreatingWindow(false);
      setCreateNotice('等待创建结果超时，请先检查窗口列表；不会自动重试');
    }, 16000);
  };
  const submit = (op: 'text' | 'text_enter' | 'keys', key?: string) => {
    if (!target || !canControl || composingRef.current) return;
    if (op !== 'keys' && (!draft || utf8Size(draft) > 65536)) { setError('文字不能为空，且不能超过 64 KiB'); return; }
    const expires_at = clock.current.deadline();
    if (expires_at === null) { setError('等待服务时间同步，请稍后重试'); return; }
    const request_id = crypto.randomUUID();
    const request = { type: 'input', request_id, target, op, text: op === 'keys' ? '' : draft,
      keys: key ? [key] : [], expires_at };
    if (!send(request)) return;
    if (op !== 'keys') {
      setDrafts(old => old[draftKey] === draft ? { ...old, [draftKey]: '' } : old);
    }
    setReceipts(old => [{ request_id, target, status: 'pending', detail: '' } as Receipt, ...old].slice(0, 30));
    timers.current.set(request_id, setTimeout(() => {
      setReceipts(old => old.map(r => r.request_id === request_id && ['pending','received'].includes(r.status)
        ? { ...r, status: 'uncertain', detail: '回执超时，请查看屏幕后判断；不会自动重发' } : r));
      timers.current.delete(request_id);
    }, 16000));
    setError('');
  };
  const lookup = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try { setPairInfo(await api('/pairings/lookup', { code: pairCode })); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const approve = async () => {
    if (!pairInfo) return;
    setBusy(true);
    try { await api('/pairings/approve', { code: pairCode, pairing_id: pairInfo.id });
      setPairInfo(null); setPairCode(''); setPairNotice('已确认配对，等待电脑代理上线。'); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const revoke = async (device: Device) => {
    if (!confirm(`撤销「${device.name}」的连接凭据？之后需要重新配对。`)) return;
    try { await api(`/devices/${device.id}/revoke`, {}); } catch (e) { setError((e as Error).message); }
  };

  if (authenticated === null) return <main className="loading">正在连接你的工作台…</main>;
  if (!authenticated) return <main className="login-layout">
    <section className="intro"><div className="wordmark"><span className="logo">k↗</span> KITTY REMOTE</div>
      <p className="eyebrow">YOUR TERMINAL. WITH YOU.</p><h1>离开电脑，<br/>接着做。</h1>
      <p className="intro-copy">把正在进行的工作带在身边。<br/>查看终端、发送任务，回去时继续同一段会话。</p>
      <div className="intro-line"><span>01 / 随身工作台</span><span>PRIVATE CONNECTION ↗</span></div></section>
    <section className="login-card"><span className="eyebrow">欢迎回来</span><h2>连接你的工作台</h2>
      <p className="muted">使用私有中转服务的管理员账号登录。</p><form onSubmit={login}>
        <label>账号<input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required maxLength={64}/></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required maxLength={256}/></label>
        {error && <p role="alert" className="error">{error}</p>}
        <button className="primary login-button" disabled={busy}>{busy ? '正在登录…' : '进入工作台 ↗'}</button>
      </form><p className="login-foot">终端任务继续在你的电脑上运行。</p></section></main>;

  return <div className={`workspace ${selected ? 'has-selection' : ''} ${pinned ? 'is-pinned' : ''} ${deviceOpen ? 'devices-open' : ''}`}><header className="topbar"><div className="wordmark"><span className="logo">k↗</span><span title={pinned ? selected?.title : undefined}>{pinned ? selected?.title || '终端窗口' : 'KITTY REMOTE'}<small>随身终端</small></span></div>
    <div className="top-actions">{selected && <button className="quiet window-toggle" aria-expanded={deviceOpen} onClick={() => setDeviceOpen(!deviceOpen)}>窗口</button>}{pinned && receipts.length > 0 && <button className="quiet" onClick={() => setReceiptOpen(!receiptOpen)}>回执</button>}<span className={`connection ${connected ? 'online' : ''}`}><i/>{connection}</span><button className="quiet" onClick={logout}>退出</button></div></header>
    <div className="workspace-body"><aside className="sidebar"><div className="section-heading"><div><p className="eyebrow">WORKSPACES</p><h2>我的电脑</h2></div><button className="icon-button" aria-label="配对电脑" onClick={() => { setPairOpen(true); setPairNotice(''); }}>＋</button></div>
      {selected && <button className="close-devices" onClick={() => setDeviceOpen(false)}>关闭窗口列表</button>}
      {devices.length === 0 && <div className="sidebar-empty"><span>⌘</span><p>第一台电脑，<br/>从这里接入。</p><button onClick={() => setPairOpen(true)}>配对电脑 ↗</button></div>}
      {devices.map(device => <section className="device" key={device.id}><div className="device-heading"><strong>{device.name}</strong><span className={device.online ? 'live-label' : 'muted'}>{device.online ? '在线' : '离线'}</span></div>
        {selected?.target.device_id === device.id && <><button className="create-window-button" onClick={createWindow} disabled={!canControl || creatingWindow}>
          {creatingWindow ? '正在创建窗口…' : '＋ 新建桌面窗口'}</button>
          {createNotice && <p className="create-window-notice" role="status">{createNotice}</p>}</>}
        {device.windows.map(w => <button className={`window-item ${sameTarget(w.target,target) ? 'selected' : ''}`} key={targetKey(w.target)} onClick={() => selectWindow(w)} disabled={!connected}>
          <span className="window-number">{String(w.target.window_id).padStart(2,'0')}</span><span className="window-copy"><strong>{w.title || '未命名窗口'}</strong><small>{w.cwd || `标签页 ${w.tab_id}`}</small></span><span>↗</span></button>)}
        {device.online && !device.windows.length && <p className="muted small">未发现可接入的 Kitty 窗口，请检查代理。</p>}
        {!device.online && <p className="muted small">等待本机代理重新连接</p>}
        <button className="revoke" onClick={() => revoke(device)}>撤销设备</button></section>)}
      <div className="sidebar-footer"><span className="live-mark">●</span> 同一个终端，同一段工作。<small>手机断开后，电脑上的任务继续运行。</small></div></aside>
    <main className="console"><div className="console-heading"><div><p className="eyebrow">LIVE TERMINAL</p><h1>{selected ? selected.title || '终端窗口' : '你的工作，随时接续。'}</h1><p className="muted path">{selected ? selected.cwd : '选择一个窗口，查看电脑上正在发生的事。'}</p></div>
      </div>
      {error && <div role="alert" className="error banner">{error}<button aria-label="关闭提示" onClick={() => setError('')}>×</button></div>}
      {historyNotice && selected && <div role="status" className="error banner">{historyNotice}</div>}
      {selected ? <><div className="screen-tools"><div className="screen-state"><span className="live-mark">●</span>实时{pinned && <span className="screen-target" title={selected.title}>{selected.title || '终端窗口'}</span>}<span className={`control-badge ${owned ? 'active' : ''}`}>{owned ? '你正在控制' : '只读查看'}</span><small>{selected.cols} × {selected.rows}</small>{historyProgress !== null && <small role="status">同步历史 {historyProgress}%</small>}</div>
        <div className="tool-buttons"><button onClick={() => setFontSize(s => Math.max(10,s-1))} aria-label="减小字号">A−</button><button onClick={() => setFontSize(s => Math.min(24,s+1))} aria-label="增大字号">A＋</button>
          <button disabled={!connected} onClick={() => { receiver.current = new BufferReceiver(); setHistoryProgress(0); send({ type:'buffer_resync' }); }}>重新同步</button>
          <button aria-pressed={pinned} onClick={() => { setPinned(!pinned); setKeyMode(false); setMoreKeys(false); setReceiptOpen(false); }}>{pinned ? '退出固定' : '固定布局'}</button></div></div>
        <div className="screen-frame"><TerminalView key={targetKey(selected.target)} snapshot={snapshot} fontSize={fontSize}/>
          {!snapshot && <div className="screen-wait">{connected ? '正在读取当前窗口…' : '连接已断开，等待重连…'}</div>}</div>
        <div className="screen-foot"><span>{snapshot?.alternate ? '备用屏幕（如 Claude 全屏模式）· 程序自己保存的历史请用更多按键中的翻页键' : `${snapshot?.type === 'buffer' ? '历史已就绪 · ' : ''}上下滑动看历史 · 左右滑动看宽行`}</span>{!owned && <button disabled={!connected || !available} onClick={() => send({ type:'claim' })}>{available ? '获取控制权' : '另一连接正在控制'}</button>}</div>
        <section className="composer"><div className="composer-heading">{compactPinned && keyMode ? <span>发送按键到当前窗口</span> : <label htmlFor="task-input">发送到当前窗口</label>}
          <div className="composer-heading-actions">{(!compactPinned || !keyMode) && <span>{utf8Size(draft).toLocaleString()} / 65,536 B</span>}
            {compactPinned && <button className="composer-mode-toggle" aria-label={keyMode ? '切到输入' : '切到按键'} aria-pressed={keyMode} onPointerDown={e => e.preventDefault()} onClick={() => { setKeyMode(!keyMode); setMoreKeys(false); }}>{keyMode ? '输入 ✎' : '按键 ⌨'}</button>}</div></div>
          {(!compactPinned || !keyMode) && <><textarea id="task-input" aria-label="任务输入" placeholder={canControl ? '输入任务… 支持中文、多行粘贴和输入法语音转文字' : '获取控制权后即可发送任务'} value={draft}
            onChange={e => setDrafts(old => ({ ...old, [draftKey]: e.target.value }))}
            onCompositionStart={() => { composingRef.current=true; setComposing(true); }} onCompositionEnd={() => { composingRef.current=false; setComposing(false); }}/>
          <div className="composer-actions"><span className="muted small">Enter 换行 · 发送后清空输入</span><div><button disabled={!canControl || !draft || composing || utf8Size(draft)>65536} onClick={() => submit('text')}>发送文字</button><button className="primary" disabled={!canControl || !draft || composing || utf8Size(draft)>65536} onClick={() => submit('text_enter')}>发送并回车 ↗</button></div></div></>}
          {(!compactPinned || keyMode) && <><div className="keybar">{commonKeys.map(([label,key]) => <button key={key} aria-label={key === 'backspace' ? 'Backspace' : label} className={key==='ctrl+c' ? 'interrupt' : ''} disabled={!canControl || composing} onPointerDown={e => e.preventDefault()} onClick={() => submit('keys',key)}>{label}</button>)}</div>
          <button className="more-keys-toggle" aria-expanded={moreKeys} onPointerDown={e => e.preventDefault()} onClick={() => setMoreKeys(!moreKeys)}>{moreKeys ? '收起更多按键' : '更多按键 ⌨'}</button></>}
          {moreKeys && <section className="key-drawer" aria-label="更多按键"><div className="drawer-heading"><strong>发送按键到终端</strong><button onPointerDown={e => e.preventDefault()} onClick={() => setMoreKeys(false)}>关闭</button></div>{extraKeys.map(group => <div key={group.title}><h3>{group.title}</h3><div className="extra-key-grid">{group.keys.map(([label,key]) => <button key={key} disabled={!canControl || composing} onPointerDown={e => e.preventDefault()} onClick={() => submit('keys',key)}>{label}</button>)}</div></div>)}</section>}
        </section>
      </> : <section className="welcome"><div className="terminal-glyph">&gt;_</div><h2>会话在电脑上，<br/>操作在你手边。</h2><p>从左侧选择已连接的 Kitty 窗口。<br/>首次使用，请先配对电脑。</p><button className="primary" onClick={() => setPairOpen(true)}>配对电脑 ↗</button><div className="welcome-notes"><span>中文与多行输入</span><span>历史阅读</span><span>精确窗口控制</span></div></section>}
      {receipts.length > 0 && (!pinned || receiptOpen) && <section className="receipts">{pinned && <button onClick={() => setReceiptOpen(false)}>关闭回执</button>}<h2>最近发送 <small>提交成功表示 Kitty 调用已返回，任务进展请看屏幕。</small></h2>{receipts.slice(0,5).map(r => <div className={`receipt ${r.status}`} key={r.request_id}><span>窗口 {r.target.window_id}</span><strong>{statuses[r.status]}</strong><small>{r.detail || r.request_id.slice(0,8)}</small></div>)}</section>}
    </main></div>
    {pairOpen && <div className="modal-backdrop" onClick={() => setPairOpen(false)}><section role="dialog" aria-modal="true" aria-labelledby="pair-title" className="modal" onClick={e=>e.stopPropagation()}><button className="modal-close" aria-label="关闭配对" onClick={() => setPairOpen(false)}>×</button><p className="eyebrow">PAIR A COMPUTER</p><h2 id="pair-title">把电脑接过来。</h2><p className="muted">在电脑上发起配对，将显示的 8 位配对码填在这里。配对码 5 分钟内有效。</p>
      {pairNotice ? <p role="status" className="success">{pairNotice}</p> : pairInfo ? <div className="pair-confirm"><p>确认连接这台电脑？</p><strong>{pairInfo.name}</strong><button className="primary" disabled={busy} onClick={approve}>确认配对</button><button onClick={() => setPairInfo(null)}>返回</button></div> : <form onSubmit={lookup}><label>配对码<input autoFocus aria-label="配对码" className="pair-code" value={pairCode} maxLength={8} autoComplete="off" onChange={e => setPairCode(e.target.value.toUpperCase())} placeholder="A1B2C3D4" required minLength={8}/></label><button className="primary" disabled={busy || pairCode.length!==8}>查找电脑 ↗</button></form>}
      {error && <p role="alert" className="error">{error}</p>}</section></div>}
  </div>;
}
