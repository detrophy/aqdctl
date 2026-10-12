"""A device kept open for another program, over JSON lines.

'serve' reads one JSON request per line on stdin and answers each with one
JSON line on stdout; everything else it prints goes to stderr. A program that
shows the device - a GUI, say - starts it as a child process and keeps it
running, so it runs with the same access as any other aqdctl command.

Requests, each with an optional "id" that the answer repeats:

  {"op": "get", "keys": [PREFIX, ...]}          the values, as get --json
  {"op": "schema", "keys": [PREFIX, ...]}       the keys, as get --keys --json
  {"op": "apply", "lines": [LINE, ...],         lines as a batch takes them
   "dry_run": false, "force": false}
  {"op": "save"}                                send a pending save now
  {"op": "close"}                               save, then end

An answer is {"id", "ok": true, ...} or {"id", "ok": false, "error":
{"status", "message"}}, with the statuses of the command line.

A settings write takes effect at once. The save command follows 3.8 s after
the last write, each write restarting the wait, as Aquasuite does; a slider moved through twenty steps is written twenty times and saved
once. A pending save is sent at once on close, at the end of stdin and on
SIGTERM. Each report is backed up once per session, before its first write."""

import argparse
import contextlib
import json
import os
import select
import signal
import sys
import time

from . import batch, core, keys


class Stop(Exception):
    """SIGTERM: end the session, saving first."""


class Session:
    def __init__(self, dev, clock=time.monotonic):
        self.dev, self.clock = dev, clock
        self.save_at = None             # when the pending save is due
        self.backed_up = set()
        self.busy = self.stopping = False

    # ------------------------------------------------------------ saving

    def due_in(self):
        """Seconds until the pending save, or None if none is pending."""
        if self.save_at is None:
            return None
        return max(0.0, self.save_at - self.clock())

    def tick(self):
        """Send the pending save if it is due."""
        if self.save_at is not None and self.clock() >= self.save_at:
            self.save()

    def save(self):
        """Send the pending save now. True if one was pending."""
        if self.save_at is None:
            return False
        self.save_at = None
        self.dev.save()
        return True

    def _write(self, before, after):
        rid = after[0]
        if rid not in self.backed_up:
            core.backup(before, self.dev)
            self.backed_up.add(rid)
        core.store(self.dev, after, save=False)
        if rid == core.CTRL_REPORT_ID:
            self.save_at = self.clock() + core.SAVE_DELAY

    # ---------------------------------------------------------- requests

    def handle(self, text):
        """The answer to one request line, as a dict."""
        try:
            request = json.loads(text)
            if not isinstance(request, dict):
                raise ValueError
        except ValueError:
            return _error(None, core.EXIT_USAGE, "A request is one JSON object per line.")
        rid = request.get("id")
        op = request.get("op")
        handler = {"get": self._get, "schema": self._schema, "apply": self._apply,
                   "save": self._save, "close": self._close}.get(op)
        if handler is None:
            return _error(rid, core.EXIT_USAGE, "Unknown op %r: get, schema, apply, save "
                          "or close." % (op,))
        try:
            answer = handler(request)
        except core.Failure as exc:
            return _error(rid, exc.code, exc.message)
        except SystemExit as exc:
            if isinstance(exc.code, int):
                return _error(rid, exc.code, "")
            return _error(rid, core.EXIT_INVALID, str(exc.code))
        return dict({"id": rid, "ok": True}, **answer)

    def _prefixes(self, request):
        prefixes = request.get("keys", [])
        if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
            core.fail(core.EXIT_USAGE, "keys is a list of key prefixes.")
        return prefixes

    def _get(self, request):
        rows = keys.values(self.dev, self._prefixes(request), opened=True)
        return {"values": {k.name: k.json(v) for k, v in rows}}

    def _schema(self, request):
        return {"keys": [k.schema() for k in keys.described(self.dev,
                                                            self._prefixes(request))]}

    def _apply(self, request):
        lines = request.get("lines")
        if not isinstance(lines, list) or not all(isinstance(l, str) for l in lines):
            core.fail(core.EXIT_USAGE, "lines is a list of lines as a batch takes them.")
        args = argparse.Namespace(force=bool(request.get("force")),
                                  dry_run=bool(request.get("dry_run")), yes=True,
                                  verbose=False, backup=None, typed=None)
        with core.device_lock(self.dev.serial):
            plan = batch.prepare(self.dev, batch.parse("\n".join(lines)), args)
            written = bool(plan.changes) and not args.dry_run
            if written:
                batch.write(self.dev, plan, args, writer=self._write)
        return {"changes": [{"key": k.name, "old": k.json(old), "new": k.json(new)}
                            for k, old, new in plan.changes],
                "written": written}

    def _save(self, request):
        return {"saved": self.save()}

    def _close(self, request):
        return {"saved": self.save(), "closing": True}


def _error(rid, status, message):
    return {"id": rid, "ok": False, "error": {"status": status, "message": message}}


def run(session, fd, out):
    """Answer the requests read from file descriptor `fd` until close or its
    end, sending each save when due and a pending one before returning. The
    descriptor is read directly: several requests can arrive in one read, and
    a buffered file would hold them back from select."""
    def emit(answer):
        out.write(json.dumps(answer, ensure_ascii=False) + "\n")
        out.flush()

    pending, ended = b"", False
    try:
        while not session.stopping:
            if b"\n" not in pending and not ended:
                ready, _, _ = select.select([fd], [], [], session.due_in())
                if not ready:
                    session.tick()
                    continue
                chunk = os.read(fd, 65536)
                pending += chunk
                ended = not chunk
                continue
            if b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
            elif pending.strip():
                line, pending = pending, b""        # a last line without a newline
            else:
                break
            if not line.strip():
                continue
            session.busy = True
            answer = session.handle(line.decode("utf-8", "replace"))
            session.busy = False
            emit(answer)
            if answer.get("closing"):
                break
            session.tick()
    except Stop:
        pass
    finally:
        session.save()


def cmd_serve(dev, args):
    out = sys.stdout
    session = Session(dev)

    def stop(signum, frame):
        # Between requests, end at once; during one, after its answer.
        session.stopping = True
        if not session.busy:
            raise Stop()
    signal.signal(signal.SIGTERM, stop)
    out.write(json.dumps({"serve": keys.API_VERSION, "serial": dev.serial,
                          "type": dev.kind.NAME}) + "\n")
    out.flush()
    # Messages of the commands a request runs - backups, notes of the safety
    # checks - go to stderr; stdout carries only the answers.
    with contextlib.redirect_stdout(sys.stderr):
        run(session, sys.stdin.fileno(), out)


def register(sub, prefix):
    p = sub.add_parser(
        "serve", formatter_class=core.HelpFormatter,
        help="keep the device open for another program, over JSON lines",
        description=__doc__.split("\n\n", 1)[1],
        epilog="example:\n"
               "  printf '{\"op\": \"get\", \"keys\": [\"live.flow\"]}\\n' | %s serve"
               % prefix)
    p.set_defaults(func=cmd_serve)
