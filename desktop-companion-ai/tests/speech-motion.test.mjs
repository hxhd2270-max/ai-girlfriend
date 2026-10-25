import fs from 'node:fs';
import test from 'node:test';
import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../web/player.js',import.meta.url),'utf8');
const {speechPlan,speechMotion}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
function wav(duration,voiced){
  const rate=24000,b=Buffer.alloc(44+Math.round(duration*rate)*2);
  b.write('RIFF');b.writeUInt32LE(b.length-8,4);b.write('WAVEfmt ',8);b.writeUInt32LE(16,16);
  b.writeUInt16LE(1,20);b.writeUInt16LE(1,22);b.writeUInt32LE(rate,24);b.writeUInt32LE(rate*2,28);
  b.writeUInt16LE(2,32);b.writeUInt16LE(16,34);b.write('data',36);b.writeUInt32LE(b.length-44,40);
  for(let i=0;i<(b.length-44)/2;i++){const t=i/rate;if(voiced(t))b.writeInt16LE(Math.round(Math.sin(t*440*Math.PI*2)*6500),44+i*2);}
  return b;
}
test('leading/trailing silence and a real sentence pause select closed-mouth ranges',()=>{
  const plan=speechPlan(wav(9,t=>t>.4&&t<3||t>5&&t<8.2));
  assert.equal(speechMotion(plan,0).kind,'pause');
  assert.equal(speechMotion(plan,1).kind,'talk-a');
  assert.equal(speechMotion(plan,4).kind,'pause');
  assert.equal(speechMotion(plan,6).kind,'talk-b');
  assert.equal(speechMotion(plan,8.8).kind,'pause');
});
test('30-second continuous speech alternates blocks without crossing closed-mouth gaps',()=>{
  const plan=speechPlan(wav(30,()=>true));
  for(let t=0;t<30;t+=.017){
    const m=speechMotion(plan,t);assert.ok(m.kind.startsWith('talk'));
    assert.ok(m.time>=.30&&m.time<=4.08||m.time>=6.34&&m.time<=11.04);
    assert.ok(m.rate>=.92&&m.rate<=1.08);
  }
  assert.ok(plan.spans.some(s=>s.kind==='talk-b'));
});
test('brief inter-word silence is merged; one-second answers do not stretch a four-second gesture',()=>{
  const plan=speechPlan(wav(1,t=>t<.4||t>.5));
  assert.equal(plan.spans.filter(s=>s.kind==='pause').length,0);
  assert.equal(plan.spans[0].rate,1);assert.equal(plan.duration,1);
});
test('ordinary sentence pauses and isolated syllables do not flash between talk and smile',()=>{
  const plan=speechPlan(wav(12,t=>(t%1.1)<.55));
  assert.equal(plan.spans.filter(s=>s.kind==='pause'&&s.start>0&&s.end<11).length,0);
  const isolated=speechPlan(wav(9,t=>t<.25||t>2&&t<6));
  assert.ok(isolated.spans.filter(s=>s.kind.startsWith('talk')).every(s=>s.end-s.start>=1.6));
});
test('all silence stays closed; malformed or unsupported WAV falls back without blocking playback',()=>{
  const plan=speechPlan(wav(2,()=>false));assert.equal(speechMotion(plan,1).kind,'pause');
  assert.equal(speechMotion(plan,2).hold,true);
  assert.equal(speechPlan(new Uint8Array(3)),null);
  const b=wav(1,()=>true);b.writeUInt16LE(3,20);assert.equal(speechPlan(b),null);
});
