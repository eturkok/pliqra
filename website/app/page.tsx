"use client";
import {useEffect, useRef, useState} from "react";
import {ArrowRight, ArrowUpRight, Check, Copy, Fingerprint, GitBranch, KeyRound, Layers, Menu, Network, Play, ShieldCheck, SlidersHorizontal, Terminal, Workflow, X, Activity, Database, Plug} from "lucide-react";
import ProductDemo from "../components/product-demo";

const REPO="https://github.com/eturkok/pliqra";
const docs=(file:string)=>`${REPO}/blob/main/docs/${file}.md`;
const examples=[`mkdir pliqra && cd pliqra
curl -fL -o compose.release.yaml https://github.com/eturkok/pliqra/releases/download/v2.1.0/compose.release.yaml
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d`, `python -m pip install pliqra-client==0.1.1
# Set PLIQRA_API_KEY to a scoped application key.

import os
from pliqra_client import Pliqra

client = Pliqra("http://localhost:8088", os.environ["PLIQRA_API_KEY"])
service = client.connect("support", ["chat", "streaming"])
for packet in service.stream([{"role": "user", "content": "Hello"}]):
    for choice in packet.get("choices", []):
        print(choice.get("delta", {}).get("content", ""), end="")`, `npm install @eturkok/pliqra@0.1.1
// Set PLIQRA_API_KEY to a scoped application key.

import {Pliqra} from '@eturkok/pliqra';

const client = new Pliqra('http://localhost:8088', process.env.PLIQRA_API_KEY);
const service = await client.connect('support', {
  requirements: ['chat', 'streaming']
});
for await (const packet of service.stream([{role: 'user', content: 'Hello'}])) {
  process.stdout.write(packet.choices?.[0]?.delta?.content || '');
}`];

function CopyButton({text,label="Copy installation commands"}:{text:string;label?:string}){
  const [state,setState]=useState("Copy");
  const timer=useRef<ReturnType<typeof setTimeout>|null>(null);
  useEffect(()=>()=>{if(timer.current)clearTimeout(timer.current)},[]);
  async function copy(){try{if(timer.current)clearTimeout(timer.current);await navigator.clipboard.writeText(text);setState("Copied");timer.current=setTimeout(()=>setState("Copy"),2200)}catch{setState("Select text to copy")}}
  return <button className="copy-button" onClick={copy} aria-label={state==="Copy"?label:state}>{state==="Copied"?<Check size={14}/>:<Copy size={14}/>}<span aria-live="polite">{state}</span></button>
}

const capabilities=[
  {icon:Layers,name:"Serve the interfaces your apps use",text:"Chat, embeddings, speech and transcription, Responses, Anthropic Messages and Ollama. Live native text/tool streams, within documented API subsets.",guide:"MODEL_INTERFACES"},
  {icon:SlidersHorizontal,name:"Build your own gateways",text:"Service selection, project/team grants, request and token windows, budgets, concurrency, input/output bounds and scoped application keys.",guide:"GATEWAYS"},
  {icon:ShieldCheck,name:"Compose your guardrails",text:"Local PII, keyword and schema checks. NeMo check APIs and custom HTTP validators. Ordered input/output stages, enforce/observe modes and visible verdicts.",guide:"GATEWAYS"},
  {icon:KeyRound,name:"Bring identity and secrets",text:"OIDC, SAML, SCIM, workload JWT federation and project/team permissions. Environment, mounted-file and Vault secret references.",guide:"IDENTITY_COMPATIBILITY"},
  {icon:Activity,name:"Explain usage and estimated cost",text:"Cumulative administrator reporting, team and site filters, per-attempt routing, token accounting, unknown-usage handling and exports.",guide:"ACCESS"},
  {icon:Workflow,name:"Publish with fresh evidence",text:"Immutable prompt versions, durable batches, external judges, evaluations, quality gates and controlled canary traffic.",guide:"WORKFLOWS"},
  {icon:Network,name:"Operate across active sites",text:"One writable PostgreSQL authority, strict shared admission, local-first placement, site drain, per-site health and safe fenced-process recovery.",guide:"MULTI_DC"},
  {icon:GitBranch,name:"Govern independent installations",text:"Explicit pairing, combined metadata visibility, resource mapping, second-administrator approval, rollout receipts and policy drift.",guide:"FLEET"},
  {icon:Fingerprint,name:"Connect through explicit contracts",text:"Discovery, capability/extension negotiation, fingerprinted bindings and deliberate renewal. Python, JavaScript/TypeScript and Go clients; draft protocol and executable core profile.",guide:"CONFORMANCE"},
  {icon:Database,name:"Keep operations recoverable",text:"Encrypted backups, recovery tools, monitoring, audit trails and retention controls. SQLite by default; PostgreSQL, organizations and analytics when needed.",guide:"OPERATIONS"},
];

const reasons = [
  {
    icon: Fingerprint,
    context: "SUPPORT APPLICATION",
    title: "Make a model swap visible.",
    situation: "Your support app relies on tool calling. An administrator changes the registered endpoint, capabilities or gateway policy.",
    decision: "Bound calls return 409 ContractChanged. The application rediscovers the service and explicitly accepts the new contract.",
    outcome: "Keep the accepted configuration visible to the application.",
    guide: "PROTOCOL",
    link: "Read the contract flow",
  },
  {
    icon: GitBranch,
    context: "THREE INDEPENDENT WORKSPACES",
    title: "Review a policy before it travels.",
    situation: "Finance needs the same token limits and guardrails across three installations with separate databases.",
    decision: "Prepare a plan for each target. A different administrator approves the exact change; delivery receipts and drift stay visible.",
    outcome: "Coordinate changes while each workspace keeps its own authority.",
    guide: "FLEET",
    link: "See fleet approvals",
  },
  {
    icon: Workflow,
    context: "A CANDIDATE MODEL RELEASE",
    title: "Put evidence before a rollout.",
    situation: "You want to send 10% of traffic to a candidate model without accepting an unmeasured regression.",
    decision: "Require fresh comparison results and configured quality gates. A changed dataset, judge or service invalidates the old evidence.",
    outcome: "Make rollout decisions against the configuration you evaluated.",
    guide: "WORKFLOWS",
    link: "See release gates",
  },
];

function tabKey(event:React.KeyboardEvent,index:number,count:number,select:(index:number)=>void){
  let next=index; if(event.key==="ArrowRight")next=(index+1)%count; else if(event.key==="ArrowLeft")next=(index+count-1)%count; else if(event.key==="Home")next=0; else if(event.key==="End")next=count-1; else return;
  event.preventDefault();select(next);event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
}

export default function Home(){
  const [menu,setMenu]=useState(false),[installTab,setInstallTab]=useState(0),[architecture,setArchitecture]=useState(0);
  const menuButton=useRef<HTMLButtonElement|null>(null);
  useEffect(()=>{if(!menu)return;function escape(e:KeyboardEvent){if(e.key==="Escape"){setMenu(false);menuButton.current?.focus()}}document.addEventListener("keydown",escape);return()=>document.removeEventListener("keydown",escape)},[menu]);
  return <>
    <a href="#main" className="skip-link">Skip to content</a>
    <header className="site-header"><div className="nav-shell"><a className="brand" href="#" aria-label="Pliqra home"><img src="/brand.svg" width="34" height="34" alt=""/>pliqra<span className="brand-mark">/</span></a><button ref={menuButton} className="mobile-menu" aria-label={menu?"Close navigation":"Open navigation"} aria-expanded={menu} aria-controls="site-nav" onClick={()=>setMenu(!menu)}>{menu?<X size={22}/>:<Menu size={22}/>}</button><nav id="site-nav" className={menu?"open":""} aria-label="Main navigation"><a href="#why" onClick={()=>setMenu(false)}>Why Pliqra</a><a href="#product" onClick={()=>setMenu(false)}>Capabilities</a><a href="#demo" onClick={()=>setMenu(false)}>Interactive demo</a><a href={docs("DISTRIBUTION")} target="_blank" rel="noreferrer">Docs <ArrowUpRight size={12}/></a><a href={REPO} className="nav-github" target="_blank" rel="noreferrer"><GitBranch size={16}/>GitHub</a><a className="nav-start" href="#install" onClick={()=>setMenu(false)}>Get started <ArrowUpRight size={14}/></a></nav></div></header>
    <main id="main" tabIndex={-1}>
      <section className="hero" aria-labelledby="hero-title"><div className="hero-grid" aria-hidden="true"/><div className="shell hero-content"><div className="hero-copy"><a className="release-pill" href={`${REPO}/releases/tag/v2.1.0`} target="_blank" rel="noreferrer"><span>2.1.0</span>Stable community release<ArrowUpRight size={12}/></a><h1 id="hero-title">Your models.<br/>Your rules.<br/><em>One clear contract.</em></h1><p className="hero-description">Turn a deployed model endpoint into a governed AI service. Make contract changes explicit, review fleet policies and gate model releases on fresh evidence.</p><div className="hero-actions"><a className="button primary" href="#demo"><Play size={15} fill="currentColor"/>Explore the demo<ArrowRight size={16}/></a><a className="button quiet" href="#install">Install Pliqra<ArrowUpRight size={16}/></a></div><p className="hero-note">Self-hosted<span>·</span>Apache-2.0<span>·</span>One Docker image to start</p></div><div className="hero-visual" aria-label="Model endpoints connect through Pliqra governance to application contracts"><div className="graph-heading"><span className="tiny-label">YOUR ENDPOINTS → YOUR AI SERVICES</span><span className="graph-status"><i/>Policy active</span></div><div className="graph-endpoints"><span><Plug size={16}/>OpenAI-compatible</span><span><Terminal size={16}/>Ollama / native APIs</span></div><div className="connection-lines" aria-hidden="true"><i/><i/><b/></div><div className="graph-core"><img src="/brand.svg" alt="" width="48" height="48"/><div><strong>pliqra</strong><span>Route. Govern. Explain.</span></div><SlidersHorizontal size={20}/></div><div className="graph-policies"><span><ShieldCheck size={13}/>Guardrails</span><span><KeyRound size={13}/>Scoped access</span><span><Activity size={13}/>Usage & cost</span></div><div className="graph-wire" aria-hidden="true"><i/></div><div className="graph-contract"><div><Fingerprint size={19}/><strong>support-chat</strong><span className="contract-state">Bound</span></div><p className="mono">chat · streaming · tools</p><div className="fingerprint-row"><span>Contract fingerprint</span><code>fp_a91c</code></div></div><div className="graph-foot"><Check size={14}/><span>Applications know what they agreed to.</span></div><div className="floating-label"><span className="mono">POST /v1/chat/completions</span><strong>200 <Check size={12}/></strong></div></div></div><div className="hero-bottom shell"><span>FROM ENDPOINT TO APPLICATION</span><div><span>Connect</span><ArrowRight size={13}/><span>Configure</span><ArrowRight size={13}/><span>Bind</span><ArrowRight size={13}/><span>Understand every call</span></div></div></section>

      <section className="section why-section" id="why" aria-labelledby="why-title">
        <div className="shell">
          <div className="section-top"><p className="eyebrow">WHY CHOOSE PLIQRA</p><span className="section-index">01 / REAL DECISIONS</span></div>
          <div className="section-intro"><h2 id="why-title">Keep control<br/>when things change.</h2><p>Choose Pliqra when applications need explicit contracts, independent installations need reviewed policies, and model releases need current evidence.</p></div>
          <div className="choice-grid">
            {reasons.map((reason, index) => <article className="choice-card" key={reason.guide}>
              <div className="choice-card-top"><span className="number">0{index + 1}</span><reason.icon size={22} aria-hidden="true"/></div>
              <p className="choice-context">{reason.context}</p>
              <h3>{reason.title}</h3>
              <p className="choice-situation">{reason.situation}</p>
              <div className="choice-decision"><p className="mini-label">THE PLIQRA DECISION</p><p>{reason.decision}</p></div>
              <p className="choice-outcome">{reason.outcome}</p>
              <a className="text-link" href={docs(reason.guide)} target="_blank" rel="noreferrer">{reason.link}<ArrowUpRight size={14}/></a>
            </article>)}
          </div>
          <p className="choice-boundary">Contract binding is opt-in and tracks registered service and policy configuration. Evaluation gates cover the selected cases and evaluator. Independent fleet members keep separate databases and quotas.</p>
        </div>
      </section>

      <section className="contract-section"><div className="shell contract-layout"><div><p className="eyebrow">THE PLIQRA APPROACH</p><h2>Make the agreement<br/><em>part of the connection.</em></h2><p>Applications can bind to a fingerprint of the service’s capabilities and gateway policy. A changed contract returns HTTP 409, so the application can deliberately rediscover and renew.</p><ul className="check-list"><li><Check size={16}/>Declared capabilities and verified behavior stay separate.</li><li><Check size={16}/>Routing and policy decisions remain inspectable.</li><li><Check size={16}/>The same service can be governed through the UI or API.</li></ul><a className="text-link" href={docs("PROTOCOL")} target="_blank" rel="noreferrer">Explore the connection protocol <ArrowUpRight size={15}/></a><p className="protocol-note">Protocol 0.1 is a draft. Binding is opt-in; standard supported inference APIs are also available.</p></div><div className="contract-terminal"><div className="terminal-top"><Terminal size={15}/><span>application → support-chat</span></div><div className="terminal-body"><p><span className="code-muted">01</span><span className="code-orange">discover</span><span>chat, streaming, tools</span></p><p><span className="code-muted">02</span><span className="code-orange">bind</span><span>fingerprint: fp_a91c</span></p><div className="terminal-event"><GitBranch size={15}/>Model or policy contract changes</div><p><span className="code-muted">03</span><span>invoke</span><span className="code-warn">409 ContractChanged</span></p><p><span className="code-muted">04</span><span className="code-orange">renew</span><span>Explicit application decision</span></p><div className="terminal-success"><Check size={14}/><span>New agreement accepted. Continue.</span></div></div><div className="terminal-caption">Illustrative protocol exchange · try it below</div></div></div></section>

      <section className="section demo-section" id="demo"><div className="shell"><div className="section-top"><p className="eyebrow">TAKE THE CONTROLS</p><span className="section-index">02 / INTERACTIVE DEMO</span></div><div className="section-intro"><h2>Meet the whole<br/>operating layer.</h2><p>Eight short scenarios. Change a contract, exhaust a quota, inspect a guardrail, drain a site or review a fleet policy. No account or running model needed.</p></div><ProductDemo/></div></section>

      <section className="section capabilities-section" id="product"><div className="shell"><div className="section-top"><p className="eyebrow">THE CAPABILITY MAP</p><span className="section-index">03 / PRODUCT</span></div><div className="section-intro"><h2>A catalog, a gateway<br/>and the controls around it.</h2><p>The daily gateway essentials stay together: routing, scoped access, limits, guardrails and usage visibility. Configure in the console or automate through the API.</p></div><div className="capability-grid">{capabilities.map((c,i)=><article key={c.name}><div className="capability-icon"><c.icon size={20}/><span>{String(i+1).padStart(2,"0")}</span></div><h3>{c.name}</h3><p>{c.text}</p><a href={docs(c.guide)} target="_blank" rel="noreferrer">Read the guide <ArrowUpRight size={13}/></a></article>)}</div></div></section>

      <section className="section architecture-section"><div className="shell"><div className="section-top"><p className="eyebrow">YOUR INFRASTRUCTURE</p><span className="section-index">04 / TOPOLOGY</span></div><div className="section-intro"><h2>One image to begin.<br/>Room for what comes next.</h2><p>Pliqra runs alongside your already deployed models. A single installation starts with SQLite; shared deployments add PostgreSQL and your existing infrastructure.</p></div><div className="architecture-tabs" role="tablist" aria-label="Deployment architecture">{["Single installation","Active/active sites","Independent fleet"].map((t,i)=><button key={t} role="tab" tabIndex={architecture===i?0:-1} onKeyDown={e=>tabKey(e,i,3,setArchitecture)} aria-selected={architecture===i} aria-controls="architecture-panel" id={`architecture-tab-${i}`} onClick={()=>setArchitecture(i)}>{t}</button>)}</div><div className="architecture-panel" id="architecture-panel" role="tabpanel" aria-labelledby={`architecture-tab-${architecture}`}><div className="architecture-nodes">{architecture===0?<><div><Terminal/><strong>Your apps</strong><small>SDKs or supported APIs</small></div><span>→</span><div className="highlight-node"><img src="/brand.svg" alt="" width="30" height="30"/><strong>Pliqra</strong><small>Gateway + console + SQLite</small></div><span>→</span><div><Layers/><strong>Your model endpoints</strong><small>Already deployed</small></div></>:architecture===1?<><div><Network/><strong>West + East gateways</strong><small>Active/active, local preference</small></div><span>↔</span><div className="highlight-node"><Database/><strong>One PostgreSQL writer</strong><small>Strict shared admission</small></div><span>↔</span><div><Layers/><strong>Site-local models</strong><small>Placement + allowed fallback</small></div></>:<><div><GitBranch/><strong>Fleet controller</strong><small>Metadata + reviewed policy</small></div><span>→</span><div className="highlight-node"><img src="/brand.svg" alt="" width="30" height="30"/><strong>Independent workspaces</strong><small>Separate databases and quotas</small></div><span>→</span><div><ShieldCheck/><strong>Per-member receipts</strong><small>Delivery + drift visibility</small></div></>}</div><p>{architecture===0?"Persist the data volume and encryption key. One SQLite process owns each volume. Configure HTTPS before exposing a workspace remotely.":architecture===1?"Your infrastructure supplies PostgreSQL HA and global ingress. Isolated gateways stop new admission; there are no offline quota grants or automatic replays of started inference.":"Fleet collects scoped metadata and distributes reviewed changes. It does not merge independent quota pools or create an exact global billing ledger."}</p><a className="text-link" href={docs(architecture===0?"OPERATIONS":architecture===1?"MULTI_DC":"FLEET")} target="_blank" rel="noreferrer">Deployment details <ArrowUpRight size={14}/></a></div></div></section>

      <section className="proof-section"><div className="shell"><div className="proof-heading"><p className="eyebrow">SHIPPED WITH EVIDENCE</p><h2>A stable release.<br/>Checks you can inspect.</h2></div><div className="proof-grid"><div><strong>9,000<span>/9,000</span></strong><p>Successful synthetic soak calls</p></div><div><strong>30<span> min</span></strong><p>Continuous synthetic acceptance</p></div><div><strong>113<span> ms</span></strong><p>Observed synthetic p95 latency</p></div></div><div className="proof-note"><p>2.1.0 acceptance · 7 November 2026. One host, 4 CPUs, PostgreSQL 18 and a deterministic 5 ms model fixture. Native AMD64/ARM64 smoke, model/database interruption recovery and RC4 SQLite upgrade/backup recovery also passed. Bounded regression evidence, not production sizing or an availability SLA.</p><a href={docs("STABLE_2_1")} target="_blank" rel="noreferrer">Inspect the evidence <ArrowUpRight size={15}/></a></div></div></section>

      <section className="section install-section" id="install"><div className="shell install-layout"><div><p className="eyebrow">MAKE YOUR FIRST CONNECTION</p><h2>Bring an endpoint.<br/><em>Start here.</em></h2><p className="install-description">Use the ready-made Linux AMD64/ARM64 image. Docker Desktop runs the matching Linux image on macOS or Windows. No source build or registry login required.</p><ol className="install-steps"><li><span>1</span><div><strong>Start Pliqra.</strong><p>Docker with Compose is required. Run the commands, then open <code>localhost:8088</code>.</p></div></li><li><span>2</span><div><strong>Connect your model.</strong><p>Create the administrator and add a reachable endpoint. Host-based models use <code>host.docker.internal</code>.</p></div></li><li><span>3</span><div><strong>Publish and connect.</strong><p>Configure the service and gateway, create a scoped key, then connect your application.</p></div></li></ol><a className="text-link" href={docs("GETTING_STARTED")} target="_blank" rel="noreferrer">Complete first-call guide <ArrowUpRight size={15}/></a></div><div className="install-code"><div className="install-tabs" role="tablist" aria-label="Installation examples">{["Docker Compose","Python SDK","JavaScript / TS"].map((t,i)=><button key={t} role="tab" tabIndex={installTab===i?0:-1} onKeyDown={e=>tabKey(e,i,3,setInstallTab)} id={`install-tab-${i}`} aria-controls="install-panel" aria-selected={installTab===i} onClick={()=>setInstallTab(i)}>{t}</button>)}</div><div className="code-header"><span>{["TERMINAL / PLIQRA 2.1.0","PYTHON 3.10+ / SDK 0.1.1","NODE 22+ / SDK 0.1.1"][installTab]}</span><CopyButton text={installTab===0?examples[0]:examples[installTab].split("\n")[0]}/></div><pre id="install-panel" role="tabpanel" aria-labelledby={`install-tab-${installTab}`} tabIndex={0}><code>{installTab===0?examples[0]:examples[installTab].split("\n")[0]}</code></pre>{installTab>0&&<><div className="client-example-note">Set <code>PLIQRA_API_KEY</code>, replace <code>support</code> with your published service, then save this as <code>{installTab===1?"app.py":"app.mjs"}</code>. Run with <code>{installTab===1?"python app.py":"node app.mjs"}</code>.</div><div className="code-header client-code-header"><span>{installTab===1?"APP.PY / PYTHON":"APP.MJS / JAVASCRIPT"}</span><CopyButton text={examples[installTab].split("\n").slice(3).join("\n")} label="Copy client example"/></div><pre className="client-code" tabIndex={0}><code>{examples[installTab].split("\n").slice(3).join("\n")}</code></pre></>}<div className="code-footer"><ShieldCheck size={15}/>{installTab===0?"Loopback by default. Persist the volume; configure HTTPS for remote access.":"Use a scoped application key. Handle ContractChanged explicitly."}</div><a className="install-reference" href={docs(installTab===0?"DISTRIBUTION":"SDK")} target="_blank" rel="noreferrer">{installTab===0?"Image digests, checksums and upgrade guide":"Package registries, clients and SDK installation"}<ArrowUpRight size={14}/></a></div></div></section>

      <section className="section faq-section"><div className="shell faq-layout"><div><p className="eyebrow">A FEW CLEAR ANSWERS</p><h2>Know the boundaries.<br/>Keep the control.</h2></div><div className="faq-list"><details><summary>Does Pliqra deploy or train models?<span aria-hidden="true">+</span></summary><div>Pliqra connects to already running model endpoints. You manage GPU capacity, model serving and training; Pliqra governs access, routing, contracts and usage.</div></details><details><summary>What changes when an application binds to a contract?<span aria-hidden="true">+</span></summary><div>A bound application accepts a fingerprint of its service capabilities and applicable gateway policy. A changed contract returns HTTP 409 and the application explicitly renews. Supported inference APIs are also available without protocol binding.</div></details><details><summary>Does the demo make real model calls?<span aria-hidden="true">+</span></summary><div>No. Scenarios run entirely in your browser with illustrative values. Console images are actual screenshots from synthetic workspaces. No account or credential is needed.</div></details><details><summary>How do multi-DC quotas stay strict?<span aria-hidden="true">+</span></summary><div>Active gateways use one shared writable PostgreSQL authority. A site that cannot reach it stops new admission. Your deployment supplies database HA, durable promotion and ingress. <a href={docs("MULTI_DC")} target="_blank" rel="noreferrer">Read the consistency and recovery boundaries.</a></div></details><details><summary>Can I bring my identity provider and guardrails?<span aria-hidden="true">+</span></summary><div>Pliqra implements documented OIDC, SAML and SCIM behavior and calls configured NeMo check APIs or custom HTTP validators. Actual vendor tenants and guardrail deployments require environment-specific acceptance. <a href={docs("IDENTITY_COMPATIBILITY")} target="_blank" rel="noreferrer">See the compatibility matrix.</a></div></details><details><summary>Is Pliqra free for commercial use?<span aria-hidden="true">+</span></summary><div>Pliqra is licensed under Apache-2.0, allowing commercial use, modification and redistribution under its terms. Community support is best-effort. <a href={`${REPO}/blob/main/LICENSE`} target="_blank" rel="noreferrer">Read the license</a> and <a href={docs("SUPPORT")} target="_blank" rel="noreferrer">support envelope.</a></div></details></div></div></section>

      <section className="closing-section"><div className="shell"><p className="eyebrow">MODELS WILL CHANGE. KEEP THE AGREEMENT VISIBLE.</p><h2>Connect with confidence.<br/><em>Keep your rules in reach.</em></h2><div className="closing-actions"><a className="button primary" href="#install">Get started with Pliqra <ArrowRight size={16}/></a><a className="button quiet" href={REPO} target="_blank" rel="noreferrer"><GitBranch size={17}/>Explore the source <ArrowUpRight size={15}/></a></div></div></section>
    </main>
    <footer className="site-footer"><div className="shell footer-main"><a className="brand" href="#" aria-label="Pliqra home"><img src="/brand.svg" alt="" width="30" height="30"/>pliqra<span className="brand-mark">/</span></a><p>Plug in models. Publish your rules. Keep the contract.</p><div><a href={REPO} target="_blank" rel="noreferrer">GitHub</a><a href={docs("CAPABILITIES")} target="_blank" rel="noreferrer">Documentation</a><a href={`${REPO}/releases/tag/v2.1.0`} target="_blank" rel="noreferrer">Releases</a><a href={`${REPO}/issues`} target="_blank" rel="noreferrer">Issues</a></div></div><div className="shell footer-bottom"><span>© 2026 Pliqra contributors · Apache-2.0</span><span>Gateway 2.1.0 · SDK 0.1.1 · Protocol 0.1 draft</span></div></footer>
  </>
}
