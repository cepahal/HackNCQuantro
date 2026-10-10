import type { ReportMedia, ReportV2 } from "../lib/api";
import { mediaUrl, publicUrl } from "../lib/api";
import ConfidenceSection from "./ConfidenceSection";

import type { DisplayModel } from "../lib/displayModel";

const count = (value: number | null | undefined) => value == null ? "Unavailable" : value.toLocaleString();
const seconds = (start: number, end: number) => `${start.toFixed(1)}s–${end.toFixed(1)}s`;
const editLabels: Partial<Record<keyof NonNullable<ReportV2["alignment"]>["edit_analysis"]["counts"], string>> = {
  matched_passages: "Matched passages", source_gaps: "Source gaps", unmatched_clip_segments: "Unmatched clip segments",
  confirmed_added_segments: "Confirmed added segments", confirmed_deleted_segments: "Confirmed deleted segments",
};

function MediaPanel({ title, media }: { title: string; media: ReportMedia }) {
  const source = publicUrl(media.post.post_url);
  const playback = mediaUrl(media.post.media);
  return <section className="panel"><h2>{title}</h2><h3>{media.post.title || media.post.author.display_name || media.post.author.handle || "Public post"}</h3>
    {playback && (media.post.media.mime_type === "image/jpeg"
      ? <img className="media" src={playback} alt={media.post.title || "Submitted media"}/>
      : <video className="media" controls preload="metadata" src={playback}/>)}
    {source && <a href={source} target="_blank" rel="noopener noreferrer">Open post ↗</a>}
    <dl className="metadata"><dt>Account</dt><dd>{media.post.author.handle || media.post.author.display_name || "Unavailable"}</dd>
      <dt>Post published</dt><dd>{media.post.published_at || "Unavailable"}</dd>
      <dt>Followers</dt><dd>{count(media.post.author.followers_count)}</dd>
      <dt>Following</dt><dd>{count(media.post.author.following_count)}</dd>
      <dt>Account posts</dt><dd>{count(media.post.author.posts_count)}</dd></dl>
    <p>{media.post.caption || "No caption was available."}</p>
    <details><summary>Transcript ({media.transcript.status.replaceAll("_", " ")})</summary>
      {media.transcript.sentences.length ? media.transcript.sentences.map((sentence) => <p key={sentence.sentence_id}>
        <span className="timestamp">{sentence.start_seconds.toFixed(1)}s–{sentence.end_seconds.toFixed(1)}s</span> {sentence.text}</p>) : <p>No transcript was available.</p>}
    </details>
  </section>;
}

export default function ResultCard({ report, display }: { report: ReportV2; display: DisplayModel }) {
  return <article><div className="verdict"><span className="eyebrow">Analysis result</span>
    <h1>{report.outcome === "matched" ? "A source match was verified" : "No verified source match"}</h1>
    <p>{report.summary || report.comparison?.summary || "There is not enough evidence to assess the post’s context."}</p>
    <small>Confidence describes the evidence for this assessment. It is not a probability that a post is true.</small></div>
    <div className="panels"><MediaPanel title="Submitted post" media={report.submitted}/>
      {report.source && <MediaPanel title="Matched source" media={report.source}/>}</div>
    <section className="panel"><ConfidenceSection {...display}/>{report.comparison && <p>Context: {report.comparison.context_assessment.replaceAll("_", " ")}. Caption: {report.comparison.caption_assessment.replaceAll("_", " ")}.</p>}</section>
    {report.alignment && <section className="panel"><h2>Matching passages and edit analysis</h2>
      {report.alignment.matches.length ? <table><thead><tr><th>Passage</th><th>Submitted clip</th><th>Source</th></tr></thead><tbody>
        {report.alignment.matches.map((match) => <tr key={match.match_id}><td>{match.match_id}</td>
          <td>{seconds(match.clip_start_seconds, match.clip_end_seconds)}</td><td>{seconds(match.source_start_seconds, match.source_end_seconds)}</td></tr>)}</tbody></table> : <p>No verified matching passages.</p>}
      <p>Edit analysis: {report.alignment.edit_analysis.status.replaceAll("_", " ")}. {report.alignment.edit_analysis.scope}</p>
      <dl className="metadata">{Object.entries(editLabels).map(([key, label]) => <div key={key}><dt>{label}</dt>
        <dd>{report.alignment!.edit_analysis.counts[key as keyof typeof report.alignment.edit_analysis.counts] == null ? "Not assessed" : report.alignment!.edit_analysis.counts[key as keyof typeof report.alignment.edit_analysis.counts]}</dd></div>)}</dl>
      {Boolean(report.alignment.edit_analysis.source_gaps.length) && <><h3>Source gaps between matched passages</h3><ul>{report.alignment.edit_analysis.source_gaps.map((gap, index) => <li key={index}>{seconds(gap.start_seconds, gap.end_seconds)}</li>)}</ul></>}
      {Boolean(report.alignment.edit_analysis.unmatched_clip_ranges.length) && <><h3>Unmatched clip ranges</h3><ul>{report.alignment.edit_analysis.unmatched_clip_ranges.map((range, index) => <li key={index}>{seconds(range.start_seconds, range.end_seconds)}</li>)}</ul></>}
    </section>}
    {Boolean(report.context?.windows.length) && <section className="panel"><h2>Surrounding source context</h2>{report.context!.windows.map((window) => <details key={window.context_id}>
      <summary>{seconds(window.requested_start_seconds, window.requested_end_seconds)} · {window.coverage}</summary>
      {window.sentences.length ? window.sentences.map((sentence) => <p key={sentence.sentence_id}><span className="timestamp">{seconds(sentence.start_seconds, sentence.end_seconds)}</span> {sentence.text}</p>) : <p>Context was unavailable for this window.</p>}
    </details>)}</section>}
    {Boolean(report.comparison?.findings.length) && <section className="panel"><h2>Findings and evidence</h2>
      {report.comparison!.findings.map((finding) => <div key={finding.finding_id}><h3>{finding.type.replaceAll("_", " ")}</h3><p>{finding.explanation}</p>
        {finding.evidence.map((evidence, index) => <blockquote key={index}>{evidence.quote}<footer>{evidence.start_seconds.toFixed(1)}s–{evidence.end_seconds.toFixed(1)}s</footer></blockquote>)}</div>)}</section>}
    <section className="panel"><h2>Limitations</h2>{report.limitations.length ? <ul>{report.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul> : <p>No additional limitations were recorded.</p>}</section>
  </article>;
}
