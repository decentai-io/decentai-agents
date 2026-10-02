"""Where one program runs: a folder made for it, holding the person's
files and the program, and gone when the call ends.

The program is a process of its own, started from the agent's
interpreter in that folder. It has the agent's surroundings and no
more: the same user, the same fence, the same way out — which the
platform has opened, for this call, to the hosts the person allowed.
"""

import asyncio
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

PROGRAM = "program.py"
SAFE_RE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]+")


class Ran:
    """How a run ended: its exit code, and what it printed."""

    def __init__(self, code: int, output: str, timed_out: bool = False):
        self.code = code
        self.output = output
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


class Workspace:
    #: What is kept of what a program printed: its beginning and its
    #: end, which is where the answer and the error are.
    HEAD_CHARS = 6000
    TAIL_CHARS = 3000
    #: What is handed back of what it made.
    FILES_MAX = 10
    FILE_MAX_BYTES = 25 * 1024 * 1024

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="code-"))
        self.folder = self.base / "work"
        self.said = self.base / "output.txt"
        self._before: Dict[str, tuple] = {}

    @staticmethod
    def name(filename: str, taken: List[str]) -> str:
        """A file's own name, made safe to lie in the working folder:
        no folder before it, nothing a shell reads, and not the
        program's or another file's."""
        name = SAFE_RE.sub("_", Path(str(filename or "")).name).strip(" .") or "file"
        stem, dot, ending = name.rpartition(".")
        if not dot:
            stem, ending = name, ""
        candidate, count = name, 1
        while candidate.lower() in (PROGRAM, *(t.lower() for t in taken)):
            count += 1
            candidate = f"{stem}-{count}" + (f".{ending}" if ending else "")
        return candidate

    def fill(self, files: Dict[str, bytes], code: str) -> None:
        """The folder as a run finds it: the person's files and the
        program, and nothing an earlier run left."""
        shutil.rmtree(self.folder, ignore_errors=True)
        self.folder.mkdir(parents=True)
        for name, content in files.items():
            (self.folder / name).write_bytes(content)
        (self.folder / PROGRAM).write_text(code, encoding="utf-8")
        self._before = self._listing()

    def _listing(self) -> Dict[str, tuple]:
        found = {}
        for path in self.folder.rglob("*"):
            if path.is_file() and not path.is_symlink() \
                    and "__pycache__" not in path.parts:
                stat = path.stat()
                found[path.relative_to(self.folder).as_posix()] = (
                    stat.st_size, stat.st_mtime_ns)
        return found

    async def run(self, packages: Optional[str], secrets: Dict[str, str],
                  seconds: float) -> Ran:
        """The program, run to its end or to ``seconds``. ``packages``
        is the folder the platform installed the allowed packages in;
        ``secrets`` reach the program as its environment and are taken
        out of what it printed."""
        environment = {**os.environ, **secrets,
                       "PYTHONIOENCODING": "utf-8",
                       "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONUNBUFFERED": "1"}
        environment.pop("PYTHONPATH", None)
        if packages:
            environment["PYTHONPATH"] = str(packages)
        timed_out = False
        with open(self.said, "wb") as said:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", PROGRAM, cwd=str(self.folder),
                env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=said, stderr=asyncio.subprocess.STDOUT,
                # A group of its own, so that what the program started
                # ends with it.
                **({"start_new_session": True} if os.name != "nt" else {}))
            try:
                await asyncio.wait_for(process.wait(), seconds)
            except asyncio.TimeoutError:
                timed_out = True
                self._end(process)
                await process.wait()
            except asyncio.CancelledError:
                self._end(process)
                raise
        return Ran(process.returncode if process.returncode is not None else 1,
                   self._printed(secrets), timed_out)

    @staticmethod
    def _end(process) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    def _printed(self, secrets: Dict[str, str]) -> str:
        try:
            text = self.said.read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""
        for value in secrets.values():
            if value:
                text = text.replace(value, "[hidden]")
        if len(text) > self.HEAD_CHARS + self.TAIL_CHARS:
            left_out = len(text) - self.HEAD_CHARS - self.TAIL_CHARS
            text = (text[: self.HEAD_CHARS]
                    + f"\n… ({left_out} characters left out) …\n"
                    + text[-self.TAIL_CHARS:])
        return text.strip()

    def made(self) -> List[Path]:
        """The files the run made or changed, by name."""
        after = self._listing()
        return [self.folder / name for name in sorted(after)
                if name != PROGRAM and after[name] != self._before.get(name)]

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)
