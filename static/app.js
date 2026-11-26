/* Track — lightweight responsive client for integrated Investments & Cash tracking.
   All portfolio and cash FX calculations run in Python. */
'use strict';
const $ = (id) => document.getElementById(id);
const state = {
  user:null, dashboard:null, trades:[], cashTxns:[],
  history:{ investments:{}, cash:{}, nav:{} },
  historyRevisions:{ investments:null, cash:null, nav:null },
  dashboardMode:'nav', analyticsMode:'investments', historyMode:'trades',
  historyRange:'1M', view:'home', homeSort:'value', sideFilter:'ALL', cashFilter:'ALL',
  tradeSide:'BUY', cashTxnType:'Credit', authMode:'login',
  hidden:localStorage.getItem('track-private')==='true',
  chart:null, chartPoints:[], chartTimestamps:[], chartScrubIndex:null,
  capitalChart:null, capitalFocus:null,
  cashDonutChart:null, navDonutChart:null,
  theme:localStorage.getItem('track-theme')==='light'?'light':'dark',
  tradeLimit:55, editId:null, editCashId:null, pendingCashSync:null, lastTax:null, busy:false
};
const inFlightHistory = new Map();
const icons = { Stock:'S', Fund:'F', ETF:'E', Crypto:'₿', Cash:'¤' };
const palette = { Stock:['#25344e','#78bdf0'], Fund:['#3c3450','#c3a7ed'], ETF:['#213c4c','#81cddd'], Crypto:['#3b3447','#e2b6cf'], Cash:['#1f3a36','#68d8ad'] };
// Reference-inspired treemap: dark jewel tones alongside lighter pastel and bright tiles.
const capitalColors = [
  '#20467f','#197c85','#317552','#597785','#566977','#a9c864','#d1a43b','#a97530',
  '#553079','#78c9a8','#b87b86','#d5bba0','#79bcda','#efdfa1','#f0a783','#efe9c6',
  '#9a69a3','#ba5c51','#d33429','#9cc86d','#65717b','#dbb134','#90a8d8','#316ea4',
  '#df8f83','#5a9cbd','#4786bd','#a87943','#b63767','#8b613d','#a9bbc8','#648bd0',
  '#6a9b85','#b2a4d9','#bf7950','#8253a3','#77b7ae','#a1aa7e','#d2ad78','#718bb6'
];
function capitalInk(color){
  const rgb=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)/255);
  const linear=rgb.map(c=>c<=0.04045?c/12.92:((c+0.055)/1.055)**2.4);
  const luminance=0.2126*linear[0]+0.7152*linear[1]+0.0722*linear[2];
  return luminance>0.179?'#19202a':'#f7f8fb';
}
const CURRENCY_NAMES = {
  USD:'US dollar', EUR:'Euro', CAD:'Canadian dollar', GBP:'British pound',
  GBX:'British pence', INR:'Indian rupee', SEK:'Swedish krona', NOK:'Norwegian krone',
  DKK:'Danish krone', CNY:'Chinese yuan', JPY:'Japanese yen', AUD:'Australian dollar',
  KRW:'South Korean won', CHF:'Swiss franc', NZD:'New Zealand dollar',
  SGD:'Singapore dollar', HKD:'Hong Kong dollar', PLN:'Polish zloty'
};
// Crisp circular SVG flags for USD, EUR, CAD, GBP, INR, SEK, NOK, DKK, CNY, JPY, AUD, KRW
const CURRENCY_FLAGS = {
  SEK:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#006aa7"/><rect x="18" y="0" width="11" height="64" fill="#fecc02"/><rect x="0" y="26.5" width="64" height="11" fill="#fecc02"/></svg>',
  USD:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#fff"/><g fill="#b22234"><rect y="0" width="64" height="5"/><rect y="10" width="64" height="5"/><rect y="20" width="64" height="5"/><rect y="30" width="64" height="5"/><rect y="40" width="64" height="5"/><rect y="50" width="64" height="5"/><rect y="60" width="64" height="4"/></g><rect width="30" height="35" fill="#3c3b6e"/><g fill="#fff"><circle cx="6" cy="6" r="1.4"/><circle cx="13" cy="6" r="1.4"/><circle cx="20" cy="6" r="1.4"/><circle cx="26" cy="6" r="1.4"/><circle cx="9.5" cy="11.5" r="1.4"/><circle cx="16.5" cy="11.5" r="1.4"/><circle cx="23.5" cy="11.5" r="1.4"/><circle cx="6" cy="17" r="1.4"/><circle cx="13" cy="17" r="1.4"/><circle cx="20" cy="17" r="1.4"/><circle cx="26" cy="17" r="1.4"/><circle cx="9.5" cy="22.5" r="1.4"/><circle cx="16.5" cy="22.5" r="1.4"/><circle cx="23.5" cy="22.5" r="1.4"/><circle cx="6" cy="28" r="1.4"/><circle cx="13" cy="28" r="1.4"/><circle cx="20" cy="28" r="1.4"/><circle cx="26" cy="28" r="1.4"/></g></svg>',
  EUR:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#003399"/><g fill="#ffcc00"><circle cx="32" cy="12" r="2.2"/><circle cx="42" cy="14.7" r="2.2"/><circle cx="49.3" cy="22" r="2.2"/><circle cx="52" cy="32" r="2.2"/><circle cx="49.3" cy="42" r="2.2"/><circle cx="42" cy="49.3" r="2.2"/><circle cx="32" cy="52" r="2.2"/><circle cx="22" cy="49.3" r="2.2"/><circle cx="14.7" cy="42" r="2.2"/><circle cx="12" cy="32" r="2.2"/><circle cx="14.7" cy="22" r="2.2"/><circle cx="22" cy="14.7" r="2.2"/></g></svg>',
  GBP:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#012169"/><path d="M0 0L64 64M64 0L0 64" stroke="#fff" stroke-width="11"/><path d="M0 0L64 64M64 0L0 64" stroke="#c8102e" stroke-width="5"/><path d="M32 0V64M0 32H64" stroke="#fff" stroke-width="15"/><path d="M32 0V64M0 32H64" stroke="#c8102e" stroke-width="9"/></svg>',
  CAD:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#fff"/><rect width="16" height="64" fill="#d80621"/><rect x="48" width="16" height="64" fill="#d80621"/><path d="M32 16l-2.6 7.5-4.4-2 1.8 7-6.8-1.5 2.4 4.2-2.4 1.8 6.4 4.5-1.2 3.5 5.4-.8v7.8h2.8v-7.8l5.4.8-1.2-3.5 6.4-4.5-2.4-1.8 2.4-4.2-6.8 1.5 1.8-7-4.4 2Z" fill="#d80621"/></svg>',
  INR:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="21.5" fill="#ff9933"/><rect y="21.5" width="64" height="21" fill="#fff"/><rect y="42.5" width="64" height="21.5" fill="#138808"/><circle cx="32" cy="32" r="7" fill="none" stroke="#000080" stroke-width="1.6"/><circle cx="32" cy="32" r="1.4" fill="#000080"/><g stroke="#000080" stroke-width="0.9"><line x1="32" y1="25" x2="32" y2="39"/><line x1="25" y1="32" x2="39" y2="32"/><line x1="27" y1="27" x2="37" y2="37"/><line x1="37" y1="27" x2="27" y2="37"/></g></svg>',
  NOK:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#ba0c2f"/><rect x="17" y="0" width="14" height="64" fill="#fff"/><rect x="0" y="25" width="64" height="14" fill="#fff"/><rect x="20.5" y="0" width="7" height="64" fill="#00205b"/><rect x="0" y="28.5" width="64" height="7" fill="#00205b"/></svg>',
  DKK:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#c60c30"/><rect x="19" y="0" width="10" height="64" fill="#fff"/><rect x="0" y="27" width="64" height="10" fill="#fff"/></svg>',
  CNY:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#de2910"/><polygon points="18,13 20.2,19.8 27.3,19.8 21.5,24 23.7,30.8 18,26.6 12.3,30.8 14.5,24 8.7,19.8 15.8,19.8" fill="#ffde00"/><g fill="#ffde00"><circle cx="31" cy="12" r="2.1"/><circle cx="35.5" cy="17.5" r="2.1"/><circle cx="35.5" cy="24.5" r="2.1"/><circle cx="31" cy="30" r="2.1"/></g></svg>',
  JPY:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#fff"/><circle cx="32" cy="32" r="15.5" fill="#bc002d"/></svg>',
  AUD:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#012169"/><rect width="32" height="32" fill="#012169"/><path d="M0 0L32 32M32 0L0 32" stroke="#fff" stroke-width="5"/><path d="M0 0L32 32M32 0L0 32" stroke="#c8102e" stroke-width="2.2"/><path d="M16 0V32M0 16H32" stroke="#fff" stroke-width="8"/><path d="M16 0V32M0 16H32" stroke="#c8102e" stroke-width="4.5"/><g fill="#fff"><circle cx="16" cy="46" r="4.2"/><circle cx="48" cy="18" r="2.3"/><circle cx="44" cy="31" r="2.3"/><circle cx="53" cy="28" r="2"/><circle cx="48" cy="44" r="2.5"/><circle cx="51" cy="35" r="1.4"/></g></svg>',
  KRW:'<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" fill="#fff"/><circle cx="32" cy="32" r="13.5" fill="#0047a0"/><path d="M18.5 32a13.5 13.5 0 0 1 27 0a6.75 6.75 0 0 1-13.5 0a6.75 6.75 0 0 0-13.5 0Z" fill="#cd2e3a"/><g stroke="#111" stroke-width="2.5" stroke-linecap="round"><line x1="11" y1="18" x2="18" y2="11"/><line x1="14" y1="21" x2="21" y2="14"/><line x1="46" y1="11" x2="53" y2="18"/><line x1="43" y1="14" x2="50" y2="21"/><line x1="11" y1="46" x2="18" y2="53"/><line x1="14" y1="43" x2="21" y2="50"/><line x1="46" y1="53" x2="53" y2="46"/><line x1="43" y1="50" x2="50" y2="43"/></g></svg>'
};
function currencyFlag(ccy, small=false){
  const code = String(ccy || '').trim().toUpperCase();
  const key = code === 'GBX' ? 'GBP' : code;
  const svg = CURRENCY_FLAGS[key];
  const cls = small ? 'flag-icon flag-sm' : 'flag-icon';
  if(svg) return `<span class="${cls}" aria-hidden="true">${svg}</span>`;
  return `<span class="${cls} flag-fallback" aria-hidden="true">${html(code.slice(0,3) || '¤')}</span>`;
}
const todayISO = () => new Date().toLocaleDateString('en-CA');
function html(s){ return String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function compact(n,digits=2){const x=Number(n)||0;return new Intl.NumberFormat('en-US',{maximumFractionDigits:digits,minimumFractionDigits:digits}).format(x);}
function quantityFmt(n){return new Intl.NumberFormat('en-US',{maximumFractionDigits:8,minimumFractionDigits:0}).format(Number(n)||0);}
function fmt(v,ccy=state.dashboard?.base_currency ?? state.user?.base_currency ?? 'SEK',digits=0){
  if(v==null)return '—';if(state.hidden)return '••••••';
  const value=compact(v,digits);return ccy==='SEK' ? `${value} kr` : ccy==='INR'?`₹${value}`: ccy==='USD'?`$${value}`:ccy==='EUR'?`€${value}`:ccy==='GBP'?`£${value}`:`${value} ${ccy}`;
}
function fmtCashNative(v,ccy,digits=2){
  if(v==null)return '—';if(state.hidden)return '••••••';
  const code=String(ccy||'').toUpperCase();
  const value=compact(v,digits);
  return code==='SEK' ? `${value} kr` : `${value} ${code}`;
}
function signedFmt(v,ccy=state.dashboard?.base_currency ?? state.user?.base_currency ?? 'SEK',digits=2){
  if(v==null)return '—';if(state.hidden)return '••••••';
  const n=Number(v)||0;
  const eps=digits===0?0.5:Math.pow(10,-digits)/2;
  if(Math.abs(n)<eps)return fmt(0,ccy,digits);
  return `${n<0?'-':'+'}${fmt(Math.abs(n),ccy,digits)}`;
}
function signedAmt(v,ccy=state.dashboard?.base_currency ?? state.user?.base_currency ?? 'SEK',digits=0){
  if(v==null)return '—';if(state.hidden)return '••••••';
  const n=Number(v)||0;
  const eps=digits===0?0.5:Math.pow(10,-digits)/2;
  if(Math.abs(n)<eps)return fmt(0,ccy,digits);
  return `${n<0?'-':''}${fmt(Math.abs(n),ccy,digits)}`;
}
function signedPct(n){return state.hidden?'••••':`${Number(n)>=0?'+':''}${compact(n,2)}%`;}
function toast(message,isError=false){const el=$('toast');el.textContent=message;el.className='toast show'+(isError?' error':'');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('show'),4200);}
async function api(url,options={}){
  const config={credentials:'same-origin',...options};
  if(config.body && !(config.body instanceof FormData) && typeof config.body!=='string'){
    config.headers={...config.headers,'Content-Type':'application/json'};
    config.body=JSON.stringify(config.body);
  }
  let res;
  try {res=await fetch(url,config);}catch(e){throw new Error('Unable to reach server. Check your connection.');}
  if(!res.ok){let d={};try{d=await res.json();}catch(_){};
    if(res.status===401 && state.user){state.user=null;showAuth();}
    throw new Error(typeof d.detail==='string'?d.detail:`Request failed (${res.status})`);
  }
  return res.status===204?{}:res.json();
}
const EYE_OPEN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_CLOSED_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18M9.8 6.3A11 11 0 0 1 12 6c6.4 0 10 6 10 6a17 17 0 0 1-3.6 4.1M6 8.1A18 18 0 0 0 2 12s3.6 6 10 6c1.4 0 2.6-.3 3.7-.8"/><path d="M9 10a3 3 0 0 0 5 4"/></svg>';
const SUN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
const MOON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>';

function syncQuickThemeIcons(){
  const icon = state.theme === 'dark' ? SUN_SVG : MOON_SVG;
  const label = state.theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
  for(const id of ['theme-quick-toggle', 'sidebar-theme-toggle']){
    const btn = $(id);
    if(btn){ btn.innerHTML = icon; btn.title = label; btn.setAttribute('aria-label', label); }
  }
}
function applyTheme(theme){
  state.theme=theme==='light'?'light':'dark';localStorage.setItem('track-theme',state.theme);
  document.documentElement.dataset.theme=state.theme;
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if(themeMeta) themeMeta.content=state.theme==='dark'?'#090b0e':'#efeae1';
  document.querySelectorAll('[data-theme-choice]').forEach(b=>{
    const selected=b.dataset.themeChoice===state.theme;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));
  });
  syncQuickThemeIcons();
  if(state.dashboard){
    renderDashboard();
    renderAnalytics();
    renderChart(currentHistorySeries());
  }
}
function toggleHidden(value){
  state.hidden=!!value;localStorage.setItem('track-private',String(value));
  const accPriv = $('account-privacy'); if(accPriv) accPriv.checked=value;
  const icon = value ? EYE_CLOSED_SVG : EYE_OPEN_SVG;
  for(const id of ['privacy-toggle', 'sidebar-privacy-toggle']){
    const btn = $(id); if(btn) btn.innerHTML = icon;
  }
  if(state.dashboard){
    renderDashboard();
    renderAnalytics();
    renderChart(currentHistorySeries());
  }
  if(state.lastTax)renderTax(state.lastTax);
  renderHistoryList();
}
function showAuth(){ $('loader').classList.add('hidden');$('app').classList.add('hidden');$('auth').classList.remove('hidden'); }
function showApp(){ $('loader').classList.add('hidden');$('auth').classList.add('hidden');$('app').classList.remove('hidden'); }
function closeSortMenu(){
  const menu=$('home-sort-menu'), btn=$('home-sort-btn');
  if(menu) menu.classList.add('hidden');
  if(btn) btn.setAttribute('aria-expanded','false');
}
function nav(tab){
  if(!state.user)return;
  closeSortMenu();
  endChartScrub();
  state.view=tab;
  document.querySelectorAll('.view').forEach(el=>el.classList.toggle('active',el.id==='view-'+tab));
  document.querySelectorAll('.nav-btn').forEach(btn=>{
    const active=btn.dataset.tab===tab;btn.classList.toggle('active',active);
    if(active)btn.setAttribute('aria-current','page');else btn.removeAttribute('aria-current');
  });
  window.scrollTo({top:0,behavior:'instant'});
  if(tab==='analytics'){
    renderAnalytics();
    setTimeout(()=>{
      fitCapitalTileLabels($('capital-chart'));
      state.cashDonutChart?.resize();
      state.navDonutChart?.resize();
    },35);
  }
  if(tab==='home'){setTimeout(()=>state.chart?.resize(),40);}
  if(tab==='history')loadHistoryData();
  if(tab==='tax')loadTax();
  if(tab==='account')renderAccount();
}

// Logos are a presentation-only enhancement. Original letter/Bitcoin icons remain
// underneath each image and are used whenever no real logo can be loaded.
// The logo providers require no account, API key, or Python/backend changes.
const issuerPatterns = [
  [/\b(?:i\s?shares)\b/i,'ishares.com'],
  [/\b(?:blackrock)\b/i,'blackrock.com'],
  [/\b(?:vanguard)\b/i,'vanguard.com'],
  [/\b(?:spdr|state street)\b/i,'statestreet.com'],
  [/\b(?:invesco|powershares)\b/i,'invesco.com'],
  [/\b(?:schwab|charles schwab)\b/i,'schwab.com'],
  [/\b(?:fidelity international)\b/i,'fidelityinternational.com'],
  [/\b(?:fidelity)\b/i,'fidelity.com'],
  [/\b(?:amundi|lyxor)\b/i,'amundi.com'],
  [/\b(?:xtrackers|dws)\b/i,'dws.com'],
  [/\b(?:ubs)\b/i,'ubs.com'],
  [/\b(?:franklin templeton|franklin|templeton)\b/i,'franklintempleton.com'],
  [/\b(?:j\.?p\.?\s?morgan|jpmorgan)\b/i,'jpmorgan.com'],
  [/\b(?:vaneck)\b/i,'vaneck.com'],
  [/\b(?:wisdomtree)\b/i,'wisdomtree.com'],
  [/\b(?:global x)\b/i,'globalxetfs.com'],
  [/\b(?:first trust)\b/i,'firsttrust.com'],
  [/\b(?:ark invest|ark innovation|ark etf|ark funds)\b/i,'ark-invest.com'],
  [/\b(?:grayscale)\b/i,'grayscale.com'],
  [/\b(?:bitwise)\b/i,'bitwiseinvestments.com'],
  [/\b(?:pimco)\b/i,'pimco.com'],
  [/\b(?:bnp paribas)\b/i,'bnpparibas.com'],
  [/\b(?:hsbc)\b/i,'hsbc.com'],
  [/\b(?:goldman sachs)\b/i,'goldmansachs.com'],
  [/\b(?:morgan stanley)\b/i,'morganstanley.com'],
  [/\b(?:t\.? rowe price)\b/i,'troweprice.com'],
  [/\b(?:dimensional)\b/i,'dimensional.com'],
  [/\b(?:legal & general|lgim)\b/i,'landg.com'],
  [/\b(?:nordea)\b/i,'nordea.com'],
  [/\b(?:swedbank|robur)\b/i,'swedbank.com'],
  [/\b(?:handelsbanken)\b/i,'handelsbanken.com'],
  [/\b(?:länsförsäkringar|lansforsakringar)\b/i,'lansforsakringar.se'],
  [/\b(?:avanza)\b/i,'avanza.se'],
  [/\b(?:nordnet)\b/i,'nordnet.se'],
  [/\b(?:spiltan)\b/i,'spiltanfonder.se'],
  [/\b(?:pareto|paretoam)\b/i,'paretoam.com'],
  [/\b(?:edelweiss|edelweissmf)\b/i,'edelweissmf.com'],
  [/\b(?:hdfc(?: mutual fund| asset management| amc)|hdfcmf)\b/i,'hdfcfund.com'],
  [/\b(?:icici prudential|icicipru(?:dential)?(?:mf)?)\b/i,'icicipruamc.com'],
  [/\b(?:nippon india|reliance mutual fund)\b/i,'mf.nipponindiaim.com'],
  [/\b(?:sbi(?: mutual fund| magnum| bluechip| small cap| focused))\b/i,'sbimf.com'],
  [/\b(?:axis mutual fund|axis (?:bluechip|small cap|midcap|flexi cap))\b/i,'axismf.com'],
  [/\b(?:uti mutual fund|uti nifty|uti flexi|uti index)\b/i,'utimf.com'],
  [/\b(?:quant mutual fund|quant small cap|quant active)\b/i,'quantmutual.com'],
  [/\b(?:parag parikh|ppfas)\b/i,'ppfas.com'],
  [/\b(?:motilal oswal)\b/i,'motilaloswalmf.com'],
  [/\b(?:kotak mutual fund|kotak (?:equity|flexi|small cap|emerging))\b/i,'kotakmf.com'],
  [/\b(?:storebrand)\b/i,'storebrand.com'],
  [/\b(?:carnegie)\b/i,'carnegie.se'],
  [/\b(?:didner & gerge|didner gerge)\b/i,'didnergerge.se'],
  [/\b(?:danske invest|danske bank)\b/i,'danskebank.com'],
  [/\b(?:skagen)\b/i,'skagenfondene.no'],
  [/\b(?:skandia)\b/i,'skandia.se'],
  [/\b(?:simplicity)\b/i,'simplicity.se'],
  [/\b(?:lannebo)\b/i,'lannebo.se'],
  [/\b(?:öhman|ohman)\b/i,'ohman.se'],
  [/\b(?:dnb)\b/i,'dnb.no'],
  [/\b(?:seb)\b/i,'sebgroup.com']
];
// Where a fund's display name is only its ticker, these identify the issuer.
const issuerTickers = [
  ['vanguard.com','VOO VTI VT VXUS VEA VWO VUG VTV VIG VYM VGT VHT VFH VDE VDC VPU VCR VAW VB VO VBR VBK BND BNDX VGSH VGIT VGLT VCSH VCIT VCLT VUSB VOOE VFIAX VTSAX VWRL VUSA VUAA VWCE'],
  ['ishares.com','SGLN IGLN EGLN PPFB CSPX SXR8 EUNL IWDA SWDA IUSA IUSN IGLA ISF EIMI IWRD EXSA SSLN ISLN'],
  ['blackrock.com','IVV IWM IWB IWF IWD IWO IWN EFA EEM IEMG ITOT IEFA ACWI URTH AGG IAU TLT SHY IEF HYG LQD USMV QUAL MTUM DGRO IJR IJH IBIT ICLN IXUS HEFA IUSG IUSV'],
  ['statestreet.com','SPY SPLG SPYG SPYV SPYD GLD GLDM DIA MDY SDY XLF XLK XLE XLY XLC XLI XLP XLV XLRE XLU XLB XBI KRE XOP SPTM SPAB'],
  ['invesco.com','QQQ QQQM RSP SPLV SPHQ PDBC TQQQ SQQQ EQQQ QQQJ XMMO XMHQ PRF PHB'],
  ['schwab.com','SCHD SCHB SCHX SCHG SCHV SCHF SCHE SCHM SCHA SCHH SCHZ SCHR SCHQ SCHI SCHJ'],
  ['fidelity.com','FTEC FDIS FENY FSTA FHLC FNCL FUTY FCOM FIDU FMAT FXAIX FSKAX FZROX FBGRX'],
  ['ark-invest.com','ARKK ARKG ARKW ARKF ARKQ ARKB ARKX PRNT IZRL'],
  ['vaneck.com','SMH GDX GDXJ MOAT ANGL MORT BIZD'],
  ['wisdomtree.com','DGRW DGS DXJ EPI USFR'],
  ['jpmorgan.com','JEPI JEPQ JPST JPIE JAAA'],
  ['globalxetfs.com','QYLD XYLD RYLD DIV BOTZ LIT PAVE COPX'],
  ['grayscale.com','GBTC ETHE GDLC'],
  ['bitwiseinvestments.com','BITB BITW'],
  ['franklintempleton.com','EZBC FLQL FLCO'],
  ['firsttrust.com','FTCS FDN CIBR SKYY'],
  ['pimco.com','BOND MINT'],
];
const cryptoNames = {
  BITCOIN:'BTC',XBT:'BTC',ETHEREUM:'ETH',ETHER:'ETH',TETHER:'USDT',
  'TETHER USD':'USDT','USD COIN':'USDC',USDC:'USDC',RIPPLE:'XRP',XRP:'XRP',
  SOLANA:'SOL',CARDANO:'ADA',DOGECOIN:'DOGE',DOGE:'DOGE',POLKADOT:'DOT',
  CHAINLINK:'LINK',AVALANCHE:'AVAX',LITECOIN:'LTC','BITCOIN CASH':'BCH',
  MONERO:'XMR',STELLAR:'XLM',TRON:'TRX',POLYGON:'MATIC',POL:'POL',
  UNISWAP:'UNI',COSMOS:'ATOM',ALGORAND:'ALGO',HEDERA:'HBAR',
  AAVE:'AAVE',NEAR:'NEAR',APTOS:'APT',SUI:'SUI',ARBITRUM:'ARB',
  OPTIMISM:'OP','INTERNET COMPUTER':'ICP',FILECOIN:'FIL',
  'SHIBA INU':'SHIB',SHIBA:'SHIB',PEPE:'PEPE',TONCOIN:'TON',
  'THE OPEN NETWORK':'TON',BNB:'BNB','BINANCE COIN':'BNB',
  ETHEREUMCLASSIC:'ETC','ETHEREUM CLASSIC':'ETC',TEZOS:'XTZ'
};
// Yahoo Finance suffixes identify the market. Do not strip them and then
// accidentally display the logo of a different company's US ticker.
// Yahoo Finance suffixes identify the exchange. Never send a European symbol
// to an unqualified US ticker API (e.g. G24.DE is not the US ticker G24).
// Market names match the public global-stock-logos SVG catalogue.
const yahooLogoMarkets = Object.freeze({
  NS:'in', BO:'in', ST:'se', DE:'germany', F:'germany', BE:'germany',
  MU:'germany', DU:'germany', HM:'germany', HA:'germany', SG:'germany',
  PA:'france', AS:'netherlands', HE:'finland', CO:'denmark', OL:'norway',
  L:'uk', MI:'italy', MC:'spain', SW:'switzerland', VI:'austria',
  BR:'belgium', LS:'portugal', IR:'ireland', WA:'poland', AT:'greece',
  IC:'iceland', TL:'estonia', RG:'latvia', VS:'lithuania',
  TA:'israel', HK:'hongkong', T:'japan', KS:'korea', KQ:'korea',
  TW:'taiwan', TWO:'taiwan', SS:'china', SZ:'china', AX:'australia',
  NZ:'newzealand', TO:'canada', V:'canada', SA:'brazil', MX:'mexico',
  SI:'singapore', KL:'malaysia', JK:'indonesia', BK:'thailand',
  JO:'southafrica', IS:'turkey', QA:'qatar', SR:'ksa', AE:'uae',
  SN:'chile', BA:'argentina', TAO:'israel'
});
// The legacy country keys used by the existing Indian/Swedish name aliases.
const catalogMarket = market => market==='se'?'sweden':market;
const stockLogoCdn = 'https://cdn.jsdelivr.net/gh/MrChartist/global-stock-logos@main/logos/';
// Exact user-supplied local logos for holdings where the external services
// either miss entirely or return the wrong brand variant. Put these files in
// /static/logos/ alongside the rest of the frontend static assets.
const localLogoByTicker = Object.freeze({
  '0P00000F04.ST':'/static/logos/pareto-asset-management.png',
  '0P00009NT9.ST':'/static/logos/spiltan-fonder.png',
  '0P0000XV5C.BO':'/static/logos/edelweiss-mutual-fund.png',
  '0P00000FYR.ST':'/static/logos/storebrand.png',
  '0P00000FYN.ST':'/static/logos/storebrand.png',
  '0P00005WD7.BO':'/static/logos/icici-prudential.png',
  'IKS.NS':'/static/logos/inventurus-knowledge-solutions.png',
  'EGLNL.XC':'/static/logos/ishares.png',
  'ADYEN.AS':'/static/logos/adyen.png',
  'AFFLE.NS':'/static/logos/affle.png',
  'ALV.DE':'/static/logos/allianz.png',
  'APLAPOLLO.NS':'/static/logos/aplapollo.png',
  'ASKAUTOLTD.NS':'/static/logos/askauto.png',
  'AXP':'/static/logos/axp.png',
  'AZN.ST':'/static/logos/azn.png',
  'ADYEN.AS':'/static/logos/adyen.png',
  'BAJFINANCE.NS':'/static/logos/bajajfin.png',
  'BONEX.ST':'/static/logos/bonesupport.png',
  'CAPLIPOINT.NS':'/static/logos/caplin.png',
  'CCL.NS':'/static/logos/ccl.png',
  'CPRT':'/static/logos/copart.png',
  'DYVOX.ST':'/static/logos/dynavox.png',
  'G24.DE':'/static/logos/g24.png',
  'GALAXYSURF.NS':'/static/logos/galaxy.png',
  'GOOGL':'/static/logos/googl.png',
  'RMS.PA':'/static/logos/hermes.png',
  'HSCL.NS':'/static/logos/hscl.png',
  'ICRA.NS':'/static/logos/icra.png',
  'INOXINDIA.NS':'/static/logos/inoxindia.png',
  'IVSO.ST':'/static/logos/invisio.png',
  'SAP.DE':'/static/logos/sap.png',
  'VIT-B.ST':'/static/logos/vitec.png',
  'WRT1V.HE':'/static/logos/wartsila.png',
  'WKL.AS':'/static/logos/wkl.png',
  'KALYANKJIL.NS':'/static/logos/kalyan.png',
  'KFINTECH.NS':'/static/logos/KFin.png',
  'KOVAI.NS':'/static/logos/kovai.png',
  'POLYCAB.NS':'/static/logos/polycab.png',
  'KIRLPNU.NS':'/static/logos/kpcl.png',
  'META':'/static/logos/meta.png',
  'MSFT':'/static/logos/microsoft.png',
  'NH.NS':'/static/logos/Narayana.png',
  'POLYMED.NS':'/static/logos/polymed.png',
  'RAY-B.ST':'/static/logos/raysearch.png',
  'MELI':'/static/logos/meli.PNG',
  
});
const localLogoByName = new Map([
  ['Pareto Asset Management','/static/logos/pareto-asset-management.png'],
  ['Spiltan Fonder','/static/logos/spiltan-fonder.png'],
  ['Edelweiss Mutual Fund','/static/logos/edelweiss-mutual-fund.png'],
  ['Storebrand','/static/logos/storebrand.png'],
  ['ICICI Prudential','/static/logos/icici-prudential.png'],
  ['Inventurus Knowledge Solutions','/static/logos/inventurus-knowledge-solutions.png'],
  ['iShares','/static/logos/ishares.png']
].map(([name,url])=>[normalizeCompanyName(name),url]));
// Names in holdings can be company names rather than tickers. These aliases
// resolve the most common Indian and Swedish holdings without changing Python.
const regionalStockNames = [
  // India (NSE/BSE)
  ['in','RELIANCE','Reliance Industries|Reliance','relianceindustries.com'],
  ['in','TCS','Tata Consultancy Services|TCS','tcs.com'],
  ['in','INFY','Infosys|Infosys Limited','infosys.com'],
  ['in','HDFCBANK','HDFC Bank','hdfcbank.com'],
  ['in','ICICIBANK','ICICI Bank','icicibank.com'],
  ['in','SBIN','State Bank of India|SBI','sbi.co.in'],
  ['in','BHARTIARTL','Bharti Airtel|Airtel','airtel.in'],
  ['in','ITC','ITC Limited|ITC','itcportal.com'],
  ['in','HINDUNILVR','Hindustan Unilever|HUL','hul.co.in'],
  ['in','LT','Larsen and Toubro|Larsen & Toubro|L&T','larsentoubro.com'],
  ['in','WIPRO','Wipro','wipro.com'],
  ['in','TATAMOTORS','Tata Motors','tatamotors.com'],
  ['in','TMPV','Tata Motors Passenger Vehicles','tatamotors.com'],
  ['in','TATASTEEL','Tata Steel','tatasteel.com'],
  ['in','M&M','Mahindra and Mahindra|Mahindra & Mahindra|M&M','mahindra.com'],
  ['in','BAJFINANCE','Bajaj Finance','bajajfinserv.in'],
  ['in','BAJAJFINSV','Bajaj Finserv','bajajfinserv.in'],
  ['in','KOTAKBANK','Kotak Mahindra Bank|Kotak Bank','kotak.com'],
  ['in','AXISBANK','Axis Bank','axisbank.com'],
  ['in','ASIANPAINT','Asian Paints','asianpaints.com'],
  ['in','SUNPHARMA','Sun Pharmaceutical|Sun Pharma','sunpharma.com'],
  ['in','MARUTI','Maruti Suzuki|Maruti','marutisuzuki.com'],
  ['in','TITAN','Titan Company|Titan','titancompany.in'],
  ['in','NESTLEIND','Nestle India','nestle.in'],
  ['in','ADANIENT','Adani Enterprises','adani.com'],
  ['in','ADANIPORTS','Adani Ports','adaniports.com'],
  ['in','ADANIGREEN','Adani Green Energy','adanigreenenergy.com'],
  ['in','NTPC','NTPC','ntpc.co.in'],
  ['in','POWERGRID','Power Grid Corporation|Power Grid','powergrid.in'],
  ['in','TATAPOWER','Tata Power','tatapower.com'],
  ['in','ULTRACEMCO','UltraTech Cement|Ultratech','ultratechcement.com'],
  ['in','HCLTECH','HCL Technologies|HCL Tech','hcltech.com'],
  ['in','TECHM','Tech Mahindra','techmahindra.com'],
  ['in','COALINDIA','Coal India','coalindia.in'],
  ['in','DRREDDY','Dr Reddys Laboratories|Dr Reddy','drreddys.com'],
  ['in','CIPLA','Cipla','cipla.com'],
  ['in','EICHERMOT','Eicher Motors','eichermotors.com'],
  ['in','ETERNAL','Eternal|Zomato','eternal.com'],
  ['in','DMART','Avenue Supermarts|DMart','dmartindia.com'],
  ['in','INDIGO','Interglobe Aviation|Indigo Airlines','goindigo.in'],
  ['in','PIDILITIND','Pidilite Industries|Pidilite','pidilite.com'],
  ['in','HDFCLIFE','HDFC Life','hdfclife.com'],
  ['in','SBILIFE','SBI Life','sbilife.co.in'],
  ['in','BEL','Bharat Electronics','bel-india.in'],
  ['in','HAL','Hindustan Aeronautics','hal-india.co.in'],
  ['in','JIOFIN','Jio Financial Services','jiofinance.in'],
  // Sweden (Nasdaq Stockholm)
  ['se','INVE-B','Investor AB|Investor B|Investor','investorab.com'],
  ['se','VOLV-B','Volvo AB|AB Volvo|Volvo Group|Volvo B','volvogroup.com'],
  ['se','VOLCAR-B','Volvo Cars|Volvo Car','volvocars.com'],
  ['se','ATCO-A','Atlas Copco|Atlas Copco A|Atlas Copco AB','atlascopcogroup.com'],
  ['se','ERIC-B','Ericsson|Ericsson B|LM Ericsson','ericsson.com'],
  ['se','SAND','Sandvik','sandvik.com'],
  ['se','SEB-A','SEB|Skandinaviska Enskilda Banken','sebgroup.com'],
  ['se','NDA-SE','Nordea|Nordea Bank','nordea.com'],
  ['se','SWED-A','Swedbank|Swedbank AB','swedbank.com'],
  ['se','SHB-A','Handelsbanken|Svenska Handelsbanken','handelsbanken.com'],
  ['se','HEXA-B','Hexagon AB|Hexagon B','hexagon.com'],
  ['se','ASSA-B','ASSA ABLOY|Assa Abloy B','assaabloy.com'],
  ['se','SAAB-B','Saab AB|Saab B|Saab','saab.com'],
  ['se','SKF-B','SKF|SKF B','skf.com'],
  ['se','HM-B','Hennes and Mauritz|Hennes & Mauritz|H&M','hmgroup.com'],
  ['se','AZA','Avanza|Avanza Bank','avanza.se'],
  ['se','EQT','EQT AB|EQT','eqtgroup.com'],
  ['se','EVO','Evolution AB|Evolution Gaming|Evolution','evolution.com'],
  ['se','ALFA','Alfa Laval','alfalaval.com'],
  ['se','ESSITY-B','Essity|Essity B','essity.com'],
  ['se','TEL2-B','Tele2|Tele2 B','tele2.com'],
  ['se','TELIA','Telia|Telia Company','teliacompany.com'],
  ['se','EPI-A','Epiroc|Epiroc A','epirocgroup.com'],
  ['se','INDU-C','Industrivarden|Industrivärden','industrivarden.se'],
  ['se','SCA-B','SCA|Svenska Cellulosa','sca.com'],
  ['se','SKA-B','Skanska|Skanska B','group.skanska.com'],
  ['se','NIBE-B','Nibe Industrier|Nibe','nibe.com'],
  ['se','BOL','Boliden','boliden.com'],
  ['se','HUSQ-B','Husqvarna|Husqvarna B','husqvarnagroup.com'],
  ['se','AXFO','Axfood','axfood.com'],
  ['se','CAST','Castellum','castellum.se'],
  ['se','SINCH','Sinch','sinch.com'],
  ['se','EMBRAC-B','Embracer Group|Embracer','embracer.com'],
  ['se','ADDTECH-B','Addtech','addtech.com'],
  ['se','ALIV-SDB','Autoliv','autoliv.com'],
  ['se','LATO-B','Latour|Investment AB Latour','latour.se'],
  ['se','BALD-B','Fastighets Balder|Balder','balder.se'],
  ['se','SOBI','Swedish Orphan Biovitrum|Sobi','sobi.com'],
  ['se','8TRA','Traton|Traton Group','traton.com']
];
const stockByName = new Map();
const stockBySymbol = new Map();
for(const [market,symbol,names,domain] of regionalStockNames){
  const entry={market,symbol,domain};
  stockBySymbol.set(`${market}:${symbol}`,entry);
  for(const name of names.split('|'))stockByName.set(normalizeCompanyName(name),entry);
}
function parseStockTicker(raw){
  const input=String(raw||'').trim().toUpperCase().replace(/^\$/,'').replace(/\s+/g,'');
  if(!input||input.length>35)return null;
  // Yahoo's .ST, .DE, .AS, .PA, .HE etc. are part of the identity.
  const yahoo=input.match(/^([A-Z0-9&_-]+)\.([A-Z]{1,3})$/);
  if(yahoo && yahooLogoMarkets[yahoo[2]])
    return {symbol:yahoo[1],market:yahooLogoMarkets[yahoo[2]],yahoo:input};
  const exchange=input.match(/^([A-Z][A-Z0-9_-]{1,24}):([A-Z0-9&_.-]{1,18})$/);
  if(exchange){
    const prefixes={NASDAQSTOCKHOLM:'se',OMXSTO:'se',STO:'se',NSE:'in',BSE:'in',
      NSEI:'in',XETRA:'germany',ETR:'germany',FRA:'germany',EPA:'france',
      AMS:'netherlands',HEL:'finland',CPH:'denmark',OSL:'norway',LON:'uk',
      NASDAQ:'us',NYSE:'us',AMEX:'us',NYSEARCA:'us',TASE:'israel'};
    return {symbol:exchange[2],market:prefixes[exchange[1]]||null,exchange:exchange[1]};
  }
  // Preserve US share-class symbols such as BRK.B; don't mistake unknown
  // Yahoo suffixes for US tickers or silently pick the wrong company.
  if(yahoo && yahoo[2]!=='A'&&yahoo[2]!=='B')return null;
  return /^[A-Z0-9][A-Z0-9.&-]{0,20}$/.test(input)
    ?{symbol:input,market:null,yahoo:input}:null;
}
function stockLogoTicker(raw){
  const parsed=parseStockTicker(raw);
  return parsed&&!parsed.market?parsed.symbol:'';
}
function regionalStockMatch(holding){
  const ticker=parseStockTicker(holding.ticker)||parseStockTicker(holding.asset);
  const name=stockByName.get(normalizeCompanyName(holding.asset));
  if(ticker?.market){
    // Prefer the actual exchange symbol over a possibly ambiguous display name.
    return stockBySymbol.get(`${ticker.market}:${ticker.symbol}`)||
      (name?.market===ticker.market?name:{market:ticker.market,symbol:ticker.symbol,domain:''});
  }
  // Explicit ticker without a country suffix may still identify a known company.
  const matchingSymbol=ticker && (stockBySymbol.get(`in:${ticker.symbol}`)||stockBySymbol.get(`se:${ticker.symbol}`));
  return matchingSymbol||name||null;
}
function regionalStockLogo(entry){
  const symbol=entry.symbol;
  return `${stockLogoCdn}${catalogMarket(entry.market)}/${encodeURIComponent(symbol.replace(/[-.]/g,'_'))}.svg`;
}
function domainLogoSources(domain){
  // On a catalog miss, try a few issuer-logo providers before falling back
  // to a generic favicon. This improves fund and ETF logo coverage without
  // hardcoding per-ticker images into the UI.
  return [`https://logoapi.sh/icon/${domain}.svg?fallback=404`,
    `https://logo.clearbit.com/${domain}`,
    `https://icons.duckduckgo.com/ip3/${domain}.ico`];
}
function cryptoLogoSymbol(holding){
  for(const input of [holding.ticker,holding.asset]){
    const raw=String(input||'').trim().toUpperCase();
    if(!raw)continue;
    if(cryptoNames[raw])return cryptoNames[raw];
    const symbol=raw.replace(/^[A-Z]+:/,'')
      .replace(/^([A-Z0-9]{2,10})[-/]?(?:USDT|USDC|USD|EUR|SEK|GBP)(?:=X)?$/,'$1');
    if(cryptoNames[symbol])return cryptoNames[symbol];
    if(/^[A-Z0-9]{2,12}$/.test(symbol) && !/^(USD|EUR|SEK|GBP)$/.test(symbol))return symbol;
  }
  return '';
}
function fundIssuerDomain(name,ticker){
  for(const [pattern,domain] of issuerPatterns)if(pattern.test(name))return domain;
  const symbol=String(ticker||name).trim().toUpperCase().split('.')[0];
  for(const [domain,symbols] of issuerTickers)
    if(symbols.split(' ').includes(symbol))return domain;
  return '';
}
function fundIssuerDomains(name,ticker){
  const domain=fundIssuerDomain(name,ticker);
  if(!domain)return [];
  // iShares is BlackRock's ETF brand. If one icon catalog lacks it, try the
  // parent's identity, rather than substituting an unrelated gold-company logo.
  return domain==='ishares.com'?['ishares.com','blackrock.com']:[domain];
}
// These are verified company websites, used as fallbacks when a listed
// company has no SVG in the global ticker catalogue. Symbols keep their
// Yahoo exchange suffix to avoid similarly named companies on other markets.
const stockDomainByYahooTicker = Object.freeze({
  'VIT-B.ST':'vitecsoftware.com', 'BONEX.ST':'bonesupport.com',
  'RAY-B.ST':'raysearchlabs.com', 'DYVOX.ST':'dynavoxgroup.com',
  'IVSO.ST':'invisio.com', 'IVX.DE':'invision.de',
  'G24.DE':'scout24.com', 'WKL.AS':'wolterskluwer.com',
  'WRT1V.HE':'wartsila.com', 'ALV.DE':'allianz.com',
  'RMS.PA':'hermes.com', 'IKS.NS':'ikshealth.com', CPRT:'copart.com'
});
const stockDomainByName = new Map([
  ['Vitec Software','vitecsoftware.com'],['Bonesupport','bonesupport.com'],
  ['Raysearch Laboratories','raysearchlabs.com'],['Dynavox Group','dynavoxgroup.com'],
  ['Scout24','scout24.com'],['Wolters Kluwer','wolterskluwer.com'],
  ['Wolters Kluver','wolterskluwer.com'],['Wärtsilä','wartsila.com'],
  ['Wartsila','wartsila.com'],['Allianz','allianz.com'],
  ['Hermès','hermes.com'],['Hermes','hermes.com'],['Copart','copart.com'],
  ['Inventurus Knowledge Solutions','ikshealth.com'],['IKS','ikshealth.com']
].map(([name,domain])=>[normalizeCompanyName(name),domain]));
function globalStockLogo(market,symbol){
  if(!market||!symbol)return '';
  // The catalogue uses TradingView's underscore class-share convention,
  // rather than Yahoo's dash notation (VIT-B.ST -> sweden/VIT_B.svg).
  const safeSymbol=symbol.toUpperCase().replace(/[-.]/g,'_');
  return `${stockLogoCdn}${catalogMarket(market)}/${encodeURIComponent(safeSymbol)}.svg`;
}
function localLogoForHolding(holding){
  const ticker=String(holding.ticker||'').trim().toUpperCase();
  if(ticker && localLogoByTicker[ticker])return localLogoByTicker[ticker];
  const normalizedName=normalizeCompanyName(holding.asset);
  return localLogoByName.get(normalizedName)||'';
}

function logoFitHint(holding){
  const asset=String(holding.asset||'').trim().toUpperCase();
  const ticker=String(holding.ticker||'').trim().toUpperCase();
  // A few marks need extra breathing room on a white plate. Apply this by
  // brand category rather than using local hardcoded image files.
  if(holding.type==='Crypto')return 'contain';
  if(/(^|\b)(META|FACEBOOK)(\b|$)/.test(asset) || ticker==='META')return 'contain';
  if(/(^|\b)(CHAINLINK|LINK)(\b|$)/.test(asset) || /(^|[.:\-])LINK($|[.:\-])/.test(ticker))return 'contain';
  if(/(^|\b)(BITCOIN|BTC|XBT)(\b|$)/.test(asset) || /(^|[.:\-])(BTC|XBT)($|[.:\-=])/.test(ticker))return 'contain';
  return '';
}
function logoSources(holding){
  const asset=String(holding.asset||'');
  const type=holding.type;
  const exactLocalLogo=localLogoForHolding(holding);
  if(type==='Crypto'){
    const coin=cryptoLogoSymbol(holding);
    return coin?[
      `https://logoapi.sh/crypto/${encodeURIComponent(coin)}.svg`,
      `https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/128/color/${encodeURIComponent(coin.toLowerCase())}.png`
    ]:[];
  }
  const parsed=parseStockTicker(holding.ticker)||parseStockTicker(asset);
  const regional=regionalStockMatch(holding);
  const urls=[];
  if(type==='Stock'||type==='ETF'||type==='Fund'){
    if(exactLocalLogo)urls.push(exactLocalLogo);
    if(type==='ETF'||type==='Fund'){
      // A fund's issuer is a better identifier than an ambiguous short ticker.
      for(const domain of fundIssuerDomains(asset,holding.ticker))
        urls.push(...domainLogoSources(domain));
    }
    if(parsed?.market){
      urls.push(globalStockLogo(parsed.market,parsed.symbol));
    }else if(parsed?.symbol){
      // Unqualified symbols are normally US-listed. Never reach this branch
      // for a recognized Yahoo exchange suffix.
      if(regional?.market)urls.push(globalStockLogo(regional.market,regional.symbol));
      else urls.push(globalStockLogo('us',parsed.symbol));
    }else if(regional){
      urls.push(globalStockLogo(regional.market,regional.symbol));
    }
    const knownDomain=stockDomainByYahooTicker[String(holding.ticker||'').trim().toUpperCase()]
      ||regional?.domain||stockDomainByName.get(normalizeCompanyName(asset));
    if(knownDomain)urls.push(...domainLogoSources(knownDomain));
    if(parsed?.symbol && !parsed.market){
      // The unkeyed ticker endpoint covers US stocks including Copart.
      urls.push(`https://logoapi.sh/ticker/${encodeURIComponent(parsed.symbol)}.svg?fallback=none`);
    }
    // The on-error name search is a last resort, not the first choice.
  }
  return [...new Set(urls.filter(Boolean))];
}
function logo(holding){
  const asset=String(holding.asset||'');
  const type=holding.type;
  const [bg,fg]=palette[type]||['#25344e','#78bdf0'];
  const icon=type==='Crypto'?'₿':asset.trim().toUpperCase().slice(0,2);
  const urls=logoSources(holding);
  const shouldLookupName=(type==='Stock'||type==='Fund'||type==='ETF')&&asset.trim().length>=2;
  const fitHint=logoFitHint(holding);
  const image=(urls.length||shouldLookupName)
    ?`<img class="asset-logo-image" ${urls.length?`src="${html(urls[0])}"`:''} alt="" aria-hidden="true" loading="lazy" decoding="async" data-logo-next="${html(JSON.stringify(urls.slice(1)))}" ${fitHint?`data-logo-fit-hint="${html(fitHint)}"`:''} ${shouldLookupName?`data-logo-search="${html(asset)}" data-logo-ticker="${html(holding.ticker||'')}"`:''}>`
    :'';
  return `<span class="asset-icon" style="background:${bg};color:${fg}" aria-hidden="true">${html(icon)}${image}</span>`;
}
const stockLogoSearchCache=new Map();
function normalizeCompanyName(name){
  return String(name||'').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g,'')
    .replace(/\b(?:incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings|holding|class|ab|publ|public|ser|series)\b/g,'')
    .replace(/[^a-z0-9]/g,'');
}
async function findStockLogoByName(name,ticker){
  const query=String(name||'').trim().replace(/\s+(?:inc\.?|corp\.?|corporation|ltd\.?|plc|ab|\(publ\))\s*$/i,'').trim();
  if(query.length<2||query.length>80)return '';
  const key=query.toLowerCase()+'|'+String(ticker||'').toUpperCase();
  if(!stockLogoSearchCache.has(key)){
    const request=(async()=>{
      try{
        const response=await fetch(`https://logoapi.sh/search?q=${encodeURIComponent(query)}`);
        if(!response.ok)return '';
        const results=(await response.json()).results||[];
        const wanted=normalizeCompanyName(query);
        const parsed=parseStockTicker(ticker);
        const symbol=parsed?.symbol?.replace(/[-.]/g,'').toUpperCase()||'';
        const found=results.find(item=>{
          if(!item||item.fuzzy||!/^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/.test(item.domain||''))return false;
          // Only accept a solid name match or a matching ticker AND exchange.
          const clean=normalizeCompanyName(item.name);
          const sameName=clean===wanted || (wanted.length>5 && clean.length>5 &&
            (clean.startsWith(wanted)||wanted.startsWith(clean)));
          const sameTicker=symbol && String(item.ticker||'').replace(/[-.]/g,'').toUpperCase()===symbol;
          // Without exchange verification, a coincident ticker can be a
          // different company on another continent.
          return sameName || (!parsed?.market && sameTicker);
        });
        return found?`https://logoapi.sh/icon/${found.domain}.svg?fallback=404`:'';
      }catch(_){return '';}
    })();
    stockLogoSearchCache.set(key,request);
  }
  return stockLogoSearchCache.get(key);
}
async function tryNameLogo(img){
  const name=img.dataset.logoSearch;
  delete img.dataset.logoSearch; // One lookup maximum per rendered icon.
  if(!name)return img.remove();
  const url=await findStockLogoByName(name,img.dataset.logoTicker);
  if(img.isConnected&&url){img.src=url;return;}
  img.remove(); // The original icon text stays exactly as before.
}
function activatePendingLogoSearch(target){
  target.querySelectorAll('.asset-logo-image:not([src])').forEach(img=>tryNameLogo(img));
}
function bindAssetLogoImages(){
  document.addEventListener('load',event=>{
    const img=event.target;
    if(img.classList?.contains('asset-logo-image')){
      const host=img.parentElement;
      if(!host)return;
      const ratio=(img.naturalWidth||1)/(img.naturalHeight||1);
      img.classList.toggle('is-wordmark',ratio>1.35||ratio<0.74);
      img.classList.toggle('is-square-logo',ratio>=0.92&&ratio<=1.08);
      img.classList.toggle('is-contained',img.dataset.logoFitHint==='contain');
      host.classList.add('has-logo');
    }
  },true);
  document.addEventListener('error',event=>{
    const img=event.target;
    if(!img.classList?.contains('asset-logo-image'))return;
    let next=[];
    try{next=JSON.parse(img.dataset.logoNext||'[]');}catch(_){}
    if(next.length){img.parentElement?.classList.remove('has-logo');img.dataset.logoNext=JSON.stringify(next.slice(1));img.src=next[0];}
    else tryNameLogo(img);
  },true);
}


function sortedHomeHoldings(holdings){
  const list = [...holdings];
  if(state.homeSort === 'pnl'){
    list.sort((a,b)=>{
      const pa = a.invested ? (a.pnl||0)/a.invested : -Infinity;
      const pb = b.invested ? (b.pnl||0)/b.invested : -Infinity;
      return pb - pa || (b.value||0) - (a.value||0);
    });
  } else if(state.homeSort === 'name'){
    list.sort((a,b)=>String(a.asset||'').localeCompare(String(b.asset||'')));
  }
  return list;
}

function currentHistorySeries(mode=state.dashboardMode, span=state.historyRange){
  return state.history[mode]?.[span] ?? null;
}

function dashboardModeMeta(mode=state.dashboardMode){
  const d = state.dashboard;
  if(!d) return { title:'INVESTMENTS', value:null, invested:0, pnl:0, realized_total:0, hasData:false };
  if(mode === 'cash'){
    const ct = d.cash?.totals || {};
    return {
      title:'CASH',
      value:Number(ct.value)||0,
      invested:Number(ct.invested)||0,
      pnl:Number(ct.pnl)||0,
      realized_total:Number(ct.realized_total)||0,
      hasData:(d.cash_count||0) > 0
    };
  }
  if(mode === 'nav'){
    const nt = d.nav || {};
    return {
      title:'NET ASSET VALUE',
      value:Number(nt.value)||0,
      invested:Number(nt.invested)||0,
      pnl:Number(nt.pnl)||0,
      realized_total:Number(nt.realized_total)||0,
      hasData:((d.trades_count||0) + (d.cash_count||0)) > 0
    };
  }
  const t = d.totals || {};
  return {
    title:'INVESTMENTS',
    value:Number(t.value)||0,
    invested:Number(t.invested)||0,
    pnl:Number(t.pnl)||0,
    realized_total:Number(d.realized_total)||0,
    hasData:(d.trades_count||0) > 0
  };
}

function renderCashBalancesCard(){
  const target = $('main-cash-balances');
  if(!target || !state.dashboard) return;
  const base = state.dashboard.base_currency || 'SEK';
  const balances = state.dashboard.cash?.balances || [];
  if(!balances.length){
    target.innerHTML = emptyMessage('No cash balances yet', 'Add a Cash transaction or upload your cash_txns.csv in Account.', null);
    return;
  }
  target.innerHTML = balances.map(b=>{
    const ccy = b.currency;
    const name = b.name || CURRENCY_NAMES[ccy] || ccy;
    const nativeText = fmtCashNative(b.amount, ccy, 2);
    const baseText = b.is_base ? '' : `<span class="cash-base-eq">${html(fmt(b.value, base, 2))}</span>`;
    return `<div class="holding-row cash-row">${currencyFlag(ccy)}<div class="holding-info"><div class="holding-name">${html(ccy)}</div><div class="holding-subtitle">${html(name)}</div></div><div class="holding-value"><strong>${html(nativeText)}</strong>${baseText}</div></div>`;
  }).join('');
}

function renderDashboard(){
  const d=state.dashboard;if(!d)return;
  const base=d.base_currency;
  const meta=dashboardModeMeta(state.dashboardMode);
  const titleEl=$('hero-title');
  if(titleEl) titleEl.textContent=meta.title;
  $('total-value').textContent=fmt(meta.value,base,0);
  $('hero-ccy').textContent=base;
  renderDevelopmentHeadline(currentHistorySeries());
  renderCashBalancesCard();
  $('holdings-badge').textContent=d.holdings.length;
  const holdings = sortedHomeHoldings(d.holdings);
  $('main-holdings').innerHTML=holdings.length?holdings.map(h=>{
    const pnlPct = h.invested ? 100*(h.pnl||0)/h.invested : 0;
    return `<div class="holding-row home-holding-row">${logo(h)}<div class="holding-info"><div class="holding-name">${html(h.asset)}</div><div class="holding-subtitle">Qty ${state.hidden?'••••':quantityFmt(h.quantity)}</div></div><div class="holding-value"><strong>${html(fmt(h.value,base,0))}</strong><span class="${(h.pnl||0)<0?'negative':''}">${html(signedPct(pnlPct))}</span></div></div>`;
  }).join(''):emptyMessage('Nothing in your portfolio yet','Add a trade or upload your existing trades CSV.','trade');
  activatePendingLogoSearch($('main-holdings'));
}

function emptyMessage(title,desc,action){return `<div class="empty"><strong>${html(title)}</strong>${html(desc)}${action?`<button type="button" data-go="${action}">Get started →</button>`:''}</div>`;}

function setDashboardMode(mode){
  if(!['investments','cash','nav'].includes(mode)) return;
  if(state.dashboardMode === mode) return;
  endChartScrub();
  state.dashboardMode = mode;
  document.querySelectorAll('[data-dash-mode]').forEach(btn=>{
    const active = btn.dataset.dashMode === mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
  renderDashboard();
  loadRange(state.historyRange);
}

function setAnalyticsMode(mode){
  if(!['investments','cash','nav'].includes(mode)) return;
  state.analyticsMode = mode;
  document.querySelectorAll('[data-analytics-mode]').forEach(btn=>{
    const active = btn.dataset.analyticsMode === mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', String(active));
  });
  for(const m of ['investments','cash','nav']){
    $('analytics-pane-'+m)?.classList.toggle('hidden', m !== mode);
  }
  if(state.dashboard){
    if(mode==='investments'){ renderInvestmentAnalytics(); setTimeout(()=>fitCapitalTileLabels($('capital-chart')),30); }
    else if(mode==='cash'){ renderCashAnalytics(); setTimeout(()=>state.cashDonutChart?.resize(),30); }
    else { renderNavAnalytics(); setTimeout(()=>state.navDonutChart?.resize(),30); }
  }
}

function renderInvestmentAnalytics(){
  const d=state.dashboard;if(!d)return;
  $('analytics-n').textContent=`${d.holdings.length} assets`;
  $('analytics-holdings').innerHTML=d.holdings.length?d.holdings.map(h=>{
    const neg = (Number(h.pnl_trade) || 0) < 0;
    const pctText = state.hidden
      ? '(••••)'
      : `(${neg?'▼':'▲'}${compact(Math.abs(Number(h.pnl_trade_pct)||0),2)}%)`;
    return `<div class="analytics-entry"><div class="analytics-row-top"><div class="analytics-identity">${logo(h)}<div class="analytics-title-group"><div class="holding-name">${html(h.asset)}</div><div class="analytics-ltp">${html(fmt(h.ltp,h.currency,2))}</div></div></div><div class="analytics-meta-right"><div>Avg. ${html(fmt(h.avg,h.currency,2))}</div><div>Qty ${state.hidden?'••••':quantityFmt(h.quantity)}</div></div></div><div class="analytics-row-metrics"><div class="analytics-metric"><span>Invested Value</span><strong>${html(fmt(h.invested_trade,h.currency,2))}</strong></div><div class="analytics-metric analytics-metric-mid"><span>Current Value</span><strong>${html(fmt(h.value_trade,h.currency,2))}</strong></div><div class="analytics-metric analytics-metric-end"><span class="analytics-pnl-pct ${neg?'negative':''}">${html(pctText)}</span><strong class="analytics-pnl-val ${neg?'negative':''}">${html(signedFmt(h.pnl_trade,h.currency,2))}</strong></div></div></div>`;
  }).join(''):emptyMessage('No investments found','Your holdings appear here after you add trades.','trade');
  activatePendingLogoSearch($('analytics-holdings'));
  renderCapitalChart();
}

function renderCashAnalytics(){
  const d=state.dashboard;if(!d)return;
  const base=d.base_currency || 'SEK';
  const cash=d.cash || { balances:[], totals:{ value:0, invested:0, pnl:0, pnl_pct:0, currencies:0 } };
  const balances=cash.balances || [];
  const totals=cash.totals || {};

  $('cash-analytics-total').textContent = fmt(totals.value || 0, base, 2);
  const pnlEl = $('cash-analytics-pnl');
  const pnlVal = Number(totals.pnl) || 0;
  pnlEl.textContent = signedFmt(pnlVal, base, 2);
  pnlEl.classList.toggle('negative', pnlVal < -0.005);
  pnlEl.classList.toggle('positive', pnlVal >= 0.005);

  const countLabel = `${balances.length} ${balances.length===1?'currency':'currencies'}`;
  $('cash-exposure-count').textContent = countLabel;
  $('cash-detail-n').textContent = countLabel;

  // Currency Exposure Donut + List
  const chartEl = $('cash-exposure-chart');
  const listEl = $('cash-exposure-list');
  const positiveBalances = balances.filter(b => (Number(b.value)||0) > 0);
  if(!positiveBalances.length){
    if(state.cashDonutChart){ state.cashDonutChart.dispose(); state.cashDonutChart=null; }
    chartEl.innerHTML = '<div class="empty" style="margin:8px">No positive cash exposure yet.</div>';
    listEl.innerHTML = '';
  } else {
    const dark = state.theme === 'dark';
    const brightPalette = dark
      ? ['#58e3fb','#68d8ad','#fbbf24','#a78bfa','#fb7185','#38bdf8','#34d399','#f472b6','#fb923c','#818cf8','#2dd4bf','#facc15']
      : ['#1b7fb3','#168065','#d97706','#7c3aed','#e11d48','#0284c7','#059669','#db2777','#ea580c','#4f46e5','#0d9488','#ca8a04'];
    const items = positiveBalances.map((b, i) => ({
      ...b,
      color: brightPalette[i % brightPalette.length]
    }));
    if(typeof echarts !== 'undefined'){
      if(state.cashDonutChart && !chartEl.querySelector('canvas')){
        state.cashDonutChart.dispose(); state.cashDonutChart=null; chartEl.innerHTML='';
      }
      if(!state.cashDonutChart) state.cashDonutChart = echarts.init(chartEl);
      state.cashDonutChart.setOption({
        animationDuration: 300,
        tooltip: {
          trigger: 'item',
          confine: true,
          backgroundColor: dark ? 'rgba(20,24,31,.96)' : 'rgba(248,245,239,.97)',
          borderWidth: 1,
          borderColor: dark ? 'rgba(255,255,255,.12)' : 'rgba(38,32,22,.12)',
          textStyle: { color: dark ? '#eef4fc' : '#1f1d1a', fontSize: 13 },
          formatter: (p) => `<b>${html(p.name)}</b><br>${html(fmt(p.value, base, 2))} (${state.hidden ? '••••' : compact(p.percent, 1) + '%'})`
        },
        series: [{
          type: 'pie',
          radius: ['58%', '84%'],
          center: ['50%', '50%'],
          avoidLabelOverlap: true,
          itemStyle: {
            borderColor: dark ? '#12151b' : '#f8f5ef',
            borderWidth: 2
          },
          label: { show: false },
          emphasis: { scale: true, scaleSize: 4 },
          data: items.map(it => ({
            name: it.currency,
            value: Math.round(it.value * 100) / 100,
            itemStyle: { color: it.color }
          }))
        }]
      }, true);
      state.cashDonutChart.resize();
    }
    listEl.innerHTML = items.map(it => {
      const shareStr = state.hidden ? '••••' : compact(it.share, 1) + '%';
      return `<div class="exposure-row"><span class="exposure-dot" style="background:${it.color}"></span>${currencyFlag(it.currency, true)}<div class="exposure-info"><strong>${html(it.currency)}</strong><span>${html(it.name || CURRENCY_NAMES[it.currency] || it.currency)}</span></div><div class="exposure-vals"><strong>${html(shareStr)}</strong><span>${html(fmt(it.value, base, 2))}</span></div></div>`;
    }).join('');
  }

  // Currencies in Detail
  $('cash-detail-list').innerHTML = balances.length ? balances.map(b => {
    const neg = (Number(b.pnl) || 0) < -0.005;
    const avgTxt = state.hidden ? '••••' : compact(b.avg_cost, 4);
    const fxTxt = b.fx_rate != null ? (state.hidden ? '••••' : compact(b.fx_rate, 4)) : '—';
    return `<div class="analytics-entry cash-detail-entry"><div class="analytics-row-top"><div class="analytics-identity">${currencyFlag(b.currency)}<div class="analytics-title-group"><div class="holding-name">${html(b.currency)}</div><div class="analytics-ltp">${html(b.name || CURRENCY_NAMES[b.currency] || b.currency)}</div></div></div><div class="cash-detail-right"><strong>${html(fmtCashNative(b.amount, b.currency, 2))}</strong><span>${html(fmt(b.value, base, 2))}</span></div></div><div class="analytics-row-metrics"><div class="analytics-metric"><span>Avg Cost (${html(base)})</span><strong>${html(avgTxt)}</strong></div><div class="analytics-metric analytics-metric-mid"><span>Current FX (${html(base)})</span><strong>${html(fxTxt)}</strong></div><div class="analytics-metric analytics-metric-end"><span>Unrealized P/L</span><strong class="analytics-pnl-val ${neg?'negative':''}">${html(signedFmt(b.pnl, base, 2))}</strong></div></div></div>`;
  }).join('') : emptyMessage('No cash currencies yet', 'Add a Cash transaction or upload cash_txns.csv in Account.', 'trade');
}

function renderNavAnalytics(){
  const d=state.dashboard;if(!d)return;
  const base=d.base_currency || 'SEK';
  const nav=d.nav || { value:0, invested:0, pnl:0, pnl_pct:0, realized_total:0, investments_value:0, cash_value:0, investments_share:0, cash_share:0 };
  const invT=d.totals || { value:0, invested:0, pnl:0 };
  const cashT=d.cash?.totals || { value:0, invested:0, pnl:0, realized_total:0 };
  const dark = state.theme === 'dark';
  const invColor = dark ? '#58e3fb' : '#1b7fb3';
  const cashColor = dark ? '#68d8ad' : '#168065';

  $('nav-donut-total').textContent = fmt(nav.value, base, 0);
  $('nav-breakdown-ccy').textContent = base;

  const chartEl = $('nav-allocation-chart');
  const invVal = Math.max(0, Number(nav.investments_value)||0);
  const cashVal = Math.max(0, Number(nav.cash_value)||0);
  const hasAny = (invVal + cashVal) > 0;

  if(typeof echarts !== 'undefined'){
    if(state.navDonutChart && !chartEl.querySelector('canvas')){
      state.navDonutChart.dispose(); state.navDonutChart=null; chartEl.innerHTML='';
    }
    if(!state.navDonutChart) state.navDonutChart = echarts.init(chartEl);
    const pieData = hasAny ? [
      { name:'Investments', value:Math.round(invVal*100)/100, itemStyle:{ color:invColor } },
      { name:'Cash', value:Math.round(cashVal*100)/100, itemStyle:{ color:cashColor } }
    ] : [
      { name:'Empty', value:1, itemStyle:{ color: dark ? '#242934' : '#e2dacb' } }
    ];
    state.navDonutChart.setOption({
      animationDuration: 300,
      tooltip: hasAny ? {
        trigger:'item', confine:true,
        backgroundColor: dark ? 'rgba(20,24,31,.96)' : 'rgba(248,245,239,.97)',
        borderWidth: 1,
        borderColor: dark ? 'rgba(255,255,255,.12)' : 'rgba(38,32,22,.12)',
        textStyle: { color: dark ? '#eef4fc' : '#1f1d1a', fontSize: 13 },
        formatter: (p) => `<b>${html(p.name)}</b><br>${html(fmt(p.value, base, 0))} (${state.hidden ? '••••' : compact(p.percent, 1) + '%'})`
      } : { show:false },
      series: [{
        type:'pie',
        radius:['68%', '92%'],
        center:['50%', '50%'],
        startAngle: 90,
        clockwise: true,
        itemStyle:{
          borderColor: dark ? '#12151b' : '#f8f5ef',
          borderWidth: 2
        },
        label:{ show:false },
        emphasis:{ scale: hasAny, scaleSize: 3 },
        data: pieData
      }]
    }, true);
    state.navDonutChart.resize();
  }

  $('nav-allocation-legend').innerHTML = `
    <div class="nav-legend-item">
      <div class="nav-legend-left">
        <span class="nav-legend-dot" style="background:${invColor}"></span>
        <div><strong>Investments</strong><span>${html(fmt(nav.investments_value, base, 0))}</span></div>
      </div>
      <span class="nav-legend-pct">${state.hidden ? '••••' : compact(nav.investments_share, 1) + '%'}</span>
    </div>
    <div class="nav-legend-item">
      <div class="nav-legend-left">
        <span class="nav-legend-dot" style="background:${cashColor}"></span>
        <div><strong>Cash</strong><span>${html(fmt(nav.cash_value, base, 0))}</span></div>
      </div>
      <span class="nav-legend-pct">${state.hidden ? '••••' : compact(nav.cash_share, 1) + '%'}</span>
    </div>`;

  const rows = [
    {
      title:'Investments',
      subtitle:`${d.holdings.length} open ${d.holdings.length===1?'holding':'holdings'}`,
      dot:invColor,
      value:invT.value||0,
      cost:invT.invested||0,
      unrealized:invT.pnl||0,
      realized:d.realized_total||0
    },
    {
      title:'Cash',
      subtitle:`${d.cash?.balances?.length||0} active ${(d.cash?.balances?.length||0)===1?'currency':'currencies'}`,
      dot:cashColor,
      value:cashT.value||0,
      cost:cashT.invested||0,
      unrealized:cashT.pnl||0,
      realized:cashT.realized_total||0
    },
    {
      title:'Total Net Asset Value',
      subtitle:'Investments + Cash',
      dot: dark ? '#eef4fc' : '#1f1d1a',
      value:nav.value||0,
      cost:nav.invested||0,
      unrealized:nav.pnl||0,
      realized:nav.realized_total||0
    }
  ];
  $('nav-breakdown-list').innerHTML = rows.map(r => {
    const uNeg = r.unrealized < -0.5;
    const rNeg = r.realized < -0.5;
    return `<div class="analytics-entry nav-breakdown-entry"><div class="analytics-row-top"><div class="analytics-identity"><span class="nav-breakdown-dot" style="background:${r.dot}"></span><div class="analytics-title-group"><div class="holding-name">${html(r.title)}</div><div class="analytics-ltp">${html(r.subtitle)}</div></div></div><div class="cash-detail-right"><strong>${html(fmt(r.value, base, 0))}</strong></div></div><div class="analytics-row-metrics"><div class="analytics-metric"><span>Cost Basis</span><strong>${html(fmt(r.cost, base, 0))}</strong></div><div class="analytics-metric analytics-metric-mid"><span>Unrealized P/L</span><strong class="analytics-pnl-val ${uNeg?'negative':''}">${html(signedFmt(r.unrealized, base, 0))}</strong></div><div class="analytics-metric analytics-metric-end"><span>Realized P/L</span><strong class="analytics-pnl-val ${rNeg?'negative':''}">${html(signedFmt(r.realized, base, 0))}</strong></div></div></div>`;
  }).join('');
}

function renderAnalytics(){
  if(!state.dashboard)return;
  if(state.analyticsMode === 'cash') renderCashAnalytics();
  else if(state.analyticsMode === 'nav') renderNavAnalytics();
  else renderInvestmentAnalytics();
}

// The backend already converts every holding's `value` into the account's base currency.
// Sum by asset rather than trade currency, then calculate shares of invested assets.
function capitalAllocation(holdings){
  const totals=new Map();
  for(const h of holdings){
    const value=Number(h.value);
    if(!Number.isFinite(value)||value<=0)continue;
    const name=String(h.asset||'Unnamed asset').trim()||'Unnamed asset';
    totals.set(name,(totals.get(name)||0)+value);
  }
  const sum=[...totals.values()].reduce((a,b)=>a+b,0);
  return [...totals].map(([name,value])=>({id:name,name,value,share:100*value/sum}))
    .sort((a,b)=>b.value-a.value || a.name.localeCompare(b.name))
    .map((item,i)=>({...item,color:capitalColors[i%capitalColors.length]}));
}
function toggleCapitalFocus(id){
  state.capitalFocus=state.capitalFocus===id?null:id;
  renderCapitalChart(true);
}

// Balanced binary treemap. Rectangle areas stay proportional to base-currency values;
// actual HTML buttons keep the names accessible even in very small allocations.
function capitalRectangles(items,x=0,y=0,w=100,h=100){
  if(!items.length)return [];
  if(items.length===1)return [{...items[0],x,y,w,h}];
  const total=items.reduce((acc,item)=>acc+item.value,0);
  let first=0,split=1,best=Infinity;
  for(let i=1;i<items.length;i++){
    first+=items[i-1].value;
    const delta=Math.abs(total/2-first);
    if(delta<best){best=delta;split=i;}
  }
  const ratio=items.slice(0,split).reduce((sum,item)=>sum+item.value,0)/total;
  if(w>=h)return [
    ...capitalRectangles(items.slice(0,split),x,y,w*ratio,h),
    ...capitalRectangles(items.slice(split),x+w*ratio,y,w*(1-ratio),h)
  ];
  return [
    ...capitalRectangles(items.slice(0,split),x,y,w,h*ratio),
    ...capitalRectangles(items.slice(split),x,y+h*ratio,w,h*(1-ratio))
  ];
}
function fitCapitalTileLabels(target){
  if(!target)return;
  target.querySelectorAll('.capital-tile:not(.is-focused)').forEach(tile=>{
    const width=tile.clientWidth, height=tile.clientHeight;
    if(!width||!height)return;
    const micro=width<43||height<30;
    const compact=width<115||height<82;
    const pad=micro?2:compact?4:8;
    const innerWidth=Math.max(1,width-2*pad-4);
    const innerHeight=Math.max(1,height-2*pad-4);
    tile.classList.toggle('is-micro',micro);
    tile.classList.toggle('is-compact',compact&&!micro);
    tile.style.setProperty('--tile-pad',pad+'px');
    const name=tile.querySelector('.capital-name');
    const percentage=tile.querySelector('.capital-share');
    const nameLength=name.textContent.length;
    // Labels stay restrained and scale down only when the block needs it.
    // On exceptionally small tiles, a short name remains visible without a percentage.
    const nameLines=(!micro&&innerWidth>=40&&innerHeight>=36)?
      (innerHeight>=100?3:2):1;
    const charsPerLine=nameLines>1?Math.max(5,Math.ceil(nameLength/nameLines)):Math.min(nameLength,13);
    const nameSize=Math.max(7,Math.min(13.2,innerWidth/(Math.max(1,charsPerLine)*.53),innerHeight/(nameLines*1.22)));
    tile.style.setProperty('--tile-name-size',nameSize.toFixed(2)+'px');
    tile.style.setProperty('--tile-name-lines',String(nameLines));
    const shareSize=Math.max(7.5,Math.min(12,nameSize*.91));
    tile.style.setProperty('--tile-share-size',shareSize.toFixed(2)+'px');
    const actualNameHeight=name.getBoundingClientRect().height;
    percentage.hidden=innerWidth<30||innerHeight<actualNameHeight+shareSize*1.18+3;
  });
}
function renderCapitalTiles(target,items,focused){
  const rectangles=capitalRectangles(items);
  target.innerHTML=`<div class="capital-fallback">${rectangles.map(item=>{
    const share=state.hidden?'••••':compact(item.share,1)+'%';
    const value=state.hidden?'••••••':fmt(item.value,state.dashboard.base_currency,0);
    return `<button type="button" class="capital-tile ${focused?'is-focused':''}" data-capital-asset="${html(item.id)}"
      style="left:${item.x}%;top:${item.y}%;width:${item.w}%;height:${item.h}%;background:${item.color};--tile-ink:${capitalInk(item.color)}"
      title="${html(item.name)} · ${html(share)}${focused?' · '+html(value):''}"
      aria-label="${html(item.name)}: ${html(share)} of capital. ${focused?'Show all assets':'Expand asset'}">
      <strong class="capital-name">${html(item.name)}</strong>
      ${focused?`<span class="capital-value">${html(value)}</span>`:''}
      <span class="capital-share">${html(share)}</span>
    </button>`;
  }).join('')}</div>`;
  fitCapitalTileLabels(target);
}
// FLIP (first/last/invert/play) gives an actual short tile expansion rather
// than the previous instant DOM replacement. No animation on data refresh.
function animateCapitalTiles(target,fromRects,fromWasFocused,focusId){
  if(!fromRects.size||!target.isConnected||!target.offsetWidth||
     !Element.prototype.animate||window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
  const duration=270;
  const easing='cubic-bezier(.22, 1, .36, 1)';
  const active=focusId||([...fromRects.keys()].length===1&&fromWasFocused?
    [...fromRects.keys()][0]:null);
  if(!active)return;
  const currentLayout=target.querySelector('.capital-fallback:not(.capital-transition-overlay)');
  const tile=[...(currentLayout?.querySelectorAll('.capital-tile')||[])]
    .find(el=>el.dataset.capitalAsset===active);
  const old=fromRects.get(active);
  if(!tile||!old)return;
  const next=tile.getBoundingClientRect();
  if(!next.width||!next.height||!old.width||!old.height)return;
  const tx=old.left-next.left, ty=old.top-next.top;
  const sx=old.width/next.width, sy=old.height/next.height;
  tile.style.zIndex='3';
  const a=tile.animate([
    {transform:`translate(${tx}px, ${ty}px) scale(${sx}, ${sy})`},
    {transform:'translate(0, 0) scale(1, 1)'}
  ],{duration,easing,fill:'none'});
  a.finished.catch(()=>{}).then(()=>{if(tile.isConnected)tile.style.zIndex='';});
  if(!focusId){
    currentLayout?.querySelectorAll('.capital-tile').forEach(other=>{
      if(other!==tile)other.animate([{opacity:0},{opacity:1}],
        {duration:220,easing,fill:'none'});
    });
  }
}
function renderCapitalChart(animate=false){
  const target=$('capital-chart');
  if(!target||!state.dashboard)return;
  // Capture current screen geometry BEFORE replacing the HTML tiles.
  const previousFocused=!!target.querySelector('.capital-tile.is-focused');
  const oldLayout=animate?target.querySelector('.capital-fallback:not(.capital-transition-overlay)'):null;
  const oldSnapshot=oldLayout?.cloneNode(true)||null;
  const previousRects=new Map();
  if(animate)oldLayout?.querySelectorAll('.capital-tile').forEach(tile=>
    previousRects.set(tile.dataset.capitalAsset,tile.getBoundingClientRect()));
  const data=capitalAllocation(state.dashboard.holdings||[]);
  if(state.capitalFocus&&!data.some(item=>item.id===state.capitalFocus))state.capitalFocus=null;
  const focused=state.capitalFocus!==null;
  const visible=focused?data.filter(item=>item.id===state.capitalFocus):data;
  $('capital-reset').classList.toggle('hidden',!focused);
  $('capital-scope').textContent=`${data.length} ${data.length===1?'asset':'assets'} · Values in ${state.dashboard.base_currency}`;
  $('capital-hint').textContent=focused?'Select the tile again to return':'Select a tile to expand';
  // The portfolio history chart still uses ECharts; the capital chart deliberately
  // uses HTML tiles so ECharts cannot silently remove labels on narrow blocks.
  if(state.capitalChart){state.capitalChart.dispose();state.capitalChart=null;}
  if(!data.length){
    target.innerHTML='<div class="empty capital-empty">No capital allocation to display yet.</div>';
    return;
  }
  renderCapitalTiles(target,visible,focused);
  if(animate && oldSnapshot && !window.matchMedia('(prefers-reduced-motion: reduce)').matches){
    // Keep the old tiles faintly underneath the growing tile for continuity.
    oldSnapshot.classList.add('capital-transition-overlay');
    oldSnapshot.setAttribute('aria-hidden','true');
    oldSnapshot.querySelectorAll('button').forEach(el=>{el.tabIndex=-1;el.disabled=true;});
    oldSnapshot.style.cssText='position:absolute;inset:0;z-index:0;pointer-events:none';
    target.prepend(oldSnapshot);
    const fresh=target.querySelector('.capital-fallback:not(.capital-transition-overlay)');
    if(fresh)fresh.style.zIndex='1';
    if(Element.prototype.animate){
      const fade=oldSnapshot.animate([{opacity:1},{opacity:0}],
        {duration:260,easing:'ease-out',fill:'forwards'});
      fade.finished.catch(()=>{}).then(()=>oldSnapshot.remove());
    }else oldSnapshot.remove();
  }
  if(animate)animateCapitalTiles(target,previousRects,previousFocused,state.capitalFocus);
}


function computeSpanDevelopment(series){
  const d=state.dashboard;
  if(!d)return null;
  const meta=dashboardModeMeta(series?.mode || state.dashboardMode);
  const invested=meta.invested;
  const rawPoints=series?.points||[];
  if(!rawPoints.length){
    if(!meta.hasData)return {value:0,pct:0,points:[]};
    return null;
  }
  const hasBaseline=series && typeof series.baseline==='number';
  const liveCumDev=meta.pnl + (Number(meta.realized_total ?? series.realized_total)||0);
  const devValue=hasBaseline ? (liveCumDev - series.baseline) : (Number(series.development ?? rawPoints[rawPoints.length-1].value)||0);
  const devPct=invested > 1e-9 ? (100 * devValue / invested) : 0;
  const points=rawPoints.map(p=>({
    date:p.date,
    value:Number(p.value)||0,
    portfolio_value:typeof p.portfolio_value==='number' ? p.portfolio_value : null,
    invested:typeof p.invested==='number' ? p.invested : null
  }));
  if(hasBaseline && points.length){
    points[points.length-1].value=Math.round(devValue*100)/100;
    if(typeof meta.value==='number'){
      points[points.length-1].portfolio_value=meta.value;
    }
    if(typeof meta.invested==='number'){
      points[points.length-1].invested=meta.invested;
    }
  }
  return {value:devValue,pct:devPct,points};
}

function formatScrubDate(isoDate){
  if(!isoDate)return '';
  const dt=new Date(isoDate+'T00:00:00');
  if(Number.isNaN(dt.getTime()))return isoDate;
  return dt.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'});
}

function renderDevelopmentHeadline(series, scrubPoint=null){
  const el=$('main-change');
  const totalEl=$('total-value');
  if(!el)return;
  const d=state.dashboard;
  const base=d?.base_currency || state.user?.base_currency || 'SEK';
  const meta=dashboardModeMeta(series?.mode || state.dashboardMode);
  if(scrubPoint && d){
    const invested=scrubPoint.invested!=null
      ? (Number(scrubPoint.invested)||0)
      : meta.invested;
    const val=Number(scrubPoint.value)||0;
    const pct=invested > 1e-9 ? (100 * val / invested) : 0;
    const cleanVal=Math.abs(val)<0.5?0:val;
    const cleanPct=Math.abs(pct)<0.005?0:pct;
    const neg=cleanVal<0;
    const pillText=state.hidden
      ? '•••• (••••••)'
      : `${signedPct(cleanPct)} (${signedAmt(cleanVal,base,0)})`;
    if(totalEl && scrubPoint.portfolio_value!=null){
      totalEl.textContent=fmt(scrubPoint.portfolio_value,base,0);
    }
    el.innerHTML=`<span class="pnl-pill ${neg?'negative':''}">${html(pillText)}</span><span class="muted scrub-date">${html(formatScrubDate(scrubPoint.date))}</span>`;
    return;
  }
  if(totalEl && d){
    totalEl.textContent=fmt(meta.value,base,0);
  }
  const dev=computeSpanDevelopment(series);
  if(!d || !dev){
    el.innerHTML='<span class="pnl-pill">—</span>';
    return;
  }
  const cleanVal=Math.abs(dev.value)<0.5?0:dev.value;
  const cleanPct=Math.abs(dev.pct)<0.005?0:dev.pct;
  const neg=cleanVal<0;
  const pillText=state.hidden
    ? '•••• (••••••)'
    : `${signedPct(cleanPct)} (${signedAmt(cleanVal,base,0)})`;
  el.innerHTML=`<span class="pnl-pill ${neg?'negative':''}">${html(pillText)}</span>`;
}

// Click/touch-and-hold scrubbing controller for the development chart.
const chartScrub = {
  timer:null, active:false, mode:null,
  startX:0, startY:0, lastX:0, lastTouchEnd:0
};

function chartIndexFromClientX(clientX){
  const pts=state.chartPoints;
  if(!pts.length)return -1;
  if(pts.length===1)return 0;
  const target=$('history-chart');
  if(!target)return -1;
  const rect=target.getBoundingClientRect();
  if(!rect.width)return -1;
  const padL=4, padR=5;
  const localX=Math.max(padL, Math.min(rect.width - padR, clientX - rect.left));
  if(state.chart && state.chartTimestamps.length===pts.length){
    try{
      const coord=state.chart.convertFromPixel({seriesIndex:0},[localX, rect.height/2]);
      if(Array.isArray(coord) && Number.isFinite(coord[0])){
        const ts=coord[0], arr=state.chartTimestamps;
        let lo=0, hi=arr.length-1;
        while(lo<hi){
          const mid=(lo+hi)>>1;
          if(arr[mid]<ts)lo=mid+1; else hi=mid;
        }
        if(lo>0 && Math.abs(arr[lo-1]-ts) <= Math.abs(arr[lo]-ts)) return lo-1;
        return lo;
      }
    }catch(_){}
  }
  const plotW=Math.max(1, rect.width - padL - padR);
  const ratio=(localX - padL)/plotW;
  return Math.max(0, Math.min(pts.length-1, Math.round(ratio*(pts.length-1))));
}

function updateChartScrubAt(clientX){
  const idx=chartIndexFromClientX(clientX);
  if(idx<0 || idx===state.chartScrubIndex)return;
  state.chartScrubIndex=idx;
  const pt=state.chartPoints[idx];
  if(!pt)return;
  renderDevelopmentHeadline(currentHistorySeries(), pt);
  if(state.chart){
    try{ state.chart.dispatchAction({type:'showTip',seriesIndex:0,dataIndex:idx}); }catch(_){}
  }
}

function endChartScrub(){
  clearTimeout(chartScrub.timer);
  chartScrub.timer=null;
  const wasActive=chartScrub.active;
  chartScrub.active=false;
  chartScrub.mode=null;
  state.chartScrubIndex=null;
  $('history-chart')?.classList.remove('is-scrubbing');
  if(wasActive){
    if(state.chart){
      try{ state.chart.dispatchAction({type:'hideTip'}); state.chart.dispatchAction({type:'updateAxisPointer',currTrigger:'leave'}); }catch(_){}
    }
    renderDevelopmentHeadline(currentHistorySeries(), null);
  }
}

function bindChartScrub(){
  const target=$('history-chart');
  if(!target || target.dataset.scrubBound)return;
  target.dataset.scrubBound='1';
  const HOLD_MS=180;
  const MOVE_CANCEL_PX=10;

  target.addEventListener('contextmenu',e=>{
    if(chartScrub.active || chartScrub.timer) e.preventDefault();
  });

  target.addEventListener('touchstart',e=>{
    if(e.touches.length!==1 || !state.chartPoints.length)return;
    const t=e.touches[0];
    clearTimeout(chartScrub.timer);
    chartScrub.mode='touch';
    chartScrub.startX=t.clientX;
    chartScrub.startY=t.clientY;
    chartScrub.lastX=t.clientX;
    chartScrub.timer=setTimeout(()=>{
      chartScrub.timer=null;
      if(chartScrub.mode!=='touch' || !state.chartPoints.length)return;
      chartScrub.active=true;
      target.classList.add('is-scrubbing');
      try{ navigator.vibrate?.(10); }catch(_){}
      updateChartScrubAt(chartScrub.lastX);
    }, HOLD_MS);
  },{passive:true});

  target.addEventListener('touchmove',e=>{
    if(chartScrub.mode!=='touch' || !e.touches.length)return;
    const t=e.touches[0];
    chartScrub.lastX=t.clientX;
    if(!chartScrub.active){
      if(Math.hypot(t.clientX-chartScrub.startX, t.clientY-chartScrub.startY) > MOVE_CANCEL_PX){
        clearTimeout(chartScrub.timer);
        chartScrub.timer=null;
        chartScrub.mode=null;
      }
      return;
    }
    e.preventDefault();
    updateChartScrubAt(t.clientX);
  },{passive:false});

  const finishTouch=()=>{
    if(chartScrub.mode==='touch'){
      chartScrub.lastTouchEnd=Date.now();
      endChartScrub();
    }
  };
  target.addEventListener('touchend',finishTouch,{passive:true});
  target.addEventListener('touchcancel',finishTouch,{passive:true});

  target.addEventListener('mousedown',e=>{
    if(e.button!==0 || !state.chartPoints.length)return;
    if(Date.now()-chartScrub.lastTouchEnd < 600)return;
    clearTimeout(chartScrub.timer);
    chartScrub.mode='mouse';
    chartScrub.startX=e.clientX;
    chartScrub.startY=e.clientY;
    chartScrub.lastX=e.clientX;
    chartScrub.timer=setTimeout(()=>{
      chartScrub.timer=null;
      if(chartScrub.mode!=='mouse' || !state.chartPoints.length)return;
      chartScrub.active=true;
      target.classList.add('is-scrubbing');
      updateChartScrubAt(chartScrub.lastX);
    }, HOLD_MS);
  });

  window.addEventListener('mousemove',e=>{
    if(chartScrub.mode!=='mouse')return;
    if(!(e.buttons & 1)){ endChartScrub(); return; }
    chartScrub.lastX=e.clientX;
    if(chartScrub.active){
      e.preventDefault();
      updateChartScrubAt(e.clientX);
    }
  });

  window.addEventListener('mouseup',()=>{
    if(chartScrub.mode==='mouse') endChartScrub();
  });
  window.addEventListener('blur',()=>{
    if(chartScrub.mode) endChartScrub();
  });
}

function renderChart(series){
  endChartScrub();
  renderDevelopmentHeadline(series, null);
  const target=$('history-chart');
  const dev=computeSpanDevelopment(series);
  const points=dev?.points||series?.points||[];
  state.chartPoints=points;
  state.chartTimestamps=points.map(p=>Date.parse(p.date));
  if(!points.length){
    if(state.chart){state.chart.clear();}
    const emptyText = state.dashboardMode==='cash'
      ? 'Cash development will appear here after you add cash transactions.'
      : 'Portfolio development will appear here when historical data is available.';
    target.innerHTML=`<div class="empty" style="margin:30px 10px">${html(emptyText)}</div>`;
    return;
  }
  if(state.chart && !target.querySelector('canvas')){state.chart.dispose();state.chart=null;target.innerHTML='';}
  const data=points.map((p,i)=>[state.chartTimestamps[i],p.value]);
  const dark = state.theme === 'dark';
  const latestVal = dev ? dev.value : data[data.length-1][1];
  const rising = latestVal >= -0.005;
  const color=rising?(dark?'#58e3fb':'#1b7fb3'):(dark?'#f27b89':'#c94052');
  if(typeof echarts==='undefined'){
    const ys=data.map(p=>p[1]);const min=Math.min(...ys);const max=Math.max(...ys);const width=640,height=230,pad=12;
    const path=ys.map((y,i)=>`${i?'L':'M'} ${(i/(ys.length-1||1)*width).toFixed(1)} ${(height-pad-(y-min)/(max-min||1)*(height-2*pad)).toFixed(1)}`).join(' ');
    target.innerHTML=`<svg viewBox="0 0 ${width} ${height}" style="width:100%;height:100%" preserveAspectRatio="none" role="img"><path d="${path}" fill="none" stroke="${color}" stroke-width="2.6" vector-effect="non-scaling-stroke"/></svg>`;return;
  }
  if(!state.chart)state.chart=echarts.init(target);
  const gradTop = rising
    ? (dark ? 'rgba(88,227,251,.22)' : 'rgba(27,127,179,.18)')
    : (dark ? 'rgba(242,123,137,.20)' : 'rgba(201,64,82,.15)');
  const gradBot = rising
    ? (dark ? 'rgba(88,227,251,0)' : 'rgba(27,127,179,0)')
    : (dark ? 'rgba(242,123,137,0)' : 'rgba(201,64,82,0)');
  const ys = data.map(p=>p[1]);
  const crossesZero = Math.min(...ys) < 0 && Math.max(...ys) > 0;
  const markLine = crossesZero ? {
    silent:true, symbol:['none','none'], label:{show:false},
    lineStyle:{color:dark?'rgba(255,255,255,.14)':'rgba(38,32,22,.16)',type:'dashed',width:1},
    data:[{yAxis:0}]
  } : undefined;
  state.chart.setOption({animationDuration:280,grid:{left:4,right:5,top:8,bottom:10,containLabel:false},
    xAxis:{type:'time',boundaryGap:false,axisLine:{show:false},axisTick:{show:false},axisLabel:{show:false},splitLine:{show:false},min:data[0][0],max:data[data.length-1][0]},
    yAxis:{type:'value',scale:true,axisLabel:{show:false},axisLine:{show:false},axisTick:{show:false},splitLine:{show:false}},
    tooltip:{
      show:true, showContent:false, trigger:'axis', triggerOn:'none',
      axisPointer:{type:'line',snap:true,lineStyle:{color:dark?'rgba(255,255,255,.24)':'rgba(38,32,22,.24)',width:1.2,type:'dashed'}}
    },
    series:[{type:'line',data,symbol:'circle',symbolSize:7,showSymbol:false,smooth:false,
      itemStyle:{color,borderColor:dark?'#12151b':'#f8f5ef',borderWidth:2},
      emphasis:{scale:true},
      lineStyle:{color,width:2.4},markLine,
      areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:gradTop},{offset:1,color:gradBot}])}}]
  },true);
  state.chart.resize();
}

function clearHistoryCache(modes=['investments','cash','nav']){
  for(const m of modes){
    state.history[m] = {};
    state.historyRevisions[m] = null;
  }
  inFlightHistory.clear();
}

function fetchHistorySpan(span, mode=state.dashboardMode){
  const rev = state.historyRevisions[mode] || '';
  const inflightKey = mode + '|' + rev + '|' + span;
  if(inFlightHistory.has(inflightKey)) return inFlightHistory.get(inflightKey);
  const url = `/api/history?span=${encodeURIComponent(span)}&mode=${encodeURIComponent(mode)}`;
  const req = api(url)
    .then(data=>{
      if(data.history_revision){
        if(state.historyRevisions[mode] && state.historyRevisions[mode] !== data.history_revision){
          state.history[mode] = {};
        }
        state.historyRevisions[mode] = data.history_revision;
      }
      state.history[mode][span] = data;
      return data;
    })
    .finally(()=>{ inFlightHistory.delete(inflightKey); });
  inFlightHistory.set(inflightKey, req);
  return req;
}

function prefetchRange(span){
  const mode = state.dashboardMode;
  if(!state.user || state.history[mode]?.[span]) return;
  fetchHistorySpan(span, mode).catch(()=>{});
}

async function loadRange(span, silent=false){
  endChartScrub();
  const mode = state.dashboardMode;
  state.historyRange=span;
  document.querySelectorAll('[data-range]').forEach(el=>el.classList.toggle('selected',el.dataset.range===span));
  const cached = state.history[mode]?.[span];
  if(cached){renderChart(cached);return;}
  if(!silent){renderDevelopmentHeadline(null);$('history-loading').classList.remove('hidden');}
  try{
    const data=await fetchHistorySpan(span, mode);
    if(state.historyRange===span && state.dashboardMode===mode) renderChart(data);
  }catch(e){if(!silent)toast('Development history: '+e.message,true);}
  finally{if(state.historyRange===span && state.dashboardMode===mode)$('history-loading').classList.add('hidden');}
}

function applyDashboardData(d, preloadedHistory=null, historyUnchanged=false){
  const prevBase = state.dashboard?.base_currency;
  const baseChanged = !!(prevBase && prevBase !== d.base_currency);
  const nextRevs = d.history_revisions || { investments: d.history_revision || null, cash: null, nav: null };
  const hadChart = !!currentHistorySeries();
  for(const m of ['investments','cash','nav']){
    const prevR = state.historyRevisions[m];
    const nextR = nextRevs[m];
    if(baseChanged || (prevR && nextR && prevR !== nextR)){
      state.history[m] = {};
    }
    if(nextR) state.historyRevisions[m] = nextR;
  }
  if(baseChanged) inFlightHistory.clear();
  state.dashboard = d;
  renderDashboard();
  renderAnalytics();
  renderAccount();
  const activeMode = state.dashboardMode;
  if(preloadedHistory){
    const targetMode = preloadedHistory.mode || activeMode;
    const targetSpan = preloadedHistory.range || state.historyRange;
    if(!state.history[targetMode]) state.history[targetMode] = {};
    state.history[targetMode][targetSpan] = preloadedHistory;
    if(state.view === 'home' && activeMode === targetMode && state.historyRange === targetSpan){
      $('history-loading').classList.add('hidden');
      renderChart(preloadedHistory);
    }
    return;
  }
  if(historyUnchanged && state.history[activeMode]?.[state.historyRange]){
    if(state.view === 'home') renderChart(state.history[activeMode][state.historyRange]);
    return;
  }
  if(state.view === 'home') loadRange(state.historyRange, hadChart && !baseChanged);
}

async function reloadDashboard(showMessage=false){
  const d=await api('/api/dashboard');
  applyDashboardData(d);
  if(showMessage){toast(d.warnings.length?'Updated. '+d.warnings[0]:'Portfolio updated');}
}

async function refreshPortfolio(){
  const btn=$('refresh-btn');
  if(btn.disabled)return;
  btn.classList.add('is-refreshing');btn.disabled=true;
  const label = btn.querySelector('.refresh-label');
  if(label) label.textContent = 'Refreshing…';
  try{
    const mode = state.dashboardMode;
    const knownRev = state.historyRevisions[mode] || '';
    const params = new URLSearchParams({
      include_dashboard: 'true',
      include_history: 'true',
      span: state.historyRange,
      mode,
      known_history_revision: knownRev
    });
    const result=await api('/api/refresh?' + params.toString(),{method:'POST'});
    if(result.dashboard){
      applyDashboardData(result.dashboard, result.history || null, !!result.history_unchanged);
    }else{
      await reloadDashboard();
    }
    const failures=Object.keys(result.errors||{});
    if((result.requested??0)===0){
      toast('FX rates & balances refreshed');
    } else {
      toast(`Updated ${result.updated}/${result.requested??0} latest prices${failures.length?`; unavailable: ${failures.join(', ')}`:''}`,!!failures.length);
    }
  }catch(e){toast(e.message,true);}
  finally{
    btn.classList.remove('is-refreshing');btn.disabled=false;
    if(label) label.textContent = 'Refresh';
  }
}

// ─── New Trade / Cash Form Mode & Cash Sync ────────────────────────────────
function isCashTradeMode(){
  return $('trade-asset-type')?.value === 'Cash';
}

function syncTradeFormMode(){
  const f = $('trade-form');
  if(!f) return;
  const cashMode = isCashTradeMode();
  $('trade-side-toggle')?.classList.toggle('hidden', cashMode);
  $('cash-type-toggle')?.classList.toggle('hidden', !cashMode);

  const assetEl = f.elements.asset;
  const tickerEl = f.elements.ticker;
  const priceEl = f.elements.price_trade_ccy;
  const sttEl = f.elements.stt_trade_ccy;

  $('lbl-asset').textContent = cashMode ? 'Memo / Note (optional)' : 'Asset name';
  assetEl.placeholder = cashMode ? 'e.g. Salary transfer, FX conversion' : 'e.g. APPLE';
  assetEl.required = !cashMode;

  $('lbl-ticker').textContent = cashMode ? 'Ticker (N/A for Cash)' : 'Ticker (yFinance)';
  tickerEl.disabled = cashMode;
  if(cashMode) tickerEl.value = '';

  $('lbl-date').textContent = cashMode ? 'Date' : 'Trade date';
  $('lbl-quantity').textContent = cashMode ? 'Amount' : 'Quantity';

  $('lbl-price').textContent = cashMode ? 'Price per unit (N/A)' : 'Price per unit';
  priceEl.disabled = cashMode;
  priceEl.required = !cashMode;
  if(cashMode) priceEl.value = '';

  $('lbl-ccy').textContent = cashMode ? 'Currency' : 'Trade currency';
  $('lbl-fees').textContent = cashMode ? 'Exchange fee' : 'Fees / brokerage';
  if(cashMode) f.elements.charges_trade_ccy.removeAttribute('min');
  else f.elements.charges_trade_ccy.min = '0';

  $('lbl-stt').textContent = cashMode ? 'Tax / STT (N/A)' : 'Tax / STT';
  sttEl.disabled = cashMode;
  if(cashMode) sttEl.value = '0';

  $('trade-preview-label').textContent = cashMode ? 'Cash transaction amount' : 'Trade value (excluding fees)';
  $('trade-submit-btn').textContent = cashMode ? 'Save cash transaction' : 'Save trade';
  updateTradePreview();
}

function formTrade(form){
  const fd=new FormData(form);const val=Object.fromEntries(fd.entries());
  for(const f of ['quantity','price_trade_ccy','charges_trade_ccy','stt_trade_ccy','fx_to_base','last_price_trade_ccy','last_price_base'])val[f]=Number(val[f]??(f==='fx_to_base'?1:0));
  val.trade_date=val.trade_date.includes('T')?val.trade_date.replace('T',' '):val.trade_date+' 00:00';return val;
}

function updateTradePreview(){
  const f=$('trade-form');
  if(!f) return;
  const ccy = (f.elements.trade_ccy.value || 'USD').toUpperCase();
  const q = Number(f.elements.quantity.value)||0;
  if(isCashTradeMode()){
    const outflow = state.cashTxnType === 'Debit' || state.cashTxnType === 'Sell';
    const signed = outflow ? -Math.abs(q) : Math.abs(q);
    $('trade-total').querySelector('strong').textContent = q > 0 ? signedFmt(signed, ccy, 2) : '—';
  } else {
    const price = Number(f.elements.price_trade_ccy.value)||0;
    $('trade-total').querySelector('strong').textContent = fmt(q*price, ccy, 2);
  }
}

function openCashSyncPrompt(suggested){
  state.pendingCashSync = suggested;
  const isDebit = suggested.txn_type === 'Debit';
  const base = state.dashboard?.base_currency || 'SEK';
  const typeLabel = isDebit ? 'Debit (Outflow)' : 'Credit (Inflow)';
  $('cash-sync-summary').textContent = `Register the matching ${suggested.txn_type.toLowerCase()} in your cash account?`;
  $('cash-sync-details').innerHTML = `
    <div class="cash-sync-row"><span>Transaction</span><strong class="${isDebit?'sell':'buy'}">${html(typeLabel)}</strong></div>
    <div class="cash-sync-row"><span>Amount</span><strong>${html(fmtCashNative(suggested.foreign_amount, suggested.foreign_ccy, 2))}</strong></div>
    <div class="cash-sync-row"><span>Date</span><strong>${html(suggested.date)}</strong></div>
    <div class="cash-sync-row"><span>FX rate (to ${html(base)})</span><strong>${html(compact(suggested.fx_rate, 4))}</strong></div>
    <div class="cash-sync-row"><span>Memo</span><strong>${html(suggested.memo)}</strong></div>`;
  $('cash-sync-dialog').showModal();
}

async function confirmCashSync(){
  const payload = state.pendingCashSync;
  if(!payload){ $('cash-sync-dialog').close(); return; }
  const btn = $('cash-sync-confirm');
  btn.disabled = true;
  try{
    await api('/api/cash/transactions', { method:'POST', body:{ ...payload, auto_fx:false } });
    state.pendingCashSync = null;
    $('cash-sync-dialog').close();
    clearHistoryCache(['cash','nav']);
    state.cashTxns = [];
    await reloadDashboard();
    toast('Trade & matching cash transaction saved');
    nav('home');
  }catch(err){
    toast('Cash registration failed: ' + err.message, true);
  }finally{
    btn.disabled = false;
  }
}

function skipCashSync(){
  state.pendingCashSync = null;
  $('cash-sync-dialog').close();
  nav('home');
}

async function submitTrade(e){
  e.preventDefault();
  const form=e.currentTarget;
  const btn=form.querySelector('[type=submit]');
  try{
    btn.disabled=true;
    if(isCashTradeMode()){
      const payload = {
        date: form.elements.trade_date.value,
        foreign_ccy: (form.elements.trade_ccy.value || '').trim().toUpperCase(),
        txn_type: state.cashTxnType,
        foreign_amount: Number(form.elements.quantity.value),
        fx_rate: Number(form.elements.fx_to_base.value || 1),
        fee_foreign: Number(form.elements.charges_trade_ccy.value || 0),
        memo: (form.elements.asset.value || '').trim(),
        auto_fx: $('trade-auto-fx').checked
      };
      await api('/api/cash/transactions', { method:'POST', body:payload });
      form.reset();
      form.elements.asset_type.value='Cash';
      form.elements.trade_date.value=todayISO();
      $('trade-auto-fx').checked=true;
      $('trade-manual-fx').disabled=true;
      syncTradeFormMode();
      clearHistoryCache(['cash','nav']);
      state.cashTxns=[];
      await reloadDashboard();
      toast('Cash transaction saved successfully');
      nav('home');
      return;
    }

    const payload={...formTrade(form), side:state.tradeSide, auto_fx:$('trade-auto-fx').checked};
    const res = await api('/api/trades',{method:'POST',body:payload});
    form.reset();
    form.elements.trade_date.value=todayISO();
    $('trade-auto-fx').checked=true;
    $('trade-manual-fx').disabled=true;
    syncTradeFormMode();
    clearHistoryCache(['investments','nav']);
    state.trades=[];
    await reloadDashboard();
    toast('Trade saved successfully');
    if(res?.suggested_cash && res.suggested_cash.foreign_amount > 0){
      openCashSyncPrompt(res.suggested_cash);
    } else {
      nav('home');
    }
  }catch(err){toast(err.message,true);}
  finally{btn.disabled=false;updateTradePreview();}
}

// ─── History Tab (Trades | Cash toggle) ────────────────────────────────────
function setHistoryMode(mode){
  if(mode !== 'trades' && mode !== 'cash') return;
  state.historyMode = mode;
  state.tradeLimit = 55;
  document.querySelectorAll('[data-history-mode]').forEach(btn=>{
    const active = btn.dataset.historyMode === mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', String(active));
  });
  $('trades-side-filters')?.classList.toggle('hidden', mode !== 'trades');
  $('cash-side-filters')?.classList.toggle('hidden', mode !== 'cash');
  const searchEl = $('history-search');
  if(searchEl){
    searchEl.placeholder = mode === 'cash'
      ? 'Search currency, type, memo…'
      : 'Search assets, symbols, currency…';
  }
  loadHistoryData();
}

async function loadHistoryData(){
  if(state.historyMode === 'cash') await loadCashTxns();
  else await loadTrades();
}

async function loadTrades(){
  try{const result=await api('/api/trades');state.trades=result.rows;renderHistoryList();}
  catch(e){toast(e.message,true);}
}

async function loadCashTxns(){
  try{const result=await api('/api/cash/transactions');state.cashTxns=result.rows;renderHistoryList();}
  catch(e){toast(e.message,true);}
}

function renderHistoryList(){
  if(state.historyMode === 'cash') renderCashTxns();
  else renderTrades();
}

function renderTrades(){
  const target=$('trades-list');if(!target)return;
  const searchEl=$('history-search');
  const query=searchEl?searchEl.value.trim().toLowerCase():'';
  const clearBtn=$('history-search-clear');
  if(clearBtn) clearBtn.classList.toggle('hidden',!query);
  const rows=state.trades.filter(r=>{
    if(state.sideFilter !== 'ALL' && r.side !== state.sideFilter) return false;
    return `${r.asset} ${r.ticker} ${r.trade_ccy} ${r.side}`.toLowerCase().includes(query);
  });
  $('trade-count').textContent=`${rows.length} ${rows.length===1?'trade':'trades'}`;
  const tickerByAsset = new Map();
  for(const h of (state.dashboard?.holdings || [])){ if(h.ticker) tickerByAsset.set(h.asset, h.ticker); }
  for(const t of state.trades){ if(t.ticker && !tickerByAsset.has(t.asset)) tickerByAsset.set(t.asset, t.ticker); }
  target.innerHTML=rows.length?rows.slice(0,state.tradeLimit).map(r=>{
    const total=r.price_trade_ccy*r.quantity;const buy=r.side==='BUY';
    const item={asset:r.asset,ticker:r.ticker||tickerByAsset.get(r.asset)||'',type:r.asset_type||'Stock'};
    return `<button class="trade-row" data-edit="${html(r.trade_id)}" type="button">${logo(item)}<span class="trade-info"><strong>${html(r.asset)} <span class="side-label ${buy?'buy':'sell'}">${buy?'BUY':'SELL'}</span></strong><span>${html(r.trade_date.slice(0,10))} · ${state.hidden?'••••':quantityFmt(r.quantity)} units · ${html(r.ticker||r.asset_type)}</span></span><span class="trade-amount"><strong>${html(fmt(total,r.trade_ccy,2))}</strong><small>${html(fmt(r.price_trade_ccy,r.trade_ccy,2))}/unit</small></span></button>`;
  }).join(''):emptyMessage('No trades found','Try another search or add a new transaction.',null);
  activatePendingLogoSearch(target);
  $('load-more-trades').classList.toggle('hidden',rows.length<=state.tradeLimit);
}

function renderCashTxns(){
  const target=$('trades-list');if(!target)return;
  const searchEl=$('history-search');
  const query=searchEl?searchEl.value.trim().toLowerCase():'';
  const clearBtn=$('history-search-clear');
  if(clearBtn) clearBtn.classList.toggle('hidden',!query);
  const base = state.dashboard?.base_currency || 'SEK';
  const rows = state.cashTxns.filter(r => {
    const isIn = r.txn_type === 'Buy' || r.txn_type === 'Credit';
    if(state.cashFilter === 'IN' && !isIn) return false;
    if(state.cashFilter === 'OUT' && isIn) return false;
    return `${r.foreign_ccy} ${r.txn_type} ${r.memo} ${r.date}`.toLowerCase().includes(query);
  });
  $('trade-count').textContent = `${rows.length} ${rows.length===1?'entry':'entries'}`;
  target.innerHTML = rows.length ? rows.slice(0, state.tradeLimit).map(r => {
    const isIn = r.txn_type === 'Buy' || r.txn_type === 'Credit';
    const labelMap = { Buy:'FX BUY', Sell:'FX SELL', Credit:'CREDIT', Debit:'DEBIT' };
    const typeLbl = labelMap[r.txn_type] || r.txn_type.toUpperCase();
    const memoPart = r.memo ? ` · ${r.memo}` : '';
    const sub = `${r.date} · FX ${state.hidden ? '••••' : compact(r.fx_rate, 4)}${memoPart}`;
    return `<button class="trade-row" data-edit-cash="${html(r.txn_id)}" type="button">${currencyFlag(r.foreign_ccy)}<span class="trade-info"><strong>${html(r.foreign_ccy)} <span class="side-label ${isIn?'buy':'sell'}">${html(typeLbl)}</span></strong><span>${html(sub)}</span></span><span class="trade-amount"><strong>${html(signedFmt(r.foreign_amount, r.foreign_ccy, 2))}</strong><small>${html(fmt(r.base_amount, base, 2))}</small></span></button>`;
  }).join('') : emptyMessage('No cash transactions found', 'Add a Cash transaction in New trade or import cash_txns.csv.', null);
  $('load-more-trades').classList.toggle('hidden', rows.length <= state.tradeLimit);
}

function openEdit(id){const row=state.trades.find(t=>t.trade_id===id);if(!row)return;
  state.editId=id;const form=$('edit-form');
  for(const input of [...form.elements]){
    if(input.name && Object.hasOwn(row,input.name))input.value=input.name==='trade_date'?row[input.name].replace(' ','T'):row[input.name];
  }
  $('edit-dialog').showModal();
}
async function submitEdit(e){e.preventDefault();const row=state.trades.find(t=>t.trade_id===state.editId);if(!row)return;
  const val={...row,...formTrade(e.currentTarget),last_price_base:row.last_price_base};
  try{await api('/api/trades/'+encodeURIComponent(state.editId),{method:'PUT',body:val});$('edit-dialog').close();
    clearHistoryCache(['investments','nav']);
    await loadTrades();await reloadDashboard();toast('Trade updated');}
  catch(err){toast(err.message,true);}
}
async function deleteEdit(){const row=state.trades.find(t=>t.trade_id===state.editId);if(!row)return;
  if(!confirm(`Delete ${row.side} ${row.asset} on ${row.trade_date.slice(0,10)}?`))return;
  try{await api('/api/trades/'+encodeURIComponent(state.editId),{method:'DELETE'});$('edit-dialog').close();
    clearHistoryCache(['investments','nav']);
    await loadTrades();await reloadDashboard();toast('Trade deleted');}catch(e){toast(e.message,true);}
}

function openCashEdit(id){
  const row=state.cashTxns.find(t=>t.txn_id===id);if(!row)return;
  state.editCashId=id;
  const form=$('edit-cash-form');
  form.elements.date.value = row.date;
  form.elements.foreign_ccy.value = row.foreign_ccy;
  form.elements.txn_type.value = row.txn_type;
  form.elements.foreign_amount.value = Math.abs(row.foreign_amount);
  form.elements.fx_rate.value = row.fx_rate;
  form.elements.fee_foreign.value = row.fee_foreign;
  form.elements.memo.value = row.memo || '';
  $('edit-cash-dialog').showModal();
}
async function submitCashEdit(e){
  e.preventDefault();
  const form=e.currentTarget;
  const payload={
    date: form.elements.date.value,
    foreign_ccy: form.elements.foreign_ccy.value.trim().toUpperCase(),
    txn_type: form.elements.txn_type.value,
    foreign_amount: Number(form.elements.foreign_amount.value),
    fx_rate: Number(form.elements.fx_rate.value),
    fee_foreign: Number(form.elements.fee_foreign.value || 0),
    memo: form.elements.memo.value.trim(),
    auto_fx: false
  };
  try{
    await api('/api/cash/transactions/'+encodeURIComponent(state.editCashId),{method:'PUT',body:payload});
    $('edit-cash-dialog').close();
    clearHistoryCache(['cash','nav']);
    await loadCashTxns();await reloadDashboard();toast('Cash transaction updated');
  }catch(err){toast(err.message,true);}
}
async function deleteCashEdit(){
  const row=state.cashTxns.find(t=>t.txn_id===state.editCashId);if(!row)return;
  if(!confirm(`Delete ${row.txn_type} ${row.foreign_ccy} on ${row.date}?`))return;
  try{
    await api('/api/cash/transactions/'+encodeURIComponent(state.editCashId),{method:'DELETE'});
    $('edit-cash-dialog').close();
    clearHistoryCache(['cash','nav']);
    await loadCashTxns();await reloadDashboard();toast('Cash transaction deleted');
  }catch(e){toast(e.message,true);}
}

// ─── Tax / Realized P/L ────────────────────────────────────────────────────
function taxDates(){const now=new Date(),end=todayISO();let start;
  switch($('tax-period').value){
    case 'lastyear':start=`${now.getFullYear()-1}-01-01`;return [start,`${now.getFullYear()-1}-12-31`];
    case '12m':{const d=new Date(now);d.setFullYear(d.getFullYear()-1);return [d.toLocaleDateString('en-CA'),end];}
    case 'custom':return [$('tax-start').value||`${now.getFullYear()}-01-01`,$('tax-end').value||end];
    default:return [`${now.getFullYear()}-01-01`,end];
  }
}
async function loadTax(){const [start,end]=taxDates();if(!start||!end)return;
  const mode=$('tax-mode').value;
  try{const report=await api(`/api/tax?start=${start}&end=${end}&mode=${mode}`);
    state.lastTax=report;renderTax(report);
    $('tax-dates').textContent=`${start} to ${end}`;
  }catch(e){toast(e.message,true);}
}
function renderTax(report){
  const trade=report.mode==='trade';const base=state.dashboard?.base_currency||'SEK';
  $('tax-value').textContent=trade?'Mixed currencies':fmt(report.total,base,2);
  $('tax-value').style.fontSize=trade?'24px':'';
  $('tax-value').classList.toggle('negative',!trade&&report.total<0);
  $('tax-count').textContent=`${report.rows.length} ${report.rows.length===1?'item':'items'}`;
  $('tax-list').innerHTML=report.rows.length?report.rows.map(r=>{
    const isCash = r.type === 'Cash';
    const ccy = isCash ? base : (trade ? r.currency : base);
    const iconHtml = isCash ? currencyFlag(r.foreign_ccy || r.asset) : logo({asset:r.asset,ticker:r.ticker||'',type:r.type||'Stock'});
    const title = r.asset;
    return `<div class="tax-entry"><div class="analytics-row-top tax-row-top"><div class="analytics-identity">${iconHtml}<div class="analytics-title-group"><div class="holding-name">${html(title)}</div><div class="holding-subtitle">Qty ${state.hidden?'••••':quantityFmt(r.quantity_sold)}</div></div></div></div><div class="analytics-row-metrics tax-row-metrics"><div class="analytics-metric"><span>Cost</span><strong>${html(fmt(r.buy_value,ccy,2))}</strong></div><div class="analytics-metric analytics-metric-mid"><span>Proceeds</span><strong>${html(fmt(r.sell_value,ccy,2))}</strong></div><div class="analytics-metric analytics-metric-end"><strong class="tax-pnl ${r.realized<0?'negative':''}">${html(signedFmt(r.realized,ccy,2))}</strong></div></div></div>`;
  }).join(''):emptyMessage('No realized trades','Sales and cash FX realizations for the chosen dates will appear here.',null);
  activatePendingLogoSearch($('tax-list'));

}

// ─── Account & Data Import/Export ──────────────────────────────────────────
function renderAccount(){
  if(!state.user)return;
  const initial = (state.user.name||'U')[0].toUpperCase();
  const baseCcy = state.dashboard?.base_currency||state.user.base_currency;
  $('profile-name').textContent=state.user.name;
  $('profile-type').textContent=state.user.google?'Google account':'Local account';
  $('account-login-method').textContent=state.user.google?'Google sign-in':'Local username and password';
  $('account-google').classList.toggle('hidden',state.user.google||!state.user.google_enabled);
  $('profile-avatar').textContent=initial;
  $('avatar-char').textContent=initial;
  const sbChar = $('sidebar-avatar-char'); if(sbChar) sbChar.textContent=initial;
  const sbName = $('sidebar-user-name'); if(sbName) sbName.textContent=state.user.name;
  $('base-select').value=baseCcy;
  document.querySelectorAll('.base-label').forEach(el=>el.textContent=baseCcy);
}
async function changeBase(e){const newBase=e.target.value;const oldBase=state.dashboard.base_currency;
  if(newBase===oldBase)return;
  e.target.disabled=true;
  try{await api('/api/settings',{method:'PUT',body:{base_currency:newBase}});clearHistoryCache();
    await reloadDashboard();renderAccount();toast('Base currency changed to '+newBase);
  }catch(error){e.target.value=oldBase;toast(error.message,true);}
  finally{e.target.disabled=false;}
}
async function importFile(e){const file=e.target.files[0];if(!file)return;
  if(!confirm(`Replace your current portfolio trades with ${file.name}? This overwrites existing trades. Download a backup first if needed.`)) {e.target.value='';return;}
  const form=new FormData();form.append('file',file);
  try{const result=await api('/api/import',{method:'POST',body:form});clearHistoryCache(['investments','nav']);state.trades=[];
    await reloadDashboard();toast(`Imported ${result.count} trades successfully`);nav('home');}
  catch(err){toast(err.message,true);}finally{e.target.value='';}
}
async function importCashFile(e){const file=e.target.files[0];if(!file)return;
  if(!confirm(`Replace your current cash transactions with ${file.name}? This overwrites existing cash entries.`)) {e.target.value='';return;}
  const form=new FormData();form.append('file',file);
  try{const result=await api('/api/cash/import',{method:'POST',body:form});clearHistoryCache(['cash','nav']);state.cashTxns=[];
    await reloadDashboard();toast(`Imported ${result.count} cash transactions successfully`);nav('home');}
  catch(err){toast(err.message,true);}finally{e.target.value='';}
}

function bindEvents(){
  bindAssetLogoImages();
  bindChartScrub();
  document.querySelectorAll('[data-tab]').forEach(el=>{
    el.addEventListener('click',()=>nav(el.dataset.tab));
    if(el.getAttribute('role')==='button'){
      el.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();nav(el.dataset.tab);}});
    }
  });
  $('cash-see-all')?.addEventListener('click',()=>{
    setAnalyticsMode('cash');
    nav('analytics');
  });
  document.querySelectorAll('[data-dash-mode]').forEach(el=>el.addEventListener('click',()=>setDashboardMode(el.dataset.dashMode)));
  document.querySelectorAll('[data-analytics-mode]').forEach(el=>el.addEventListener('click',()=>setAnalyticsMode(el.dataset.analyticsMode)));
  document.querySelectorAll('[data-history-mode]').forEach(el=>el.addEventListener('click',()=>setHistoryMode(el.dataset.historyMode)));

  document.addEventListener('click',e=>{
    const target=e.target.closest('[data-go]');
    if(target)nav(target.dataset.go);
    if(!e.target.closest('#home-sort-dropdown')) closeSortMenu();
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape') closeSortMenu();
  });
  $('home-sort-btn')?.addEventListener('click',e=>{
    e.stopPropagation();
    const menu=$('home-sort-menu');
    const opening=menu.classList.contains('hidden');
    menu.classList.toggle('hidden',!opening);
    e.currentTarget.setAttribute('aria-expanded',String(opening));
  });
  document.querySelectorAll('#home-sort-menu [data-sort]').forEach(el=>el.addEventListener('click',()=>{
    state.homeSort = el.dataset.sort;
    document.querySelectorAll('#home-sort-menu [data-sort]').forEach(b=>{
      const active=b===el;
      b.classList.toggle('active',active);
      b.setAttribute('aria-checked',String(active));
    });
    closeSortMenu();
    renderDashboard();
  }));
  document.querySelectorAll('[data-range]').forEach(el=>{
    el.addEventListener('click',()=>loadRange(el.dataset.range));
    el.addEventListener('pointerenter',()=>{ if(window.matchMedia('(hover: hover)').matches) prefetchRange(el.dataset.range); });
  });
  document.querySelectorAll('[data-side-filter]').forEach(el=>el.addEventListener('click',()=>{
    state.sideFilter = el.dataset.sideFilter;
    state.tradeLimit = 55;
    document.querySelectorAll('[data-side-filter]').forEach(b=>b.classList.toggle('active',b===el));
    renderTrades();
  }));
  document.querySelectorAll('[data-cash-filter]').forEach(el=>el.addEventListener('click',()=>{
    state.cashFilter = el.dataset.cashFilter;
    state.tradeLimit = 55;
    document.querySelectorAll('[data-cash-filter]').forEach(b=>b.classList.toggle('active',b===el));
    renderCashTxns();
  }));
  $('refresh-btn').addEventListener('click',refreshPortfolio);
  $('capital-reset').addEventListener('click',()=>{state.capitalFocus=null;renderCapitalChart(true);});
  $('capital-chart').addEventListener('click',e=>{
    const tile=e.target.closest('[data-capital-asset]');
    if(tile)toggleCapitalFocus(tile.dataset.capitalAsset);
  });
  $('privacy-toggle').addEventListener('click',()=>toggleHidden(!state.hidden));
  $('sidebar-privacy-toggle')?.addEventListener('click',()=>toggleHidden(!state.hidden));
  $('theme-quick-toggle')?.addEventListener('click',()=>applyTheme(state.theme==='dark'?'light':'dark'));
  $('sidebar-theme-toggle')?.addEventListener('click',()=>applyTheme(state.theme==='dark'?'light':'dark'));
  $('account-privacy').addEventListener('change',e=>toggleHidden(e.target.checked));
  document.querySelectorAll('[data-theme-choice]').forEach(b=>b.addEventListener('click',()=>applyTheme(b.dataset.themeChoice)));
  document.querySelectorAll('[data-auth]').forEach(el=>el.addEventListener('click',()=>{
    state.authMode=el.dataset.auth;document.querySelectorAll('[data-auth]').forEach(x=>x.classList.toggle('active',x===el));
    $('auth-action').textContent=state.authMode==='login'?'Sign in':'Create account';
    $('auth-password').autocomplete=state.authMode==='register'?'new-password':'current-password';
  }));
  $('auth-form').addEventListener('submit',async e=>{
    e.preventDefault();const btn=e.currentTarget.querySelector('button[type=submit]');btn.disabled=true;
    try{await api('/api/'+(state.authMode==='login'?'login':'register'),{method:'POST',body:{username:$('auth-username').value,password:$('auth-password').value}});
      await startLoggedIn();}
    catch(error){toast(error.message,true);}finally{btn.disabled=false;}
  });

  $('trade-form').elements.trade_date.value=todayISO();
  $('trade-form').addEventListener('submit',submitTrade);
  $('trade-form').addEventListener('input',updateTradePreview);
  $('trade-asset-type').addEventListener('change',syncTradeFormMode);
  document.querySelectorAll('[data-side]').forEach(el=>el.addEventListener('click',()=>{
    state.tradeSide=el.dataset.side;document.querySelectorAll('[data-side]').forEach(x=>x.classList.toggle('selected',x===el));
  }));
  document.querySelectorAll('[data-cash-type]').forEach(el=>el.addEventListener('click',()=>{
    state.cashTxnType=el.dataset.cashType;
    document.querySelectorAll('[data-cash-type]').forEach(x=>x.classList.toggle('selected',x===el));
    updateTradePreview();
  }));
  $('trade-auto-fx').addEventListener('change',e=>{ $('trade-manual-fx').disabled=e.target.checked; });
  syncTradeFormMode();

  $('cash-sync-confirm')?.addEventListener('click',confirmCashSync);
  $('cash-sync-skip')?.addEventListener('click',skipCashSync);
  $('close-cash-sync')?.addEventListener('click',skipCashSync);
  $('cash-sync-dialog')?.addEventListener('click',e=>{if(e.target===$('cash-sync-dialog'))skipCashSync();});

  $('history-search').addEventListener('input',()=>{state.tradeLimit=55;renderHistoryList();});
  $('history-search-clear')?.addEventListener('click',()=>{$('history-search').value='';state.tradeLimit=55;renderHistoryList();$('history-search').focus();});
  $('load-more-trades').addEventListener('click',()=>{state.tradeLimit+=55;renderHistoryList();});
  $('trades-list').addEventListener('click',e=>{
    const cashRow=e.target.closest('[data-edit-cash]');
    if(cashRow){ openCashEdit(cashRow.dataset.editCash); return; }
    const row=e.target.closest('[data-edit]');
    if(row)openEdit(row.dataset.edit);
  });
  $('close-dialog').addEventListener('click',()=>$('edit-dialog').close());
  $('edit-dialog').addEventListener('click',e=>{if(e.target===$('edit-dialog'))$('edit-dialog').close();});
  $('edit-form').addEventListener('submit',submitEdit);
  $('delete-trade').addEventListener('click',deleteEdit);

  $('close-cash-dialog').addEventListener('click',()=>$('edit-cash-dialog').close());
  $('edit-cash-dialog').addEventListener('click',e=>{if(e.target===$('edit-cash-dialog'))$('edit-cash-dialog').close();});
  $('edit-cash-form').addEventListener('submit',submitCashEdit);
  $('delete-cash-txn').addEventListener('click',deleteCashEdit);

  $('tax-period').addEventListener('change',e=>{$('tax-custom').classList.toggle('hidden',e.target.value!=='custom');loadTax();});
  for(const id of ['tax-mode','tax-start','tax-end'])$(id).addEventListener('change',loadTax);
  $('tax-export').addEventListener('click',()=>{const [start,end]=taxDates();window.location.href=`/api/export/tax?start=${start}&end=${end}&mode=${$('tax-mode').value}`;});
  $('base-select').addEventListener('change',changeBase);
  $('file-export').addEventListener('click',()=>{window.location.href='/api/export/trades';});
  $('file-import').addEventListener('change',importFile);
  $('cash-file-export').addEventListener('click',()=>{window.location.href='/api/export/cash';});
  $('cash-file-import').addEventListener('change',importCashFile);
  $('logout-btn').addEventListener('click',async()=>{
    try{
      await api('/api/logout',{method:'POST'});
      state.user=null;state.dashboard=null;state.trades=[];state.cashTxns=[];
      clearHistoryCache();showAuth();toast('Signed out');
    }catch(e){toast(e.message,true);}
  });
  window.addEventListener('resize',()=>{
    state.chart?.resize();
    state.cashDonutChart?.resize();
    state.navDonutChart?.resize();
    fitCapitalTileLabels($('capital-chart'));
  });
}

async function startLoggedIn(prefetchedAccount=null){
  const account = prefetchedAccount || await api('/api/me');
  state.user=account;
  if(!state.user.authenticated){showAuth();return;}
  renderAccount();showApp();nav('home');
  $('history-loading').classList.remove('hidden');
  const dashReq = api('/api/dashboard');
  const histReq = fetchHistorySpan(state.historyRange, state.dashboardMode).catch(err=>{
    toast('Development history: '+err.message,true);
    return null;
  });
  try{
    const [d, h] = await Promise.all([dashReq, histReq]);
    applyDashboardData(d, h || null, false);
  }catch(error){
    toast(error.message,true);
  }finally{
    $('history-loading').classList.add('hidden');
  }
}

async function init(){
  bindEvents();applyTheme(state.theme);toggleHidden(state.hidden);
  try{
    const account=await api('/api/me');
    $('google-container').classList.toggle('hidden',!account.google_enabled);
    $('google-hint').classList.toggle('hidden',!!account.google_enabled);
    if(account.authenticated){await startLoggedIn(account);}else showAuth();
  }catch(error){showAuth();toast(error.message,true);}
}
init();
