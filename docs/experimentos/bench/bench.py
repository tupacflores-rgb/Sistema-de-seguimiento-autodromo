import time, threading, queue, pickle, statistics as st
import numpy as np, cv2
import multiprocessing as mp
from multiprocessing import shared_memory

H,W=720,960
def T(f,n=200):
    ts=[]
    for _ in range(n):
        t=time.perf_counter(); f(); ts.append((time.perf_counter()-t)*1e3)
    return st.median(ts), np.percentile(ts,95)

rng=np.random.default_rng(0)
# realistic-ish frame: smooth image + noise, so JPEG is realistic size
base=cv2.GaussianBlur(rng.integers(0,255,(H,W,3),dtype=np.uint8),(31,31),0)
frame=cv2.add(base, rng.integers(0,20,(H,W,3),dtype=np.uint8))
ok,jpg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,80])
print("jpeg bytes",len(jpg))

def report(name,r): print(f"{name:45s} median {r[0]:.3f} ms  p95 {r[1]:.3f} ms")

report("np.copy 960x720x3", T(lambda: frame.copy()))
report("pickle dumps+loads (protocol 5)", T(lambda: pickle.loads(pickle.dumps(frame,protocol=5))))
q=queue.Queue(maxsize=1)
def tq(): q.put(frame); q.get()
report("queue.Queue put/get (ref, no copy)", T(tq,2000))

def worker_echo(qi,qo):
    while True:
        x=qi.get()
        if x is None: break
        qo.put(x.shape)
def worker_shm(name,qi,qo):
    shm=shared_memory.SharedMemory(name=name)
    a=np.ndarray((H,W,3),np.uint8,buffer=shm.buf)
    while True:
        x=qi.get()
        if x is None: break
        s=int(a[0,0,0]); qo.put(s)
    shm.close()

def worker_cpu(n):
    img=cv2.imdecode(jpg,cv2.IMREAD_COLOR)
    return n

if __name__=="__main__":
    cv2.setNumThreads(1)
    report("cv2.imdecode 960x720 (1 thread)", T(lambda: cv2.imdecode(jpg,cv2.IMREAD_COLOR)))
    g=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    small=cv2.resize(frame,(480,360))
    report("cvtColor BGR2GRAY", T(lambda: cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)))
    report("resize to 480x360", T(lambda: cv2.resize(frame,(480,360),interpolation=cv2.INTER_AREA)))
    mog=cv2.createBackgroundSubtractorMOG2(500,16,False)
    report("MOG2.apply 960x720", T(lambda: mog.apply(frame),100))
    mog2=cv2.createBackgroundSubtractorMOG2(500,16,False)
    report("MOG2.apply 480x360", T(lambda: mog2.apply(small),100))
    ad=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),cv2.aruco.DetectorParameters())
    report("ArucoDetector.detectMarkers gray 960x720", T(lambda: ad.detectMarkers(g),50))
    Hm=np.eye(3); pts=rng.random((10,1,2)).astype(np.float32)
    report("perspectiveTransform 10 pts", T(lambda: cv2.perspectiveTransform(pts,Hm),2000))

    # GIL test: imdecode N times serial vs 2 threads
    N=300
    t=time.perf_counter()
    for _ in range(N): cv2.imdecode(jpg,cv2.IMREAD_COLOR)
    ser=time.perf_counter()-t
    def job():
        for _ in range(N//2): cv2.imdecode(jpg,cv2.IMREAD_COLOR)
    ths=[threading.Thread(target=job) for _ in range(2)]
    t=time.perf_counter(); [x.start() for x in ths]; [x.join() for x in ths]; par=time.perf_counter()-t
    print(f"GIL test imdecode x{N}: serial {ser*1e3:.0f} ms, 2 threads {par*1e3:.0f} ms, speedup {ser/par:.2f}x")
    # pure python control
    def pyjob(n):
        s=0
        for i in range(n): s+=i*i
    M=3_000_000
    t=time.perf_counter(); pyjob(M); ser=time.perf_counter()-t
    ths=[threading.Thread(target=pyjob,args=(M//2,)) for _ in range(2)]
    t=time.perf_counter(); [x.start() for x in ths]; [x.join() for x in ths]; par=time.perf_counter()-t
    print(f"GIL control pure-python: serial {ser*1e3:.0f} ms, 2 threads {par*1e3:.0f} ms, speedup {ser/par:.2f}x")

    ctx=mp.get_context("spawn")
    qi,qo=ctx.Queue(),ctx.Queue()
    t=time.perf_counter()
    p=ctx.Process(target=worker_echo,args=(qi,qo)); p.start()
    qi.put(np.zeros(1)); qo.get()
    print(f"spawn process start+first roundtrip {(time.perf_counter()-t)*1e3:.0f} ms")
    def rt(): qi.put(frame); qo.get()
    report("mp.Queue(spawn) send frame + ack", T(rt,200))
    qi.put(None); p.join()
    shm=shared_memory.SharedMemory(create=True,size=frame.nbytes)
    a=np.ndarray((H,W,3),np.uint8,buffer=shm.buf)
    p=ctx.Process(target=worker_shm,args=(shm.name,qi,qo)); p.start()
    qi.put(0); qo.get()
    def rts(): a[:]=frame; qi.put(1); qo.get()
    report("shared_memory write frame + signal + ack", T(rts,500))
    def rtj(): qi.put(jpg.tobytes()); qo.get() if False else None
    qi.put(None); p.join(); shm.close(); shm.unlink()
    p=ctx.Process(target=worker_echo,args=(qi,qo)); p.start(); qi.put(np.zeros(1)); qo.get()
    def rtjpg(): qi.put(jpg); qo.get()
    report("mp.Queue(spawn) send JPEG bytes + ack", T(rtjpg,500))
    qi.put(None); p.join()
