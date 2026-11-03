"""S4: signal handling cleans up children and then behaves as the host process expects."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from .conftest import run_snippet

# A child that records its pid, then sleeps long enough to be noticed if orphaned.
_SLEEPER = "tool('sh')('-c', 'echo $$ >> {pids}; exec sleep 30')"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_all_dead(pidfile: Path) -> None:
    pids = [int(p) for p in pidfile.read_text().split()]
    assert pids, "children never started"
    deadline = time.monotonic() + 3
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [p for p in pids if _alive(p)], "orphaned children"


def _self_signal(signum: int, after: float = 0.5) -> str:
    return f"threading.Timer({after}, os.kill, (os.getpid(), {signum})).start()"


def test_sigint_raises_keyboardinterrupt_after_cleanup(tmp_path):
    pids = tmp_path / "pids"
    snippet = run_snippet(f"""
        import os, signal, threading
        from clichain import tool
        {_self_signal(signal.SIGINT)}
        try:
            {_SLEEPER.format(pids=pids)}.pipe(tool('cat')()).run()
            print("returned")
        except KeyboardInterrupt:
            print("KeyboardInterrupt")
        print(signal.getsignal(signal.SIGINT) is signal.default_int_handler)
    """)
    assert snippet.stdout.split() == ["KeyboardInterrupt", "True"]
    _assert_all_dead(pids)


def test_non_raising_handler_gets_result_130(tmp_path):
    pids = tmp_path / "pids"
    snippet = run_snippet(f"""
        import os, signal, threading
        from clichain import tool
        seen = []
        signal.signal(signal.SIGINT, lambda s, f: seen.append(s))
        {_self_signal(signal.SIGINT)}
        r = {_SLEEPER.format(pids=pids)}.run()
        print(r.returncode, seen)
    """)
    assert snippet.stdout.strip() == "130 [2]"
    _assert_all_dead(pids)


def test_sigterm_default_kills_process_after_cleanup(tmp_path):
    pids = tmp_path / "pids"
    snippet = run_snippet(f"""
        import os, signal, threading
        from clichain import tool
        {_self_signal(signal.SIGTERM)}
        {_SLEEPER.format(pids=pids)}.run()
        print("survived")
    """)
    assert snippet.returncode == -signal.SIGTERM
    assert "survived" not in snippet.stdout
    _assert_all_dead(pids)


def test_ignored_sigint_stays_ignored():
    snippet = run_snippet(f"""
        import os, signal, threading
        from clichain import tool
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        {_self_signal(signal.SIGINT, after=0.2)}
        r = tool('sleep')('0.6').run()
        print(r.returncode, signal.getsignal(signal.SIGINT) is signal.SIG_IGN)
    """)
    assert snippet.stdout.strip() == "0 True"


def test_sigterm_reaches_parallel_each_children(tmp_path):
    pids = tmp_path / "pids"
    snippet = run_snippet(f"""
        import os, signal, threading
        from clichain import tool
        {_self_signal(signal.SIGTERM, after=1.0)}
        tool('printf')('a\\nb\\n').each(lambda _: {_SLEEPER.format(pids=pids)}, workers=2).run()
    """)
    assert snippet.returncode == -signal.SIGTERM
    _assert_all_dead(pids)


def test_run_from_worker_thread_installs_no_handlers():
    snippet = run_snippet("""
        import signal, threading
        from clichain import tool
        seen = []
        def work():
            seen.append(tool('echo')('x').run().ok)
            seen.append(signal.getsignal(signal.SIGINT) is signal.default_int_handler)
        t = threading.Thread(target=work); t.start(); t.join()
        print(seen)
    """)
    assert snippet.stdout.strip() == "[True, True]"
