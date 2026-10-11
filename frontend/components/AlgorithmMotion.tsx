'use client';
import {useEffect,useState} from 'react';
import {motionSteps,motionStep,type AnalysisProgress} from '../lib/analysis-progress';
import AlgorithmAnimation from './AlgorithmAnimation';
import styles from './AlgorithmMotion.module.css';
const titles=['Read the submitted clip','Build search clues','Compare possible sources','Locate the matching passage','Recover surrounding sentences','Assemble the source excerpt'];
const timecode=(n:number|null)=>n===null?'Unavailable':`${Math.floor(n/60)}:${(n%60).toFixed(1).padStart(4,'0')}`;
export default function AlgorithmMotion({progress:p,replay=false,stale=false,videoPlayable=false}:{progress:AnalysisProgress;replay?:boolean;stale?:boolean;videoPlayable?:boolean}){
 const [selected,setSelected]=useState<number|null>(null),[playing,setPlaying]=useState(false),[motion,setMotion]=useState(true);
 const terminal=p.stage==='failed'||p.stage==='no_match',maximum=motionStep(p),step=replay?Math.min(selected??maximum,maximum):maximum;
 useEffect(()=>{if(!playing)return;const timer=setTimeout(()=>{if(step>=maximum)setPlaying(false);else setSelected(step+1)},2300);return()=>clearTimeout(timer)},[playing,step,maximum]);
 return <section className={`${styles.frame} ${!motion?styles.noMotion:''}`} aria-label="Analysis process">
 <header className={styles.header}><span className={styles.brand}>ZoomOut / {replay?'SAVED ANALYSIS':'LIVE ANALYSIS'}</span><span>{terminal?'STOPPED':`${String(step+1).padStart(2,'0')} / 06`}</span></header>
 <div className={styles.heading}><div className={styles.label}>{p.title||'Your submitted video'}</div><h2>{terminal?(p.stage==='no_match'?'No verified source found':'Analysis interrupted'):titles[step]}</h2></div>
 {!terminal&&<AlgorithmAnimation progress={{...p,stage:replay?(['transcript','search','candidates','align','context','export'] as const)[step]:p.stage}} motionEnabled={motion}/>}
 <div className={styles.mount}><div className={styles.scene} key={`${step}:${terminal}`}>
 {terminal?<p>{p.message||'See the report for details.'}</p>:<>
 {step===0&&<><div className={styles.film} aria-hidden="true"/><p className={styles.label}>Input transcript excerpt</p><blockquote className={styles.quote}>{p.excerpt||'Waiting for the transcript…'}</blockquote></>}
 {step===1&&<><p className={styles.label}>Distinctive phrase</p><div className={styles.clue}>{p.anchor||'No search phrase recorded yet.'}</div><p className={styles.label}>Speaker / event clue · inferred</p><div className={styles.clue}>{p.speaker_clue||'No speaker or event clue recorded.'}</div></>}
 {step===2&&<><p className={styles.label}>Possible recordings</p>{p.candidates.length?p.candidates.map((c,i)=><div className={styles.candidate} key={i}><span>{c.title||'Untitled candidate'}</span><span>{c.score===null?'Unavailable':c.score.toFixed(1)}</span></div>):<p>Searching for candidate recordings…</p>}<p className={styles.label}>Scores are matching heuristics, not probabilities of truth.</p></>}
 {step===3&&<><div className={styles.clue}>{p.anchor||p.excerpt||'Submitted passage'}</div><div className={styles.connector} aria-hidden="true"/><div className={styles.film} aria-hidden="true"/><p>{p.source_title||'Evaluating passage alignment…'}</p><p className={styles.label}>Source passage {timecode(p.source_start)} → {timecode(p.source_end)}</p></>}
 {step===4&&<div className={styles.context}>{(['before','after'] as const).map(side=><div key={side}><p className={styles.label}>Context {side}</p>{p[side].length?p[side].map((text,i)=><p className={styles.clue} key={i}>{text}</p>):<p>Sentence context unavailable; the report records any fallback.</p>}</div>)}</div>}
 {step===5&&<><p>{p.source_title||'Selected source recording'}</p>{videoPlayable&&<div className={styles.track} aria-label="Schematic, not proportional to duration"><span>BEFORE</span><span>MATCHED PASSAGE</span><span>AFTER</span></div>}<p>{videoPlayable?'Continuous source excerpt ready.':p.video_ready?'Source excerpt stored. Playback has not been confirmed.':p.stage==='complete'?'Report ready. Context-video export unavailable.':'Context-video export is being assessed.'}</p>{videoPlayable&&<p className={styles.label}>All sections come from the source recording. Diagram not to scale.</p>}</>}
 </>}
 </div></div><p className={styles.status} role="status">{stale?'Progress connection interrupted. Keeping the last confirmed stage.':terminal?p.message:replay?'Replay of recorded analysis data.':`Confirmed stage: ${motionSteps[step]}.`}</p>
 <nav className={styles.steps} aria-label="Analysis stages">{motionSteps.map((name,i)=><button key={name} disabled={!replay||i>maximum||terminal} aria-current={i===step?'step':undefined} onClick={()=>{setSelected(i);setPlaying(false)}}>{String(i+1).padStart(2,'0')} {name}</button>)}</nav>
 <footer className={styles.footer}><span>{replay?'Recorded results':'Waiting advances only when the backend reports progress.'}</span>{replay&&!terminal&&<button onClick={()=>{if(playing)setPlaying(false);else{setSelected(0);setPlaying(true)}}}>{playing?'Pause replay':'Play replay'}</button>}<button onClick={()=>setMotion(!motion)} aria-pressed={!motion}>Reduce motion</button></footer></section>
}
