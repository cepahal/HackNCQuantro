'use client';
import {useEffect,useState} from 'react';
import type {ReportV2} from '../lib/api-types';
import {getAnalysisProgress} from '../lib/api';
import {progressFromReport,personalizedProgressAvailable,type AnalysisProgress} from '../lib/analysis-progress';
import AnalysisLoadingScreen from './AnalysisLoadingScreen';
import AlgorithmMotion from './AlgorithmMotion';
import styles from './AlgorithmMotion.module.css';
function useProgress(jobId:string|null,enabled:boolean){
 const [saved,setSaved]=useState<{jobId:string;data:AnalysisProgress|null;stale:boolean}|null>(null);
 useEffect(()=>{if(!jobId||!enabled)return;let disposed=false,timer:ReturnType<typeof setTimeout>;const controller=new AbortController();
 const poll=async()=>{let finished=false;try{const data=await getAnalysisProgress(jobId,AbortSignal.any([controller.signal,AbortSignal.timeout(8000)]));if(disposed)return;setSaved({jobId,data,stale:false});finished=['complete','no_match','failed'].includes(data.stage)}catch{if(disposed)return;setSaved(old=>({jobId,data:old?.jobId===jobId?old.data:null,stale:true}))}if(!disposed&&!finished)timer=setTimeout(poll,document.hidden?10000:2000)};
 void poll();return()=>{disposed=true;controller.abort();clearTimeout(timer)}},[jobId,enabled]);
 return saved?.jobId===jobId?saved:null;
}
export function PersonalizedAnalysisLoading({jobId,complete,onDone}:{jobId:string|null;complete:boolean;onDone:()=>void}){
 const live=useProgress(jobId,!complete),data=live?.data;
 useEffect(()=>{if(!complete)return;const timer=setTimeout(onDone,window.matchMedia('(prefers-reduced-motion: reduce)').matches?0:350);return()=>clearTimeout(timer)},[complete,onDone]);
 if(!data||!personalizedProgressAvailable(data))return <AnalysisLoadingScreen complete={complete}/>;
 return <div className={styles.overlay}><AlgorithmMotion progress={data} stale={live?.stale}/></div>;
}
export function AnalysisReplay({report,videoPlayable=false}:{report:ReportV2;videoPlayable?:boolean}){
 const [open,setOpen]=useState(false),live=useProgress(report.job_id,open);
 return <details className={styles.replay} onToggle={event=>setOpen(event.currentTarget.open)}><summary>How this video was analyzed</summary>{open&&<AlgorithmMotion progress={live?.data??progressFromReport(report)} replay videoPlayable={videoPlayable} stale={live?.stale}/>}</details>;
}
