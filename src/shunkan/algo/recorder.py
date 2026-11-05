"""Full-depth tick recorder.

Nobody sells retail-grade Indian options depth history at a quality you can
execute research against, so you have to capture your own. Start this before
the open and leave it running; every strategy you ever test is limited by what
this collected, and you cannot go back and re-record yesterday.

Design constraints that matter:

* The websocket thread must never block on disk. A flush that stalls the
  socket drops ticks silently, and a gap in the tape is invisible later but
  will quietly bias every backtest that spans it. Ticks are buffered under a
  lock and written by a separate thread.
* Every tick is stamped with local receive time in nanoseconds *before* it is
  queued, so queueing delay never contaminates the timestamp used for replay.
* Dropped ticks are counted and written into session metadata rather than
  swallowed. A recording that lost 3% of its ticks should announce that.
* Flush on shutdown, including on SIGINT, or you lose the tail of the session
  -- which for a closing-auction study is precisely the part you wanted.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from shunkan.algo.schema import ARROW_SCHEMA, COLUMN_NAMES, SCHEMA_VERSION, tick_to_row
from shunkan.config import APP_DIR
from shunkan.stream.parser import Tick

TICKS_DIR = APP_DIR / "ticks"

# Above this the buffer is considered pathological: the writer is not keeping
# up and memory is growing without bound. We drop and count rather than OOM
# the process mid-session, because a truncated recording beats no recording.
MAX_BUFFER = 2_000_000


@dataclass
class RecorderStats:
    ticks_received: int = 0
    ticks_written: int = 0
    ticks_dropped: int = 0
    flushes: int = 0
    bytes_written: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def drop_rate(self) -> float:
        return self.ticks_dropped / self.ticks_received if self.ticks_received else 0.0

    def as_dict(self) -> dict:
        return {
            "ticks_received": self.ticks_received,
            "ticks_written": self.ticks_written,
            "ticks_dropped": self.ticks_dropped,
            "drop_rate": round(self.drop_rate, 6),
            "flushes": self.flushes,
            "bytes_written": self.bytes_written,
            "started_at": self.started_at,
        }


class TickRecorder:
    """Buffer depth ticks off the socket thread and write Parquet parts.

        rec = TickRecorder(tokens=[12030210])
        rec.start()
        ticker.start(tokens, rec.on_ticks, mode="full")
        ...
        rec.stop()          # flushes the tail and writes session.json

    `on_ticks` is the KiteTicker callback signature, so this drops in wherever
    the UI's tick handler goes.
    """

    def __init__(
        self,
        tokens: list[int] | None = None,
        root: Path | None = None,
        session: str | None = None,
        flush_rows: int = 50_000,
        flush_seconds: float = 15.0,
        compression: str = "zstd",
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.tokens = list(tokens or [])
        self.root = Path(root) if root else TICKS_DIR
        self.session = session or datetime.now().strftime("%Y-%m-%d")
        self.flush_rows = flush_rows
        self.flush_seconds = flush_seconds
        self.compression = compression
        # Injectable so tests can produce a tape with realistic inter-tick
        # spacing. Generating a synthetic session in a tight loop stamps
        # thousands of ticks into the same millisecond, which then never age
        # past the execution latency gate and silently never fill — a fixture
        # bug that reads exactly like a broken engine.
        self._clock: Callable[[], int] = clock or time.time_ns

        self.stats = RecorderStats()
        self._buf: deque[dict] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._part = 0

    # ---- lifecycle -----------------------------------------------------

    @property
    def session_dir(self) -> Path:
        """Directory holding ONLY parquet parts, so it reads as a dataset."""
        return self.root / self.session

    @property
    def meta_path(self) -> Path:
        return self.root / f"{self.session}.json"

    def refuse_existing(self) -> None:
        """Raise FileExistsError if this session already has parts or metadata.

        Part numbering restarts at 1, so recording into a used name overwrites the old tape.
        """
        if self.meta_path.exists() or any(self.session_dir.glob("*.parquet")):
            raise FileExistsError(
                f"session {self.session!r} already exists under {self.root}; use a new name"
            )

    def start(self) -> None:
        self.refuse_existing()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._writer_loop, name="tick-recorder", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> RecorderStats:
        """Stop accepting ticks, drain the buffer, write metadata."""
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._flush()  # anything the writer thread did not get to
        self._write_meta()
        return self.stats

    def __enter__(self) -> "TickRecorder":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- tick path (hot; runs on the socket thread) ---------------------

    def on_ticks(self, ticks: list[Tick], ts_ns: int | None = None) -> None:
        """KiteTicker callback. Must stay cheap — no disk, no formatting.

        `ts_ns` lets a caller that also acts on the batch share one stamp.
        """
        if not ticks:
            return
        ts = self._clock() if ts_ns is None else ts_ns  # stamp before queueing
        rows = [tick_to_row(t, ts) for t in ticks]
        with self._wake:
            self.stats.ticks_received += len(rows)
            if len(self._buf) + len(rows) > MAX_BUFFER:
                self.stats.ticks_dropped += len(rows)
                return
            self._buf.extend(rows)
            if len(self._buf) >= self.flush_rows:
                self._wake.notify()

    # ---- writer thread --------------------------------------------------

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                self._wake.wait(timeout=self.flush_seconds)
            try:
                self._flush()
            except Exception:
                # A failed write must not kill the recorder: the socket keeps
                # delivering and the next flush may well succeed. The loss is
                # visible afterwards as written < received.
                continue

    def _drain(self) -> list[dict]:
        with self._lock:
            if not self._buf:
                return []
            rows = list(self._buf)
            self._buf.clear()
        return rows

    def _flush(self) -> None:
        rows = self._drain()
        if not rows:
            return
        table = pa.Table.from_pydict(
            {name: [r[name] for r in rows] for name in COLUMN_NAMES},
            schema=ARROW_SCHEMA,
        )
        self._part += 1
        path = self.session_dir / f"part-{self._part:05d}.parquet"
        pq.write_table(table, path, compression=self.compression)
        self.stats.ticks_written += len(rows)
        self.stats.flushes += 1
        try:
            self.stats.bytes_written += path.stat().st_size
        except OSError:
            pass

    def _write_meta(self) -> None:
        meta = {
            "schema_version": SCHEMA_VERSION,
            "session": self.session,
            "tokens": self.tokens,
            "parts": self._part,
            "finished_at": time.time(),
            **self.stats.as_dict(),
        }
        # Deliberately NOT inside session_dir: pyarrow's dataset reader tries
        # to parse every file in a directory as Parquet, so a stray JSON there
        # makes the obvious `pq.read_table(session_dir)` fail with a confusing
        # "magic bytes not found". Keeping the dataset directory pure Parquet
        # means the obvious thing works.
        try:
            self.meta_path.write_text(json.dumps(meta, indent=2))
        except OSError:
            pass


def record_live(
    tokens: list[int],
    api_key: str,
    access_token: str,
    duration: float | None = None,
    root: Path | None = None,
) -> RecorderStats:
    """Convenience: wire a KiteTicker straight into a recorder and block.

    `duration` in seconds, or None to run until interrupted. Subscribes in
    FULL mode -- quote mode carries no depth, which would make the recording
    useless for execution modelling even though it looks fine on inspection.
    """
    from shunkan.stream.ticker import KiteTicker

    rec = TickRecorder(tokens=tokens, root=root)
    ticker = KiteTicker(api_key, access_token)
    rec.start()
    ticker.start(tokens, rec.on_ticks, mode="full")
    try:
        if duration is None:
            while True:
                time.sleep(1.0)
        else:
            time.sleep(duration)
    except KeyboardInterrupt:
        pass
    finally:
        ticker.stop()
        stats = rec.stop()
    return stats
