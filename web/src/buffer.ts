import { sameTarget, utf8Size, type Snapshot, type Target } from './protocol';

export const MAX_HISTORY = 16 * 1024 * 1024;
export interface BufferStart {
  v: 1; type: 'buffer_start'; target: Target; seq: number; base_seq: number;
  rows: number; cols: number; alternate: boolean; sampled_at: number;
  start: number; delete_count: number; insert_count: number; bytes: number;
}
export type BufferPart = BufferStart
  | { v: 1; type: 'buffer_chunk'; target: Target; seq: number; index: number; content: string }
  | { v: 1; type: 'buffer_end'; target: Target; seq: number; chunks: number };

export class BufferReceiver {
  current: Snapshot | null = null;
  pending: BufferStart | null = null;
  private parts: string[] = [];
  private size = 0;
  started = 0;
  get progress() { return this.pending ? Math.round(100 * this.size / Math.max(1, this.pending.bytes)) : null; }
  resetTransfer() { this.pending = null; this.parts = []; this.size = 0; this.started = 0; }
  accept(part: BufferPart): Snapshot | null {
    if (part.type === 'buffer_start') {
      const ints = [part.seq, part.base_seq, part.start, part.delete_count, part.insert_count, part.bytes, part.rows, part.cols];
      if (ints.some(n => !Number.isSafeInteger(n) || n < 0) || part.seq < 1 || part.rows < 1 || part.rows > 500 || part.cols < 1 || part.cols > 1000
          || part.bytes > MAX_HISTORY || part.insert_count > MAX_HISTORY + 1
          || (part.base_seq && (!this.current || this.current.seq !== part.base_seq || !sameTarget(this.current.target, part.target)))
          || (this.current && part.seq <= this.current.seq)) throw new Error('历史版本不匹配');
      const old = part.base_seq ? this.current!.content.split('\n') : [];
      if (part.start + part.delete_count > old.length) throw new Error('历史更新范围无效');
      this.resetTransfer(); this.pending = part; this.started = performance.now(); return null;
    }
    const meta = this.pending;
    if (!meta || part.seq !== meta.seq || !sameTarget(part.target, meta.target)) throw new Error('历史分块已失效');
    if (part.type === 'buffer_chunk') {
      if (part.index !== this.parts.length || typeof part.content !== 'string') throw new Error('历史分块顺序无效');
      this.size += utf8Size(part.content);
      if (this.size > meta.bytes || this.size > MAX_HISTORY || this.parts.length >= 1024) throw new Error('历史超过 16 MiB');
      this.parts.push(part.content); return null;
    }
    if (part.chunks !== this.parts.length || this.size !== meta.bytes) throw new Error('历史传输不完整');
    const inserted = meta.insert_count ? this.parts.join('').split('\n') : [];
    if (inserted.length !== meta.insert_count) throw new Error('历史行数不匹配');
    const old = meta.base_seq ? this.current!.content.split('\n') : [];
    const content = old.slice(0, meta.start).concat(inserted, old.slice(meta.start + meta.delete_count)).join('\n');
    if (utf8Size(content) > MAX_HISTORY) throw new Error('历史超过 16 MiB');
    this.current = { v: 1, type: 'buffer', target: meta.target, seq: meta.seq, sampled_at: meta.sampled_at,
      rows: meta.rows, cols: meta.cols, alternate: meta.alternate, content };
    this.resetTransfer(); return this.current;
  }
}
