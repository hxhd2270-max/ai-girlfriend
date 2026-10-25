const result=document.getElementById('pair-result');
const status=document.getElementById('pair-status');
let timer,closed=false,working=false;
async function refresh(){
  clearTimeout(timer);
  if(closed)return;
  try{
    const response=await fetch('/api/wallpaper-pair');
    if(!response.ok)throw Error();
    const pair=await response.json();
    status.textContent=pair.paired
      ? `${pair.connected>0?'壁纸已连接':'已配对，等待壁纸连接'} · ${new Date(pair.expires*1000).toLocaleDateString('zh-CN')} 到期`
      : '尚未配对。连接后运行项目中的 start-wallpaper.cmd。';
  }catch{status.textContent='正在等待本地服务…';}
  if(!closed)timer=setTimeout(refresh,5000);
}
async function action(run){
  if(working)return;working=true;
  const buttons=['install-wallpaper','pair-wallpaper','unpair-wallpaper'].map(id=>document.getElementById(id));
  buttons.forEach(b=>b.disabled=true);
  try{await run();}catch{result.textContent='操作未完成，请检查本地服务和增强壁纸目录后重试。';}
  finally{working=false;buttons.forEach(b=>b.disabled=false);await refresh();}
}
document.getElementById('install-wallpaper').onclick=()=>action(async()=>{
  const response=await fetch('/api/wallpaper-pair/install',{method:'POST'});
  if(!response.ok){
    result.textContent=response.status===409?'增强壁纸配置不可写。请确认 PURPLE-Enhanced 目录及其配对文件存在。':'配对未保存，请检查本地服务后重试。';
    return;
  }
  result.textContent='已写入增强壁纸。请运行项目中的 start-wallpaper.cmd，刷新壁纸入口；服务重启后无需重新配对。';
});
document.getElementById('pair-wallpaper').onclick=()=>action(async()=>{
  const response=await fetch('/api/wallpaper-pair',{method:'POST'});
  if(!response.ok)throw Error();
  const pair=await response.json();
  if(typeof pair.token!=='string'||!/^[A-Za-z0-9_-]{43}$/.test(pair.token))throw Error();
  const config='window.PURPLE_AI_CONFIG = Object.freeze('+JSON.stringify({enabled:true,token:pair.token})+');';
  const url=URL.createObjectURL(new Blob([config],{type:'text/javascript'}));
  const link=document.createElement('a');link.href=url;link.download='ai-pair.local.js';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  result.textContent='已下载配对文件。替换目标增强副本中的同名文件；使用当前增强壁纸时，再运行 start-wallpaper.cmd 刷新入口。请勿将此文件分享出去。';
});
document.getElementById('unpair-wallpaper').onclick=()=>action(async()=>{
  const response=await fetch('/api/wallpaper-pair',{method:'DELETE'});
  if(!response.ok)throw Error();
  result.textContent='壁纸连接已撤销。重新连接后请运行 start-wallpaper.cmd。';
});
window.addEventListener('companion:ready',refresh);
window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);});
window.addEventListener('pageshow',()=>{closed=false;refresh();});
refresh();
