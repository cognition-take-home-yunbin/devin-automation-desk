import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function Base({ size = 18, children, ...rest }: P) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...rest}
    >
      {children}
    </svg>
  );
}

export const Icon = {
  Logo: (p: P) => (
    <Base {...p} strokeWidth={2.2}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5a4.5 4.5 0 1 0 4.5 4.5" />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    </Base>
  ),
  Bolt: (p: P) => (
    <Base {...p}>
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
    </Base>
  ),
  Alert: (p: P) => (
    <Base {...p}>
      <path d="M12 3 2.5 20h19L12 3z" />
      <path d="M12 9v5" />
      <circle cx="12" cy="17" r=".6" fill="currentColor" />
    </Base>
  ),
  Check: (p: P) => (
    <Base {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12.5 2.3 2.3 4.7-5" />
    </Base>
  ),
  Gauge: (p: P) => (
    <Base {...p}>
      <path d="M4 15a8 8 0 1 1 16 0" />
      <path d="M12 15l3.5-4.5" />
      <circle cx="12" cy="15" r="1.2" fill="currentColor" stroke="none" />
    </Base>
  ),
  Wallet: (p: P) => (
    <Base {...p}>
      <rect x="3" y="6" width="18" height="13" rx="3" />
      <path d="M3 10h18" />
      <circle cx="16.5" cy="14.5" r="1" fill="currentColor" stroke="none" />
    </Base>
  ),
  Branch: (p: P) => (
    <Base {...p}>
      <circle cx="6" cy="5" r="2.2" />
      <circle cx="6" cy="19" r="2.2" />
      <circle cx="18" cy="8" r="2.2" />
      <path d="M6 7.2v9.6M18 10.2c0 3-2.5 4.3-6 4.8-3 .4-6 .9-6 1.8" />
    </Base>
  ),
  Session: (p: P) => (
    <Base {...p}>
      <rect x="3" y="4" width="18" height="14" rx="3" />
      <path d="m7.5 9 2.5 2.5L7.5 14M12.5 14h4" />
    </Base>
  ),
  Slack: (p: P) => (
    <Base {...p}>
      <path d="M8 3v6M16 15v6M3 16h6M15 8h6" />
      <rect x="5" y="9" width="6" height="6" rx="1.5" />
      <rect x="13" y="9" width="6" height="6" rx="1.5" />
    </Base>
  ),
  Refresh: (p: P) => (
    <Base {...p}>
      <path d="M20 12a8 8 0 1 1-2.3-5.7" />
      <path d="M20 4v4.5h-4.5" />
    </Base>
  ),
  Search: (p: P) => (
    <Base {...p}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m20 20-3.8-3.8" />
    </Base>
  ),
  Trash: (p: P) => (
    <Base {...p}>
      <path d="M4 7h16M9 7V4.5h6V7M6.5 7l.8 12h9.4l.8-12" />
    </Base>
  ),
  External: (p: P) => (
    <Base {...p}>
      <path d="M7 17 17 7M9 7h8v8" />
    </Base>
  ),
  Close: (p: P) => (
    <Base {...p}>
      <path d="m6 6 12 12M18 6 6 18" />
    </Base>
  ),
  Pause: (p: P) => (
    <Base {...p}>
      <rect x="6" y="4" width="4" height="16" rx="1.2" />
      <rect x="14" y="4" width="4" height="16" rx="1.2" />
    </Base>
  ),
  Radar: (p: P) => (
    <Base {...p}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4.5" />
      <path d="M12 12 18.5 6" />
    </Base>
  ),
  Doc: (p: P) => (
    <Base {...p}>
      <path d="M7 3h7l4 4v14H7z" />
      <path d="M14 3v4h4M10 12h5M10 16h5" />
    </Base>
  ),
  Sparkle: (p: P) => (
    <Base {...p}>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18" />
    </Base>
  ),
  Flask: (p: P) => (
    <Base {...p}>
      <path d="M9 3h6M10 3v6L4.5 18.5A2 2 0 0 0 6.3 21h11.4a2 2 0 0 0 1.8-2.5L14 9V3" />
      <path d="M7.5 15h9" />
    </Base>
  ),
  Inbox: (p: P) => (
    <Base {...p}>
      <path d="M4 13h4l1.5 2.5h5L16 13h4" />
      <path d="M5 6h14l2 7v5H3v-5z" />
    </Base>
  ),
};
