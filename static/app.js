/* Track — lightweight mobile-first client. All portfolio calculations run in Python. */
'use strict';
const $ = (id) => document.getElementById(id);
const state = { user:null, dashboard:null, trades:[], history:{}, historyRange:'1M', view:'home',
  tradeSide:'BUY', authMode:'login', hidden:localStorage.getItem('track-private')==='true',
  chart:null, capitalChart:null, capitalFocus:null, theme:localStorage.getItem('track-theme')==='light'?'light':'dark', tradeLimit:55, editId:null, lastTax:null, busy:false };
// Preserve the home list's expanded rows when the dashboard is re-rendered.
const expandedHomeHoldings = new Set();
const icons = { Stock:'S', Fund:'F', ETF:'E', Crypto:'₿' };
const palette = { Stock:['#25344e','#78bdf0'], Fund:['#3c3450','#c3a7ed'], ETF:['#213c4c','#81cddd'], Crypto:['#3b3447','#e2b6cf'] };
// Reference-inspired treemap: dark jewel tones alongside lighter pastel and bright tiles.
// 40 individual hues ensure a varied palette even for larger portfolios.
const capitalColors = [
  '#20467f','#197c85','#317552','#597785','#566977','#a9c864','#d1a43b','#a97530',
  '#553079','#78c9a8','#b87b86','#d5bba0','#79bcda','#efdfa1','#f0a783','#efe9c6',
  '#9a69a3','#ba5c51','#d33429','#9cc86d','#65717b','#dbb134','#90a8d8','#316ea4',
  '#df8f83','#5a9cbd','#4786bd','#a87943','#b63767','#8b613d','#a9bbc8','#648bd0',
  '#6a9b85','#b2a4d9','#bf7950','#8253a3','#77b7ae','#a1aa7e','#d2ad78','#718bb6'
];
// Choose dark ink on light tiles and white ink on dark tiles, as in the reference image.
function capitalInk(color){
  const rgb=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)/255);
  const linear=rgb.map(c=>c<=0.04045?c/12.92:((c+0.055)/1.055)**2.4);
  const luminance=0.2126*linear[0]+0.7152*linear[1]+0.0722*linear[2];
  return luminance>0.179?'#19202a':'#f7f8fb';
}
const todayISO = () => new Date().toLocaleDateString('en-CA');
function html(s){ return String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function compact(n,digits=2){const x=Number(n)||0;return new Intl.NumberFormat('en-US',{maximumFractionDigits:digits,minimumFractionDigits:digits}).format(x);}
function quantityFmt(n){return new Intl.NumberFormat('en-US',{maximumFractionDigits:8,minimumFractionDigits:0}).format(Number(n)||0);}
function fmt(v,ccy=state.dashboard?.base_currency ?? state.user?.base_currency ?? 'SEK',digits=0){
  if(v==null)return '—';if(state.hidden)return '••••••';
  const value=compact(v,digits);return ccy==='SEK' ? `${value} kr` : ccy==='INR'?`₹${value}`: ccy==='USD'?`$${value}`:ccy==='EUR'?`€${value}`:ccy==='GBP'?`£${value}`:`${value} ${ccy}`;
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
function applyTheme(theme){
  state.theme=theme==='light'?'light':'dark';localStorage.setItem('track-theme',state.theme);
  document.documentElement.dataset.theme=state.theme;
  document.querySelector('meta[name="theme-color"]').content=state.theme==='dark'?'#0c0d0f':'#f0f3f9';
  document.querySelectorAll('[data-theme-choice]').forEach(b=>{
    const selected=b.dataset.themeChoice===state.theme;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));
  });
  if(state.dashboard){renderDashboard();renderAnalytics();renderChart(state.history[state.historyRange]??null);}
}
function toggleHidden(value){state.hidden=!!value;localStorage.setItem('track-private',String(value));$('account-privacy').checked=value;
  $('privacy-toggle').innerHTML=value?'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3l18 18M9.8 6.3A11 11 0 0 1 12 6c6.4 0 10 6 10 6a17 17 0 0 1-3.6 4.1M6 8.1A18 18 0 0 0 2 12s3.6 6 10 6c1.4 0 2.6-.3 3.7-.8"/><path d="M9 10a3 3 0 0 0 5 4"/></svg>':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
  if(state.dashboard){renderDashboard();renderAnalytics();renderChart(state.history[state.historyRange]??null);}
  if(state.lastTax)renderTax(state.lastTax);
  renderTrades();
}
function showAuth(){ $('loader').classList.add('hidden');$('app').classList.add('hidden');$('auth').classList.remove('hidden'); }
function showApp(){ $('loader').classList.add('hidden');$('auth').classList.add('hidden');$('app').classList.remove('hidden'); }
function nav(tab){
  if(!state.user)return;
  state.view=tab;
  document.querySelectorAll('.view').forEach(el=>el.classList.toggle('active',el.id==='view-'+tab));
  document.querySelectorAll('.nav-btn').forEach(btn=>{
    const active=btn.dataset.tab===tab;btn.classList.toggle('active',active);
    if(active)btn.setAttribute('aria-current','page');else btn.removeAttribute('aria-current');
  });
  window.scrollTo({top:0,behavior:'instant'});
  if(tab==='analytics'){renderAnalytics();setTimeout(()=>fitCapitalTileLabels($('capital-chart')),30);}
  if(tab==='home'){setTimeout(()=>state.chart?.resize(),40);}
  if(tab==='history')loadTrades();
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
  'EGLNL.XC':'/static/logos/ishares.png'
  'ADYEN.AS':'/static/logos/adyen.png'
  'AFFLE.NS':'/static/logos/affle.png'
  'ALV:DE':'/static/logos/allianz.png'
  'APLAPOLLO.NS':'/static/logos/aplapollo.png'
  'ASKAUTOLTD:NS':'/static/logos/askauto.png'
  'AXP':'/static/logos/axp.png'
  'AZN.ST':'/static/logos/azn.png'
  'ADYEN.AS':'/static/logos/adyen.png'
  'BAJFINANCE.NS':'/static/logos/bajajfin.png'
  'BONEX.ST':'/static/logos/bonesupport.png'
  'CAPLIPOINT.NS':'/static/logos/caplin.png'
  'CCL.NS':'/static/logos/ccl.png'
  'CPRT':'/static/logos/coprt.png'
  'DYVOX.ST':'/static/logos/dynavox.png'
  'G24.DE':'/static/logos/g24.png'
  'GALAXYSURF.NS':'/static/logos/galaxy.png'
  'GOOGL':'/static/logos/googl.png'
  'RMS.PA':'/static/logos/hermes.png'
  'HSCL.NS':'/static/logos/hscl.png'
  'ICRA.NS':'/static/logos/icra.png'
  'INOXINDIA.NS':'/static/logos/inoxindia.png'
  'IVSO.ST':'/static/logos/invisio.png'
  'SAP.DE':'/static/logos/sap.png'
  'VIT-B.ST':'/static/logos/vitec.png'
  'WRT1V.HE':'/static/logos/wartsila.png'
  'WKL.AS':'/static/logos/wkl.png'
  'KALYANKJIL.NS':'/static/logos/kalyan.png'
  'KFINTECH.NS':'/static/logos/kfin.png'
  'KOVAI.NS':'/static/logos/kovai.png'
  'POLYCAB.NS':'/static/logos/polycab.png'
  'KPCL.NS':'/static/logos/kpcl.png'
  'META':'/static/logos/meta.png'
  'MSFT':'/static/logos/microsoft.png'
  'NH.NS':'/static/logos/Narayana.png'
  'POLYMED.NS':'/static/logos/polymed.png'
  
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
function renderDashboard(){
  const d=state.dashboard;if(!d)return;
  const t=d.totals, base=d.base_currency;
  $('total-value').textContent=fmt(t.value,base,0);
  $('hero-ccy').textContent=base;
  $('main-change').innerHTML=`<span class="pnl-pill ${t.pnl<0?'negative':''}">${html(signedPct(t.pnl_pct))}</span><span class="muted">Unrealized gain / loss</span>`;
  $('invested-value').textContent=fmt(t.invested,base,0);
  $('unrealized-value').textContent=fmt(t.pnl,base,0);
  $('unrealized-value').classList.toggle('negative',t.pnl<0);
  $('holdings-count').textContent=state.hidden?'••':t.assets;
  $('holdings-badge').textContent=d.holdings.length;
  $('main-holdings').innerHTML=d.holdings.length?d.holdings.map(h=>{
    const key=JSON.stringify([h.asset,h.currency,h.type]);
    const expanded=expandedHomeHoldings.has(key);
    return `<div class="holding-row home-holding-row" role="button" tabindex="0" aria-expanded="${expanded}" data-holding-key="${html(key)}">${logo(h)}<div class="holding-info"><div class="holding-name">${html(h.asset)}</div><div class="holding-subtitle">${state.hidden?'••••':quantityFmt(h.quantity)} ${h.type==='Crypto'?'units':'shares'}</div><div class="holding-subtitle home-holding-prices" ${expanded?'':'hidden'}><div>Last Price: ${html(fmt(h.ltp,h.currency,2))}</div><div>Avg Price: ${html(fmt(h.avg,h.currency,2))}</div></div></div><div class="holding-value"><strong>${html(fmt(h.value,base,0))}</strong><span class="${(h.pnl||0)<0?'negative':''}">${html(signedPct(h.invested?100*(h.pnl||0)/h.invested:0))}</span></div></div>`;
  }).join(''):emptyMessage('Nothing in your portfolio yet','Add a trade or upload your existing trades CSV.','trade');
  activatePendingLogoSearch($('main-holdings'));
}
function toggleHomeHolding(row){
  const expanded=row.getAttribute('aria-expanded')!=='true';
  row.setAttribute('aria-expanded',String(expanded));
  row.querySelector('.home-holding-prices').hidden=!expanded;
  if(expanded)expandedHomeHoldings.add(row.dataset.holdingKey);
  else expandedHomeHoldings.delete(row.dataset.holdingKey);
}
function emptyMessage(title,desc,action){return `<div class="empty"><strong>${html(title)}</strong>${html(desc)}${action?`<button type="button" data-go="${action}">Get started →</button>`:''}</div>`;}
function renderAnalytics(){
  const d=state.dashboard;if(!d)return;
  $('analytics-n').textContent=`${d.holdings.length} assets`;
  $('analytics-holdings').innerHTML=d.holdings.length?d.holdings.map(h=>
    `<div class="analytics-entry"><div class="holding-row">${logo(h)}<div class="holding-info"><div class="holding-name">${html(h.asset)}</div></div><div class="holding-value"><strong>${html(fmt(h.value_trade,h.currency,2))}</strong><span class="${h.pnl_trade<0?'negative':''}">${html(fmt(h.pnl_trade,h.currency,2))}</span></div></div><div class="holding-details"><div><span>Quantity</span><b>${state.hidden?'••••':quantityFmt(h.quantity)}</b></div><div><span>Last price (${html(h.currency)})</span><b>${html(fmt(h.ltp,h.currency,2))}</b></div><div><span>Avg price (${html(h.currency)})</span><b>${html(fmt(h.avg,h.currency,2))}</b></div><div><span>Unrealized P/L (${html(h.currency)})</span><b>${html(fmt(h.pnl_trade,h.currency,2))}</b></div><div><span>Portfolio share</span><b>${state.hidden?'••••':compact(h.share,2)+'%'}</b></div><div><span>P/L (%)</span><b>${html(signedPct(h.pnl_trade_pct))}</b></div></div></div>`
  ).join(''):emptyMessage('No investments found','Your holdings appear here after you add trades.','trade');
  activatePendingLogoSearch($('analytics-holdings'));
  renderCapitalChart();
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
function renderChart(series){
  const target=$('history-chart');const points=series?.points||[];
  if(!points.length){if(state.chart){state.chart.clear();}target.innerHTML='<div class="empty" style="margin:30px 10px">Portfolio history will appear here when historical data is available.</div>';return;}
  if(state.chart && !target.querySelector('canvas')){state.chart.dispose();state.chart=null;target.innerHTML='';}
  const data=points.map(p=>[p.date,p.value]);
  if(typeof echarts==='undefined'){
    const ys=data.map(p=>p[1]);const min=Math.min(...ys);const max=Math.max(...ys);const width=640,height=230,pad=12;
    const path=ys.map((y,i)=>`${i?'L':'M'} ${(i/(ys.length-1||1)*width).toFixed(1)} ${(height-pad-(y-min)/(max-min||1)*(height-2*pad)).toFixed(1)}`).join(' ');
    target.innerHTML=`<svg viewBox="0 0 ${width} ${height}" style="width:100%;height:100%" preserveAspectRatio="none" role="img"><path d="${path}" fill="none" stroke="#59e4fc" stroke-width="3" vector-effect="non-scaling-stroke"/></svg>`;return;
  }
  if(!state.chart)state.chart=echarts.init(target);
  const rising=data[data.length-1][1]>=data[0][1];
  const color=rising?'#59e4fc':'#ed919b';
  state.chart.setOption({animationDuration:340,grid:{left:4,right:5,top:16,bottom:12,containLabel:false},
    xAxis:{type:'time',boundaryGap:false,axisLine:{show:false},axisTick:{show:false},axisLabel:{show:false},splitLine:{show:false},min:data[0][0],max:data[data.length-1][0]},
    yAxis:{type:'value',scale:true,axisLabel:{show:false},axisLine:{show:false},axisTick:{show:false},splitLine:{show:false}},
    tooltip:{trigger:'axis',confine:true,backgroundColor:state.theme==='dark'?'#23262c':'#fff',borderWidth:1,borderColor:state.theme==='dark'?'#343941':'#dfe4dc',textStyle:{color:state.theme==='dark'?'#edf3fc':'#252a24',fontSize:14},
      formatter:(items)=>{if(!items?.length)return '';const p=items[0];return `<div style="font-size:13px;opacity:.7;margin-bottom:5px">${html(new Date(p.value[0]).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}))}</div><b>${html(fmt(p.value[1],state.dashboard.base_currency,0))}</b>`;}},
    series:[{type:'line',data,symbol:'none',sampling:'lttb',smooth:false,lineStyle:{color,width:2.5},
      areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:rising?'rgba(85,184,237,.19)':'rgba(237,145,155,.16)'},{offset:1,color:rising?'rgba(85,184,237,0)':'rgba(237,145,155,0)'}])}}]
  },true);
  state.chart.resize();
}
async function loadRange(span){
  state.historyRange=span;document.querySelectorAll('[data-range]').forEach(el=>el.classList.toggle('selected',el.dataset.range===span));
  if(state.history[span]){renderChart(state.history[span]);return;}
  $('history-loading').classList.remove('hidden');
  const key=span;
  try{const data=await api('/api/history?span='+encodeURIComponent(span));state.history[key]=data;
    if(state.historyRange===span)renderChart(data);
  }catch(e){toast('Portfolio history: '+e.message,true);}
  finally{if(state.historyRange===span)$('history-loading').classList.add('hidden');}
}
async function reloadDashboard(showMessage=false){
  const d=await api('/api/dashboard');
  if(state.dashboard && (state.dashboard.version!==d.version || state.dashboard.base_currency!==d.base_currency))state.history={};
  state.dashboard=d;renderDashboard();renderAnalytics();
  if(showMessage){toast(d.warnings.length?'Updated. '+d.warnings[0]:'Portfolio updated');}
  if(state.view==='home')loadRange(state.historyRange);
}
async function refreshPortfolio(){
  const btn=$('refresh-btn');btn.classList.add('is-refreshing');btn.disabled=true;
  try{const result=await api('/api/refresh',{method:'POST'});await reloadDashboard();
    const failures=Object.keys(result.errors||{});
    toast(`Updated ${result.updated}/${result.requested??0} latest prices${failures.length?`; unavailable: ${failures.join(', ')}`:''}`,!!failures.length);}
  catch(e){toast(e.message,true);}finally{btn.classList.remove('is-refreshing');btn.disabled=false;}
}
function formTrade(form){const fd=new FormData(form);const val=Object.fromEntries(fd.entries());
  for(const f of ['quantity','price_trade_ccy','charges_trade_ccy','stt_trade_ccy','fx_to_base','last_price_trade_ccy','last_price_base'])val[f]=Number(val[f]??(f==='fx_to_base'?1:0));
  val.trade_date=val.trade_date.includes('T')?val.trade_date.replace('T',' '):val.trade_date+' 00:00';return val;
}
function updateTradePreview(){const f=$('trade-form');let q=Number(f.elements.quantity.value)||0,price=Number(f.elements.price_trade_ccy.value)||0;
  $('trade-total').querySelector('strong').textContent=fmt(q*price,f.elements.trade_ccy.value.toUpperCase()||'USD',2);
}
async function submitTrade(e){e.preventDefault();const form=e.currentTarget;const btn=form.querySelector('[type=submit]');
  try{btn.disabled=true;const payload={...formTrade(form),side:state.tradeSide,auto_fx:$('trade-auto-fx').checked};
    await api('/api/trades',{method:'POST',body:payload});form.reset();form.elements.trade_date.value=todayISO();$('trade-auto-fx').checked=true;$('trade-manual-fx').disabled=true;
    state.history={};state.trades=[];await reloadDashboard();toast('Trade saved successfully');nav('home');
  }catch(err){toast(err.message,true);}finally{btn.disabled=false;updateTradePreview();}
}
async function loadTrades(){
  try{const result=await api('/api/trades');state.trades=result.rows;renderTrades();}
  catch(e){toast(e.message,true);}
}
function renderTrades(){
  const target=$('trades-list');if(!target)return;
  const query=$('history-search').value.trim().toLowerCase();
  const rows=state.trades.filter(r=>`${r.asset} ${r.ticker} ${r.trade_ccy} ${r.side}`.toLowerCase().includes(query));
  $('trade-count').textContent=`${rows.length} trades`;
  target.innerHTML=rows.length?rows.slice(0,state.tradeLimit).map(r=>{
    const total=r.price_trade_ccy*r.quantity;const buy=r.side==='BUY';
    return `<button class="trade-row" data-edit="${html(r.trade_id)}" type="button"><span class="trade-side ${buy?'buy':'sell'}">${buy?'↗':'↘'}</span><span class="trade-info"><strong>${html(r.asset)} <span class="side-label ${buy?'buy':'sell'}">${buy?'BUY':'SELL'}</span></strong><span>${html(r.trade_date.slice(0,10))} · ${state.hidden?'••••':quantityFmt(r.quantity)} units · ${html(r.ticker||r.asset_type)}</span></span><span class="trade-amount"><strong>${html(fmt(total,r.trade_ccy,2))}</strong><small>${html(fmt(r.price_trade_ccy,r.trade_ccy,2))}/unit</small></span></button>`;
  }).join(''):emptyMessage('No trades found','Try another search or add a new transaction.',null);
  $('load-more-trades').classList.toggle('hidden',rows.length<=state.tradeLimit);
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
    await loadTrades();await reloadDashboard();toast('Trade updated');}
  catch(err){toast(err.message,true);}
}
async function deleteEdit(){const row=state.trades.find(t=>t.trade_id===state.editId);if(!row)return;
  if(!confirm(`Delete ${row.side} ${row.asset} on ${row.trade_date.slice(0,10)}?`))return;
  try{await api('/api/trades/'+encodeURIComponent(state.editId),{method:'DELETE'});$('edit-dialog').close();
    await loadTrades();await reloadDashboard();toast('Trade deleted');}catch(e){toast(e.message,true);}
}
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
  $('tax-count').textContent=`${report.rows.length} assets`;
  $('tax-list').innerHTML=report.rows.length?report.rows.map(r=>{
    const ccy=trade?r.currency:base;
    return `<div class="tax-entry"><div><strong>${html(r.asset)}</strong><strong class="tax-pnl ${r.realized<0?'negative':''}">${html(fmt(r.realized,ccy,2))}</strong></div><small>Sold: ${quantityFmt(r.quantity_sold)} · Cost ${html(fmt(r.buy_value,ccy,2))} · Proceeds ${html(fmt(r.sell_value,ccy,2))}</small></div>`;
  }).join(''):emptyMessage('No realized trades','Sales for the chosen dates will appear here.',null);
  $('tax-note').textContent=(base==='INR'&&!trade?'INR reporting excludes STT from both FIFO cost and proceeds. ':'')+
    (trade?'Values are not summed because their trade currencies differ. ':'')+'Tax information is informational and should be verified before filing.';
}
function renderAccount(){
  if(!state.user)return;
  $('profile-name').textContent=state.user.name;
  $('profile-type').textContent=state.user.google?'Google account':'Local account';
  $('account-login-method').textContent=state.user.google?'Google sign-in':'Local username and password';
  $('account-google').classList.toggle('hidden',state.user.google||!state.user.google_enabled);
  $('profile-avatar').textContent=(state.user.name||'U')[0].toUpperCase();
  $('avatar-char').textContent=(state.user.name||'U')[0].toUpperCase();
  $('base-select').value=state.dashboard?.base_currency||state.user.base_currency;
  document.querySelectorAll('.base-label').forEach(el=>el.textContent=state.dashboard?.base_currency||state.user.base_currency);
}
async function changeBase(e){const newBase=e.target.value;const oldBase=state.dashboard.base_currency;
  if(newBase===oldBase)return;
  e.target.disabled=true;
  try{await api('/api/settings',{method:'PUT',body:{base_currency:newBase}});state.history={};
    await reloadDashboard();renderAccount();toast('Base currency changed to '+newBase);
  }catch(error){e.target.value=oldBase;toast(error.message,true);}
  finally{e.target.disabled=false;}
}
async function importFile(e){const file=e.target.files[0];if(!file)return;
  if(!confirm(`Replace your current portfolio with ${file.name}? This overwrites existing trades. Download a backup first if needed.`)) {e.target.value='';return;}
  const form=new FormData();form.append('file',file);
  try{const result=await api('/api/import',{method:'POST',body:form});state.history={};state.trades=[];
    await reloadDashboard();toast(`Imported ${result.count} trades successfully`);nav('home');}
  catch(err){toast(err.message,true);}finally{e.target.value='';}
}
function bindEvents(){
  bindAssetLogoImages();
  document.querySelectorAll('[data-tab]').forEach(el=>el.addEventListener('click',()=>nav(el.dataset.tab)));
  document.addEventListener('click',e=>{const target=e.target.closest('[data-go]');if(target)nav(target.dataset.go);});
  document.querySelectorAll('[data-range]').forEach(el=>el.addEventListener('click',()=>loadRange(el.dataset.range)));
  $('main-holdings').addEventListener('click',e=>{
    const row=e.target.closest('.home-holding-row');
    if(row)toggleHomeHolding(row);
  });
  $('main-holdings').addEventListener('keydown',e=>{
    if(e.key!=='Enter'&&e.key!==' ')return;
    const row=e.target.closest('.home-holding-row');
    if(row){e.preventDefault();toggleHomeHolding(row);}
  });
  $('refresh-btn').addEventListener('click',refreshPortfolio);
  $('capital-reset').addEventListener('click',()=>{state.capitalFocus=null;renderCapitalChart(true);});
  $('capital-chart').addEventListener('click',e=>{
    const tile=e.target.closest('[data-capital-asset]');
    if(tile)toggleCapitalFocus(tile.dataset.capitalAsset);
  });
  $('privacy-toggle').addEventListener('click',()=>toggleHidden(!state.hidden));
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
  document.querySelectorAll('[data-side]').forEach(el=>el.addEventListener('click',()=>{
    state.tradeSide=el.dataset.side;document.querySelectorAll('[data-side]').forEach(x=>x.classList.toggle('selected',x===el));
  }));
  $('trade-auto-fx').addEventListener('change',e=>{ $('trade-manual-fx').disabled=e.target.checked; });
  $('history-search').addEventListener('input',()=>{state.tradeLimit=55;renderTrades();});
  $('load-more-trades').addEventListener('click',()=>{state.tradeLimit+=55;renderTrades();});
  $('trades-list').addEventListener('click',e=>{const row=e.target.closest('[data-edit]');if(row)openEdit(row.dataset.edit);});
  $('close-dialog').addEventListener('click',()=>$('edit-dialog').close());
  $('edit-dialog').addEventListener('click',e=>{if(e.target===$('edit-dialog'))$('edit-dialog').close();});
  $('edit-form').addEventListener('submit',submitEdit);
  $('delete-trade').addEventListener('click',deleteEdit);
  $('tax-period').addEventListener('change',e=>{$('tax-custom').classList.toggle('hidden',e.target.value!=='custom');loadTax();});
  for(const id of ['tax-mode','tax-start','tax-end'])$(id).addEventListener('change',loadTax);
  $('tax-export').addEventListener('click',()=>{const [start,end]=taxDates();window.location.href=`/api/export/tax?start=${start}&end=${end}&mode=${$('tax-mode').value}`;});
  $('base-select').addEventListener('change',changeBase);
  $('file-export').addEventListener('click',()=>{window.location.href='/api/export/trades';});
  $('file-import').addEventListener('change',importFile);
  $('logout-btn').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});state.user=null;state.dashboard=null;state.trades=[];state.history={};expandedHomeHoldings.clear();showAuth();toast('Signed out');}catch(e){toast(e.message,true);}});
  window.addEventListener('resize',()=>{state.chart?.resize();fitCapitalTileLabels($('capital-chart'));});
}
async function startLoggedIn(){
  state.user=await api('/api/me');if(!state.user.authenticated){showAuth();return;}
  $('avatar-char').textContent=state.user.name[0].toUpperCase();
  renderAccount();showApp();nav('home');
  try{await reloadDashboard();}catch(error){toast(error.message,true);}
}
async function init(){bindEvents();applyTheme(state.theme);toggleHidden(state.hidden);
  try{const account=await api('/api/me');
    $('google-container').classList.toggle('hidden',!account.google_enabled);
    $('google-hint').classList.toggle('hidden',!!account.google_enabled);
    if(account.authenticated){state.user=account;await startLoggedIn();}else showAuth();
  }catch(error){showAuth();toast(error.message,true);}
}
init();
