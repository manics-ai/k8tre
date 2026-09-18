#!/usr/bin/env python3
# Extract and run fenced codeblocks from a markdown file

from argparse import ArgumentParser
from os import getenv
from subprocess import run

CI = getenv("CI") == "true"

parser = ArgumentParser(description="Extract fenced codeblocks from a markdown file")
parser.add_argument("input", help="Input markdown file")
parser.add_argument(
    "--sub",
    action="append",
    default=[],
    help="Substitute a string in the input file (string=replacement)",
)
parser.add_argument("--run", action="store_true", help="Run the code")
parser.add_argument(
    "--blocks",
    "--index",
    dest="blocks",
    default=None,
    help="Block index or range to run (e.g. '3', '1:5', '1,3,5')",
)
parser.add_argument(
    "--languages",
    default="bash,shell,sh,",
    help="Comma-separated codeblock languages to extract (default: 'bash,shell,sh,')",
)

args = parser.parse_args()

allowed_languages = set(args.languages.split(","))

def parse_blocks_arg(blocks_arg, total_blocks):
    selected = set()
    for part in blocks_arg.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start_str, end_str = part.split(":", 1)
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else total_blocks
            selected.update(range(start, end))
        else:
            selected.add(int(part))
    return selected

raw_blocks = []
inside_code = False
current_lang = ""
script = []
block_start_line = 0
n = 0

with open(args.input) as f:
    for line in f:
        n += 1
        if line.startswith("```"):
            if not inside_code:
                inside_code = True
                current_lang = line[3:].strip()
                block_start_line = n
                script = []
            else:
                inside_code = False
                raw_blocks.append((current_lang, "".join(script), block_start_line))
                script = []
        elif inside_code:
            for sub in args.sub:
                find, replace = sub.split("=", 1)
                line = line.replace(find, replace)
            script.append(line)

if inside_code:
    raise ValueError(f"Line {n}: Incomplete script: Missing closing ```")

selected_indices = (
    parse_blocks_arg(args.blocks, len(raw_blocks))
    if args.blocks is not None
    else set(range(len(raw_blocks)))
)

for idx, (lang, s, start_line) in enumerate(raw_blocks):
    if idx not in selected_indices:
        continue
    if lang not in allowed_languages:
        print(f"Skipping block {idx} (line {start_line}, language '{lang}')")
        continue

    if CI:
        firstline = s.strip().splitlines()[0] if s.strip() else ""
        print(f"::group::Block {idx}: {firstline}")

    print(f"Running Block {idx} (line {start_line}, lang: '{lang}')\n```\n{s}\n```", flush=True)
    if args.run:
        run(["bash", "-o", "errexit", "-o", "xtrace", "-c", s], check=True)

    if CI:
        print("::endgroup::")
