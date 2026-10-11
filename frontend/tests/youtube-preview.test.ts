import assert from "node:assert/strict";
import test from "node:test";
import { youtubePreviewUrl } from "../lib/youtube-preview.ts";

test("known YouTube link formats become a fixed-host player URL", () => {
  for (const url of [
    "https://www.youtube.com/watch?v=TCm9788Tb5g",
    "https://youtube.com/watch?v=TCm9788Tb5g&t=3&feature=share",
    "https://m.youtube.com/watch?v=TCm9788Tb5g",
    "https://youtu.be/TCm9788Tb5g?si=tracking",
    "https://www.youtube.com/shorts/TCm9788Tb5g",
    "https://www.youtube.com/live/TCm9788Tb5g",
    "https://www.youtube-nocookie.com/embed/TCm9788Tb5g",
  ]) assert.equal(youtubePreviewUrl(url), "https://www.youtube-nocookie.com/embed/TCm9788Tb5g?playsinline=1&rel=0");
});

test("invalid, ambiguous, credentialed and lookalike URLs cannot create embeds", () => {
  for (const url of [null, undefined, "", "not-a-url", "javascript:alert(1)",
    "//youtube.com/watch?v=TCm9788Tb5g", "http://youtube.com/watch?v=TCm9788Tb5g",
    "https://youtube.com.evil.test/watch?v=TCm9788Tb5g", "https://youtube.com@evil.test/watch?v=TCm9788Tb5g",
    "https://user:password@youtube.com/watch?v=TCm9788Tb5g", "https://youtube.com:8443/watch?v=TCm9788Tb5g",
    "https://www.youtube.com/watch?v=TCm9788Tb5g&v=pJvRUL81ZU8", "https://youtube.com/playlist?list=TCm9788Tb5g",
    "https://youtu.be/TCm9788Tb5g/extra", "https://youtu.be/TCm9788Tb5g ",
    "https://www.youtube.com/watch?v=%3Cscript%3E", "https://example.com/video.mp4",
  ]) assert.equal(youtubePreviewUrl(url), null, String(url));
});
