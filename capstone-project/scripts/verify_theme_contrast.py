"""Check text contrast for the actual shared CSS tokens (WCAG AA normal text)."""
import json
from pathlib import Path
import re

root=Path(__file__).resolve().parents[1]
css=(root/'src/capstone_rag/webapp/static/css/theme.css').read_text(encoding='utf-8')
def tokens(selector):
    body=re.search(re.escape(selector)+r'\s*\{(.*?)\}',css,re.S).group(1)
    return dict(re.findall(r'(--[\w-]+):\s*([^;]+);',body))
base=tokens(':root')
def colour(value,values):
    while value.startswith('var('): value=values[value[4:-1]]
    return tuple(int(value[i:i+2],16)/255 for i in (1,3,5))
def luminance(rgb):
    c=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
    return sum(a*b for a,b in zip(c,(.2126,.7152,.0722)))
pairs=[(fg,bg) for fg in ('--text','--text-dim','--text-faint') for bg in ('--bg','--surface','--surface-2','--surface-3')]
pairs += [('--accent','--accent-soft'),('--critical','--critical-soft'),('--warning','--warning-soft'),('--ok','--ok-soft'),('--on-action','--action'),('--on-action','--action-hover')]
report=[]
for theme,values in [('light',base),('dark',{**base,**tokens(':root[data-theme="dark"]')})]:
    for fg,bg in pairs:
        a,b=sorted((luminance(colour(values[fg],values)),luminance(colour(values[bg],values))))
        ratio=(b+.05)/(a+.05)
        report.append({'theme':theme,'foreground':fg,'background':bg,'contrast':round(ratio,2),'passed':ratio>=4.5})
out=root/'docs/evidence/design_system/contrast.json'
out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
failed=[r for r in report if not r['passed']]
print(json.dumps({'pairs':len(report),'failed':failed,'output':str(out)},indent=2))
raise SystemExit(bool(failed))
