import csv
from datetime import date
from pathlib import Path

def day(value):
    d=date.fromisoformat(value)
    if d.isoformat()!=value:raise ValueError('expected YYYY-MM-DD')
    return d

def subjects(path,as_of):
    cutoff=day(as_of)
    with Path(path).open(newline='',encoding='utf-8') as f:
        reader=csv.DictReader(f)
        if reader.fieldnames!=['asset_id','cohort','installed','failed']:raise ValueError('unexpected columns')
        rows=list(reader)
    seen=set(); result=[];excluded=[]
    for r in rows:
        if None in r or any(v is None for v in r.values()):raise ValueError('ragged CSV')
        if any(not r[k].strip() for k in ('asset_id','cohort','installed')):raise ValueError('empty required field')
        if r['asset_id'] in seen:raise ValueError('duplicate asset')
        seen.add(r['asset_id']); start=day(r['installed']);end=day(r['failed']) if r['failed'] else None
        if end is not None and end<start:raise ValueError('failure precedes installation')
        if start>cutoff:excluded.append(r['asset_id']);continue
        event=end is not None and end<=cutoff
        result.append({'asset_id':r['asset_id'],'cohort':r['cohort'],'duration_days':((end if event else cutoff)-start).days,'event':event})
    if not result:raise ValueError('no observed subjects')
    return sorted(result,key=lambda r:r['asset_id']),sorted(excluded)
