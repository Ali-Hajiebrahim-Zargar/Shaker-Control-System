const fs=require('fs'),vm=require('vm'),assert=require('assert');
const path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../shaker_csv_viewer.py'),'utf8');
const template=source.match(/HTML_TEMPLATE = r'''([\s\S]*?)'''/)[1];
const script=template.match(/<script>\n'use strict';([\s\S]*?)<\/script>/)[1];
class Element{
 constructor(id){this.id=id;this.value='';this.checked=true;this.disabled=false;this.hidden=false;this.children=[];this.listeners={};this.textContent='';this._selected=0;}
 addEventListener(e,fn){this.listeners[e]=fn;}
 setAttribute(name,value){this[name]=value;}
 appendChild(c){this.children.push(c);if(this.id==='record'||this.id==='channel'){if(this.children.length===1)this.value=c.value;}return c;}
 replaceChildren(...a){this.children=a;this._selected=0;this.value='';}
 get options(){return this.children;}
 get selectedIndex(){return this.children.findIndex(x=>x.value===this.value);}
 set selectedIndex(i){this._selected=i;this.value=this.children[i]?.value||'';}
 click(){return this.listeners.click?.();}
 remove(){}
}
const els={};for(const m of template.matchAll(/id="([^"]+)"/g))els[m[1]]=new Element(m[1]);
const n=4001,t=Array.from({length:n},(_,i)=>i/200), y=t.map(t=>3+2*Math.sin(2*Math.PI*2*t)+.2*Math.sin(2*Math.PI*35*t));
const rec=(label,unit='N')=>({label,t,y,unit,peakDistance:.08,medianDt:.005,note:'Test',states:null,capture:null,channel:null});
const doc={file:'test.csv',format:'Force / combined project CSV',scope:false,warnings:[],records:[rec('Force'),rec('Position','counts')]};
els['period-tolerance'].value='10';els['noise-threshold'].value='5';els['applied-frequency'].value='';els['viewer-data'].textContent=JSON.stringify(doc);els.filter.value='none';els.drag.value='zoom';els['measure-trace'].value='1';
const gd=els.plot;gd.events={};gd.on=(name,fn)=>gd.events[name]=fn;
function assignLayout(gd,updates){for(const [k,v]of Object.entries(updates)){if(k==='xaxis.range[0]')gd.layout.xaxis.range[0]=v;else if(k==='xaxis.range[1]')gd.layout.xaxis.range[1]=v;else if(k.includes('.')){const[a,b]=k.split('.');gd.layout[a][b]=v;}else gd.layout[k]=v;}gd._fullLayout=gd.layout;}
const Plotly={
 async newPlot(gd,traces,layout){gd.data=traces;gd.layout=layout;if(!layout.yaxis.range)layout.yaxis.range=[0,6];gd._fullLayout=layout;return gd;},
 async react(gd,traces,layout){return this.newPlot(gd,traces,layout);},
 async restyle(gd,update,indices){for(let k=0;k<indices.length;k++)for(const [name,value]of Object.entries(update))gd.data[indices[k]][name]=Array.isArray(value)?value[k%value.length]:value;return gd;},
 async relayout(gd,update){assignLayout(gd,update);gd.events.plotly_relayout?.(update);return gd;}
};
const context=vm.createContext({document:{getElementById:id=>els[id],createElement:tag=>new Element(tag),body:new Element('body')},Plotly,console,setTimeout,clearTimeout,Blob,URL});
vm.runInContext(script,context);
const run=s=>vm.runInContext(s,context),pause=()=>new Promise(r=>setTimeout(r,100));
(async()=>{
 await pause();assert.equal(gd.data.length,6);assert.equal(els.stats.children.length,2);assert(!els.notice.textContent);
 const pure=t.map(t=>3+2*Math.sin(2*Math.PI*2*t));context.testT=t;context.testY=pure;
 const m=run('metrics(testT,testY,.08,.35,.005)');assert(Math.abs(m.frequency-2)<1e-9);assert(Math.abs(m.avgMax-5)<1e-9);assert(Math.abs(m.avgMin-1)<1e-9);assert.equal(m.maxIdx.length,40);
 console.log('PASS: known 2 Hz sine frequency and 5/1 N extrema');
 assert(Number.isNaN(run('metrics([0,1,2],[7,7,7],0,.35,1).frequency')));
 assert.equal(run('metrics([0,1,2],[7,7,7],0,.35,1).rms'),7);
 assert(Number.isNaN(run('metrics([],[],0,.35,1).frequency')));
 assert(Number.isNaN(run('metrics([0,1,2,3,4,5,20,21,22,23,24,25],[0,1,2,1,0,0,0,1,2,1,0,0],0,0,1).frequency')));
 console.log('PASS: constant/empty windows and gap exclusion');
 els.filter.value='lowpass';els.cut1.value='5';await els['apply-filter'].click();
 assert(els['filter-status'].textContent.includes('low-pass'));assert.notDeepEqual(gd.data[0].y,gd.data[1].y);
 const lp=run('lowPass(testY,testT,5,.005)');assert.equal(lp[0],pure[0]);assert(lp.every(Number.isFinite));
 const hp=run('highPass(testY,testT,1,.005)');assert.equal(hp[0],0);assert(hp.every(Number.isFinite));
 els.cut1.value='100';await els['apply-filter'].click();assert(els.notice.textContent.includes('Nyquist'));assert(els['filter-status'].textContent.includes('low-pass'));
 els.filter.value='bandpass';els.cut1.value='8';els.cut2.value='4';await els['apply-filter'].click();assert(els.notice.textContent.includes('Upper cutoff'));
 els.cut1.value='1';els.cut2.value='10';await els['apply-filter'].click();assert(els['filter-status'].textContent.includes('band-pass'));assert(!els.notice.textContent);
 console.log('PASS: low/high/band filtering and cutoff validation');
 els['x-start'].value='2';els['x-end'].value='5';await els['apply-x'].click();assert(els.visible.textContent.includes('601 samples'));
 await Plotly.relayout(gd,{'xaxis.range':[5,7]});await pause();assert(els.visible.textContent.includes('401 samples'));
 await Plotly.relayout(gd,{'xaxis.range[0]':6,'xaxis.range[1]':7});await pause();assert(els.visible.textContent.includes('201 samples'));
 await Plotly.relayout(gd,{'xaxis.autorange':true});await pause();assert(els.visible.textContent.includes('4001 samples'));
 console.log('PASS: manual X, slider/array range, box range, autorange refresh');
 els['y-center'].value='3';els['y-span'].value='8';await els['apply-y'].click();assert.deepEqual(gd.layout.yaxis.range,[-1,7]);
 await els['reset-y'].click();assert(gd.layout.yaxis.range[0]<1 && gd.layout.yaxis.range[1]>5);
 els['show-filtered'].checked=false;await els['show-filtered'].listeners.change();assert.equal(gd.data[1].visible,false);assert.equal(gd.data[2].visible,false);assert.equal(gd.data[3].visible,false);
 els['show-filtered'].checked=true;await els['show-filtered'].listeners.change();
 console.log('PASS: Y controls and marker visibility');
 await els['reset-filter'].click();gd.events.plotly_click({points:[{curveNumber:0,x:1,y:3}]});gd.events.plotly_click({points:[{curveNumber:0,x:1.5,y:3}]});
 assert(els.measurement.textContent.includes('Δt = 0.5 s'));assert(els.measurement.textContent.includes('1/Δt = 2 Hz'));
 gd.events.plotly_click({points:[{curveNumber:0,x:2,y:3}]});assert(els.measurement.textContent.includes('Click the second point.'));
 els.filter.value='highpass';els.cut1.value='1';await els['apply-filter'].click();assert.equal(gd.data[4].x.length,0);
 console.log('PASS: two cursors, reciprocal period, third click, clear on refilter');
 els.record.value='1';await els.record.listeners.change();assert.equal(gd.layout.yaxis.title.text,'counts');assert(els['filter-status'].textContent.startsWith('Applied: None'));assert.equal(gd.data[4].x.length,0);
 await els.export.click();assert(!els.notice.textContent);
 console.log('PASS: switching signals resets filter/cursors and CSV export runs');
 // Inject a large record to exercise extrema without Math.min(...largeArray).
 run('DOC.records[0].t=Array.from({length:150000},(_,i)=>i/200);DOC.records[0].y=DOC.records[0].t.map(v=>Math.sin(v));');els.record.value='0';await els.record.listeners.change();assert(els.visible.textContent.includes('150000 samples'));assert(Number.isFinite(gd.layout.yaxis.range[0]));
 console.log('PASS: 150,000-sample metrics and Y fit');
 // Replace records with independent captures and verify channel navigation.
 run("DOC.scope=true;DOC.records=[{...record,label:'CH1 capture 1',channel:'CH1',capture:1},{...record,label:'CH2 capture 2',channel:'CH2',capture:2}];");
 els.channel.value='CH2';await els.channel.listeners.change();assert.equal(els.record.options.length,1);assert.equal(run('record.channel'),'CH2');assert.equal(els.previous.disabled,true);assert.equal(els.next.disabled,true);
 console.log('PASS: channel filtering and capture selector');

 // Synthetic asymmetric, quantized waveform with several upper-shoulder maxima.
 const fsample=1000, tt=Array.from({length:12000},(_,i)=>i/fsample);
 const knots=[[0,-1],[.1,1],[.13,.82],[.17,1.08],[.22,.89],[.30,1],[.42,-1],[.9,-1],[1,-1]];
 const yy=tt.map(t=>{const p=t%1;let j=1;while(j<knots.length-1&&knots[j][0]<p)j++;const [a,va]=knots[j-1],[b,vb]=knots[j];return va+(vb-va)*(p-a)/(b-a);});
 context.shapeT=tt;context.shapeY=yy;
 const guided=run('detectPeaks(shapeT,shapeY,.01,.35,.001,{frequency:1,tolerance:.1,noise:.05})');
 assert(guided.maxIdx.length>=10);assert.equal(guided.maxIdx.length,guided.minIdx.length);
 assert(guided.minIdx.every(i=>yy[i]<-.99));assert(guided.maxIdx.every(i=>yy[i]>1.07));
 assert(guided.intervals.every(p=>Math.abs(p.duration-1)<1e-9));
 assert.equal(new Set(guided.maxIdx.map(i=>Math.floor(tt[i]))).size,guided.maxIdx.length);
 assert.equal(new Set(guided.minIdx.map(i=>Math.floor(tt[i]))).size,guided.minIdx.length);
 assert(guided.minIdx.every(i=>(tt[i]%1)>.55&&(tt[i]%1)<.8));
 console.log('PASS: one max/min per asymmetric cycle; rejects upper shoulders; centers flat minima');
 // Independent measured frequency, tighter tolerance and wrong sub/harmonic input.
 context.driftY=tt.map(t=>Math.sin(2*Math.PI*2.08*t));
 const tolerant=run('detectPeaks(shapeT,driftY,.01,.35,.001,{frequency:2,tolerance:.1,noise:.05})');
 assert(tolerant.intervals.length>20);
 const detectedHz=1/(tolerant.intervals.reduce((s,p)=>s+p.duration,0)/tolerant.intervals.length);
 assert(Math.abs(detectedHz-2.08)<.0001);assert(Math.abs(detectedHz-2)>.07);
 assert.equal(run('detectPeaks(shapeT,driftY,.01,.35,.001,{frequency:2,tolerance:.02,noise:.05}).intervals.length'),0);
 for(const freq of [.5,2])assert.equal(run(`detectPeaks(shapeT,shapeY,.01,.35,.001,{frequency:${freq},tolerance:.1,noise:.05}).intervals.length`),0);
 console.log('PASS: measured 2.08 Hz is not forced to 2 Hz; tolerance and wrong-frequency rejection');
 // Constant signals and capture edges do not manufacture a cycle.
 assert.equal(run('detectPeaks([0,.1,.2,.3],[1,1,1,1],0,.35,.1,{frequency:1,tolerance:.1,noise:.05}).intervals.length'),0);
 assert.equal(run('detectPeaks(shapeT.slice(0,700),shapeY.slice(0,700),0,.35,.001,{frequency:1,tolerance:.1,noise:.05}).intervals.length'),0);
 const tg=tt.filter(t=>t<5||t>=6), yg=tg.map(t=>Math.sin(2*Math.PI*t));context.gapT=tg;context.gapY=yg;
 const gdetection=run('detectPeaks(gapT,gapY,0,.35,.001,{frequency:1,tolerance:.1,noise:.05})');
 assert(gdetection.intervals.length>5);assert(gdetection.intervals.every(p=>!(p.a<5&&p.b>6)));
 console.log('PASS: no invented peaks for constant/partial captures or across data gaps');
 // Inject the shoulder waveform into a selectable record and use the real UI handlers.
 run("DOC.scope=false;DOC.records=[{...record,label:'Shoulder test',t:shapeT,y:shapeY,medianDt:.001,peakDistance:.08,unit:'V',capture:null,channel:null}];recordOptions();");
 els.record.value='0';await els.record.listeners.change();
 els['applied-frequency'].value='1';els['period-tolerance'].value='10';els['noise-threshold'].value='5';await els['apply-peaks'].click();
 assert(!els.notice.textContent);assert(els.distance.disabled);assert(els.threshold.disabled);assert(!els['period-tolerance'].disabled);
 assert(els['peak-note'].textContent.includes('Guided by 1 Hz'));assert(gd.data[3].y.every(v=>v<-.99));
 const allMinX=gd.data[3].x.slice();els['x-start'].value='3.25';els['x-end'].value='6.4';await els['apply-x'].click();
 assert.deepEqual(Array.from(gd.data[3].x),Array.from(allMinX.filter(v=>v>=3.25&&v<=6.4)));
 console.log('PASS: applied-frequency controls and stable detected peaks while zooming');
 // Scroll, arrows, and wheel preserve the Y span and X window.
 els['y-center'].value='0';els['y-span'].value='2';await els['apply-y'].click();
 const beforeX=gd.layout.xaxis.range.slice(),beforeMarkers=gd.data[3].x.slice();
 await els['y-up'].click();assert(Math.abs(gd.layout.yaxis.range[0]+.8)<1e-9);assert(Math.abs(gd.layout.yaxis.range[1]-1.2)<1e-9);
 await els['y-down'].click();assert(Math.abs(gd.layout.yaxis.range[0]+1)<1e-9);
 els['y-scroll'].value='800';await els['y-scroll'].listeners.input();
 assert(Math.abs(gd.layout.yaxis.range[1]-gd.layout.yaxis.range[0]-2)<1e-9);assert(Number(els['y-center'].value)>0);
 const beforeCenter=Number(els['y-center'].value);let wheelPrevented=false;
 els['y-scroll'].listeners.wheel({deltaY:-100,preventDefault(){wheelPrevented=true;}});await pause();
 assert(wheelPrevented);assert(Math.abs(Number(els['y-center'].value)-beforeCenter-.2)<1e-6);
 assert.deepEqual(gd.layout.xaxis.range,beforeX);assert.deepEqual(gd.data[3].x,beforeMarkers);
 await els['y-fit'].click();assert(gd.layout.yaxis.range[0]<-1);assert(gd.layout.yaxis.range[1]>1);
 console.log('PASS: vertical slider/arrows/wheel move only Y, preserve span, update center, and Fit works');
 // Validation and return to automatic mode.
 els['period-tolerance'].value='80';await els['apply-peaks'].click();assert(els.notice.textContent.includes('between 0 and 45'));
 els['period-tolerance'].value='10';els['noise-threshold'].value='-1';await els['apply-peaks'].click();assert(els.notice.textContent.includes('Noise threshold'));
 els['noise-threshold'].value='5';els['applied-frequency'].value='0';await els['apply-peaks'].click();assert(!els.notice.textContent);assert(!els.distance.disabled);assert(els['period-tolerance'].disabled);
 console.log('PASS: frequency/tolerance/noise input validation and automatic-mode restoration');


 // Scope path: voltage scale, negative waveform time, captures and channels.
 run("DOC.scope=true;DOC.records=[0,1,2].map(i=>({...record,label:'Scope '+i,t:testT.map(v=>v-5),y:testT.map(v=>.003+.02*Math.sin(2*Math.PI*2*(v-5))+.001*Math.sin(2*Math.PI*30*(v-5))),unit:'V',medianDt:.005,peakDistance:.02,capture:i+1,channel:i===2?'CH2':'CH1'}));");
 els.channel.value='All channels';run('recordOptions();');els.record.value='0';await els.record.listeners.change();
 els['applied-frequency'].value='2';await els['apply-peaks'].click();
 assert(!els.notice.textContent);assert.equal(gd.layout.yaxis.title.text,'V');assert(gd.data[3].x.length>20);
 const scopeCount=gd.data[3].x.length;const scopeHz=Number(els.stats.children[1].children[1].textContent);assert(Math.abs(scopeHz-2)<.01);
 els['applied-frequency'].value='10';await els['apply-peaks'].click();assert.equal(gd.data[3].x.length,0);assert(els['peak-note'].textContent.includes('No complete cycles match'));
 els['applied-frequency'].value='2';await els['apply-peaks'].click();await els.next.click();assert.equal(gd.data[3].x.length,scopeCount);
 els.channel.value='CH2';await els.channel.listeners.change();assert.equal(gd.data[3].x.length,scopeCount);assert(els['peak-note'].textContent.includes('Guided by 2 Hz'));
 console.log('PASS: scope voltage/time path, applied-frequency response and guidance across captures/channels');

 // Reported failure: a 1 Hz guide with only 2.5 saved samples per second.
 // Vary sample phase and amplitude units; markers must remain actual samples,
 // Frequency now uses measured intervals even when sampling is sparse.
 const sparseT=Array.from({length:251},(_,i)=>i*.4-10);
 context.sparseT=sparseT;
 for(const scale of [1,.003])for(const phase of [0,.2,.7,1.3,2.1]){
  const values=sparseT.map(v=>-.006+scale*Math.sin(2*Math.PI*v+phase));context.sparseY=values;
  const sd=run('detectPeaks(sparseT,sparseY,99,99,.4,{frequency:1,tolerance:.1,noise:.05})');
  assert(sd.samplingLimited);assert(sd.maxIdx.length>40);assert(sd.minIdx.length>40);
  assert(sd.intervals.length>40);
  assert(sd.maxIdx.every(i=>values[i]>=values[i-1]&&values[i]>=values[i+1]));
  assert(sd.minIdx.every(i=>values[i]<=values[i-1]&&values[i]<=values[i+1]));
  context.sparseDetection=sd;
  const sm=run('metrics(sparseT,sparseY,0,.35,.4,sparseDetection)');
  assert(Math.abs(sm.frequency-1)<.015);assert(Number.isFinite(sm.avgMax));assert(Number.isFinite(sm.avgMin));
 }
 assert(run('detectPeaks(sparseT,sparseT.map(()=>7),0,.35,.4,{frequency:1,tolerance:.1,noise:.05}).maxIdx.length')===0);
 assert(run('detectPeaks(sparseT,sparseT.map(()=>7),0,.35,.4,{frequency:1,tolerance:.1,noise:.05}).reason').includes('Constant signal'));
 assert(!run('detectPeaks(sparseT,sparseY,0,.35,.4,{frequency:1.25,tolerance:.1,noise:.05}).samplingLimited'));
 console.log('PASS: 1 Hz at 2.5 saved samples/s, multiple phases/scales, actual extrema and measured frequency');

 // Screenshot-like 5-sample repeating spikes: do not relabel a 2 s visible
 // repetition as a verified 1 s cycle just because the user enters 1 Hz.
 context.sparseY=sparseT.map((_,i)=>[-.0064,-.0086,-.004,-.006,-.0063][i%5]);
 run("DOC.scope=true;DOC.records=[{...record,label:'Sparse scope',t:sparseT,y:sparseY,unit:'V',medianDt:.4,capture:1,channel:'CH1'}];recordOptions();");
 els.record.value='0';await els.record.listeners.change();
 els['applied-frequency'].value='1';await els['apply-peaks'].click();
 assert(!els.notice.textContent);assert(gd.data[2].x.length>40);assert(gd.data[3].x.length>40);
 assert(!els['peak-status'].hidden);assert(els['peak-status'].textContent.includes('2.5 samples per expected cycle'));
 assert(els['filter-status'].textContent.includes('Saved-data sample rate (median): 2.5 Hz'));
 assert(Math.abs(Number(els.stats.children[1].children[1].textContent)-.5)<1e-10);
 assert(els['peak-status'].textContent.includes('differs from the 1 Hz guide'));
 assert(els['peak-note'].textContent.includes('Sampling-limited observed extrema'));
 const sparseMin=gd.data[3].x.slice(),sparseMax=gd.data[2].x.slice();
 els['period-tolerance'].value='0';await els['apply-peaks'].click();assert.deepEqual(gd.data[3].x,sparseMin);
 els['x-start'].value='12';els['x-end'].value='30';await els['apply-x'].click();
 assert.deepEqual(Array.from(gd.data[3].x),Array.from(sparseMin.filter(v=>v>=12&&v<=30)));
 assert.deepEqual(Array.from(gd.data[2].x),Array.from(sparseMax.filter(v=>v>=12&&v<=30)));
 assert(!els['peak-status'].hidden); // report() must not erase the warning.
 await els['reset-filter'].click();assert(!els['peak-status'].hidden);
 els['show-min'].checked=false;await els['show-min'].listeners.change();assert.equal(gd.data[3].visible,false);
 els['show-min'].checked=true;await els['show-min'].listeners.change();assert.equal(gd.data[3].visible,true);
 const sparseBeforeError=gd.data[3].x.slice();els['applied-frequency'].value='1.25';await els['apply-peaks'].click();
 assert(els.notice.textContent.includes('Nyquist'));assert.deepEqual(gd.data[3].x,sparseBeforeError);
 els['applied-frequency'].value='0';await els['apply-peaks'].click();assert(els['peak-status'].hidden);
 assert(Number.isFinite(Number(els.stats.children[1].children[1].textContent)));
 console.log('PASS: sparse scope Update peaks keeps max/min, persistent warning, zoom/filter/toggles and auto recovery');

 // Reproduce the latest screenshot's failure: a long inactive baseline makes
 // all visible troughs lie above the old mean - SD gate. A small shoulder on
 // each major oscillation used to give 200 maxima, zero minima and no frequency.
 context.baselineT=Array.from({length:1000},(_,i)=>i*.4-200);
 context.baselineY=context.baselineT.map((_,i)=>i<500?-.02:[-.0065,-.0086,-.004,-.0064,-.0060][i%5]);
 for(const f of [0,1]){
  const d=run(`detectPeaks(baselineT,baselineY,.02,.35,.4,{frequency:${f},tolerance:.1,noise:.05})`);
  assert(d.maxIdx.length>=98&&d.maxIdx.length<=100);
  assert.equal(d.minIdx.length,d.maxIdx.length-1);
  assert(d.maxIdx.every(i=>context.baselineY[i]===-.004));
  assert(d.minIdx.every(i=>context.baselineY[i]===-.0086));
  const hz=d.intervals.length/d.intervals.reduce((s,p)=>s+p.duration,0);assert(Math.abs(hz-.5)<1e-10);
  if(f)assert(d.reason.includes('differs from the 1 Hz guide'));
 }
 // A constant amplitude offset must change the values, not the marker indices.
 context.shiftedY=context.baselineY.map(v=>v+18);
 const original=run('detectPeaks(baselineT,baselineY,.02,.35,.4,{frequency:0,tolerance:.1,noise:.05})');
 const shifted=run('detectPeaks(baselineT,shiftedY,.02,.35,.4,{frequency:0,tolerance:.1,noise:.05})');
 assert.deepEqual(shifted.maxIdx,original.maxIdx);assert.deepEqual(shifted.minIdx,original.minIdx);
 console.log('PASS: inactive baseline/shoulder regression, real minima, one main peak per oscillation, DC invariance');

 // Never suppress the input-versus-applied distinction, including a failed apply.
 assert(els['peak-active'].textContent.includes('Active mode: AUTOMATIC'));
 els['applied-frequency'].value='1';await els['applied-frequency'].listeners.input();
 assert(els['peak-active'].textContent.includes('NOT APPLIED'));
 assert(els['peak-active'].textContent.includes('AUTOMATIC'));
 let enterPrevented=false;
 await els['applied-frequency'].listeners.keydown({key:'Enter',preventDefault(){enterPrevented=true;}});
 assert(enterPrevented);assert(els['peak-active'].textContent.includes('Applied guide: 1 Hz'));
 assert(!els['peak-active'].textContent.includes('NOT APPLIED'));
 els['applied-frequency'].value='1.25';await els['applied-frequency'].listeners.input();await els['apply-peaks'].click();
 assert(els['peak-active'].textContent.includes('Applied guide: 1 Hz'));
 assert(els['peak-active'].textContent.includes('NOT APPLIED'));
 console.log('PASS: active mode, pending frequency, Enter apply and failed apply preserve the displayed active guide');

 console.log('ALL NODE INTERACTION TESTS PASSED (Plotly/DOM adapter; no browser rendering)');
})().catch(e=>{console.error(e);process.exitCode=1;});
