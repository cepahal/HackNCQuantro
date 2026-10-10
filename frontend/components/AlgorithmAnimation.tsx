"use client";

import { useEffect, useRef, useState } from "react";
import { useAnimate, useInView, useReducedMotion } from "framer-motion";

const captions = [
  "Searching for the source.",
  "Original found.",
  "Mapping each clip to its timestamp.",
  "Matching the missing moments.",
  "Adding the missing context back.",
];
const markerPositions = [39, 44, 53, 59, 62];
const fragmentOffsets = [[0, 0], [14, 0], [7, 7], [0, 14], [14, 14]];
const gapSegments = markerPositions.slice(0, -1).map((position, index) => {
  const distance = markerPositions[index + 1] - position;
  return { left: position + distance / 4, width: distance / 2 };
});

export default function AlgorithmAnimation() {
  const [scope, animate] = useAnimate();
  const view = useInView(scope, { once: false, amount: 0.25 });
  const reduced = useReducedMotion();
  const [step, setStep] = useState(0);
  const [replay, setReplay] = useState(0);
  const running = useRef(false);

  useEffect(() => {
    if (!view || reduced || running.current) return;
    running.current = true;
    let cancelled = false;
    const pause = (duration: number) => new Promise<void>((resolve) => window.setTimeout(resolve, duration));
    const mobile = window.matchMedia("(max-width: 600px)").matches;
    const cellPitch = mobile ? 26 : 36;
    const sourceOffset = mobile ? 35 : 54;
    const rail = mobile ? { left: "14%", width: "72%" } : { left: "22%", width: "56%" };
    const stageWidth = scope.current?.clientWidth ?? window.innerWidth;
    const stageHeight = scope.current?.clientHeight ?? 440;
    const targetStartX = stageWidth * 0.43 + cellPitch * 4;
    const targetStartY = stageHeight * 0.25 + cellPitch;
    const targetEndX = stageWidth * 0.5 - 14;
    const targetEndY = stageHeight * 0.65 - 14;
    const gridWidth = (mobile ? 20 : 28) * 5 + (mobile ? 6 : 8) * 4;
    const outsideGridX = stageWidth * 0.43 + gridWidth + 22;
    const curvePoints = [
      [targetStartX, targetStartY],
      [outsideGridX, targetStartY],
      [outsideGridX, targetEndY],
      [targetEndX, targetEndY],
    ];
    const curve = Array.from({ length: 17 }, (_, index) => {
      const t = index / 16;
      const inverse = 1 - t;
      return curvePoints[0].map((_, axis) =>
        inverse ** 3 * curvePoints[0][axis] +
        3 * inverse ** 2 * t * curvePoints[1][axis] +
        3 * inverse * t ** 2 * curvePoints[2][axis] +
        t ** 3 * curvePoints[3][axis],
      );
    });

    const run = async () => {
      while (!cancelled) {
        await Promise.all([
          animate(".stage-grid", { opacity: 1 }, { duration: 0 }),
          animate(".stage-source", { x: 0, y: 0, scale: 1, opacity: 1 }, { duration: 0 }),
          animate(".stage-target", {
            left: `calc(43% + ${cellPitch * 4}px)`, top: `calc(25% + ${cellPitch}px)`, width: 28, height: 28,
            x: 0, y: 0, scaleX: 1, scaleY: 1, borderRadius: 7,
            backgroundColor: "#101116", opacity: 1,
          }, { duration: 0 }),
          ...fragmentOffsets.map(([x, y], index) =>
            animate(`.stage-fragment-${index}`, {
              left: `calc(43% - 139px + ${x}px)`, top: `calc(25% + ${sourceOffset + 1 + y}px)`,
              scale: 0, opacity: 0,
            }, { duration: 0 }),
          ),
          animate(".stage-gap", { y: 0, opacity: 0 }, { duration: 0 }),
        ]);

        if (cancelled) break;
        setStep(0);
        await animate(".stage-source", { y: [-7, 7, -7, 7, 0] }, { duration: 1.8, ease: "easeInOut" });
        await pause(250);

        if (cancelled) break;
        setStep(1);
        await Promise.all([
          animate(".stage-target", { backgroundColor: "#e73552" }, { duration: 0.3 }),
          animate(".stage-source", { y: 0 }, { duration: 0.3 }),
        ]);
        await pause(500);
        if (cancelled) break;
        await animate(".stage-target", {
          x: curve.map(([x]) => x - targetStartX),
          y: curve.map(([, y]) => y - targetStartY),
        }, { duration: 1.45, ease: "linear" });
        await pause(350);

        if (cancelled) break;
        setStep(2);
        await Promise.all([
          animate(".stage-grid", { opacity: 0 }, { duration: 0.45 }),
          animate(".stage-target", {
            scaleX: (stageWidth * Number.parseFloat(rail.width) / 100) / 28,
            scaleY: 0.5,
            borderRadius: 7, backgroundColor: "#d0d3dc",
          }, { duration: 0.85, ease: "easeInOut" }),
          animate(".stage-source", { scale: 0.35, opacity: 0 }, { duration: 0.45 }),
          ...markerPositions.map((_, index) =>
            animate(`.stage-fragment-${index}`, { scale: 1, opacity: 1 }, { duration: 0.45, delay: 0.04 * index }),
          ),
        ]);
        await pause(300);

        if (cancelled) break;
        setStep(3);
        await Promise.all(markerPositions.map((position, index) =>
          animate(`.stage-fragment-${index}`, {
            left: `calc(${position}% - 6px)`, top: "calc(65% - 20px)",
          }, { duration: 0.9, ease: "easeInOut", delay: index * 0.06 }),
        ));
        await pause(350);

        if (cancelled) break;
        setStep(4);
        await Promise.all(gapSegments.map((_, index) =>
          animate(`.stage-gap-${index}`, { y: -14, opacity: 1 }, {
            duration: 0.55, ease: "easeOut", delay: index * 0.1,
          }),
        ));
        await pause(1800);
      }
    };

    void run();
    return () => {
      cancelled = true;
      running.current = false;
    };
  }, [view, reduced, animate, replay, scope]);

  return (
    <div>
      <div className="hatch">
        <div className="algorithm-stage" ref={scope} aria-hidden="true">
          <div className="stage-grid">
            {Array.from({ length: 20 }, (_, index) => (
              <i className={`stage-cell${index === 9 ? " stage-cell-target-space" : ""}`} key={index} />
            ))}
          </div>
          <div className="stage-target" />
          <div className="stage-source" />
          {gapSegments.map((segment, index) => (
            <div
              className={`stage-gap stage-gap-${index}`}
              key={`gap-${index}`}
              style={{ left: `${segment.left}%`, width: `${segment.width}%` }}
            />
          ))}
          {markerPositions.map((_, index) => (
            <div className={`stage-fragment stage-fragment-${index}`} key={`fragment-${index}`} />
          ))}
          <div className="stage-status">SOURCE ALIGNMENT · {step + 1}/5</div>
        </div>
      </div>
      <p className="stage-caption" aria-live="polite">{captions[step]}</p>
      <button className="btn btn-outline" onClick={() => { setStep(0); setReplay((value) => value + 1); }}>Replay</button>
      <p className="sr-only">A source clip is found, aligned to a timeline, and separated into gaps so missing context can be restored.</p>
    </div>
  );
}
