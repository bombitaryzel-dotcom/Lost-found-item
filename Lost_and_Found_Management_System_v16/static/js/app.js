document.addEventListener('DOMContentLoaded',()=>{
  const root=document.documentElement;
  const themeButton=document.querySelector('[data-theme-toggle]');
  const themeIcon=document.querySelector('[data-theme-icon]');
  const applyTheme=(theme)=>{
    if(theme==='dark') root.dataset.theme='dark'; else delete root.dataset.theme;
    if(themeIcon) themeIcon.textContent=theme==='dark'?'☀':'◐';
    if(themeButton) themeButton.setAttribute('aria-label',theme==='dark'?'Switch to light mode':'Switch to dark mode');
  };
  let savedTheme='light';
  try { savedTheme=localStorage.getItem('sfxc-lf-theme')||'light'; } catch (_) {}
  applyTheme(savedTheme==='dark'?'dark':'light');
  themeButton?.addEventListener('click',()=>{
    const next=root.dataset.theme==='dark'?'light':'dark';
    applyTheme(next);
    try { localStorage.setItem('sfxc-lf-theme',next); } catch (_) {}
  });

  const menuButton=document.querySelector('[data-menu]');
  const nav=document.querySelector('[data-nav]');
  const sidebar=document.querySelector('.sidebar');
  menuButton?.addEventListener('click',()=>{
    nav?.classList.toggle('open');
    sidebar?.classList.toggle('open');
  });

  document.querySelectorAll('[data-print]').forEach(el=>el.addEventListener('click',()=>window.print()));
  document.querySelectorAll('.flash').forEach((el,i)=>setTimeout(()=>{
    el.style.opacity='0';
    el.style.transform='translateY(-8px)';
    setTimeout(()=>el.remove(),250);
  },4500+i*500));
});
