import json, collections
r=json.load(open('aruco_results.json'))
def tab(key, jpeg, tilt):
    sizes=sorted({x['size'] for x in r}); blurs=sorted({x['blur'] for x in r})
    print(f"\n{key} | jpeg={jpeg} tilt={tilt}  (filas=lado px, cols=blur px {blurs})")
    for s in sizes:
        row=[x for x in r if x['size']==s and x['jpeg']==jpeg and x['tilt']==tilt]
        row=sorted(row,key=lambda x:x['blur'])
        print(f"{s:>3} "+" ".join(f"{x[key+'_rate']*100:5.0f}" for x in row))
for k in ('default','tuned'):
    for t in (0,45,60,70):
        tab(k,60,t)
print("\nfalse IDs total:",{k:sum(x[k+'_falseIDs'] for x in r) for k in ('default','tuned')}, "sobre", len(r)*40, "imgs")
# corner error median (blur 0, jpeg 90)
print("\nerror esquinas mediano px (blur0,jpeg90,tilt0/60):")
for s in sorted({x['size'] for x in r}):
    out=[]
    for t in (0,60):
        for k in ('default','tuned'):
            x=[y for y in r if y['size']==s and y['tilt']==t and y['blur']==0 and y['jpeg']==90][0]
            out.append(f"{k[0]}{t}:{x[k+'_cornerErr']:.2f}" if x[k+'_cornerErr'] is not None else f"{k[0]}{t}:-")
    print(s, " ".join(out))
# jpeg effect
print("\njpeg90 vs 60, tuned, tilt45, blur0/4:")
for s in (10,12,14,16,20):
    a=[ (y['jpeg'],y['blur'],y['tuned_rate']) for y in r if y['size']==s and y['tilt']==45 and y['blur'] in (0,4)]
    print(s,sorted(a))
