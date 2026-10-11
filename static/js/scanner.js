document.addEventListener('DOMContentLoaded',()=>{
  const start=document.querySelector('#start-scanner'), stop=document.querySelector('#stop-scanner'), status=document.querySelector('#scanner-status');
  if(!start) return;
  let reader=null;
  const say=t=>{if(status) status.textContent=t;};
  const open=value=>{window.location.href='/manage/scanner/lookup?value='+encodeURIComponent(value);};
  start.addEventListener('click',async()=>{
    if(!window.Html5Qrcode){say('Scanner library could not load. Check internet access or use manual lookup below.');return;}
    reader=new Html5Qrcode('qr-reader');
    try { await reader.start({facingMode:'environment'},{fps:10,qrbox:{width:230,height:230}},text=>open(text),()=>{}); start.hidden=true; stop.hidden=false; say('Camera active. Hold the item QR inside the frame.'); }
    catch(e){say('Camera could not start. Allow camera access or use manual lookup below.');}
  });
  stop.addEventListener('click',async()=>{if(reader){try{await reader.stop();reader.clear();}catch(e){} reader=null;}stop.hidden=true;start.hidden=false;say('Camera stopped.');});
});
