const $=id=>document.getElementById(id),player=$('audition-player');
let scores={};
try{scores=JSON.parse(localStorage.getItem('companion-audition-scores')||'{}');}catch{}
const kinds={api_generated:'接口生成',official_demo:'官方演示（非同文实测）'};
async function load(){
  try{
    const boot=await fetch('/api/bootstrap');if(!boot.ok)throw Error('服务连接失败');
    const response=await fetch('/api/auditions');if(!response.ok)throw Error('样本读取失败');
    const {samples}=await response.json();
    $('notice').textContent=samples.length?'已有 '+samples.length+' 段本地样本。':'还没有真实语音样本。账号有效免费额度和服务端凭据配置完成后，先生成一小段验证。';
    for(const sample of samples){
      const article=document.createElement('article');article.className='turn';
      const title=document.createElement('h2');title.textContent=sample.label;article.append(title);
      const text=document.createElement('p');text.textContent=sample.text;article.append(text);
      const meta=document.createElement('p');meta.textContent=(kinds[sample.sourceKind]||'来源未确认')+' · '+sample.status;article.append(meta);
      const play=document.createElement('button');play.textContent='播放这一段';play.disabled=sample.status!=='ready'||!kinds[sample.sourceKind];
      play.onclick=async()=>{player.pause();player.removeAttribute('src');player.load();player.src='/api/auditions/'+encodeURIComponent(sample.id)+'/audio';$('now').textContent='当前：'+sample.label;try{await player.play();}catch{$('error').textContent='播放未开始，请点击播放器重试。';}};article.append(play);
      const label=document.createElement('label');label.textContent=' 综合听感 ';
      const select=document.createElement('select');
      for(const [value,name] of [['','未评分'],['1','1 · 不适合'],['2','2'],['3','3 · 一般'],['4','4'],['5','5 · 很适合']]){const option=document.createElement('option');option.value=value;option.textContent=name;select.append(option);}
      select.value=scores[sample.id]||'';select.onchange=()=>{scores[sample.id]=select.value;try{localStorage.setItem('companion-audition-scores',JSON.stringify(scores));}catch{$('error').textContent='浏览器未能保存评分。';}};label.append(select);article.append(label);$('samples').append(article);
    }
  }catch(e){$('notice').textContent='暂时无法读取试听样本。';$('error').textContent=e.message;}
}
$('export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({date:new Date().toISOString(),scores},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='audition-scores.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
window.addEventListener('pagehide',()=>{player.pause();player.removeAttribute('src');player.load();});
load();
