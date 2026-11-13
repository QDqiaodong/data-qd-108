import argparse
import json
from .report import analyze

def main():
    p=argparse.ArgumentParser(description='Compute cohort Kaplan-Meier survival and restricted mean lifetime')
    p.add_argument('--source',required=True);p.add_argument('--as-of',required=True)
    p.add_argument('--horizon',required=True,type=int);p.add_argument('--output',required=True)
    a=p.parse_args()
    try:r=analyze(a.source,a.as_of,a.horizon,a.output)
    except (ValueError,OSError) as e:p.exit(2,str(e)+'\n')
    print(json.dumps({'cohorts':{k:{key:v for key,v in c.items() if key!='curve'} for k,c in r['cohorts'].items()}},sort_keys=True))
if __name__=='__main__':main()
