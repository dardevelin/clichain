"""Capture output from one step and use it in a later step's arguments."""

from clichain import tool

pwd = tool("pwd")
ls = tool("ls")

result = (
    pwd()
    .capture("cwd")
    .pipe(ls("-la", "{cwd}"))
    .run()
)

print(result.stdout)
