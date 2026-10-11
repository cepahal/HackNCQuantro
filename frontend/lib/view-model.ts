import type { ReportV2 as Report } from "./api-types";
import type { ReportDiagnostics } from "./report-diagnostics";

export interface DataMarker {
  id: string;
  label: string;
  value: number | string | boolean | null;
  unit: string;
  origin: "recorded" | "derived" | "model" | "metadata" | "diagnostic";
  path: string;
  // Only known scales are exposed. Confidence labels never become percentages.
  range: readonly [number, number] | null;
}

function intervalSeconds(ranges: Array<{ start_seconds: number; end_seconds: number }>): number | null {
  if (ranges.some(r => !Number.isFinite(r.start_seconds) || !Number.isFinite(r.end_seconds)
    || r.start_seconds < 0 || r.end_seconds <= r.start_seconds)) return null;
  let total = 0, end = 0;
  for (const range of [...ranges].sort((a,b) => a.start_seconds-b.start_seconds)) {
    total += Math.max(0, range.end_seconds-Math.max(end, range.start_seconds));
    end = Math.max(end, range.end_seconds);
  }
  return total;
}

/** Chart-ready projection of existing public report data; never calls a provider. */
export function reportDataMarkers(report: Report, diagnostics?: ReportDiagnostics | null): DataMarker[] {
  const markers: DataMarker[] = [];
  const add = (path: string, label: string, value: DataMarker["value"] | undefined, unit = "label",
    origin: DataMarker["origin"] = "recorded", range: DataMarker["range"] = null) => {
    const clean = value === undefined || (typeof value === "number" && (!Number.isFinite(value)
      || value < 0 || (range && (value < range[0] || value > range[1])))) ? null : value;
    markers.push({id:path, path, label, value:clean, unit, origin, range});
  };
  add("outcome", "Analysis outcome", report.outcome);
  for (const [side, media] of [["submitted",report.submitted],["source",report.source]] as const) {
    for (const [field, unit] of [["duration_seconds","seconds"],["width","pixels"],["height","pixels"],["fps","frames/second"]] as const)
      add(`${side}.post.media.${field}`, `${side} ${field.replaceAll("_"," ")}`, media?.post.media[field], unit, "metadata");
    add(`${side}.post.media.has_audio`, `${side} audio present`, media?.post.media.has_audio);
    add(`${side}.post.retrieval.provider`, `${side} transcript/retrieval provider`, media?.post.retrieval.provider);
    for (const field of ["followers_count","following_count","posts_count"] as const)
      add(`${side}.post.author.${field}`, `${side} ${field.replaceAll("_"," ")}`, media?.post.author[field], "count", "metadata");
    for (const field of ["views_count","likes_count","comments_count","shares_count"] as const)
      add(`${side}.post.engagement.${field}`, `${side} ${field.replaceAll("_"," ")}`, media?.post.engagement[field], "count", "metadata");
    add(`${side}.transcript.status`, `${side} transcript status`, media?.transcript.status);
    add(`${side}.transcript.sentences.length`, `${side} timed transcript sentences`, media?.transcript.sentences.length, "count", "derived");
    add(`${side}.transcript.processed_seconds`, `${side} processed transcript interval`, media ? intervalSeconds(media.transcript.processed_ranges) : null, "seconds", "derived");
    add(`${side}.transcript.words.length`, `${side} timed words supplied`, media ? media.transcript.sentences.reduce((n,s) => n+(s.words?.length ?? 0),0) : null, "count", "derived");
    add(`${side}.frames.length`, `${side} extracted frames`, media?.frames.length, "count", "derived");
    add(`${side}.visual_text.length`, `${side} visual text entries`, media?.visual_text.length, "count", "derived");
  }
  const aligned = report.outcome === "matched" && report.alignment?.status === "verified"
    && report.source !== null && report.alignment.matches.length > 0
    && report.alignment.clip_media_id === report.submitted.post.media.media_id
    && report.alignment.source_media_id === report.source.post.media.media_id;
  const alignment = aligned ? report.alignment : null;
  add("alignment.match_quality.confidence.level", "Source-match confidence", alignment?.match_quality.confidence.level, "level", "model");
  add("alignment.match_quality.matched_clip_seconds", "Matched submitted speech", alignment?.match_quality.matched_clip_seconds, "seconds");
  add("alignment.match_quality.clip_coverage_ratio", "Submitted clip aligned", alignment?.match_quality.clip_coverage_ratio, "ratio", "recorded", [0,1]);
  alignment?.match_quality.metrics.forEach((metric,index) => {
    if (typeof metric.name !== "string") return;
    const unit = typeof metric.unit === "string" ? metric.unit : "unspecified";
    add(`alignment.match_quality.metrics[${index}].value`, metric.name.replaceAll("_"," "),
      typeof metric.value === "number" ? metric.value : null, unit, "recorded", unit === "score_0_100" ? [0,100] : null);
  });
  const editsAssessed = alignment?.edit_analysis.status === "assessed";
  for (const field of ["matched_passages","source_gaps","unmatched_clip_segments","confirmed_added_segments","confirmed_deleted_segments"] as const)
    add(`alignment.edit_analysis.counts.${field}`, field.replaceAll("_"," "), field === "matched_passages" || editsAssessed ? alignment?.edit_analysis.counts[field] : null, "count");
  add("alignment.edit_analysis.source_gap_seconds", "Approximate source gap duration", editsAssessed ? intervalSeconds(alignment.edit_analysis.source_gaps) : null, "seconds", "derived");
  add("alignment.edit_analysis.unmatched_clip_seconds", "Unmatched submitted duration", editsAssessed ? intervalSeconds(alignment.edit_analysis.unmatched_clip_ranges) : null, "seconds", "derived");
  const comparison = aligned ? report.comparison : null;
  add("comparison.context_assessment", "Context assessment", comparison?.context_assessment, "label", "model");
  add("comparison.caption_assessment", "Caption assessment", comparison?.caption_assessment, "label", "model");
  add("comparison.assessment_confidence.level", "Context-assessment confidence", comparison?.assessment_confidence.level, "level", "model");
  add("comparison.findings.length", "Recorded findings", comparison?.findings.length, "count", "derived");
  add("comparison.evidence.length", "Recorded evidence quotes", comparison?.findings.reduce((n,f) => n+f.evidence.length,0), "count", "derived");
  const context = aligned && report.context?.source_media_id === report.source?.post.media.media_id ? report.context : null;
  add("context.windows.length", "Context windows", context?.windows.length, "count", "derived");
  add("context.requested_seconds", "Requested context interval", context ? intervalSeconds(context.windows.map(w => ({start_seconds:w.requested_start_seconds,end_seconds:w.requested_end_seconds}))) : null, "seconds", "derived");
  const exportAllowed = aligned && ["high","moderate"].includes(alignment?.match_quality.confidence.level ?? "");
  const video = exportAllowed ? report.context_video : null;
  for (const [field,unit] of [["duration_seconds","seconds"],["width","pixels"],["height","pixels"],["fps","frames/second"]] as const)
    add(`context_video.${field}`, `Generated video ${field.replaceAll("_"," ")}`, video?.[field], unit);
  for (const field of ["start_seconds","end_seconds","strategy","before_method","after_method"] as const) {
    const value = exportAllowed ? report.context_selection?.[field] : null;
    add(`context_selection.${field}`, `Export ${field.replaceAll("_"," ")}`, field.endsWith("seconds")
      ? typeof value === "number" ? value : null : typeof value === "string" ? value : null, field.endsWith("seconds") ? "seconds" : "label");
  }
  add("limitations.length", "Report limitations", report.limitations.length, "count", "derived");
  if (diagnostics?.job_id === report.job_id && diagnostics.capture_status === "recorded") {
    add("diagnostics.events.length", "Recorded resolver events", diagnostics.events.length, "count", "diagnostic");
    diagnostics.events.forEach((event,index) => {
      const prefix = `diagnostics.events[${index}]`;
      add(`${prefix}.elapsed_seconds`, `${event.stage}: ${event.status} (${event.timing_kind ?? "timing scope unknown"})`, event.elapsed_seconds, "seconds", "diagnostic");
      for (const field of ["match_score","returned_count","kept_count","candidate_count","threshold"] as const) {
        if (!(field in event.data)) continue;
        add(`${prefix}.data.${field}`, `${event.stage}: ${field.replaceAll("_"," ")}`, typeof event.data[field] === "number" ? event.data[field] : null,
          field === "match_score" || field === "threshold" ? "score_0_100" : "count", "diagnostic", field === "match_score" || field === "threshold" ? [0,100] : null);
      }
      const candidates = event.data.candidates;
      if (Array.isArray(candidates)) candidates.forEach((candidate,i) => {
        if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return;
        const value = candidate.duration;
        if (typeof value === "number" || value === null)
          add(`${prefix}.data.candidates[${i}].duration`, `${event.stage}: candidate ${i+1} duration hint`, value, "seconds", "metadata");
      });
    });
  }
  return markers;
}

export interface TimelineMarker {
  id: string; track: "submitted" | "source"; kind: "match" | "gap" | "unmatched" | "context";
  startSeconds: number; endSeconds: number; path: string;
}

export function reportTimelineMarkers(report: Report): TimelineMarker[] {
  const alignment = report.alignment;
  if (report.outcome !== "matched" || alignment?.status !== "verified" || !report.source
    || alignment.clip_media_id !== report.submitted.post.media.media_id || alignment.source_media_id !== report.source.post.media.media_id) return [];
  const rows: TimelineMarker[] = [];
  alignment.matches.forEach((match,i) => {
    rows.push({id:`clip-${match.match_id}`,track:"submitted",kind:"match",startSeconds:match.clip_start_seconds,endSeconds:match.clip_end_seconds,path:`alignment.matches[${i}]`},
      {id:`source-${match.match_id}`,track:"source",kind:"match",startSeconds:match.source_start_seconds,endSeconds:match.source_end_seconds,path:`alignment.matches[${i}]`});
  });
  if (alignment.edit_analysis.status === "assessed") {
    alignment.edit_analysis.source_gaps.forEach((r,i) => rows.push({id:`gap-${i}`,track:"source",kind:"gap",startSeconds:r.start_seconds,endSeconds:r.end_seconds,path:`alignment.edit_analysis.source_gaps[${i}]`}));
    alignment.edit_analysis.unmatched_clip_ranges.forEach((r,i) => rows.push({id:`unmatched-${i}`,track:"submitted",kind:"unmatched",startSeconds:r.start_seconds,endSeconds:r.end_seconds,path:`alignment.edit_analysis.unmatched_clip_ranges[${i}]`}));
  }
  if (report.context?.source_media_id === report.source.post.media.media_id)
    report.context.windows.forEach((r,i) => rows.push({id:r.context_id,track:"source",kind:"context",startSeconds:r.requested_start_seconds,endSeconds:r.requested_end_seconds,path:`context.windows[${i}]`}));
  return rows.filter(r => Number.isFinite(r.startSeconds) && Number.isFinite(r.endSeconds) && r.startSeconds >= 0 && r.endSeconds > r.startSeconds);
}

export function derive(report: Report) {
  const alignment = report.alignment;
  if (!alignment) return { gapSeconds: null, passageSpan: null, leftOutPct: null, sourceMatchedSeconds: null, outOfOrder: false };
  const matches = [...alignment.matches].sort((a,b)=>a.source_start_seconds-b.source_start_seconds);
  const chronological = matches.every((match,index)=>index===0 || (matches[index-1].source_end_seconds<=match.source_start_seconds && matches[index-1].clip_end_seconds<=match.clip_start_seconds));
  const gaps = alignment.edit_analysis.source_gaps;
  const gapSeconds = alignment.edit_analysis.status === "assessed" && chronological ? gaps.reduce((sum,gap)=>sum + Math.max(0,gap.end_seconds-gap.start_seconds),0) : null;
  const passageSpan = matches.length ? matches.at(-1)!.source_end_seconds-matches[0].source_start_seconds : null;
  return { gapSeconds, passageSpan, leftOutPct:gapSeconds != null && passageSpan && passageSpan>0 ? gapSeconds/passageSpan : null, sourceMatchedSeconds:matches.reduce((sum,m)=>sum+m.source_end_seconds-m.source_start_seconds,0), outOfOrder:!chronological };
}
export function sentenceLookup(transcript: Report["submitted"]["transcript"], ref: string) {
  return transcript.sentences.find(sentence => sentence.sentence_id === ref || sentence.sentence_id === ref.replace(/^(clip|source)_/, "") || `source_${sentence.sentence_id}` === ref || `clip_${sentence.sentence_id}` === ref);
}
