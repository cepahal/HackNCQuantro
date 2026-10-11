"use client";

import Image from "next/image";
import { useState } from "react";
import type { ReportV2 } from "../lib/api-types";
import { mediaUrl, publicUrl } from "../lib/api";
import { youtubePreviewUrl } from "../lib/youtube-preview";
import styles from "./ReportPlayer.module.css";

type ReportMedia = ReportV2["submitted"];

export default function ReportPlayer({ media, label, generated = false }: { media: ReportMedia; label: string; generated?: boolean }) {
  const [failed, setFailed] = useState(false);
  const { width, height } = media.post.media;
  const [ratio, setRatio] = useState(width && height ? width / height : 16 / 9);
  const playback = mediaUrl(media.post.media);
  const original = generated ? null : publicUrl(media.post.post_url);
  const youtube = generated ? null : youtubePreviewUrl(media.post.post_url);
  const image = media.post.media.mime_type.startsWith("image/");
  const direct = playback && !failed;
  return <div className="hatch"><section className="panel" aria-label={label}>
    <h3 className={styles.label}>{label}</h3>
    {direct ? <div className={styles.frame} style={{ aspectRatio: ratio }}>
      {image ? <Image src={playback} alt={media.post.title || label} fill unoptimized
        sizes="(max-width: 600px) 100vw, 50vw" className={styles.image} onError={() => setFailed(true)} />
        : <video aria-label={label} controls playsInline preload="metadata" src={playback}
          onError={() => setFailed(true)} onLoadedMetadata={event => {
            const video = event.currentTarget;
            if (video.videoWidth && video.videoHeight) setRatio(video.videoWidth / video.videoHeight);
          }} />}
    </div> : youtube ? <div className={styles.embed}>
      <iframe src={youtube} title={`${label}: YouTube player`} loading="lazy"
        referrerPolicy="strict-origin-when-cross-origin"
        allow="accelerometer; encrypted-media; gyroscope; picture-in-picture; web-share" allowFullScreen />
    </div> : <div className={styles.unavailable} role="status">
      <strong>{image ? "Image preview unavailable" : "Video preview unavailable"}</strong>
      <p>{generated ? "The generated context video could not be loaded. The matched recording remains available separately." : original ? "Open the original post to view this recording." : "No playable media or public post link is available."}</p>
    </div>}
    <div className={styles.footer}>
      {original && <a href={original} target="_blank" rel="noopener noreferrer">
        {youtube ? "Open on YouTube ↗" : "Open original post ↗"}
      </a>}
      {!direct && youtube && <p>Hosted by YouTube. If playback is restricted, open the video on YouTube.</p>}
    </div>
  </section></div>;
}
