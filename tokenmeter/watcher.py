"""로그 파일 감시 + 토큰 델타 추출 (Infrastructure).

services.yaml 의 ServiceSpec 하나당 ServiceReader 하나. 파싱 규칙(경로/매치/필드/
누적 여부)을 전부 스펙에서 읽으므로, 새 서비스는 YAML 편집만으로 붙는다.

핵심 규약
- 기동 시 과거 로그를 먹지 않는다 (`prime()`).
- jsonl 은 byte offset 증분 읽기, 개행으로 끝나지 않은 마지막 줄은 소비하지 않는다.
- cumulative 는 직전 누적값과의 차이만 emit, 값이 줄면 리셋으로 보고 baseline 만 갱신.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .config import TOKEN_FIELDS, Config, ServiceSpec, dig, resolve_plan
from .endpoints import resolve as resolve_endpoint
from .meter import Meter, TokenDelta, live_path
from .pricing import context_window, vendor_of

try:  # watchdog 은 있으면 쓰고, 없으면 폴링만으로 동작한다
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except Exception:  # pragma: no cover - 환경 의존
    Observer = None
    FileSystemEventHandler = object  # type: ignore[assignment,misc]

PRIME_WINDOW_SEC = 2 * 24 * 3600  # prime 시 '내용까지' 파싱할 최근 파일 범위
GC_INTERVAL_SEC = 300.0  # 메모리 정리 주기
GC_IDLE_SEC = 6 * 3600.0  # 이 시간 동안 조용한 파일의 파일별 seen 은 버린다
SEEN_CAP = 200_000  # 전역 seen 상한. 넘으면 오래된 절반을 버린다 (삽입 순서 = FIFO)
DEBOUNCE_SEC = 0.12  # watchdog 이벤트 디바운스
TICK_SEC = 0.2  # 워처 루프 틱

Vector = Tuple[int, int, int, int]  # (input, cache_read, cache_write, output)


def _debug(msg: str) -> None:
    if os.environ.get("TOKENMETER_DEBUG") not in (None, "", "0"):
        print(f"[tokenmeter] {msg}", file=sys.stderr)


def _num(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


class ServiceReader:
    """ServiceSpec 하나에 대한 파일 파싱 + 진행 상태."""

    def __init__(self, spec: ServiceSpec, on_delta: Callable[[TokenDelta], None]) -> None:
        self.spec = spec
        self.on_delta = on_delta
        # 요금제는 로그가 아니라 클라이언트 인증 설정에서 나온다 → 기동 때 한 번만 본다
        self.plan = resolve_plan(spec)
        self._endpoint: Dict[str, str] = {}  # (세션|벤더) → 엔드포인트
        self._offset: Dict[str, int] = {}  # 파일 → 다음에 읽을 byte 위치 (jsonl)
        self._mtime: Dict[str, float] = {}  # 파일 → 마지막으로 읽은 mtime
        self._lines: Dict[str, int] = {}  # 파일 → 누적 줄 번호 (key 없을 때의 대체 키)
        self._ctx: Dict[str, Dict[str, str]] = {}  # 파일 → {cwd, model}
        self._seen: Dict[str, Set[str]] = {}  # 파일 → 이미 먹은 delta 키 (spec.key 가 없을 때)
        # spec.key 가 있으면 그 키는 전역 고유다(claude-code 의 uuid). 파일별로 나눠 두면
        # 세션 fork/resume 이 기존 레코드를 새 .jsonl 로 복사할 때 통째로 재계상된다.
        self._seen_keys: Dict[str, None] = {}
        # ponytail: 누적 baseline 은 '파일 단위'로 보관한다. 같은 key 가 여러 파일에
        # 걸쳐 나타나는 서비스가 생기면 전역 dict 로 올려야 한다.
        self._base: Dict[str, Dict[str, Vector]] = {}
        self._blind: Set[str] = set()  # 내용을 안 보고 건너뛴 파일 (첫 관측은 baseline 만)
        self._touch: Dict[str, float] = {}  # 파일 → 마지막 활동 시각
        self._last_gc = time.time()

    # ── 파일 목록 ────────────────────────────────────────────────────────
    def files(self) -> Iterator[Path]:
        """roots × patterns 로 감시 대상 파일을 훑는다."""
        for root in self.spec.existing_roots():
            for pattern in self.spec.patterns:
                try:
                    for path in root.glob(pattern):
                        if path.is_file():
                            yield path
                except OSError as exc:
                    _debug(f"{self.spec.name}: glob 실패 {root}/{pattern} {exc!r}")

    def wanted(self, path: Path) -> bool:
        """watchdog 이 알려준 경로가 이 서비스 패턴에 맞는지 (파일명만 느슨하게 확인)."""
        return any(
            fnmatch.fnmatch(path.name, pattern.rsplit("/", 1)[-1])
            for pattern in self.spec.patterns
        )

    # ── 기동 ────────────────────────────────────────────────────────────
    def prime(self) -> None:
        """기존 로그를 '이미 먹은 것'으로 표시한다 (emit 절대 금지).

        최근 2일 이내 파일만 내용을 파싱해 baseline/seen/컨텍스트를 채우고,
        그보다 오래된 파일은 오프셋만 EOF 로 밀어 비용을 아낀다.
        """
        cutoff = time.time() - PRIME_WINDOW_SEC
        for path in self.files():
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                self.read_file(path, emit=False)
            else:
                self._offset[key] = stat.st_size
                self._mtime[key] = stat.st_mtime
                self._touch[key] = time.time()
                self._blind.add(key)

    # ── 폴링 ────────────────────────────────────────────────────────────
    def poll(self) -> int:
        """변경된 파일만 읽어 delta 를 emit 한다. emit 개수 반환."""
        emitted = 0
        for path in self.files():
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            if self._mtime.get(key) == stat.st_mtime:
                if self.spec.format == "json" or self._offset.get(key, 0) >= stat.st_size:
                    continue
            emitted += len(self.read_file(path, emit=True))
        self._gc()
        return emitted

    def _gc(self) -> None:
        """오래 조용한 파일의 상태를 버려 메모리 누수를 막는다.

        살아 있는 파일은 파일별 seen 만 비운다. baseline(_base)/컨텍스트(_ctx)까지
        버리면 재개된 세션의 한 턴이 통째로 유실되고 이후 귀속이 깨진다.
        """
        now = time.time()
        if now - self._last_gc < GC_INTERVAL_SEC:
            return
        self._last_gc = now
        for key, touched in list(self._touch.items()):
            if now - touched < GC_IDLE_SEC:
                continue
            self._seen.pop(key, None)  # 줄 번호 키는 계속 증가하므로 비워도 충돌하지 않는다
            if Path(key).exists():
                # 오프셋은 남겨 둔다. 버리면 다음 폴에서 파일을 통째로 다시 먹는다.
                self._touch[key] = now
                continue
            self._ctx.pop(key, None)
            self._lines.pop(key, None)
            self._base.pop(key, None)
            self._touch.pop(key, None)
            self._offset.pop(key, None)
            self._mtime.pop(key, None)
            self._blind.discard(key)

    # ── 엔드포인트 ───────────────────────────────────────────────────────
    def endpoint_for(self, session: str, vendor: str) -> str:
        """이 세션이 통신하는 URL.

        훅이 세션 시작 때 찍어 둔 라우팅 환경을 먼저 쓴다 — 데몬은 여러 세션에
        걸쳐 살아 있으므로 데몬 자신의 환경으로는 세션별 구분이 안 된다.
        """
        cache_key = f"{session}|{vendor}"
        if cache_key in self._endpoint:
            return self._endpoint[cache_key]
        env: Optional[Dict[str, str]] = None
        if session:
            try:
                rec = json.loads(
                    live_path(self.spec.name, session).read_text(encoding="utf-8")
                )
                found = rec.get("routing_env")
                env = {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}
            except (OSError, ValueError, AttributeError):
                env = None  # 라이브 파일이 없으면 데몬 환경으로 폴백
        value = resolve_endpoint(self.spec, env, vendor, self.plan)
        if len(self._endpoint) > 1000:  # 세션이 계속 늘어도 메모리는 묶어둔다
            self._endpoint.clear()
        self._endpoint[cache_key] = value
        return value

    # ── 파싱 ────────────────────────────────────────────────────────────
    def read_file(self, path: Path, emit: bool = True) -> List[TokenDelta]:
        """파일 하나를 (증분) 파싱한다. emit=False 면 on_delta 를 부르지 않는다."""
        out: List[TokenDelta] = []
        try:
            if self.spec.format == "json":
                self._read_json(path, out, emit)
            else:
                self._read_jsonl(path, out, emit)
        except Exception as exc:  # 감시가 죽으면 안 된다
            _debug(f"{self.spec.name}: {path} 읽기 실패 {exc!r}")
        return out

    def _read_json(self, path: Path, out: List[TokenDelta], emit: bool) -> None:
        """파일 전체 = 레코드 1개."""
        key = str(path)
        stat = path.stat()
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not raw:
            return
        obj = json.loads(raw)  # 쓰는 중이면 여기서 터지고, mtime 을 안 남겨 다음에 재시도
        self._touch[key] = time.time()
        self._handle(obj, path, 0, out, emit)
        self._mtime[key] = stat.st_mtime

    def _read_jsonl(self, path: Path, out: List[TokenDelta], emit: bool) -> None:
        """한 줄 = 레코드 1개. byte offset 증분 읽기."""
        key = str(path)
        stat = path.stat()
        offset = self._offset.get(key, 0)
        if stat.st_size < offset:  # 로테이트/절삭
            offset = 0
            self._lines.pop(key, None)
            if not self.spec.key:
                # 줄 번호가 키였으니 seen 을 비운다. key 가 있으면 그대로 둬야 중복을 막는다
                self._seen.pop(key, None)
        if stat.st_size == offset:
            self._mtime[key] = stat.st_mtime
            return

        with path.open("rb") as fh:
            fh.seek(offset)
            buf = fh.read(stat.st_size - offset)

        end = buf.rfind(b"\n")
        if end < 0:
            # 완결된 줄이 하나도 없다 → 오프셋 그대로 두고 다음 폴에서 다시
            return
        self._touch[key] = time.time()
        pos = offset
        line_no = self._lines.get(key, 0)
        for chunk in buf[: end + 1].split(b"\n")[:-1]:
            pos += len(chunk) + 1
            line_no += 1
            text = chunk.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                # 개행으로 끝난 줄인데도 깨졌다면 진짜 손상 → 조용히 건너뛴다.
                # (쓰다 만 마지막 줄은 애초에 위에서 잘라내 소비하지 않는다)
                _debug(f"{self.spec.name}: {path}:{line_no} JSON 파싱 실패, 건너뜀")
                continue
            self._handle(obj, path, line_no, out, emit)
        self._offset[key] = pos
        self._lines[key] = line_no
        self._mtime[key] = stat.st_mtime

    def _matches(self, obj: Any) -> bool:
        for dot_path, want in self.spec.match.items():
            if str(dig(obj, dot_path)) != str(want):
                return False
        return True

    def _handle(
        self, obj: Any, path: Path, line_no: int, out: List[TokenDelta], emit: bool
    ) -> None:
        key = str(path)
        ctx = self._ctx.setdefault(key, {})

        # 컨텍스트는 match 여부와 무관하게 모든 줄에서 학습한다
        # (codex 의 session_meta/turn_context 처럼 별도 줄에 cwd/model 이 있다)
        for name, dot_path in self.spec.context.items():
            value = dig(obj, dot_path)
            if value not in (None, ""):
                ctx[name] = str(value)

        if not self._matches(obj):
            return

        vector: Vector = tuple(  # type: ignore[assignment]
            _num(dig(obj, self.spec.fields.get(field))) for field in TOKEN_FIELDS
        )

        if self.spec.mode == "cumulative":
            record_key = str(dig(obj, self.spec.key)) if self.spec.key else key
            bases = self._base.setdefault(key, {})
            previous = bases.get(record_key)
            bases[record_key] = vector
            if previous is None:
                if key in self._blind:
                    # prime/GC 로 내용을 못 본 파일 → 첫 관측은 baseline 으로만 쓴다
                    self._blind.discard(key)
                    return
                diff: Vector = vector
            else:
                diff = tuple(n - o for n, o in zip(vector, previous))  # type: ignore[assignment]
                if any(v < 0 for v in diff):
                    return  # 누적값이 줄었다 = 리셋. baseline 만 갱신하고 emit 은 안 한다
        else:
            raw_key = dig(obj, self.spec.key) if self.spec.key else None
            if raw_key not in (None, ""):
                # 전역 키 → 파일이 달라도 같은 레코드는 한 번만 먹는다
                if str(raw_key) in self._seen_keys:
                    return
                self._seen_keys[str(raw_key)] = None
                if len(self._seen_keys) > SEEN_CAP:
                    for old in list(self._seen_keys)[: SEEN_CAP // 2]:
                        del self._seen_keys[old]
            else:
                seen = self._seen.setdefault(key, set())
                if f"{key}:{line_no}" in seen:
                    return
                seen.add(f"{key}:{line_no}")
            diff = vector

        input_tokens, cache_read, cache_write, output_tokens = diff
        if self.spec.input_includes_cache:
            input_tokens = max(0, input_tokens - cache_read)

        def learned(name: str, fallback: str = "") -> str:
            """이 레코드에 있으면 그 값, 없으면 같은 파일에서 학습해 둔 값."""
            value = dig(obj, self.spec.context.get(name))
            return str(value) if value not in (None, "") else (ctx.get(name) or fallback)

        model = learned("model", self.spec.default_model)
        cwd = learned("cwd")
        # 벤더는 로그에 있으면 그걸 쓰고(codex model_provider / opencode providerID),
        # 없으면 서비스 기본값, 그것도 없으면 모델명에서 추론한다.
        vendor = learned("vendor") or self.spec.vendor or vendor_of(model)
        session = learned("session")

        # 컨텍스트 점유는 '증분' 이 아니라 이 레코드가 말하는 현재값이다 (diff 가 아닌 vector).
        # 압축이 일어나면 다음 레코드에서 저절로 내려간다.
        # 서브에이전트는 부모와 같은 sessionId 로 찍힌다 — 그 컨텍스트를 세션 ctx% 로
        # 삼으면 남의 창 크기를 내 것으로 읽는다. 토큰/비용은 내가 낸 것이므로 그대로 둔다.
        subagent = bool(self.spec.subagent and dig(obj, self.spec.subagent))
        if subagent:
            ctx_now = ctx_win = 0
        else:
            ctx_now = (_num(dig(obj, self.spec.ctx_tokens)) if self.spec.ctx_tokens
                       else vector[0] + vector[1] + vector[2])
            ctx_win = (_num(dig(obj, self.spec.ctx_window)) if self.spec.ctx_window
                       else context_window(model, ctx_now))

        delta = TokenDelta(
            input_tokens=input_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            output_tokens=output_tokens,
            model=model,
            service=self.spec.name,
            project=Path(cwd).name if cwd else "",
            session=session,
            vendor=vendor,
            plan=self.plan,
            endpoint=self.endpoint_for(session, vendor),
            effort=learned("effort"),
            ctx_tokens=ctx_now,
            ctx_window=ctx_win,
            subagent=subagent,
        )
        if delta.total <= 0:
            return
        out.append(delta)
        if emit:
            self.on_delta(delta)


class _EventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """watchdog 이벤트를 (reader, path) 로 바꿔 큐에 넣는다."""

    def __init__(self, reader: ServiceReader, sink: Callable[[ServiceReader, Path], None]):
        super().__init__()
        self.reader = reader
        self.sink = sink

    def on_any_event(self, event: Any) -> None:  # pragma: no cover - 환경 의존
        if getattr(event, "is_directory", False):
            return
        raw = getattr(event, "dest_path", None) or getattr(event, "src_path", None)
        if not raw:
            return
        path = Path(raw)
        if self.reader.wanted(path):
            self.sink(self.reader, path)


class MultiWatcher:
    """활성 서비스 전부를 감시한다. watchdog + 주기 폴백 폴링."""

    def __init__(self, meter: Meter, config: Config) -> None:
        self.meter = meter
        self.config = config
        self.readers: Dict[str, ServiceReader] = {}
        self.poll_seconds = float(config.setting("poll_seconds", 2.0))
        self._observer: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending: Dict[str, Tuple[ServiceReader, float]] = {}
        self._pending_lock = threading.Lock()
        self._read_lock = threading.Lock()  # 폴링 스레드 vs 외부 poll_once() 경합 방지

    # ── 수명주기 ────────────────────────────────────────────────────────
    def start(self, services: Optional[Sequence[str]] = None) -> None:
        for spec in self.config.enabled_services():
            if services is not None and spec.name not in services:
                continue
            if not spec.existing_roots():
                _debug(f"{spec.name}: 로그 경로가 없어 건너뜀")
                continue
            reader = ServiceReader(spec, self._on_delta)
            reader.prime()  # 과거 로그를 먹지 않기 위한 필수 단계
            self.readers[spec.name] = reader
        self._start_observer()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tokenmeter-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception as exc:
                _debug(f"observer 종료 실패 {exc!r}")
            self._observer = None

    def poll_once(self) -> int:
        """전 서비스 1회 폴링. 데몬 루프에서 직접 불러도 된다."""
        total = 0
        with self._read_lock:
            for reader in list(self.readers.values()):
                try:
                    total += reader.poll()
                except Exception as exc:
                    _debug(f"{reader.spec.name}: poll 실패 {exc!r}")
        return total

    # ── 내부 ────────────────────────────────────────────────────────────
    def _on_delta(self, delta: TokenDelta) -> None:
        try:
            self.meter.ingest(delta)
        except Exception as exc:
            _debug(f"ingest 실패 {exc!r}")

    def _start_observer(self) -> None:
        if Observer is None:
            _debug("watchdog 미설치 → 폴링만 사용")
            return
        try:
            observer = Observer()
            scheduled = 0
            for reader in self.readers.values():
                for root in reader.spec.existing_roots():
                    observer.schedule(_EventHandler(reader, self._queue), str(root), recursive=True)
                    scheduled += 1
            if not scheduled:
                return
            observer.start()
            self._observer = observer
        except Exception as exc:  # 실패해도 폴링만으로 정상 동작해야 한다
            _debug(f"watchdog 시작 실패, 폴링만 사용 {exc!r}")
            self._observer = None

    def _queue(self, reader: ServiceReader, path: Path) -> None:
        with self._pending_lock:
            self._pending[str(path)] = (reader, time.time())

    def _loop(self) -> None:
        last_full = 0.0
        while not self._stop.is_set():
            now = time.time()
            due: List[Tuple[ServiceReader, Path]] = []
            with self._pending_lock:
                for key, (reader, queued) in list(self._pending.items()):
                    if now - queued >= DEBOUNCE_SEC:
                        due.append((reader, Path(key)))
                        del self._pending[key]
            if due:
                with self._read_lock:
                    for reader, path in due:
                        reader.read_file(path, emit=True)
            if now - last_full >= self.poll_seconds:
                last_full = now
                self.poll_once()
            self._stop.wait(TICK_SEC)
