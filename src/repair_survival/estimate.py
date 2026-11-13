from collections import defaultdict
from fractions import Fraction

def estimate(rows,horizon):
    if isinstance(horizon,bool) or not isinstance(horizon,int) or horizon<=0:
        raise ValueError('horizon must be positive integer days')
    groups=defaultdict(list)
    for row in rows:groups[row['cohort']].append(row)
    curves={}
    for cohort,subjects in sorted(groups.items()):
        if horizon>max(r['duration_days'] for r in subjects):
            raise ValueError('horizon exceeds observed follow-up in '+cohort)
        times=defaultdict(lambda:[0,0])
        for r in subjects:times[r['duration_days']][0 if r['event'] else 1]+=1
        risk=len(subjects);survival=Fraction(1);curve=[];median=None;previous=0;area=Fraction(0)
        for t,(failures,censored) in sorted(times.items()):
            if previous<horizon:
                area+=survival*(min(t,horizon)-previous)
            previous=t
            before=risk
            survival*=Fraction(risk-failures,risk)
            if median is None and survival<=Fraction(1,2):median=t
            curve.append({'day':t,'at_risk':before,'failures':failures,'censored':censored,'survival':float(survival),'survival_fraction':str(survival)})
            risk-=failures+censored
        curves[cohort]={'subjects':len(subjects),'events':sum(r['event'] for r in subjects),'median_days':median,'rmst_horizon_days':horizon,'rmst_days':float(area),'curve':curve}
    return curves
