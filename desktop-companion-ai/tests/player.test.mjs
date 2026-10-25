import fs from 'node:fs';
import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../web/player.js',import.meta.url),'utf8');
const {TurnPlayer,shouldSend}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Audio {
  constructor(){this.handlers={};this.paused=true;this.pending=[];}
  addEventListener(k,f){this.handlers[k]=f;}
  pause(){this.paused=true;}
  removeAttribute(){this.src='';}
  load(){}
  play(){this.paused=false;return new Promise(r=>this.pending.push(r));}
}
function fixtureWav(duration=2){
  const rate=16000,b=Buffer.alloc(44+Math.round(duration*rate)*2);
  b.write('RIFF');b.writeUInt32LE(b.length-8,4);b.write('WAVEfmt ',8);b.writeUInt32LE(16,16);
  b.writeUInt16LE(1,20);b.writeUInt16LE(1,22);b.writeUInt32LE(rate,24);b.writeUInt32LE(rate*2,28);
  b.writeUInt16LE(2,32);b.writeUInt16LE(16,34);b.write('data',36);b.writeUInt32LE(b.length-44,40);
  for(let i=0;i<(b.length-44)/2;i++)b.writeInt16LE(Math.round(Math.sin(i/rate*440*Math.PI*2)*5000),44+i*2);
  return b;
}
const audio=new Audio(),reports=[],errors=[];
const p=new TurnPlayer(audio,(e,phase)=>reports.push([e.turnId,phase]),e=>errors.push(e));
const e=(turn,id=0)=>({turnId:turn,segmentId:id,audio:'AA==',mime:'audio/wav',durationMs:420,testAudio:true});
p.begin('old');p.enqueue(e('old'));p.stop();audio.pending.shift()();await Promise.resolve();
assert.deepEqual(reports,[],'late play promise must not report old playback');
p.begin('new');assert.equal(p.enqueue(e('old')),false);p.enqueue(e('new'));audio.pending.shift()();await Promise.resolve();
assert.deepEqual(reports,[['new','started']]);
audio.handlers.ended();assert.deepEqual(reports,[['new','started'],['new','ended']]);
assert.equal(p.enqueue(e('new')),false,'completed segment must not replay');
const gaps=[];const gapAudio=new Audio(),gapPlayer=new TurnPlayer(gapAudio,()=>{},()=>{},null,()=>gaps.push('stop'));
gapPlayer.begin('gap');gaps.length=0;gapPlayer.enqueue(e('gap'));gapAudio.pending.shift()();await Promise.resolve();gapAudio.handlers.ended();
assert.deepEqual(gaps,[],'temporary segment gap must keep the speaking turn open');
assert.equal(gapPlayer.finish('old'),false);assert.equal(gapPlayer.finish('gap'),true);assert.deepEqual(gaps,['stop']);
p.begin('budget');
assert.equal(p.enqueue({...e('budget'),testAudio:false,durationMs:20000}),true);
assert.equal(p.enqueue({...e('budget',1),testAudio:false,durationMs:11000}),false,'budget includes current audio');
errors.length=0;
p.begin('limit');p.enqueue(e('limit'));for(let i=1;i<=5;i++)p.enqueue(e('limit',i));
assert.equal(errors.length,1);assert.equal(audio.paused,true);assert.equal(p.queue.length,0);
assert.equal(shouldSend({key:'Enter',isComposing:true},false),false);
assert.equal(shouldSend({key:'Enter',keyCode:229},false),false);
assert.equal(shouldSend({key:'Enter'},true),false);
assert.equal(shouldSend({key:'Enter',shiftKey:true},false),false);
assert.equal(shouldSend({key:'Enter'},false),true);
console.log('PASS: late play promise, old turn, sequential single player, bounded queue, IME guard (5 branches).');
let release;const gatedAudio=new Audio(),gatedReports=[];
const gated=new TurnPlayer(gatedAudio,(e,p)=>gatedReports.push(p),()=>{},()=>new Promise(r=>{release=r;}));
gated.begin('gated');gated.enqueue(e('gated'));gated.stop();release();await Promise.resolve();
assert.equal(gatedAudio.pending.length,0,'cancelled video preparation must never start audio');
assert.deepEqual(gatedReports,[]);
const fixture=fixtureWav();
const plannedAudio=new Audio();let received,allow;const plannedReports=[];
const planned=new TurnPlayer(plannedAudio,(e,p)=>plannedReports.push(p),()=>{},(event,a)=>{received={event,a};return new Promise(r=>{allow=r;});});
planned.begin('planned');planned.enqueue({...e('planned'),audio:fixture.toString('base64'),durationMs:2000});
assert.equal(received.a,plannedAudio);assert.ok(received.event.motionPlan.spans.length>0);
assert.equal(plannedAudio.pending.length,0,'audio waits for the actual WAV motion plan to be prepared');
allow();await Promise.resolve();assert.equal(plannedAudio.pending.length,1);
plannedAudio.pending.shift()();await Promise.resolve();assert.deepEqual(plannedReports,['started']);
plannedAudio.handlers.ended();assert.deepEqual(plannedReports,['started','ended'],'memory playback receipts remain complete');
let synchronized=false;const syncAudio=new Audio();const synchronizedPlayer=new TurnPlayer(syncAudio,()=>{},()=>{},null,null,(event,a)=>{synchronized=event.turnId==='sync'&&a===syncAudio;});
synchronizedPlayer.begin('sync');synchronizedPlayer.enqueue(e('sync'));syncAudio.pending.shift()();await Promise.resolve();
assert.equal(synchronized,true,'successful audio start directly releases the speaking loop');
