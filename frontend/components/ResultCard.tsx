import type { EditCounts, ReportMedia, ReportV2 } from "../lib/api-types";
import { mediaUrl, publicUrl } from "../lib/api";
import { confidenceDisplay, displayCount, NO_SOURCE_MESSAGE, NOT_ASSESSED, posterCard, timeRange, UNAVAILABLE } from "../lib/display";
import ConfidenceSection from "./ConfidenceSection";

const editLabels: Record<keyof EditCounts, string> = {
  matched_passages: "Matched passages", source_gaps: "Source gaps", unmatched_clip_segments: "Unmatched clip segments",
  confirmed_added_segments: "Confirmed added segments", confirmed_deleted_segments: "Confirmed deleted segments",
};

function MediaPanel({ title, media }: { title: string; media: ReportMedia }) {
  const { post, transcript } = media;
  const source = publicUrl(post.post_url);
  const playback = mediaUrl(post.media);
  return <section className="panel" aria-label={title}><h2>{title}</h2><h3>{post.title || "Untitled post"}</h3>
    {playback && (post.media.mime_type === "image/jpeg"
      ? <img className="media" src={playback} alt={post.title || title}/>
      : <video className="media" controls preload="metadata" src={playback}/>)}
    {source && <a href={source} target="_blank" rel="noopener noreferrer">Open post ↗</a>}
    <dl className="metadata"><div><dt>Platform</dt><dd>{post.platform}</dd></div>
      <div><dt>Published</dt><dd>{post.published_at ?? UNAVAILABLE}</dd></div>
      <div><dt>Duration</dt><dd>{post.media.duration_seconds === null ? UNAVAILABLE : `${post.media.duration_seconds.toFixed(1)}s`}</dd></div></dl>
    <p>{post.caption || "No caption was available."}</p>
    <h3>Transcript ({transcript.status})</h3>
    {transcript.sentences.length ? <ol className="transcript">{transcript.sentences.map((sentence) => <li key={sentence.sentence_id}>
      <span className="timestamp">{timeRange(sentence.start_seconds, sentence.end_seconds)}</span> {sentence.text}</li>)}</ol>
      : <p>No transcript was available.</p>}
  </section>;
}

function PosterPanel({ title, media }: { title: string; media: ReportMedia }) {
  const card = posterCard(media.post.author);
  return <div className="card" aria-label={title}><h3>{title}</h3>
    <dl className="metadata"><div><dt>Handle</dt><dd>{card.handle}</dd></div>
      <div><dt>Followers</dt><dd>{card.followers_count}</dd></div>
      <div><dt>Verified</dt><dd>{card.verified}</dd></div></dl></div>;
}

export default function ResultCard({ report }: { report: ReportV2 }) {
  const alignment = report.alignment;
  const edit = alignment?.edit_analysis ?? null;
  return <article><div className="verdict"><span className="eyebrow">Analysis result</span>
    <h1>{report.outcome === "matched" ? "A source match was verified" : NO_SOURCE_MESSAGE}</h1>
    <p>{report.summary || report.comparison?.summary || "There is not enough evidence to assess the post’s context."}</p></div>

    <div className="panels"><MediaPanel title="Submitted post" media={report.submitted}/>
      {report.outcome === "matched" && report.source
        ? <MediaPanel title="Matched source" media={report.source}/>
        : <section className="panel" aria-label="Matched source"><h2>Matched source</h2><p role="status">{NO_SOURCE_MESSAGE}</p></section>}</div>

    <section className="panel"><h2>Confidence</h2><ConfidenceSection {...confidenceDisplay(report)}/>
      {report.comparison && <p>Context: {report.comparison.context_assessment.replaceAll("_", " ")}. Caption: {report.comparison.caption_assessment.replaceAll("_", " ")}.</p>}</section>

    <section className="panel" aria-label="Poster metadata"><h2>Poster metadata</h2><div className="cards">
      <PosterPanel title="Submitted poster" media={report.submitted}/>
      {report.outcome === "matched" && report.source && <PosterPanel title="Source poster" media={report.source}/>}</div></section>

    <section className="panel"><h2>Findings and evidence</h2>
      {report.comparison?.findings.length ? report.comparison.findings.map((finding) => <div key={finding.finding_id}>
        <h3>{finding.type.replaceAll("_", " ")}</h3><p>{finding.explanation}</p>
        {finding.evidence.map((evidence, index) => <blockquote key={index}>{evidence.quote}<footer>{timeRange(evidence.start_seconds, evidence.end_seconds)}</footer></blockquote>)}</div>)
        : <p>No findings were recorded.</p>}</section>

    <section className="panel"><h2>Edit analysis</h2>
      {edit ? <>
        <p>Status: {edit.status.replaceAll("_", " ")}. {edit.scope}</p>
        <dl className="metadata">{(Object.keys(editLabels) as Array<keyof EditCounts>).map((key) => <div key={key}><dt>{editLabels[key]}</dt>
          <dd>{displayCount(edit.counts[key], NOT_ASSESSED)}</dd></div>)}</dl>
        {alignment!.matches.length > 0 && <table><thead><tr><th>Passage</th><th>Submitted clip</th><th>Source</th></tr></thead><tbody>
          {alignment!.matches.map((match) => <tr key={match.match_id}><td>{match.match_id}</td>
            <td>{timeRange(match.clip_start_seconds, match.clip_end_seconds)}</td><td>{timeRange(match.source_start_seconds, match.source_end_seconds)}</td></tr>)}</tbody></table>}
        <h3>Source gaps</h3>{edit.source_gaps.length
          ? <ul>{edit.source_gaps.map((gap, index) => <li key={index}>{timeRange(gap.start_seconds, gap.end_seconds)}</li>)}</ul> : <p>None recorded.</p>}
        <h3>Unmatched clip ranges</h3>{edit.unmatched_clip_ranges.length
          ? <ul>{edit.unmatched_clip_ranges.map((range, index) => <li key={index}>{timeRange(range.start_seconds, range.end_seconds)}</li>)}</ul> : <p>None recorded.</p>}
      </> : <p>Status: {NOT_ASSESSED}. Edits can only be analyzed against a verified source.</p>}</section>

    {Boolean(report.context?.windows.length) && <section className="panel"><h2>Surrounding source context</h2>{report.context!.windows.map((window) => <details key={window.context_id}>
      <summary>{timeRange(window.requested_start_seconds, window.requested_end_seconds)} · {window.coverage}</summary>
      {window.sentences.length ? window.sentences.map((sentence) => <p key={sentence.sentence_id}><span className="timestamp">{timeRange(sentence.start_seconds, sentence.end_seconds)}</span> {sentence.text}</p>) : <p>Context was unavailable for this window.</p>}
    </details>)}</section>}

    <section className="panel"><h2>Limitations</h2>{report.limitations.length
      ? <ul>{report.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul> : <p>No additional limitations were recorded.</p>}</section>
  </article>;
}
