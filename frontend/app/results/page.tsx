"use client";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import Navbar from "../../components/Navbar";
import LoadingState from "../../components/LoadingState";
import ResultCard from "../../components/ResultCard";
import { getJob, getReport, normalizeError } from "../../lib/api";
import type { ApiError, Job, Report } from "../../lib/types";

function ResultsContent(){
 const params=useSearchParams();const fixture=params.get("fixture");const jobId=params.get("job");const reportId=params.get("id");const[report,setReport]=useState<Report|null>(null);const[job,setJob]=useState<Job|null>(null);const[error,setError]=useState<ApiError|null>(null);const[attempt,setAttempt]=useState(0);const[copy,setCopy]=useState(false);
 const load=useCallback(async()=>{setReport(null);setError(null);try{if(fixture){setReport(await getReport(fixture));return}if(reportId){setReport(await getReport(reportId));return}if(jobId){let current:Job|null=null;for(let tries=0;tries<80;tries++){current=await getJob(jobId);setJob(current);if(current.status==="failed")throw normalizeError({error:current.error||{code:"ANALYSIS_FAILED",stage:current.stage||"unknown",message:"Analysis could not be completed.",retryable:true}});if(current.status==="completed"){setReport(await getReport(current.report_id||current.job_id));return}await new Promise(resolve=>setTimeout(resolve,2000))}throw normalizeError({error:{code:"TIMEOUT",stage:current?.stage||"unknown",message:"Analysis is taking longer than expected. Try again.",retryable:true}})}throw normalizeError({error:{code:"MISSING_REPORT",stage:"load_report",message:"Choose a report to view.",retryable:false}})}catch(reason){setError(normalizeError(reason))}},[fixture,reportId,jobId]);
 useEffect(()=>{load()},[load,attempt]);
 const copyLink=async()=>{await navigator.clipboard?.writeText(window.location.href);setCopy(true);window.setTimeout(()=>setCopy(false),1500)};
 return <><Navbar/><main className="page-wrap"><header className="report-head"><div className="report-meta"><Link href="/">← Back</Link><span>·</span><span className="tabular">{jobId||report?.job_id||"Sample report"}</span><div className="report-actions"><button className="btn btn-outline" onClick={copyLink}>{copy?"Copied":"Copy link"}</button></div></div><h1>{report?.outcome==="no_verified_match"?"No verified source found.":"Analysis."}</h1>{report?.summary&&<p className="summary">{report.summary}</p>}</header>{error?<div className="error-card" role="alert"><h2>We couldn’t load this report.</h2><p>{error.error.message}</p><p className="subtle">{error.error.code} · {error.error.stage}</p>{error.error.retryable&&<button className="btn btn-black" onClick={()=>setAttempt(value=>value+1)}>Retry</button>}</div>:report?<ResultCard report={report}/>:<LoadingState job={job}/>}</main><footer className="footer" style={{marginTop:50}}><div className="page-wrap footer-inner"><Link className="brand" href="/" aria-label="Unzoom home"><Image className="brand-image" src="/logo.png" alt="Unzoom" width={180} height={31} /></Link><p>You deserve the truth.</p><p>Built at HackNC</p><p>© 2026 Unzoom</p></div></footer></>;
}
export default function ResultsPage(){return <Suspense fallback={<main className="page-wrap" style={{paddingTop:80}}><LoadingState job={null}/></main>}><ResultsContent/></Suspense>}
