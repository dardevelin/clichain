"""Basic piping: echo | grep | sort | wc."""

from clichain import tool

echo = tool("echo")
grep = tool("grep")
sort = tool("sort")
wc = tool("wc")

result = (
    echo("cherry\napple\nbanana\napricot")
    .pipe(grep("a"))
    .pipe(sort())
    .pipe(wc("-l"))
    .run()
)

print(f"Lines matching 'a': {result.stdout.strip()}")
