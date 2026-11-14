"""The optional starter catalog coexists with local books and Wi-Fi controls."""
import ast
from pathlib import Path
import types
import unittest

PATH=Path(__file__).resolve().parents[1]/'overlay/apps/pocketreader.py'
tree=ast.parse(PATH.read_text())
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PocketReaderApp')

def load(names,scope):
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
    code=ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[]))
    exec(compile(code,str(PATH),'exec'),scope);return scope['App']

class StarterUI(unittest.TestCase):
    def test_available_books_merge_and_disable_keeps_local(self):
        state={'enabled':True};local=[{'id':'local','title':'Local','package':'local.prb'}]
        starter=types.SimpleNamespace(load_catalog=lambda: [{'id':'pg1','title':'Public','author':'Author'}] if state['enabled'] else [])
        reader=types.SimpleNamespace(has_download=lambda entry: False,local_books=lambda: local,sort_books=lambda entries:sorted(entries,key=lambda e:e['title']))
        app=load({'_set_catalog','_show_library_page'},{'book_library':reader,'starter_library':starter,'REFRESH_ROW':8})()
        app.library_page=0;app.selected=0;app._set_catalog({'books':[]})
        self.assertEqual([x['id'] for x in app.all_books],['local','pg1'])
        self.assertTrue(app.all_books[1]['_starter']);self.assertEqual(app.all_books[1]['package'],'pg1.prb')
        state['enabled']=False;app._set_catalog({'books':[]});self.assertEqual([x['id'] for x in app.all_books],['local'])

    def test_toggle_is_distinct_from_wifi_and_does_not_auto_connect(self):
        state={'enabled':False};starter=types.SimpleNamespace(enabled=lambda:state['enabled'],set_enabled=lambda v:state.update(enabled=v))
        app=load({'_wifi_main_items','_select_wifi_item'},{'starter_library':starter})()
        app._wifi_networks=[{'ssid':'Home'}];app._wifi_view='main';app._wifi_cursor=6
        app._show_wifi_led=lambda:None;app._load_cached_catalog=lambda:None;app._catalog_pending=False
        self.assertEqual(app._wifi_main_items()[7][0],'login')
        app._select_wifi_item();self.assertTrue(state['enabled']);self.assertTrue(app._catalog_pending)
        app._select_wifi_item();self.assertFalse(state['enabled'])

    def test_cloud_icon_for_remote_and_book_icon_for_download(self):
        app=load({'_draw_book_state_icon'}, {'BLACK':1})()
        class Canvas:
            def __init__(self): self.rectangles=[]; self.fills=[]
            def rect(self,*args): self.rectangles.append(args)
            def fill_rect(self,*args): self.fills.append(args)
        remote=Canvas(); local=Canvas()
        app._draw_book_state_icon(remote,0,0,False)
        app._draw_book_state_icon(local,0,0,True)
        self.assertGreater(len(remote.fills),5)
        self.assertEqual(remote.rectangles,[])
        self.assertEqual(len(local.rectangles),1)

    def test_wifi_menu_responds_while_connection_or_catalog_runs(self):
        wifi=types.SimpleNamespace(settings_generation=lambda:0,expire_if_due=lambda:None)
        transfer=types.SimpleNamespace(generation=lambda:0)
        clock=types.SimpleNamespace(ticks_ms=lambda:123)
        App=load({'run'}, {'wifi_setup':wifi, 'book_transfer':transfer, 'time':clock})
        for active in ('_wifi_task','_catalog_task'):
            app=App(); app.launched=False; app.detail=False
            app._transfer_generation=app._web_settings_generation=0
            app._exit_pending=app._setup_pending=app._sleep_pending=app._sleep_requested=False
            app._download_task=app._wifi_task=app._catalog_task=None
            setattr(app,active,object())
            app._update_library_idle_lights=lambda:None
            app._handle_menu_sleep_chord=lambda:False
            app._update_device_sleep_timer=lambda:False
            app._catalog_pending=False;app._wifi_menu=True
            calls=[];app._run_wifi_menu=lambda:calls.append('responded')
            app.run()
            self.assertEqual(calls,['responded'])

    def test_existing_wifi_connection_is_not_restarted(self):
        app=load({'_queue_wifi_connection'}, {})()
        task=object(); app._wifi_task=task
        app._queue_wifi_connection('Another network',None)
        self.assertIs(app._wifi_task,task)
        self.assertEqual(app.update_status,'WIFI CONNECTING')

if __name__=='__main__':unittest.main()
