"""No-still-only-shots gate (Travis 2026-10-04 22:21 CEST). Per shot of a mute (manifest with clips[].seg_s + dissolves),
decode at 180x320 gray, consecutive-frame |diff| (dissolve overlaps excluded). Metrics:
  mean_fd   = mean abs frame difference (0-255)
  peak_fd   = median over frames of the 99.5th-percentile |diff| (does ANY region move?)
FAIL (still-only) if peak_fd < 3.0 AND mean_fd < 0.15 ; WATCH if peak_fd < 6.0 or mean_fd < 0.25.
usage: stillgate.py mute.mp4 manifest.json out.json"""
import json,subprocess,numpy as np,sys
FAIL_PEAK,FAIL_MEAN,WATCH_PEAK,WATCH_MEAN=3.0,0.15,6.0,0.25
def run(mute,man,out=None):
    man=json.load(open(man)) if isinstance(man,str) else man
    W,H=180,320
    raw=subprocess.run(['ffmpeg','-v','error','-i',mute,'-vf',f'scale={W}:{H}','-f','rawvideo','-pix_fmt','gray','-'],capture_output=True,check=True).stdout
    F=np.frombuffer(raw,np.uint8).reshape(-1,H,W).astype(np.int16); fps=24
    diss=man.get('dissolves',{}); t=0; rows=[]
    for c in man['clips']:
        s=t; e=t+c['seg_s']; rows.append((c['id'],s,e)); t=e-diss.get(c['id'],0)
    res=[]
    for i,(sid,s,e) in enumerate(rows):
        a=s if i==0 else max(s,rows[i-1][2]); b=e if i+1==len(rows) else min(e,rows[i+1][1])
        seg=F[int(round(a*fps))+1:int(round(b*fps))-1]; d=np.abs(np.diff(seg,axis=0)).reshape(len(seg)-1,-1)
        mean=float(d.mean()); peak=float(np.median(np.percentile(d,99.5,axis=1)))
        v='FAIL' if (peak<FAIL_PEAK and mean<FAIL_MEAN) else ('WATCH' if (peak<WATCH_PEAK or mean<WATCH_MEAN) else 'PASS')
        res.append(dict(id=sid,t0=round(a,2),t1=round(b,2),mean_fd=round(mean,3),peak_fd=round(peak,2),verdict=v))
    if out: json.dump(res,open(out,'w'),indent=1)
    return res
if __name__=='__main__':
    for r in run(*sys.argv[1:4]): print(f"{r['id']:5s} {r['t0']:7.2f}-{r['t1']:7.2f} mean {r['mean_fd']:6.3f} peak {r['peak_fd']:6.2f} {r['verdict']}")
