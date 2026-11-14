"""Remember the reader screen only for a deep-sleep wake."""
import json
import machine
import os

PATH = '/data/pocketreader_sleep_resume.json'
SESSION_PATH = '/data/pocketreader_open_book.json'
_first_launch = True


def save_open_book(book_id):
    temporary = SESSION_PATH + '.tmp'
    previous = SESSION_PATH + '.previous'
    try:
        os.stat(SESSION_PATH)
        current_exists = True
    except OSError:
        current_exists = False
    if not current_exists:
        try:
            os.stat(previous)
        except OSError:
            pass
        else:
            os.rename(previous, SESSION_PATH)
    with open(temporary, 'w') as output:
        json.dump({'version': 1, 'book_id': book_id}, output)
    try:
        os.remove(previous)
    except OSError:
        pass
    try:
        os.stat(SESSION_PATH)
        had_previous = True
    except OSError:
        had_previous = False
    if had_previous:
        os.rename(SESSION_PATH, previous)
    try:
        os.rename(temporary, SESSION_PATH)
    except OSError:
        if had_previous:
            os.rename(previous, SESSION_PATH)
        raise
    if had_previous:
        os.remove(previous)


def clear_open_book():
    for path in (SESSION_PATH, SESSION_PATH + '.previous', SESSION_PATH + '.tmp'):
        try:
            os.remove(path)
        except OSError:
            pass


def take_first_launch():
    global _first_launch
    if not _first_launch:
        return None
    _first_launch = False
    for path in (SESSION_PATH, SESSION_PATH + '.previous'):
        try:
            with open(path, 'r') as source:
                value = json.load(source)
            if isinstance(value, dict) and value.get('version') == 1 and isinstance(value.get('book_id'), str):
                return value
        except (OSError, ValueError):
            pass
    return None


def save(book_id, library_page, selected, bottom_choice=0):
    value = {'version': 1, 'book_id': book_id,
             'library_page': int(library_page), 'selected': int(selected),
             'bottom_choice': int(bottom_choice)}
    temporary = PATH + '.tmp'
    with open(temporary, 'w') as output:
        json.dump(value, output)
    try:
        os.remove(PATH)
    except OSError:
        pass
    os.rename(temporary, PATH)


def peek():
    if machine.reset_cause() != machine.DEEPSLEEP_RESET:
        return None
    try:
        with open(PATH, 'r') as source:
            value = json.load(source)
        return value if isinstance(value, dict) and value.get('version') == 1 else None
    except (OSError, ValueError):
        return None


def take():
    value = peek()
    try:
        os.remove(PATH)
    except OSError:
        pass
    return value


def confirm_wake_hold():
    """Require a sustained User B press after a PocketReader deep-sleep wake."""
    import esp32
    import time

    pin = machine.Pin(16, machine.Pin.IN, machine.Pin.PULL_UP)
    started = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), started) < 2000:
        if pin.value():
            # Do not re-arm a low-level wake while the button is bouncing.
            stable = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), stable) < 100:
                time.sleep_ms(20)
                if not pin.value():
                    stable = time.ticks_ms()
            esp32.wake_on_ext0(pin, esp32.WAKEUP_ALL_LOW)
            machine.deepsleep()
            return False
        time.sleep_ms(20)
    return True
