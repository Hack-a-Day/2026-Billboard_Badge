"""Temporary PocketReader XL access point and local Wi-Fi setup page."""

import asyncio as aio
import binascii
import json
import machine
import network
import os
import socket
import sys
import time


CONFIG_PATH = "/data/pocketreader_config.json"
HOTSPOT_KEY_PATH = "/data/pocketreader_hotspot_key.json"
WEB_KEY_PATH = "/data/pocketreader_web_key.json"
PORTAL_TIMEOUT_MS = 10 * 60 * 1000
WEB_ACTIVITY_GRACE_MS = 60 * 1000
_web_last_activity = None
_mode = "off"
_deadline = None
_generation = 0
_operations = 0
_web_error = ""
_hostname_restart_required = False
_settings_generation = 0


def _hex(value):
    return "".join("{:02X}".format(byte) for byte in value)


def credentials():
    station = network.WLAN(network.WLAN.IF_STA)
    try:
        mac = station.config("mac")
    except Exception:
        mac = machine.unique_id()
    suffix = _hex(mac)[-6:]
    try:
        with open(HOTSPOT_KEY_PATH, "r") as handle:
            password = json.load(handle).get("password", "")
    except (OSError, ValueError):
        password = ""
    if len(password) < 12:
        password = _hex(os.urandom(8))
        _write_json(HOTSPOT_KEY_PATH, {"password": password})
    return "PocketReader-Setup-" + suffix, password


def web_password():
    try:
        with open(WEB_KEY_PATH, "r") as handle:
            password = json.load(handle).get("password", "")
    except (OSError, ValueError):
        password = ""
    if len(password) < 8:
        password = _hex(os.urandom(8))
        _write_json(WEB_KEY_PATH, {"password": password})
    return password


def _read_config():
    try:
        with open(CONFIG_PATH, "r") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(value, handle)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(temporary, path)


def validate_hostname(value):
    name = str(value).strip().lower()
    if name.endswith('.local'):
        name = name[:-6]
    if not name or len(name) > 32 or name[0] == '-' or name[-1] == '-':
        raise ValueError('Use a name of 1–32 letters, numbers or hyphens.')
    if any(not ('a' <= char <= 'z' or '0' <= char <= '9' or char == '-') for char in name):
        raise ValueError('Use a name of 1–32 letters, numbers or hyphens.')
    return name


def apply_hostname():
    """Apply a saved name before starting a Wi-Fi interface, if supported."""
    name = _read_config().get('hostname', '')
    if not name:
        return False
    try:
        network.hostname(validate_hostname(name))
        return True
    except (AttributeError, OSError, ValueError) as exc:
        print('Could not set network name:', exc)
        return False


def save_hostname(value):
    global _hostname_restart_required
    name = validate_hostname(value)
    config = _read_config()
    changed = name != config.get('hostname', '')
    config['hostname'] = name
    _write_json(CONFIG_PATH, config)
    if changed:
        # Some MicroPython builds initialize mDNS only once per boot, even if
        # Wi-Fi is disconnected and reconnected with a different hostname.
        _hostname_restart_required = True
    return name, changed


def hostname_restart_required():
    return _hostname_restart_required


def saved_networks():
    config = _read_config()
    saved = [item for item in config.get("networks", [])
             if isinstance(item, dict) and item.get("ssid")]
    old_ssid = config.get("ssid", "")
    if old_ssid and not any(item.get("ssid") == old_ssid for item in saved):
        saved.insert(0, {"ssid": old_ssid, "password": config.get("wifi_password", "")})
    saved.sort(key=lambda item: item["ssid"].lower())
    return saved


def show_device_passwords():
    return _read_config().get('show_device_passwords', True) is not False


def set_show_device_passwords(show):
    global _settings_generation
    config = _read_config()
    config['show_device_passwords'] = bool(show)
    _write_json(CONFIG_PATH, config)
    _settings_generation += 1


def scan_networks():
    """Return visible network names without changing the chosen connection."""
    station = network.WLAN(network.WLAN.IF_STA)
    was_active = station.active()
    try:
        if not was_active:
            apply_hostname()
            station.active(True)
        names = set()
        for result in station.scan():
            try:
                name = result[0].decode('utf-8')
            except (UnicodeError, AttributeError, IndexError):
                continue
            # Hidden networks can still be entered manually.
            if name and len(name.encode('utf-8')) <= 32:
                names.add(name)
        return sorted(names, key=lambda name: name.lower())[:40]
    finally:
        if not was_active:
            station.active(False)


def remove_network(ssid):
    config = _read_config()
    was_selected = bool(ssid and config.get("ssid") == ssid)
    saved = [item for item in saved_networks() if item.get("ssid") != ssid]
    config["networks"] = saved
    if config.get("ssid") == ssid:
        config["ssid"] = saved[0]["ssid"] if saved else ""
        config["wifi_password"] = saved[0].get("password", "") if saved else ""
    _write_json(CONFIG_PATH, config)
    if was_selected and keep_enabled():
        turn_off()


def edit_network(values):
    original = values.get('original_ssid', '')
    ssid = values.get('ssid', '').strip()
    if not ssid or len(ssid.encode('utf-8')) > 32:
        raise ValueError('Enter a Wi-Fi name of up to 32 bytes.')
    networks = saved_networks()
    current = next((item for item in networks if item['ssid'] == original), None)
    if current is None:
        raise ValueError('Saved network not found.')
    if ssid != original and any(item['ssid'] == ssid for item in networks):
        raise ValueError('A network with that name already exists.')
    password = values.get('wifi_password', '')
    if values.get('open_network') == '1':
        password = ''
    elif not password:
        password = current.get('password', '')
    current['ssid'], current['password'] = ssid, password
    config = _read_config()
    config['networks'] = networks
    if config.get('ssid') == original:
        config['ssid'], config['wifi_password'] = ssid, password
    _write_json(CONFIG_PATH, config)


def current_mode():
    return _mode


def keep_enabled():
    return _mode in ("timed", "always")


def radio_status():
    station = network.WLAN(network.WLAN.IF_STA)
    ap = network.WLAN(network.WLAN.IF_AP)
    return (bool(station.active() or ap.active()), bool(station.isconnected()))


async def connect_saved(ssid, duration_ms, on_radio=None):
    global _mode, _deadline, _generation
    chosen = None
    for item in saved_networks():
        if item.get("ssid") == ssid:
            chosen = item
            break
    if chosen is None:
        raise ValueError("Saved network not found")
    station = network.WLAN(network.WLAN.IF_STA)
    if not station.active():
        apply_hostname()
    station.active(True)
    if on_radio:
        on_radio()
    if station.isconnected():
        try:
            connected_ssid = station.config("ssid")
        except Exception:
            connected_ssid = None
        if connected_ssid != ssid:
            station.disconnect()
    if not station.isconnected():
        station.connect(ssid, chosen.get("password", ""))
        deadline = time.ticks_add(time.ticks_ms(), 25000)
        while not station.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                station.disconnect()
                station.active(False)
                _mode, _deadline = "off", None
                raise ValueError("Wi-Fi connection timed out")
            await aio.sleep_ms(200)
    config = _read_config()
    config["networks"] = saved_networks()
    config["ssid"] = ssid
    config["wifi_password"] = chosen.get("password", "")
    config["wifi_always_on"] = duration_ms is None
    _write_json(CONFIG_PATH, config)
    _mode = "always" if duration_ms is None else "timed"
    _deadline = None if duration_ms is None else time.ticks_add(time.ticks_ms(), duration_ms)
    _generation += 1
    if duration_ms is not None:
        aio.create_task(_turn_off_later(_generation, duration_ms))
    aio.create_task(_run_network_portal(_generation))


def _record_web_activity():
    global _web_last_activity
    _web_last_activity = time.ticks_ms()


def web_activity_recent():
    return (_web_last_activity is not None
            and time.ticks_diff(time.ticks_ms(), _web_last_activity) < WEB_ACTIVITY_GRACE_MS)


async def _turn_off_later(generation, duration_ms):
    await aio.sleep_ms(duration_ms)
    while generation == _generation and _mode == "timed" and (_operations or web_activity_recent()):
        await aio.sleep_ms(1000)
    if generation == _generation and _mode == "timed":
        turn_off()


async def _restart_after_hostname_change():
    """Restart after saving enough information to restore an active connection."""
    global _web_error
    await aio.sleep_ms(500)
    while _operations:
        await aio.sleep_ms(250)
    try:
        config = _read_config()
        remaining = None if _mode == 'always' else (time.ticks_diff(_deadline, time.ticks_ms()) if _mode == 'timed' else 0)
        if keep_enabled() and (remaining is None or remaining > 0):
            station = network.WLAN(network.WLAN.IF_STA)
            ssid = station.config('ssid')
            if not ssid:
                raise ValueError('No connected network to restore')
            config['hostname_resume_wifi'] = {'ssid': ssid, 'duration_ms': remaining}
        else:
            config.pop('hostname_resume_wifi', None)
        _write_json(CONFIG_PATH, config)
        machine.reset()
    except Exception as exc:
        _web_error = 'Restart failed: ' + str(exc)


def take_hostname_resume():
    """Consume the one-time reconnect request saved before a name change."""
    config = _read_config()
    saved = config.pop('hostname_resume_wifi', None)
    if saved is None:
        return None
    _write_json(CONFIG_PATH, config)
    if not isinstance(saved, dict) or not saved.get('ssid'):
        return None
    duration = saved.get('duration_ms')
    if duration is not None and (type(duration) is not int or duration <= 0 or duration > 3600000):
        return None
    return saved['ssid'], duration


def _schedule_hostname_restart():
    aio.create_task(_restart_after_hostname_change())


def turn_off():
    global _mode, _deadline, _generation
    station = network.WLAN(network.WLAN.IF_STA)
    try:
        station.disconnect()
    except OSError:
        pass
    station.active(False)
    _mode, _deadline = "off", None
    _generation += 1
    config = _read_config()
    config["wifi_always_on"] = False
    _write_json(CONFIG_PATH, config)


def expire_if_due():
    if not _operations and not web_activity_recent() and _mode == "timed" and _deadline is not None and time.ticks_diff(time.ticks_ms(), _deadline) >= 0:
        turn_off()
        return True
    return False


def begin_operation():
    global _operations
    _operations += 1


def end_operation():
    global _operations
    _operations = max(0, _operations - 1)


def always_on_ssid():
    config = _read_config()
    return config.get("ssid", "") if config.get("wifi_always_on") else ""


def preferred_ssid():
    return _read_config().get("ssid", "")


def _decode(value):
    value = value.replace("+", " ")
    result = bytearray()
    index = 0
    while index < len(value):
        if value[index] == "%" and index + 2 < len(value):
            try:
                result.append(int(value[index + 1 : index + 3], 16))
                index += 3
                continue
            except ValueError:
                pass
        result.extend(value[index].encode())
        index += 1
    return result.decode("utf-8", "replace")


def _form_values(body):
    values = {}
    for field in body.split("&"):
        if "=" not in field:
            continue
        key, value = field.split("=", 1)
        values[_decode(key)] = _decode(value)
    return values


def _response(connection, status, content_type, body, extra_headers=""):
    encoded = body.encode()
    connection.sendall(
        "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n{}\r\n".format(
            status, content_type, len(encoded), extra_headers
        ).encode()
    )
    connection.sendall(encoded)


def _escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


def _change_web_password(values):
    global _settings_generation
    if values.get("old_password", "") != web_password():
        raise ValueError("The current web password is incorrect.")
    new_password = values.get("password", "")
    if new_password != values.get("confirm_password", ""):
        raise ValueError("The new passwords do not match.")
    if (len(new_password) < 8 or len(new_password) > 32 or ':' in new_password
            or any(ord(c) < 33 or ord(c) > 126 for c in new_password)):
        raise ValueError("Use 8 to 32 printable characters, with no spaces or colon.")
    _write_json(WEB_KEY_PATH, {"password": new_password})
    _settings_generation += 1
    return new_password


def _size_text(size):
    if size < 1024:
        return '{} B'.format(size)
    if size < 1024 * 1024:
        return '{:.1f} KB'.format(size / 1024)
    return '{:.1f} MB'.format(size / (1024 * 1024))


def settings_generation():
    return _settings_generation


def _save_reader_settings(values):
    global _settings_generation
    from lib import book_library as lib
    size = values.get('text_size', '')
    brightness = values.get('light_brightness', '')
    progress = values.get('reading_light_mode', '')
    family = values.get('font_family', lib.font_family())
    try:
        timeout = int(values.get('idle_light_minutes', ''))
        sleep_timer = int(values.get('sleep_timer_minutes', lib.sleep_timer_minutes()))
    except ValueError:
        raise ValueError('Invalid timer setting.')
    if size not in ('small', 'medium', 'large', 'xl') or family not in ('opendyslexic', 'ubuntu', 'tinos') or brightness not in ('off', 'dim', 'medium', 'bright') or progress not in ('off', 'book', 'chapter') or timeout not in (0, 1, 5, 10) or sleep_timer not in (0, 5, 10, 30):
        raise ValueError('Invalid reader setting.')
    prefs = lib._read_json(lib.PREFERENCES_PATH, {})
    if not isinstance(prefs, dict):
        prefs = {}
    prefs.update({'text_size': size, 'font_family': family, 'idle_light_minutes': timeout, 'sleep_timer_minutes': sleep_timer,
                  'light_brightness': brightness, 'availability_lights': values.get('availability_lights') == '1',
                  'reading_light_mode': progress, 'quick_boot': values.get('quick_boot') == '1'})
    lib._write_json(lib.PREFERENCES_PATH, prefs)
    _settings_generation += 1


def _select_options(options, current):
    return ''.join("<option value='{}'{}>{}</option>".format(_escape(value),
        ' selected' if str(value) == str(current) else '', _escape(label)) for value, label in options)


def _page(message="", nonce="", scanned_networks=None):
    notice = "<p class='notice'>{}</p>".format(_escape(message)) if message else ""
    config = _read_config()
    networks = "".join(
        "<li class='network'><details><summary>{}</summary><form method='post' action='/edit-network'>"
        "<input type='hidden' name='nonce' value='{}'><input type='hidden' name='original_ssid' value='{}'>"
        "<label>Wi-Fi name<input name='ssid' value='{}' maxlength='32' required></label>"
        "<label>New Wi-Fi password (leave blank to keep current)<input type='password' name='wifi_password' maxlength='128'></label>"
        "<label><input class='check' type='checkbox' name='open_network' value='1'> This is an open network</label>"
        "<button class='small' type='submit'>Save changes</button></form></details>"
        "<form method='post' action='/remove'><input type='hidden' name='nonce' value='{}'>"
        "<input type='hidden' name='ssid' value='{}'><button class='small' type='submit'>Remove</button></form></li>".format(
            _escape(item['ssid']), _escape(nonce), _escape(item['ssid']), _escape(item['ssid']), _escape(nonce), _escape(item['ssid']))
        for item in saved_networks()
    )
    scan_options = "".join("<option value='{}'>{}</option>".format(_escape(name), _escape(name))
                           for name in (scanned_networks or ()))
    scan_picker = ("<label>Nearby networks<select id='nearby-networks' "
                   "onchange=\"if(this.value)document.getElementById('wifi-name').value=this.value\">"
                   "<option value=''>Choose a network</option>{}</select></label>".format(scan_options)
                   if scanned_networks else "")
    page = """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>PocketReader XL Setup</title><style>body{font-family:monospace;background:#111;color:#eee;max-width:38rem;margin:auto;padding:2rem}label{display:block;margin:1rem 0}input,select{box-sizing:border-box;width:100%;padding:.8rem;background:#222;color:#fff;border:1px solid #777;font:inherit}button{padding:.9rem 1.3rem;background:#b11;color:#fff;border:1px solid #f44;font:inherit}.small{padding:.35rem .6rem}.network{display:flex;align-items:flex-start;gap:.75rem;margin:.5rem 0}.network details{flex:1;min-width:0}.network summary{cursor:pointer;overflow-wrap:anywhere;padding:.35rem 0}.network form{margin:0}.check{width:auto}.notice{color:#f88}.file-kind{font-weight:bold}.prb,.file-link.prb:visited{color:#8ac8ff}.pri,.file-link.pri:visited{color:#8dddaf}.prp,.file-link.prp:visited{color:#ffd27c}.other,.file-link.other:visited{color:#eee}#files li{margin:.45rem 0;overflow-wrap:anywhere}</style></head>
<body><script>
let lastInteraction=Date.now();
['pointerdown','keydown','input','change','scroll','touchstart'].forEach(event=>document.addEventListener(event,()=>{lastInteraction=Date.now()},{passive:true}));
setInterval(()=>{if(!document.hidden && Date.now()-lastInteraction<45000)fetch('/api/activity?nonce=__NONCE__',{method:'POST'}).catch(()=>{});},15000);
</script><img src='/wordmark.png' alt='PocketReader' style='max-width:80%;height:auto;background:white'><p>Add or manage Wi-Fi networks.</p>__NOTICE__
<h2>Network settings</h2>
<form method='post' action='/display-passwords'><input type='hidden' name='nonce' value='__NONCE__'>
<label><input class='check' type='checkbox' name='show_device_passwords' value='1' __SHOW_DEVICE_PASSWORDS__> Show hotspot and web passwords on the badge</label>
<p>Keep a copy of both passwords before hiding them; you will need the hotspot password to connect directly.</p>
<button class='small' type='submit'>Save password display</button></form>
<h2>Saved networks</h2><ul>__NETWORKS__</ul>
<h2>Local address</h2><form method='post' action='/hostname'>
<input type='hidden' name='nonce' value='__NONCE__'>
<label>Device name (.local)<input name='hostname' value='__HOSTNAME__' placeholder='readername' maxlength='38' required></label>
<p>Use letters, numbers and hyphens. The reader interface is available at __HOSTNAME_ADDRESS__. You can change the name there. Changing it restarts the badge and temporarily disconnects Wi-Fi.</p>
<button type='submit'>Save device name</button></form>
<form method='post' action='/scan'><input type='hidden' name='nonce' value='__NONCE__'><button type='submit'>Scan for Wi-Fi networks</button></form>
<form method='post' action='/save'>
<input type='hidden' name='nonce' value='__NONCE__'>
__NETWORK_PICKER__
<label>Wi-Fi name<input id='wifi-name' name='ssid' required maxlength='64' autocomplete='off'></label>
<p>Choose a nearby network above or enter its name, including for a hidden network.</p>
<label>Wi-Fi password<input name='wifi_password' type='password' maxlength='128'></label>
<button type='submit'>Save</button></form>"""
    page = page.replace('__NOTICE__', notice).replace('__NETWORKS__', networks).replace('__NETWORK_PICKER__', scan_picker)
    page = page.replace('__NONCE__', _escape(nonce))
    page = page.replace('__HOSTNAME__', _escape(config.get('hostname', '')))
    name = config.get('hostname', '')
    if name:
        address = 'http://' + validate_hostname(name) + '.local'
        name_address = "<a href='{}'>{}</a>".format(_escape(address), _escape(address))
    else:
        name_address = '<code>http://hostname.local</code> after a device name is set'
    page = page.replace('__HOSTNAME_ADDRESS__', name_address)
    page = page.replace('__SHOW_DEVICE_PASSWORDS__', 'checked' if show_device_passwords() else '')
    from lib import book_library as reader
    brightness, availability = reader.light_settings()
    settings = """<h2>Reader settings</h2><form method='post' action='/reader-settings'>
<input type='hidden' name='nonce' value='__NONCE__'>
<label>Text size<select name='text_size'>__TEXT_SIZE__</select></label>
<label>Font<select name='font_family'>__FONT_FAMILY__</select></label>
<label>Put reader to sleep after<select name='sleep_timer_minutes'>__DEVICE_SLEEP__</select></label>
<p>The reader can also sleep when you hold both Menu buttons for three seconds. Hold User B to wake it.</p>
<label>Turn library lights off after<select name='idle_light_minutes'>__TIMEOUT__</select></label>
<label>Light brightness<select name='light_brightness'>__BRIGHTNESS__</select></label>
<label><input class='check' type='checkbox' name='availability_lights' value='1' __AVAILABILITY__> Show availability lights</label>
<label>Use lights for reading progress<select name='reading_light_mode'>__PROGRESS__</select></label>
<label><input class='check' type='checkbox' name='quick_boot' value='1' __QUICK_BOOT__> Quick Boot (applies next launch; may leave red ghosting)</label>
<p>Font and text size apply when a book is next opened. Wi-Fi is managed in Network settings.</p>
<button type='submit'>Save reader settings</button></form>"""
    settings = settings.replace('__TEXT_SIZE__', _select_options((('small', 'Small'), ('medium', 'Medium'), ('large', 'Large'), ('xl', 'XLarge')), reader.text_size()))
    settings = settings.replace('__FONT_FAMILY__', _select_options((('opendyslexic', 'OpenDyslexic'), ('ubuntu', 'Ubuntu'), ('tinos', 'Tinos')), reader.font_family()))
    settings = settings.replace('__DEVICE_SLEEP__', _select_options(((5, '5 minutes'), (10, '10 minutes'), (30, '30 minutes'), (0, 'Never')), reader.sleep_timer_minutes()))
    settings = settings.replace('__TIMEOUT__', _select_options(((1, '1 minute'), (5, '5 minutes'), (10, '10 minutes'), (0, 'Never')), reader.idle_light_minutes()))
    settings = settings.replace('__BRIGHTNESS__', _select_options((('off', 'Off'), ('dim', 'Dim'), ('medium', 'Medium'), ('bright', 'Bright')), brightness))
    settings = settings.replace('__PROGRESS__', _select_options((('off', 'Off'), ('book', 'Book progress'), ('chapter', 'Chapter progress')), reader.reading_light_mode()))
    settings = settings.replace('__AVAILABILITY__', 'checked' if availability else '').replace('__QUICK_BOOT__', 'checked' if reader.quick_boot_enabled() else '')
    page += settings
    from lib import book_transfer
    links = ''
    for name in book_transfer.list_files():
        try:
            size = _size_text(book_transfer.file_size(name))
        except (OSError, ValueError):
            size = 'size unavailable'
        extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        kind = extension if extension in ('prb', 'pri', 'prp') else 'other'
        label = kind.upper() if kind != 'other' else 'FILE'
        links += "<li><span class='file-kind {}'>{}</span> <a class='file-link {}' href='/files/{}'>{}</a> <span>({})</span></li>".format(kind, label, kind, _escape(name), _escape(name), _escape(size))
    try:
        space = book_transfer.storage_space()
        storage = '{} used of {}; {} free'.format(_size_text(space['used']), _size_text(space['total']), _size_text(space['free']))
    except OSError:
        storage = 'Storage information unavailable'
    page += """<h2>Books and reading data</h2><p id='storage'>__STORAGE__</p><p>Select a filename to download it.</p><p><span class='file-kind prb'>PRB</span> Book &nbsp; <span class='file-kind pri'>PRI</span> Index &nbsp; <span class='file-kind prp'>PRP</span> Reading progress</p>
<ul id='files'>__FILES__</ul>
<div id='drop-zone' tabindex='0' style='border:2px dashed #aaa;padding:1rem;margin:1rem 0'>Drag book files here or choose files below.</div>
<input type='file' id='bookfile' accept='.prb,.prp,.pri' multiple>
<button type='button' id='upload-button'>Upload files</button><p id='upload-status' role='status'></p>
<script>
let selectedFiles=[], uploading=false;
const input=document.getElementById('bookfile'), zone=document.getElementById('drop-zone');
const status=document.getElementById('upload-status'), button=document.getElementById('upload-button');
function chooseFiles(files){selectedFiles=Array.from(files);status.textContent=selectedFiles.length+' file(s) selected';}
input.addEventListener('change',()=>chooseFiles(input.files));
zone.addEventListener('dragover',e=>{e.preventDefault();zone.style.borderColor='#f44'});
zone.addEventListener('dragleave',()=>{zone.style.borderColor='#aaa'});
zone.addEventListener('drop',e=>{e.preventDefault();zone.style.borderColor='#aaa';chooseFiles(e.dataTransfer.files);uploadFiles()});
button.addEventListener('click',uploadFiles);
async function uploadFiles(){
 if(!selectedFiles.length||uploading)return;
 uploading=true; button.disabled=true; input.disabled=true;
 const files=selectedFiles.slice().sort((a,b)=>(a.name.toLowerCase().endsWith('.prb')?0:1)-(b.name.toLowerCase().endsWith('.prb')?0:1));
 let messages=[];
 for(const file of files){
  status.textContent='Uploading '+file.name+' ('+(messages.length+1)+'/'+files.length+')...';
  try{
   let response=await fetch('/files/'+encodeURIComponent(file.name)+'?nonce=__NONCE__',{method:'POST',body:file});
   let message=await response.text();messages.push(file.name+': '+message);
   if(response.ok){let name=message.startsWith('Uploaded ')?message.slice(9):file.name;let li=document.createElement('li'),a=document.createElement('a'),tag=document.createElement('span');let ext=name.split('.').pop().toLowerCase(),kind=['prb','pri','prp'].includes(ext)?ext:'other';tag.className='file-kind '+kind;tag.textContent=kind==='other'?'FILE':kind.toUpperCase();li.appendChild(tag);li.appendChild(document.createTextNode(' '));a.className='file-link '+kind;a.href='/files/'+encodeURIComponent(name);a.textContent=name;li.appendChild(a);li.appendChild(document.createTextNode(' ('+file.size+' B)'));document.getElementById('files').appendChild(li)}
  }catch(error){messages.push(file.name+': Upload failed')}
 }
 try{let r=await fetch('/api/storage');if(r.ok){let s=await r.json();document.getElementById('storage').textContent=s.used_text+' used of '+s.total_text+'; '+s.free_text+' free'}}catch(error){}
 status.style.whiteSpace='pre-line';status.textContent=messages.join('\\n');selectedFiles=[];input.value='';button.disabled=false;input.disabled=false;uploading=false;
}
</script>
<h2>Sleep image</h2><p>Upload an optimized 960 × 672 PNG in badge orientation, up to 256 KB. Use black and white, or black, white, and red. With Quick Boot enabled, red appears as gray.</p>
<input type='file' id='sleep-image' accept='.png,image/png'><button type='button' id='sleep-upload'>Upload sleep image</button><p id='sleep-status' role='status'></p>
<script>document.getElementById('sleep-upload').onclick=async()=>{const f=document.getElementById('sleep-image').files[0],s=document.getElementById('sleep-status');if(!f){s.textContent='Choose a PNG image first.';return}try{s.textContent='Uploading…';let r=await fetch('/api/sleep-image?nonce=__NONCE__',{method:'POST',body:f});s.textContent=await r.text()}catch(e){s.textContent='Upload failed.'}};</script>
<h2>Web password</h2><form method='post' action='/password'><input type='hidden' name='nonce' value='__NONCE__'>
<label>Current web password<input type='password' name='old_password' autocomplete='current-password' required></label>
<label>New web password (8–32 printable characters, no spaces or colons)<input type='password' name='password' autocomplete='new-password' minlength='8' maxlength='32' required></label>
<label>Confirm new web password<input type='password' name='confirm_password' autocomplete='new-password' minlength='8' maxlength='32' required></label>
<button type='submit'>Change web password</button></form></body></html>"""
    page = page.replace('__FILES__', links).replace('__STORAGE__', storage).replace('__NONCE__', _escape(nonce))
    return page


def _write_config(values):
    global _settings_generation
    ssid = values.get("ssid", "").strip()
    if not ssid:
        raise ValueError("Enter a Wi-Fi name.")
    config = _read_config()
    existing = saved_networks()
    previous = {}
    for item in existing:
        if item.get("ssid") == ssid:
            previous = item
            break
    wifi_password = values.get("wifi_password", "") or previous.get("password", "")
    networks = [item for item in existing if item.get("ssid") != ssid]
    networks.append({"ssid": ssid, "password": wifi_password})
    config.update(
        {
            "ssid": ssid,
            "wifi_password": wifi_password,
            "networks": networks,
        }
    )
    _write_json(CONFIG_PATH, config)
    _settings_generation += 1


async def _serve(address, cancel_requested=None, timeout_ms=None, password=None):
    nonce = _hex(os.urandom(12))
    server = None
    try:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((address, 80))
        server.listen(2)
        server.setblocking(False)
        started = time.ticks_ms()
        while (timeout_ms is None or time.ticks_diff(time.ticks_ms(), started) < timeout_ms
               or web_activity_recent()):
            if cancel_requested and cancel_requested():
                return "cancelled"
            try:
                connection, _ = server.accept()
            except OSError:
                await aio.sleep_ms(50)
                continue
            authenticated_request = False
            try:
                connection.settimeout(2)
                request = connection.recv(8192)
                while b"\r\n\r\n" not in request and len(request) < 8192:
                    part = connection.recv(8192 - len(request))
                    if not part:
                        break
                    request += part
                header, _, raw_body = request.partition(b"\r\n\r\n")
                content_length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        content_length = int(line.split(b":", 1)[1].strip())
                if content_length < 0:
                    raise ValueError("Invalid content length")
                first_line = header.split(b"\r\n", 1)[0].decode("ascii", "ignore")
                if password is not None:
                    expected = ("pocketreader:" + password).encode()
                    authenticated = False
                    for line in header.split(b"\r\n"):
                        if line.lower().startswith(b"authorization: basic "):
                            try:
                                authenticated = binascii.a2b_base64(line.split(None, 2)[2]) == expected
                            except Exception:
                                pass
                    if not authenticated:
                        _response(connection, "401 Unauthorized", "text/plain", "Sign in using your PocketReader web password.",
                                  'WWW-Authenticate: Basic realm="PocketReader XL"\r\n')
                        await aio.sleep_ms(500)
                        continue
                parts = first_line.split(" ")
                method = parts[0] if parts else ""
                target = parts[1] if len(parts) > 1 else ""
                path, _, query = target.partition("?")
                if path == "/api/activity":
                    if method == "POST" and _form_values(query).get("nonce") == nonce:
                        _record_web_activity()
                        _response(connection, "200 OK", "text/plain", "OK")
                    else:
                        _response(connection, "403 Forbidden", "text/plain", "Invalid request")
                    continue
                if method in ("GET", "POST"):
                    authenticated_request = True
                    _record_web_activity()
                if method == "GET" and path == "/api/books":
                    from lib import book_transfer
                    _response(connection, "200 OK", "application/json; charset=utf-8",
                              json.dumps({"schema": 1, "books": book_transfer.list_books()}))
                    continue
                if method == "GET" and path == "/api/storage":
                    from lib import book_transfer
                    try:
                        space = book_transfer.storage_space()
                        for key in ('used', 'total', 'free'):
                            space[key + '_text'] = _size_text(space[key])
                        _response(connection, "200 OK", "application/json", json.dumps(space))
                    except OSError:
                        _response(connection, "503 Service Unavailable", "text/plain", "Storage information unavailable")
                    continue
                if method == "GET" and path == "/wordmark.png":
                    try:
                        logo = "/data/graphics/pocketreader-wordmark.png"
                        length = os.stat(logo)[6]
                        connection.sendall(("HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: {}\r\nConnection: close\r\n\r\n".format(length)).encode())
                        with open(logo, "rb") as image:
                            while True:
                                chunk = image.read(2048)
                                if not chunk:
                                    break
                                connection.sendall(chunk)
                    except OSError:
                        _response(connection, "404 Not Found", "text/plain", "Logo unavailable")
                    continue
                if method == 'POST' and path == '/api/sleep-image':
                    try:
                        if _form_values(query).get('nonce') != nonce:
                            raise ValueError('Setup page expired; reload it')
                        from lib import sleep_image
                        sleep_image.receive(content_length, raw_body, connection)
                        _response(connection, '200 OK', 'text/plain', 'Sleep image saved.')
                    except Exception as exc:
                        _response(connection, '400 Bad Request', 'text/plain', str(exc))
                    continue
                if path.startswith("/files/"):
                    from lib import book_transfer
                    name = _decode(path[len("/files/"):])
                    if method == "GET":
                        try:
                            file_path = book_transfer.download_path(name)
                            length = os.stat(file_path)[6]
                            connection.sendall(("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nContent-Disposition: attachment; filename=\"{}\"\r\nConnection: close\r\n\r\n".format(length, name)).encode())
                            with open(file_path, "rb") as source:
                                while True:
                                    chunk = source.read(2048)
                                    if not chunk:
                                        break
                                    connection.sendall(chunk)
                        except Exception as exc:
                            _response(connection, "404 Not Found", "text/plain", str(exc))
                    elif method == "POST":
                        try:
                            if _form_values(query).get("nonce") != nonce:
                                raise ValueError("Setup page expired; reload it")
                            saved = book_transfer.receive(name, content_length, raw_body, connection)
                            _response(connection, "200 OK", "text/plain", "Uploaded " + saved)
                        except Exception as exc:
                            _response(connection, "400 Bad Request", "text/plain", str(exc))
                    continue
                if content_length > 8192:
                    raise ValueError("Request is too large")
                while len(raw_body) < content_length:
                    part = connection.recv(content_length - len(raw_body))
                    if not part:
                        break
                    raw_body += part
                if method == "POST" and path == "/api/remove":
                    from lib import book_transfer
                    try:
                        values = _form_values(raw_body.decode("utf-8", "replace"))
                        if values.get("nonce") != nonce:
                            raise ValueError("Setup page expired; reload it")
                        book_transfer.remove_book(values.get("id", ""))
                        _response(connection, "200 OK", "application/json", '{"removed":true}')
                    except Exception as exc:
                        _response(connection, "400 Bad Request", "text/plain; charset=utf-8", str(exc))
                    continue
                if first_line.startswith("POST /save ") or first_line.startswith("POST /reader-settings ") or first_line.startswith("POST /display-passwords ") or first_line.startswith("POST /remove ") or first_line.startswith("POST /edit-network ") or first_line.startswith("POST /password ") or first_line.startswith("POST /scan ") or first_line.startswith("POST /hostname "):
                    try:
                        values = _form_values(raw_body.decode("utf-8", "replace"))
                        if values.get("nonce") != nonce:
                            raise ValueError("The setup page expired; reload it.")
                        if first_line.startswith("POST /hostname "):
                            name, changed = save_hostname(values.get('hostname', ''))
                            if changed:
                                if keep_enabled():
                                    message = 'Saved. The badge is restarting; when the reader opens and Wi-Fi reconnects, try http://{}.local.'.format(name)
                                else:
                                    message = 'Saved. The badge is restarting; reconnect from the reader Wi-Fi menu, then try http://{}.local.'.format(name)
                                _schedule_hostname_restart()
                            else:
                                message = 'Device name unchanged.'
                        elif first_line.startswith("POST /scan "):
                            found = scan_networks()
                            message = "Found {} network(s). Choose a Wi-Fi name below or enter one manually.".format(len(found))
                            _response(connection, "200 OK", "text/html; charset=utf-8", _page(message, nonce, found))
                            continue
                        elif first_line.startswith("POST /remove "):
                            remove_network(values.get("ssid", ""))
                            message = "Network removed."
                        elif first_line.startswith("POST /edit-network "):
                            edit_network(values)
                            message = "Network updated. Reconnect to use the changed details."
                        elif first_line.startswith("POST /reader-settings "):
                            _save_reader_settings(values)
                            message = "Reader settings saved. Font and text size apply when you next open a book."
                        elif first_line.startswith("POST /display-passwords "):
                            set_show_device_passwords(values.get('show_device_passwords') == '1')
                            message = "Password display setting saved."
                        elif first_line.startswith("POST /password "):
                            new_password = _change_web_password(values)
                            password = new_password if password is not None else None
                            message = "Web password changed."
                        else:
                            _write_config(values)
                            message = "Saved. Choose a network on the badge to connect."
                        _response(connection, "200 OK", "text/html; charset=utf-8", _page(message, nonce))
                    except Exception as exc:
                        _response(connection, "400 Bad Request", "text/html; charset=utf-8", _page(str(exc), nonce))
                else:
                    _response(connection, "200 OK", "text/html; charset=utf-8", _page("", nonce))
            except Exception as exc:
                # Keep request details and credentials out of the response,
                # while retaining the exception on the serial console.
                print_exception = getattr(sys, 'print_exception', None)
                if print_exception:
                    print_exception(exc)
                try:
                    _response(connection, "400 Bad Request", "text/plain; charset=utf-8", "Invalid request")
                except Exception:
                    pass
            finally:
                if authenticated_request:
                    _record_web_activity()
                try:
                    connection.close()
                except Exception:
                    pass
        return "timeout"
    finally:
        if server is not None:
            server.close()


async def _run_network_portal(generation):
    global _web_error
    _web_error = ""
    try:
        station = network.WLAN(network.WLAN.IF_STA)
        await _serve(station.ifconfig()[0],
                     lambda: generation != _generation or not keep_enabled(),
                     password=web_password())
    except Exception as exc:
        _web_error = str(exc)


def network_portal_status():
    station = network.WLAN(network.WLAN.IF_STA)
    if keep_enabled() and station.isconnected():
        return station.ifconfig()[0], _web_error
    return "", _web_error


def local_hostname():
    try:
        return validate_hostname(network.hostname())
    except (AttributeError, OSError, ValueError, TypeError):
        return ''


async def run_portal(cancel_requested=None, on_radio=None):
    ssid, password = credentials()
    access_point = network.WLAN(network.WLAN.IF_AP)
    apply_hostname()
    access_point.active(True)
    try:
        try:
            access_point.config(ssid=ssid, password=password, security=network.AUTH_WPA2_PSK)
        except Exception:
            access_point.config(essid=ssid, password=password, authmode=network.AUTH_WPA2_PSK)
        if on_radio:
            on_radio()
        return await _serve(access_point.ifconfig()[0], cancel_requested, PORTAL_TIMEOUT_MS)
    finally:
        access_point.active(False)
