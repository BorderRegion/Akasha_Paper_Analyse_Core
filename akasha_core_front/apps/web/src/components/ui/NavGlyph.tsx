import type { ReactElement } from 'react';

const paths: Record<string, string> = {
  home: 'M3 10 12 3l9 7M5 9v12h14V9M9 21v-8h6v8',
  library: 'M4 4h4v16H4zM10 4h4v16h-4zM17 4l4 15-4 1-4-15',
  collections: 'M3 7h7l2-3h8v16H3zM7 12h9M7 16h6',
  techniques: 'M8 4c-5 3-5 9 0 12v4h8v-4c5-3 5-9 0-12M9 17h6M12 8v6M9 11h6',
  review: 'M8 3h8v4H8zM7 5H4v16h16V5h-3M8 13l3 3 5-6',
  operations: 'M3 12h4l3-7 4 14 3-7h4',
  settings: 'M4 7h16M4 17h16M9 4v6M15 14v6',
};
export function NavGlyph({ name }: { name: string }): ReactElement {
  return <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.home} /></svg>;
}
