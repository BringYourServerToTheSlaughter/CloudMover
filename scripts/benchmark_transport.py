"""Disposable LOCAL ONLY benchmark: simulated metadata + real Python child launches.

Run from the repository root with the test environment:
  uv run python scripts/benchmark_transport.py --files 14 --delay-ms 10
Never launches proton-drive, opens credentials, or contacts any provider.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
from threading import Lock
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from src.cloudmover.providers import proton
from test_destination_regression import FakeProton


def benchmark(files,concurrency,grouped,delay_ms):
    backend=FakeProton()
    lock=Lock()
    errors=[]
    latencies=[]
    cpu_start=resource.getrusage(resource.RUSAGE_CHILDREN)
    self_start=resource.getrusage(resource.RUSAGE_SELF)
    def process(cmd,**kw):
        # Simulate a command round trip using an actual disposable subprocess.
        subprocess.run([sys.executable,'-c','import time; time.sleep('+str(delay_ms/1000)+')'],
                       check=True,capture_output=True,timeout=10)
        with lock:
            return backend.process(cmd,**kw)
    def job(file):
        began=perf_counter()
        try:
            proton.assert_no_remote_file('/my-files/CloudMover/'+file.name)
            proton.safe_upload_promote(file,'/my-files/CloudMover',file.name)
        except Exception as exc:
            with lock:errors.append(type(exc).__name__)
        finally:
            with lock:latencies.append(perf_counter()-began)
    def observed(local,event,**data):
        if data.get('phase')=='safe_upload_total' and data.get('status')=='completed':
            latencies.append(data['duration_ms']/1000)
    began=perf_counter()
    with patch.object(proton,'cli',return_value='proton-drive'),patch.object(proton,'_run_cli_process',side_effect=process):
        if grouped:
            try:
                for parent,group in proton.transport_groups([(p,'/my-files/CloudMover',p.name) for p in files]):
                    proton.safe_upload_group(group,parent,observe_file=observed)
            except Exception as exc:errors.append(type(exc).__name__)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(job,files))
    wall=perf_counter()-began
    cpu_end=resource.getrusage(resource.RUSAGE_CHILDREN)
    self_end=resource.getrusage(resource.RUSAGE_SELF)
    child_cpu=(cpu_end.ru_utime+cpu_end.ru_stime)-(cpu_start.ru_utime+cpu_start.ru_stime)
    parent_cpu=(self_end.ru_utime+self_end.ru_stime)-(self_start.ru_utime+self_start.ru_stime)
    return dict(mode='grouped' if grouped else 'original_per_file',concurrency=concurrency,
                files=len(files),wall_seconds=round(wall,3),
                average_file_latency_seconds=round(sum(latencies)/len(latencies),3) if latencies else None,
                max_file_latency_seconds=round(max(latencies),3) if latencies else None,
                subprocess_count=len(backend.calls),failures=len(errors),timeouts=errors.count('TimeoutExpired'),
                child_cpu_seconds=round(child_cpu,3),parent_cpu_seconds=round(parent_cpu,3),
                cpu_percent_of_one_core=round((child_cpu+parent_cpu)/wall*100,1),
                simulated_provider_errors=0,real_provider_errors=None)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files',type=int,default=14)
    parser.add_argument('--delay-ms',type=float,default=10)
    args=parser.parse_args()
    if not 1<=args.files<=5000 or not 0<=args.delay_ms<=1000:parser.error('Invalid benchmark bounds')
    with tempfile.TemporaryDirectory(prefix='cloudmover-local-benchmark-') as td:
        files=[]
        for i in range(args.files):
            p=Path(td)/f"Family ({i}) - O'Brien 日本語.jpg"
            p.write_bytes(bytes([i%256])*4096);files.append(p)
        results=[benchmark(files,n,False,args.delay_ms) for n in (1,2,3)]
        results.append(benchmark(files,1,True,args.delay_ms))
    print(json.dumps({'simulation_only':True,'delay_ms_per_command':args.delay_ms,'results':results},indent=2))


if __name__=='__main__':main()
