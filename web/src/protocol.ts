export interface Target { device_id: string; agent_epoch: string; kitty_instance_id: string; window_id: number }
export interface WindowInfo { target: Target; title: string; cwd: string; os_window_id: number; tab_id: number; rows: number; cols: number; alternate: boolean }
export interface Device { id: string; name: string; online: boolean; windows: WindowInfo[] }
export interface Snapshot { v: 1; type: 'snapshot' | 'history' | 'buffer'; target: Target; seq: number; sampled_at: number; rows: number; cols: number; alternate: boolean; content: string }
export type InputStatus = 'pending' | 'received' | 'submitted' | 'rejected' | 'failed' | 'uncertain';
export interface Receipt { request_id: string; target: Target; status: InputStatus; detail: string }
export interface CreateWindowResult { v: 1; type: 'create_window_result'; request_id: string;
  target: Target; status: 'received' | 'created' | 'rejected' | 'uncertain'; detail: string;
  window?: WindowInfo }
export type Message =
  | import('./buffer').BufferPart
  | { v: 1; type: 'state'; devices: Device[]; server_time: number }
  | Snapshot
  | ({ v: 1; type: 'receipt' } & Receipt)
  | CreateWindowResult
  | { v: 1; type: 'lease'; target: Target; owned: boolean; available: boolean; expires_at: number }
  | { v: 1; type: 'target_error' | 'history_error'; target: Target; detail: string }
  | { v: 1; type: 'error'; detail: string }
  | { v: 1; type: 'ping' | 'pong' };
export const targetKey = (t: Target) => `${t.device_id}/${t.agent_epoch}/${t.kitty_instance_id}/${t.window_id}`;
export const sameTarget = (a: Target | null | undefined, b: Target | null | undefined) => !!a && !!b && targetKey(a) === targetKey(b);
export const statuses: Record<InputStatus, string> = { pending: '待发送', received: '已接收', submitted: '已提交至 Kitty', rejected: '已拒绝', failed: '失败', uncertain: '结果不确定' };
export const utf8Size = (text: string) => new TextEncoder().encode(text).length;
export function disconnected(receipts: Receipt[]): Receipt[] {
  return receipts.map(r => ['pending', 'received'].includes(r.status) ? { ...r, status: 'uncertain', detail: '连接中断，请查看屏幕后判断；不会自动重发' } : r);
}
export function sanitizeAnsi(text: string): string {
  let out = '', i = 0;
  const c1: Record<string, string> = { '\x90':'P', '\x98':'X', '\x9b':'[', '\x9d':']', '\x9e':'^', '\x9f':'_' };
  while (i < text.length) {
    const ch = text[i];
    if (ch === '\x1b' || ch in c1) {
      let kind: string;
      if (ch === '\x1b') { i++; if (i >= text.length) break; kind = text[i]; }
      else kind = c1[ch];
      i++;
      if (']PX^_'.includes(kind)) {
        while (i < text.length) {
          if (text[i] === '\x07' || text[i] === '\x9c') { i++; break; }
          if (text.startsWith('\x1b\\', i)) { i += 2; break; }
          i++;
        }
      } else if (kind === '[') {
        const start = i;
        while (i < text.length && !(text[i] >= '@' && text[i] <= '~')) i++;
        if (i < text.length) {
          const value = text.slice(start, ++i);
          if (/^(?:[0-9:;]*m|[0-9;]*[Hf]|[0-6] q|\?25[hl])$/.test(value)) out += '\x1b[' + value;
        }
      }
      continue;
    }
    const n = ch.charCodeAt(0);
    if ('\n\r\t'.includes(ch) || (n >= 32 && !(n >= 127 && n <= 159))) out += ch;
    i++;
  }
  return out;
}

// Anchor relay epoch time to a monotonic browser clock, independent of device wall time.
export class RelayClock {
  private anchor: { server: number; local: number } | null = null;
  sync(server: number, local = performance.now()) {
    this.anchor = Number.isFinite(server) && server > 0 ? { server, local } : null;
  }
  deadline(local = performance.now()): number | null {
    return this.anchor ? this.anchor.server + (local - this.anchor.local) / 1000 + 10 : null;
  }
}
