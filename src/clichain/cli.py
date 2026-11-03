"""clichain CLI — explain, check, compile."""

from __future__ import annotations

import argparse
import importlib.util
import sys


def cmd_explain(args: argparse.Namespace) -> int:
    from clichain.errors import explain_code_lines, parse_code

    parsed = parse_code(args.code)
    if parsed is None:
        print(f"unknown error code: {args.code!r} (expected S<signal> or X<exit>, e.g. S13, X127)")
        return 1
    print("\n".join(explain_code_lines(*parsed)))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Import the script without executing any pipeline, then check every tool it uses.

    Exits 1 when any check with on_fail="error" fails, so CI can gate on it.
    """
    from clichain.pipeline import Pipeline, dry_run
    from clichain.render import set_output

    set_output(sys.stderr)
    spec = importlib.util.spec_from_file_location("__clichain_script__", args.script)
    if spec is None or spec.loader is None:
        print(f"error: cannot load {args.script}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    with dry_run() as executed:
        try:
            spec.loader.exec_module(module)
        except SystemExit:
            pass
        except Exception as exc:  # the script is still worth checking up to this point
            print(f"warning: script raised {type(exc).__name__}: {exc}", file=sys.stderr)

    found: dict[int, tuple[str, Pipeline]] = {}
    for name, obj in vars(module).items():
        if isinstance(obj, Pipeline):
            found.setdefault(id(obj), (name, obj))
    for pipeline in executed:
        found.setdefault(id(pipeline), (f"run at {pipeline!r}", pipeline))

    if not found:
        print("no pipelines or tools found in script", file=sys.stderr)
    failed = False
    for name, pipeline in found.values():
        print(f"{name}: {pipeline!r}", file=sys.stderr)
        failed |= any(r.should_stop for r in pipeline.check())
    return 1 if failed else 0


def cmd_compile(args: argparse.Namespace) -> int:
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "error: pyinstaller not installed\n  install with: pip install cmdchain[compile]",
            file=sys.stderr,
        )
        return 1

    from PyInstaller import __main__ as pyinstaller_main  # type: ignore[import-not-found]

    output = args.output or args.script.rsplit(".", 1)[0]
    pyinstaller_args = [args.script, "--onefile", "--name", output, "--noconfirm"]
    if args.clean:
        pyinstaller_args.append("--clean")
    print(f"compiling {args.script} -> {output}")
    pyinstaller_main.run(pyinstaller_args)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="clichain", description="CLI tool chaining for Python")
    sub = parser.add_subparsers(dest="command")

    p_explain = sub.add_parser("explain", help="explain an error code")
    p_explain.add_argument("code", help="error code (e.g. S13, X127)")

    p_check = sub.add_parser("check", help="preflight a script (exit 1 if a check fails)")
    p_check.add_argument("script", help="path to .py script")

    p_compile = sub.add_parser("compile", help="compile script to binary")
    p_compile.add_argument("script", help="path to .py script")
    p_compile.add_argument("-o", "--output", help="output binary name")
    p_compile.add_argument("--clean", action="store_true", help="clean build artifacts")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    handlers = {"explain": cmd_explain, "check": cmd_check, "compile": cmd_compile}
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
