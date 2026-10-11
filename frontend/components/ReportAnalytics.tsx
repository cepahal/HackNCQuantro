"use client";
import { useState, type CSSProperties } from "react";
import { motion, useReducedMotion } from "framer-motion";
import type { ReportInterpretation, TranscriptSentence } from "../lib/api-types";
import type { AnalyticsData } from "../lib/report-analytics";
import type { SourceLead } from "../lib/source-leads";
import { time } from "../lib/format";
import styles from "./ReportAnalytics.module.css";

export interface ReportAnalyticsProps {
  data: AnalyticsData;
  interpretation?: ReportInterpretation | null;
  interpretationState: "loading" | "ready" | "unavailable";
  spliceVideoReady?: boolean;
  sourceEvidence?: Array<TranscriptSentence & {media_id:string}>;
}

export function evidenceLayerPosition(index:number,active:number|null){
  const base=index*24;
  if(active===null)return base;
  return index<active?base-60:index>active?base+120:base;

}

export default function ReportAnalytics({ data, interpretation, interpretationState, spliceVideoReady = false, sourceEvidence = [] }: ReportAnalyticsProps) {
  const reducedMotion = useReducedMotion();
  const [activeLayer,setActiveLayer]=useState<number|null>(null);
  const [pinnedLayer,setPinnedLayer]=useState(false);
  if (!data.isVideoDetected) return null;
  const { metrics, wafer } = data;
  const reviewQuotes=(interpretation?.source_review?.references??[]).map(ref=>sourceEvidence.find(sentence=>sentence.media_id===ref.media_id&&sentence.sentence_id===ref.sentence_id))
    .filter((sentence):sentence is TranscriptSentence & {media_id:string}=>Boolean(sentence?.text.trim()))
    .filter((sentence,index,all)=>all.findIndex(item=>item.media_id===sentence.media_id&&item.sentence_id===sentence.sentence_id)===index).slice(0,4);
  const contextText = interpretation?.status === "ready" ? interpretation.context_gap : data.contextText;
  const layers = [
    { available: true, title: "Source-match confidence", value: wafer.confidence, note: "Strength of the recorded speech match." },
    { available: wafer.wordMatches !== null, title: "Word overlap", value: wafer.wordMatches ? `${wafer.wordMatches.matched}/${wafer.wordMatches.total} words` : "Unavailable",
      note: "Exact token overlap in aligned transcript passages." },
    { available: wafer.sourceGaps !== null, title: "Splice assessment", value: wafer.sourceGaps === null ? "Not assessed" : `${wafer.sourceGaps} source ${wafer.sourceGaps === 1 ? "gap" : "gaps"}`,
      note: "Gaps between aligned passages in the source recording." },

  ].filter(layer => layer.available);
  return <section className={styles.analytics} aria-label="Source analysis">
    <div className="max-w-3xl"><p className={styles.eyebrow}>The context</p>
      <p className={styles.context}>{contextText}</p>
      <p className={styles.caption} role="status">{interpretationState === "loading" ? "Preparing a concise Gemini reading. Recorded evidence is shown meanwhile."
        : interpretation?.status === "ready" ? "Gemini reading of the saved transcript and source context."
          : "Gemini reading unavailable. Showing the saved analysis."}</p>
      {contextText && contextText.length > 260 && <details className={styles.fullReading}><summary>Read the full context analysis</summary><p>{contextText}</p></details>}
    </div>
    <dl className={`${styles.metrics} grid grid-cols-1 gap-6 sm:grid-cols-3`}>
      {metrics.generationSeconds !== null && <div><dt>Generation time</dt><dd>{metrics.generationSeconds.toFixed(1)}s</dd><p>Recorded pipeline time</p></div>}
      <div><dt>General summary</dt><dd className={styles.summary}>{interpretation?.status === "ready" ? interpretation.summary : metrics.summary}</dd></div>
      {metrics.contextWindows.length > 0 && <div><dt>Source context window</dt><dd className={styles.window}>{metrics.contextWindows.length
        ? metrics.contextWindows.map((window, index) => <span key={index}>{time(window.start, true)} to {time(window.end, true)}</span>) : "Unavailable"}</dd><p>Transcript ranges reviewed</p></div>}
    </dl>
    {interpretation?.status==="ready"&&interpretation.source_review&&<section className={styles.sourceReview} aria-label="Source excerpt review" data-review={interpretation.source_review.status}>
      <h2>{interpretation.source_review.status==="potential_issue"?"Questionable statements discovered":"No clearly inaccurate data discovered"}</h2>
      {interpretation.source_review.status==="potential_issue"&&<>
        <p>{interpretation.source_review.fact_check}</p>
        {reviewQuotes.length>0&&<details className={styles.reviewEvidence}><summary>Source evidence ({reviewQuotes.length})</summary>{reviewQuotes.map(sentence=><figure key={`${sentence.media_id}:${sentence.sentence_id}`}><blockquote>{sentence.text}</blockquote><figcaption>{time(sentence.start_seconds,true)} to {time(sentence.end_seconds,true)}</figcaption></figure>)}</details>}
      </>}
    </section>}
    <div className={styles.wafer} aria-label="Recorded evidence" onMouseLeave={()=>{if(!pinnedLayer)setActiveLayer(null)}} onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget)){setActiveLayer(null);setPinnedLayer(false)}}}>
      <div className={styles.stack} data-testid="evidence-wafer" data-motion={spliceVideoReady && !reducedMotion ? "animated" : "static"}>
        {layers.map((layer,index)=><motion.button type="button" key={layer.title} className={styles.disc}
          aria-label={layer.title} aria-pressed={activeLayer===index} aria-controls={`evidence-layer-${index}`}
          onMouseEnter={()=>{setActiveLayer(index);setPinnedLayer(false)}} onFocus={()=>{setActiveLayer(index);setPinnedLayer(true)}} onClick={()=>{setActiveLayer(index);setPinnedLayer(true)}}
          style={{zIndex:layers.length-index}}
          animate={{top:evidenceLayerPosition(index,activeLayer),
            y:spliceVideoReady&&!reducedMotion?[0,-4,0]:0}}
          transition={reducedMotion?{duration:0}:{top:{duration:.25},y:{duration:5,repeat:Infinity,delay:index*.22,ease:"easeInOut"}}}>
          <div className={styles.grid}/><span className={styles.discNumber}>0{index+1}</span>
          {index===0&&<span className={styles.matchMark}>✓</span>}
        </motion.button>)}
      </div>
      <div className={styles.layers} style={{top:activeLayer===null?0:200+evidenceLayerPosition(activeLayer,activeLayer)+84,"--evidence-label-top":`${activeLayer===null?0:200+evidenceLayerPosition(activeLayer,activeLayer)+84}px`} as CSSProperties}>
        {layers.map((layer,index)=><section key={layer.title} id={`evidence-layer-${index}`} className={styles.layer} hidden={index!==activeLayer}>
          <p className={styles.layerTitle}>{layer.title}</p><h2>{layer.value}</h2><p className={styles.layerNote}>{layer.note}</p>
        </section>)}
      </div>
    </div>
  </section>;
}

export interface SourceOptionsProps {
  options: SourceLead[];
  isVideoDetected: boolean;
  pendingUrl: string | null;
  error: string | null;
  onSelect: (option: SourceLead) => void;
}

export function SourceOptions({ options, isVideoDetected, pendingUrl, error, onSelect }: SourceOptionsProps) {
  return <section className={`${styles.options} ${isVideoDetected ? "" : styles.centered}`} aria-label="Alternative source options">
    <p className={styles.eyebrow}>{isVideoDetected ? "Explore another recording" : "Continue the search"}</p>
    <h2>{isVideoDetected ? "Other possible sources" : "A source has not been confirmed yet."}</h2>
    <p className={styles.optionsNote}>Select a saved candidate to recheck its speech against your submitted clip. </p>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {Array.from({ length: 4 }, (_, index) => {
        const option = options[index];
        return <button key={option?.url ?? index} type="button" className={styles.option}
          hidden={!option} disabled={!option || pendingUrl !== null} onClick={() => option && onSelect(option)}
          aria-label={option ? `Recheck ${option.title}` : `Source option ${index + 1} unavailable`}>
          <span className={styles.optionIndex}>0{index + 1}</span><span>
            <span className={styles.optionTitle}>{option?.title ?? "No additional candidate recorded"}</span>
            <span className={styles.optionReason}>{pendingUrl === option?.url ? "Checking transcript and source context..." : option ? "Recheck this recording" : "Unavailable"}</span>
          </span><span aria-hidden="true">{option ? "↗" : "·"}</span>
        </button>;
      })}
    </div>
    {pendingUrl && <p role="status" className={styles.optionsNote}>The selected source is being checked. The saved report will stay available.</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </section>;
}
