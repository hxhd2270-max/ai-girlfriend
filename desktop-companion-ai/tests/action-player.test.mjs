import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../wallpaper/action-player.js',import.meta.url),'utf8');
const motionSource=fs.readFileSync(new URL('../web/player.js',import.meta.url),'utf8');
const {speechPlan,speechMotion}=await import('data:text/javascript;base64,'+Buffer.from(motionSource).toString('base64'));
function harness(){
  let now=0,id=0;const timers=new Map(),events=new Map(),videos=[],audios=[];
  const node=()=>({style:{},dataset:{},classList:{add(){},remove(){}},append(){},setAttribute(){}});
  function video(){const v={...node(),readyState:2,currentTime:0,handlers:{},muted:true,paused:true,
    addEventListener(k,f){this.handlers[k]=f;},removeEventListener(){},load(){this.currentTime=0;this.ended=false;},
    play(){this.paused=false;return Promise.resolve();},pause(){this.paused=true;}};videos.push(v);return v;}
  const window={companionSpeechMotion:{speechMotion},purpleActionHost:{claim(){},release(){},sound:()=>({enabled:true,volume:.5})},
    addEventListener(k,f){events.set(k,f);},dispatchEvent(e){events.get(e.type)?.(e);}};
  const dialogueAudio={muted:false,volume:1};
  const document={body:node(),head:node(),hidden:false,getElementById:id=>id==='player'?dialogueAudio:node(),querySelector:()=>node(),
    createElement:t=>{if(t==='video')return video();if(t==='audio'){const a={paused:true,currentTime:0,plays:0,handlers:{},addEventListener(k,f){this.handlers[k]=f;},play(){this.paused=false;this.ended=false;this.plays++;return Promise.resolve();},pause(){this.paused=true;}};audios.push(a);return a;}return node();},addEventListener(){}};
  vm.runInNewContext(source,{window,document,CustomEvent:class{constructor(type,o){this.type=type;this.detail=o.detail;}},
    Date:{now:()=>now},setTimeout:(f,ms)=>{timers.set(++id,{f,at:now+ms});return id;},clearTimeout:k=>timers.delete(k),
    setInterval:f=>{window.tick=f;}});
  const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
  return {window,videos,audios,dialogueAudio,document,flush,emit:(type,detail)=>window.dispatchEvent({type,detail}),
    async advance(ms){now+=ms;for(const [key,t] of [...timers])if(t.at<=now){timers.delete(key);t.f();}window.tick();await flush();},
    end(){const v=videos.find(v=>!v.paused&&v.style.opacity==='1');v.ended=true;v.handlers.ended?.();}};
}

async function listen(h){
  await h.flush();h.emit('companion:activity',{draft:true,editing:true});await h.flush();h.end();await h.flush();
  assert.equal(h.window.companionActions.state,'listening');
}
async function prepare(h,event={turnId:'one'},audio={paused:true,ended:false}){
  const job=h.window.companionActions.prepareSpeech(event,audio);await h.flush();
  await h.advance(30);
  assert.equal(h.window.companionActions.state,'speaking');
  await job;return audio;
}
test('thinking intro waits one second, yields to speech, resets each turn',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating'});await h.flush();
  assert.equal(h.window.companionActions.state,'thinking');
  assert.equal(h.audios[0].plays,0);await h.advance(999);assert.equal(h.audios[0].plays,0);await h.advance(1);
  assert.equal(h.audios[0].plays,1);assert.equal(h.audios[0].paused,false);
  const v=h.videos.find(v=>v.style.opacity==='1');assert.equal(v.loop,true);assert.equal(v.muted,true);
  h.emit('companion:reply-state',{state:'buffering'});await h.advance(12000);assert.equal(h.audios[0].plays,1);
  await prepare(h);assert.equal(h.audios[0].paused,true);
  h.emit('companion:reply-state',{state:'idle'});await h.flush();
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(1000);assert.equal(h.audios[0].plays,2);
  h.emit('companion:reply-state',{state:'error'});await h.flush();
  assert.equal(h.window.companionActions.state,'listening');assert.equal(h.audios[0].paused,true);
});
test('long wait uses short hums with quiet gaps, count cap and cancellation',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(1000);
  const a=h.audios[0];
  a.currentTime=1.92;a.ended=true;a.handlers.ended();
  await h.advance(2999);assert.equal(a.plays,1);
  h.emit('companion:reply-progress',{chars:200});
  h.emit('companion:reply-state',{state:'buffering'});await h.advance(1);
  assert.equal(a.plays,2);assert.equal(a.currentTime,.15);
  await h.advance(630);assert.equal(a.paused,true);
  a.handlers.timeupdate();a.ended=true;a.handlers.ended(); // Duplicate end must not schedule twice.
  await h.advance(2999);assert.equal(a.plays,2);
  await h.advance(1);assert.equal(a.plays,3);
  await h.advance(630);await h.advance(20000);assert.equal(a.plays,3);
  h.emit('companion:reply-state',{state:'cancelled'});await h.flush();
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(1000);
  assert.equal(a.plays,4);assert.equal(a.currentTime,0);
  a.ended=true;a.handlers.ended();
  await prepare(h);await h.advance(15000);assert.equal(a.plays,4);assert.equal(a.paused,true);
});
test('estimates use output length and completed waits, repeated states preserve start',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(2000);
  h.emit('companion:reply-progress',{chars:100});
  h.emit('companion:reply-state',{state:'buffering'});
  assert.equal(h.document.body.dataset.thinkingEstimateMs,'9000');
  await h.advance(1000);h.emit('companion:reply-state',{state:'buffering'});
  assert.equal(h.document.body.dataset.thinkingEstimateMs,'8000');
  await prepare(h);h.emit('companion:reply-state',{state:'idle'});await h.flush();
  h.emit('companion:reply-state',{state:'generating'});await h.flush();
  assert.equal(h.document.body.dataset.thinkingEstimateMs,'3000');
  h.emit('companion:reply-state',{state:'cancelled'});await h.flush();
  await h.advance(10000);h.emit('companion:reply-state',{state:'generating'});await h.flush();
  assert.equal(h.document.body.dataset.thinkingEstimateMs,'3000');
});
test('late play promise after cancel cannot schedule a short clip cutoff in a new turn',async()=>{
  const h=harness();await listen(h);let resolve;
  h.audios[0].play=function(){this.plays++;this.paused=false;return new Promise(r=>{resolve=r;});};
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(1000);
  h.emit('companion:reply-state',{state:'cancelled'});await h.flush();resolve();await h.flush();
  assert.equal(h.document.body.dataset.thinkingAudio,'stopped');
  await h.advance(20000);assert.equal(h.audios[0].plays,1);assert.equal(h.audios[0].paused,true);
});
test('near expected readiness defers once; very long waits stop new fillers',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(1000);
  await h.advance(6500);const a=h.audios[0];a.ended=true;a.handlers.ended();
  await h.advance(3000);assert.equal(a.plays,1); // 1.5 seconds left in default estimate.
  await h.advance(2000);assert.equal(a.plays,2); // Wrong estimate cannot defer forever.
  await h.advance(630);await h.advance(45000);assert.equal(a.plays,2);
});
test('wallpaper source mute cannot mute thinking; dialogue gain and mute still apply',async()=>{
  const h=harness();await listen(h);
  h.window.purpleActionHost.sound=()=>({enabled:false,volume:0});
  h.dialogueAudio.volume=.8;
  h.emit('companion:reply-state',{state:'generating'});await h.flush();
  await h.advance(1000);
  assert.equal(h.audios[0].volume,.8);assert.equal(h.audios[0].muted,false);
  assert.equal(h.document.body.dataset.thinkingAudio,'playing');
  h.emit('companion:reply-state',{state:'cancelled'});await h.flush();
  h.dialogueAudio.muted=true;
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(1000);assert.equal(h.audios[0].muted,true);
});
test('cancel before delayed thinking audio prevents late playback',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating'});await h.flush();await h.advance(500);
  h.emit('companion:reply-state',{state:'cancelled'});await h.flush();await h.advance(1500);
  assert.equal(h.audios[0].plays,0);assert.equal(h.window.companionActions.state,'listening');
});
test('cancel pending thinking cannot resurrect it; turning finishes before thinking',async()=>{
  const h=harness();await h.flush();
  h.emit('companion:reply-state',{state:'generating'});await h.flush();
  assert.equal(h.window.companionActions.state,'to-listening');h.end();await h.flush();await h.advance(40);
  assert.equal(h.window.companionActions.state,'thinking');
  h.emit('purple:host-state',{paused:true});assert.equal(h.audios[0].paused,true);
  h.emit('purple:host-state',{paused:false});await h.flush();await h.advance(40);
  assert.equal(h.window.companionActions.state,'listening');
  h.emit('companion:reply-state',{state:'generating'});h.emit('companion:reply-state',{state:'cancelled'});await h.flush();
  assert.equal(h.window.companionActions.state,'listening');
});
test('long typing, two turns, direct listening and guarded three-second return',async()=>{
  const h=harness();await listen(h);await h.advance(90000);assert.equal(h.window.companionActions.state,'listening');
  h.emit('companion:activity',{draft:false,editing:false});
  for(let i=0;i<2;i++){
    h.emit('companion:reply-state',{state:'generating'});const audio=await prepare(h,{turnId:String(i)});
    assert.equal(h.window.companionActions.state,'speaking');
    const v=h.videos.find(v=>v.style.opacity==='1');assert.equal(v.currentTime,4/24);assert.equal(v.paused,true);
    audio.paused=false;await h.advance(40);assert.equal(v.paused,false);assert.equal(v.loop,true);
    h.window.companionActions.finishSpeech();await h.flush();assert.equal(h.window.companionActions.state,'listening');
    h.emit('companion:reply-state',{state:'idle'});
  }
  await h.advance(2999);assert.equal(h.window.companionActions.state,'listening');
  h.window.companionActions.finishSpeech();h.emit('companion:reply-state',{state:'idle'});
  await h.advance(1);assert.equal(h.window.companionActions.state,'to-reading');h.end();await h.flush();assert.equal(h.window.companionActions.state,'reading');
});
test('cancel during introduction prevents late speaking and pause resumes attentive',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-progress',{turnId:'cancel',chars:1});await h.flush();await h.advance(30);
  assert.equal(h.window.companionActions.state,'to-speaking');h.window.companionActions.finishSpeech();await h.advance(40);
  assert.equal(h.window.companionActions.state,'listening');
  h.emit('purple:host-state',{paused:true});assert.ok(h.videos.every(v=>v.paused));
  h.emit('purple:host-state',{paused:false});await h.flush();assert.equal(h.window.companionActions.state,'listening');
});
test('first text starts interruptible introduction and ready audio cuts directly to speech',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating'});
  h.emit('companion:reply-progress',{turnId:'fast',chars:1});await h.flush();await h.advance(30);
  assert.equal(h.window.companionActions.state,'to-speaking');
  const intro=h.videos.find(v=>v.style.opacity==='1');assert.equal(intro.ended,false);
  const audio={paused:true,ended:false};
  await h.window.companionActions.prepareSpeech({turnId:'fast',segmentId:0},audio);
  assert.equal(h.window.companionActions.state,'speaking');assert.equal(intro.paused,true);
  const speaking=h.videos.find(v=>v.style.opacity==='1');assert.equal(speaking.currentTime,4/24);
});
test('generation start begins preparation and audio-start signal releases visible speech loop',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-state',{state:'generating',turnId:'early'});await h.flush();await h.advance(30);
  assert.equal(h.window.companionActions.state,'to-speaking');
  const audio={paused:true,ended:false};await h.window.companionActions.prepareSpeech({turnId:'early'},audio);
  const speaking=h.videos.find(v=>v.style.opacity==='1');assert.equal(speaking.paused,true);
  audio.paused=false;assert.equal(h.window.companionActions.speechStarted({turnId:'early'},audio),true);await h.flush();
  assert.equal(speaking.paused,false);
  assert.equal(h.window.companionActions.speechStarted({turnId:'stale'},audio),false);
});
test('audio readiness interrupts an unfinished attention transition',async()=>{
  const h=harness();await h.flush();
  h.emit('companion:reply-state',{state:'generating',turnId:'attention'});await h.flush();
  assert.equal(h.window.companionActions.state,'to-listening');
  const audio={paused:true,ended:false};await h.window.companionActions.prepareSpeech({turnId:'attention'},audio);
  assert.equal(h.window.companionActions.state,'speaking');
  const speaking=h.videos.find(v=>v.style.opacity==='1');assert.equal(speaking.currentTime,4/24);
});
test('cancelled preparation cannot later cut a newer turn to speech',async()=>{
  const h=harness();await listen(h);
  h.emit('companion:reply-progress',{turnId:'old',chars:1});h.emit('companion:reply-state',{state:'cancelled'});await h.flush();
  assert.equal(h.window.companionActions.state,'listening');
  await h.advance(20000);assert.notEqual(h.window.companionActions.state,'to-speaking');
});
test('continuous long audio uses native looping without seeks or state-bridge restart',async()=>{
  const h=harness();await listen(h);const audio=await prepare(h);
  const v=h.videos.find(v=>v.style.opacity==='1');let seeks=0,position=v.currentTime;
  Object.defineProperty(v,'currentTime',{get:()=>position,set:x=>{seeks++;position=x;}});
  audio.paused=false;
  for(let i=0;i<750;i++)await h.advance(40);
  assert.equal(seeks,0);assert.equal(v.playbackRate,1);assert.equal(v.loop,true);assert.equal(v.paused,false);
  h.emit('purple:ai-state',{active:false,speaking:false});h.emit('purple:ai-state',{active:true,speaking:true});await h.flush();
  assert.equal(h.window.companionActions.state,'speaking');assert.equal(seeks,0);
  audio.paused=true;await h.advance(40);assert.equal(v.paused,true);
});
test('queued segments in same turn keep pose and do not repeat introduction',async()=>{
  const h=harness();await listen(h);const audio=await prepare(h,{turnId:'same',segmentId:0});
  const v=h.videos.find(v=>v.style.opacity==='1');v.currentTime=2.5;
  await h.window.companionActions.prepareSpeech({turnId:'same',segmentId:1},audio);
  assert.equal(h.window.companionActions.state,'speaking');assert.equal(v.currentTime,2.5);
  h.window.companionActions.finishSpeech();await h.flush();assert.equal(h.window.companionActions.state,'listening');
});
test('idle during turning and renewed input on return preserve attention',async()=>{
  const h=harness();await h.flush();h.emit('companion:activity',{draft:false,editing:false});await h.flush();
  await h.advance(3000);h.end();await h.flush();assert.equal(h.window.companionActions.state,'to-reading');
  h.emit('companion:activity',{draft:true,editing:true});await h.flush();assert.equal(h.window.companionActions.state,'to-listening');
  h.end();await h.flush();await h.advance(10000);assert.equal(h.window.companionActions.state,'listening');
});
