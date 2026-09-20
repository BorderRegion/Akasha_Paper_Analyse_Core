/**
 * UX-023 — one failed import never clears the successful ones.
 *
 * Requirement (docs/03 §S03): 混合批次中一项失败不清空其他成功项；只重试失败项，
 * 绑定同一幂等键; duplicate files offer "打开已有论文" instead of creating a copy;
 * "upload finished" is not "analysis finished"; limits come from capabilities.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { ImportDialog } from '../../src/features/import/ImportDialog';
import { Providers } from '../../src/app/providers';
import { ApiClient } from '../../src/api/client';
import { createFakeServer } from '../support/fake-server';

function pdf(name: string, size = 2048): File {
  const content = new Uint8Array(size);
  content.set([0x25, 0x50, 0x44, 0x46, 0x2d]); // %PDF-
  return new File([content], name, { type: 'application/pdf' });
}

function renderDialog(server: ReturnType<typeof createFakeServer>) {
  const client = new ApiClient({ mode: 'TEST', fetchImpl: server.fetch });
  return render(
    <Providers client={client}>
      <ImportDialog open onClose={() => undefined} />
    </Providers>,
  );
}

async function pickFiles(names: string[]): Promise<void> {
  const input = screen.getByLabelText('选择文件');
  const user = userEvent.setup();
  await user.upload(input, names.map((name) => pdf(name)));
}

describe('UX-023 import keeps successful items when one fails', () => {
  beforeEach(() => {
    window.history.pushState({}, '', '/app/library');
  });

  it('shows the server limits from capabilities instead of a UI constant', async () => {
    const server = createFakeServer({
      limits: {
        upload_file_bytes: 1024,
        upload_batch_bytes: 4096,
        upload_files: 3,
        library_page_size: 50,
      },
    });
    renderDialog(server);
    expect(await screen.findByText(/单文件 1\.0 KiB/)).toBeInTheDocument();
    expect(screen.getByText(/每批 3 个/)).toBeInTheDocument();
  });

  it('keeps the imported files when another file fails, and retries only the failure', async () => {
    const server = createFakeServer();
    server.failUploadFor.add('broken.pdf');
    renderDialog(server);

    await pickFiles(['good-one.pdf', 'broken.pdf', 'good-two.pdf']);

    await waitFor(() => expect(screen.getAllByTestId('import-row-IMPORTED')).toHaveLength(2));
    const failed = screen.getByTestId('import-row-FAILED');
    expect(within(failed).getByText('不是有效的 PDF 或文件已损坏')).toBeInTheDocument();
    expect(screen.getByTestId('failed-note')).toHaveTextContent('1 个文件失败，其他 2 个不受影响');

    // Retry only the failed file; it succeeds this time.
    server.failUploadFor.clear();
    await userEvent.setup().click(screen.getByRole('button', { name: '只重试失败文件' }));
    await waitFor(() => expect(screen.getAllByTestId('import-row-IMPORTED')).toHaveLength(3));

    // The retry reused the SAME idempotency key: no second item for one file.
    const brokenUploads = server.uploads.filter((upload) => upload.filename === 'broken.pdf');
    expect(brokenUploads).toHaveLength(2);
    expect(brokenUploads[0]?.idempotencyKey).toBe(brokenUploads[1]?.idempotencyKey);
    expect(new Set(server.uploads.map((upload) => upload.idempotencyKey)).size).toBe(3);
  });

  it('never presents "upload finished" as "analysis finished"', async () => {
    const server = createFakeServer();
    renderDialog(server);
    await pickFiles(['one.pdf']);

    await waitFor(() => expect(screen.getByTestId('import-row-IMPORTED')).toBeInTheDocument());
    expect(screen.getByText(/上传后还需要解析和分析/)).toBeInTheDocument();
    expect(within(screen.getByTestId('import-row-IMPORTED')).getByText(/已排队解析/)).toBeInTheDocument();
  });

  it('offers to open an existing paper for a duplicate instead of copying it', async () => {
    const server = createFakeServer();
    // The fake server reports DUPLICATE for a file the server already knows.
    const original = server.fetch;
    server.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await original(input as never, init);
      if (!String(input).includes('/files')) return response;
      const body = (await response.clone().json()) as { data?: Record<string, unknown> };
      return new Response(
        JSON.stringify({
          data: { ...body.data, state: 'DUPLICATE', paper_id: 'pap_existing' },
          meta: { contract_version: '1.0.0', snapshot_id: 's', observed_at: 'now', partial: false, warnings: [] },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake' } },
      );
    }) as unknown as typeof fetch;

    renderDialog(server);
    await pickFiles(['already-there.pdf']);
    const row = await screen.findByTestId('import-row-DUPLICATE');
    expect(within(row).getByRole('link', { name: /打开已有论文/ })).toHaveAttribute(
      'href',
      '/app/papers/pap_existing',
    );
  });

  it('refuses an over-limit file before sending any bytes', async () => {
    const server = createFakeServer({
      limits: {
        upload_file_bytes: 512,
        upload_batch_bytes: 4096,
        upload_files: 5,
        library_page_size: 50,
      },
    });
    renderDialog(server);
    await pickFiles(['huge.pdf']); // 2048 bytes > 512 limit

    const row = await screen.findByTestId('import-row-FAILED');
    expect(within(row).getByText(/超出体积上限/)).toBeInTheDocument();
    expect(server.uploads, 'an over-limit file must not reach the server').toHaveLength(0);
  });

  it('uploads at most two files at a time', async () => {
    const server = createFakeServer();
    const inFlight = new Set<number>();
    let peak = 0;
    const original = server.fetch;
    server.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!String(input).includes('/files')) return original(input as never, init);
      const marker = Date.now() + Math.random();
      inFlight.add(marker);
      peak = Math.max(peak, inFlight.size);
      await new Promise((resolve) => setTimeout(resolve, 20));
      const response = await original(input as never, init);
      inFlight.delete(marker);
      return response;
    }) as unknown as typeof fetch;

    renderDialog(server);
    await pickFiles(['a.pdf', 'b.pdf', 'c.pdf', 'd.pdf']);
    await waitFor(() => expect(screen.getAllByTestId('import-row-IMPORTED')).toHaveLength(4), {
      timeout: 4000,
    });
    expect(peak).toBeLessThanOrEqual(2);
  });

  it('opens the dialog from the library page and keeps it reachable', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    render(<App mode="TEST" fetchImpl={server.fetch} />);
    await screen.findByRole('heading', { name: '文献库' });
    expect(screen.queryByRole('dialog', { name: '导入 PDF' })).not.toBeInTheDocument();
  });
});
