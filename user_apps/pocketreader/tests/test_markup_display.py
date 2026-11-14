"""The badge draws supported book-format styles using the right font and alignment."""
import ast
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'
tree = ast.parse(SOURCE.read_text())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_draw_detail')

class MarkupDisplay(unittest.TestCase):
    def test_headings_and_centered_lines_use_expected_fonts(self):
        class Font:
            def __init__(self, width): self.width=width
            def measure(self,text): return len(text)*self.width
        normal, large=Font(10),Font(15)
        families={'tinos':{'medium':(normal,32,24),'large':(large,40,19)}}
        class Canvas:
            def __init__(self): self.calls=[]
            def text(self,*args): self.calls.append(args)
            def fill_rect(self,*args): pass
        scope={'READER_FAMILIES':families,'PORTRAIT_WIDTH':672,'BLACK':1,'RED':2,'HACK16':normal,
               'book_library':types.SimpleNamespace(text_size=lambda:'medium')}
        code=ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App',bases=[],keywords=[],body=[method],decorator_list=[])],type_ignores=[]))
        exec(compile(code,str(SOURCE),'exec'),scope);app=scope['App']()
        app.reader=types.SimpleNamespace(entry={'title':'Book'},header={'schema':4},size='medium',font_family='tinos',
            pages=[1],read_page=lambda page:(['\x1fHBig title','','\x1fCCentered','\x1fLLeft heading','','Body'],0))
        app.current_page=0;app._bookmarks=[];app._chapters=[];app.update_status=''
        app._draw_navigation_hints=app._draw_battery=app._draw_bottom_hints=lambda *args,**kwargs:None
        canvas=Canvas();app._draw_detail(canvas)
        title=next(item for item in canvas.calls if item[0]=='Big title')
        centered=next(item for item in canvas.calls if item[0]=='Centered')
        left=next(item for item in canvas.calls if item[0]=='Left heading')
        body=next(item for item in canvas.calls if item[0]=='Body')
        self.assertIs(title[-1],large);self.assertEqual(title[1],(672-large.measure('Big title'))//2)
        self.assertIs(centered[-1],normal);self.assertEqual(centered[1],(672-normal.measure('Centered'))//2)
        self.assertIs(left[-1],large);self.assertEqual(left[1],32)
        self.assertIs(body[-1],normal);self.assertEqual(body[1],32)

if __name__=='__main__': unittest.main()
