"""The rotation log, rotations/demo.csv: one row per rotation, oldest first.

    date,active_user_before_rotation,active_user_after_rotation,actor,approved_by,run_url
    2026-10-05,blue,green,octocat,hubot;monalisa,https://github.com/octocat/basic-rotation/actions/runs/1

On `date`, the API's login switched from the before-login to the after-login,
and the before-login's password was switched off. `actor` started the
rotation, `approved_by` lists who approved its steps, separated by `;`, and
`run_url` is the run that carried it out, or `local` for a rotation done by
hand. Nothing reads the log at runtime: it only records rotations. Rows are
only ever appended, and the log never holds a password.

    python3 rotate/rotation_log.py check FILE [--previous FILE]

checks a log against its rules and, given the log as it was on the base
commit, that rows were only added, never changed or removed.
"""

import argparse
import csv
import datetime
import io
import re
import sys
from typing import List, NamedTuple

HEADER = [
    "date",
    "active_user_before_rotation",
    "active_user_after_rotation",
    "actor",
    "approved_by",
    "run_url",
]

# The names the log uses, and the database login each one stands for.
NAMES = {"blue": "app_blue", "green": "app_green"}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_APPROVERS = re.compile(r"[A-Za-z0-9-]+(;[A-Za-z0-9-]+)*")
_RUN_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+|local")


class RotationLogError(ValueError):
    """The log breaks one of its rules."""


class Rotation(NamedTuple):
    date: str
    active_user_before_rotation: str
    active_user_after_rotation: str
    actor: str
    approved_by: str
    run_url: str


def parse_rotations(text: str) -> List[Rotation]:
    """The rotations in `text`, oldest first; RotationLogError names the bad line."""
    rows = list(csv.reader(text.splitlines()))
    if not rows or [cell.strip() for cell in rows[0]] != HEADER:
        raise RotationLogError(f"line 1 must be the header {','.join(HEADER)}")
    rotations: List[Rotation] = []
    for line, row in enumerate(rows[1:], start=2):
        cells = [cell.strip() for cell in row]
        if not any(cells):
            continue
        rotation = _parse_row(line, cells)
        if rotations:
            _check_follows(line, rotations[-1], rotation)
        rotations.append(rotation)
    return rotations


def _parse_row(line: int, cells: List[str]) -> Rotation:
    if len(cells) != len(HEADER):
        raise RotationLogError(f"line {line}: expected {len(HEADER)} columns, got {len(cells)}")
    rotation = Rotation(*cells)
    if not _DATE.fullmatch(rotation.date):
        raise RotationLogError(f"line {line}: date must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(rotation.date)
    except ValueError:
        raise RotationLogError(f"line {line}: {rotation.date} is not a real date")
    for column in ("active_user_before_rotation", "active_user_after_rotation"):
        if getattr(rotation, column) not in NAMES:
            raise RotationLogError(f"line {line}: {column} must be one of {', '.join(NAMES)}")
    if rotation.active_user_before_rotation == rotation.active_user_after_rotation:
        raise RotationLogError(f"line {line}: the login before and after must differ")
    if not rotation.actor:
        raise RotationLogError(f"line {line}: actor is empty")
    if not _APPROVERS.fullmatch(rotation.approved_by):
        raise RotationLogError(f"line {line}: approved_by must be GitHub logins separated by ';'")
    if not _RUN_URL.fullmatch(rotation.run_url):
        raise RotationLogError(f"line {line}: run_url must be a GitHub Actions run link, or local")
    return rotation


def _check_follows(line: int, previous: Rotation, rotation: Rotation) -> None:
    if rotation.active_user_before_rotation != previous.active_user_after_rotation:
        raise RotationLogError(
            f"line {line}: the login before this rotation must be "
            f"{previous.active_user_after_rotation}, the one the previous row switched to"
        )
    if rotation.date < previous.date:
        raise RotationLogError(f"line {line}: dated before the previous row")


def read_rotations(path: str) -> List[Rotation]:
    with open(path) as file:
        return parse_rotations(file.read())


def check_only_appended(previous: List[Rotation], current: List[Rotation]) -> None:
    if current[: len(previous)] != previous:
        raise RotationLogError("rows already in the log were changed or removed; only add rows")


def append_rotation(path: str, rotation: Rotation) -> None:
    """Add `rotation` at the end of the log, after checking it follows the rules."""
    rotations = read_rotations(path)
    rotation = _parse_row(len(rotations) + 2, list(rotation))
    if rotations:
        _check_follows(len(rotations) + 2, rotations[-1], rotation)
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(rotation)
    with open(path, "a") as file:
        file.write(buffer.getvalue())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check the rotation log.")
    parser.add_argument("command", choices=["check"])
    parser.add_argument("file")
    parser.add_argument("--previous", help="the log as it was on the base commit")
    args = parser.parse_args(argv)
    try:
        rotations = read_rotations(args.file)
        if args.previous:
            check_only_appended(read_rotations(args.previous), rotations)
    except (RotationLogError, FileNotFoundError) as error:
        print(f"{args.file}: {error}")
        return 1
    print(f"{args.file}: OK, {len(rotations)} rotation(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
