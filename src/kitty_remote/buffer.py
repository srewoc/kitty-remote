"""Bounded, versioned terminal buffers. Transport chunks never depend on scrolling."""

from collections.abc import Iterator
from typing import Literal

from pydantic import Field

from .protocol import MAX_HISTORY, Model, Snapshot, Target, packet


class BufferStart(Model):
    v: Literal[1] = 1
    type: Literal["buffer_start"] = "buffer_start"
    target: Target
    seq: int = Field(gt=0)
    base_seq: int = Field(ge=0)
    sampled_at: float
    rows: int = Field(ge=1, le=500)
    cols: int = Field(ge=1, le=1000)
    alternate: bool
    start: int = Field(ge=0)
    delete_count: int = Field(ge=0)
    insert_count: int = Field(ge=0, le=MAX_HISTORY + 1)
    bytes: int = Field(ge=0, le=MAX_HISTORY)


def frames(current: dict, previous: dict | None) -> Iterator[dict]:
    """One contiguous line splice; repeated lines cannot create guessed appends."""
    new = current["content"].split("\n")
    old = previous["content"].split("\n") if previous else []
    start = end = 0
    if previous and (current["cols"], current["alternate"]) == (
        previous["cols"],
        previous["alternate"],
    ):
        while start < min(len(old), len(new)) and old[start] == new[start]:
            start += 1
        while end < min(len(old), len(new)) - start and old[-end - 1] == new[-end - 1]:
            end += 1
    inserted = new[start : len(new) - end]
    content = "\n".join(inserted)
    common = {"target": current["target"], "seq": current["seq"]}
    yield packet(
        "buffer_start",
        **common,
        base_seq=previous["seq"] if previous else 0,
        sampled_at=current["sampled_at"],
        rows=current["rows"],
        cols=current["cols"],
        alternate=current["alternate"],
        start=start,
        delete_count=len(old) - start - end,
        insert_count=len(inserted),
        bytes=len(content.encode()),
    )
    count = 0
    # Even JSON's worst-case escaping fits comfortably within a 1 MiB frame.
    for offset in range(0, len(content), 96 * 1024):
        yield packet(
            "buffer_chunk", **common, index=count, content=content[offset : offset + 96 * 1024]
        )
        count += 1
    yield packet("buffer_end", **common, chunks=count)


class BufferAssembler:
    def __init__(self):
        self.current: dict | None = None
        self.pending: BufferStart | None = None
        self.parts: list[str] = []
        self.size = 0

    def push(self, message: dict) -> dict | None:
        if message["type"] == "buffer_start":
            meta = BufferStart.model_validate(message)
            if meta.base_seq and (
                not self.current
                or meta.base_seq != self.current["seq"]
                or meta.target.model_dump() != self.current["target"]
            ):
                raise ValueError("历史基础版本不匹配")
            if self.current and meta.seq <= self.current["seq"]:
                raise ValueError("历史版本倒退")
            old = self.current["content"].split("\n") if meta.base_seq else []
            if meta.start + meta.delete_count > len(old):
                raise ValueError("历史更新范围无效")
            self.pending, self.parts, self.size = meta, [], 0
            return None
        meta = self.pending
        if (
            not meta
            or message.get("seq") != meta.seq
            or message.get("target") != meta.target.model_dump()
        ):
            raise ValueError("历史分块不属于当前传输")
        if message["type"] == "buffer_chunk":
            content = message.get("content")
            if message.get("index") != len(self.parts) or not isinstance(content, str):
                raise ValueError("历史分块顺序无效")
            self.size += len(content.encode())
            if self.size > meta.bytes or self.size > MAX_HISTORY or len(self.parts) >= 1024:
                raise ValueError("历史超过 16 MiB 限制")
            self.parts.append(content)
            return None
        if (
            message["type"] != "buffer_end"
            or message.get("chunks") != len(self.parts)
            or self.size != meta.bytes
        ):
            raise ValueError("历史传输不完整")
        inserted = "".join(self.parts).split("\n") if meta.insert_count else []
        if len(inserted) != meta.insert_count:
            raise ValueError("历史行数不匹配")
        old = self.current["content"].split("\n") if meta.base_seq else []
        content = "\n".join(old[: meta.start] + inserted + old[meta.start + meta.delete_count :])
        result = Snapshot(
            type="buffer",
            target=meta.target,
            seq=meta.seq,
            sampled_at=meta.sampled_at,
            rows=meta.rows,
            cols=meta.cols,
            alternate=meta.alternate,
            content=content,
        ).model_dump()
        self.current, self.pending, self.parts = result, None, []
        return result
