from sqlalchemy import Column,Integer,String,JSON,Text,Float
from src.config.database import Base
class Attribution(Base):
 __tablename__='sla_attributions'
 id=Column(Integer,primary_key=True); name=Column(String(120),unique=True); base_run_id=Column(Integer); target_run_id=Column(Integer); status=Column(String(20)); revision=Column(Integer,default=1); result=Column(JSON); error=Column(Text); checksum=Column(String(64)); actor=Column(String(120)); created=Column(Float); updated=Column(Float)
