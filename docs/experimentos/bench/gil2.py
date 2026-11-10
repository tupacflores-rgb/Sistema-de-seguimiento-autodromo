import time,threading,numpy as np,cv2
cv2.setNumThreads(1)
rng=np.random.default_rng(1)
f=[cv2.GaussianBlur(rng.integers(0,255,(360,480,3),dtype=np.uint8),(15,15),0) for _ in range(20)]
g=cv2.cvtColor(cv2.resize(f[0],(960,720)),cv2.COLOR_BGR2GRAY)
ad=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),cv2.aruco.DetectorParameters())
def mogjob(n):
    m=cv2.createBackgroundSubtractorMOG2(500,16,False)
    for i in range(n): m.apply(f[i%20])
def arjob(n):
    for _ in range(n): ad.detectMarkers(g)
for name,job,N in [("MOG2 480x360",mogjob,400),("ArUco 960x720",arjob,80)]:
    t=time.perf_counter(); job(N); s=time.perf_counter()-t
    th=[threading.Thread(target=job,args=(N//2,)) for _ in range(2)]
    t=time.perf_counter(); [x.start() for x in th]; [x.join() for x in th]; p=time.perf_counter()-t
    print(f"{name}: serial {s*1e3:.0f} ms, 2 threads {p*1e3:.0f} ms, speedup {s/p:.2f}x")
import sys; print("GIL enabled:", getattr(sys,'_is_gil_enabled',lambda:True)())
