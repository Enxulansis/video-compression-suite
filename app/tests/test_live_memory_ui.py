"""Actual front-end resource recovery logic, no browser/media or real network."""
from pathlib import Path
import subprocess
import unittest


class MemoryUI(unittest.TestCase):
    def test_polling_bounds_retry_and_preserves_measured_results(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={};const node=id=>nodes[id]??={textContent:'',innerHTML:'',title:'',classList:{add(){},remove(){},toggle(){}},parentElement:{classList:{add(){},toggle(){}}},querySelectorAll:()=>[]};
let src=fs.readFileSync(process.argv[1],'utf8').replace(/\nwire\(\);\s*boot\(\);\s*$/,'\n');
src+=`
(async()=>{
let checks=0;const check=(fn)=>{fn();checks++;};
let reads=0,resolve;const snapshot={memory:{available:1,free_mb:4700,commit_limit_mb:84000,commit_used_mb:67000},policy:{margin_mb:2500},probe_gate:{ok:true},sampled_at:Date.now()/1000};
api=()=>{reads++;return new Promise(r=>resolve=r);};
const a=refreshMemory({retry:false}),b=refreshMemory({retry:false});
check(()=>assert.equal(reads,1));resolve(snapshot);await Promise.all([a,b]);
check(()=>assert(node('memoryStatus').textContent.includes('4.6 GB')));
const actualProbe=probeFiles;const actualRefresh=refreshPlan;
let retried=[],finishProbe;probeFiles=async(es,opts)=>{retried=es;assert.equal(opts.checkSeek,false);await new Promise(r=>finishProbe=r);};
state.files=Array.from({length:6},(_,i)=>({path:String(i),retryable:true}));
state.files[0].probing=true;state.files[1].retryable=false;api=async()=>snapshot;
await refreshMemory();check(()=>assert.deepEqual(retried.map(f=>f.path),['2','3']));finishProbe();
retried=[];api=async()=>({...snapshot,probe_gate:{ok:false}});await refreshMemory();check(()=>assert.equal(retried.length,0));
probeFiles=actualProbe;renderFiles=()=>{};updateStartButton=()=>{};refreshPlan=()=>{};maybeWarnBroken=()=>{};
const f={path:'owned.mp4',name:'owned.mp4'};state.files=[f];
api=async p=>{if(p==='/api/memory')return snapshot;const e=new Error('waiting');e.retryable=true;throw e;};
await probeFiles([f],{checkSeek:false});check(()=>{assert(f.retryable);assert.equal(f.error,'');assert(f.notice.includes('waiting'));assert.equal(f.probing,false);});
let deletedResolve;api=p=>p==='/api/memory'?Promise.resolve(snapshot):new Promise(r=>deletedResolve=r);
const pending=probeFiles([f],{checkSeek:false});state.files=[];deletedResolve({items:[{ok:true,path:f.path}]});await pending;await refreshMemory({retry:false});
check(()=>assert.equal(f.media,undefined));
state.files=[];api=async()=>{throw new Error('read unavailable');};await refreshMemory();check(()=>assert(node('memoryStatus').textContent.includes('读取失败')));
const sentinel={complete:true,rows:[]};state.files=[{path:'owned.mp4'}];state.batchEstimate=sentinel;state.batchSeq=7;state.batchDisplayMode='measured';
batchBody=()=>({paths:['owned.mp4']});api=async()=>({files:1,complete:true,rows:[{warnings:['new RAM warning']}],totals:{est_bytes:99}});
node('batchTotals').innerHTML='real sample estimate';await loadBatchEstimate({quiet:true});
check(()=>{assert.strictEqual(state.batchEstimate,sentinel);assert.equal(node('batchTotals').innerHTML,'real sample estimate');assert(node('planDecisions').textContent.includes('new RAM warning'));});
api=async()=>{throw new Error('poll timeout');};state.batchDisplayMode=null;await loadBatchEstimate({quiet:true});check(()=>assert.strictEqual(state.batchEstimate,sentinel));
state.files=[];api=(p,opts)=>new Promise((res,rej)=>opts.signal.addEventListener('abort',()=>rej(new Error('deadline'))));
const bounded=refreshMemory();window.lastTimer();await bounded;
check(()=>assert(node('memoryStatus').textContent.includes('读取失败')));
api=async()=>snapshot;await refreshMemory();check(()=>assert(node('memoryStatus').textContent.includes('4.6 GB')));
api=async()=>({...snapshot,memory:{available:1,free_mb:0,commit_limit_mb:84000,commit_used_mb:84000},probe_gate:{ok:false}});
await refreshMemory();check(()=>{assert(node('memoryStatus').textContent.includes('可用内存 0 MB'));assert(node('memoryStatus').textContent.includes('提交余量 0 MB'));});
console.log(JSON.stringify({passed:true,checks}));
})().catch(e=>{console.error(e);process.exitCode=1;});`;
const win={};vm.runInNewContext(src,{document:{getElementById:node,querySelector:()=>null},window:win,console,process,assert,node,AbortController,Date,Set,Map,clearTimeout:()=>{},setTimeout:fn=>{win.lastTimer=fn;return 0;}},{timeout:4000});
'''
        web = Path(__file__).resolve().parent.parent / 'web' / 'app.js'
        result = subprocess.run(['D:/nodejs/node.exe', '-e', script, str(web)],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"passed":true', result.stdout)
        print(result.stdout.strip())


if __name__ == '__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MemoryUI)).wasSuccessful())
