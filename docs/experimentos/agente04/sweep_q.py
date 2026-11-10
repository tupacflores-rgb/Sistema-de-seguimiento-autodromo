import numpy as np, sys
from run_experiments import run_one
def ev(cfg, ov, seeds=(1,2,3)):
    rs=[run_one(cfg,s,0.15,40,ov) for s in seeds]
    return (np.mean([r['rms_pos'] for r in rs]), np.mean([r['v_rms']['kf'] for r in rs]),
            sum(r['M']['idsw_track'] for r in rs), sum(r['M']['dup'] for r in rs), sum(r['M']['missed'] for r in rs))
for q in [1e6,4e6,1.5e7,5e7,2e8]:
    print('CV q',q, ev('C2_global_CV_ArUco',dict(q_cv=q)),flush=True)
for q in [1e8,1e9,1e10,1e11]:
    print('CA q',q, ev('C3_global_CA_ArUco',dict(q_ca=q)),flush=True)
for ql,qh in [(5e5,3e7),(1e6,1e8),(2e6,3e8)]:
    print('IMM',ql,qh, ev('C4_global_IMM_ArUco',dict(q_imm=(ql,qh))),flush=True)
