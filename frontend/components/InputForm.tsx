"use client";
import { useState, type FormEvent } from "react";

export default function InputForm({ busy, onSubmit }: { busy: boolean; onSubmit: (url: string) => Promise<void> }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let parsed: URL;
    try { parsed = new URL(url.trim()); } catch { setError("Enter a complete public post URL."); return; }
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
      setError("Enter a public HTTP or HTTPS URL without credentials."); return;
    }
    setError("");
    await onSubmit(parsed.href);
  }
  return <form onSubmit={submit}><label htmlFor="post-url">Public post or video URL</label>
    <div className="input-row"><input id="post-url" type="url" required value={url} maxLength={8192}
      onChange={(event) => setUrl(event.target.value)} placeholder="https://…" disabled={busy}/>
      <button disabled={busy} type="submit">{busy ? "Analyzing…" : "Check context"}</button></div>
    {error && <p role="alert" className="error">{error}</p>}
  </form>;
}
