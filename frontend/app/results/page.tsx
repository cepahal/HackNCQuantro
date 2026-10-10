"use client";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import Navbar from "../../components/Navbar";
import LoadingState from "../../components/LoadingState";
import ResultCard from "../../components/ResultCard";
import { analysisInFlight, getReport, startAnalysis } from "../../lib/api";
import type { ReportV2 as Report } from "../../lib/api-types";
import { errorDisplay, reportSummary, type ErrorDisplay } from "../../lib/display";

function ResultsContent(){
 const params=useSearchParams();const router=useRouter();const jobId=params.get("job");const reportId=params.get("id")||params.get("report_id");const[report,setReport]=useState<Report|null>(null);const[error,setError]=useState<ErrorDisplay|null>(null);const[attempt,setAttempt]=useState(0);const[copy,setCopy]=useState(false);
 const load=useCallback(async()=>{setReport(null);setError(null);try{if(reportId){setReport(await getReport(reportId));return}if(jobId){const run=analysisInFlight(jobId);if(!run)throw errorDisplay({error:{code:"ANALYSIS_NOT_RUNNING",stage:"run_analysis",message:"This analysis is no longer running in this tab. Start a new check from the home page.",retryable:false}});const result=await run.result;router.replace(`/results?id=${encodeURIComponent(result.report_id)}`);return}throw errorDisplay({error:{code:"MISSING_REPORT",stage:"load_report",message:"Choose a report to view.",retryable:false}})}catch(reason){setError(errorDisplay(reason))}},[reportId,jobId,router]);
 useEffect(()=>{load()},[load,attempt]);
 const retry=()=>{const run=!reportId&&jobId?analysisInFlight(jobId):undefined;if(run){router.replace(`/results?job=${encodeURIComponent(startAnalysis(run.url))}`);return}setAttempt(value=>value+1)};
 const copyLink=async()=>{await navigator.clipboard?.writeText(window.location.href);setCopy(true);window.setTimeout(()=>setCopy(false),1500)};
 const summary=report?reportSummary(report):null;
 return <><Navbar/><main className="page-wrap"><header className="report-head"><div className="report-meta"><Link href="/">← Back</Link><span>·</span><span className="tabular">{jobId||report?.job_id||reportId}</span><div className="report-actions"><button className="btn btn-outline" onClick={copyLink}>{copy?"Copied":"Copy link"}</button></div></div><h1>{report?.outcome==="no_verified_match"?"No verified source found.":"Analysis."}</h1>{summary&&<p className="summary">{summary}</p>}</header>{error?<div className="error-card" role="alert"><h2>We couldn’t load this report.</h2><p>{error.error.message}</p><p className="subtle">{error.error.code} · {error.error.stage}</p>{error.error.retryable&&<button className="btn btn-black" onClick={retry}>Retry</button>}</div>:report?<ResultCard report={report}/>:<LoadingState job={null}/>}</main><footer className="footer" style={{marginTop:50}}><div className="page-wrap footer-inner"><Link className="brand" href="/" aria-label="Unzoom home"><Image className="brand-image" src="/logo.png" alt="Unzoom" width={180} height={31} /></Link><p>You deserve the truth.</p><p>Built at HackNC</p><p>© 2026 Unzoom</p></div></footer></>;
}
export default function ResultsPage(){return <Suspense fallback={<main className="page-wrap" style={{paddingTop:80}}><LoadingState job={null}/></main>}><ResultsContent/></Suspense>}
