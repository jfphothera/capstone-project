"""Low-footprint authenticated QMS/ERP lab service (stdlib only)."""
import argparse,json,mimetypes,os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--kind',choices=['qms','erp'],required=True);p.add_argument('--data-root',required=True);p.add_argument('--host',required=True);p.add_argument('--port',type=int,required=True);p.add_argument('--token-file',required=True);a=p.parse_args();root=Path(a.data_root);token=Path(a.token_file).read_text(encoding='utf-8').strip()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,format,*args):pass
        def send_json(self,status,value):
            data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path=='/api/health':return self.send_json(200,{'ok':True,'service':a.kind,'format':'capstone-business-service/1'})
            if self.headers.get('Authorization')!='Bearer '+token:return self.send_json(401,{'detail':'Bearer token required'})
            try:
                if a.kind=='erp' and self.path.startswith('/api/erp/'):
                    name=self.path.rsplit('/',1)[-1]
                    if name not in {'customers','devices','orders','shipments'}:raise FileNotFoundError(name)
                    return self.send_json(200,{'rows':json.loads((root/f'{name}.json').read_text(encoding='utf-8'))})
                if a.kind=='qms' and self.path=='/api/qms/files':
                    return self.send_json(200,{'documents':[{'name':x.name,'content':x.read_text(encoding='utf-8')} for x in sorted(root.glob('*.md'))]})
                if a.kind=='qms' and self.path=='/api/qms/compatibility':
                    return self.send_json(200,json.loads((root/'qms_compatibility.json').read_text(encoding='utf-8')))
                return self.send_json(404,{'detail':'not found'})
            except FileNotFoundError:return self.send_json(404,{'detail':'not found'})
    ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=='__main__':main()
