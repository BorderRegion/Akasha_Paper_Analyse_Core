import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const webRoot = process.cwd();
const repoRoot = resolve(webRoot, '../..');

function readJson<T>(path: string): T | null {
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, 'utf8')) as T;
}

describe('UX-058 the build is reproducible from the lockfile', () => {
  it('ships a single npm lockfile and pins the Node version', () => {
    expect(existsSync(resolve(webRoot, 'package-lock.json'))).toBe(true);
    expect(existsSync(resolve(webRoot, 'pnpm-lock.yaml'))).toBe(false);
    expect(existsSync(resolve(webRoot, 'yarn.lock'))).toBe(false);
    // The Node version is pinned with the app (apps/web/.nvmrc), next to the
    // lockfile it belongs to.
    const nvmrc = readFileSync(resolve(webRoot, '.nvmrc'), 'utf8').trim();
    const pkg = readJson<{ engines?: { node?: string; npm?: string } }>(
      resolve(webRoot, 'package.json'),
    );
    expect(nvmrc).toMatch(/^20\./);
    expect(pkg?.engines?.node).toBeTruthy();
    expect(pkg?.engines?.npm).toBeTruthy();
  });

  it('has no @latest or floating version in the lockfile root dependencies', () => {
    const pkg = readJson<{ dependencies: Record<string, string>; devDependencies: Record<string, string> }>(
      resolve(webRoot, 'package.json'),
    );
    const all = { ...(pkg?.dependencies ?? {}), ...(pkg?.devDependencies ?? {}) };
    for (const [name, version] of Object.entries(all)) {
      expect(version, `${name} must be pinned exactly`).toMatch(/^\d+\.\d+\.\d+$/);
    }
  });

  it('produces a production build under base /app', () => {
    const dist = resolve(webRoot, 'dist');
    expect(existsSync(resolve(dist, 'index.html'))).toBe(true);
    const html = readFileSync(resolve(dist, 'index.html'), 'utf8');
    // Assets are referenced from /app/ so a deep link under /app resolves them.
    expect(html).toMatch(/\/app\/assets\//);
    const assets = readdirSync(resolve(dist, 'assets'));
    expect(assets.some((name) => name.endsWith('.js'))).toBe(true);
    expect(assets.some((name) => name.endsWith('.css'))).toBe(true);
  });
});

describe('UX-059 production deep links and API 404s', () => {
  it('keeps the SPA shell free of hard-coded API hosts', () => {
    const html = readFileSync(resolve(webRoot, 'dist/index.html'), 'utf8');
    expect(html).not.toMatch(/http:\/\/127\.0\.0\.1:8420/);
    expect(html).not.toMatch(/localhost:3000/);
  });

  it('routes every documented page under /app so a refresh finds the shell', () => {
    const routes = ['home', 'library', 'papers/:paperId', 'review', 'collections', 'techniques', 'compare', 'operations', 'settings'];
    const source = readFileSync(resolve(webRoot, 'src/app/App.tsx'), 'utf8');
    for (const route of routes) {
      expect(source, `route ${route} missing from the router`).toContain(`path: '${route}'`);
    }
    // The backend serves the shell for any /app/* path (asserted in
    // tests/integration/ui_contracts/test_deployment_spa.py).
    expect(existsSync(resolve(repoRoot, '../akasha_core/tests/integration/ui_contracts/test_deployment_spa.py'))).toBe(true);
  });

  it('never ships a fixture fallback into the production bundle', () => {
    const assets = readdirSync(resolve(webRoot, 'dist/assets')).filter((name) => name.endsWith('.js'));
    for (const name of assets) {
      const content = readFileSync(resolve(webRoot, 'dist/assets', name), 'utf8');
      expect(content, `${name} mentions MSW`).not.toMatch(/msw|mockServiceWorker/i);
      expect(content, `${name} mentions fixture fallback`).not.toMatch(/FIXTURE_FALLBACK/);
    }
  });
});

