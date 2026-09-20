import type { ReactElement } from 'react';

/** Code-native botanical motif; decorative, never a loading or status signal. */
export function QuietMark({ className }: { className?: string }): ReactElement {
  return <svg className={className} viewBox="0 0 280 320" fill="none" aria-hidden="true">
    <circle cx="142" cy="142" r="103" stroke="currentColor" opacity=".16" />
    <circle cx="142" cy="142" r="80" stroke="currentColor" opacity=".12" />
    <path d="M52 287c49-25 88-90 99-193M111 215c-45 4-61-23-56-45 30-1 54 12 56 45ZM137 166c-34-4-47-30-39-48 26 4 42 22 39 48ZM147 128c37-7 56-30 48-51-28 4-46 25-48 51ZM126 193c46 1 70-17 68-43-33-3-59 13-68 43Z" stroke="currentColor" strokeWidth="1.4" />
    <path d="M151 94c-23-15-30-46-17-64 23 14 30 41 17 64Z" fill="currentColor" opacity=".18" />
    <path d="M36 257h196M36 264h196M36 271h196M36 278h196" stroke="currentColor" opacity=".1" />
    <circle cx="224" cy="58" r="3" fill="currentColor" opacity=".45" />
  </svg>;
}
