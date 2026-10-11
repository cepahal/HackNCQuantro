"use client";

import { useEffect, useRef, useState } from "react";

const stages = ["Retrieving the media", "Transcribing the speech", "Searching for the source", "Aligning passages", "Building your report"];

function SphereCanvas() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;

    const count = 112;
    const points = Array.from({ length: count }, (_, index) => {
      const y = 1 - (index / (count - 1)) * 2;
      const radius = Math.sqrt(1 - y * y);
      const angle = Math.PI * (3 - Math.sqrt(5)) * index;
      return { x: Math.cos(angle) * radius, y, z: Math.sin(angle) * radius };
    });
    const links: [number, number][] = [];
    for (let i = 0; i < count; i++) {
      for (let j = i + 1; j < count; j++) {
        const dx = points[i].x - points[j].x;
        const dy = points[i].y - points[j].y;
        const dz = points[i].z - points[j].z;
        if (dx * dx + dy * dy + dz * dz < 0.19) links.push([i, j]);
      }
    }

    let width = 0;
    let height = 0;
    let frame = 0;
    let lastDraw = 0;
    let pointer: { x: number; y: number } | null = null;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      width = bounds.width;
      height = bounds.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const onPointerMove = (event: PointerEvent) => {
      const bounds = canvas.getBoundingClientRect();
      pointer = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
    };
    const onPointerLeave = () => { pointer = null; };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    canvas.addEventListener("pointermove", onPointerMove, { passive: true });
    canvas.addEventListener("pointerleave", onPointerLeave, { passive: true });
    resize();

    const draw = (now: number) => {
      frame = requestAnimationFrame(draw);
      if (document.hidden || now - lastDraw < 32 || width === 0) return;
      lastDraw = now;
      const spin = reducedMotion.matches ? 0.35 : now * 0.00022;
      const tilt = -0.18;
      const sin = Math.sin(spin);
      const cos = Math.cos(spin);
      const projected = points.map(point => {
        const x = point.x * cos - point.z * sin;
        const z = point.x * sin + point.z * cos;
        const y = point.y * Math.cos(tilt) - z * Math.sin(tilt);
        const depth = point.y * Math.sin(tilt) + z * Math.cos(tilt);
        let px = width / 2 + x * width * 0.39;
        let py = height / 2 + y * height * 0.39;
        if (pointer) {
          const dx = px - pointer.x;
          const dy = py - pointer.y;
          const distance = Math.hypot(dx, dy);
          const influence = Math.max(0, 1 - distance / 100);
          if (distance > 0) {
            px += (dx / distance) * influence * 18;
            py += (dy / distance) * influence * 18;
          }
        }
        return { x: px, y: py, z: depth };
      });

      context.clearRect(0, 0, width, height);
      context.lineWidth = 0.7;
      for (const [a, b] of links) {
        const first = projected[a];
        const second = projected[b];
        context.strokeStyle = `rgba(231,53,82,${0.13 + Math.max(0, (first.z + second.z) / 2) * 0.15})`;
        context.beginPath();
        context.moveTo(first.x, first.y);
        context.lineTo(second.x, second.y);
        context.stroke();
      }
      for (const point of projected) {
        context.beginPath();
        context.fillStyle = `rgba(231,53,82,${0.48 + Math.max(0, point.z) * 0.48})`;
        context.arc(point.x, point.y, 1.15 + Math.max(0, point.z) * 0.85, 0, Math.PI * 2);
        context.fill();
      }
    };
    frame = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerleave", onPointerLeave);
    };
  }, []);

  return <canvas ref={canvasRef} aria-hidden="true" />;
}

export default function AnalysisLoadingScreen({ complete = false, onDone }: { complete?: boolean; onDone?: () => void }) {
  const [fading, setFading] = useState(false);
  const doneRef = useRef(false);
  useEffect(() => {
    if (!complete) return;
    const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 780;
    const timer = window.setTimeout(() => setFading(true), delay);
    return () => window.clearTimeout(timer);
  }, [complete]);
  useEffect(() => {
    if (!fading || doneRef.current) return;
    const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 520;
    const timer = window.setTimeout(() => {
      doneRef.current = true;
      onDone?.();
    }, delay);
    return () => window.clearTimeout(timer);
  }, [fading, onDone]);

  return <div className={`analysis-loading${fading ? " is-fading" : ""}`} role="status" aria-label={complete ? "Analysis complete" : "Analysis in progress"} aria-busy={!complete}>
    <div className={`analysis-loading__sphere${complete ? " is-collapsing" : ""}`}><SphereCanvas /></div>
    <section className={`analysis-loading__plaque${complete ? " is-leaving" : ""}`} aria-hidden={complete}>
      <p className="analysis-loading__eyebrow">{complete ? "ANALYSIS COMPLETE" : "ANALYSIS IN PROGRESS"}</p>
      <h2>{complete ? "Your report is ready." : "Finding the source."}</h2>
      <p className="analysis-loading__copy">We’re following the media and checking its surrounding context.</p>
      <ol>{stages.map((stage, index) => <li key={stage}><span aria-hidden="true">{complete ? "✓" : String(index + 1).padStart(2, "0")}</span>{stage}</li>)}</ol>
    </section>
  </div>;
}
