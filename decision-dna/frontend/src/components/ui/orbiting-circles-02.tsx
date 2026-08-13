"use client";

import React from "react";
import { Mail, CalendarDays, Ticket, Brain, Network, GitBranch, MessagesSquare, Scale, type LucideIcon } from "lucide-react";
import ParticleSphereAnimation from "@/components/ui/orbiting-circles-02-utils/particalsphear";

/**
 * Orbiting rings around the particle globe represent DecisionDNA's actual
 * pipeline rather than generic integration logos: raw sources feed in on
 * the inner ring, the AI + graph reasoning layer sits in the middle, and
 * the reconstructed decision trail orbits on the outside.
 */
const orbits: {
  size: string;
  duration: number;
  icons: { icon: LucideIcon; label: string; angle: number }[];
}[] = [
  {
    size: "w-110 h-110 md:w-180 md:h-180",
    duration: 18,
    icons: [
      { icon: Mail, label: "Emails", angle: -60 },
      { icon: CalendarDays, label: "Meeting notes", angle: 0 },
      { icon: Ticket, label: "Jira tickets", angle: 60 },
    ],
  },
  {
    size: "w-150 h-150 md:w-220 md:h-220",
    duration: 24,
    icons: [
      { icon: Brain, label: "AI reasoning", angle: 0 },
      { icon: Network, label: "Knowledge graph", angle: -90 },
    ],
  },
  {
    size: "w-180 h-180 md:w-265 md:h-265",
    duration: 30,
    icons: [
      { icon: GitBranch, label: "Decision timeline", angle: -60 },
      { icon: MessagesSquare, label: "Ask & answer", angle: 0 },
      { icon: Scale, label: "Dissent & confidence", angle: 60 },
    ],
  },
];

export default function OrbitingCirclesGlobeDemo() {
  return (
    <div className="relative w-full h-110 md:h-160 overflow-hidden flex justify-center">
      <style>{`
        @keyframes orbit-cw {
          from { transform: rotate(var(--start-angle)) }
          to   { transform: rotate(calc(var(--start-angle) + 360deg)) }
        }
        @keyframes orbit-ccw {
          from { transform: rotate(var(--start-angle)) }
          to   { transform: rotate(calc(var(--start-angle) - 360deg)) }
        }
        @keyframes counter-cw {
          from { transform: rotate(var(--counter-offset, 0deg)) }
          to   { transform: rotate(calc(var(--counter-offset, 0deg) - 360deg)) }
        }
        @keyframes counter-ccw {
          from { transform: rotate(var(--counter-offset, 0deg)) }
          to   { transform: rotate(calc(var(--counter-offset, 0deg) + 360deg)) }
        }
        @media (prefers-reduced-motion: reduce) {
          .orbit-ring [style*="--start-angle"],
          .orbit-ring [style*="--counter-offset"] {
            animation: none !important;
            transform: rotate(var(--start-angle, 0deg)) !important;
          }
        }
      `}</style>

      {/* Center particle globe */}
      <div className="absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 aspect-square pointer-events-none w-75 md:w-145 z-10">
        <ParticleSphereAnimation />
      </div>

      {/* Orbiting rings */}
      {orbits.map((orbit, index) => {
        const isCW = index % 2 === 0;
        const orbitAnim = isCW ? "orbit-cw" : "orbit-ccw";
        const counterAnim = isCW ? "counter-cw" : "counter-ccw";

        const allIcons = [
          ...orbit.icons,
          ...orbit.icons.map((ic) => ({
            ...ic,
            angle: ic.angle + 180,
            label: `${ic.label}-mirror`,
          })),
        ];

        return (
          <div
            key={index}
            className={`orbit-ring absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 rounded-full border border-border ${orbit.size}`}
          >
            {allIcons.map((iconData, iconIndex) => {
              const Icon = iconData.icon;
              return (
                <div
                  key={iconIndex}
                  className="absolute top-0 left-1/2 h-1/2 -ml-8 origin-bottom flex flex-col justify-start items-center"
                  style={
                    {
                      "--start-angle": `${iconData.angle}deg`,
                      animation: `${orbitAnim} ${orbit.duration}s linear infinite`,
                    } as React.CSSProperties
                  }
                >
                  <div
                    className="p-3 sm:p-4 border border-border rounded-full bg-background -mt-8 relative z-10"
                    title={iconData.label.replace("-mirror", "")}
                    style={
                      {
                        "--counter-offset": `${-iconData.angle}deg`,
                        animation: `${counterAnim} ${orbit.duration}s linear infinite`,
                      } as React.CSSProperties
                    }
                  >
                    <Icon
                      aria-hidden="true"
                      strokeWidth={1.75}
                      className="w-6 h-6 md:w-8 md:h-8 text-foreground"
                    />
                  </div>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
