import type { Report } from "./types";

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
