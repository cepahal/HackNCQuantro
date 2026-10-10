"use client";

import { useEffect, useRef } from "react";

export default function PointerCursor() {
  const cursorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const cursor = cursorRef.current;
    if (!cursor || !window.matchMedia("(pointer: fine)").matches || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let frame = 0;
    let x = 0;
    let y = 0;
    const move = (event: PointerEvent) => {
      x = event.clientX;
      y = event.clientY;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        cursor.style.transform = `translate3d(${x - 1}px, ${y - 1}px, 0)`;
        cursor.classList.add("is-visible");
        frame = 0;
      });
    };
    const hide = () => cursor.classList.remove("is-visible");
    window.addEventListener("pointermove", move, { passive: true });
    document.documentElement.addEventListener("pointerleave", hide);
    return () => {
      window.removeEventListener("pointermove", move);
      document.documentElement.removeEventListener("pointerleave", hide);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return <div className="custom-cursor" ref={cursorRef} aria-hidden="true"><svg className="cursor-pointer" viewBox="0 0 24 32"><path d="M2 1v25l6-6 5 10 4-2-5-10h9L2 1Z"/></svg></div>;
}
