from pydantic import BaseModel,Field,model_validator
class Login(BaseModel): email:str; password:str
class DataInput(BaseModel): data:dict
class RunInput(BaseModel): name:str=Field(min_length=2,max_length=120); policy_id:int; period:str='全部'
class ReviewInput(BaseModel): note:str=Field(min_length=4,max_length=2000)
class ScheduleInput(BaseModel): name:str=Field(min_length=2,max_length=120); run_id:int; interval_seconds:int=Field(ge=5,le=86400)
class AttributionInput(BaseModel):
 name:str=Field(min_length=2,max_length=120); base_run_id:int; target_run_id:int
 @model_validator(mode='after')
 def distinct(self):
  if self.base_run_id==self.target_run_id: raise ValueError('基准报告与目标报告不能相同')
  return self
class AttributionSelect(BaseModel):
 base_run_id:int; target_run_id:int
 @model_validator(mode='after')
 def distinct(self):
  if self.base_run_id==self.target_run_id: raise ValueError('基准报告与目标报告不能相同')
  return self
