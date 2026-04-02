"""clichain CLI — compile, explain, check."""

from __future__ import annotations

import argparse
import sys


def cmd_explain(args: argparse.Namespace) -> int:
    from clichain.core import _ERROR_DETAIL, _EXIT_CODES, _SIGNALS

    code = args.code.upper()

    if code.startswith("S"):
        sig = int(code[1:])
        if sig in _SIGNALS:
            name, desc = _SIGNALS[sig]
            print(f"error[{code}]: signal {sig} ({name})")
            print(f"  {desc}")
        else:
            print(f"error[{code}]: signal {sig}")
            print("  unknown signal")
    elif code.startswith("X"):
        exit_code = int(code[1:])
        if exit_code in _EXIT_CODES:
            print(f"error[{code}]: exit {exit_code}")
            print(f"  {_EXIT_CODES[exit_code]}")
        else:
            print(f"error[{code}]: exit {exit_code}")
            print("  unknown exit code")
    else:
        print(f"unknown error code: {code}")
        return 1

    detail = _ERROR_DETAIL.get(code)
    if detail:
        print()
        print(f"  {detail}")

    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import importlib.util

    from clichain.core import Pipeline, set_output

    set_output(sys.stderr)

    spec = importlib.util.spec_from_file_location("__clichain_script__", args.script)
    if not spec or not spec.loader:
        print(f"error: cannot load {args.script}", file=sys.stderr)
        return 1

    # Import the script — this will create tool() instances
    # but won't run pipelines unless they call .run() at module level
    module = importlib.util.module_from_spec(spec)

    # Collect all Cmd and Pipeline objects after import
    import contextlib

    with contextlib.suppress(SystemExit):
        spec.loader.exec_module(module)

    # Find all Pipeline/Cmd objects in the module and check them
    from clichain.core import Cmd

    checked = False
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, Pipeline):
            print(f"pipeline: {name}", file=sys.stderr)
            obj.check()
            checked = True
        elif isinstance(obj, Cmd):
            print(f"tool: {name} ({obj._binary})", file=sys.stderr)
            obj.check()
            checked = True

    if not checked:
        print("no pipelines or tools found in script", file=sys.stderr)

    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    import importlib.util

    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "error: pyinstaller not installed\n  install with: pip install cmdchain[compile]",
            file=sys.stderr,
        )
        return 1

    from PyInstaller import __main__ as pyinstaller_main  # type: ignore[import-not-found]

    script = args.script
    output = args.output or script.rsplit(".", 1)[0]

    pyinstaller_args = [
        script,
        "--onefile",
        "--name",
        output,
        "--noconfirm",
    ]

    if args.clean:
        pyinstaller_args.append("--clean")

    print(f"compiling {script} -> {output}")
    pyinstaller_main.run(pyinstaller_args)

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="clichain",
        description="CLI tool chaining for Python",
    )
    sub = parser.add_subparsers(dest="command")

    # explain
    p_explain = sub.add_parser("explain", help="explain an error code")
    p_explain.add_argument("code", help="error code (e.g. S13, X127)")

    # check
    p_check = sub.add_parser("check", help="preflight a script")
    p_check.add_argument("script", help="path to .py script")

    # compile
    p_compile = sub.add_parser("compile", help="compile script to binary")
    p_compile.add_argument("script", help="path to .py script")
    p_compile.add_argument("-o", "--output", help="output binary name")
    p_compile.add_argument("--clean", action="store_true", help="clean build artifacts")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    handlers = {
        "explain": cmd_explain,
        "check": cmd_check,
        "compile": cmd_compile,
    }

    sys.exit(handlers[args.command](args))
