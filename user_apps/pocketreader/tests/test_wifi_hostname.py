"""Hostname validation, reconnection and saved Wi-Fi timing."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/lib/wifi_setup.py'

class HostnameTest(unittest.TestCase):
    def test_automatic_wifi_deadline_extends_without_changing_manual_wifi(self):
        wifi = self.load_wifi(); now={'ms':100}; scheduled=[]
        wifi.time=types.SimpleNamespace(ticks_ms=lambda:now['ms'], ticks_add=lambda a,b:a+b,
                                        ticks_diff=lambda a,b:a-b)
        def collect(coroutine): scheduled.append(coroutine)
        wifi.aio=types.SimpleNamespace(create_task=collect)
        wifi._automatic_connection=True; wifi._mode='always'; wifi._generation=3
        self.assertTrue(wifi.extend_automatic_connection(120000))
        self.assertEqual(wifi._deadline,120100)
        now['ms']=30000
        self.assertTrue(wifi.extend_automatic_connection(120000))
        self.assertEqual(wifi._deadline,150000)
        self.assertEqual(wifi._mode,'timed')
        wifi._automatic_connection=False
        self.assertFalse(wifi.extend_automatic_connection(120000))
        self.assertEqual(wifi._deadline,150000)
        for task in scheduled: task.close()

    def test_extended_deadline_is_respected_by_original_timer(self):
        wifi=self.load_wifi(); now={'ms':0}; events=[]
        wifi.time=types.SimpleNamespace(ticks_ms=lambda:now['ms'], ticks_diff=lambda a,b:a-b)
        wifi._mode='timed'; wifi._deadline=200; wifi._generation=1
        wifi._automatic_connection=True
        wifi.turn_off=lambda remember=True:events.append((now['ms'],remember))
        async def sleep_ms(ms): now['ms']+=ms
        wifi.aio=types.SimpleNamespace(sleep_ms=sleep_ms)
        asyncio.run(wifi._turn_off_later(1,100))
        self.assertEqual(events,[(200,False)])

    def test_web_activity_delays_wifi_expiration(self):
        wifi = self.load_wifi()
        now = {'ms': 0}; events = []
        wifi.time = types.SimpleNamespace(ticks_ms=lambda: now['ms'], ticks_diff=lambda a, b: a-b)
        wifi.turn_off = lambda remember=True: events.append('off')
        wifi._mode = 'timed'; wifi._deadline = 100; wifi._generation = 1
        wifi._record_web_activity()
        now['ms'] = 100
        self.assertFalse(wifi.expire_if_due())
        async def sleep_ms(ms): now['ms'] += ms
        wifi.aio = types.SimpleNamespace(sleep_ms=sleep_ms)
        asyncio.run(wifi._turn_off_later(1, 100))
        self.assertGreaterEqual(now['ms'], wifi.WEB_ACTIVITY_GRACE_MS)
        self.assertEqual(events, ['off'])

    def test_scanned_networks_are_visible_and_selectable(self):
        wifi = self.load_wifi()
        wifi._read_config = lambda: {}
        wifi.saved_networks = lambda: []
        wifi.show_device_passwords = lambda: True
        reader = types.SimpleNamespace(light_settings=lambda: ('medium', False),
            text_size=lambda: 'medium', font_family=lambda: 'opendyslexic',
            idle_light_minutes=lambda: 5, sleep_timer_minutes=lambda: 10, reading_light_mode=lambda: 'off',
            quick_boot_enabled=lambda: False)
        transfer = types.SimpleNamespace(list_files=lambda: ['My & Book.PRB', 'book.pri', 'book.prp'],
            file_size=lambda name: 100,
            storage_space=lambda: {'used': 0, 'total': 1000, 'free': 1000})
        package = types.ModuleType('lib'); package.book_library = reader; package.book_transfer = transfer
        with patch.dict(sys.modules, {'lib':package}):
            page = wifi._page('', 'token', ['Home & Family', "Guest's <network>"])
        self.assertIn("<select id='nearby-networks'", page)
        self.assertIn("<option value='Home &amp; Family'>Home &amp; Family</option>", page)
        self.assertIn('Guest&#39;s &lt;network&gt;', page)
        self.assertIn("document.getElementById('wifi-name').value=this.value", page)
        self.assertIn("name='wifi_password'", page)
        self.assertIn("<select name='sleep_timer_minutes'>", page)
        self.assertIn("<option value='10' selected>10 minutes</option>", page)
        self.assertIn("class='file-kind prb'>PRB</span> <a class='file-link prb'", page)
        self.assertIn('My &amp; Book.PRB', page)
        self.assertIn("class='file-kind pri'>PRI</span> <a class='file-link pri'", page)
        self.assertIn("class='file-kind prp'>PRP</span> <a class='file-link prp'", page)
        self.assertIn("tag.className='file-kind '+kind", page)

    def load_wifi(self):
        with patch.dict(sys.modules, {'machine': types.ModuleType('machine'), 'network': types.ModuleType('network')}):
            spec = importlib.util.spec_from_file_location('hostname_wifi_test', SOURCE)
            wifi = importlib.util.module_from_spec(spec); spec.loader.exec_module(wifi)
        return wifi

    def test_name_validation(self):
        wifi = self.load_wifi()
        self.assertEqual(wifi.validate_hostname(' My-Reader.local '), 'my-reader')
        for invalid in ('', '-reader', 'reader-', 'reader name', 'a.b', 'x'*33):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): wifi.validate_hostname(invalid)

    def test_restart_preserves_and_consumes_remaining_duration(self):
        wifi = self.load_wifi(); events = []; state = {'hostname': 'my-reader'}
        class Station:
            def config(self, name): return 'home'
        wlan = type('WLAN', (), {'IF_STA': 0, '__new__': lambda cls, interface: Station()})
        wifi.network = types.SimpleNamespace(WLAN=wlan)
        wifi.machine = types.SimpleNamespace(reset=lambda: events.append('reset'))
        wifi.time = types.SimpleNamespace(ticks_ms=lambda: 1000, ticks_diff=lambda a, b: a-b)
        wifi._mode = 'timed'; wifi._deadline = 31000; wifi._operations = 0
        wifi._read_config = lambda: dict(state)
        def write(_path, value): state.clear(); state.update(value)
        wifi._write_json = write
        async def sleep_ms(_): pass
        wifi.aio = types.SimpleNamespace(sleep_ms=sleep_ms)
        asyncio.run(wifi._restart_after_hostname_change())
        self.assertEqual(events, ['reset'])
        self.assertEqual(wifi.take_hostname_resume(), ('home', 30000))
        self.assertIsNone(wifi.take_hostname_resume())
        self.assertEqual(state['hostname'], 'my-reader')

    def test_saved_name_is_applied_before_network_starts(self):
        wifi = self.load_wifi(); events = []
        class Station:
            enabled = False
            connected = False
            def active(self, value=None):
                if value is not None: self.enabled = value; events.append(('active', value))
                return self.enabled
            def isconnected(self): return self.connected
            def connect(self, ssid, password): self.connected = True; events.append(('connect', ssid))
        station = Station()
        wlan = type('WLAN', (), {'IF_STA': 0, '__new__': lambda cls, interface: station})
        def hostname(name):
            self.assertFalse(station.enabled)
            events.append(('hostname', name))
        wifi.network = types.SimpleNamespace(WLAN=wlan, hostname=hostname)
        wifi._read_config = lambda: {'hostname': 'my-reader'}
        wifi._write_json = lambda *_: None
        wifi.saved_networks = lambda: [{'ssid': 'home', 'password': 'secret'}]
        wifi.time = types.SimpleNamespace(ticks_ms=lambda: 100, ticks_add=lambda a,b: a+b)
        def discard_task(coroutine): coroutine.close()
        wifi.aio = types.SimpleNamespace(create_task=discard_task)
        asyncio.run(wifi.connect_saved('home', None))
        self.assertEqual(events, [('hostname', 'my-reader'), ('active', True), ('connect', 'home')])

    def test_temporary_connection_does_not_change_saved_wifi_settings(self):
        wifi = self.load_wifi(); writes = []
        class Station:
            enabled = False
            connected = False
            def active(self, value=None):
                if value is not None: self.enabled = value
                return self.enabled
            def isconnected(self): return self.connected
            def connect(self, ssid, password): self.connected = True
            def disconnect(self): self.connected = False
        station = Station()
        wlan = type('WLAN', (), {'IF_STA': 0, '__new__': lambda cls, interface: station})
        wifi.network = types.SimpleNamespace(WLAN=wlan, hostname=lambda _: None)
        wifi._read_config = lambda: {'hostname': 'my-reader', 'wifi_always_on': True}
        wifi._write_json = lambda *_: writes.append(True)
        wifi.saved_networks = lambda: [{'ssid':'home', 'password':'example'}]
        wifi.time = types.SimpleNamespace(ticks_ms=lambda:100, ticks_add=lambda a,b:a+b)
        def discard_task(coroutine): coroutine.close()
        wifi.aio = types.SimpleNamespace(create_task=discard_task)
        asyncio.run(wifi.connect_saved('home', None, remember=False))
        wifi.turn_off(remember=False)
        self.assertEqual(writes, [])
        self.assertFalse(station.enabled)

    def test_expired_timer_does_not_reconnect(self):
        wifi = self.load_wifi(); events = []; state = {'hostname': 'my-reader'}
        wifi.time = types.SimpleNamespace(ticks_ms=lambda: 1000, ticks_diff=lambda a, b: a-b)
        wifi.machine = types.SimpleNamespace(reset=lambda: events.append('reset'))
        wifi._mode = 'timed'; wifi._deadline = 900; wifi._operations = 0
        wifi._read_config = lambda: dict(state)
        def write(_path, value): state.clear(); state.update(value)
        wifi._write_json = write
        async def sleep_ms(_): pass
        wifi.aio = types.SimpleNamespace(sleep_ms=sleep_ms)
        asyncio.run(wifi._restart_after_hostname_change())
        self.assertEqual(events, ['reset'])
        self.assertIsNone(wifi.take_hostname_resume())
