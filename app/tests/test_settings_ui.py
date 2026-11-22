"""Actual settings JS with controlled promises: no real HTTP, video or store."""
from pathlib import Path
import subprocess
import unittest


class SettingsUI(unittest.TestCase):
    def test_actual_auto_save_serialization_failure_and_layout(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const web=process.argv[1],html=fs.readFileSync(web+'/index.html','utf8');
class Node{constructor(){this.value='';this.checked=false;this.events={};this.textContent='';this.innerHTML='';this.dataset={};this.attributes={};this.queries={};this.children=[];this.classes=new Set();this.classList={add:(...x)=>x.forEach(v=>this.classes.add(v)),remove:(...x)=>x.forEach(v=>this.classes.delete(v)),contains:x=>this.classes.has(x),toggle:(x,on)=>{on=on??!this.classes.has(x);on?this.classes.add(x):this.classes.delete(x);return on;}};}addEventListener(k,f){(this.events[k]??=[]).push(f);}setAttribute(k,v){this.attributes[k]=v;}querySelector(k){return this.queries[k]??=new Node();}querySelectorAll(){return [];}focus(){}blur(){}appendChild(n){this.children.push(n);}}
const nodes=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Node()]));
const document={getElementById:id=>nodes[id]||null,querySelector:()=>({value:'beside',checked:true}),querySelectorAll:()=>[],createElement:()=>new Node(),addEventListener:()=>{},activeElement:null};
let source=fs.readFileSync(web+'/app.js','utf8').replace(/\nwire\(\);\s*boot\(\);\s*$/,'\n');
source+=`
(async()=>{
let checks=0;const check=(f)=>{f();checks++;};
let stored={show_advanced:false,show_extreme:false,compatibility:'modern',slow_av1:false,deep_verify:false,vmaf_check:false,memory_guard:true,memory_margin_mb:2500,memory_low_water_mb:1500,default_out_dir:'',location:'beside',limits:{gpu:1,cpu:2}};
state.settings={...stored};state.limits={...stored.limits};state.jobs=[{id:'sentinel',argv:['unchanged']}];const queue=state.jobs,queueBefore=JSON.stringify(queue);
loadAbout=async()=>{};let applied=0,replans=0;applyInterface=()=>applied++;refreshPlan=()=>replans++;applyLocationUI=()=>{};toast=()=>{};
let writes=[];const response=patch=>{const limits={...stored.limits,...(patch.limits||{})};stored={...stored,...patch,limits};return {settings:JSON.parse(JSON.stringify(stored)),limits:{...limits}};};
api=async(p,o)=>{assert.equal(p,'/api/settings');if(!o)return {settings:stored};const patch=JSON.parse(o.body);writes.push(patch);return response(patch);};
wireSettings();syncSettingsPanel();
check(()=>{for(const id of ['setSaveInterface','setSaveOutDir','setLocation','setDefaultOutDir','limitGpu','limitCpu','deepVerify','vmafCheck','setCompatibility'])assert(!nodes[id],id);assert(nodes.runtimeSettingsBtn);});
nodes.setShowExtreme.checked=true;await nodes.setShowExtreme.events.change[0]();closeSettings();openSettings();
check(()=>{assert(nodes.setShowExtreme.checked);assert(stored.show_extreme);assert.equal(writes.at(-1).show_extreme,true);assert.equal(Object.keys(writes.at(-1)).length,1);});
await loadSettings();openSettings();check(()=>assert(nodes.setShowExtreme.checked));
nodes.setLimitGpu.value='2';await nodes.setLimitGpu.events.change[0]();check(()=>assert.deepEqual(writes.at(-1),{limits:{gpu:2}}));
nodes.setLimitCpu.value='3';await nodes.setLimitCpu.events.change[0]();check(()=>{assert.deepEqual(state.limits,{gpu:2,cpu:3});assert(nodes.perfSummary.textContent.includes('CPU 3'));});
nodes.setDeepVerify.checked=true;await nodes.setDeepVerify.events.change[0]();check(()=>{assert.deepEqual(writes.at(-1),{deep_verify:true});assert(nodes.verifySummary.textContent.includes('深度'));});
document.activeElement=nodes.setMemoryMargin;nodes.setMemoryMargin.value='4100';nodes.setShowAdvanced.checked=true;await nodes.setShowAdvanced.events.change[0]();check(()=>assert.equal(nodes.setMemoryMargin.value,'4100'));document.activeElement=null;
let pending=[];api=(p,o)=>new Promise(resolve=>pending.push({patch:JSON.parse(o.body),resolve}));
nodes.setShowExtreme.checked=true;const a=nodes.setShowExtreme.events.change[0]();nodes.setShowExtreme.checked=false;const b=nodes.setShowExtreme.events.change[0]();
await Promise.resolve();check(()=>assert.equal(pending.length,1));closeSettings();openSettings();check(()=>assert.equal(nodes.setShowExtreme.checked,false));
pending[0].resolve(response(pending[0].patch));await a;await Promise.resolve();check(()=>{assert.equal(pending.length,2);assert.equal(nodes.setShowExtreme.checked,false);});
pending[1].resolve(response(pending[1].patch));await b;check(()=>{assert.equal(stored.show_extreme,false);assert.equal(nodes.setShowExtreme.checked,false);assert.equal(settingsDraft.size,0);});
api=async()=>{throw new Error('injected disk denied');};nodes.setShowExtreme.checked=true;await nodes.setShowExtreme.events.change[0]();
check(()=>{assert.equal(nodes.setShowExtreme.checked,false);assert.equal(nodes.setShowExtreme.attributes['aria-invalid'],'true');assert(nodes.settingsSaveStatus.textContent.includes('未保存'));assert(!nodes.settingsRetryBtn.classList.contains('hidden'));});
closeSettings();openSettings();check(()=>{assert.equal(nodes.setShowExtreme.checked,false);assert(settingsFailures.has('show_extreme'));});
api=async(p,o)=>response(JSON.parse(o.body));await retrySettings();check(()=>{assert(stored.show_extreme);assert(nodes.setShowExtreme.checked);assert.equal(settingsFailures.size,0);});
api=async()=>({ok:true});nodes.setShowExtreme.checked=false;await nodes.setShowExtreme.events.change[0]();check(()=>{assert(nodes.setShowExtreme.checked);assert(settingsFailures.has('show_extreme'));});
api=async(p,o)=>response(JSON.parse(o.body));await retrySettings();
nodes.compatibilityChoice.value='av1';await nodes.compatibilityChoice.events.change[0]();check(()=>{assert.equal(stored.compatibility,'av1');assert(replans>0);});
nodes.slowAv1Choice.checked=true;await nodes.slowAv1Choice.events.change[0]();check(()=>assert(stored.slow_av1));
openSettings(true);check(()=>assert(nodes.settingsRuntime.open));
state.backendSupportsSimple=true;state.backendSupportsHandoff=true;state.files=[{path:'owned.mp4',media:{}}];state.activeFile='owned.mp4';state.batchEstimate={complete:true};
let releasePreference;api=(p,o)=>new Promise(r=>releasePreference=()=>r(response(JSON.parse(o.body))));
const saving=savePreference({compatibility:'modern'});check(()=>{assert(nodes.startBtn.disabled);assert(nodes.previewBtn.disabled);});await Promise.resolve();releasePreference();await saving;
check(()=>{assert.equal(nodes.startBtn.disabled,false);assert.equal(nodes.previewBtn.disabled,false);});state.files=[];
check(()=>{assert.strictEqual(state.jobs,queue);assert.equal(JSON.stringify(queue),queueBefore);assert.equal(stored.memory_margin_mb,2500);assert.equal(stored.memory_low_water_mb,1500);});
console.log(JSON.stringify({passed:true,checks}));
})().catch(e=>{console.error(e);process.exitCode=1;});`;
vm.runInNewContext(source,{document,nodes,window:{},localStorage:{getItem:()=>null},console,process,assert,JSON,Map,Set,Date,clearTimeout:()=>{},setTimeout:()=>0,html},{timeout:5000});
'''
        web = Path(__file__).resolve().parent.parent / 'web'
        result = subprocess.run(['D:/nodejs/node.exe', '-e', code, str(web)],
                                capture_output=True, text=True, encoding='utf-8', timeout=12)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('"passed":true',result.stdout)
        print(result.stdout.strip())


if __name__ == '__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SettingsUI)).wasSuccessful())
