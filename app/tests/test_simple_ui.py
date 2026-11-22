"""Actual page structure and JS behavior in an isolated lightweight DOM harness.

This does not claim pixel/browser verification. Node and HTTP have bounded timeouts.
"""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest

WEB = Path(__file__).resolve().parent.parent / 'web'


class Page(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids = []; self.stack = []; self.parents = {}; self.sections = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id']); self.parents[attrs['id']] = list(self.stack)
        if tag == 'section': self.sections.append(attrs.get('class', ''))
        if tag not in {'input', 'meta', 'link', 'br', 'img', 'hr', 'source'}:
            self.stack.append((tag, attrs.get('class', '')))
    def handle_endtag(self, tag):
        if tag in [t[0] for t in self.stack]:
            while self.stack:
                if self.stack.pop()[0] == tag: break


class ConsoleUI(unittest.TestCase):
    def test_page_ids_unique_and_queue_separate(self):
        page = Page(); page.feed((WEB / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual(len(page.ids), len(set(page.ids)))
        self.assertIn(('section', 'panel queue-panel'), page.parents['jobList'])
        self.assertNotIn(('section', 'panel console-panel'), page.parents['jobList'])
        self.assertNotIn(('details', 'advanced hidden'), page.parents['optOutDir'])
        self.assertIn(('section', 'panel console-panel'), page.parents['batchTotals'])

    def test_actual_javascript_helpers_settings_and_mode(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const web = process.argv[1];
const html = fs.readFileSync(web + '/index.html', 'utf8');
class Node {
  constructor() { this.value=''; this.checked=false; this.textContent=''; this.innerHTML=''; this.children=[]; this.events={}; this.queries={}; this.classes=new Set(); this.dataset={}; this.style={};
    this.classList={add:(...xs)=>xs.forEach(x=>this.classes.add(x)),remove:(...xs)=>xs.forEach(x=>this.classes.delete(x)),contains:x=>this.classes.has(x),toggle:(x,force)=>{const on=force??!this.classes.has(x);on?this.classes.add(x):this.classes.delete(x);return on;}};
  }
  appendChild(n){this.children.push(n);return n;} addEventListener(k,fn){(this.events[k]??=[]).push(fn);}
  set value(v){this._value=String(v);} get value(){return this._value;}
  setAttribute(k,v){this[k]=v;} querySelector(k){return this.queries[k]??=new Node();}
  querySelectorAll(k){return k==='[data-goal]' ? goals : [];}
  focus(){} remove(){} blur(){}
}
const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(x=>[x[1],new Node()]));
for (const [id,value] of Object.entries({optCrf:'',optTemplate:'{stem}_{profile}_{height}p',optMaxEdge:'0',optScalePct:'0',optGop:'1',optAudio:'copy',optSubs:'keep',optContainer:'',optQualityMode:'crf',optLoudness:'0',optDefaultDir:'',optOutDir:''})) nodes[id].value=value;
nodes.optKeepTime.checked=true;
const goals=['light','balanced','strong'].map(goal=>{const n=new Node();n.dataset.goal=goal;return n;});
const document={getElementById:id=>{if(id.endsWith('SaveError'))return null;assert(nodes[id],`missing ID ${id}`);return nodes[id];}, createElement:()=>new Node(),querySelector:()=>({value:'beside',checked:true}),querySelectorAll:()=>[],addEventListener:()=>{},body:new Node()};
let source=fs.readFileSync(web+'/app.js','utf8').replace(/\nwire\(\);\s*boot\(\);\s*$/, '\n');
source+=`
(async () => {
let checks=0;const check=(label,fn)=>{fn();checks++;};
state.backendSupportsSimple=true;state.backendSupportsHandoff=true;
state.profiles=['P1','P2','P3','P4','P5','P6','P7','P8','A0','A1','A2','A3','A4','A5','A6'].map(id=>({id,name:id,name_en:'',summary:'',compat:4,speed:'',available:true}));
check('unknown metrics',()=>{for(const args of [[0,10],[10,0],[10,NaN],[10,Infinity],[10,20,10],[-1,10]]) assert.equal(sizeBenefit(...args),null);});
check('inverse range',()=>assert.equal(sizeBenefit(1000,100,200).multiple,'5.00–10.00×'));
check('growth',()=>assert(sizeBenefit(100,200,300).change.includes('增加')));
check('mixed range',()=>assert(sizeBenefit(100,50,150).change.includes('可能增加')));
check('actual multiple',()=>assert.equal(sizeBenefit(100,25).multiple,'4.00×'));
check('zero saving is known',()=>assert.equal(sizeBenefit(100,100).change,'节省 0% · 0 B'));
check('invalid duration',()=>{assert.equal(fmtDuration(Infinity),'未知');assert.equal(fmtDuration(NaN),'未知');});
check('known duration',()=>{assert.equal(fmtDuration(450),'7:30');assert.equal(fmtDuration(3661),'1:01:01');});
check('video bitrate',()=>assert.equal(bitrateText({video:{bit_rate:42300000}}),'视频码率 42.30 Mbps'));
check('total bitrate label',()=>assert(bitrateText({bit_rate:1000000}).startsWith('总码率 ')));
check('estimated bitrate label',()=>assert(bitrateText({size_bytes:1000000,duration:10}).startsWith('总码率估算')));
check('unknown bitrate',()=>assert.equal(bitrateText({duration:NaN,size_bytes:100}),'码率未知'));
state.files=[{media:{duration:450}},{media:{duration:0}}];
check('partial duration says unknown',()=>assert(batchDurationText().includes('1 项时长未知')));state.files=[];
check('missing SSE uses polling',()=>assert.doesNotThrow(()=>connectEvents()));
check('old backend cannot submit new parameters',()=>{state.backendSupportsSimple=false;state.files=[{media:{duration:1}}];state.batchEstimate={complete:true};updateStartButton();assert(nodes.startBtn.disabled);state.backendSupportsSimple=true;state.backendSupportsHandoff=true;state.files=[];});
check('default groups hidden',()=>assert.equal(profileOptions('').length,0));
check('current hidden pinned',()=>assert.equal(profileOptions('A6')[0].id,'A6'));
check('pinned disabled',()=>assert(profileOptionHtml('A6').includes('disabled')));
state.settings.show_advanced=true;
check('nine advanced',()=>assert.equal(profileOptions('').length,9));
state.settings.show_advanced=false;state.settings.show_extreme=true;
check('five independent extreme',()=>assert.equal(profileOptions('').length,5));
check('remux excluded',()=>assert(!profileOptions('').some(p=>p.id==='P8')));
state.settings.show_advanced=true;
check('fourteen manual',()=>assert.equal(profileOptions('').length,14));
state.settings={};applyInterface();
check('only three goals visible',()=>{assert(!nodes.simpleGoals.classList.contains('hidden'));assert(nodes.profileList.classList.contains('hidden'));assert(nodes.advanced.classList.contains('hidden'));});
check('simple payload',()=>{nodes.optHdr.checked=true;nodes.expertMode.checked=true;nodes.optMaxEdge.value='640';const ov=overrides();assert.equal(ov.intent.goal,'balanced');assert.equal(ov.intent.compatibility,'modern');assert.equal(ov.hdr_to_sdr,undefined);assert.equal(ov.allow_unsafe,undefined);});
check('per file manual bypasses auto',()=>{const f={override:{profile:'A2',crf:40}};assert.deepEqual(fileSelection(f).overrides.intent,{});assert.equal(fileSelection(f).profile,'A2');assert.equal(fileSelection(f).overrides.crf,40);});
state.batchEstimate={complete:true,rows:[]};state.plan={profile_id:'P3',intent:{goal:'balanced'},resolved_overrides:{crf:28,preset:'p5',ten_bit:true,container:'mkv',audio_mode:'copy',loudness_measured:{},intent:{}}};
state.settings.show_advanced=true;switchSelectionMode();
check('manual keeps resolved codec quality depth',()=>{assert.equal(state.selectionMode,'manual');assert.equal(overrides().crf,28);assert.equal(overrides().preset,'p5');assert.equal(overrides().ten_bit,true);assert.equal(overrides().container,'mkv');assert.equal(overrides().loudness_measured,undefined);assert.equal(overrides().allow_unsafe,false);});
check('seed follows new output folder',()=>{nodes.optDefaultDir.value='E:/new-output';const selection=fileSelection({seed:{default_out_dir:'E:/old-output',crf:28},seedProfile:'P3'});assert.equal(selection.overrides.default_out_dir,'E:/new-output');nodes.optDefaultDir.value='';});
check('quality edit keeps per-file HDR and container',()=>{const f={seed:{crf:28,ten_bit:true,container:'mkv',preset:'p5'},seedProfile:'P3'};state.files=[f];nodes.optCrf.value='32';syncManualSeeds(['crf']);assert.equal(f.seed.crf,32);assert.equal(f.seed.ten_bit,true);assert.equal(f.seed.container,'mkv');assert.equal(fileSelection(f).overrides.ten_bit,true);nodes.optCrf.value='28';state.files=[];});
state.settings.show_advanced=false;applyInterface();
check('hidden current does not reset',()=>{assert.equal(state.selectionMode,'manual');assert.equal(state.profileId,'P3');assert.equal(overrides().crf,28);});
switchSelectionMode();
check('return simple',()=>{assert.equal(state.selectionMode,'simple');assert.equal(overrides().intent.goal,'balanced');});
const queue=[{id:'sentinel',argv:['unchanged'],profile_id:'A6'}];state.jobs=queue;wireSettings();
api=async(path,opts)=>{assert.equal(path,'/api/settings');const patch=JSON.parse(opts.body);assert(!('memory_margin_mb' in patch));state.settings={...state.settings,...patch};return {settings:state.settings};};
nodes.setShowExtreme.checked=true;nodes.setShowAdvanced.checked=false;nodes.compatibilityChoice.value='modern';nodes.slowAv1Choice.checked=false;
await nodes.setShowExtreme.events.change[0]();
check('settings leave queue untouched',()=>{assert.strictEqual(state.jobs,queue);assert.deepEqual(state.jobs[0].argv,['unchanged']);assert.equal(state.settings.show_extreme,true);});
state.files=[];state.batchSeq=10;state.batchEstimate={complete:true};nodes.batchTotals.innerHTML='old number';markBatchStale();
check('stale values removed',()=>{assert.equal(state.batchEstimate,null);assert(!nodes.batchTotals.innerHTML.includes('old number'));assert(nodes.startBtn.disabled);});
state.files=[];state.batchEstimate={complete:true};
renderBatchTotals({files:1,complete:true,totals:{src_bytes:1000,est_low:100,est_high:200,est_bytes:150,est_seconds:20},rows:[]},'coefficient');
check('benefit hero',()=>assert(nodes.batchTotals.innerHTML.includes('5.00–10.00×')));
renderBatchTotals({files:1,complete:false,totals:{src_bytes:1000,est_low:100,est_high:200},blocked:[{path:'hdr.mkv',error:'HDR conflict'}],rows:[]},'coefficient');
check('blocked whole benefit suppressed',()=>{assert(!nodes.batchTotals.innerHTML.includes('5.00–10.00×'));assert(nodes.planDecisions.textContent.includes('HDR conflict'));});
state.files=['a.mp4','b.mp4'].map(path=>({path,name:path,media:{duration:10,size_bytes:1000,summary:{resolution:'320x180',codec:'H264',fps:24,audio_tracks:0,subtitle_tracks:0}}}));
state.batchEstimate={complete:true,rows:state.files.map(f=>({path:f.path,name:f.name,src_bytes:1000,encoder:'hevc_nvenc',container:'mp4',resolved_overrides:{}}))};
state.batchCancelled=true;
api=async()=>({estimate:{mid:100,low:80,high:120,est_seconds:2}});
await measureBatch();
check('partial samples not whole batch return',()=>{assert(nodes.batchTotals.innerHTML.includes('仅测算了部分文件'));assert(!nodes.batchTotals.innerHTML.includes('预计倍率'));});
state.files=[];
console.log(JSON.stringify({checks,passed:true}));
})().catch(err=>{console.error(err);process.exitCode=1;});`;
vm.runInNewContext(source,{document,nodes,window:{},localStorage:{getItem:()=>null,setItem:()=>{}},navigator:{},console,assert,process,Set,Number,JSON,Date,Math,String,clearTimeout:()=>{},setTimeout:()=>0,setInterval:()=>0,clearInterval:()=>{}},{timeout:5000});
'''
        result = subprocess.run(['D:/nodejs/node.exe', '-e', script, str(WEB)],
                                capture_output=True, text=True, encoding='utf-8', timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['passed']); self.assertGreaterEqual(data['checks'], 28)
        print('Actual JS isolated DOM checks:', data['checks'])


if __name__ == '__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ConsoleUI)).wasSuccessful())
