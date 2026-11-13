import json
import os
from pathlib import Path
import tempfile
from .subjects import subjects
from .estimate import estimate

def analyze(source,as_of,horizon,target):
    rows,excluded=subjects(source,as_of)
    report={'as_of':as_of,'excluded_future_installations':excluded,'subjects':rows,'cohorts':estimate(rows,horizon)}
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.survival-',suffix='.tmp',dir=target.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(report,f,indent=2,sort_keys=True);f.write('\n')
        os.replace(temp,target)
    finally:Path(temp).unlink(missing_ok=True)
    return report
