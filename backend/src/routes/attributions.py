from fastapi import APIRouter,Depends,HTTPException
from src.config.database import database
from src.controllers.auth import current,editor
from src.models.attribution import Attribution
from src.schemas.requests import AttributionInput,AttributionSelect
from src.services.attribution import listing,open_review,retry_review
from src.utils.serialize import row
router=APIRouter()
@router.get('/attributions')
def attributions(db=Depends(database),u=Depends(current)):
 return listing(db)
@router.get('/attributions/{id}')
def detail(id:int,db=Depends(database),u=Depends(current)):
 obj=db.get(Attribution,id)
 if not obj: raise HTTPException(404,'复盘不存在')
 return row(obj)
@router.post('/attributions')
def create(b:AttributionInput,db=Depends(database),u=Depends(editor)):
 return row(open_review(db,u,b))
@router.put('/attributions/{id}/selection')
def reselect(id:int,b:AttributionSelect,db=Depends(database),u=Depends(editor)):
 obj=db.get(Attribution,id)
 if not obj: raise HTTPException(404,'复盘不存在')
 payload=AttributionInput(name=obj.name,base_run_id=b.base_run_id,target_run_id=b.target_run_id)
 return row(open_review(db,u,payload))
@router.post('/attributions/{id}/retry')
def retry(id:int,db=Depends(database),u=Depends(editor)):
 return row(retry_review(db,u,id))
