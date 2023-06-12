#!/usr/bin/env python3.10
import os
from typing import (
    NamedTuple,
    Callable,
    TypeVar,
    Iterable,
    Iterator,
    IO,
    Any,
    ContextManager,
    ParamSpec,
    Generator,
)
import typer
import tempfile
from contextlib import contextmanager
from pathlib import Path
import inotify.adapters
import inotify.constants
from concurrent.futures import wait, ThreadPoolExecutor, Future
from threading import Lock, Condition, Event
from time import time
from functools import wraps
import shutil


T = TypeVar("T")
C = TypeVar("C", bound=ContextManager)
P = ParamSpec("P")
R = TypeVar("R")


def move_on_close(file: IO[str], dest: Path):
    return on_exit(
        file,
        lambda e: shutil.move(file.name, dest) if e is None else os.remove(file.name),
    )


@contextmanager
def on_exit(
    manager: C, func: Callable[[BaseException | None], None]
) -> Generator[C, None, None]:
    try:
        with manager:
            yield manager
        func(None)
    except BaseException as e:
        func(e)
        raise e


@contextmanager
def before_exit(manager: C, func: Callable[[C], None]):
    with manager:
        try:
            yield manager
        finally:
            func(manager)


class ConsumeResult(NamedTuple):
    match: str
    rest: str

    def __bool__(self):
        return len(self.match) != 0

    def map_split(self, fn: Callable[[str], "str | ConsumeResult"]):
        result = fn(self.rest)
        match, new_rest = result if isinstance(result, ConsumeResult) else ("", result)
        return ConsumeResult(self.match + match, new_rest)

    @classmethod
    def from_prefix(cls, input: str, prefix: str):
        if input.startswith(prefix):
            return ConsumeResult(prefix, input[len(prefix) :])


def skip(index: int, elements: Iterable[T]) -> Iterator[T]:
    for i, element in enumerate(elements):
        if i != index:
            yield element


def consume(input: str, *prefixes: str) -> ConsumeResult:
    for i, prefix in enumerate(prefixes):
        if match := ConsumeResult.from_prefix(input, prefix):
            return match.map_split(lambda rest: consume(rest, *skip(i, prefixes)))

    return ConsumeResult("", input)


app = typer.Typer()


def future_raise(future: Future[Any]):
    e = future.exception()
    if e is not None:
        raise e


def exceptions_suck_ass(func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs):
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt as e:
            raise typer.Abort from e

    return wrapper


def break_when(should_stop: Event, iter: Iterable[T]) -> Iterator[T]:
    for item in iter:
        yield item
        if should_stop.is_set():
            break


def debounce(
    iter: Callable[[Event], Iterable[T]], timeout: float, max_queue_size: int = 1024
) -> Iterator[list[T]]:
    """
    Yields lists of elements from `iter` where the time taken to yield
    them is less than `timeout`

    ```
    def sleepn(time: float):
        sleep(time)
        return time

    assert [*debounce(map(sleepn, [.1, .1, .2, .1, .3, .3, .0]), .15)] == [
        [0.1, 0.1],
        [0.2, 0.1],
        [0.3],
        [0.3, 0.0]
    ]
    ```
    """
    queue: list[T] = []
    last_time: float = time()
    lock = Lock()
    queue_cleared = Condition()
    stop_runnning_event = Event()

    def get_elements():
        nonlocal last_time
        for elem in break_when(stop_runnning_event, iter(stop_runnning_event)):
            with lock:
                size_good = len(queue) < max_queue_size
            if not size_good:
                with queue_cleared:
                    queue_cleared.wait()
            with lock:
                last_time = time()
                queue.append(elem)

    with before_exit(
        ThreadPoolExecutor(max_workers=1), lambda _: stop_runnning_event.set()
    ) as executor:
        get_elements_fut = executor.submit(get_elements)
        while True:
            with lock:
                wait_for = last_time + timeout - time()
            if wait_for < 0:
                wait_for = timeout
                if len(queue) > 0:
                    with lock:
                        queue, old_queue = [], queue
                    with queue_cleared:
                        queue_cleared.notify_all()
                    yield old_queue
            done, not_done = wait([get_elements_fut], timeout=wait_for)
            if len(done) > 0:
                future_raise(*done)
                yield queue
                return
            (get_elements_fut,) = not_done


def prepend_pattern(pattern: str, dir: str) -> str:
    if pattern.strip() == "":
        return ""
    if pattern.startswith("//"):
        return pattern
    _ = (result := consume(pattern, "#include ")) or (
        result := consume(pattern, "(?d)", "(?i)", "!")
    )
    # Handle the case of floating patterns
    result = result.map_split(
        lambda path: path[1:] if path.startswith("/") else ("**/" + path)
    )
    return result.match + os.path.join(dir, result.rest)


def ensure_endln(line: str):
    return line + "\n" if not line.endswith("\n") else line


def write_from_ignores(ignores_root: Path, folder_ignore: IO[str], default: IO[str]):
    for file in ignores_root.iterdir():
        if not file.is_file():
            continue
        name = file.name
        with file.open() as file_contents:
            folder_ignore.write(f"// Entries from {name!r}\n\n")
            for line in map(ensure_endln, file_contents):
                folder_ignore.write(prepend_pattern(line, f"/{name}"))

    folder_ignore.write(f"// Default entries\n\n")
    folder_ignore.writelines(map(ensure_endln, default))


@app.command()
@exceptions_suck_ass
def monitor(
    ignores_root: Path,
    folder_ignore: Path,
    default: typer.FileText,
    timeout: float = 5.0,
):
    def receive_events(should_stop: Event):
        i = inotify.adapters.Inotify()
        i.add_watch(f"{ignores_root}", inotify.constants.IN_CLOSE_WRITE)
        for e in break_when(should_stop, i.event_gen(yield_nones=True)):
            if e is None:
                continue
            (_, _, _, filename) = e
            typer.echo(
                f"Received event close after write for file {filename!r} on ignores directory, waiting to group events"
            )
            yield e

    typer.echo("Waiting for events")
    # `debounce` groups events that happen within `timeout` of each other
    for events in debounce(receive_events, timeout):
        typer.echo(f"Pooled {len(events)} event(s), updating ignores directory")
        with move_on_close(
            tempfile.NamedTemporaryFile("w", delete=False), folder_ignore
        ) as temp_file:
            write_from_ignores(ignores_root, temp_file, default)


@app.command()
def oneshot(ignores_root: Path, folder_ignore: Path, default: typer.FileText):
    with move_on_close(
        tempfile.NamedTemporaryFile("w", delete=False), folder_ignore
    ) as temp_file:
        write_from_ignores(ignores_root, temp_file, default)


@app.command()
def single_file(new_root: str, input: typer.FileText, out: typer.FileTextWrite):
    out.writelines(prepend_pattern(pattern, new_root) for pattern in input)


if __name__ == "__main__":
    app()
