"""Convert all video files in the current directory to mp3.

Demonstrates: capture, filter, each with workers.
"""

import sys

from clichain import set_output, tool

set_output(sys.stdout)

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".webm")

pwd = tool("pwd")
ls = tool("ls")
ffmpeg = tool("ffmpeg", version=">=6.0", msg="brew install ffmpeg")

pipeline = (
    pwd()
    .capture("cwd")
    .pipe(ls("{cwd}"))
    .filter(lambda f: f.endswith(VIDEO_EXTS))
    .each(
        lambda f: ffmpeg("-i", f, "-vn", f.rsplit(".", 1)[0] + ".mp3"),
        workers=4,
    )
)

# Inspect requirements
pipeline.describe()

# Uncomment to execute:
# result = pipeline.run()
# result.report()
