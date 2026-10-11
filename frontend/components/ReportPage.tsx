"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import Navbar from "./Navbar";
import AnalysisLoadingScreen from "./AnalysisLoadingScreen";
import ResultCard from "./ResultCard";
import { analysisInFlight, getReport, getDisplayModel, startAnalysis, waitForReport } from "../lib/api";
import type { ReportV2 as Report, DisplayModel } from "../lib/api-types";
import { errorDisplay, type ErrorDisplay } from "../lib/display";

export default function ReportPage({jobId,reportId}:{jobId:string|null;reportId:string|null}){
 const router=useRouter();const[report,setReport]=useState<Report|null>(null);const[display,setDisplay]=useState<DisplayModel|null>(null);const[error,setError]=useState<ErrorDisplay|null>(null);const[attempt,setAttempt]=useState(0);const[copyStatus,setCopyStatus]=useState("");const[loadingDismissed,setLoadingDismissed]=useState(false);const[routeAfterReveal,setRouteAfterReveal]=useState<string|null>(null);const skipNextLoading=useRef(false);const requestKey=useRef(`${jobId}:${reportId}`);
 useEffect(()=>{
  const nextKey=`${jobId}:${reportId}`;
  if(requestKey.current!==nextKey){requestKey.current=nextKey;if(skipNextLoading.current){skipNextLoading.current=false}else{setLoadingDismissed(false);setReport(null);setDisplay(null);setError(null)}}
  const controller=new AbortController();
  async function load(){try{
   if(reportId){const [loaded,model]=await Promise.all([getReport(reportId),getDisplayModel(reportId)]);if(!controller.signal.aborted){setReport(loaded);setDisplay(model)}return}
   if(jobId){
    const run=analysisInFlight(jobId);
    const id=run?(await run.result).report_id:await waitForReport(jobId,controller.signal);
     if(controller.signal.aborted)return;
     const [loaded,model]=await Promise.all([getReport(id),getDisplayModel(id)]);
     if(!controller.signal.aborted){setReport(loaded);setDisplay(model);setRouteAfterReveal(id)}
    return;
   }
   throw errorDisplay({error:{code:"MISSING_REPORT",stage:"load_report",message:"Choose a report to view.",retryable:false}});
  }catch(reason){if(!controller.signal.aborted)setError(errorDisplay(reason))}}
  void load();return()=>controller.abort();
 },[reportId,jobId,router,attempt]);
 const retry=()=>{setReport(null);setDisplay(null);setError(null);setLoadingDismissed(false);setRouteAfterReveal(null);const run=!reportId&&jobId?analysisInFlight(jobId):undefined;if(run){router.replace(`/results?job=${encodeURIComponent(startAnalysis(run.url))}`);return}setAttempt(value=>value+1)};
 const copyLink=async()=>{try{if(!navigator.clipboard?.writeText)throw new Error("Clipboard unavailable");await navigator.clipboard.writeText(window.location.href);setCopyStatus("Link copied.")}catch{setCopyStatus("Couldn’t copy. Copy the address from your browser.")}window.setTimeout(()=>setCopyStatus(""),2200)};
 const ready=Boolean(error||(report&&display));
 const finishLoading=()=>{setLoadingDismissed(true);if(routeAfterReveal){skipNextLoading.current=true;router.replace(`/results?id=${encodeURIComponent(routeAfterReveal)}`)}};
 return <><Navbar/><main className="page-wrap"><header className="report-head"><div className="report-meta"><Link className="report-back" href="/">← Back</Link><span>·</span><span className="tabular">{jobId||report?.job_id||reportId}</span><div className="report-actions"><button className="btn btn-outline" onClick={copyLink}>{copyStatus.startsWith("Link copied")?"Copied":copyStatus.startsWith("Couldn’t")?"Copy failed":"Copy link"}</button><span role="status" className="sr-only">{copyStatus}</span></div></div><h1>Analysis.</h1></header>{error?<div className="error-card" role="alert"><h2>We couldn’t load this report.</h2><p>{error.error.message}</p><p className="subtle">{error.error.code} · {error.error.stage}</p>{error.error.retryable&&<button className="btn btn-black" onClick={retry}>Retry</button>}</div>:report&&display?<ResultCard report={report} display={display}/>:null}</main><footer className="footer" style={{marginTop:50}}><div className="page-wrap footer-inner"><Link className="brand" href="/" aria-label="Zoomout home"><Image className="brand-image" src="/logo-light.png" alt="Zoomout" width={180} height={35} unoptimized /></Link><p>The full record, in context.</p><p>Built at HackNC</p><p>© 2026 Zoomout</p></div></footer>{!loadingDismissed&&<AnalysisLoadingScreen complete={ready} onDone={finishLoading}/>}</>;
}
export function ReportLoading(){return <AnalysisLoadingScreen/>}
