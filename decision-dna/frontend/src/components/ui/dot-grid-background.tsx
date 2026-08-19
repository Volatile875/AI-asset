"use client";

import { useEffect, useRef } from "react";

/**
 * Animated WebGL dot-grid background, recolored to DecisionDNA's
 * dark-purple theme (--accent-purple: #8b5cf6). Loads Three.js from
 * the CDN at runtime so no bundler import is required.
 */
export default function DotGridBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let active = true;
    let renderer: any;
    let geometry: any;
    let material: any;
    let scene: any;
    let camera: any;
    let animationId: number;
    let cleanupResize: (() => void) | undefined;

    const initThree = (THREE: any) => {
      if (!canvasRef.current || !active) return;
      const canvas = canvasRef.current;
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: false });
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setSize(canvas.clientWidth, canvas.clientHeight);

      scene = new THREE.Scene();
      camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

      // Purple palette derived from the app's CSS custom properties:
      // --accent-purple (#8b5cf6), its hover shade (#7c3aed), and the
      // lighter highlight (#c084fc) used elsewhere for headings/labels.
      const hex = (h: string) => {
        const n = parseInt(h.replace("#", ""), 16);
        return new THREE.Vector3(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255);
      };

      const uniforms = {
        u_time: { value: 0 },
        u_resolution: { value: new THREE.Vector2(canvas.clientWidth * 2, canvas.clientHeight * 2) },
        // Raised opacity floor (was 0.25) so even the "dim" phase of each dot's
        // cycle still reads clearly as purple against the near-black background.
        u_opacities: { value: [0.55, 0.55, 0.6, 0.7, 0.75, 0.8, 0.88, 0.94, 1.0, 1.0] },
        u_colors: {
          value: [
            hex("#8b5cf6"), // accent-purple
            hex("#8b5cf6"),
            hex("#7c3aed"), // accent-purple-hover
            hex("#c084fc"), // highlight lavender
            hex("#8b5cf6"),
            hex("#a78bfa"),
          ],
        },
        u_total_size: { value: 22.0 },
        u_dot_size: { value: 5.5 }, // was 4.0 — bigger, more visible dots
        u_reverse: { value: 0 },
      };

      material = new THREE.ShaderMaterial({
        vertexShader: `
          precision mediump float;
          uniform vec2 u_resolution;
          out vec2 fragCoord;
          void main() {
            gl_Position = vec4(position, 1.0);
            fragCoord = (position.xy + 1.0) * 0.5 * u_resolution;
            fragCoord.y = u_resolution.y - fragCoord.y;
          }
        `,
        fragmentShader: `
          precision mediump float;
          in vec2 fragCoord;

          uniform float u_time;
          uniform float u_opacities[10];
          uniform vec3 u_colors[6];
          uniform float u_total_size;
          uniform float u_dot_size;
          uniform vec2 u_resolution;
          uniform int u_reverse;

          out vec4 fragColor;

          float PHI = 1.61803398874989484820459;
          float random(vec2 xy) {
              return fract(tan(distance(xy * PHI, xy) * 0.5) * xy.x);
          }

          void main() {
              vec2 st = fragCoord.xy;
              st.x -= abs(floor((mod(u_resolution.x, u_total_size) - u_dot_size) * 0.5));
              st.y -= abs(floor((mod(u_resolution.y, u_total_size) - u_dot_size) * 0.5));

              float opacity = step(0.0, st.x) * step(0.0, st.y);

              vec2 st2 = vec2(int(st.x / u_total_size), int(st.y / u_total_size));

              float frequency = 5.0;
              float show_offset = random(st2);
              float rand = random(st2 * floor((u_time / frequency) + show_offset + frequency));
              opacity *= u_opacities[int(rand * 10.0)];
              opacity *= 1.0 - step(u_dot_size / u_total_size, fract(st.x / u_total_size));
              opacity *= 1.0 - step(u_dot_size / u_total_size, fract(st.y / u_total_size));

              // Brightness boost so the purple reads clearly against near-black,
              // instead of the muted tone the raw palette color gives on its own.
              vec3 color = u_colors[int(show_offset * 6.0)] * 1.5;

              float animation_speed_factor = 3.0;
              vec2 center_grid = u_resolution / 2.0 / u_total_size;
              float dist_from_center = distance(center_grid, st2);

              float timing_offset_intro = dist_from_center * 0.01 + (random(st2) * 0.15);

              float current_timing_offset = timing_offset_intro;
              opacity *= step(current_timing_offset, u_time * animation_speed_factor);
              opacity *= clamp((1.0 - step(current_timing_offset + 0.1, u_time * animation_speed_factor)) * 1.25, 1.0, 1.25);

              fragColor = vec4(color, opacity);
              fragColor.rgb *= fragColor.a;
          }
        `,
        uniforms: uniforms,
        glslVersion: THREE.GLSL3,
        blending: THREE.CustomBlending,
        blendSrc: THREE.SrcAlphaFactor,
        blendDst: THREE.OneFactor,
        transparent: true,
      });

      geometry = new THREE.PlaneGeometry(2, 2);
      const mesh = new THREE.Mesh(geometry, material);
      scene.add(mesh);

      const startTime = performance.now();
      const animate = () => {
        if (!active) return;
        animationId = requestAnimationFrame(animate);
        uniforms.u_time.value = (performance.now() - startTime) / 1000.0;
        renderer.render(scene, camera);
      };
      animate();

      const handleResize = () => {
        if (!canvasRef.current) return;
        const w = canvasRef.current.clientWidth;
        const h = canvasRef.current.clientHeight;
        renderer.setSize(w, h);
        uniforms.u_resolution.value.set(w * 2, h * 2);
      };
      window.addEventListener("resize", handleResize);
      cleanupResize = () => window.removeEventListener("resize", handleResize);
    };

    if ((window as any).THREE) {
      initThree((window as any).THREE);
    } else {
      const existing = document.querySelector<HTMLScriptElement>(
        'script[data-dna-three-loader="true"]'
      );
      const onLoad = () => {
        if ((window as any).THREE) initThree((window as any).THREE);
      };
      if (existing) {
        existing.addEventListener("load", onLoad);
      } else {
        const script = document.createElement("script");
        script.src = "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js";
        script.async = true;
        script.dataset.dnaThreeLoader = "true";
        script.addEventListener("load", onLoad);
        document.head.appendChild(script);
      }
    }

    return () => {
      active = false;
      if (cleanupResize) cleanupResize();
      if (animationId) cancelAnimationFrame(animationId);
      if (renderer) renderer.dispose();
      if (geometry) geometry.dispose();
      if (material) material.dispose();
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        zIndex: 0,
      }}
    />
  );
}