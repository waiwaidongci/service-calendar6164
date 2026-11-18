import os,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
_tmp=tempfile.mkdtemp(prefix='attribution-tests-')
os.environ.setdefault('DATABASE_URL','sqlite:///'+os.path.join(_tmp,'test.db'))
os.environ.setdefault('REDIS_URL','redis://localhost:6379/15')
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('PROJECT_CONFIG',str(ROOT/'project.json'))
