"""Actual JS submission/confirmation behavior; bounded VM, no network or file actions."""
import json
from pathlib import Path
import subprocess
import unittest

WEB = Path(__file__).resolve().parent.parent / 'web'


class SubmissionUI(unittest.TestCase):
    def test_submission_and_consent(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),web=process.argv[1];
const html=fs.readFileSync(web+'/index.html','utf8');
let document;
class Node {
 constructor(){this.value='';this.checked=false;this.disabled=false;this.textContent='';this.innerHTML='';this.classes=new Set(['hidden']);this.events={};this.style={};this.dataset={};this.classList={add:(...xs)=>xs.forEach(x=>this.classes.add(x)),remove:(...xs)=>xs.forEach(x=>this.classes.delete(x)),contains:x=>this.classes.has(x),toggle:(x,f)=>{const on=f??!this.classes.has(x);on?this.classes.add(x):this.classes.delete(x);return on;}};}
 addEventListener(k,fn){(this.events[k]??=[]).push(fn);} removeEventListener(k,fn){this.events[k]=(this.events[k]||[]).filter(x=>x!==fn);}
 focus(){document.activeElement=this;} querySelector(){return new Node();} querySelectorAll(){return [];} setAttribute(){} appendChild(){} remove(){}
}
const nodes=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(x=>[x[1],new Node()]));
document=new Node();document.getElementById=id=>{assert(nodes[id],id);return nodes[id];};
document.createElement=()=>new Node();document.body=new Node();document.querySelector=()=>({value:'beside'});document.querySelectorAll=()=>[];
let source=fs.readFileSync(web+'/app.js','utf8').replace(/\nwire\(\);\s*boot\(\);\s*$/,'\n');
source+=`
(async()=>{
 let count=0;const check=(name,fn)=>{fn();count++;};const tick=()=>new Promise(r=>setImmediate(r));
 const make=(path,extra={})=>({path,name:path,media:{size_bytes:100,duration:1},...extra});
 const prepare=()=>{state.backendSupportsSimple=true;state.backendSupportsHandoff=true;state.mode='compress';state.submitting=false;state.batchSeq=1;state.batchEstimate={complete:true,rows:[]};state.settings={};state.files=[make('a.mp4'),make('b.mp4')];state.activeFile='a.mp4';nodes.recycleSources.checked=false;nodes.recycleSources.disabled=false;};
 let renders=0,refreshes=0,calls=[];
 renderFiles=()=>renders++;renderEstimate=()=>{};renderJobs=()=>{};refreshPlan=()=>{state.batchSeq++;state.batchEstimate=null;};refreshJobs=()=>refreshes++;
 preflightBatch=async()=>true;confirmDestructiveProfile=async()=>true;
 fileSelection=f=>({profile:f.path==='a.mp4'?'P1':'P3',overrides:{location:'beside',crf:28}});
 prepare();
 const original=state.files.slice();const pending=make('unknown.mp4',{media:null});state.files.push(pending);
 handoffSubmittedFiles(original,{jobs:[{src:'a.mp4',status:'queued'},{src:'b.mp4',status:'failed'}],errors:[]});
 check('partial handoff preserves failed and unprobed',()=>{assert.deepEqual(state.files,[original[1],pending]);assert.equal(state.activeFile,'b.mp4');assert(nodes.handoffStatus.textContent.includes('未成功'));});
 prepare();const old=state.files[0],added=make('a.mp4');state.files.push(added);
 handoffSubmittedFiles([old],{jobs:[{src:'a.mp4',status:'queued'}]});
 check('new object with same path survives',()=>assert(state.files.includes(added)&&!state.files.includes(old)));
 prepare();handoffSubmittedFiles(state.files.slice(),{jobs:[],skipped:[{path:'a.mp4'}]});
 check('explicitly skipped item leaves pending list',()=>assert.deepEqual(state.files.map(f=>f.path),['b.mp4']));
 prepare();handoffSubmittedFiles(state.files.slice(),{jobs:[]});
 check('unacknowledged items remain',()=>assert.equal(state.files.length,2));
 prepare();api=async(p,o)=>{calls.push({p,body:JSON.parse(o.body)});return {jobs:[{src:'a.mp4',status:'queued'},{src:'b.mp4',status:'queued'}]};};
 calls=[];await submitPendingFiles();
 check('one request covers different parameter selections',()=>{assert.equal(calls.length,1);assert.equal(calls[0].p,'/api/jobs/batch');assert.equal(calls[0].body.per_file['a.mp4'].profile,'P1');assert.equal(calls[0].body.per_file['b.mp4'].profile,'P3');assert.equal(calls[0].body.recycle_sources,false);assert.equal(state.files.length,0);});
 prepare();let release;calls=[];api=(p,o)=>{calls.push({p,body:JSON.parse(o.body)});return new Promise(r=>release=r);};
 const submission=submitPendingFiles();await tick();state.files.push(make('later.mp4'));
 release({jobs:[{src:'a.mp4',status:'queued'},{src:'b.mp4',status:'queued'}]});await submission;
 check('files added during request survive',()=>assert.deepEqual(state.files.map(f=>f.path),['later.mp4']));
 prepare();nodes.recycleSources.checked=true;calls=[];
 const cancelled=submitPendingFiles();await tick();
 check('second confirmation includes real count and locations and safe default focus',()=>{assert(!nodes.recycleConfirm.classList.contains('hidden'));assert(nodes.recycleConfirmSummary.textContent.includes('2'));assert(nodes.recycleConfirmFiles.innerHTML.includes('compressed'));assert.strictEqual(document.activeElement,nodes.recycleConfirmCancel);assert.equal(calls.length,0);});
 nodes.recycleConfirmCancel.onclick();await cancelled;
 check('cancel confirmation never submits',()=>{assert.equal(calls.length,0);assert.equal(state.files.length,2);assert(nodes.recycleConfirm.classList.contains('hidden'));});
 prepare();nodes.recycleSources.checked=true;calls=[];
 api=async(p,o)=>{calls.push({p,body:JSON.parse(o.body)});return {jobs:[{src:'a.mp4',status:'queued'}],errors:[{path:'b.mp4',error:'fixture'}]};};
 const confirmed=submitPendingFiles();await tick();nodes.recycleConfirmAccept.onclick();await confirmed;
 check('confirmed action is scoped and reset after partial response',()=>{assert.equal(calls.length,1);assert.equal(calls[0].body.confirm_recycle,true);assert.equal(calls[0].body.expected_count,2);assert(!nodes.recycleSources.checked);assert.deepEqual(state.files.map(f=>f.path),['b.mp4']);});
 prepare();nodes.recycleSources.checked=true;calls=[];
 const changed=submitPendingFiles();await tick();state.batchSeq++;nodes.recycleConfirmAccept.onclick();await changed;
 check('plan changed while dialog open prevents submission',()=>assert.equal(calls.length,0));
 prepare();nodes.recycleSources.checked=true;calls=[];
 const escaped=submitPendingFiles();await tick();document.events.keydown[0]({key:'Escape',preventDefault(){}});await escaped;
 check('escape cancels without side effects',()=>assert.equal(calls.length,0));
 prepare();nodes.recycleSources.checked=true;applyAfterPolicy({notify:false,power:'none',recycle_source:false});
 check('queue refresh cannot change local source-handling draft',()=>assert(nodes.recycleSources.checked));
 check('source handling is not part of global completion policy',()=>assert(!('recycle_source' in afterPolicy())));
 prepare();api=async()=>{throw Error('owned timeout');};await submitPendingFiles();
 check('unknown submission result retains pending files',()=>{assert.equal(state.files.length,2);assert(nodes.toast.textContent.includes('以免重复'));assert(!state.submitting);});
 prepare();calls=[];api=async()=>{calls.push(1);return {};};state.backendSupportsHandoff=false;await submitPendingFiles();await clearSuccessfulRecords();
 check('old backend cannot submit or use old clear semantics',()=>assert.equal(calls.length,0));
 prepare();state.jobs=[{id:'good',status:'done'},{id:'bad',status:'failed'},{id:'pending',status:'done'}];
 api=async()=>({removed:1,removed_ids:['good']});const generation=jobsFetchSeq;await clearSuccessfulRecords();
 check('clear uses server IDs and invalidates stale list response',()=>{assert.deepEqual(state.jobs.map(j=>j.id),['bad','pending']);assert(jobsFetchSeq>generation);});
 prepare();const hostile=make('<img onerror=alert(1)>.mp4');
 const prompt=confirmRecycleBatch([hostile],{[hostile.path]:{overrides:{location:'beside'}}},[]);
 check('confirmation escapes file names',()=>{assert(!nodes.recycleConfirmFiles.innerHTML.includes('<img'));assert(nodes.recycleConfirmFiles.innerHTML.includes('&lt;img'));});nodes.recycleConfirmClose.onclick();await prompt;
 console.log(JSON.stringify({passed:true,checks:count}));
})().catch(e=>{console.error(e);process.exitCode=1;});`;
vm.runInNewContext(source,{document,nodes,window:{confirm:()=>true},localStorage:{getItem:()=>null,setItem(){}},navigator:{},console,assert,process,Set,Number,JSON,Date,Math,String,setImmediate,clearTimeout(){},setTimeout:()=>0,setInterval:()=>0,clearInterval(){}},{timeout:5000});
'''
        result = subprocess.run(['D:/nodejs/node.exe', '-e', script, str(WEB)], capture_output=True,
                                text=True, encoding='utf-8', timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['passed']); self.assertGreaterEqual(data['checks'], 17)
        print('Actual submission JS checks:', data['checks'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
