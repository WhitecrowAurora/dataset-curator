from urllib.parse import parse_qs,urlsplit
from .service import listing,file


def read(handler,raw):
    path=urlsplit(raw).path
    if path=='/admin/v1/result-exports':
        handler._json(200,listing(handler.store));return True
    if not path.startswith('/admin/v1/result-exports/'):return False
    eid=path.rsplit('/',1)[-1];q=parse_qs(urlsplit(raw).query,keep_blank_values=True)
    source,record=file(handler.store,eid,(q.get('token') or [None])[0])
    handler.send_response(200)
    for key,value in {'Content-Type':'application/zip','Content-Length':str(record['size']),
                      'Content-Disposition':'attachment; filename="lulynx-results-'+eid+'.zip"',
                      'X-Artifact-SHA256':record['sha256'],'Cache-Control':'no-store'}.items():handler.send_header(key,value)
    handler.end_headers();handler._responded=True
    try:
        with source.open('rb') as stream:
            while chunk:=stream.read(1024*1024):handler.wfile.write(chunk)
    except (OSError,ConnectionError):handler.close_connection=True
    return True
