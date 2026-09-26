import { describe, expect, it } from 'vitest';
import { BufferReceiver, MAX_HISTORY, type BufferStart } from './buffer';
import { commonKeys, extraKeys } from './keys';
import { utf8Size } from './protocol';

const target = { device_id: 'd', agent_epoch: 'e', kitty_instance_id: 'k', window_id: 1 };
const start = (content: string, seq = 1, extra = {}): BufferStart => ({
  v: 1, type: 'buffer_start', target, seq, base_seq: 0, rows: 24, cols: 80,
  alternate: false, sampled_at: 1, start: 0, delete_count: 0,
  insert_count: content.split('\n').length, bytes: utf8Size(content), ...extra,
});
function transfer(r: BufferReceiver, content: string, seq = 1, extra = {}) {
  r.accept(start(content, seq, extra));
  r.accept({ v: 1, type: 'buffer_chunk', target, seq, index: 0, content });
  return r.accept({ v: 1, type: 'buffer_end', target, seq, chunks: 1 });
}
describe('continuous terminal history', () => {
  it('loads more than 2,000 lines and 512 KiB atomically', () => {
    const r = new BufferReceiver();
    const body = ('中文🙂' + 'x'.repeat(90) + '\n').repeat(10000);
    const meta = start(body);
    r.accept(meta); expect(r.current).toBeNull();
    r.accept({ v: 1, type: 'buffer_chunk', target, seq: 1, index: 0, content: body.slice(0, 100) });
    expect(r.current).toBeNull();
    r.accept({ v: 1, type: 'buffer_chunk', target, seq: 1, index: 1, content: body.slice(100) });
    expect(r.accept({ v: 1, type: 'buffer_end', target, seq: 1, chunks: 2 })?.content).toBe(body);
  });
  it('replaces repeated lines without guessing appended output', () => {
    const r = new BufferReceiver(); transfer(r, 'a\nx\nx\nz');
    expect(transfer(r, '中文🙂', 2, { base_seq: 1, start: 1, delete_count: 2 })?.content).toBe('a\n中文🙂\nz');
  });
  it('rejects gaps, invalid revisions and oversized history', () => {
    const r = new BufferReceiver();
    expect(() => r.accept(start('a', 2, { base_seq: 1 }))).toThrow();
    expect(() => r.accept(start('a', 1, { bytes: MAX_HISTORY + 1 }))).toThrow();
    r.accept(start('a'));
    expect(() => r.accept({ v: 1, type: 'buffer_end', target, seq: 1, chunks: 0 })).toThrow();
    r.resetTransfer(); expect(r.pending).toBeNull();
  });
  it('can replace the whole buffer after reconnect or screen switch', () => {
    const r = new BufferReceiver(); transfer(r, 'main history');
    expect(transfer(r, 'alternate', 4, { alternate: true })?.content).toBe('alternate');
  });
  it('exposes all planned keys without duplicates', () => {
    const keys = [...commonKeys.map(k => k[1]), ...extraKeys.flatMap(g => g.keys.map(k => k[1]))];
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain('shift+tab'); expect(keys).toContain('page_up'); expect(keys).toContain('f12');
  });
});
