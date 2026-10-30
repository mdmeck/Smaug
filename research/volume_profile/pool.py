import pandas as pd, numpy as np, os
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
exec(open(SP + 'vp_full.py').read().split('# ================================================================ main')[0])
r = pd.read_pickle(SP + 'robust.pkl'); r = r[(r['bin']==0.25)&(r['mode']=='confirm')&r.fam.isin(['prev POC','prev POC ~placebo'])]
r = r.assign(placebo=r.fam.str.contains('placebo'))[['day','period','placebo','good','bad']]
c = pd.read_pickle(SP + 'confirm.pkl'); c = c[c.fam=='C1 prev POC'][['day','period','placebo','good','bad']]
a = pd.concat([r, c]); a['yr'] = pd.to_datetime(a.day.astype(str)).dt.year
a['score'] = 3*a.good - a.bad
real, plc = a[~a.placebo], a[a.placebo]
for col in ('good','score'):
    lo, hi = boot_diff(real.day.values, real[col].astype(float).values, plc.day.values, plc[col].astype(float).values, ci=95)
    print(col, 'real', round(real[col].mean(),4), 'placebo', round(plc[col].mean(),4), 'diff', round(real[col].mean()-plc[col].mean(),4), '95ci', round(lo,4), round(hi,4))
print('events/session', round(len(real)/a.day.nunique(),2), 'sessions', a.day.nunique())
print(a.groupby(['yr','placebo']).good.mean().unstack().round(3))
