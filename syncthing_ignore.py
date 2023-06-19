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
    NamedTuple,
    Annotated,
    TypeAlias,
)
import typer
import tempfile
from contextlib import contextmanager
from pathlib import Path
import inotify.adapters
import inotify.constants
from concurrent.futures import wait, ThreadPoolExecutor, Future
from threading import Lock, Condition, Event
import time
from functools import wraps
import shutil
import filecmp
import datetime
import croniter
import docker
from returns.result import Result, Success, Failure


T = TypeVar("T")
C = TypeVar("C", bound=ContextManager)
P = ParamSpec("P")
R = TypeVar("R")


def move_on_close(
    file: IO[str], dest: Path, on_file_changed: Callable[[], Any] = lambda: None
):
    def move_file(e):
        if e is not None:
            os.remove(file.name)
        if os.path.exists(dest) and filecmp.cmp(file.name, dest):
            return
        shutil.move(file.name, dest)
        on_file_changed()

    return on_exit(
        file,
        move_file,
    )


@contextmanager
def on_exit(
    manager: C, func: Callable[[BaseException | None], Any]
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
from_dir = typer.Typer()
app.add_typer(from_dir, name="from-dir")


def future_raise(future: Future[Any]):
    e = future.exception()
    if e is not None:
        raise e


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
    last_time: float = time.time()
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
                last_time = time.time()
                queue.append(elem)

    with before_exit(
        ThreadPoolExecutor(max_workers=1), lambda _: stop_runnning_event.set()
    ) as executor:
        get_elements_fut = executor.submit(get_elements)
        while True:
            with lock:
                wait_for = last_time + timeout - time.time()
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
    def ignores():
        for file in ignores_root.iterdir():
            if not file.is_file():
                continue
            name = file.name
            with file.open() as file_contents:
                yield name, file_contents

    write_stignore(ignores(), folder_ignore, default)


def write_stignore(
    ignores: Iterable[tuple[str, Iterable[str]]],
    folder_ignore: IO[str],
    default: IO[str],
):
    for name, contents in ignores:
        folder_ignore.write(f"// Entries from {name!r}\n\n")
        for line in map(ensure_endln, contents):
            folder_ignore.write(prepend_pattern(line, f"/{name}"))

    folder_ignore.write(f"// Default entries\n\n")
    folder_ignore.writelines(default)


class IgnoresDirArgs(NamedTuple):
    folder_ignore: Path
    base_ignore: Path
    ignores_root: Path


@from_dir.callback()
def ignores_dir(
    ctx: typer.Context,
    folder_ignore: Path = typer.Argument(
        ..., help="Path to the subdirectory ignore files"
    ),
    base_ignore: Path = typer.Argument(..., help="Path to the base ignore file"),
    ignores_root: Path = typer.Argument(
        ..., help="Path to the root ignore file for folder"
    ),
):
    ctx.obj = IgnoresDirArgs(folder_ignore, base_ignore, ignores_root)


def merge_ignores(params: IgnoresDirArgs):
    changed = False

    def set_changed():
        nonlocal changed
        changed = True

    with move_on_close(
        tempfile.NamedTemporaryFile("w", delete=False),
        params.folder_ignore,
        set_changed,
    ) as temp_file, open(params.base_ignore) as default:
        write_from_ignores(params.ignores_root, temp_file, default)
    return changed


def follow_crontab(
    crontab: croniter.croniter,
) -> Iterator[tuple[datetime.datetime, datetime.timedelta | None]]:
    delta = None
    while True:
        next_run: datetime.datetime = crontab.get_next(datetime.datetime)
        yield next_run, delta
        now = next_run.now(next_run.tzinfo)
        delta = next_run - now
        time.sleep(delta.total_seconds())


def validate_crontab(value: str) -> str:
    if not croniter.croniter.is_valid(value):
        raise typer.BadParameter("Invalid crontab format")
    return value


@from_dir.command()
def timed(
    ctx: typer.Context,
    crontab: Annotated[str, typer.Argument(..., callback=validate_crontab)],
    now: bool = True,
):
    params: IgnoresDirArgs = ctx.obj
    it = follow_crontab(croniter.croniter(crontab))
    if not now:
        next(it)
    for _ in it:
        if merge_ignores(params):
            typer.echo("Updated ignores file")


@from_dir.command()
def monitor(ctx: typer.Context, timeout: float = 5.0):
    params: IgnoresDirArgs = ctx.obj

    def receive_events(should_stop: Event):
        i = inotify.adapters.Inotify()
        i.add_watch(f"{params.ignores_root}", inotify.constants.IN_CLOSE_WRITE)
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
        merge_ignores(params)


@from_dir.command()
def oneshot(ignores_root: Path, folder_ignore: Path, default: typer.FileText):
    with move_on_close(
        tempfile.NamedTemporaryFile("w", delete=False), folder_ignore
    ) as temp_file:
        write_from_ignores(ignores_root, temp_file, default)


def guess_name_from_mounts(mounts, folder_root: Path) -> set[str]:
    sources = (Path(mount["Source"]) for mount in mounts)
    subdir_matches = (
        source.relative_to(folder_root)
        for source in sources
        if source.is_relative_to(folder_root)
    )
    name_candidates = set(match.parts[0] for match in subdir_matches)
    return name_candidates


BadAPIValue = dict[str, "str | BadAPIValue"]
ContainerID: TypeAlias = str
IgnoreContents: TypeAlias = str
IgnoreSubdirName: TypeAlias = str


@app.command()
def from_docker_labels(
    folder_ignore: Path,
    default_ignore: Path,
    host_folder_root: Path,
    stignore_contents_label: str,
    subdir_name_label: str,
):
    client = docker.from_env()
    ignores: dict[IgnoreSubdirName, IgnoreContents] = {}
    owners: dict[IgnoreSubdirName, ContainerID] = {}

    def add_container(
        container_id: ContainerID, labels, get_mounts: Callable[[], Any]
    ) -> Result[None | str, str]:
        name: IgnoreSubdirName
        if stignore_contents_label not in labels:
            return Success(None)
        contents = labels[stignore_contents_label]
        if subdir_name_label in labels:
            name = labels[subdir_name_label]
        else:
            guesses = guess_name_from_mounts(get_mounts(), host_folder_root)
            if len(guesses) != 1:
                return Failure(
                    "Failed to determine a single subdirectory based "
                    f"on mounts, guesses for subdirectory were {guesses!r}"
                )
            (name,) = guesses

        if name in owners:
            return Failure(
                f"Subdirectory name {name!r} in use by container ({owners[name]})"
            )
        owners[name] = container_id
        ignores[name] = contents
        return Success(name)

    def remove_container(container_id: ContainerID) -> IgnoreSubdirName | None:
        if container_id not in owners.values():
            return None
        for name, id in owners.items():
            if id == container_id:
                ignores.pop(name)
                owners.pop(name)
                return name

    def write_ignore_file():
        with (
            open(default_ignore) as fd_default_ignore,
            open(folder_ignore, "w") as fd_folder_ignore,
        ):
            write_stignore(
                ((name, contents.splitlines()) for name, contents in ignores.items()),
                fd_folder_ignore,
                fd_default_ignore,
            )

    typer.echo("Initializing based on existing containers")

    for container in client.api.containers(all=True):
        container_id: ContainerID = container["Id"]
        container_name: str = container["Names"][0]
        match add_container(
            container_id, container["Labels"], lambda: container["Mounts"]
        ):
            case Failure(reason):
                typer.echo(
                    f"Failure to process container {container_name!r} ({container_id}): {reason}"
                )
            case Success(str(name)):
                typer.echo(
                    f"Updating stignore contents for {container_name!r} ({container_id}) using subdir {name}"
                )
    write_ignore_file()

    typer.echo("Listening for container started events")

    for event in client.api.events(
        decode=True, filters=dict(type="container", event=["create", "destroy"])
    ):
        container_id: ContainerID = event["id"]
        attrs = event["Actor"]["Attributes"]
        container_name = attrs["name"]
        if event["Action"] == "destroy":
            name = remove_container(container_id)
            if name is None:
                continue
            typer.echo(
                f"Container {container_name!r} ({container_id}) using "
                f"subdir {name} removed, updating stignore contents"
            )
            write_ignore_file()
            continue

        match add_container(
            container_id,
            attrs,
            lambda: client.api.inspect_container(container_id)["Mounts"],
        ):
            case Failure(reason):
                typer.echo(
                    f"Failed to process container {container_name!r} ({container_id}): {reason}"
                )
            case Success(str(name)):
                typer.echo(
                    f"Updating stignore contents for {container_name!r} ({container_id}) using subdir {name}"
                )
        write_ignore_file()


if __name__ == "__main__":
    app()
