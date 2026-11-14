#!/usr/bin/env python3
"""Install the PocketReader XL prototype onto a factory Billboard Badge."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
OVERLAY = ROOT / "overlay"
SWITCHER_BACKUP = ROOT / "backup" / "app_switcher.py"
EPD_BACKUP = ROOT / "backup" / "epd.py"
SLEEP_IMAGE = ROOT / "assets" / "sleep" / "pocketreader-sleep-floyd-steinberg-device.png"
OLD_SLEEP_IMAGE_SHA256 = 'aae321c0cf583ef6a998ae4e258d3b03bcd2e6bb56a1da545199fcf08bd75d79'
FONT_LICENSE = ROOT / "assets" / "opendyslexic-license.txt"
UBUNTU_LICENSE = ROOT / "assets/fonts/ubuntu-nerd-font/LICENCE.txt"
TINOS_LICENSE = ROOT / "assets/fonts/tinos-nerd-font/LICENSE.txt"

# Back up existing upstream files before replacing them. Missing optional files
# are normal on the factory image; never overwrite an existing local backup.
UPSTREAM_FILES = (
    ("lib/large_png.py", "lib/large_png.py"),
    ("lib/epd_grayscale.py", "lib/epd_grayscale.py"),
    ("lib/epd_partial.py", "lib/epd_partial.py"),
    ("lib/smooth_text.py", "lib/smooth_text.py"),
    ("data/rendered_luts.py", "data/rendered_luts.py"),
    ("apps/schedule.py", "apps/schedule.py"),
)


def command(port: str | None, *args: str) -> list[str]:
    result = ["mpremote"]
    if port:
        result.extend(["connect", port])
    result.append("resume")
    result.extend(args)
    return result


def run(cmd: list[str], dry_run: bool = False) -> None:
    print("+", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True)


def backup_optional(port: str | None, relative_path: str, dry_run: bool) -> None:
    backup = ROOT / "backup" / "upstream" / relative_path
    if backup.exists():
        print(f"Keeping existing backup: {backup}")
        return
    device_path = "/" + relative_path
    if dry_run:
        print(f"Would back up {device_path} if present: {backup}")
        return
    parent, name = device_path.rsplit("/", 1)
    check = "import os; print({!r} in os.listdir({!r}))".format(name, parent)
    result = subprocess.run(command(port, "exec", check), check=True, capture_output=True, text=True)
    answer = result.stdout.strip().splitlines()[-1:]
    if answer == ["False"]:
        print(f"No existing {device_path} to back up.")
        return
    if answer != ["True"]:
        raise RuntimeError("Could not verify whether {} exists on the badge".format(device_path))
    backup.parent.mkdir(parents=True, exist_ok=True)
    run(command(port, "cp", ":" + device_path, str(backup)))


def reader_only_mode(port: str | None) -> bool:
    result = subprocess.run(command(port, "exec", "import os; print('pocketreader_reader_only' in os.listdir('/data'))"),
                            check=True, capture_output=True, text=True)
    answer = result.stdout.strip().splitlines()[-1:]
    if answer not in (["True"], ["False"]):
        raise RuntimeError("Could not check the badge's app configuration")
    return answer == ["True"]


def install_sample(port: str | None, name: str, dry_run: bool) -> None:
    """Recover interrupted installs without overwriting any existing files."""
    files = (name + '.prb', name + '.pri')
    expected = hashlib.sha256((ROOT / 'books' / files[0]).read_bytes()).hexdigest()
    if dry_run:
        present = (False, False)
        print('Would install or recover sample without overwriting files:', name)
    else:
        result = subprocess.run(command(port, 'exec',
            "import os; n=os.listdir('/books'); print('{}' in n, '{}' in n)".format(*files)),
            check=True, capture_output=True, text=True)
        answer = result.stdout.strip().splitlines()[-1:]
        if not answer or answer[0] not in ('True True', 'True False', 'False True', 'False False'):
            raise RuntimeError('Could not check existing sample files')
        present = tuple(value == 'True' for value in answer[0].split())
    if present == (False, True):
        # Deleting a book can leave its rebuildable index behind. Stage the new
        # index before replacing the orphan; reading preferences are separate.
        index_path = '/books/' + files[1]
        staged = index_path + '.installing'
        backup = index_path + '.replaced'
        run(command(port, 'cp', str(ROOT / 'books' / files[1]), ':' + staged), dry_run)
        replace = '''import os
index = {!r}
staged = {!r}
backup = {!r}
try:
    os.stat(backup)
except OSError:
    pass
else:
    raise OSError('Previous index recovery is needed')
os.rename(index, backup)
try:
    os.rename(staged, index)
except OSError:
    os.rename(backup, index)
    raise
os.remove(backup)
'''.format(index_path, staged, backup)
        run(command(port, 'exec', replace), dry_run)
    if not present[0]:
        run(command(port, 'cp', str(ROOT / 'books' / files[0]), ':/books/' + files[0]), dry_run)
    # A running reader can retain the old module after its file is replaced.
    run(command(port, 'exec',
        "import sys; sys.modules.pop('lib.book_transfer', None); "
        "import lib.book_transfer as transfer; transfer.register_installed_book({!r}, {!r})".format(name, expected)), dry_run)
    if not present[1]:
        run(command(port, 'cp', str(ROOT / 'books' / files[1]), ':/books/' + files[1]), dry_run)


def install_default_image(port: str | None, dry_run: bool) -> None:
    if not dry_run:
        result = subprocess.run(command(port, 'exec',
            "import os; print('pocketreader-sleep.png' in os.listdir('/data/graphics'))"),
            check=True, capture_output=True, text=True)
        answer = result.stdout.strip().splitlines()[-1:]
        if answer == ['True']:
            result = subprocess.run(command(port, 'exec',
                "import hashlib,binascii; f=open('/data/graphics/pocketreader-sleep.png','rb'); "
                "print(binascii.hexlify(hashlib.sha256(f.read()).digest()).decode()); f.close()"),
                check=True, capture_output=True, text=True)
            digest = result.stdout.strip().splitlines()[-1:]
            if digest != [OLD_SLEEP_IMAGE_SHA256]:
                print('Keeping existing custom or updated sleep image')
                return
            print('Replacing the previous monochrome default with the color default')
        elif answer != ['False']:
            raise RuntimeError('Could not check existing sleep image')
    run(command(port, 'cp', str(SLEEP_IMAGE), ':/data/graphics/pocketreader-sleep.png'), dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="Serial device, for example /dev/ttyACM0 or COM5")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without changing the badge")
    args = parser.parse_args()

    if shutil.which("mpremote") is None and not args.dry_run:
        print("mpremote is not installed. Run: python3 -m pip install --upgrade mpremote", file=sys.stderr)
        return 2

    reader_only = reader_only_mode(args.port) if not args.dry_run else False
    if args.dry_run:
        print("Schedule app is skipped when the badge has reader-only mode enabled.")

    backups = (
        (SWITCHER_BACKUP, ":/lib/app_switcher.py"),
        (EPD_BACKUP, ":/lib/epd.py"),
    )
    for backup, device_path in backups:
        if not args.dry_run and not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            run(command(args.port, "cp", device_path, str(backup)))
        elif backup.exists():
            print(f"Keeping existing backup: {backup}")
        else:
            print(f"Would create backup: {backup}")

    for _, device_path in UPSTREAM_FILES:
        backup_optional(args.port, device_path, args.dry_run)

    run(command(args.port, "cp", str(OVERLAY / "lib" / "epd.py"), ":/lib/epd.py"), args.dry_run)
    for source, target in UPSTREAM_FILES:
        if reader_only and target == "apps/schedule.py":
            print("Keeping reader-only mode: skipping the schedule app.")
            continue
        run(command(args.port, "cp", str(OVERLAY / source), ":/" + target), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_partial.py"), ":/lib/pocketreader_partial.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_display.py"), ":/lib/pocketreader_display.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_luts.py"), ":/lib/pocketreader_luts.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_font_data.py"), ":/lib/pocketreader_font_data.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_fonts.bin"), ":/lib/pocketreader_fonts.bin"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "book_library.py"), ":/lib/book_library.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "book_transfer.py"), ":/lib/book_transfer.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_pagination.py"), ":/lib/pocketreader_pagination.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "starter_library.py"), ":/lib/starter_library.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "pocketreader_sleep.py"), ":/lib/pocketreader_sleep.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "sleep_image.py"), ":/lib/sleep_image.py"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "wifi_setup.py"), ":/lib/wifi_setup.py"), args.dry_run)
    run(command(args.port, "cp", str(ROOT / "assets" / "wordmark-doms-scribbles-preview.png"), ":/data/graphics/pocketreader-wordmark.png"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "apps" / "pocketreader.py"), ":/apps/pocketreader.py"), args.dry_run)
    install_default_image(args.port, args.dry_run)
    run(command(args.port, "cp", str(FONT_LICENSE), ":/data/pocketreader-font-license.txt"), args.dry_run)
    run(command(args.port, "cp", str(UBUNTU_LICENSE), ":/data/pocketreader-ubuntu-license.txt"), args.dry_run)
    run(command(args.port, "cp", str(TINOS_LICENSE), ":/data/pocketreader-tinos-license.txt"), args.dry_run)
    run(command(args.port, "cp", str(OVERLAY / "lib" / "app_switcher.py"), ":/lib/app_switcher.py"), args.dry_run)
    run(command(args.port, 'exec', "import os; os.mkdir('/books') if 'books' not in os.listdir('/') else None"), args.dry_run)
    for name in ('the-time-machine-43d66b728e', 'the-war-of-the-worlds-c3be57880e'):
        install_sample(args.port, name, args.dry_run)
    run(command(args.port, "reset"), args.dry_run)
    if args.dry_run:
        print("Dry run complete; the badge was not changed.")
    else:
        print("PocketReader XL Billboard prototype installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
