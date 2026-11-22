"""Shared helper: the Testing Lab browser code is split across modules."""
from pathlib import Path

JS = Path(__file__).parents[1] / 'src' / 'capstone_rag' / 'webapp' / 'static' / 'js'


def lab_js_text():
    files = [JS / 'testing_lab.js', *sorted((JS / 'lab').glob('*.js'))]
    return '\n'.join(path.read_text(encoding='utf-8') for path in files)


def compact(text):
    """Whitespace-insensitive form for source-shape assertions."""
    return ''.join(text.split())
