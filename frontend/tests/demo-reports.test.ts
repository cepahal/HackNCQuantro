import assert from "node:assert/strict";
import test from "node:test";
import { demoJobForUrl, demoReportPath, isDemoJob } from "../lib/demo-reports.ts";

test("demo links match by YouTube video ID across URL variants", () => {
  for (const [id, urls] of Object.entries({
    qzYTN4lfEn4: ["https://www.youtube.com/shorts/qzYTN4lfEn4", "https://youtu.be/qzYTN4lfEn4?si=x", "https://www.youtube.com/watch?v=qzYTN4lfEn4&t=2"],
    tSj9gkgCM8Q: ["https://www.youtube.com/watch?v=tSj9gkgCM8Q", "https://m.youtube.com/shorts/tSj9gkgCM8Q?feature=share"],
    "3XPuGqqWLtk": ["https://youtube.com/shorts/3XPuGqqWLtk", " https://youtu.be/3XPuGqqWLtk "],
  })) for (const url of urls) {
    const job = demoJobForUrl(url);
    assert.ok(job?.includes(id), url);
    assert.equal(isDemoJob(job!), true);
    assert.equal(demoReportPath(`/reports/report_${job}/display`), `/demo/${id}/display.json`);
  }
});

test("other links keep the normal analysis path", () => {
  for (const url of ["https://www.youtube.com/watch?v=TCm9788Tb5g", "https://example.com/shorts/qzYTN4lfEn4", "not a url"])
    assert.equal(demoJobForUrl(url), null, url);
  assert.equal(isDemoJob("job_123"), false);
  assert.equal(demoReportPath("/reports/report_job_123"), null);
});
