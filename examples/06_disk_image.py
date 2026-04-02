"""Disk imaging with checksum.

Recreates: pv /dev/sda | tee /mnt/DiskImage.img | shasum -a 256 > /mnt/DiskImage.img.sha256

Compare with raw subprocess (35 lines of manual pipe wiring)
vs clichain (7 lines).
"""

from clichain import tool

pv = tool("pv")
tee = tool("tee")
shasum = tool("shasum")

pipeline = (
    pv("/dev/sda")
    .pipe(tee("/mnt/DiskImage.img"))
    .pipe(shasum("-a", "256"))
    .redirect(stdout="/mnt/DiskImage.img.sha256")
)

# Inspect before running
pipeline.describe()

# Uncomment to execute:
# pipeline.run()
