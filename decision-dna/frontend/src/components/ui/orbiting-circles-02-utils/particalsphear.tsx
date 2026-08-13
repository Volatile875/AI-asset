"use client";

import { useEffect, useRef } from "react";

/**
 * ParticleSphereAnimation
 *
 * A dependency-free rotating particle globe rendered on a 2D canvas.
 * Points are distributed evenly across the sphere with a Fibonacci-sphere
 * layout, projected with simple perspective math, and painter-sorted by
 * depth each frame so the far side of the globe passes behind the near
 * side. No WebGL/three.js — this is intentionally small since it's only
 * ever shown at a few hundred pixels behind the orbiting rings.
 *
 * Honors prefers-reduced-motion by drawing a single static frame instead
 * of animating.
 */

const POINT_COUNT = 640;
const ROTATION_SPEED = 0.00022; // radians per ms

function buildSpherePoints(count: number) {
  // Fibonacci sphere: evenly distributed points, no RNG needed.
  const points: { x: number; y: number; z: number }[] = [];
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));

  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2; // 1 -> -1
    const radiusAtY = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = goldenAngle * i;
    const x = Math.cos(theta) * radiusAtY;
    const z = Math.sin(theta) * radiusAtY;
    points.push({ x, y, z });
  }
  return points;
}

export default function ParticleSphereAnimation() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const points = buildSpherePoints(POINT_COUNT);
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let width = 0;
    let height = 0;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);

    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue("--primary").trim() || "#8b5cf6";

    const resize = () => {
      const rect = container.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const ro = new ResizeObserver(resize);
    ro.observe(container);
    resize();

    const rotated = points.map((p) => ({ ...p, sx: 0, sy: 0, sz: 0, scale: 1 }));

    const draw = (angle: number) => {
      ctx.clearRect(0, 0, width, height);
      if (width === 0 || height === 0) return;

      const radius = Math.min(width, height) * 0.46;
      const cx = width / 2;
      const cy = height / 2;
      const cosA = Math.cos(angle);
      const sinA = Math.sin(angle);
      const wobble = Math.sin(angle * 0.6) * 0.12;
      const cosW = Math.cos(wobble);
      const sinW = Math.sin(wobble);

      for (let i = 0; i < points.length; i++) {
        const p = points[i];
        // rotate around Y
        const x1 = p.x * cosA + p.z * sinA;
        const z1 = -p.x * sinA + p.z * cosA;
        // slight rotate around X for a gentle wobble
        const y2 = p.y * cosW - z1 * sinW;
        const z2 = p.y * sinW + z1 * cosW;

        const perspective = 2.4 / (2.4 + z2);
        rotated[i].sx = cx + x1 * radius * perspective;
        rotated[i].sy = cy + y2 * radius * perspective;
        rotated[i].sz = z2;
        rotated[i].scale = perspective;
      }

      rotated.sort((a, b) => a.sz - b.sz);

      for (const p of rotated) {
        const depth = (p.sz + 1) / 2; // 0 (far) -> 1 (near)
        const size = 0.6 + depth * 1.6;
        const alpha = 0.12 + depth * 0.55;
        ctx.beginPath();
        ctx.fillStyle = accent;
        ctx.globalAlpha = alpha;
        ctx.arc(p.sx, p.sy, size, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    };

    if (reduceMotion) {
      draw(0.6);
      return () => ro.disconnect();
    }

    let raf = 0;
    let start = performance.now();
    const tick = (now: number) => {
      const angle = (now - start) * ROTATION_SPEED;
      draw(angle);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return (
    <div ref={containerRef} className="relative h-full w-full">
      <canvas ref={canvasRef} className="block h-full w-full" />
      <div
        className="pointer-events-none absolute inset-0 rounded-full"
        style={{
          background:
            "radial-gradient(closest-side, color-mix(in srgb, var(--primary) 22%, transparent), transparent 72%)",
        }}
      />
    </div>
  );
}
