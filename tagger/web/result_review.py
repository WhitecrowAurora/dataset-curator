from fastapi import APIRouter,HTTPException,Query
from fastapi.responses import FileResponse,Response
from ..result_review import store,service,images

router=APIRouter(prefix='/api/result-review')


def call(fn,*args):
    try:return fn(*args)
    except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from exc


@router.get('')
def sessions():return dict(items=store.sessions())


@router.post('')
def import_results(body:dict):return call(service.import_result,body.get('source',''),body.get('repo',''))


@router.get('/{sid}')
def state(sid:str):return call(store.view,sid)


@router.post('/{sid}/index')
def index(sid:str,body:dict):return call(service.import_index,sid,body)


@router.post('/{sid}/download')
def download(sid:str,body:dict):return call(service.download,sid,body)


@router.post('/{sid}/prepare')
def prepare(sid:str):return call(service.prepare_packages,sid)


@router.post('/{sid}/cancel')
def cancel(sid:str):
    if sid in service.CANCEL:service.CANCEL[sid].set()
    return dict(ok=True)


@router.get('/{sid}/records')
def records(sid:str,repo:str,pkg:str,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50)):
    return call(service.records,sid,repo,pkg,offset,limit)


@router.get('/{sid}/image')
def image(sid:str,repo:str,pkg:str,image_id:str):
    return Response(call(images.thumbnail,sid,repo,pkg,image_id),media_type='image/jpeg',headers={'Cache-Control':'private, max-age=300'})


@router.post('/{sid}/decision')
def decision(sid:str,body:dict):return call(service.decide,sid,body)


@router.get('/{sid}/export')
def export(sid:str):return FileResponse(call(service.export,sid),media_type='application/x-ndjson',filename='lulynx-review-decisions.jsonl')
