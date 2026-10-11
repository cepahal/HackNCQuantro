/** Extract the video ID from a known fixed-host YouTube link; never from arbitrary report URLs. */
export function youtubeVideoId(value: string | null | undefined): string | null {
  if (!value || value !== value.trim() || /\s/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
    let id: string | undefined;
    if (url.hostname === "youtu.be") {
      const parts = url.pathname.split("/");
      if (parts.length !== 2) return null;
      id = parts[1];
    } else if (["youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"].includes(url.hostname)) {
      if (url.pathname === "/watch") {
        const ids = url.searchParams.getAll("v");
        if (ids.length !== 1) return null;
        id = ids[0];
      } else {
        const match = /^\/(?:shorts|embed|live)\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname);
        id = match?.[1];
      }
    }
    return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
  } catch { return null; }
}

/** Construct a fixed-host player URL; never embed arbitrary report URLs. */
export function youtubePreviewUrl(value: string | null | undefined): string | null {
  const id = youtubeVideoId(value);
  return id ? `https://www.youtube-nocookie.com/embed/${id}?playsinline=1&rel=0` : null;
}
