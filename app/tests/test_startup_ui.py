"""Actual boot requests, shell events and readiness in a bounded isolated JS harness."""
from pathlib import Path
import subprocess
import unittest


class StartupUI(unittest.TestCase):
    def test_parallel_reads_shell_dedup_and_ready_gate(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const web=process.argv[1],html=fs.readFileSync(web+'/index.html','utf8');
class Node{constructor(){this.value='';this.checked=false;this.innerHTML='';this.textContent='';this.dataset={};this.classes=new Set();this.children=[];this.events={};this.queries={};this.style={};this.classList={add:(...x)=>x.forEach(v=>this.classes.add(v)),remove:(...x)=>x.forEach(v=>this.classes.delete(v)),contains:x=>this.classes.has(x),toggle:(x,on)=>{on=on??!this.classes.has(x);on?this.classes.add(x):this.classes.delete(x);}};}addEventListener(k,f){(this.events[k]??=[]).push(f);}setAttribute(k,v){this[k]=v;}querySelector(k){return this.queries[k]??=new Node();}querySelectorAll(k){return k==='[data-goal]'?goals:[];}prepend(n){this.children.unshift(n);}appendChild(n){this.children.push(n);return n;}focus(){}blur(){}remove(){}}
const nodes=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Node()]));
nodes.memoryStatus.parentElement=new Node();
for(const [id,value]of Object.entries({optTemplate:'{stem}_{profile}_{height}p',optCrf:'',optContainer:'',optMaxEdge:'0',optScalePct:'0',optGop:'1',optAudio:'copy',optSubs:'keep',optLoudness:'0',optDefaultDir:'',optOutDir:''}))nodes[id].value=value;
const goals=['light','balanced','strong'].map(goal=>{const n=new Node();n.dataset.goal=goal;return n;});
const query=new Node(),document={getElementById:id=>nodes[id]||null,querySelector:s=>s.startsWith('input')?{value:'beside',checked:true}:query,querySelectorAll:()=>[],createElement:()=>new Node(),addEventListener:()=>{},body:new Node()};
let source=fs.readFileSync(web+'/app.js','utf8').replace(/\nwire\(\);\s*boot\(\);\s*$/,'\n');
source+=`
(async()=>{
let checks=0;const check=f=>{f();checks++;};let calls=[],pending={};
const tick=()=>new Promise(r=>setImmediate(r));
api=(p,o)=>{calls.push(p);if(['/api/caps','/api/profiles','/api/jobs','/api/capabilities','/api/settings'].includes(p))return new Promise(r=>pending[p]=r);return Promise.resolve(p==='/api/memory'?{memory:{available:1,free_mb:6000,commit_limit_mb:80000,commit_used_mb:50000},policy:{margin_mb:2500},probe_gate:{ok:true},sampled_at:Date.now()/1000}:p==='/api/version'?{version:'fixture',ffmpeg:{},limits:{gpu:1,cpu:1}}:{ok:true});};
connectEvents=()=>{};window.__vcsDesktopShell=true;await window.__vcsOnDesktopShell();check(()=>assert.equal(calls.length,0));
const starting=boot();await tick();
check(()=>{assert.deepEqual(calls,['/api/caps','/api/profiles']);assert(state.initializing);assert(nodes.pickBtn.disabled);assert(nodes.settingsBtn.disabled);assert.equal(state.caps,null);});
pending['/api/profiles']({profiles:[],default:'P3',simple_mode:true,queue_handoff:true});await tick();check(()=>assert.equal(calls.length,2));
pending['/api/caps']({caps:{available:true,encoders:{hevc_nvenc:true},hwaccels:[],ffmpeg:'fixture',version:'fixture'}});await tick();
check(()=>{for(const p of ['/api/jobs','/api/capabilities','/api/settings'])assert(calls.includes(p));assert(state.caps.available);assert(nodes.pickBtn.disabled);});
const duplicated=loadCapabilities();window.__vcsOnDesktopShell();await tick();check(()=>assert.equal(calls.filter(p=>p==='/api/capabilities').length,1));
pending['/api/jobs']({jobs:[],stats:{},restore:{held:false},on_success:{}});
pending['/api/capabilities']({native_dialogs:true,inbox_dir:'owned-fixture',hardware:{gpu:4,cpu:2,gpu_cap:8,cpu_cap:4},limits:{gpu:2,cpu:1}});
pending['/api/settings']({settings:{location:'beside',compatibility:'modern',memory_guard:true,memory_margin_mb:2500,memory_low_water_mb:1500,limits:{gpu:2,cpu:1}}});
await Promise.all([starting,duplicated]);
check(()=>{assert.equal(state.initializing,false);assert(!nodes.pickBtn.disabled);assert(!nodes.settingsBtn.disabled);assert(window.__vcsReady);assert(nodes.startBtn.disabled);assert(nodes.settingsClose.events.click.length===1);});
state.limits={gpu:3,cpu:2};await loadCapabilities();window.__vcsOnDesktopShell();await tick();
check(()=>{assert.equal(calls.filter(p=>p==='/api/capabilities').length,1);assert.deepEqual(state.limits,{gpu:3,cpu:2});assert(nodes.pickNote.textContent.includes('桌面窗口'));});
capabilitiesLoaded=false;const late=loadCapabilities();await tick();
api=async(p,o)=>({settings:{...state.settings,limits:{gpu:5,cpu:2}},limits:{gpu:5,cpu:2}});
await savePreference({limits:{gpu:5}});
pending['/api/capabilities']({native_dialogs:true,hardware:{gpu:4,cpu:2},limits:{gpu:2,cpu:1}});await late;
check(()=>assert.deepEqual(state.limits,{gpu:5,cpu:2}));
state.files=[{path:'owned.mp4',media:{}}];state.activeFile='owned.mp4';state.batchEstimate={complete:true};state.initializing=true;updateStartButton();
check(()=>{assert(nodes.startBtn.disabled);assert(nodes.previewBtn.disabled);assert(nodes.measureBtn.disabled);});
state.initializing=false;state.caps={available:true,encoders:{hevc_nvenc:true,av1_qsv:false},hwaccels:[],nativeDialogs:true,inboxDir:'owned',hardware_pending:['av1_qsv']};
renderCapsBadge(state.caps);check(()=>{assert(nodes.capsBadge.textContent.includes('补充检测中'));assert(window.__vcsReady);assert.equal(state.caps.encoders.av1_qsv,false);});
let capReads=0,releaseLate;api=(p)=>{if(p==='/api/caps'){capReads++;return new Promise(r=>releaseLate=r);}return Promise.resolve({hardware:{gpu:4,cpu:2,basis:'complete'},limits:{gpu:1,cpu:1}});};
supplementalDeadline=Date.now()+30000;nodes.setLimitCpu.value='3.5';const read=refreshSupplementalCaps();const duplicate=refreshSupplementalCaps();await tick();
check(()=>assert.equal(capReads,1));releaseLate({caps:{available:true,encoders:{hevc_nvenc:true,av1_qsv:true},hwaccels:[],hardware_pending:[],hardware_complete:true}});await Promise.all([read,duplicate]);
check(()=>{assert.deepEqual(state.limits,{gpu:5,cpu:2});assert.equal(nodes.setLimitCpu.value,'3.5');assert(state.caps.nativeDialogs);assert.equal(state.caps.inboxDir,'owned');assert(!nodes.capsBadge.textContent.includes('补充检测中'));assert.equal(state.hardware.gpu,4);});
state.caps.hardware_pending=['hevc_qsv'];renderCapsBadge(state.caps);api=async()=>{throw Error('fixture update failed');};supplementalDeadline=Date.now()+30000;await refreshSupplementalCaps();
supplementalDeadline=Date.now()-1;await refreshSupplementalCaps();check(()=>{assert(state.caps.encoders.hevc_nvenc);assert(nodes.capsBadge.title.includes('更新暂不可用'));assert(nodes.capsBadge.textContent.includes('暂未完成'));assert.deepEqual(state.limits,{gpu:5,cpu:2});});
console.log(JSON.stringify({passed:true,checks,initial_phases:2,initial_capabilities_requests:1,explicit_retry_cases:1}));
})().catch(e=>{console.error(e);process.exitCode=1;});`;
vm.runInNewContext(source,{document,nodes,window:{},location:{search:''},localStorage:{getItem:()=>null,setItem:()=>{}},navigator:{},console,process,assert,JSON,Map,Set,Date,AbortController,URLSearchParams,setImmediate,clearTimeout:()=>{},setTimeout:()=>0,setInterval:()=>0,clearInterval:()=>{}},{timeout:5000});
'''
        web=Path(__file__).resolve().parent.parent/'web'
        result=subprocess.run(['D:/nodejs/node.exe','-e',code,str(web)],capture_output=True,text=True,encoding='utf-8',timeout=12)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertIn('"passed":true',result.stdout)
        print(result.stdout.strip())


if __name__=='__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StartupUI)).wasSuccessful())
