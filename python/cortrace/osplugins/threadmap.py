"""Files exchanged with cortrace-decode.

values file   `cortrace-decode --dump-switch-values`: one 0xPOINTER per line
thread map    `cortrace-decode --thread-map`: 0xPOINTER <TAB> tid <TAB> name
"""

from . import ThreadInfo


def read_values(path):
    """The pointers in a values file."""
    values = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                values.add(int(line, 0))
    return values


def read_map(path):
    """{pointer: ThreadInfo} from a thread map (comments and blanks skipped)."""
    threads = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t", 2)
            if len(fields) < 2:
                continue
            name = fields[2].strip() if len(fields) == 3 else ""
            threads[int(fields[0], 0)] = ThreadInfo(int(fields[1]), name)
    return threads


def write_map(path, threads):
    """Write {pointer: ThreadInfo} as a thread map."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("# pointer\ttid\tname\n")
        for ptr in sorted(threads):
            info = threads[ptr]
            f.write(f"0x{ptr:08x}\t{info.tid}\t{info.name}\n")
